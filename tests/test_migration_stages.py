"""The migration stages beyond the Warehouse: table data, connections, pipelines,
Spark jobs, SQL scripts, schedules and shortcuts.

Offline. Fabric is a fake workspace, the Warehouse and the Synapse pool are fake
DB-API connections. What this cannot establish is that Fabric accepts each
exact request body; that needs a live run, and Fabric's own error text is
surfaced if it does not.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from discovery_agent.api import migration as service_module
from discovery_agent.api.migration import (
    MigrationService, apply_stages, data_sources, parse_credentials, parse_options, pipeline_bundle, sources_for,
)
from discovery_agent.api.service import ApiError
from discovery_agent.migration import capabilities, datacopy, fabric_connections, jobs, pipelines, warehouse_ddl
from discovery_agent.migration.common import (
    COMPLETED, CONNECTION, DATA, DATASET, DEFERRED, DEFERRED_STATUS, FAILED, PIPELINE, SCHEDULE, SCRIPT, SHORTCUT,
    SKIPPED, SPARKJOB, TABLE, MigrationError, Source,
)
from discovery_agent.migration.fabric_rest import FabricApiError
from discovery_agent.migration.preflight import content_findings, environment_checks
from discovery_agent.migration.runner import MigrationRun, Migrator
from discovery_agent.source_strategy import P0Artifact
from discovery_agent.sql.models import DistributionPolicy, SqlColumn, SqlObjectKey, SqlTable

from test_migration import FakeDb, FakeSession, WS, table  # noqa: E402 - shared fixtures

# --- fakes ------------------------------------------------------------------------


class StageRest:
    """A fake Fabric workspace: collections that list, create and report schedules."""

    def __init__(self, **seed):
        self.store = {"notebooks": [], "dataPipelines": [], "sparkJobDefinitions": [], "environments": [], "lakehouses": [],
                      "connections": [], "warehouses": [{"id": "wh-1", "displayName": "pool01"}]}
        for key, value in seed.items():
            self.store[key] = list(value)
        self.schedules = {}
        self.created = []
        self.fail = {}

    def _collection(self, path):
        return "connections" if path == "/connections" else path.split("/")[3]

    def list(self, path):
        if "/jobs/Pipeline/schedules" in path:
            return list(self.schedules.get(path.split("/")[4], []))
        return list(self.store.get(self._collection(path), []))

    def get(self, path):
        if "/warehouses/" in path:
            return {"properties": {"connectionString": "tcp:abc.datawarehouse.fabric.microsoft.com,1433"}}
        return {"id": WS, "capacityId": "cap-1"}

    def patch(self, path, body):
        return {}

    def create(self, path, body):
        for needle, error in self.fail.items():
            if needle in path:
                raise error
        self.created.append((path, body))
        if "/jobs/Pipeline/schedules" in path:
            self.schedules.setdefault(path.split("/")[4], []).append(body)
            return {}
        if "/shortcuts" in path:
            return {}
        coll = self._collection(path)
        item = {"id": f"{coll}-{len(self.store[coll]) + 1}", "displayName": body.get("displayName")}
        if coll == "connections":
            item["connectionDetails"] = {"type": body["connectionDetails"]["type"]}
        self.store[coll].append(item)
        return {"id": item["id"]}


class DataDb:
    """A fake Warehouse that stores the rows it is given."""

    def __init__(self, tables=None, fail_after=None, drop_last=False):
        self.tables = {k: list(v) for k, v in (tables or {}).items()}
        self.fail_after = fail_after
        self.drop_last = drop_last
        self.statements = []
        self.inserts = []

    def cursor(self):
        return DataCursor(self)

    def close(self):
        pass


class DataCursor:
    def __init__(self, db):
        self.db, self._row = db, None

    def execute(self, sql, *params):
        db = self.db
        db.statements.append(sql)
        if sql == warehouse_ddl.EXISTS_QUERY:
            self._row = (1,) if params[0] in db.tables else (None,)
        elif sql.startswith("SELECT SCHEMA_ID"):
            self._row = (1,)
        elif sql.startswith("SELECT COUNT_BIG"):
            self._row = (len(db.tables[sql.split("FROM ")[1]]),)
        elif sql.startswith("TRUNCATE TABLE"):
            db.tables[sql.split("TABLE ")[1]] = []
        elif sql.startswith("INSERT INTO"):
            name = sql.split("INSERT INTO ")[1].split(" (")[0]
            columns = sql.split(" VALUES ")[0].count("],") + 1
            rows = [list(params[i:i + columns]) for i in range(0, len(params), columns)]
            db.inserts.append((len(rows), len(params), sql))
            if db.fail_after is not None and len(db.tables[name]) >= db.fail_after:
                raise RuntimeError("The connection was reset")
            db.tables[name].extend(rows[:-1] if db.drop_last else rows)
        elif sql.startswith("CREATE TABLE"):
            name = sql.split("CREATE TABLE ")[1].split(" (")[0]
            db.tables[name] = []

    def fetchone(self):
        return self._row


class SourceDb:
    """A fake Synapse pool: serves a table's rows in chunks, and external-table locations."""

    def __init__(self, rows=(), location=None):
        self.rows, self.location = list(rows), location

    def cursor(self):
        return SourceCursor(self)

    def close(self):
        pass


