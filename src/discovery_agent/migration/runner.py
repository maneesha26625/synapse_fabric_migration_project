"""One migration run: the plan's objects, moved into Fabric one at a time.

What this build migrates:

* **Notebooks** -> Fabric Notebook items, through the Fabric REST API.
* **The dedicated SQL pool** -> a Fabric Warehouse of the same name (created
  if missing, reused if it exists).
* **Schemas, tables, views, stored procedures** -> that Warehouse, as schema
  only: tables are created empty, views and procedures from their own text.

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
from typing import Any, Callable, Dict, List, Optional

from discovery_agent.migration import notebooks, warehouse_ddl
from discovery_agent.migration.fabric_rest import FabricApiError, FabricRestClient

NOTEBOOK, TABLE, VIEW, PROCEDURE, DEFERRED, MISSING = "notebook", "table", "view", "procedure", "deferred", "missing"
POOL, SCHEMA = "pool", "schema"
#: The UI types this build can migrate, and the kind each becomes.
MIGRATABLE = {
    "Dedicated SQL Pool": POOL, "Schema": SCHEMA, "Table": TABLE, "View": VIEW,
    "Stored Procedure": PROCEDURE, "Notebook": NOTEBOOK,
}
MIGRATABLE_TYPES = tuple(MIGRATABLE)
#: Within one wave: the warehouse, then schemas, tables, the views that read
#: them, then procedures.
_KIND_ORDER = {POOL: -2, SCHEMA: -1, TABLE: 0, VIEW: 1, PROCEDURE: 2, NOTEBOOK: 3, MISSING: 4, DEFERRED: 5}
#: Fabric errors about the workspace itself, not one object: after the first,
#: the rest of the run fails fast with the same message instead of repeating it.
WORKSPACE_ERRORS = {"FeatureNotAvailable", "WorkspaceNotFound", "CapacityNotActive", "CapacityLimitExceeded"}
_FABRIC_KINDS = {NOTEBOOK, POOL, SCHEMA, TABLE, VIEW, PROCEDURE}

_DEFERRED_REASONS = {
    "Pipeline": "Pipelines move in a later session: their datasets and linked services need Fabric connections first.",
    "Dataset": "Datasets move in a later session, as part of the pipelines that use them.",
    "Linked Service": "Linked services become Fabric connections in a later session; they need credentials set up in Fabric.",
    "Spark Job Definition": "Spark job definitions move in a later session.",
    "SQL Script": "SQL scripts move in a later session.",
    "External Table": "External tables move in a later session, as OneLake shortcuts.",
}

#: Statuses an item can have.
PENDING, IN_PROGRESS, COMPLETED, FAILED, SKIPPED, DEFERRED_STATUS = (
    "PENDING", "IN PROGRESS", "COMPLETED", "FAILED", "SKIPPED", "DEFERRED")


SCHEMA_EXISTS_QUERY = "SELECT SCHEMA_ID(?)"


def deferred_reason(object_type: str) -> str:
    return _DEFERRED_REASONS.get(object_type, f"{object_type} objects move in a later session.")


def warehouse_name_for(pool: Optional[str]) -> str:
    """A valid Fabric Warehouse name derived from the dedicated SQL pool's name."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", pool or "") or "SynapseMigration"
    return name if name[0].isalpha() else "wh_" + name


class MigrationError(Exception):
    """One object could not be migrated; the message says why, in plain words."""


@dataclass
class Source:
    """What the run needs to know about one plan item."""

    id: str
    name: str
    type: str
    wave: int
    kind: str
    payload: Any = None  # a notebook resource, or a SqlTable / SqlView / SqlProcedure
    schema: Optional[str] = None
    object_name: Optional[str] = None
    reason: Optional[str] = None


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
                "items": [i.to_dict() for i in self.items], "logs": list(self.logs[-2000:]),
            }


