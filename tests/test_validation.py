"""Migration validation: Synapse compared with Fabric, object by object. Offline.

The Warehouse is a fake that answers the catalog queries the validator asks; the
Fabric workspace is a fake that lists items and returns stored definitions.
"""

from __future__ import annotations

import base64
import json
import re
from decimal import Decimal  # noqa: F401 - kept for parity with the stage tests

import pytest

from discovery_agent.api.migration import MigrationService
from discovery_agent.migration import validation as v
from discovery_agent.migration import warehouse_ddl
from discovery_agent.migration.common import (
    CONNECTION, DATA, DATASET, DEFERRED, ENVIRONMENT, NOTEBOOK, PIPELINE, POOL, PROCEDURE, SCHEDULE, SCHEMA, SCRIPT, SHORTCUT,
    SPARKJOB, TABLE, VIEW, Source,
)
from discovery_agent.sql.models import (
    CatalogObjectType, DefinitionState, DistributionPolicy, SqlColumn, SqlModuleDefinition, SqlObjectKey, SqlTable, SqlView,
)

from test_migration import WS, FakeSession, fake_job  # noqa: E402
from test_migration_stages import (  # noqa: E402
    BUNDLE, PIPELINE_RES, SCRIPT_RES, SJD, SQL_LS, StageRest, SourceDb, external, fake_job2, trigger,
)

SPARK_META = {"nodeSize": "Small", "nodeSizeFamily": "MemoryOptimized", "nodeCount": 3,
              "autoScale": {"enabled": True, "minNodeCount": 3, "maxNodeCount": 3}, "sparkVersion": "3.5"}


def b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def plain_table(name="Orders"):
    cols = (SqlColumn(1, "Id", "int", 4, 10, 0, False, False), SqlColumn(2, "Label", "varchar", 50, 0, 0, True, False),
            SqlColumn(3, "Amount", "decimal", 9, 18, 4, True, False), SqlColumn(4, "At", "datetime2", 8, 27, 3, True, False))
    return SqlTable(key=SqlObjectKey("pool", "sales", name), distribution=DistributionPolicy.HASH, columns=cols)


def type_row(schema, table, column):
    """The Warehouse's catalog row for a column, from the type the migration would have created."""
    spec = warehouse_ddl.map_type(column)[0]
    m = re.match(r"(\w+)(?:\((\w+)(?:,(\d+))?\))?", spec)
    base, a, b = m.group(1), m.group(2), m.group(3)
    length = precision = scale = 0
    if base in ("varchar", "varbinary", "char", "binary"):
        length = -1 if a == "MAX" else int(a)
    elif base == "decimal":
        precision, scale = int(a), int(b)
    elif base in ("datetime2", "time"):
        scale = int(a)
    return (schema, table, column.column_id, column.name, base, length, precision, scale, 1)


class CatalogDb:
    def __init__(self, tables=None, modules=None, rows=None, break_modules=False):
        self.tables = tables or {}          # (schema, name) -> SqlTable
        self.modules = modules or {}        # (schema, name) -> (type, text)
        self.rows = rows or {}              # "[s].[t]" -> count
        self.break_modules = break_modules

    def cursor(self):
        return CatalogCursor(self)

    def close(self):
        pass


