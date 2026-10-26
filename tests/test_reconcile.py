"""Data reconciliation beyond row counts: value profiles of both sides, compared. Offline."""

from __future__ import annotations

from decimal import Decimal

from discovery_agent.migration import reconcile as rc
from discovery_agent.migration import validation as v
from discovery_agent.migration.common import DATA, Source
from discovery_agent.sql.models import SqlColumn, SqlIndex, SqlIndexColumn, SqlObjectKey, SqlTable

from test_validation import CatalogCursor, CatalogDb, plain_table, validator  # noqa: E402
from test_migration_stages import SourceCursor, SourceDb  # noqa: E402


def keyed_table():
    t = plain_table()
    pk = SqlIndex(index_id=2, name="PK_Orders", is_primary_key=True, columns=(SqlIndexColumn(1, "Id", key_ordinal=1),))
    return SqlTable(key=t.key, columns=t.columns, indexes=(pk,))


def test_measures_read_nulls_exact_sums_dates_to_the_second_and_text_lengths():
    items, left_out = rc.measures(plain_table())
    assert left_out == 0
    assert [m.label for m in items] == [
        "sum of Id", "NULLs in Label", "length of Label", "NULLs in Amount", "sum of Amount", "NULLs in At", "earliest At", "latest At"]
    sql = rc.profile_sql("sales", "Orders", items, ["[At] >= '2024-01-01'"])
    assert "SUM(CAST([Amount] AS decimal(38,4)))" in sql and "CONVERT(varchar(19), MIN([At]), 120)" in sql
    assert sql.endswith("FROM [sales].[Orders] WHERE [At] >= '2024-01-01'")


def test_types_the_migration_changes_are_profiled_for_nulls_only():
    t = SqlTable(key=SqlObjectKey("pool", "s", "T"), columns=(
        SqlColumn(1, "Off", "datetimeoffset", 10, 34, 7, True, False), SqlColumn(2, "Blob", "varbinary", 100, 0, 0, True, False),
        SqlColumn(3, "Note", "ntext", 16, 0, 0, True, False), SqlColumn(4, "Ratio", "float", 8, 53, 0, False, False)))
    assert [m.label for m in rc.measures(t)[0]] == ["NULLs in Off", "NULLs in Blob", "NULLs in Note", "sum of Ratio"]


def _items():
    return rc.measures(plain_table())[0]


SRC = [Decimal(55), 2, Decimal(120), 0, Decimal("1000.5000"), 1, "2024-01-01 00:00:00", "2024-12-31 23:59:59"]


def test_equal_profiles_match_and_a_difference_is_a_mismatch_named_with_both_values():
    assert rc.compare(_items(), SRC, list(SRC), rc.ReconcileOptions(profile=True)).status == "MATCH"
    off = list(SRC)
    off[4] = Decimal("999.5000")
    c = rc.compare(_items(), SRC, off, rc.ReconcileOptions(profile=True))
    assert c.status == "MISMATCH" and "sum of Amount: 1,000.5 in Synapse, 999.5 in Fabric" in c.detail
    late = list(SRC)
    late[7] = "2024-12-31 23:59:58"
    assert "latest At" in rc.compare(_items(), SRC, late, rc.ReconcileOptions(profile=True, tolerance_pct=5)).detail


def test_an_approved_tolerance_and_a_person_judging_differences():
    off = list(SRC)
    off[4] = Decimal("1000.4000")
    within = rc.compare(_items(), SRC, off, rc.ReconcileOptions(profile=True, tolerance_pct=0.1))
    assert within.status == "MATCH" and "0.1% tolerance" in within.detail
    judged = rc.compare(_items(), SRC, off, rc.ReconcileOptions(profile=True, differences="review"))
    assert judged.status == "REVIEW" and "for a person to judge" in judged.detail


def test_float_sums_allow_for_rounding_only():
    t = SqlTable(key=SqlObjectKey("pool", "s", "T"), columns=(SqlColumn(1, "R", "float", 8, 53, 0, False, False),))
    items = rc.measures(t)[0]
    assert rc.compare(items, [1234.5678901234], [1234.5678901235], rc.ReconcileOptions()).status == "MATCH"
    assert rc.compare(items, [1234.5], [1234.6], rc.ReconcileOptions()).status == "MISMATCH"


