"""Tests for the Unified Discovery Record.

Model-level tests only: nothing here connects to a source, resolves a
reference, or runs an extractor beyond reusing its output shape.
"""

from __future__ import annotations

import json

import pytest

from discovery_agent.discovery_models import (
    ArtifactIdentity,
    AuthenticationMetadata,
    AuthenticationType,
    AzureResource,
    DefinitionFacet,
    DependencyFacet,
    DiscoveryRecordSet,
    DriftStatus,
    FacetKind,
    InfrastructureFacet,
    InfrastructureRole,
    RecordIssue,
    RuntimeFacet,
    SecurityFacet,
    SourceKey,
    SourceKeyKind,
    UnifiedDiscoveryRecord,
    missing_facet,
    source_unavailable,
)
from discovery_agent.extractors.common_models import (
    ConfigEntry,
    SecretKind,
    SecretReference,
)
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionProvenance,
    ExtractionResult,
    ExtractionStatus,
    ExtractorInfo,
    IssueCode,
    ReferenceKind,
    SourceType,
)
from discovery_agent.models import AssetType, Evidence
from discovery_agent.source_strategy import P0Artifact

FAKE_SECRET = "not-a-real-secret-value-00000"


# --- fixtures ----------------------------------------------------------------


def repo_provenance(path="workspace/pipeline/PL_Load.json"):
    return ExtractionProvenance(
        source_type=SourceType.REPOSITORY,
        source_format="synapse_pipeline_json",
        source_path=path,
        sha256="a" * 64,
        repository_url="https://github.com/contoso/synapse-workspace",
        ref="main",
        commit_sha="b" * 40,
    )


def synapse_provenance(name="PL_Load"):
    return ExtractionProvenance(
        source_type=SourceType.SYNAPSE,
        source_format="synapse_api_json",
        resource_id=f"/workspaces/poc/pipelines/{name}",
    )


def sql_provenance(obj="poolone.dbo.TripsData"):
    return ExtractionProvenance(
        source_type=SourceType.SQL,
        source_format="sql_catalog",
        resource_id=obj,
    )


def azure_provenance(resource_id="/subscriptions/x/pools/sparkpool01"):
    return ExtractionProvenance(
        source_type=SourceType.AZURE,
        source_format="arm_resource",
        resource_id=resource_id,
    )


def pipeline_identity(name="PL_Load", workspace=None):
    return ArtifactIdentity(
        artifact=P0Artifact.PIPELINE,
        name=name,
        workspace=workspace,
        source_keys=(
            SourceKey(
                SourceType.REPOSITORY,
                SourceKeyKind.REPOSITORY_PATH,
                f"workspace/pipeline/{name}.json",
            ),
        ),
    )


def table_identity(name="TripsData", database="poolone", schema="dbo"):
    return ArtifactIdentity(
        artifact=P0Artifact.DEDICATED_SQL_TABLE,
        name=name,
        database=database,
        schema=schema,
        source_keys=(
            SourceKey(
                SourceType.SQL,
                SourceKeyKind.SQL_OBJECT_NAME,
                f"{database}.{schema}.{name}",
            ),
        ),
    )


def definition_facet(content="<PipelineDefinition>", status=ExtractionStatus.SUCCESS, **kw):
    return DefinitionFacet(
        source=SourceType.REPOSITORY,
        provenance=repo_provenance(),
        status=status,
        content=content,
        content_type="PipelineDefinition",
        extractor="pipeline",
        extractor_version="1.0.0",
        **kw,
    )


def dataset_reference(target="DS_Sales", location="properties.activities[0].inputs[0]"):
    return ArtifactReference(
        source_artifact_id="synapse://pipeline/PL_Load",
        source_artifact_type=AssetType.PIPELINE,
        kind=ReferenceKind.ARTIFACT,
        target_type=AssetType.DATASET,
        target_name=target,
        location=location,
        evidence=Evidence("workspace/pipeline/PL_Load.json", None, "pipeline"),
    )


