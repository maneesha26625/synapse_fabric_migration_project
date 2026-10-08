"""Date filters for table data: load only part of a table's rows, then keep it in sync.

A table without a filter is copied whole, as before. A filter says, per table:

* **which rows belong** (the *scope*): a date column and a fixed date range, for
  example ``order_date >= 2024-10-08``. The dates are fixed when the plan is
  made, never "two years before today", so every run of the same plan copies the
  same rows and validation compares like with like.
* **whether to keep loading** (*sync*): a *change column* whose value moves when a
  row is added or changed (``modified_at``), and the *key columns* that identify a
  row. After the first load, a sync pipeline copies only rows whose change column
  is past the table's *watermark*, the newest change already loaded, and applies
  them to the Warehouse table: rows with the same key are replaced, new ones added.

The watermark and the SQL each sync runs live in a control table in the
Warehouse (``migration_sync.sync_tables``), one row per synced table, so the sync
pipeline itself is the same for every table and every run. Registering a table
is adding its row; nothing in the pipeline changes.

What this cannot do, and says so: a row deleted in Synapse leaves no change to
copy, so hard deletes are not carried over (a soft delete that sets a
``deleted_at`` or flag column *and* moves the change column is). Without key
columns a changed row cannot be matched, so changes arrive as extra rows.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

from discovery_agent.migration import datacopy
from discovery_agent.migration.warehouse_ddl import qualified, quote
from discovery_agent.sql.models import SqlColumn, SqlTable

#: Column types a filter can use.
DATE_TYPES = frozenset({"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"})
#: Where the sync machinery lives in the Warehouse.
SYNC_SCHEMA = "migration_sync"
CONTROL_TABLE = "sync_tables"
CONTROL = qualified(SYNC_SCHEMA, CONTROL_TABLE)
#: Re-read window choices for synced tables with key columns, in minutes.
OVERLAPS = {"none": 0, "1h": 60, "1d": 24 * 60}
#: Written as the watermark when the scope holds no rows yet, so the first sync copies everything after it.
EMPTY_WATERMARK = "1900-01-01T00:00:00"
MAX_FILTERS = 10000
_NAME_MAX = 128
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: What ``CONVERT(varchar, <date type>, 126)`` returns: an ISO date or timestamp, perhaps with an offset.
_WATERMARK = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d{1,7})?)?([+-]\d{2}:\d{2}|Z)?$")


class FilterError(ValueError):
    """A filter that is malformed, or does not fit its table."""


@dataclass(frozen=True)
class DataFilter:
    """One table's filter, as the operator set it. Column names are checked later, against the table."""

    column: str = ""
    start: str = ""  # YYYY-MM-DD, inclusive
    end: str = ""  # YYYY-MM-DD, exclusive
    sync: bool = False
    change_column: str = ""
    keys: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """The request shape this was parsed from."""
        return {"column": self.column, "from": self.start, "to": self.end, "sync": self.sync,
                "changeColumn": self.change_column, "keys": list(self.keys)}

    def describe(self) -> str:
        parts = []
        if self.column:
            bounds = " and ".join(b for b in (f"on or after {self.start}" if self.start else "",
                                              f"before {self.end}" if self.end else "") if b)
            parts.append(f"rows with {self.column} {bounds}")
        if self.sync:
            parts.append(f"kept in sync by {self.change_column or self.column}")
        return "; ".join(parts) or "every row"


def table_key(schema: str, name: str) -> str:
    """How a filter is addressed: ``schema.table``, lowercase."""
    return f"{schema}.{name}".lower()


def _text(value: Any, what: str, limit: int = _NAME_MAX) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise FilterError(f"The {what} is not valid.")
    return value.strip()


def _date(value: Any, what: str) -> str:
    text = _text(value, what, 10)
    if not text:
        return ""
    try:
        if not _DATE.match(text):
            raise ValueError
        date.fromisoformat(text)
    except ValueError:
        raise FilterError(f"The {what} must be a date written as YYYY-MM-DD.") from None
    return text


