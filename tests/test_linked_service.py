"""Tests for LinkedServiceExtractor.

Synthetic linked services in temporary directories. No network, no git, no
dependency on the real repository.

Several tests use FAKE_SECRET, a value that must never appear in any output.
It is deliberately distinctive so a single substring check over the whole
serialized result is a meaningful assertion.
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
    LinkedServiceExtractor,
    ReferenceKind,
    RepositoryArtifactSource,
    SourceType,
    default_registry,
)
from discovery_agent.extractors.common_models import AuthenticationType, SecretKind
from discovery_agent.extractors.linked_service import parse_connection_string
from discovery_agent.extractors.linked_service_models import (
    LinkedServiceCategory,
    ResourceKind,
)
from discovery_agent.models import AssetType
from discovery_agent.source_strategy import P0Artifact

FAKE_SECRET = "NOT-A-REAL-SECRET-zzz999"

KEY_VAULT_SECRET = {
    "type": "AzureKeyVaultSecret",
    "store": {"referenceName": "keyVaultLinkedservice", "type": "LinkedServiceReference"},
    "secretName": "adlsAccessKey",
}
CONNECT_VIA = {
    "referenceName": "AutoResolveIntegrationRuntime",
    "type": "IntegrationRuntimeReference",
}


# --- fixtures ----------------------------------------------------------------


def make_artifact(name="LS_Test", path=None) -> DetectedArtifact:
    return DetectedArtifact(
        artifact_type="linkedService",
        artifact_name=name,
        source_path=path or f"workspace/linkedService/{name}.json",
        source_format="synapse_linked_service_json",
        confidence=1.0,
        discovery_source=DiscoverySource.PATH_AND_STRUCTURE,
        category=ArtifactCategory.SYNAPSE,
        evidence=DetectionEvidence((Signal(SignalType.PATH, "linkedService/"),)),
        sha256="f" * 64,
    )


def linked_service_json(service_type="HttpServer", name="LS_Test", **properties):
    document = {"name": name, "properties": {"type": service_type}}
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
    return LinkedServiceExtractor().extract(artifact, context)


def serialized(result) -> str:
    """Everything a consumer could see: content, result envelope, summary."""
    return json.dumps(
        [
            result.content.to_dict() if result.content else None,
            result.to_dict(),
            result.content.summary() if result.content else None,
        ]
    )


# --- 1: minimal valid linked service -----------------------------------------


def test_minimal_valid_linked_service(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json("AzureKeyVault", name="LS_Vault", typeProperties={}),
    )

    assert result.status is ExtractionStatus.SUCCESS
    definition = result.content
    assert definition.name == "LS_Vault"
    assert definition.type == "AzureKeyVault"
    assert definition.category is LinkedServiceCategory.KEY_VAULT
    assert definition.recognized is True


def test_name_falls_back_to_the_artifact_name(tmp_path):
    result = extract(tmp_path, {"properties": {"type": "AzureKeyVault"}})

    assert result.content.name == "LS_Test"


def test_annotations_and_description(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "HttpServer",
            typeProperties={"url": "https://example.com"},
            annotations=["zulu", "alpha"],
            description="Fetches the daily extract.",
        ),
    )

    assert result.content.annotations == ("alpha", "zulu")
    assert result.content.description == "Fetches the daily extract."


# --- 2: every type actually present in the repository ------------------------


def test_http_server(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "HttpServer",
            typeProperties={
                "url": "https://raw.githubusercontent.com/org/repo/main/data.csv",
                "enableServerCertificateValidation": True,
                "authenticationType": "Anonymous",
            },
            connectVia=CONNECT_VIA,
        ),
    )

    definition = result.content
    assert definition.category is LinkedServiceCategory.HTTP
    assert definition.connection.endpoint.endswith("data.csv")
    assert definition.authentication[0].authentication_type is AuthenticationType.ANONYMOUS
    assert definition.resources[0].kind is ResourceKind.ENDPOINT
    assert definition.resources[0].is_dynamic is False
    settings = {s.key: s.value for s in definition.settings}
    assert settings["enableServerCertificateValidation"] == "true"


def test_azure_key_vault(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureKeyVault",
            typeProperties={
                "baseUrl": "@{concat('https://',linkedService().keyVaultName,'.vault.azure.net/')}"
            },
            parameters={"keyVaultName": {"type": "string"}},
        ),
    )

    definition = result.content
    assert definition.category is LinkedServiceCategory.KEY_VAULT
    assert definition.resources[0].kind is ResourceKind.KEY_VAULT
    assert definition.resources[0].is_dynamic is True
    # A vault with no credential property runs as the workspace identity.
    assert (
        definition.authentication[0].authentication_type
        is AuthenticationType.MANAGED_IDENTITY
    )
    assert [p.name for p in definition.parameters] == ["keyVaultName"]


def test_azure_blob_fs_with_key_vault_account_key(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureBlobFS",
            typeProperties={
                "url": "@{concat('https://',linkedService().datalakeAccountName,'.dfs.core.windows.net')}",
                "accountKey": KEY_VAULT_SECRET,
            },
            parameters={
                "keyVaultName": {"type": "string"},
                "datalakeAccountName": {"type": "string"},
            },
            connectVia=CONNECT_VIA,
        ),
    )

    definition = result.content
    assert definition.category is LinkedServiceCategory.DATA_LAKE
    assert definition.resources[0].kind is ResourceKind.STORAGE_ACCOUNT
    assert definition.resources[0].is_dynamic is True
    assert (
        definition.authentication[0].authentication_type is AuthenticationType.ACCOUNT_KEY
    )
    assert definition.key_vault_stores == ("keyVaultLinkedservice",)
    assert definition.uses_key_vault


def test_azure_sql_dw_with_connection_string(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureSqlDW",
            typeProperties={
                "connectionString": (
                    "Integrated Security=False;Encrypt=True;Connection Timeout=30;"
                    "Data Source=@{linkedService().SynapseWorkspaceName};"
                    "Initial Catalog=@{linkedService().SQLDedicatedPoolName};"
                    "User ID=@{linkedService().SQLLoginUsername}"
                ),
                "password": {
                    "type": "AzureKeyVaultSecret",
                    "store": {
                        "referenceName": "keyVaultLinkedservice",
                        "type": "LinkedServiceReference",
                    },
                    "secretName": "synapseSqlLoginPassword",
                },
            },
            connectVia=CONNECT_VIA,
        ),
    )

    definition = result.content
    connection = definition.connection
    assert definition.category is LinkedServiceCategory.SQL_DATA_WAREHOUSE
    assert connection.server == "@{linkedService().SynapseWorkspaceName}"
    assert connection.database == "@{linkedService().SQLDedicatedPoolName}"
    assert connection.user_id == "@{linkedService().SQLLoginUsername}"
    settings = {s.key: s.value for s in connection.settings}
    assert settings == {
        "Integrated Security": "False",
        "Encrypt": "True",
        "Connection Timeout": "30",
    }
    assert definition.authentication[0].authentication_type is AuthenticationType.SQL_LOGIN
    assert definition.resources[0].kind is ResourceKind.SQL_SERVER


def test_power_bi_workspace(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "PowerBIWorkspace",
            typeProperties={
                "workspaceID": "9f1c-1234",
                "tenantID": "72f988bf-86f1-41af-91ab-2d7cd011db47",
            },
        ),
    )

    definition = result.content
    assert definition.category is LinkedServiceCategory.BUSINESS_INTELLIGENCE
    assert definition.connection.workspace_id == "9f1c-1234"
    assert definition.connection.tenant_id.startswith("72f988bf")
    assert definition.resources[0].kind is ResourceKind.BI_WORKSPACE


def test_registered_aliases_share_a_handler(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureSqlDatabase",
            typeProperties={"connectionString": "Data Source=srv;Initial Catalog=db"},
        ),
    )

    assert result.content.recognized is True
    assert result.content.category is LinkedServiceCategory.SQL_DATABASE
    assert result.content.connection.server == "srv"


# --- 3: parameters -----------------------------------------------------------


def test_parameters_are_sorted_with_defaults_preserved(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureKeyVault",
            typeProperties={},
            parameters={
                "zeta": {"type": "string", "defaultValue": "z"},
                "alpha": {"type": "string"},
                "nullable": {"type": "string", "defaultValue": None},
            },
        ),
    )

    parameters = {p.name: p for p in result.content.parameters}
    assert [p.name for p in result.content.parameters] == ["alpha", "nullable", "zeta"]
    assert parameters["zeta"].default_value == "z"
    assert parameters["alpha"].has_default is False
    # "no default" stays distinguishable from "default = null".
    assert parameters["nullable"].has_default is True
    assert parameters["nullable"].default_value is None


def test_a_secret_named_parameter_default_is_dropped(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureKeyVault",
            typeProperties={},
            parameters={"password": {"type": "string", "defaultValue": FAKE_SECRET}},
        ),
    )

    parameter = result.content.parameters[0]
    assert parameter.name == "password"
    assert parameter.has_default is True  # the shape is kept
    assert parameter.default_value is None  # the value is not
    assert FAKE_SECRET not in serialized(result)

    secret = [s for s in result.content.secrets if s.property_name == "password"][0]
    assert secret.location == "properties.parameters.password.defaultValue"


def test_a_vault_name_parameter_is_not_treated_as_a_secret(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureKeyVault",
            typeProperties={},
            parameters={"keyVaultName": {"type": "string", "defaultValue": "kv1"}},
        ),
    )

    assert result.content.parameters[0].default_value == "kv1"


# --- 4: expressions ----------------------------------------------------------


def test_expressions_are_preserved_verbatim(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureBlobFS",
            typeProperties={
                "url": "@{concat('https://',linkedService().datalakeAccountName,'.dfs.core.windows.net')}",
                "accountKey": KEY_VAULT_SECRET,
            },
        ),
    )

    expressions = {e.expression for e in result.content.expressions}
    assert (
        "@{concat('https://',linkedService().datalakeAccountName,'.dfs.core.windows.net')}"
        in expressions
    )
    assert result.content.is_parameterized


def test_expression_objects_inside_a_reference_are_captured(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureBlobFS",
            typeProperties={
                "accountKey": {
                    "type": "AzureKeyVaultSecret",
                    "store": {
                        "referenceName": "keyVaultLinkedservice",
                        "type": "LinkedServiceReference",
                        "parameters": {
                            "keyVaultName": {
                                "value": "@linkedService().keyVaultName",
                                "type": "Expression",
                            }
                        },
                    },
                    "secretName": "adlsAccessKey",
                }
            },
        ),
    )

    assert "@linkedService().keyVaultName" in {
        e.expression for e in result.content.expressions
    }


def test_interpolation_inside_a_connection_string_is_captured(tmp_path):
    """The shared scanner only sees strings starting with '@'; a connection
    string never does, so the parser contributes these."""
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureSqlDW",
            typeProperties={
                "connectionString": "Data Source=@{linkedService().Server};Encrypt=True"
            },
        ),
    )

    expressions = result.content.expressions
    assert [e.expression for e in expressions] == ["@{linkedService().Server}"]
    assert expressions[0].location.endswith("connectionString[Data Source]")


def test_expressions_are_not_evaluated_or_translated(tmp_path):
    original = "@{concat('https://', linkedService().account, '.dfs.core.windows.net')}"
    result = extract(
        tmp_path, linked_service_json("AzureBlobFS", typeProperties={"url": original})
    )

    assert result.content.connection.endpoint == original
    assert original in {e.expression for e in result.content.expressions}


# --- 5, 13: references -------------------------------------------------------


def test_integration_runtime_reference(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "HttpServer",
            typeProperties={"url": "https://example.com"},
            connectVia=CONNECT_VIA,
        ),
    )

    definition = result.content
    reference = definition.connect_via
    assert reference is not None
    assert reference.target_name == "AutoResolveIntegrationRuntime"
    assert reference.target_type is AssetType.INTEGRATION_RUNTIME
    assert reference.kind is ReferenceKind.COMPUTE
    assert reference.location == "properties.connectVia"
    assert definition.integration_runtime == "AutoResolveIntegrationRuntime"
    # The convenience view does not remove it from the reference list.
    assert reference in definition.references


def test_key_vault_store_reference(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureBlobFS",
            typeProperties={"url": "https://a.dfs.core.windows.net", "accountKey": KEY_VAULT_SECRET},
            connectVia=CONNECT_VIA,
        ),
    )

    by_name = {r.target_name: r for r in result.content.references}
    assert set(by_name) == {"AutoResolveIntegrationRuntime", "keyVaultLinkedservice"}
    assert by_name["keyVaultLinkedservice"].target_type is AssetType.LINKED_SERVICE


def test_every_reference_stays_unresolved(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureBlobFS",
            typeProperties={"accountKey": KEY_VAULT_SECRET},
            connectVia=CONNECT_VIA,
        ),
    )

    assert result.content.references
    assert all(not r.resolved for r in result.content.references)
    assert all(
        r.source_artifact_id == "synapse://linkedService/LS_Test"
        for r in result.content.references
    )


def test_ordinary_strings_never_become_references(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureKeyVault",
            typeProperties={
                "baseUrl": "https://kv.vault.azure.net",
                "note": "keyVaultLinkedservice",
                "owner": "AutoResolveIntegrationRuntime",
            },
        ),
    )

    assert result.content.references == ()


# --- 6, 7: authentication and Key Vault --------------------------------------


@pytest.mark.parametrize(
    "type_properties,expected",
    [
        ({"accountKey": KEY_VAULT_SECRET}, AuthenticationType.ACCOUNT_KEY),
        ({"sasToken": KEY_VAULT_SECRET}, AuthenticationType.SAS),
        (
            {"servicePrincipalId": "00000000-0000-0000-0000-000000000000"},
            AuthenticationType.SERVICE_PRINCIPAL,
        ),
        ({}, AuthenticationType.MANAGED_IDENTITY),
    ],
)
def test_storage_authentication_is_read_from_which_property_is_present(
    tmp_path, type_properties, expected
):
    document = linked_service_json("AzureBlobFS", typeProperties=type_properties)
    result = extract(tmp_path, document)

    assert result.content.authentication[0].authentication_type is expected


def test_service_principal_id_is_recorded_as_an_identity_not_a_credential(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureBlobFS",
            typeProperties={
                "url": "https://a.dfs.core.windows.net",
                "servicePrincipalId": "11111111-2222-3333-4444-555555555555",
                "servicePrincipalKey": KEY_VAULT_SECRET,
            },
        ),
    )

    authentication = result.content.authentication[0]
    assert authentication.authentication_type is AuthenticationType.SERVICE_PRINCIPAL
    assert authentication.identity_name.startswith("11111111")
    assert FAKE_SECRET not in serialized(result)


def test_key_vault_reference_keeps_names_not_values(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json("AzureBlobFS", typeProperties={"accountKey": KEY_VAULT_SECRET}),
    )

    secret = result.content.secrets[0]
    assert secret.kind is SecretKind.KEY_VAULT
    assert secret.secret_name == "adlsAccessKey"  # a name is safe
    assert secret.store_name == "keyVaultLinkedservice"
    assert secret.location == "properties.typeProperties.accountKey"
    assert not hasattr(secret, "value")


# --- 8, 12: secret-bearing properties ----------------------------------------


def test_an_inline_secret_literal_is_recorded_but_never_carried(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureBlobFS",
            typeProperties={"url": "https://a.dfs.core.windows.net", "accountKey": FAKE_SECRET},
        ),
    )

    secret = [s for s in result.content.secrets if s.property_name == "accountKey"][0]
    assert secret.kind is SecretKind.INLINE_LITERAL
    assert secret.location == "properties.typeProperties.accountKey"
    assert FAKE_SECRET not in serialized(result)


def test_a_secure_string_value_never_reaches_the_model(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "SqlServer",
            typeProperties={
                "connectionString": "Data Source=srv;Initial Catalog=db",
                "password": {"type": "SecureString", "value": FAKE_SECRET},
            },
        ),
    )

    assert result.content.secrets[0].kind is SecretKind.SECURE_STRING
    assert FAKE_SECRET not in serialized(result)


def test_a_password_inside_a_connection_string_is_dropped(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureSqlDatabase",
            typeProperties={
                "connectionString": (
                    f"Data Source=srv;Initial Catalog=db;User ID=admin;Password={FAKE_SECRET}"
                )
            },
        ),
    )
    definition = result.content

    assert FAKE_SECRET not in serialized(result)
    assert definition.connection.server == "srv"
    assert definition.connection.user_id == "admin"
    assert "Password" not in {s.key for s in definition.connection.settings}

    secret = [s for s in definition.secrets if s.property_name == "Password"][0]
    assert secret.kind is SecretKind.INLINE_LITERAL
    assert secret.location.endswith("connectionString[Password]")


def test_the_raw_connection_string_is_never_stored(tmp_path):
    raw = "Data Source=srv;Initial Catalog=db;Encrypt=True"
    result = extract(
        tmp_path,
        linked_service_json("AzureSqlDW", typeProperties={"connectionString": raw}),
    )

    assert raw not in serialized(result)
    assert result.content.connection.server == "srv"


def test_a_connection_string_without_credentials_reports_no_secret(tmp_path):
    """The name-based scanner is conservative; the parser knows better.

    A connection string whose password is externalized to Key Vault contains
    no secret, and reporting one would send Assessment chasing a phantom.
    """
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureSqlDW",
            typeProperties={
                "connectionString": "Data Source=srv;Encrypt=True",
                "password": KEY_VAULT_SECRET,
            },
        ),
    )

    kinds = {s.kind for s in result.content.secrets}
    assert kinds == {SecretKind.KEY_VAULT}
    assert not [
        s for s in result.content.secrets if s.location.endswith("connectionString")
    ]


def test_parse_connection_string_never_returns_a_secret_value():
    connection, expressions, keywords = parse_connection_string(
        f"Server=s;Password={FAKE_SECRET};AccountKey={FAKE_SECRET};Encrypt=True",
        "properties.typeProperties.connectionString",
    )

    assert keywords == ("Password", "AccountKey")  # names only
    assert FAKE_SECRET not in json.dumps(connection.to_dict())
    assert connection.server == "s"


def test_an_unknown_type_never_curates_a_secret_property(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "SomeFutureService",
            typeProperties={"endpoint": "https://x.example.com", "apiKey": FAKE_SECRET},
        ),
    )

    assert FAKE_SECRET not in serialized(result)
    assert "apiKey" not in {s.key for s in result.content.settings}
    assert [s.property_name for s in result.content.secrets] == ["apiKey"]


def test_warnings_and_errors_never_carry_a_secret(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "SomeFutureService",
            typeProperties={"password": FAKE_SECRET},
            someUnknownProperty={"password": FAKE_SECRET},
        ),
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert result.warnings
    assert FAKE_SECRET not in json.dumps([w.to_dict() for w in result.warnings])
    assert FAKE_SECRET not in json.dumps([e.to_dict() for e in result.errors])
    assert FAKE_SECRET not in serialized(result)


# --- 9: unknown type ---------------------------------------------------------


def test_unknown_type_is_preserved_with_a_warning(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "SomeFutureService",
            typeProperties={
                "endpoint": "https://future.example.com",
                "widget": "sprocket",
                "count": 3,
                "nested": {"a": 1},
            },
            connectVia=CONNECT_VIA,
            parameters={"p": {"type": "string"}},
        ),
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert result.succeeded
    definition = result.content
    assert definition.type == "SomeFutureService"
    assert definition.recognized is False
    assert definition.category is LinkedServiceCategory.UNKNOWN
    # Generic extraction still happened.
    assert definition.integration_runtime == "AutoResolveIntegrationRuntime"
    assert definition.parameters
    assert definition.connection.endpoint == "https://future.example.com"
    assert {s.key: s.value for s in definition.settings} == {
        "endpoint": "https://future.example.com",
        "widget": "sprocket",
        "count": "3",
    }
    assert any("SomeFutureService" in w.message for w in result.warnings)


def test_unknown_property_is_warned_about(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureKeyVault", typeProperties={}, someFutureProperty={"a": 1}
        ),
    )

    assert result.status is ExtractionStatus.PARTIAL
    assert any("someFutureProperty" in w.message for w in result.warnings)


def test_a_recognized_service_produces_no_warnings(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "HttpServer",
            typeProperties={"url": "https://example.com", "authenticationType": "Anonymous"},
            connectVia=CONNECT_VIA,
            annotations=[],
        ),
    )

    assert result.status is ExtractionStatus.SUCCESS
    assert result.warnings == ()


# --- 10, 11: malformed input -------------------------------------------------


def test_malformed_json_fails(tmp_path):
    result = extract(tmp_path, None, raw="{not json")

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert result.errors[0].code is IssueCode.MALFORMED_ARTIFACT


def test_missing_properties_fails(tmp_path):
    result = extract(tmp_path, {"name": "LS_Test"})

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert "no properties object" in result.errors[0].message


def test_missing_type_fails(tmp_path):
    result = extract(tmp_path, {"name": "LS", "properties": {"typeProperties": {}}})

    assert result.status is ExtractionStatus.FAILED
    assert "no type" in result.errors[0].message


def test_json_root_that_is_not_an_object_fails(tmp_path):
    result = extract(tmp_path, None, raw="[1, 2, 3]")

    assert result.status is ExtractionStatus.FAILED
    assert "not an object" in result.errors[0].message


def test_empty_type_properties_is_valid_not_malformed(tmp_path):
    result = extract(tmp_path, linked_service_json("AzureKeyVault", typeProperties={}))

    assert result.status is ExtractionStatus.SUCCESS
    assert result.content is not None


# --- framework integration ---------------------------------------------------


def test_provenance_is_preserved(tmp_path):
    repository = RepositorySource(
        provider="github",
        repository_url="https://github.com/contoso/synapse-workspace",
        ref="main",
        local_path=tmp_path,
        commit_sha="c" * 40,
    )
    result = extract(
        tmp_path,
        linked_service_json("AzureKeyVault", typeProperties={}),
        repository=repository,
    )

    provenance = result.provenance
    assert provenance.source_type is SourceType.REPOSITORY
    assert provenance.repository_url.endswith("synapse-workspace")
    assert provenance.ref == "main"
    assert provenance.commit_sha == "c" * 40
    assert provenance.source_path == "workspace/linkedService/LS_Test.json"
    assert provenance.sha256 == "f" * 64
    assert result.extractor.name == "linked_service"
    assert result.artifact_id == "synapse://linkedService/LS_Test"


def test_registry_selects_the_linked_service_extractor():
    registry = default_registry()

    selected = registry.require(AssetType.LINKED_SERVICE, SourceType.REPOSITORY)

    assert isinstance(selected, LinkedServiceExtractor)
    assert selected.supported_types == (AssetType.LINKED_SERVICE,)
    # Both sources serve the same {name, properties} document, so the same
    # extractor is selected for a live workspace as for a clone.
    assert selected.supported_sources == (SourceType.REPOSITORY, SourceType.SYNAPSE)
    assert AssetType.LINKED_SERVICE in registry.supported_types()


def test_extraction_runs_through_the_orchestrator(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            linked_service_json(
                "AzureBlobFS",
                typeProperties={"accountKey": KEY_VAULT_SECRET},
                connectVia=CONNECT_VIA,
            )
        ),
        encoding="utf-8",
    )
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path))

    run = ExtractorOrchestrator(default_registry(), context).run([artifact])

    assert len(run.results) == 1
    assert run.results[0].succeeded
    assert run.results[0].artifact_type is AssetType.LINKED_SERVICE
    assert {r.target_name for r in run.references} == {
        "AutoResolveIntegrationRuntime",
        "keyVaultLinkedservice",
    }


def test_result_becomes_a_unified_discovery_record(tmp_path):
    """Compatibility with the record layer, without building the layer."""
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureKeyVault", typeProperties={"baseUrl": "https://kv.vault.azure.net/"}
        ),
    )

    facet = DefinitionFacet.from_extraction_result(result)
    assert facet.content_type == "LinkedServiceDefinition"
    assert facet.is_usable

    record = UnifiedDiscoveryRecord.from_extraction_result(result)
    assert record is not None
    assert record.artifact is P0Artifact.LINKED_SERVICE
    assert record.logical_id == "synapse://linkedService/LS_Test"
    assert record.has_usable_definition


# --- determinism -------------------------------------------------------------


def test_extraction_is_deterministic(tmp_path):
    document = linked_service_json(
        "AzureBlobFS",
        typeProperties={
            "url": "@{concat('https://',linkedService().account,'.dfs.core.windows.net')}",
            "accountKey": KEY_VAULT_SECRET,
        },
        parameters={"zeta": {"type": "string"}, "alpha": {"type": "string"}},
        connectVia=CONNECT_VIA,
        annotations=["b", "a"],
    )

    first = extract(tmp_path, document)
    second = extract(tmp_path, document)

    assert first.content == second.content
    assert first.references == second.references


def test_unordered_collections_are_sorted(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureBlobFS",
            typeProperties={"accountKey": KEY_VAULT_SECRET},
            parameters={"zeta": {"type": "string"}, "alpha": {"type": "string"}},
            annotations=["zulu", "alpha"],
            connectVia=CONNECT_VIA,
        ),
    )
    definition = result.content

    assert [p.name for p in definition.parameters] == ["alpha", "zeta"]
    assert definition.annotations == ("alpha", "zulu")
    locations = [r.location for r in definition.references]
    assert locations == sorted(locations)


def test_to_dict_and_summary_are_json_serializable(tmp_path):
    result = extract(
        tmp_path,
        linked_service_json(
            "AzureSqlDW",
            typeProperties={
                "connectionString": "Data Source=srv;Initial Catalog=db",
                "password": KEY_VAULT_SECRET,
            },
            connectVia=CONNECT_VIA,
        ),
    )

    json.dumps(result.content.to_dict())
    summary = result.content.summary()
    json.dumps(summary)
    assert summary["integration_runtime"] == "AutoResolveIntegrationRuntime"
    assert summary["key_vault_stores"] == ["keyVaultLinkedservice"]
    assert summary["authentication"] == ["sql_login"]
