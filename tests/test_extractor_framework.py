"""Tests for the extractor framework itself.

No real artifact extractor is exercised here — the fixtures below are small
fake extractors. Everything runs against temporary directories; no network,
no git, no model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import pytest

from discovery_agent.acquisition.models import RepositorySource
from discovery_agent.artifacts.models import (
    ArtifactCategory,
    ArtifactDetectionResult,
    DetectedArtifact,
    DetectionEvidence,
    DiscoverySource,
    Signal,
    SignalType,
)
from discovery_agent.config import DiscoveryConfig
from discovery_agent.errors import (
    DuplicateExtractorError,
    ExtractionContractError,
    MalformedArtifactError,
    NoExtractorError,
    UnsupportedSourceError,
)
from discovery_agent.extractors import (
    ArtifactReference,
    ExtractionContext,
    ExtractionIssue,
    ExtractionResult,
    ExtractionStatus,
    Extractor,
    ExtractorOrchestrator,
    ExtractorRegistry,
    IssueCode,
    ReferenceKind,
    RepositoryArtifactSource,
    SourceType,
    run_extraction,
)
from discovery_agent.models import AssetType, Evidence, asset_id


# --- fixtures: artifacts, sources, and fake extractors -----------------------


@dataclass(frozen=True)
class FakePipelineModel:
    """Stands in for a future PipelineDefinition. The framework never reads it."""

    activity_count: int


def make_artifact(
    artifact_type: str = "pipeline",
    name: str = "PL_Load_Sales",
    path: str = "workspace/pipeline/PL_Load_Sales.json",
    category: ArtifactCategory = ArtifactCategory.SYNAPSE,
    sha256: Optional[str] = "a" * 64,
) -> DetectedArtifact:
    return DetectedArtifact(
        artifact_type=artifact_type,
        artifact_name=name,
        source_path=path,
        source_format="synapse_pipeline_json",
        confidence=1.0,
        discovery_source=DiscoverySource.PATH_AND_STRUCTURE,
        category=category,
        evidence=DetectionEvidence((Signal(SignalType.PATH, "pipeline/"),)),
        sha256=sha256,
    )


def write_artifact(root: Path, artifact: DetectedArtifact, document) -> None:
    target = root / artifact.source_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document), encoding="utf-8")


def make_context(root: Path, repository=None, **config_options) -> ExtractionContext:
    config = None
    if config_options:
        config = DiscoveryConfig(source=root, out=root / "out", **config_options)
    return ExtractionContext(
        source=RepositoryArtifactSource(root, repository), config=config
    )


class FakePipelineExtractor(Extractor[FakePipelineModel]):
    """The shape a real PipelineExtractor will have."""

    name = "fake-pipeline"
    version = "1.2.3"
    supported_types = (AssetType.PIPELINE,)
    supported_sources = (SourceType.REPOSITORY,)

    def extract(self, artifact, context):
        document = context.source.read_json(artifact)
        activities = document.get("properties", {}).get("activities", [])
        references = tuple(
            ArtifactReference(
                source_artifact_id=asset_id(AssetType.PIPELINE, artifact.artifact_name),
                source_artifact_type=AssetType.PIPELINE,
                kind=ReferenceKind.ARTIFACT,
                target_type=AssetType.DATASET,
                target_name=activity["dataset"],
                location=f"properties.activities[{index}].dataset",
                evidence=Evidence(artifact.source_path, None, self.name),
            )
            for index, activity in enumerate(activities)
            if "dataset" in activity
        )
        return self.success(
            artifact, context, FakePipelineModel(len(activities)), references=references
        )


class FakeNotebookExtractor(Extractor[FakePipelineModel]):
    name = "fake-notebook"
    version = "0.1.0"
    supported_types = (AssetType.NOTEBOOK,)
    supported_sources = (SourceType.REPOSITORY, SourceType.SYNAPSE)

    def extract(self, artifact, context):
        return self.success(artifact, context, FakePipelineModel(0))


class WarningExtractor(Extractor[FakePipelineModel]):
    name = "warns"
    version = "1.0.0"
    supported_types = (AssetType.DATASET,)

    def extract(self, artifact, context):
        return self.success(
            artifact,
            context,
            FakePipelineModel(1),
            warnings=(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    "dynamic expression not evaluated",
                    "properties.typeProperties.location.fileName",
                ),
            ),
        )


class ExplodingExtractor(Extractor[FakePipelineModel]):
    name = "explodes"
    version = "1.0.0"
    supported_types = (AssetType.TRIGGER,)

    def extract(self, artifact, context):
        raise ZeroDivisionError("bug inside the extractor")


class LiveOnlyExtractor(Extractor[FakePipelineModel]):
    """Declares a source that the repository run cannot satisfy."""

    name = "live-only"
    version = "1.0.0"
    supported_types = (AssetType.SQL_SCRIPT,)
    supported_sources = (SourceType.SYNAPSE,)

    def extract(self, artifact, context):  # pragma: no cover - never selected
        raise AssertionError("must not be called from a repository run")


class WrongReturnExtractor(Extractor[FakePipelineModel]):
    name = "wrong-return"
    version = "1.0.0"
    supported_types = (AssetType.DATAFLOW,)

    def extract(self, artifact, context):
        return {"not": "a result"}


# --- 1, 10, 11: registration -------------------------------------------------


def test_register_and_retrieve_an_extractor():
    registry = ExtractorRegistry()
    extractor = FakePipelineExtractor()
    registry.register(extractor)

    assert registry.find(AssetType.PIPELINE, SourceType.REPOSITORY) is extractor
    assert registry.require(AssetType.PIPELINE, SourceType.REPOSITORY) is extractor
    assert registry.supports(AssetType.PIPELINE, SourceType.REPOSITORY)
    assert len(registry) == 1


def test_registry_accepts_extractors_at_construction():
    registry = ExtractorRegistry([FakePipelineExtractor(), FakeNotebookExtractor()])

    assert len(registry) == 2
    assert registry.supported_types() == (AssetType.NOTEBOOK, AssetType.PIPELINE)


def test_multiple_extractors_are_kept_separate():
    pipeline, notebook = FakePipelineExtractor(), FakeNotebookExtractor()
    registry = ExtractorRegistry([pipeline, notebook])

    assert registry.find(AssetType.PIPELINE, SourceType.REPOSITORY) is pipeline
    assert registry.find(AssetType.NOTEBOOK, SourceType.REPOSITORY) is notebook
    assert registry.extractors == (pipeline, notebook)


def test_one_extractor_can_claim_several_sources():
    registry = ExtractorRegistry([FakeNotebookExtractor()])

    assert registry.supported_sources(AssetType.NOTEBOOK) == (
        SourceType.REPOSITORY,
        SourceType.SYNAPSE,
    )


def test_duplicate_registration_is_rejected():
    registry = ExtractorRegistry([FakePipelineExtractor()])

    with pytest.raises(DuplicateExtractorError, match="already registered"):
        registry.register(FakePipelineExtractor())


def test_duplicate_registration_can_be_overridden_deliberately():
    first = FakePipelineExtractor()
    replacement = FakePipelineExtractor()
    replacement.name = "replacement"
    registry = ExtractorRegistry([first])

    registry.register(replacement, replace=True)

    assert registry.find(AssetType.PIPELINE, SourceType.REPOSITORY) is replacement


def test_partial_overlap_is_still_a_duplicate():
    """Claiming any already-taken pair is rejected, not just an exact match."""

    class OverlappingExtractor(Extractor):
        name = "overlapping"
        version = "1.0.0"
        supported_types = (AssetType.PIPELINE, AssetType.DATASET)

        def extract(self, artifact, context):  # pragma: no cover
            raise AssertionError

    registry = ExtractorRegistry([FakePipelineExtractor()])
    with pytest.raises(DuplicateExtractorError):
        registry.register(OverlappingExtractor())
    # The rejected registration left nothing behind.
    assert registry.find(AssetType.DATASET, SourceType.REPOSITORY) is None


def test_extractor_must_declare_what_it_supports():
    class Undeclared(Extractor):
        name = "undeclared"

        def extract(self, artifact, context):  # pragma: no cover
            raise AssertionError

    with pytest.raises(ValueError, match="supported_types"):
        ExtractorRegistry().register(Undeclared())


# --- 3, 4, 12: rejection paths -----------------------------------------------


def test_unsupported_artifact_type_is_reported_clearly():
    registry = ExtractorRegistry([FakePipelineExtractor()])

    assert registry.find(AssetType.NOTEBOOK, SourceType.REPOSITORY) is None
    with pytest.raises(NoExtractorError, match="no extractor registered for notebook"):
        registry.require(AssetType.NOTEBOOK, SourceType.REPOSITORY)


def test_unsupported_source_type_is_a_different_error():
    """A type nobody handles and a type handled for another source differ."""
    registry = ExtractorRegistry([LiveOnlyExtractor()])

    with pytest.raises(UnsupportedSourceError, match="from source repository"):
        registry.require(AssetType.SQL_SCRIPT, SourceType.REPOSITORY)


def test_empty_registry_reports_no_extractor():
    with pytest.raises(NoExtractorError):
        ExtractorRegistry().require(AssetType.PIPELINE, SourceType.REPOSITORY)


def test_orchestrator_records_a_missing_extractor_instead_of_failing(tmp_path):
    artifact = make_artifact()
    write_artifact(tmp_path, artifact, {"properties": {"activities": []}})
    orchestrator = ExtractorOrchestrator(ExtractorRegistry(), make_context(tmp_path))

    run = orchestrator.run([artifact])
    result = run.results[0]

    assert result.status is ExtractionStatus.SKIPPED
    assert result.errors[0].code is IssueCode.NO_EXTRACTOR
    assert result.content is None
    assert run.skipped == (result,)


def test_orchestrator_records_an_unsupported_source(tmp_path):
    artifact = make_artifact(artifact_type="sqlscript", name="SQL_Create")
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([LiveOnlyExtractor()]), make_context(tmp_path)
    )

    result = orchestrator.run([artifact]).results[0]

    assert result.status is ExtractionStatus.SKIPPED
    assert result.errors[0].code is IssueCode.UNSUPPORTED_SOURCE


def test_non_synapse_artifacts_are_not_extraction_candidates(tmp_path):
    readme = make_artifact(
        artifact_type="non_synapse",
        name="README",
        path="README.md",
        category=ArtifactCategory.NON_SYNAPSE,
    )
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([FakePipelineExtractor()]), make_context(tmp_path)
    )

    assert orchestrator.run([readme]).results == ()


# --- 5, 6: running through the orchestrator, and provenance ------------------


def test_orchestrator_runs_the_selected_extractor(tmp_path):
    artifact = make_artifact()
    write_artifact(
        tmp_path,
        artifact,
        {"properties": {"activities": [{"name": "Copy", "dataset": "DS_Sales"}]}},
    )
    registry = ExtractorRegistry([FakePipelineExtractor(), FakeNotebookExtractor()])

    run = ExtractorOrchestrator(registry, make_context(tmp_path)).run([artifact])
    result = run.results[0]

    assert result.status is ExtractionStatus.SUCCESS
    assert result.content == FakePipelineModel(activity_count=1)
    assert result.artifact_id == "synapse://pipeline/PL_Load_Sales"
    assert result.artifact_type is AssetType.PIPELINE
    assert result.extractor.name == "fake-pipeline"
    assert result.extractor.version == "1.2.3"
    assert result.succeeded


def test_run_detection_accepts_detector_output_directly(tmp_path):
    artifact = make_artifact()
    write_artifact(tmp_path, artifact, {"properties": {"activities": []}})
    detection = ArtifactDetectionResult(root=tmp_path, artifacts=(artifact,))
    registry = ExtractorRegistry([FakePipelineExtractor()])

    run = run_extraction(detection, registry, make_context(tmp_path))

    assert len(run.results) == 1
    assert run.results[0].succeeded


def test_provenance_carries_repository_identity(tmp_path):
    artifact = make_artifact()
    write_artifact(tmp_path, artifact, {"properties": {"activities": []}})
    repository = RepositorySource(
        provider="github",
        repository_url="https://github.com/contoso/synapse-workspace",
        ref="main",
        local_path=tmp_path,
        commit_sha="b" * 40,
    )
    context = make_context(tmp_path, repository=repository)

    result = ExtractorOrchestrator(
        ExtractorRegistry([FakePipelineExtractor()]), context
    ).run([artifact]).results[0]
    provenance = result.provenance

    assert provenance.source_type is SourceType.REPOSITORY
    assert provenance.repository_url == "https://github.com/contoso/synapse-workspace"
    assert provenance.commit_sha == "b" * 40
    assert provenance.ref == "main"
    assert provenance.source_path == "workspace/pipeline/PL_Load_Sales.json"
    assert provenance.sha256 == "a" * 64
    assert provenance.source_format == "synapse_pipeline_json"


def test_provenance_works_without_repository_metadata(tmp_path):
    artifact = make_artifact()
    write_artifact(tmp_path, artifact, {"properties": {"activities": []}})

    result = ExtractorOrchestrator(
        ExtractorRegistry([FakePipelineExtractor()]), make_context(tmp_path)
    ).run([artifact]).results[0]

    assert result.provenance.repository_url is None
    assert result.provenance.source_path == artifact.source_path


def test_provenance_survives_a_skipped_artifact(tmp_path):
    artifact = make_artifact()
    result = ExtractorOrchestrator(ExtractorRegistry(), make_context(tmp_path)).run(
        [artifact]
    ).results[0]

    assert result.provenance.source_path == artifact.source_path
    assert result.provenance.sha256 == "a" * 64


# --- 7, 8: warnings and errors ----------------------------------------------


def test_warnings_are_preserved_and_downgrade_the_status(tmp_path):
    artifact = make_artifact(artifact_type="dataset", name="DS_Sales")
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([WarningExtractor()]), make_context(tmp_path)
    )

    result = orchestrator.run([artifact]).results[0]

    assert result.status is ExtractionStatus.PARTIAL
    assert result.succeeded  # partial still produced usable content
    assert len(result.warnings) == 1
    assert result.warnings[0].code is IssueCode.UNSUPPORTED_CONSTRUCT
    assert result.warnings[0].location.startswith("properties.")
    assert result.content is not None


def test_an_extractor_crash_becomes_a_failed_result(tmp_path):
    artifact = make_artifact(artifact_type="trigger", name="TR_Daily")
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([ExplodingExtractor()]), make_context(tmp_path)
    )

    result = orchestrator.run([artifact]).results[0]

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert result.errors[0].code is IssueCode.EXTRACTION_FAILURE
    assert "ZeroDivisionError" in result.errors[0].message


def test_a_missing_file_becomes_a_malformed_artifact_error(tmp_path):
    artifact = make_artifact()  # never written to disk
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([FakePipelineExtractor()]), make_context(tmp_path)
    )

    result = orchestrator.run([artifact]).results[0]

    assert result.status is ExtractionStatus.FAILED
    assert result.errors[0].code is IssueCode.MALFORMED_ARTIFACT


def test_invalid_json_is_reported_not_swallowed(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([FakePipelineExtractor()]), make_context(tmp_path)
    )

    result = orchestrator.run([artifact]).results[0]

    assert result.status is ExtractionStatus.FAILED
    assert "invalid json" in result.errors[0].message


def test_one_failure_does_not_stop_the_run(tmp_path):
    good = make_artifact()
    bad = make_artifact(artifact_type="trigger", name="TR_Daily", path="workspace/trigger/TR_Daily.json")
    write_artifact(tmp_path, good, {"properties": {"activities": []}})
    registry = ExtractorRegistry([FakePipelineExtractor(), ExplodingExtractor()])

    run = ExtractorOrchestrator(registry, make_context(tmp_path)).run([good, bad])

    assert run.counts_by_status() == {"failed": 1, "success": 1}
    assert len(run.succeeded) == 1
    assert len(run.failed) == 1


def test_fail_fast_reraises_instead_of_recording(tmp_path):
    artifact = make_artifact(artifact_type="trigger", name="TR_Daily")
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([ExplodingExtractor()]),
        make_context(tmp_path),
        fail_fast=True,
    )

    with pytest.raises(ZeroDivisionError):
        orchestrator.run([artifact])


def test_fail_fast_defaults_to_the_existing_parse_error_policy(tmp_path):
    strict = ExtractorOrchestrator(
        ExtractorRegistry(), make_context(tmp_path, on_parse_error="fail")
    )
    lenient = ExtractorOrchestrator(
        ExtractorRegistry(), make_context(tmp_path, on_parse_error="skip")
    )

    assert strict.fail_fast is True
    assert lenient.fail_fast is False


def test_an_extractor_returning_the_wrong_type_is_a_contract_error(tmp_path):
    artifact = make_artifact(artifact_type="dataflow", name="DF_Trips")
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([WrongReturnExtractor()]), make_context(tmp_path)
    )

    with pytest.raises(ExtractionContractError, match="not an ExtractionResult"):
        orchestrator.run([artifact])


# --- the result contract -----------------------------------------------------


def make_result(status, content=None, warnings=(), errors=(), references=()):
    from discovery_agent.extractors.models import ExtractionProvenance, ExtractorInfo

    return ExtractionResult(
        artifact_id="synapse://pipeline/PL",
        artifact_type=AssetType.PIPELINE,
        artifact_name="PL",
        status=status,
        provenance=ExtractionProvenance(SourceType.REPOSITORY, "synapse_pipeline_json"),
        extractor=ExtractorInfo("t", "1"),
        content=content,
        references=references,
        warnings=warnings,
        errors=errors,
    )


def test_a_failure_may_not_masquerade_as_empty_content():
    """The rule that stops a malformed pipeline becoming activities = []."""
    issue = (ExtractionIssue(IssueCode.MALFORMED_ARTIFACT, "broken"),)

    with pytest.raises(ExtractionContractError, match="must not return content"):
        make_result(ExtractionStatus.FAILED, content=FakePipelineModel(0), errors=issue)


def test_a_failure_must_explain_itself():
    with pytest.raises(ExtractionContractError, match="must carry errors"):
        make_result(ExtractionStatus.FAILED)


def test_a_success_may_not_carry_errors():
    issue = (ExtractionIssue(IssueCode.EXTRACTION_FAILURE, "broken"),)

    with pytest.raises(ExtractionContractError, match="cannot carry errors"):
        make_result(ExtractionStatus.SUCCESS, content=FakePipelineModel(0), errors=issue)


def test_a_partial_result_must_explain_itself():
    with pytest.raises(ExtractionContractError, match="must explain itself"):
        make_result(ExtractionStatus.PARTIAL, content=FakePipelineModel(0))


def test_a_skipped_result_carries_no_content():
    with pytest.raises(ExtractionContractError, match="must not return content"):
        make_result(ExtractionStatus.SKIPPED, content=FakePipelineModel(0))


def test_result_to_dict_covers_the_framework_fields():
    payload = make_result(ExtractionStatus.SUCCESS, content=FakePipelineModel(0)).to_dict()

    assert set(payload) == {
        "artifact_id",
        "artifact_type",
        "artifact_name",
        "status",
        "provenance",
        "extractor",
        "references",
        "warnings",
        "errors",
    }
    # content is deliberately absent: serializing it is the writer's job.
    assert "content" not in payload


# --- references --------------------------------------------------------------


def test_references_are_observations_not_resolved_edges(tmp_path):
    artifact = make_artifact()
    write_artifact(
        tmp_path,
        artifact,
        {"properties": {"activities": [{"name": "Copy", "dataset": "DS_Sales"}]}},
    )
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([FakePipelineExtractor()]), make_context(tmp_path)
    )

    run = orchestrator.run([artifact])
    reference = run.references[0]

    assert reference.resolved is False, "the framework must never claim a reference is valid"
    assert reference.target_id == "synapse://dataset/DS_Sales"
    assert reference.source_artifact_id == "synapse://pipeline/PL_Load_Sales"
    assert reference.location == "properties.activities[0].dataset"
    assert reference.evidence.source_file == artifact.source_path
    assert reference.evidence.extractor == "fake-pipeline"


def test_a_non_artifact_target_has_no_target_id():
    reference = ArtifactReference(
        source_artifact_id="synapse://notebook/NB",
        source_artifact_type=AssetType.NOTEBOOK,
        kind=ReferenceKind.STORAGE_PATH,
        target_name="abfss://raw@lake.dfs.core.windows.net/trips",
        location="cells[3]",
        evidence=Evidence("notebook/NB.json", 12, "fake"),
    )

    assert reference.target_type is None
    assert reference.target_id is None
    assert reference.to_dict()["resolved"] is False


def test_run_collects_references_across_artifacts(tmp_path):
    first = make_artifact()
    second = make_artifact(name="PL_Other", path="workspace/pipeline/PL_Other.json")
    write_artifact(tmp_path, first, {"properties": {"activities": [{"dataset": "A"}]}})
    write_artifact(tmp_path, second, {"properties": {"activities": [{"dataset": "B"}]}})
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([FakePipelineExtractor()]), make_context(tmp_path)
    )

    run = orchestrator.run([first, second])

    assert [r.target_name for r in run.references] == ["A", "B"]


# --- 9: determinism ----------------------------------------------------------


def test_results_are_ordered_by_source_path(tmp_path):
    names = ["PL_Zulu", "PL_Alpha", "PL_Mike"]
    artifacts = []
    for name in names:
        artifact = make_artifact(name=name, path=f"workspace/pipeline/{name}.json")
        write_artifact(tmp_path, artifact, {"properties": {"activities": []}})
        artifacts.append(artifact)
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([FakePipelineExtractor()]), make_context(tmp_path)
    )

    run = orchestrator.run(artifacts)
    paths = [r.provenance.source_path for r in run.results]

    assert paths == sorted(paths)


def test_two_runs_produce_identical_results(tmp_path):
    artifact = make_artifact()
    write_artifact(
        tmp_path, artifact, {"properties": {"activities": [{"dataset": "DS_Sales"}]}}
    )
    registry = ExtractorRegistry([FakePipelineExtractor()])
    context = make_context(tmp_path)

    first = ExtractorOrchestrator(registry, context).run([artifact])
    second = ExtractorOrchestrator(registry, context).run([artifact])

    assert first.results == second.results
    assert first.counts_by_type() == second.counts_by_type() == {"pipeline": 1}


def test_input_order_does_not_change_output_order(tmp_path):
    first = make_artifact(name="PL_A", path="workspace/pipeline/PL_A.json")
    second = make_artifact(name="PL_B", path="workspace/pipeline/PL_B.json")
    for artifact in (first, second):
        write_artifact(tmp_path, artifact, {"properties": {"activities": []}})
    orchestrator = ExtractorOrchestrator(
        ExtractorRegistry([FakePipelineExtractor()]), make_context(tmp_path)
    )

    forward = orchestrator.run([first, second])
    backward = orchestrator.run([second, first])

    assert forward.results == backward.results


# --- the source abstraction --------------------------------------------------


def test_repository_source_reads_text_and_json(tmp_path):
    artifact = make_artifact()
    write_artifact(tmp_path, artifact, {"name": "PL_Load_Sales"})
    source = RepositoryArtifactSource(tmp_path)

    assert source.source_type is SourceType.REPOSITORY
    assert source.read_json(artifact) == {"name": "PL_Load_Sales"}
    assert "PL_Load_Sales" in source.read_text(artifact)


def test_repository_source_tolerates_a_utf8_bom(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"name": "PL"}), encoding="utf-8-sig")

    assert RepositoryArtifactSource(tmp_path).read_json(artifact) == {"name": "PL"}


def test_repository_source_raises_a_typed_error_for_missing_content(tmp_path):
    with pytest.raises(MalformedArtifactError) as excinfo:
        RepositoryArtifactSource(tmp_path).read_text(make_artifact())

    assert excinfo.value.source_path == "workspace/pipeline/PL_Load_Sales.json"


def test_a_custom_source_needs_only_two_methods(tmp_path):
    """Proves the framework does not assume Git: a fake live source works."""
    from discovery_agent.extractors.models import ExtractionProvenance
    from discovery_agent.extractors.sources import ArtifactSource

    class FakeSynapseSource(ArtifactSource):
        source_type = SourceType.SYNAPSE

        def provenance_for(self, artifact):
            return ExtractionProvenance(
                source_type=SourceType.SYNAPSE,
                source_format="synapse_api_json",
                resource_id=f"/workspaces/poc/notebooks/{artifact.artifact_name}",
            )

        def read_text(self, artifact):
            return json.dumps({"properties": {"cells": []}})

    artifact = make_artifact(artifact_type="notebook", name="NB_Explore")
    context = ExtractionContext(source=FakeSynapseSource())
    registry = ExtractorRegistry([FakeNotebookExtractor()])

    result = ExtractorOrchestrator(registry, context).run([artifact]).results[0]

    assert result.succeeded
    assert result.provenance.source_type is SourceType.SYNAPSE
    assert result.provenance.resource_id.endswith("NB_Explore")
    assert result.provenance.source_path is None


def test_extractor_supports_reports_both_dimensions():
    extractor = FakeNotebookExtractor()

    assert extractor.supports(AssetType.NOTEBOOK, SourceType.REPOSITORY)
    assert extractor.supports(AssetType.NOTEBOOK, SourceType.SYNAPSE)
    assert not extractor.supports(AssetType.NOTEBOOK, SourceType.AZURE)
    assert not extractor.supports(AssetType.PIPELINE, SourceType.REPOSITORY)


def test_empty_run_is_well_formed(tmp_path):
    run = ExtractorOrchestrator(ExtractorRegistry(), make_context(tmp_path)).run([])

    assert run.results == ()
    assert run.references == ()
    assert run.counts_by_status() == {}
