"""Date filters and keeping tables in sync: which rows a table loads, and how later changes follow.

Offline, like the stage tests: Fabric, the Warehouse and the Synapse pool are fakes.
What this cannot establish is that Fabric runs the generated sync pipeline exactly as
written; that needs a live run.
"""

from __future__ import annotations

import pytest

from discovery_agent.api.migration import apply_stages, parse_options
from discovery_agent.api.service import ApiError
from discovery_agent.migration import datafilter, datapipeline
from discovery_agent.migration.common import COMPLETED, DATA, FAILED, SKIPPED, TABLE, Source
from discovery_agent.migration.datafilter import DataFilter, FilterError
from discovery_agent.migration.preflight import content_findings
from discovery_agent.sql.models import IndexKind, SqlColumn, SqlIndex, SqlIndexColumn, SqlObjectKey, SqlTable

from test_migration_stages import DataDb, PipelineRest, by_target, load, pipeline_content, rows  # noqa: E402 - shared fakes

ORDERS = "[sales].[Orders]"
CONTROL = datafilter.CONTROL


def orders(with_key=True):
    columns = (
        SqlColumn(1, "OrderId", "int", 4, 10, 0, False, False),
        SqlColumn(2, "Customer", "nvarchar", 200, 0, 0, True, False),
        SqlColumn(3, "OrderDate", "date", 3, 10, 0, False, False),
        SqlColumn(4, "ModifiedAt", "datetime2", 8, 27, 7, True, False),
    )
    indexes = (SqlIndex(2, IndexKind.NONCLUSTERED, "pk_orders", is_primary_key=True,
                        columns=(SqlIndexColumn(1, "OrderId", key_ordinal=1),)),) if with_key else ()
    return SqlTable(key=SqlObjectKey("pool", "sales", "Orders"), columns=columns, indexes=indexes)


def item(f=None, tbl=None):
    return Source("Orders#data", "sales.Orders (data)", "Table data", 2, DATA, payload=tbl or orders(),
                  schema="sales", object_name="Orders", data_filter=f)


SINCE = DataFilter(column="OrderDate", start="2024-10-08")
SYNCED = DataFilter(column="OrderDate", start="2024-10-08", sync=True, change_column="ModifiedAt")


# ---- what an operator can ask for ------------------------------------------------------------


def test_a_filter_is_parsed_from_the_request_shape():
    got = datafilter.parse_filters({"Sales.Orders": {"column": "OrderDate", "from": "2024-10-08", "to": "", "sync": True,
                                                     "changeColumn": "ModifiedAt", "keys": ["OrderId"]}})
    assert got == {"sales.orders": DataFilter("OrderDate", "2024-10-08", "", True, "ModifiedAt", ("OrderId",))}
    assert got["sales.orders"].to_dict()["from"] == "2024-10-08"
    # change column and keys mean nothing without sync, so they are dropped
    plain = datafilter.parse_one("t", {"column": "OrderDate", "from": "2024-01-01", "changeColumn": "x", "keys": ["y"]})
    assert plain == DataFilter("OrderDate", "2024-01-01")


@pytest.mark.parametrize("raw, message", [
    ({"column": "OrderDate"}, "give a start date"),
    ({"from": "2024-01-01"}, "choose the date column"),
    ({"column": "OrderDate", "from": "2024-13-01"}, "YYYY-MM-DD"),
    ({"column": "OrderDate", "from": "last year"}, "YYYY-MM-DD"),
    ({"column": "OrderDate", "from": "2025-01-01", "to": "2024-01-01"}, "before the end date"),
    ({"column": "OrderDate", "to": "2025-01-01", "sync": True}, "cannot have an end date"),
    ({"sync": True}, "choose the change column"),
    ({}, "nothing to do"),
    ({"column": "OrderDate", "from": "2024-01-01", "sync": True, "keys": "OrderId"}, "key columns are not valid"),
])
def test_a_filter_that_cannot_mean_anything_is_refused_with_the_reason(raw, message):
    with pytest.raises(FilterError, match=message):
        datafilter.parse_one("sales.Orders", raw)


def test_the_api_refuses_a_bad_filter_and_keeps_the_re_read_window_in_range():
    with pytest.raises(ApiError, match="YYYY-MM-DD"):
        parse_options({"options": {"dataFilters": {"sales.Orders": {"column": "OrderDate", "from": "soon"}}}})
    with pytest.raises(ApiError, match="re-read window"):
        parse_options({"options": {"syncOverlap": "1w"}})
    options = parse_options({"options": {"dataFilters": {"sales.Orders": {"column": "OrderDate", "from": "2024-10-08"}}}})
    assert options["dataFilters"]["sales.orders"] == SINCE and options["syncOverlap"] == "1h"


