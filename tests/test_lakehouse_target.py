"""A Fabric Lakehouse as the SQL target: the roads that differ from a Warehouse's.

Offline. Fabric is a fake workspace that runs notebooks and pipelines by recording them, the SQL analytics
endpoint and the Synapse pool are fake DB-API connections, and OneLake is a fake file store. What this cannot
establish is that Spark accepts every converted statement; the notebooks list what they could not convert.
"""

from __future__ import annotations

import base64
import json
import re

import pytest

from discovery_agent.api.migration import git_items, parse_options
from discovery_agent.api.service import ApiError
from discovery_agent.migration import datapipeline, lakehouse_ddl, notebooks, pipelines, spark_sql, warehouse_ddl
from discovery_agent.migration.common import (
    COMPLETED, DATA, DEFERRED_STATUS, FAILED, PIPELINE, POOL, PROCEDURE, SCHEMA, SCRIPT, SKIPPED, TABLE, VIEW, Source, lakehouse_name,
)
from discovery_agent.migration.preflight import content_findings
from discovery_agent.migration.runner import MigrationRun, Migrator
from discovery_agent.migration.validation import MATCH, MISMATCH, REVIEW, Validator
from discovery_agent.sql.models import (
    CatalogObjectType, DefinitionState, DistributionPolicy, SqlColumn, SqlIndex, SqlIndexColumn, SqlModuleDefinition, SqlObjectKey,
    SqlParameter, SqlProcedure, SqlTable, SqlView,
)

WS = "ws-1"
LAKE = "pool01"
ORDERS = "[sales].[Orders]"


def table(schema="sales", name="Orders", columns=None, indexes=()):
    columns = columns or (
        SqlColumn(1, "OrderId", "int", 4, 10, 0, False, True),
        SqlColumn(2, "Customer", "nvarchar", 200, 0, 0, True, False),
        SqlColumn(3, "Amount", "money", 8, 19, 4, True, False),
        SqlColumn(4, "OrderedAt", "datetime", 8, 23, 3, False, False),
    )
    return SqlTable(key=SqlObjectKey("pool", schema, name), distribution=DistributionPolicy.HASH, columns=columns, indexes=indexes)


def procedure(text, schema="sales", name="usp_Load", params=()):
    return SqlProcedure(key=SqlObjectKey("pool", schema, name, CatalogObjectType.PROCEDURE),
                        definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text=text), parameters=tuple(params))


def view(text="CREATE VIEW sales.vOrders AS SELECT OrderId FROM sales.Orders"):
    return SqlView(key=SqlObjectKey("pool", "sales", "vOrders", CatalogObjectType.VIEW),
                   definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text=text))


def code_of(document):
    return "\n".join("".join(c["source"]) for c in document["cells"] if c["cell_type"] == "code")


def decode(body):
    return json.loads(base64.b64decode(body["definition"]["parts"][0]["payload"]))


# =============================================================================
# fakes
# =============================================================================


class LakeRest:
    """A fake workspace with a Lakehouse, notebooks that run, and pipelines that load an endpoint's tables."""

    def __init__(self, endpoint_db=None, source_rows=None, lakehouses=(), default_schema=True, notebook_outcome="Completed"):
        self.store = {"lakehouses": [dict(x) for x in lakehouses], "notebooks": [], "dataPipelines": [], "warehouses": []}
        self.endpoint_db = endpoint_db
        self.source_rows = dict(source_rows or {})
        self.default_schema = default_schema
        self.notebook_outcome = notebook_outcome
        self.created, self.updated, self.runs = [], [], []

    def list(self, path):
        if path == "/connections":
            return [{"id": "conn-1", "displayName": "synapse-ws-pool01", "connectionDetails": {"path": "ws.sql.azuresynapse.net;pool01"}}]
        return list(self.store.get(path.split("/")[3], []))

    def get(self, path):
        if "/jobs/instances/" in path:
            return {"status": self.notebook_outcome if "nb" in path else "Completed"}
        if "/lakehouses/" in path:
            props = {"sqlEndpointProperties": {"id": "sql-1", "connectionString": "tcp:lake.datawarehouse.fabric.microsoft.com,1433",
                                               "provisioningStatus": "Success"}}
            if self.default_schema:
                props["defaultSchema"] = "dbo"
            return {"properties": props}
        return {}

    def patch(self, path, body):
        return {}

    def create(self, path, body, what=""):
        self.created.append((path, body))
        if path.endswith("/updateDefinition"):
            self.updated.append((path, body))
            return {}
        if path.endswith("/refreshMetadata"):
            return {}
        coll = path.split("/")[3]
        item = {"id": f"{coll}-{len(self.store[coll]) + 1}", "displayName": body.get("displayName")}
        self.store[coll].append(item)
        return {"id": item["id"]}

    def run_job(self, path, body):
        self.runs.append((path, body))
        if "jobType=Pipeline" in path:
            for e in body["executionData"]["parameters"]["tables"]:
                key = f"[{e['schema']}].[{e['table']}]"
                if e["tableAction"] == "Overwrite":
                    self.endpoint_db.tables[key] = []
                self.endpoint_db.tables[key].extend(self.source_rows.get(key, []))
            return f"/workspaces/{WS}/items/pipe/jobs/instances/job-{len(self.runs)}"
        return f"/workspaces/{WS}/items/nb/jobs/instances/job-{len(self.runs)}"

    def created_named(self, name):
        return next(b for p, b in self.created if isinstance(b, dict) and b.get("displayName") == name)


