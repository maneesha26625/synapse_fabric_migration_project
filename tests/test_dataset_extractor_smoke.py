"""DatasetExtractor against the real local repository snapshot.

Runs walk -> detect -> extract over the clone acquisition produced. Local
reads only: no network, no git, no Synapse or Fabric API.

Also cross-checks the dataset names the pipeline extractor observed against
the datasets actually present. That is a *consistency signal*, not dependency
resolution: no reference is marked resolved and no graph is built here.
"""

from __future__ import annotations

import json
import pytest

from conftest import requires_snapshot, smoke_run

from discovery_agent.extractors import (
    ExtractionContext,
    ExtractionStatus,
    ExtractorOrchestrator,
    ReferenceKind,
    RepositoryArtifactSource,
    default_registry,
)
from discovery_agent.extractors.dataset_models import LocationCategory
from discovery_agent.models import AssetType

pytestmark = requires_snapshot


def build_run(with_repository_metadata: bool = False):
    """One discovery run, assembled by ``discovery.run`` rather than by hand.

    ``with_repository_metadata`` used to mean "invent a RepositorySource with
    a zeroed commit SHA". It now means "go through the connection layer", so
    the provenance these tests check is the snapshot's real identity.
    """
    run = smoke_run(connected=with_repository_metadata)
    return run.detection, run.extraction


@pytest.fixture(scope="module")
def detection_and_run():
    return build_run()


@pytest.fixture(scope="module")
def run(detection_and_run):
    return detection_and_run[1]


@pytest.fixture(scope="module")
def datasets(run):
    extracted = [
        result
        for result in run.results
        if result.artifact_type is AssetType.DATASET
        and result.status is not ExtractionStatus.SKIPPED
    ]
    assert extracted, "no datasets were extracted"
    return extracted


@pytest.fixture(scope="module")
def definitions(datasets):
    return {result.content.name: result.content for result in datasets}


def test_every_dataset_extracts_without_failing(datasets):
    for result in datasets:
        assert result.succeeded, f"{result.artifact_name}: {result.errors}"
        assert result.content is not None


def test_dataset_count_matches_what_the_detector_found(detection_and_run, datasets):
    detection, _ = detection_and_run
    detected = [
        a for a in detection.synapse_artifacts if a.artifact_type == "dataset"
    ]

    assert len(datasets) == len(detected)


def test_every_dataset_type_has_a_handler(definitions):
    """No unrecognized types in this repository; all are modelled."""
    unrecognized = {d.type for d in definitions.values() if not d.recognized}

    assert unrecognized == set()
    assert {d.type for d in definitions.values()} <= {
        "DelimitedText",
        "AzureSqlDWTable",
    }


def test_every_dataset_declares_a_linked_service(definitions):
    for name, definition in definitions.items():
        assert definition.linked_service is not None, f"{name} has no linked service"
        assert definition.linked_service.target_type is AssetType.LINKED_SERVICE
        assert definition.linked_service.kind is ReferenceKind.ARTIFACT
        assert definition.linked_service.resolved is False
        assert definition.linked_service.location == "properties.linkedServiceName"


def test_linked_service_names_exist_in_the_repository(detection_and_run, definitions):
    """A consistency signal, not resolution."""
    detection, _ = detection_and_run
    known = {
        a.artifact_name
        for a in detection.synapse_artifacts
        if a.artifact_type == "linkedService"
    }

    referenced = {
        d.linked_service_name for d in definitions.values() if d.linked_service_name
    }
    assert referenced, "expected linked service references"
    assert referenced <= known, f"not in repository: {sorted(referenced - known)}"


def test_sql_datasets_resolve_to_tables(definitions):
    sql = [d for d in definitions.values() if d.type == "AzureSqlDWTable"]

    assert sql
    tables = {d.location.table for d in sql if d.location and d.location.table}
    assert tables, "expected at least one SQL dataset to name a table"
    for definition in sql:
        if definition.location is not None:
            assert definition.location.category is LocationCategory.SQL_TABLE


def test_file_datasets_resolve_to_locations(definitions):
    delimited = [d for d in definitions.values() if d.type == "DelimitedText"]

    assert delimited
    categories = {
        d.location.category for d in delimited if d.location is not None
    }
    assert LocationCategory.DATA_LAKE in categories
    assert LocationCategory.HTTP in categories

    lake = [
        d
        for d in delimited
        if d.location and d.location.category is LocationCategory.DATA_LAKE
    ]
    for definition in lake:
        assert definition.location.file_name
        assert definition.location.file_system


