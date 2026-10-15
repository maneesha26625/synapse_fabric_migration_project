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

from discovery_agent.diagnostics import redacted
from discovery_agent.migration import datacopy, datafilter, datapipeline, fabric_connections, failures, security, warehouse_ddl
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
        # The wave's other tables load with it; one whose table did not complete is left for its own turn, to be blocked.
        batch = [current] + [i for i in run.items
                             if i is not current and i.status == PENDING and i.source.kind == DATA and i.source.wave == source.wave
                             and not self._blocker(i)]  # type: ignore[attr-defined]
        for item in batch[1:]:
            run.update(item, status=IN_PROGRESS, started_at=_now(), completed_at=None, error=None, notes=[], category=None,
                       step="Waiting for this wave's data pipeline", attempts=item.attempts + 1)
        try:
            results = self._load_wave(source.wave, [i.source for i in batch])
        except Exception as exc:
            # Whatever stopped the wave stops every table in it, so none is left "in progress" for good.
            if isinstance(exc, FabricApiError):
                message = exc.message
            elif isinstance(exc, MigrationError):
                message = str(exc)
            else:
                message = (f"Unexpected {type(exc).__name__}: {redacted(str(exc)[:300])} "
                           f"(reference {self._reference(source, exc)})")  # type: ignore[attr-defined]
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
        category = failures.classify(error or step) if status == FAILED else None
        if status == SKIPPED and item.maybe_created:
            status, notes = COMPLETED, ["Found loaded on this try: an earlier attempt in this run most likely loaded it (the "
                                        "accelerator stopped under it).", *notes]
        run.update(item, status=status, step=step, target=target, notes=list(notes), error=error, completed_at=_now(),
                   category=category, maybe_created=False if status != FAILED else item.maybe_created)
        run.log(status, item.source.name, error or (step + (f" -> {target}" if target else "")))
        for note in notes:
            run.log("NOTE", item.source.name, note)

    def _earlier_load(self, wave: int, sources: List[Source], results: Dict[str, Result]) -> Tuple[List[Source], set]:
        """A load of this wave that this run started before the accelerator stopped (its record says so) is followed
        to its end before anything else: a second load must never run beside it, nor empty a table under it.

        Returns the sources still to load, and the tables that load filled: those are kept as it left them, even
        with Replace. A table whose earlier load did not complete is failed, for a person to look at."""
        run = self.run
        running = [e for e in run.executions if e.get("kind") == "pipelineRun" and e.get("wave") == wave and e.get("status") == "running"]
        if not running:
            return sources, set()
        execution = running[-1]
        name = str(execution.get("pipeline") or datapipeline.pipeline_name(run.warehouse, wave))
        tables = set(execution.get("tables") or [])
        mine = [s for s in sources if s.id in tables]
        self._progress(f"Following pipeline '{name}', which this run started before the accelerator stopped")
        try:
            state = self._wait_for_run(str(execution.get("instance") or ""), name, len(mine))
        except FabricApiError as exc:
            if exc.status != 404:
                # Fabric did not answer this time: the run stays one to follow, and the next pass asks again. Never
                # treated as ended, or a second load could start beside one still going.
                raise
            # Fabric no longer knows the run: never start another blind. A person checks it.
            run.finish_execution(str(execution["id"]), "unknown", exc.message)
            for s in mine:
                results[s.id] = (FAILED, "The earlier pipeline run could not be followed", f"{run.workspace_name} / {name}", [
                    f"This run started pipeline '{name}' before the accelerator stopped, and Fabric cannot say how it ended "
                    f"({exc.message}). Check it in Fabric's Monitoring hub, then Retry Failed; choose 'Replace' if some rows "
                    "already arrived."])
            return [s for s in sources if s.id not in tables], set()
        status = str(state.get("status") or "unknown")
        run.finish_execution(str(execution["id"]), status)
        if status == "Completed":
            return sources, tables
        reason = datapipeline.failure_reason(state)
        for s in mine:
            results[s.id] = (FAILED, "Pipeline run did not complete", f"{run.workspace_name} / {name}", [
                f"The pipeline run this run started before the accelerator stopped ended as {status}. {reason}",
                "Fix the cause, then Retry Failed; choose 'Replace' if some rows already arrived."])
        return [s for s in sources if s.id not in tables], set()

    def _running_elsewhere(self, wave: int) -> Optional[Dict[str, Any]]:
        """The run of this wave's pipeline going in Fabric now, if one is (Fabric's own job list); else None. When
        Fabric cannot say, None: this is a safety net, and the loads it guards are recorded and followed anyway."""
        run = self.run
        pipeline_id = self._find(self._ids("dataPipelines"), datapipeline.pipeline_name(run.warehouse, wave))  # type: ignore[attr-defined]
        if not pipeline_id:
            return None
        try:
            jobs = self._client().list(f"/workspaces/{run.workspace_id}/items/{pipeline_id}/jobs/instances")  # type: ignore[attr-defined]
        except FabricApiError:
            return None
        return next((j for j in jobs if str(j.get("status") or "") in ("NotStarted", "InProgress")), None)

    def _load_wave(self, wave: int, sources: List[Source]) -> Dict[str, Result]:
        run = self.run
        results: Dict[str, Result] = {}
        replace = run.settings.get("dataMode", "if_empty") == "replace"
        cursor = self._warehouse().cursor()  # type: ignore[attr-defined]
        # 0. A load of this wave this run had started before a restart: followed to its end first. And none starts
        #    while the wave's pipeline is running in Fabric for anything else (a run since closed, a person).
        sources, filled = self._earlier_load(wave, sources, results)
        going = self._running_elsewhere(wave) if sources else None
        if going is not None:
            name = datapipeline.pipeline_name(run.warehouse, wave)
            note = (f"Pipeline '{name}' is already running in Fabric (started {going.get('startTimeUtc') or 'moments ago'}), "
                    "and not by this pass. A second load beside it could copy rows twice, or empty a table under it. "
                    "Wait for it to finish (Fabric's Monitoring hub shows it), then Retry Failed.")
            for s in sources:
                results[s.id] = (FAILED, "Its wave's pipeline is already running", f"{run.workspace_name} / {name}", [note])
            return results

        # 1. Which tables still need rows, and which rows: a date filter must fit its table.
        load: List[Tuple[Source, Optional[datafilter.Resolved]]] = []
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
            resolved: Optional[datafilter.Resolved] = None
            if s.data_filter is not None:
                try:
                    resolved = datafilter.resolve(s.payload, s.data_filter)
                except datafilter.FilterError as exc:
                    results[s.id] = (FAILED, "The date filter does not fit the table", target,
                                     [str(exc), "Change the filter in Plan, Stages & credentials (Table data), then Retry Failed."])
                    continue
            if not self._exists(cursor, schema, name):  # type: ignore[attr-defined]
                results[s.id] = (FAILED, "Not in the Warehouse", target, [
                    "The table is not in the Warehouse yet. Run the Warehouse & schema stage first (it creates the table; this stage loads it)."])
                continue
            existing = self._scalar(cursor, datacopy.count_sql(schema, name))
            if existing and s.id in filled:
                results[s.id] = (COMPLETED, f"Loaded {datacopy.describe_count(existing)} by the pipeline run started before "
                                 "the accelerator stopped", target, [
                                     "Its row counts were not compared here, since the accelerator stopped while it ran. "
                                     "Validate compares them."])
                continue
            if existing and not replace:
                notes: List[str] = []
                if resolved is not None and resolved.sync:
                    notes.append("Already kept in sync by the sync pipeline." if self._registered(cursor, schema, name) else
                                 "Not kept in sync yet: its rows were loaded without a watermark. Run Table data with "
                                 "'Replace it' to reload it within the filter and start keeping it in sync.")
                results[s.id] = (SKIPPED, f"Already has {datacopy.describe_count(existing)}; left unchanged", target, notes)
                continue
            load.append((s, resolved))
        if not load:
            return results

        # 2. The connection the pipeline reads Synapse through, the Warehouse it writes, and the pipeline.
        connection = self._pool_connection()
        if connection is None:
            note = (f"Enter credentials for '{run.source_connection_name}' in the Table data stage (a SQL login or a service principal "
                    "that can read the pool). Synapse does not give up a secret, so Fabric cannot be given one automatically.")
            for s, _ in load:
                results[s.id] = (DEFERRED_STATUS, "Needs the Synapse pool connection", None, [note])
            return results
        warehouse = self._warehouse_info()  # type: ignore[attr-defined]
        if warehouse is None:
            raise MigrationError(f"Warehouse {run.warehouse} is not in the workspace. Run the Warehouse & schema stage first.")

        # 3. Each table's rows. A table kept in sync is loaded up to its newest change now, and
        #    registered with that watermark, so the first sync starts exactly where this load ends.
        reader, unverified = None, ""
        try:
            reader = self._source_connection().cursor()
        except MigrationError as exc:
            unverified = str(exc)
        overlap = datafilter.OVERLAPS.get(str(run.settings.get("syncOverlap") or "1h"), 0)
        predicates: Dict[str, List[str]] = {}
        extra: Dict[str, List[str]] = {}
        ready: List[Source] = []
        synced = False
        for s, resolved in load:
            schema, name = s.schema or "dbo", s.object_name or s.name
            if resolved is None:
                predicates[s.id], extra[s.id] = [], []
                ready.append(s)
                continue
            extra[s.id] = datafilter.notes(resolved)
            watermark: Optional[str] = None
            if resolved.sync and resolved.change is not None:
                if self._sync_schedule_on():
                    # A sync during this load would interleave with it: a reload's TRUNCATE would wipe rows the
                    # sync had just applied (past the new watermark, so never copied again), and a first load
                    # would duplicate the rows the sync re-reads.
                    results[s.id] = (FAILED, "Switch off the sync schedule first", f"{run.warehouse}.{schema}.{name}", [
                        f"The schedule of '{datapipeline.sync_name(run.warehouse)}' is on. Loading a table kept in sync while "
                        "syncs can run would lose or duplicate rows. Switch the schedule off in Fabric, Retry Failed, then "
                        "switch it back on once the load is done."])
                    continue
                if reader is None:
                    results[s.id] = (FAILED, "Needs the Synapse source to start syncing", f"{run.warehouse}.{schema}.{name}", [
                        f"Keeping a table in sync starts from the newest change in Synapse, which could not be read: {unverified}"])
                    continue
                try:
                    reader.execute(datafilter.watermark_sql(s.payload, resolved))
                    row = reader.fetchone()
                    watermark = datafilter.clean_watermark(row[0] if row else None)
                except datafilter.FilterError as exc:
                    results[s.id] = (FAILED, "Could not read the newest change", f"{run.warehouse}.{schema}.{name}", [str(exc)])
                    continue
                self._register(cursor, s, resolved, watermark, overlap)
                extra[s.id].append(f"Registered for sync: changes to {resolved.change.name} after {watermark} are left to the sync pipeline.")
                synced = True
            predicates[s.id] = datafilter.first_load_predicates(resolved, watermark)
            ready.append(s)
        if not ready:
            return results
        if synced:
            sync_note = self._sync_pipeline(str(connection["id"]), warehouse)
            for s, resolved in load:
                if resolved is not None and resolved.sync and s.id not in results:
                    extra[s.id].append(sync_note)

        # IDENTITY tables load through a staging copy without IDENTITY, then move in with IDENTITY_INSERT.
        identity = {s.id for s in ready if warehouse_ddl.identity_columns(s.payload)}
        for s in ready:
            if s.id in identity:
                self._load_staging(cursor, s)
                extra[s.id].append(f"Loaded through {datapipeline.LOAD_SCHEMA}.{datapipeline.load_staging(s.schema or 'dbo', s.object_name or s.name)} "
                                   "with IDENTITY_INSERT, keeping the Synapse key values; the identity was then reseeded.")
        entries = [datapipeline.entry(s.payload, replace, datafilter.select_sql(s.payload, predicates[s.id]) if predicates[s.id] else "",
                                      identity=s.id in identity)
                   for s in ready]
        name = datapipeline.pipeline_name(run.warehouse, wave)
        pipeline_id, created = self._data_pipeline(name, entries, str(connection["id"]), warehouse)
        made = f"Pipeline '{name}' {'created' if created else 'already existed; reused with this run’s table list'}"

        if run.settings.get("dataRun", "run") == "create":
            for s in ready:
                results[s.id] = (COMPLETED, "Pipeline ready; run it in Fabric to load the rows",
                                 f"{run.workspace_name} / {name}", [made + "."] + extra[s.id])
            return results

        # 4. Run it, wait, and compare row counts: Synapse's are counted with the same filter.
        instance = self._client().run_job(  # type: ignore[attr-defined]
            f"/workspaces/{run.workspace_id}/items/{pipeline_id}/jobs/instances?jobType=Pipeline", datapipeline.run_body(entries))
        # Recorded before it is followed: a restart while it runs follows this run, never starts a second one beside it.
        execution = run.record_execution({"kind": "pipelineRun", "wave": wave, "pipeline": name, "pipelineId": pipeline_id,
                                          "instance": instance, "tables": [s.id for s in ready]})
        state = self._wait_for_run(instance, name, len(ready))  # still running after the timeout: the record stays "running"
        run.finish_execution(execution, str(state.get("status") or "unknown"))
        if state.get("status") != "Completed":
            reason = datapipeline.failure_reason(state)
            for s in ready:
                results[s.id] = (FAILED, "Pipeline run did not complete", f"{run.workspace_name} / {name}",
                                 [made + ".", f"Run status: {state.get('status') or 'unknown'}. {reason}",
                                  "Fix the cause, then Retry Failed; choose 'Replace' if some rows already arrived."])
            return results
        for s in ready:
            schema, table_name = s.schema or "dbo", s.object_name or s.name
            target = f"{run.warehouse}.{schema}.{table_name}"
            if s.id in identity:
                try:
                    self._finish_identity(cursor, s)
                except MigrationError as exc:
                    results[s.id] = (FAILED, "Rows staged but not moved into the table", target, [made + ".", str(exc)])
                    continue
            landed = self._scalar(cursor, datacopy.count_sql(schema, table_name))
            notes = [made + "."] + extra[s.id]
            if reader is None:
                notes.append(f"Row counts were not compared: {unverified}")
                results[s.id] = (COMPLETED, f"Loaded {datacopy.describe_count(landed)} by pipeline", target, notes)
                continue
            expected = self._scalar(reader, datafilter.count_sql(s.payload.key.schema, s.payload.key.name, predicates[s.id]))
            within = " within the filter" if predicates[s.id] else ""
            if expected == landed:
                results[s.id] = (COMPLETED, f"Loaded {datacopy.describe_count(landed)} by pipeline; counts match{within}", target, notes)
            else:
                results[s.id] = (FAILED, "Row counts do not match", target, notes + [
                    f"{datacopy.describe_count(expected)} in Synapse{within}, {datacopy.describe_count(landed)} in the Warehouse. "
                    "Check the pipeline run in Fabric, then Retry Failed with 'Replace' to load it again."])
        return results

    # -- IDENTITY tables ------------------------------------------------------------------------

    def _load_staging(self, cursor: Any, source: Source) -> None:
        """The staging copy an IDENTITY table is loaded through: its columns and masks, without IDENTITY."""
        schema, name = source.schema or "dbo", source.object_name or source.name
        stage = datapipeline.load_staging(schema, name)
        if self._exists(cursor, datapipeline.LOAD_SCHEMA, stage):  # type: ignore[attr-defined]
            return
        self._run_sql(cursor, warehouse_ddl.ensure_schema(datapipeline.LOAD_SCHEMA), "create schema " + datapipeline.LOAD_SCHEMA)  # type: ignore[attr-defined]
        masks = [(m.column, clause) for m in source.masks for clause in [security.mask_clause(m)] if clause]
        sql, _ = warehouse_ddl.create_table(source.payload, masks, identity=False, schema=datapipeline.LOAD_SCHEMA, name=stage)
        self._run_sql(cursor, sql, f"staging table for {schema}.{name}")  # type: ignore[attr-defined]

    def _finish_identity(self, cursor: Any, source: Source) -> None:
        """After the pipeline: if staged rows are still waiting (a pipeline made before the post-copy step existed),
        move them in here, inside the Warehouse, exactly as the pipeline's step would have."""
        schema, name = source.schema or "dbo", source.object_name or source.name
        stage = datapipeline.load_staging(schema, name)
        if not self._scalar(cursor, datacopy.count_sql(datapipeline.LOAD_SCHEMA, stage)):
            return
        self._run_sql(cursor, datapipeline.identity_finish(source.payload, schema, name), "IDENTITY_INSERT load")  # type: ignore[attr-defined]
        self._run_sql(cursor, datapipeline.identity_cleanup(schema, name), "staging cleanup and identity reseed")  # type: ignore[attr-defined]

    # -- keeping tables in sync ---------------------------------------------------------------

    def _registered(self, cursor: Any, schema: str, name: str) -> bool:
        """Whether a Warehouse table has a row in the sync control table."""
        if not self._exists(cursor, datafilter.SYNC_SCHEMA, datafilter.CONTROL_TABLE):  # type: ignore[attr-defined]
            return False
        cursor.execute(datafilter.REGISTERED_SQL, datafilter.table_key(schema, name))
        row = cursor.fetchone()
        return bool(row) and row[0] is not None

    def _register(self, cursor: Any, source: Source, resolved: datafilter.Resolved, watermark: str, overlap: int) -> None:
        """Make the control table and this table's staging table if missing, then (re)write its row with this watermark."""
        schema, name = source.schema or "dbo", source.object_name or source.name
        for statement in datafilter.CONTROL_DDL:
            cursor.execute(statement)
        if not self._exists(cursor, datafilter.SYNC_SCHEMA, datafilter.staging_table(schema, name)):  # type: ignore[attr-defined]
            cursor.execute(datafilter.staging_ddl(schema, name, source.payload))
        cursor.execute(datafilter.UNREGISTER_SQL, datafilter.table_key(schema, name))
        cursor.execute(datafilter.REGISTER_SQL, *datafilter.registration(source.payload, resolved, schema, name, watermark, overlap))
        self.run.log("SYNC", source.name, f"Registered for sync with watermark {watermark}")

    def _sync_schedule_on(self) -> bool:
        """Whether the Warehouse's sync pipeline exists and has a schedule switched on. Read once per pass."""
        if "_sync_on" not in self.__dict__:
            run = self.run
            pipeline_id = self._find(self._ids("dataPipelines"), datapipeline.sync_name(run.warehouse))  # type: ignore[attr-defined]
            schedules = self._client().list(f"/workspaces/{run.workspace_id}/items/{pipeline_id}/jobs/Pipeline/schedules") if pipeline_id else []  # type: ignore[attr-defined]
            self.__dict__["_sync_on"] = any(s.get("enabled") for s in schedules)
        return bool(self.__dict__["_sync_on"])

    def _sync_pipeline(self, connection_id: str, warehouse: Any) -> str:
        """The Warehouse's sync pipeline, created once with a daily schedule switched off. A note on where it stands."""
        cached = self.__dict__.get("_sync_note")
        if cached:
            return cached
        run = self.run
        name = datapipeline.sync_name(run.warehouse)
        pipeline_id = self._find(self._ids("dataPipelines"), name)  # type: ignore[attr-defined]
        if pipeline_id:
            note = f"Sync pipeline '{name}' already exists; it reads its table list from {datafilter.CONTROL}, so it now includes this table."
        else:
            target = datapipeline.WarehouseTarget(warehouse.artifact_id, warehouse.endpoint, warehouse.name, run.workspace_id)
            created = self._client().create(f"/workspaces/{run.workspace_id}/dataPipelines", datapipeline.create_body(  # type: ignore[attr-defined]
                name, datapipeline.sync_definition(connection_id, target),
                f"Copies new and changed rows of the tables registered in {datafilter.CONTROL} into Warehouse {run.warehouse}"))
            self._forget("dataPipelines")  # type: ignore[attr-defined]
            pipeline_id = str(created.get("id") or "") or self._find(self._ids("dataPipelines"), name) or ""  # type: ignore[attr-defined]
            if not pipeline_id:
                raise MigrationError(f"Pipeline '{name}' was created but Fabric did not return its id. Retry in a minute.")
            run.log("CREATE", name, "Sync pipeline for the tables kept in sync")
            note = f"Sync pipeline '{name}' created."
        path = f"/workspaces/{run.workspace_id}/items/{pipeline_id}/jobs/Pipeline/schedules"
        if not self._client().list(path):  # type: ignore[attr-defined]
            self._client().create(path, datapipeline.sync_schedule_body(datetime.now(timezone.utc)))  # type: ignore[attr-defined]
            note += (f" Its daily schedule ({datapipeline.SYNC_TIME} UTC) was created switched off: switch it on in Fabric "
                     "once the first load is done.")
        self.__dict__["_sync_note"] = note
        return note

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
            body = fabric_connections.create_body(plan, supplied, fabric_connections.key_vault_ids(self._connections()))  # type: ignore[attr-defined]
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
