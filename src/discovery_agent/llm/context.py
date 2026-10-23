"""What a transformation prompt is made of: the object's source, what the rules do with it, and
what discovery knows about its surroundings.

The source text extracted here is also what an accepted transform is pinned to: its hash is stored
with the transform, and a run uses the transform only while the hash still matches.

Everything about the *output* depends on where the SQL pool goes (the run option ``target``): for a
Warehouse a procedure stays T-SQL; for a Lakehouse its rule output is the Spark notebook the run would
create, the prompt asks for one, and a table is a Delta ``CREATE TABLE`` in Spark SQL. Prompts for a
Lakehouse come from ``lakehouse/<template>`` beside the Warehouse ones, when that file exists.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from discovery_agent.llm import templates
from discovery_agent.migration import lakehouse_ddl, notebooks, pipelines, spark_sql, tsql_rules, warehouse_ddl
from discovery_agent.migration.common import (
    FUNCTION, LAKEHOUSE_TARGET, NOTEBOOK, PIPELINE, PROCEDURE, SCRIPT, TABLE, TARGETS, VIEW, WAREHOUSE_TARGET, Source,
)
from discovery_agent.migration.preflight import SYNAPSE_ONLY

#: The kinds a draft can be generated for, and the template that describes each.
TEMPLATES = {PROCEDURE: "stored_procedure.md", VIEW: "view.md", TABLE: "table.md", SCRIPT: "sql_script.md", PIPELINE: "pipeline.md",
             NOTEBOOK: "notebook.md", FUNCTION: "function.md"}
TRANSFORMABLE_KINDS = tuple(TEMPLATES)
_OUTPUT = {
    PROCEDURE: "`CREATE PROCEDURE` statement", VIEW: "`CREATE VIEW` statement", TABLE: "`CREATE TABLE` statement",
    FUNCTION: "`CREATE FUNCTION` statement (scalar or inline table-valued)",
    SCRIPT: "T-SQL script text", PIPELINE: "Synapse pipeline resource JSON (it is converted to Fabric afterwards)",
    NOTEBOOK: 'notebook as JSON, `{"language": ..., "cells": [...]}`, in the same shape as the source',
}
#: What a Lakehouse draft is, where it differs: procedures and scripts that change data become PySpark notebooks.
_LAKEHOUSE_OUTPUT = {
    PROCEDURE: 'PySpark notebook as JSON, `{"language": "python", "cells": [...]}` (the same shape as the rule output)',
    SCRIPT: 'PySpark notebook as JSON, `{"language": "python", "cells": [...]}` (the same shape as the rule output)',
    TABLE: "Spark SQL `CREATE TABLE IF NOT EXISTS ... USING DELTA` statement",
    VIEW: "`CREATE VIEW` statement for the Lakehouse's SQL analytics endpoint",
    FUNCTION: "`CREATE FUNCTION` statement for the Lakehouse's SQL analytics endpoint (scalar or inline table-valued)",
}
#: Lakehouse kinds whose draft is a notebook (cells JSON) rather than SQL text.
NOTEBOOK_DRAFTS = (PROCEDURE, SCRIPT)
_PLATFORM = {PIPELINE: "Synapse pipeline JSON", SCRIPT: "Synapse SQL script", NOTEBOOK: "Synapse notebook cells"}


def target_of(value: Any) -> str:
    """A request's target, ``warehouse`` when it names none (or one this build does not know)."""
    return value if value in TARGETS else WAREHOUSE_TARGET


def lakehouse_draft_is_notebook(kind: str, target: str) -> bool:
    return target == LAKEHOUSE_TARGET and kind in NOTEBOOK_DRAFTS
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
    #: Where the output goes (``warehouse`` / ``lakehouse``), and the language of the rule output and of a draft.
    target: str = WAREHOUSE_TARGET
    output_language: str = ""

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


def _sql_subject(kind: str, text: str, target: str = WAREHOUSE_TARGET) -> Subject:
    converted = tsql_rules.rewrite(text.strip())
    where = "a Lakehouse's SQL analytics endpoint" if target == LAKEHOUSE_TARGET else "a Fabric Warehouse"
    problems = [f"Still uses {label}, which {where} rejects." for label in synapse_only(converted.text)]
    return Subject(kind, "sql", text.strip(), converted.text if converted.changed else "", list(converted.notes), problems,
                   target, "sql")


def notebook_cells(document: Mapping[str, Any]) -> str:
    """A generated notebook as the editable cells JSON a reviewer (or the model) works on."""
    cells = []
    for cell in document.get("cells") or []:
        entry: Dict[str, Any] = {"cell_type": cell.get("cell_type"), "source": "".join(cell.get("source") or [])}
        if (cell.get("metadata") or {}).get("tags"):
            entry["tags"] = list(cell["metadata"]["tags"])
        cells.append(entry)
    return json.dumps({"language": "python", "cells": cells}, indent=2, ensure_ascii=False)


