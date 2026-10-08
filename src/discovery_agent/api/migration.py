"""The migration run behind the Migrate step.

    GET  /api/migration/run        the current run (or an idle one)
    POST /api/migration/start      {items: [{id, wave}]} -> starts a run
    POST /api/migration/control    {action: pause | resume | retry | reset}

A run needs a finished discovery (the Session) and a connected Fabric target
(FabricTarget), signed in with either the Azure CLI or the Fabric CLI, whose
workspace is on a Fabric capacity. It runs on one background thread; the page
polls ``GET /api/migration/run``. Only one run at a time.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from discovery_agent.api.fabric import FabricTarget
from discovery_agent.api.service import ApiError, Session
from discovery_agent.migration import planner as planning
from discovery_agent.migration.fabric_rest import NO_CAPACITY, FabricApiError, FabricRestClient
from discovery_agent.migration.preflight import content_findings, environment_checks
from discovery_agent.migration import capabilities, datafilter, datapipeline, fabric_connections, pipelines
from discovery_agent.migration.validation import Validator, summarize
from discovery_agent.migration.runner import (
    CONNECTION,
    DATA,
    DATASET,
    DEFERRED,
    ENVIRONMENT,
    MIGRATABLE,
    MISSING,
    NOTEBOOK,
    PIPELINE,
    POOL,
    SCHEDULE,
    SCHEMA,
    SCRIPT,
    SPARKJOB,
    TABLE,
    COLLATIONS,
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
from discovery_agent.sql.models import SqlTable

MAX_ITEMS = 10000
#: Runner kinds that this build creates in Fabric (everything else is deferred).
_AUTOMATED_KINDS = frozenset(MIGRATABLE.values()) | {DATA}
DATA_MODES = ("if_empty", "replace")
DATA_RUNS = ("run", "create")
MAX_CREDENTIALS = 100
MAX_CREDENTIAL_FIELD = 2048
MAX_HISTORY = 20
#: How the validation report groups its rows.
VALIDATION_ORDER = ("Warehouse", "Schema", "Tables", "Data Count", "Views", "Stored Procedures", "Spark", "Notebooks", "Connections",
                    "Pipelines", "Spark Jobs", "SQL Scripts", "Schedules", "Shortcuts", "Manual")
SCOPES = ("automated", "all")


def sql_driver() -> str:
    """The SQL Server ODBC driver this machine would use for the Warehouse, or empty."""
    try:
        import pyodbc  # noqa: PLC0415 - optional dependency
        drivers = pyodbc.drivers()
    except Exception:  # noqa: BLE001 - no driver is an answer, not an error
        return ""
    return next((d for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server") if d in drivers), "")


def parse_options(body: dict) -> dict:
    """Validated run options. Raises ApiError for anything out of range."""
    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    scope = str(options.get("scope") or "all")
    if scope not in SCOPES:
        raise ApiError(400, "invalid_options", "Choose a scope of automated or all.")
    stages = options.get("stages")
    if stages is None:
        stages = list(capabilities.STAGE_KEYS)
    if not isinstance(stages, list) or any(k not in capabilities.STAGE_KEYS for k in stages):
        raise ApiError(400, "invalid_options", "One of the selected stages is not known.")
    mode = str(options.get("dataMode") or "if_empty")
    if mode not in DATA_MODES:
        raise ApiError(400, "invalid_options", "Choose to skip or replace tables that already have rows.")
    data_run = str(options.get("dataRun") or "run")
    if data_run not in DATA_RUNS:
        raise ApiError(400, "invalid_options", "Choose to create and run the data pipelines, or only create them.")
    collation = str(options.get("collation") or "match_synapse")
    if collation not in COLLATIONS:
        raise ApiError(400, "invalid_options", "Choose a Warehouse collation: same as Synapse, case-insensitive or case-sensitive.")
    overlap = str(options.get("syncOverlap") or "1h")
    if overlap not in datafilter.OVERLAPS:
        raise ApiError(400, "invalid_options", "Choose a re-read window for synced tables: none, one hour or one day.")
    return {"scope": scope, "stopOnFailure": bool(options.get("stopOnFailure")), "stages": list(dict.fromkeys(stages)),
            "dataMode": mode, "dataRun": data_run, "collation": collation, "syncOverlap": overlap,
            "dataFilters": parse_data_filters(options.get("dataFilters"))}


def parse_data_filters(raw: Any) -> Dict[str, datafilter.DataFilter]:
    """Per-table date filters, checked for shape; whether their columns exist is checked against each table later."""
    try:
        return datafilter.parse_filters(raw)
    except datafilter.FilterError as exc:
        raise ApiError(400, "invalid_filters", str(exc)) from None


def parse_credentials(raw: Any) -> Dict[str, Dict[str, str]]:
    """Connection credentials from a request, validated for shape only. Never logged or echoed."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_CREDENTIALS:
        raise ApiError(400, "invalid_credentials", "The connection credentials are not in a valid format.")
    out: Dict[str, Dict[str, str]] = {}
    for name, fields in raw.items():
        if not isinstance(name, str) or not isinstance(fields, dict):
            raise ApiError(400, "invalid_credentials", "The connection credentials are not in a valid format.")
        clean = {str(k): v for k, v in fields.items() if isinstance(v, str) and len(v) <= MAX_CREDENTIAL_FIELD}
        if any(v.strip() for v in clean.values()):  # all blank: nothing chosen, so the connection stays deferred
            out[name] = clean
    return out


