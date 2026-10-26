"""Data reconciliation beyond row counts: a profile of each table's values, read from both sides and compared.

Equal row counts do not prove equal data: a load can drop one row and double another, truncate a decimal,
or lose a time. So for a table whose counts agree, one aggregate query per side reads a profile:

* **NULLs** in every nullable column;
* the exact **sum** of every numeric column (``decimal(38, s)``, so integers and money add up exactly;
  ``float`` and ``real`` are summed as float and compared with a rounding allowance);
* the **earliest and latest** value of every date and date/time column, to the second;
* the **total length** of every text column (characters, so ``nvarchar`` stored as UTF-8 ``varchar`` still agrees);
* **duplicate keys**: key values held by more than one row, for a table with a primary key or unique
  constraint. Fabric does not enforce keys, so a load retried twice shows up here and nowhere else.

The profile reads only types whose values the migration keeps: ``datetimeoffset`` (its offset is dropped),
``time``, binary, spatial and ``text`` / ``ntext`` columns are profiled for NULLs only. Read-only on both
sides: every statement is a single SELECT.

A **tolerance** (a percentage of the larger side) lets sums, lengths and NULL counts differ by an approved
amount; earliest/latest values and duplicate keys are always exact. With ``differences="review"`` a
difference is left for a person to judge (REVIEW) rather than called a mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.migration import datacopy, datafilter, warehouse_ddl
from discovery_agent.sql.models import SqlTable

#: Columns profiled per table; a wider table is profiled on its first columns, and says so.
MAX_COLUMNS = 100
#: A float sum's allowance for the order rows are added in, relative to the larger side.
FLOAT_RELATIVE = Decimal("1e-9")
MAX_TOLERANCE_PCT = 10.0

_EXACT = {"bit": 0, "tinyint": 0, "smallint": 0, "int": 0, "bigint": 0, "money": 4, "smallmoney": 4}
_FLOAT = {"float", "real"}
_DATES = {"date", "datetime", "datetime2", "smalldatetime"}
_TEXT = {"char", "varchar", "nchar", "nvarchar", "sysname"}


@dataclass(frozen=True)
class ReconcileOptions:
    """Whether to profile tables whose row counts agree, and how strictly to compare the profiles."""

    profile: bool = False
    #: Also compare each view's rows and value profile on both sides (``equivalence``).
    views: bool = False
    #: Also compare each stored procedure's first result set (column names and types) on both sides.
    procedures: bool = False
    tolerance_pct: float = 0.0
    #: "mismatch": a difference outside the tolerance is a MISMATCH; "review": a person judges it (REVIEW).
    differences: str = "mismatch"

    @classmethod
    def parse(cls, raw: Any) -> "ReconcileOptions":
        if not isinstance(raw, Mapping):
            return cls()
        try:
            pct = float(raw.get("tolerancePct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        pct = pct if pct == pct else 0.0  # NaN
        differences = "review" if raw.get("differences") == "review" else "mismatch"
        return cls(profile=raw.get("profile") is True, views=raw.get("views") is True, procedures=raw.get("procedures") is True,
                   tolerance_pct=max(0.0, min(pct, MAX_TOLERANCE_PCT)), differences=differences)

    def describe(self) -> str:
        if self.differences == "review":
            return "differences left for a person to judge"
        return "exact" if not self.tolerance_pct else f"within {self.tolerance_pct:g}%"


@dataclass(frozen=True)
class Measure:
    """One value of the profile: what it is (``label``), how to read it, and how to compare it."""

    label: str
    expression: str
    #: "count" | "sum" | "float" | "text" | "first" | "last"
    kind: str


def measures(table: SqlTable) -> Tuple[List[Measure], int]:
    """The profile's measures for a table, and how many columns were left out for width."""
    columns = datacopy.columns_of(table)
    out: List[Measure] = []
    for column in columns[:MAX_COLUMNS]:
        q = warehouse_ddl.quote(column.name)
        kind = (column.data_type or "").strip().lower()
        if column.is_nullable is not False:
            out.append(Measure(f"NULLs in {column.name}", f"SUM(CAST(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END AS bigint))", "count"))
        if kind in _EXACT or kind in ("decimal", "numeric"):
            scale = _EXACT.get(kind, column.scale or 0)
            value = f"CAST({q} AS int)" if kind == "bit" else q
            out.append(Measure(f"sum of {column.name}", f"SUM(CAST({value} AS decimal(38,{scale})))", "sum"))
        elif kind in _FLOAT:
            out.append(Measure(f"sum of {column.name}", f"SUM(CAST({q} AS float))", "float"))
        elif kind in _DATES:
            out.append(Measure(f"earliest {column.name}", f"CONVERT(varchar(19), MIN({q}), 120)", "first"))
            out.append(Measure(f"latest {column.name}", f"CONVERT(varchar(19), MAX({q}), 120)", "last"))
        elif kind in _TEXT:
            out.append(Measure(f"length of {column.name}", f"SUM(CAST(LEN({q}) AS bigint))", "text"))
    return out, max(0, len(columns) - MAX_COLUMNS)


def profile_sql(schema: str, name: str, items: Sequence[Measure], predicates: Sequence[str] = ()) -> str:
    """One SELECT that reads every measure of a table (on the source, within the date filter's scope)."""
    select = ", ".join(f"{m.expression} AS m{i}" for i, m in enumerate(items))
    return f"SELECT {select} FROM {warehouse_ddl.qualified(schema, name)}" + _where(predicates)


