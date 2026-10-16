"""The migration run behind the Migrate step.

    GET  /api/migration/run              the current run (or an idle one)
    POST /api/migration/start            {items: [{id, wave}]} -> starts a run (or asks for its sign-off)
    POST /api/migration/approve          {decision: approve | reject, comment?} -> a person signs a waiting run off
    POST /api/migration/control          {action: pause | resume | retry | reset, version?, credentials?}
    GET  /api/migration/runs             every run of the project, newest first
    GET  /api/migration/runs/{id}        one run as its record has it
    GET  /api/migration/runs/{id}/events its events in order (``after``, ``limit``)
    GET  /api/migration/plans/{id}       one recorded plan version
    GET  /api/migration/policy           the run and object statuses, their moves, the sign-off rule

A run needs a finished discovery (the Session) and a connected Fabric target
(FabricTarget), signed in with either the Azure CLI or the Fabric CLI, whose
workspace is on a Fabric capacity. It runs on one background thread; the page
polls ``GET /api/migration/run``. Only one run at a time.

Every run is recorded in the project's store (``state.runs``): its status changes and every change to an object,
as they happen. A restart, or a switch of project and back, brings the current run back; one the process stopped
under is paused as interrupted, and Resume carries it on where it was, checking Fabric first for anything it was
in the middle of making. Reset closes a run and keeps its record, as the run history.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from discovery_agent.api.fabric import FabricError, FabricTarget
from discovery_agent.api.service import ApiError, Session
from discovery_agent.diagnostics import record_unexpected, redacted, unexpected_message
from discovery_agent.state import approvals, context
from discovery_agent.state.runs import RunRecorder
from discovery_agent.state.store import Store
from discovery_agent.migration import failures, run_state
from discovery_agent.migration import planner as planning
from discovery_agent.migration.fabric_rest import NO_CAPACITY, FabricApiError, FabricRestClient
from discovery_agent.migration.preflight import code_text as preflight_code_text
from discovery_agent.migration.preflight import content_findings, environment_checks
from discovery_agent.migration import capabilities, datafilter, datapipeline, fabric_connections, fabric_git, pipelines, storage_remap
from discovery_agent.migration.onelake import OneLakeFiles
from discovery_agent.migration.runtime_checks import RuntimeOptions
from discovery_agent.migration.validation import Validator, summarize
from discovery_agent.migration.common import lakehouse_of
from discovery_agent.migration.runner import (
    COMPLETED,
    CONNECTION,
    DATA,
    DATASET,
    DEFERRED,
    ENVIRONMENT,
    FUNCTION,
    IN_PROGRESS,
    MIGRATABLE,
    MISSING,
    NOTEBOOK,
    PENDING,
    PIPELINE,
    POOL,
    PROCEDURE,
    SCHEDULE,
    SCHEMA,
    SCRIPT,
    SECURITY,
    SHORTCUT,
    SPARKJOB,
    TABLE,
    VIEW,
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

_log = logging.getLogger("discovery_agent.migration")

MAX_ITEMS = 10000
#: Runner kinds that this build creates in Fabric (everything else is deferred).
_AUTOMATED_KINDS = frozenset(MIGRATABLE.values()) | {DATA}
DATA_MODES = ("if_empty", "replace")
DATA_RUNS = ("run", "create")
#: After a run completes: commit what it created to the workspace's Fabric Git branch, or leave it.
GIT_COMMITS = ("auto", "off")
#: What that commit takes: only the items the run created or updated, or everything waiting in the workspace.
COMMIT_SCOPES = ("run", "all")
MAX_CREDENTIALS = 100
MAX_CREDENTIAL_FIELD = 2048
MAX_HISTORY = 20
#: How the validation report groups its rows.
VALIDATION_ORDER = ("Warehouse", "Schema", "Tables", "Data Count", "Functions", "Views", "Stored Procedures", "Security", "Spark",
                    "Notebooks", "Connections", "Pipelines", "Spark Jobs", "SQL Scripts", "Schedules", "Shortcuts", "Manual")
SCOPES = ("automated", "all")
#: An object already in Fabric: left as it is (the default), or updated in place. Tables are never replaced.
EXISTING = ("skip", "update")


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
    existing = str(options.get("existing") or "skip")
    if existing not in EXISTING:
        raise ApiError(400, "invalid_options", "Choose to skip or update objects that are already in Fabric.")
    routes = parse_storage_routes(options.get("storageRoutes"))
    overlap = str(options.get("syncOverlap") or "1h")
    if overlap not in datafilter.OVERLAPS:
        raise ApiError(400, "invalid_options", "Choose a re-read window for synced tables: none, one hour or one day.")
    git_commit = str(options.get("gitCommit") or "auto")
    if git_commit not in GIT_COMMITS:
        raise ApiError(400, "invalid_options", "Choose to commit to Fabric Git after the run (auto) or not (off).")
    commit_scope = str(options.get("commitScope") or "run")
    if commit_scope not in COMMIT_SCOPES:
        raise ApiError(400, "invalid_options", "Choose what the Fabric Git commit takes: the run's items (run) or everything (all).")
    return {"scope": scope, "stopOnFailure": bool(options.get("stopOnFailure")), "waveGate": bool(options.get("waveGate")),
            "stages": list(dict.fromkeys(stages)), "dataMode": mode, "dataRun": data_run, "collation": collation,
            "syncOverlap": overlap, "existing": existing, "storageRoutes": routes, "gitCommit": git_commit,
            "commitScope": commit_scope, "dataFilters": parse_data_filters(options.get("dataFilters"))}


_ACCOUNT_NAME = re.compile(r"^[a-z0-9]{3,24}$")
_CONTAINER_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")
_CONNECTION_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def parse_storage_routes(raw: Any) -> List[Dict[str, str]]:
    """Storage locations to route through OneLake shortcuts: [{account, container, connectionId? | linkedService?}]."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or len(raw) > 50:
        raise ApiError(400, "invalid_options", "The storage routes are not in a valid format.")
    out: List[Dict[str, str]] = []
    for r in raw:
        if not isinstance(r, dict):
            raise ApiError(400, "invalid_options", "The storage routes are not in a valid format.")
        account, container = str(r.get("account") or "").lower(), str(r.get("container") or "").lower()
        cid, ls = str(r.get("connectionId") or ""), str(r.get("linkedService") or "")
        if not _ACCOUNT_NAME.match(account) or not _CONTAINER_NAME.match(container):
            raise ApiError(400, "invalid_options", f"'{container}' on '{account}' is not a storage account and container name.")
        if (cid and not _CONNECTION_ID.match(cid)) or len(ls) > 260 or not (cid or ls):
            raise ApiError(400, "invalid_options", f"Choose a Fabric connection for {container} on {account}.")
        out.append({"account": account, "container": container, **({"connectionId": cid} if cid else {}),
                    **({"linkedService": ls} if ls else {})})
    return out


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


def _person_enters_credentials() -> None:
    """Credentials come only with a request a person makes: never from the agent or the accelerator itself."""
    if context.actor().kind != context.PERSON:
        raise ApiError(403, "person_only", "Only a person enters credentials: they travel once, with the request a person makes.")


def parse_credential_names(raw: Any) -> Set[str]:
    """The linked services a person has entered credentials for, by name only.

    Planning needs to know which have them, never what they are: the credentials themselves are sent once, when
    a run starts."""
    if raw in (None, []):
        return set()
    if not isinstance(raw, list) or len(raw) > MAX_CREDENTIALS or not all(isinstance(n, str) and 0 < len(n) <= 260 for n in raw):
        raise ApiError(400, "invalid_request", "credentialNames must be a list of linked-service names.")
    return set(raw)


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


def needs_synapse_source(sources: List[Source]) -> bool:
    """Whether running these items reads the Synapse pool itself: a table's data load (its row count) or an
    external table (its location). Nothing else does, so a plan without them needs no SQL pool sign-in."""
    return any(s.kind in (DATA, SHORTCUT) for s in sources)


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

#: The Fabric item each kind of run object is created as: what its commit to Fabric Git is made of.
_GIT_ITEM_TYPES = {NOTEBOOK: "Notebook", SCRIPT: "Notebook", PIPELINE: "DataPipeline", SPARKJOB: "SparkJobDefinition",
                   ENVIRONMENT: "Environment"}
#: Kinds whose objects live inside the run's Warehouse item.
_WAREHOUSE_KINDS = frozenset({POOL, SCHEMA, TABLE, VIEW, PROCEDURE, FUNCTION, SECURITY})


def git_items(run: MigrationRun) -> Set[Tuple[str, str]]:
    """The Fabric items a run created or updated, as (item type, display name): all its Fabric Git commit may take.

    Schemas, tables, views, procedures, functions and roles change the Warehouse item. External-table shortcuts,
    storage routed through OneLake, and a Spark job's default Lakehouse change the run's Lakehouse. Connections,
    data loads and schedules are not items Fabric keeps in Git."""
    with run.lock:
        done = [i.source for i in run.items if i.status == COMPLETED]
    items: Set[Tuple[str, str]] = set()
    lakehouse = False
    for source in done:
        if source.kind in _GIT_ITEM_TYPES:
            items.add((_GIT_ITEM_TYPES[source.kind], source.name))
        elif source.kind in _WAREHOUSE_KINDS:
            items.add(("Warehouse", run.warehouse))
        lakehouse = lakehouse or source.kind in (SHORTCUT, SPARKJOB)
    if lakehouse or (items and run.settings.get("storageRoutes")):
        items.add(("Lakehouse", lakehouse_of(run.warehouse, run.settings)))
    return items


