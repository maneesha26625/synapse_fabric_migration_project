"""Stage handlers for runs whose SQL target is a Fabric Lakehouse (run option ``target: lakehouse``). Mixed into ``Migrator``.

A Lakehouse is not a Warehouse with another name, so several objects take a different road:

* **The pool** becomes a Lakehouse with schemas, named like the pool. Its SQL analytics endpoint is the
  T-SQL side of it: read-only for tables, but it holds views, functions and permissions.
* **Schemas and tables** are created by Spark, not T-SQL (the endpoint cannot create tables): every pending
  schema and table goes into one notebook (``lakehouse_ddl``), which the run creates, runs once, and reads
  the outcome of, statement by statement, from a file it writes into the Lakehouse.
* **Views and functions** are created on the SQL analytics endpoint with T-SQL, as in a Warehouse, once
  the endpoint has picked up the new tables (``refreshMetadata``).
* **Stored procedures** cannot live on a read-only endpoint, so each becomes a PySpark notebook
  (``spark_sql``), which pipelines run with a Notebook activity where they ran the procedure.
* **SQL scripts** that only read stay T-SQL, in a notebook bound to the SQL analytics endpoint; scripts
  that change data become PySpark notebooks like procedures.
* **Table data** is loaded by the same per-wave pipelines, writing Delta tables (``stages``), and
  **roles and permissions** are applied on the SQL analytics endpoint (``runner``).

Handlers return ``(status, step, target, notes)`` or raise ``MigrationError`` / ``FabricApiError``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from discovery_agent.migration import datacopy, datafilter, datapipeline, jobs, lakehouse_ddl, notebooks, pipelines, spark_sql
from discovery_agent.migration.common import (
    COMPLETED, DEFERRED_STATUS, FAILED, IN_PROGRESS, PENDING, PROCEDURE, SCHEMA, SKIPPED, TABLE, WORKSPACE_ERRORS, MigrationError, Source,
    is_lakehouse, override_note,
)
from discovery_agent.migration.fabric_rest import FabricApiError
from discovery_agent.migration.storage_remap import ONELAKE_HOST

Result = tuple  # (status, step, target, notes)
#: How long the table notebook may run (a Spark session can take minutes to start).
NOTEBOOK_TIMEOUT_SECONDS = 60 * 60
#: Synapse's default collation is case-insensitive; a Lakehouse compares strings by their bytes.
CASE_NOTE = ("String comparisons are case-sensitive in Spark and on the SQL analytics endpoint (Latin1_General_100_BIN2_UTF8), "
             "unlike the Synapse pool: check filters and joins on text that relied on case-insensitive matching.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LakehouseStageMixin:
    # Provided by Migrator ------------------------------------------------
    run: Any
    _item: Any
    _onelake: Any
    _sleep: Any
    _sql_factory: Any

    def _lakehouse_target(self) -> bool:
        return is_lakehouse(getattr(self.run, "settings", None))

    def _target_label(self) -> str:
        return "Lakehouse" if self._lakehouse_target() else "Warehouse"

    # -- the Lakehouse and its SQL analytics endpoint ---------------------------------

    def _lakehouse_endpoint(self) -> Tuple[str, str, str]:
        """(Lakehouse id, SQL analytics endpoint id, endpoint host), once the endpoint is provisioned."""
        cached = self.__dict__.get("_endpoint")
        if cached:
            return cached
        run, client = self.run, self._client()  # type: ignore[attr-defined]
        name, lakehouse = self._lakehouse()  # type: ignore[attr-defined]
        host = endpoint = ""
        for _ in range(40):  # a new Lakehouse takes a little while to publish its SQL analytics endpoint
            detail = client.get(f"/workspaces/{run.workspace_id}/lakehouses/{lakehouse}")
            props = detail.get("properties") or {}
            if not self.__dict__.get("_lakehouse_created") and props and not props.get("defaultSchema"):
                raise MigrationError(
                    f"Lakehouse '{name}' already exists without schemas, so the pool's schemas cannot be kept in it. Rename or "
                    "delete it in Fabric (a new one is created with schemas), then Retry Failed.")
            sql = props.get("sqlEndpointProperties") or {}
            host = str(sql.get("connectionString") or "")
            endpoint = str(sql.get("id") or "")
            status = str(sql.get("provisioningStatus") or "Success").lower()
            if host and endpoint and status == "success":
                break
            self._sleep(3)
        if not host or not endpoint:
            raise MigrationError(f"Lakehouse {name} has no SQL analytics endpoint yet. Wait a minute, then Retry Failed.")
        self.__dict__["_endpoint"] = (lakehouse, endpoint, host.replace("tcp:", "").split(",")[0].strip())
        return self.__dict__["_endpoint"]

    def _open_sql_endpoint(self) -> Any:
        """A T-SQL session on the Lakehouse's SQL analytics endpoint (the database is the Lakehouse's name)."""
        _, _, host = self._lakehouse_endpoint()
        self.run.log("CONNECT", self.run.warehouse, f"SQL analytics endpoint {host}")
        return self._sql_factory(host, self.run.warehouse)

    def _refresh_endpoint(self) -> None:
        """Ask the SQL analytics endpoint to pick up Delta tables Spark or a pipeline just wrote. Without it the
        endpoint catches up on its own, usually within a minute."""
        if self.__dict__.get("_endpoint_fresh"):
            return
        _, endpoint, _ = self._lakehouse_endpoint()
        try:
            self._client().create(  # type: ignore[attr-defined]
                f"/workspaces/{self.run.workspace_id}/sqlEndpoints/{endpoint}/refreshMetadata", {},
                what="refreshing the SQL analytics endpoint")
        except FabricApiError as exc:
            if exc.code in WORKSPACE_ERRORS:
                raise
            self.run.log("NOTE", self.run.warehouse, f"SQL analytics endpoint refresh not available ({exc.message[:160]}); "
                         "waiting for Fabric's own sync")
            self._sleep(30)
        self.__dict__["_endpoint_fresh"] = True

    def _endpoint_changed(self) -> None:
        """Tables were created or loaded: the endpoint needs a refresh before it is read again."""
        self.__dict__.pop("_endpoint_fresh", None)

    def _lakehouse_info(self) -> pipelines.Lakehouse:
        name, lakehouse = self._lakehouse()  # type: ignore[attr-defined]
        return pipelines.Lakehouse(lakehouse, name, self.run.workspace_id)

    # -- the pool -> the Lakehouse ---------------------------------------------------------

    def _lh_pool(self, source: Source) -> Result:
        name, _ = self._lakehouse()  # type: ignore[attr-defined]
        created = bool(self.__dict__.get("_lakehouse_created"))
        self._warehouse()  # type: ignore[attr-defined] - connects to the SQL analytics endpoint
        notes = ["Tables become Delta tables (created by a Spark notebook); views and functions are created on its SQL analytics "
                 "endpoint; stored procedures become Spark notebooks that pipelines run."]
        source_collation = (getattr(self.run, "source_collation", None) or "").upper()
        if "_CS" not in source_collation and "_BIN" not in source_collation:
            notes.append(CASE_NOTE)
        step = "Fabric Lakehouse created (with schemas)" if created else "Fabric Lakehouse already existed; reused"
        return COMPLETED, step, f"{self.run.workspace_name} / {name}", notes

    # -- schemas and tables: one Spark notebook for the run -------------------------------------

    def _lh_ddl(self, source: Source) -> Result:
        """Create this schema or table, and every other pending one, with one notebook run; report each."""
        run = self.run
        current = self._item
        batch = [current] + [i for i in run.items
                             if i is not current and i.status == PENDING and i.source.kind in (SCHEMA, TABLE)]
        with run.lock:
            for item in batch[1:]:
                item.status, item.started_at, item.error, item.notes = IN_PROGRESS, _now(), None, []
                item.step = "Waiting for the Lakehouse table notebook"
        try:
            results = self._create_lakehouse_objects([i.source for i in batch])
        except (MigrationError, FabricApiError) as exc:
            message = exc.message if isinstance(exc, FabricApiError) else str(exc)
            for item in batch[1:]:
                self._finish(item, FAILED, "Failed", None, [], message)  # type: ignore[attr-defined]
            raise
        for item in batch[1:]:
            status, step, target, notes = results[item.source.id]
            self._finish(item, status, step, target, notes, " ".join(notes) or step if status == FAILED else None)  # type: ignore[attr-defined]
        status, step, target, notes = results[source.id]
        if status == FAILED:
            raise MigrationError(" ".join(notes) or step)
        return status, step, target, notes

    def _create_lakehouse_objects(self, sources: Sequence[Source]) -> Dict[str, Result]:
        run, client = self.run, self._client()  # type: ignore[attr-defined]
        wid = run.workspace_id
        name, lakehouse = self._lakehouse()  # type: ignore[attr-defined]
        results: Dict[str, Result] = {}
        statements: List[Tuple[str, str, str]] = []
        notes_by: Dict[str, List[str]] = {}
        targets: Dict[str, str] = {}
        schemas: List[str] = []
        for s in sources:
            if s.kind == SCHEMA:
                schema = s.schema or s.name
                targets[s.id] = f"{name}.{schema}"
                statements.append((s.id, "", lakehouse_ddl.create_schema(schema)))
                continue
            schema, table = s.schema or "dbo", s.object_name or s.name
            targets[s.id] = f"{name}.{schema}.{table}"
            if s.override:
                # Accepted for a Lakehouse: a reviewed Spark SQL CREATE TABLE for the Delta table.
                sql, notes = s.override["code"].strip().rstrip(";"), [override_note(s)]
            else:
                try:
                    sql, notes = lakehouse_ddl.create_table(s.payload)
                except lakehouse_ddl.UnsupportedColumn as exc:
                    results[s.id] = (FAILED, "Cannot be created", targets[s.id], [str(exc)])
                    continue
            if s.masks:
                notes.append("Dynamic data masks apply on the SQL analytics endpoint only: Spark and OneLake access read the "
                             "values unmasked, so limit who has workspace access.")
            if schema.lower() not in [x.lower() for x in schemas]:
                schemas.append(schema)
            notes_by[s.id] = notes
            statements.append((s.id, f"{schema}.{table}", sql))
        # A table's schema first, unless a schema item of this batch creates it (a Lakehouse with schemas has dbo already).
        created = {(s.schema or s.name).lower() for s in sources if s.kind == SCHEMA}
        statements = [(f"schema:{x}", "", lakehouse_ddl.create_schema(x)) for x in schemas
                      if x.lower() != "dbo" and x.lower() not in created] + statements
        if not statements:
            return results

        notebook = lakehouse_ddl.notebook_name(name)
        relative = lakehouse_ddl.results_path(run.run_id)
        document = lakehouse_ddl.ddl_notebook(statements, f"abfss://{wid}@{ONELAKE_HOST}/{lakehouse}/{relative}", lakehouse, name, wid)
        notebook_id = self._save_notebook(notebook, document, f"Creates the migrated schemas and tables in Lakehouse {name}")
        self._progress(f"Notebook '{notebook}' starting: {len(statements)} statement(s)")  # type: ignore[attr-defined]
        instance = client.run_job(f"/workspaces/{wid}/items/{notebook_id}/jobs/instances?jobType=RunNotebook", {})
        state = self._wait_for_notebook(instance, notebook, len(statements))
        self._endpoint_changed()
        ran = f"Notebook '{notebook}' ran the CREATE statements."
        if state.get("status") != "Completed":
            reason = datapipeline.failure_reason(state)
            for s in sources:
                results.setdefault(s.id, (FAILED, "The table notebook did not complete", targets.get(s.id),
                                          [f"Run status: {state.get('status') or 'unknown'}. {reason}",
                                           f"Open '{notebook}' in Fabric to see the error, then Retry Failed."]))
            return results

        outcome = self._ddl_outcome(lakehouse, relative)
        for s in sources:
            if s.id in results:
                continue
            notes = list(notes_by.get(s.id, []))
            what = "Schema" if s.kind == SCHEMA else "Table"
            if outcome is None:
                results[s.id] = self._checked_on_endpoint(s, targets[s.id], notes, ran)
                continue
            got = outcome.get(s.id) or {}
            if not got.get("ok"):
                results[s.id] = (FAILED, f"Spark could not create the {what.lower()}", targets[s.id],
                                 [str(got.get("error") or "The notebook did not report this statement.")] + notes)
            elif got.get("existed"):
                results[s.id] = (SKIPPED, f"{what} already in the Lakehouse; left unchanged", targets[s.id], [])
            elif s.kind == SCHEMA:
                results[s.id] = (COMPLETED, "Schema created", targets[s.id], [])
            else:
                results[s.id] = self._with_masks(s, targets[s.id], notes)
        return results

    def _with_masks(self, source: Source, target: str, notes: List[str]) -> Result:
        """A created table: its dynamic data masks applied on the SQL analytics endpoint, if it has any."""
        step = "Table created as a Delta table (empty: the Table data stage loads it with a pipeline)"
        if not source.masks:
            return COMPLETED, step, target, notes
        try:
            self._refresh_endpoint()
            added = self._add_missing_masks(self._warehouse().cursor(), source)  # type: ignore[attr-defined]
        except MigrationError as exc:
            return FAILED, "Table created; its dynamic data masks were not applied", target, [
                f"{exc} Apply the masks on the SQL analytics endpoint before anyone reads the table there."] + notes
        return COMPLETED, step, target, added + notes

    def _checked_on_endpoint(self, source: Source, target: str, notes: List[str], ran: str) -> Result:
        """Without the notebook's own report (OneLake files not available to this run): look for the table on the endpoint."""
        if source.kind == SCHEMA:
            return COMPLETED, "Schema created", target, [ran]
        self._refresh_endpoint()
        schema, name = source.schema or "dbo", source.object_name or source.name
        if self._exists(self._warehouse().cursor(), schema, name):  # type: ignore[attr-defined]
            return self._with_masks(source, target, notes)
        return FAILED, "Not found after the table notebook ran", target, [
            ran, "The table is not on the SQL analytics endpoint. Open the notebook in Fabric to see what Spark reported."]

    def _ddl_outcome(self, lakehouse: str, relative: str) -> Optional[Dict[str, Dict[str, Any]]]:
        """What each statement of the table notebook did, from the file it wrote; None when it cannot be read."""
        if self._onelake is None:
            return None
        try:
            return json.loads(self._onelake.read(self.run.workspace_id, lakehouse, relative).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - fall back to looking on the endpoint
            self.run.log("NOTE", self.run.warehouse, f"The table notebook's report could not be read ({str(exc)[:160]}); "
                         "checking the SQL analytics endpoint instead")
            return None

    def _save_notebook(self, name: str, document: Dict[str, Any], description: str) -> str:
        """Create a notebook, or replace the definition of the one with this name. Returns its id."""
        client, wid = self._client(), self.run.workspace_id  # type: ignore[attr-defined]
        body = notebooks.create_body(name, document, description)
        existing = self._find(self._ids("notebooks"), name)  # type: ignore[attr-defined]
        if existing:
            self._update_definition(existing, body)  # type: ignore[attr-defined]
            return existing
        created = client.create(f"/workspaces/{wid}/notebooks", body)
        self._forget("notebooks")  # type: ignore[attr-defined]
        notebook_id = str(created.get("id") or "") or self._find(self._ids("notebooks"), name) or ""  # type: ignore[attr-defined]
        if not notebook_id:
            raise MigrationError(f"Notebook '{name}' was created but Fabric did not return its id. Retry in a minute.")
        return notebook_id

    def _wait_for_notebook(self, instance: str, name: str, statements: int) -> Dict[str, Any]:
        waited = 0
        while True:
            state = self._client().get(instance)  # type: ignore[attr-defined]
            if str(state.get("status") or "") in datapipeline.FINISHED:
                return state
            if waited >= NOTEBOOK_TIMEOUT_SECONDS:
                raise MigrationError(f"Notebook '{name}' is still running after {waited // 60} minutes. "
                                     "Follow it in Fabric's Monitoring hub, then Retry Failed.")
            self._progress(f"Notebook '{name}' running: {statements} statement(s) ({waited // 60} min)")  # type: ignore[attr-defined]
            self._sleep(datapipeline.POLL_SECONDS)
            waited += datapipeline.POLL_SECONDS

    # -- table data into Delta tables ---------------------------------------------------------

    def _load_wave_lakehouse(self, wave: int, sources: List[Source]) -> Dict[str, Result]:
        """``StageMixin._load_wave`` for a Lakehouse: the wave's pipeline writes Delta tables, counted on the SQL endpoint."""
        run = self.run
        results: Dict[str, Result] = {}
        replace = run.settings.get("dataMode", "if_empty") == "replace"
        self._refresh_endpoint()
        cursor = self._warehouse().cursor()  # type: ignore[attr-defined]
        load: List[Tuple[Source, List[str], List[str]]] = []  # (table, predicates, notes)
        for s in sources:
            schema, name = s.schema or "dbo", s.object_name or s.name
            target = f"{run.warehouse}.{schema}.{name}"
            problem = datacopy.preflight(s.payload)
            if problem:
                results[s.id] = (SKIPPED, "External table: no rows to load", target, [problem[1]]) if problem[0] == "EXTERNAL" \
                    else (FAILED, "Cannot be loaded", target, [problem[1]])
                continue
            predicates: List[str] = []
            extra: List[str] = []
            if s.data_filter is not None:
                try:
                    resolved = datafilter.resolve(s.payload, s.data_filter)
                except datafilter.FilterError as exc:
                    results[s.id] = (FAILED, "The date filter does not fit the table", target,
                                     [str(exc), "Change the filter in Plan, Stages & credentials (Table data), then Retry Failed."])
                    continue
                predicates = datafilter.scope_predicates(resolved)
                extra = datafilter.notes(resolved)
                if resolved.sync:
                    extra.append("Keeping a table in sync is not available when the target is a Lakehouse (the sync pipeline "
                                 "merges changes with T-SQL in a Warehouse). It was loaded once, within its filter; schedule its "
                                 "wave's pipeline with 'Replace it' to refresh it.")
            if not self._exists(cursor, schema, name):  # type: ignore[attr-defined]
                results[s.id] = (FAILED, "Not in the Lakehouse", target, [
                    "The table is not on the Lakehouse's SQL analytics endpoint yet. Run the Warehouse & schema stage first "
                    "(it creates the Delta table; this stage loads it)."])
                continue
            existing = self._scalar(cursor, datacopy.count_sql(schema, name))  # type: ignore[attr-defined]
            if existing and not replace:
                results[s.id] = (SKIPPED, f"Already has {datacopy.describe_count(existing)}; left unchanged", target, extra)
                continue
            load.append((s, predicates, extra))
        if not load:
            return results

        connection = self._pool_connection()  # type: ignore[attr-defined]
        if connection is None:
            note = (f"Enter credentials for '{run.source_connection_name}' in the Table data stage (a SQL login or a service principal "
                    "that can read the pool). Synapse does not give up a secret, so Fabric cannot be given one automatically.")
            for s, _, _ in load:
                results[s.id] = (DEFERRED_STATUS, "Needs the Synapse pool connection", None, [note])
            return results
        info = self._lakehouse_info()
        target_lh = datapipeline.LakehouseTarget(info.artifact_id, info.name, info.workspace_id)
        entries = [datapipeline.lakehouse_entry(s.payload, replace, datafilter.select_sql(s.payload, p) if p else "") for s, p, _ in load]
        name = datapipeline.lakehouse_pipeline_name(run.warehouse, wave)
        pipeline_id = self._find(self._ids("dataPipelines"), name)  # type: ignore[attr-defined]
        made = f"Pipeline '{name}' already existed; reused with this run’s table list"
        if not pipeline_id:
            created = self._client().create(f"/workspaces/{run.workspace_id}/dataPipelines", datapipeline.create_body(  # type: ignore[attr-defined]
                name, datapipeline.lakehouse_definition(entries, str(connection["id"]), target_lh),
                f"Loads wave tables from the Synapse pool into the Delta tables of Lakehouse {info.name}"))
            self._forget("dataPipelines")  # type: ignore[attr-defined]
            pipeline_id = str(created.get("id") or "") or self._find(self._ids("dataPipelines"), name) or ""  # type: ignore[attr-defined]
            if not pipeline_id:
                raise MigrationError(f"Pipeline '{name}' was created but Fabric did not return its id. Retry in a minute.")
            made = f"Pipeline '{name}' created"

        if run.settings.get("dataRun", "run") == "create":
            for s, _, extra in load:
                results[s.id] = (COMPLETED, "Pipeline ready; run it in Fabric to load the rows", f"{run.workspace_name} / {name}",
                                 [made + "."] + extra)
            return results

        instance = self._client().run_job(  # type: ignore[attr-defined]
            f"/workspaces/{run.workspace_id}/items/{pipeline_id}/jobs/instances?jobType=Pipeline", datapipeline.run_body(entries))
        state = self._wait_for_run(instance, name, len(load))  # type: ignore[attr-defined]
        self._endpoint_changed()
        if state.get("status") != "Completed":
            reason = datapipeline.failure_reason(state)
            for s, _, _ in load:
                results[s.id] = (FAILED, "Pipeline run did not complete", f"{run.workspace_name} / {name}",
                                 [made + ".", f"Run status: {state.get('status') or 'unknown'}. {reason}",
                                  "Fix the cause, then Retry Failed with 'Replace it' (the copy appends)."])
            return results

        reader, unverified = None, ""
        try:
            reader = self._source_connection().cursor()  # type: ignore[attr-defined]
        except MigrationError as exc:
            unverified = str(exc)
        pending = list(load)
        for attempt in range(4):  # the endpoint can trail the Delta log by a little: count again after a refresh
            self._refresh_endpoint()
            waiting = []
            for s, predicates, extra in pending:
                schema, table = s.schema or "dbo", s.object_name or s.name
                target = f"{run.warehouse}.{schema}.{table}"
                landed = self._scalar(cursor, datacopy.count_sql(schema, table))  # type: ignore[attr-defined]
                notes = [made + "."] + extra
                if reader is None:
                    results[s.id] = (COMPLETED, f"Loaded {datacopy.describe_count(landed)} by pipeline", target,
                                     notes + [f"Row counts were not compared: {unverified}"])
                    continue
                expected = self._scalar(reader, datafilter.count_sql(s.payload.key.schema, s.payload.key.name, predicates))  # type: ignore[attr-defined]
                within = " within the filter" if predicates else ""
                if expected == landed:
                    results[s.id] = (COMPLETED, f"Loaded {datacopy.describe_count(landed)} by pipeline; counts match{within}", target, notes)
                elif attempt < 3:
                    waiting.append((s, predicates, extra))
                else:
                    results[s.id] = (FAILED, "Row counts do not match", target, notes + [
                        f"{datacopy.describe_count(expected)} in Synapse{within}, {datacopy.describe_count(landed)} in the Lakehouse. "
                        "Check the pipeline run in Fabric, then Retry Failed with 'Replace it' to load it again."])
            if not waiting:
                break
            pending = waiting
            self._endpoint_changed()
            self._sleep(datapipeline.POLL_SECONDS)
        return results

    # -- stored procedures -> notebooks ------------------------------------------------------

    def _procedure_catalog(self) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
        """For EXEC between procedures: each procedure of the run's notebook name, and its parameter names, by
        ``schema.name`` and ``name`` (lower-cased)."""
        if "_procedures" not in self.__dict__:
            names: Dict[str, str] = {}
            params: Dict[str, List[str]] = {}
            for item in self.run.items:
                s = item.source
                if s.kind != PROCEDURE:
                    continue
                schema, name = s.schema or "dbo", s.object_name or s.name
                for key in spark_sql.procedure_keys(schema, name):
                    names.setdefault(key, spark_sql.notebook_name(schema, name))
                    params.setdefault(key, spark_sql.parameter_names(s.payload))
            self.__dict__["_procedures"] = (names, params)
        return self.__dict__["_procedures"]

    def _lh_procedure(self, source: Source) -> Result:
        definition = getattr(source.payload, "definition", None)
        text = getattr(definition, "text", None)
        if not source.override and (not text or not getattr(definition, "is_readable", False)):
            raise MigrationError("The definition could not be read from the pool (encrypted, or VIEW DEFINITION is not granted), so it cannot be converted.")
        schema, name = source.schema or "dbo", source.object_name or source.name
        notebook = spark_sql.notebook_name(schema, name)
        target = f"{self.run.workspace_name} / {notebook}"
        existing = self._find(self._ids("notebooks"), notebook)  # type: ignore[attr-defined]
        if existing and not self._updating():  # type: ignore[attr-defined]
            return SKIPPED, "Already in the Fabric workspace as a notebook; left unchanged", target, []
        lakehouse_name, lakehouse = self._lakehouse()  # type: ignore[attr-defined]
        if source.override:
            # Accepted for a Lakehouse: the reviewed notebook itself, not a fresh conversion.
            try:
                document = lakehouse_ddl.document_from_cells(source.override["code"], lakehouse, lakehouse_name, self.run.workspace_id)
            except lakehouse_ddl.NotANotebook as exc:
                raise MigrationError(f"{exc} Discard it, or accept a corrected one.") from exc
            self._save_notebook(notebook, document, f"Stored procedure {schema}.{name}, converted from Synapse T-SQL (reviewed)")
            return COMPLETED, ("Updated as" if existing else "Created as") + " a Spark notebook (LLM-assisted, reviewed)", target, [
                override_note(source), f"Attached to Lakehouse {lakehouse_name}. Pipelines that called the procedure run this notebook instead."]
        names, params = self._procedure_catalog()
        code, remap_notes = self._remap(str(text))  # type: ignore[attr-defined]
        converted = spark_sql.procedure_notebook(
            code, name=f"{schema}_{name}", lakehouse_id=lakehouse, lakehouse=lakehouse_name, workspace_id=self.run.workspace_id,
            pool=getattr(self.run, "pool_name", "") or "", notebooks=names, callee_params=params)
        self._save_notebook(notebook, converted.document, f"Stored procedure {schema}.{name}, converted from Synapse T-SQL to Spark SQL")
        notes = [
            f"A Lakehouse's SQL analytics endpoint is read-only, so the procedure became the Spark notebook '{notebook}', "
            f"attached to Lakehouse {lakehouse_name}. Pipelines that called it run the notebook instead."]
        notes += converted.notes + remap_notes + [f"Review: {r}" for r in converted.review]
        step = "Converted to a Spark notebook"
        if converted.review:
            step += f"; {len(converted.review)} item{'s' if len(converted.review) != 1 else ''} to review before it runs"
        if existing:
            step = step.replace("Converted to", "Updated as", 1)
        return COMPLETED, step, target, notes

    # -- SQL scripts ------------------------------------------------------------------------

    def _lh_script(self, source: Source) -> Result:
        self._need_definition(source, "SQL script")  # type: ignore[attr-defined]
        wid = self.run.workspace_id
        target = f"{self.run.workspace_name} / {source.name}"
        existing = self._find(self._ids("notebooks"), source.name)  # type: ignore[attr-defined]
        if existing and not self._updating():  # type: ignore[attr-defined]
            return SKIPPED, "A notebook with this name is already in the workspace; left unchanged", target, []
        payload = json.loads(json.dumps(source.payload or {}))
        content = payload.setdefault("properties", {}).setdefault("content", {})
        content["query"], remap_notes = self._remap(str(content.get("query") or ""))  # type: ignore[attr-defined]
        query = str(content["query"])
        lakehouse_name, lakehouse = self._lakehouse()  # type: ignore[attr-defined]
        if source.override:
            # Accepted for a Lakehouse: the reviewed Spark notebook itself.
            try:
                document = lakehouse_ddl.document_from_cells(source.override["code"], lakehouse, lakehouse_name, wid)
            except lakehouse_ddl.NotANotebook as exc:
                raise MigrationError(f"{exc} Discard it, or accept a corrected one.") from exc
            notes = [f"Attached to Lakehouse {lakehouse_name}."]
            step = "Created as a Spark notebook (LLM-assisted, reviewed)"
        elif not spark_sql.changes_data(query):
            _, endpoint, _ = self._lakehouse_endpoint()
            try:
                document, notes = jobs.sql_script_notebook(payload, endpoint, lakehouse_name, kind=jobs.SQL_ENDPOINT)
            except jobs.NotConvertible as exc:
                return DEFERRED_STATUS, "Needs a rewrite", None, [str(exc)]
            notes.insert(0, "It only reads, so it stays T-SQL in a notebook bound to the Lakehouse's SQL analytics endpoint.")
            step = "Created as a T-SQL notebook on the SQL analytics endpoint"
        else:
            names, params = self._procedure_catalog()
            converted = spark_sql.script_notebook(
                query, name=source.name, lakehouse_id=lakehouse, lakehouse=lakehouse_name, workspace_id=wid,
                pool=getattr(self.run, "pool_name", "") or "", notebooks=names, callee_params=params)
            document = converted.document
            notes = ["It changes data, which the read-only SQL analytics endpoint cannot do, so it was converted to a Spark "
                     f"notebook attached to Lakehouse {lakehouse_name}."] + converted.notes + [f"Review: {r}" for r in converted.review]
            step = "Converted to a Spark notebook" + (f"; {len(converted.review)} item(s) to review before it runs" if converted.review else "")
        if source.override:
            notes.insert(0, override_note(source))
        notes.extend(remap_notes)
        body = jobs.sql_script_body(source.name, document)
        if existing:
            self._update_definition(existing, body)  # type: ignore[attr-defined]
            return COMPLETED, "Updated in Fabric (definition replaced)", target, notes
        self._client().create(f"/workspaces/{wid}/notebooks", body)  # type: ignore[attr-defined]
        self._forget("notebooks")  # type: ignore[attr-defined]
        return COMPLETED, step, target, notes