class CatalogCursor:
    def __init__(self, db):
        self.db, self._rows, self._one = db, [], None

    def execute(self, sql, *params):
        db = self.db
        if sql == v.SCHEMAS_SQL:
            self._rows = [("dbo",), *[(s,) for s in {k[0] for k in {**db.tables, **db.modules}}]]
        elif sql == v.OBJECTS_SQL:
            self._rows = [(s, n, "U") for (s, n) in db.tables] + [(s, n, t) for (s, n), (t, _) in db.modules.items()]
        elif sql == v.COLUMNS_SQL:
            self._rows = [type_row(s, n, c) for (s, n), t in db.tables.items() for c in t.columns]
        elif sql == v.MODULES_SQL:
            if db.break_modules:
                raise RuntimeError("Invalid object name 'sys.sql_modules'")
            self._rows = [(s, n, text) for (s, n), (_, text) in db.modules.items()]
        elif sql.startswith("SELECT COUNT_BIG"):
            self._one = (db.rows.get(sql.split("FROM ")[1], 0),)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class ValRest(StageRest):
    def __init__(self, definitions=None, **seed):
        super().__init__(**seed)
        self.definitions = definitions or {}

    def create(self, path, body):
        if "/getDefinition" in path:
            self.asked = getattr(self, "asked", []) + [path]
            return self.definitions.get(path.split("?")[0].split("/")[-2], {})
        return super().create(path, body)


def validator(rest=None, db=None, source=None, artifacts=None):
    rest = rest or ValRest()
    db = db or CatalogDb()
    return v.Validator(rest, WS, "Sales WS", "pool01", lambda host, name: db, (lambda: source) if source is not None else None, artifacts)


def check(sources, **kw):
    return validator(**kw).run(sources)


def tbl(table=None, name="Orders"):
    table = table or plain_table(name)
    return Source(f"t-{name}", f"sales.{name}", "Table", 2, TABLE, payload=table, schema="sales", object_name=name)


def by_cat(rows, category):
    return [r for r in rows if r["category"] == category]


# --- pure helpers --------------------------------------------------------------------------


def test_warehouse_column_types_are_spelled_like_the_migration_spells_them():
    assert v.actual_type("varchar", -1, 0, 0) == "varchar(MAX)" and v.actual_type("varchar", 50, 0, 0) == "varchar(50)"
    assert v.actual_type("numeric", 9, 18, 4) == "decimal(18,4)" and v.actual_type("datetime2", 8, 27, 3) == "datetime2(3)"
    assert v.actual_type("INT", 4, 10, 0) == "int"


def test_definitions_compare_without_comments_whitespace_or_a_trailing_semicolon():
    assert v._norm("CREATE VIEW a AS -- note\n SELECT  1 ;") == v._norm("create view a as /* x */ select 1")


def test_activities_are_counted_through_nested_containers_but_not_dataset_settings():
    assert v.count_activities(PIPELINE_RES["properties"]["activities"]) == 5  # Copy, notebook, ForEach, Inner, procedure
    fabric = {"properties": {"activities": [{"name": "C", "type": "Copy", "typeProperties": {"source": {"datasetSettings": {"type": "X", "typeProperties": {}}}}}]}}
    assert v.count_activities(fabric["properties"]["activities"]) == 1


# --- the Warehouse ---------------------------------------------------------------------------


def test_a_migrated_table_matches_on_columns_and_types():
    t = plain_table()
    rows = check([tbl(t)], db=CatalogDb({("sales", "Orders"): t}), rest=ValRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}]))
    assert rows[0]["status"] == v.MATCH and rows[0]["source"] == "4 columns" == rows[0]["target"]


def test_a_converted_type_is_a_review_not_a_mismatch_and_the_note_says_which():
    t = plain_table()
    odd = SqlTable(key=t.key, columns=t.columns + (SqlColumn(5, "Tiny", "tinyint", 1, 3, 0, True, False),))
    rows = check([tbl(odd)], db=CatalogDb({("sales", "Orders"): odd}))
    assert rows[0]["status"] == v.REVIEW and "tinyint" in rows[0]["detail"]


def test_a_missing_table_a_missing_column_and_a_changed_type_are_mismatches():
    t = plain_table()
    assert check([tbl(t)], db=CatalogDb())[0]["status"] == v.MISMATCH
    short = SqlTable(key=t.key, columns=t.columns[:3])
    assert "missing: At" in check([tbl(t)], db=CatalogDb({("sales", "Orders"): short}))[0]["detail"]
    changed = SqlTable(key=t.key, columns=(SqlColumn(1, "Id", "bigint", 8, 19, 0, False, False),) + t.columns[1:])
    detail = check([tbl(t)], db=CatalogDb({("sales", "Orders"): changed}))[0]["detail"]
    assert "Id (int expected, bigint found)" in detail


