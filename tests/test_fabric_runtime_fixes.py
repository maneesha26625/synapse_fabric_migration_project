"""Fixes for objects that were created in Fabric but failed when they ran. Offline.

Each case comes from a real failure in a migrated workspace:

* a master pipeline failed with "The external references connection cannot be found in the trident payload":
  Execute Pipeline had been converted to InvokePipeline, which needs a Fabric connection;
* a procedure failed on ``COPY INTO ... CREDENTIAL = (IDENTITY = 'Managed Identity')`` built as dynamic SQL;
* a procedure failed on ``SET IDENTITY_INSERT`` for a table migrated without IDENTITY;
* notebooks, Spark jobs and COPY / OPENROWSET failed with 401 / "Cannot find the CREDENTIAL" on the storage account
  the Synapse managed identity could read: now a finding before the run.
"""

from __future__ import annotations

from discovery_agent.migration import pipelines, tsql_rules
from discovery_agent.migration.common import NOTEBOOK, PROCEDURE, SCRIPT, SPARKJOB, Source
from discovery_agent.migration.preflight import content_findings
from discovery_agent.sql.models import CatalogObjectType, DefinitionState, SqlModuleDefinition, SqlObjectKey, SqlProcedure

DYNAMIC_COPY = ("SET @sql = 'COPY INTO stage.G FROM ''' + @root + 'green/*.parquet'' "
                "WITH (FILE_TYPE = ''PARQUET'', CREDENTIAL = (IDENTITY = ''Managed Identity''))';\nEXEC (@sql);")


# -- T-SQL rules -----------------------------------------------------------------------------------------

def test_the_managed_identity_credential_is_removed_from_copy_even_inside_dynamic_sql():
    r = tsql_rules.rewrite(DYNAMIC_COPY)
    assert "Managed Identity" not in r.text and "WITH (FILE_TYPE = ''PARQUET'')'" in r.text
    assert any("Storage Blob Data Reader" in n for n in r.notes)


def test_the_credential_is_removed_wherever_it_sits_in_the_options():
    first = tsql_rules.rewrite("COPY INTO t FROM 'x' WITH (CREDENTIAL = (IDENTITY = 'Managed Identity'), FILE_TYPE = 'CSV');").text
    assert first == "COPY INTO t FROM 'x' WITH ( FILE_TYPE = 'CSV');"
    alone = tsql_rules.rewrite("COPY INTO t FROM 'x' WITH (CREDENTIAL = (IDENTITY = 'Managed Identity'));").text
    assert alone == "COPY INTO t FROM 'x' ;"
    sas = "COPY INTO t FROM 'x' WITH (CREDENTIAL = (IDENTITY = 'Shared Access Signature', SECRET = 's'));"
    assert tsql_rules.rewrite(sas).text == sas  # only Synapse's managed identity is unsupported


def test_identity_insert_is_removed_in_code_and_in_dynamic_sql_but_not_in_comments():
    sql = ("-- SET IDENTITY_INSERT dw.D ON is needed in Synapse\nSET IDENTITY_INSERT dw.DimLocation ON;\n"
           "INSERT INTO dw.DimLocation (LocationKey) SELECT 1;\nSET IDENTITY_INSERT [dw].[DimLocation] OFF\n"
           "EXEC('SET IDENTITY_INSERT dw.X ON; INSERT dw.X VALUES (1)');")
    r = tsql_rules.rewrite(sql)
    assert r.text.startswith("-- SET IDENTITY_INSERT dw.D ON is needed in Synapse\n")
    assert r.text.count("/* removed for Fabric: SET IDENTITY_INSERT") == 3 and "INSERT INTO dw.DimLocation (LocationKey) SELECT 1;" in r.text
    assert tsql_rules.rewrite(r.text).text == r.text  # safe to repeat


# -- pipelines -------------------------------------------------------------------------------------------

def test_execute_pipeline_becomes_execute_pipeline_by_id_without_a_connection():
    ctx = pipelines.Context(datasets={}, linked_services={}, connections={}, notebooks={}, pipelines={"child": "pl-1"},
                            spark_jobs={}, workspace_id="ws", warehouse=None)
    act = {"name": "Run child", "type": "ExecutePipeline",
           "typeProperties": {"pipeline": {"referenceName": "child", "type": "PipelineReference"}, "waitOnCompletion": True,
                              "parameters": {"p": 1}}}
    out = pipelines.convert({"properties": {"activities": [act]}}, ctx)
    got = out.definition["properties"]["activities"][0]
    assert got["type"] == "ExecutePipeline" and "externalReferences" not in got
    assert got["typeProperties"] == {"pipeline": {"referenceName": "pl-1", "type": "PipelineReference"}, "waitOnCompletion": True,
                                     "parameters": {"p": 1}}


# -- the plan warns about storage access -------------------------------------------------------------

def proc(text):
    payload = SqlProcedure(key=SqlObjectKey("pool", "operation", "usp_Load", CatalogObjectType.PROCEDURE),
                           definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text=text))
    return Source("p", "operation.usp_Load", "Stored Procedure", 1, PROCEDURE, payload=payload, schema="operation", object_name="usp_Load")


def storage(findings):
    return [f for f in findings if f.code == "EXTERNAL_STORAGE"]


def test_code_that_uses_a_storage_account_directly_is_flagged_before_the_run():
    nb = Source("n", "NB_Read_Raw_Data", "Notebook", 1, NOTEBOOK, payload={"properties": {"cells": [
        {"cell_type": "code", "source": ["df.write.csv('abfss://migrationcontainer@contosoextstorage.dfs.core.windows.net/x')"]}]}})
    found = storage(content_findings([nb]))
    assert len(found) == 1 and found[0].severity == "MEDIUM" and "contosoextstorage" in found[0].message
    built = storage(content_findings([proc("DECLARE @r varchar(99) = 'https://' + @Acct + '.dfs.core.windows.net/'; " + DYNAMIC_COPY)]))
    assert "a storage account built from variables" in built[0].message
    script = Source("s", "load", "SQL Script", 1, SCRIPT, payload={"properties": {"content": {
        "query": "SELECT * FROM OPENROWSET(BULK 'https://acct2.blob.core.windows.net/c/*.csv', FORMAT = 'CSV') AS r"}}})
    assert "acct2" in storage(content_findings([script]))[0].message
    assert not storage(content_findings([proc("CREATE PROC p AS SELECT 1")]))


def test_a_spark_job_whose_main_file_is_on_the_storage_account_is_a_high_risk():
    sjd = Source("j", "JBD_Process_Cab_Data", "Spark Job Definition", 1, SPARKJOB, payload={"properties": {"jobProperties": {
        "file": "abfss://migrationcontainer@contosoextstorage.dfs.core.windows.net/jobs/process_cab_data.py"}}})
    found = storage(content_findings([sjd]))
    assert found[0].severity == "HIGH" and "main file" in found[0].message


def test_an_accepted_transform_is_what_gets_checked_for_storage_access():
    clean = proc("CREATE PROC p AS COPY INTO t FROM 'https://acct.dfs.core.windows.net/c/x';")
    clean.override = {"code": "CREATE PROC p AS INSERT t SELECT * FROM lake.dbo.x;", "acceptedAt": "x"}
    assert not storage(content_findings([clean]))