def _notebook_subject(kind: str, text: str, name: str, converted: "spark_sql.Converted") -> Subject:
    """A procedure or script for a Lakehouse: the rule output is the Spark notebook the run would create."""
    return Subject(kind, "sql", text.strip(), notebook_cells(converted.document), list(converted.notes),
                   [f"The notebook needs a person: {r}" for r in converted.review], LAKEHOUSE_TARGET, "json")


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


def extract(source: Source, target: str = WAREHOUSE_TARGET, pool: str = "", lakehouse_name: str = "",
            procedures: Optional[Mapping[str, Any]] = None) -> Subject:
    """The source text and the rule-based result for one plan item, for a Warehouse or a Lakehouse target.
    ``pool`` is the SQL pool being migrated: what a Lakehouse pipeline must stop using. For a Lakehouse,
    ``lakehouse_name`` names it in the notebooks, and ``procedures`` (``{"notebooks": ..., "params": ...}``, as
    ``spark_sql`` takes them) lets an EXEC of another procedure become a run of its notebook, as in the run."""
    kind, payload = source.kind, source.payload
    lakehouse = target == LAKEHOUSE_TARGET
    if kind not in TRANSFORMABLE_KINDS:
        raise NotTransformable(f"Drafts are not available for {source.type or kind} objects yet.")
    if payload is None:
        raise NotTransformable("Discovery did not keep this object's definition. Run discovery again.")

    if kind in (VIEW, PROCEDURE, FUNCTION):
        definition = getattr(payload, "definition", None)
        text = getattr(definition, "text", None)
        if not text or not getattr(definition, "is_readable", False):
            raise NotTransformable("The definition could not be read from the pool (encrypted, or VIEW DEFINITION is not granted).")
        if lakehouse and kind == PROCEDURE:
            subject = _notebook_subject(kind, text, source.name, spark_sql.procedure_notebook(
                text, name=f"{source.schema or 'dbo'}_{source.object_name or source.name}", lakehouse_id="", lakehouse=lakehouse_name,
                workspace_id="", pool=pool, **_calls(procedures)))
        else:
            subject = _sql_subject(kind, text, target)
    elif kind == TABLE:
        if not getattr(payload, "columns", None):
            raise NotTransformable("No columns were discovered for this table.")
        subject = Subject(kind, "sql", table_source(payload), target=target, output_language="sql")
        ddl = lakehouse_ddl if lakehouse else warehouse_ddl
        try:
            subject.rule_output, subject.rule_notes = ddl.create_table(payload)
        except (warehouse_ddl.UnsupportedColumn, lakehouse_ddl.UnsupportedColumn) as exc:
            subject.problems.append(str(exc))
    elif kind == SCRIPT:
        content = ((payload.get("properties") or {}).get("content") or {}) if isinstance(payload, Mapping) else {}
        query = str(content.get("query") or "")
        if not query.strip():
            raise NotTransformable("The script has no query text.")
        language = str((content.get("metadata") or {}).get("language") or "sql").lower()
        if language != "sql":
            raise NotTransformable(f"The script is {language}, not SQL.")
        if lakehouse:
            # A Lakehouse run turns a script that changes data into a Spark notebook (a reading one stays T-SQL);
            # a draft is always a notebook, so the reviewer sees the same shape whichever it is.
            subject = _notebook_subject(kind, query, source.name, spark_sql.script_notebook(
                query, name=source.name, lakehouse_id="", lakehouse=lakehouse_name, workspace_id="", pool=pool, **_calls(procedures)))
        else:
            subject = _sql_subject(kind, query)
    elif kind == NOTEBOOK:
        if not isinstance(payload, Mapping) or not (payload.get("properties") or {}).get("cells"):
            raise NotTransformable("Discovery did not keep this notebook's cells. Run discovery again.")
        subject = Subject(kind, "json", json.dumps(notebooks.cells_document(payload), indent=2, ensure_ascii=False),
                          target=target, output_language="json")
        subject.rule_output, subject.rule_notes, subject.problems = notebook_rules(payload, target)
    else:  # PIPELINE
        bundle = payload if isinstance(payload, Mapping) else {}
        resource = bundle.get("resource") or {}
        if not resource:
            raise NotTransformable("Discovery did not keep this pipeline's definition. Run discovery again.")
        subject = Subject(kind, "json", json.dumps(resource, indent=2, ensure_ascii=False), target=target, output_language="json")
        subject.problems = [f"No Fabric equivalent the converter knows: {a}" for a in unsupported_activities(resource, bundle, target, pool)]

    if len(subject.code) > MAX_SOURCE_CHARS:
        raise NotTransformable(f"The definition is longer than {MAX_SOURCE_CHARS:,} characters; convert it by hand.")
    return subject


