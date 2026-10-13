"""What a transformation prompt is made of: the object's source, what the rules do with it, and
what discovery knows about its surroundings.

The source text extracted here is also what an accepted transform is pinned to: its hash is stored
with the transform, and a run uses the transform only while the hash still matches.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from discovery_agent.llm import templates
from discovery_agent.migration import notebooks, pipelines, tsql_rules, warehouse_ddl
from discovery_agent.migration.common import NOTEBOOK, PIPELINE, PROCEDURE, SCRIPT, TABLE, VIEW, Source
from discovery_agent.migration.preflight import SYNAPSE_ONLY

#: The kinds a draft can be generated for, and the template that describes each.
TEMPLATES = {PROCEDURE: "stored_procedure.md", VIEW: "view.md", TABLE: "table.md", SCRIPT: "sql_script.md", PIPELINE: "pipeline.md",
             NOTEBOOK: "notebook.md"}
TRANSFORMABLE_KINDS = tuple(TEMPLATES)
_OUTPUT = {
    PROCEDURE: "`CREATE PROCEDURE` statement", VIEW: "`CREATE VIEW` statement", TABLE: "`CREATE TABLE` statement",
    SCRIPT: "T-SQL script text", PIPELINE: "Synapse pipeline resource JSON (it is converted to Fabric afterwards)",
    NOTEBOOK: 'notebook as JSON, `{"language": ..., "cells": [...]}`, in the same shape as the source',
}
_PLATFORM = {PIPELINE: "Synapse pipeline JSON", SCRIPT: "Synapse SQL script", NOTEBOOK: "Synapse notebook cells"}
MAX_DEPENDENCIES = 40
MAX_COLUMNS_PER_TABLE = 60
MAX_SOURCE_CHARS = 200_000


class NotTransformable(Exception):
    """This object cannot be sent for a draft; the message says why."""


@dataclass
class Subject:
    """One object's source, ready for a prompt."""

    kind: str
    language: str
    code: str
    rule_output: str = ""
    rule_notes: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def source_hash(self) -> str:
        return source_hash(self.kind, self.code)


def source_hash(kind: str, code: str) -> str:
    return hashlib.sha256(f"{kind}\n{code}".encode("utf-8")).hexdigest()


def _live_sql(text: str) -> str:
    return re.sub(r"/\*.*?\*/|--[^\n]*", " ", text, flags=re.S)


def synapse_only(text: str) -> List[str]:
    """Synapse-only constructs still in live (uncommented) code."""
    live = _live_sql(text)
    return [label for pattern, label in SYNAPSE_ONLY if pattern.search(live)]


def _sql_subject(kind: str, text: str) -> Subject:
    converted = tsql_rules.rewrite(text.strip())
    problems = [f"Still uses {label}, which a Fabric Warehouse rejects." for label in synapse_only(converted.text)]
    return Subject(kind, "sql", text.strip(), converted.text if converted.changed else "", list(converted.notes), problems)


def _source_type(column: Any) -> str:
    name = (column.data_type or "unknown").lower()
    length = column.max_length
    if name in ("char", "varchar", "binary", "varbinary"):
        return f"{name}({'MAX' if length is not None and length < 0 else length})"
    if name in ("nchar", "nvarchar"):
        return f"{name}({'MAX' if length is not None and length < 0 else (length or 0) // 2})"
    if name in ("decimal", "numeric"):
        return f"{name}({column.precision},{column.scale})"
    if name in ("datetime2", "time", "datetimeoffset"):
        return f"{name}({column.scale})"
    return name