# --- identity ----------------------------------------------------------------


def test_repository_artifact_id_matches_the_extractor_id_scheme():
    """Reference target ids must join onto record ids without translation."""
    identity = pipeline_identity()

    assert identity.logical_id == "synapse://pipeline/PL_Load"
    assert identity.logical_id == dataset_reference().source_artifact_id
    assert identity.asset_type is AssetType.PIPELINE
    assert identity.qualified_name == "PL_Load"


def test_sql_object_gets_its_own_id_scheme():
    identity = table_identity()

    assert identity.logical_id == "sql://poolone/dbo/TripsData"
    assert identity.asset_type is None  # not a repository artifact
    assert identity.qualified_name == "poolone.dbo.TripsData"


def test_sql_object_without_a_schema_defaults_to_dbo():
    identity = ArtifactIdentity(
        artifact=P0Artifact.SQL_VIEW, name="vTrips", database="poolone"
    )

    assert identity.logical_id == "sql://poolone/dbo/vTrips"


def test_scoped_id_qualifies_by_workspace_without_changing_logical_id():
    identity = pipeline_identity(workspace="poc-ws")

    assert identity.logical_id == "synapse://pipeline/PL_Load"
    assert identity.scoped_id == "synapse://poc-ws/pipeline/PL_Load"


def test_identity_carries_a_natural_key_per_source():
    """The raw material a future reconciler matches on."""
    identity = pipeline_identity().with_key(
        SourceKey(SourceType.SYNAPSE, SourceKeyKind.SYNAPSE_ARTIFACT_NAME, "PL_Load")
    )

    assert len(identity.source_keys) == 2
    assert identity.key_for(SourceType.SYNAPSE).key == "PL_Load"
    assert identity.key_for(SourceType.REPOSITORY).kind is SourceKeyKind.REPOSITORY_PATH
    assert identity.key_for(SourceType.AZURE) is None


def test_adding_the_same_key_twice_is_a_no_op():
    identity = pipeline_identity()
    key = identity.source_keys[0]

    assert identity.with_key(key) is identity


def test_an_identity_needs_a_name():
    with pytest.raises(ValueError, match="needs a name"):
        ArtifactIdentity(artifact=P0Artifact.PIPELINE, name="")


# --- 1: repository-only artifact ---------------------------------------------


def test_repository_only_record_is_complete_not_broken():
    record = UnifiedDiscoveryRecord(
        identity=pipeline_identity(), definition=definition_facet()
    )

    assert record.facet_kinds == (FacetKind.DEFINITION,)
    assert record.sources == (SourceType.REPOSITORY,)
    assert record.is_definition_only
    assert record.has_usable_definition
    assert record.runtime is None
    assert record.infrastructure is None


def test_a_record_can_be_built_from_an_extraction_result():
    result = ExtractionResult(
        artifact_id="synapse://pipeline/PL_Load",
        artifact_type=AssetType.PIPELINE,
        artifact_name="PL_Load",
        status=ExtractionStatus.SUCCESS,
        provenance=repo_provenance(),
        extractor=ExtractorInfo("pipeline", "1.0.0"),
        content="<PipelineDefinition>",
        references=(dataset_reference(),),
    )

    record = UnifiedDiscoveryRecord.from_extraction_result(result, workspace="poc-ws")

    assert record.logical_id == "synapse://pipeline/PL_Load"
    assert record.identity.workspace == "poc-ws"
    assert record.definition.extractor == "pipeline"
    assert record.definition.content_type == "str"
    assert record.dependencies is not None
    assert record.identity.key_for(SourceType.REPOSITORY).key.endswith("PL_Load.json")


def test_a_non_p0_artifact_produces_no_record():
    """A trigger is a real artifact but out of P0 scope; inventing a record
    for it would misrepresent what was discovered."""
    result = ExtractionResult(
        artifact_id="synapse://trigger/TR_Daily",
        artifact_type=AssetType.TRIGGER,
        artifact_name="TR_Daily",
        status=ExtractionStatus.SUCCESS,
        provenance=repo_provenance(),
        extractor=ExtractorInfo("trigger", "1.0.0"),
        content="<TriggerDefinition>",
    )

    assert UnifiedDiscoveryRecord.from_extraction_result(result) is None


