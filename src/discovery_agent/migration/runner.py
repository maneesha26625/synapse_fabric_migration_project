"""One migration run: the plan's objects, moved into Fabric one at a time.

What this build migrates:

* **Notebooks** -> Fabric Notebook items, through the Fabric REST API.
* **The dedicated SQL pool** -> a Fabric Warehouse of the same name (created
  if missing, reused if it exists).
* **Schemas, tables, views, stored procedures** -> that Warehouse, as schema
  only: tables are created empty (with their keys, NOT ENFORCED), views and
  procedures from their own text after the T-SQL rules (``tsql_rules``) have
  removed what a Warehouse rejects.
* **Table data** -> Fabric data pipelines, one per wave (``stages``).
* **Functions** -> the Warehouse, like views (scalar and inline table-valued only).
* **Roles and users** -> Warehouse roles, members and GRANT/DENY permissions (``security``);
  dynamic data masks are written into each table's CREATE TABLE.

Everything else in the plan is marked DEFERRED with the reason, so a run never
pretends to have moved what it cannot move yet. An object that already exists
in Fabric is SKIPPED, never overwritten, which makes a re-run safe.

The runner talks to Fabric and SQL only through the two factories it is given,
which is what lets the tests drive it with fakes.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from discovery_agent.diagnostics import record_unexpected, redacted
from discovery_agent.migration import environments, notebooks, security, tsql_rules, warehouse_ddl
from discovery_agent.migration.common import (  # noqa: F401 - re-exported: tests and the API import these from here
    COMPLETED, CONNECTION, DATA, DATASET, DEFERRED, DEFERRED_STATUS, ENVIRONMENT, FAILED, FUNCTION, IN_PROGRESS, MISSING,
    NOTEBOOK, PENDING, PIPELINE, POOL, PROCEDURE, SCHEDULE, SCHEMA, SCRIPT, SECURITY, SHORTCUT, SKIPPED, SPARKJOB, TABLE,
    VIEW, WORKSPACE_ERRORS, MigrationError, Source, override_note,
)
from discovery_agent.migration.stages import StageMixin
from discovery_agent.migration.stages_fabric import FabricStageMixin
from discovery_agent.migration.fabric_rest import FabricApiError, FabricRestClient

#: The UI types this build can migrate, and the kind each becomes.
MIGRATABLE = {
    "Dedicated SQL Pool": POOL, "Schema": SCHEMA, "Table": TABLE, "View": VIEW,
    "Stored Procedure": PROCEDURE, "Notebook": NOTEBOOK, "Spark Pool": ENVIRONMENT,
    "Linked Service": CONNECTION, "Pipeline": PIPELINE, "Dataset": DATASET,
    "Spark Job Definition": SPARKJOB, "SQL Script": SCRIPT, "Trigger": SCHEDULE, "External Table": SHORTCUT,
    "Function": FUNCTION, "Security Object": SECURITY,
}
MIGRATABLE_TYPES = tuple(MIGRATABLE)
#: Within one wave, in the order things must exist: the warehouse, schemas and connections, tables,
#: the views and procedures that read them, their rows, then notebooks, jobs, pipelines and schedules.
_KIND_ORDER = {
    # Functions before the views and procedures that call them; roles and grants once the objects exist.
    POOL: -2, SCHEMA: -1, CONNECTION: -0.5, TABLE: 0, FUNCTION: 0.5, VIEW: 1, PROCEDURE: 2, SECURITY: 2.1, DATA: 2.2, DATASET: 2.3,
    ENVIRONMENT: 2.5, NOTEBOOK: 3, SCRIPT: 3.2, SPARKJOB: 3.5, PIPELINE: 4, SHORTCUT: 4.5, SCHEDULE: 5,
    MISSING: 6, DEFERRED: 7,
}
_FABRIC_KINDS = {NOTEBOOK, ENVIRONMENT, POOL, SCHEMA, TABLE, VIEW, PROCEDURE, DATA, CONNECTION, PIPELINE,
                 SPARKJOB, SCRIPT, SCHEDULE, SHORTCUT, FUNCTION, SECURITY}

_DEFERRED_REASONS = {
    "Pipeline": "Pipelines move in a later session: their datasets and linked services need Fabric connections first.",
    "Dataset": "Datasets move in a later session, as part of the pipelines that use them.",
    "Linked Service": "Linked services become Fabric connections in a later session; they need credentials set up in Fabric.",
    "Spark Job Definition": "Spark job definitions move in a later session.",
    "SQL Script": "SQL scripts move in a later session.",
    "External Table": "External tables move in a later session, as OneLake shortcuts.",
}

SCHEMA_EXISTS_QUERY = "SELECT SCHEMA_ID(?)"
#: Warehouse tables without an IDENTITY column.
WITHOUT_IDENTITY_QUERY = ("SELECT s.name, t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
                          "WHERE NOT EXISTS (SELECT 1 FROM sys.identity_columns ic WHERE ic.object_id = t.object_id)")

#: Warehouse collations: Fabric's default is case-sensitive; Synapse's default is case-insensitive.
CASE_SENSITIVE = "Latin1_General_100_BIN2_UTF8"
CASE_INSENSITIVE = "Latin1_General_100_CI_AS_KS_WS_SC_UTF8"
COLLATIONS = ("match_synapse", "case_insensitive", "case_sensitive")


def warehouse_collation(choice: str, synapse_collation: Optional[str]) -> str:
    """The collation to create the Warehouse with, from the run option and the pool's own collation."""
    if choice == "case_sensitive":
        return CASE_SENSITIVE
    if choice == "case_insensitive":
        return CASE_INSENSITIVE
    source = (synapse_collation or "").upper()
    sensitive = "_CS" in source or "_BIN" in source
    return CASE_SENSITIVE if sensitive else CASE_INSENSITIVE  # an unknown pool collation is Synapse's default, case-insensitive


