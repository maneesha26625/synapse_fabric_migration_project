"""Tests for cross-source record assembly: identity, drift, and provenance.

The questions this file exists to answer:

* Are a pipeline in a clone and a pipeline in a workspace the same pipeline?
  Only if the workspace says the clone is its own repository. Nothing here
  merges on a matching name.
* If they are the same, do they agree? Compared canonically, so formatting is
  never drift, and never claimed at all when either side could not be read.
* Does anything leak? Run over linked services with Key Vault references,
  secure strings and inline literals, checked end to end.
"""

from __future__ import annotations

import json

import pytest

from discovery_agent.artifacts.models import (
    ArtifactCategory,
    ArtifactDetectionResult,
    DetectedArtifact,
    DetectionEvidence,
    DiscoverySource,
)
from discovery_agent.connections.synapse import (
    WorkspaceRepository,
    workspace_repository_from_payload,
)
from discovery_agent.discovery_models import DriftStatus, FacetKind
from discovery_agent.extractors import (
    ExtractionContext,
    ExtractorOrchestrator,
    default_registry,
)
from discovery_agent.extractors.models import (
    ExtractionProvenance,
    IssueCode,
    SourceType,
)
from discovery_agent.extractors.sources import ArtifactSource
from discovery_agent.models import AssetType
from discovery_agent.records import (
    IdentityResolution,
    build_records,
    compare,
    coverage,
    definitions_from_extraction,
    definitions_from_workspace,
    differing_paths,
    records_from_catalog,
    resolve_identity,
)
from discovery_agent.source_strategy import P0Artifact
from discovery_agent.sql.models import (
    CatalogDatabase,
    CatalogObjectType,
    CatalogPrincipal,
    DefinitionState,
    SqlModuleDefinition,
    SqlObjectKey,
    SqlProcedure,
    SqlTable,
    SqlView,
)
from discovery_agent.sql.result import SqlCatalogDiscovery
from discovery_agent.synapse import api
from discovery_agent.synapse.models import (
    SynapseWorkspaceDiscovery,
    artifact_from_resource,
)

WORKSPACE = "poc-ws"
REPOSITORY_URL = "https://github.com/contoso/synapse-workspace"

FAKE_SECRET = "not-a-real-secret-value-00000"

PIPELINE = {
    "activities": [
        {
            "name": "Copy",
            "type": "Copy",
            "inputs": [{"referenceName": "DS_In", "type": "DatasetReference"}],
        }
    ]
}

LINKED_SERVICE = {
    "type": "AzureSqlDW",
    "typeProperties": {
        "connectionString": {
            "type": "AzureKeyVaultSecret",
            "store": {"referenceName": "LS_Vault", "type": "LinkedServiceReference"},
            "secretName": "sql-connection",
        },
        "password": {"type": "SecureString", "value": FAKE_SECRET},
    },
}


# --- fixtures ----------------------------------------------------------------


class DictSource(ArtifactSource):
    """Serves documents from a dict keyed by source path."""

    def __init__(self, documents, source_type=SourceType.REPOSITORY):
        self.documents = documents
        self.source_type = source_type

    def provenance_for(self, artifact):
        return ExtractionProvenance(
            source_type=self.source_type,
            source_format=artifact.source_format,
            source_path=artifact.source_path,
            sha256=artifact.sha256,
            repository_url=REPOSITORY_URL if self.source_type is SourceType.REPOSITORY else None,
            ref="main" if self.source_type is SourceType.REPOSITORY else None,
        )

    def read_text(self, artifact):
        return json.dumps(self.documents[artifact.source_path])


def detected(asset_type, name, path, source_format):
    return DetectedArtifact(
        artifact_type=asset_type.value,
        artifact_name=name,
        source_path=path,
        source_format=source_format,
        confidence=1.0,
        discovery_source=DiscoverySource.PATH_AND_STRUCTURE,
        category=ArtifactCategory.SYNAPSE,
        evidence=DetectionEvidence(),
        sha256="d" * 64,
    )


