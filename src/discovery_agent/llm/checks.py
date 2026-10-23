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
from discovery_agent.migration.common import FUNCTION, LAKEHOUSE_TARGET, NOTEBOOK, PIPELINE, PROCEDURE, SCRIPT, TABLE, VIEW, WAREHOUSE_TARGET

_CREATE = {
    VIEW: re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*CREATE\s+(?:OR\s+ALTER\s+)?VIEW\b", re.I | re.S),
    PROCEDURE: re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*CREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\b", re.I | re.S),
    TABLE: re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*CREATE\s+TABLE\b", re.I | re.S),
    FUNCTION: re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*CREATE\s+(?:OR\s+ALTER\s+)?FUNCTION\b", re.I | re.S),
}
_WHAT = {VIEW: "CREATE VIEW", PROCEDURE: "CREATE PROCEDURE", TABLE: "CREATE TABLE", FUNCTION: "CREATE FUNCTION"}
#: ``RETURNS @t TABLE``: a multi-statement table-valued function, which a Fabric Warehouse does not support.
_MULTI_STATEMENT = re.compile(r"\bRETURNS\s+@\w+\s+TABLE\b", re.I)


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
    if kind == FUNCTION and _MULTI_STATEMENT.search(code):
        out.append(_check("error", "It is still a multi-statement table-valued function (RETURNS @... TABLE), which a Fabric "
                                   "Warehouse rejects. Rewrite it as an inline table-valued function (RETURNS TABLE AS RETURN SELECT ...)."))
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


def _cell_text(cell: Dict[str, Any]) -> str:
    source = cell.get("source")
    return source if isinstance(source, str) else "".join(source or [])


def _spark_notebook(code: str) -> List[Dict[str, str]]:
    """A Lakehouse procedure or script: a PySpark notebook as cells JSON, whose code cells must be valid Python."""
    try:
        document = json.loads(code)
    except ValueError as exc:
        return [_check("error", f"It is not valid JSON ({exc.msg} at line {exc.lineno}); it must be the notebook's cells.")]
    cells = document.get("cells") if isinstance(document, dict) else None
    if not isinstance(cells, list) or not cells:
        return [_check("error", 'It has no "cells" list, so it is not a notebook.')]
    out: List[Dict[str, str]] = []
    code_cells = [c for c in cells if isinstance(c, dict) and c.get("cell_type", "code") == "code"]
    for n, cell in enumerate(code_cells, 1):
        # Magics (%run, %pip) and shell lines are not Python; everything else must compile.
        lines = [line for line in _cell_text(cell).splitlines() if not line.lstrip().startswith(("%", "!"))]
        try:
            compile("\n".join(lines), f"cell {n}", "exec")
        except SyntaxError as exc:
            out.append(_check("error", f"Code cell {n} is not valid Python (line {exc.lineno}: {exc.msg})."))
    if not any("parameters" in (c.get("tags") or []) for c in code_cells):
        out.append(_check("warning", 'No cell is tagged "parameters": a pipeline cannot pass the procedure\'s parameters to it.'))
    joined = "\n".join(_cell_text(c) for c in code_cells)
    if re.search(r"\bBEGIN\s+TRAN|\bsp_executesql\b|\bIDENTITY_INSERT\b|\[dbo\]", joined, re.I):
        out.append(_check("warning", "It still contains T-SQL (BEGIN TRAN, sp_executesql, IDENTITY_INSERT or [brackets]), which Spark rejects."))
    if "# REVIEW" in joined:
        out.append(_check("warning", "It still has # REVIEW markers: something is left for a person to finish."))
    return out


def _delta_table(code: str) -> List[Dict[str, str]]:
    """A Lakehouse table: a Spark SQL CREATE TABLE for a Delta table, without T-SQL table syntax."""
    out: List[Dict[str, str]] = []
    if not _CREATE[TABLE].match(code):
        out.append(_check("error", "It does not start with CREATE TABLE, so the run could not create it."))
    if not re.search(r"\bUSING\s+DELTA\b", code, re.I):
        out.append(_check("warning", "It has no USING DELTA: a Lakehouse table must be a Delta table."))
    if re.search(r"\bIDENTITY\b|\bPRIMARY\s+KEY\b|\bFOREIGN\s+KEY\b|\bDISTRIBUTION\s*=|\bCLUSTERED\b|\[[^\]]+\]", code, re.I):
        out.append(_check("error", "It still uses T-SQL table syntax (IDENTITY, keys, DISTRIBUTION, CLUSTERED or [brackets]), "
                                   "which Spark rejects for a Delta table."))
    if not _balanced(code):
        out.append(_check("error", "Its parentheses do not balance."))
    return out


def run(kind: str, code: str, unconverted: List[Any], original: Optional[Dict[str, Any]] = None,
        target: str = WAREHOUSE_TARGET) -> List[Dict[str, str]]:
    """``original`` is the Synapse resource, needed for notebooks (their transform carries only the cells).
    ``target``: a Lakehouse draft of a procedure or script is a Spark notebook, and of a table a Delta CREATE TABLE."""
    lakehouse = target == LAKEHOUSE_TARGET
    if lakehouse and kind in (PROCEDURE, SCRIPT):
        found = _spark_notebook(code)
    elif lakehouse and kind == TABLE:
        found = _delta_table(code)
    elif kind == PIPELINE:
        found = _pipeline(code)
    elif kind == NOTEBOOK:
        found = _notebook(code, original)
    else:
        found = _sql(kind, code) if kind in (VIEW, PROCEDURE, TABLE, SCRIPT, FUNCTION) else []
    if unconverted:
        found.append(_check("warning", f"The model left {len(unconverted)} item{'s' if len(unconverted) != 1 else ''} for a person; see below."))
    if not any(c["level"] == "error" for c in found):
        found.insert(0, _check("ok", "Passed the automatic checks. They are not a substitute for reading it."))
    return found


def blocking(found: List[Dict[str, str]]) -> bool:
    return any(c["level"] == "error" for c in found)