def test_a_skipped_extraction_still_produces_a_record_that_says_why():
    """The artifact exists and was found; only the definition is missing."""
    result = ExtractionResult(
        artifact_id="synapse://linkedService/LS_Lake",
        artifact_type=AssetType.LINKED_SERVICE,
        artifact_name="LS_Lake",
        status=ExtractionStatus.SKIPPED,
        provenance=repo_provenance("workspace/linkedService/LS_Lake.json"),
        extractor=ExtractorInfo("framework", "0.1.0"),
        errors=(
            ExtractionIssue(IssueCode.NO_EXTRACTOR, "no extractor registered"),
        ),
    )

    record = UnifiedDiscoveryRecord.from_extraction_result(result)

    assert record is not None
    assert record.has_usable_definition is False
    assert record.content is None
    assert record.issues_with_code(IssueCode.NO_EXTRACTOR)


# --- 2: SQL-only artifact ----------------------------------------------------


def test_sql_only_record():
    record = UnifiedDiscoveryRecord(
        identity=table_identity(),
        definition=DefinitionFacet(
            source=SourceType.SQL,
            provenance=sql_provenance(),
            content="<TableDefinition>",
            content_type="TableDefinition",
            extractor="sql-table",
            extractor_version="1.0.0",
        ),
        runtime=RuntimeFacet(
            source=SourceType.SQL,
            provenance=sql_provenance(),
            observations=(ConfigEntry("row_count", "1048576"),),
        ),
    )

    assert record.artifact is P0Artifact.DEDICATED_SQL_TABLE
    assert record.sources == (SourceType.SQL,)
    assert record.logical_id.startswith("sql://")
    assert record.identity.asset_type is None
    # For a table, runtime facts come from SQL itself, not from Synapse.
    assert record.runtime.source is SourceType.SQL


# --- 3, 5: several sources, provenance per facet -----------------------------


def build_fully_enriched_record():
    return UnifiedDiscoveryRecord(
        identity=pipeline_identity(workspace="poc-ws").with_key(
            SourceKey(SourceType.SYNAPSE, SourceKeyKind.SYNAPSE_ARTIFACT_NAME, "PL_Load")
        ),
        definition=definition_facet(),
        runtime=RuntimeFacet(
            source=SourceType.SYNAPSE,
            provenance=synapse_provenance(),
            published=True,
            drift=DriftStatus.DRIFTED,
            drift_paths=("properties.activities[0].policy.timeout",),
            observations=(ConfigEntry("last_run_status", "Succeeded"),),
        ),
        infrastructure=InfrastructureFacet(
            source=SourceType.AZURE,
            provenance=azure_provenance(),
            resources=(
                AzureResource(
                    resource_id="/subscriptions/x/integrationRuntimes/AutoResolveIR",
                    resource_type="Microsoft.Synapse/workspaces/integrationRuntimes",
                    name="AutoResolveIR",
                    role=InfrastructureRole.COMPUTE,
                    properties=(ConfigEntry("type", "Managed"),),
                ),
            ),
        ),
        dependencies=DependencyFacet(
            source=SourceType.REPOSITORY,
            provenance=repo_provenance(),
            references=(dataset_reference(),),
        ),
    )


def test_record_with_repository_synapse_and_azure_facets():
    record = build_fully_enriched_record()

    assert set(record.facet_kinds) == {
        FacetKind.DEFINITION,
        FacetKind.RUNTIME,
        FacetKind.INFRASTRUCTURE,
        FacetKind.DEPENDENCY,
    }
    assert record.sources == (
        SourceType.REPOSITORY,
        SourceType.SYNAPSE,
        SourceType.AZURE,
    )
    assert not record.is_definition_only


