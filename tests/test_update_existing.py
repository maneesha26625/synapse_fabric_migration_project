"""Run option ``existing: update``: objects already in Fabric are updated in place instead of skipped. Offline.

Views and procedures use CREATE OR ALTER, so the old object stays if the new text is rejected. Fabric items keep
their id (``updateDefinition``), so pipelines that call them and their schedules still point at them. Tables are
never replaced: they hold data.
"""

from __future__ import annotations

import pytest

from discovery_agent.api.migration import parse_options
from discovery_agent.api.service import ApiError
from discovery_agent.migration import tsql_rules
from discovery_agent.migration.common import COMPLETED, NOTEBOOK, PIPELINE, SCRIPT, SKIPPED, SPARKJOB, TABLE, VIEW, Source

from test_migration import WS, FakeDb, notebook_payload, table, view
from test_migration_stages import BUNDLE, SCRIPT_RES, SJD, StageRest, migrate, one

UPDATE = {"existing": "update"}


class UpdRest(StageRest):
    def __init__(self, **seed):
        super().__init__(**seed)
        self.updated = []

    def create(self, path, body):
        if path.endswith("/updateDefinition"):
            self.updated.append((path, body))
            return {}
        return super().create(path, body)


# -- CREATE OR ALTER -----------------------------------------------------------------------------------

def test_create_or_alter_rewrites_the_create_and_leaves_comments_and_strings_alone():
    sql = "-- CREATE VIEW old\nCREATE   VIEW [s].[v] AS SELECT 'CREATE VIEW' AS x"
    assert tsql_rules.create_or_alter(sql) == "-- CREATE VIEW old\nCREATE OR ALTER VIEW [s].[v] AS SELECT 'CREATE VIEW' AS x"
    assert tsql_rules.create_or_alter("create proc p as select 1").startswith("CREATE OR ALTER proc p")
    already = "CREATE OR ALTER PROCEDURE p AS SELECT 1"
    assert tsql_rules.create_or_alter(already) == already
    assert tsql_rules.create_or_alter("SELECT 1") is None


# -- the Warehouse -------------------------------------------------------------------------------------

def vsrc():
    return Source("v", "sales.vOrders", "View", 1, VIEW, payload=view(), schema="sales", object_name="vOrders")


def test_an_existing_view_is_skipped_by_default_and_updated_in_place_on_request():
    db = FakeDb(existing={"[sales].[vOrders]"})
    run, _, _ = migrate([vsrc()], db=db)
    assert one(run).status == SKIPPED and not any(s.startswith("CREATE") for s in db.statements)
    db = FakeDb(existing={"[sales].[vOrders]"})
    run, _, _ = migrate([vsrc()], db=db, settings=UPDATE)
    assert one(run).status == COMPLETED and "updated in place" in one(run).step
    assert "CREATE OR ALTER VIEW sales.vOrders AS SELECT OrderId FROM sales.Orders" in db.statements
    assert not any(s.upper().startswith("DROP") for s in db.statements)


def test_a_new_view_is_still_created_plainly_when_updating():
    db = FakeDb()
    run, _, _ = migrate([vsrc()], db=db, settings=UPDATE)
    assert one(run).step.startswith("View created") and "CREATE VIEW sales.vOrders AS SELECT OrderId FROM sales.Orders" in db.statements


def test_an_existing_table_is_never_replaced():
    db = FakeDb(existing={"[sales].[Orders]"})
    run, _, _ = migrate([Source("t", "sales.Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders")],
                        db=db, settings=UPDATE)
    assert one(run).status == SKIPPED and not any("CREATE TABLE" in s for s in db.statements)


# -- Fabric items ------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind, type_, name, payload, collection", [
    (NOTEBOOK, "Notebook", "LoadSales", notebook_payload(), "notebooks"),
    (PIPELINE, "Pipeline", "pl_load", BUNDLE, "dataPipelines"),
    (SPARKJOB, "Spark Job Definition", "job1", SJD, "sparkJobDefinitions"),
    (SCRIPT, "SQL Script", "q1", SCRIPT_RES, "notebooks"),
])
def test_an_existing_fabric_item_keeps_its_id_and_gets_the_new_definition(kind, type_, name, payload, collection):
    seed = {collection: [{"id": "item-7", "displayName": name}],
            "connections": [{"id": "conn-dw", "displayName": "ls_sql"}, {"id": "conn-adls", "displayName": "ls_adls"}],
            "notebooks" if collection != "notebooks" else "environments": [{"id": "nb-1", "displayName": "LoadSales"}]}
    if kind == PIPELINE:
        seed[collection].append({"id": "pl-9", "displayName": "pl_child"})  # the sample pipeline calls it
    if kind == SPARKJOB:
        seed["lakehouses"] = [{"id": "lh-1", "displayName": "pool01_lakehouse"}]  # a Spark job needs a default Lakehouse
    src = Source("x", name, type_, 1, kind, payload=payload)
    skipped, rest, _ = migrate([src], rest=UpdRest(**seed))
    assert one(skipped).status == SKIPPED and not rest.updated
    run, rest, _ = migrate([src], rest=UpdRest(**seed), settings=UPDATE)
    assert one(run).status == COMPLETED and one(run).step == "Updated in Fabric (definition replaced)", one(run).error
    path, body = rest.updated[0]
    assert path == f"/workspaces/{WS}/items/item-7/updateDefinition" and body["definition"]["parts"]
    assert not [c for c in rest.created if not c[0].endswith("/updateDefinition")]  # nothing new created


def test_the_option_is_validated():
    assert parse_options({"options": {"existing": "update"}})["existing"] == "update"
    assert parse_options({})["existing"] == "skip"
    with pytest.raises(ApiError):
        parse_options({"options": {"existing": "overwrite"}})