IDLE = {
    "runId": "", "state": "idle", "total": 0, "completed": 0, "inProgress": 0, "failed": 0,
    "pending": 0, "skipped": 0, "deferred": 0, "workspace": None, "warehouse": None, "git": None, "items": [], "logs": [],
}


def _artifacts(job: Any) -> Dict[P0Artifact, Dict[str, dict]]:
    """Every discovered Synapse artifact's raw resource, by kind and name."""
    out: Dict[P0Artifact, Dict[str, dict]] = {}
    synapse = getattr(job.run, "synapse", None) if getattr(job, "run", None) is not None else None
    for artifact in getattr(synapse, "artifacts", ()) or ():
        out.setdefault(artifact.artifact, {})[artifact.name] = dict(artifact.payload)
    # A Git or ZIP source: the environment's definitions, its parameters already applied.
    for kind, by_name in (getattr(job, "repository_artifacts", None) or {}).items():
        out.setdefault(kind, {}).update(by_name)
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


#: Why a multi-statement table-valued function is left for a person.
MULTI_STATEMENT_REASON = ("Fabric Warehouse does not support multi-statement table-valued functions. Rewrite it as an inline "
                          "table-valued function or a view: generate an LLM-assisted draft (Discover > the function > LLM transform), "
                          "review and accept it, and the run creates it; or create it by hand.")


def rewritable(source: Source) -> bool:
    """A deferred multi-statement function that an accepted inline rewrite can bring back into the run."""
    return source.kind == DEFERRED and source.type == "Function" and source.payload is not None


def _catalog(job: Any) -> Any:
    """The SQL pool's catalog from the discovery, when one was read (functions, security, masks)."""
    return getattr(getattr(job, "run", None), "catalog", None)


def _function_source(oid: str, name: str, object_type: str, wave: int, catalog: Any, extra: Any) -> Source:
    meta = dict(extra.metadata or {}) if extra is not None else {}
    schema = str(meta.get("schemaName") or "dbo")
    short = name.split(".")[-1]
    found = catalog.function(schema, short) if catalog is not None and hasattr(catalog, "function") else None
    if found is None:
        return Source(oid, name, object_type, wave, DEFERRED, reason="The function's definition was not kept by discovery. Run discovery again.")
    if found.is_multi_statement:
        # Kept with its definition: an accepted LLM-assisted rewrite (inline table-valued) makes it a function again.
        return Source(oid, name, object_type, wave, DEFERRED, payload=found, schema=schema, object_name=short, reason=(
            MULTI_STATEMENT_REASON))
    return Source(oid, name, object_type, wave, FUNCTION, payload=found, schema=schema, object_name=short)


def _security_source(oid: str, name: str, object_type: str, wave: int, catalog: Any, mapped: Any = None,
                     replacements: Optional[Dict[str, str]] = None) -> Source:
    """A role or user. ``mapped``: the catalog with the SQL users the migration brief replaces (``replacements``,
    by lowercase name) already replaced by their Microsoft Entra principals (``security.replace_sql_users``)."""
    from discovery_agent.migration import security as security_sql

    sec = getattr(catalog, "security", None) if catalog is not None else None
    principal = sec.principal(name) if sec is not None else None
    if principal is None:
        return Source(oid, name, object_type, wave, DEFERRED, reason="The role or user was not kept by discovery. Run discovery again.")
    if principal.is_sql_user:
        replacement = (replacements or {}).get(name.lower())
        if replacement and mapped is not None:
            # Planned, run and validated as the Entra principal that replaces it, with its roles and permissions.
            return Source(oid, name, object_type, wave, SECURITY, payload=mapped, object_name=replacement)
        plan = security_sql.plan_principal(sec, name)
        todo = security_sql.manual_statements(plan)
        roles = [r for r in sec.roles_of(name) if not (sec.principal(r) and sec.principal(r).is_resource_class)]
        steps = (["add it to role(s) " + ", ".join(roles)] if roles else []) + (["run: " + " ".join(todo[:5])] if todo else [])
        return Source(oid, name, object_type, wave, DEFERRED, reason=(
            "SQL user: Fabric has no SQL authentication. Choose the Microsoft Entra user or group that replaces it (in Consult, "
            "or by hand)" + ("; then " + "; ".join(steps) if steps else "") + "."))
    return Source(oid, name, object_type, wave, SECURITY, payload=mapped if mapped is not None else sec)


def sources_for(plan: List[dict], job: Any, pool: Optional[str], principals: Optional[Dict[str, str]] = None,
                masks: Optional[List[str]] = None) -> List[Source]:
    """Turn plan entries into what the runner needs, from the finished discovery. ``principals``: the Microsoft Entra
    user or group that replaces each SQL user, by its name (the migration brief's ``security.sqlUserMap``). ``masks``:
    patterns of schema.table.column whose columns get the default dynamic data mask where Synapse has none (the
    brief's G1 and G1M); a mask only hides, so adding one never shows anything Synapse did not."""
    from discovery_agent.migration import security as security_sql
    from discovery_agent.sql.security import SqlMaskedColumn

    rows = {item["id"]: item for item in job.items}
    artifacts = _artifacts(job)
    catalog = _catalog(job)
    catalog_security = getattr(catalog, "security", None) if catalog is not None else None
    replacements = {str(k).lower(): str(v) for k, v in (principals or {}).items() if v}
    masks_wanted = [str(p).lower() for p in masks or [] if str(p).strip()]
    mapped = security_sql.replace_sql_users(catalog_security, replacements) if catalog_security is not None and replacements else None
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
        if kind == FUNCTION:
            sources.append(_function_source(oid, name, object_type, wave, catalog, extra))
            continue
        if kind == SECURITY:
            sources.append(_security_source(oid, name, object_type, wave, catalog, mapped, replacements))
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
        schema = getattr(key, "schema", None) or record.identity.schema or "dbo"
        object_name = getattr(key, "name", None) or record.identity.name
        masks = tuple(catalog_security.masks_on(schema, object_name)) if kind == TABLE and catalog_security is not None else ()
        if kind == TABLE and masks_wanted:
            have = {str(m.column).lower() for m in masks}
            masks += tuple(SqlMaskedColumn(schema, object_name, c.name, "default()") for c in getattr(content, "columns", None) or ()
                           if str(c.name).lower() not in have and _sensitive(schema, object_name, c.name, masks_wanted))
        sources.append(Source(
            oid, name, object_type, wave, kind, payload=content, schema=schema, object_name=object_name, masks=masks,
        ))
    return sources


def _sensitive(schema: str, table: str, column: str, patterns: List[str]) -> bool:
    """Whether schema.table.column matches one of the brief's sensitive-column patterns (lower case, * for any text)."""
    name = f"{schema}.{table}.{column}".lower()
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


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


#: For each kind, the kinds it needs to exist in Fabric before it can be made. One of those failing in this pass
#: blocks it (a view on a table, a pipeline on the notebooks it runs); kinds that only need them at run time
#: (a notebook reading a table, a procedure, with SQL's deferred name resolution) are never held up.
_BLOCKING_NEEDS = {VIEW: {TABLE, VIEW, FUNCTION}, FUNCTION: {TABLE, VIEW, FUNCTION},
                   PIPELINE: {NOTEBOOK, PIPELINE, SPARKJOB, CONNECTION}, SCHEDULE: {PIPELINE}}
_RUN_ID = re.compile(r"^[0-9]{3,9}$")
_PLAN_ID = re.compile(r"^P[0-9]{3,9}$")


def run_needs(sources: List[Source], job: Any) -> Dict[str, List[str]]:
    """What each object of a run needs from the same run before it can be made: from the dependency graph,
    where making it needs the other in Fabric; and each table's data load needs its table."""
    kinds = {s.id: s.kind for s in sources}
    needs: Dict[str, List[str]] = {}
    for edge in (getattr(job, "graph", None) or {}).get("edges", []):
        source, target = str(edge.get("source")), str(edge.get("target"))
        if source in kinds and target in kinds and kinds[target] in _BLOCKING_NEEDS.get(kinds[source], ()):
            needs.setdefault(source, []).append(target)
    for s in sources:
        if s.kind == DATA and s.id.endswith("#data") and s.id[:-5] in kinds:
            needs.setdefault(s.id, []).append(s.id[:-5])
    return needs


def _no_audit(*args: Any, **kwargs: Any) -> None:
    return None


def _event_line(event: Dict[str, Any]) -> str:
    """A log event as the run's log shows it (``MigrationRun.log``)."""
    return f"{event.get('at')}  {str(event.get('level') or ''):<10}{event.get('object')}: {event.get('message')}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int(value: Any, default: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_request", f"{name} must be a whole number.") from None