def test_each_tables_filter_rides_on_its_data_item():
    table_source = Source("t", "sales.Orders", "Table", 2, TABLE, payload=orders(), schema="sales", object_name="Orders")
    other = Source("u", "sales.Lines", "Table", 2, TABLE, payload=orders(), schema="sales", object_name="Lines")
    kept = apply_stages([table_source, other], {"stages": ["data"], "scope": "automated", "dataFilters": {"sales.orders": SINCE}})
    data = {s.id: s.data_filter for s in kept if s.kind == DATA}
    assert data == {"t#data": SINCE, "u#data": None}


# ---- a filter against its table ------------------------------------------------------------------


def test_a_filter_must_name_real_date_columns_and_keys_default_to_the_primary_key():
    r = datafilter.resolve(orders(), DataFilter("orderdate", "2024-10-08", sync=True, change_column="modifiedat"))
    assert (r.column.name, r.change.name, r.keys, r.keys_from_table) == ("OrderDate", "ModifiedAt", ("OrderId",), True)
    assert datafilter.resolve(orders(with_key=False), SYNCED).keys == ()
    with pytest.raises(FilterError, match="no column Shipped"):
        datafilter.resolve(orders(), DataFilter("Shipped", "2024-01-01"))
    with pytest.raises(FilterError, match="nvarchar, not a date"):
        datafilter.resolve(orders(), DataFilter("Customer", "2024-01-01"))
    with pytest.raises(FilterError, match="no column Nope"):
        datafilter.resolve(orders(), DataFilter("OrderDate", "2024-01-01", sync=True, change_column="ModifiedAt", keys=("Nope",)))


def test_the_first_load_reads_the_scope_and_a_synced_table_stops_at_its_watermark():
    plain = datafilter.resolve(orders(), DataFilter("OrderDate", "2024-01-01", "2025-01-01"))
    assert datafilter.select_sql(orders(), datafilter.first_load_predicates(plain, None)).endswith(
        "FROM [sales].[Orders] WHERE [OrderDate] >= '2024-01-01' AND [OrderDate] < '2025-01-01'")
    synced = datafilter.resolve(orders(), SYNCED)
    where = datafilter.first_load_predicates(synced, "2026-10-08T09:30:00.1234567")
    assert where == ["[OrderDate] >= '2024-10-08'",
                     "([ModifiedAt] <= CAST('2026-10-08T09:30:00.1234567' AS datetime2(7)) OR [ModifiedAt] IS NULL)"]
    # an empty scope has no watermark to stop at
    assert datafilter.first_load_predicates(synced, datafilter.EMPTY_WATERMARK) == ["[OrderDate] >= '2024-10-08'"]


def test_a_sync_reads_from_the_last_watermark_and_with_keys_re_reads_the_window():
    keyed = datafilter.sync_copy_sql(orders(), datafilter.resolve(orders(), SYNCED), 60)
    assert keyed.endswith("WHERE [OrderDate] >= '2024-10-08' AND [ModifiedAt] >= DATEADD(minute, -60, "
                          "CAST('{{from}}' AS datetime2(7))) AND [ModifiedAt] <= CAST('{{to}}' AS datetime2(7))")
    # without keys a re-read row would arrive twice: strictly after the watermark, no window
    loose = datafilter.sync_copy_sql(orders(with_key=False), datafilter.resolve(orders(with_key=False), SYNCED), 60)
    assert "[ModifiedAt] > CAST('{{from}}' AS datetime2(7))" in loose and "DATEADD" not in loose


def test_the_apply_batch_replaces_matched_rows_adds_the_rest_and_moves_the_watermark_in_one_transaction():
    sql = datafilter.apply_sql(orders(), datafilter.resolve(orders(), SYNCED), "sales", "Orders")
    stage = "[migration_sync].[sales__Orders]"
    assert sql.startswith("BEGIN TRANSACTION;") and sql.endswith("COMMIT TRANSACTION;")
    assert f"DELETE FROM [sales].[Orders] WHERE EXISTS (SELECT 1 FROM {stage} AS s WHERE s.[OrderId] = [sales].[Orders].[OrderId]);" in sql
    assert f"INSERT INTO [sales].[Orders] ([OrderId], [Customer], [OrderDate], [ModifiedAt]) SELECT [OrderId], [Customer], [OrderDate], [ModifiedAt] FROM {stage};" in sql
    assert f"SET watermark = '{{{{to}}}}'" in sql and "WHERE table_name = 'sales.orders'" in sql
    append_only = datafilter.apply_sql(orders(with_key=False), datafilter.resolve(orders(with_key=False), SYNCED), "sales", "Orders")
    assert "DELETE" not in append_only