def repository_side(pipeline=None, linked_service=None):
    """One committed pipeline and one committed linked service."""
    documents = {
        "workspace/pipeline/PL_Load.json": {
            "name": "PL_Load",
            "properties": pipeline if pipeline is not None else PIPELINE,
        },
        "workspace/linkedService/LS_Pool.json": {
            "name": "LS_Pool",
            "properties": linked_service if linked_service is not None else LINKED_SERVICE,
        },
    }
    artifacts = (
        detected(
            AssetType.PIPELINE,
            "PL_Load",
            "workspace/pipeline/PL_Load.json",
            "synapse_pipeline_json",
        ),
        detected(
            AssetType.LINKED_SERVICE,
            "LS_Pool",
            "workspace/linkedService/LS_Pool.json",
            "synapse_linked_service_json",
        ),
    )
    detection = ArtifactDetectionResult(root=".", artifacts=artifacts)
    source = DictSource(documents)
    run = ExtractorOrchestrator(
        default_registry(), ExtractionContext(source=source)
    ).run(artifacts)
    return run, detection, source


def workspace_side(pipeline=None, extra=()):
    """The same two artifacts, as the live workspace reports them."""
    resources = [
        (
            api.PIPELINES,
            {
                "name": "PL_Load",
                "properties": pipeline if pipeline is not None else PIPELINE,
                "etag": "live-1",
            },
        ),
        (
            api.LINKED_SERVICES,
            {"name": "LS_Pool", "properties": LINKED_SERVICE, "etag": "live-2"},
        ),
    ]
    resources.extend(extra)
    artifacts = tuple(artifact_from_resource(e, r) for e, r in resources)
    discovery = SynapseWorkspaceDiscovery(
        workspace=WORKSPACE,
        endpoint=f"https://{WORKSPACE}.dev.azuresynapse.net",
        provenance=ExtractionProvenance(
            source_type=SourceType.SYNAPSE, source_format="synapse_artifact"
        ),
        artifacts=artifacts,
    )
    source = DictSource(
        {a.route: dict(a.payload) for a in artifacts}, SourceType.SYNAPSE
    )
    run = ExtractorOrchestrator(
        default_registry(), ExtractionContext(source=source)
    ).run(tuple(a.detected() for a in artifacts))
    return run, discovery


def assemble(resolution, reason="because", pipeline=None, workspace_pipeline=None):
    repo_run, detection, repo_source = repository_side(pipeline=pipeline)
    live_run, live_discovery = workspace_side(pipeline=workspace_pipeline)
    live = definitions_from_workspace(live_run, live_discovery)
    committed = definitions_from_extraction(
        repo_run, detection=detection, source=repo_source, comparable={d.key for d in live}
    )
    return build_records(
        repository=committed,
        workspace_artifacts=live,
        workspace=WORKSPACE,
        resolution=resolution,
        reason=reason,
    )


# --- 1: identity resolution --------------------------------------------------


def test_a_workspace_git_configuration_reconstructs_its_repository_url():
    repository = workspace_repository_from_payload(
        {
            "workspaceRepositoryConfiguration": {
                "type": "WorkspaceGitHubConfiguration",
                "accountName": "contoso",
                "repositoryName": "synapse-workspace",
                "collaborationBranch": "main",
                "rootFolder": "/workspace",
            }
        }
    )

    assert repository.repository_url == REPOSITORY_URL
    assert repository.is_github
    assert repository.collaboration_branch == "main"


def test_an_azure_devops_configuration_builds_the_git_url_shape_ado_uses():
    repository = WorkspaceRepository(
        kind="WorkspaceVSTSConfiguration",
        account_name="contoso",
        project_name="Data",
        repository_name="synapse",
    )

    assert repository.repository_url == "https://dev.azure.com/contoso/Data/_git/synapse"


def test_a_workspace_with_no_git_configuration_reports_none():
    assert workspace_repository_from_payload({}) is None
    assert workspace_repository_from_payload(
        {"workspaceRepositoryConfiguration": {"type": "WorkspaceGitHubConfiguration"}}
    ) is None


