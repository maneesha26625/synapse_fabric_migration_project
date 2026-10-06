"""The rule-based conversions: T-SQL rewrites, keys, collation, notebook code,
parameterised linked services and the extra pipeline activity types.

Offline, with the same fakes as the stage tests. Each rule is checked on the
code it changes, on code it must leave alone, and for being safe to repeat.
"""

from __future__ import annotations

import json

from discovery_agent.migration import fabric_connections, jobs, notebooks, pipelines, tsql_rules, warehouse_ddl
from discovery_agent.migration.common import COMPLETED, PROCEDURE, TABLE, VIEW, Source
from discovery_agent.migration.runner import CASE_INSENSITIVE, CASE_SENSITIVE, MigrationRun, Migrator, warehouse_collation
from discovery_agent.sql.models import CatalogObjectType, DefinitionState, IndexKind, SqlIndex, SqlIndexColumn, SqlModuleDefinition, SqlObjectKey, SqlProcedure

from test_migration import FakeDb, FakeRest, WS, table  # noqa: E402 - shared fixtures

# --- T-SQL rules ---------------------------------------------------------------------

PROC = """CREATE PROC dbo.sp_Load AS
BEGIN
  SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
  -- WITH (DISTRIBUTION = HASH(x)) in a comment stays as it is
  CREATE TABLE stage.t_new WITH (DISTRIBUTION = HASH([OrderId]), CLUSTERED COLUMNSTORE INDEX)
  AS SELECT * FROM stage.t WITH (NOLOCK) WHERE note = 'WITH (HEAP)';
  CREATE TABLE stage.p WITH (HEAP, PARTITION ([d] RANGE RIGHT FOR VALUES (1, 2))) AS SELECT 1 AS d;
  RENAME OBJECT [stage].[t_new] TO [t];
  RENAME OBJECT db1.dbo.x TO y;
  EXEC sp_addrolemember 'largerc', 'loader';
  CREATE WORKLOAD GROUP wg WITH (MIN_PERCENTAGE_RESOURCE = 10, CAP_PERCENTAGE_RESOURCE = 20);
  WITH cte AS (SELECT 1 AS a) SELECT * FROM cte;
  SET TRANSACTION ISOLATION LEVEL SNAPSHOT;
END"""


def test_synapse_only_tsql_is_converted_and_everything_else_is_left_alone():
    out = tsql_rules.rewrite(PROC)
    text = out.text
    assert "WITH (DISTRIBUTION = HASH([OrderId])" not in text.split("/* removed for Fabric:")[0]
    assert "CREATE TABLE stage.t_new /* removed for Fabric: WITH (DISTRIBUTION = HASH([OrderId]), CLUSTERED COLUMNSTORE INDEX) */" in text
    assert "CREATE TABLE stage.p /* removed for Fabric: WITH (HEAP, PARTITION ([d] RANGE RIGHT FOR VALUES (1, 2))) */ AS SELECT 1 AS d" in text
    assert "EXEC sp_rename N'[stage].[t_new]', N't';" in text and "EXEC sp_rename N'dbo.x', N'y';" in text
    assert "/* removed for Fabric: SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; */" in text
    assert "/* removed for Fabric: EXEC sp_addrolemember 'largerc', 'loader'; */" in text
    assert "/* removed for Fabric: CREATE WORKLOAD GROUP wg WITH (MIN_PERCENTAGE_RESOURCE = 10, CAP_PERCENTAGE_RESOURCE = 20); */" in text
    # left alone: a comment, a table hint, a string, a CTE, snapshot isolation
    for kept in ("-- WITH (DISTRIBUTION = HASH(x)) in a comment stays as it is", "FROM stage.t WITH (NOLOCK)", "'WITH (HEAP)'",
                 "WITH cte AS (SELECT 1 AS a)", "SET TRANSACTION ISOLATION LEVEL SNAPSHOT;"):
        assert kept in text
    assert len(out.notes) == 4 and out.changed
    assert tsql_rules.rewrite(text).notes == []  # safe to repeat


