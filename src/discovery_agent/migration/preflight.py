"""Look inside each object before a run, so a problem is a finding, not a mid-run failure.

This reads what discovery already holds (column metadata, view and procedure
text, notebook JSON) and reuses the very translators the runner will use, so
what is predicted here is what the run does. Nothing here connects anywhere.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Set

from discovery_agent.migration import storage_remap
from discovery_agent.migration import datacopy, datafilter, environments, fabric_connections, jobs, notebooks, pipelines, tsql_rules, warehouse_ddl
from discovery_agent.migration import security as security_sql
from discovery_agent.migration.common import (
    CONNECTION, DATA, DEFERRED, ENVIRONMENT, FUNCTION, NOTEBOOK, PIPELINE, PROCEDURE, SCHEDULE, SCRIPT, SECURITY, SPARKJOB, TABLE,
    VIEW, Source,
)
from discovery_agent.migration.planner import BLOCKING, HIGH, LOW, MEDIUM, Finding

#: Dedicated-pool-only T-SQL that a Fabric Warehouse rejects, checked *after* the T-SQL rules
#: (``tsql_rules``) have converted what they can. CTAS, OPTION (LABEL) and UPDATE STATISTICS
#: are not listed: a Warehouse supports them.
SYNAPSE_ONLY = (
    (re.compile(r"\bDISTRIBUTION\s*=", re.I), "a DISTRIBUTION option"),
    (re.compile(r"\bCLUSTERED\s+COLUMNSTORE\b", re.I), "CLUSTERED COLUMNSTORE INDEX"),
    (re.compile(r"\bRENAME\s+OBJECT\b", re.I), "RENAME OBJECT"),
    (re.compile(r"\bsys\.(?:dm_)?pdw_", re.I), "Synapse system views (sys.pdw_*)"),
    (re.compile(r"\bCREATE\s+(?:MATERIALIZED\s+VIEW|EXTERNAL\s+TABLE)\b", re.I), "a materialized view or external table"),
    (re.compile(r"IDENTITY\s*=\s*'Managed Identity'", re.I), "COPY INTO with Synapse's managed identity"),
    # Listed as unsupported in Fabric Warehouse's T-SQL surface area.
    (re.compile(r"\bSET\s+ROWCOUNT\b", re.I), "SET ROWCOUNT"),
    (re.compile(r"\bFOR\s+XML\b", re.I), "SELECT ... FOR XML"),
    (re.compile(r"\bCREATE\s+SYNONYM\b", re.I), "a synonym"),
    (re.compile(r"\bPREDICT\s*\(", re.I), "PREDICT"),
    (re.compile(r"\bCREATE\s+USER\b", re.I), "CREATE USER (Fabric creates a user on its first GRANT)"),
    (re.compile(r"\bCREATE\s+(?:OR\s+ALTER\s+)?TRIGGER\b", re.I), "a trigger"),
)


def _sql_findings(source: Source, text: str, after_rules: bool = True) -> List[Finding]:
    """What the T-SQL rules will fix (LOW) and what is left for a person (MEDIUM)."""
    converted = tsql_rules.rewrite(text)
    # The rules leave what they removed as a comment; check only live code for leftovers.
    live = re.sub(r"/\*.*?\*/|--[^\n]*", " ", converted.text, flags=re.S)
    hits = [label for pattern, label in SYNAPSE_ONLY if pattern.search(live)]
    found: List[Finding] = []
    if converted.changed:
        found.append(Finding(source.id, "TSQL_CONVERTED", LOW, f"{source.name}: converted automatically: " + " ".join(converted.notes)))
    if hits:
        found.append(Finding(source.id, "SYNAPSE_TSQL", MEDIUM,
                             f"{source.name} uses {', '.join(hits)}, which a Fabric Warehouse rejects. Review it after the run."))
    return found


def _table(source: Source) -> List[Finding]:
    unsupported = [m for m in source.masks if security_sql.mask_clause(m) is None]
    if unsupported:
        names = ", ".join(f"{m.column} ({m.function})" for m in unsupported)
        return [Finding(source.id, "MASK_UNSUPPORTED", BLOCKING,
                        f"{source.name}: masking function(s) Fabric does not have: {names}. The table would not be created, "
                        "so its rows are never loaded unmasked; change the mask first.")]
    try:
        _, notes = warehouse_ddl.create_table(source.payload, [(m.column, security_sql.mask_clause(m)) for m in source.masks])
    except warehouse_ddl.UnsupportedColumn as exc:
        return [Finding(source.id, "UNSUPPORTED_TYPE", BLOCKING, f"{source.name}: {exc} It cannot be recreated.")]
    except Exception as exc:  # noqa: BLE001 - a malformed record is a finding, not a crash
        return [Finding(source.id, "DEFINITION_UNREADABLE", HIGH, f"{source.name}: the table definition could not be read ({type(exc).__name__}).")]
    if not notes:
        return []
    severity = MEDIUM if any("IDENTITY dropped" in n for n in notes) else LOW
    shown = "; ".join(notes[:3]) + (f"; and {len(notes) - 3} more" if len(notes) > 3 else "")
    return [Finding(source.id, "TYPE_CONVERSION", severity, f"{source.name} changes on the way: {shown}.")]


def _module(source: Source) -> List[Finding]:
    definition = getattr(source.payload, "definition", None)
    text = getattr(definition, "text", None)
    if not text or not getattr(definition, "is_readable", False):
        return [Finding(source.id, "DEFINITION_UNREADABLE", BLOCKING,
                        f"{source.name}: the definition is encrypted or VIEW DEFINITION is not granted, so it cannot be recreated.")]
    return _sql_findings(source, text)


def _security(source: Source) -> List[Finding]:
    try:
        plan = security_sql.plan_principal(source.payload, source.object_name or source.name)
    except Exception:  # noqa: BLE001 - a principal discovery did not keep is a finding, not a crash
        return [Finding(source.id, "DEFINITION_UNREADABLE", HIGH, f"{source.name}: the role or user was not kept by discovery. Run discovery again.")]
    found = []
    if plan.notes:
        found.append(Finding(source.id, "SECURITY_REVIEW", MEDIUM if any("SQL user" in n for n in plan.notes) else LOW,
                             f"{source.name}: " + " ".join(plan.notes)))
    denies = [s for _, s in plan.permissions if s.startswith("DENY ")]
    if denies:
        found.append(Finding(source.id, "SECURITY_DENY", LOW,
                             f"{source.name}: {len(denies)} DENY permission(s). In Fabric, workspace Admins, Members and Contributors "
                             "hold CONTROL on the Warehouse, so a DENY does not restrict them."))
    return found


def _deferred(source: Source) -> List[Finding]:
    """A function or principal the run leaves for a person (multi-statement TVF, SQL user): say why, up front."""
    if source.type not in ("Function", "Security Object") or not source.reason:
        return []
    return [Finding(source.id, "NEEDS_PERSON", MEDIUM, f"{source.name}: {source.reason}")]


def _notebook(source: Source) -> List[Finding]:
    if not isinstance(source.payload, dict) or not source.payload:
        return [Finding(source.id, "DEFINITION_UNREADABLE", HIGH, f"{source.name}: discovery did not keep the notebook's content. Run discovery again.")]
    try:
        _, notes = notebooks.to_fabric_ipynb(source.payload)
    except notebooks.NotebookNotMigratable as exc:
        return [Finding(source.id, "NEEDS_REWRITE", HIGH, f"{source.name}: {exc}")]
    review = [n for n in notes if n.startswith("Review before running")]
    return [Finding(source.id, "NOTEBOOK_REVIEW", MEDIUM, f"{source.name}: {review[0]}")] if review else []


def _environment(source: Source) -> List[Finding]:
    meta = source.payload if isinstance(source.payload, dict) else {}
    notes = [n for n in environments.plan(meta, source.name).notes if "dropped" not in n]
    if not notes:
        return []
    return [Finding(source.id, "SPARK_SETTINGS", LOW, f"{source.name}: {' '.join(notes[:2])}")]


def _data(source: Source) -> List[Finding]:
    problem = datacopy.preflight(source.payload)
    if problem is not None and problem[0] != "EXTERNAL":
        return [Finding(source.id, "DATA_UNSUPPORTED", HIGH, f"{source.name}: {problem[1]}")]
    if problem is not None or source.data_filter is None:
        return []
    try:
        resolved = datafilter.resolve(source.payload, source.data_filter)
    except datafilter.FilterError as exc:
        return [Finding(source.id, "FILTER_INVALID", HIGH, f"{source.name}: the date filter does not fit the table. {exc}")]
    if resolved.sync and not resolved.keys:
        return [Finding(source.id, "SYNC_APPEND_ONLY", MEDIUM,
                        f"{source.name}: kept in sync without key columns, so a changed row arrives as a second row instead of "
                        "replacing the first. Choose key columns unless the table only ever gains rows.")]
    return []


class _Always(dict):
    """A lookup that has every key: lets a pipeline convert as if its Fabric dependencies existed."""

    def __missing__(self, key: str) -> str:
        return "preflight"


def _pipeline(source: Source) -> List[Finding]:
    bundle = source.payload
    if not isinstance(bundle, dict) or not bundle.get("resource"):
        return [Finding(source.id, "DEFINITION_UNREADABLE", HIGH, f"{source.name}: discovery did not keep the pipeline's content. Run discovery again.")]
    context = pipelines.Context(
        datasets=bundle.get("datasets", {}), linked_services=bundle.get("linkedServices", {}), connections=_Always(),
        notebooks=_Always(), pipelines=_Always(), spark_jobs=_Always(), workspace_id="preflight",
        warehouse=pipelines.Warehouse("preflight", "preflight", "preflight"), pool_name="")
    converted = pipelines.convert(bundle["resource"], context)
    found: List[Finding] = []
    if converted.unsupported:
        found.append(Finding(source.id, "NEEDS_REWRITE", HIGH,
                             f"{source.name} has activities with no Fabric equivalent ({'; '.join(converted.unsupported[:4])}), so it will not be created."))
    unresolved = [m for m in converted.missing if m.startswith("dataset")]
    if unresolved:
        found.append(Finding(source.id, "DEPENDENCY_UNRESOLVED", HIGH, f"{source.name} uses {', '.join(unresolved[:3])}, which discovery did not capture."))
    return found


def _sparkjob(source: Source) -> List[Finding]:
    try:
        jobs.spark_job(source.payload or {}, None)
    except jobs.NotConvertible as exc:
        return [Finding(source.id, "NEEDS_REWRITE", HIGH, f"{source.name}: {exc}")]
    return []


def _script(source: Source) -> List[Finding]:
    try:
        jobs.sql_script_notebook(source.payload or {}, None, "warehouse")
    except jobs.NotConvertible as exc:
        return [Finding(source.id, "NEEDS_REWRITE", HIGH, f"{source.name}: {exc}")]
    query = str(((source.payload or {}).get("properties") or {}).get("content", {}).get("query") or "")
    return _sql_findings(source, query)


def _schedule(source: Source) -> List[Finding]:
    try:
        jobs.schedule_bodies(source.payload or {})
    except jobs.NotConvertible as exc:
        return [Finding(source.id, "RECREATE_BY_HAND", MEDIUM, f"{source.name}: {exc}")]
    return []


def _connection(source: Source, names: Set[str], pool: str = "") -> List[Finding]:
    plan = fabric_connections.parse(source.payload or {})
    if plan.unsupported:
        if source.uses and all(pipelines.points_at_pool(source.payload or {}, pool, use) for use in source.uses):
            return []  # every use is the migrated pool: the run replaces it with the Warehouse
        return [Finding(source.id, "CONNECTION_BY_HAND", MEDIUM, f"{source.name}: {plan.unsupported}")]
    if source.name not in names:
        return [Finding(source.id, "NEEDS_CREDENTIALS", MEDIUM,
                        f"{source.name} needs credentials entered in Plan, Stages & credentials (the Connections stage) before Fabric can create it; until then it stays deferred, and pipelines that use it cannot be created.")]
    return []


_CHECKS = {TABLE: _table, VIEW: _module, PROCEDURE: _module, FUNCTION: _module, NOTEBOOK: _notebook, ENVIRONMENT: _environment,
           DATA: _data, PIPELINE: _pipeline, SPARKJOB: _sparkjob, SCRIPT: _script, SCHEDULE: _schedule,
           SECURITY: _security, DEFERRED: _deferred}


def content_findings(sources: Sequence[Source], credential_names: Optional[Set[str]] = None, pool: str = "",
                     storage_routes: Optional[Sequence[Dict[str, Any]]] = None) -> List[Finding]:
    """Findings for every object whose own content predicts trouble. ``pool`` is the SQL pool being migrated;
    ``storage_routes`` the storage locations the run sends through OneLake shortcuts (run option ``storageRoutes``)."""
    names = credential_names or set()
    routes = planned_routes(storage_routes)
    out: List[Finding] = []
    direct: Set[str] = set()
    for source in sources:
        if source.kind == CONNECTION:
            out.extend(_connection(source, names, pool))
            continue
        first = len(out)
        if source.override:
            out.extend(_override(source))
        else:
            check = _CHECKS.get(source.kind)
            if check is not None:
                out.extend(check(source))
        storage = _external_storage(source, routes)
        if storage is not None:
            if storage.code == "STORAGE_ROUTED":
                out[first:] = _without_storage_review(out[first:])
            out.append(storage)
            if storage.code == "EXTERNAL_STORAGE":
                direct.add(source.id)
        serverless = _serverless(source, routes)
        if serverless is not None:
            out.append(serverless)
    out.extend(_depends_on_storage(sources, direct))
    return out


#: ADLS Gen2 / Blob storage named in code: abfss://container@account.dfs..., https://account.blob..., or built from parts.
_STORAGE_URL = re.compile(r"(?:abfss?|wasbs?)://[^@\s'\"]*@([A-Za-z0-9]+)\.(?:dfs|blob)\.core\.windows\.net"
                          r"|https?://([A-Za-z0-9]+)\.(?:dfs|blob)\.core\.windows\.net", re.IGNORECASE)
_STORAGE_SUFFIX = re.compile(r"\.(?:dfs|blob)\.core\.windows\.net", re.IGNORECASE)


def code_text(source: Source) -> str:
    """The code the run will create for an object, as text: the accepted transform, or the original."""
    if source.override:
        return str(source.override.get("code") or "")
    payload = source.payload
    if source.kind in (VIEW, PROCEDURE, FUNCTION):
        return str(getattr(getattr(payload, "definition", None), "text", "") or "")
    if source.kind == SCRIPT:
        return str(((payload or {}).get("properties") or {}).get("content", {}).get("query") or "")
    if source.kind in (NOTEBOOK, SPARKJOB) and isinstance(payload, dict):
        return json.dumps(payload.get("properties") or {})
    return ""


def planned_routes(specs: Optional[Sequence[Dict[str, Any]]]) -> storage_remap.Routes:
    """The run option ``storageRoutes`` as routes, for predicting what the run rewrites (ids are placeholders)."""
    return storage_remap.Routes(storage_remap.Route(str(r["account"]), str(r["container"]), "ws", "lh", str(r["container"]))
                                for r in specs or [] if r.get("account") and r.get("container"))


STORAGE_WHY = ("Fabric runs it as the signed-in user, not the Synapse managed identity; a storage account in another "
               "Microsoft Entra tenant rejects that user whatever roles it has, and one in the same tenant needs a role for "
               "it. Route the account through a OneLake shortcut (Plan > Stages & credentials > External storage), or it "
               "fails with 401 / \"Cannot find the CREDENTIAL\".")


def _external_storage(source: Source, routes: Optional[storage_remap.Routes] = None) -> Optional[Finding]:
    """Code that reads or writes an Azure storage account directly, and whether the run routes it through OneLake."""
    if source.kind not in (VIEW, PROCEDURE, FUNCTION, SCRIPT, NOTEBOOK, SPARKJOB):
        return None
    text = code_text(source)
    if not text or not _STORAGE_SUFFIX.search(text):
        return None
    accounts = sorted({(a or b).lower() for a, b in _STORAGE_URL.findall(text)})
    named = ", ".join(accounts) if accounts else "a storage account built from variables"
    main_file = source.kind == SPARKJOB and bool(re.search(r'"(?:file|executableFile)"\s*:\s*"(?:abfss?|https?)://', text))
    if routes:
        rewritten, notes = storage_remap.rewrite(text, routes)
        if rewritten != text and not any(n.startswith("Not rewritten") for n in notes):
            extra = (" Its main file is copied into the Lakehouse with the paths in its code rewritten; check that code once "
                     "the run has copied it.") if main_file else ""
            return Finding(source.id, "STORAGE_ROUTED", LOW,
                           f"{source.name}: its paths on {named} will go through the OneLake shortcut.{extra}")
    what = "Its main file is kept on" if main_file else "It reads or writes"
    return Finding(source.id, "EXTERNAL_STORAGE", HIGH if main_file else MEDIUM,
                   f"{source.name}: {what} {named} directly. {STORAGE_WHY}")


def _without_storage_review(found: List[Finding]) -> List[Finding]:
    """A notebook's review note, minus its storage-path item once the run routes that storage through OneLake."""
    out: List[Finding] = []
    for f in found:
        if f.code != "NOTEBOOK_REVIEW" or "direct storage paths" not in f.message:
            out.append(f)
            continue
        head, _, items = f.message.partition("uses: ")
        kept = [i for i in items.rstrip(".").split("; ") if not i.startswith("direct storage paths")]
        if kept:
            out.append(Finding(f.object_id, f.code, f.severity, f"{head}uses: {'; '.join(kept)}."))
    return out