def test_row_counts_match_differ_or_have_not_been_loaded():
    t = plain_table()
    data = Source("t#data", "sales.Orders (data)", "Table data", 2, DATA, payload=t, schema="sales", object_name="Orders")
    db = lambda n: CatalogDb({("sales", "Orders"): t}, rows={"[sales].[Orders]": n})  # noqa: E731
    same = check([data], db=db(10), source=SourceDb([0] * 10))[0]
    assert same["status"] == v.MATCH and same["category"] == "Data Count" and same["object"] == "sales.Orders"
    assert check([data], db=db(0), source=SourceDb([0] * 10))[0]["status"] == v.REVIEW
    off = check([data], db=db(7), source=SourceDb([0] * 10))[0]
    assert off["status"] == v.MISMATCH and "3 row(s) missing" in off["detail"]
    unread = check([data], db=db(7))[0]
    assert unread["status"] == v.REVIEW and "not connected" in unread["detail"]


def test_views_and_procedures_compare_their_definitions():
    view = SqlView(key=SqlObjectKey("pool", "sales", "vO", CatalogObjectType.VIEW), definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text="CREATE VIEW sales.vO AS SELECT 1"))
    src = Source("v", "sales.vO", "View", 3, VIEW, payload=view, schema="sales", object_name="vO")
    same = check([src], db=CatalogDb(modules={("sales", "vO"): ("V", "create view sales.vO as\n select 1;")}))[0]
    assert same["status"] == v.MATCH
    assert check([src], db=CatalogDb(modules={("sales", "vO"): ("V", "CREATE VIEW sales.vO AS SELECT 2")}))[0]["status"] == v.REVIEW
    assert check([src], db=CatalogDb())[0]["status"] == v.MISMATCH
    # a Warehouse that will not show module text still proves the object is there
    assert check([src], db=CatalogDb(modules={("sales", "vO"): ("V", "x")}, break_modules=True))[0]["status"] == v.REVIEW


def test_pool_and_schema_are_checked_and_a_missing_warehouse_is_reported_once_per_object():
    pool = Source("p", "pool01", "Dedicated SQL Pool", 1, POOL)
    schema = Source("s", "pool01.sales", "Schema", 1, SCHEMA, schema="sales", object_name="sales")
    t = plain_table()
    good = check([pool, schema], db=CatalogDb({("sales", "Orders"): t}), rest=ValRest())
    assert [r["status"] for r in good] == [v.MATCH, v.MATCH]
    gone = check([pool, schema, tbl(t)], rest=ValRest(warehouses=[]))
    assert [r["status"] for r in gone] == [v.MISMATCH] * 3 and "does not exist" in gone[0]["detail"]


# --- the Fabric items ----------------------------------------------------------------------------

NB = {"properties": {"cells": [{"cell_type": "code", "source": ["1"]}, {"cell_type": "code", "source": ["2"]}]}}


def test_notebooks_compare_cell_counts_and_a_missing_one_is_a_mismatch():
    src = Source("n", "LoadSales", "Notebook", 4, NOTEBOOK, payload=NB)
    rest = lambda cells: ValRest(notebooks=[{"id": "nb-1", "displayName": "LoadSales"}],  # noqa: E731
                                 definitions={"nb-1": {"definition": {"parts": [{"path": "notebook-content.ipynb", "payload": b64({"cells": [{}] * cells})}]}}})
    asked = rest(2)
    assert check([src], rest=asked)[0]["status"] == v.MATCH
    assert asked.asked == [f"/workspaces/{WS}/notebooks/nb-1/getDefinition?format=ipynb"]  # the ipynb form, not Git format
    wrong = check([src], rest=rest(5))[0]
    assert wrong["status"] == v.MISMATCH and wrong["source"] == "2 cells" and wrong["target"] == "5 cells"
    assert check([src], rest=ValRest())[0]["status"] == v.MISMATCH
    unreadable = check([src], rest=ValRest(notebooks=[{"id": "nb-1", "displayName": "LoadSales"}]))[0]
    assert unreadable["status"] == v.REVIEW and "could not be read" in unreadable["detail"]