def deferred_reason(object_type: str) -> str:
    return _DEFERRED_REASONS.get(object_type, f"{object_type} objects move in a later session.")


def warehouse_name_for(pool: Optional[str]) -> str:
    """A valid Fabric Warehouse name derived from the dedicated SQL pool's name."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", pool or "") or "SynapseMigration"
    return name if name[0].isalpha() else "wh_" + name


@dataclass
class Item:
    source: Source
    status: str = PENDING
    step: str = "Waiting"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    target: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.source.id, "name": self.source.name, "type": self.source.type,
            "wave": self.source.wave, "step": self.step, "status": self.status,
            "startedAt": self.started_at, "completedAt": self.completed_at,
            "error": self.error, "notes": list(self.notes), "target": self.target,
            # Created from an accepted LLM-assisted transform rather than the rule-based conversion.
            "llm": ({"acceptedAt": self.source.override.get("acceptedAt"), "edited": bool(self.source.override.get("edited"))}
                    if self.source.override else None),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MigrationRun:
    """The state of one run, shared by the worker thread and HTTP requests."""

    def __init__(self, run_id: str, sources: List[Source], workspace_id: str, workspace_name: str, warehouse: str) -> None:
        self.lock = threading.RLock()
        self.run_id = run_id
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.warehouse = warehouse
        ordered = sorted(sources, key=lambda s: (s.wave, _KIND_ORDER.get(s.kind, 9)))
        self.items = [Item(s) for s in ordered]
        self.logs: List[str] = []
        self.state = "running"  # running | paused | completed
        self.pause = threading.Event()
        #: Run options: ``scope`` is what was asked for, ``stop_on_failure`` halts at a wave boundary.
        self.scope = "all"
        self.stop_on_failure = False
        self.halt_waived = False  # set when the operator resumes past a halt
        #: Strategy settings for the stages (data mode, data run, collation, sync re-read window). Never credentials.
        self.settings: Dict[str, Any] = {"dataMode": "if_empty", "dataRun": "run", "collation": "match_synapse", "syncOverlap": "1h",
                                         "gitCommit": "auto"}
        self.stages: List[str] = []
        self.pool_name: str = ""  # the Synapse pool being migrated, for retargeting pipelines
        #: The pool's SQL endpoint and the Fabric connection the data pipelines read it through.
        self.source_server: str = ""
        self.source_database: str = ""
        self.source_connection_name: str = ""
        self.source_collation: Optional[str] = None
        self.halted_reason: Optional[str] = None
        #: What the commit to the workspace's Fabric Git branch did once the run completed (None: not tried yet).
        self.git: Optional[Dict[str, Any]] = None
        for item in self.items:
            if item.source.kind == DEFERRED:
                item.status, item.step, item.error = DEFERRED_STATUS, "Not migrated in this session", item.source.reason

    def log(self, level: str, name: str, message: str) -> None:
        with self.lock:
            self.logs.append(f"{_now()}  {level:<8}{name}: {message}")

    def to_dict(self) -> dict:
        with self.lock:
            counts = {s: 0 for s in (PENDING, IN_PROGRESS, COMPLETED, FAILED, SKIPPED, DEFERRED_STATUS)}
            for item in self.items:
                counts[item.status] += 1
            return {
                "runId": self.run_id, "state": self.state, "total": len(self.items),
                "completed": counts[COMPLETED], "inProgress": counts[IN_PROGRESS], "failed": counts[FAILED],
                "pending": counts[PENDING], "skipped": counts[SKIPPED], "deferred": counts[DEFERRED_STATUS],
                "workspace": self.workspace_name, "warehouse": self.warehouse,
                "options": {"scope": self.scope, "stopOnFailure": self.stop_on_failure, "stages": list(self.stages), **self.settings},
                "haltedReason": self.halted_reason,
                "git": dict(self.git) if self.git else None,
                "items": [i.to_dict() for i in self.items], "logs": list(self.logs[-2000:]),
            }


class Migrator(StageMixin, FabricStageMixin):
    """One pass over a run's PENDING items. A retry is a new pass."""

    def __init__(
        self,
        run: MigrationRun,
        rest_factory: Callable[[], FabricRestClient],
        sql_factory: Callable[[str, str], Any],
        sleep: Callable[[float], None] = lambda s: None,
        source_factory: Optional[Callable[[], Any]] = None,
        credentials: Optional[Dict[str, Dict[str, str]]] = None,
        onelake: Any = None,
    ) -> None:
        self.run = run
        #: Files in the Lakehouse (``onelake.OneLakeFiles``): copies of Spark job main files with rerouted storage paths.
        self._onelake = onelake
        self._source_factory = source_factory
        self._source: Any = None
        self._item: Optional["Item"] = None
        #: Secrets for connections, by linked-service name. In memory for the run's life; never logged or returned.
        self._credentials = credentials or {}
        self._rest_factory = rest_factory
        self._sql_factory = sql_factory
        self._sleep = sleep
        self._rest: Optional[FabricRestClient] = None
        self._notebook_names: Optional[set] = None
        self._sql: Any = None
        self._sql_error: Optional[str] = None
        self._warehouse_created = False
        self._collation: Optional[str] = None
        #: A workspace-level failure; every later Fabric object fails with it at once.
        self._fatal: Optional[str] = None
        #: Log references of the unexpected errors this pass has written down, by (type, message).
        self._unexpected: Dict[Tuple[str, str], str] = {}

    def _reference(self, source: Source, exc: Exception) -> str:
        """The log reference for an unexpected error. Each distinct error is written once per pass, so a fault
        that hits every object leaves one traceback in the log, not thousands."""
        key = (type(exc).__name__, str(exc)[:300])
        if key not in self._unexpected:
            self._unexpected[key] = record_unexpected(f"migration run {self.run.run_id}: {source.name}", exc)
        return self._unexpected[key]

    # -- the loop ----------------------------------------------------------

    def execute(self) -> None:
        run = self.run
        for item in run.items:
            if item.status != PENDING:
                continue
            if run.pause.is_set():
                with run.lock:
                    run.state = "paused"
                run.log("PAUSED", "run", "paused before the next object")
                self._close()
                return
            reason = self._halt_reason(item)
            if reason:
                with run.lock:
                    run.state, run.halted_reason = "paused", reason
                run.log("HALTED", "run", reason)
                self._close()
                return
            self._one(item)
        with run.lock:
            run.state = "completed"
        run.log("DONE", "run", "every pending object was processed")
        self._close()

    def _halt_reason(self, item: Item) -> Optional[str]:
        """Stop-on-failure: do not start a wave while an earlier one has failures."""
        run = self.run
        if not run.stop_on_failure or run.halt_waived:
            return None
        wave = item.source.wave
        failed = sorted({i.source.wave for i in run.items if i.status == FAILED and i.source.wave < wave})
        if not failed:
            return None
        return f"Stopped before wave {wave}: wave {failed[0]} has failed objects. Fix them and Retry Failed, or Resume to continue anyway."

    def _one(self, item: Item) -> None:
        run, source = self.run, item.source
        with run.lock:
            item.status, item.started_at, item.error, item.notes = IN_PROGRESS, _now(), None, []
            item.step = "Creating in Fabric"
        self._item = item
        run.log("START", source.name, f"{source.type} ({source.kind})")
        try:
            if self._fatal and source.kind in _FABRIC_KINDS:
                raise MigrationError(self._fatal)
            handler = {
                NOTEBOOK: self._notebook, POOL: self._pool, SCHEMA: self._schema,
                TABLE: self._table, VIEW: self._module, PROCEDURE: self._module, FUNCTION: self._module,
                SECURITY: self._security, ENVIRONMENT: self._environment,
                DATA: self._data, CONNECTION: self._connection, DATASET: self._dataset, PIPELINE: self._pipeline,
                SPARKJOB: self._sparkjob, SCRIPT: self._script, SCHEDULE: self._schedule, SHORTCUT: self._shortcut,
            }.get(source.kind)
            if handler is None:
                raise MigrationError(source.reason or "This object is not in the current discovery results. Run discovery again.")
            status, step, target, notes = handler(source)
        except (MigrationError, FabricApiError) as exc:
            message = exc.message if isinstance(exc, FabricApiError) else str(exc)
            cause = exc if isinstance(exc, FabricApiError) else exc.__cause__
            if isinstance(cause, FabricApiError) and cause.code in WORKSPACE_ERRORS:
                self._fatal = message
            with run.lock:
                item.status, item.step, item.error, item.completed_at = FAILED, "Failed", message, _now()
            run.log("FAILED", source.name, message)
            return
        except Exception as exc:  # noqa: BLE001 - one object never stops the run; the API's log gets the traceback
            message = f"Unexpected {type(exc).__name__}: {redacted(str(exc)[:300])} (reference {self._reference(source, exc)})"
            with run.lock:
                item.status, item.step, item.error, item.completed_at = FAILED, "Failed", message, _now()
            run.log("FAILED", source.name, message)
            return
        with run.lock:
            item.status, item.step, item.target, item.notes, item.completed_at = status, step, target, notes, _now()
        run.log(status, source.name, step + (f" -> {target}" if target else ""))
        for note in notes:
            run.log("NOTE", source.name, note)

    # -- notebooks ---------------------------------------------------------

    def _client(self) -> FabricRestClient:
        if self._rest is None:
            self._rest = self._rest_factory()
        return self._rest

    def _notebook(self, source: Source):
        if not isinstance(source.payload, dict) or not source.payload:
            raise MigrationError("The notebook's definition was not kept by discovery. Run discovery again.")
        try:
            payload = notebooks.with_cells(source.payload, source.override["code"]) if source.override else source.payload
            document, notes = notebooks.to_fabric_ipynb(payload, pool_name=self.run.pool_name, warehouse=self.run.warehouse)
        except notebooks.NotebookNotMigratable as exc:
            if source.override:
                raise MigrationError(str(exc)) from exc
            return DEFERRED_STATUS, "Not migrated: needs a rewrite", None, [str(exc)]
        if source.override:
            notes.insert(0, override_note(source))
        remapped: List[str] = []
        for cell in document.get("cells") or []:
            if cell.get("cell_type") != "code":
                continue
            text = "".join(cell.get("source") or [])
            routed, cell_notes = self._remap(text)
            if routed != text:
                cell["source"] = routed.splitlines(keepends=True)
            remapped.extend(n for n in cell_notes if n not in remapped)
        notes.extend(remapped)
        client = self._client()
        wid = self.run.workspace_id
        if self._notebook_names is None:
            self._notebook_names = {str(n.get("displayName", "")).lower() for n in client.list(f"/workspaces/{wid}/notebooks")}
        target = f"{self.run.workspace_name} / {source.name}"
        description = str((source.payload.get("properties") or {}).get("description") or "Migrated from Azure Synapse")
        if source.name.lower() in self._notebook_names:
            if not self._updating():
                return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
            item_id = self._find(self._ids("notebooks"), source.name)
            if not item_id:
                raise MigrationError("The notebook is in the workspace but its id could not be found to update it. Retry.")
            self._update_definition(item_id, notebooks.create_body(source.name, document, description))
            return COMPLETED, "Updated in Fabric (definition replaced)", target, notes
        try:
            self._client().create(f"/workspaces/{wid}/notebooks", notebooks.create_body(source.name, document, description))
        except FabricApiError as exc:
            if exc.code == "ItemDisplayNameAlreadyInUse":
                self._notebook_names.add(source.name.lower())
                return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
            raise
        self._notebook_names.add(source.name.lower())
        return COMPLETED, "Created as a Fabric notebook", target, notes

    # -- spark pool -> custom pool + environment ---------------------------

    def _environment(self, source: Source):
        client, wid = self._client(), self.run.workspace_id
        target = f"{self.run.workspace_name} / {source.name}"
        existing = {str(e.get("displayName", "")).lower() for e in client.list(f"/workspaces/{wid}/environments")}
        if source.name.lower() in existing:
            return SKIPPED, "Environment already in the Fabric workspace; left unchanged", target, []
        wanted = environments.plan(source.payload if isinstance(source.payload, dict) else {}, source.name)
        notes = list(wanted.notes)
        compute = dict(wanted.compute)

        pool_names = {str(p.get("name", "")).lower() for p in client.list(f"/workspaces/{wid}/spark/pools")}
        try:
            if source.name.lower() not in pool_names:
                client.create(f"/workspaces/{wid}/spark/pools", wanted.pool)
                scale = wanted.pool["autoScale"]
                notes.append(f"Created Fabric Spark pool '{source.name}' ({wanted.pool['nodeSize']}, "
                             f"{scale['minNodeCount']}-{scale['maxNodeCount']} nodes).")
            compute["instancePool"] = {"name": source.name, "type": "Workspace"}
        except FabricApiError as exc:
            if exc.code in WORKSPACE_ERRORS:
                raise
            notes.append(f"A custom Spark pool could not be created ({exc.message[:160]}); the Environment uses the Starter Pool.")
            compute = environments.starter_compute(wanted.compute)
            compute["instancePool"] = dict(environments.STARTER_POOL)

        created = client.create(f"/workspaces/{wid}/environments", {
            "displayName": source.name, "description": "Migrated from an Azure Synapse Spark pool",
        })
        env_id = str(created.get("id") or "")
        if not env_id:
            for env in client.list(f"/workspaces/{wid}/environments"):
                if str(env.get("displayName", "")).lower() == source.name.lower():
                    env_id = str(env.get("id") or "")
        if not env_id:
            raise MigrationError("The Environment was created but Fabric did not return its id. Check the workspace.")
        base = f"/workspaces/{wid}/environments/{env_id}/staging"
        try:
            client.patch(f"{base}/sparkcompute", compute)
            client.create(f"{base}/publish", {})
        except FabricApiError as exc:
            raise MigrationError(f"The Environment was created but its compute settings were not applied or published: {exc.message} Finish it in Fabric.") from exc
        return COMPLETED, "Created as a Fabric Environment and published", target, notes

    # -- warehouse ---------------------------------------------------------

    def _warehouse(self) -> Any:
        if self._sql is not None:
            return self._sql
        if self._sql_error:
            raise MigrationError(self._sql_error)
        try:
            self._sql = self._open_warehouse()
        except MigrationError as exc:
            self._sql_error = str(exc)
            raise
        except FabricApiError as exc:
            self._sql_error = f"The warehouse could not be prepared: {exc.message}"
            raise MigrationError(self._sql_error) from exc
        except Exception as exc:  # noqa: BLE001 - driver, token or network failure
            self._sql_error = f"Could not connect to warehouse {self.run.warehouse}: {type(exc).__name__}: {str(exc)[:300]}"
            raise MigrationError(self._sql_error) from exc
        return self._sql

    def _find_warehouse(self) -> Optional[dict]:
        name = self.run.warehouse.lower()
        for warehouse in self._client().list(f"/workspaces/{self.run.workspace_id}/warehouses"):
            if str(warehouse.get("displayName", "")).lower() == name:
                return warehouse
        return None

    def _open_warehouse(self) -> Any:
        run, client = self.run, self._client()
        warehouse = self._find_warehouse()
        if warehouse is None:
            collation = warehouse_collation(str(run.settings.get("collation") or "match_synapse"), run.source_collation)
            run.log("CREATE", run.warehouse, f"creating the Fabric Warehouse with collation {collation}")
            client.create(f"/workspaces/{run.workspace_id}/warehouses", {
                "displayName": run.warehouse, "description": "Migrated from an Azure Synapse dedicated SQL pool",
                "creationPayload": {"defaultCollation": collation},
            })
            self._warehouse_created = True
            self._collation = collation
        host = ""
        for _ in range(20):  # a new warehouse takes a moment to publish its SQL endpoint
            warehouse = warehouse or self._find_warehouse()
            if warehouse is not None:
                detail = client.get(f"/workspaces/{run.workspace_id}/warehouses/{warehouse['id']}")
                host = str((detail.get("properties") or {}).get("connectionString") or "")
                if host:
                    break
            self._sleep(3)
        if not host:
            raise MigrationError(f"Warehouse {run.warehouse} has no SQL endpoint yet. Wait a minute, then Retry Failed.")
        host = host.replace("tcp:", "").split(",")[0].strip()
        run.log("CONNECT", run.warehouse, f"SQL endpoint {host}")
        return self._sql_factory(host, run.warehouse)

    def _exists(self, cursor: Any, schema: str, name: str) -> bool:
        cursor.execute(warehouse_ddl.EXISTS_QUERY, warehouse_ddl.qualified(schema, name))
        row = cursor.fetchone()
        return bool(row) and row[0] is not None

    def _prepare(self, source: Source):
        schema = source.schema or "dbo"
        name = source.object_name or source.name
        cursor = self._warehouse().cursor()
        target = f"{self.run.warehouse}.{schema}.{name}"
        return cursor, schema, name, target

    def _pool(self, source: Source):
        self._warehouse()
        notes: List[str] = []
        if self._warehouse_created:
            step = "Fabric Warehouse created"
            sensitive = self._collation == CASE_SENSITIVE
            notes.append(f"Collation {self._collation} ({'case-sensitive' if sensitive else 'case-insensitive'}"
                         f"{', like the Synapse pool' if self.run.settings.get('collation', 'match_synapse') == 'match_synapse' else ''}).")
        else:
            step = "Fabric Warehouse already existed; reused"
            notes.append("An existing Warehouse keeps the collation it was created with; it cannot be changed afterwards.")
        return COMPLETED, step, f"{self.run.workspace_name} / {self.run.warehouse}", notes

    def _schema(self, source: Source):
        schema = source.schema or source.name
        cursor = self._warehouse().cursor()
        target = f"{self.run.warehouse}.{schema}"
        cursor.execute(SCHEMA_EXISTS_QUERY, schema)
        row = cursor.fetchone()
        if row and row[0] is not None:
            return SKIPPED, "Schema already in the warehouse", target, []
        self._run_sql(cursor, warehouse_ddl.ensure_schema(schema), "create schema " + schema)
        return COMPLETED, "Schema created", target, []

    def _table(self, source: Source):
        cursor, schema, name, target = self._prepare(source)
        if self._exists(cursor, schema, name):
            added = self._add_missing_masks(cursor, source)
            if added:
                return COMPLETED, "Already in the warehouse; dynamic data masks added", target, added
            return SKIPPED, "Already in the warehouse; left unchanged", target, []
        sql, notes = self._ddl(source)
        self._run_sql(cursor, warehouse_ddl.ensure_schema(schema), "create schema " + schema)
        self._run_sql(cursor, sql, "create table")
        for statement in warehouse_ddl.key_constraints(source.payload):
            try:
                cursor.execute(statement)
                notes.append("Key kept as NOT ENFORCED: " + statement.split(" ADD CONSTRAINT ", 1)[1].rstrip(";"))
            except Exception as exc:  # noqa: BLE001 - a key is a hint; the table stands without it
                notes.append(f"Key not recreated ({str(exc)[:160]}): {statement}")
        return COMPLETED, "Table created (empty: the Table data stage loads it with a pipeline)", target, notes

    @staticmethod
    def _ddl(source: Source):
        if source.override:
            notes = [override_note(source)]
            if source.masks:
                notes.append("Dynamic data masks are not added to a reviewed CREATE TABLE: check they are in it.")
            return source.override["code"], notes
        clauses, unsupported = [], []
        for mask in source.masks:
            clause = security.mask_clause(mask)
            if clause:
                clauses.append((mask.column, clause))
            else:
                unsupported.append(mask)
        if unsupported:
            names = ", ".join(f"{m.column} ({m.function})" for m in unsupported)
            raise MigrationError(f"Masking function(s) Fabric does not have: {names}. The table is not created, so its rows "
                                 "are never loaded unmasked. Change the mask in Synapse or create the table by hand.")
        try:
            return warehouse_ddl.create_table(source.payload, clauses)
        except warehouse_ddl.UnsupportedColumn as exc:
            raise MigrationError(str(exc)) from exc

    def _add_missing_masks(self, cursor: Any, source: Source) -> List[str]:
        """An existing table gets the dynamic data masks it lacks (a mask only hides; adding one never exposes)."""
        if not source.masks:
            return []
        try:
            cursor.execute(security.MASKS_SQL)
            present = {(str(r[0]).lower(), str(r[1]).lower(), str(r[2]).lower()) for r in cursor.fetchall()}
        except Exception as exc:  # noqa: BLE001 - masks cannot be compared; say so rather than guess
            raise MigrationError(f"The table exists, but its dynamic data masks could not be read to check them: {str(exc)[:200]}") from exc
        notes = []
        for mask in source.masks:
            if (mask.schema.lower(), mask.table.lower(), mask.column.lower()) in present:
                continue
            statement = security.add_mask_statement(mask)
            if statement is None:
                raise MigrationError(f"Column {mask.column} has masking function {mask.function}, which Fabric does not have. Mask it by hand.")
            self._run_sql(cursor, statement, f"dynamic data mask on {mask.column}")
            notes.append(f"{mask.column}: dynamic data mask added ({mask.function})")
        return notes

    # -- roles, members and permissions ---------------------------------------

    def _security(self, source: Source):
        plan = security.plan_principal(source.payload, source.name)
        principal = plan.principal
        target = f"{self.run.warehouse} / {principal.kind_label.lower()} {principal.name}"
        if principal.is_sql_user:
            # A plan never sends one (sources_for defers SQL users), but a hand-built run must not grant to it.
            raise MigrationError("A SQL user cannot be recreated in Fabric. Replace it with a Microsoft Entra principal.")
        cursor = self._warehouse().cursor()
        notes = list(plan.notes)
        created = False
        if plan.create_role:
            cursor.execute(security.ROLE_EXISTS_QUERY, principal.name)
            found = cursor.fetchone()
            if not (found and found[0] is not None):
                self._run_sql(cursor, plan.create_role, f"role {principal.name}")
                created = True
        rejected: List[str] = []
        for _, statement in plan.members + plan.permissions:
            try:
                cursor.execute(statement)
            except Exception as exc:  # noqa: BLE001 - collect every rejection, like the Migration Assistant's "Fix problems" list
                rejected.append(f"{statement} -> {str(exc)[:160]}")
        total = len(plan.members) + len(plan.permissions)
        if rejected:
            raise MigrationError(f"Fabric rejected {len(rejected)} of {total} statement(s); the others are applied and Retry Failed "
                                 "re-applies them all safely: " + " | ".join(rejected[:5]))
        if principal.is_role:
            step = ("Role created" if created else "Role already in the warehouse") + \
                f"; {len(plan.members)} member(s) and {len(plan.permissions)} permission(s) applied"
        elif plan.permissions:
            step = f"{len(plan.permissions)} permission(s) applied (Fabric creates the user on its first GRANT or DENY)"
        else:
            step = "Nothing granted directly: this principal's access comes from its role memberships"
        if total:
            notes.append("Workspace roles and item permissions still decide who can connect: give these principals "
                         "Read on the Warehouse in Fabric.")
        return COMPLETED, step, target, notes

    def _module(self, source: Source):
        definition = getattr(source.payload, "definition", None)
        text = getattr(definition, "text", None)
        if not text or not getattr(definition, "is_readable", False):
            raise MigrationError("The definition could not be read from the pool (encrypted, or VIEW DEFINITION is not granted), so it cannot be recreated.")
        cursor, schema, name, target = self._prepare(source)
        exists = self._exists(cursor, schema, name)
        if exists and not self._updating():
            return SKIPPED, "Already in the warehouse; left unchanged", target, []
        # An accepted transform still goes through the rules: they are safe to repeat and catch a slip.
        converted = tsql_rules.rewrite(source.override["code"] if source.override else text.strip(), self._tables_without_identity(cursor))
        routed, remap_notes = self._remap(converted.text)
        converted.notes.extend(remap_notes)
        statement = routed
        if exists:
            statement = tsql_rules.create_or_alter(routed)
            if statement is None:
                return SKIPPED, "Already in the warehouse; not updated (its definition has no CREATE statement)", target, []
        self._run_sql(cursor, warehouse_ddl.ensure_schema(schema), "create schema " + schema)
        self._run_sql(cursor, statement, ("update " if exists else "create ") + source.type.lower())
        what = {VIEW: "View", FUNCTION: "Function"}.get(source.kind, "Stored procedure")
        done = "updated in place (CREATE OR ALTER)" if exists else "created"
        if source.override:
            return COMPLETED, f"{what} {done} (LLM-assisted, reviewed)", target, [override_note(source), *converted.notes]
        return COMPLETED, f"{what} {done}" + (" (converted for Fabric)" if converted.changed else ""), target, converted.notes

    def _tables_without_identity(self, cursor: Any) -> set:
        """``schema.table`` of every Warehouse table with no IDENTITY column (e.g. created before IDENTITY was kept),
        where ``SET IDENTITY_INSERT`` is rejected. Read when first needed; an unreadable catalog removes nothing."""
        if "_no_identity" not in self.__dict__:
            try:
                cursor.execute(WITHOUT_IDENTITY_QUERY)
                self.__dict__["_no_identity"] = {f"{r[0]}.{r[1]}".lower() for r in cursor.fetchall()}
            except Exception:  # noqa: BLE001 - keep the statement; Fabric reports it if it is wrong
                self.__dict__["_no_identity"] = set()
        return self.__dict__["_no_identity"]

    @staticmethod
    def _run_sql(cursor: Any, statement: Optional[str], what: str) -> None:
        if not statement:
            return
        try:
            cursor.execute(statement)
        except Exception as exc:  # noqa: BLE001 - the driver's message is the useful part
            raise MigrationError(f"Fabric Warehouse rejected the {what}: {str(exc)[:400]}. Review it by hand.") from exc

    def _close(self) -> None:
        if self._source is not None:
            try:
                self._source.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
            self._source = None
        if self._sql is not None:
            try:
                self._sql.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
            self._sql = None
