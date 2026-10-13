"""Dedicated SQL pool schema -> Fabric Warehouse T-SQL.

Tables are rebuilt from the discovered column metadata rather than from text,
because a dedicated pool has no ``CREATE TABLE`` text to copy and its storage
clauses (DISTRIBUTION, CLUSTERED COLUMNSTORE INDEX, PARTITION) have no meaning
in a Fabric Warehouse, which manages storage itself.

Fabric Warehouse supports a smaller set of types. Each unsupported type is
mapped to its closest supported one and the change is reported, so nobody is
surprised by a ``datetime`` that came back as ``datetime2(6)``.

IDENTITY columns stay IDENTITY. Fabric supports it on ``bigint`` only, with no
seed or increment, so an ``int`` identity becomes ``bigint IDENTITY``. Existing
key values are kept: the data stage loads them through a staging table with
``SET IDENTITY_INSERT`` and then reseeds (``DBCC CHECKIDENT ... RESEED``), the
migration path Microsoft documents for Fabric Data Warehouse.

Views and procedures are created from their own definition text, unchanged:
that text is the most faithful thing there is, and where Fabric's T-SQL
differs the statement fails with the server's own message, which is reported.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from discovery_agent.sql.models import SqlColumn, SqlTable

#: Types Fabric Warehouse accepts as they are (precision handled below).
_SAME = {"bit", "smallint", "int", "bigint", "float", "real", "date", "uniqueidentifier"}
#: Source types an IDENTITY column can have; in Fabric every one becomes bigint IDENTITY.
_IDENTITY_TYPES = {"tinyint", "smallint", "int", "bigint", "decimal", "numeric"}
MAX_TIME_PRECISION = 6


class UnsupportedColumn(Exception):
    """A column whose type has no Fabric Warehouse equivalent."""


def quote(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def qualified(schema: str, name: str) -> str:
    return f"{quote(schema)}.{quote(name)}"


def _length(max_length: Optional[int], per_char: int = 1) -> Optional[int]:
    """Character count from sys.columns.max_length, or None for MAX (-1)."""
    if max_length is None or max_length < 0:
        return None
    return max(1, max_length // per_char)


def _sized(type_name: str, length: Optional[int], limit: int = 8000) -> str:
    if length is None or length > limit:
        return f"{type_name}(MAX)"
    return f"{type_name}({length})"


def map_type(column: SqlColumn) -> Tuple[str, Optional[str]]:
    """The Fabric Warehouse type for one column, and a note when it changed."""
    source = (column.data_type or "").strip().lower()
    name = column.name
    precision, scale = column.precision, column.scale

    if column.is_identity and source in _IDENTITY_TYPES:
        if source == "bigint":
            return "bigint", None
        return "bigint", f"{name}: {source} IDENTITY stored as bigint IDENTITY (Fabric supports IDENTITY on bigint only)"
    if source in _SAME:
        return source, None
    if source in ("decimal", "numeric"):
        return f"decimal({precision or 18},{scale or 0})", None
    if source == "tinyint":
        return "smallint", f"{name}: tinyint stored as smallint"
    if source == "money":
        return "decimal(19,4)", f"{name}: money stored as decimal(19,4)"
    if source == "smallmoney":
        return "decimal(10,4)", f"{name}: smallmoney stored as decimal(10,4)"
    if source in ("datetime2", "time"):
        digits = scale if scale is not None else MAX_TIME_PRECISION
        if digits > MAX_TIME_PRECISION:
            return f"{source}({MAX_TIME_PRECISION})", f"{name}: {source}({digits}) stored with 6 fractional digits"
        return f"{source}({digits})", None
    if source == "datetime":
        return "datetime2(3)", f"{name}: datetime stored as datetime2(3)"
    if source == "smalldatetime":
        return "datetime2(0)", f"{name}: smalldatetime stored as datetime2(0)"
    if source == "datetimeoffset":
        return f"datetime2({min(scale if scale is not None else 6, MAX_TIME_PRECISION)})", f"{name}: datetimeoffset stored as datetime2; the time-zone offset is not kept"
    if source == "char":
        return _sized("char", _length(column.max_length)), None
    if source == "varchar":
        return _sized("varchar", _length(column.max_length)), None
    if source in ("nchar", "nvarchar", "sysname"):
        chars = _length(column.max_length, per_char=2)
        # Fabric stores text as UTF-8 varchar; a character can take up to 4 bytes.
        size = None if chars is None else chars * 4
        return _sized("varchar", size), f"{name}: {source} stored as UTF-8 varchar"
    if source in ("text", "ntext", "xml"):
        return "varchar(MAX)", f"{name}: {source} stored as varchar(MAX)"
    if source in ("binary", "varbinary"):
        return _sized("varbinary", _length(column.max_length)), (f"{name}: binary stored as varbinary" if source == "binary" else None)
    if source == "image":
        return "varbinary(MAX)", f"{name}: image stored as varbinary(MAX)"
    if source in ("timestamp", "rowversion"):
        return "varbinary(8)", f"{name}: rowversion stored as varbinary(8); it no longer changes on update"
    if source in ("geography", "geometry"):
        return "varbinary(MAX)", f"{name}: {source} stored as varbinary(MAX) (well-known binary)"
    raise UnsupportedColumn(f"Column {name} has type {source or 'unknown'}, which Fabric Warehouse cannot store.")


def identity_columns(table: SqlTable) -> List[SqlColumn]:
    """The table's IDENTITY columns that stay IDENTITY in Fabric (in practice, at most one)."""
    return [c for c in sorted(table.columns, key=lambda c: c.column_id)
            if c.is_identity and (c.data_type or "").strip().lower() in _IDENTITY_TYPES]