def test_matching_the_same_repository_is_proven():
    repository = WorkspaceRepository(
        kind="WorkspaceGitHubConfiguration",
        account_name="contoso",
        repository_name="synapse-workspace",
    )

    resolution, reason = resolve_identity(repository, REPOSITORY_URL)

    assert resolution is IdentityResolution.PROVEN
    assert resolution.permits_merging
    assert "which is the repository that was scanned" in reason


def test_url_comparison_ignores_the_things_that_do_not_change_a_repository():
    repository = WorkspaceRepository(
        kind="WorkspaceGitHubConfiguration",
        account_name="contoso",
        repository_name="synapse-workspace",
    )

    for url in (
        "https://github.com/contoso/synapse-workspace.git",
        "https://github.com/Contoso/Synapse-Workspace",
        "git@github.com:contoso/synapse-workspace.git",
    ):
        assert repository.matches(url) is True, url


def test_a_different_repository_is_a_positive_answer_and_forbids_merging():
    repository = WorkspaceRepository(
        kind="WorkspaceGitHubConfiguration",
        account_name="contoso",
        repository_name="some-other-repo",
    )

    resolution, reason = resolve_identity(repository, REPOSITORY_URL)

    assert resolution is IdentityResolution.DIFFERENT_REPOSITORY
    assert not resolution.permits_merging
    assert "is not the repository that was scanned" in reason


def test_no_git_configuration_is_undetermined_and_never_rounded_to_a_mismatch():
    """"We could not check" and "we checked and they differ" are opposites."""
    resolution, reason = resolve_identity(None, REPOSITORY_URL)

    assert resolution is IdentityResolution.UNDETERMINED
    assert resolution is not IdentityResolution.DIFFERENT_REPOSITORY
    assert not resolution.permits_merging
    assert "no Git integration" in reason


def test_a_run_with_no_repository_has_nothing_to_identify():
    resolution, _ = resolve_identity(None, None)

    assert resolution is IdentityResolution.NOT_APPLICABLE


# --- 2: merging, or refusing to -----------------------------------------------


def test_a_proven_identity_produces_one_record_carrying_both_sources():
    records = assemble(IdentityResolution.PROVEN)

    pipeline = records.by_id("synapse://poc-ws/pipeline/PL_Load")
    assert pipeline is not None
    assert pipeline.definition.source is SourceType.REPOSITORY
    assert pipeline.runtime.source is SourceType.SYNAPSE
    assert {k.source for k in pipeline.identity.source_keys} == {
        SourceType.REPOSITORY,
        SourceType.SYNAPSE,
    }
    assert len(records.records) == 2


def test_an_unproven_identity_keeps_both_observations_as_two_records():
    records = assemble(IdentityResolution.UNDETERMINED, reason="no git integration")

    pipelines = records.of_artifact(P0Artifact.PIPELINE)
    assert len(pipelines) == 2
    assert {p.definition.source for p in pipelines} == {
        SourceType.REPOSITORY,
        SourceType.SYNAPSE,
    }
    assert len(records.records) == 4


def test_an_unproven_pair_records_the_relationship_it_could_not_establish():
    records = assemble(IdentityResolution.UNDETERMINED, reason="no git integration")

    for record in records.of_artifact(P0Artifact.PIPELINE):
        messages = [i.message for i in record.issues]
        assert any("may be the same artifact" in m for m in messages)
        assert any("no git integration" in m for m in messages)
        assert any("reported separately" in m for m in messages)


def test_unmerged_records_keep_distinct_ids_without_losing_the_logical_one():
    records = assemble(IdentityResolution.DIFFERENT_REPOSITORY)

    pipelines = records.of_artifact(P0Artifact.PIPELINE)
    assert {p.logical_id for p in pipelines} == {"synapse://pipeline/PL_Load"}
    assert len({p.identity.scoped_id for p in pipelines}) == 2