def _serverless(source: Source, routes: Optional[storage_remap.Routes] = None) -> Optional[Finding]:
    """SQL written for Synapse's serverless pool. It runs on the Warehouse in Fabric; its OPENROWSET over storage works
    there once that storage goes through a OneLake shortcut (checked on a real Warehouse), and fails otherwise."""
    if source.kind == SCRIPT:
        connection = (((source.payload or {}).get("properties") or {}).get("content") or {}).get("currentConnection") or {}
        if str(connection.get("poolName") or "").lower() != "built-in":
            return None
        text = code_text(source)
        where = "it"
    elif source.kind == PIPELINE and isinstance(source.payload, dict):
        services = source.payload.get("linkedServices") or {}
        on_demand = {n for n, ls in services.items() if pipelines.is_serverless_service(ls or {})}
        used = [a for a in _activities((source.payload.get("resource") or {}).get("properties", {}).get("activities") or [])
                if (a.get("linkedServiceName") or {}).get("referenceName") in on_demand]
        if not used:
            return None
        text = json.dumps([a.get("typeProperties") or {} for a in used])
        where = ", ".join(str(a.get("name")) for a in used[:3])
    else:
        return None
    if not storage_remap.uses_storage(text):
        return Finding(source.id, "SERVERLESS_SQL", LOW,
                       f"{source.name}: {where} ran on Synapse's serverless SQL pool; in Fabric it runs on the Warehouse.")
    rewritten, notes = storage_remap.rewrite(text, routes) if routes else (text, ["Not rewritten"])
    if rewritten != text and not any(n.startswith("Not rewritten") for n in notes):
        return Finding(source.id, "SERVERLESS_SQL", LOW,
                       f"{source.name}: {where} ran on Synapse's serverless SQL pool; in Fabric it runs on the Warehouse, and its "
                       "queries over files go through the OneLake shortcut.")
    return Finding(source.id, "SERVERLESS_SQL", MEDIUM,
                   f"{source.name}: {where} ran on Synapse's serverless SQL pool and queries storage directly. In Fabric it runs "
                   "on the Warehouse, which cannot read that storage as the signed-in user: route the storage account "
                   "through a OneLake shortcut, or leave it out.")

