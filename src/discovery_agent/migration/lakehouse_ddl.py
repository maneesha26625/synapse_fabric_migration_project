"""Dedicated SQL pool schema -> Fabric Lakehouse (Delta tables), for runs whose SQL target is a Lakehouse.

A Lakehouse's tables are Delta tables written by Spark. Its SQL analytics endpoint reads them with
T-SQL but cannot create or change them, so a table is created with Spark SQL, not T-SQL: the run
gathers every table's ``CREATE TABLE`` into one notebook (``ddl_notebook``), runs it once, and reads
back what each statement did from a small JSON file the notebook writes into the Lakehouse.

What changes on the way, each reported in the table's notes:

* **Types** map to their Spark (Delta) equivalents. Text of every length is ``STRING``; dates and
  times are ``TIMESTAMP`` (microseconds); ``time`` has no Spark type and is kept as text.
* **IDENTITY** is not kept: a Delta table in a Lakehouse does not generate key values. The loaded
  rows keep the values Synapse gave them; new rows need their keys made by the code that writes them.
* **Primary keys and unique constraints** are not kept: a Lakehouse table has no key constraints.
* **Distribution, columnstore and partitioning** are dropped: Delta and Fabric manage storage.
* **Column names** that Delta cannot hold as they are (spaces, ``,;{}()=``) are kept by switching the
  table to Delta column mapping by name.

The SQL analytics endpoint shows each table with T-SQL types (``endpoint_type``), which validation
compares with what it finds there.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence, Tuple

from discovery_agent.sql.models import SqlColumn, SqlTable

#: The schema stored procedures converted to notebooks keep their temporary tables in.
WORK_SCHEMA = "tmp_work"
#: Characters a Delta column name cannot hold without column mapping.
_NEEDS_MAPPING = re.compile(r"[ ,;{}()\n\t=]")
#: The notebook that creates a run's Lakehouse tables, and where it writes what each statement did.
RESULTS_FOLDER = "Files/_migration"


class UnsupportedColumn(Exception):
    """A column whose type has no Delta equivalent."""


def quote(name: str) -> str:
    """A Spark SQL identifier in backticks."""
    return "`" + str(name).replace("`", "``") + "`"


def qualified(schema: str, name: str) -> str:
    return f"{quote(schema)}.{quote(name)}"


def map_type(column: SqlColumn) -> Tuple[str, Optional[str]]:
    """The Spark SQL type for one column, and a note when something about it changed."""
    source = (column.data_type or "").strip().lower()
    name = column.name
    if source == "bit":
        return "BOOLEAN", None
    if source == "tinyint":
        return "SMALLINT", f"{name}: tinyint stored as SMALLINT (Spark's TINYINT is signed)"
    if source in ("smallint", "int", "bigint"):
        return {"smallint": "SMALLINT", "int": "INT", "bigint": "BIGINT"}[source], None
    if source in ("decimal", "numeric"):
        return f"DECIMAL({column.precision or 18},{column.scale or 0})", None
    if source == "money":
        return "DECIMAL(19,4)", f"{name}: money stored as DECIMAL(19,4)"
    if source == "smallmoney":
        return "DECIMAL(10,4)", f"{name}: smallmoney stored as DECIMAL(10,4)"
    if source == "float":
        return ("FLOAT", None) if column.precision is not None and 0 < column.precision <= 24 else ("DOUBLE", None)
    if source == "real":
        return "FLOAT", None
    if source == "date":
        return "DATE", None
    if source in ("datetime", "smalldatetime"):
        return "TIMESTAMP", None
    if source == "datetime2":
        if column.scale is not None and column.scale > 6:
            return "TIMESTAMP", f"{name}: datetime2({column.scale}) stored as TIMESTAMP (microseconds)"
        return "TIMESTAMP", None
    if source == "datetimeoffset":
        return "TIMESTAMP", f"{name}: datetimeoffset stored as TIMESTAMP in UTC; the time-zone offset is not kept"
    if source == "time":
        return "STRING", f"{name}: time stored as STRING (Spark has no time-of-day type)"
    if source in ("char", "varchar", "nchar", "nvarchar", "sysname"):
        if column.max_length is not None and column.max_length < 0:
            return "STRING", (f"{name}: {source}(max) stored as STRING; the SQL analytics endpoint may show long values "
                              "cut to 8000 bytes")
        return "STRING", None
    if source in ("text", "ntext", "xml"):
        return "STRING", f"{name}: {source} stored as STRING; the SQL analytics endpoint may show long values cut to 8000 bytes"
    if source == "uniqueidentifier":
        return "STRING", f"{name}: uniqueidentifier stored as STRING"
    if source in ("binary", "varbinary", "image"):
        return "BINARY", None
    if source in ("timestamp", "rowversion"):
        return "BINARY", f"{name}: rowversion stored as BINARY; it no longer changes on update"
    if source in ("geography", "geometry"):
        return "BINARY", f"{name}: {source} stored as BINARY (well-known binary)"
    raise UnsupportedColumn(f"Column {name} has type {source or 'unknown'}, which a Lakehouse (Delta) table cannot store.")


def endpoint_type(column: SqlColumn) -> str:
    """How the SQL analytics endpoint shows the column, in validation's spelling (``validation.actual_type``)."""
    spark, _ = map_type(column)
    if spark.startswith("DECIMAL"):
        return "decimal" + spark[len("DECIMAL"):].lower()
    return {
        "BOOLEAN": "bit", "SMALLINT": "smallint", "INT": "int", "BIGINT": "bigint", "FLOAT": "real", "DOUBLE": "float",
        "DATE": "date", "TIMESTAMP": "datetime2(6)", "STRING": "varchar(8000)", "BINARY": "varbinary(8000)",
    }[spark]