def test_an_artifact_only_in_git_is_reported_as_not_published():
    repo_run, detection, repo_source = repository_side()
    live_run, live_discovery = workspace_side()
    live = tuple(
        d
        for d in definitions_from_workspace(live_run, live_discovery)
        if d.name != "PL_Load"
    )
    committed = definitions_from_extraction(
        repo_run, detection=detection, source=repo_source
    )

    records = build_records(
        repository=committed,
        workspace_artifacts=live,
        workspace=WORKSPACE,
        resolution=IdentityResolution.PROVEN,
        reason="proven",
    )

    pipeline = records.of_artifact(P0Artifact.PIPELINE)[0]
    assert pipeline.runtime.drift is DriftStatus.NOT_PUBLISHED
    assert pipeline.runtime.published is False


def test_an_artifact_only_in_the_workspace_is_reported_as_not_in_source_control():
    repo_run, detection, repo_source = repository_side()
    live_run, live_discovery = workspace_side(
        extra=[
            (
                api.NOTEBOOKS,
                {
                    "name": "NB_Adhoc",
                    "properties": {
                        "nbformat": 4,
                        "cells": [],
                        "metadata": {"language_info": {"name": "python"}},
                    },
                },
            )
        ]
    )
    live = definitions_from_workspace(live_run, live_discovery)
    committed = definitions_from_extraction(
        repo_run, detection=detection, source=repo_source, comparable={d.key for d in live}
    )

    records = build_records(
        repository=committed,
        workspace_artifacts=live,
        workspace=WORKSPACE,
        resolution=IdentityResolution.PROVEN,
        reason="proven",
    )

    notebook = records.of_artifact(P0Artifact.NOTEBOOK)[0]
    assert notebook.runtime.drift is DriftStatus.NOT_IN_SOURCE_CONTROL
    assert notebook.definition.source is SourceType.SYNAPSE


# --- 3: drift ----------------------------------------------------------------


def test_identical_definitions_are_in_sync():
    records = assemble(IdentityResolution.PROVEN)

    pipeline = records.by_id("synapse://poc-ws/pipeline/PL_Load")
    assert pipeline.runtime.drift is DriftStatus.IN_SYNC
    assert pipeline.runtime.drift_paths == ()
    assert pipeline.runtime.issues == ()


def test_formatting_and_key_order_are_never_drift():
    reordered = {
        "activities": [
            {
                "type": "Copy",
                "inputs": [{"type": "DatasetReference", "referenceName": "DS_In"}],
                "name": "Copy",
            }
        ]
    }

    records = assemble(IdentityResolution.PROVEN, workspace_pipeline=reordered)

    assert records.by_id("synapse://poc-ws/pipeline/PL_Load").runtime.drift is (
        DriftStatus.IN_SYNC
    )


def test_a_changed_definition_drifts_and_names_the_property():
    changed = {
        "activities": PIPELINE["activities"],
        "description": "published by hand",
    }

    records = assemble(IdentityResolution.PROVEN, workspace_pipeline=changed)

    runtime = records.by_id("synapse://poc-ws/pipeline/PL_Load").runtime
    assert runtime.drift is DriftStatus.DRIFTED
    assert runtime.drift_paths == ("description",)
    assert runtime.issues[0].code is IssueCode.DRIFT_DETECTED


def test_reordering_activities_is_drift_because_order_is_meaning():
    two = {
        "activities": [
            {"name": "A", "type": "Copy"},
            {"name": "B", "type": "Copy"},
        ]
    }
    reversed_order = {"activities": list(reversed(two["activities"]))}

    records = assemble(
        IdentityResolution.PROVEN, pipeline=two, workspace_pipeline=reversed_order
    )

    assert records.by_id("synapse://poc-ws/pipeline/PL_Load").runtime.drift is (
        DriftStatus.DRIFTED
    )


def test_an_unreadable_definition_is_unknown_and_never_in_sync():
    from discovery_agent.records import SourceDefinition

    repo_run, detection, repo_source = repository_side()
    live_run, live_discovery = workspace_side()
    live = definitions_from_workspace(live_run, live_discovery)
    committed = definitions_from_extraction(repo_run, detection=detection, source=None)

    status, paths, issue = compare(committed[0], live[0])

    assert status is DriftStatus.UNKNOWN
    assert status is not DriftStatus.IN_SYNC
    assert paths == ()
    assert "no claim is made about whether they agree" in issue.message