def test_provenance_is_recorded_per_facet_not_per_record():
    record = build_fully_enriched_record()
    provenance = record.provenance_by_facet()

    assert provenance["definition"]["source_type"] == "repository"
    assert provenance["definition"]["commit_sha"] == "b" * 40
    assert provenance["definition"]["source_path"].endswith("PL_Load.json")
    assert provenance["runtime"]["source_type"] == "synapse"
    assert provenance["runtime"]["resource_id"].endswith("PL_Load")
    assert provenance["infrastructure"]["source_type"] == "azure"
    assert provenance["infrastructure"]["resource_id"].endswith("sparkpool01")


def test_drift_is_carried_on_the_runtime_facet():
    record = build_fully_enriched_record()

    assert record.runtime.drift is DriftStatus.DRIFTED
    assert record.runtime.drift_paths == (
        "properties.activities[0].policy.timeout",
    )
    assert record.runtime.published is True


def test_infrastructure_resources_are_typed_and_queryable():
    record = build_fully_enriched_record()
    compute = record.infrastructure.resources_with_role(InfrastructureRole.COMPUTE)

    assert len(compute) == 1
    assert compute[0].name == "AutoResolveIR"
    assert record.infrastructure.resources_with_role(InfrastructureRole.STORAGE) == ()


# --- 4: missing optional facets ----------------------------------------------


def test_missing_facets_are_stated_rather_than_implied():
    record = UnifiedDiscoveryRecord(
        identity=pipeline_identity(),
        definition=definition_facet(),
        issues=(
            missing_facet(FacetKind.RUNTIME, SourceType.SYNAPSE),
            source_unavailable(
                FacetKind.INFRASTRUCTURE, SourceType.AZURE, "no ARM credentials supplied"
            ),
        ),
    )

    assert record.runtime is None
    assert record.infrastructure is None

    missing = record.issues_with_code(IssueCode.MISSING_INFORMATION)
    assert len(missing) == 1
    assert missing[0].facet is FacetKind.RUNTIME
    assert missing[0].source is SourceType.SYNAPSE

    unavailable = record.issues_with_code(IssueCode.SOURCE_UNAVAILABLE)
    assert len(unavailable) == 1
    assert "no ARM credentials" in unavailable[0].message


def test_an_empty_record_is_still_valid():
    record = UnifiedDiscoveryRecord(identity=table_identity())

    assert record.facets == ()
    assert record.sources == ()
    assert record.content is None
    assert record.references == ()
    assert record.has_usable_definition is False


def test_facet_lookup_by_kind():
    record = build_fully_enriched_record()

    assert record.facet(FacetKind.RUNTIME) is record.runtime
    assert record.facet(FacetKind.SECURITY) is None


# --- 6: issues and partial information ---------------------------------------


def test_facet_issues_are_aggregated_with_their_context():
    record = UnifiedDiscoveryRecord(
        identity=pipeline_identity(),
        definition=definition_facet(
            status=ExtractionStatus.PARTIAL,
            issues=(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    "activity type 'Future' is not modelled",
                    "properties.activities[3]",
                ),
            ),
        ),
        issues=(missing_facet(FacetKind.RUNTIME, SourceType.SYNAPSE),),
    )

    all_issues = record.all_issues
    assert len(all_issues) == 2

    from_facet = record.issues_with_code(IssueCode.UNSUPPORTED_CONSTRUCT)[0]
    assert from_facet.facet is FacetKind.DEFINITION
    assert from_facet.source is SourceType.REPOSITORY

    # Partial content is still usable content.
    assert record.has_usable_definition