def table_source(table: Any) -> str:
    """The table as a dedicated-pool ``CREATE TABLE``. A pool keeps no DDL text, so it is rebuilt from the catalog."""
    lines = []
    for column in sorted(table.columns, key=lambda c: c.column_id):
        identity = " IDENTITY(1,1)" if column.is_identity else ""
        nullable = "NULL" if column.is_nullable is not False else "NOT NULL"
        lines.append(f"    {warehouse_ddl.quote(column.name)} {_source_type(column)}{identity} {nullable}")
    distribution = table.distribution.value
    options = []
    if distribution == "hash":
        columns = ", ".join(warehouse_ddl.quote(c) for c in table.distribution_column_names) or "?"
        options.append(f"DISTRIBUTION = HASH({columns})")
    elif distribution == "round_robin":
        options.append("DISTRIBUTION = ROUND_ROBIN")
    elif distribution == "replicate":
        options.append("DISTRIBUTION = REPLICATE")
    header = "-- Rebuilt from the pool catalog: a dedicated pool keeps no CREATE TABLE text.\n"
    body = f"CREATE TABLE {warehouse_ddl.qualified(table.key.schema, table.key.name)} (\n" + ",\n".join(lines) + "\n)"
    return header + body + (f"\nWITH ({', '.join(options)})" if options else "") + ";"


def extract(source: Source) -> Subject:
    """The source text and the rule-based result for one plan item."""
    kind, payload = source.kind, source.payload
    if kind not in TRANSFORMABLE_KINDS:
        raise NotTransformable(f"Drafts are not available for {source.type or kind} objects yet.")
    if payload is None:
        raise NotTransformable("Discovery did not keep this object's definition. Run discovery again.")

    if kind in (VIEW, PROCEDURE):
        definition = getattr(payload, "definition", None)
        text = getattr(definition, "text", None)
        if not text or not getattr(definition, "is_readable", False):
            raise NotTransformable("The definition could not be read from the pool (encrypted, or VIEW DEFINITION is not granted).")
        subject = _sql_subject(kind, text)
    elif kind == TABLE:
        if not getattr(payload, "columns", None):
            raise NotTransformable("No columns were discovered for this table.")
        subject = Subject(kind, "sql", table_source(payload))
        try:
            subject.rule_output, subject.rule_notes = warehouse_ddl.create_table(payload)
        except warehouse_ddl.UnsupportedColumn as exc:
            subject.problems.append(str(exc))
    elif kind == SCRIPT:
        content = ((payload.get("properties") or {}).get("content") or {}) if isinstance(payload, Mapping) else {}
        query = str(content.get("query") or "")
        if not query.strip():
            raise NotTransformable("The script has no query text.")
        language = str((content.get("metadata") or {}).get("language") or "sql").lower()
        if language != "sql":
            raise NotTransformable(f"The script is {language}, not SQL.")
        subject = _sql_subject(kind, query)
    elif kind == NOTEBOOK:
        if not isinstance(payload, Mapping) or not (payload.get("properties") or {}).get("cells"):
            raise NotTransformable("Discovery did not keep this notebook's cells. Run discovery again.")
        subject = Subject(kind, "json", json.dumps(notebooks.cells_document(payload), indent=2, ensure_ascii=False))
        subject.rule_output, subject.rule_notes, subject.problems = notebook_rules(payload)
    else:  # PIPELINE
        bundle = payload if isinstance(payload, Mapping) else {}
        resource = bundle.get("resource") or {}
        if not resource:
            raise NotTransformable("Discovery did not keep this pipeline's definition. Run discovery again.")
        subject = Subject(kind, "json", json.dumps(resource, indent=2, ensure_ascii=False))
        subject.problems = [f"No Fabric equivalent the converter knows: {a}" for a in unsupported_activities(resource, bundle)]

    if len(subject.code) > MAX_SOURCE_CHARS:
        raise NotTransformable(f"The definition is longer than {MAX_SOURCE_CHARS:,} characters; convert it by hand.")
    return subject


def notebook_rules(payload: Mapping[str, Any]):
    """(rule output in the same cells shape, what the rules changed, what is left for review)."""
    try:
        document, notes = notebooks.to_fabric_ipynb(payload)
    except notebooks.NotebookNotMigratable as exc:
        return "", [], [str(exc)]
    converted = notebooks.cells_document({"properties": {"cells": document["cells"], "metadata": document["metadata"]}})
    review = [n for n in notes if n.startswith("Review before running")]
    changed = [n for n in notes if not n.startswith("Review before running")]
    original = notebooks.cells_document(payload)
    output = json.dumps(converted, indent=2, ensure_ascii=False) if converted["cells"] != original["cells"] else ""
    return output, changed, review