def parse_one(name: str, raw: Any) -> DataFilter:
    """One filter from a request. Raises FilterError naming the table."""
    if not isinstance(raw, Mapping):
        raise FilterError(f"The date filter for {name} is not in a valid format.")
    try:
        keys_raw = raw.get("keys") or []
        if not isinstance(keys_raw, list) or len(keys_raw) > 32:
            raise FilterError("The key columns are not valid.")
        f = DataFilter(
            column=_text(raw.get("column"), "date column"), start=_date(raw.get("from"), "start date"),
            end=_date(raw.get("to"), "end date"), sync=bool(raw.get("sync")),
            change_column=_text(raw.get("changeColumn"), "change column"),
            keys=tuple(k for k in (_text(k, "key column") for k in keys_raw) if k),
        )
    except FilterError as exc:
        raise FilterError(f"{name}: {exc}") from None
    if f.column and not (f.start or f.end):
        raise FilterError(f"{name}: give a start date, an end date or both for {f.column}, or clear the date column.")
    if (f.start or f.end) and not f.column:
        raise FilterError(f"{name}: choose the date column the dates apply to.")
    if f.start and f.end and f.start >= f.end:
        raise FilterError(f"{name}: the start date must be before the end date.")
    if f.sync and f.end:
        raise FilterError(f"{name}: a table kept in sync keeps receiving new rows, so it cannot have an end date.")
    if f.sync and not (f.change_column or f.column):
        raise FilterError(f"{name}: choose the change column (for example modified_at) that moves when a row is added or changed.")
    if not f.sync:
        f = DataFilter(f.column, f.start, f.end)  # change column and keys only mean something when syncing
    if not f.column and not f.sync:
        raise FilterError(f"{name}: the filter has nothing to do. Clear it to copy every row.")
    return f