def _activities(acts: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for a in acts:
        if not isinstance(a, dict):
            continue
        out.append(a)
        tp = a.get("typeProperties") or {}
        for key in ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities"):
            out.extend(_activities(tp.get(key) or []))
        for case in tp.get("cases") or []:
            out.extend(_activities(case.get("activities") or []))
    return out


def _depends_on_storage(sources: Sequence[Source], direct: Set[str]) -> List[Finding]:
    """Pipelines that run a notebook, Spark job or procedure which still reads storage directly: they fail through it."""
    found: List[Finding] = []
    by_kind: Dict[str, Set[str]] = {}
    for s in sources:
        if s.id in direct:
            name = (s.object_name or s.name) if s.kind == PROCEDURE else s.name
            by_kind.setdefault(s.kind, set()).add(name.lower())
    if not by_kind:
        return found
    for s in sources:
        if s.kind != PIPELINE or not isinstance(s.payload, dict):
            continue
        resource = s.payload.get("resource") or {}
        names = pipelines.referenced_names(resource)
        hits = [n for n in names["notebooks"] if n.lower() in by_kind.get(NOTEBOOK, set())]
        hits += [n for n in names["sparkJobs"] if n.lower() in by_kind.get(SPARKJOB, set())]
        for a in _activities((resource.get("properties") or {}).get("activities") or []):
            proc = str((a.get("typeProperties") or {}).get("storedProcedureName") or "")
            short = proc.replace("[", "").replace("]", "").split(".")[-1].lower()
            if proc and short in by_kind.get(PROCEDURE, set()):
                hits.append(proc)
        if hits:
            found.append(Finding(s.id, "DEPENDS_ON_STORAGE", MEDIUM,
                                 f"{s.name} runs {', '.join(sorted(set(hits))[:4])}, which still read storage directly, so it fails "
                                 "with them. Route their storage through a OneLake shortcut."))
    return found

def _override(source: Source) -> List[Finding]:
    """An object with an accepted LLM-assisted transform: check the code the run will use, not the original."""
    code = str((source.override or {}).get("code") or "")
    found = [Finding(source.id, "LLM_TRANSFORM", LOW,
                     f"{source.name}: uses the LLM-assisted transform accepted on {(source.override or {}).get('acceptedAt') or 'an earlier date'}, "
                     "not the rule-based conversion.")]
    if source.kind in (VIEW, PROCEDURE, FUNCTION, SCRIPT):
        found.extend(f for f in _sql_findings(source, code) if f.code != "TSQL_CONVERTED")
    elif source.kind == PIPELINE:
        try:
            resource = json.loads(code)
        except ValueError:
            return found + [Finding(source.id, "DEFINITION_UNREADABLE", HIGH, f"{source.name}: the accepted transform is not valid JSON.")]
        bundle = {**(source.payload if isinstance(source.payload, dict) else {}), "resource": resource}
        found.extend(_pipeline(dataclasses.replace(source, payload=bundle, override=None)))
    elif source.kind == NOTEBOOK:
        try:
            payload = notebooks.with_cells(source.payload if isinstance(source.payload, dict) else {}, code)
        except notebooks.NotebookNotMigratable as exc:
            return found + [Finding(source.id, "DEFINITION_UNREADABLE", HIGH, f"{source.name}: {exc}")]
        found.extend(_notebook(dataclasses.replace(source, payload=payload, override=None)))
    return found


def environment_checks(fabric_state: Dict[str, Any], sql_driver: str = "", source_connected: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Pass/warn/fail checks on the target and the machine, before any object is touched."""
    checks: List[Dict[str, Any]] = []
    connected = fabric_state.get("status") == "connected"
    checks.append({
        "label": "Fabric target connected",
        "status": "ok" if connected else "fail",
        "detail": f"Workspace {fabric_state.get('workspaceName')}" if connected
        else "Connect the Fabric target and pass its connection test on the Connections page.",
    })
    if connected:
        assigned = fabric_state.get("capacityAssigned")
        checks.append({
            "label": "Workspace on a Fabric capacity",
            "status": "ok" if assigned else "fail",
            "detail": "Items can be created." if assigned
            else "Assign a Fabric or Trial capacity under Workspace settings, License info, then test the connection again.",
        })
    checks.append({
        "label": "SQL driver for the Warehouse",
        "status": "ok" if sql_driver else "fail",
        "detail": sql_driver or "ODBC Driver 18 (or 17) for SQL Server is not installed on the machine running the API.",
    })
    if source_connected is not None:
        checks.append({
            "label": "Synapse source connected (row counts and shortcuts)",
            "status": "ok" if source_connected else "fail",
            "detail": "Row counts are compared and external-table locations read through the discovery sign-in." if source_connected
            else "Connect the Synapse source again: data pipelines still run, but row counts cannot be compared and shortcuts cannot be made.",
        })
    return checks
