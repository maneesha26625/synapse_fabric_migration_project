"""Tests for PipelineExtractor.

Synthetic pipeline JSON written into temporary directories. No network, no
git, no dependency on the real repository — that is the smoke test's job.
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
from discovery_agent.extractors import (
    ExtractionContext,
    ExtractionStatus,
    ExtractorOrchestrator,
    IssueCode,
    PipelineExtractor,
    ReferenceKind,
    RepositoryArtifactSource,
    SourceType,
    default_registry,
)
from discovery_agent.extractors.pipeline_models import ScriptLanguage
from discovery_agent.extractors.sql_models import (
    SqlFeatureKind,
    SqlObjectKind,
    SqlOperation,
)
from discovery_agent.extractors.synapse_json import ExpressionForm
from discovery_agent.models import AssetType

EXPRESSION = {"value": "@pipeline().parameters.KeyVaultName", "type": "Expression"}


# --- fixtures ----------------------------------------------------------------


def make_artifact(name="PL_Test", path=None) -> DetectedArtifact:
    return DetectedArtifact(
        artifact_type="pipeline",
        artifact_name=name,
        source_path=path or f"workspace/pipeline/{name}.json",
        source_format="synapse_pipeline_json",
        confidence=1.0,
        discovery_source=DiscoverySource.PATH_AND_STRUCTURE,
        category=ArtifactCategory.SYNAPSE,
        evidence=DetectionEvidence((Signal(SignalType.PATH, "pipeline/"),)),
        sha256="c" * 64,
    )


def pipeline_json(activities, name="PL_Test", **properties) -> dict:
    document = {"name": name, "properties": {"activities": activities}}
    document["properties"].update(properties)
    return document


def extract(tmp_path, document, artifact=None, repository=None, raw=None):
    """Write a pipeline and extract it through the real extractor."""
    artifact = artifact or make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        raw if raw is not None else json.dumps(document), encoding="utf-8"
    )
    context = ExtractionContext(
        source=RepositoryArtifactSource(tmp_path, repository)
    )
    return PipelineExtractor().extract(artifact, context)


def activity(name, activity_type, **fields) -> dict:
    return dict(name=name, type=activity_type, **fields)


def dataset_ref(name, **parameters) -> dict:
    reference = {"referenceName": name, "type": "DatasetReference"}
    if parameters:
        reference["parameters"] = parameters
    return reference


def find(definition, name):
    for candidate in definition.walk():
        if candidate.name == name:
            return candidate
    raise AssertionError(f"activity {name!r} not found")


# --- 1, 2: basic structure and dependencies ----------------------------------


def test_simple_pipeline_with_one_activity(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [activity("Wait1", "Wait", typeProperties={"waitTimeInSeconds": 5})],
            name="PL_Simple",
        ),
    )

    assert result.status is ExtractionStatus.SUCCESS
    definition = result.content
    assert definition.name == "PL_Simple"
    assert definition.activity_count == 1
    assert definition.activities[0].name == "Wait1"
    assert definition.activities[0].type == "Wait"
    assert definition.activities[0].recognized
    assert definition.max_depth == 1


def test_depends_on_with_conditions(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity("First", "Wait", typeProperties={}),
                activity(
                    "Second",
                    "Wait",
                    typeProperties={},
                    dependsOn=[
                        {"activity": "First", "dependencyConditions": ["Succeeded", "Skipped"]}
                    ],
                ),
            ]
        ),
    )

    second = find(result.content, "Second")
    assert len(second.depends_on) == 1
    assert second.depends_on[0].activity == "First"
    assert second.depends_on[0].conditions == ("Skipped", "Succeeded")


def test_activity_order_is_preserved(tmp_path):
    names = ["Zulu", "Alpha", "Mike"]
    result = extract(
        tmp_path,
        pipeline_json([activity(n, "Wait", typeProperties={}) for n in names]),
    )

    assert [a.name for a in result.content.activities] == names


def test_policy_and_user_properties(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Copy1",
                    "Copy",
                    typeProperties={},
                    policy={
                        "timeout": "0.00:10:00",
                        "retry": 3,
                        "retryIntervalInSeconds": 30,
                        "secureInput": False,
                        "secureOutput": True,
                    },
                    userProperties=[{"name": "Source", "value": "adls"}],
                )
            ]
        ),
    )

    copy = find(result.content, "Copy1")
    assert copy.policy.timeout == "0.00:10:00"
    assert copy.policy.retry == 3
    assert copy.policy.retry_interval_seconds == 30
    assert copy.policy.secure_input is False
    assert copy.policy.secure_output is True
    assert copy.user_properties[0].name == "Source"
    assert copy.user_properties[0].value == "adls"


# --- 3-7: reference extraction -----------------------------------------------


def test_dataset_reference(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Copy1",
                    "Copy",
                    typeProperties={},
                    inputs=[dataset_ref("tripsDataSource")],
                    outputs=[dataset_ref("tripDataSink")],
                )
            ]
        ),
    )

    references = result.content.references
    assert {r.target_name for r in references} == {"tripsDataSource", "tripDataSink"}
    for reference in references:
        assert reference.target_type is AssetType.DATASET
        assert reference.kind is ReferenceKind.ARTIFACT
        assert reference.resolved is False
        assert reference.source_artifact_id == "synapse://pipeline/PL_Test"
    inputs = [r for r in references if r.target_name == "tripsDataSource"][0]
    assert inputs.location == "properties.activities[0].inputs[0]"


def test_linked_service_reference(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Copy1",
                    "Copy",
                    typeProperties={
                        "enableStaging": True,
                        "stagingSettings": {
                            "linkedServiceName": {
                                "referenceName": "LS_Lake",
                                "type": "LinkedServiceReference",
                            }
                        },
                    },
                )
            ]
        ),
    )

    reference = result.content.references[0]
    assert reference.target_name == "LS_Lake"
    assert reference.target_type is AssetType.LINKED_SERVICE
    assert reference.target_id == "synapse://linkedService/LS_Lake"
    assert "stagingSettings.linkedServiceName" in reference.location


def test_execute_pipeline_reference(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "RunChild",
                    "ExecutePipeline",
                    typeProperties={
                        "pipeline": {
                            "referenceName": "PL_Child",
                            "type": "PipelineReference",
                        },
                        "waitOnCompletion": True,
                    },
                )
            ]
        ),
    )

    reference = result.content.references[0]
    assert reference.target_type is AssetType.PIPELINE
    assert reference.target_name == "PL_Child"
    settings = {s.key: s.value for s in find(result.content, "RunChild").settings}
    assert settings["waitOnCompletion"] == "true"


def test_notebook_activity_reference_and_spark_pool(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "RunNotebook",
                    "SynapseNotebook",
                    typeProperties={
                        "notebook": {
                            "referenceName": "NB_Explore",
                            "type": "NotebookReference",
                        },
                        "sparkPool": {
                            "referenceName": "sparkpool01",
                            "type": "BigDataPoolReference",
                        },
                        "numExecutors": 4,
                    },
                )
            ]
        ),
    )

    by_name = {r.target_name: r for r in result.content.references}
    assert by_name["NB_Explore"].target_type is AssetType.NOTEBOOK
    assert by_name["NB_Explore"].kind is ReferenceKind.ARTIFACT
    # A Spark pool is workspace compute, not a repository artifact.
    assert by_name["sparkpool01"].target_type is None
    assert by_name["sparkpool01"].kind is ReferenceKind.COMPUTE
    assert by_name["sparkpool01"].target_id is None


def test_data_flow_activity_reference_and_compute(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "RunFlow",
                    "ExecuteDataFlow",
                    typeProperties={
                        "dataflow": {
                            "referenceName": "DF_Trips",
                            "type": "DataFlowReference",
                        },
                        "compute": {"coreCount": 8, "computeType": "General"},
                        "traceLevel": "Fine",
                    },
                )
            ]
        ),
    )

    flow = find(result.content, "RunFlow")
    assert flow.references[0].target_type is AssetType.DATAFLOW
    assert flow.compute.core_count == 8
    assert flow.compute.compute_type == "General"
    assert {s.key: s.value for s in flow.settings}["traceLevel"] == "Fine"


def test_stored_procedure_name_is_a_sql_object_not_an_artifact(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "RunProc",
                    "SqlPoolStoredProcedure",
                    typeProperties={"storedProcedureName": "dbo.usp_Load"},
                )
            ]
        ),
    )

    reference = result.content.references[0]
    assert reference.target_name == "dbo.usp_Load"
    assert reference.kind is ReferenceKind.SQL_OBJECT
    assert reference.target_type is None
    assert reference.location.endswith("storedProcedureName")


def test_web_activity_url_is_an_external_endpoint(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "CallApi",
                    "WebActivity",
                    typeProperties={"url": "https://api.example.com/run", "method": "POST"},
                )
            ]
        ),
    )

    reference = result.content.references[0]
    assert reference.kind is ReferenceKind.EXTERNAL_ENDPOINT
    assert reference.target_type is None
    assert reference.target_name == "https://api.example.com/run"


def test_unknown_reference_type_is_recorded_with_a_warning(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Odd",
                    "Copy",
                    typeProperties={
                        "thing": {"referenceName": "X", "type": "MysteryReference"}
                    },
                )
            ]
        ),
    )

    assert result.status is ExtractionStatus.PARTIAL
    reference = result.content.references[0]
    assert reference.kind is ReferenceKind.UNKNOWN
    assert reference.target_type is None
    assert any("MysteryReference" in w.message for w in result.warnings)


# --- 8-11: nested control flow -----------------------------------------------


def test_nested_for_each(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "ForEachFile",
                    "ForEach",
                    typeProperties={
                        "isSequential": True,
                        "items": {"value": "@pipeline().parameters.files", "type": "Expression"},
                        "activities": [
                            activity(
                                "CopyInner",
                                "Copy",
                                typeProperties={},
                                inputs=[dataset_ref("DS_Inner")],
                            )
                        ],
                    },
                )
            ]
        ),
    )

    definition = result.content
    outer = find(definition, "ForEachFile")
    inner = find(definition, "CopyInner")

    assert outer.is_container
    assert [c.name for c in outer.children] == ["CopyInner"]
    assert inner.parent == "ForEachFile"
    assert definition.max_depth == 2
    assert definition.activity_count == 2
    # The inner activity's reference belongs to the inner activity.
    assert [r.target_name for r in inner.references] == ["DS_Inner"]
    assert outer.references == ()
    assert {s.key: s.value for s in outer.settings}["isSequential"] == "true"


def test_nested_if_condition_branches(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "CheckIt",
                    "IfCondition",
                    typeProperties={
                        "expression": {"value": "@greater(1,0)", "type": "Expression"},
                        "ifTrueActivities": [activity("WhenTrue", "Wait", typeProperties={})],
                        "ifFalseActivities": [activity("WhenFalse", "Wait", typeProperties={})],
                    },
                )
            ]
        ),
    )

    definition = result.content
    assert [c.name for c in find(definition, "CheckIt").children] == [
        "WhenTrue",
        "WhenFalse",
    ]
    assert find(definition, "WhenTrue").branch == "ifTrue"
    assert find(definition, "WhenFalse").branch == "ifFalse"
    assert find(definition, "WhenTrue").parent == "CheckIt"


def test_nested_until(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Poll",
                    "Until",
                    typeProperties={
                        "expression": {"value": "@equals(1,1)", "type": "Expression"},
                        "timeout": "0.00:30:00",
                        "activities": [activity("Check", "Wait", typeProperties={})],
                    },
                )
            ]
        ),
    )

    definition = result.content
    assert find(definition, "Check").parent == "Poll"
    assert definition.max_depth == 2
    assert {s.key: s.value for s in find(definition, "Poll").settings}["timeout"] == "0.00:30:00"


def test_switch_with_multiple_branches(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Route",
                    "Switch",
                    typeProperties={
                        "on": {"value": "@pipeline().parameters.mode", "type": "Expression"},
                        "cases": [
                            {"value": "full", "activities": [activity("FullLoad", "Wait", typeProperties={})]},
                            {"value": "delta", "activities": [activity("DeltaLoad", "Wait", typeProperties={})]},
                        ],
                        "defaultActivities": [activity("Fallback", "Wait", typeProperties={})],
                    },
                )
            ]
        ),
    )

    definition = result.content
    branches = {c.name: c.branch for c in find(definition, "Route").children}
    assert branches == {
        "FullLoad": "case=full",
        "DeltaLoad": "case=delta",
        "Fallback": "default",
    }
    assert definition.activity_count == 4


def test_deeply_nested_hierarchy_is_preserved(tmp_path):
    """ForEach -> IfCondition -> Copy must not flatten."""
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Outer",
                    "ForEach",
                    typeProperties={
                        "activities": [
                            activity(
                                "Middle",
                                "IfCondition",
                                typeProperties={
                                    "ifTrueActivities": [
                                        activity(
                                            "Inner",
                                            "Copy",
                                            typeProperties={},
                                            inputs=[dataset_ref("DS_Deep")],
                                        )
                                    ]
                                },
                            )
                        ]
                    },
                )
            ]
        ),
    )

    definition = result.content
    assert definition.max_depth == 3
    assert definition.activity_count == 3
    assert find(definition, "Inner").parent == "Middle"
    assert find(definition, "Middle").parent == "Outer"
    assert find(definition, "Outer").parent is None
    assert [r.target_name for r in definition.references] == ["DS_Deep"]
    assert find(definition, "Inner").path.endswith("ifTrueActivities[0]")


def test_unknown_container_type_still_recurses(tmp_path):
    """An unmodelled activity holding an activities list keeps its children."""
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "FutureLoop",
                    "SomeFutureControlFlow",
                    typeProperties={"activities": [activity("Nested", "Wait", typeProperties={})]},
                )
            ]
        ),
    )

    definition = result.content
    assert definition.activity_count == 2
    assert find(definition, "Nested").parent == "FutureLoop"
    assert find(definition, "FutureLoop").recognized is False


# --- 12, 13: parameters and variables ----------------------------------------


def test_parameters(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [activity("Wait1", "Wait", typeProperties={})],
            parameters={
                "SchemaName": {"type": "string", "defaultValue": "tripFares"},
                "BatchSize": {"type": "int", "defaultValue": 100},
                "NoDefault": {"type": "string"},
            },
        ),
    )

    parameters = {p.name: p for p in result.content.parameters}
    assert [p.name for p in result.content.parameters] == [
        "BatchSize",
        "NoDefault",
        "SchemaName",
    ]
    assert parameters["SchemaName"].type == "string"
    assert parameters["SchemaName"].default_value == "tripFares"
    assert parameters["SchemaName"].has_default is True
    assert parameters["BatchSize"].default_value == "100"
    assert parameters["NoDefault"].has_default is False
    assert parameters["NoDefault"].default_value is None


def test_variables(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [activity("Wait1", "Wait", typeProperties={})],
            variables={"counter": {"type": "Integer", "defaultValue": 0}},
        ),
    )

    variable = result.content.variables[0]
    assert variable.name == "counter"
    assert variable.type == "Integer"
    assert variable.default_value == "0"


def test_folder_annotations_and_description(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [activity("Wait1", "Wait", typeProperties={})],
            folder={"name": "Ingestion"},
            annotations=["daily", "finance"],
            description="Loads the daily extract.",
            concurrency=2,
        ),
    )

    definition = result.content
    assert definition.folder == "Ingestion"
    assert definition.annotations == ("daily", "finance")
    assert definition.description == "Loads the daily extract."
    assert definition.concurrency == 2


# --- 14: expressions ---------------------------------------------------------


def test_expressions_are_preserved_verbatim(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Copy1",
                    "Copy",
                    typeProperties={},
                    outputs=[dataset_ref("DS_Sink", keyVaultName=EXPRESSION)],
                )
            ]
        ),
    )

    expressions = result.content.expressions
    assert len(expressions) == 1
    assert expressions[0].expression == "@pipeline().parameters.KeyVaultName"
    assert expressions[0].form is ExpressionForm.OBJECT
    assert expressions[0].location == (
        "properties.activities[0].outputs[0].parameters.keyVaultName"
    )


def test_inline_string_expressions_are_captured(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Wait1",
                    "Wait",
                    typeProperties={"waitTimeInSeconds": "@pipeline().parameters.delay"},
                )
            ]
        ),
    )

    expression = result.content.expressions[0]
    assert expression.form is ExpressionForm.INLINE
    assert expression.expression == "@pipeline().parameters.delay"


def test_control_flow_conditions_are_captured_as_expressions(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Loop",
                    "ForEach",
                    typeProperties={
                        "items": {"value": "@pipeline().parameters.files", "type": "Expression"},
                        "activities": [],
                    },
                )
            ]
        ),
    )

    assert [e.expression for e in result.content.expressions] == [
        "@pipeline().parameters.files"
    ]


def test_expressions_are_not_evaluated_or_translated(tmp_path):
    original = "@concat('a', pipeline().parameters.b)"
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Wait1",
                    "Wait",
                    typeProperties={"waitTimeInSeconds": {"value": original, "type": "Expression"}},
                )
            ]
        ),
    )

    assert result.content.expressions[0].expression == original


def test_embedded_sql_is_preserved(tmp_path):
    query = "IF NOT EXISTS (SELECT * FROM sys.schemas) BEGIN SELECT 1 END"
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Lookup1",
                    "Lookup",
                    typeProperties={
                        "source": {"type": "SqlDWSource", "sqlReaderQuery": {"value": query, "type": "Expression"}},
                        "firstRowOnly": False,
                    },
                )
            ]
        ),
    )

    script = find(result.content, "Lookup1").scripts[0]
    assert script.language is ScriptLanguage.SQL
    assert script.text == query
    assert script.location.endswith("source.sqlReaderQuery")


# --- 15: multiple references from one activity -------------------------------


def test_multiple_references_from_one_activity(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Copy1",
                    "Copy",
                    typeProperties={
                        "stagingSettings": {
                            "linkedServiceName": {
                                "referenceName": "LS_Stage",
                                "type": "LinkedServiceReference",
                            }
                        }
                    },
                    inputs=[dataset_ref("DS_In")],
                    outputs=[dataset_ref("DS_Out")],
                )
            ]
        ),
    )

    copy = find(result.content, "Copy1")
    assert len(copy.references) == 3
    assert {r.target_name for r in copy.references} == {"DS_In", "DS_Out", "LS_Stage"}
    assert {r.target_type for r in copy.references} == {
        AssetType.DATASET,
        AssetType.LINKED_SERVICE,
    }


def test_copy_data_movement_shape(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Copy1",
                    "Copy",
                    typeProperties={
                        "source": {
                            "type": "DelimitedTextSource",
                            "storeSettings": {"type": "HttpReadSettings"},
                            "formatSettings": {"type": "DelimitedTextReadSettings"},
                        },
                        "sink": {
                            "type": "SqlDWSink",
                            "preCopyScript": "TRUNCATE TABLE dbo.Trips",
                            "tableOption": "autoCreate",
                        },
                        "enableStaging": True,
                        "translator": {"type": "TabularTranslator"},
                    },
                )
            ]
        ),
    )

    copy = find(result.content, "Copy1")
    movement = copy.data_movement
    assert movement.source_type == "DelimitedTextSource"
    assert movement.sink_type == "SqlDWSink"
    assert movement.source_store_type == "HttpReadSettings"
    assert movement.source_format_type == "DelimitedTextReadSettings"
    assert movement.translator_type == "TabularTranslator"
    assert movement.staging_enabled is True
    assert copy.scripts[0].text == "TRUNCATE TABLE dbo.Trips"
    assert {s.key: s.value for s in copy.settings}["sink.tableOption"] == "autoCreate"


# --- 16: unknown activity types ----------------------------------------------


def test_unknown_activity_type_is_preserved_with_a_warning(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Mystery",
                    "SomeNewActivityType",
                    typeProperties={"widget": "sprocket", "count": 3, "nested": {"a": 1}},
                    dependsOn=[{"activity": "Other", "dependencyConditions": ["Succeeded"]}],
                )
            ]
        ),
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert result.succeeded
    mystery = find(result.content, "Mystery")
    assert mystery.name == "Mystery"
    assert mystery.type == "SomeNewActivityType"
    assert mystery.recognized is False
    assert mystery.depends_on[0].activity == "Other"
    settings = {s.key: s.value for s in mystery.settings}
    assert settings == {"widget": "sprocket", "count": "3"}  # scalars only
    assert result.content.unrecognized_activities == (mystery,)

    warning = [w for w in result.warnings if w.code is IssueCode.UNSUPPORTED_CONSTRUCT][0]
    assert "SomeNewActivityType" in warning.message
    assert warning.location == "properties.activities[0]"


def test_unknown_activity_references_are_still_extracted(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Mystery",
                    "SomeNewActivityType",
                    typeProperties={"thing": dataset_ref("DS_Still_Found")},
                )
            ]
        ),
    )

    assert [r.target_name for r in result.content.references] == ["DS_Still_Found"]


# --- 17, 18: malformed input -------------------------------------------------


def test_malformed_json_fails(tmp_path):
    result = extract(tmp_path, None, raw="{not json")

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert result.errors[0].code is IssueCode.MALFORMED_ARTIFACT
    assert "invalid json" in result.errors[0].message


def test_missing_properties_fails(tmp_path):
    result = extract(tmp_path, {"name": "PL_Test"})

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert "no properties object" in result.errors[0].message


def test_missing_activities_list_fails(tmp_path):
    result = extract(tmp_path, {"name": "PL_Test", "properties": {"parameters": {}}})

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert "no activities list" in result.errors[0].message


def test_activities_of_the_wrong_type_fails(tmp_path):
    result = extract(tmp_path, {"name": "PL", "properties": {"activities": "nope"}})

    assert result.status is ExtractionStatus.FAILED


def test_json_root_that_is_not_an_object_fails(tmp_path):
    result = extract(tmp_path, None, raw="[1, 2, 3]")

    assert result.status is ExtractionStatus.FAILED
    assert "not an object" in result.errors[0].message


def test_an_empty_activity_list_is_a_real_empty_pipeline(tmp_path):
    """The distinction that matters: empty is not the same as malformed."""
    result = extract(tmp_path, pipeline_json([]))

    assert result.status is ExtractionStatus.SUCCESS
    assert result.content is not None
    assert result.content.activities == ()


def test_a_nameless_activity_is_warned_about_not_dropped_silently(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json([{"type": "Wait"}, activity("Good", "Wait", typeProperties={})]),
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert [a.name for a in result.content.activities] == ["Good"]
    assert any("no name" in w.message for w in result.warnings)


def test_an_activity_without_a_type_is_warned_about(tmp_path):
    result = extract(tmp_path, pipeline_json([{"name": "Nameless"}]))

    assert result.status is ExtractionStatus.PARTIAL
    assert any("has no type" in w.message for w in result.warnings)


# --- 19: no name guessing ----------------------------------------------------


def test_a_bare_string_is_never_a_dependency(tmp_path):
    """Strings that look like artifact names must not become references."""
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Copy1",
                    "Copy",
                    typeProperties={
                        "sink": {"type": "SqlDWSink", "tableOption": "customer-data"},
                        "someLabel": "tripsDataSource",
                        "description": "writes to faresDataSink",
                    },
                )
            ],
            parameters={"datasetName": {"type": "string", "defaultValue": "tripsDataSource"}},
        ),
    )

    assert result.content.references == ()


def test_a_reference_shaped_object_missing_its_type_is_not_a_reference(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [activity("Copy1", "Copy", typeProperties={"thing": {"referenceName": "DS_X"}})]
        ),
    )

    assert result.content.references == ()


def test_a_type_not_ending_in_reference_is_not_a_reference(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Copy1",
                    "Copy",
                    typeProperties={
                        "source": {"referenceName": "DS_X", "type": "DelimitedTextSource"}
                    },
                )
            ]
        ),
    )

    assert result.content.references == ()


# --- 20, 21, 22: framework integration ---------------------------------------


def test_provenance_is_preserved(tmp_path):
    repository = RepositorySource(
        provider="github",
        repository_url="https://github.com/contoso/synapse-workspace",
        ref="main",
        local_path=tmp_path,
        commit_sha="d" * 40,
    )
    result = extract(
        tmp_path,
        pipeline_json([activity("Wait1", "Wait", typeProperties={})]),
        repository=repository,
    )

    provenance = result.provenance
    assert provenance.source_type is SourceType.REPOSITORY
    assert provenance.repository_url == "https://github.com/contoso/synapse-workspace"
    assert provenance.ref == "main"
    assert provenance.commit_sha == "d" * 40
    assert provenance.source_path == "workspace/pipeline/PL_Test.json"
    assert provenance.sha256 == "c" * 64
    assert provenance.source_format == "synapse_pipeline_json"
    assert result.extractor.name == "pipeline"
    assert result.artifact_id == "synapse://pipeline/PL_Test"


def test_registry_selects_the_pipeline_extractor():
    registry = default_registry()

    selected = registry.require(AssetType.PIPELINE, SourceType.REPOSITORY)

    assert isinstance(selected, PipelineExtractor)
    assert selected.supported_types == (AssetType.PIPELINE,)
    # Both sources serve the same {name, properties} document, so the same
    # extractor is selected for a live workspace as for a clone.
    assert selected.supported_sources == (SourceType.REPOSITORY, SourceType.SYNAPSE)


def test_extraction_runs_through_the_orchestrator(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            pipeline_json(
                [activity("Copy1", "Copy", typeProperties={}, inputs=[dataset_ref("DS_In")])]
            )
        ),
        encoding="utf-8",
    )
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path))

    run = ExtractorOrchestrator(default_registry(), context).run([artifact])

    assert len(run.results) == 1
    result = run.results[0]
    assert result.succeeded
    assert result.artifact_type is AssetType.PIPELINE
    assert result.content.activity_count == 1
    # References surface at the run level for the future graph stage.
    assert [r.target_name for r in run.references] == ["DS_In"]


def test_malformed_pipeline_through_the_orchestrator(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text("{oops", encoding="utf-8")
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path))

    run = ExtractorOrchestrator(default_registry(), context).run([artifact])

    assert run.results[0].status is ExtractionStatus.FAILED
    assert run.failed == run.results


# --- determinism -------------------------------------------------------------


def test_extraction_is_deterministic(tmp_path):
    document = pipeline_json(
        [
            activity(
                "Copy1",
                "Copy",
                typeProperties={
                    "stagingSettings": {
                        "linkedServiceName": {
                            "referenceName": "LS_A",
                            "type": "LinkedServiceReference",
                        }
                    }
                },
                inputs=[dataset_ref("DS_B", keyVaultName=EXPRESSION)],
                outputs=[dataset_ref("DS_A")],
            )
        ],
        parameters={"z": {"type": "string"}, "a": {"type": "string"}},
    )

    first = extract(tmp_path, document)
    second = extract(tmp_path, document)

    assert first.content == second.content
    assert first.references == second.references


def test_references_and_parameters_are_sorted(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Copy1",
                    "Copy",
                    typeProperties={},
                    inputs=[dataset_ref("DS_Zulu")],
                    outputs=[dataset_ref("DS_Alpha")],
                )
            ],
            parameters={"zeta": {"type": "string"}, "alpha": {"type": "string"}},
        ),
    )

    locations = [r.location for r in result.content.references]
    assert locations == sorted(locations)
    assert [p.name for p in result.content.parameters] == ["alpha", "zeta"]


def test_to_dict_is_json_serializable(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Loop",
                    "ForEach",
                    typeProperties={"activities": [activity("Inner", "Wait", typeProperties={})]},
                )
            ]
        ),
    )

    payload = result.content.to_dict()
    json.dumps(payload)  # must not raise
    assert payload["activities"][0]["children"][0]["name"] == "Inner"


# --- 20: SQL analysis of embedded scripts ------------------------------------


def copy_with(sink_script=None, source_query=None, name="Copy1"):
    """A Copy activity carrying up to two embedded SQL scripts."""
    sink = {"type": "SqlDWSink"}
    if sink_script is not None:
        sink["preCopyScript"] = sink_script
    source = {"type": "SqlDWSource"}
    if source_query is not None:
        source["sqlReaderQuery"] = source_query
    return activity(name, "Copy", typeProperties={"source": source, "sink": sink})


def script_activity(*statements, name="Script1"):
    return activity(
        name,
        "Script",
        typeProperties={"scripts": [{"text": s} for s in statements]},
    )


def only_script(result, activity_name="Copy1"):
    scripts = find(result.content, activity_name).scripts
    assert len(scripts) == 1
    return scripts[0]


def test_embedded_sql_is_analysed(tmp_path):
    result = extract(
        tmp_path, pipeline_json([copy_with(sink_script="TRUNCATE TABLE dbo.Trips")])
    )
    script = only_script(result)

    assert [o.qualified_name for o in script.objects] == ["dbo.Trips"]
    assert script.objects[0].operation is SqlOperation.TRUNCATE
    assert script.objects[0].kind is SqlObjectKind.TABLE


def test_analysis_leaves_the_text_and_location_untouched(tmp_path):
    sql = "  TRUNCATE TABLE dbo.Trips;\n-- trailing comment\n"
    result = extract(tmp_path, pipeline_json([copy_with(sink_script=sql)]))
    script = only_script(result)

    assert script.text == sql
    assert script.location == "properties.activities[0].typeProperties.sink.preCopyScript"
    assert script.language is ScriptLanguage.SQL


def test_findings_carry_the_script_location_and_a_line_number(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json([copy_with(sink_script="SELECT 1;\nTRUNCATE TABLE dbo.Trips;")]),
    )

    assert only_script(result).objects[0].location == (
        "properties.activities[0].typeProperties.sink.preCopyScript:L2"
    )


def test_an_activity_with_no_sql_has_no_findings(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json([activity("Wait1", "Wait", typeProperties={"waitTimeInSeconds": 1})]),
    )
    waited = find(result.content, "Wait1")

    assert waited.scripts == ()
    assert waited.sql_objects == ()
    assert result.status is ExtractionStatus.SUCCESS


# --- findings stay attached to the individual script -------------------------


def test_two_scripts_in_one_activity_keep_their_own_findings(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                copy_with(
                    sink_script="TRUNCATE TABLE dbo.Target",
                    source_query="SELECT * FROM dbo.Source",
                )
            ]
        ),
    )
    by_location = {s.location.rsplit(".", 1)[-1]: s for s in find(result.content, "Copy1").scripts}

    assert set(by_location) == {"preCopyScript", "sqlReaderQuery"}
    assert [o.qualified_name for o in by_location["preCopyScript"].objects] == ["dbo.Target"]
    assert [o.qualified_name for o in by_location["sqlReaderQuery"].objects] == ["dbo.Source"]


def test_a_script_activitys_statements_are_analysed_separately(tmp_path):
    """The future Script activity: one findings set per statement, not pooled."""
    result = extract(
        tmp_path,
        pipeline_json(
            [
                script_activity(
                    "CREATE TABLE dbo.Staging (id int)",
                    "INSERT INTO dbo.Final SELECT * FROM dbo.Staging",
                    "DROP TABLE dbo.Staging",
                )
            ]
        ),
    )
    scripts = find(result.content, "Script1").scripts

    assert len(scripts) == 3
    assert [o.operation for o in scripts[0].objects] == [SqlOperation.CREATE]
    assert {o.qualified_name for o in scripts[1].objects} == {"dbo.Final", "dbo.Staging"}
    assert [o.operation for o in scripts[2].objects] == [SqlOperation.DROP]


def test_each_statement_keeps_its_own_indexed_location(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json([script_activity("SELECT * FROM dbo.A", "SELECT * FROM dbo.B")]),
    )
    scripts = find(result.content, "Script1").scripts

    assert [s.location for s in scripts] == [
        "properties.activities[0].typeProperties.scripts[0].text",
        "properties.activities[0].typeProperties.scripts[1].text",
    ]
    assert scripts[0].objects[0].location.endswith("scripts[0].text:L1")
    assert scripts[1].objects[0].location.endswith("scripts[1].text:L1")


def test_activity_level_sql_objects_gather_every_script(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json([script_activity("SELECT * FROM dbo.A", "SELECT * FROM dbo.B")]),
    )

    assert {o.qualified_name for o in find(result.content, "Script1").sql_objects} == {
        "dbo.A",
        "dbo.B",
    }


# --- the reference channel ---------------------------------------------------


def test_sql_objects_surface_as_references(tmp_path):
    result = extract(
        tmp_path, pipeline_json([copy_with(sink_script="TRUNCATE TABLE dbo.Trips")])
    )
    sql_references = [
        r for r in find(result.content, "Copy1").references
        if r.kind is ReferenceKind.SQL_OBJECT
    ]

    assert [r.target_name for r in sql_references] == ["dbo.Trips"]
    assert sql_references[0].source_artifact_type is AssetType.PIPELINE
    assert sql_references[0].source_artifact_id == "synapse://pipeline/PL_Test"
    assert sql_references[0].evidence.extractor == "pipeline"


def test_a_sql_reference_is_never_resolved_or_typed(tmp_path):
    result = extract(
        tmp_path, pipeline_json([copy_with(sink_script="TRUNCATE TABLE dbo.Trips")])
    )
    reference = [
        r for r in result.content.references if r.kind is ReferenceKind.SQL_OBJECT
    ][0]

    assert reference.target_type is None
    assert reference.target_id is None
    assert reference.resolved is False


def test_the_same_table_in_two_scripts_yields_one_reference(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                copy_with(
                    sink_script="TRUNCATE TABLE dbo.Trips",
                    source_query="SELECT * FROM dbo.Trips",
                )
            ]
        ),
    )
    copy = find(result.content, "Copy1")
    sql_references = [r for r in copy.references if r.kind is ReferenceKind.SQL_OBJECT]

    assert [r.target_name for r in sql_references] == ["dbo.Trips"]
    # Both occurrences are still recorded, one on each script.
    assert len([o for o in copy.sql_objects if o.qualified_name == "dbo.Trips"]) == 2


def test_temporary_objects_are_findings_but_not_references(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [copy_with(sink_script="SELECT * INTO #stage FROM dbo.Real; SELECT * FROM #stage;")]
        ),
    )
    copy = find(result.content, "Copy1")

    assert "#stage" in {o.object_name for o in copy.sql_objects}
    assert {r.target_name for r in copy.references} == {"dbo.Real"}


def test_an_interpolated_table_stays_a_dependency(tmp_path):
    sql = "TRUNCATE TABLE @{pipeline().parameters.SchemaName}.Trips"
    result = extract(tmp_path, pipeline_json([copy_with(sink_script=sql)]))
    copy = find(result.content, "Copy1")

    assert copy.sql_objects[0].kind is SqlObjectKind.INTERPOLATED
    assert [r.target_name for r in copy.references] == [
        "@{pipeline().parameters.SchemaName}.Trips"
    ]
    assert copy.references[0].resolved is False


def test_system_catalog_names_are_not_filtered(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [copy_with(sink_script="SELECT * FROM INFORMATION_SCHEMA.TABLES")]
        ),
    )

    assert [o.qualified_name for o in only_script(result).objects] == [
        "INFORMATION_SCHEMA.TABLES"
    ]
    assert "INFORMATION_SCHEMA.TABLES" in {
        r.target_name for r in result.content.references
    }


def test_nested_activities_are_analysed_and_own_their_references(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                activity(
                    "Loop",
                    "ForEach",
                    typeProperties={
                        "activities": [copy_with(sink_script="TRUNCATE TABLE dbo.Inner")]
                    },
                )
            ]
        ),
    )
    inner = find(result.content, "Copy1")

    assert [o.qualified_name for o in inner.sql_objects] == ["dbo.Inner"]
    assert [r.target_name for r in inner.references] == ["dbo.Inner"]
    assert find(result.content, "Loop").references == ()
    assert "dbo.Inner" in {r.target_name for r in result.content.references}


# --- dynamic SQL and features ------------------------------------------------


def test_dynamic_sql_in_embedded_script_warns_once_for_the_activity(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                copy_with(
                    sink_script="EXEC('TRUNCATE TABLE dbo.A')",
                    source_query="EXEC sp_executesql @stmt",
                )
            ]
        ),
    )

    dynamic_warnings = [
        w for w in result.warnings if "dynamic SQL site(s)" in w.message
    ]
    assert len(dynamic_warnings) == 1
    assert "2 dynamic SQL site(s)" in dynamic_warnings[0].message
    assert dynamic_warnings[0].code is IssueCode.UNSUPPORTED_CONSTRUCT
    assert result.status is ExtractionStatus.PARTIAL


def test_the_table_inside_dynamic_sql_is_not_claimed_as_a_dependency(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json([copy_with(sink_script="EXEC('TRUNCATE TABLE dbo.Secret')")]),
    )

    assert only_script(result).has_dynamic_sql
    assert "dbo.Secret" not in {r.target_name for r in result.content.references}


def test_sql_without_dynamic_sql_adds_no_warning(tmp_path):
    result = extract(
        tmp_path, pipeline_json([copy_with(sink_script="TRUNCATE TABLE dbo.Trips")])
    )

    assert result.warnings == ()
    assert result.status is ExtractionStatus.SUCCESS


def test_dedicated_pool_features_are_recorded_on_the_script(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                script_activity(
                    "CREATE TABLE dbo.T (id int) WITH (DISTRIBUTION = HASH(id), "
                    "CLUSTERED COLUMNSTORE INDEX)"
                )
            ]
        ),
    )
    features = find(result.content, "Script1").scripts[0].features

    assert SqlFeatureKind.DISTRIBUTION in {f.kind for f in features}
    assert SqlFeatureKind.INDEX in {f.kind for f in features}


def test_no_cross_database_feature_is_claimed(tmp_path):
    """The pipeline has no connection context, so the question is left open."""
    result = extract(
        tmp_path,
        pipeline_json([copy_with(sink_script="SELECT * FROM other.dbo.Sales")]),
    )
    script = only_script(result)

    assert script.objects[0].database == "other"
    assert SqlFeatureKind.CROSS_DATABASE not in {f.kind for f in script.features}


def test_credential_literals_in_embedded_sql_are_recorded(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [script_activity("CREATE DATABASE SCOPED CREDENTIAL c WITH SECRET = 'abc'")]
        ),
    )
    script = find(result.content, "Script1").scripts[0]

    assert [s.property_name for s in script.secrets] == ["SECRET"]
    assert all("abc" not in f.evidence for f in script.features)


# --- serialization -----------------------------------------------------------


def test_sql_findings_survive_to_dict(tmp_path):
    result = extract(
        tmp_path, pipeline_json([copy_with(sink_script="TRUNCATE TABLE dbo.Trips")])
    )
    payload = result.content.to_dict()
    json.dumps(payload)  # must not raise

    script = payload["activities"][0]["scripts"][0]
    assert script["text"] == "TRUNCATE TABLE dbo.Trips"
    assert script["objects"][0]["qualified_name"] == "dbo.Trips"
    assert script["objects"][0]["operation"] == "truncate"
    assert script["dynamic_sql"] == []
    assert script["features"] == []
    assert script["secrets"] == []


def test_pipeline_level_accessors_gather_every_script(tmp_path):
    result = extract(
        tmp_path,
        pipeline_json(
            [
                copy_with(sink_script="TRUNCATE TABLE dbo.A", name="C1"),
                activity(
                    "Loop",
                    "ForEach",
                    typeProperties={
                        "activities": [copy_with(sink_script="TRUNCATE TABLE dbo.B", name="C2")]
                    },
                ),
            ]
        ),
    )

    assert len(result.content.scripts) == 2
    assert {o.qualified_name for o in result.content.sql_objects} == {"dbo.A", "dbo.B"}
