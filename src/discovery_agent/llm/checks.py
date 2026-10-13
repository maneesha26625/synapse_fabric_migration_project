"""Checks on a draft before a person reviews it. They catch the obvious, not every mistake.

Each check gives ``{"level": "error" | "warning" | "ok", "message": ...}``. A draft with an error
cannot be accepted until it is edited; warnings are shown to the reviewer.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from discovery_agent.llm.context import synapse_only, unsupported_activities
from discovery_agent.migration import notebooks, tsql_rules
from discovery_agent.migration.common import NOTEBOOK, PIPELINE, PROCEDURE, SCRIPT, TABLE, VIEW

_CREATE = {
    VIEW: re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*CREATE\s+(?:OR\s+ALTER\s+)?VIEW\b", re.I | re.S),
    PROCEDURE: re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*CREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\b", re.I | re.S),
    TABLE: re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*CREATE\s+TABLE\b", re.I | re.S),
}
_WHAT = {VIEW: "CREATE VIEW", PROCEDURE: "CREATE PROCEDURE", TABLE: "CREATE TABLE"}


def _check(level: str, message: str) -> Dict[str, str]:
    return {"level": level, "message": message}


def _balanced(text: str) -> bool:
    depth = 0
    for char in re.sub(r"'(?:[^']|'')*'|\[[^\]]*\]|--[^\n]*|/\*.*?\*/", "", text, flags=re.S):
        depth += char == "("
        depth -= char == ")"
        if depth < 0:
            return False
    return depth == 0


def _sql(kind: str, code: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if kind in _CREATE and not _CREATE[kind].match(code):
        out.append(_check("error", f"It does not start with {_WHAT[kind]}, so the run could not create it."))
    if kind == TABLE and re.search(r"\b(?:PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY)\b", code, re.I):
        out.append(_check("warning", "It declares keys. The run adds the table's keys itself, NOT ENFORCED; remove them here to avoid a duplicate."))
    if not _balanced(code):
        out.append(_check("error", "Its parentheses do not balance."))
    leftovers = synapse_only(code)
    if leftovers:
        out.append(_check("error", "It still uses " + ", ".join(leftovers) + ", which a Fabric Warehouse rejects."))
    rewritten = tsql_rules.rewrite(code)
    if rewritten.changed:
        out.append(_check("warning", "The rules will still change it when it runs: " + " ".join(rewritten.notes)))
    return out


def _pipeline(code: str) -> List[Dict[str, str]]:
    try:
        resource = json.loads(code)
    except ValueError as exc:
        return [_check("error", f"It is not valid JSON ({exc.msg} at line {exc.lineno}).")]
    activities = (resource.get("properties") or {}).get("activities") if isinstance(resource, dict) else None
    if not isinstance(activities, list):
        return [_check("error", "It has no properties.activities list, so it is not a pipeline resource.")]
    left = unsupported_activities(resource)
    if left:
        return [_check("error", "The pipeline converter still cannot convert: " + "; ".join(left) + ".")]
    return []


def _notebook(code: str, original: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    try:
        payload = notebooks.with_cells(original or {}, code)
        _, notes = notebooks.to_fabric_ipynb(payload)
    except notebooks.NotebookNotMigratable as exc:
        return [_check("error", str(exc))]
    out = [_check("warning", n) for n in notes if n.startswith("Review before running")]
    before = len(((original or {}).get("properties") or {}).get("cells") or [])
    after = len((payload.get("properties") or {}).get("cells") or [])
    if before and after != before:
        out.append(_check("warning", f"The notebook had {before} cells and now has {after}. Check that nothing was dropped."))
    return out


def run(kind: str, code: str, unconverted: List[Any], original: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """``original`` is the Synapse resource, needed for notebooks (their transform carries only the cells)."""
    if kind == PIPELINE:
        found = _pipeline(code)
    elif kind == NOTEBOOK:
        found = _notebook(code, original)
    else:
        found = _sql(kind, code) if kind in (VIEW, PROCEDURE, TABLE, SCRIPT) else []
    if unconverted:
        found.append(_check("warning", f"The model left {len(unconverted)} item{'s' if len(unconverted) != 1 else ''} for a person; see below."))
    if not any(c["level"] == "error" for c in found):
        found.insert(0, _check("ok", "Passed the automatic checks. They are not a substitute for reading it."))
    return found


def blocking(found: List[Dict[str, str]]) -> bool:
    return any(c["level"] == "error" for c in found)