def duplicates_sql(schema: str, name: str, keys: Sequence[str], predicates: Sequence[str] = ()) -> str:
    """How many key values more than one row holds."""
    cols = ", ".join(warehouse_ddl.quote(k) for k in keys)
    return (f"SELECT COUNT_BIG(*) FROM (SELECT {cols} FROM {warehouse_ddl.qualified(schema, name)}{_where(predicates)} "
            f"GROUP BY {cols} HAVING COUNT_BIG(*) > 1) AS d")


def _where(predicates: Sequence[str]) -> str:
    return " WHERE " + " AND ".join(predicates) if predicates else ""


def table_keys(table: SqlTable) -> Tuple[str, ...]:
    return datafilter.table_keys(table)


def _number(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _shown(value: Any) -> str:
    if value is None:
        return "none"
    n = _number(value)
    if n is None:
        return str(value)
    if n == n.to_integral_value():
        return f"{int(n):,}"
    return f"{n:,.6f}".rstrip("0").rstrip(".")


def _agrees(m: Measure, src: Any, tgt: Any, options: ReconcileOptions) -> Tuple[bool, bool]:
    """(agrees exactly or within the allowance for float rounding, agrees only within the approved tolerance)."""
    if m.kind in ("first", "last"):
        return (str(src) if src is not None else None) == (str(tgt) if tgt is not None else None), False
    a, b = _number(src) or Decimal(0), _number(tgt) or Decimal(0)
    if a == b:
        return True, False
    scale = max(abs(a), abs(b))
    if m.kind == "float" and abs(a - b) <= scale * FLOAT_RELATIVE:
        return True, False
    if options.tolerance_pct and abs(a - b) <= scale * Decimal(str(options.tolerance_pct)) / 100:
        return False, True
    return False, False


@dataclass(frozen=True)
class Comparison:
    status: str  # "MATCH" | "REVIEW" | "MISMATCH"
    summary: str  # e.g. "14 measures"
    detail: str


def compare(items: Sequence[Measure], src: Sequence[Any], tgt: Sequence[Any], options: ReconcileOptions,
            duplicates: Optional[Tuple[int, int]] = None, keys: Sequence[str] = (), left_out: int = 0) -> Comparison:
    """The verdict on two profiles of the same measures, and what differs, in words."""
    differ: List[str] = []
    tolerated: List[str] = []
    for i, m in enumerate(items):
        exact, within = _agrees(m, src[i] if i < len(src) else None, tgt[i] if i < len(tgt) else None, options)
        if exact:
            continue
        text = f"{m.label}: {_shown(src[i] if i < len(src) else None)} in Synapse, {_shown(tgt[i] if i < len(tgt) else None)} in Fabric"
        (tolerated if within else differ).append(text)
    dup_note = ""
    if duplicates is not None:
        s_dup, t_dup = duplicates
        if t_dup > s_dup:
            differ.insert(0, f"duplicate keys ({', '.join(keys)}): {t_dup:,} key value(s) held by more than one row in Fabric, "
                             f"{s_dup:,} in Synapse; a load ran twice or rows were appended")
        elif s_dup:
            dup_note = f" Synapse already holds {s_dup:,} duplicate key value(s) ({', '.join(keys)}); Fabric has the same."
    count = len(items) + (1 if duplicates is not None else 0)
    summary = f"{count} measure{'s' if count != 1 else ''}"
    wide = f" The first {MAX_COLUMNS} columns were profiled; {left_out} more were not." if left_out else ""
    if differ:
        shown = "; ".join(differ[:4]) + (f"; and {len(differ) - 4} more" if len(differ) > 4 else "")
        status = "REVIEW" if options.differences == "review" else "MISMATCH"
        lead = "Differences for a person to judge: " if status == "REVIEW" else "The values differ: "
        return Comparison(status, summary, lead + shown + "." + wide)
    if tolerated:
        return Comparison("MATCH", summary, f"Values agree within the approved {options.tolerance_pct:g}% tolerance "
                                            f"({len(tolerated)} differ inside it, e.g. {tolerated[0]})." + dup_note + wide)
    if not items and duplicates is None:
        return Comparison("REVIEW", "0 measures", "Nothing to profile: no column of a type the profile reads, and no key.")
    return Comparison("MATCH", summary, "NULLs, sums, earliest and latest dates, text lengths"
                                        f"{' and duplicate keys' if duplicates is not None else ''} agree." + dup_note + wide)


def read(cursor: Any, sql: str) -> List[Any]:
    cursor.execute(sql)
    row = cursor.fetchone()
    return list(row) if row is not None else []


def run_profile(source_cursor: Any, target_cursor: Any, table: SqlTable, target: Tuple[str, str],
                options: ReconcileOptions, predicates: Sequence[str] = ()) -> Comparison:
    """Profile one table on both sides and compare. ``target`` is its (schema, name) in Fabric; ``predicates``
    the date filter's scope on the Synapse side (the Fabric table holds only those rows)."""
    items, left_out = measures(table)
    src_schema, src_name = table.key.schema, table.key.name
    src = read(source_cursor, profile_sql(src_schema, src_name, items, predicates)) if items else []
    tgt = read(target_cursor, profile_sql(target[0], target[1], items)) if items else []
    keys = table_keys(table)
    duplicates = None
    if keys:
        s_dup = read(source_cursor, duplicates_sql(src_schema, src_name, keys, predicates))
        t_dup = read(target_cursor, duplicates_sql(target[0], target[1], keys))
        duplicates = (int(s_dup[0] or 0) if s_dup else 0, int(t_dup[0] or 0) if t_dup else 0)
    return compare(items, src, tgt, options, duplicates, keys, left_out)