class FakeOneLake:
    """OneLake: the table notebook's report. Each statement succeeds and creates its table on the endpoint, unless failed."""

    def __init__(self, rest, endpoint_db, fail=(), existing=()):
        self.rest, self.db, self.fail, self.existing = rest, endpoint_db, set(fail), set(existing)
        self.reads = []

    def read(self, workspace, lakehouse, path):
        self.reads.append(path)
        body = [b for p, b in self.rest.created if b.get("displayName", "").startswith("ddl_")][-1]
        namespace = {}
        exec(code_of(decode(body)).split("import json")[0], namespace)  # the statements list cell only
        report = {}
        for key, name, _sql in namespace["statements"]:
            if key in self.fail:
                report[key] = {"ok": False, "error": "[PARSE_SYNTAX_ERROR] boom"}
                continue
            existed = name in self.existing
            report[key] = {"ok": True, "existed": existed}
            if name:
                schema, table_name = name.split(".")
                self.db.tables.setdefault(f"[{schema}].[{table_name}]", [])
        return json.dumps(report).encode()


class EndpointDb:
    """The SQL analytics endpoint: tables as the Delta log shows them, and every T-SQL statement it ran."""

    def __init__(self, tables=None):
        self.tables = {k: list(v) for k, v in (tables or {}).items()}
        self.statements = []

    def cursor(self):
        db = self

        class Cursor:
            _row = None

            def execute(self, sql, *params):
                db.statements.append(sql)
                if sql == warehouse_ddl.EXISTS_QUERY:
                    self._row = (1,) if params[0] in db.tables else (None,)
                elif sql.startswith("SELECT COUNT_BIG"):
                    self._row = (len(db.tables[sql.split("FROM ")[1]]),)
                else:
                    self._row = (None,)

            def fetchone(self):
                return self._row

            def fetchall(self):
                return []

        return Cursor()

    def close(self):
        pass


class SourceCounts:
    def __init__(self, counts):
        self.counts = counts

    def cursor(self):
        counts = self.counts

        class Cursor:
            _row = None

            def execute(self, sql, *params):
                self._row = (counts.get(sql.split("FROM ")[1].split(" WHERE")[0], 0),)

            def fetchone(self):
                return self._row

        return Cursor()

    def close(self):
        pass


def lakehouse_run(sources, rest=None, db=None, onelake="auto", settings=None, source=None):
    db = db or EndpointDb()
    rest = rest or LakeRest(db)
    rest.endpoint_db = db
    files = FakeOneLake(rest, db) if onelake == "auto" else onelake
    run = MigrationRun("007", sources, WS, "Sales WS", LAKE)
    run.pool_name, run.source_server, run.source_database, run.source_connection_name = "pool01", "ws.sql.azuresynapse.net", "pool01", "synapse-ws-pool01"
    run.settings.update({"target": "lakehouse", **(settings or {})})
    connects = []

    def sql_factory(host, database):
        connects.append((host, database))
        return db

    Migrator(run, lambda: rest, sql_factory, source_factory=(lambda: source) if source else None, onelake=files).execute()
    return run, rest, db, connects


def items(run):
    return {i.source.name: i for i in run.items}


# =============================================================================
# Delta tables
# =============================================================================


def test_a_table_becomes_a_delta_table_with_spark_types_and_says_what_it_loses():
    keyed = table(indexes=(SqlIndex(2, name="PK_Orders", is_primary_key=True, columns=(SqlIndexColumn(1, "OrderId", 1),)),))
    sql, notes = lakehouse_ddl.create_table(keyed)

    assert sql.startswith("CREATE TABLE IF NOT EXISTS `sales`.`Orders` (")
    assert "`OrderId` INT NOT NULL" in sql and "`Customer` STRING" in sql and "`Amount` DECIMAL(19,4)" in sql
    assert "`OrderedAt` TIMESTAMP NOT NULL" in sql and sql.rstrip().endswith("USING DELTA")
    assert any("IDENTITY not kept" in n for n in notes)
    assert any("Primary key" in n for n in notes) and any("Distribution" in n for n in notes)
    assert any("money" in n for n in notes)


def test_column_names_delta_cannot_hold_switch_on_column_mapping():
    sql, notes = lakehouse_ddl.create_table(table(columns=(SqlColumn(1, "Order Id", "int", 4, 10, 0, True, False),)))
    assert "'delta.columnMapping.mode' = 'name'" in sql and any("column mapping" in n for n in notes)


def test_types_spark_cannot_hold_are_refused_and_endpoint_types_are_what_validation_expects():
    with pytest.raises(lakehouse_ddl.UnsupportedColumn):
        lakehouse_ddl.create_table(table(columns=(SqlColumn(1, "V", "sql_variant", 8016, 0, 0, True, False),)))
    assert lakehouse_ddl.endpoint_type(SqlColumn(1, "A", "decimal", 9, 18, 2, True, False)) == "decimal(18,2)"
    assert lakehouse_ddl.endpoint_type(SqlColumn(1, "B", "datetime", 8, 23, 3, True, False)) == "datetime2(6)"
    assert lakehouse_ddl.endpoint_type(SqlColumn(1, "C", "bit", 1, 1, 0, True, False)) == "bit"