def unsupported_activities(resource: Mapping[str, Any], bundle: Optional[Mapping[str, Any]] = None) -> List[str]:
    """Activities the pipeline converter would refuse. Run with nothing in Fabric yet: only ``unsupported`` is used."""
    bundle = bundle or {}
    context = pipelines.Context(
        datasets=bundle.get("datasets", {}), linked_services=bundle.get("linkedServices", {}), connections={},
        notebooks={}, pipelines={}, spark_jobs={}, workspace_id="", warehouse=None, pool_name="")
    try:
        return list(pipelines.convert(resource, context).unsupported)
    except Exception as exc:  # noqa: BLE001 - a malformed pipeline is reported, not raised
        return [f"the converter could not read it ({type(exc).__name__})"]


# -- the prompt --------------------------------------------------------------------------


def _fence(*texts: str) -> str:
    longest = max([len(m) for t in texts for m in re.findall(r"`+", t or "")] or [0])
    return "`" * max(3, longest + 1)


def _discovery_notes(detail: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    constructs = (detail.get("overview") or {}).get("synapseSpecificConstructs") or []
    if constructs:
        out.append("Synapse-specific constructs: " + ", ".join(str(c) for c in constructs))
    for issue in detail.get("issues") or []:
        if issue.get("message"):
            out.append(str(issue["message"]))
    return out


def _dependencies(detail: Mapping[str, Any], records: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for dep in (detail.get("dependencies") or [])[:MAX_DEPENDENCIES]:
        line = f"`{dep.get('name')}` ({dep.get('type') or dep.get('kind')})"
        record = records.get(dep.get("objectId") or "")
        columns = getattr(getattr(record, "content", None), "columns", None)
        if columns:
            shown = sorted(columns, key=lambda c: c.column_id)[:MAX_COLUMNS_PER_TABLE]
            line += ": " + ", ".join(f"{c.name} {_source_type(c)}" for c in shown)
            if len(columns) > MAX_COLUMNS_PER_TABLE:
                line += f", ... ({len(columns) - MAX_COLUMNS_PER_TABLE} more)"
        out.append(line)
    return out


def build(subject: Subject, source: Source, detail: Mapping[str, Any], target: Mapping[str, Any],
          records: Mapping[str, Any], prompts_dir: Path) -> str:
    """The full prompt for one object, from the templates in ``prompts_dir``."""
    context: Dict[str, Any] = {
        "object": {"id": source.id, "name": source.object_name or source.name, "type": source.type, "kind": subject.kind,
                   "schema": source.schema if subject.kind in (TABLE, VIEW, PROCEDURE) else ""},
        "notebook": {"language": json.loads(subject.code).get("language", "") if subject.kind == NOTEBOOK else ""},
        "source": {"platform": _PLATFORM.get(subject.kind, "Azure Synapse dedicated SQL pool"), "language": subject.language,
                   "code": subject.code, "fence": _fence(subject.code)},
        "target": {"platform": "Microsoft Fabric",
                   "component": (detail.get("target") or {}).get("component") or detail.get("fabricTarget") or "Microsoft Fabric",
                   "workspace": target.get("workspace") or "", "warehouse": target.get("warehouse") or ""},
        "rules": {"output": subject.rule_output, "fence": _fence(subject.rule_output), "notes": subject.rule_notes,
                  "problems": subject.problems},
        "discovery": {"notes": _discovery_notes(detail)},
        "dependencies": _dependencies(detail, records),
        "output": {"description": _OUTPUT[subject.kind]},
    }
    base = templates.read(prompts_dir / "base.md")
    if not base.strip():
        raise templates.TemplateError(f"The prompt template base.md is missing from {prompts_dir}.")
    context["type_instructions"] = templates.render(templates.read(prompts_dir / TEMPLATES[subject.kind]), context).strip()
    context["project"] = templates.render(templates.read(prompts_dir / "project.md"), context).strip()
    return templates.render(base, context)