def test_text_without_synapse_only_constructs_is_returned_unchanged():
    view = "CREATE VIEW v AS SELECT a FROM t WITH (NOLOCK) WHERE s = 'RENAME OBJECT x TO y'"
    out = tsql_rules.rewrite(view)
    assert out.text == view and not out.changed
    assert tsql_rules.rewrite("").text == "" and tsql_rules.rewrite(None).text == ""


def test_a_with_clause_holding_more_than_storage_options_is_not_touched():
    external = "CREATE EXTERNAL TABLE e (a int) WITH (LOCATION = '/x', DATA_SOURCE = ds, FILE_FORMAT = ff)"
    assert tsql_rules.rewrite(external).text == external


def test_procedures_are_created_from_the_converted_text_and_say_what_changed():
    proc = SqlProcedure(key=SqlObjectKey("pool", "dbo", "sp_Load", CatalogObjectType.PROCEDURE),
                        definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text=PROC))
    db = FakeDb()
    run = MigrationRun("001", [Source("p", "dbo.sp_Load", "Stored Procedure", 1, PROCEDURE, payload=proc, schema="dbo", object_name="sp_Load")],
                       WS, "Sales WS", "pool01")
    Migrator(run, lambda: FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}]), lambda h, d: db).execute()
    item = run.items[0]
    assert item.status == COMPLETED and "converted for Fabric" in item.step and len(item.notes) == 4
    created = next(s for s in db.statements if s.startswith("CREATE PROC"))
    assert "RENAME OBJECT" not in created and "sp_rename" in created


def test_sql_scripts_get_the_same_rules():
    payload = {"properties": {"content": {"query": "RENAME OBJECT dbo.a TO b;", "metadata": {"language": "sql"}}}}
    document, notes = jobs.sql_script_notebook(payload, "wh-1", "pool01")
    assert "".join(document["cells"][0]["source"]) == "EXEC sp_rename N'dbo.a', N'b';"
    assert any("sp_rename" in n for n in notes)


# --- keys --------------------------------------------------------------------------------


def keyed_table():
    pk = SqlIndex(index_id=2, kind=IndexKind.NONCLUSTERED, name="PK_Orders", is_unique=True, is_primary_key=True,
                  columns=(SqlIndexColumn(column_id=1, name="OrderId", key_ordinal=1),))
    uq = SqlIndex(index_id=3, kind=IndexKind.NONCLUSTERED, name=None, is_unique=True, is_unique_constraint=True,
                  columns=(SqlIndexColumn(column_id=2, name="Customer", key_ordinal=1), SqlIndexColumn(column_id=4, name="OrderedAt", key_ordinal=2)))
    plain = SqlIndex(index_id=4, kind=IndexKind.NONCLUSTERED, name="ix", columns=(SqlIndexColumn(column_id=3, name="Amount", key_ordinal=1),))
    base = table()
    return type(base)(key=base.key, distribution=base.distribution, columns=base.columns, indexes=(pk, uq, plain))


def test_primary_keys_and_unique_constraints_are_recreated_not_enforced():
    statements = warehouse_ddl.key_constraints(keyed_table())
    assert statements == [
        "ALTER TABLE [sales].[Orders] ADD CONSTRAINT [PK_Orders] PRIMARY KEY NONCLUSTERED ([OrderId]) NOT ENFORCED;",
        "ALTER TABLE [sales].[Orders] ADD CONSTRAINT [UQ_Orders_3] UNIQUE NONCLUSTERED ([Customer], [OrderedAt]) NOT ENFORCED;",
    ]


def test_a_table_is_created_with_its_keys_and_a_rejected_key_does_not_fail_the_table():
    class Picky(FakeDb):
        def cursor(self):
            cursor = super().cursor()
            execute = cursor.execute

            def guarded(sql, *params):
                if "UNIQUE NONCLUSTERED" in sql:
                    raise RuntimeError("not supported")
                return execute(sql, *params)

            cursor.execute = guarded
            return cursor

    db = Picky()
    run = MigrationRun("001", [Source("t", "sales.Orders", "Table", 1, TABLE, payload=keyed_table(), schema="sales", object_name="Orders")],
                       WS, "Sales WS", "pool01")
    Migrator(run, lambda: FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}]), lambda h, d: db).execute()
    item = run.items[0]
    assert item.status == COMPLETED and "Table data stage" in item.step
    assert any(n.startswith("Key kept as NOT ENFORCED: [PK_Orders]") for n in item.notes)
    assert any(n.startswith("Key not recreated (not supported)") for n in item.notes)