def test_pipelines_compare_activity_counts_and_datasets_follow_their_pipelines():
    src = Source("p", "pl_load", "Pipeline", 4, PIPELINE, payload=BUNDLE)
    content = {"properties": {"activities": [{"name": f"a{i}", "type": "Wait", "typeProperties": {}} for i in range(5)]}}
    rest = ValRest(dataPipelines=[{"id": "pl-1", "displayName": "pl_load"}],
                   definitions={"pl-1": {"definition": {"parts": [{"path": "pipeline-content.json", "payload": b64(content)}]}}})
    ok = check([src], rest=rest)[0]
    assert ok["status"] == v.MATCH and ok["source"] == "5 activities"
    short = ValRest(dataPipelines=[{"id": "pl-1", "displayName": "pl_load"}], definitions={"pl-1": {"definition": {"parts": [
        {"path": "pipeline-content.json", "payload": b64({"properties": {"activities": []}})}]}}})
    assert check([src], rest=short)[0]["status"] == v.MISMATCH
    ds = Source("d", "ds_dw", "Dataset", 3, DATASET, payload={})
    artifacts = {"pipelines": {"pl_load": PIPELINE_RES}}
    assert check([ds], rest=rest, artifacts=artifacts)[0]["status"] == v.MATCH
    assert check([ds], rest=ValRest(), artifacts=artifacts)[0]["status"] == v.MISMATCH
    assert check([ds], rest=ValRest(), artifacts={"pipelines": {}})[0]["detail"].startswith("No pipeline uses it")


def test_a_spark_pool_is_matched_on_the_pool_and_the_environment():
    src = Source("sp", "transportation", "Spark Pool", 1, ENVIRONMENT, payload=SPARK_META)
    pool = {"name": "transportation", "nodeSize": "Small", "autoScale": {"minNodeCount": 3, "maxNodeCount": 3}}
    both = ValRest(environments=[{"id": "e", "displayName": "transportation"}], spark=[pool])
    assert check([src], rest=both)[0]["status"] == v.MATCH
    resized = ValRest(environments=[{"id": "e", "displayName": "transportation"}], spark=[{**pool, "nodeSize": "Large"}])
    assert check([src], rest=resized)[0]["status"] == v.REVIEW
    assert check([src], rest=ValRest(spark=[pool]))[0]["status"] == v.MISMATCH
    assert check([src], rest=ValRest())[0]["status"] == v.MISMATCH


def test_connections_jobs_scripts_schedules_and_shortcuts_are_found_or_missing():
    rest = ValRest(connections=[{"id": "c", "displayName": "ls_sql"}], sparkJobDefinitions=[{"id": "j", "displayName": "job1"}],
                   notebooks=[{"id": "n", "displayName": "q1"}], dataPipelines=[{"id": "pl-1", "displayName": "pl_load"}],
                   lakehouses=[{"id": "lh", "displayName": "pool01_lakehouse"}], items=[{"name": "Ext"}])
    rest.schedules["pl-1"] = [{"enabled": False}]
    sources = [Source("c", "ls_sql", "Linked Service", 1, CONNECTION, payload=SQL_LS), Source("j", "job1", "Spark Job Definition", 4, SPARKJOB, payload=SJD),
               Source("q", "q1", "SQL Script", 3, SCRIPT, payload=SCRIPT_RES), Source("t", "t1", "Trigger", 7, SCHEDULE, payload=trigger("Day")),
               Source("e", "ext.Ext", "External Table", 2, SHORTCUT, payload=external(), schema="ext", object_name="Ext")]
    assert [r["status"] for r in check(sources, rest=rest)] == [v.MATCH] * 5
    assert [r["status"] for r in check(sources, rest=ValRest())] == [v.MISMATCH] * 5
    no_schedule = ValRest(dataPipelines=[{"id": "pl-1", "displayName": "pl_load"}])
    assert check([sources[3]], rest=no_schedule)[0]["status"] == v.MISMATCH
    by_hand = Source("t", "t9", "Trigger", 7, SCHEDULE, payload=trigger("Month"))
    assert check([by_hand], rest=rest)[0]["status"] == v.REVIEW


