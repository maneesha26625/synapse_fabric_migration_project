"""Storage read directly by code, routed through OneLake shortcuts instead. Offline.

The cases come from a real workspace whose storage account is in another Microsoft Entra tenant than Fabric: every
notebook, Spark job and COPY that read it directly failed with 401, while a Fabric connection with the account key
reached it. Paths are built literally, from f-strings, and from T-SQL concatenation; all three are covered.
"""

from __future__ import annotations

import json

import pytest

from discovery_agent.api.migration import parse_options
from discovery_agent.api.service import ApiError
from discovery_agent.migration import onelake, storage_remap
from discovery_agent.migration.common import COMPLETED, NOTEBOOK, PIPELINE, PROCEDURE, SCRIPT, SPARKJOB, Source
from discovery_agent.migration.preflight import content_findings
from discovery_agent.migration.storage_remap import Route, Routes, rewrite
from discovery_agent.sql.models import CatalogObjectType, DefinitionState, SqlModuleDefinition, SqlObjectKey, SqlProcedure

from test_migration import FakeDb, notebook_payload
from test_migration_stages import StageRest, migrate, one

ACCT, CONT = "contosoextstorage", "migrationcontainer"
ROUTES = Routes([Route(ACCT, CONT, "ws-1", "lh-1", CONT)])
ONE = "abfss://ws-1@onelake.dfs.fabric.microsoft.com/lh-1/Files"
SPEC = [{"account": ACCT, "container": CONT, "connectionId": "9f8e7d6c-aaaa-4bbb-8ccc-ddddeeeeffff"}]


# -- the rewrite -------------------------------------------------------------------------------------

@pytest.mark.parametrize("code, expected", [
    (f'p = "abfss://{CONT}@{ACCT}.dfs.core.windows.net/raw/x.csv"', f'p = "{ONE}/{CONT}/raw/x.csv"'),
    ('base = f"abfss://{args.container}@{args.storage_account}.dfs.core.windows.net/nyccab"', f'base = f"{ONE}/{{args.container}}/nyccab"'),
    (f'base = f"abfss://{{container}}@{ACCT}.dfs.core.windows.net/x"', f'base = f"{ONE}/{{container}}/x"'),
    (f"OPENROWSET(BULK 'https://{ACCT}.dfs.core.windows.net/{CONT}/csv/*.csv')",
     f"OPENROWSET(BULK 'https://onelake.dfs.fabric.microsoft.com/ws-1/lh-1/Files/{CONT}/csv/*.csv')"),
    ("DECLARE @r varchar(500) = 'https://' + @StorageAccount + '.dfs.core.windows.net/' + @Container + '/curated/';",
     "DECLARE @r varchar(500) = 'https://onelake.dfs.fabric.microsoft.com/ws-1/lh-1/Files/' + @Container + '/curated/';"),
])
def test_every_way_of_writing_a_path_goes_through_the_shortcut(code, expected):
    out, notes = rewrite(code, ROUTES)
    assert out == expected and notes and notes[0].startswith("Storage paths now go through the OneLake shortcut")


def test_a_location_without_a_route_is_left_and_said_so():
    out, notes = rewrite("x = 'abfss://other@elsewhere.dfs.core.windows.net/a'", ROUTES)
    assert out == "x = 'abfss://other@elsewhere.dfs.core.windows.net/a'" and "other@elsewhere" in notes[-1]


def test_an_account_from_a_variable_is_left_when_two_accounts_are_routed():
    two = Routes([Route(ACCT, CONT, "w", "l", CONT), Route("second", "c", "w", "l", "c")])
    code = 'f"abfss://{c}@{a}.dfs.core.windows.net/x"'
    assert rewrite(code, two)[0] == code


def test_suggestions_find_locations_and_the_connection_that_reaches_them():
    found = storage_remap.locations([f"abfss://{CONT}@{ACCT}.dfs.core.windows.net/a", f"https://{ACCT}.dfs.core.windows.net/{CONT}/b"])
    assert found == {(ACCT, CONT): 2}
    suggested = storage_remap.suggest(found, [
        {"id": "c-account", "name": "whole account", "path": f"https://{ACCT}.dfs.core.windows.net"},
        {"id": "c-exact", "name": "LS_ADLS", "path": f"https://{ACCT}.dfs.core.windows.net/{CONT}"},
        {"id": "c-other", "name": "other", "path": "https://zzz.dfs.core.windows.net/c"}])
    assert suggested == [{"account": ACCT, "container": CONT, "uses": 2, "connectionId": "c-exact", "connectionName": "LS_ADLS",
                          "linkedService": None}]
    assert storage_remap.suggest(found, [])[0]["connectionId"] is None