class Migrator:
    """One pass over a run's PENDING items. A retry is a new pass."""

    def __init__(
        self,
        run: MigrationRun,
        rest_factory: Callable[[], FabricRestClient],
        sql_factory: Callable[[str, str], Any],
        sleep: Callable[[float], None] = lambda s: None,
    ) -> None:
        self.run = run
        self._rest_factory = rest_factory
        self._sql_factory = sql_factory
        self._sleep = sleep
        self._rest: Optional[FabricRestClient] = None
        self._notebook_names: Optional[set] = None
        self._sql: Any = None
        self._sql_error: Optional[str] = None
        self._warehouse_created = False
        #: A workspace-level failure; every later Fabric object fails with it at once.
        self._fatal: Optional[str] = None

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
            self._one(item)
        with run.lock:
            run.state = "completed"
        run.log("DONE", "run", "every pending object was processed")
        self._close()

    def _one(self, item: Item) -> None:
        run, source = self.run, item.source
        with run.lock:
            item.status, item.started_at, item.error, item.notes = IN_PROGRESS, _now(), None, []
            item.step = "Creating in Fabric"
        run.log("START", source.name, f"{source.type} ({source.kind})")
        try:
            if self._fatal and source.kind in _FABRIC_KINDS:
                raise MigrationError(self._fatal)
            handler = {
                NOTEBOOK: self._notebook, POOL: self._pool, SCHEMA: self._schema,
                TABLE: self._table, VIEW: self._module, PROCEDURE: self._module,
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
        except Exception as exc:  # noqa: BLE001 - one object never stops the run
            message = f"Unexpected {type(exc).__name__}: {str(exc)[:300]}"
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
            document, notes = notebooks.to_fabric_ipynb(source.payload)
        except notebooks.NotebookNotMigratable as exc:
            return DEFERRED_STATUS, "Not migrated: needs a rewrite", None, [str(exc)]
        client = self._client()
        wid = self.run.workspace_id
        if self._notebook_names is None:
            self._notebook_names = {str(n.get("displayName", "")).lower() for n in client.list(f"/workspaces/{wid}/notebooks")}
        target = f"{self.run.workspace_name} / {source.name}"
        if source.name.lower() in self._notebook_names:
            return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
        description = str((source.payload.get("properties") or {}).get("description") or "Migrated from Azure Synapse")
        try:
            self._client().create(f"/workspaces/{wid}/notebooks", notebooks.create_body(source.name, document, description))
        except FabricApiError as exc:
            if exc.code == "ItemDisplayNameAlreadyInUse":
                self._notebook_names.add(source.name.lower())
                return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
            raise
        self._notebook_names.add(source.name.lower())
        return COMPLETED, "Created as a Fabric notebook", target, notes

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
            run.log("CREATE", run.warehouse, "creating the Fabric Warehouse")
            client.create(f"/workspaces/{run.workspace_id}/warehouses", {
                "displayName": run.warehouse, "description": "Migrated from an Azure Synapse dedicated SQL pool",
            })
            self._warehouse_created = True
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
        step = "Fabric Warehouse created" if self._warehouse_created else "Fabric Warehouse already existed; reused"
        return COMPLETED, step, f"{self.run.workspace_name} / {self.run.warehouse}", []

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
            return SKIPPED, "Already in the warehouse; left unchanged", target, []
        sql, notes = self._ddl(source)
        self._run_sql(cursor, warehouse_ddl.ensure_schema(schema), "create schema " + schema)
        self._run_sql(cursor, sql, "create table")
        return COMPLETED, "Table created (empty: data moves in a later session)", target, notes

    @staticmethod
    def _ddl(source: Source):
        try:
            return warehouse_ddl.create_table(source.payload)
        except warehouse_ddl.UnsupportedColumn as exc:
            raise MigrationError(str(exc)) from exc

    def _module(self, source: Source):
        definition = getattr(source.payload, "definition", None)
        text = getattr(definition, "text", None)
        if not text or not getattr(definition, "is_readable", False):
            raise MigrationError("The definition could not be read from the pool (encrypted, or VIEW DEFINITION is not granted), so it cannot be recreated.")
        cursor, schema, name, target = self._prepare(source)
        if self._exists(cursor, schema, name):
            return SKIPPED, "Already in the warehouse; left unchanged", target, []
        self._run_sql(cursor, warehouse_ddl.ensure_schema(schema), "create schema " + schema)
        self._run_sql(cursor, text.strip(), "create " + source.type.lower())
        what = "View" if source.kind == VIEW else "Stored procedure"
        return COMPLETED, f"{what} created", target, []

    @staticmethod
    def _run_sql(cursor: Any, statement: Optional[str], what: str) -> None:
        if not statement:
            return
        try:
            cursor.execute(statement)
        except Exception as exc:  # noqa: BLE001 - the driver's message is the useful part
            raise MigrationError(f"Fabric Warehouse rejected the {what}: {str(exc)[:400]}. Review it by hand.") from exc

    def _close(self) -> None:
        if self._sql is not None:
            try:
                self._sql.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
            self._sql = None