def test_things_set_up_by_hand_are_listed_for_review_and_a_failing_check_does_not_stop_the_report():
    ir = Source("ir", "AutoResolve", "Integration Runtime", 1, DEFERRED, reason="Set it up by hand.")
    notebook_without_payload = Source("n", "Broken", "Notebook", 4, NOTEBOOK, payload=None)

    class Exploding(ValRest):
        def list(self, path):
            raise KeyError("boom")

    rows = check([notebook_without_payload, ir], rest=Exploding())
    assert rows[0]["status"] == v.REVIEW and "could not run" in rows[0]["detail"]
    assert rows[1]["category"] == "Manual" and rows[1]["status"] == v.REVIEW and rows[1]["detail"] == "Set it up by hand."


# --- the service --------------------------------------------------------------------------------------


def fabric(connected=True):
    from discovery_agent.api.fabric import FabricError

    def target():
        if not connected:
            raise FabricError(409, "target_not_connected", "Connect the Fabric target first.")
        return WS, "Sales WS", "azure_cli"

    return type("F", (), {"migration_target": staticmethod(target), "state": staticmethod(lambda: {})})()


def test_the_service_validates_every_discovered_object_in_a_stable_grouped_order():
    rest = ValRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}], notebooks=[{"id": "nb-1", "displayName": "LoadSales"}],
                   definitions={"nb-1": {"definition": {"parts": [{"path": "notebook-content.ipynb", "payload": b64({"cells": [{}, {}]})}]}}})
    orders = plain_table()
    db = CatalogDb({("sales", "Orders"): orders}, rows={"[sales].[Orders]": 4})
    svc = MigrationService(FakeSession(fake_job()), fabric(), rest_factory=lambda: rest, sql_factory=lambda h, d: db, source_factory=lambda: SourceDb([0] * 4))
    out = svc.validate({})
    cats = [r["category"] for r in out["rows"]]
    assert cats == sorted(cats, key=lambda c: ["Tables", "Data Count", "Notebooks", "Pipelines"].index(c))
    assert out["summary"][v.MATCH] + out["summary"][v.REVIEW] + out["summary"][v.MISMATCH] == len(out["rows"])
    assert out["warehouse"] == "pool01" and out["workspace"] == "Sales WS"
    assert svc.validate({"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}]})["rows"][0]["category"] == "Notebooks"


def test_validation_needs_a_connected_target_and_discovery_and_a_sane_request():
    from discovery_agent.api.fabric import FabricError
    from discovery_agent.api.service import ApiError
    with pytest.raises(FabricError):
        MigrationService(FakeSession(fake_job()), fabric(False), rest_factory=lambda: ValRest()).validate({})
    with pytest.raises(ApiError) as nodisc:
        MigrationService(FakeSession(None), fabric(), rest_factory=lambda: ValRest()).validate({})
    assert nodisc.value.code == "no_results"
    with pytest.raises(ApiError) as bad:
        MigrationService(FakeSession(fake_job()), fabric(), rest_factory=lambda: ValRest()).validate({"items": "x"})
    assert bad.value.code == "invalid_plan"