class SourceCursor:
    def __init__(self, db):
        self.db, self._pos, self._row = db, 0, None

    def execute(self, sql, *params):
        if sql.startswith("SELECT COUNT_BIG"):
            self._row = (len(self.db.rows),)
        elif sql == jobs.EXTERNAL_LOCATION_SQL:
            self._row = self.db.location
        self._pos = 0

    def fetchone(self):
        return self._row

    def fetchmany(self, n):
        chunk = self.db.rows[self._pos:self._pos + n]
        self._pos += n
        return chunk


def migrate(sources, rest=None, db=None, source=None, credentials=None, settings=None, pool_name="pool01"):
    rest = rest or StageRest()
    db = db or DataDb()
    run = MigrationRun("001", sources, WS, "Sales WS", "pool01")
    run.pool_name = pool_name
    if settings:
        run.settings.update(settings)
    Migrator(run, lambda: rest, lambda host, database: db, source_factory=(lambda: source) if source is not None else None,
             credentials=credentials).execute()
    return run, rest, db


def one(run):
    return run.items[0]


def rows(n):
    return [(i, f"customer {i}", Decimal("10.5000"), datetime(2026, 1, 1)) for i in range(n)]


def data_source(tbl=None):
    tbl = tbl or table()
    return Source("t#data", "sales.Orders (data)", "Table data", 2, DATA, payload=tbl, schema="sales", object_name="Orders")


ORDERS = "[sales].[Orders]"

# =============================================================================
# table data
# =============================================================================


def test_rows_are_copied_in_safe_batches_with_typed_parameters_and_counts_verified():
    run, _, db = migrate([data_source()], db=DataDb({ORDERS: []}), source=SourceDb(rows(2500)))
    item = one(run)
    assert item.status == COMPLETED and "2,500 rows" in item.step and "counts match" in item.step
    assert len(db.tables[ORDERS]) == 2500
    # 4 columns -> 500 rows per INSERT (2000 parameters), so five statements
    assert [n for n, _, _ in db.inserts] == [500] * 5
    assert all(params <= 2000 for _, params, _ in db.inserts)
    assert "CAST(? AS int)" in db.inserts[0][2] and "CAST(? AS decimal(19,4))" in db.inserts[0][2]


def test_a_table_that_already_has_rows_is_left_alone_unless_replace_is_chosen():
    existing = {ORDERS: [[1, "x", Decimal("1"), datetime(2026, 1, 1)]]}
    run, _, db = migrate([data_source()], db=DataDb(existing), source=SourceDb(rows(3)))
    assert one(run).status == SKIPPED and len(db.tables[ORDERS]) == 1
    run, _, db = migrate([data_source()], db=DataDb(existing), source=SourceDb(rows(3)), settings={"dataMode": "replace"})
    assert one(run).status == COMPLETED and len(db.tables[ORDERS]) == 3
    assert any("Replaced 1 row" in n for n in one(run).notes)


def test_a_table_over_the_row_limit_is_deferred_to_a_pipeline_copy():
    run, _, db = migrate([data_source()], db=DataDb({ORDERS: []}), source=SourceDb(rows(50)), settings={"maxRows": 10})
    item = one(run)
    assert item.status == DEFERRED_STATUS and "Copy activity" in item.notes[0] and db.tables[ORDERS] == []


def test_a_row_count_mismatch_clears_the_partial_rows_and_fails():
    run, _, db = migrate([data_source()], db=DataDb({ORDERS: []}, drop_last=True), source=SourceDb(rows(10)))
    item = one(run)
    assert item.status == FAILED and "do not match" in item.error and db.tables[ORDERS] == []


def test_a_failure_part_way_clears_what_was_loaded_so_a_rerun_is_not_fooled():
    run, _, db = migrate([data_source()], db=DataDb({ORDERS: []}, fail_after=500), source=SourceDb(rows(2000)))
    assert one(run).status == FAILED and "partial rows were cleared" in one(run).error and db.tables[ORDERS] == []


def test_data_needs_the_table_to_exist_and_the_source_to_be_connected():
    run, _, _ = migrate([data_source()], db=DataDb({}), source=SourceDb(rows(1)))
    assert "Warehouse stage first" in one(run).error
    run, _, _ = migrate([data_source()], db=DataDb({ORDERS: []}))
    assert "not connected" in one(run).error


def test_an_empty_source_table_is_a_completed_no_op_and_an_external_table_is_skipped():
    run, _, _ = migrate([data_source()], db=DataDb({ORDERS: []}), source=SourceDb([]))
    assert one(run).status == COMPLETED and "empty" in one(run).step
    external = SqlTable(key=SqlObjectKey("pool", "sales", "Ext"), is_external=True, columns=table().columns)
    run, _, _ = migrate([data_source(external)], db=DataDb({"[sales].[Orders]": []}), source=SourceDb([]))
    assert one(run).status == SKIPPED


