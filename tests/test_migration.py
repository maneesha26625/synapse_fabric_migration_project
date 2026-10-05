"""The migration run: notebooks and SQL schema into Fabric, everything else deferred.

Offline. Fabric is a fake REST client or a fake HTTP sender, and the Warehouse
is a fake DB-API connection, so nothing here reaches Azure. What these tests
cannot establish -- that Fabric accepts this exact notebook document, and that
a real Warehouse accepts each translated statement -- needs a live run.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from types import SimpleNamespace

import pytest

from discovery_agent.api.migration import MigrationService, sources_for
from discovery_agent.api.service import ApiError
from discovery_agent.migration import notebooks, warehouse_ddl
from discovery_agent.migration.fabric_rest import FabricApiError, FabricRestClient
from discovery_agent.migration.runner import (
    COMPLETED, DEFERRED, DEFERRED_STATUS, FAILED, MIGRATABLE_TYPES, NOTEBOOK, PENDING, POOL, PROCEDURE,
    SCHEMA, SCHEMA_EXISTS_QUERY, SKIPPED, TABLE, VIEW, MigrationRun, Migrator, Source, warehouse_name_for,
)
from discovery_agent.source_strategy import P0Artifact
from discovery_agent.sql.models import (
    CatalogObjectType, DefinitionState, DistributionPolicy, SqlColumn, SqlModuleDefinition,
    SqlObjectKey, SqlProcedure, SqlTable, SqlView,
)

WS = "11111111-2222-3333-4444-555555555555"


# --- fixtures -----------------------------------------------------------------


def notebook_payload(**props):
    base = {
        "nbformat": 4, "nbformat_minor": 2,
        "bigDataPool": {"referenceName": "sparkpool01", "type": "BigDataPoolReference"},
        "sessionProperties": {"driverMemory": "28g", "numExecutors": 2},
        "metadata": {"language_info": {"name": "python"}, "kernelspec": {"name": "synapse_pyspark"}},
        "cells": [
            {"cell_type": "markdown", "source": ["# Load sales\n"], "metadata": {}},
            {"cell_type": "code", "source": ["df = spark.read.parquet('abfss://raw@acct.dfs.core.windows.net/x')\n", "display(df)"],
             "metadata": {"microsoft": {"language": "python"}}, "outputs": [{"output_type": "stream", "text": ["hi"]}], "execution_count": 3},
        ],
    }
    base.update(props)
    return {"name": "LoadSales", "properties": base}


def table(schema="sales", name="Orders", columns=None, distribution=DistributionPolicy.HASH):
    columns = columns or (
        SqlColumn(1, "OrderId", "int", 4, 10, 0, False, True),
        SqlColumn(2, "Customer", "nvarchar", 200, 0, 0, True, False),
        SqlColumn(3, "Amount", "money", 8, 19, 4, True, False),
        SqlColumn(4, "OrderedAt", "datetime", 8, 23, 3, False, False),
    )
    return SqlTable(key=SqlObjectKey("pool", schema, name), distribution=distribution, columns=columns)


def view(schema="sales", name="vOrders", text="CREATE VIEW sales.vOrders AS SELECT OrderId FROM sales.Orders"):
    state = DefinitionState.AVAILABLE if text else DefinitionState.OPAQUE
    return SqlView(key=SqlObjectKey("pool", schema, name, CatalogObjectType.VIEW), definition=SqlModuleDefinition(state=state, text=text))


class FakeRest:
    def __init__(self, notebooks=(), warehouses=(), fail_create=None, endpoint="abc.datawarehouse.fabric.microsoft.com", capacity="cap-1"):
        self.notebooks = list(notebooks)
        self.warehouses = [dict(w) for w in warehouses]
        self.fail_create = fail_create
        self.endpoint = endpoint
        self.capacity = capacity
        self.created = []
        self.calls = []

    def list(self, path):
        self.calls.append(("list", path))
        if path.endswith("/notebooks"):
            return [{"displayName": n} for n in self.notebooks]
        if path.endswith("/warehouses"):
            return list(self.warehouses)
        return []

    def get(self, path):
        self.calls.append(("get", path))
        if path == f"/workspaces/{WS}":
            return {"id": WS, "displayName": "Sales WS", **({"capacityId": self.capacity} if self.capacity else {})}
        return {"properties": {"connectionString": self.endpoint}}

    def create(self, path, body):
        self.calls.append(("create", path))
        if self.fail_create:
            raise self.fail_create
        self.created.append((path, body))
        if path.endswith("/warehouses"):
            self.warehouses.append({"id": "wh-1", "displayName": body["displayName"]})
        return {}


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._row = None

    def execute(self, sql, *params):
        self.db.statements.append(sql)
        if sql in (warehouse_ddl.EXISTS_QUERY, SCHEMA_EXISTS_QUERY):
            self._row = (1,) if params[0] in self.db.existing else (None,)
            return
        if self.db.reject and self.db.reject in sql:
            raise RuntimeError("Incorrect syntax near 'x'")

    def fetchone(self):
        return self._row


class FakeDb:
    def __init__(self, existing=(), reject=None):
        self.existing = set(existing)
        self.reject = reject
        self.statements = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def run_with(sources, rest=None, db=None, warehouse="pool01"):
    rest = rest or FakeRest(warehouses=[{"id": "wh-1", "displayName": warehouse}])
    db = db or FakeDb()
    connects = []
    run = MigrationRun("001", sources, WS, "Sales WS", warehouse)

    def sql_factory(host, database):
        connects.append((host, database))
        return db

    Migrator(run, lambda: rest, sql_factory).execute()
    return run, rest, db, connects


def by_name(run):
    return {i.source.name: i for i in run.items}


# --- notebooks ----------------------------------------------------------------


def test_a_notebook_keeps_its_cells_and_drops_what_only_synapse_understands():
    document, notes = notebooks.to_fabric_ipynb(notebook_payload())

    assert document["nbformat"] == 4 and document["metadata"]["language_info"]["name"] == "python"
    assert [c["cell_type"] for c in document["cells"]] == ["markdown", "code"]
    code = document["cells"][1]
    assert code["source"][0].startswith("df = spark.read")
    assert code["outputs"] == [] and code["execution_count"] is None
    assert code["metadata"] == {"microsoft": {"language": "python"}}
    assert "bigDataPool" not in json.dumps(document) and "sessionProperties" not in json.dumps(document)
    joined = " ".join(notes)
    assert "sparkpool01" in joined and "session size" in joined and "outputs were not copied" in joined
    assert "ADLS paths" in joined


@pytest.mark.parametrize("synapse, fabric", [("scala", "scala"), ("sparksql", "sql"), ("r", "r"), ("pyspark", "python")])
def test_the_notebook_language_is_carried_over(synapse, fabric):
    payload = notebook_payload(metadata={"language_info": {"name": synapse}})
    assert notebooks.to_fabric_ipynb(payload)[0]["metadata"]["language_info"]["name"] == fabric


def test_a_dotnet_notebook_is_not_migratable():
    with pytest.raises(notebooks.NotebookNotMigratable, match="C#"):
        notebooks.to_fabric_ipynb(notebook_payload(metadata={"language_info": {"name": "csharp"}}))


def test_synapse_only_apis_are_flagged_for_review_not_rewritten():
    cells = [{"cell_type": "code", "source": "token = TokenLibrary.getConnectionString('ls')"}]
    document, notes = notebooks.to_fabric_ipynb(notebook_payload(cells=cells))
    assert document["cells"][0]["source"] == ["token = TokenLibrary.getConnectionString('ls')"]
    assert any("TokenLibrary" in n for n in notes)


def test_the_create_body_carries_the_document_as_base64_ipynb():
    document, _ = notebooks.to_fabric_ipynb(notebook_payload())
    body = notebooks.create_body("LoadSales", document, "desc")
    part = body["definition"]["parts"][0]
    assert body["displayName"] == "LoadSales" and body["definition"]["format"] == "ipynb"
    assert part["path"] == "notebook-content.ipynb" and part["payloadType"] == "InlineBase64"
    assert json.loads(base64.b64decode(part["payload"])) == document


# --- warehouse DDL ------------------------------------------------------------


@pytest.mark.parametrize("column, expected, noted", [
    (SqlColumn(1, "a", "int"), "int", False),
    (SqlColumn(1, "a", "decimal", 9, 18, 2), "decimal(18,2)", False),
    (SqlColumn(1, "a", "tinyint"), "smallint", True),
    (SqlColumn(1, "a", "money"), "decimal(19,4)", True),
    (SqlColumn(1, "a", "datetime"), "datetime2(3)", True),
    (SqlColumn(1, "a", "datetime2", 8, 27, 7), "datetime2(6)", True),
    (SqlColumn(1, "a", "datetimeoffset", 10, 34, 7), "datetime2(6)", True),
    (SqlColumn(1, "a", "varchar", 50), "varchar(50)", False),
    (SqlColumn(1, "a", "varchar", -1), "varchar(MAX)", False),
    (SqlColumn(1, "a", "nvarchar", 200), "varchar(400)", True),
    (SqlColumn(1, "a", "nvarchar", 8000), "varchar(MAX)", True),
    (SqlColumn(1, "a", "nchar", 20), "varchar(40)", True),
    (SqlColumn(1, "a", "ntext"), "varchar(MAX)", True),
    (SqlColumn(1, "a", "binary", 16), "varbinary(16)", True),
    (SqlColumn(1, "a", "uniqueidentifier"), "uniqueidentifier", False),
])
def test_types_map_to_what_fabric_warehouse_supports(column, expected, noted):
    sql_type, note = warehouse_ddl.map_type(column)
    assert sql_type == expected
    assert bool(note) is noted


def test_a_type_with_no_equivalent_is_refused():
    with pytest.raises(warehouse_ddl.UnsupportedColumn, match="sql_variant"):
        warehouse_ddl.map_type(SqlColumn(1, "a", "sql_variant"))


def test_a_table_is_rebuilt_from_its_columns_without_storage_clauses():
    sql, notes = warehouse_ddl.create_table(table())
    assert sql.startswith("CREATE TABLE [sales].[Orders] (")
    assert "[OrderId] int NOT NULL" in sql and "[Customer] varchar(400) NULL" in sql
    assert "DISTRIBUTION" not in sql.upper() and "INDEX" not in sql.upper() and "IDENTITY" not in sql.upper()
    assert any("IDENTITY dropped" in n for n in notes)
    assert any("Distribution (hash)" in n for n in notes)


def test_names_are_quoted_safely():
    assert warehouse_ddl.qualified("s]x", "t") == "[s]]x].[t]"
    assert warehouse_ddl.ensure_schema("dbo") is None
    assert warehouse_ddl.ensure_schema("o'brien") == "IF SCHEMA_ID(N'o''brien') IS NULL EXEC(N'CREATE SCHEMA [o''brien]');"


def test_the_warehouse_is_named_after_the_pool():
    assert warehouse_name_for("SalesDW") == "SalesDW"
    assert warehouse_name_for("sales-dw 01") == "sales_dw_01"
    assert warehouse_name_for("1pool") == "wh_1pool"
    assert warehouse_name_for(None) == "SynapseMigration"


# --- the REST client ----------------------------------------------------------


class FakeSender:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, dict(headers), body))
        status, hdrs, payload = self.responses.pop(0)
        return status, hdrs, json.dumps(payload).encode()


def client(sender):
    return FabricRestClient(lambda: "tok", send=sender, sleep=lambda s: None)


def test_a_201_create_returns_the_item_and_sends_the_token_only_in_a_header():
    sender = FakeSender([(201, {}, {"id": "nb-1"})])
    assert client(sender).create(f"/workspaces/{WS}/notebooks", {"displayName": "x"}) == {"id": "nb-1"}
    method, url, headers, body = sender.requests[0]
    assert method == "POST" and url == f"https://api.fabric.microsoft.com/v1/workspaces/{WS}/notebooks"
    assert headers["Authorization"] == "Bearer tok" and b"tok" not in body


def test_a_202_create_is_polled_until_it_succeeds():
    op = "https://api.fabric.microsoft.com/v1/operations/op-1"
    sender = FakeSender([
        (202, {"location": op, "retry-after": "1"}, {}),
        (200, {"retry-after": "1"}, {"status": "Running"}),
        (200, {}, {"status": "Succeeded"}),
        (200, {}, {"id": "nb-2"}),
    ])
    assert client(sender).create("/workspaces/w/notebooks", {}) == {"id": "nb-2"}
    assert sender.requests[-1][1] == op + "/result"


def test_a_failed_operation_raises_with_fabrics_reason():
    sender = FakeSender([(202, {"location": "https://api.fabric.microsoft.com/v1/operations/o"}, {}),
                         (200, {}, {"status": "Failed", "error": {"errorCode": "InvalidDefinition", "message": "bad notebook"}})])
    with pytest.raises(FabricApiError, match="bad notebook") as exc:
        client(sender).create("/x", {})
    assert exc.value.code == "InvalidDefinition"


def test_throttling_is_retried_then_succeeds():
    sender = FakeSender([(429, {"retry-after": "2"}, {}), (200, {}, {"value": [{"id": 1}]})])
    assert client(sender).list("/workspaces/w/notebooks") == [{"id": 1}]


def test_a_link_outside_fabric_is_never_followed():
    sender = FakeSender([(202, {"location": "https://evil.example.com/op"}, {})])
    with pytest.raises(FabricApiError) as exc:
        client(sender).create("/x", {})
    assert exc.value.code == "unexpected_host"
    assert len(sender.requests) == 1


def test_a_regional_redirect_link_is_polled_on_the_fabric_host_instead():
    op = "11111111-2222-3333-4444-555555555555"
    sender = FakeSender([
        (202, {"location": f"https://wabi-india-central-a-primary-redirect.analysis.windows.net/v1/operations/{op}"}, {}),
        (200, {}, {"status": "Succeeded"}),
        (200, {}, {"id": "wh-1"}),
    ])
    assert client(sender).create("/workspaces/w/warehouses", {}) == {"id": "wh-1"}
    assert all(url.startswith("https://api.fabric.microsoft.com/v1/") for _, url, _, _ in sender.requests)
    assert sender.requests[1][1].endswith(f"/operations/{op}")


def test_listing_follows_continuation_tokens():
    sender = FakeSender([(200, {}, {"value": [{"id": 1}], "continuationToken": "a b"}), (200, {}, {"value": [{"id": 2}]})])
    assert [i["id"] for i in client(sender).list("/workspaces/w/notebooks")] == [1, 2]
    assert sender.requests[1][1].endswith("continuationToken=a%20b")


def test_an_http_error_carries_fabrics_error_code():
    sender = FakeSender([(400, {}, {"errorCode": "ItemDisplayNameAlreadyInUse", "message": "taken"})])
    with pytest.raises(FabricApiError) as exc:
        client(sender).create("/x", {})
    assert exc.value.code == "ItemDisplayNameAlreadyInUse"


# --- the run ------------------------------------------------------------------


def src(name, kind, wave=1, payload=None, type_=None, schema="sales", reason=None):
    type_ = type_ or {NOTEBOOK: "Notebook", TABLE: "Table", VIEW: "View", PROCEDURE: "Stored Procedure", DEFERRED: "Pipeline"}[kind]
    return Source(name, name, type_, wave, kind, payload=payload, schema=schema, object_name=name.split(".")[-1], reason=reason)


def test_notebooks_are_created_and_existing_ones_skipped():
    rest = FakeRest(notebooks=["Existing"])
    run, rest, _, _ = run_with([src("LoadSales", NOTEBOOK, payload=notebook_payload()), src("Existing", NOTEBOOK, payload=notebook_payload())], rest=rest)
    items = by_name(run)
    assert items["LoadSales"].status == COMPLETED and items["LoadSales"].target == "Sales WS / LoadSales"
    assert items["Existing"].status == SKIPPED
    assert [p for p, _ in rest.created] == [f"/workspaces/{WS}/notebooks"]
    assert run.state == "completed"


def test_tables_views_and_procedures_go_to_the_warehouse_in_dependency_order():
    proc = SqlProcedure(key=SqlObjectKey("pool", "sales", "uspLoad", CatalogObjectType.PROCEDURE),
                        definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text="CREATE PROC sales.uspLoad AS SELECT 1"))
    sources = [
        src("sales.uspLoad", PROCEDURE, payload=proc),
        src("sales.vOrders", VIEW, payload=view()),
        src("sales.Orders", TABLE, payload=table()),
    ]
    run, _, db, connects = run_with(sources)
    assert [i.source.kind for i in run.items] == [TABLE, VIEW, PROCEDURE]
    assert all(i.status == COMPLETED for i in run.items)
    creates = [s for s in db.statements if s.startswith("CREATE")]
    assert creates[0].startswith("CREATE TABLE [sales].[Orders]") and creates[1].startswith("CREATE VIEW") and creates[2].startswith("CREATE PROC")
    assert connects == [("abc.datawarehouse.fabric.microsoft.com", "pool01")]  # one connection per pass
    assert by_name(run)["sales.Orders"].target == "pool01.sales.Orders"
    assert db.closed


def test_an_existing_sql_object_is_skipped_not_replaced():
    db = FakeDb(existing={"[sales].[Orders]"})
    run, _, db, _ = run_with([src("sales.Orders", TABLE, payload=table())], db=db)
    assert run.items[0].status == SKIPPED
    assert not any(s.startswith("CREATE") for s in db.statements)


def test_a_missing_warehouse_is_created_first():
    rest = FakeRest(warehouses=[])
    run, rest, _, _ = run_with([src("sales.Orders", TABLE, payload=table())], rest=rest)
    assert run.items[0].status == COMPLETED
    assert rest.created[0][0] == f"/workspaces/{WS}/warehouses" and rest.created[0][1]["displayName"] == "pool01"


def test_a_rejected_statement_fails_that_object_only():
    db = FakeDb(reject="vOrders")
    run, _, _, _ = run_with([src("sales.vOrders", VIEW, payload=view()), src("sales.Orders", TABLE, payload=table())], db=db)
    items = by_name(run)
    assert items["sales.Orders"].status == COMPLETED
    assert items["sales.vOrders"].status == FAILED and "Incorrect syntax" in items["sales.vOrders"].error


def test_an_unreadable_definition_fails_with_the_reason():
    run, _, _, _ = run_with([src("sales.vSecret", VIEW, payload=view(name="vSecret", text=None))])
    assert run.items[0].status == FAILED and "VIEW DEFINITION" in run.items[0].error


def test_a_warehouse_that_cannot_be_reached_fails_each_sql_object_once_without_retrying_it():
    rest = FakeRest(warehouses=[], fail_create=FabricApiError(403, "InsufficientPrivileges", "no"))
    sources = [src("sales.Orders", TABLE, payload=table()), src("sales.Lines", TABLE, payload=table(name="Lines"))]
    run, rest, _, _ = run_with(sources, rest=rest)
    assert all(i.status == FAILED for i in run.items)
    assert sum(1 for c in rest.calls if c[0] == "create") == 1


def test_everything_else_is_deferred_with_a_reason_and_never_touched():
    rest = FakeRest()
    sources = [src("CopySales", DEFERRED, type_="Pipeline", reason="later"), src("LoadSales", NOTEBOOK, payload=notebook_payload())]
    run, rest, _, _ = run_with(sources, rest=rest)
    items = by_name(run)
    assert items["CopySales"].status == DEFERRED_STATUS and items["CopySales"].error == "later"
    assert run.to_dict()["deferred"] == 1 and run.to_dict()["completed"] == 1


def test_a_dotnet_notebook_is_deferred_not_failed():
    payload = notebook_payload(metadata={"language_info": {"name": "csharp"}})
    run, rest, _, _ = run_with([src("DotNet", NOTEBOOK, payload=payload)])
    assert run.items[0].status == DEFERRED_STATUS and not rest.created


def test_an_unexpected_error_on_one_object_does_not_stop_the_run():
    class Boom(FakeRest):
        def list(self, path):
            raise KeyError("x")
    run, _, _, _ = run_with([src("A", NOTEBOOK, payload=notebook_payload()), src("sales.Orders", TABLE, payload=table())], rest=Boom())
    assert all(i.status == FAILED for i in run.items)
    assert run.state == "completed"


def test_the_migratable_types_are_the_ones_chosen_for_this_session():
    assert set(MIGRATABLE_TYPES) == {
        "Dedicated SQL Pool", "Schema", "Table", "View", "Stored Procedure", "Notebook", "Spark Pool", "Linked Service",
        "Pipeline", "Dataset", "Spark Job Definition", "SQL Script", "Trigger", "External Table"}
    assert "Integration Runtime" not in MIGRATABLE_TYPES  # always by hand


def test_the_pool_becomes_the_warehouse_and_schemas_are_created_once():
    rest = FakeRest(warehouses=[])
    db = FakeDb(existing={"staging"})
    sources = [
        Source("pool", "dedicatedsql", "Dedicated SQL Pool", 1, POOL),
        Source("s1", "dedicatedsql.stage", "Schema", 1, SCHEMA, schema="stage"),
        Source("s2", "dedicatedsql.staging", "Schema", 1, SCHEMA, schema="staging"),
        src("stage.Trips", TABLE, payload=table(schema="stage", name="Trips"), schema="stage"),
    ]
    run, rest, db, _ = run_with(sources, rest=rest, db=db)
    items = by_name(run)
    assert [i.source.kind for i in run.items][:3] == [POOL, SCHEMA, SCHEMA]
    assert items["dedicatedsql"].status == COMPLETED and items["dedicatedsql"].step == "Fabric Warehouse created"
    assert items["dedicatedsql.stage"].status == COMPLETED
    assert items["dedicatedsql.staging"].status == SKIPPED
    assert items["stage.Trips"].status == COMPLETED
    assert sum(1 for p, _ in rest.created if p.endswith("/warehouses")) == 1


def test_an_existing_warehouse_is_reused():
    run, rest, _, _ = run_with([Source("pool", "dedicatedsql", "Dedicated SQL Pool", 1, POOL)])
    assert run.items[0].status == COMPLETED and "reused" in run.items[0].step
    assert not rest.created


def test_a_workspace_without_capacity_fails_once_then_fast():
    error = FabricApiError(403, "FeatureNotAvailable", "This workspace is not on a Fabric capacity (Fabric: FeatureNotAvailable)")
    rest = FakeRest(warehouses=[], fail_create=error)
    sources = [src("A", NOTEBOOK, payload=notebook_payload()), src("sales.Orders", TABLE, payload=table()),
               src("sales.Lines", TABLE, payload=table(name="Lines"))]
    run, rest, _, _ = run_with(sources, rest=rest)
    assert all(i.status == FAILED and "Fabric capacity" in i.error for i in run.items)
    assert sum(1 for c in rest.calls if c[0] == "create") == 1  # nothing tried after the first refusal


def test_feature_not_available_is_explained_as_a_capacity_problem():
    sender = FakeSender([(403, {}, {"errorCode": "FeatureNotAvailable", "message": "The feature is not available"})])
    with pytest.raises(FabricApiError) as exc:
        client(sender).create("/x", {})
    assert "not on a Fabric capacity" in exc.value.message and "License info" in exc.value.message
    assert "Contributor" not in exc.value.message


# --- the service: plan -> sources, preconditions, pause/resume/retry -----------


def fake_job():
    nb_record = SimpleNamespace(identity=SimpleNamespace(name="LoadSales", schema=None), content=None)
    tb = table()
    tb_record = SimpleNamespace(identity=SimpleNamespace(name="Orders", schema="sales"), content=tb)
    items = [
        {"id": "synapse://notebook/LoadSales", "name": "LoadSales", "type": "Notebook"},
        {"id": "sql://ws/pool/sales/Orders", "name": "sales.Orders", "type": "Table"},
        {"id": "synapse://pipeline/Copy", "name": "Copy", "type": "Pipeline"},
    ]
    artifact = SimpleNamespace(artifact=P0Artifact.NOTEBOOK, name="LoadSales", payload=notebook_payload())
    return SimpleNamespace(
        items=items,
        records_by_id={"synapse://notebook/LoadSales": nb_record, "sql://ws/pool/sales/Orders": tb_record,
                       "synapse://pipeline/Copy": SimpleNamespace(identity=SimpleNamespace(name="Copy", schema=None), content=None)},
        run=SimpleNamespace(synapse=SimpleNamespace(artifacts=(artifact,))),
        graph={
            "nodes": [
                {"id": "synapse://notebook/LoadSales", "name": "LoadSales", "type": "Notebook", "classification": "DIRECT", "fabricTarget": "Fabric Notebook", "wave": 4, "dependedOnBy": 0},
                {"id": "sql://ws/pool/sales/Orders", "name": "sales.Orders", "type": "Table", "classification": "TRANSFORM", "fabricTarget": "Warehouse table", "wave": 2, "dependedOnBy": 1},
                {"id": "synapse://pipeline/Copy", "name": "Copy", "type": "Pipeline", "classification": "RECONFIGURE", "fabricTarget": "Data pipeline", "wave": 5, "dependedOnBy": 0},
            ],
            "edges": [{"source": "synapse://notebook/LoadSales", "target": "sql://ws/pool/sales/Orders"}],
        },
    )


def test_plan_entries_become_sources_with_their_definitions():
    plan = [{"id": "synapse://notebook/LoadSales", "wave": 4}, {"id": "sql://ws/pool/sales/Orders", "wave": 2},
            {"id": "synapse://pipeline/Copy", "wave": 5}, {"id": "gone", "wave": 1}]
    sources = {s.id: s for s in sources_for(plan, fake_job(), "pool01")}
    assert sources["synapse://notebook/LoadSales"].kind == NOTEBOOK and sources["synapse://notebook/LoadSales"].payload["name"] == "LoadSales"
    orders = sources["sql://ws/pool/sales/Orders"]
    assert orders.kind == TABLE and orders.schema == "sales" and orders.object_name == "Orders" and orders.wave == 2
    assert sources["synapse://pipeline/Copy"].kind == "pipeline"
    assert sources["gone"].kind == "missing"


class FakeSession:
    def __init__(self, job=None):
        self.job = job

    def migration_snapshot(self):
        if self.job is None:
            raise ApiError(409, "no_results", "There are no discovery results yet.")
        return self.job, "pool01"


class FakeFabric:
    def __init__(self, ready=True):
        self.ready = ready

    def state(self):
        return {"status": "connected" if self.ready else "disconnected", "workspaceName": "Sales WS", "capacityAssigned": True}

    def migration_target(self):
        from discovery_agent.api.fabric import FabricError
        if not self.ready:
            raise FabricError(409, "target_not_connected", "Connect the Fabric target first.")
        return WS, "Sales WS", "fabric_cli"


def wait_until(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def service(job=None, ready=True, rest=None, db=None):
    rest = rest or FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}])
    db = db or FakeDb()
    return MigrationService(FakeSession(job), FakeFabric(ready), rest_factory=lambda: rest, sql_factory=lambda h, d: db)


def test_a_run_needs_a_plan_a_discovery_and_a_connected_fabric_target():
    from discovery_agent.api.fabric import FabricError
    with pytest.raises(ApiError) as empty:
        service(fake_job()).start({"items": []})
    assert empty.value.code == "empty_plan"
    with pytest.raises(ApiError) as nodisc:
        service(None).start({"items": [{"id": "x", "wave": 1}]})
    assert nodisc.value.code == "no_results"
    with pytest.raises(FabricError) as nofab:
        service(fake_job(), ready=False).start({"items": [{"id": "x", "wave": 1}]})
    assert nofab.value.code == "target_not_connected"


def test_a_started_run_finishes_in_the_background_and_reports_every_object():
    svc = service(fake_job())
    first = svc.start({"items": [{"id": "synapse://notebook/LoadSales", "wave": 4}, {"id": "sql://ws/pool/sales/Orders", "wave": 1},
                                 {"id": "synapse://pipeline/Copy", "wave": 5}], "options": {"stages": ["warehouse", "notebooks"]}})
    assert first["runId"] == "001" and first["warehouse"] == "pool01"
    assert wait_until(lambda: svc.state()["state"] == "completed")
    state = svc.state()
    assert (state["completed"], state["deferred"], state["failed"]) == (2, 1, 0)
    assert [i["name"] for i in state["items"]] == ["sales.Orders", "LoadSales", "Copy"]  # wave order
    assert any("DONE" in line for line in state["logs"])


def test_pause_stops_before_the_next_object_and_resume_continues():
    gate = threading.Event()

    class SlowRest(FakeRest):
        def create(self, path, body):
            gate.wait(5)
            return super().create(path, body)

    rest = SlowRest()
    svc = service(fake_job(), rest=rest)
    svc.start({"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}, {"id": "sql://ws/pool/sales/Orders", "wave": 2}],
               "options": {"stages": ["warehouse", "notebooks"]}})
    svc.control({"action": "pause"})
    gate.set()
    assert wait_until(lambda: svc.state()["state"] == "paused")
    assert svc.state()["pending"] == 1
    svc.control({"action": "resume"})
    assert wait_until(lambda: svc.state()["state"] == "completed")
    assert svc.state()["completed"] == 2


def test_retry_runs_only_the_failed_objects_again():
    db = FakeDb(reject="CREATE TABLE")
    svc = service(fake_job(), db=db)
    svc.start({"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}, {"id": "sql://ws/pool/sales/Orders", "wave": 1}],
               "options": {"stages": ["warehouse", "notebooks"]}})
    assert wait_until(lambda: svc.state()["state"] == "completed")
    assert svc.state()["failed"] == 1
    db.reject = None
    svc.control({"action": "retry"})
    assert wait_until(lambda: svc.state()["state"] == "completed" and svc.state()["failed"] == 0)
    assert svc.state()["completed"] == 2


def test_only_one_run_at_a_time():
    gate = threading.Event()

    class SlowRest(FakeRest):
        def list(self, path):
            gate.wait(5)
            return super().list(path)

    svc = service(fake_job(), rest=SlowRest())
    svc.start({"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}]})
    with pytest.raises(ApiError) as busy:
        svc.start({"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}]})
    assert busy.value.code == "run_in_progress"
    gate.set()
    assert wait_until(lambda: svc.state()["state"] == "completed")


def test_a_workspace_without_a_fabric_capacity_is_refused_before_anything_runs():
    rest = FakeRest(capacity=None)
    svc = service(fake_job(), rest=rest)
    with pytest.raises(ApiError) as exc:
        svc.start({"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}]})
    assert exc.value.code == "no_fabric_capacity" and "License info" in exc.value.message
    assert svc.state()["state"] == "idle" and not rest.created


def test_schema_and_pool_items_become_warehouse_steps():
    job = fake_job()
    job.items += [
        {"id": "synapse://dedicated_sql_pool/pool01", "name": "pool01", "type": "Dedicated SQL Pool"},
        {"id": "synapse://dedicated_sql_pool/other", "name": "other", "type": "Dedicated SQL Pool"},
        {"id": "sql://pool01/stage", "name": "pool01.stage", "type": "Schema"},
    ]
    job.extras_by_id = {
        "synapse://dedicated_sql_pool/pool01": SimpleNamespace(name="pool01", metadata={}),
        "synapse://dedicated_sql_pool/other": SimpleNamespace(name="other", metadata={}),
        "sql://pool01/stage": SimpleNamespace(name="pool01.stage", metadata={"schemaName": "stage"}),
    }
    plan = [{"id": i["id"], "wave": 1} for i in job.items[-3:]]
    sources = {s.id: s for s in sources_for(plan, job, "pool01")}
    assert sources["synapse://dedicated_sql_pool/pool01"].kind == POOL
    assert sources["synapse://dedicated_sql_pool/other"].kind == DEFERRED and "pool01" in sources["synapse://dedicated_sql_pool/other"].reason
    assert sources["sql://pool01/stage"].kind == SCHEMA and sources["sql://pool01/stage"].schema == "stage"


def test_the_fabric_cli_transport_sends_through_fab_api_with_the_body_in_a_json_file(monkeypatch, tmp_path):
    from discovery_agent.api import fabric as fabric_module

    seen = {}

    def fake_run(args, timeout=60, raw=False):
        seen["args"] = list(args)
        path = args[args.index("-i") + 1]
        seen["body"] = json.loads(open(path, encoding="utf-8").read())
        seen["path"] = path
        return 0, json.dumps({"status_code": 202, "headers": {"Location": "https://api.fabric.microsoft.com/v1/operations/o1", "Retry-After": "2"}, "text": "(Empty)"})

    monkeypatch.setattr(fabric_module, "_require", lambda cli, name: "fab")
    monkeypatch.setattr(fabric_module, "_run", fake_run)
    target = fabric_module.FabricTarget()
    status, headers, body = target._fab_send("POST", f"https://api.fabric.microsoft.com/v1/workspaces/{WS}/notebooks?x=1", {}, b'{"displayName": "A"}')

    assert seen["args"][:6] == ["fab", "api", f"workspaces/{WS}/notebooks", "-X", "post", "--show_headers"]
    assert seen["args"][seen["args"].index("-P") + 1] == "x=1"
    assert seen["body"] == {"displayName": "A"}
    assert not __import__("os").path.exists(seen["path"])  # the temp file is removed
    assert status == 202 and headers["location"].endswith("/operations/o1") and body == b""


def test_fabric_cli_is_accepted_for_migration():
    from discovery_agent.api import fabric as fabric_module
    target = fabric_module.FabricTarget()
    target.status, target.method, target.workspace_id, target.workspace_name = "connected", "fabric_cli", WS, "Sales WS"
    assert target.migration_target() == (WS, "Sales WS", "fabric_cli")


def test_the_idle_state_has_every_field_the_page_reads():
    state = service(fake_job()).state()
    assert state["state"] == "idle" and state["items"] == [] and "deferred" in state and "skipped" in state
    assert PENDING == "PENDING"


# --- spark pool -> custom pool + environment ------------------------------------

from discovery_agent.migration import environments  # noqa: E402
from discovery_agent.migration.runner import ENVIRONMENT  # noqa: E402

#: The real dedicated pool from the sample workspace, as discovery records it.
SPARK_POOL = {
    "sparkVersion": "3.5", "nodeSize": "Small", "nodeSizeFamily": "MemoryOptimized", "nodeCount": 10,
    "autoScale": {"enabled": True, "minNodeCount": 3, "maxNodeCount": 3},
    "autoPause": {"enabled": True, "delayInMinutes": 15},
    "dynamicExecutorAllocation": {"enabled": False},
}


class EnvRest(FakeRest):
    def __init__(self, environments_=(), pools=(), fail_pool=None, fail_publish=None, **kw):
        super().__init__(**kw)
        self.environments = list(environments_)
        self.pools = list(pools)
        self.fail_pool = fail_pool
        self.fail_publish = fail_publish
        self.patched = []

    def list(self, path):
        if path.endswith("/environments"):
            return [{"id": "env-1", "displayName": n} for n in self.environments]
        if path.endswith("/spark/pools"):
            return [{"name": n} for n in self.pools]
        return super().list(path)

    def create(self, path, body):
        if path.endswith("/spark/pools"):
            if self.fail_pool:
                raise self.fail_pool
            self.created.append((path, body))
            self.pools.append(body["name"])
            return {}
        if path.endswith("/environments"):
            self.created.append((path, body))
            self.environments.append(body["displayName"])
            return {"id": "env-1"}
        if path.endswith("/publish"):
            if self.fail_publish:
                raise self.fail_publish
            self.created.append((path, body))
            return {}
        return super().create(path, body)

    def patch(self, path, body):
        self.patched.append((path, body))
        return {}


def test_the_spark_pool_settings_carry_over_to_fabric():
    wanted = environments.plan(SPARK_POOL, "transportation")
    assert wanted.pool["nodeSize"] == "Small" and wanted.pool["nodeFamily"] == "MemoryOptimized"
    assert wanted.pool["autoScale"] == {"enabled": True, "minNodeCount": 3, "maxNodeCount": 3}
    assert wanted.pool["dynamicExecutorAllocation"] == {"enabled": False}
    assert wanted.compute["runtimeVersion"] == "1.3"
    assert (wanted.compute["driverCores"], wanted.compute["executorMemory"]) == (4, "28g")
    assert any("Auto-pause" in n for n in wanted.notes)


def test_an_unknown_size_or_version_falls_back_and_says_so():
    wanted = environments.plan({"nodeSize": "Weird", "sparkVersion": "2.4"}, "p")
    assert wanted.pool["nodeSize"] == "Medium" and wanted.compute["runtimeVersion"] == "1.3"
    assert sum("used" in n for n in wanted.notes) == 2


def test_a_pool_without_autoscale_uses_its_node_count():
    wanted = environments.plan({"nodeSize": "Large", "nodeCount": 5}, "p")
    assert wanted.pool["autoScale"] == {"enabled": True, "minNodeCount": 5, "maxNodeCount": 5}


def test_a_spark_pool_becomes_a_pool_an_environment_and_is_published():
    rest = EnvRest()
    run, rest, _, _ = run_with([Source("sp", "transportation", "Spark Pool", 1, ENVIRONMENT, payload=SPARK_POOL)], rest=rest)
    item = run.items[0]
    assert item.status == COMPLETED and "published" in item.step
    paths = [p for p, _ in rest.created]
    assert paths == [f"/workspaces/{WS}/spark/pools", f"/workspaces/{WS}/environments",
                     f"/workspaces/{WS}/environments/env-1/staging/publish"]
    patch_path, compute = rest.patched[0]
    assert patch_path.endswith("/environments/env-1/staging/sparkcompute")
    assert compute["instancePool"] == {"name": "transportation", "type": "Workspace"}


def test_an_existing_environment_is_left_alone():
    run, rest, _, _ = run_with([Source("sp", "transportation", "Spark Pool", 1, ENVIRONMENT, payload=SPARK_POOL)],
                               rest=EnvRest(environments_=["Transportation"]))
    assert run.items[0].status == SKIPPED and not rest.created and not rest.patched


def test_a_refused_custom_pool_falls_back_to_the_starter_pool():
    rest = EnvRest(fail_pool=FabricApiError(403, "InsufficientPrivileges", "needs admin"))
    run, rest, _, _ = run_with([Source("sp", "transportation", "Spark Pool", 1, ENVIRONMENT, payload=SPARK_POOL)], rest=rest)
    item = run.items[0]
    assert item.status == COMPLETED and any("Starter Pool" in n for n in item.notes)
    assert rest.patched[0][1]["instancePool"]["name"] == "Starter Pool"
    assert rest.patched[0][1]["driverCores"] == 8


def test_a_publish_failure_is_reported_not_hidden():
    rest = EnvRest(fail_publish=FabricApiError(400, "PublishFailed", "bad config"))
    run, _, _, _ = run_with([Source("sp", "transportation", "Spark Pool", 1, ENVIRONMENT, payload=SPARK_POOL)], rest=rest)
    assert run.items[0].status == FAILED and "bad config" in run.items[0].error


# --- the planner ----------------------------------------------------------------

from discovery_agent.migration import planner as planning  # noqa: E402
from discovery_agent.migration.preflight import content_findings, environment_checks  # noqa: E402


def po(id_, type_, wave, kind=None, deps=(), hub=0, cls="DIRECT"):
    return planning.PlanObject(id_, id_, type_, wave, cls, "Target", kind, tuple(deps), hub)


def test_the_planner_is_deterministic_and_fingerprints_its_input():
    objs = [po("t1", "Table", 2, TABLE), po("v1", "View", 3, VIEW, ["t1"]), po("p1", "Pipeline", 4)]
    a, b = planning.analyze(objs), planning.analyze(list(reversed(objs)))
    assert a == b and len(a["fingerprint"]) == 12
    assert planning.analyze(objs[:2])["fingerprint"] != a["fingerprint"]


def test_strategy_follows_what_the_build_can_create_then_the_classification():
    result = planning.analyze([po("t", "Table", 2, TABLE), po("ir", "Integration Runtime", 1, cls="MANUAL"),
                               po("d", "Dataset", 3, cls="REVIEW"), po("pl", "Pipeline", 4)])
    got = {k: v["strategy"] for k, v in result["objectStrategies"].items()}
    assert got == {"t": "automated", "ir": "manual", "d": "assess", "pl": "later"}
    assert result["needsReview"]["count"] == 2


def test_a_dependency_planned_later_blocks_and_lowers_readiness():
    ok = planning.analyze([po("t", "Table", 2, TABLE), po("v", "View", 3, VIEW, ["t"])])
    bad = planning.analyze([po("t", "Table", 4, TABLE), po("v", "View", 3, VIEW, ["t"])])
    assert ok["blocking"] == 0 and ok["readiness"] == 100
    assert bad["blocking"] == 1 and bad["readiness"] == 75
    assert bad["risks"][0]["code"] == "DEPENDENCY_LATER"


def test_a_missing_dependency_is_high_and_a_hub_is_flagged():
    result = planning.analyze([po("v", "View", 3, VIEW, ["gone"]), po("t", "Table", 2, TABLE, hub=6)])
    codes = {r["code"] for r in result["risks"]}
    assert {"DEPENDENCY_NOT_PLANNED", "HIGH_BLAST_RADIUS"} <= codes


def test_a_failed_target_check_blocks():
    result = planning.analyze([po("t", "Table", 2, TABLE)], checks=[{"label": "Fabric target connected", "status": "fail", "detail": "x"}])
    assert result["blocking"] == 1 and result["risks"][0]["code"] == "TARGET_NOT_READY"


def test_effort_is_a_review_allowance_for_automated_and_more_for_manual():
    auto = planning.analyze([po("t", "Pipeline", 1, PROCEDURE)])["effortDays"]
    later = planning.analyze([po("t", "Pipeline", 1)])["effortDays"]
    manual = planning.analyze([po("t", "Pipeline", 1, cls="MANUAL")])["effortDays"]
    assert auto < later < manual


def test_preflight_reads_the_content_the_run_will_translate():
    bad_col = SqlColumn(1, "Shape", "sql_variant", 8, 0, 0, True, False)
    findings = content_findings([
        src("sales.Orders", TABLE, payload=table()),
        src("sales.Odd", TABLE, payload=table(name="Odd", columns=(bad_col,))),
        src("sales.vOk", VIEW, payload=view()),
        src("sales.vOpaque", VIEW, payload=view(name="vOpaque", text="")),
        src("sales.vCtas", VIEW, payload=view(name="vCtas", text="CREATE TABLE x WITH (DISTRIBUTION = ROUND_ROBIN) AS SELECT 1")),
    ])
    by = {(f.object_id, f.code): f.severity for f in findings}
    assert by[("sales.Orders", "TYPE_CONVERSION")] in ("LOW", "MEDIUM")
    assert by[("sales.Odd", "UNSUPPORTED_TYPE")] == "BLOCKING"
    assert by[("sales.vOpaque", "DEFINITION_UNREADABLE")] == "BLOCKING"
    assert by[("sales.vCtas", "SYNAPSE_TSQL")] == "MEDIUM"
    assert not [f for f in findings if f.object_id == "sales.vOk"]


def test_environment_checks_explain_each_failure():
    off = environment_checks({"status": "disconnected"}, "")
    assert [c["status"] for c in off] == ["fail", "fail"]
    on = environment_checks({"status": "connected", "workspaceName": "W", "capacityAssigned": False}, "ODBC Driver 18 for SQL Server")
    assert [c["status"] for c in on] == ["ok", "fail", "ok"]


def test_stop_on_failure_halts_at_the_wave_boundary_and_resume_waives_it():
    class Failing(FakeRest):
        def create(self, path, body):
            raise FabricApiError(400, "Bad", "nope")
    sources = [src("A", NOTEBOOK, wave=1, payload=notebook_payload()), src("sales.Orders", TABLE, wave=2, payload=table())]
    run = MigrationRun("001", sources, WS, "W", "pool01")
    run.stop_on_failure = True
    Migrator(run, lambda: Failing(), lambda h, d: FakeDb()).execute()
    assert run.state == "paused" and "wave 2" in run.halted_reason
    assert [i.status for i in run.items] == [FAILED, PENDING]
    run.halt_waived, run.halted_reason = True, None
    Migrator(run, lambda: Failing(), lambda h, d: FakeDb()).execute()
    assert run.state == "completed" and run.items[1].status != PENDING


ALL_THREE = [{"id": "synapse://notebook/LoadSales", "wave": 4}, {"id": "sql://ws/pool/sales/Orders", "wave": 2},
             {"id": "synapse://pipeline/Copy", "wave": 5}]


def test_the_planner_endpoint_reports_strategy_checks_and_a_history_of_recorded_runs():
    svc = service(fake_job())
    first = svc.analyze({"items": ALL_THREE})
    assert first["strategyCounts"] == {"automated": 3, "manual": 0, "assess": 0, "later": 0, "deselected": 0}
    off = svc.analyze({"items": ALL_THREE, "options": {"stages": ["warehouse"]}})
    assert off["strategyCounts"]["deselected"] == 2 and off["strategyCounts"]["automated"] == 1
    assert first["history"] == []  # an unrecorded look leaves no history
    assert [c["status"] for c in first["checks"]][:2] == ["ok", "ok"]
    again = svc.analyze({"items": ALL_THREE, "record": True})
    assert again["fingerprint"] == first["fingerprint"]
    assert len(again["history"]) == 1 and again["history"][0]["id"] == "P001"
    assert len(svc.analyze({"items": ALL_THREE, "record": True})["history"]) == 2


def test_the_planner_rejects_a_malformed_plan():
    with pytest.raises(ApiError) as exc:
        service(fake_job()).analyze({"items": "nope"})
    assert exc.value.code == "invalid_plan"


def test_scope_automated_leaves_deferred_objects_out_of_the_run():
    svc = service(fake_job())
    svc.start({"items": ALL_THREE, "options": {"scope": "automated", "stages": ["warehouse", "notebooks"]}})
    assert wait_until(lambda: svc.state()["state"] == "completed")
    state = svc.state()
    assert state["total"] == 2 and state["deferred"] == 0 and state["options"]["scope"] == "automated"
    allscope = service(fake_job())
    allscope.start({"items": ALL_THREE, "options": {"scope": "all", "stages": ["warehouse", "notebooks"]}})
    assert wait_until(lambda: allscope.state()["state"] == "completed")
    assert allscope.state()["total"] == 3 and allscope.state()["deferred"] == 1  # the pipeline: its stage is off


def test_a_plan_with_nothing_automated_is_refused_and_bad_options_are_rejected():
    svc = service(fake_job())
    with pytest.raises(ApiError) as nothing:
        svc.start({"items": [{"id": "not-in-discovery", "wave": 1}], "options": {"scope": "automated"}})
    assert nothing.value.code == "nothing_to_migrate"
    with pytest.raises(ApiError) as bad:
        svc.start({"items": ALL_THREE, "options": {"scope": "everything"}})
    assert bad.value.code == "invalid_options"
