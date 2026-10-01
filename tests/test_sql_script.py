"""Tests for SQLScriptExtractor.

**Entirely synthetic.** The sample repository contains zero SQL script
artifacts, so nothing here has been exercised against real data. The fixtures
are built to the same contract the artifact detector already encodes —
``properties.content.query`` plus ``properties.type == "SqlQuery"`` — which
is the one verified anchor available.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from discovery_agent.acquisition.models import RepositorySource
from discovery_agent.artifacts.models import (
    ArtifactCategory,
    DetectedArtifact,
    DetectionEvidence,
    DiscoverySource,
    Signal,
    SignalType,
)
from discovery_agent.discovery_models import DefinitionFacet, UnifiedDiscoveryRecord
from discovery_agent.extractors import (
    ExtractionContext,
    ExtractionStatus,
    ExtractorOrchestrator,
    IssueCode,
    ReferenceKind,
    RepositoryArtifactSource,
    SourceType,
    SQLScriptExtractor,
    default_registry,
)
from discovery_agent.extractors.common_models import SecretKind
from discovery_agent.extractors.sql_scanning import (
    blank_comments_and_literals,
    split_qualified_name,
)
from discovery_agent.extractors.sql_models import (
    SqlFeatureKind,
    SqlObjectKind,
    SqlOperation,
)
from discovery_agent.models import AssetType
from discovery_agent.source_strategy import P0Artifact

FAKE_SECRET = "NOT-A-REAL-SECRET-qqq111"


# --- fixtures ----------------------------------------------------------------


def make_artifact(name="SQL_Test", path=None) -> DetectedArtifact:
    return DetectedArtifact(
        artifact_type="sqlscript",
        artifact_name=name,
        source_path=path or f"workspace/sqlscript/{name}.json",
        source_format="synapse_sql_script_json",
        confidence=1.0,
        discovery_source=DiscoverySource.PATH_AND_STRUCTURE,
        category=ArtifactCategory.SYNAPSE,
        evidence=DetectionEvidence((Signal(SignalType.PATH, "sqlscript/"),)),
        sha256="b" * 64,
    )


def sql_script_json(query="SELECT 1", name="SQL_Test", content=None, **properties):
    body = {"query": query}
    body.update(content or {})
    document = {"name": name, "properties": {"content": body, "type": "SqlQuery"}}
    document["properties"].update(properties)
    return document


def extract(tmp_path, document, artifact=None, repository=None, raw=None):
    artifact = artifact or make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        raw if raw is not None else json.dumps(document), encoding="utf-8"
    )
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path, repository))
    return SQLScriptExtractor().extract(artifact, context)


def objects_of(definition):
    return {o.qualified_name: o for o in definition.objects}


# --- 1, 2: minimal script and SQL preservation -------------------------------


def test_minimal_sql_script(tmp_path):
    result = extract(tmp_path, sql_script_json("SELECT 1", name="SQL_Probe"))

    assert result.status is ExtractionStatus.SUCCESS
    definition = result.content
    assert definition.name == "SQL_Probe"
    assert definition.type == "SqlQuery"
    assert definition.sql_text == "SELECT 1"
    assert definition.recognized is True


def test_sql_text_is_preserved_byte_for_byte(tmp_path):
    query = (
        "-- a comment\r\n"
        "SELECT   a ,\tb\n"
        "  FROM dbo.Thing   -- trailing\n"
        "\n"
        "/* block */\n"
    )
    result = extract(tmp_path, sql_script_json(query))

    assert result.content.sql_text == query
    assert result.content.line_count == len(query.splitlines())
    assert result.content.character_count == len(query)


def test_an_empty_query_is_valid_not_malformed(tmp_path):
    result = extract(tmp_path, sql_script_json(""))

    assert result.status is ExtractionStatus.SUCCESS
    assert result.content.sql_text == ""
    assert result.content.is_empty
    assert result.content.objects == ()


# --- 3, 4: metadata ----------------------------------------------------------


def test_folder_description_annotations_and_language(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json(
            "SELECT 1",
            content={"metadata": {"language": "sql"}, "resultLimit": 5000},
            folder={"name": "Build"},
            description="Rebuilds the aggregate.",
            annotations=["zulu", "alpha"],
        ),
    )

    definition = result.content
    assert definition.folder == "Build"
    assert definition.description == "Rebuilds the aggregate."
    assert definition.annotations == ("alpha", "zulu")
    assert definition.language == "sql"
    assert {s.key: s.value for s in definition.settings}["resultLimit"] == "5000"


def test_current_connection_and_serverless_detection(tmp_path):
    dedicated = extract(
        tmp_path,
        sql_script_json(
            "SELECT 1",
            content={
                "currentConnection": {
                    "databaseName": "poolone",
                    "poolName": "poolone",
                    "type": "SqlPool",
                }
            },
        ),
    )
    serverless = extract(
        tmp_path,
        sql_script_json(
            "SELECT 1",
            content={
                "currentConnection": {"databaseName": "master", "poolName": "Built-in"}
            },
        ),
    )

    assert dedicated.content.connection.pool_name == "poolone"
    assert dedicated.content.connection.database_name == "poolone"
    assert dedicated.content.connection.is_serverless is False
    assert serverless.content.connection.is_serverless is True


def test_parameters_are_read_defensively(tmp_path):
    """The standard artifact does not declare them; the field is read anyway."""
    result = extract(
        tmp_path,
        sql_script_json(
            "SELECT 1",
            parameters={
                "zeta": {"type": "string", "defaultValue": "z"},
                "alpha": {"type": "string"},
            },
        ),
    )

    parameters = {p.name: p for p in result.content.parameters}
    assert [p.name for p in result.content.parameters] == ["alpha", "zeta"]
    assert parameters["zeta"].default_value == "z"
    assert parameters["alpha"].has_default is False


def test_no_parameters_is_the_normal_case(tmp_path):
    result = extract(tmp_path, sql_script_json("SELECT 1"))

    assert result.content.parameters == ()


# --- 5, 6, 7, 8: object references -------------------------------------------


def test_table_references_by_statement(tmp_path):
    query = (
        "INSERT INTO dbo.Target SELECT * FROM dbo.Source;\n"
        "TRUNCATE TABLE dbo.Staging;\n"
        "UPDATE dbo.Counters SET n = 1;\n"
        "DELETE FROM dbo.Old;\n"
        "CREATE TABLE dbo.New (id INT);\n"
        "DROP TABLE dbo.Gone;\n"
    )
    result = extract(tmp_path, sql_script_json(query))
    found = objects_of(result.content)

    assert found["dbo.Target"].operation is SqlOperation.WRITE
    assert found["dbo.Source"].operation is SqlOperation.READ
    assert found["dbo.Staging"].operation is SqlOperation.TRUNCATE
    assert found["dbo.Counters"].operation is SqlOperation.WRITE
    assert found["dbo.Old"].operation is SqlOperation.WRITE
    assert found["dbo.New"].kind is SqlObjectKind.TABLE
    assert found["dbo.Gone"].operation is SqlOperation.DROP
    # DELETE FROM must not also be reported as a bare FROM.
    assert len([o for o in result.content.objects if o.object_name == "Old"]) == 1


def test_a_from_target_kind_is_honestly_unknown(tmp_path):
    """A FROM target could be a table, a view or a synonym. Do not guess."""
    result = extract(tmp_path, sql_script_json("SELECT * FROM dbo.Sales"))

    assert result.content.objects[0].kind is SqlObjectKind.UNKNOWN
    assert result.content.objects[0].operation is SqlOperation.READ


def test_view_references(tmp_path):
    query = (
        "CREATE VIEW rpt.vTrips AS SELECT * FROM dbo.TripsData;\n"
        "DROP VIEW IF EXISTS rpt.vOld;\n"
        "ALTER VIEW rpt.vTrips AS SELECT 1;\n"
    )
    result = extract(tmp_path, sql_script_json(query))
    found = objects_of(result.content)

    assert found["rpt.vTrips"].kind is SqlObjectKind.VIEW
    assert found["rpt.vOld"].kind is SqlObjectKind.VIEW
    assert found["rpt.vOld"].operation is SqlOperation.DROP


def test_stored_procedure_references(tmp_path):
    query = (
        "CREATE PROCEDURE dbo.usp_Load AS BEGIN SELECT 1 END;\n"
        "EXEC dbo.usp_Refresh;\n"
        "EXECUTE staging.usp_Clean;\n"
    )
    result = extract(tmp_path, sql_script_json(query))
    found = objects_of(result.content)

    assert found["dbo.usp_Load"].kind is SqlObjectKind.PROCEDURE
    assert found["dbo.usp_Load"].operation is SqlOperation.CREATE
    assert found["dbo.usp_Refresh"].operation is SqlOperation.EXECUTE
    assert found["staging.usp_Clean"].kind is SqlObjectKind.PROCEDURE


def test_qualified_names_are_split(tmp_path):
    query = (
        "SELECT * FROM Sales;\n"
        "SELECT * FROM dbo.Sales;\n"
        "SELECT * FROM poolone.dbo.Sales;\n"
        "SELECT * FROM [My DB].[My Schema].[My Table];\n"
    )
    result = extract(tmp_path, sql_script_json(query))
    by_location = {o.location.rsplit(":L", 1)[1]: o for o in result.content.objects}

    assert (by_location["1"].database, by_location["1"].schema) == (None, None)
    assert (by_location["2"].database, by_location["2"].schema) == (None, "dbo")
    assert (by_location["3"].database, by_location["3"].schema) == ("poolone", "dbo")
    bracketed = by_location["4"]
    assert bracketed.database == "My DB"
    assert bracketed.schema == "My Schema"
    assert bracketed.object_name == "My Table"
    assert bracketed.qualified_name == "My DB.My Schema.My Table"


def test_cross_database_reference_is_flagged(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json(
            "SELECT * FROM other.dbo.Sales",
            content={"currentConnection": {"databaseName": "poolone", "poolName": "poolone"}},
        ),
    )

    cross = [
        f for f in result.content.features if f.kind is SqlFeatureKind.CROSS_DATABASE
    ]
    assert len(cross) == 1
    assert "other" in cross[0].construct


def test_joins_are_captured(tmp_path):
    query = (
        "SELECT * FROM dbo.A a\n"
        "INNER JOIN dbo.B b ON a.id = b.id\n"
        "LEFT OUTER JOIN dbo.C c ON a.id = c.id\n"
    )
    result = extract(tmp_path, sql_script_json(query))

    assert set(result.content.referenced_names) == {"dbo.A", "dbo.B", "dbo.C"}


def test_comments_and_string_literals_are_not_scanned(tmp_path):
    """A commented-out FROM must not become a phantom dependency."""
    query = (
        "-- FROM dbo.CommentedOut\n"
        "/* FROM dbo.BlockCommented */\n"
        "SELECT 'FROM dbo.InsideAString' AS note FROM dbo.Real;\n"
    )
    result = extract(tmp_path, sql_script_json(query))

    assert result.content.referenced_names == ("dbo.Real",)


def test_cte_names_are_not_database_objects(tmp_path):
    query = (
        "WITH recent AS (SELECT * FROM dbo.Trips),\n"
        "     totals AS (SELECT * FROM recent)\n"
        "SELECT * FROM totals;\n"
    )
    result = extract(tmp_path, sql_script_json(query))

    assert result.content.referenced_names == ("dbo.Trips",)


def test_temp_objects_are_marked_and_excluded_from_references(tmp_path):
    query = "SELECT * INTO #staging FROM dbo.Real;\nSELECT * FROM #staging;\n"
    result = extract(tmp_path, sql_script_json(query))
    definition = result.content

    temp = [o for o in definition.objects if o.object_name.startswith("#")]
    assert temp
    assert all(o.kind is SqlObjectKind.TEMPORARY for o in temp)
    assert all(not o.is_durable for o in temp)
    assert "#staging" not in definition.referenced_names
    assert "#staging" not in {r.target_name for r in definition.references}


def test_table_functions_are_not_treated_as_objects(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json(
            "SELECT * FROM OPENROWSET(BULK 'https://lake.dfs.core.windows.net/x.parquet',"
            " FORMAT='PARQUET') AS rows"
        ),
    )

    assert "OPENROWSET" not in {o.object_name for o in result.content.objects}
    openrowset = [
        f for f in result.content.features if f.kind is SqlFeatureKind.OPENROWSET
    ]
    assert len(openrowset) == 1
    assert openrowset[0].evidence.startswith("https://lake.dfs.core.windows.net")


def test_reads_and_writes_are_separable(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json("INSERT INTO dbo.Sink SELECT * FROM dbo.Src"),
    )
    definition = result.content

    assert [o.qualified_name for o in definition.objects_read] == ["dbo.Src"]
    assert [o.qualified_name for o in definition.objects_written] == ["dbo.Sink"]


# --- Synapse-specific features -----------------------------------------------


def test_dedicated_pool_features_are_detected(tmp_path):
    query = (
        "CREATE TABLE dbo.Agg\n"
        "WITH (DISTRIBUTION = HASH(vendor_id), CLUSTERED COLUMNSTORE INDEX)\n"
        "AS SELECT vendor_id, SUM(tip) AS tips FROM dbo.Trips GROUP BY vendor_id;\n"
    )
    result = extract(tmp_path, sql_script_json(query))
    kinds = result.content.features_by_kind()

    assert kinds.get("distribution") == 1
    assert kinds.get("index") == 1
    assert kinds.get("ctas") == 1


@pytest.mark.parametrize(
    "query,expected",
    [
        ("CREATE EXTERNAL TABLE dbo.Ext (id INT)", SqlFeatureKind.EXTERNAL_TABLE),
        ("CREATE EXTERNAL DATA SOURCE src WITH (LOCATION='x')", SqlFeatureKind.EXTERNAL_DATA_SOURCE),
        ("CREATE EXTERNAL FILE FORMAT f WITH (FORMAT_TYPE=PARQUET)", SqlFeatureKind.EXTERNAL_FILE_FORMAT),
        ("COPY INTO dbo.T FROM 'https://x/y' WITH (FILE_TYPE='CSV')", SqlFeatureKind.COPY_INTO),
        ("SELECT 1 OPTION (LABEL = 'tag')", SqlFeatureKind.LABEL),
        ("CREATE STATISTICS s ON dbo.T (col)", SqlFeatureKind.STATISTICS),
        ("BEGIN TRANSACTION", SqlFeatureKind.TRANSACTION),
        ("GRANT SELECT ON dbo.T TO reader", SqlFeatureKind.PERMISSION),
    ],
)
def test_individual_features(tmp_path, query, expected):
    result = extract(tmp_path, sql_script_json(query))

    assert expected.value in result.content.features_by_kind()


def test_features_carry_no_severity(tmp_path):
    """Difficulty is Assessment's judgement, not extraction's."""
    result = extract(
        tmp_path,
        sql_script_json("CREATE TABLE t WITH (DISTRIBUTION = ROUND_ROBIN) AS SELECT 1 AS a"),
    )

    assert not hasattr(result.content.features[0], "severity")


# --- 9: dynamic SQL ----------------------------------------------------------


@pytest.mark.parametrize(
    "query,construct",
    [
        ("EXEC('SELECT * FROM ' + @tbl)", "EXEC(...)"),
        ("EXECUTE (@sql)", "EXEC(...)"),
        ("EXEC sp_executesql @stmt", "sp_executesql"),
        ("EXEC @dynamic", "EXEC @variable"),
    ],
)
def test_dynamic_sql_is_recorded(tmp_path, query, construct):
    result = extract(tmp_path, sql_script_json(query))

    assert result.status is ExtractionStatus.PARTIAL
    assert result.content.has_dynamic_sql
    assert construct in {d.construct for d in result.content.dynamic_sql}
    assert any("not statically determinable" in w.message for w in result.warnings)


def test_dynamic_sql_dependencies_are_not_guessed(tmp_path):
    """The table named inside the dynamic string must not become a reference."""
    result = extract(
        tmp_path,
        sql_script_json("EXEC('SELECT * FROM dbo.SecretTable');\nSELECT 1 FROM dbo.Real;"),
    )

    assert result.content.referenced_names == ("dbo.Real",)
    assert result.content.has_dynamic_sql


def test_a_script_without_dynamic_sql_has_no_warning(tmp_path):
    result = extract(tmp_path, sql_script_json("SELECT * FROM dbo.Real"))

    assert result.status is ExtractionStatus.SUCCESS
    assert result.warnings == ()
    assert result.content.dynamic_sql == ()


# --- 10: expressions and references in the artifact JSON ---------------------


def test_expressions_in_the_artifact_json_are_captured(tmp_path):
    """Plain SQL scripts rarely carry ADF expressions, but the field is scanned."""
    result = extract(
        tmp_path,
        sql_script_json(
            "SELECT 1",
            content={
                "currentConnection": {
                    "databaseName": {"value": "@pipeline().parameters.db", "type": "Expression"}
                }
            },
        ),
    )

    assert [e.expression for e in result.content.expressions] == [
        "@pipeline().parameters.db"
    ]


def test_structural_references_in_the_artifact_json(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json(
            "SELECT 1",
            content={
                "currentConnection": {
                    "poolName": "poolone",
                    "store": {"referenceName": "LS_Vault", "type": "LinkedServiceReference"},
                }
            },
        ),
    )

    linked = [
        r for r in result.content.references if r.target_type is AssetType.LINKED_SERVICE
    ]
    assert linked[0].target_name == "LS_Vault"
    assert linked[0].resolved is False


def test_sql_objects_surface_on_the_reference_channel(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json("SELECT * FROM dbo.A JOIN dbo.B ON 1=1"),
    )
    references = result.content.references

    assert {r.target_name for r in references} == {"dbo.A", "dbo.B"}
    for reference in references:
        assert reference.kind is ReferenceKind.SQL_OBJECT
        assert reference.target_type is None  # a SQL object is not a repo artifact
        assert reference.target_id is None
        assert reference.resolved is False
        assert reference.source_artifact_id == "synapse://sqlscript/SQL_Test"


def test_repeated_objects_yield_one_reference(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json("SELECT * FROM dbo.A;\nSELECT * FROM dbo.A;\nINSERT INTO dbo.A VALUES (1);"),
    )

    assert [r.target_name for r in result.content.references] == ["dbo.A"]
    assert len(result.content.objects) == 3  # every occurrence is still recorded


# --- 11: malformed input -----------------------------------------------------


def test_malformed_json_fails(tmp_path):
    result = extract(tmp_path, None, raw="{not json")

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert result.errors[0].code is IssueCode.MALFORMED_ARTIFACT


def test_missing_properties_fails(tmp_path):
    result = extract(tmp_path, {"name": "SQL_Test"})

    assert result.status is ExtractionStatus.FAILED
    assert "no properties object" in result.errors[0].message


def test_missing_content_fails(tmp_path):
    result = extract(tmp_path, {"name": "SQL", "properties": {"type": "SqlQuery"}})

    assert result.status is ExtractionStatus.FAILED
    assert "no content object" in result.errors[0].message


def test_missing_query_fails(tmp_path):
    """The detector identifies a SQL script by this field; without it the
    artifact is not what it was classified as."""
    result = extract(
        tmp_path, {"name": "SQL", "properties": {"content": {}, "type": "SqlQuery"}}
    )

    assert result.status is ExtractionStatus.FAILED
    assert "no query string" in result.errors[0].message
    assert result.errors[0].location == "properties.content.query"


def test_json_root_that_is_not_an_object_fails(tmp_path):
    result = extract(tmp_path, None, raw="[1, 2, 3]")

    assert result.status is ExtractionStatus.FAILED
    assert "not an object" in result.errors[0].message


# --- 12: unknown constructs --------------------------------------------------


def test_an_unexpected_artifact_type_is_warned_about_not_rejected(tmp_path):
    document = sql_script_json("SELECT * FROM dbo.Real")
    document["properties"]["type"] = "SparkSqlQuery"

    result = extract(tmp_path, document)

    assert result.status is ExtractionStatus.PARTIAL
    assert result.content.recognized is False
    assert result.content.referenced_names == ("dbo.Real",)  # still extracted
    assert any("SparkSqlQuery" in w.message for w in result.warnings)


def test_unknown_properties_are_warned_about(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json(
            "SELECT 1", content={"someFutureKey": 1}, someFutureProperty={"a": 1}
        ),
    )

    assert result.status is ExtractionStatus.PARTIAL
    messages = " ".join(w.message for w in result.warnings)
    assert "someFutureKey" in messages
    assert "someFutureProperty" in messages


def test_unparseable_sql_does_not_fail_extraction(tmp_path):
    """Extraction is not validation; the text is preserved regardless."""
    result = extract(tmp_path, sql_script_json("SELCT ** FRM (((") )

    assert result.status is ExtractionStatus.SUCCESS
    assert result.content.sql_text == "SELCT ** FRM ((("


# --- security ----------------------------------------------------------------


def test_credential_literals_are_recorded_and_redacted_in_evidence(tmp_path):
    query = (
        "CREATE DATABASE SCOPED CREDENTIAL cred\n"
        f"WITH IDENTITY = 'app', SECRET = '{FAKE_SECRET}';\n"
    )
    result = extract(tmp_path, sql_script_json(query))
    definition = result.content

    # The definition is preserved: it is the artifact.
    assert FAKE_SECRET in definition.sql_text

    # Derived surfaces never carry it.
    assert FAKE_SECRET not in json.dumps([f.to_dict() for f in definition.features])
    assert FAKE_SECRET not in json.dumps([s.to_dict() for s in definition.secrets])
    assert FAKE_SECRET not in json.dumps(definition.summary())
    assert FAKE_SECRET not in json.dumps(definition.to_dict(include_sql=False))
    assert FAKE_SECRET not in json.dumps(result.to_dict())

    secret = [s for s in definition.secrets if s.property_name == "SECRET"][0]
    assert secret.kind is SecretKind.INLINE_LITERAL
    assert secret.location.startswith("properties.content.query:L")


def test_summary_never_carries_the_sql_text(tmp_path):
    result = extract(
        tmp_path, sql_script_json("SELECT super_distinctive_column FROM dbo.T")
    )

    assert "super_distinctive_column" not in json.dumps(result.content.summary())
    assert result.content.summary()["referenced_names"] == ["dbo.T"]


def test_the_extractor_never_executes_sql(tmp_path):
    """A destructive statement is analysed as text and nothing else."""
    result = extract(tmp_path, sql_script_json("DROP TABLE dbo.Important;"))

    assert result.content.objects[0].operation is SqlOperation.DROP
    assert result.content.sql_text == "DROP TABLE dbo.Important;"


# --- scanner units -----------------------------------------------------------


def test_blanking_preserves_offsets_and_lines():
    sql = "SELECT 1 -- comment\nFROM t /* block */\n"
    blanked = blank_comments_and_literals(sql)

    assert len(blanked) == len(sql)
    assert blanked.count("\n") == sql.count("\n")
    assert "comment" not in blanked
    assert "FROM t" in blanked


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Sales", (None, None, "Sales")),
        ("dbo.Sales", (None, "dbo", "Sales")),
        ("db.dbo.Sales", ("db", "dbo", "Sales")),
        ("srv.db.dbo.Sales", ("db", "dbo", "Sales")),  # linked server dropped
        ("[my db].[my schema].[my table]", ("my db", "my schema", "my table")),
    ],
)
def test_split_qualified_name(raw, expected):
    assert split_qualified_name(raw) == expected


# --- framework integration ---------------------------------------------------


def test_provenance_is_preserved(tmp_path):
    repository = RepositorySource(
        provider="github",
        repository_url="https://github.com/contoso/synapse-workspace",
        ref="main",
        local_path=tmp_path,
        commit_sha="d" * 40,
    )
    result = extract(tmp_path, sql_script_json("SELECT 1"), repository=repository)

    provenance = result.provenance
    assert provenance.source_type is SourceType.REPOSITORY
    assert provenance.repository_url.endswith("synapse-workspace")
    assert provenance.commit_sha == "d" * 40
    assert provenance.source_path == "workspace/sqlscript/SQL_Test.json"
    assert provenance.sha256 == "b" * 64
    assert result.extractor.name == "sql_script"
    assert result.artifact_id == "synapse://sqlscript/SQL_Test"


def test_registry_selects_the_sql_script_extractor():
    registry = default_registry()

    selected = registry.require(AssetType.SQL_SCRIPT, SourceType.REPOSITORY)

    assert isinstance(selected, SQLScriptExtractor)
    assert selected.supported_types == (AssetType.SQL_SCRIPT,)
    # Both sources serve the same {name, properties} document, so the same
    # extractor is selected for a live workspace as for a clone.
    assert selected.supported_sources == (SourceType.REPOSITORY, SourceType.SYNAPSE)
    assert AssetType.SQL_SCRIPT in registry.supported_types()


def test_extraction_runs_through_the_orchestrator(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(sql_script_json("SELECT * FROM dbo.Trips")), encoding="utf-8"
    )
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path))

    run = ExtractorOrchestrator(default_registry(), context).run([artifact])

    assert len(run.results) == 1
    assert run.results[0].succeeded
    assert run.results[0].artifact_type is AssetType.SQL_SCRIPT
    assert [r.target_name for r in run.references] == ["dbo.Trips"]


# --- 13: unified discovery record --------------------------------------------


def test_result_becomes_a_unified_discovery_record(tmp_path):
    result = extract(tmp_path, sql_script_json("SELECT * FROM dbo.Trips"))

    facet = DefinitionFacet.from_extraction_result(result)
    assert facet.content_type == "SQLScriptDefinition"
    assert facet.is_usable

    record = UnifiedDiscoveryRecord.from_extraction_result(result)
    assert record is not None
    assert record.artifact is P0Artifact.SQL_SCRIPT
    assert record.logical_id == "synapse://sqlscript/SQL_Test"
    assert record.has_usable_definition
    assert record.dependencies is not None
    # SQL objects carry no target_id, so they never join onto a record today.
    assert record.dependencies.target_ids == ()


# --- determinism -------------------------------------------------------------


def test_extraction_is_deterministic(tmp_path):
    document = sql_script_json(
        "SELECT * FROM dbo.Z JOIN dbo.A ON 1=1;\nEXEC dbo.usp_X;\n",
        annotations=["b", "a"],
        content={"currentConnection": {"poolName": "p", "databaseName": "d"}},
    )

    first = extract(tmp_path, document)
    second = extract(tmp_path, document)

    assert first.content == second.content
    assert first.references == second.references


def test_ordering_is_stable(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json("SELECT * FROM dbo.Zulu JOIN dbo.Alpha ON 1=1", annotations=["z", "a"]),
    )
    definition = result.content

    assert [o.location for o in definition.objects] == sorted(
        o.location for o in definition.objects
    )
    assert definition.annotations == ("a", "z")
    assert definition.referenced_names == ("dbo.Alpha", "dbo.Zulu")


def test_to_dict_and_summary_are_json_serializable(tmp_path):
    result = extract(
        tmp_path,
        sql_script_json(
            "CREATE TABLE dbo.T WITH (DISTRIBUTION = REPLICATE) AS SELECT 1 AS a"
        ),
    )

    json.dumps(result.content.to_dict())
    json.dumps(result.content.summary())
    assert result.content.to_dict()["sql_text"].startswith("CREATE TABLE")
    assert "sql_text" not in result.content.to_dict(include_sql=False)
