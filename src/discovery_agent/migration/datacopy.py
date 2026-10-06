"""How a table's rows are read and counted, for the data pipelines and validation.

Data moves with Fabric data pipelines (``datapipeline``), not through this
process. What lives here is the SQL those pipelines and the checks use: the
SELECT that reads a table from the Synapse pool in a form a Copy activity can
carry, and the row count both sides are compared on.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from discovery_agent.migration import warehouse_ddl
from discovery_agent.sql.models import SqlColumn, SqlTable

#: A Warehouse table takes at most 1024 columns, so a wider source cannot be loaded.
MAX_COLUMNS = 1024


def columns_of(table: SqlTable) -> List[SqlColumn]:
    return sorted(table.columns, key=lambda c: c.column_id)


def source_expression(column: SqlColumn) -> str:
    """How to read a column so a Copy activity can carry it."""
    ref = warehouse_ddl.quote(column.name)
    kind = (column.data_type or "").lower()
    if kind in ("geography", "geometry"):
        return f"{ref}.STAsBinary() AS {ref}"
    if kind == "xml":
        return f"CAST({ref} AS nvarchar(max)) AS {ref}"
    return ref


def select_sql(table: SqlTable) -> str:
    cols = ", ".join(source_expression(c) for c in columns_of(table))
    return f"SELECT {cols} FROM {warehouse_ddl.qualified(table.key.schema, table.key.name)}"


def count_sql(schema: str, name: str) -> str:
    return f"SELECT COUNT_BIG(*) FROM {warehouse_ddl.qualified(schema, name)}"


def describe_count(n: int) -> str:
    return f"{n:,} row{'' if n == 1 else 's'}"


def preflight(table: SqlTable) -> Optional[Tuple[str, str]]:
    """Why a table's data cannot be loaded, as (code, message), or None."""
    if table.is_external:
        return ("EXTERNAL", "An external table stores no rows in the pool; it becomes a OneLake shortcut instead.")
    if not table.columns:
        return ("NO_COLUMNS", "No columns were discovered, so its rows cannot be mapped.")
    if len(table.columns) > MAX_COLUMNS:
        return ("TOO_WIDE", f"It has {len(table.columns)} columns; a Warehouse table takes at most {MAX_COLUMNS}.")
    return None