def test_an_opaque_definition_is_reported_as_present_but_unreadable():
    """An encrypted stored procedure exists; its body cannot be read."""
    record = UnifiedDiscoveryRecord(
        identity=ArtifactIdentity(
            artifact=P0Artifact.STORED_PROCEDURE,
            name="usp_Load",
            database="poolone",
            schema="dbo",
        ),
        definition=DefinitionFacet(
            source=SourceType.SQL,
            provenance=sql_provenance("poolone.dbo.usp_Load"),
            status=ExtractionStatus.PARTIAL,
            content="<StoredProcedureDefinition metadata only>",
            issues=(
                ExtractionIssue(
                    IssueCode.OPAQUE_DEFINITION,
                    "created WITH ENCRYPTION; sys.sql_modules.definition is NULL",
                ),
            ),
        ),
    )

    opaque = record.issues_with_code(IssueCode.OPAQUE_DEFINITION)
    assert len(opaque) == 1
    assert record.definition.content is not None  # present, just not the body


def test_a_failed_definition_may_not_carry_content():
    with pytest.raises(ValueError, match="must not carry content"):
        DefinitionFacet(
            source=SourceType.REPOSITORY,
            provenance=repo_provenance(),
            status=ExtractionStatus.FAILED,
            content="<PipelineDefinition>",
        )


# --- 7: references stay unresolved -------------------------------------------


def test_references_are_carried_unresolved():
    record = build_fully_enriched_record()

    assert len(record.references) == 1
    assert all(not r.resolved for r in record.references)
    assert record.dependencies.target_ids == ("synapse://dataset/DS_Sales",)


def test_a_resolved_reference_is_rejected():
    """Resolution belongs to the graph stage; a record must not pre-empt it."""
    resolved = ArtifactReference(
        source_artifact_id="synapse://pipeline/PL_Load",
        source_artifact_type=AssetType.PIPELINE,
        kind=ReferenceKind.ARTIFACT,
        target_type=AssetType.DATASET,
        target_name="DS_Sales",
        location="properties.activities[0].inputs[0]",
        evidence=Evidence("x.json", None, "pipeline"),
        resolved=True,
    )

    with pytest.raises(ValueError, match="must not contain resolved references"):
        DependencyFacet(
            source=SourceType.REPOSITORY,
            provenance=repo_provenance(),
            references=(resolved,),
        )


def test_a_reference_without_a_synapse_target_has_no_target_id():
    """A storage path is a real reference but never joins onto a record."""
    storage = ArtifactReference(
        source_artifact_id="synapse://notebook/NB",
        source_artifact_type=AssetType.NOTEBOOK,
        kind=ReferenceKind.STORAGE_PATH,
        target_type=None,
        target_name="abfss://raw@lake.dfs.core.windows.net/trips",
        location="cells[3]",
        evidence=Evidence("nb.json", 12, "notebook"),
    )
    facet = DependencyFacet(
        source=SourceType.REPOSITORY,
        provenance=repo_provenance(),
        references=(storage, dataset_reference()),
    )

    assert len(facet.references) == 2
    assert facet.target_ids == ("synapse://dataset/DS_Sales",)


def test_target_ids_join_onto_record_ids():
    """The mechanism the future graph stage will use."""
    pipeline = UnifiedDiscoveryRecord(
        identity=pipeline_identity(),
        definition=definition_facet(),
        dependencies=DependencyFacet(
            source=SourceType.REPOSITORY,
            provenance=repo_provenance(),
            references=(dataset_reference(),),
        ),
    )
    dataset = UnifiedDiscoveryRecord(
        identity=ArtifactIdentity(artifact=P0Artifact.DATASET, name="DS_Sales"),
        definition=definition_facet(content="<DatasetDefinition>"),
    )
    record_set = DiscoveryRecordSet((pipeline, dataset))

    target = pipeline.dependencies.target_ids[0]
    assert record_set.by_id(target) is dataset


# --- 8: security metadata without secret values ------------------------------