# =============================================================================
# T-SQL procedures -> Spark notebooks
# =============================================================================

PROC = """
CREATE PROCEDURE [sales].[usp_Load] @RunDate date = NULL, @Region nvarchar(20) = N'EU', @Rows int OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    IF OBJECT_ID('tempdb..#stage') IS NOT NULL DROP TABLE #stage;
    BEGIN TRY
        BEGIN TRAN;
        SELECT o.OrderId, ISNULL(o.Customer, 'n/a') AS Customer, LEN(o.Note) AS NoteLen
        INTO #stage
        FROM [pool01].[sales].[Orders] o WITH (NOLOCK)
        WHERE o.OrderedAt >= @RunDate AND o.Region = @Region;

        UPDATE sales.Fact SET Amount = st.Amount, UpdatedAt = GETDATE()
        FROM #stage st WHERE sales.Fact.OrderId = st.OrderId;

        INSERT INTO sales.Fact (OrderId, Customer) SELECT TOP 100 st.OrderId, st.Customer FROM #stage st;
        SET @Rows = @@ROWCOUNT;
        EXEC sales.usp_Audit @Proc = 'usp_Load', @Rows = @Rows;
        COMMIT;
    END TRY
    BEGIN CATCH
        ROLLBACK;
        THROW;
    END CATCH
    RETURN 0;
END
"""


def convert(text=PROC, **kw):
    return spark_sql.procedure_notebook(text, name="sales_usp_Load", lakehouse_id="lh-1", lakehouse=LAKE, workspace_id=WS,
                                        pool="pool01", notebooks={"sales.usp_audit": "sp_sales_usp_Audit"}, **kw)


def test_a_procedure_becomes_a_notebook_attached_to_the_lakehouse_with_its_parameters_as_the_parameters_cell():
    out = convert()
    doc = out.document
    deps = doc["metadata"]["dependencies"]["lakehouse"]
    assert deps == {"default_lakehouse": "lh-1", "default_lakehouse_name": LAKE, "default_lakehouse_workspace_id": WS}
    params = next(c for c in doc["cells"] if c["metadata"].get("tags") == ["parameters"])
    assert "".join(params["source"]).splitlines() == [
        "rundate = None  # @rundate date", "region = 'EU'  # @region nvarchar(20)", "rows = None  # @rows int OUTPUT"]
    compile(code_of(doc), "notebook", "exec")  # the generated Python is valid
    assert out.review == []


def test_the_body_runs_in_spark_with_variables_as_named_parameters():
    code = code_of(convert().document)
    # SELECT INTO a temp table -> a work table in tmp_work, dropped at the end even when the procedure fails.
    assert "CREATE OR REPLACE TABLE tmp_work.sales_usp_load_stage USING DELTA AS SELECT o.OrderId, coalesce(o.Customer, 'n/a') AS Customer, length(rtrim(o.Note)) AS NoteLen" in code
    assert "FROM `sales`.`Orders` o\n" in code and "NOLOCK" not in code  # the pool's name and the hint are gone
    assert "o.OrderedAt >= :rundate AND o.Region = :region" in code
    assert "spark.catalog.tableExists('tmp_work.sales_usp_load_stage')" in code
    assert 'spark.sql("DROP TABLE IF EXISTS tmp_work.sales_usp_load_stage")' in code.split("finally:")[1]
    # UPDATE ... FROM -> MERGE; TOP -> LIMIT; @@ROWCOUNT from the Delta metrics.
    assert "MERGE INTO sales.Fact\nUSING tmp_work.sales_usp_load_stage AS `st`\nON (sales.Fact.OrderId = st.OrderId)\nWHEN MATCHED THEN UPDATE SET `Amount` = st.Amount, `UpdatedAt` = current_timestamp()" in code
    assert "FROM tmp_work.sales_usp_load_stage st LIMIT 100" in code
    assert 'v["rows"] = scalar(r"SELECT :rowcount")' in code
    # EXEC of a migrated procedure -> run its notebook; TRY/CATCH -> try/except; RETURN -> the exit value.
    assert "notebookutils.notebook.run('sp_sales_usp_Audit', 3600, {\"proc\": 'usp_Load', \"rows\": v[\"rows\"]})" in code
    assert "except Exception as exc:  # BEGIN CATCH" in code and "        raise\n" in code
    assert "return 0" in code and 'notebookutils.notebook.exit(json.dumps({"rows": v.get("rows"), "return_value": result}' in code
    assert "BEGIN TRAN" not in code.replace("# removed: BEGIN TRAN", "")