def columns_of(table: SqlTable) -> List[SqlColumn]:
    return sorted(table.columns, key=lambda c: c.column_id)


def create_table(table: SqlTable, *, schema: Optional[str] = None, name: Optional[str] = None) -> Tuple[str, List[str]]:
    """``CREATE TABLE IF NOT EXISTS ... USING DELTA`` for a Lakehouse, and notes on what changed."""
    if not table.columns:
        raise UnsupportedColumn("No columns were discovered for this table, so it cannot be recreated.")
    notes: List[str] = []
    lines: List[str] = []
    mapped = False
    for column in columns_of(table):
        spark_type, note = map_type(column)
        if note:
            notes.append(note)
        if column.is_identity:
            notes.append(f"{column.name}: IDENTITY not kept (a Lakehouse table does not generate keys); loaded rows keep "
                         "their Synapse values, new rows need their keys made by the code that writes them")
        if _NEEDS_MAPPING.search(column.name):
            mapped = True
        nullability = " NOT NULL" if column.is_nullable is False else ""
        lines.append(f"    {quote(column.name)} {spark_type}{nullability}")
    keys = [i for i in table.indexes if i.is_primary_key or i.is_unique_constraint]
    if keys:
        notes.append("Primary key / unique constraint not kept: a Lakehouse table has no key constraints. "
                     "Define relationships in the semantic model.")
    if table.distribution.value not in ("unknown", "not_applicable"):
        notes.append(f"Distribution ({table.distribution.value}) and index settings dropped: Delta manages storage itself.")
    properties = ""
    if mapped:
        properties = ("\nTBLPROPERTIES ('delta.columnMapping.mode' = 'name', 'delta.minReaderVersion' = '2', "
                      "'delta.minWriterVersion' = '5')")
        notes.append("Column names with spaces or special characters kept with Delta column mapping by name.")
    target = qualified(schema or table.key.schema, name or table.key.name)
    sql = f"CREATE TABLE IF NOT EXISTS {target} (\n" + ",\n".join(lines) + f"\n) USING DELTA{properties}"
    return sql, notes


def create_schema(schema: str) -> str:
    return f"CREATE SCHEMA IF NOT EXISTS {quote(schema)}"


def results_path(run_id: str) -> str:
    """Where the DDL notebook writes what each statement did, inside the Lakehouse."""
    return f"{RESULTS_FOLDER}/ddl_run_{re.sub(r'[^A-Za-z0-9_-]', '_', run_id)}.json"


def notebook_name(lakehouse: str) -> str:
    return f"ddl_{lakehouse}"[:120]


