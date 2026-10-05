"""Copy one table's rows from the Synapse pool into the Fabric Warehouse.

Read in chunks from the source, write as multi-row ``INSERT ... VALUES``
statements. That is the one bulk path a Warehouse accepts through a plain SQL
connection; it is correct and needs no staging storage, but it is not the
fastest one, so there is a row ceiling (``DEFAULT_MAX_ROWS``) beyond which the
table is left for a pipeline Copy activity.

Every parameter is wrapped in ``CAST(? AS <target type>)``. The driver infers a
type from the first row it sees, and a NULL in a binary or date column then
fails with an implicit-conversion error; casting to the column's own type
removes the guesswork. The target type is the same one ``warehouse_ddl`` used
to create the column, so the two cannot disagree.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

from discovery_agent.migration import warehouse_ddl
from discovery_agent.sql.models import SqlColumn, SqlTable

#: Rows pulled from the source per round trip.
READ_CHUNK = 5000
#: Rows per INSERT. SQL Server caps a statement at 1000 row constructors and 2100 parameters.
MAX_ROWS_PER_INSERT = 1000
MAX_PARAMETERS = 2000
#: Above this many rows a direct copy is the wrong tool; a pipeline Copy activity is.
DEFAULT_MAX_ROWS = 1_000_000
#: A Warehouse table takes at most 1024 columns, so a wider source cannot be loaded.
MAX_COLUMNS = 1024


def columns_of(table: SqlTable) -> List[SqlColumn]:
    return sorted(table.columns, key=lambda c: c.column_id)


def batch_rows(column_count: int) -> int:
    """Rows per INSERT for a table this wide: as many as the parameter limit allows."""
    return max(1, min(MAX_ROWS_PER_INSERT, MAX_PARAMETERS // max(1, column_count)))


def source_expression(column: SqlColumn) -> str:
    """How to read a column so the driver can carry it."""
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


def insert_sql(table: SqlTable, rows: int) -> str:
    cols = columns_of(table)
    names = ", ".join(warehouse_ddl.quote(c.name) for c in cols)
    cells = ", ".join(f"CAST(? AS {warehouse_ddl.map_type(c)[0]})" for c in cols)
    values = ", ".join(f"({cells})" for _ in range(rows))
    return f"INSERT INTO {warehouse_ddl.qualified(table.key.schema, table.key.name)} ({names}) VALUES {values}"


def to_parameter(value: Any) -> Any:
    """A driver value in the form the Warehouse driver binds reliably."""
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, float) and value != value:  # NaN has no SQL representation
        return None
    if isinstance(value, Decimal) and not value.is_finite():
        return None
    return value


def flatten(rows: Iterable[Sequence[Any]]) -> List[Any]:
    return [to_parameter(v) for row in rows for v in row]


def copy_rows(
    source_cursor: Any,
    target_cursor: Any,
    table: SqlTable,
    on_progress: Optional[Callable[[int], None]] = None,
    read_chunk: int = READ_CHUNK,
) -> int:
    """Stream the table across. Returns the rows written."""
    per_insert = batch_rows(len(table.columns))
    statements = {}
    written = 0
    source_cursor.execute(select_sql(table))
    while True:
        chunk = source_cursor.fetchmany(read_chunk)
        if not chunk:
            break
        for start in range(0, len(chunk), per_insert):
            batch = chunk[start:start + per_insert]
            sql = statements.get(len(batch))
            if sql is None:
                sql = statements[len(batch)] = insert_sql(table, len(batch))
            target_cursor.execute(sql, *flatten(batch))
            written += len(batch)
        if on_progress is not None:
            on_progress(written)
    return written


def describe_count(n: int) -> str:
    return f"{n:,} row{'' if n == 1 else 's'}"


def preflight(table: SqlTable) -> Optional[Tuple[str, str]]:
    """Why a table's data cannot be copied by this path, as (code, message), or None."""
    if table.is_external:
        return ("EXTERNAL", "An external table stores no rows in the pool; it becomes a OneLake shortcut instead.")
    if not table.columns:
        return ("NO_COLUMNS", "No columns were discovered, so its rows cannot be mapped.")
    if len(table.columns) > MAX_COLUMNS:
        return ("TOO_WIDE", f"It has {len(table.columns)} columns; a Warehouse table takes at most {MAX_COLUMNS}.")
    return None