def test_values_are_prepared_for_the_driver_and_wide_tables_are_batched_by_parameter_count():
    assert datacopy.to_parameter(bytearray(b"ab")) == b"ab" and datacopy.to_parameter(float("nan")) is None
    assert datacopy.to_parameter(Decimal("NaN")) is None and datacopy.to_parameter(5) == 5
    assert datacopy.batch_rows(1) == 1000 and datacopy.batch_rows(4) == 500 and datacopy.batch_rows(1500) == 1
    wide = SqlTable(key=SqlObjectKey("pool", "s", "W"), columns=tuple(SqlColumn(i, f"c{i}", "int", 4, 10, 0, True, False) for i in range(1, 1100)))
    assert datacopy.preflight(wide)[0] == "TOO_WIDE"
    geo = SqlColumn(1, "g", "geography", -1, 0, 0, True, False)
    assert datacopy.source_expression(geo) == "[g].STAsBinary() AS [g]"


# =============================================================================
# connections
# =============================================================================

SQL_LS = {"name": "ls_sql", "properties": {"type": "AzureSqlDW", "typeProperties": {
    "connectionString": "Server=tcp:syn.sql.azuresynapse.net,1433;Database=pool01;User ID=u;Password=TOPSECRET;"}}}
ADLS_LS = {"name": "ls_adls", "properties": {"type": "AzureBlobFS", "typeProperties": {"url": "https://acct.dfs.core.windows.net/"}}}
BLOB_LS = {"name": "ls_blob", "properties": {"type": "AzureBlobStorage", "typeProperties": {
    "connectionString": "DefaultEndpointsProtocol=https;AccountName=blobacct;AccountKey=K;EndpointSuffix=core.windows.net"}}}
KV_LS = {"name": "ls_kv", "properties": {"type": "AzureKeyVault", "typeProperties": {"baseUrl": "https://kv.vault.azure.net"}}}


def test_linked_services_map_to_fabric_connection_types_without_carrying_secrets():
    sql = fabric_connections.parse(SQL_LS)
    assert (sql.fabric_type, [p["value"] for p in sql.parameters]) == ("SQL", ["syn.sql.azuresynapse.net", "pool01"])
    assert "TOPSECRET" not in json.dumps(sql.describe()) and "TOPSECRET" not in json.dumps(sql.parameters)
    adls = fabric_connections.parse(ADLS_LS)
    assert adls.fabric_type == "AzureDataLakeStorage" and adls.parameters[0]["value"] == "https://acct.dfs.core.windows.net"
    blob = fabric_connections.parse(BLOB_LS)
    assert [p["value"] for p in blob.parameters] == ["blobacct", "blob.core.windows.net"]
    assert fabric_connections.parse(KV_LS).unsupported and "by hand" in fabric_connections.parse(KV_LS).unsupported


def test_a_connection_body_carries_the_credential_only_in_credential_details():
    plan = fabric_connections.parse(SQL_LS)
    body = fabric_connections.create_body(plan, {"authType": "basic", "username": "me", "password": "pw"})
    assert body["credentialDetails"]["credentials"] == {"credentialType": "Basic", "username": "me", "password": "pw"}
    assert body["connectionDetails"]["type"] == "SQL" and "pw" not in json.dumps(body["connectionDetails"])
    sp = fabric_connections.create_body(plan, {"authType": "servicePrincipal", "tenantId": "t", "clientId": "c", "clientSecret": "s"})
    assert sp["credentialDetails"]["credentials"]["credentialType"] == "ServicePrincipal"
    with pytest.raises(fabric_connections.CredentialError, match="password"):
        fabric_connections.create_body(plan, {"authType": "basic", "username": "me"})
    adls = fabric_connections.parse(ADLS_LS)
    with pytest.raises(fabric_connections.CredentialError, match="path"):
        fabric_connections.create_body(adls, {"authType": "key", "key": "k"})
    ok = fabric_connections.create_body(adls, {"authType": "key", "key": "k", "path": "raw"})
    assert {"dataType": "Text", "name": "path", "value": "raw"} in ok["connectionDetails"]["parameters"]


def test_the_connection_stage_defers_without_credentials_then_creates_and_never_logs_the_secret():
    source = Source("ls", "ls_sql", "Linked Service", 1, CONNECTION, payload=SQL_LS)
    run, rest, _ = migrate([source])
    assert one(run).status == DEFERRED_STATUS and one(run).step == "Needs credentials" and not rest.created
    creds = {"ls_sql": {"authType": "basic", "username": "me", "password": "S3CRET-VALUE"}}
    run, rest, _ = migrate([source], credentials=creds)
    assert one(run).status == COMPLETED and rest.created[0][0] == "/connections"
    assert rest.created[0][1]["credentialDetails"]["credentials"]["password"] == "S3CRET-VALUE"
    assert "S3CRET-VALUE" not in json.dumps(run.to_dict())