def test_storage_routes_are_validated():
    assert parse_options({"options": {"storageRoutes": SPEC}})["storageRoutes"] == SPEC
    assert parse_options({"options": {"storageRoutes": [{"account": ACCT, "container": CONT, "linkedService": "LS_ADLS"}]}})["storageRoutes"][0]["linkedService"] == "LS_ADLS"
    for bad in ([{"account": "Bad Name!", "container": CONT, "connectionId": SPEC[0]["connectionId"]}],
                [{"account": ACCT, "container": CONT}], "nope"):
        with pytest.raises(ApiError):
            parse_options({"options": {"storageRoutes": bad}})


# -- the plan ---------------------------------------------------------------------------------------

def nb(cells):
    return Source("n", "NB_Read_Raw_Data", "Notebook", 1, NOTEBOOK, payload=notebook_payload(
        cells=[{"cell_type": "code", "source": [c], "metadata": {}} for c in cells]))


def codes(findings):
    return {f.code for f in findings}


def test_routed_storage_is_a_low_note_and_unrouted_storage_explains_the_tenant():
    src = nb([f"df.write.csv('abfss://{CONT}@{ACCT}.dfs.core.windows.net/x')"])
    unrouted = [f for f in content_findings([src]) if f.code == "EXTERNAL_STORAGE"][0]
    assert "another Microsoft Entra tenant" in unrouted.message and "OneLake shortcut" in unrouted.message
    routed = content_findings([src], storage_routes=SPEC)
    assert "STORAGE_ROUTED" in codes(routed) and "EXTERNAL_STORAGE" not in codes(routed)


def test_a_pipeline_that_runs_unrouted_code_is_flagged_too():
    src = nb([f"spark.read.csv('abfss://{CONT}@{ACCT}.dfs.core.windows.net/x')"])
    pipe = Source("p", "PL_Ingest", "Pipeline", 1, PIPELINE, payload={"resource": {"name": "PL_Ingest", "properties": {"activities": [
        {"name": "Run", "type": "SynapseNotebook", "typeProperties": {"notebook": {"referenceName": "NB_Read_Raw_Data", "type": "NotebookReference"}}}]}},
        "datasets": {}, "linkedServices": {}})
    flagged = [f for f in content_findings([src, pipe]) if f.code == "DEPENDS_ON_STORAGE"]
    assert flagged and "NB_Read_Raw_Data" in flagged[0].message
    assert "DEPENDS_ON_STORAGE" not in codes(content_findings([src, pipe], storage_routes=SPEC))


def test_serverless_scripts_and_pipelines_are_called_out():
    script = Source("s", "SQL_Serverless_CSV", "SQL Script", 1, SCRIPT, payload={"properties": {"content": {
        "query": "SELECT 1", "metadata": {"language": "sql"}, "currentConnection": {"databaseName": "master", "poolName": "Built-in"}}}})
    pipe = Source("p", "PL_Serverless_Test", "Pipeline", 1, PIPELINE, payload={"resource": {"properties": {"activities": [
        {"name": "Serverless_Assessment", "type": "Script", "linkedServiceName": {"referenceName": "LS_Serverless_SQL"}, "typeProperties": {}}]}},
        "datasets": {}, "linkedServices": {"LS_Serverless_SQL": {"properties": {"type": "AzureSqlDatabase", "typeProperties": {
            "connectionString": "Data Source=ws-ondemand.sql.azuresynapse.net;Initial Catalog=master"}}}}})
    found = [f for f in content_findings([script, pipe]) if f.code == "SERVERLESS_SQL"]
    assert len(found) == 2 and "Serverless_Assessment" in found[1].message


# -- the run ---------------------------------------------------------------------------------------

class ShortcutRest(StageRest):
    def __init__(self, **seed):
        super().__init__(lakehouses=[{"id": "lh-1", "displayName": "pool01_lakehouse"}], **seed)
        self.shortcuts = []

    def create(self, path, body):
        if path.endswith("/shortcuts"):
            self.shortcuts.append((path, body))
            return {}
        return super().create(path, body)


class FakeFiles:
    def __init__(self, files):
        self.files, self.written = dict(files), {}

    def read(self, ws, lh, path):
        return self.files[(ws, lh, path)]

    def write(self, ws, lh, path, data):
        self.written[(ws, lh, path)] = data


