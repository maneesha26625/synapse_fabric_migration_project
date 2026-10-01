"""PipelineExtractor against the real local repository snapshot.

Runs the whole chain that exists today — walk, detect, extract — over the
clone acquisition produced. Local reads only: no network, no git.

Assertions are about what the extractor understood, not about exact counts
the upstream repository could legitimately change. Skips cleanly when the
snapshot is absent.
"""

from __future__ import annotations

import pytest

from conftest import requires_snapshot, smoke_run

from discovery_agent.extractors.synapse_json import ExpressionForm
from discovery_agent.extractors import (
    ExtractionContext,
    ExtractionStatus,
    ExtractorOrchestrator,
    ReferenceKind,
    RepositoryArtifactSource,
    default_registry,
)
from discovery_agent.extractors.sql_models import (
    SqlFeatureKind,
    SqlObjectKind,
    SqlOperation,
)
from discovery_agent.models import AssetType

pytestmark = requires_snapshot


def build_run(with_repository_metadata: bool = False):
    """One discovery run, assembled by ``discovery.run`` rather than by hand.

    ``with_repository_metadata`` used to mean "invent a RepositorySource with
    a zeroed commit SHA". It now means "go through the connection layer", so
    the provenance these tests check is the snapshot's real identity.
    """
    return smoke_run(connected=with_repository_metadata).extraction


@pytest.fixture(scope="module")
def run():
    return build_run()


@pytest.fixture(scope="module")
def pipelines(run):
    extracted = [
        result
        for result in run.results
        if result.artifact_type is AssetType.PIPELINE
        and result.status is not ExtractionStatus.SKIPPED
    ]
    assert extracted, "no pipelines were extracted"
    return extracted


def test_every_pipeline_extracts_without_failing(pipelines):
    for result in pipelines:
        assert result.succeeded, f"{result.artifact_name}: {result.errors}"
        assert result.content is not None


def test_expected_pipeline_is_present(pipelines):
    names = {result.content.name for result in pipelines}
    assert "TripFaresDataPipeline" in names


def test_unhandled_types_are_recorded_as_skipped(run):
    """Artifact types without an extractor are recorded, not silently dropped."""
    skipped_types = {
        result.artifact_type
        for result in run.results
        if result.status is ExtractionStatus.SKIPPED
    }

    assert AssetType.PIPELINE not in skipped_types
    # Which types are still unhandled shrinks as extractors land, so this
    # asserts the rule rather than naming the next one.
    assert skipped_types, "expected artifact types still awaiting an extractor"
    for result in run.results:
        if result.status is ExtractionStatus.SKIPPED:
            assert result.errors, "a skipped artifact must say why"


def test_activities_are_extracted_with_their_types(pipelines):
    pipeline = [p.content for p in pipelines if p.content.name == "TripFaresDataPipeline"][0]

    assert pipeline.activity_count >= 6
    assert {"Copy", "ExecuteDataFlow", "Lookup"} <= set(pipeline.activity_types)
    assert pipeline.unrecognized_activities == (), (
        f"unmodelled activity types: "
        f"{[a.type for a in pipeline.unrecognized_activities]}"
    )
    assert all(a.name for a in pipeline.walk())


def test_internal_dependencies_are_extracted(pipelines):
    pipeline = [p.content for p in pipelines if p.content.name == "TripFaresDataPipeline"][0]
    by_name = {a.name: a for a in pipeline.walk()}

    lookup = by_name["Create Schema If Does Not Exists"]
    upstream = {d.activity for d in lookup.depends_on}

    assert upstream == {"IngestTripDataIntoADLS", "IngestTripFaresDataIntoADLS"}
    assert all(d.conditions == ("Succeeded",) for d in lookup.depends_on)


def test_references_point_at_real_workspace_artifacts(pipelines, run):
    pipeline = [p.content for p in pipelines if p.content.name == "TripFaresDataPipeline"][0]
    targets = {r.target_name for r in pipeline.references}

    assert {"tripsDataSource", "faresDataSource"} <= targets  # datasets
    assert "tripFaresDataTransformations" in targets  # data flow
    assert "TripFaresDataLakeStorageLinkedService" in targets  # linked service

    kinds = {r.target_type for r in pipeline.references}
    assert AssetType.DATASET in kinds
    assert AssetType.DATAFLOW in kinds
    assert AssetType.LINKED_SERVICE in kinds

    for reference in pipeline.references:
        assert reference.resolved is False, "references are observations, not edges"
        assert reference.location.startswith("properties.activities[")
        assert reference.evidence.source_file.endswith(".json")

    # Embedded SQL adds SQL_OBJECT references alongside the artifact ones;
    # only the artifact ones name a workspace artifact.
    assert ReferenceKind.ARTIFACT in {
        r.kind for r in pipeline.references if r.target_name in targets
    }
    for reference in pipeline.references:
        if reference.kind is ReferenceKind.ARTIFACT:
            assert reference.target_type is not None


