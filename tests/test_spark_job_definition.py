"""Tests for the Spark job definition extractor.

The sample repository contains no Spark job definition, so every fixture here
is synthetic and says so. Nothing in this file asserts anything about the real
repository, and the live validation reports zero for this artifact type
because zero is what is there.

A Spark job definition is deliberately not treated as a notebook: it carries
no code, only a pointer to an application file, the class to invoke, the
arguments, and the resources to ask the pool for.
"""

from __future__ import annotations

import json

import pytest

from discovery_agent.artifacts.models import (
    ArtifactCategory,
    DetectedArtifact,
    DetectionEvidence,
    DiscoverySource,
)
from discovery_agent.extractors import ExtractionContext, default_registry
from discovery_agent.extractors.models import (
    ExtractionStatus,
    IssueCode,
    ReferenceKind,
    SourceType,
)
from discovery_agent.extractors.spark_job_definition import SparkJobDefinitionExtractor
from discovery_agent.extractors.spark_job_definition_models import SparkArtifactKind
from discovery_agent.extractors.sources import ArtifactSource
from discovery_agent.extractors.models import ExtractionProvenance
from discovery_agent.models import AssetType

MAIN_JAR = "abfss://artifacts@lake.dfs.core.windows.net/jobs/batch-1.2.0.jar"


class InMemorySource(ArtifactSource):
    """Serves one synthetic document. No file, no workspace, no network."""

    source_type = SourceType.REPOSITORY

    def __init__(self, document, source_type=SourceType.REPOSITORY):
        self.document = document
        self.source_type = source_type

    def provenance_for(self, artifact):
        return ExtractionProvenance(
            source_type=self.source_type,
            source_format=artifact.source_format,
            source_path=artifact.source_path,
            sha256=artifact.sha256,
        )

    def read_text(self, artifact):
        return json.dumps(self.document)


def detected(name="SJD_Batch", path="workspace/sparkJobDefinition/SJD_Batch.json"):
    return DetectedArtifact(
        artifact_type=AssetType.SPARK_JOB_DEFINITION.value,
        artifact_name=name,
        source_path=path,
        source_format="synapse_spark_job_definition_json",
        confidence=1.0,
        discovery_source=DiscoverySource.PATH_AND_STRUCTURE,
        category=ArtifactCategory.SYNAPSE,
        evidence=DetectionEvidence(),
        sha256="c" * 64,
    )


def document(**properties):
    base = {
        "targetBigDataPool": {
            "referenceName": "sparkpool01",
            "type": "BigDataPoolReference",
        },
        "requiredSparkVersion": "3.3",
        "language": "scala",
        "folder": {"name": "batch"},
        "description": "nightly aggregate",
        "jobProperties": {
            "name": "batch",
            "file": MAIN_JAR,
            "className": "com.contoso.Batch",
            "args": ["--date", "2024-01-01", 7],
            "conf": {
                "spark.sql.shuffle.partitions": "200",
                "spark.dynamicAllocation.enabled": True,
            },
            "jars": ["abfss://artifacts@lake.dfs.core.windows.net/lib/util.jar"],
            "pyFiles": [],
            "files": ["config.yaml"],
            "archives": [],
            "driverMemory": "28g",
            "driverCores": 4,
            "executorMemory": "28g",
            "executorCores": 4,
            "numExecutors": 2,
        },
    }
    base.update(properties)
    return {"name": "SJD_Batch", "properties": base}


def extract(doc=None, source_type=SourceType.REPOSITORY):
    source = InMemorySource(doc if doc is not None else document(), source_type)
    return SparkJobDefinitionExtractor().extract(
        detected(), ExtractionContext(source=source)
    )


# --- registration ------------------------------------------------------------


def test_the_registry_selects_this_extractor_for_both_sources():
    registry = default_registry()

    for source_type in (SourceType.REPOSITORY, SourceType.SYNAPSE):
        selected = registry.require(AssetType.SPARK_JOB_DEFINITION, source_type)
        assert isinstance(selected, SparkJobDefinitionExtractor)


def test_the_ninth_p0_artifact_now_has_an_extractor():
    assert AssetType.SPARK_JOB_DEFINITION in default_registry().supported_types()


# --- the application ---------------------------------------------------------


def test_the_application_file_is_recorded_as_a_pointer_and_never_fetched():
    result = extract()

    main = result.content.job.main_file
    assert main.uri == MAIN_JAR
    assert main.kind is SparkArtifactKind.MAIN_FILE
    assert main.scheme == "abfss"
    assert main.is_absolute


def test_a_relative_application_path_is_recorded_as_not_absolute():
    """It depends on a workspace default that will not survive a migration."""
    doc = document()
    doc["properties"]["jobProperties"]["file"] = "jobs/batch.jar"

    result = extract(doc)

    assert result.content.job.main_file.scheme is None
    assert not result.content.job.main_file.is_absolute


def test_the_entry_point_and_arguments_are_preserved_in_order():
    result = extract()

    job = result.content.job
    assert job.class_name == "com.contoso.Batch"
    # Order is meaning for command-line arguments.
    assert job.arguments == ("--date", "2024-01-01", "7")