def apply_stages(sources: List[Source], options: dict) -> List[Source]:
    """Add the data loads, then keep only what the selected stages cover."""
    stages = set(options["stages"])
    pool = list(sources)
    if "data" in stages:
        pool += data_sources(sources, options.get("dataFilters"))
    kept: List[Source] = []
    for s in pool:
        if s.kind in (DEFERRED, MISSING):
            if options["scope"] == "all":
                kept.append(s)
            continue
        key = capabilities.stage_of_kind(s.kind)
        if key in stages:
            kept.append(s)
        elif options["scope"] == "all":
            label = capabilities.stage(key).label if key else s.type
            kept.append(Source(s.id, s.name, s.type, s.wave, DEFERRED, reason=f"Its stage ({label}) is switched off for this run."))
    return kept


def plan_objects(plan: List[dict], job: Any, sources: List[Source], stages: Optional[set] = None) -> List[planning.PlanObject]:
    """The planner's view of a plan: each entry with its kind, wave and dependencies."""
    nodes = {n["id"]: n for n in (job.graph or {}).get("nodes", [])}
    needs: Dict[str, List[str]] = {}
    for edge in (job.graph or {}).get("edges", []):
        needs.setdefault(edge["source"], []).append(edge["target"])
    kinds = {s.id: s.kind for s in sources}
    out = []
    for entry in plan:
        oid = str(entry.get("id") or "")
        node = nodes.get(oid)
        if node is None:
            continue
        kind = kinds.get(oid)
        out.append(planning.PlanObject(
            id=oid, name=node["name"], type=node["type"], wave=int(entry.get("wave") or node.get("wave") or 1),
            classification=str(node.get("classification") or ""), fabric_target=str(node.get("fabricTarget") or ""),
            kind=kind if kind in _AUTOMATED_KINDS else None,
            depends_on=tuple(needs.get(oid, ())), depended_on_by=int(node.get("dependedOnBy") or 0),
            selected=stages is None or capabilities.stage_of_kind(kind or "") in stages,
        ))
    return out

IDLE = {
    "runId": "", "state": "idle", "total": 0, "completed": 0, "inProgress": 0, "failed": 0,
    "pending": 0, "skipped": 0, "deferred": 0, "workspace": None, "warehouse": None, "items": [], "logs": [],
}


def _artifacts(job: Any) -> Dict[P0Artifact, Dict[str, dict]]:
    """Every discovered Synapse artifact's raw resource, by kind and name."""
    out: Dict[P0Artifact, Dict[str, dict]] = {}
    synapse = getattr(job.run, "synapse", None) if getattr(job, "run", None) is not None else None
    for artifact in getattr(synapse, "artifacts", ()) or ():
        out.setdefault(artifact.artifact, {})[artifact.name] = dict(artifact.payload)
    return out