def run_routed(sources, rest=None, files=None, db=None):
    from discovery_agent.migration.runner import MigrationRun, Migrator
    from test_migration import WS
    from test_migration_stages import DataDb
    rest = rest or ShortcutRest()
    run = MigrationRun("001", sources, WS, "Sales WS", "pool01")
    run.pool_name = "pool01"
    run.settings.update({"storageRoutes": SPEC})
    Migrator(run, lambda: rest, lambda h, d: db or DataDb(), onelake=files).execute()
    return run, rest


def test_the_run_creates_the_shortcut_once_and_rewrites_a_notebook():
    run, rest = run_routed([nb([f"df = spark.read.csv('abfss://{CONT}@{ACCT}.dfs.core.windows.net/raw')"]),
                            Source("n2", "NB_Two", "Notebook", 1, NOTEBOOK, payload=notebook_payload(
                                cells=[{"cell_type": "code", "source": ['f"abfss://{c}@{a}.dfs.core.windows.net/x"'], "metadata": {}}]))])
    assert [i.status for i in run.items] == [COMPLETED, COMPLETED]
    assert len(rest.shortcuts) == 1
    path, body = rest.shortcuts[0]
    assert path.endswith("/items/lh-1/shortcuts") and body["path"] == "Files" and body["name"] == CONT
    assert body["target"]["adlsGen2"] == {"location": f"https://{ACCT}.dfs.core.windows.net", "subpath": f"/{CONT}",
                                         "connectionId": SPEC[0]["connectionId"]}
    created = [b for p, b in rest.created if p.endswith("/notebooks")][0]
    import base64
    doc = json.loads(base64.b64decode(created["definition"]["parts"][0]["payload"]))
    code = "".join(doc["cells"][0]["source"])
    assert "onelake.dfs.fabric.microsoft.com/lh-1/Files/migrationcontainer/raw" in code and ".core.windows.net" not in code
    assert any("OneLake shortcut" in n for n in run.items[0].notes)


def test_a_procedure_building_the_path_in_t_sql_is_rewritten():
    text = ("CREATE PROC operation.usp_Load @A varchar(100), @C varchar(100) AS "
            "DECLARE @r varchar(500) = 'https://' + @A + '.dfs.core.windows.net/' + @C + '/curated/'; SELECT @r;")
    proc = SqlProcedure(key=SqlObjectKey("pool", "operation", "usp_Load", CatalogObjectType.PROCEDURE),
                        definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text=text))
    db = FakeDb()
    run, _ = run_routed([Source("p", "operation.usp_Load", "Stored Procedure", 1, PROCEDURE, payload=proc,
                                schema="operation", object_name="usp_Load")], db=db)
    assert one(run).status == COMPLETED
    created = [s for s in db.statements if s.startswith("CREATE PROC")][0]
    assert "'https://onelake.dfs.fabric.microsoft.com/" in created and "core.windows.net" not in created


def test_a_spark_job_runs_a_rewritten_copy_of_its_main_file_and_the_original_is_untouched():
    sjd = {"name": "JBD_Process", "properties": {"language": "python", "jobProperties": {
        "file": f"abfss://{CONT}@{ACCT}.dfs.core.windows.net/jobs/process.py", "args": ["--storage-account", ACCT]}}}
    from test_migration import WS
    main = 'base = f"abfss://{args.container}@{args.storage_account}.dfs.core.windows.net/nyccab"\n'
    files = FakeFiles({(WS, "lh-1", f"Files/{CONT}/jobs/process.py"): main.encode()})
    run, rest = run_routed([Source("j", "JBD_Process", "Spark Job Definition", 1, SPARKJOB, payload=sjd)], files=files)
    assert one(run).status == COMPLETED, one(run).error
    (ws, lh, path), data = next(iter(files.written.items()))
    assert path == "Files/_migrated_jobs/JBD_Process/process.py" and b"onelake.dfs.fabric.microsoft.com" in data
    assert files.files[(WS, "lh-1", f"Files/{CONT}/jobs/process.py")] == main.encode()  # the original stays as it was
    import base64
    body = [b for p, b in rest.created if p.endswith("/sparkJobDefinitions")][0]
    content = json.loads(base64.b64decode(body["definition"]["parts"][0]["payload"]))
    assert content["executableFile"] == f"abfss://{WS}@onelake.dfs.fabric.microsoft.com/lh-1/Files/_migrated_jobs/JBD_Process/process.py"