def test_a_watermark_is_checked_before_it_reaches_sql():
    assert datafilter.clean_watermark("2026-10-08T09:30:00.1234567") == "2026-10-08T09:30:00.1234567"
    assert datafilter.clean_watermark("2026-10-08T09:30:00+05:30") == "2026-10-08T09:30:00+05:30"
    assert datafilter.clean_watermark(None) == datafilter.EMPTY_WATERMARK
    with pytest.raises(FilterError):
        datafilter.clean_watermark("2026-10-08'; DROP TABLE x; --")


def test_a_long_staging_name_stays_within_the_limit_and_distinct():
    a, b = datafilter.staging_table("s", "x" * 130), datafilter.staging_table("s", "x" * 129)
    assert len(a) <= 128 and len(b) <= 128 and a != b


def test_the_sync_pipeline_reads_its_table_list_from_the_control_table():
    target = datapipeline.WarehouseTarget("wh-1", "abc.datawarehouse.fabric.microsoft.com", "pool01", "ws")
    content = datapipeline.sync_definition("conn-1", target)
    lookup, loop = content["properties"]["activities"]
    assert lookup["typeProperties"]["source"]["sqlReaderQuery"].endswith(f"FROM {CONTROL} ORDER BY table_name")
    assert lookup["typeProperties"]["firstRowOnly"] is False
    assert loop["typeProperties"]["items"]["value"] == "@activity('Tables to sync').output.value"
    # every apply batch writes the control table, so tables sync one at a time
    assert loop["typeProperties"]["isSequential"] is True
    newest, copy, apply = loop["typeProperties"]["activities"]
    assert [a["type"] for a in (newest, copy, apply)] == ["Lookup", "Copy", "Script"]
    assert copy["dependsOn"][0]["activity"] == "Newest change" and apply["dependsOn"][0]["activity"] == "Copy changes"
    assert "item().watermark" in copy["typeProperties"]["source"]["sqlReaderQuery"]["value"]
    # an empty scope gives '' (never NULL), and the run then keeps the last watermark
    assert "if(empty(activity('Newest change').output.firstRow.wm), item().watermark" in apply["typeProperties"]["scripts"][0]["text"]["value"]
    assert datafilter.watermark_sql(orders(), datafilter.resolve(orders(), SYNCED)).startswith("SELECT COALESCE(CONVERT(varchar(40), MAX([ModifiedAt]), 126), '') AS wm")
    assert copy["typeProperties"]["sink"]["datasetSettings"]["typeProperties"]["schema"] == datafilter.SYNC_SCHEMA
    assert apply["linkedService"]["properties"]["typeProperties"]["artifactId"] == "wh-1"
    body = datapipeline.sync_schedule_body(__import__("datetime").datetime(2026, 10, 8, 12, 0))
    assert body["enabled"] is False and body["configuration"]["times"] == ["02:00"]


# ---- the stage ---------------------------------------------------------------------------------


class SyncDb(DataDb):
    """The fake Warehouse, also keeping the control table's rows as the register statement writes them."""

    def __init__(self, tables=None):
        super().__init__(tables)
        self.registered = []

    def cursor(self):
        inner, db = super().cursor(), self

        class Cursor:
            def execute(self, sql, *params):
                if sql == datafilter.REGISTER_SQL:
                    db.statements.append(sql)
                    db.registered.append(params)
                else:
                    inner.execute(sql, *params)

            def fetchone(self):
                return inner.fetchone()

        return Cursor()


def by_id(run):
    return {i.source.id: i for i in run.items}


class FilteredSource:
    """A fake Synapse pool: counts per table and WHERE clause, and the newest change of a synced table."""

    def __init__(self, counts, watermark="2026-10-08T09:30:00.0000000"):
        self.counts, self.watermark, self.seen = dict(counts), watermark, []

    def cursor(self):
        db = self

        class Cursor:
            _row = None

            def execute(self, sql, *params):
                db.seen.append(sql)
                if sql.startswith("SELECT COALESCE(CONVERT"):
                    self._row = (db.watermark,)
                else:
                    self._row = (db.counts.get(sql.split("FROM ")[1], 0),)

            def fetchone(self):
                return self._row

        return Cursor()

    def close(self):
        pass