def test_an_existing_connection_and_an_unsupported_linked_service_are_handled():
    run, rest, _ = migrate([Source("ls", "ls_sql", "Linked Service", 1, CONNECTION, payload=SQL_LS)],
                           rest=StageRest(connections=[{"id": "c1", "displayName": "ls_sql"}]))
    assert one(run).status == SKIPPED and not rest.created
    run, _, _ = migrate([Source("kv", "ls_kv", "Linked Service", 1, CONNECTION, payload=KV_LS)])
    assert one(run).status == DEFERRED_STATUS and "by hand" in one(run).notes[0]


# =============================================================================
# pipelines
# =============================================================================

DS_DW = {"name": "ds_dw", "properties": {"type": "AzureSqlDWTable", "linkedServiceName": {"referenceName": "ls_sql", "type": "LinkedServiceReference"},
                                         "typeProperties": {"schema": "sales", "table": "Orders"}, "schema": []}}
DS_CSV = {"name": "ds_csv", "properties": {"type": "DelimitedText", "linkedServiceName": {"referenceName": "ls_adls", "type": "LinkedServiceReference"},
                                           "parameters": {"file": {"type": "String"}},
                                           "typeProperties": {"location": {"type": "AzureBlobFSLocation", "fileName": "@dataset().file", "fileSystem": "raw"}}}}
PIPELINE_RES = {"name": "pl_load", "properties": {
    "parameters": {"f": {"type": "String"}},
    "activities": [
        {"name": "CopyOrders", "type": "Copy", "dependsOn": [], "policy": {"timeout": "1.00:00:00"},
         "typeProperties": {"source": {"type": "DelimitedTextSource"}, "sink": {"type": "SqlDWSink"}, "enableStaging": True},
         "inputs": [{"referenceName": "ds_csv", "type": "DatasetReference", "parameters": {"file": {"value": "@pipeline().parameters.f", "type": "Expression"}}}],
         "outputs": [{"referenceName": "ds_dw", "type": "DatasetReference"}]},
        {"name": "RunNb", "type": "SynapseNotebook", "dependsOn": [{"activity": "CopyOrders", "dependencyConditions": ["Succeeded"]}],
         "typeProperties": {"notebook": {"referenceName": "LoadSales", "type": "NotebookReference"}, "parameters": {"d": {"value": "x", "type": "string"}},
                            "sparkPool": {"referenceName": "p", "type": "BigDataPoolReference"}}},
        {"name": "Loop", "type": "ForEach", "typeProperties": {"items": {"value": "@pipeline().parameters.l", "type": "Expression"}, "activities": [
            {"name": "Inner", "type": "ExecutePipeline", "typeProperties": {"pipeline": {"referenceName": "pl_child", "type": "PipelineReference"}, "waitOnCompletion": True}}]}},
        {"name": "Proc", "type": "SqlPoolStoredProcedure", "typeProperties": {"storedProcedureName": "[operation].[usp_x]"}, "sqlPool": {"referenceName": "pool01"}},
    ]}}
BUNDLE = {"resource": PIPELINE_RES, "datasets": {"ds_dw": DS_DW, "ds_csv": DS_CSV}, "linkedServices": {"ls_sql": SQL_LS, "ls_adls": ADLS_LS}}


def context(**over):
    base = dict(datasets=BUNDLE["datasets"], linked_services=BUNDLE["linkedServices"], connections={"ls_adls": "conn-adls"},
                notebooks={"LoadSales": "nb-1"}, pipelines={"pl_child": "pl-9"}, spark_jobs={}, workspace_id=WS,
                warehouse=pipelines.Warehouse("wh-1", "abc.datawarehouse.fabric.microsoft.com", "pool01"), pool_name="pool01")
    base.update(over)
    return pipelines.Context(**base)


def test_a_pipeline_is_rebuilt_for_fabric_with_inline_datasets_ids_and_the_warehouse():
    out = pipelines.convert(PIPELINE_RES, context())
    assert not out.unsupported and not out.missing
    acts = {a["name"]: a for a in out.definition["properties"]["activities"]}
    copy = acts["CopyOrders"]["typeProperties"]
    assert "inputs" not in acts["CopyOrders"] and copy["sink"]["type"] == "DataWarehouseSink"
    assert copy["sink"]["datasetSettings"]["type"] == "DataWarehouseTable"
    assert copy["sink"]["datasetSettings"]["linkedService"]["properties"]["typeProperties"]["artifactId"] == "wh-1"
    src = copy["source"]["datasetSettings"]
    assert src["externalReferences"] == {"connection": "conn-adls"}
    assert src["typeProperties"]["location"]["fileName"] == {"value": "@pipeline().parameters.f", "type": "Expression"}  # @dataset().file substituted
    assert "enableStaging" not in copy
    nb = acts["RunNb"]
    assert nb["type"] == "TridentNotebook" and nb["typeProperties"]["notebookId"] == "nb-1" and nb["typeProperties"]["workspaceId"] == WS
    inner = acts["Loop"]["typeProperties"]["activities"][0]
    assert inner["type"] == "InvokePipeline" and inner["typeProperties"]["pipelineId"] == "pl-9"
    assert acts["Proc"]["type"] == "SqlServerStoredProcedure" and acts["Proc"]["linkedService"]["properties"]["type"] == "DataWarehouse"
    assert out.definition["properties"]["parameters"] == {"f": {"type": "String"}}
    assert any("Warehouse 'pool01'" in n for n in out.notes)


