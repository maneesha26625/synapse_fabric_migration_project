"""Tests for DatasetExtractor.

Synthetic dataset JSON in temporary directories. No network, no git, no
dependency on the real repository.
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
    DatasetExtractor,
    ExtractionContext,
    ExtractionStatus,
    ExtractorOrchestrator,
    IssueCode,
    ReferenceKind,
    RepositoryArtifactSource,
    SourceType,
    default_registry,
)
from discovery_agent.extractors.dataset_models import (
    LocationCategory,
    ResourceCategory,
    SecretKind,
)
from discovery_agent.extractors.synapse_json import ExpressionForm
from discovery_agent.models import AssetType

FAKE_SECRET = "sv=2021&sig=NOT-A-REAL-SIGNATURE-abc123"

LINKED_SERVICE = {
    "referenceName": "TripFaresDataLakeStorageLinkedService",
    "type": "LinkedServiceReference",
}


def make_artifact(name="DS_Test", path=None) -> DetectedArtifact:
    return DetectedArtifact(
        artifact_type="dataset",
        artifact_name=name,
        source_path=path or f"workspace/dataset/{name}.json",
        source_format="synapse_dataset_json",
        confidence=1.0,
        discovery_source=DiscoverySource.PATH_AND_STRUCTURE,
        category=ArtifactCategory.SYNAPSE,
        evidence=DetectionEvidence((Signal(SignalType.PATH, "dataset/"),)),
        sha256="d" * 64,
    )


def dataset_json(dataset_type="DelimitedText", name="DS_Test", **properties):
    document = {"name": name, "properties": {"type": dataset_type}}
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
    return DatasetExtractor().extract(artifact, context)


def expression(text):
    return {"value": text, "type": "Expression"}


# --- 1, 20: basic structure --------------------------------------------------


def test_basic_dataset(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            "DelimitedText",
            name="DS_Fares",
            linkedServiceName=LINKED_SERVICE,
            folder={"name": "TripFareDatasets"},
            annotations=["raw", "daily"],
        ),
    )

    assert result.status is ExtractionStatus.SUCCESS
    definition = result.content
    assert definition.name == "DS_Fares"
    assert definition.type == "DelimitedText"
    assert definition.folder == "TripFareDatasets"
    assert definition.annotations == ("daily", "raw")
    assert definition.recognized is True


def test_empty_but_valid_configuration_is_not_malformed(tmp_path):
    """typeProperties {} and schema [] are real declarations, not failures."""
    result = extract(
        tmp_path,
        dataset_json(
            "AzureSqlDWTable",
            linkedServiceName=LINKED_SERVICE,
            typeProperties={},
            schema=[],
            annotations=[],
        ),
    )

    assert result.status is ExtractionStatus.SUCCESS
    assert result.content is not None
    assert result.content.schema.declared is True
    assert result.content.schema.column_count == 0
    assert result.content.location is None  # nothing was declared to locate


def test_name_falls_back_to_the_artifact_name(tmp_path):
    document = {"properties": {"type": "DelimitedText"}}

    result = extract(tmp_path, document)

    assert result.content.name == "DS_Test"


# --- 2, 9: linked service and references -------------------------------------


def test_linked_service_reference(tmp_path):
    result = extract(
        tmp_path, dataset_json(linkedServiceName=LINKED_SERVICE)
    )

    definition = result.content
    reference = definition.linked_service
    assert reference is not None
    assert reference.target_name == "TripFaresDataLakeStorageLinkedService"
    assert reference.target_type is AssetType.LINKED_SERVICE
    assert reference.kind is ReferenceKind.ARTIFACT
    assert reference.resolved is False, "references are observations, not edges"
    assert reference.location == "properties.linkedServiceName"
    assert reference.source_artifact_id == "synapse://dataset/DS_Test"
    assert definition.linked_service_name == "TripFaresDataLakeStorageLinkedService"
    # The convenience view does not remove it from the reference list.
    assert reference in definition.references


def test_multiple_references_are_all_captured(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            linkedServiceName=LINKED_SERVICE,
            typeProperties={
                "location": {"type": "AzureBlobFSLocation"},
                "store": {"referenceName": "KV_LS", "type": "LinkedServiceReference"},
                "runtime": {
                    "referenceName": "AutoResolveIntegrationRuntime",
                    "type": "IntegrationRuntimeReference",
                },
            },
        ),
    )

    references = result.content.references
    assert len(references) == 3
    by_name = {r.target_name: r for r in references}
    assert by_name["KV_LS"].target_type is AssetType.LINKED_SERVICE
    assert by_name["AutoResolveIntegrationRuntime"].kind is ReferenceKind.COMPUTE
    # Only the dataset's own linkedServiceName is promoted.
    assert result.content.linked_service.target_name.endswith("LinkedService")


def test_unknown_reference_type_is_recorded_with_a_warning(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(typeProperties={"x": {"referenceName": "Y", "type": "OddReference"}}),
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert result.content.references[0].kind is ReferenceKind.UNKNOWN
    assert result.content.references[0].target_type is None
    assert any("OddReference" in w.message for w in result.warnings)


# --- 3: parameters -----------------------------------------------------------


def test_parameters_with_and_without_defaults(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            parameters={
                "keyVaultName": {"type": "string", "defaultValue": "kvmsft"},
                "datalakeAccountName": {"type": "string", "defaultValue": "adlsmsft"},
                "SchemaName": {"type": "string"},
                "nullable": {"type": "string", "defaultValue": None},
            }
        ),
    )

    parameters = {p.name: p for p in result.content.parameters}
    assert [p.name for p in result.content.parameters] == sorted(parameters)

    assert parameters["keyVaultName"].default_value == "kvmsft"
    assert parameters["keyVaultName"].has_default is True
    assert parameters["SchemaName"].has_default is False
    assert parameters["SchemaName"].default_value is None
    # "no default" and "default = null" stay distinguishable.
    assert parameters["nullable"].has_default is True
    assert parameters["nullable"].default_value is None


def test_a_vault_name_parameter_is_not_treated_as_a_secret(tmp_path):
    """keyVaultName is an identifier; redacting it would destroy information."""
    result = extract(
        tmp_path,
        dataset_json(parameters={"keyVaultName": {"type": "string", "defaultValue": "kv1"}}),
    )

    parameter = result.content.parameters[0]
    assert parameter.is_secret is False
    assert parameter.default_value == "kv1"


# --- 4: expressions ----------------------------------------------------------


def test_expressions_are_preserved_verbatim(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            typeProperties={"schema": expression("@dataset().SchemaName"), "table": "T"},
            linkedServiceName={
                "referenceName": "LS",
                "type": "LinkedServiceReference",
                "parameters": {"kv": expression("@dataset().keyVaultName")},
            },
        ),
    )

    expressions = {e.expression: e for e in result.content.expressions}
    assert "@dataset().SchemaName" in expressions
    assert "@dataset().keyVaultName" in expressions
    assert expressions["@dataset().SchemaName"].form is ExpressionForm.OBJECT
    assert (
        expressions["@dataset().SchemaName"].location
        == "properties.typeProperties.schema"
    )
    assert result.content.is_parameterized


def test_an_expression_valued_field_keeps_the_expression_as_its_value(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            "AzureSqlDWTable",
            typeProperties={"schema": expression("@dataset().SchemaName"), "table": "Agg"},
        ),
    )

    location = result.content.location
    assert location.schema == "@dataset().SchemaName"
    assert location.table == "Agg"
    # The expression list says exactly which path was dynamic.
    assert any(
        e.location == "properties.typeProperties.schema"
        for e in result.content.expressions
    )


def test_expressions_are_not_evaluated_or_translated(tmp_path):
    original = "@concat(dataset().folder, '/', dataset().file)"
    result = extract(
        tmp_path, dataset_json(typeProperties={"location": {"folderPath": expression(original)}})
    )

    assert result.content.expressions[0].expression == original
    assert result.content.location.folder_path == original


# --- 5, 8: location and resources --------------------------------------------


def test_file_location_is_extracted(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            typeProperties={
                "location": {
                    "type": "AzureBlobFSLocation",
                    "fileName": "fares-data.csv",
                    "fileSystem": "public",
                    "folderPath": "raw/2024",
                }
            }
        ),
    )

    location = result.content.location
    assert location.category is LocationCategory.DATA_LAKE
    assert location.kind == "AzureBlobFSLocation"
    assert location.file_name == "fares-data.csv"
    assert location.file_system == "public"
    assert location.folder_path == "raw/2024"


def test_http_location_without_a_url_is_still_recorded(tmp_path):
    """The real repository's HTTP datasets carry only a type; the URL is on
    the linked service."""
    result = extract(
        tmp_path, dataset_json(typeProperties={"location": {"type": "HttpServerLocation"}})
    )

    location = result.content.location
    assert location.category is LocationCategory.HTTP
    assert location.kind == "HttpServerLocation"
    assert location.url is None
    assert result.content.resources == ()


def test_sql_table_location(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            "AzureSqlDWTable",
            typeProperties={"table": "TripsData", "schema": "dbo", "database": "pool1"},
        ),
    )

    location = result.content.location
    assert location.category is LocationCategory.SQL_TABLE
    assert location.table == "TripsData"
    assert location.schema == "dbo"
    assert location.database == "pool1"
    assert location.kind is None  # SQL datasets have no location object


def test_url_is_captured_as_an_external_resource(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            typeProperties={
                "location": {
                    "type": "HttpServerLocation",
                    "relativeUrl": "https://example.com/data/trips.csv",
                }
            }
        ),
    )

    resource = result.content.resources[0]
    assert resource.uri == "https://example.com/data/trips.csv"
    assert resource.scheme == "https"
    assert resource.category is ResourceCategory.ENDPOINT
    assert resource.location == "properties.typeProperties.location.relativeUrl"
    # A URL is a resource, never a Synapse artifact reference.
    assert result.content.references == ()


def test_storage_uri_in_a_folder_path_is_a_storage_resource(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            typeProperties={
                "location": {
                    "type": "AzureBlobFSLocation",
                    "folderPath": "abfss://raw@lake.dfs.core.windows.net/trips",
                }
            }
        ),
    )

    resource = result.content.resources[0]
    assert resource.scheme == "abfss"
    assert resource.category is ResourceCategory.STORAGE


# --- 6: declared schema ------------------------------------------------------


def test_declared_schema_is_extracted_in_order(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            schema=[
                {"name": "medallion", "type": "String"},
                {"name": "fare_amount", "type": "Decimal", "precision": 10, "scale": 2},
                {"name": "vendor_id", "type": "String"},
            ]
        ),
    )

    schema = result.content.schema
    assert schema.declared is True
    assert schema.column_count == 3
    assert [c.name for c in schema.columns] == ["medallion", "fare_amount", "vendor_id"]
    assert [c.ordinal for c in schema.columns] == [0, 1, 2]
    assert schema.columns[1].precision == 10
    assert schema.columns[1].scale == 2


def test_absent_schema_is_distinguished_from_an_empty_one(tmp_path):
    absent = extract(tmp_path, dataset_json())
    empty = extract(tmp_path, dataset_json(schema=[]))

    assert absent.content.schema.declared is False
    assert empty.content.schema.declared is True
    assert empty.content.schema.columns == ()


def test_the_legacy_structure_key_is_read_as_schema(tmp_path):
    result = extract(
        tmp_path, dataset_json(structure=[{"name": "col1", "type": "String"}])
    )

    assert result.content.schema.declared is True
    assert result.content.schema.columns[0].name == "col1"


def test_declared_schema_is_not_a_physical_table_schema(tmp_path):
    """A SQL dataset's column list is the author's declaration, nothing more."""
    result = extract(
        tmp_path,
        dataset_json("AzureSqlDWTable", typeProperties={"table": "TripsData"}, schema=[]),
    )

    assert result.content.schema.column_count == 0
    assert result.content.location.table == "TripsData"


# --- 7: type-specific configuration ------------------------------------------


def test_delimited_text_format_settings(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            "DelimitedText",
            typeProperties={
                "location": {"type": "AzureBlobFSLocation"},
                "columnDelimiter": ",",
                "escapeChar": "\\",
                "quoteChar": '"',
                "firstRowAsHeader": True,
                "compressionCodec": "gzip",
            },
        ),
    )

    settings = result.content.format_settings
    assert settings.column_delimiter == ","
    assert settings.escape_char == "\\"
    assert settings.quote_char == '"'
    assert settings.first_row_as_header is True
    assert settings.compression_codec == "gzip"


def test_parquet_shares_the_file_format_handler(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            "Parquet",
            typeProperties={
                "location": {"type": "AzureBlobFSLocation", "fileName": "x.parquet"},
                "compressionCodec": "snappy",
            },
        ),
    )

    assert result.content.recognized is True
    assert result.content.format_settings is None
    assert {s.key: s.value for s in result.content.settings}["compressionCodec"] == "snappy"


def test_raw_type_properties_are_not_copied_wholesale(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            "DelimitedText",
            typeProperties={
                "location": {"type": "AzureBlobFSLocation"},
                "columnDelimiter": ",",
                "someDeepNested": {"a": {"b": {"c": 1}}},
            },
        ),
    )

    payload = json.dumps(result.content.to_dict())
    assert '"b"' not in payload  # the nested blob was not carried over


# --- 10, 11: unknown constructs ----------------------------------------------


def test_unknown_dataset_type_is_preserved_with_a_warning(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            "SomeFutureDatasetType",
            linkedServiceName=LINKED_SERVICE,
            typeProperties={
                "widget": "sprocket",
                "count": 3,
                "location": {"type": "AzureBlobFSLocation", "fileName": "f.bin"},
                "nested": {"a": 1},
            },
            parameters={"p": {"type": "string"}},
        ),
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert result.succeeded
    definition = result.content
    assert definition.name == "DS_Test"
    assert definition.type == "SomeFutureDatasetType"
    assert definition.recognized is False
    # Generic extraction still happened.
    assert definition.linked_service is not None
    assert definition.location.file_name == "f.bin"
    assert definition.parameters
    settings = {s.key: s.value for s in definition.settings}
    assert settings == {"widget": "sprocket", "count": "3"}  # scalars only
    assert any("SomeFutureDatasetType" in w.message for w in result.warnings)


def test_unknown_optional_property_is_warned_about(tmp_path):
    result = extract(tmp_path, dataset_json(someFutureProperty={"a": 1}))

    assert result.status is ExtractionStatus.PARTIAL
    assert any("someFutureProperty" in w.message for w in result.warnings)


def test_a_recognized_dataset_produces_no_warnings(tmp_path):
    result = extract(
        tmp_path,
        dataset_json("DelimitedText", linkedServiceName=LINKED_SERVICE, schema=[]),
    )

    assert result.status is ExtractionStatus.SUCCESS
    assert result.warnings == ()


# --- 12, 13: malformed input -------------------------------------------------


def test_malformed_json_fails(tmp_path):
    result = extract(tmp_path, None, raw="{not json")

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert result.errors[0].code is IssueCode.MALFORMED_ARTIFACT


def test_missing_properties_fails(tmp_path):
    result = extract(tmp_path, {"name": "DS_Test"})

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert "no properties object" in result.errors[0].message


def test_missing_dataset_type_fails(tmp_path):
    result = extract(tmp_path, {"name": "DS", "properties": {"schema": []}})

    assert result.status is ExtractionStatus.FAILED
    assert "no type" in result.errors[0].message


def test_json_root_that_is_not_an_object_fails(tmp_path):
    result = extract(tmp_path, None, raw="[1, 2, 3]")

    assert result.status is ExtractionStatus.FAILED
    assert "not an object" in result.errors[0].message


# --- 14: no name guessing ----------------------------------------------------


def test_ordinary_strings_never_become_references(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            typeProperties={
                "table": "TripFaresSynapseAnalyticsLinkedService",
                "note": "tripsDataSource",
                "location": {"type": "AzureBlobFSLocation", "fileName": "faresDataSink"},
            },
            parameters={"p": {"type": "string", "defaultValue": "azureSynapseAnalyticsTable"}},
        ),
    )

    assert result.content.references == ()


def test_a_reference_shaped_object_missing_its_type_is_not_a_reference(tmp_path):
    result = extract(
        tmp_path, dataset_json(typeProperties={"thing": {"referenceName": "LS_X"}})
    )

    assert result.content.references == ()


def test_a_type_not_ending_in_reference_is_not_a_reference(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            typeProperties={
                "location": {"referenceName": "LS_X", "type": "AzureBlobFSLocation"}
            }
        ),
    )

    assert result.content.references == ()


# --- 15: secrets -------------------------------------------------------------


def test_key_vault_secret_reference_is_recorded_without_a_value(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            typeProperties={
                "password": {
                    "type": "AzureKeyVaultSecret",
                    "store": {"referenceName": "KV_LS", "type": "LinkedServiceReference"},
                    "secretName": "sql-password",
                }
            }
        ),
    )

    secret = result.content.secrets[0]
    assert secret.kind is SecretKind.KEY_VAULT
    assert secret.secret_name == "sql-password"  # a name, not a value
    assert secret.store_name == "KV_LS"
    assert secret.location == "properties.typeProperties.password"


def test_secure_string_value_never_reaches_the_model(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            typeProperties={"password": {"type": "SecureString", "value": FAKE_SECRET}}
        ),
    )
    definition = result.content

    assert definition.secrets[0].kind is SecretKind.SECURE_STRING
    assert FAKE_SECRET not in json.dumps(definition.to_dict())
    assert FAKE_SECRET not in json.dumps(definition.summary())


def test_inline_secret_literal_is_recorded_but_redacted(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            typeProperties={"sasToken": FAKE_SECRET, "accountName": "lakeacct"}
        ),
    )
    definition = result.content

    secret = [s for s in definition.secrets if s.kind is SecretKind.INLINE_LITERAL][0]
    assert secret.property_name == "sasToken"
    assert secret.location == "properties.typeProperties.sasToken"
    # The value appears nowhere in the model or any reporting surface.
    assert FAKE_SECRET not in json.dumps(definition.to_dict())
    assert FAKE_SECRET not in json.dumps(definition.summary())
    assert FAKE_SECRET not in json.dumps([s.to_dict() for s in definition.secrets])
    # Non-secret configuration is untouched.
    assert {s.key: s.value for s in definition.settings}.get("accountName") is None


def test_a_secret_named_parameter_default_is_redacted(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            parameters={"password": {"type": "string", "defaultValue": FAKE_SECRET}}
        ),
    )

    parameter = result.content.parameters[0]
    assert parameter.is_secret is True
    assert parameter.has_default is True  # the shape is kept
    assert parameter.default_value is None  # the value is not
    assert FAKE_SECRET not in json.dumps(result.content.to_dict())


# --- 16, 17, 18: framework integration ---------------------------------------


def test_provenance_is_preserved(tmp_path):
    repository = RepositorySource(
        provider="github",
        repository_url="https://github.com/contoso/synapse-workspace",
        ref="main",
        local_path=tmp_path,
        commit_sha="a" * 40,
    )
    result = extract(
        tmp_path, dataset_json(linkedServiceName=LINKED_SERVICE), repository=repository
    )

    provenance = result.provenance
    assert provenance.source_type is SourceType.REPOSITORY
    assert provenance.repository_url.endswith("synapse-workspace")
    assert provenance.ref == "main"
    assert provenance.commit_sha == "a" * 40
    assert provenance.source_path == "workspace/dataset/DS_Test.json"
    assert provenance.sha256 == "d" * 64
    assert provenance.source_format == "synapse_dataset_json"
    assert result.extractor.name == "dataset"
    assert result.artifact_id == "synapse://dataset/DS_Test"


def test_registry_selects_the_dataset_extractor():
    registry = default_registry()

    selected = registry.require(AssetType.DATASET, SourceType.REPOSITORY)

    assert isinstance(selected, DatasetExtractor)
    assert selected.supported_types == (AssetType.DATASET,)
    # Membership, not an exact tuple: the registry grows with each extractor.
    assert AssetType.DATASET in registry.supported_types()


def test_extraction_runs_through_the_orchestrator(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(dataset_json(linkedServiceName=LINKED_SERVICE)), encoding="utf-8"
    )
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path))

    run = ExtractorOrchestrator(default_registry(), context).run([artifact])

    assert len(run.results) == 1
    assert run.results[0].succeeded
    assert run.results[0].artifact_type is AssetType.DATASET
    assert [r.target_name for r in run.references] == [
        "TripFaresDataLakeStorageLinkedService"
    ]


def test_malformed_dataset_through_the_orchestrator(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text("{oops", encoding="utf-8")
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path))

    run = ExtractorOrchestrator(default_registry(), context).run([artifact])

    assert run.results[0].status is ExtractionStatus.FAILED


# --- 19: determinism ---------------------------------------------------------


def test_extraction_is_deterministic(tmp_path):
    document = dataset_json(
        "DelimitedText",
        linkedServiceName={
            "referenceName": "LS",
            "type": "LinkedServiceReference",
            "parameters": {"z": expression("@dataset().z"), "a": expression("@dataset().a")},
        },
        typeProperties={
            "location": {"type": "AzureBlobFSLocation", "fileName": "f.csv"},
            "columnDelimiter": ",",
        },
        parameters={"zeta": {"type": "string"}, "alpha": {"type": "string"}},
        schema=[{"name": "b"}, {"name": "a"}],
    )

    first = extract(tmp_path, document)
    second = extract(tmp_path, document)

    assert first.content == second.content
    assert first.references == second.references


def test_unordered_collections_are_sorted_and_schema_order_is_kept(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            parameters={"zeta": {"type": "string"}, "alpha": {"type": "string"}},
            annotations=["zulu", "alpha"],
            schema=[{"name": "zeta"}, {"name": "alpha"}],
        ),
    )

    definition = result.content
    assert [p.name for p in definition.parameters] == ["alpha", "zeta"]
    assert definition.annotations == ("alpha", "zulu")
    # Schema order is semantic and must survive.
    assert [c.name for c in definition.schema.columns] == ["zeta", "alpha"]


def test_to_dict_and_summary_are_json_serializable(tmp_path):
    result = extract(
        tmp_path,
        dataset_json(
            linkedServiceName=LINKED_SERVICE,
            typeProperties={"location": {"type": "AzureBlobFSLocation"}, "columnDelimiter": ","},
            schema=[{"name": "a", "type": "String"}],
        ),
    )

    json.dumps(result.content.to_dict())
    summary = result.content.summary()
    json.dumps(summary)
    assert summary["linked_service"] == "TripFaresDataLakeStorageLinkedService"
    assert summary["declared_columns"] == 1
