"""The pool's estate (volume, row-level security, external sources, workload) and what is built on it:
the inventory, row-level security in the Warehouse, and the capacity assessment. Offline."""

from __future__ import annotations

from datetime import datetime

from discovery_agent.api import mapping
from discovery_agent.migration import capacity, preflight, security
from discovery_agent.migration.common import SECURITY, Source
from discovery_agent.sql import estate as es

from test_sql_catalog import full_responses, responses, source  # noqa: E402


def test_sizes_add_up_distributed_tables_and_take_one_copy_of_a_replicated_one():
    rows = [{"schema_name": "dbo", "table_name": "Fact", "node_id": n, "row_count": 100, "reserved_pages": 128} for n in (1, 2)]
    rows += [{"schema_name": "dbo", "table_name": "Dim", "node_id": n, "row_count": 50, "reserved_pages": 16} for n in (1, 2)]
    sizes = {s.table: s for s in es.build_sizes(rows, replicated=[("dbo", "Dim")])}
    assert (sizes["Fact"].rows, sizes["Fact"].reserved_kb) == (200, 2048)
    assert (sizes["Dim"].rows, sizes["Dim"].reserved_kb) == (50, 128)


def test_policies_group_their_predicates():
    rows = [
        {"policy_schema": "sec", "policy_name": "SalesFilter", "is_enabled": 1, "predicate_type": "FILTER", "operation": None,
         "predicate_definition": "([sec].[fn_rep]([Rep]))", "target_schema": "dbo", "target_name": "Sales"},
        {"policy_schema": "sec", "policy_name": "SalesFilter", "is_enabled": 1, "predicate_type": "BLOCK", "operation": "AFTER INSERT",
         "predicate_definition": "([sec].[fn_rep]([Rep]))", "target_schema": "dbo", "target_name": "Sales"},
    ]
    (policy,) = es.build_policies(rows)
    assert policy.qualified_name == "sec.SalesFilter" and len(policy.filters) == 1 and len(policy.blocks) == 1


def test_workload_finds_peak_concurrency_and_which_queries_can_be_replayed():
    t = lambda m: datetime(2026, 10, 1, 9, m)  # noqa: E731
    requests = [{"start_time": t(0), "end_time": t(10), "elapsed_ms": 600_000}, {"start_time": t(5), "end_time": t(8), "elapsed_ms": 1},
                {"start_time": t(7), "end_time": t(9), "elapsed_ms": 1}, {"start_time": t(10), "end_time": t(11), "elapsed_ms": 1}]
    samples = [{"command_text": "SELECT a FROM dbo.T", "run_count": 5, "avg_ms": 2000, "max_ms": 3000, "min_ms": 1000},
               {"command_text": "SELECT a INTO #t FROM dbo.T", "run_count": 1, "avg_ms": 10, "max_ms": 10, "min_ms": 10},
               {"command_text": "select x from dbo.T where y = @p", "run_count": 1, "avg_ms": 10, "max_ms": 10, "min_ms": 10}]
    w = es.build_workload(samples, requests)
    assert w.peak_concurrency == 3 and w.requests == 4
    assert [s.replayable for s in w.samples] == [True, False, False]


def test_discovery_reads_the_estate_and_an_unreadable_part_is_unknown_not_none():
    found = source(full_responses(table_sizes=RuntimeError("VIEW DATABASE STATE permission denied"))).discover()
    assert found.estate is not None and not found.estate.sizes_readable and found.estate.policies_readable
    assert any("table_sizes" in (i.location or "") for i in found.all_issues)
    assert found.estate.summary()["totalGb"] is None


def test_inventory_lists_policies_sources_formats_and_lake_databases():
    policy = es.SqlSecurityPolicy("sec", "SalesFilter", True, (
        es.SqlPredicate("FILTER", "([sec].[fn_rep]([Rep]))", "dbo", "Sales"),))
    estate = es.SqlEstate(policies=(policy,), data_sources=(es.SqlExternalDataSource("lake", "abfss://raw@acct.dfs.core.windows.net", "HADOOP", "cred"),),
                          file_formats=(es.SqlExternalFileFormat("parquet", "PARQUET"),))

    class Catalog:
        class database:  # noqa: N801
            name = "pool"
    Catalog.estate = estate
    extras = {e.source_type + ":" + e.name: e for e in mapping.estate_extras(Catalog)}
    p = extras["Security Object:sec.SalesFilter"]
    assert {d["targetId"] for d in p.dependencies} == {"sql://pool/dbo/Sales", "sql://pool/sec/fn_rep"}
    assert extras["External Data Source:lake"].metadata["credential"] == "cred"
    assert "External File Format:parquet" in extras
    (lake,) = mapping.lake_database_extras([{"name": "sales_lake", "properties": {"Source": {"Location": "abfss://x", "Properties": {"FormatType": "parquet"}}}}],
                                           {"sales_lake": [{"name": "orders"}]})
    assert lake.source_type == "Lake Database" and lake.metadata["tables"] == ["orders"] and lake.metadata["formatType"] == "parquet"


def test_row_level_security_becomes_a_filter_policy_and_block_predicates_go_to_a_person():
    policy = es.SqlSecurityPolicy("sec", "SalesFilter", True, (
        es.SqlPredicate("FILTER", "([sec].[fn_rep]([Rep]))", "dbo", "Sales"),
        es.SqlPredicate("BLOCK", "([sec].[fn_rep]([Rep]))", "dbo", "Sales", "AFTER INSERT")))
    plan = security.plan_policy(policy)
    assert plan.statement == ("CREATE SECURITY POLICY [sec].[SalesFilter] ADD FILTER PREDICATE [sec].[fn_rep]([Rep]) "
                              "ON [dbo].[Sales] WITH (STATE = ON);")
    assert "BLOCK" in plan.notes[0]
    src = Source("security://pool/policy/sec.SalesFilter", "sec.SalesFilter", "Security Object", 3, SECURITY, payload=policy)
    codes = {f.code for f in preflight.content_findings([src])}
    assert {"ROW_LEVEL_SECURITY", "SECURITY_REVIEW"} <= codes


def test_capacity_assessment_states_its_basis_and_compares_the_target():
    est = es.SqlEstate(sizes=(es.SqlTableSize("dbo", "Fact", 10, 50 * 1024 * 1024),),
                       workload=es.SqlWorkload(peak_concurrency=40, requests=100, window_start="2026-10-01T00:00", window_end="2026-10-02T00:00"))
    a = capacity.assess(est, {"name": "DW500c"}, [{"name": "p", "nodeSize": "Small", "nodeCount": 3}], (), target_sku="F64")
    assert a["sql"]["cu"] == 45  # DW500c at 6 CU per DW100c, sized up by half for concurrency above 32
    assert a["spark"]["cu"] == 6 and a["recommendation"]["sku"] == "F64" and a["target"]["fits"] is True
    assert any("assumed" in c for c in a["caveats"]) and any("GB" in e for e in a["evidence"])
    assert capacity.sku_for(65) == "F128" and capacity.sku_for(5000) is None
    assert capacity.assess()["recommendation"]["sku"] is None