def test_a_pipeline_reports_missing_dependencies_and_activities_it_cannot_convert():
    out = pipelines.convert(PIPELINE_RES, context(connections={}, notebooks={}, pipelines={}))
    assert {"connection ls_adls", "notebook LoadSales", "pipeline pl_child"} <= set(out.missing)
    flow = {"properties": {"activities": [{"name": "DF", "type": "ExecuteDataFlow", "typeProperties": {}}, {"name": "W", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}}]}}
    bad = pipelines.convert(flow, context())
    assert bad.unsupported == ["DF (ExecuteDataFlow)"] and [a["name"] for a in bad.definition["properties"]["activities"]] == ["W"]


def test_pipeline_references_are_listed_for_planning():
    refs = pipelines.referenced_names(PIPELINE_RES)
    assert refs["datasets"] == ["ds_csv", "ds_dw"] and refs["notebooks"] == ["LoadSales"] and refs["pipelines"] == ["pl_child"]


def test_the_pipeline_stage_creates_it_defers_a_rewrite_and_fails_on_missing_pieces():
    rest = StageRest(notebooks=[{"id": "nb-1", "displayName": "LoadSales"}], dataPipelines=[{"id": "pl-9", "displayName": "pl_child"}],
                     connections=[{"id": "conn-adls", "displayName": "ls_adls"}])
    source = Source("p", "pl_load", "Pipeline", 4, PIPELINE, payload=BUNDLE)
    run, rest, _ = migrate([source], rest=rest)
    assert one(run).status == COMPLETED
    path, body = rest.created[0]
    assert path == f"/workspaces/{WS}/dataPipelines" and body["displayName"] == "pl_load"
    content = json.loads(base64.b64decode(body["definition"]["parts"][0]["payload"]))
    assert content["properties"]["activities"][1]["typeProperties"]["notebookId"] == "nb-1"
    # rerun: already there
    run, _, _ = migrate([source], rest=rest)
    assert one(run).status == SKIPPED
    # a missing connection is a failure that says what to run first
    run, _, _ = migrate([source], rest=StageRest(notebooks=[{"id": "nb-1", "displayName": "LoadSales"}], dataPipelines=[{"id": "pl-9", "displayName": "pl_child"}]))
    assert one(run).status == FAILED and "connection ls_adls" in one(run).error
    flow = {"resource": {"name": "df", "properties": {"activities": [{"name": "DF", "type": "ExecuteDataFlow"}]}}, "datasets": {}, "linkedServices": {}}
    run, rest, _ = migrate([Source("p", "df", "Pipeline", 4, PIPELINE, payload=flow)])
    assert one(run).status == DEFERRED_STATUS and "ExecuteDataFlow" in one(run).notes[0] and not rest.created


def test_datasets_are_folded_into_pipelines_and_a_missing_definition_is_an_error():
    run, _, _ = migrate([Source("d", "ds_dw", "Dataset", 3, DATASET, payload=DS_DW)])
    assert one(run).status == COMPLETED and "Embedded" in one(run).step
    run, _, _ = migrate([Source("p", "pl", "Pipeline", 4, PIPELINE, payload=None)])
    assert one(run).status == FAILED and "Run discovery again" in one(run).error


# =============================================================================
# Spark jobs, SQL scripts, schedules, shortcuts
# =============================================================================

SJD = {"name": "job1", "properties": {"language": "python", "targetBigDataPool": {"referenceName": "transportation"},
                                      "jobProperties": {"file": "abfss://c@a.dfs.core.windows.net/main.py", "args": ["--d", "1"],
                                                        "jars": ["abfss://c@a.dfs.core.windows.net/x.jar"], "conf": {"a": "b"}, "executorCores": 4}}}


def test_a_spark_job_keeps_its_file_and_arguments_and_attaches_the_environment():
    content, notes = jobs.spark_job(SJD, "env-1")
    assert content["executableFile"].endswith("main.py") and content["commandLineArguments"] == "--d 1"
    assert content["language"] == "Python" and content["environmentArtifactId"] == "env-1"
    assert content["additionalLibraryUris"] == ["abfss://c@a.dfs.core.windows.net/x.jar"]
    assert any("dropped" in n for n in notes)
    with pytest.raises(jobs.NotConvertible):
        jobs.spark_job({"properties": {"language": "dotnet", "jobProperties": {"file": "x"}}})
    with pytest.raises(jobs.NotConvertible, match="main file"):
        jobs.spark_job({"properties": {"language": "python", "jobProperties": {}}})