def test_a_filtered_table_loads_only_its_rows_and_is_counted_with_the_same_filter():
    db = DataDb({ORDERS: []})
    rest = PipelineRest(db, {ORDERS: rows(3)})
    source = FilteredSource({f"{ORDERS} WHERE [OrderDate] >= '2024-10-08'": 3, ORDERS: 10})
    run = load([item(SINCE)], db, rest, source)
    got = by_target(run)["pool01.sales.Orders"]
    assert got.status == COMPLETED and got.step == "Loaded 3 rows by pipeline; counts match within the filter"
    assert "Loads rows with OrderDate on or after 2024-10-08." in got.notes
    entry = rest.runs[0][1]["executionData"]["parameters"]["tables"][0]
    assert entry["query"].endswith("FROM [sales].[Orders] WHERE [OrderDate] >= '2024-10-08'")
    # no table kept in sync: no sync pipeline, no control table
    assert [b["displayName"] for p, b in rest.created if p.endswith("/dataPipelines")] == ["load_pool01_wave_2"]
    assert not any("migration_sync" in s for s in db.statements)


def test_a_synced_table_is_loaded_up_to_its_watermark_registered_and_given_the_sync_pipeline():
    db = SyncDb({ORDERS: []})
    rest = PipelineRest(db, {ORDERS: rows(4)})
    bounded = (f"{ORDERS} WHERE [OrderDate] >= '2024-10-08' AND ([ModifiedAt] <= "
               "CAST('2026-10-08T09:30:00.0000000' AS datetime2(7)) OR [ModifiedAt] IS NULL)")
    run = load([item(SYNCED)], db, rest, FilteredSource({bounded: 4}))
    got = by_target(run)["pool01.sales.Orders"]
    assert got.status == COMPLETED and "counts match within the filter" in got.step, got.notes
    # the first load stops at the watermark
    assert rest.runs[0][1]["executionData"]["parameters"]["tables"][0]["query"].endswith(bounded.replace(ORDERS, "FROM " + ORDERS))
    # the control table, the staging table and the table's row
    assert any(s.startswith("IF OBJECT_ID('[migration_sync].[sync_tables]') IS NULL CREATE TABLE") for s in db.statements)
    assert any(s.startswith("CREATE TABLE [migration_sync].[sales__Orders] AS SELECT * FROM [sales].[Orders] WHERE 1 = 0") for s in db.statements)
    assert datafilter.UNREGISTER_SQL in db.statements and len(db.registered) == 1
    registered = db.registered[0]
    assert registered[0] == "sales.orders" and registered[5] == "2026-10-08T09:30:00.0000000"
    assert "DATEADD(minute, -60" in registered[7]  # the default re-read window, with the primary key
    # one sync pipeline, with a daily schedule switched off
    names = [b["displayName"] for p, b in rest.created if p.endswith("/dataPipelines")]
    assert names == ["sync_pool01", "load_pool01_wave_2"]
    sync = pipeline_content(rest, "sync_pool01")
    assert sync["properties"]["activities"][0]["name"] == "Tables to sync"
    schedules = [b for p, b in rest.created if p.endswith("/jobs/Pipeline/schedules")]
    assert len(schedules) == 1 and schedules[0]["enabled"] is False
    assert any("Sync pipeline 'sync_pool01' created" in n for n in got.notes)
    assert any("Changed rows replace the Warehouse row with the same OrderId" in n for n in got.notes)


def test_a_synced_table_is_not_loaded_while_the_sync_schedule_is_on():
    db = SyncDb({ORDERS: []})
    rest = PipelineRest(db, {ORDERS: rows(2)}, dataPipelines=[{"id": "pl-sync", "displayName": "sync_pool01"}])
    rest.schedules["pl-sync"] = [{"enabled": True}]
    run = load([item(SYNCED)], db, rest, FilteredSource({}))
    got = by_id(run)["Orders#data"]
    assert got.status == FAILED and "Switch the schedule off in Fabric" in got.error
    assert not db.registered and not rest.runs
    # switched off: the load goes ahead
    db = SyncDb({ORDERS: []})
    rest = PipelineRest(db, {ORDERS: rows(2)}, dataPipelines=[{"id": "pl-sync", "displayName": "sync_pool01"}])
    rest.schedules["pl-sync"] = [{"enabled": False}]
    bounded = (f"{ORDERS} WHERE [OrderDate] >= '2024-10-08' AND ([ModifiedAt] <= "
               "CAST('2026-10-08T09:30:00.0000000' AS datetime2(7)) OR [ModifiedAt] IS NULL)")
    run = load([item(SYNCED)], db, rest, FilteredSource({bounded: 2}))
    assert by_id(run)["Orders#data"].status == COMPLETED and len(db.registered) == 1 and rest.runs