def create_table(table: SqlTable, masks: Iterable[Tuple[str, str]] = (), *, identity: bool = True,
                 schema: Optional[str] = None, name: Optional[str] = None) -> Tuple[str, List[str]]:
    """``CREATE TABLE`` for a Fabric Warehouse, and notes on what changed.

    ``masks`` are ``(column, MASKED WITH (...) clause)`` pairs: a column's dynamic data mask is part of
    its definition, so the table never exists, even empty, without it. ``identity=False`` with another
    ``schema``/``name`` makes the staging copy an IDENTITY table is loaded through: same columns and
    masks, no IDENTITY, so the original key values can be copied into it as they are."""
    if not table.columns:
        raise UnsupportedColumn("No columns were discovered for this table, so it cannot be recreated.")
    masked = {str(c).lower(): clause for c, clause in masks}
    keep = {c.column_id for c in identity_columns(table)} if identity else set()
    notes: List[str] = []
    lines: List[str] = []
    for column in sorted(table.columns, key=lambda c: c.column_id):
        sql_type, note = map_type(column)
        if note:
            notes.append(note)
        marker = ""
        if column.column_id in keep:
            marker = " IDENTITY"
            notes.append(f"{column.name}: IDENTITY kept; Fabric chooses the seed and increment, existing values are loaded "
                         "with IDENTITY_INSERT and the identity is reseeded")
        elif column.is_identity and identity:
            notes.append(f"{column.name}: IDENTITY dropped ({column.data_type} cannot be IDENTITY in Fabric); values are loaded as they are")
        nullability = "NULL" if column.is_nullable is not False and column.column_id not in keep else "NOT NULL"
        mask = masked.get(column.name.lower())
        if mask:
            notes.append(f"{column.name}: dynamic data mask kept ({mask})")
        lines.append(f"    {quote(column.name)} {sql_type}{marker} {mask + ' ' if mask else ''}{nullability}")
    if table.distribution.value not in ("unknown", "not_applicable"):
        notes.append(f"Distribution ({table.distribution.value}) and index settings dropped: Fabric manages storage itself.")
    target = qualified(schema or table.key.schema, name or table.key.name)
    sql = f"CREATE TABLE {target} (\n" + ",\n".join(lines) + "\n);"
    return sql, notes


def key_constraints(table: SqlTable) -> List[str]:
    """``ALTER TABLE ... ADD CONSTRAINT ... NOT ENFORCED`` for each primary key and unique constraint.

    Synapse already holds these as NOT ENFORCED; a Warehouse accepts them in the
    same form (NONCLUSTERED, NOT ENFORCED). They are not checked, but the query
    optimiser and tools such as Power BI use them, so they are worth keeping."""
    statements: List[str] = []
    for index in table.indexes:
        if not (index.is_primary_key or index.is_unique_constraint):
            continue
        columns = [c.name for c in index.key_columns if c.name]
        if not columns:
            continue
        kind = "PRIMARY KEY" if index.is_primary_key else "UNIQUE"
        name = index.name or f"{'PK' if index.is_primary_key else 'UQ'}_{table.key.name}_{index.index_id}"
        statements.append(
            f"ALTER TABLE {qualified(table.key.schema, table.key.name)} ADD CONSTRAINT {quote(name)} "
            f"{kind} NONCLUSTERED ({', '.join(quote(c) for c in columns)}) NOT ENFORCED;")
    return statements


def ensure_schema(schema: str) -> Optional[str]:
    """A statement creating ``schema`` when it is missing, or None for dbo."""
    if schema.lower() == "dbo":
        return None
    literal = schema.replace("'", "''")
    inner = f"CREATE SCHEMA {quote(schema)}".replace("'", "''")
    return f"IF SCHEMA_ID(N'{literal}') IS NULL EXEC(N'{inner}');"


EXISTS_QUERY = "SELECT OBJECT_ID(?)"