def test_drift_paths_are_top_level_only_and_never_invented():
    assert differing_paths({"a": 1, "b": 2}, {"a": 1, "b": 3}) == ("b",)
    assert differing_paths({"a": {"deep": 1}}, {"a": {"deep": 2}}) == ("a",)
    assert differing_paths({"a": 1}, {}) == ("a",)
    assert differing_paths("not a mapping", {}) == ("properties",)


def test_both_fingerprints_are_recorded_so_a_verdict_can_be_rechecked():
    records = assemble(IdentityResolution.PROVEN)

    observations = {
        o.key: o.value
        for o in records.by_id("synapse://poc-ws/pipeline/PL_Load").runtime.observations
    }
    assert len(observations["committed_comparison_sha256"]) == 64
    assert len(observations["published_comparison_sha256"]) == 64
    assert "identity_resolution" in observations


# --- the normalisation the comparison rests on -------------------------------


def test_the_api_defaults_git_omits_are_not_drift():
    """Observed live: the API materialises these and the repository does not.

    Every one of them was a false drift verdict against the real workspace
    before normalisation, on artifacts nobody had touched.
    """
    from discovery_agent.synapse.models import normalize_definition

    committed = {
        "activities": [
            {"name": "Copy", "inputs": [{"referenceName": "DS", "type": "DatasetReference"}]}
        ]
    }
    published = {
        "activities": [
            {
                "name": "Copy",
                "inputs": [
                    {"parameters": {}, "referenceName": "DS", "type": "DatasetReference"}
                ],
            }
        ],
        "lastPublishTime": "2026-09-18T09:51:55Z",
        "policy": {"elapsedTimeMetric": {}},
        "typeProperties": {},
        "description": None,
    }

    assert normalize_definition(committed) == normalize_definition(published)
    assert differing_paths(committed, published) == ()


def test_normalisation_never_hides_a_real_difference():
    from discovery_agent.synapse.models import normalize_definition

    one = {"activities": [{"name": "A"}, {"name": "B"}]}
    emptied = {"activities": []}
    renamed = {"activities": [{"name": "A"}, {"name": "C"}]}
    reordered = {"activities": [{"name": "B"}, {"name": "A"}]}

    assert normalize_definition(one) != normalize_definition(emptied)
    assert normalize_definition(one) != normalize_definition(renamed)
    # Array order is meaning, not formatting.
    assert normalize_definition(one) != normalize_definition(reordered)


def test_normalisation_keeps_a_declared_false_or_zero():
    """Falsy is not empty. ``enabled: false`` is a declaration."""
    from discovery_agent.synapse.models import normalize_definition

    assert normalize_definition({"enabled": False}) == {"enabled": False}
    assert normalize_definition({"count": 0}) == {"count": 0}
    assert normalize_definition({"name": ""}) == {"name": ""}


# --- 4: SQL records ----------------------------------------------------------


def catalog():
    key = lambda schema, name, kind: SqlObjectKey(  # noqa: E731
        database="poolone", schema=schema, name=name, object_type=kind, object_id=1
    )
    return SqlCatalogDiscovery(
        database=CatalogDatabase(name="poolone", server="poc-ws.sql.azuresynapse.net"),
        principal=CatalogPrincipal(user_name="discovery_reader"),
        provenance=ExtractionProvenance(
            source_type=SourceType.SQL,
            source_format="sql_catalog",
            resource_id="poc-ws.sql.azuresynapse.net/poolone",
        ),
        tables=(SqlTable(key=key("dbo", "Sales", CatalogObjectType.TABLE)),),
        views=(
            SqlView(
                key=key("dbo", "vSales", CatalogObjectType.VIEW),
                definition=SqlModuleDefinition(
                    DefinitionState.AVAILABLE,
                    "CREATE VIEW dbo.vSales AS SELECT * FROM dbo.Sales",
                ),
                referenced_objects=(),
            ),
        ),
        procedures=(
            SqlProcedure(key=key("dbo", "pLoad", CatalogObjectType.PROCEDURE)),
        ),
    )