def test_the_re_read_window_follows_the_stage_option():
    db = SyncDb({ORDERS: []})
    rest = PipelineRest(db, {ORDERS: rows(1)})
    load([item(SYNCED)], db, rest, FilteredSource({}), settings={"syncOverlap": "none"})
    copy_query = db.registered[0][7]
    assert "DATEADD" not in copy_query and "[ModifiedAt] >= CAST('{{from}}'" in copy_query


def test_a_filter_that_does_not_fit_fails_that_table_alone():
    db = DataDb({ORDERS: [], "[sales].[Lines]": []})
    rest = PipelineRest(db, {"[sales].[Lines]": rows(2)})
    lines = Source("Lines#data", "sales.Lines (data)", "Table data", 2, DATA,
                   payload=SqlTable(key=SqlObjectKey("pool", "sales", "Lines"), columns=orders().columns), schema="sales", object_name="Lines")
    run = load([item(DataFilter("Shipped", "2024-01-01")), lines], db, rest, FilteredSource({"[sales].[Lines]": 2}))
    got = by_id(run)
    assert got["Orders#data"].status == FAILED and "no column Shipped" in got["Orders#data"].error
    assert got["Lines#data"].status == COMPLETED


def test_a_synced_table_that_already_has_rows_says_whether_it_is_in_sync():
    db = DataDb({ORDERS: [[1]]})
    run = load([item(SYNCED)], db, PipelineRest(db), FilteredSource({}))
    got = by_target(run)["pool01.sales.Orders"]
    assert got.status == SKIPPED and "Not kept in sync yet" in got.notes[0] and "Replace it" in got.notes[0]


def test_the_plan_flags_a_filter_that_does_not_fit_and_a_sync_without_keys():
    bad = content_findings([item(DataFilter("Customer", "2024-01-01"))])
    assert [f.code for f in bad] == ["FILTER_INVALID"]
    loose = content_findings([item(SYNCED, orders(with_key=False))])
    assert [f.code for f in loose] == ["SYNC_APPEND_ONLY"]
    assert content_findings([item(SYNCED)]) == []


# ---- validation --------------------------------------------------------------------------------


def _validator(target, counts, watermark=None):
    """A validator whose row counts are given: Synapse's by WHERE clause, the Warehouse's as one number."""
    from discovery_agent.migration.validation import Validator
    v = Validator(None, "ws", "Sales WS", "pool01", lambda host, name: None, None)
    v.target_rows = lambda schema, name: target
    v.source_rows = lambda schema, name, predicates=(): counts[" AND ".join(predicates)]
    v._watermark = lambda schema, name: watermark
    return v


def test_validation_counts_synapse_with_the_same_filter():
    scope = "[OrderDate] >= '2024-10-08'"
    ok = _validator(3, {scope: 3})._check_filtered(item(SINCE), "sales", "Orders")
    assert ok["status"] == "MATCH" and "rows with OrderDate on or after 2024-10-08" in ok["detail"]
    short = _validator(2, {scope: 3})._check_filtered(item(SINCE), "sales", "Orders")
    assert short["status"] == "MISMATCH" and "1 row(s) missing from Fabric" in short["detail"]


def test_a_synced_table_between_its_last_sync_and_now_is_in_step_not_wrong():
    scope = "[OrderDate] >= '2024-10-08'"
    through = scope + " AND ([ModifiedAt] <= CAST('2026-10-08T00:00:00' AS datetime2(7)) OR [ModifiedAt] IS NULL)"
    v = _validator(4, {scope: 6, through: 4}, watermark="2026-10-08T00:00:00")
    got = v._check_filtered(item(SYNCED), "sales", "Orders")
    assert got["status"] == "REVIEW" and "In step up to the last sync (2026-10-08T00:00:00)" in got["detail"]
    # more rows in Fabric than Synapse has: the likely cause is named
    extra = _validator(8, {scope: 6, through: 4}, watermark="2026-10-08T00:00:00")._check_filtered(item(SYNCED), "sales", "Orders")
    assert extra["status"] == "MISMATCH" and "hard deletes" in extra["detail"]
