"""Beyond "it compiles": do migrated views return the same data, procedures the same shape, and queries run fast enough?

* **Views** (``view_profile``): the value profile of ``reconcile`` read from the view on both sides, plus its row
  count, built from the columns Fabric reports for the view. Two views that agree on rows, NULLs, sums,
  date ranges and text lengths very probably return the same result; one that differs certainly does not.
* **Procedures** (``procedure_shape``): the first result set's columns, from ``sp_describe_first_result_set``
  on both sides. Metadata only: neither side runs the procedure. Type names are compared after the
  conversions the migration makes by design (``nvarchar`` is ``varchar`` in Fabric, and so on).
* **Performance** (``replay``): the most demanding SELECT statements from Synapse's request history, run on
  Fabric, timed, and compared with how long they took on Synapse. Only plain reads are replayed (no
  ``SELECT INTO``, temporary tables or variables), so nothing changes in Fabric. The Synapse time is what the
  pool recorded server-side; the Fabric time is measured here, including bringing the rows back, so a
  query returning many rows reads slower than it is. Each query runs twice and the second (warm) run counts.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.migration import reconcile, tsql_rules, warehouse_ddl
from discovery_agent.sql.models import SqlColumn, SqlObjectKey, SqlTable

MATCH, REVIEW, MISMATCH = "MATCH", "REVIEW", "MISMATCH"

#: Fabric spells these Synapse types differently by design; a result set that changed only this way agrees.
_SAME_TYPE = {"nvarchar": "varchar", "nchar": "char", "ntext": "varchar", "text": "varchar", "sysname": "varchar",
              "datetime": "datetime2", "smalldatetime": "datetime2", "datetimeoffset": "datetime2", "money": "decimal",
              "smallmoney": "decimal", "numeric": "decimal", "tinyint": "smallint", "image": "varbinary", "binary": "varbinary",
              "timestamp": "varbinary", "rowversion": "varbinary"}
#: Rows drained from a replayed query before it is stopped; the time then covers this many rows.
MAX_ROWS = 1_000_000
FETCH = 10_000


@dataclass(frozen=True)
class PerformanceOptions:
    enabled: bool = False
    #: Slower than Synapse by more than this percentage is a regression.
    threshold_pct: float = 50.0
    #: How many of the history's queries to replay.
    limit: int = 10
    #: A regression is a MISMATCH (an SLA was agreed) rather than a REVIEW.
    strict: bool = False

    @classmethod
    def parse(cls, raw: Any) -> "PerformanceOptions":
        if not isinstance(raw, Mapping):
            return cls()
        try:
            pct = float(raw.get("thresholdPct") if raw.get("thresholdPct") is not None else 50)
            limit = int(raw.get("limit") or 10)
        except (TypeError, ValueError):
            pct, limit = 50.0, 10
        return cls(enabled=raw.get("enabled") is True, threshold_pct=max(0.0, min(pct, 1000.0)), limit=max(1, min(limit, 50)),
                   strict=raw.get("strict") is True)


# ---- views -------------------------------------------------------------------------------------


def _base(type_name: str) -> str:
    return re.sub(r"\(.*\)$", "", (type_name or "").strip().lower())


def columns_from_catalog(rows: Sequence[Tuple]) -> Tuple[SqlColumn, ...]:
    """Fabric's ``COLUMNS_SQL`` rows for one object -> SqlColumns the profile can read."""
    return tuple(SqlColumn(int(r[2]), str(r[3]), str(r[4]), r[5], r[6], r[7], bool(r[8])) for r in sorted(rows, key=lambda r: r[2]))


def view_profile(source_cursor: Any, target_cursor: Any, schema: str, name: str, columns: Sequence[SqlColumn],
                 options: reconcile.ReconcileOptions) -> reconcile.Comparison:
    """The view's row count and value profile, on both sides, compared like a table's."""
    table = SqlTable(key=SqlObjectKey("view", schema, name), columns=tuple(columns))
    items, left_out = reconcile.measures(table)
    items = [reconcile.Measure("rows", "COUNT_BIG(*)", "count")] + items
    src = reconcile.read(source_cursor, reconcile.profile_sql(schema, name, items))
    tgt = reconcile.read(target_cursor, reconcile.profile_sql(schema, name, items))
    return reconcile.compare(items, src, tgt, options, left_out=left_out)


# ---- procedures --------------------------------------------------------------------------------


def describe_sql(exec_text: str) -> str:
    return f"EXEC sys.sp_describe_first_result_set @tsql = N'{exec_text.replace(chr(39), chr(39) * 2)}'"