def ddl_notebook(statements: Sequence[Tuple[str, str, str]], result_url: str, lakehouse_id: str, lakehouse: str,
                 workspace_id: str) -> Dict[str, object]:
    """The ipynb document that runs ``(key, table, Spark SQL)`` statements in order, one at a time, and writes
    ``{key: {"ok": bool, "existed": bool, "error": str}}`` to ``result_url``. ``table`` (``schema.name``, or empty)
    is looked up first, so a table already there is reported as such. A failed statement does not stop the others."""
    listing = "statements = [\n" + "".join(f"    ({key!r}, {table!r}, {sql!r}),\n" for key, table, sql in statements) + "]\n"
    runner = (
        "import json\n"
        "results = {}\n"
        "for key, table, sql in statements:\n"
        "    try:\n"
        "        existed = bool(table) and spark.catalog.tableExists(table)\n"
        "        if not existed:\n"
        "            spark.sql(sql)\n"
        "        results[key] = {\"ok\": True, \"existed\": existed}\n"
        "    except Exception as exc:  # one statement never stops the others; its error is reported on its object\n"
        "        results[key] = {\"ok\": False, \"error\": str(exc)[:1500]}\n"
        f"notebookutils.fs.put({result_url!r}, json.dumps(results), True)\n"
        "print(f\"{sum(r['ok'] for r in results.values())} of {len(results)} statements succeeded\")\n"
    )
    intro = (f"# Lakehouse tables for {lakehouse}\n\n"
             "Created by the Synapse migration run. Each statement creates a schema or a Delta table "
             "(`IF NOT EXISTS`, so running it again changes nothing that is already there).")
    return lakehouse_document([("markdown", intro), ("code", listing), ("code", runner)], lakehouse_id, lakehouse, workspace_id)


class NotANotebook(Exception):
    """An accepted transform that is not a notebook's cells."""


def document_from_cells(code: str, lakehouse_id: str, lakehouse: str, workspace_id: str) -> Dict[str, object]:
    """A reviewed notebook (``{"language", "cells": [{"cell_type", "source", "tags"?}]}``, the shape a Lakehouse
    procedure or script transform is accepted in) as an ipynb document attached to the Lakehouse."""
    try:
        edited = json.loads(code)
    except ValueError as exc:
        raise NotANotebook(f"The accepted transform is not valid JSON ({exc}).") from exc
    if not isinstance(edited, dict) or not isinstance(edited.get("cells"), list) or not edited["cells"]:
        raise NotANotebook('The accepted transform has no "cells" list.')
    document = lakehouse_document([], lakehouse_id, lakehouse, workspace_id)
    for n, cell in enumerate(edited["cells"], 1):
        if not isinstance(cell, dict) or not isinstance(cell.get("source"), (str, list)):
            raise NotANotebook(f"Cell {n} of the accepted transform has no source text.")
        kind = cell.get("cell_type") if cell.get("cell_type") in ("code", "markdown", "raw") else "code"
        text = cell["source"] if isinstance(cell["source"], str) else "".join(cell["source"])
        out: Dict[str, object] = {"cell_type": kind, "source": text.splitlines(keepends=True),
                                  "metadata": {"tags": list(cell["tags"])} if cell.get("tags") else {}}
        if kind == "code":
            out.update({"outputs": [], "execution_count": None})
        document["cells"].append(out)  # type: ignore[union-attr]
    return document


def lakehouse_document(cells: Sequence[Tuple[str, str]], lakehouse_id: str, lakehouse: str, workspace_id: str,
                       parameters: Optional[str] = None) -> Dict[str, object]:
    """A PySpark ipynb document with the Lakehouse attached as its default, from ``(cell type, text)`` pairs.

    ``parameters`` becomes the notebook's parameters cell, the one a pipeline's notebook activity overrides."""
    out: List[Dict[str, object]] = []
    if parameters is not None:
        out.append({"cell_type": "code", "source": parameters.splitlines(keepends=True), "outputs": [], "execution_count": None,
                    "metadata": {"tags": ["parameters"]}})
    for kind, text in cells:
        cell: Dict[str, object] = {"cell_type": kind, "source": text.splitlines(keepends=True), "metadata": {}}
        if kind == "code":
            cell.update({"outputs": [], "execution_count": None})
        out.append(cell)
    if parameters is not None and len(out) > 1 and out[1].get("cell_type") == "markdown":
        out[0], out[1] = out[1], out[0]  # the title first, then the parameters
    return {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {
            "language_info": {"name": "python"},
            "kernel_info": {"name": "synapse_pyspark"},
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "dependencies": {"lakehouse": {"default_lakehouse": lakehouse_id, "default_lakehouse_name": lakehouse,
                                           "default_lakehouse_workspace_id": workspace_id}},
        },
        "cells": out,
    }
