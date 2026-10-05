"""Look inside each object before a run, so a problem is a finding, not a mid-run failure.

This reads what discovery already holds (column metadata, view and procedure
text, notebook JSON) and reuses the very translators the runner will use, so
what is predicted here is what the run does. Nothing here connects anywhere.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Set

from discovery_agent.migration import datacopy, environments, fabric_connections, jobs, notebooks, pipelines, warehouse_ddl
from discovery_agent.migration.common import (
    CONNECTION, DATA, ENVIRONMENT, NOTEBOOK, PIPELINE, PROCEDURE, SCHEDULE, SCRIPT, SPARKJOB, TABLE, VIEW, Source,
)
from discovery_agent.migration.planner import BLOCKING, HIGH, LOW, MEDIUM, Finding

#: Dedicated-pool-only T-SQL that a Fabric Warehouse rejects or ignores.
SYNAPSE_ONLY = (
    (re.compile(r"\bDISTRIBUTION\s*=", re.I), "a DISTRIBUTION option"),
    (re.compile(r"\bCLUSTERED\s+COLUMNSTORE\b", re.I), "CLUSTERED COLUMNSTORE INDEX"),
    (re.compile(r"\bOPTION\s*\(\s*LABEL\b", re.I), "an OPTION (LABEL) query hint"),
    (re.compile(r"\bCREATE\s+TABLE\b[^;]*?\bAS\s+SELECT\b", re.I | re.S), "CREATE TABLE AS SELECT (CTAS)"),
    (re.compile(r"\bRENAME\s+OBJECT\b", re.I), "RENAME OBJECT"),
    (re.compile(r"\bUPDATE\s+STATISTICS\b", re.I), "UPDATE STATISTICS"),
)


def _table(source: Source) -> List[Finding]:
    try:
        _, notes = warehouse_ddl.create_table(source.payload)
    except warehouse_ddl.UnsupportedColumn as exc:
        return [Finding(source.id, "UNSUPPORTED_TYPE", BLOCKING, f"{source.name}: {exc} It cannot be recreated.")]
    except Exception as exc:  # noqa: BLE001 - a malformed record is a finding, not a crash
        return [Finding(source.id, "DEFINITION_UNREADABLE", HIGH, f"{source.name}: the table definition could not be read ({type(exc).__name__}).")]
    if not notes:
        return []
    severity = MEDIUM if any("IDENTITY" in n for n in notes) else LOW
    shown = "; ".join(notes[:3]) + (f"; and {len(notes) - 3} more" if len(notes) > 3 else "")
    return [Finding(source.id, "TYPE_CONVERSION", severity, f"{source.name} changes on the way: {shown}.")]


def _module(source: Source) -> List[Finding]:
    definition = getattr(source.payload, "definition", None)
    text = getattr(definition, "text", None)
    if not text or not getattr(definition, "is_readable", False):
        return [Finding(source.id, "DEFINITION_UNREADABLE", BLOCKING,
                        f"{source.name}: the definition is encrypted or VIEW DEFINITION is not granted, so it cannot be recreated.")]
    hits = [label for pattern, label in SYNAPSE_ONLY if pattern.search(text)]
    if hits:
        return [Finding(source.id, "SYNAPSE_TSQL", MEDIUM,
                        f"{source.name} uses {', '.join(hits)}, which a Fabric Warehouse may reject. Review it after the run.")]
    return []


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
    if problem is None or problem[0] == "EXTERNAL":
        return []
    return [Finding(source.id, "DATA_UNSUPPORTED", HIGH, f"{source.name}: {problem[1]}")]


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
    hits = [label for pattern, label in SYNAPSE_ONLY if pattern.search(query)]
    if hits:
        return [Finding(source.id, "SYNAPSE_TSQL", MEDIUM, f"{source.name} uses {', '.join(hits)}, which a Fabric Warehouse may reject.")]
    return []


def _schedule(source: Source) -> List[Finding]:
    try:
        jobs.schedule_bodies(source.payload or {})
    except jobs.NotConvertible as exc:
        return [Finding(source.id, "RECREATE_BY_HAND", MEDIUM, f"{source.name}: {exc}")]
    return []


def _connection(source: Source, names: Set[str]) -> List[Finding]:
    plan = fabric_connections.parse(source.payload or {})
    if plan.unsupported:
        return [Finding(source.id, "CONNECTION_BY_HAND", MEDIUM, f"{source.name}: {plan.unsupported}")]
    if source.name not in names:
        return [Finding(source.id, "NEEDS_CREDENTIALS", MEDIUM,
                        f"{source.name} needs credentials entered under Connections before Fabric can create it; until then it stays deferred, and pipelines that use it cannot be created.")]
    return []


_CHECKS = {TABLE: _table, VIEW: _module, PROCEDURE: _module, NOTEBOOK: _notebook, ENVIRONMENT: _environment,
           DATA: _data, PIPELINE: _pipeline, SPARKJOB: _sparkjob, SCRIPT: _script, SCHEDULE: _schedule}


def content_findings(sources: Sequence[Source], credential_names: Optional[Set[str]] = None) -> List[Finding]:
    """Findings for every object whose own content predicts trouble."""
    names = credential_names or set()
    out: List[Finding] = []
    for source in sources:
        if source.kind == CONNECTION:
            out.extend(_connection(source, names))
            continue
        check = _CHECKS.get(source.kind)
        if check is not None:
            out.extend(check(source))
    return out


def environment_checks(fabric_state: Dict[str, Any], sql_driver: str = "", source_connected: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Pass/warn/fail checks on the target and the machine, before any object is touched."""
    checks: List[Dict[str, Any]] = []
    connected = fabric_state.get("status") == "connected"
    checks.append({
        "label": "Fabric target connected",
        "status": "ok" if connected else "fail",
        "detail": f"Workspace {fabric_state.get('workspaceName')}" if connected
        else "Connect the Fabric target and pass its connection test on the Fabric Target page.",
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
            "label": "Synapse source connected (for table data and shortcuts)",
            "status": "ok" if source_connected else "fail",
            "detail": "Rows are read through the discovery sign-in." if source_connected
            else "Connect the Synapse source again: the data load reads the rows from it.",
        })
    return checks
