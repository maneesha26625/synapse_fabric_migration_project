"""The migration run behind the Execute page.

    GET  /api/migration/run        the current run (or an idle one)
    POST /api/migration/start      {items: [{id, wave}]} -> starts a run
    POST /api/migration/control    {action: pause | resume | retry}

A run needs a finished discovery (the Session) and a connected Fabric target
(FabricTarget), signed in with either the Azure CLI or the Fabric CLI, whose
workspace is on a Fabric capacity. It runs on one background thread; the page
polls ``GET /api/migration/run``. Only one run at a time.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from discovery_agent.api.fabric import FabricTarget
from discovery_agent.api.service import ApiError, Session
from discovery_agent.migration.fabric_rest import NO_CAPACITY, FabricApiError, FabricRestClient
from discovery_agent.migration.runner import (
    DEFERRED,
    MIGRATABLE,
    MISSING,
    POOL,
    SCHEMA,
    MigrationRun,
    Migrator,
    Source,
    deferred_reason,
    warehouse_name_for,
)
from discovery_agent.source_strategy import P0Artifact
from discovery_agent.sql.auth import AccessTokenAuthentication
from discovery_agent.sql.config import SqlConnectionConfig
from discovery_agent.sql.connection import PyodbcConnector

MAX_ITEMS = 10000

IDLE = {
    "runId": "", "state": "idle", "total": 0, "completed": 0, "inProgress": 0, "failed": 0,
    "pending": 0, "skipped": 0, "deferred": 0, "workspace": None, "warehouse": None, "items": [], "logs": [],
}


def sources_for(plan: List[dict], job: Any, pool: Optional[str]) -> List[Source]:
    """Turn plan entries into what the runner needs, from the finished discovery."""
    rows = {item["id"]: item for item in job.items}
    notebooks: Dict[str, dict] = {}
    synapse = getattr(job.run, "synapse", None) if job.run is not None else None
    for artifact in getattr(synapse, "artifacts", ()) or ():
        if artifact.artifact is P0Artifact.NOTEBOOK:
            notebooks[artifact.name] = dict(artifact.payload)

    sources: List[Source] = []
    for entry in plan:
        oid = str(entry.get("id") or "")
        try:
            wave = int(entry.get("wave") or 1)
        except (TypeError, ValueError):
            wave = 1
        row = rows.get(oid)
        if row is None:
            sources.append(Source(oid, oid, "Unknown", wave, MISSING))
            continue
        object_type = str(row.get("type") or "")
        kind = MIGRATABLE.get(object_type)
        extra = job.extras_by_id.get(oid) if hasattr(job, "extras_by_id") else None
        if kind == POOL:
            # Only the pool that was discovered becomes the warehouse.
            if pool and extra is not None and extra.name.lower() != pool.lower():
                sources.append(Source(oid, row["name"], object_type, wave, DEFERRED,
                                      reason=f"Only the SQL pool you connected ({pool}) is migrated in this run."))
            else:
                sources.append(Source(oid, row["name"], object_type, wave, POOL))
            continue
        if kind == SCHEMA:
            schema = str((extra.metadata or {}).get("schemaName") if extra is not None else "") or row["name"].split(".")[-1]
            sources.append(Source(oid, row["name"], object_type, wave, SCHEMA, schema=schema, object_name=schema))
            continue
        record = job.records_by_id.get(oid)
        if kind is None or record is None:
            sources.append(Source(oid, row["name"], object_type, wave, DEFERRED, reason=deferred_reason(object_type)))
            continue
        if kind == "notebook":
            sources.append(Source(oid, record.identity.name, object_type, wave, kind, payload=notebooks.get(record.identity.name)))
            continue
        content = record.content
        key = getattr(content, "key", None)
        sources.append(Source(
            oid, row["name"], object_type, wave, kind, payload=content,
            schema=getattr(key, "schema", None) or record.identity.schema or "dbo",
            object_name=getattr(key, "name", None) or record.identity.name,
        ))
    return sources


class MigrationService:
    def __init__(
        self,
        session: Session,
        fabric: FabricTarget,
        rest_factory: Optional[Callable[[], FabricRestClient]] = None,
        sql_factory: Optional[Callable[[str, str], Any]] = None,
    ) -> None:
        self._session = session
        self._fabric = fabric
        self._lock = threading.Lock()
        self._run: Optional[MigrationRun] = None
        self._thread: Optional[threading.Thread] = None
        self._counter = 0
        self._rest_factory = rest_factory or fabric.rest_client
        self._sql_factory = sql_factory or self._open_sql

    def _open_sql(self, host: str, database: str) -> Any:
        """A writable session on a Fabric Warehouse, signed in for the Fabric Target's tenant."""
        config = SqlConnectionConfig(server=host, database=database, application_name="synapse-fabric-migration")
        auth = AccessTokenAuthentication(token_provider=self._fabric.sql_token_provider())
        return PyodbcConnector(config, auth, readonly=False).connect()

    def _check_capacity(self, workspace_id: str) -> None:
        """Refuse up front when the workspace cannot hold Fabric items at all."""
        try:
            workspace = self._rest_factory().get(f"/workspaces/{workspace_id}")
        except FabricApiError as exc:
            raise ApiError(502, "fabric_error", f"The Fabric workspace could not be read: {exc.message}") from exc
        if not workspace.get("capacityId"):
            raise ApiError(409, "no_fabric_capacity", NO_CAPACITY + ".")

    # -- routes ------------------------------------------------------------

    def state(self) -> dict:
        run = self._run
        return run.to_dict() if run is not None else dict(IDLE)

    def start(self, body: dict) -> dict:
        plan = body.get("items")
        if not isinstance(plan, list) or not plan:
            raise ApiError(400, "empty_plan", "The migration plan is empty.")
        if len(plan) > MAX_ITEMS or not all(isinstance(p, dict) for p in plan):
            raise ApiError(400, "invalid_plan", "The migration plan is not in a valid format.")
        job, pool = self._session.migration_snapshot()
        workspace_id, workspace_name, method = self._fabric.migration_target()
        if self._busy():
            raise ApiError(409, "run_in_progress", "A migration run is already in progress. Pause it or wait for it to finish.")
        self._check_capacity(workspace_id)
        with self._lock:
            if self._busy():
                raise ApiError(409, "run_in_progress", "A migration run is already in progress. Pause it or wait for it to finish.")
            self._counter += 1
            sources = sources_for(plan, job, pool)
            run = MigrationRun(f"{self._counter:03d}", sources, workspace_id, workspace_name, warehouse_name_for(pool))
            via = "Azure CLI" if method == "azure_cli" else "Fabric CLI"
            run.log("RUN", "run", f"{len(sources)} objects, workspace {workspace_name}, warehouse {run.warehouse}, signed in with {via}")
            self._run = run
            self._spawn(run)
            return run.to_dict()

    def control(self, body: dict) -> dict:
        action = str(body.get("action") or "")
        with self._lock:
            run = self._run
            if run is None:
                raise ApiError(409, "no_run", "There is no migration run.")
            if action == "pause":
                if run.state == "running":
                    run.pause.set()
                    run.log("PAUSE", "run", "pause requested; stopping after the current object")
            elif action == "resume":
                if run.state == "paused" and not self._busy():
                    run.pause.clear()
                    with run.lock:
                        run.state = "running"
                    run.log("RESUME", "run", "resumed")
                    self._spawn(run)
            elif action == "retry":
                if self._busy():
                    raise ApiError(409, "run_in_progress", "Wait for the run to finish or pause it before retrying.")
                with run.lock:
                    failed = [i for i in run.items if i.status == "FAILED"]
                    for item in failed:
                        item.status, item.step, item.error, item.started_at, item.completed_at = "PENDING", "Waiting", None, None, None
                    run.state = "running"
                run.pause.clear()
                run.log("RETRY", "run", f"retrying {len(failed)} failed objects")
                self._spawn(run)
            else:
                raise ApiError(400, "invalid_action", "Choose pause, resume or retry.")
            return run.to_dict()

    # -- internals ---------------------------------------------------------

    def _busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _spawn(self, run: MigrationRun) -> None:
        migrator = Migrator(run, self._rest_factory, self._sql_factory, sleep=_sleep)
        self._thread = threading.Thread(target=migrator.execute, name=f"migration-{run.run_id}", daemon=True)
        self._thread.start()


def _sleep(seconds: float) -> None:
    threading.Event().wait(seconds)