def test_delimited_text_format_settings_are_extracted(definitions):
    delimited = [d for d in definitions.values() if d.type == "DelimitedText"]

    for definition in delimited:
        settings = definition.format_settings
        assert settings is not None
        assert settings.column_delimiter == ","
        assert settings.first_row_as_header is True
        assert settings.quote_char == '"'


def test_declared_schemas_are_extracted_in_order(definitions):
    with_columns = [d for d in definitions.values() if d.schema.column_count > 0]

    assert with_columns, "expected at least one dataset to declare columns"
    for definition in with_columns:
        assert definition.schema.declared is True
        ordinals = [c.ordinal for c in definition.schema.columns]
        assert ordinals == list(range(len(ordinals)))
        assert all(c.name for c in definition.schema.columns)


def test_empty_declared_schemas_are_not_treated_as_missing(definitions):
    """Most datasets here declare `schema: []` — valid, not absent."""
    empty = [
        d
        for d in definitions.values()
        if d.schema.declared and d.schema.column_count == 0
    ]

    assert empty, "expected datasets declaring an empty schema"


def test_parameters_and_expressions_are_extracted(definitions):
    parameterized = [d for d in definitions.values() if d.parameters]

    assert parameterized
    for definition in parameterized:
        assert [p.name for p in definition.parameters] == sorted(
            p.name for p in definition.parameters
        )

    expressions = [e for d in definitions.values() for e in d.expressions]
    assert expressions
    assert all(e.expression.startswith("@") for e in expressions)
    assert any("@dataset()." in e.expression for e in expressions)
    assert all(e.location.startswith("properties.") for e in expressions)


def test_a_dynamic_sql_schema_keeps_its_expression(definitions):
    dynamic = [
        d
        for d in definitions.values()
        if d.location is not None
        and d.location.schema
        and d.location.schema.startswith("@")
    ]

    assert dynamic, "expected a dataset whose SQL schema is an expression"
    definition = dynamic[0]
    assert definition.location.schema == "@dataset().SchemaName"
    assert any(
        e.location == "properties.typeProperties.schema" for e in definition.expressions
    )


def test_no_secrets_and_no_warnings_in_this_repository(datasets):
    for result in datasets:
        assert result.warnings == (), (
            f"{result.artifact_name}: {[w.message for w in result.warnings]}"
        )
        assert result.status is ExtractionStatus.SUCCESS
        assert result.content.secrets == ()


def test_summaries_are_json_serializable(definitions):
    for definition in definitions.values():
        json.dumps(definition.summary())
        json.dumps(definition.to_dict())


def test_provenance_is_complete():
    _, run = build_run(with_repository_metadata=True)
    result = [
        r
        for r in run.results
        if r.artifact_type is AssetType.DATASET and r.content is not None
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
    assert result.provenance.source_format == "synapse_dataset_json"
    assert result.extractor.name == "dataset"


def test_three_extractors_run_and_the_rest_is_recorded_as_skipped(run):
    handled = {
        r.artifact_type for r in run.results if r.status is not ExtractionStatus.SKIPPED
    }
    skipped = {
        r.artifact_type for r in run.results if r.status is ExtractionStatus.SKIPPED
    }

    assert {AssetType.DATASET, AssetType.NOTEBOOK, AssetType.PIPELINE} <= handled
    assert skipped, "expected artifact types still awaiting an extractor"
    assert not (handled & skipped)
    for result in run.results:
        if result.status is ExtractionStatus.SKIPPED:
            assert result.errors, "a skipped artifact must say why"


# --- cross-check with the pipeline extractor ---------------------------------


def test_pipeline_dataset_references_name_datasets_that_exist(
    detection_and_run, definitions
):
    """Consistency signal only — nothing here resolves a reference.

    Every dataset name a pipeline activity points at should correspond to a
    dataset artifact in the same snapshot. A miss would mean either a broken
    workspace or an extraction bug, and is worth knowing about early.
    """
    detection, run = detection_and_run
    pipelines = [
        r
        for r in run.results
        if r.artifact_type is AssetType.PIPELINE and r.content is not None
    ]
    assert pipelines, "no pipelines extracted to cross-check against"

    referenced = {
        reference.target_name
        for pipeline in pipelines
        for reference in pipeline.content.references
        if reference.target_type is AssetType.DATASET
    }
    assert referenced, "expected the pipeline to reference datasets"

    missing = sorted(referenced - set(definitions))
    assert not missing, f"pipeline references datasets not in the snapshot: {missing}"

    # The cross-check must not have changed any reference's status.
    for pipeline in pipelines:
        for reference in pipeline.content.references:
            assert reference.resolved is False


def test_extraction_is_reproducible():
    _, first = build_run()
    _, second = build_run()

    assert [r.content for r in first.results] == [r.content for r in second.results]