def test_every_dependency_kind_is_kept_apart():
    result = extract()

    job = result.content.job
    assert [a.uri for a in job.artifacts_of(SparkArtifactKind.JAR)] == [
        "abfss://artifacts@lake.dfs.core.windows.net/lib/util.jar"
    ]
    assert [a.uri for a in job.artifacts_of(SparkArtifactKind.FILE)] == ["config.yaml"]
    assert job.artifacts_of(SparkArtifactKind.PYTHON_FILE) == ()
    assert result.content.artifact_count == 3


def test_spark_configuration_is_kept_sorted_and_rendered_stably():
    result = extract()

    entries = result.content.job.configuration
    assert [(e.key, e.value) for e in entries] == [
        ("spark.dynamicAllocation.enabled", "true"),
        ("spark.sql.shuffle.partitions", "200"),
    ]


def test_the_resource_request_is_what_the_job_asks_for_not_what_it_gets():
    result = extract()

    resources = result.content.job.resources
    assert resources.driver_memory == "28g"
    assert resources.executor_cores == 4
    assert resources.executor_count == 2
    assert resources.is_declared


def test_an_undeclared_resource_is_none_and_never_zero():
    """None means the pool default applies, which is a fact about the pool."""
    doc = document()
    del doc["properties"]["jobProperties"]["numExecutors"]

    result = extract(doc)

    assert result.content.job.resources.executor_count is None


# --- compute and references --------------------------------------------------


def test_the_target_pool_is_observed_by_the_shared_reference_scanner():
    result = extract()

    compute = result.content.compute
    assert compute.target_name == "sparkpool01"
    assert compute.kind is ReferenceKind.COMPUTE
    # A Spark pool is workspace infrastructure, not a repository artifact.
    assert compute.target_type is None
    assert not compute.resolved
    assert compute in result.references


def test_the_spark_version_is_recorded():
    result = extract()

    assert result.content.required_spark_version == "3.3"
    assert result.content.language == "scala"
    assert result.content.folder == "batch"


def test_a_job_with_no_pool_says_so_rather_than_reporting_none():
    doc = document()
    del doc["properties"]["targetBigDataPool"]

    result = extract(doc)

    assert result.content.compute is None
    assert not result.content.targets_a_pool
    assert any(
        i.code is IssueCode.MISSING_INFORMATION and "targetBigDataPool" in i.location
        for i in result.warnings
    )


def test_a_job_with_no_application_file_says_so():
    doc = document()
    del doc["properties"]["jobProperties"]["file"]

    result = extract(doc)

    assert not result.content.job.has_main_file
    assert any(
        "the application this job runs could not be determined" in i.message
        for i in result.warnings
    )


def test_a_spark_configuration_reference_is_promoted_and_still_listed():
    doc = document(
        targetSparkConfiguration={
            "referenceName": "conf01",
            "type": "SparkConfigurationReference",
        }
    )

    result = extract(doc)

    assert result.content.spark_configuration.target_name == "conf01"
    assert result.content.spark_configuration in result.references


# --- failure and unmodelled constructs --------------------------------------


def test_an_unmodelled_property_is_reported_rather_than_dropped():
    result = extract(document(someFutureProperty={"x": 1}))

    assert result.status is ExtractionStatus.PARTIAL
    assert any(
        i.code is IssueCode.UNSUPPORTED_CONSTRUCT
        and "someFutureProperty" in i.message
        for i in result.warnings
    )


def test_an_unmodelled_job_property_is_reported():
    doc = document()
    doc["properties"]["jobProperties"]["futureSetting"] = 1

    result = extract(doc)

    assert any("futureSetting" in i.message for i in result.warnings)


def test_a_malformed_job_properties_block_does_not_fail_the_whole_artifact():
    doc = document()
    doc["properties"]["jobProperties"] = "not an object"

    result = extract(doc)

    assert result.content is not None
    assert result.content.name == "SJD_Batch"
    assert any(i.code is IssueCode.MALFORMED_ARTIFACT for i in result.warnings)


def test_malformed_args_are_reported_rather_than_silently_empty():
    doc = document()
    doc["properties"]["jobProperties"]["args"] = "--date 2024-01-01"

    result = extract(doc)

    assert result.content.job.arguments == ()
    assert any("args" in i.location for i in result.warnings)


def test_a_document_with_no_properties_fails_and_carries_no_content():
    result = extract({"name": "SJD_Batch"})

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert result.errors[0].code is IssueCode.MALFORMED_ARTIFACT


def test_the_same_document_extracts_identically_from_either_source():
    """One extractor, two sources. That is the whole point of ArtifactSource."""
    from_git = extract(source_type=SourceType.REPOSITORY)
    from_workspace = extract(source_type=SourceType.SYNAPSE)

    assert from_git.content == from_workspace.content
    assert from_git.provenance.source_type is SourceType.REPOSITORY
    assert from_workspace.provenance.source_type is SourceType.SYNAPSE


def test_a_secure_string_in_the_artifact_never_carries_its_value_out():
    """The artifact may contain one; the extracted model may not."""
    doc = document()
    doc["properties"]["jobProperties"]["conf"]["spark.password"] = {
        "type": "SecureString",
        "value": "not-a-real-password-00000",
    }

    result = extract(doc)

    rendered = json.dumps(result.content.to_dict())
    assert "not-a-real-password-00000" not in rendered
    # The reference is kept -- that a secret is used is a finding.
    assert any(s.kind.value == "secure_string" for s in result.content.secrets)
    assert all(
        not hasattr(s, "value") for s in result.content.secrets
    ), "SecretReference has no field a value could occupy"