def test_the_spark_job_stage_creates_it_with_the_matching_environment():
    rest = StageRest(environments=[{"id": "env-7", "displayName": "transportation"}])
    run, rest, _ = migrate([Source("j", "job1", "Spark Job Definition", 4, SPARKJOB, payload=SJD)], rest=rest)
    assert one(run).status == COMPLETED
    path, body = rest.created[0]
    assert path.endswith("/sparkJobDefinitions") and body["definition"]["format"] == "SparkJobDefinitionV1"
    assert json.loads(base64.b64decode(body["definition"]["parts"][0]["payload"]))["environmentArtifactId"] == "env-7"


SCRIPT_RES = {"name": "q1", "properties": {"content": {"query": "SELECT 1;\nSELECT 2;", "metadata": {"language": "sql"}, "currentConnection": {"poolName": "pool01"}}}}


def test_a_sql_script_becomes_a_t_sql_notebook_bound_to_the_warehouse():
    document, notes = jobs.sql_script_notebook(SCRIPT_RES, "wh-1", "pool01")
    cell = document["cells"][0]
    assert "".join(cell["source"]) == "SELECT 1;\nSELECT 2;" and cell["metadata"]["microsoft"]["language_group"] == "sqldatawarehouse"
    assert document["metadata"]["dependencies"]["warehouse"]["default_warehouse"] == "wh-1"
    assert any("Review before running" in n for n in notes)
    with pytest.raises(jobs.NotConvertible):
        jobs.sql_script_notebook({"properties": {"content": {"query": "  "}}}, None, "w")


def test_the_script_stage_creates_a_notebook_and_skips_a_name_already_taken():
    run, rest, _ = migrate([Source("q", "q1", "SQL Script", 3, SCRIPT, payload=SCRIPT_RES)])
    assert one(run).status == COMPLETED and rest.created[0][0].endswith("/notebooks")
    run, rest, _ = migrate([Source("q", "q1", "SQL Script", 3, SCRIPT, payload=SCRIPT_RES)], rest=StageRest(notebooks=[{"id": "n", "displayName": "q1"}]))
    assert one(run).status == SKIPPED and not rest.created


def trigger(frequency, interval=1, schedule=None, kind="ScheduleTrigger", pipelines_=("pl_load",)):
    return {"name": "t1", "properties": {"type": kind, "runtimeState": "Started",
                                         "typeProperties": {"recurrence": {"frequency": frequency, "interval": interval, "startTime": "2026-01-05T06:30:00Z",
                                                                           "timeZone": "UTC", **({"schedule": schedule} if schedule else {})}},
                                         "pipelines": [{"pipelineReference": {"referenceName": p, "type": "PipelineReference"}} for p in pipelines_]}}


def test_trigger_recurrences_map_to_fabric_schedules_created_switched_off():
    (name, body), = jobs.schedule_bodies(trigger("Minute", 15))[0]
    assert name == "pl_load" and body["enabled"] is False and body["configuration"]["type"] == "Cron" and body["configuration"]["interval"] == 15
    assert jobs.schedule_bodies(trigger("Hour", 2))[0][0][1]["configuration"]["interval"] == 120
    daily = jobs.schedule_bodies(trigger("Day", 1, {"hours": [6, 18], "minutes": [0]}))[0][0][1]["configuration"]
    assert daily["type"] == "Daily" and daily["times"] == ["06:00", "18:00"]
    weekly = jobs.schedule_bodies(trigger("Week", 1, {"weekDays": ["Monday", "Friday"]}))[0][0][1]["configuration"]
    assert weekly["weekdays"] == ["Monday", "Friday"] and weekly["times"] == ["06:30"]
    for bad in (trigger("Day", 3), trigger("Month"), trigger("Hour", 1, kind="TumblingWindowTrigger")):
        with pytest.raises(jobs.NotConvertible):
            jobs.schedule_bodies(bad)


def test_the_schedule_stage_needs_the_pipeline_and_does_not_double_schedule():
    source = Source("t", "t1", "Trigger", 7, SCHEDULE, payload=trigger("Day"))
    run, _, _ = migrate([source])
    assert one(run).status == FAILED and "Pipelines stage first" in one(run).error
    rest = StageRest(dataPipelines=[{"id": "pl-1", "displayName": "pl_load"}])
    run, rest, _ = migrate([source], rest=rest)
    assert one(run).status == COMPLETED and "switched off" in one(run).step
    assert rest.created[0][0] == f"/workspaces/{WS}/items/pl-1/jobs/Pipeline/schedules"
    run, rest2, _ = migrate([source], rest=rest)
    assert one(run).status == SKIPPED and len(rest2.created) == 1


def test_shortcut_targets_come_from_the_pools_data_source_and_only_for_azure_storage():
    assert jobs.shortcut_target("abfss://raw@acct.dfs.core.windows.net/base", "/ext/orders/") == ("https://acct.dfs.core.windows.net", "/raw/base/ext/orders")
    assert jobs.shortcut_target("wasbs://c@a.blob.core.windows.net", "x")[1] == "/c/x"
    with pytest.raises(jobs.NotConvertible):
        jobs.shortcut_target("hdfs://namenode/x", "y")