def test_security_facet_records_mechanism_not_credentials():
    record = UnifiedDiscoveryRecord(
        identity=ArtifactIdentity(artifact=P0Artifact.LINKED_SERVICE, name="LS_Lake"),
        definition=definition_facet(content="<LinkedServiceDefinition>"),
        security=SecurityFacet(
            source=SourceType.REPOSITORY,
            provenance=repo_provenance("workspace/linkedService/LS_Lake.json"),
            authentication=(
                AuthenticationMetadata(
                    authentication_type=AuthenticationType.MANAGED_IDENTITY,
                    location="properties.typeProperties",
                    identity_name="poc-ws",
                    target="https://lake.dfs.core.windows.net",
                ),
            ),
            secret_references=(
                SecretReference(
                    kind=SecretKind.KEY_VAULT,
                    location="properties.typeProperties.accountKey",
                    secret_name="lake-account-key",
                    store_name="KV_LS",
                ),
            ),
        ),
    )

    payload = json.dumps(record.to_dict())
    assert FAKE_SECRET not in payload
    assert "lake-account-key" in payload  # a name is safe to record
    assert "KV_LS" in payload

    secret = record.security.secret_references[0]
    assert secret.kind is SecretKind.KEY_VAULT
    # The guarantee is structural: there is no field a value could occupy.
    assert not hasattr(secret, "value")
    assert not hasattr(record.security.authentication[0], "value")
    assert not hasattr(record.security.authentication[0], "secret")


def test_authentication_metadata_has_no_value_field():
    fields = AuthenticationMetadata.__dataclass_fields__
    assert set(fields) == {
        "authentication_type",
        "location",
        "identity_name",
        "target",
    }


# --- record set --------------------------------------------------------------


def test_record_set_rejects_two_records_for_the_same_artifact():
    one = UnifiedDiscoveryRecord(identity=pipeline_identity())
    two = UnifiedDiscoveryRecord(identity=pipeline_identity())

    with pytest.raises(ValueError, match="duplicate artifact ids"):
        DiscoveryRecordSet((one, two))


def test_a_workspace_scoped_record_does_not_collide_with_an_unscoped_one():
    """Two sources saw a same-named artifact and identity was not proven, so
    both observations are kept. They need two ids, and the workspace is the
    real fact that separates them."""
    from dataclasses import replace

    unscoped = UnifiedDiscoveryRecord(identity=pipeline_identity())
    scoped = UnifiedDiscoveryRecord(
        identity=replace(pipeline_identity(), workspace="poc-ws")
    )

    record_set = DiscoveryRecordSet((unscoped, scoped))

    assert len(record_set.records) == 2
    assert unscoped.logical_id == scoped.logical_id
    assert unscoped.identity.scoped_id != scoped.identity.scoped_id
    assert record_set.by_id(scoped.identity.scoped_id) is scoped


def test_record_set_summary_and_lookups():
    record_set = DiscoveryRecordSet(
        (
            build_fully_enriched_record(),
            UnifiedDiscoveryRecord(
                identity=table_identity(),
                definition=DefinitionFacet(
                    source=SourceType.SQL,
                    provenance=sql_provenance(),
                    content="<TableDefinition>",
                ),
            ),
        )
    )

    summary = record_set.summary()
    assert summary["record_count"] == 2
    assert summary["by_artifact"] == {"dedicated_sql_table": 1, "pipeline": 1}
    assert summary["by_source"] == {"azure": 1, "repository": 1, "sql": 1, "synapse": 1}
    assert summary["reference_count"] == 1
    assert summary["definition_only"] == 1

    assert record_set.of_artifact(P0Artifact.PIPELINE)[0].artifact is P0Artifact.PIPELINE
    assert record_set.by_id("nope") is None


def test_record_serialization_is_json_safe_and_content_free_in_summary():
    record = build_fully_enriched_record()

    json.dumps(record.to_dict())
    summary = record.summary()
    json.dumps(summary)

    assert summary["facets"] == ["definition", "runtime", "infrastructure", "dependency"]
    assert summary["sources"] == ["repository", "synapse", "azure"]
    assert summary["reference_count"] == 1
    # The definition model itself is never serialized by the record; only the
    # fact that it exists and what type it is. Writers handle the model.
    facet = record.to_dict()["definition"]
    assert "content" not in facet
    assert facet["has_content"] is True
    assert facet["content_type"] == "PipelineDefinition"
    assert "<PipelineDefinition>" not in json.dumps(record.to_dict())