def test_every_sql_object_kind_becomes_a_record_with_sql_provenance():
    records = records_from_catalog(catalog(), workspace=WORKSPACE)

    assert len(records) == 3
    assert {r.artifact for r in records} == {
        P0Artifact.DEDICATED_SQL_TABLE,
        P0Artifact.SQL_VIEW,
        P0Artifact.STORED_PROCEDURE,
    }
    assert {r.definition.source for r in records} == {SourceType.SQL}
    for record in records:
        assert record.definition.provenance.resource_id.startswith(
            "poc-ws.sql.azuresynapse.net/poolone/dbo/"
        )


def test_a_sql_record_uses_the_sql_id_scheme_rather_than_the_synapse_one():
    records = records_from_catalog(catalog())

    view = next(r for r in records if r.artifact is P0Artifact.SQL_VIEW)
    assert view.logical_id == "sql://poolone/dbo/vSales"
    assert view.identity.qualified_name == "poolone.dbo.vSales"


def test_an_opaque_module_is_partial_and_still_a_record():
    discovery = catalog()
    opaque = SqlView(
        key=discovery.views[0].key,
        definition=SqlModuleDefinition(DefinitionState.OPAQUE),
        issues=discovery.views[0].issues,
    )
    records = records_from_catalog(
        SqlCatalogDiscovery(
            database=discovery.database,
            principal=discovery.principal,
            provenance=discovery.provenance,
            views=(opaque,),
        )
    )

    view = records[0]
    assert view.definition.status.value == "partial"
    assert view.definition.content is opaque


def test_a_view_body_produces_dependency_observations_but_not_on_itself():
    from discovery_agent.extractors.sql_scanning import scan_objects

    discovery = catalog()
    body = discovery.views[0].definition.text
    view_with_refs = SqlView(
        key=discovery.views[0].key,
        definition=discovery.views[0].definition,
        referenced_objects=scan_objects(body, "poolone.dbo.vSales.definition"),
    )
    records = records_from_catalog(
        SqlCatalogDiscovery(
            database=discovery.database,
            principal=discovery.principal,
            provenance=discovery.provenance,
            views=(view_with_refs,),
        )
    )

    references = records[0].dependencies.references
    targets = {r.target_name for r in references}
    assert "dbo.Sales" in targets
    assert "dbo.vSales" not in targets, "a view does not depend on itself"
    assert all(not r.resolved for r in references)


# --- 5: source isolation and coverage ----------------------------------------


def test_git_only_produces_only_repository_records():
    repo_run, detection, repo_source = repository_side()
    records = build_records(
        repository=definitions_from_extraction(
            repo_run, detection=detection, source=repo_source
        )
    )

    assert {r.definition.source for r in records.records} == {SourceType.REPOSITORY}
    assert all(r.runtime is None for r in records.records)


def test_synapse_only_produces_only_workspace_records():
    live_run, live_discovery = workspace_side()
    records = build_records(
        workspace_artifacts=definitions_from_workspace(live_run, live_discovery),
        workspace=WORKSPACE,
    )

    assert {r.definition.source for r in records.records} == {SourceType.SYNAPSE}


def test_sql_only_produces_only_catalog_records():
    records = build_records(catalog=catalog(), workspace=WORKSPACE)

    assert {r.definition.source for r in records.records} == {SourceType.SQL}


def test_all_three_sources_together_cover_every_p0_artifact_that_exists():
    repo_run, detection, repo_source = repository_side()
    live_run, live_discovery = workspace_side()
    live = definitions_from_workspace(live_run, live_discovery)
    records = build_records(
        repository=definitions_from_extraction(
            repo_run, detection=detection, source=repo_source, comparable={d.key for d in live}
        ),
        workspace_artifacts=live,
        catalog=catalog(),
        workspace=WORKSPACE,
        resolution=IdentityResolution.PROVEN,
        reason="proven",
    )

    found = coverage(records)
    assert found["pipeline"]["records"] == 1
    assert found["pipeline"]["sources"] == {"repository": 1, "synapse": 1}
    assert found["dedicated_sql_table"]["sources"] == {"sql": 1}
    assert found["sql_view"]["records"] == 1
    assert found["stored_procedure"]["records"] == 1