def parse_filters(raw: Any) -> Dict[str, DataFilter]:
    """Every table's filter, by ``schema.table`` (lowercase). Raises FilterError."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping) or len(raw) > MAX_FILTERS:
        raise FilterError("The date filters are not in a valid format.")
    out: Dict[str, DataFilter] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not 2 < len(name) <= 2 * _NAME_MAX + 1 or "." not in name:
            raise FilterError("The date filters are not in a valid format.")
        out[name.strip().lower()] = parse_one(name, value)
    return out


# ---- a filter checked against its table ------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """A filter whose columns exist on the table and have the right types, with their exact names."""

    filter: DataFilter
    column: Optional[SqlColumn]
    change: Optional[SqlColumn]
    keys: Tuple[str, ...]
    #: Whether the key columns were the table's own primary key or unique constraint, not chosen.
    keys_from_table: bool = False

    @property
    def sync(self) -> bool:
        return self.filter.sync


def date_columns(table: SqlTable) -> List[SqlColumn]:
    return [c for c in datacopy.columns_of(table) if (c.data_type or "").lower() in DATE_TYPES]


def table_keys(table: SqlTable) -> Tuple[str, ...]:
    """The table's primary key columns, else its first unique constraint's, else none."""
    ordered = sorted(table.indexes, key=lambda i: (not i.is_primary_key, not i.is_unique_constraint, i.index_id))
    for index in ordered:
        if (index.is_primary_key or index.is_unique_constraint) and index.key_columns:
            names = tuple(c.name for c in index.key_columns if c.name)
            if names:
                return names
    return ()


def _column(table: SqlTable, name: str, what: str, dated: bool) -> SqlColumn:
    match = next((c for c in table.columns if c.name.lower() == name.lower()), None)
    if match is None:
        raise FilterError(f"{table.key.schema}.{table.key.name} has no column {name} (the {what}).")
    if dated and (match.data_type or "").lower() not in DATE_TYPES:
        raise FilterError(f"{match.name} (the {what}) is {match.data_type}, not a date or time column.")
    return match


def resolve(table: SqlTable, f: DataFilter) -> Resolved:
    """Check a filter against the table it is for. Raises FilterError."""
    column = _column(table, f.column, "date column", True) if f.column else None
    change = _column(table, f.change_column or f.column, "change column", True) if f.sync else None
    keys: Tuple[str, ...] = ()
    from_table = False
    if f.sync:
        if f.keys:
            keys = tuple(_column(table, k, "key column", False).name for k in f.keys)
        else:
            keys, from_table = table_keys(table), True
    return Resolved(f, column, change, keys, from_table and bool(keys))


def notes(r: Resolved) -> List[str]:
    """What the operator should know about how this table is loaded."""
    out = [f"Loads {r.filter.describe()}."]
    if r.sync:
        if r.keys:
            source = "the table's key" if r.keys_from_table else "the key columns chosen"
            out.append(f"Changed rows replace the Warehouse row with the same {', '.join(r.keys)} ({source}).")
        else:
            out.append("No key columns: a changed row is added again rather than replacing the old one. "
                       "Choose key columns, or keep this table append-only.")
        out.append("Rows deleted in Synapse are not removed from the Warehouse; soft deletes that move "
                   f"{r.change.name if r.change else 'the change column'} are carried over.")
    return out


# ---- SQL ---------------------------------------------------------------------------------


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _cast_type(column: SqlColumn) -> str:
    """The type a watermark is compared as: offsets are kept for datetimeoffset, time for everything else."""
    return "datetimeoffset(7)" if (column.data_type or "").lower() == "datetimeoffset" else "datetime2(7)"


def _at(column: SqlColumn, value: str) -> str:
    return f"CAST({_literal(value)} AS {_cast_type(column)})"


def scope_predicates(r: Resolved) -> List[str]:
    """The rows that belong, from the date range."""
    out: List[str] = []
    if r.column is not None:
        ref = quote(r.column.name)
        if r.filter.start:
            out.append(f"{ref} >= {_literal(r.filter.start)}")
        if r.filter.end:
            out.append(f"{ref} < {_literal(r.filter.end)}")
    return out


def _where(predicates: List[str]) -> str:
    return " WHERE " + " AND ".join(predicates) if predicates else ""


def first_load_predicates(r: Resolved, watermark: Optional[str]) -> List[str]:
    """The first load's rows: the scope, and for a synced table nothing newer than the watermark.

    The upper bound is what makes the first load and the syncs after it meet exactly: a row
    changed after the watermark was read is left for the first sync instead of being copied twice.
    Rows whose change column is NULL have no change to wait for, so the first load takes them.
    """
    out = scope_predicates(r)
    if r.sync and r.change is not None and watermark and watermark != EMPTY_WATERMARK:
        ref = quote(r.change.name)
        out.append(f"({ref} <= {_at(r.change, watermark)} OR {ref} IS NULL)")
    return out


def select_sql(table: SqlTable, predicates: List[str]) -> str:
    return datacopy.select_sql(table) + _where(predicates)


def count_sql(schema: str, name: str, predicates: List[str]) -> str:
    return datacopy.count_sql(schema, name) + _where(predicates)


def watermark_sql(table: SqlTable, r: Resolved) -> str:
    """The newest change in the scope, as ISO text, or '' when the scope is empty.

    Never NULL: a pipeline Lookup may leave a NULL column out of its output, and the
    sync pipeline's expression would then fail on a missing property instead of
    falling back to the last watermark.
    """
    assert r.change is not None
    return (f"SELECT COALESCE(CONVERT(varchar(40), MAX({quote(r.change.name)}), 126), '') AS wm "
            f"FROM {qualified(table.key.schema, table.key.name)}{_where(scope_predicates(r))}")


def clean_watermark(value: Any) -> str:
    """A watermark read back from Synapse or the Warehouse, checked before it goes into SQL."""
    text = str(value or "").strip()
    if not text:
        return EMPTY_WATERMARK
    if not _WATERMARK.match(text):
        raise FilterError(f"The watermark '{text[:40]}' is not a date or timestamp.")
    return text


def staging_table(schema: str, name: str) -> str:
    """The staging table a sync copies changed rows into: one per synced table, in the sync schema."""
    base = f"{schema}__{name}"
    if len(base) <= _NAME_MAX:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return f"{base[:_NAME_MAX - 13]}_{digest}"


#: Placeholders the sync pipeline replaces with the watermarks of each run.
FROM_MARK, TO_MARK = "{{from}}", "{{to}}"


def sync_copy_sql(table: SqlTable, r: Resolved, overlap_minutes: int) -> str:
    """The rows one sync copies: the scope, changed after the last watermark, up to the newest change.

    With key columns the window starts *at* the last watermark (minus the re-read window), because
    re-reading a row only replaces it with itself. Without keys a re-read row would be added twice,
    so the window starts just after the watermark and nothing is re-read.
    """
    assert r.change is not None
    ref = quote(r.change.name)
    start = _at(r.change, FROM_MARK)
    if r.keys:
        if overlap_minutes:
            start = f"DATEADD(minute, -{int(overlap_minutes)}, {start})"
        lower = f"{ref} >= {start}"
    else:
        lower = f"{ref} > {start}"
    return select_sql(table, scope_predicates(r) + [lower, f"{ref} <= {_at(r.change, TO_MARK)}"])


def apply_sql(table: SqlTable, r: Resolved, target_schema: str, target_name: str) -> str:
    """The Warehouse batch one sync runs after copying: replace matched rows, add the rest, move the watermark."""
    target = qualified(target_schema, target_name)
    stage = qualified(SYNC_SCHEMA, staging_table(target_schema, target_name))
    cols = ", ".join(quote(c.name) for c in datacopy.columns_of(table))
    steps = ["BEGIN TRANSACTION;"]
    if r.keys:
        match = " AND ".join(f"s.{quote(k)} = {target}.{quote(k)}" for k in r.keys)
        steps.append(f"DELETE FROM {target} WHERE EXISTS (SELECT 1 FROM {stage} AS s WHERE {match});")
    steps.append(f"INSERT INTO {target} ({cols}) SELECT {cols} FROM {stage};")
    steps.append(f"UPDATE {CONTROL} SET watermark = {_literal(TO_MARK)}, synced_at = SYSUTCDATETIME(), "
                 f"rows_copied = (SELECT COUNT_BIG(*) FROM {stage}) WHERE table_name = {_literal(table_key(target_schema, target_name))};")
    steps.append("COMMIT TRANSACTION;")
    return " ".join(steps)


# ---- the control table ----------------------------------------------------------------------

#: Run in order, each on its own, to create the sync schema and control table when they are missing.
CONTROL_DDL = (
    f"IF SCHEMA_ID({_literal(SYNC_SCHEMA)}) IS NULL EXEC({_literal('CREATE SCHEMA ' + quote(SYNC_SCHEMA))})",
    f"IF OBJECT_ID({_literal(CONTROL)}) IS NULL CREATE TABLE {CONTROL} ("
    "table_name varchar(300) NOT NULL, target_schema varchar(128) NOT NULL, target_table varchar(128) NOT NULL, "
    "staging_table varchar(128) NOT NULL, change_column varchar(128) NOT NULL, watermark varchar(40) NOT NULL, "
    "watermark_query varchar(MAX) NOT NULL, copy_query varchar(MAX) NOT NULL, apply_script varchar(MAX) NOT NULL, "
    "filter varchar(4000) NOT NULL, registered_at datetime2(6) NOT NULL, synced_at datetime2(6) NULL, rows_copied bigint NULL)",
)
REGISTERED_SQL = f"SELECT watermark FROM {CONTROL} WHERE table_name = ?"
UNREGISTER_SQL = f"DELETE FROM {CONTROL} WHERE table_name = ?"
REGISTER_SQL = (f"INSERT INTO {CONTROL} (table_name, target_schema, target_table, staging_table, change_column, watermark, "
                "watermark_query, copy_query, apply_script, filter, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME())")


def staging_ddl(target_schema: str, target_name: str) -> str:
    """An empty copy of the Warehouse table, with its exact column types, to stage changed rows in."""
    stage = qualified(SYNC_SCHEMA, staging_table(target_schema, target_name))
    return f"CREATE TABLE {stage} AS SELECT * FROM {qualified(target_schema, target_name)} WHERE 1 = 0"


def registration(table: SqlTable, r: Resolved, target_schema: str, target_name: str, watermark: str,
                 overlap_minutes: int) -> Tuple[Any, ...]:
    """The REGISTER_SQL parameters for one synced table."""
    assert r.change is not None
    return (table_key(target_schema, target_name), target_schema, target_name, staging_table(target_schema, target_name),
            r.change.name, watermark, watermark_sql(table, r), sync_copy_sql(table, r, overlap_minutes),
            apply_sql(table, r, target_schema, target_name), r.filter.describe()[:4000])