def test_duplicate_keys_in_fabric_are_a_mismatch_even_within_a_tolerance():
    c = rc.compare(_items(), SRC, list(SRC), rc.ReconcileOptions(tolerance_pct=10), duplicates=(0, 3), keys=("Id",))
    assert c.status == "MISMATCH" and "duplicate keys (Id): 3" in c.detail
    same = rc.compare(_items(), SRC, list(SRC), rc.ReconcileOptions(), duplicates=(2, 2), keys=("Id",))
    assert same.status == "MATCH" and "Synapse already holds 2" in same.detail


def test_options_parse_and_clamp():
    assert rc.ReconcileOptions.parse(None) == rc.ReconcileOptions()
    o = rc.ReconcileOptions.parse({"profile": True, "tolerancePct": 50, "differences": "review"})
    assert o.profile and o.tolerance_pct == rc.MAX_TOLERANCE_PCT and o.differences == "review"
    assert rc.ReconcileOptions.parse({"profile": "yes", "tolerancePct": "x"}) == rc.ReconcileOptions()


# --- in the validator ------------------------------------------------------------------------


class ProfileCursor(CatalogCursor):
    def execute(self, sql, *params):
        if " AS m0" in sql:
            self._one = tuple(self.db.profile)
        elif "HAVING COUNT_BIG" in sql:
            self._one = (self.db.dupes,)
        else:
            super().execute(sql, *params)


class ProfileDb(CatalogDb):
    def __init__(self, profile, dupes=0, **kw):
        super().__init__(**kw)
        self.profile, self.dupes = profile, dupes

    def cursor(self):
        return ProfileCursor(self)


class ProfileSourceCursor(SourceCursor):
    def execute(self, sql, *params):
        self.db.asked.append(sql)
        if " AS m0" in sql:
            self._row = tuple(SRC)
        elif "HAVING COUNT_BIG" in sql:
            self._row = (0,)
        else:
            super().execute(sql, *params)


class ProfileSourceDb(SourceDb):
    def __init__(self, rows):
        super().__init__(rows)
        self.asked = []

    def cursor(self):
        return ProfileSourceCursor(self)


def _data(t):
    return Source("t#data", "sales.Orders (data)", "Table data", 2, DATA, payload=t, schema="sales", object_name="Orders")


def _run(target_profile, options, rows=10, dupes=0):
    t = keyed_table()
    db = ProfileDb(target_profile, dupes, tables={("sales", "Orders"): t}, rows={"[sales].[Orders]": rows})
    source = ProfileSourceDb([0] * 10)
    val = validator(db=db, source=source)
    val.reconcile = options
    return val.run([_data(t)]), source


def test_validation_adds_a_profile_row_when_counts_agree_and_profiling_is_on():
    rows, _ = _run(list(SRC), rc.ReconcileOptions(profile=True))
    assert [(r["category"], r["status"]) for r in rows] == [("Data Count", v.MATCH), ("Data Profile", v.MATCH)]
    assert rows[1]["object"] == "sales.Orders" and "9 measures" == rows[1]["source"]
    off = list(SRC)
    off[1] = 5
    rows, _ = _run(off, rc.ReconcileOptions(profile=True))
    assert rows[1]["status"] == v.MISMATCH and "NULLs in Label: 2 in Synapse, 5 in Fabric" in rows[1]["detail"]
    rows, _ = _run(list(SRC), rc.ReconcileOptions(profile=True), dupes=4)
    assert rows[1]["status"] == v.MISMATCH and "duplicate keys" in rows[1]["detail"]


def test_no_profile_when_off_or_when_counts_already_differ():
    rows, source = _run(list(SRC), rc.ReconcileOptions())
    assert [r["category"] for r in rows] == ["Data Count"] and not any(" AS m0" in q for q in source.asked)
    rows, source = _run(list(SRC), rc.ReconcileOptions(profile=True), rows=7)
    assert [r["category"] for r in rows] == ["Data Count"] and not any(" AS m0" in q for q in source.asked)


def test_an_unreadable_profile_is_a_review_never_a_match():
    class Broken(ProfileCursor):
        def execute(self, sql, *params):
            if " AS m0" in sql:
                raise RuntimeError("Arithmetic overflow error converting expression to data type numeric.")
            super().execute(sql, *params)

    t = keyed_table()
    db = ProfileDb(list(SRC), tables={("sales", "Orders"): t}, rows={"[sales].[Orders]": 10})
    db.cursor = lambda: Broken(db)
    val = validator(db=db, source=ProfileSourceDb([0] * 10))
    val.reconcile = rc.ReconcileOptions(profile=True)
    rows = val.run([_data(t)])
    assert rows[1]["status"] == v.REVIEW and "Arithmetic overflow" in rows[1]["detail"]
