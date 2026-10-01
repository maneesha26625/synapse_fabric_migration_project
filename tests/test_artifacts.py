"""Tests for the artifact detector.

Every fixture is synthetic and written into a temporary directory, so nothing
here depends on GitHub, the network, or the real cloned repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from discovery_agent.artifacts import (
    ArtifactCategory,
    ArtifactDetector,
    DiscoverySource,
    SignalType,
    detect_artifacts,
)
from discovery_agent.artifacts.detector import (
    CONFIDENCE_NONE,
    CONFIDENCE_STRUCTURE_AND_PATH,
    CONFIDENCE_STRUCTURE_ONLY,
    CONFIDENCE_STRUCTURE_PATH_CONFLICT,
    CONFIDENCE_WEAK_WITH_PATH,
)
from discovery_agent.config import DiscoveryConfig
from discovery_agent.errors import ParseError
from discovery_agent.repository import walk_repository

# --- synthetic Synapse artifacts --------------------------------------------

NOTEBOOK = {
    "name": "NB_Explore_Taxi",
    "properties": {
        "nbformat": 4,
        "nbformat_minor": 2,
        "bigDataPool": {"referenceName": "sparkpool", "type": "BigDataPoolReference"},
        "cells": [{"cell_type": "code", "source": ["df = spark.read.parquet(path)"]}],
    },
}

PIPELINE = {
    "name": "PL_Load_Sales",
    "properties": {
        "activities": [
            {"name": "Copy", "type": "Copy", "typeProperties": {}},
        ],
        "parameters": {"runDate": {"type": "String"}},
    },
}

DATASET = {
    "name": "DS_Fares_Source",
    "properties": {
        "type": "DelimitedText",
        "linkedServiceName": {"referenceName": "LS_Lake", "type": "LinkedServiceReference"},
        "typeProperties": {"location": {"type": "AzureBlobFSLocation"}},
    },
}

LINKED_SERVICE = {
    "name": "LS_KeyVault",
    "properties": {
        "type": "AzureKeyVault",
        "typeProperties": {"baseUrl": "https://example.vault.azure.net/"},
    },
}

TRIGGER = {
    "name": "TR_Daily",
    "properties": {
        "type": "ScheduleTrigger",
        "pipelines": [{"pipelineReference": {"referenceName": "PL_Load_Sales"}}],
        "typeProperties": {"recurrence": {"frequency": "Day", "interval": 1}},
    },
}

SQL_SCRIPT = {
    "name": "SQL_Create_Dim",
    "properties": {
        "content": {
            "query": "CREATE TABLE dbo.DimDate WITH (DISTRIBUTION = REPLICATE) AS SELECT 1",
            "currentConnection": {"databaseName": "poolone", "poolName": "poolone"},
        },
        "type": "SqlQuery",
    },
}

DATAFLOW = {
    "name": "DF_Trip_Fares",
    "properties": {
        "type": "MappingDataFlow",
        "typeProperties": {
            "sources": [{"name": "src"}],
            "sinks": [{"name": "snk"}],
            "transformations": [],
            "script": "source(output(a as string))~> src",
        },
    },
}

SPARK_JOB_DEFINITION = {
    "name": "SJD_Aggregate",
    "properties": {
        "targetBigDataPool": {"referenceName": "sparkpool", "type": "BigDataPoolReference"},
        "jobProperties": {
            "name": "SJD_Aggregate",
            "file": "abfss://jobs@lake.dfs.core.windows.net/agg.py",
            "driverMemory": "4g",
        },
    },
}

INTEGRATION_RUNTIME = {
    "name": "AutoResolveIntegrationRuntime",
    "properties": {"type": "Managed", "typeProperties": {"computeProperties": {}}},
}

CREDENTIAL = {
    "name": "WorkspaceSystemIdentity",
    "properties": {"type": "ManagedIdentity"},
}

MANAGED_VNET = {"name": "default", "type": "Microsoft.Synapse/workspaces/managedVirtualNetworks"}

ARM_TEMPLATE = {
    "$schema": "https://schema.management.azure.com/schemas/2015-01-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {},
    "resources": [{"type": "Microsoft.KeyVault/vaults", "name": "kv"}],
}

PACKAGE_JSON = {"name": "web", "version": "1.0.0", "dependencies": {"react": "^18"}}

METADATA_JSON = {
    "$schema": "https://example.com/metadata.schema.json",
    "itemDisplayName": "Test Drive",
    "description": "sample",
}

AMBIGUOUS_JSON = {"name": "something", "properties": {"description": "nothing typed here"}}


# --- fixtures ----------------------------------------------------------------


def write_json(path: Path, document) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def make_config(source: Path, **overrides) -> DiscoveryConfig:
    options = dict(source=source, out=source / "out")
    options.update(overrides)
    return DiscoveryConfig(**options)


def detect_one(tmp_path: Path, relative: str, document, **config_options):
    """Write one file, walk, detect, and return the single verdict."""
    repo = tmp_path / "repo"
    write_json(repo / relative, document)
    result = detect_artifacts(
        walk_repository(make_config(repo)), make_config(repo, **config_options)
    )
    assert len(result.artifacts) == 1
    return result.artifacts[0]


def detect_raw(tmp_path: Path, relative: str, text: str, **config_options):
    repo = tmp_path / "repo"
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    config = make_config(repo, **config_options)
    result = detect_artifacts(walk_repository(make_config(repo)), config)
    assert len(result.artifacts) == 1
    return result.artifacts[0]


def build_workspace(tmp_path: Path, root_folder: str = "workspace") -> Path:
    """A repository laid out the way Synapse Git integration writes one."""
    repo = tmp_path / "repo"
    base = repo / root_folder
    write_json(base / "notebook" / "NB_Explore_Taxi.json", NOTEBOOK)
    write_json(base / "pipeline" / "PL_Load_Sales.json", PIPELINE)
    write_json(base / "dataset" / "DS_Fares_Source.json", DATASET)
    write_json(base / "linkedService" / "LS_KeyVault.json", LINKED_SERVICE)
    write_json(base / "trigger" / "TR_Daily.json", TRIGGER)
    write_json(base / "sqlscript" / "SQL_Create_Dim.json", SQL_SCRIPT)
    write_json(base / "dataflow" / "DF_Trip_Fares.json", DATAFLOW)
    write_json(base / "sparkJobDefinition" / "SJD_Aggregate.json", SPARK_JOB_DEFINITION)
    write_json(base / "integrationRuntime" / "AutoResolveIntegrationRuntime.json", INTEGRATION_RUNTIME)
    write_json(base / "credential" / "WorkspaceSystemIdentity.json", CREDENTIAL)
    write_json(base / "managedVirtualNetwork" / "default.json", MANAGED_VNET)
    (base / "kqlscript").mkdir(parents=True, exist_ok=True)
    (base / "kqlscript" / "Analyze.kql").write_text("Trips | take 10", encoding="utf-8")
    write_json(repo / "azuredeploy.json", ARM_TEMPLATE)
    (repo / "README.md").write_text("# poc", encoding="utf-8")
    return repo


def detect_workspace(tmp_path: Path):
    repo = build_workspace(tmp_path)
    return detect_artifacts(walk_repository(make_config(repo)), make_config(repo))


def by_path(result):
    return {a.source_path: a for a in result.artifacts}


# --- 1-8: the required Synapse artifact types --------------------------------


@pytest.mark.parametrize(
    "folder,file_name,document,expected_type,expected_format",
    [
        ("notebook", "NB_Explore_Taxi.json", NOTEBOOK, "notebook", "synapse_notebook_json"),
        ("pipeline", "PL_Load_Sales.json", PIPELINE, "pipeline", "synapse_pipeline_json"),
        ("dataset", "DS_Fares_Source.json", DATASET, "dataset", "synapse_dataset_json"),
        ("linkedService", "LS_KeyVault.json", LINKED_SERVICE, "linkedService", "synapse_linked_service_json"),
        ("trigger", "TR_Daily.json", TRIGGER, "trigger", "synapse_trigger_json"),
        ("sqlscript", "SQL_Create_Dim.json", SQL_SCRIPT, "sqlscript", "synapse_sql_script_json"),
        ("dataflow", "DF_Trip_Fares.json", DATAFLOW, "dataflow", "synapse_dataflow_json"),
        ("sparkJobDefinition", "SJD_Aggregate.json", SPARK_JOB_DEFINITION, "sparkJobDefinition", "synapse_spark_job_definition_json"),
    ],
)
def test_core_artifact_types_are_detected(
    tmp_path, folder, file_name, document, expected_type, expected_format
):
    artifact = detect_one(tmp_path, f"workspace/{folder}/{file_name}", document)

    assert artifact.artifact_type == expected_type
    assert artifact.source_format == expected_format
    assert artifact.category is ArtifactCategory.SYNAPSE
    assert artifact.confidence == CONFIDENCE_STRUCTURE_AND_PATH
    assert artifact.discovery_source is DiscoverySource.PATH_AND_STRUCTURE
    assert artifact.artifact_name == document["name"]
    assert artifact.source_path == f"workspace/{folder}/{file_name}"
    assert artifact.sha256


def test_artifact_name_falls_back_to_the_file_stem(tmp_path):
    document = {"properties": dict(PIPELINE["properties"])}
    artifact = detect_one(tmp_path, "workspace/pipeline/PL_Unnamed.json", document)

    assert artifact.artifact_type == "pipeline"
    assert artifact.artifact_name == "PL_Unnamed"


def test_workspace_root_folder_may_be_absent(tmp_path):
    artifact = detect_one(tmp_path, "notebook/NB_Explore_Taxi.json", NOTEBOOK)
    assert artifact.artifact_type == "notebook"
    assert artifact.confidence == CONFIDENCE_STRUCTURE_AND_PATH


def test_workspace_folders_are_matched_case_insensitively(tmp_path):
    artifact = detect_one(tmp_path, "workspace/LinkedService/LS_KeyVault.json", LINKED_SERVICE)
    assert artifact.artifact_type == "linkedService"
    assert artifact.confidence == CONFIDENCE_STRUCTURE_AND_PATH


def test_additional_workspace_types_are_detected(tmp_path):
    result = detect_workspace(tmp_path)
    found = by_path(result)

    assert found["workspace/integrationRuntime/AutoResolveIntegrationRuntime.json"].artifact_type == "integrationRuntime"
    assert found["workspace/credential/WorkspaceSystemIdentity.json"].artifact_type == "credential"
    assert found["workspace/managedVirtualNetwork/default.json"].artifact_type == "managedVirtualNetwork"
    assert found["workspace/kqlscript/Analyze.kql"].artifact_type == "kqlscript"


def test_weak_types_get_reduced_confidence(tmp_path):
    result = detect_workspace(tmp_path)
    found = by_path(result)

    for path in (
        "workspace/integrationRuntime/AutoResolveIntegrationRuntime.json",
        "workspace/credential/WorkspaceSystemIdentity.json",
        "workspace/managedVirtualNetwork/default.json",
        "workspace/kqlscript/Analyze.kql",
    ):
        assert found[path].confidence == CONFIDENCE_WEAK_WITH_PATH


def test_integration_runtime_is_not_mistaken_for_a_linked_service(tmp_path):
    """Both share {type, typeProperties}; the guard must keep them apart."""
    artifact = detect_one(
        tmp_path,
        "workspace/integrationRuntime/AutoResolveIntegrationRuntime.json",
        INTEGRATION_RUNTIME,
    )
    assert artifact.artifact_type == "integrationRuntime"


def test_dataset_is_not_mistaken_for_a_linked_service(tmp_path):
    artifact = detect_one(tmp_path, "workspace/dataset/DS_Fares_Source.json", DATASET)
    assert artifact.artifact_type == "dataset"


def test_dataflow_is_not_mistaken_for_a_linked_service(tmp_path):
    artifact = detect_one(tmp_path, "workspace/dataflow/DF_Trip_Fares.json", DATAFLOW)
    assert artifact.artifact_type == "dataflow"


# --- 9: non-Synapse JSON -----------------------------------------------------


def test_arm_template_is_not_a_synapse_artifact(tmp_path):
    artifact = detect_one(tmp_path, "azuredeploy.json", ARM_TEMPLATE)

    assert artifact.category is ArtifactCategory.NON_SYNAPSE
    assert artifact.artifact_type == "non_synapse"
    assert artifact.source_format == "arm_template_json"


def test_schema_governed_json_is_not_a_synapse_artifact(tmp_path):
    artifact = detect_one(tmp_path, "metadata.json", METADATA_JSON)

    assert artifact.category is ArtifactCategory.NON_SYNAPSE
    assert artifact.source_format == "json_schema_document"


def test_ordinary_json_is_not_assumed_to_be_an_artifact(tmp_path):
    artifact = detect_one(tmp_path, "package.json", PACKAGE_JSON)

    assert artifact.category is ArtifactCategory.UNKNOWN
    assert artifact.source_format == "unknown_json"
    assert artifact.confidence == CONFIDENCE_NONE


def test_arm_template_inside_a_synapse_folder_stays_non_synapse(tmp_path):
    artifact = detect_one(tmp_path, "workspace/pipeline/azuredeploy.json", ARM_TEMPLATE)
    assert artifact.category is ArtifactCategory.NON_SYNAPSE


# --- 10: unsupported file types ----------------------------------------------


def test_documentation_and_assets_are_non_synapse(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# docs", encoding="utf-8")
    (repo / "LICENSE").write_text("MIT", encoding="utf-8")
    (repo / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    result = detect_artifacts(walk_repository(make_config(repo)), make_config(repo))

    assert {a.category for a in result.artifacts} == {ArtifactCategory.NON_SYNAPSE}
    assert all(a.source_format == "repository_file" for a in result.artifacts)


def test_known_synapse_folder_with_unsupported_content(tmp_path):
    repo = tmp_path / "repo"
    target = repo / "workspace" / "PowerBITemplate" / "Report.pbit"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"PK\x03\x04binary")

    result = detect_artifacts(walk_repository(make_config(repo)), make_config(repo))
    artifact = result.artifacts[0]

    assert artifact.category is ArtifactCategory.UNSUPPORTED
    assert artifact.source_format == "unsupported_synapse_file"
    assert result.unsupported == (artifact,)


def test_unrecognized_binary_outside_a_synapse_folder_is_unknown(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")

    result = detect_artifacts(walk_repository(make_config(repo)), make_config(repo))

    assert result.artifacts[0].category is ArtifactCategory.UNKNOWN


def test_kql_outside_its_folder_is_not_claimed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scratch.kql").write_text("Trips | take 5", encoding="utf-8")

    result = detect_artifacts(walk_repository(make_config(repo)), make_config(repo))

    assert result.artifacts[0].category is ArtifactCategory.UNKNOWN


# --- 11: ambiguous JSON ------------------------------------------------------


def test_ambiguous_synapse_shaped_json_is_unknown(tmp_path):
    artifact = detect_one(tmp_path, "workspace/pipeline/Mystery.json", AMBIGUOUS_JSON)

    assert artifact.category is ArtifactCategory.UNSUPPORTED
    assert artifact.confidence == CONFIDENCE_NONE
    # The folder hint is retained as evidence, but it did not classify the file.
    assert artifact.evidence.of_type(SignalType.PATH)
    assert artifact.discovery_source is DiscoverySource.NONE


def test_weak_structure_without_a_matching_folder_is_not_classified(tmp_path):
    """A generic {type: ManagedIdentity} shape proves nothing on its own."""
    artifact = detect_one(tmp_path, "config/WorkspaceSystemIdentity.json", CREDENTIAL)

    assert artifact.category is ArtifactCategory.UNKNOWN
    assert artifact.confidence == CONFIDENCE_NONE
    assert artifact.evidence.of_type(SignalType.PARSE)


def test_unparseable_json_is_unknown_under_the_skip_policy(tmp_path):
    artifact = detect_raw(tmp_path, "workspace/pipeline/Broken.json", "{not json")

    assert artifact.category is ArtifactCategory.UNKNOWN
    assert artifact.source_format == "unparseable_json"
    assert artifact.evidence.of_type(SignalType.PARSE)


def test_unparseable_json_raises_under_the_fail_policy(tmp_path):
    with pytest.raises(ParseError) as excinfo:
        detect_raw(
            tmp_path, "workspace/pipeline/Broken.json", "{not json", on_parse_error="fail"
        )
    assert excinfo.value.source_file == "workspace/pipeline/Broken.json"


def test_json_array_root_is_unknown(tmp_path):
    artifact = detect_raw(tmp_path, "list.json", "[1, 2, 3]")

    assert artifact.category is ArtifactCategory.UNKNOWN
    assert artifact.source_format == "unparseable_json"


def test_utf8_bom_is_tolerated(tmp_path):
    repo = tmp_path / "repo"
    target = repo / "workspace" / "notebook" / "NB_Bom.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(NOTEBOOK), encoding="utf-8-sig")

    result = detect_artifacts(walk_repository(make_config(repo)), make_config(repo))

    assert result.artifacts[0].artifact_type == "notebook"


# --- 12: misleading folder vs structure --------------------------------------


def test_structure_wins_over_a_misleading_folder(tmp_path):
    """A pipeline sitting in notebook/ is a pipeline, flagged for review."""
    artifact = detect_one(tmp_path, "workspace/notebook/PL_Load_Sales.json", PIPELINE)

    assert artifact.artifact_type == "pipeline"
    assert artifact.confidence == CONFIDENCE_STRUCTURE_PATH_CONFLICT
    assert artifact.discovery_source is DiscoverySource.STRUCTURE

    conflicts = artifact.evidence.of_type(SignalType.PATH_CONFLICT)
    assert len(conflicts) == 1
    assert "folder=notebook" in conflicts[0].value
    assert "structure=pipeline" in conflicts[0].value


def test_a_folder_name_alone_never_classifies(tmp_path):
    """A README inside notebook/ must not become a notebook."""
    repo = tmp_path / "repo"
    target = repo / "workspace" / "notebook" / "README.md"
    target.parent.mkdir(parents=True)
    target.write_text("# how these notebooks work", encoding="utf-8")

    result = detect_artifacts(walk_repository(make_config(repo)), make_config(repo))
    artifact = result.artifacts[0]

    assert artifact.category is ArtifactCategory.NON_SYNAPSE
    assert artifact.artifact_type != "notebook"


def test_structure_only_match_outside_any_known_folder(tmp_path):
    artifact = detect_one(tmp_path, "exports/PL_Load_Sales.json", PIPELINE)

    assert artifact.artifact_type == "pipeline"
    assert artifact.confidence == CONFIDENCE_STRUCTURE_ONLY
    assert artifact.discovery_source is DiscoverySource.STRUCTURE
    assert artifact.evidence.of_type(SignalType.PATH) == ()


# --- 13: determinism ---------------------------------------------------------


def test_classification_is_identical_across_runs(tmp_path):
    repo = build_workspace(tmp_path)
    config = make_config(repo)

    first = detect_artifacts(walk_repository(config), config)
    second = detect_artifacts(walk_repository(config), config)

    assert first.artifacts == second.artifacts
    assert first.counts_by_type() == second.counts_by_type()


def test_results_are_sorted_by_source_path(tmp_path):
    result = detect_workspace(tmp_path)
    paths = [a.source_path for a in result.artifacts]

    assert paths == sorted(paths)


def test_confidence_values_come_from_a_fixed_set(tmp_path):
    result = detect_workspace(tmp_path)
    allowed = {
        CONFIDENCE_NONE,
        CONFIDENCE_STRUCTURE_PATH_CONFLICT,
        CONFIDENCE_WEAK_WITH_PATH,
        CONFIDENCE_STRUCTURE_ONLY,
        CONFIDENCE_STRUCTURE_AND_PATH,
    }
    assert {a.confidence for a in result.artifacts} <= allowed


def test_detector_does_not_walk_the_filesystem_itself(tmp_path):
    """Files outside the walk result are never classified."""
    repo = build_workspace(tmp_path)
    config = make_config(repo)
    walk = walk_repository(make_config(repo, include=("*/notebook/*",)))

    result = detect_artifacts(walk, config)

    assert [a.source_path for a in result.artifacts] == [
        "workspace/notebook/NB_Explore_Taxi.json"
    ]


# --- 14: evidence ------------------------------------------------------------


def test_evidence_records_both_signals_for_a_confident_match(tmp_path):
    artifact = detect_one(tmp_path, "workspace/notebook/NB_Explore_Taxi.json", NOTEBOOK)
    signals = artifact.evidence.signals

    assert [s.type for s in signals] == [SignalType.PATH, SignalType.JSON_STRUCTURE]
    assert signals[0].value == "notebook/"
    assert "nbformat" in signals[1].value


def test_evidence_is_structured_data_not_prose(tmp_path):
    artifact = detect_one(tmp_path, "workspace/pipeline/PL_Load_Sales.json", PIPELINE)
    payload = artifact.evidence.to_dict()

    assert set(payload) == {"signals"}
    for signal in payload["signals"]:
        assert set(signal) == {"type", "value"}
        assert isinstance(signal["type"], str)
        assert isinstance(signal["value"], str)


def test_every_verdict_carries_evidence(tmp_path):
    result = detect_workspace(tmp_path)

    for artifact in result.artifacts:
        assert artifact.evidence.signals, f"no evidence for {artifact.source_path}"


def test_unknown_files_retain_their_reason(tmp_path):
    artifact = detect_one(tmp_path, "package.json", PACKAGE_JSON)
    structure = artifact.evidence.of_type(SignalType.JSON_STRUCTURE)

    assert structure
    assert structure[0].value == "no matching artifact structure"


def test_detected_artifact_to_dict_has_the_required_fields(tmp_path):
    artifact = detect_one(tmp_path, "workspace/notebook/NB_Explore_Taxi.json", NOTEBOOK)
    payload = artifact.to_dict()

    for field in (
        "artifact_type",
        "artifact_name",
        "source_path",
        "source_format",
        "confidence",
        "evidence",
    ):
        assert field in payload
    assert payload["evidence"]["signals"]


# --- result shape ------------------------------------------------------------


def test_result_separates_categories(tmp_path):
    result = detect_workspace(tmp_path)

    assert len(result.synapse_artifacts) == 12
    assert {a.source_path for a in result.non_synapse} == {
        "README.md",
        "azuredeploy.json",
    }
    assert result.unknown == ()
    assert result.unsupported == ()


def test_synapse_counts_by_type(tmp_path):
    result = detect_workspace(tmp_path)

    assert result.synapse_counts_by_type() == {
        "credential": 1,
        "dataflow": 1,
        "dataset": 1,
        "integrationRuntime": 1,
        "kqlscript": 1,
        "linkedService": 1,
        "managedVirtualNetwork": 1,
        "notebook": 1,
        "pipeline": 1,
        "sparkJobDefinition": 1,
        "sqlscript": 1,
        "trigger": 1,
    }


def test_empty_walk_produces_an_empty_result(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = detect_artifacts(walk_repository(make_config(repo)), make_config(repo))

    assert result.artifacts == ()
    assert result.counts_by_type() == {}


def test_rules_are_injectable(tmp_path):
    """The rule set is a parameter, so it can be narrowed or extended."""
    repo = build_workspace(tmp_path)
    detector = ArtifactDetector(make_config(repo), structure_rules=(), extension_rules=())
    result = detector.detect(walk_repository(make_config(repo)))

    assert result.synapse_artifacts == ()
