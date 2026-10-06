"""The table-data stage: rows move with Fabric data pipelines. Mixed into ``Migrator``,
which supplies the Fabric client, the Warehouse session and the run state it uses.

Structure (tables, views, procedures) is created with T-SQL by the Warehouse
stage. Data is loaded by a generated data pipeline, one per wave: the first
table of a wave that reaches this stage gathers every pending table of the
same wave, works out which still need rows, creates (or reuses) that wave's
pipeline, and, unless the run only creates pipelines, runs it and checks the
row counts. Each table keeps its own row in the run, so the result is reported
table by table.

Handlers return ``(status, step, target, notes)`` or raise ``MigrationError`` /
``FabricApiError``, exactly like the handlers in the runner.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from discovery_agent.migration import datacopy, datapipeline, fabric_connections
from discovery_agent.migration.common import (
    COMPLETED, DATA, DEFERRED_STATUS, FAILED, IN_PROGRESS, PENDING, SKIPPED, MigrationError, Source,
)
from discovery_agent.migration.fabric_rest import FabricApiError

Result = tuple  # (status, step, target, notes)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StageMixin:
    """The table-data stage, and helpers the other stages share."""

    # Provided by Migrator ------------------------------------------------
    run: Any
    _source_factory: Any
    _source: Any
    _item: Any
    _sleep: Any
    _credentials: Dict[str, Dict[str, str]]

    # -- shared helpers ------------------------------------------------------

    def _source_connection(self) -> Any:
        """A read-only session on the Synapse pool, through the discovery sign-in."""
        if self._source is None:
            if self._source_factory is None:
                raise MigrationError("The Synapse source is not connected. Reconnect it on the Connections page.")
            try:
                self._source = self._source_factory()
            except Exception as exc:  # noqa: BLE001 - driver, token or network failure
                raise MigrationError(f"Could not connect to the Synapse pool: {type(exc).__name__}: {str(exc)[:300]}") from exc
        return self._source

    def _progress(self, text: str) -> None:
        item = getattr(self, "_item", None)
        if item is not None:
            with self.run.lock:
                item.step = text

    @staticmethod
    def _scalar(cursor: Any, sql: str, *params: Any) -> int:
        cursor.execute(sql, *params)
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    # -- table data: one pipeline per wave ------------------------------------------

    def _data(self, source: Source) -> Result:
        """Load this table's wave with a data pipeline, and report every table in it."""
        run = self.run
        current = self._item
        batch = [current] + [i for i in run.items
                             if i is not current and i.status == PENDING and i.source.kind == DATA and i.source.wave == source.wave]
        with run.lock:
            for item in batch[1:]:
                item.status, item.started_at, item.error, item.notes = IN_PROGRESS, _now(), None, []
                item.step = "Waiting for this wave's data pipeline"
        try:
            results = self._load_wave(source.wave, [i.source for i in batch])
        except (MigrationError, FabricApiError) as exc:
            message = exc.message if isinstance(exc, FabricApiError) else str(exc)
            for item in batch[1:]:
                self._finish(item, FAILED, "Failed", None, [], message)
            raise
        for item in batch[1:]:
            status, step, target, notes = results[item.source.id]
            self._finish(item, status, step, target, notes, " ".join(notes) or step if status == FAILED else None)
        status, step, target, notes = results[source.id]
        if status == FAILED:
            raise MigrationError(" ".join(notes) or step)
        return status, step, target, notes

    def _finish(self, item: Any, status: str, step: str, target: Optional[str], notes: List[str], error: Optional[str] = None) -> None:
        run = self.run
        with run.lock:
            item.status, item.step, item.target, item.notes, item.error, item.completed_at = status, step, target, list(notes), error, _now()
        run.log(status, item.source.name, error or (step + (f" -> {target}" if target else "")))
        for note in notes:
            run.log("NOTE", item.source.name, note)

    def _load_wave(self, wave: int, sources: List[Source]) -> Dict[str, Result]:
        run = self.run
        results: Dict[str, Result] = {}
        replace = run.settings.get("dataMode", "if_empty") == "replace"
        cursor = self._warehouse().cursor()  # type: ignore[attr-defined]

        # 1. Which tables still need rows.
        load: List[Source] = []
        for s in sources:
            schema, name = s.schema or "dbo", s.object_name or s.name
            target = f"{run.warehouse}.{schema}.{name}"
            problem = datacopy.preflight(s.payload)
            if problem:
                if problem[0] == "EXTERNAL":
                    results[s.id] = (SKIPPED, "External table: no rows to load", target, [problem[1]])
                else:
                    results[s.id] = (FAILED, "Cannot be loaded", target, [problem[1]])
                continue
            if not self._exists(cursor, schema, name):  # type: ignore[attr-defined]
                results[s.id] = (FAILED, "Not in the Warehouse", target, [
                    "The table is not in the Warehouse yet. Run the Warehouse & schema stage first (it creates the table; this stage loads it)."])
                continue
            existing = self._scalar(cursor, datacopy.count_sql(schema, name))
            if existing and not replace:
                results[s.id] = (SKIPPED, f"Already has {datacopy.describe_count(existing)}; left unchanged", target, [])
                continue
            load.append(s)
        if not load:
            return results

        # 2. The connection the pipeline reads Synapse through, the Warehouse it writes, and the pipeline.
        connection = self._pool_connection()
        if connection is None:
            note = (f"Enter credentials for '{run.source_connection_name}' in the Table data stage (a SQL login or a service principal "
                    "that can read the pool). Synapse does not give up a secret, so Fabric cannot be given one automatically.")
            for s in load:
                results[s.id] = (DEFERRED_STATUS, "Needs the Synapse pool connection", None, [note])
            return results
        warehouse = self._warehouse_info()  # type: ignore[attr-defined]
        if warehouse is None:
            raise MigrationError(f"Warehouse {run.warehouse} is not in the workspace. Run the Warehouse & schema stage first.")
        entries = [datapipeline.entry(s.payload, replace) for s in load]
        name = datapipeline.pipeline_name(run.warehouse, wave)
        pipeline_id, created = self._data_pipeline(name, entries, str(connection["id"]), warehouse)
        made = f"Pipeline '{name}' {'created' if created else 'already existed; reused with this run’s table list'}"

        if run.settings.get("dataRun", "run") == "create":
            for s in load:
                results[s.id] = (COMPLETED, "Pipeline ready; run it in Fabric to load the rows",
                                 f"{run.workspace_name} / {name}", [made + "."])
            return results

        # 3. Run it, wait, and compare row counts.
        instance = self._client().run_job(  # type: ignore[attr-defined]
            f"/workspaces/{run.workspace_id}/items/{pipeline_id}/jobs/instances?jobType=Pipeline", datapipeline.run_body(entries))
        state = self._wait_for_run(instance, name, len(load))
        if state.get("status") != "Completed":
            reason = datapipeline.failure_reason(state)
            for s in load:
                results[s.id] = (FAILED, "Pipeline run did not complete", f"{run.workspace_name} / {name}",
                                 [made + ".", f"Run status: {state.get('status') or 'unknown'}. {reason}",
                                  "Fix the cause, then Retry Failed; choose 'Replace' if some rows already arrived."])
            return results
        reader, unverified = None, ""
        try:
            reader = self._source_connection().cursor()
        except MigrationError as exc:
            unverified = str(exc)
        for s in load:
            schema, table_name = s.schema or "dbo", s.object_name or s.name
            target = f"{run.warehouse}.{schema}.{table_name}"
            landed = self._scalar(cursor, datacopy.count_sql(schema, table_name))
            notes = [made + "."]
            if reader is None:
                notes.append(f"Row counts were not compared: {unverified}")
                results[s.id] = (COMPLETED, f"Loaded {datacopy.describe_count(landed)} by pipeline", target, notes)
                continue
            expected = self._scalar(reader, datacopy.count_sql(s.payload.key.schema, s.payload.key.name))
            if expected == landed:
                results[s.id] = (COMPLETED, f"Loaded {datacopy.describe_count(landed)} by pipeline; counts match", target, notes)
            else:
                results[s.id] = (FAILED, "Row counts do not match", target, notes + [
                    f"{datacopy.describe_count(expected)} in Synapse, {datacopy.describe_count(landed)} in the Warehouse. "
                    "Check the pipeline run in Fabric, then Retry Failed with 'Replace' to load it again."])
        return results

    def _pool_connection(self) -> Optional[Dict[str, Any]]:
        """The Fabric connection to the Synapse pool: an existing one, or one made from the credentials entered."""
        run = self.run
        found = datapipeline.find_pool_connection(self._connections(), run.source_connection_name,  # type: ignore[attr-defined]
                                                  run.source_server, run.source_database)
        if found is not None:
            return dict(found)
        supplied = self._credentials.get(run.source_connection_name)
        if not supplied:
            return None
        if not run.source_server or not run.source_database:
            raise MigrationError("The Synapse pool's SQL endpoint is not known. Reconnect the Synapse source with its dedicated SQL pool.")
        plan = fabric_connections.pool_plan(run.source_connection_name, run.source_server, run.source_database)
        try:
            body = fabric_connections.create_body(plan, supplied)
        except fabric_connections.CredentialError as exc:
            raise MigrationError(str(exc)) from exc
        created = self._client().create("/connections", body)  # type: ignore[attr-defined]
        connection = {"id": created.get("id"), "displayName": plan.name}
        self.__dict__.setdefault("_conn_cache", {})[plan.name] = connection
        run.log("CREATE", plan.name, "Fabric connection to the Synapse pool, for the data pipelines")
        return connection

    def _data_pipeline(self, name: str, entries: List[Dict[str, str]], connection_id: str, warehouse: Any) -> Tuple[str, bool]:
        """(pipeline id, created now) for a wave's data pipeline, creating it when it does not exist."""
        run = self.run
        existing = self._find(self._ids("dataPipelines"), name)  # type: ignore[attr-defined]
        if existing:
            return existing, False
        target = datapipeline.WarehouseTarget(warehouse.artifact_id, warehouse.endpoint, warehouse.name, run.workspace_id)
        content = datapipeline.definition(entries, connection_id, target)
        created = self._client().create(f"/workspaces/{run.workspace_id}/dataPipelines", datapipeline.create_body(  # type: ignore[attr-defined]
            name, content, f"Loads wave tables from the Synapse pool into Warehouse {run.warehouse}"))
        self._forget("dataPipelines")  # type: ignore[attr-defined]
        pipeline_id = str(created.get("id") or "") or self._find(self._ids("dataPipelines"), name) or ""  # type: ignore[attr-defined]
        if not pipeline_id:
            raise MigrationError(f"Pipeline '{name}' was created but Fabric did not return its id. Retry in a minute.")
        return pipeline_id, True

    def _wait_for_run(self, instance: str, name: str, tables: int) -> Dict[str, Any]:
        waited = 0
        while True:
            state = self._client().get(instance)  # type: ignore[attr-defined]
            status = str(state.get("status") or "")
            if status in datapipeline.FINISHED:
                return state
            if waited >= datapipeline.RUN_TIMEOUT_SECONDS:
                raise MigrationError(f"Pipeline '{name}' is still running after {waited // 60} minutes. "
                                     "Follow it in Fabric's Monitoring hub; Retry Failed checks the row counts once it has finished.")
            self._progress(f"Pipeline '{name}' running: {tables} table{'s' if tables != 1 else ''} ({waited // 60} min)")
            self._sleep(datapipeline.POLL_SECONDS)
            waited += datapipeline.POLL_SECONDS