def first_result_set(cursor: Any, exec_text: str) -> List[Tuple[str, str]]:
    """(column name, base type) of a procedure's first result set, in order. Hidden columns are left out."""
    cursor.execute(describe_sql(exec_text))
    names = [d[0] for d in (cursor.description or ())]
    out = []
    for r in cursor.fetchall():
        row = dict(zip(names, tuple(r))) if names else {}
        if row.get("is_hidden"):
            continue
        out.append((str(row.get("name") or ""), _base(str(row.get("system_type_name") or ""))))
    return out


def procedure_shape(source_cursor: Any, target_cursor: Any, exec_text: str) -> Tuple[str, str, str, str]:
    """(status, synapse, fabric, detail) for a procedure's first result set on both sides."""
    src = first_result_set(source_cursor, exec_text)
    tgt = first_result_set(target_cursor, exec_text)
    shown_s = f"{len(src)} column(s)" if src else "no result set"
    shown_t = f"{len(tgt)} column(s)" if tgt else "no result set"
    if [n.lower() for n, _ in src] != [n.lower() for n, _ in tgt]:
        return MISMATCH, shown_s, shown_t, (f"The first result set differs: Synapse returns {', '.join(n for n, _ in src[:6]) or 'nothing'}; "
                                           f"Fabric returns {', '.join(n for n, _ in tgt[:6]) or 'nothing'}.")
    differ = [f"{n} ({a} -> {b})" for (n, a), (_, b) in zip(src, tgt) if _SAME_TYPE.get(a, a) != _SAME_TYPE.get(b, b)]
    if differ:
        return REVIEW, shown_s, shown_t, f"Same columns; type(s) differ: {', '.join(differ[:4])}."
    return MATCH, shown_s, shown_t, ("Same columns and types in the first result set (described, not run)." if src
                                     else "Neither side returns a result set.")


# ---- performance -------------------------------------------------------------------------------


def label_of(sample: Any) -> str:
    text = " ".join(str(sample.text).split())
    return sample.label or (text[:70] + ("…" if len(text) > 70 else ""))


def timed(cursor: Any, sql: str, clock: Callable[[], float] = time.perf_counter) -> Tuple[float, int]:
    """Run one read and drain its rows (up to ``MAX_ROWS``): (milliseconds, rows)."""
    start = clock()
    cursor.execute(sql)
    rows = 0
    fetch = getattr(cursor, "fetchmany", None)
    while rows < MAX_ROWS:
        chunk = fetch(FETCH) if callable(fetch) else cursor.fetchall()
        if not chunk:
            break
        rows += len(chunk)
        if not callable(fetch):
            break
    return (clock() - start) * 1000, rows


def replay(cursor: Any, sample: Any, options: PerformanceOptions, pool: str = "", warehouse: str = "",
           clock: Callable[[], float] = time.perf_counter) -> Dict[str, str]:
    """One history query on Fabric against its Synapse time: a validation row's fields."""
    label = label_of(sample)
    synapse = f"{sample.avg_ms / 1000:,.2f} s avg ({sample.runs:,} run{'s' if sample.runs != 1 else ''})"
    if not sample.replayable:
        return {"object": label, "status": REVIEW, "source": synapse, "target": "not run",
                "detail": "Not replayed: it writes, or uses temporary tables or variables. Benchmark it in its pipeline or report."}
    sql = tsql_rules.rewrite(sample.text).text
    if pool and warehouse and pool.lower() != warehouse.lower():
        sql = re.sub(rf"\[?{re.escape(pool)}\]?\.", f"{warehouse_ddl.quote(warehouse)}.", sql, flags=re.IGNORECASE)
    try:
        cold, _ = timed(cursor, sql, clock)
        warm, rows = timed(cursor, sql, clock)
    except Exception as exc:  # noqa: BLE001 - the server's message is the result
        return {"object": label, "status": MISMATCH, "source": synapse, "target": "failed",
                "detail": f"Fabric rejected the query: {str(exc).split(']')[-1].strip()[:300]}"}
    fabric = f"{warm / 1000:,.2f} s (first run {cold / 1000:,.2f} s, {rows:,} rows)"
    base = max(sample.avg_ms, 1)
    change = (warm - base) / base * 100
    if change <= options.threshold_pct:
        verdict = "faster" if change < 0 else "slower"
        return {"object": label, "status": MATCH, "source": synapse, "target": fabric,
                "detail": f"{abs(change):,.0f}% {verdict} than on Synapse, within the {options.threshold_pct:g}% allowed."}
    return {"object": label, "status": MISMATCH if options.strict else REVIEW, "source": synapse, "target": fabric,
            "detail": f"{change:,.0f}% slower than on Synapse (allowed: {options.threshold_pct:g}%). Check the target's data layout, "
                      "the query plan and capacity use before cutover; Fabric's time here includes returning the rows."}