def _linked_service_refs(node: Any) -> List[str]:
    """Names of every linked service a resource points at."""
    found: List[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            ref = n.get("linkedServiceName")
            if isinstance(ref, dict) and isinstance(ref.get("referenceName"), str):
                found.append(ref["referenceName"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return sorted(set(found))


def pipeline_bundle(resource: dict, artifacts: Dict[P0Artifact, Dict[str, dict]]) -> dict:
    """A pipeline with the datasets and linked services it needs, which Fabric embeds in it."""
    datasets_all = artifacts.get(P0Artifact.DATASET, {})
    services_all = artifacts.get(P0Artifact.LINKED_SERVICE, {})
    wanted = pipelines.referenced_names(resource)["datasets"]
    datasets = {n: datasets_all[n] for n in wanted if n in datasets_all}
    services = {n: services_all[n] for n in {*_linked_service_refs(resource), *(r for d in datasets.values() for r in _linked_service_refs(d))} if n in services_all}
    return {"resource": resource, "datasets": datasets, "linkedServices": services}


def linked_service_uses(name: str, artifacts: Dict[P0Artifact, Dict[str, dict]]) -> Tuple[Dict[str, Any], ...]:
    """The parameter values every pipeline gives linked service ``name``, one entry per use.

    A use is an activity that names it, or a pipeline's reference to a dataset
    on it, with the dataset's ``@dataset()`` values filled in by that reference.
    """
    datasets = artifacts.get(P0Artifact.DATASET, {})
    uses: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("linkedServiceName")
            if isinstance(ref, dict) and ref.get("referenceName") == name:
                uses.append(dict(ref.get("parameters") or {}))
            if node.get("type") == "DatasetReference":
                ds = datasets.get(str(node.get("referenceName") or ""))
                ds_ref = ((ds or {}).get("properties") or {}).get("linkedServiceName") or {}
                if ds is not None and ds_ref.get("referenceName") == name:
                    uses.append(pipelines.dataset_linked_service_values(ds, node))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for resource in artifacts.get(P0Artifact.PIPELINE, {}).values():
        walk(resource)
    return tuple(uses)


def sources_for(plan: List[dict], job: Any, pool: Optional[str]) -> List[Source]:
    """Turn plan entries into what the runner needs, from the finished discovery."""
    rows = {item["id"]: item for item in job.items}
    artifacts = _artifacts(job)
    by_kind = {
        NOTEBOOK: artifacts.get(P0Artifact.NOTEBOOK, {}), PIPELINE: artifacts.get(P0Artifact.PIPELINE, {}),
        CONNECTION: artifacts.get(P0Artifact.LINKED_SERVICE, {}), DATASET: artifacts.get(P0Artifact.DATASET, {}),
        SPARKJOB: artifacts.get(P0Artifact.SPARK_JOB_DEFINITION, {}), SCRIPT: artifacts.get(P0Artifact.SQL_SCRIPT, {}),
    }

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
        name = row["name"]
        kind = MIGRATABLE.get(object_type)
        extra = job.extras_by_id.get(oid) if hasattr(job, "extras_by_id") else None

        # Objects discovered as "extras" (no record): handled first, or they would be deferred below.
        if kind == POOL:
            # Only the pool that was discovered becomes the warehouse.
            if pool and extra is not None and extra.name.lower() != pool.lower():
                sources.append(Source(oid, name, object_type, wave, DEFERRED,
                                      reason=f"Only the SQL pool you connected ({pool}) is migrated in this run."))
            else:
                sources.append(Source(oid, name, object_type, wave, POOL, payload=dict(extra.metadata or {}) if extra is not None else {}))
            continue
        if kind == SCHEMA:
            schema = str((extra.metadata or {}).get("schemaName") if extra is not None else "") or name.split(".")[-1]
            sources.append(Source(oid, name, object_type, wave, SCHEMA, schema=schema, object_name=schema))
            continue
        if kind == ENVIRONMENT:
            sources.append(Source(oid, name, object_type, wave, kind, payload=dict(extra.metadata) if extra is not None else {}))
            continue
        if kind == SCHEDULE:
            raw = dict(getattr(extra, "raw", None) or {}) if extra is not None else {}
            if not raw:
                sources.append(Source(oid, name, object_type, wave, DEFERRED, reason="The trigger's definition was not kept by discovery. Run discovery again."))
            else:
                sources.append(Source(oid, name, object_type, wave, kind, payload=raw))
            continue

        record = job.records_by_id.get(oid)
        if kind is None or record is None:
            sources.append(Source(oid, name, object_type, wave, DEFERRED, reason=deferred_reason(object_type)))
            continue
        if kind in by_kind:
            resource = by_kind[kind].get(record.identity.name)
            if kind == PIPELINE and resource is not None:
                resource = pipeline_bundle(resource, artifacts)
            uses = linked_service_uses(record.identity.name, artifacts) if kind == CONNECTION else ()
            sources.append(Source(oid, record.identity.name, object_type, wave, kind, payload=resource, uses=uses))
            continue
        content = record.content
        key = getattr(content, "key", None)
        sources.append(Source(
            oid, name, object_type, wave, kind, payload=content,
            schema=getattr(key, "schema", None) or record.identity.schema or "dbo",
            object_name=getattr(key, "name", None) or record.identity.name,
        ))
    return sources


def data_sources(sources: List[Source], filters: Optional[Dict[str, datafilter.DataFilter]] = None) -> List[Source]:
    """One data-load item per migrated table, in the table's wave, with its date filter if it has one.
    External tables have no rows to load."""
    out = []
    for s in sources:
        if s.kind == TABLE and s.payload is not None and not getattr(s.payload, "is_external", False):
            found = (filters or {}).get(datafilter.table_key(s.schema or "dbo", s.object_name or s.name))
            out.append(Source(f"{s.id}#data", f"{s.name} (data)", "Table data", s.wave, DATA, payload=s.payload,
                              schema=s.schema, object_name=s.object_name, data_filter=found))
    return out


def filterable_tables(job: Any) -> List[dict]:
    """The discovered tables a date filter can apply to: those with a date or time column, with their keys."""
    out: List[dict] = []
    for oid, record in sorted((getattr(job, "records_by_id", {}) or {}).items()):
        table = getattr(record, "content", None)
        if not isinstance(table, SqlTable) or table.is_external:
            continue
        dates = datafilter.date_columns(table)
        if dates:
            out.append({"id": oid, "schema": table.key.schema, "name": table.key.name,
                        "key": datafilter.table_key(table.key.schema, table.key.name),
                        "dateColumns": [{"name": c.name, "type": c.data_type} for c in dates],
                        "keyColumns": list(datafilter.table_keys(table))})
    return out


class MigrationService:
    def __init__(
        self,
        session: Session,
        fabric: FabricTarget,
        rest_factory: Optional[Callable[[], FabricRestClient]] = None,
        sql_factory: Optional[Callable[[str, str], Any]] = None,
        source_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._session = session
        self._source_factory = source_factory
        #: Connection credentials for the run's life. Memory only: never logged, returned or written.
        self._credentials: Dict[str, Dict[str, str]] = {}
        self._fabric = fabric
        self._lock = threading.Lock()
        self._run: Optional[MigrationRun] = None
        self._thread: Optional[threading.Thread] = None
        self._counter = 0
        self._history: List[dict] = []
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

    def analyze(self, body: dict) -> dict:
        """The planner's strategy, risks, effort and checks for a plan. Reads, never writes."""
        plan = body.get("items")
        if not isinstance(plan, list) or not all(isinstance(p, dict) for p in plan) or len(plan) > MAX_ITEMS:
            raise ApiError(400, "invalid_plan", "The migration plan is not in a valid format.")
        options = parse_options(body)
        credentials = parse_credentials(body.pop("credentials", None))
        job, pool = self._session.migration_snapshot()
        every = sources_for(plan, job, pool)
        stages = set(options["stages"])
        sources = apply_stages(every, {**options, "scope": "automated"})
        objects = plan_objects(plan, job, every, stages)
        needs_source = bool(stages & {"data", "shortcuts"})
        checks = environment_checks(self._fabric.state(), sql_driver(),
                                    source_connected=self._source_available() if needs_source else None)
        names = set(credentials) | set(self._credentials)
        result = planning.analyze(objects, content_findings(sources, names, pool or ""), checks)
        result["options"] = {**options, "dataFilters": {k: f.to_dict() for k, f in options["dataFilters"].items()}}
        if body.get("record"):
            with self._lock:
                entry = {
                    "id": f"P{len(self._history) + 1:03d}", "plannerVersion": result["plannerVersion"],
                    "fingerprint": result["fingerprint"], "objects": result["objects"],
                    "effortDays": result["effortDays"], "waves": len(result["waves"]),
                    "readiness": result["readiness"], "blocking": result["blocking"],
                    "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": "COMPLETED",
                }
                self._history = (self._history + [entry])[-MAX_HISTORY:]
        with self._lock:
            result["history"] = list(reversed(self._history))
        return result

    def start(self, body: dict) -> dict:
        plan = body.get("items")
        if not isinstance(plan, list) or not plan:
            raise ApiError(400, "empty_plan", "The migration plan is empty.")
        if len(plan) > MAX_ITEMS or not all(isinstance(p, dict) for p in plan):
            raise ApiError(400, "invalid_plan", "The migration plan is not in a valid format.")
        options = parse_options(body)
        credentials = parse_credentials(body.pop("credentials", None))
        scope, stop_on_failure = options["scope"], options["stopOnFailure"]
        job, pool = self._session.migration_snapshot()
        workspace_id, workspace_name, method = self._fabric.migration_target()
        if self._busy():
            raise ApiError(409, "run_in_progress", "A migration run is already in progress. Pause it or wait for it to finish.")
        self._check_capacity(workspace_id)
        with self._lock:
            if self._busy():
                raise ApiError(409, "run_in_progress", "A migration run is already in progress. Pause it or wait for it to finish.")
            self._counter += 1
            sources = apply_stages(sources_for(plan, job, pool), options)
            if not [s for s in sources if s.kind not in (DEFERRED, MISSING)]:
                raise ApiError(400, "nothing_to_migrate", "No object in the plan can be created by this build. Choose scope All to see them listed as deferred.")
            run = MigrationRun(f"{self._counter:03d}", sources, workspace_id, workspace_name, warehouse_name_for(pool))
            via = "Azure CLI" if method == "azure_cli" else "Fabric CLI"
            run.scope, run.stop_on_failure = scope, stop_on_failure
            run.stages, run.pool_name = list(options["stages"]), pool or ""
            run.settings = {"dataMode": options["dataMode"], "dataRun": options["dataRun"], "collation": options["collation"],
                            "syncOverlap": options["syncOverlap"]}
            self._describe_source(run, job, pool)
            if credentials:
                self._credentials = credentials
            run.log("RUN", "run", f"{len(sources)} objects, workspace {workspace_name}, warehouse {run.warehouse}, signed in with {via}")
            self._run = run
            self._spawn(run)
            return run.to_dict()

    def control(self, body: dict) -> dict:
        action = str(body.get("action") or "")
        if action == "reset":
            return self.reset()
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
                    run.halt_waived, run.halted_reason = True, None
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
                run.halt_waived, run.halted_reason = False, None
                run.log("RETRY", "run", f"retrying {len(failed)} failed objects")
                self._spawn(run)
            else:
                raise ApiError(400, "invalid_action", "Choose pause, resume, retry or reset.")
            return run.to_dict()

    def reset(self) -> dict:
        """Forget the run's record, and the credentials it was given, so the next
        run starts fresh. Objects already created in Fabric stay there: a new
        run skips what exists, it never deletes or overwrites."""
        with self._lock:
            if self._busy():
                raise ApiError(409, "run_in_progress",
                               "The run is still working. Pause it, wait for the current object to finish, then reset.")
            if self._run is not None:
                self._run.pause.set()  # a forgotten run can never be resumed
            self._run = None
            self._credentials = {}
        return dict(IDLE)

    # -- internals ---------------------------------------------------------

    def _busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def validate(self, body: dict) -> dict:
        """Compare the discovered Synapse objects with what is now in Fabric. Reads both sides, writes neither."""
        plan = body.get("items")
        if plan is not None and (not isinstance(plan, list) or not all(isinstance(p, dict) for p in plan) or len(plan) > MAX_ITEMS):
            raise ApiError(400, "invalid_plan", "The list of objects to validate is not in a valid format.")
        job, pool = self._session.migration_snapshot()
        workspace_id, workspace_name, _ = self._fabric.migration_target()
        plan = plan or [{"id": i["id"], "wave": 1} for i in job.items]
        given = body.get("options") if isinstance(body.get("options"), dict) else {}
        filters = parse_data_filters(given.get("dataFilters"))
        sources = apply_stages(sources_for(plan, job, pool),
                               {"stages": list(capabilities.STAGE_KEYS), "scope": "all", "dataFilters": filters})
        factory = self._source_factory or getattr(self._session, "source_sql_factory", lambda: None)()
        warehouse = warehouse_name_for(pool)
        validator = Validator(
            self._rest_factory(), workspace_id, workspace_name, warehouse, self._sql_factory, factory,
            artifacts={"pipelines": _artifacts(job).get(P0Artifact.PIPELINE, {})})
        rows = validator.run(sources)
        order = {c: n for n, c in enumerate(VALIDATION_ORDER)}
        rows.sort(key=lambda r: (order.get(r["category"], len(order)), r["object"].lower()))
        return {"rows": rows, "summary": summarize(rows), "workspace": workspace_name, "warehouse": warehouse}

    def _source_endpoint(self) -> Optional[tuple]:
        getter = getattr(self._session, "source_endpoint", None)
        return getter() if callable(getter) else None

    def _describe_source(self, run: MigrationRun, job: Any, pool: Optional[str]) -> None:
        """The Synapse pool the data pipelines read: its endpoint, its collation, and the connection's name."""
        endpoint = self._source_endpoint()
        if endpoint:
            workspace, server, database = endpoint
            run.source_server, run.source_database = server, database
            run.source_connection_name = datapipeline.source_connection_name(workspace, database)
        for extra in (getattr(job, "extras_by_id", {}) or {}).values():
            if getattr(extra, "source_type", "") == "Dedicated SQL Pool" and pool and str(extra.name).lower() == pool.lower():
                run.source_collation = (extra.metadata or {}).get("collation")

    def _source_available(self) -> bool:
        return self._source_factory is not None or getattr(self._session, "source_sql_factory", lambda: None)() is not None

    def capabilities(self) -> dict:
        """The stages, their options, and the linked services that need credentials. Reads only."""
        linked: List[dict] = []
        endpoint = self._source_endpoint()
        if endpoint:
            # The data pipelines read the pool through a Fabric connection, which needs a credential.
            workspace, server, database = endpoint
            plan = fabric_connections.pool_plan(datapipeline.source_connection_name(workspace, database), server, database)
            linked.append(plan.describe(stage="data"))
        tables: List[dict] = []
        try:
            job, _ = self._session.migration_snapshot()
            for name, payload in sorted(_artifacts(job).get(P0Artifact.LINKED_SERVICE, {}).items()):
                linked.append(fabric_connections.parse(payload).describe())
            tables = filterable_tables(job)
        except ApiError:
            pass
        return {"stages": capabilities.describe(), "defaults": capabilities.default_settings(), "linkedServices": linked,
                "dataTables": tables}

    def _spawn(self, run: MigrationRun) -> None:
        factory = self._source_factory or getattr(self._session, "source_sql_factory", lambda: None)()
        migrator = Migrator(run, self._rest_factory, self._sql_factory, sleep=_sleep,
                            source_factory=factory, credentials=self._credentials)
        self._thread = threading.Thread(target=migrator.execute, name=f"migration-{run.run_id}", daemon=True)
        self._thread.start()


def _sleep(seconds: float) -> None:
    threading.Event().wait(seconds)