def external(name="Ext"):
    return SqlTable(key=SqlObjectKey("pool", "ext", name), is_external=True, columns=table().columns)


def test_the_shortcut_stage_creates_a_lakehouse_and_a_shortcut_using_the_storage_connection():
    src = SourceDb(location=("abfss://raw@acct.dfs.core.windows.net/", "/orders/"))
    rest = StageRest(connections=[{"id": "conn-1", "displayName": "ls_adls", "connectionDetails": {"type": "AzureDataLakeStorage", "path": "https://acct.dfs.core.windows.net/raw"}}])
    run, rest, _ = migrate([Source("e", "ext.Ext", "External Table", 2, SHORTCUT, payload=external(), schema="ext", object_name="Ext")], rest=rest, source=src)
    assert one(run).status == COMPLETED
    paths = [p for p, _ in rest.created]
    assert paths[0].endswith("/lakehouses") and paths[1].endswith("/shortcuts")
    target = rest.created[1][1]["target"]["adlsGen2"]
    assert target == {"location": "https://acct.dfs.core.windows.net", "subpath": "/raw/orders", "connectionId": "conn-1"}


def test_the_shortcut_stage_explains_a_missing_connection_or_an_unsupported_location():
    ext = Source("e", "ext.Ext", "External Table", 2, SHORTCUT, payload=external(), schema="ext", object_name="Ext")
    run, _, _ = migrate([ext], source=SourceDb(location=("abfss://raw@acct.dfs.core.windows.net/", "/o/")))
    assert one(run).status == FAILED and "Connections stage" in one(run).error
    run, _, _ = migrate([ext], source=SourceDb(location=("hdfs://x", "/o/")))
    assert one(run).status == DEFERRED_STATUS and "by hand" in one(run).step
    run, _, _ = migrate([ext], source=SourceDb(location=None))
    assert one(run).status == DEFERRED_STATUS


# =============================================================================
# ordering, options, credentials, planning
# =============================================================================


def test_objects_run_in_the_order_they_depend_on_each_other():
    sources = [Source(k, k, "x", 4, k) for k in (SCHEDULE, PIPELINE, SPARKJOB, SCRIPT, DATA, TABLE, CONNECTION, DEFERRED)]
    ordered = [i.source.kind for i in MigrationRun("1", sources, WS, "W", "p").items]
    assert ordered == [CONNECTION, TABLE, DATA, SCRIPT, SPARKJOB, PIPELINE, SCHEDULE, DEFERRED]


def test_run_options_are_validated_and_credentials_are_checked_for_shape_only():
    assert parse_options({})["stages"] == list(capabilities.STAGE_KEYS) and parse_options({})["dataMode"] == "if_empty"
    for bad in ({"stages": ["nope"]}, {"stages": "data"}, {"dataMode": "wipe"}, {"maxRows": 0}, {"maxRows": "many"}, {"maxRows": 10**9}, {"scope": "x"}):
        with pytest.raises(ApiError) as exc:
            parse_options({"options": bad})
        assert exc.value.code == "invalid_options"
    assert parse_credentials(None) == {} and parse_credentials({"a": {"password": "p", "n": 5}}) == {"a": {"password": "p"}}
    for bad in ("x", {"a": "x"}, {1: {}}):
        with pytest.raises(ApiError):
            parse_credentials(bad)


def test_stages_decide_what_is_in_the_run_and_data_is_added_per_migrated_table():
    tbl = Source("t", "sales.Orders", "Table", 2, TABLE, payload=table(), schema="sales", object_name="Orders")
    ext = Source("e", "ext.Ext", "External Table", 2, SHORTCUT, payload=external())
    pl = Source("p", "pl", "Pipeline", 4, PIPELINE, payload=BUNDLE)
    everything = {"stages": list(capabilities.STAGE_KEYS), "scope": "all"}
    kinds = [s.kind for s in apply_stages([tbl, ext, pl], everything)]
    assert DATA in kinds and kinds.count(DATA) == 1  # not for the external table
    narrowed = apply_stages([tbl, ext, pl], {"stages": ["warehouse"], "scope": "all"})
    assert [s.kind for s in narrowed] == [TABLE, DEFERRED, DEFERRED] and "switched off" in narrowed[1].reason
    assert [s.kind for s in apply_stages([tbl, ext, pl], {"stages": ["warehouse"], "scope": "automated"})] == [TABLE]
    assert [s.id for s in data_sources([tbl, ext])] == ["t#data"]