def test_referenced_artifacts_actually_exist_in_the_repository(pipelines, run):
    """Not resolution — just a sanity check that the names are real ones.

    The graph stage will do this properly; here it only confirms the
    extractor is reading real reference fields rather than stray strings.
    """
    pipeline = [p.content for p in pipelines if p.content.name == "TripFaresDataPipeline"][0]
    known = {result.artifact_name for result in run.results}

    for reference in pipeline.references:
        if reference.target_type is not None:
            assert reference.target_name in known, (
                f"{reference.target_name} at {reference.location} is not an "
                f"artifact in this repository"
            )


def test_expressions_are_captured_verbatim(pipelines):
    pipeline = [p.content for p in pipelines if p.content.name == "TripFaresDataPipeline"][0]
    expressions = pipeline.expressions

    assert expressions, "the real pipeline is parameterized and should have expressions"
    assert any("pipeline().parameters.KeyVaultName" in e.expression for e in expressions)
    assert all(e.location.startswith("properties.activities[") for e in expressions)

    # An inline expression is a bare string starting with "@". An expression
    # object may instead hold interpolated content -- this pipeline's Lookup
    # query is SQL with "@{...}" embedded in it -- so only the dynamic marker
    # is guaranteed, and the text is kept exactly as written either way.
    for expression in expressions:
        if expression.form is ExpressionForm.INLINE:
            assert expression.expression.startswith("@")
        else:
            assert "@" in expression.expression

    interpolated = [e for e in expressions if not e.expression.startswith("@")]
    assert interpolated, "the Lookup query is interpolated SQL and must be kept whole"
    assert "sys.schemas" in interpolated[0].expression


def test_parameters_are_extracted_with_defaults(pipelines):
    pipeline = [p.content for p in pipelines if p.content.name == "TripFaresDataPipeline"][0]
    parameters = {p.name: p for p in pipeline.parameters}

    assert "SchemaName" in parameters
    assert parameters["SchemaName"].type == "string"
    assert parameters["SchemaName"].has_default
    assert [p.name for p in pipeline.parameters] == sorted(parameters)


LOOKUP_QUERY = "properties.activities[3].typeProperties.source.sqlReaderQuery"
TRIPS_PRECOPY = "properties.activities[4].typeProperties.sink.preCopyScript"
FARES_PRECOPY = "properties.activities[5].typeProperties.sink.preCopyScript"


@pytest.fixture(scope="module")
def trip_fares(pipelines):
    return [p.content for p in pipelines if p.content.name == "TripFaresDataPipeline"][0]


@pytest.fixture(scope="module")
def scripts_by_location(trip_fares):
    return {script.location: script for script in trip_fares.scripts}


def test_embedded_sql_is_preserved(pipelines):
    pipeline = [p.content for p in pipelines if p.content.name == "TripFaresDataPipeline"][0]
    scripts = [s for activity in pipeline.walk() for s in activity.scripts]

    assert scripts, "this pipeline embeds SQL and it must not be lost"
    assert any("sys.schemas" in s.text for s in scripts)


def test_the_three_real_sql_blocks_are_found_where_expected(scripts_by_location):
    assert set(scripts_by_location) == {LOOKUP_QUERY, TRIPS_PRECOPY, FARES_PRECOPY}


def test_analysis_does_not_disturb_the_sql_text(scripts_by_location):
    """The text is the definition; the scanner reads it and rewrites nothing."""
    lookup = scripts_by_location[LOOKUP_QUERY]

    assert lookup.text.startswith("IF NOT EXISTS (SELECT * FROM sys.schemas")
    assert lookup.text.endswith("END")
    assert "@{pipeline().parameters.SchemaName}" in lookup.text

    trips = scripts_by_location[TRIPS_PRECOPY]
    assert trips.text.startswith("IF (EXISTS (SELECT *")
    assert "Truncate table TripsData;" in trips.text