class MigrationService:
    def __init__(
        self,
        session: Session,
        fabric: FabricTarget,
        rest_factory: Optional[Callable[[], FabricRestClient]] = None,
        sql_factory: Optional[Callable[[str, str], Any]] = None,
        source_factory: Optional[Callable[[], Any]] = None,
        transforms: Any = None,
        driver: Optional[Callable[[], str]] = None,
        records: Optional[Store] = None,
        audit: Optional[Callable[..., Any]] = None,
        consult: Any = None,
    ) -> None:
        self._session = session
        #: The consultation (``api.consult.ConsultService``): the approved brief a run starts under, and what Plan,
        #: Migrate and Validate take from it. None: no brief is read, and runs start as they did before briefs.
        self._consult = consult
        self._source_factory = source_factory
        #: The ODBC driver the Warehouse sessions would use (``sql_driver``); the tests give one, so they need none installed.
        self._driver = driver or sql_driver
        #: The TransformService, when LLM-assisted transforms are wired in: it attaches accepted ones to the plan items.
        self._transforms = transforms
        #: Connection credentials for the run's life. Memory only: never logged, returned or written.
        self._credentials: Dict[str, Dict[str, str]] = {}
        self._fabric = fabric
        self._lock = threading.Lock()
        self._run: Optional[MigrationRun] = None
        #: The current pass's thread, and every pass thread not yet finished (one may still be tidying up a run
        #: that a new run or a Reset has already closed).
        self._thread: Optional[threading.Thread] = None
        self._threads: List[threading.Thread] = []
        self._counter = 0
        self._history: List[dict] = []
        #: The background validation: its state, progress and, once finished, its rows.
        self._validation: Dict[str, Any] = {"state": "idle"}
        self._validation_thread: Optional[threading.Thread] = None
        #: The last Fabric Git commit (after a run, or by hand), and the thread of one made by hand.
        self._last_commit: Optional[dict] = None
        self._git_thread: Optional[threading.Thread] = None
        self._rest_factory = rest_factory or fabric.rest_client
        self._sql_factory = sql_factory or self._open_sql
        #: The project's durable record (``state.store.Store``): its runs, plan versions, validation and commits.
        #: None: everything is kept in memory only (the tests that build the service on its own).
        self._records = records
        self._runs = RunRecorder(records) if records is not None else None
        #: Records what people and the accelerator do (``state.audit``); never raises.
        self._audit = audit or _no_audit
        #: Changes with every switch of project: a request that read the project before one is refused, never written
        #: into the next project.
        self._generation = 0
        self._load_project()

    def _open_sql(self, host: str, database: str) -> Any:
        """A writable session on a Fabric Warehouse, signed in for the Fabric Target's tenant."""
        config = SqlConnectionConfig(server=host, database=database, application_name="synapse-fabric-migration")
        auth = AccessTokenAuthentication(token_provider=self._fabric.sql_token_provider())
        return PyodbcConnector(config, auth, readonly=False).connect()

    # -- the project's record ---------------------------------------------------------------------------

    def _load_project(self) -> None:
        """What the project keeps comes back: plan versions, the last validation and commit, and the current run,
        which is recovered if the process stopped while it was working."""
        if self._records is None:
            return
        recent = self._records.records("plans", newest_first=True, limit=MAX_HISTORY)
        self._history = [{**p, "id": f"P{p['seq']:03d}"} for p in reversed(recent)]
        validation = self._records.get("validation") or {"state": "idle"}
        if validation.get("state") == "running":
            validation = {"state": "failed", "startedAt": validation.get("startedAt"), "error": {
                "code": "interrupted", "message": "The validation was cut short when the accelerator stopped. Run it again."}}
            self._put("validation", validation)
        self._validation = validation
        self._last_commit = self._records.get("git")
        pointer = self._runs.pointer()
        # After the highest run recorded, whatever the pointer says: a run's record is never written over.
        self._counter = max(pointer["counter"], self._runs.highest())
        if not pointer["current"]:
            return
        loaded = self._runs.load(pointer["current"])
        if loaded is None:
            return
        run = self._restore_run(loaded)
        self._run = run
        if run.status in run_state.WORKING:
            self._recover(run)
        elif run.status not in run_state.TERMINAL:
            # Waiting for sign-off, paused, or finished with failures: whatever carries it on needs the credentials
            # it was given again, since they are never kept.
            with run.lock:
                run.needs_input = self._needed_credentials(run)
            run.save()

    def _restore_run(self, loaded: Dict[str, Any]) -> MigrationRun:
        """A recorded run, rebuilt from the project's discovery when that is still the one the run was made from."""
        document, items = loaded["run"], loaded["items"]
        pins = document.get("pins") or {}
        try:
            job, pool = self._session.migration_snapshot()
        except ApiError:
            job, pool = None, None
        made_from = (pins.get("snapshot") or {}).get("fingerprint")
        now_from = (getattr(job, "snapshot", None) or {}).get("fingerprint") if job is not None else None
        same = job is not None and (made_from is None or made_from == now_from)
        sources = None
        if same:
            options = {"stages": document.get("stages") or list(capabilities.STAGE_KEYS), "scope": document.get("scope") or "all",
                       "dataFilters": parse_data_filters(pins.get("dataFilters"))}
            settings = document.get("settings") or {}
            sources = apply_stages(sources_for(pins.get("plan") or [], job, pool, settings.get("sqlUserMap") or {},
                                               settings.get("maskColumns") or []), options)
            if self._transforms is not None:
                self._transforms.apply(sources)
        run = MigrationRun.restore(document, items, sources, self._runs)
        if job is not None and not same:
            run.resumable = False
            run.unresumable_reason = ("Discovery has been run again since this run was made, so it cannot carry on from where it "
                                      "stopped. Its record is kept; start a new run from the new discovery.")
        run.logs = [_event_line(e) for e in self._runs.events(run.run_id) if e.get("type") == "log"][-2000:]
        return run

    def _recover(self, run: MigrationRun) -> None:
        """The process stopped while this run was working. Put it on hold, honestly, so Resume carries on safely."""
        with context.bound(actor=context.system(), run=run.run_id):
            needed = self._needed_credentials(run)
            with run.lock:
                run.needs_input = needed
            names = run.interrupt()
            run.log("RECOVERED", "run", "The accelerator stopped while this run was working. It is paused; Resume carries on "
                    "from where it stopped" + (f" (it was working on {', '.join(names[:5])})" if names else "") + "."
                    + (f" Enter the credentials for {', '.join(needed)} first: they are never kept." if needed else ""))
            self._audit("run.interrupted", target={"type": "run", "id": run.run_id}, run_id=run.run_id,
                        details={"working": names, "needsInput": needed})

    def _needed_credentials(self, run: MigrationRun) -> List[str]:
        """The connections whose credentials the run was given, still needs for what is left, and does not have."""
        given = set(run.pins.get("credentialNames") or [])
        if not given:
            return []
        waiting = [i.source for i in run.items if i.status in (PENDING, IN_PROGRESS, run_state.ITEM_FAILED, run_state.BLOCKED)]
        needed = {s.name for s in waiting if s.kind == CONNECTION} & given
        if any(s.kind == DATA for s in waiting) and run.source_connection_name in given:
            needed.add(run.source_connection_name)
        return sorted(n for n in needed if n not in self._credentials)

    def _save_pointer(self, current: Optional[str]) -> None:
        if self._runs is not None:
            try:
                self._runs.set_pointer(current, self._counter)
            except OSError as exc:
                _log.warning("Could not save which run is current: %s", exc)

    def _put(self, name: str, value: Any) -> None:
        if self._records is not None:
            try:
                self._records.put(name, value)
            except OSError as exc:
                _log.warning("Could not save %s: %s", name, exc)

    def _busy_reason(self) -> Optional[str]:
        """Why the project cannot be switched now, or None. Call with ``_lock`` held."""
        if self._busy():
            return "A migration run is working; pause it or wait for it to finish."
        if any(t.is_alive() for t in self._threads):
            return "A migration run is finishing; try again in a moment."
        if self._validation_thread is not None and self._validation_thread.is_alive():
            return "Validation is running; wait for it to finish."
        if self._git_thread is not None and self._git_thread.is_alive():
            return "A Fabric Git commit is running; wait for it to finish."
        return None

    def busy_reason(self) -> Optional[str]:
        """Why the project cannot be switched now, or None."""
        with self._lock:
            return self._busy_reason()

    def switch(self, records: Optional[Store]) -> None:
        """Hold another project's runs, plan versions, validation and commits instead.

        The check, the swap and the load happen under one hold of the lock, so nothing (a Start from another page)
        can land in between: it waits, then finds the new project, or is refused as made for the old one."""
        with self._lock:
            reason = self._busy_reason()
            if reason:
                raise ApiError(409, "migration_busy", reason)
            self._generation += 1
            self._records = records
            self._runs = RunRecorder(records) if records is not None else None
            self._run, self._thread, self._threads, self._credentials = None, None, [], {}
            self._history, self._validation, self._last_commit, self._counter = [], {"state": "idle"}, None, 0
            self._load_project()  # takes no lock of this service's

    def _same_project(self, generation: int) -> None:
        """Refuse to write what was read from a project the service no longer holds. Call with ``_lock`` held."""
        if generation != self._generation:
            raise ApiError(409, "project_switched", "The project was switched while this was being prepared. Try again.")

    def _brief(self) -> Any:
        """The approved migration brief's policy (``consult.policy.Policy``), or None. Never raises."""
        if self._consult is None:
            return None
        try:
            return self._consult.in_force()
        except Exception as exc:  # noqa: BLE001 - a brief that cannot be read is said by Consult; here it is no brief
            _log.warning("Could not read the migration brief: %s: %s", type(exc).__name__, exc)
            return None

    def _principals(self, policy: Any = None) -> Dict[str, str]:
        """The Microsoft Entra principal that replaces each SQL user, as the approved brief says."""
        policy = policy if policy is not None else self._brief()
        return policy.sql_user_map() if policy is not None else {}

    def _masks(self, policy: Any = None) -> List[str]:
        """The sensitive columns the approved brief masks, as schema.table.column patterns."""
        policy = policy if policy is not None else self._brief()
        return policy.mask_columns() if policy is not None else []

    @staticmethod
    def _signoff_rule(brief: Any) -> str:
        return approvals.rule_for_policy(brief)

    # -- capacity and blocking risks ----------------------------------------------------------------

    def _check_capacity(self, workspace_id: str) -> None:
        """Refuse up front when the workspace cannot hold Fabric items at all."""
        try:
            workspace = self._rest_factory().get(f"/workspaces/{workspace_id}")
        except FabricApiError as exc:
            raise ApiError(502, "fabric_error", f"The Fabric workspace could not be read: {exc.message}") from exc
        if not workspace.get("capacityId"):
            raise ApiError(409, "no_fabric_capacity", NO_CAPACITY + ".")

    def _refuse_blocking(self, plan: List[dict], options: dict, credentials: Dict[str, Dict[str, str]], job: Any,
                         pool: Optional[str]) -> dict:
        """Refuse a run the planner finds a blocking risk in: one that would fail or do the wrong thing. Returns the
        planner's analysis, so the run can say what it was started from.

        The Plan step holds Start back for the same reason; this is the same rule where no page can skip it,
        worked out again on what this run is given, so a plan scored earlier cannot slip one through."""
        result, _ = self._assessment(plan, options, set(credentials), job, pool)
        blocking = [r for r in result["risks"] if r["severity"] == planning.BLOCKING]
        if blocking:
            shown = " ".join(r["message"] for r in blocking[:3])
            more = f" There {'is' if len(blocking) == 4 else 'are'} {len(blocking) - 3} more in Plan." if len(blocking) > 3 else ""
            raise ApiError(409, "plan_blocked", f"The run was not started: the plan has {len(blocking)} blocking "
                           f"risk{'s' if len(blocking) != 1 else ''}. {shown}{more} Resolve them in Plan, then start the run.")
        return result

    # -- routes ------------------------------------------------------------

    def state(self) -> dict:
        run = self._run
        return run.to_dict() if run is not None else dict(IDLE)

    def policy(self) -> dict:
        """How runs and their objects move, and the sign-off rule in force: what the page reads instead of a copy."""
        brief = self._brief()
        return {**run_state.describe(), "signoff": self._signoff_rule(brief),
                "signoffPolicies": list(approvals.POLICIES), "signoffConfigured": approvals.configured(),
                "failureCategories": {c: failures.MEANING[c] for c in failures.CATEGORIES}}

    def _assessment(self, plan: List[dict], options: dict, credential_names: Set[str], job: Any,
                    pool: Optional[str]) -> Tuple[dict, List[Source]]:
        """The planner's analysis of a plan run with these options, and the sources it read. Reads, never writes.

        ``analyze`` shows it on the Plan step; ``start`` refuses a run it finds a blocking risk in."""
        brief = self._brief()
        every = sources_for(plan, job, pool, self._principals(brief), self._masks(brief))
        stages = set(options["stages"])
        sources = apply_stages(every, {**options, "scope": "automated"})
        if self._transforms is not None:
            self._transforms.apply(sources)
        objects = plan_objects(plan, job, every, stages)
        # Asked only when the plan has table data to load or external tables: the Table data and External tables
        # stages are on by default, and a source connected without a SQL pool has nothing for them to read.
        checks = environment_checks(self._fabric.state(), self._driver(),
                                    source_connected=self._source_available() if needs_synapse_source(sources) else None)
        # A connection already in Fabric is reused by the run (skipped), so it needs no credentials either.
        names = set(credential_names) | set(self._credentials) | self._existing_connection_names()
        return planning.analyze(objects, content_findings(sources, names, pool or "", options["storageRoutes"]), checks), sources

    def analyze(self, body: dict) -> dict:
        """The planner's strategy, risks, effort and checks for a plan. Reads, never writes (but for the plan
        version it records with ``record``)."""
        plan = body.get("items")
        if not isinstance(plan, list) or not all(isinstance(p, dict) for p in plan) or len(plan) > MAX_ITEMS:
            raise ApiError(400, "invalid_plan", "The migration plan is not in a valid format.")
        if "credentials" in body:
            # Refused, not dropped: credentials travel once, with the run. Planning needs only whose they are.
            raise ApiError(400, "credentials_not_accepted", "Credentials are sent only when a run starts. To plan, send the "
                           "names of the linked services that have them (credentialNames).")
        options = parse_options(body)
        names = parse_credential_names(body.get("credentialNames"))
        generation = self._generation
        job, pool = self._session.migration_snapshot()
        result, sources = self._assessment(plan, options, names, job, pool)
        result["storage"] = self._storage_suggestions(sources, job)
        result["options"] = {**options, "dataFilters": {k: f.to_dict() for k, f in options["dataFilters"].items()}}
        entries = [{"id": str(p.get("id") or ""), "wave": _int(p.get("wave"), 1, "wave")} for p in plan]
        result["brief"] = self._consult.check_plan(entries, options, job, pool) if self._consult is not None else None
        if body.get("record"):
            self._record_plan(plan, result, job, generation)
        with self._lock:
            result["history"] = [{k: v for k, v in h.items() if k not in ("items", "options", "seq")} for h in reversed(self._history)]
        return result

    def _record_plan(self, plan: List[dict], result: dict, job: Any, generation: int) -> None:
        """Keep this plan as the project's next plan version: what was planned, from which discovery, by whom."""
        snapshot = getattr(job, "snapshot", None) or {}
        entry = {
            "plannerVersion": result["plannerVersion"], "fingerprint": result["fingerprint"], "objects": result["objects"],
            "effortDays": result["effortDays"], "waves": len(result["waves"]), "readiness": result["readiness"],
            "blocking": result["blocking"], "createdAt": _now(), "status": "COMPLETED", "snapshot": snapshot.get("id"),
            "by": context.actor().to_dict(),
            # The plan itself, so a version can be read back: its objects and waves, and the options (never credentials).
            "items": [{"id": str(p.get("id") or ""), "wave": _int(p.get("wave"), 1, "wave")} for p in plan],
            "options": dict(result["options"]),
        }
        with self._lock:
            self._same_project(generation)
            if self._records is not None:
                try:
                    recorded = self._records.append("plans", entry)
                    entry = {**recorded, "id": f"P{recorded['seq']:03d}"}
                except OSError as exc:
                    _log.warning("Could not record the plan version: %s", exc)
            entry.setdefault("id", f"P{len(self._history) + 1:03d}")
            self._history = (self._history + [entry])[-MAX_HISTORY:]
        self._audit("plan.recorded", target={"type": "plan", "id": entry["id"]}, details={
            "fingerprint": entry["fingerprint"], "objects": entry["objects"], "readiness": entry["readiness"],
            "blocking": entry["blocking"], "snapshot": entry.get("snapshot")})

    def latest_plan(self) -> Optional[dict]:
        """The latest recorded plan version, with its objects and options (never credentials), or None: what the
        orchestrator's execution plan rests on, and what it starts a run of."""
        with self._lock:
            latest = dict(self._history[-1]) if self._history else None
        if latest is not None:
            latest.pop("seq", None)
        return latest

    def plan_version(self, version_id: str) -> dict:
        """One recorded plan version, with its objects and options."""
        if not _PLAN_ID.match(version_id or ""):
            raise ApiError(404, "not_found", "There is no such plan version.")
        found = next((dict(h) for h in self._history if h.get("id") == version_id), None)
        if found is None and self._records is not None:
            seq = int(version_id[1:])
            found = next(({**r, "id": version_id} for r in self._records.records("plans", after=seq - 1, limit=1)
                          if r["seq"] == seq), None)
        if found is None:
            raise ApiError(404, "not_found", "There is no such plan version.")
        found.pop("seq", None)
        return found

    def _fabric_connections(self) -> List[Dict[str, Any]]:
        """Fabric connections the signed-in user can see; empty when the target is not connected or cannot be read."""
        try:
            if self._fabric.state().get("status") == "connected":
                return list(self._rest_factory().list("/connections"))
        except Exception:  # noqa: BLE001 - planning reads this as a hint; it never fails a plan
            pass
        return []

    def _existing_connection_names(self) -> set:
        return {str(c.get("displayName") or "") for c in self._fabric_connections()} - {""}

    def _storage_suggestions(self, sources: List[Source], job: Any) -> List[dict]:
        """Storage locations the plan's code reads directly, each with the connection a OneLake shortcut can use.

        Candidates: the Fabric ADLS / Blob connections already there (when the target is connected), then the Synapse
        storage linked services, whose connections the Connections stage creates in the same run."""
        texts = [preflight_code_text(s) for s in sources]
        found = storage_remap.locations(texts)
        if not found and not any(storage_remap.uses_storage(t) for t in texts):
            return []
        candidates: List[Dict[str, Any]] = []
        for c in self._fabric_connections():
            details = c.get("connectionDetails") or {}
            if storage_remap.connection_location(str(details.get("path") or "")):
                candidates.append({"id": str(c.get("id") or ""), "name": str(c.get("displayName") or ""),
                                   "path": str(details.get("path") or "")})
        known = {c["name"] for c in candidates}
        for name, ls in (_artifacts(job).get(P0Artifact.LINKED_SERVICE) or {}).items():
            props = (ls.get("properties") or {})
            url = str((props.get("typeProperties") or {}).get("url") or "")
            if props.get("type") in ("AzureBlobFS", "AzureBlobStorage") and storage_remap.connection_location(url) and name not in known:
                candidates.append({"id": None, "name": f"{name} (created by the Connections stage)", "path": url, "linkedService": name})
        return storage_remap.suggest(found, candidates)

    # -- a run's life -------------------------------------------------------------------------------

    def start(self, body: dict) -> dict:
        try:
            return self._start(body)
        except (ApiError, FabricError) as exc:
            self._audit("run.refused", target={"type": "run", "id": None}, outcome="refused", code=exc.code,
                        details={"message": exc.message})
            raise

    def _start(self, body: dict) -> dict:
        plan = body.get("items")
        if not isinstance(plan, list) or not plan:
            raise ApiError(400, "empty_plan", "The migration plan is empty.")
        if len(plan) > MAX_ITEMS or not all(isinstance(p, dict) for p in plan):
            raise ApiError(400, "invalid_plan", "The migration plan is not in a valid format.")
        options = parse_options(body)
        credentials = parse_credentials(body.pop("credentials", None))
        if credentials:
            _person_enters_credentials()
        # Everything that can refuse the request is checked before anything changes: a refused Start never touches
        # the current run.
        entries = [{"id": str(p.get("id") or ""), "wave": _int(p.get("wave"), 1, "wave")} for p in plan]
        scope, stop_on_failure = options["scope"], options["stopOnFailure"]
        generation = self._generation
        job, pool = self._session.migration_snapshot()
        workspace_id, workspace_name, method = self._fabric.migration_target()
        if self._busy():
            raise ApiError(409, "run_in_progress", "A migration run is already in progress. Pause it or wait for it to finish.")
        # The brief the run starts under: refused here, under a required consultation, without an approved one that fits
        # the estate, or with differences from it that nobody explained.
        gate, brief, explained = (self._consult.run_gate(entries, options, job, pool, body.get("deviationReason"))
                                  if self._consult is not None else ({"mode": None, "version": None, "differences": []}, None, None))
        self._check_capacity(workspace_id)
        analysis = self._refuse_blocking(plan, options, credentials, job, pool)
        with self._lock:
            self._same_project(generation)
            if self._busy():
                raise ApiError(409, "run_in_progress", "A migration run is already in progress. Pause it or wait for it to finish.")
            principals, masks = self._principals(brief), self._masks(brief)
            sources = apply_stages(sources_for(plan, job, pool, principals, masks), options)
            if not [s for s in sources if s.kind not in (DEFERRED, MISSING)]:
                raise ApiError(400, "nothing_to_migrate", "No object in the plan can be created by this build. Choose scope All to see them listed as deferred.")
            transform_log = self._transforms.apply(sources) if self._transforms is not None else []
            named_warehouse, named_lakehouse = brief.names() if brief is not None else (None, None)
            warehouse = named_warehouse or warehouse_name_for(pool)
            run = MigrationRun(f"{self._counter + 1:03d}", sources, workspace_id, workspace_name, warehouse,
                               recorder=self._runs, needs=run_needs(sources, job))
            via = "Azure CLI" if method == "azure_cli" else "Fabric CLI"
            run.scope, run.stop_on_failure = scope, stop_on_failure
            run.stages, run.pool_name = list(options["stages"]), pool or ""
            run.settings = {"dataMode": options["dataMode"], "dataRun": options["dataRun"], "collation": options["collation"],
                            "existing": options["existing"], "storageRoutes": options["storageRoutes"],
                            "syncOverlap": options["syncOverlap"], "gitCommit": options["gitCommit"],
                            "commitScope": options["commitScope"], "waveGate": options["waveGate"],
                            "lakehouse": lakehouse_of(warehouse, {"lakehouse": named_lakehouse}), "sqlUserMap": principals,
                            # Taken from the brief itself, as the names are (C8, E5, G1 and G1M, E1).
                            "parallelCopies": (brief.parallel_copies() if brief is not None else None) or datapipeline.PARALLEL_COPIES,
                            "schedules": (brief.schedules() if brief is not None else None) or "off",
                            "maskColumns": masks, "targetRole": brief.target_role() if brief is not None else None}
            self._describe_source(run, job, pool)
            snapshot = getattr(job, "snapshot", None) or {}
            # What the run was made from, so its record can say, and a later reader can check, exactly that.
            run.pins = {"plan": entries, "plannerFingerprint": analysis.get("fingerprint"), "readiness": analysis.get("readiness"),
                        "snapshot": {"id": snapshot.get("id"), "fingerprint": snapshot.get("fingerprint")} if snapshot else None,
                        "credentialNames": sorted(credentials), "signedInWith": via,
                        "dataFilters": {k: f.to_dict() for k, f in options["dataFilters"].items()},
                        # The migration brief it starts under, by version and hash, and how the run differs from it.
                        "brief": gate}
            rule = self._signoff_rule(brief)
            reason = approvals.required(run.settings, run.stages, rule=rule)
            run.approval = approvals.request(reason, approvals.subject_hash(entries, run.settings, run.stages), rule=rule)
            # Nothing is left to refuse: the new run replaces the current one, which is closed and kept in the history.
            if self._run is not None:
                self._close_run(self._run, "replaced by a new run")
            self._counter += 1
            if credentials:
                self._credentials = credentials
            self._run = run
            if explained and brief is not None:
                # The reason given with this request for differing from the brief, kept now that the run exists.
                recorded = self._consult.record_deviation(brief, gate.get("differences") or [], explained)
                gate["deviation"] = {k: recorded.get(k) for k in ("seq", "reason", "by", "at")}
            run.save()
            self._save_pointer(run.run_id)
            run.log("RUN", "run", f"{len(sources)} objects, workspace {workspace_name}, warehouse {run.warehouse}, signed in with {via}")
            if gate.get("version") is not None:
                differs = gate.get("differences") or []
                run.log("BRIEF", "run", f"Under migration brief version {gate['version']}" + (
                    f"; it differs in {len(differs)} way(s), explained: {(gate.get('deviation') or {}).get('reason') or 'not required'}"
                    if differs else ", with nothing changed from it"))
            reviewed = sum(1 for s in sources if s.override)
            if reviewed:
                run.log("NOTE", "run", f"{reviewed} object{'s' if reviewed != 1 else ''} use{'s' if reviewed == 1 else ''} an accepted LLM-assisted transform")
            for line in transform_log:
                run.log("NOTE", "run", line)
            self._audit("run.requested", target={"type": "run", "id": run.run_id}, run_id=run.run_id, details={
                "objects": len(sources), "stages": list(run.stages), "existing": options["existing"],
                "dataMode": options["dataMode"], "dataRun": options["dataRun"], "credentialNames": sorted(credentials),
                "workspace": workspace_name, "warehouse": run.warehouse, "plannerFingerprint": analysis.get("fingerprint"),
                "snapshot": (run.pins.get("snapshot") or {}).get("id"), "brief": gate.get("version"),
                "briefHash": gate.get("hash"), "differences": len(gate.get("differences") or []), "signoffRule": rule})
            if reason:
                run.transition(run_state.AWAITING_APPROVAL, reason)
                run.log("SIGN-OFF", "run", f"Waiting for a person to sign it off. {reason}")
                self._audit("run.signoff_requested", target={"type": "run", "id": run.run_id}, run_id=run.run_id,
                            details={"reason": reason, "policy": run.approval["policy"]})
                return run.to_dict()
            self._audit("run.approved", target={"type": "run", "id": run.run_id}, run_id=run.run_id, actor=context.system(),
                        details={"automatic": True, "policy": run.approval["policy"]})
            self._spawn(run, "start")
            return run.to_dict()

    def approve(self, body: dict) -> dict:
        """A person signs a waiting run off (``decision: approve``), and it starts; or declines it (``reject``)."""
        decision = str(body.get("decision") or "")
        if decision not in ("approve", "reject"):
            raise ApiError(400, "invalid_request", "Choose approve or reject.")
        # Before anything is taken from the request (the credentials that come with a sign-off among them).
        if context.actor().kind != context.PERSON:
            raise ApiError(403, "person_only", "Only a person signs a run off, or declines it.")
        with self._lock:
            run = self._run
            if run is None or (body.get("runId") and str(body.get("runId")) != run.run_id):
                raise ApiError(409, "no_run", "There is no such run waiting for sign-off.")
            if run.status != run_state.AWAITING_APPROVAL:
                raise ApiError(409, "not_waiting", "This run is not waiting for sign-off.")
            asked = run.approval or {}
            digest = approvals.subject_hash(run.pins.get("plan") or [], run.settings, run.stages)
            if digest != asked.get("subjectHash") or (body.get("subjectHash") and body.get("subjectHash") != digest):
                raise ApiError(409, "stale", "What is signed off is not what this run would do. Read the run again.")
            if decision == "approve":
                if not run.resumable:
                    raise ApiError(409, "not_resumable", run.unresumable_reason
                                   or "This run cannot run any more. Decline it, or close it with Reset.")
                changed = self._consult.still_holds(run.pins.get("brief")) if self._consult is not None else None
                if changed:
                    raise ApiError(409, "brief_changed", changed)
                self._take_credentials(run, body)
            try:
                record = approvals.decide(asked, decision == "approve", str(body.get("comment") or ""))
            except PermissionError as exc:
                raise ApiError(403, "person_only", str(exc)) from exc
            with run.lock:
                run.approval = record
            if self._records is not None:
                try:
                    self._records.append("approvals", {"subject": {"type": "run", "id": run.run_id, "hash": digest},
                                                       "decision": record["decision"], "by": record["by"], "at": record["at"],
                                                       "comment": record["comment"]})
                except OSError as exc:
                    _log.warning("Could not record the sign-off: %s", exc)
            said = f": {record['comment']}" if record["comment"] else ""
            details = {"comment": record["comment"], "subjectHash": digest}
            if decision == "reject":
                run.transition(run_state.REJECTED, "declined at sign-off")
                run.log("REJECTED", "run", f"Declined at sign-off by {record['by']['id']}{said}")
                self._audit("run.rejected", target={"type": "run", "id": run.run_id}, run_id=run.run_id,
                            reason=record["comment"], details=details)
                return run.to_dict()
            with run.lock:
                run.needs_input = []
            run.transition(run_state.QUEUED, "signed off")
            run.log("APPROVED", "run", f"Signed off by {record['by']['id']}{said}")
            self._audit("run.approved", target={"type": "run", "id": run.run_id}, run_id=run.run_id,
                        reason=record["comment"], details={**details, "automatic": False})
            self._spawn(run, "start")
            return run.to_dict()

    def control(self, body: dict) -> dict:
        action = str(body.get("action") or "")
        if action == "reset":
            return self.reset()
        with self._lock:
            run = self._run
            if run is None:
                raise ApiError(409, "no_run", "There is no migration run.")
            expected = body.get("version")
            if expected is not None and _int(expected, -1, "version") != run.version:
                raise ApiError(409, "stale", "The run changed since this page read it. Read it again, then try once more.")
            self._settle(run)
            if action == "pause":
                if run.status in run_state.WORKING:
                    run.pause.set()
                    run.log("PAUSE", "run", "pause requested; stopping after the current object")
                    self._audit("run.pause_requested", target={"type": "run", "id": run.run_id}, run_id=run.run_id)
            elif action == "resume":
                if run.status == run_state.PAUSED and not self._busy():
                    # A run waiting for a wave's sign-off: this Resume, by a person, is that sign-off, for that wave only.
                    wave = run.waiting_wave()
                    if wave is not None and context.actor().kind != context.PERSON:
                        raise ApiError(403, "person_only", f"Only a person can sign off wave {wave} and let it start.")
                    self._ready_to_carry_on(run, body)
                    run.pause.clear()
                    with run.lock:
                        if wave is not None:
                            run.released_waves.append(wave)
                        else:
                            run.halt_waived = True
                        run.halted_reason, run.needs_input = None, []
                    run.transition(run_state.QUEUED, "wave signed off" if wave is not None else "resume requested")
                    if wave is not None:
                        self._sign_off_wave(run, wave, str(body.get("comment") or ""))
                    run.log("RESUME", "run", "resumed" if wave is None else f"resumed: wave {wave} signed off")
                    self._audit("run.resumed", target={"type": "run", "id": run.run_id}, run_id=run.run_id)
                    self._spawn(run, "resume")
            elif action == "retry":
                if self._busy():
                    raise ApiError(409, "run_in_progress", "Wait for the run to finish or pause it before retrying.")
                if run.status == run_state.AWAITING_APPROVAL:
                    raise ApiError(409, "not_signed_off", "This run is waiting for sign-off; sign it off before it runs.")
                if run.status in run_state.TERMINAL:
                    return run.to_dict()  # a completed, cancelled or declined run is done: nothing carries it on
                self._ready_to_carry_on(run, body)
                again = [i for i in run.items if i.status in run_state.NEEDS_ATTENTION]
                for item in again:
                    run.update(item, status=PENDING, step="Waiting", error=None, started_at=None, completed_at=None, category=None)
                run.pause.clear()
                with run.lock:
                    run.halt_waived, run.halted_reason, run.needs_input = False, None, []
                run.transition(run_state.QUEUED, "retry requested")
                run.log("RETRY", "run", f"retrying {len(again)} failed objects")
                self._audit("run.retried", target={"type": "run", "id": run.run_id}, run_id=run.run_id,
                            details={"objects": len(again)})
                self._spawn(run, "retry")
            else:
                raise ApiError(400, "invalid_action", "Choose pause, resume, retry or reset.")
            return run.to_dict()

    def _sign_off_wave(self, run: MigrationRun, wave: int, comment: str) -> None:
        """Record that a person let the next wave of a run start: in the approvals, and the trail."""
        said = " ".join(comment.split())[:500] or None
        who = context.actor().to_dict()
        if self._records is not None:
            try:
                self._records.append("approvals", {"subject": {"type": "wave", "id": f"{run.run_id}/{wave}"}, "decision": "approved",
                                                   "by": who, "at": _now(), "comment": said})
            except OSError as exc:
                _log.warning("Could not record the wave's sign-off: %s", exc)
        self._audit("run.wave_signed_off", target={"type": "run", "id": run.run_id}, run_id=run.run_id, reason=said,
                    details={"wave": wave, "comment": said})

    def _settle(self, run: MigrationRun) -> None:
        """A run whose pass has no thread left is never shown as working: what it was doing goes back in the queue."""
        if run.status in run_state.WORKING and not (self._thread is not None and self._thread.is_alive()):
            run.abort("Its pass ended without finishing.")

    def _take_credentials(self, run: MigrationRun, body: dict) -> None:
        """Credentials sent with a request that carries a run on, taken for the run's life, in memory only; and a
        refusal while any it needs are missing (they were lost with a restart: they are never kept)."""
        given = parse_credentials(body.get("credentials"))
        if given:
            _person_enters_credentials()
            self._credentials.update(given)
        missing = [n for n in run.needs_input if n not in self._credentials]
        if missing:
            raise ApiError(409, "needs_input", f"Enter the credentials for {', '.join(missing)} in Plan, Stages & credentials, "
                           "then carry on. They are never kept, so a restart of the accelerator loses them.")

    def _ready_to_carry_on(self, run: MigrationRun, body: dict) -> None:
        """Refuse to carry a run on that cannot: the discovery it was made from is gone, or it needs credentials."""
        if not run.resumable:
            raise ApiError(409, "not_resumable", run.unresumable_reason or "This run cannot carry on.")
        self._take_credentials(run, body)

    def _close_run(self, run: MigrationRun, reason: str) -> None:
        """The run stops being the project's current one. One that was not done is cancelled; its record stays."""
        run.pause.set()  # a closed run can never be resumed
        self._settle(run)
        if run.status not in run_state.TERMINAL:
            run.transition(run_state.CANCELLED, reason)
        with run.lock:
            run.closed_at = _now()
        run.save()
        run.log("CLOSED", "run", f"{reason}; its record is kept in the run history")
        self._audit("run.closed", target={"type": "run", "id": run.run_id}, run_id=run.run_id,
                    reason=reason, details={"status": run.status})

    def reset(self) -> dict:
        """Close the current run: it stops being the one Migrate shows, and its record stays in the run history.
        The credentials it was given are forgotten. Objects already created in Fabric stay there: a new run skips
        what exists, it never deletes or overwrites."""
        with self._lock:
            if self._busy():
                raise ApiError(409, "run_in_progress",
                               "The run is still working. Pause it, wait for the current object to finish, then reset.")
            if self._run is not None:
                self._close_run(self._run, "closed by Reset")
            self._run = None
            self._credentials = {}
            self._save_pointer(None)
        return dict(IDLE)

    # -- the run history ------------------------------------------------------------------------------

    def runs(self) -> dict:
        """Every run of the project, newest first, and which is current."""
        current = self._run
        listed = self._runs.runs() if self._runs is not None else []
        if current is not None:
            mine = self._summary(current)
            listed = [mine if r.get("runId") == current.run_id else r for r in listed]
            if not any(r.get("runId") == current.run_id for r in listed):
                listed.insert(0, mine)
        return {"runs": listed, "current": current.run_id if current is not None else None}

    @staticmethod
    def _summary(run: MigrationRun) -> dict:
        document = run.document()
        summary = {k: document.get(k) for k in ("runId", "status", "pausedReason", "createdAt", "startedAt", "finishedAt",
                                                "closedAt", "workspace", "warehouse", "requestedBy", "counts", "approval")}
        return {**summary, "passes": len(run.passes)}

    def run_record(self, run_id: str) -> dict:
        """One run as its record has it: the current one live, any other as it was left."""
        run = self._run
        if run is not None and run.run_id == run_id:
            return run.to_dict()
        loaded = self._runs.load(run_id) if self._runs is not None and _RUN_ID.match(run_id or "") else None
        if loaded is None:
            raise ApiError(404, "not_found", "There is no such run.")
        shell = MigrationRun.restore(loaded["run"], loaded["items"], None, None)
        shell.logs = [_event_line(e) for e in self._runs.events(run_id) if e.get("type") == "log"][-2000:]
        shell.unresumable_reason = "This run is not the current one; its record is kept as history."
        return shell.to_dict()

    def run_events(self, run_id: str, after: Any = None, limit: Any = None) -> dict:
        """A run's events in order, after ``after``: every log line, change of status and change to an object."""
        if self._runs is None or not _RUN_ID.match(run_id or "") or self._runs.store.get(f"runs/{run_id}/run") is None:
            raise ApiError(404, "not_found", "There is no such run.")
        start = max(0, _int(after, 0, "after"))
        events = self._runs.events(run_id, after=start, limit=max(1, min(_int(limit, 500, "limit"), 2000)))
        return {"runId": run_id, "events": events, "last": events[-1]["seq"] if events else start}

    def artifact(self, run_id: str, digest: str) -> Tuple[bytes, str, str]:
        """What a run sent to Fabric for one of its objects, as it was kept: a definition (JSON) or a statement (SQL).
        Only what the run's own record names can be read. Returns (bytes, media type, file name)."""
        record = self.run_record(run_id)
        part = next((p for i in record.get("items") or [] for p in ((i.get("artifact") or {}).get("parts") or [])
                     if p.get("hash") == digest), None)
        data = self._records.blob(digest) if part is not None and self._records is not None else None
        if data is None:
            raise ApiError(404, "not_found", "This run kept nothing under that name.")
        sql = part.get("kind") == "sql"
        return data, "text/plain; charset=utf-8" if sql else "application/json", f"run-{run_id}-{digest[:12]}.{'sql' if sql else 'json'}"

    # -- internals ---------------------------------------------------------

    def _busy(self) -> bool:
        """A pass is working on objects. Once the run reports completed or paused, its thread only
        tidies up (closing sessions, handing the commit to its own thread), so the run is free."""
        if self._thread is None or not self._thread.is_alive():
            return False
        return self._run is not None and self._run.status in run_state.WORKING

    def runtime_refusal(self, runtime: Any) -> Optional[str]:
        """Why Validate may not really run what ``runtime.run`` names now (the brief's H4, unless K10 was kept), or None."""
        runs = [str(k) for k in (runtime or {}).get("run") or []] if isinstance(runtime, dict) else []
        brief = self._brief() if runs else None
        return brief.external_writes_refusal(runs) if brief is not None else None

    def _refuse_runtime(self, body: dict) -> None:
        refused = self.runtime_refusal(body.get("runtime"))
        if refused:
            raise ApiError(409, "external_writes", refused)

    def start_validation(self, body: dict) -> dict:
        """Validate in the background, so runs in Fabric can be followed for as long as they take. Poll ``validation_state``."""
        self._session.migration_snapshot()       # refuse now, not in the thread, when there is nothing to validate
        self._fabric.migration_target()
        self._refuse_runtime(body)
        with self._lock:
            if self._validation_thread is not None and self._validation_thread.is_alive():
                raise ApiError(409, "validation_running", "Validation is already running; wait for it to finish.")
            started = _now()
            self._validation = {"state": "running", "progress": "Starting", "startedAt": started}
            self._put("validation", self._validation)
            runtime = body.get("runtime") if isinstance(body.get("runtime"), dict) else {}
            self._audit("validation.started", target={"type": "validation", "id": started}, details={
                "probe": bool(runtime.get("probe", True)) if runtime else False,
                "run": [str(k) for k in runtime.get("run") or []] if isinstance(runtime.get("run"), list) else [],
                "objects": len(body["items"]) if isinstance(body.get("items"), list) else None})

            def work() -> None:
                def progress(text: str) -> None:
                    self._validation = {**self._validation, "progress": text}
                try:
                    out = self.validate(body, progress=progress)
                    self._validation = {"state": "completed", "startedAt": started, "finishedAt": _now(), **out}
                    self._audit("validation.completed", target={"type": "validation", "id": started}, actor=context.system(),
                                details={"summary": out.get("summary")})
                except ApiError as exc:
                    self._validation = {"state": "failed", "startedAt": started, "error": {"code": exc.code, "message": exc.message}}
                    self._audit("validation.completed", target={"type": "validation", "id": started}, actor=context.system(),
                                outcome="failed", code=exc.code, details={"message": exc.message})
                except Exception as exc:  # noqa: BLE001 - reported, never raised into the thread; the API's log gets the traceback
                    reference = record_unexpected("validation", exc)
                    self._validation = {"state": "failed", "startedAt": started, "error": {
                        "code": "validation_failed", "reference": reference,
                        "message": f"Validation stopped: {type(exc).__name__}: {redacted(str(exc)[:300])} (reference {reference}). "
                                   "The details are in the API's log."}}
                    self._audit("validation.completed", target={"type": "validation", "id": started}, actor=context.system(),
                                outcome="failed", code="validation_failed", details={"message": self._validation["error"]["message"]})
                finally:
                    self._put("validation", self._validation)

            snapshot = dict(self._validation)  # before the thread starts: a quick validation may finish first
            self._validation_thread = context.spawn(work, name="validation")
            return snapshot

    def validation_state(self) -> dict:
        return dict(self._validation)

    def validate(self, body: dict, progress: Optional[Callable[[str], None]] = None) -> dict:
        """Compare the discovered Synapse objects with what is now in Fabric, and (with ``runtime``) whether they run there.

        Reads both sides. Writes nothing, unless ``runtime.run`` names kinds to run now in Fabric."""
        plan = body.get("items")
        if plan is not None and (not isinstance(plan, list) or not all(isinstance(p, dict) for p in plan) or len(plan) > MAX_ITEMS):
            raise ApiError(400, "invalid_plan", "The list of objects to validate is not in a valid format.")
        job, pool = self._session.migration_snapshot()
        workspace_id, workspace_name, _ = self._fabric.migration_target()
        self._refuse_runtime(body)
        plan = plan or [{"id": i["id"], "wave": 1} for i in job.items]
        given = body.get("options") if isinstance(body.get("options"), dict) else {}
        # Compared with what the run made: its Warehouse and its principals, else what the approved brief names.
        run, brief = self._run, self._brief()
        made = run if run is not None and run.workspace_id == workspace_id else None
        # The rows each table was loaded with: the date filters the request names, else the ones the run itself was
        # given (the agent's validations name none), so a table loaded from a date is never counted against all of it.
        filters = parse_data_filters(given["dataFilters"] if "dataFilters" in given
                                     else (made.pins.get("dataFilters") if made is not None else None))
        # The run's own map, even an empty one: what it made is what is checked, not what the brief says now.
        principals = (made.settings.get("sqlUserMap") or {}) if made is not None else self._principals(brief)
        masks = (made.settings.get("maskColumns") or []) if made is not None else self._masks(brief)
        sources = apply_stages(sources_for(plan, job, pool, principals, masks),
                               {"stages": list(capabilities.STAGE_KEYS), "scope": "all", "dataFilters": filters})
        factory = self._source_factory or getattr(self._session, "source_sql_factory", lambda: None)()
        named_warehouse, named_lakehouse = brief.names() if brief is not None else (None, None)
        warehouse = made.warehouse if made is not None else (named_warehouse or warehouse_name_for(pool))
        lakehouse = lakehouse_of(warehouse, made.settings if made is not None else {"lakehouse": named_lakehouse})
        validator = Validator(
            self._rest_factory(), workspace_id, workspace_name, warehouse, self._sql_factory, factory,
            artifacts={"pipelines": _artifacts(job).get(P0Artifact.PIPELINE, {})},
            runtime=RuntimeOptions.parse(body["runtime"]) if isinstance(body.get("runtime"), dict) else None,
            progress=progress or (lambda text: None), lakehouse_name=lakehouse)
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

    def _spawn(self, run: MigrationRun, trigger: str = "start") -> None:
        """Start a pass over the run's pending objects on its own thread. Call with ``_lock`` held."""
        factory = self._source_factory or getattr(self._session, "source_sql_factory", lambda: None)()
        files = None
        if run.settings.get("storageRoutes"):
            try:
                files = OneLakeFiles(self._fabric.storage_token_provider())
            except Exception:  # noqa: BLE001 - only Spark job main files need it; without it they are reported, not copied
                files = None
        migrator = Migrator(run, self._rest_factory, self._sql_factory, sleep=_sleep, source_factory=factory,
                            credentials=self._credentials, onelake=files, trigger=trigger, artifacts=self._records)

        def work() -> None:
            try:
                migrator.execute()
            except Exception as exc:  # noqa: BLE001 - the pass could not even start; the run says so, never a dead thread
                reference = record_unexpected(f"migration run {run.run_id}", exc)
                run.abort(unexpected_message("The pass could not run", exc, reference))
            status, paused = migrator.outcome or (run.status, run.paused_reason)
            with context.bound(actor=context.system()):
                self._audit_pass(run, status, paused)
            # A pause or a stop-on-failure halt returns early; Resume and Retry come back
            # through here, so the commit happens whenever the run actually completes.
            if run_state.legacy_state(status) == "completed" and run.settings.get("gitCommit", "auto") == "auto":
                self._start_commit_after_run(run)

        self._thread = context.spawn(work, name=f"migration-{run.run_id}", run=run.run_id)
        self._threads = [t for t in self._threads if t.is_alive()] + [self._thread]

    def _audit_pass(self, run: MigrationRun, status: str, paused: Optional[str]) -> None:
        target = {"type": "run", "id": run.run_id}
        if status == run_state.PAUSED:
            details = {"pausedReason": paused, "reason": run.halted_reason}
            if paused == run_state.HALTED:
                self._audit("run.halted", target=target, run_id=run.run_id, details=details)
            else:
                self._audit("run.paused", target=target, run_id=run.run_id, details=details)
            return
        counts = run.counts()
        self._audit("run.completed", target=target, run_id=run.run_id,
                    outcome="done" if status == run_state.COMPLETED else "failed",
                    details={"status": status, "completed": counts.get(COMPLETED, 0), "failed": counts.get(run_state.ITEM_FAILED, 0),
                             "blocked": counts.get(run_state.BLOCKED, 0), "skipped": counts.get(run_state.SKIPPED, 0),
                             "deferred": counts.get(run_state.DEFERRED, 0), "passes": len(run.passes)})

    def _start_commit_after_run(self, run: MigrationRun) -> None:
        """Commit on a thread of its own, so the run is free (Retry, Reset) the moment it completes.

        The thread is registered before the run says "committing", under the same lock that
        `git_state` reads it with, so the two never disagree."""
        with self._lock:
            if self._git_thread is not None and self._git_thread.is_alive():
                with run.lock:
                    run.git = {"state": "blocked", "message": "Another commit was still running. Use Commit now when it finishes."}
                run.save()
                return
            self._git_thread = context.thread(self._commit_after_run, run, name=f"fabric-git-{run.run_id}", run=run.run_id)
            with run.lock:
                run.git = {"state": "committing", "message": "Committing the migrated items to Fabric Git…"}
            self._git_thread.start()

    # -- Fabric Git ----------------------------------------------------------

    def _git_comment(self, run: Optional[MigrationRun]) -> str:
        if run is None:
            return "Synapse to Fabric migration: commit from the Migration Accelerator"
        made = sum(1 for i in run.items if i.status == "COMPLETED")
        return f"Synapse to Fabric migration run {run.run_id}: {made} object(s) created in {run.workspace_name}"

    def _commit(self, workspace_id: str, comment: str, run: MigrationRun) -> dict:
        """Commit what ``run`` created or updated to the workspace's Fabric Git branch, and nothing else waiting there.
        Never raises: the answer says what happened."""
        started = _now()
        try:
            client = self._rest_factory()
            conn = fabric_git.connection(client, workspace_id)
            if not conn.connected:
                out = {"state": "skipped", "message": fabric_git.KNOWN_NOT_CONNECTED, "at": started}
            else:
                # The brief decides what the commit takes (E0S): only what the run made (the default), or all that waits.
                whole = run.settings.get("commitScope") == "all"
                result = fabric_git.commit(client, workspace_id, comment, only=None if whole else git_items(run))
                out = {"state": result.state, "items": result.items, "skipped": result.skipped, "others": result.others,
                       "commit": result.commit, "branch": conn.branch, "commitUrl": fabric_git.commit_url(conn, result.commit),
                       "pullRequestUrl": fabric_git.pull_request_url(conn), "at": started}
                out["message"] = (f"Committed {result.items} item(s) to {conn.branch}." if result.state == "committed"
                                  else "Nothing new to commit: the branch already has everything the run created.")
                if result.skipped:
                    out["message"] += f" Left out {len(result.skipped)} data-load pipeline(s): they are rebuilt per environment."
                if result.others:
                    shown = ", ".join(result.others[:5]) + (f" and {len(result.others) - 5} more" if len(result.others) > 5 else "")
                    out["message"] += (f" Left out {len(result.others)} other change(s) waiting in the workspace that this run did not "
                                       f"make ({shown}): commit them from Fabric's Source control panel if they belong in Git.")
        except fabric_git.GitNotReady as exc:
            out = {"state": "blocked", "message": str(exc), "at": started}
        except FabricApiError as exc:
            out = {"state": "failed", "message": exc.message, "code": exc.code, "at": started}
        except Exception as exc:  # noqa: BLE001 - a commit problem is reported, never raised into the run; the API's log gets it
            reference = record_unexpected("the Fabric Git commit", exc)
            out = {"state": "failed", "message": unexpected_message("The commit could not be made", exc, reference), "at": started}
        outcome = {"committed": "done", "nothing": "done", "skipped": "refused", "blocked": "refused"}.get(out["state"], "failed")
        self._audit("git.commit", target={"type": "run", "id": run.run_id}, run_id=run.run_id, outcome=outcome,
                    code=out.get("code") or out["state"],
                    details={"state": out["state"], "items": out.get("items"), "branch": out.get("branch"),
                             "commit": out.get("commit"), "others": len(out.get("others") or []), "message": out.get("message")})
        return out

    def _commit_after_run(self, run: MigrationRun) -> None:
        with context.bound(actor=context.system()):
            result = self._commit(run.workspace_id, self._git_comment(run), run)
        with run.lock:
            run.git = result
        run.save()
        run.log("GIT", "run", result.get("message") or result.get("state", ""))
        with self._lock:
            self._last_commit = result
        self._put("git", result)

    def git_state(self) -> dict:
        """Where the target workspace is connected in Fabric Git, what waits to be committed, and the
        pull-request link. Reads only."""
        try:
            workspace_id, workspace_name, _ = self._fabric.migration_target()
        except FabricError:
            return {"state": "no_target", "message": "Connect the Fabric target workspace first."}
        with self._lock:
            committing = self._git_thread is not None and self._git_thread.is_alive()
            last = dict(self._last_commit) if self._last_commit else None
        try:
            out = fabric_git.describe(self._rest_factory(), workspace_id)
        except FabricApiError as exc:
            out = {"state": "error", "message": exc.message, "code": exc.code}
        except Exception as exc:  # noqa: BLE001 - the page shows a reason, never a traceback; the API's log gets it
            reference = record_unexpected("reading the Fabric Git status", exc)
            out = {"state": "error", "message": unexpected_message("The Git status could not be read", exc, reference)}
        out["workspace"] = workspace_name
        out["committing"] = committing
        out["lastCommit"] = last
        return out

    def git_commit(self, body: dict) -> dict:
        """Commit what the current run created or updated to the target workspace's Fabric Git branch, on a
        background thread. Other changes waiting in the workspace are left for a person, and listed."""
        unknown = sorted(set(body) - {"comment"})
        if unknown:
            raise ApiError(400, "invalid_request", f"Unexpected field(s): {', '.join(unknown)}.")
        workspace_id, _, _ = self._fabric.migration_target()
        run = self._run
        if run is None or run.workspace_id != workspace_id:
            raise ApiError(409, "no_run", "There is no migration run for this workspace to commit. Changes made outside a run "
                           "are committed from Fabric's Source control panel.")
        comment = str(body.get("comment") or "").strip() or self._git_comment(run)
        with self._lock:
            if self._busy():
                raise ApiError(409, "run_in_progress", "A migration run is working. It commits by itself when it completes.")
            if self._git_thread is not None and self._git_thread.is_alive():
                raise ApiError(409, "commit_in_progress", "A commit is already running.")
            self._audit("git.commit_requested", target={"type": "run", "id": run.run_id}, run_id=run.run_id,
                        details={"comment": comment})

            def work() -> None:
                result = self._commit(workspace_id, comment, run)
                with self._lock:
                    self._last_commit = result
                    if self._run is run:
                        with run.lock:
                            run.git = result
                run.save()
                self._put("git", result)

            self._git_thread = context.spawn(work, name="fabric-git-commit", run=run.run_id)
        return self.git_state()


def _sleep(seconds: float) -> None:
    threading.Event().wait(seconds)