def test_coverage_lists_every_p0_artifact_including_the_ones_not_found():
    """An absent key would read as "not considered". Zero reads as "none"."""
    found = coverage(build_records())

    assert set(found) == {a.value for a in P0Artifact}
    assert found["sparkJobDefinition"] == {
        "records": 0,
        "primary_source": "repository",
        "sources": {},
        "with_drift": 0,
        "unresolved": 0,
    }


def test_coverage_names_the_primary_source_from_the_frozen_strategy():
    found = coverage(build_records())

    assert found["dedicated_sql_table"]["primary_source"] == "sql"
    assert found["sql_view"]["primary_source"] == "sql"
    assert found["stored_procedure"]["primary_source"] == "sql"
    for git_primary in ("sqlscript", "pipeline", "dataset", "linkedService", "notebook"):
        assert found[git_primary]["primary_source"] == "repository"


# --- 6: security -------------------------------------------------------------


def test_a_key_vault_reference_survives_as_a_reference_and_never_as_a_value():
    records = assemble(IdentityResolution.PROVEN)

    linked = records.by_id("synapse://poc-ws/linkedService/LS_Pool")
    security = linked.facet(FacetKind.SECURITY)
    assert security is not None
    kinds = {s.kind.value for s in security.secret_references}
    assert "key_vault" in kinds
    names = {s.secret_name for s in security.secret_references}
    assert "sql-connection" in names
    assert "LS_Vault" in {s.store_name for s in security.secret_references}


@pytest.mark.parametrize("resolution", list(IdentityResolution))
def test_no_secret_value_reaches_a_record_under_any_resolution(resolution):
    records = assemble(resolution)

    rendered = json.dumps(
        [r.to_dict() for r in records.records], default=str
    ) + json.dumps(records.summary(), default=str)
    assert FAKE_SECRET not in rendered


def test_no_secret_value_reaches_the_record_summary_or_coverage():
    records = assemble(IdentityResolution.PROVEN)

    rendered = json.dumps(
        [r.summary() for r in records.records] + [coverage(records)], default=str
    )
    assert FAKE_SECRET not in rendered


def test_authentication_metadata_has_no_field_a_secret_could_occupy():
    records = assemble(IdentityResolution.PROVEN)

    security = records.by_id("synapse://poc-ws/linkedService/LS_Pool").facet(
        FacetKind.SECURITY
    )
    for entry in security.authentication:
        assert not hasattr(entry, "value")
        assert not hasattr(entry, "secret")
    for reference in security.secret_references:
        assert not hasattr(reference, "value")


def test_a_credential_bearing_repository_url_never_reaches_identity_resolution():
    """It cannot: the configuration refuses one at construction."""
    from discovery_agent.connections.models import GitRepositoryConfig
    from discovery_agent.errors import ConfigError

    with pytest.raises(ConfigError) as caught:
        GitRepositoryConfig(
            repository_url=f"https://user:{FAKE_SECRET}@github.com/contoso/x"
        )

    assert FAKE_SECRET not in str(caught.value)


# --- 7: determinism ----------------------------------------------------------


def test_assembling_the_same_sources_twice_produces_identical_records():
    first = assemble(IdentityResolution.PROVEN)
    second = assemble(IdentityResolution.PROVEN)

    assert [r.to_dict() for r in first.records] == [r.to_dict() for r in second.records]


def test_record_order_is_stable_and_grouped_by_artifact():
    records = assemble(IdentityResolution.UNDETERMINED)

    artifacts = [r.artifact.value for r in records.records]
    assert artifacts == sorted(artifacts, key=lambda a: [x.value for x in P0Artifact].index(a))