def fake_job2():
    nb = SimpleNamespace(identity=SimpleNamespace(name="LoadSales", schema=None), content=None)
    items = [{"id": "p1", "name": "pl_load", "type": "Pipeline"}, {"id": "ls1", "name": "ls_sql", "type": "Linked Service"},
             {"id": "sp", "name": "transportation", "type": "Spark Pool"}, {"id": "tr", "name": "t1", "type": "Trigger"},
             {"id": "ir", "name": "AutoResolve", "type": "Integration Runtime"}]
    records = {i["id"]: SimpleNamespace(identity=SimpleNamespace(name=i["name"], schema=None), content=None) for i in items[:2]}
    artifacts = [SimpleNamespace(artifact=P0Artifact.PIPELINE, name="pl_load", payload=PIPELINE_RES),
                 SimpleNamespace(artifact=P0Artifact.LINKED_SERVICE, name="ls_sql", payload=SQL_LS),
                 SimpleNamespace(artifact=P0Artifact.LINKED_SERVICE, name="ls_adls", payload=ADLS_LS),
                 SimpleNamespace(artifact=P0Artifact.DATASET, name="ds_dw", payload=DS_DW),
                 SimpleNamespace(artifact=P0Artifact.DATASET, name="ds_csv", payload=DS_CSV)]
    extras = {"sp": SimpleNamespace(name="transportation", metadata={"nodeSize": "Small"}, raw=None),
              "tr": SimpleNamespace(name="t1", metadata={}, raw=trigger("Day"))}
    return SimpleNamespace(items=items, records_by_id=records, extras_by_id=extras, run=SimpleNamespace(synapse=SimpleNamespace(artifacts=tuple(artifacts))),
                           graph={"nodes": [{**i, "classification": "DIRECT", "fabricTarget": "t", "wave": 1, "dependedOnBy": 0} for i in items], "edges": []})


def test_extras_and_pipeline_bundles_are_built_from_discovery_not_deferred():
    plan = [{"id": i, "wave": 1} for i in ("p1", "ls1", "sp", "tr", "ir")]
    got = {s.id: s for s in sources_for(plan, fake_job2(), "pool01")}
    assert got["sp"].kind == "environment" and got["sp"].payload == {"nodeSize": "Small"}  # an extra: no record, still handled
    assert got["tr"].kind == SCHEDULE and got["tr"].payload["properties"]["type"] == "ScheduleTrigger"
    assert got["ls1"].kind == CONNECTION and got["ls1"].payload["properties"]["type"] == "AzureSqlDW"
    bundle = got["p1"].payload
    assert set(bundle["datasets"]) == {"ds_dw", "ds_csv"} and set(bundle["linkedServices"]) == {"ls_sql", "ls_adls"}
    assert got["ir"].kind == DEFERRED  # an integration runtime is always by hand


def test_planning_flags_what_would_fail_before_anything_runs():
    job = fake_job2()
    sources = apply_stages(sources_for([{"id": i, "wave": 1} for i in ("p1", "ls1", "tr")], job, "pool01"), {"stages": list(capabilities.STAGE_KEYS), "scope": "all"})
    codes = {f.code for f in content_findings(sources, set())}
    assert "NEEDS_CREDENTIALS" in codes
    assert "NEEDS_CREDENTIALS" not in {f.code for f in content_findings(sources, {"ls_sql"})}
    flow = Source("p", "df", "Pipeline", 4, PIPELINE, payload={"resource": {"properties": {"activities": [{"name": "DF", "type": "ExecuteDataFlow"}]}}, "datasets": {}, "linkedServices": {}})
    bad_script = Source("q", "q", "SQL Script", 3, SCRIPT, payload={"properties": {"content": {"query": "CREATE TABLE x WITH (DISTRIBUTION = ROUND_ROBIN) AS SELECT 1", "metadata": {"language": "sql"}}}})
    bad_trigger = Source("t", "t", "Trigger", 7, SCHEDULE, payload=trigger("Month"))
    codes = {f.code for f in content_findings([flow, bad_script, bad_trigger], set())}
    assert {"NEEDS_REWRITE", "SYNAPSE_TSQL", "RECREATE_BY_HAND"} <= codes


def test_the_source_check_appears_only_when_a_stage_needs_the_pool():
    off = environment_checks({"status": "connected", "workspaceName": "W", "capacityAssigned": True}, "ODBC Driver 18 for SQL Server")
    on = environment_checks({"status": "connected", "workspaceName": "W", "capacityAssigned": True}, "ODBC Driver 18 for SQL Server", source_connected=False)
    assert len(on) == len(off) + 1 and on[-1]["status"] == "fail"


def test_the_service_exposes_the_stages_and_the_linked_services_that_need_credentials():
    svc = MigrationService(FakeSession(fake_job2()), SimpleNamespace(state=lambda: {"status": "connected", "workspaceName": "W", "capacityAssigned": True},
                                                                  migration_target=lambda: (WS, "W", "azure_cli")), rest_factory=lambda: StageRest())
    caps = svc.capabilities()
    assert [s["key"] for s in caps["stages"]] == list(capabilities.STAGE_KEYS)
    data = next(s for s in caps["stages"] if s["key"] == "data")
    assert data["options"][0]["key"] == "dataMode" and {c["value"] for c in data["options"][0]["choices"]} == {"if_empty", "replace"}
    assert {l["name"] for l in caps["linkedServices"]} == {"ls_sql", "ls_adls"}
    assert next(l for l in caps["linkedServices"] if l["name"] == "ls_adls")["needsPath"] is True
    assert "TOPSECRET" not in json.dumps(caps)