def test_a_route_named_by_linked_service_uses_the_connection_the_run_created():
    rest = ShortcutRest(connections=[{"id": "conn-9", "displayName": "LS_ADLS_external"}])
    from discovery_agent.migration.runner import MigrationRun, Migrator
    from test_migration import WS
    from test_migration_stages import DataDb
    run = MigrationRun("001", [nb([f"x = 'abfss://{CONT}@{ACCT}.dfs.core.windows.net/a'"])], WS, "Sales WS", "pool01")
    run.settings.update({"storageRoutes": [{"account": ACCT, "container": CONT, "linkedService": "LS_ADLS_external"}]})
    Migrator(run, lambda: rest, lambda h, d: DataDb()).execute()
    assert rest.shortcuts[0][1]["target"]["adlsGen2"]["connectionId"] == "conn-9"


def test_onelake_files_create_append_and_flush():
    calls = []

    def send(method, url, headers, body):
        calls.append((method, url.split("?")[-1] if "?" in url else "", body))
        return 200, {}, b"hello" if method == "GET" else b""

    files = onelake.OneLakeFiles(lambda: "t", send=send)
    assert files.read("w", "l", "Files/a b.py") == b"hello"
    files.write("w", "l", "Files/x.py", b"abc")
    assert [c[:2] for c in calls[1:]] == [("PUT", "resource=file"), ("PATCH", "action=append&position=0"), ("PATCH", "action=flush&position=3")]
    assert onelake.lakehouse_path("abfss://w@onelake.dfs.fabric.microsoft.com/l/Files/a/b.py") == ("w", "l", "Files/a/b.py")
    assert onelake.lakehouse_path("abfss://c@acct.dfs.core.windows.net/x") is None


def test_a_routed_notebook_is_not_also_told_to_review_its_storage_paths():
    src = nb([f"df = spark.read.csv('abfss://{CONT}@{ACCT}.dfs.core.windows.net/x')\nTokenLibrary.getSecret('kv', 's')"])
    unrouted = [f.message for f in content_findings([src]) if f.code == "NOTEBOOK_REVIEW"][0]
    assert "direct storage paths" in unrouted and "TokenLibrary" in unrouted
    routed = [f.message for f in content_findings([src], storage_routes=SPEC) if f.code == "NOTEBOOK_REVIEW"]
    assert len(routed) == 1 and "direct storage paths" not in routed[0] and "TokenLibrary" in routed[0]
    only_storage = nb([f"df = spark.read.csv('abfss://{CONT}@{ACCT}.dfs.core.windows.net/x')"])
    assert "NOTEBOOK_REVIEW" not in codes(content_findings([only_storage], storage_routes=SPEC))


def test_a_serverless_script_activity_runs_on_the_warehouse_with_its_storage_routed():
    """A real pipeline's Script activity still ran in Synapse's serverless pool, reading storage directly:
    'Cannot find the CREDENTIAL https://acct.dfs.core.windows.net/...'."""
    from discovery_agent.migration import pipelines
    serverless = {"properties": {"type": "AzureSqlDatabase", "typeProperties": {
        "connectionString": "Data Source=ws-ondemand.sql.azuresynapse.net;Initial Catalog=master"}}}
    query = f"SELECT COUNT(*) FROM OPENROWSET(BULK 'https://{ACCT}.dfs.core.windows.net/{CONT}/csv/*.csv', FORMAT='CSV') r"
    resource = {"name": "PL_Serverless_Test", "properties": {"activities": [
        {"name": "Serverless_Assessment", "type": "Script", "linkedServiceName": {"referenceName": "LS_Serverless_SQL"},
         "typeProperties": {"scripts": [{"type": "Query", "text": query}]}}]}}
    rest = ShortcutRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}])
    src = Source("p", "PL_Serverless_Test", "Pipeline", 1, PIPELINE,
                 payload={"resource": resource, "datasets": {}, "linkedServices": {"LS_Serverless_SQL": serverless}})
    run, rest = run_routed([src], rest=rest)
    assert one(run).status == COMPLETED, one(run).error
    import base64
    body = [b for p, b in rest.created if p.endswith("/dataPipelines")][0]
    act = json.loads(base64.b64decode(body["definition"]["parts"][0]["payload"]))["properties"]["activities"][0]
    assert act["linkedService"]["properties"]["type"] == "DataWarehouse" and "externalReferences" not in act
    text = act["typeProperties"]["scripts"][0]["text"]
    from test_migration import WS
    assert f"onelake.dfs.fabric.microsoft.com/{WS}/lh-1/Files/migrationcontainer/csv" in text and "core.windows.net" not in text
    assert pipelines.is_serverless_service(serverless) and not pipelines.is_serverless_service({"properties": {"typeProperties": {
        "connectionString": "Data Source=ws.sql.azuresynapse.net;Initial Catalog=pool01"}}})