def test_what_it_cannot_convert_is_marked_for_review_not_guessed():
    out = convert("""CREATE PROCEDURE dbo.p AS
        DECLARE @sql nvarchar(max) = N'SELECT 1';
        EXEC sp_executesql @sql;
        EXEC dbo.not_migrated 1;
        UPDATE a SET a.x = b.x FROM dbo.A a JOIN dbo.B b ON a.k = b.k JOIN dbo.C c ON c.k = b.k;
        SELECT SCOPE_IDENTITY();""")
    code = code_of(out.document)
    assert len(out.review) == 4 and code.count("# REVIEW:") == 3
    assert any("Dynamic SQL" in r for r in out.review) and any("not_migrated" in r for r in out.review)
    assert any("several joined tables" in r for r in out.review) and any("SCOPE_IDENTITY" in r for r in out.review)
    assert "Review before running" in "".join(out.document["cells"][0]["source"])
    compile(code, "notebook", "exec")


@pytest.mark.parametrize("tsql, spark", [
    ("SELECT DATEDIFF(day, @a, @b)", "datediff(CAST(:b AS DATE), CAST(:a AS DATE))"),
    ("SELECT DATEADD(mm, 1, d) FROM t", "dateadd(MONTH, 1, d)"),
    ("SELECT CONVERT(varchar(10), x) FROM t", "CAST(x AS STRING)"),
    ("SELECT CHARINDEX('a', s) FROM t", "locate('a', s)"),
    ("SELECT IIF(x > 1, 'it''s', NEWID()) FROM t", "if(x > 1, 'it\\'s', uuid())"),
    ("SELECT 'a' + name FROM t", "'a' || name"),
    ("SELECT CAST(x AS decimal(10, 2)) FROM t", "CAST(x AS DECIMAL(10,2))"),
])
def test_t_sql_expressions_become_spark_sql(tsql, spark):
    code = code_of(convert(f"CREATE PROCEDURE dbo.p @a date, @b date AS {tsql};").document)
    assert spark in code


def test_control_flow_and_cursors_become_python():
    code = code_of(convert("""CREATE PROCEDURE dbo.p AS
        DECLARE @id int, @n int = 0;
        DECLARE c CURSOR FOR SELECT Id FROM dbo.T;
        OPEN c;
        FETCH NEXT FROM c INTO @id;
        WHILE @@FETCH_STATUS = 0
        BEGIN
            IF @id > 10 BREAK;
            SET @n += 1;
            FETCH NEXT FROM c INTO @id;
        END
        CLOSE c; DEALLOCATE c;
        IF @n = 0 RAISERROR('none', 16, 1) ELSE PRINT 'done';""").document)
    assert "cursor_c = iter(q(r\"SELECT Id FROM dbo.T\").collect())" in code
    assert 'while truth(r":fetch_status = 0"):' in code and "            break" in code
    assert 'v["n"] = scalar(r"SELECT :n + (1)")' in code
    assert "raise Exception('none')" in code and "print('done')" in code
    compile(code, "notebook", "exec")


def test_parameter_names_are_valid_python_and_pipeline_parameters_follow_them():
    out = convert("CREATE PROCEDURE dbo.p @from date, @class int = 3 AS SELECT @from, @class;")
    params = "".join(next(c for c in out.document["cells"] if c["metadata"].get("tags")).get("source"))
    assert "p_from = None" in params and "p_class = 3" in params
    compile(code_of(out.document), "notebook", "exec")
    assert spark_sql.notebook_parameters({"from": {"value": "2026-01-01", "type": "Datetime"}, "class": {"value": 3, "type": "Int32"}}) == {
        "p_from": {"value": "2026-01-01", "type": "string"}, "p_class": {"value": 3, "type": "int"}}


def test_scripts_that_only_read_are_told_apart_from_scripts_that_change_data():
    assert not spark_sql.changes_data("SELECT * FROM t WHERE note = 'INSERT here' -- DELETE later")
    assert spark_sql.changes_data("SELECT a INTO #t FROM s")
    assert spark_sql.changes_data("UPDATE t SET a = 1")


# =============================================================================
# a run into a Lakehouse
# =============================================================================


def test_the_pool_becomes_a_lakehouse_with_schemas_and_tables_are_made_by_one_notebook_run():
    sources = [
        Source("pool", "pool01", "Dedicated SQL Pool", 1, POOL),
        Source("s1", "sales", "Schema", 1, SCHEMA, schema="sales"),
        Source("t1", "sales.Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders"),
        Source("t2", "mart.Days", "Table", 2, TABLE, payload=table("mart", "Days"), schema="mart", object_name="Days"),
    ]
    run, rest, db, connects = lakehouse_run(sources)
    got = items(run)

    lake_body = rest.created_named(LAKE)
    assert lake_body["creationPayload"] == {"enableSchemas": True} and lakehouse_name(LAKE, run.settings) == LAKE
    assert got["pool01"].status == COMPLETED and got["pool01"].step == "Fabric Lakehouse created (with schemas)"
    assert any("case-sensitive" in n for n in got["pool01"].notes)
    assert connects == [("lake.datawarehouse.fabric.microsoft.com", LAKE)]
    # One notebook run created the schema and both tables (and the mart schema the second table needs).
    notebook_runs = [p for p, _ in rest.runs if "RunNotebook" in p]
    assert len(notebook_runs) == 1
    statements = code_of(decode(rest.created_named("ddl_pool01")))
    assert "CREATE SCHEMA IF NOT EXISTS `mart`" in statements and "CREATE TABLE IF NOT EXISTS `sales`.`Orders`" in statements
    assert [got[n].status for n in ("sales", "sales.Orders", "mart.Days")] == [COMPLETED, COMPLETED, COMPLETED]
    assert got["sales.Orders"].step.startswith("Table created as a Delta table") and got["sales.Orders"].target == "pool01.sales.Orders"
    assert any("IDENTITY not kept" in n for n in got["sales.Orders"].notes)