def test_the_lookup_query_reads_catalogs_and_builds_sql_at_runtime(
    scripts_by_location,
):
    lookup = scripts_by_location[LOOKUP_QUERY]

    # System catalogs are kept, not filtered: what the SQL names is a fact.
    assert {o.qualified_name for o in lookup.objects} == {
        "sys.schemas",
        "sys.symmetric_keys",
    }
    assert all(o.operation is SqlOperation.READ for o in lookup.objects)

    assert lookup.has_dynamic_sql
    assert [d.construct for d in lookup.dynamic_sql] == ["EXEC(...)"]
    assert lookup.dynamic_sql[0].location == LOOKUP_QUERY + ":L3"

    # The CREATE SCHEMA inside the EXEC string is not mistaken for a real one.
    assert not any(o.operation is SqlOperation.CREATE for o in lookup.objects)


@pytest.mark.parametrize(
    "location,table",
    [(TRIPS_PRECOPY, "TripsData"), (FARES_PRECOPY, "FaresData")],
)
def test_the_pre_copy_scripts_truncate_their_target_table(
    scripts_by_location, location, table
):
    script = scripts_by_location[location]
    truncated = [o for o in script.objects if o.operation is SqlOperation.TRUNCATE]

    assert [o.qualified_name for o in truncated] == [table]
    assert truncated[0].kind is SqlObjectKind.TABLE
    assert truncated[0].statement == "TRUNCATE TABLE"
    assert truncated[0].location == location + ":L6"
    assert "INFORMATION_SCHEMA.TABLES" in {o.qualified_name for o in script.objects}
    assert not script.has_dynamic_sql


def test_the_truncated_tables_are_now_sql_dependencies_of_the_pipeline(trip_fares):
    """The point of the integration: two destructive writes, previously unseen."""
    targets = {
        r.target_name
        for r in trip_fares.references
        if r.kind is ReferenceKind.SQL_OBJECT
    }

    assert {"TripsData", "FaresData"} <= targets
    assert {"sys.schemas", "sys.symmetric_keys", "INFORMATION_SCHEMA.TABLES"} <= targets


def test_sql_dependencies_are_observations_not_resolved_edges(trip_fares):
    for reference in trip_fares.references:
        if reference.kind is not ReferenceKind.SQL_OBJECT:
            continue
        assert reference.target_type is None, "a table is not a repository artifact"
        assert reference.target_id is None
        assert reference.resolved is False
        assert reference.source_artifact_type is AssetType.PIPELINE
        assert reference.evidence.extractor == "pipeline"


def test_no_cross_database_feature_is_claimed_for_embedded_sql(trip_fares):
    """Pipeline SQL has no connection context, so the question is not answered."""
    kinds = {f.kind for script in trip_fares.scripts for f in script.features}

    assert SqlFeatureKind.CROSS_DATABASE not in kinds


def test_no_credential_literals_in_the_real_embedded_sql(trip_fares):
    assert [s for script in trip_fares.scripts for s in script.secrets] == []


def test_copy_activities_record_their_data_movement(pipelines):
    pipeline = [p.content for p in pipelines if p.content.name == "TripFaresDataPipeline"][0]
    copies = [a for a in pipeline.walk() if a.type == "Copy"]

    assert copies
    for copy in copies:
        assert copy.data_movement is not None
        assert copy.data_movement.source_type
        assert copy.data_movement.sink_type


def test_the_only_warning_is_the_dynamic_sql_admission(pipelines):
    """Nothing here is unmodelled; the one warning is an honest gap, not a bug.

    The Lookup builds its CREATE SCHEMA with EXEC of a string, whose target
    is not statically knowable. Admitting that makes the extraction PARTIAL.
    """
    for result in pipelines:
        messages = [w.message for w in result.warnings]
        assert len(messages) == 1, f"{result.artifact_name}: {messages}"
        assert "dynamic SQL site(s) present" in messages[0]
        assert result.status is ExtractionStatus.PARTIAL
        assert result.succeeded


def test_provenance_is_complete(pipelines):
    run_with_metadata = build_run(with_repository_metadata=True)
    result = [
        r
        for r in run_with_metadata.results
        if r.artifact_type is AssetType.PIPELINE and r.content is not None
    ][0]

    assert result.provenance.repository_url.endswith("1-click-POC")
    assert result.provenance.ref == "main"
    # The real SHA acquisition reported, not a placeholder. This assertion
    # used to pass against a hand-built "0" * 40 because nothing connected
    # acquisition to extraction.
    assert result.provenance.commit_sha
    assert result.provenance.commit_sha != "0" * 40
    assert len(result.provenance.commit_sha) == 40
    assert result.provenance.source_path.endswith(".json")
    assert result.provenance.sha256
    assert result.provenance.source_format == "synapse_pipeline_json"
    assert result.extractor.name == "pipeline"
    assert result.extractor.version


def test_extraction_is_reproducible():
    first = build_run()
    second = build_run()

    assert [r.content for r in first.results] == [r.content for r in second.results]
    assert first.references == second.references