def _calls(procedures: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    return {"notebooks": (procedures or {}).get("notebooks") or {}, "callee_params": (procedures or {}).get("params") or {}}


def notebook_rules(payload: Mapping[str, Any], target: str = WAREHOUSE_TARGET):
    """(rule output in the same cells shape, what the rules changed, what is left for review)."""
    try:
        # For a Lakehouse, reads of the pool become reads of its tables; the run's ids are not known here.
        lakehouse = ("", "the run's Lakehouse", "") if target == LAKEHOUSE_TARGET else None
        document, notes = notebooks.to_fabric_ipynb(payload, lakehouse=lakehouse, pool_name=_pool_of(payload) if lakehouse else "")
    except notebooks.NotebookNotMigratable as exc:
        return "", [], [str(exc)]
    converted = notebooks.cells_document({"properties": {"cells": document["cells"], "metadata": document["metadata"]}})
    review = [n for n in notes if n.startswith("Review before running")]
    changed = [n for n in notes if not n.startswith("Review before running")]
    original = notebooks.cells_document(payload)
    output = json.dumps(converted, indent=2, ensure_ascii=False) if converted["cells"] != original["cells"] else ""
    return output, changed, review


def _pool_of(payload: Mapping[str, Any]) -> str:
    """The pool a notebook reads with ``synapsesql("pool.schema.table")``, or empty."""
    m = re.search(r"synapsesql\(\s*[\"']([^\"'.]+)\.", json.dumps(payload))
    return m.group(1) if m else ""


def unsupported_activities(resource: Mapping[str, Any], bundle: Optional[Mapping[str, Any]] = None,
                           target: str = WAREHOUSE_TARGET, pool: str = "") -> List[str]:
    """Activities the pipeline converter would refuse. Run with nothing in Fabric yet: only ``unsupported`` is used."""
    bundle = bundle or {}
    lakehouse = pipelines.Lakehouse("", "the run's Lakehouse", "") if target == LAKEHOUSE_TARGET else None
    context = pipelines.Context(
        datasets=bundle.get("datasets", {}), linked_services=bundle.get("linkedServices", {}), connections={},
        notebooks={}, pipelines={}, spark_jobs={}, workspace_id="", warehouse=None, pool_name=pool,
        lakehouse=lakehouse)
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
    lakehouse = subject.target == LAKEHOUSE_TARGET
    component = (detail.get("target") or {}).get("component") or detail.get("fabricTarget") or "Microsoft Fabric"
    if lakehouse and subject.kind in (TABLE, VIEW, PROCEDURE, FUNCTION, SCRIPT):
        component = "Fabric Lakehouse (Delta tables; read-only SQL analytics endpoint)"
    context: Dict[str, Any] = {
        "object": {"id": source.id, "name": source.object_name or source.name, "type": source.type, "kind": subject.kind,
                   "schema": source.schema if subject.kind in (TABLE, VIEW, PROCEDURE, FUNCTION) else ""},
        "notebook": {"language": json.loads(subject.code).get("language", "") if subject.kind == NOTEBOOK else ""},
        "source": {"platform": _PLATFORM.get(subject.kind, "Azure Synapse dedicated SQL pool"), "language": subject.language,
                   "code": subject.code, "fence": _fence(subject.code)},
        "target": {"platform": "Microsoft Fabric", "component": component, "kind": subject.target,
                   "workspace": target.get("workspace") or "",
                   "warehouse": "" if lakehouse else target.get("warehouse") or "",
                   "lakehouse": target.get("warehouse") or "" if lakehouse else ""},
        "rules": {"output": subject.rule_output, "fence": _fence(subject.rule_output), "notes": subject.rule_notes,
                  "problems": subject.problems, "language": subject.output_language or subject.language},
        "discovery": {"notes": _discovery_notes(detail)},
        "dependencies": _dependencies(detail, records),
        "output": {"description": (_LAKEHOUSE_OUTPUT.get(subject.kind) if lakehouse else None) or _OUTPUT[subject.kind]},
    }
    base = templates.read(prompts_dir / "base.md")
    if not base.strip():
        raise templates.TemplateError(f"The prompt template base.md is missing from {prompts_dir}.")
    template = prompts_dir / TEMPLATES[subject.kind]
    if lakehouse and (prompts_dir / "lakehouse" / TEMPLATES[subject.kind]).is_file():
        template = prompts_dir / "lakehouse" / TEMPLATES[subject.kind]
    context["type_instructions"] = templates.render(templates.read(template), context).strip()
    context["project"] = templates.render(templates.read(prompts_dir / "project.md"), context).strip()
    return templates.render(base, context)