def test_a_statement_spark_rejects_fails_only_its_table_and_existing_tables_are_left_alone():
    db = EndpointDb()
    rest = LakeRest(db)
    files = FakeOneLake(rest, db, fail={"t2"}, existing={"sales.Orders"})
    sources = [Source("t1", "sales.Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders"),
               Source("t2", "sales.Bad", "Table", 1, TABLE, payload=table(name="Bad"), schema="sales", object_name="Bad")]
    run, *_ = lakehouse_run(sources, rest=rest, db=db, onelake=files)
    got = items(run)
    assert got["sales.Orders"].status == SKIPPED and got["sales.Bad"].status == FAILED
    assert "PARSE_SYNTAX_ERROR" in got["sales.Bad"].error


def test_without_the_notebooks_report_tables_are_looked_for_on_the_endpoint():
    db = EndpointDb({ORDERS: []})
    run, *_ = lakehouse_run([Source("t1", "sales.Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders")],
                            db=db, onelake=None)
    assert items(run)["sales.Orders"].status == COMPLETED


def test_a_failed_table_notebook_fails_every_table_with_its_reason():
    db = EndpointDb()
    rest = LakeRest(db, notebook_outcome="Failed")
    run, *_ = lakehouse_run([Source("t1", "sales.Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders")],
                            rest=rest, db=db)
    assert items(run)["sales.Orders"].status == FAILED and "Run status: Failed" in items(run)["sales.Orders"].error


def test_an_existing_lakehouse_without_schemas_is_refused_rather_than_flattened():
    db = EndpointDb()
    rest = LakeRest(db, lakehouses=[{"id": "lh-9", "displayName": LAKE}], default_schema=False)
    run, *_ = lakehouse_run([Source("pool", "pool01", "Dedicated SQL Pool", 1, POOL)], rest=rest, db=db)
    assert items(run)["pool01"].status == FAILED and "without schemas" in items(run)["pool01"].error


def test_views_go_to_the_sql_endpoint_after_a_metadata_refresh_and_procedures_become_notebooks():
    db = EndpointDb({ORDERS: []})
    sources = [
        Source("v1", "sales.vOrders", "View", 1, VIEW, payload=view(), schema="sales", object_name="vOrders"),
        Source("p1", "sales.usp_Load", "Stored Procedure", 1, PROCEDURE, payload=procedure(PROC), schema="sales", object_name="usp_Load"),
        Source("p2", "sales.usp_Audit", "Stored Procedure", 1, PROCEDURE,
               payload=procedure("CREATE PROCEDURE sales.usp_Audit @Proc varchar(50), @Rows int AS INSERT INTO sales.Audit VALUES (@Proc, @Rows)",
                                 name="usp_Audit", params=(SqlParameter(1, "@Proc", "varchar"), SqlParameter(2, "@Rows", "int"))),
               schema="sales", object_name="usp_Audit"),
    ]
    run, rest, db, _ = lakehouse_run(sources, db=db)
    got = items(run)
    assert any(p.endswith("/sqlEndpoints/sql-1/refreshMetadata") for p, _ in rest.created)
    assert got["sales.vOrders"].status == COMPLETED and any(s.startswith("CREATE VIEW sales.vOrders") for s in db.statements)
    load = got["sales.usp_Load"]
    assert load.status == COMPLETED and load.step == "Converted to a Spark notebook" and load.target == "Sales WS / sp_sales_usp_Load"
    assert any("read-only" in n for n in load.notes)
    code = code_of(decode(rest.created_named("sp_sales_usp_Load")))
    assert "notebookutils.notebook.run('sp_sales_usp_Audit'" in code  # the EXEC found the other procedure's notebook
    assert not any("CREATE PROCEDURE" in s for s in db.statements)  # nothing was tried on the read-only endpoint


def test_a_reading_script_stays_t_sql_on_the_endpoint_and_a_writing_one_becomes_spark():
    def script(name, query):
        return Source(name, name, "SQL Script", 1, SCRIPT, payload={"properties": {"content": {"query": query, "metadata": {"language": "sql"}}}})

    run, rest, _, _ = lakehouse_run([script("Report", "SELECT TOP 10 * FROM sales.Orders"), script("Fix", "UPDATE sales.Orders SET Amount = 0 WHERE Amount < 0")])
    report, fix = decode(rest.created_named("Report")), decode(rest.created_named("Fix"))
    assert report["metadata"]["dependencies"]["warehouse"]["known_warehouses"] == [{"id": "sql-1", "type": "Lakewarehouse"}]
    assert fix["metadata"]["dependencies"]["lakehouse"]["default_lakehouse_name"] == LAKE
    assert 'q(r"UPDATE sales.Orders SET Amount = 0 WHERE Amount < 0")' in code_of(fix)
    assert items(run)["Fix"].step == "Converted to a Spark notebook"