# --- collation -------------------------------------------------------------------------


def test_the_warehouse_collation_follows_the_option_and_the_pools_own_collation():
    assert warehouse_collation("match_synapse", "SQL_Latin1_General_CP1_CI_AS") == CASE_INSENSITIVE
    assert warehouse_collation("match_synapse", "Latin1_General_100_CS_AS") == CASE_SENSITIVE
    assert warehouse_collation("match_synapse", "Latin1_General_100_BIN2") == CASE_SENSITIVE
    assert warehouse_collation("match_synapse", None) == CASE_INSENSITIVE  # Synapse's default
    assert warehouse_collation("case_sensitive", "SQL_Latin1_General_CP1_CI_AS") == CASE_SENSITIVE
    assert warehouse_collation("case_insensitive", "Latin1_General_100_BIN2") == CASE_INSENSITIVE


def test_a_new_warehouse_is_created_with_the_chosen_collation_and_says_so():
    rest = FakeRest(warehouses=[])
    run = MigrationRun("001", [Source("pool", "pool01", "Dedicated SQL Pool", 1, "pool")], WS, "Sales WS", "pool01")
    run.source_collation = "SQL_Latin1_General_CP1_CI_AS"
    Migrator(run, lambda: rest, lambda h, d: FakeDb()).execute()
    body = next(b for p, b in rest.created if p.endswith("/warehouses"))
    assert body["creationPayload"] == {"defaultCollation": CASE_INSENSITIVE}
    assert "case-insensitive, like the Synapse pool" in run.items[0].notes[0]


def test_an_existing_warehouse_keeps_its_collation():
    run = MigrationRun("001", [Source("pool", "pool01", "Dedicated SQL Pool", 1, "pool")], WS, "Sales WS", "pool01")
    Migrator(run, lambda: FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}]), lambda h, d: FakeDb()).execute()
    assert "cannot be changed" in run.items[0].notes[0]


# --- notebook code -----------------------------------------------------------------------

CELL = '''from notebookutils import mssparkutils
import com.microsoft.spark.sqlanalytics
from com.microsoft.spark.sqlanalytics.Constants import Constants
df = spark.read.synapsesql("dedicatedsql.dbo.FactSales")
other = spark.read.option("x", 1).synapsesql("otherpool.dbo.T")
mssparkutils.fs.ls("/x")
w = mssparkutils.env.getWorkspaceName()
df.write.synapsesql("dedicatedsql.dbo.T", Constants.INTERNAL)
'''


def test_python_cells_move_to_notebookutils_and_the_fabric_sql_connector():
    code, notes = notebooks.rewrite_python(CELL, "dedicatedsql", "dedicatedsql_wh")
    assert "from notebookutils import mssparkutils" not in code
    assert 'notebookutils.fs.ls("/x")' in code
    assert "mssparkutils.env.getWorkspaceName()" in code  # flagged for review, not guessed at
    assert "import com.microsoft.spark.fabric" in code and "com.microsoft.spark.fabric.Constants" in code
    assert 'spark.read.synapsesql("dedicatedsql_wh.dbo.FactSales")' in code
    assert 'synapsesql("otherpool.dbo.T")' in code  # another pool: not this migration's Warehouse
    assert 'df.write.synapsesql("dedicatedsql.dbo.T"' in code  # writes differ in Fabric: left for review
    assert len(notes) == 3
    assert notebooks.rewrite_python(code, "dedicatedsql", "dedicatedsql_wh")[1] == []  # safe to repeat