def test_table_data_is_loaded_into_delta_tables_and_counted_on_the_endpoint():
    db = EndpointDb({ORDERS: []})
    rest = LakeRest(db, source_rows={ORDERS: [1, 2, 3]})
    source = SourceCounts({ORDERS: 3})
    run, rest, db, _ = lakehouse_run([Source("d1", "sales.Orders (data)", "Table data", 1, DATA, payload=table(), schema="sales", object_name="Orders")],
                                     rest=rest, db=db, source=source)
    item = run.items[0]
    assert item.status == COMPLETED and item.step == "Loaded 3 rows by pipeline; counts match"
    content = decode(rest.created_named("load_pool01_lakehouse_wave_1"))
    copy = content["properties"]["activities"][0]["typeProperties"]["activities"][0]
    sink = copy["typeProperties"]["sink"]
    assert sink["type"] == "LakehouseTableSink" and sink["datasetSettings"]["linkedService"]["properties"]["type"] == "Lakehouse"
    assert sink["tableActionOption"] == {"value": "@item().tableAction", "type": "Expression"}
    assert [a["name"] for a in content["properties"]["activities"][0]["typeProperties"]["activities"]] == ["Copy table"]


def test_replacing_rows_overwrites_the_delta_table():
    db = EndpointDb({ORDERS: [9, 9]})
    rest = LakeRest(db, source_rows={ORDERS: [1, 2, 3]})
    run, rest, db, _ = lakehouse_run([Source("d1", "sales.Orders (data)", "Table data", 1, DATA, payload=table(), schema="sales", object_name="Orders")],
                                     rest=rest, db=db, source=SourceCounts({ORDERS: 3}), settings={"dataMode": "replace"})
    assert run.items[0].status == COMPLETED and db.tables[ORDERS] == [1, 2, 3]
    entry = next(b for p, b in rest.runs if "Pipeline" in p)["executionData"]["parameters"]["tables"][0]
    assert entry["tableAction"] == "Overwrite"


# =============================================================================
# pipelines, notebooks, options
# =============================================================================


def _context(procedures=None):
    pool_ls = {"properties": {"type": "AzureSqlDW", "typeProperties": {"connectionString": "Server=ws.sql.azuresynapse.net;Database=pool01"}}}
    datasets = {"OrdersDs": {"properties": {"linkedServiceName": {"referenceName": "PoolLs"}, "type": "AzureSqlDWTable",
                                            "typeProperties": {"schema": "sales", "table": "Orders"}}}}
    return pipelines.Context(datasets=datasets, linked_services={"PoolLs": pool_ls}, connections={"Blob": "c-1"}, notebooks={}, pipelines={},
                             spark_jobs={}, workspace_id=WS, warehouse=None, pool_name="pool01",
                             lakehouse=pipelines.Lakehouse("lh-1", LAKE, WS), procedure_notebooks=procedures or {})


def test_a_procedure_call_runs_the_notebook_it_became_with_the_same_parameter_values():
    act = {"name": "Load", "type": "SqlPoolStoredProcedure", "typeProperties": {
        "storedProcedureName": "[sales].[usp_Load]", "storedProcedureParameters": {"RunDate": {"value": "@pipeline().parameters.d", "type": "Datetime"},
                                                                                  "Batch": {"value": 5, "type": "Int32"}}}}
    out = pipelines.convert({"properties": {"activities": [act]}}, _context({"sales.usp_load": "nb-7"}))
    converted = out.definition["properties"]["activities"][0]
    assert converted["type"] == "TridentNotebook" and converted["typeProperties"]["notebookId"] == "nb-7"
    assert converted["typeProperties"]["parameters"] == {"rundate": {"value": "@pipeline().parameters.d", "type": "string"},
                                                         "batch": {"value": 5, "type": "int"}}
    assert out.missing == [] and out.unsupported == []


def test_copies_of_pool_tables_read_and_write_lakehouse_tables_but_queries_and_scripts_need_a_rewrite():
    copy = {"name": "Copy", "type": "Copy", "inputs": [{"referenceName": "OrdersDs"}], "outputs": [{"referenceName": "OrdersDs"}],
            "typeProperties": {"source": {"type": "SqlDWSource"}, "sink": {"type": "SqlDWSink", "preCopyScript": "TRUNCATE TABLE sales.Orders"}}}
    out = pipelines.convert({"properties": {"activities": [copy]}}, _context())
    tp = out.definition["properties"]["activities"][0]["typeProperties"]
    assert tp["source"]["type"] == "LakehouseTableSource" and tp["sink"] == {
        "type": "LakehouseTableSink", "tableActionOption": "Overwrite", "datasetSettings": tp["sink"]["datasetSettings"]}
    assert tp["sink"]["datasetSettings"]["type"] == "LakehouseTable" and out.unsupported == []

    query = json.loads(json.dumps(copy))
    query["typeProperties"]["source"]["sqlReaderQuery"] = "SELECT * FROM sales.Orders WHERE x = 1"
    script = {"name": "Clean", "type": "Script", "linkedServiceName": {"referenceName": "PoolLs"}, "typeProperties": {"scripts": []}}
    out = pipelines.convert({"properties": {"activities": [query, script]}}, _context())
    assert len(out.unsupported) == 2 and "query" in out.unsupported[0] and "Script activity" in out.unsupported[1]


def test_notebook_reads_of_the_pool_read_the_lakehouse_tables():
    payload = {"properties": {"metadata": {"language_info": {"name": "python"}}, "cells": [
        {"cell_type": "code", "source": ["df = spark.read.option('x', 1).synapsesql(\"pool01.sales.Orders\")\n"]}]}}
    document, notes = notebooks.to_fabric_ipynb(payload, pool_name="pool01", warehouse=LAKE, lakehouse=("lh-1", LAKE, WS))
    assert "".join(document["cells"][0]["source"]) == 'df = spark.read.table("sales.Orders")\n'
    assert document["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] == "lh-1"
    assert any("spark.read.table" in n for n in notes)


def test_the_target_option_is_validated_and_a_lakehouse_run_commits_the_lakehouse_and_its_notebooks():
    assert parse_options({"options": {}})["target"] == "warehouse"
    assert parse_options({"options": {"target": "lakehouse"}})["target"] == "lakehouse"
    with pytest.raises(ApiError):
        parse_options({"options": {"target": "kql"}})
    run = MigrationRun("001", [Source("t", "sales.Orders", "Table", 1, TABLE, schema="sales", object_name="Orders"),
                               Source("p", "sales.usp_Load", "Stored Procedure", 1, PROCEDURE, schema="sales", object_name="usp_Load"),
                               Source("v", "sales.v", "View", 1, VIEW, schema="sales", object_name="v")], WS, "W", LAKE)
    run.settings["target"] = "lakehouse"
    for item in run.items:
        item.status = COMPLETED
    assert git_items(run) == {("Lakehouse", LAKE), ("Notebook", "sp_sales_usp_Load"), ("Notebook", "ddl_pool01")}


def test_preflight_predicts_the_lakehouse_roads():
    sources = [
        Source("t1", "sales.Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders"),
        Source("p1", "dbo.p", "Stored Procedure", 1, PROCEDURE, payload=procedure("CREATE PROCEDURE dbo.p AS EXEC sp_executesql N'x'", "dbo", "p"),
               schema="dbo", object_name="p"),
    ]
    found = {f.code: f for f in content_findings(sources, target="lakehouse")}
    assert "IDENTITY not kept" in found["TYPE_CONVERSION"].message
    assert found["NOTEBOOK_REVIEW"].severity == "MEDIUM" and "Dynamic SQL" in found["NOTEBOOK_REVIEW"].message
    assert "NOTEBOOK_REVIEW" not in {f.code for f in content_findings(sources)}  # a Warehouse keeps the procedure as it is


class ValidationRest:
    def __init__(self, notebooks_=()):
        self.notebooks = notebooks_

    def list(self, path):
        if path.endswith("/lakehouses"):
            return [{"id": "lh-1", "displayName": LAKE}]
        if path.endswith("/notebooks"):
            return [{"id": f"nb-{n}", "displayName": n} for n in self.notebooks]
        return []

    def get(self, path):
        return {"properties": {"sqlEndpointProperties": {"connectionString": "lake.datawarehouse.fabric.microsoft.com"}}}


def test_validation_reads_the_lakehouse_endpoint_and_looks_for_procedures_as_notebooks():
    connects = []

    def sql_factory(host, database):
        connects.append((host, database))
        return EndpointDb()

    sources = [Source("pool", "pool01", "Dedicated SQL Pool", 1, POOL),
               Source("p1", "sales.usp_Load", "Stored Procedure", 1, PROCEDURE, payload=procedure(PROC), schema="sales", object_name="usp_Load"),
               Source("p2", "sales.usp_Gone", "Stored Procedure", 1, PROCEDURE, payload=procedure(PROC, name="usp_Gone"), schema="sales", object_name="usp_Gone")]
    rows = Validator(ValidationRest(["sp_sales_usp_Load"]), WS, "W", LAKE, sql_factory, target="lakehouse").run(sources)
    assert connects == [("lake.datawarehouse.fabric.microsoft.com", LAKE)]
    assert [r["status"] for r in rows] == [MATCH, REVIEW, MISMATCH]
    assert rows[0]["target"] == f"Lakehouse {LAKE}" and rows[1]["target"] == "notebook sp_sales_usp_Load"


# =============================================================================
# LLM-assisted transforms follow the target
# =============================================================================

from test_transform import OID, PROMPTS, FakeModel, FakeSession  # noqa: E402

from discovery_agent.api.transform import TransformService  # noqa: E402
from discovery_agent.llm import checks, context  # noqa: E402
from discovery_agent.llm.store import TransformStore  # noqa: E402

LAKE_PROC_DRAFT = json.dumps({"language": "python", "cells": [
    {"cell_type": "markdown", "source": "# sales.LoadOrders"},
    {"cell_type": "code", "source": "run_date = None", "tags": ["parameters"]},
    {"cell_type": "code", "source": "spark.sql('INSERT INTO sales.x SELECT 1 AS a')\nnotebookutils.notebook.exit('{}')"},
]})


def transforms(tmp_path, model=None):
    return TransformService(FakeSession(), model=model or FakeModel(), store=TransformStore(tmp_path / "t"), prompts=PROMPTS,
                            synchronous=True)


def test_the_rule_output_and_prompt_follow_the_target(tmp_path):
    svc = transforms(tmp_path)
    warehouse = svc.object(OID)
    lakehouse = svc.object(OID, {"target": "lakehouse"})
    assert warehouse["source"]["outputLanguage"] == "sql" and "CREATE TABLE" in warehouse["source"]["ruleOutput"]
    assert lakehouse["target"] == "lakehouse" and lakehouse["source"]["outputLanguage"] == "json"
    cells = json.loads(lakehouse["source"]["ruleOutput"])["cells"]
    assert any("def run():" in c["source"] for c in cells) and any(c.get("tags") == ["parameters"] for c in cells)
    prompt = svc.prompt(OID, {"target": "lakehouse"})["prompt"]
    assert "PySpark notebook" in prompt and "Fabric Lakehouse" in prompt and "| Fabric Warehouse |" not in prompt
    assert "PySpark notebook" not in svc.prompt(OID)["prompt"]


def test_a_table_rule_output_is_delta_ddl_for_a_lakehouse():
    subject = context.extract(Source("t", "sales.Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders"),
                              "lakehouse")
    assert subject.rule_output.startswith("CREATE TABLE IF NOT EXISTS `sales`.`Orders`") and "USING DELTA" in subject.rule_output


def test_drafts_and_accepted_transforms_are_kept_per_target_and_a_run_uses_its_own(tmp_path):
    svc = transforms(tmp_path)
    svc.generate({"ids": [OID]})  # a Warehouse draft (T-SQL)
    svc.accept({"id": OID})
    lake = transforms(tmp_path, FakeModel(LAKE_PROC_DRAFT))
    lake.generate({"ids": [OID], "target": "lakehouse"})
    assert lake.object(OID, {"target": "lakehouse"})["state"] == "draft"
    assert lake.object(OID)["state"] == "accepted"  # the Warehouse's accepted transform is untouched

    def sources():
        return [Source(OID, "sales.LoadOrders", "Stored Procedure", 1, PROCEDURE, payload=FakeSession().proc,
                       schema="sales", object_name="LoadOrders")]

    unused = sources()
    log = lake.apply(unused, "lakehouse")
    assert unused[0].override is None and "made for a Warehouse" in log[0]

    lake.accept({"id": OID, "target": "lakehouse"})
    for target, starts in (("warehouse", "CREATE PROCEDURE"), ("lakehouse", "{")):
        chosen = sources()
        lake.apply(chosen, target)
        assert chosen[0].override["code"].startswith(starts)


def test_a_lakehouse_draft_is_checked_as_a_spark_notebook_or_a_delta_table():
    assert not checks.blocking(checks.run(PROCEDURE, LAKE_PROC_DRAFT, [], None, "lakehouse"))
    bad = json.dumps({"cells": [{"cell_type": "code", "source": "BEGIN TRAN\nUPDATE x"}]})
    found = checks.run(PROCEDURE, bad, [], None, "lakehouse")
    assert checks.blocking(found) and any("not valid Python" in c["message"] for c in found)
    assert checks.blocking(checks.run(TABLE, "CREATE TABLE [dbo].[t] (a int IDENTITY)", [], None, "lakehouse"))
    assert not checks.blocking(checks.run(TABLE, "CREATE TABLE IF NOT EXISTS `dbo`.`t` (a INT) USING DELTA", [], None, "lakehouse"))
    # The same T-SQL procedure is fine for a Warehouse, and not a notebook for a Lakehouse.
    assert checks.blocking(checks.run(PROCEDURE, "CREATE PROCEDURE dbo.p AS SELECT 1", [], None, "lakehouse"))


def test_a_lakehouse_run_creates_the_accepted_notebook_and_the_accepted_delta_table():
    proc = Source("p1", "sales.usp_Load", "Stored Procedure", 1, PROCEDURE, payload=procedure(PROC), schema="sales", object_name="usp_Load",
                  override={"code": LAKE_PROC_DRAFT, "acceptedAt": "2026-10-23T10:00:00+00:00", "edited": False})
    tbl = Source("t1", "sales.Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders",
                 override={"code": "CREATE TABLE IF NOT EXISTS `sales`.`Orders` (`OrderId` BIGINT) USING DELTA", "acceptedAt": "x", "edited": True})
    run, rest, _, _ = lakehouse_run([proc, tbl])
    got = items(run)
    assert got["sales.usp_Load"].step == "Created as a Spark notebook (LLM-assisted, reviewed)"
    document = decode(rest.created_named("sp_sales_usp_Load"))
    assert [c["cell_type"] for c in document["cells"]] == ["markdown", "code", "code"]
    assert document["cells"][1]["metadata"] == {"tags": ["parameters"]}
    assert document["metadata"]["dependencies"]["lakehouse"]["default_lakehouse_name"] == LAKE
    assert "`OrderId` BIGINT) USING DELTA" in code_of(decode(rest.created_named("ddl_pool01")))
    assert got["sales.Orders"].status == COMPLETED and "LLM-assisted" in got["sales.Orders"].notes[0]