def test_only_python_cells_are_rewritten_and_the_notes_say_what_remains():
    payload = {"properties": {"metadata": {"language_info": {"name": "python"}}, "cells": [
        {"cell_type": "code", "source": CELL},
        {"cell_type": "code", "source": "mssparkutils.fs.ls(\"/y\")", "metadata": {"microsoft": {"language": "scala"}}},
    ]}}
    document, notes = notebooks.to_fabric_ipynb(payload, pool_name="dedicatedsql", warehouse="dedicatedsql_wh")
    assert "notebookutils.fs.ls" in "".join(document["cells"][0]["source"])
    assert "".join(document["cells"][1]["source"]) == 'mssparkutils.fs.ls("/y")'  # Scala: left as it is
    joined = " ".join(notes)
    assert "Rewritten for Fabric" in joined and "synapsesql writes" in joined and "mssparkutils.env" in joined


# --- parameterised linked services --------------------------------------------------------


def test_a_parameterised_linked_service_uses_its_default_values():
    ls = {"name": "ls_param", "properties": {
        "type": "AzureSqlDW",
        "parameters": {"db": {"type": "String", "defaultValue": "pool01"}, "srv": {"type": "String", "defaultValue": "ws.sql.azuresynapse.net"}},
        "typeProperties": {"connectionString": "Server=tcp:@{linkedService().srv},1433;Database=@{linkedService().db};"}}}
    plan = fabric_connections.parse(ls)
    assert plan.unsupported is None
    assert {p["name"]: p["value"] for p in plan.parameters} == {"server": "ws.sql.azuresynapse.net", "database": "pool01"}
    assert "default values" in plan.notes[0]


def test_a_parameter_without_a_default_cannot_become_one_connection():
    ls = {"name": "ls_param", "properties": {"type": "AzureSqlDW", "parameters": {"db": {"type": "String"}},
                                             "typeProperties": {"connectionString": "Server=tcp:x,1433;Database=@{linkedService().db};"}}}
    plan = fabric_connections.parse(ls)
    assert plan.unsupported and "db" in plan.unsupported


def test_an_unsupported_linked_service_says_which_name_to_give_the_hand_made_connection():
    plan = fabric_connections.parse({"name": "ls_func", "properties": {"type": "AzureFunction", "typeProperties": {}}})
    assert "name it 'ls_func'" in plan.unsupported


# --- pipeline activities -----------------------------------------------------------------


def context(connections):
    return pipelines.Context(datasets={}, linked_services={}, connections=connections, notebooks={}, pipelines={}, spark_jobs={},
                             workspace_id=WS, warehouse=None)


def test_external_service_activities_point_at_the_connection_of_the_same_name():
    pipeline = {"properties": {"activities": [
        {"name": "Score", "type": "AzureFunctionActivity", "linkedServiceName": {"referenceName": "ls_func", "type": "LinkedServiceReference"},
         "typeProperties": {"functionName": "score", "method": "POST"}},
        {"name": "Notify", "type": "WebHook", "typeProperties": {"url": "https://example.org/hook", "method": "POST"}},
        {"name": "Train", "type": "DatabricksNotebook", "linkedServiceName": {"referenceName": "ls_dbx", "type": "LinkedServiceReference"},
         "typeProperties": {"notebookPath": "/train"}},
    ]}}
    out = pipelines.convert(pipeline, context({"ls_func": "c-1"}))
    acts = {a["name"]: a for a in out.definition["properties"]["activities"]}
    assert acts["Score"]["externalReferences"] == {"connection": "c-1"} and "linkedServiceName" not in acts["Score"]
    assert acts["Notify"]["type"] == "WebHook"
    assert out.missing == ["connection ls_dbx"] and not out.unsupported


def test_an_activity_that_needs_a_second_linked_service_is_reported_not_guessed():
    pipeline = {"properties": {"activities": [
        {"name": "Hive", "type": "HDInsightHive", "linkedServiceName": {"referenceName": "ls_hdi", "type": "LinkedServiceReference"},
         "typeProperties": {"scriptPath": "s.hql", "scriptLinkedService": {"referenceName": "ls_blob", "type": "LinkedServiceReference"}}},
    ]}}
    out = pipelines.convert(pipeline, context({"ls_hdi": "c-2"}))
    assert out.unsupported and "scriptLinkedService" in out.unsupported[0]
    assert json.dumps(out.definition).count("Hive") == 0
