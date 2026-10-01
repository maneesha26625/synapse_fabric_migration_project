"""Tests for live Synapse artifact discovery, without a workspace.

Every test here drives the real ``SynapseArtifactSource`` through a fake
client returning deterministic responses. No network, no subscription, no
sign-in -- the same discipline the SQL catalog tests follow against a fake
connector.

What is deliberately not tested here: that the routes are the ones a real
workspace serves. Only a real workspace can answer that, and the live
validation is where it is asked.
"""

from __future__ import annotations

import json

import pytest

from discovery_agent.errors import MalformedArtifactError, SynapseConnectionError
from discovery_agent.extractors import (
    ExtractionContext,
    ExtractorOrchestrator,
    default_registry,
)
from discovery_agent.extractors.models import (
    ExtractionStatus,
    IssueCode,
    SourceType,
)
from discovery_agent.models import AssetType
from discovery_agent.source_strategy import P0Artifact
from discovery_agent.synapse import api
from discovery_agent.synapse.api import (
    ARTIFACT_ENDPOINTS,
    ARTIFACTS_API_VERSION,
    NO_ARTIFACTS_API,
    ArtifactEndpoint,
    endpoint_for,
    is_registered,
)
from discovery_agent.synapse.client import ArtifactsClient, ArtifactsResponse
from discovery_agent.synapse.models import (
    artifact_from_resource,
    canonical_json,
    content_hash,
)
from discovery_agent.synapse.source import (
    SynapseArtifactSource,
    p0_artifacts_served,
    unavailable_discovery,
)

WORKSPACE = "poc-ws"
ENDPOINT = "https://poc-ws.dev.azuresynapse.net"


# --- a fake data plane -------------------------------------------------------


class FakeClient(ArtifactsClient):
    """Answers canned responses and records every URL it was asked for."""

    def __init__(self, responses=None, endpoint=ENDPOINT):
        self.endpoint = endpoint
        self.responses = responses or {}
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        response = self.responses.get(url)
        if response is None:
            # An unconfigured route is a 404, which is what a workspace that
            # does not serve it actually returns.
            return ArtifactsResponse(404, {"error": {"message": "not found"}})
        if isinstance(response, Exception):
            raise response
        return response


def listing(*resources, next_link=None):
    payload = {"value": list(resources)}
    if next_link:
        payload["nextLink"] = next_link
    return ArtifactsResponse(200, payload)


def resource(name, properties=None, path="pipelines", etag="etag-1"):
    return {
        "id": (
            f"/subscriptions/00000000-0000-0000-0000-000000000000"
            f"/resourceGroups/rg/providers/Microsoft.Synapse/workspaces/"
            f"{WORKSPACE}/{path}/{name}"
        ),
        "name": name,
        "type": f"Microsoft.Synapse/workspaces/{path}",
        "properties": properties if properties is not None else {"activities": []},
        "etag": etag,
    }


def url_for(path):
    return f"{ENDPOINT}/{path}?api-version={ARTIFACTS_API_VERSION}"


def source(responses=None, endpoint=ENDPOINT):
    return SynapseArtifactSource(
        FakeClient(responses or {}, endpoint=endpoint), workspace=WORKSPACE
    )


PIPELINE_PROPERTIES = {
    "activities": [
        {
            "name": "Copy",
            "type": "Copy",
            "inputs": [{"referenceName": "DS_In", "type": "DatasetReference"}],
            "outputs": [{"referenceName": "DS_Out", "type": "DatasetReference"}],
        }
    ],
    "folder": {"name": "ingest"},
    "description": "moves rows",
}


# --- 1: the endpoint registry ------------------------------------------------


def test_the_registry_covers_every_p0_artifact_the_api_serves():
    served = set(p0_artifacts_served())

    assert served == {
        P0Artifact.PIPELINE,
        P0Artifact.DATASET,
        P0Artifact.LINKED_SERVICE,
        P0Artifact.NOTEBOOK,
        P0Artifact.SQL_SCRIPT,
        P0Artifact.SPARK_JOB_DEFINITION,
    }


def test_the_three_sql_artifacts_are_recorded_as_having_no_api():
    """Absent for a reason, and the reason is stated rather than implied."""
    assert set(NO_ARTIFACTS_API) == {
        P0Artifact.DEDICATED_SQL_TABLE,
        P0Artifact.SQL_VIEW,
        P0Artifact.STORED_PROCEDURE,
    }
    for artifact, reason in NO_ARTIFACTS_API.items():
        assert "SQL catalog" in reason
        with pytest.raises(KeyError, match="no Synapse Artifacts endpoint"):
            endpoint_for(artifact)


@pytest.mark.parametrize(
    "artifact, path",
    [
        (P0Artifact.PIPELINE, "pipelines"),
        (P0Artifact.DATASET, "datasets"),
        # Lowercase, as the service documents it. Normalising it here would
        # be inventing an endpoint.
        (P0Artifact.LINKED_SERVICE, "linkedservices"),
        (P0Artifact.NOTEBOOK, "notebooks"),
        (P0Artifact.SQL_SCRIPT, "sqlScripts"),
        (P0Artifact.SPARK_JOB_DEFINITION, "sparkJobDefinitions"),
    ],
)
def test_each_route_is_pinned_exactly_as_the_service_spells_it(artifact, path):
    endpoint = endpoint_for(artifact)

    assert endpoint.path == path
    assert endpoint.url(ENDPOINT) == f"{ENDPOINT}/{path}?api-version=2020-12-01"


def test_the_api_version_is_pinned():
    assert ARTIFACTS_API_VERSION == "2020-12-01"


def test_a_lookalike_endpoint_is_refused():
    """Identity, not equality: a caller-built route is a caller-supplied route."""
    forged = ArtifactEndpoint(P0Artifact.PIPELINE, "pipelines", "synapse_pipeline_json")

    assert not is_registered(forged)
    with pytest.raises(SynapseConnectionError, match="not a registered artifact"):
        source().list_endpoint(forged)


def test_an_endpoint_cannot_be_an_absolute_or_query_bearing_path():
    with pytest.raises(ValueError):
        ArtifactEndpoint(P0Artifact.PIPELINE, "/pipelines", "x")
    with pytest.raises(ValueError):
        ArtifactEndpoint(P0Artifact.PIPELINE, "pipelines?api-version=2015-01-01", "x")


# --- 2: listing --------------------------------------------------------------


def test_a_listing_returns_every_artifact_with_its_identity():
    pool = source({url_for("pipelines"): listing(resource("PL_Load", PIPELINE_PROPERTIES))})

    artifacts, outcome = pool.list_endpoint(api.PIPELINES)

    assert outcome.reachable
    assert outcome.count == 1
    assert artifacts[0].name == "PL_Load"
    assert artifacts[0].artifact is P0Artifact.PIPELINE
    assert artifacts[0].route == "pipelines/PL_Load"
    assert artifacts[0].etag == "etag-1"
    assert artifacts[0].folder == "ingest"
    assert artifacts[0].resource_id.endswith("/pipelines/PL_Load")


def test_an_empty_workspace_is_a_finding_and_an_unreachable_one_is_not():
    """The distinction the whole EndpointOutcome type exists for."""
    empty = source({url_for("pipelines"): listing()})
    artifacts, outcome = empty.list_endpoint(api.PIPELINES)
    assert artifacts == ()
    assert outcome.reachable
    assert outcome.count == 0
    assert outcome.issue is None

    denied = source({url_for("pipelines"): ArtifactsResponse(403, {})})
    artifacts, outcome = denied.list_endpoint(api.PIPELINES)
    assert artifacts == ()
    assert not outcome.reachable
    assert outcome.issue is not None


def test_paging_follows_the_service_supplied_link_and_keeps_every_page():
    second = f"{ENDPOINT}/pipelines?api-version=2020-12-01&continuation=abc"
    pool = source(
        {
            url_for("pipelines"): listing(resource("A"), next_link=second),
            second: listing(resource("B"), resource("C")),
        }
    )

    artifacts, outcome = pool.list_endpoint(api.PIPELINES)

    assert [a.name for a in artifacts] == ["A", "B", "C"]
    assert outcome.pages == 2
    assert outcome.reachable


def test_a_next_link_that_repeats_itself_stops_rather_than_looping():
    first = url_for("pipelines")
    pool = source({first: listing(resource("A"), next_link=first)})

    artifacts, outcome = pool.list_endpoint(api.PIPELINES)

    assert [a.name for a in artifacts] == ["A"]
    assert not outcome.reachable
    assert "already served" in outcome.issue.message


def test_a_permission_failure_says_so_and_claims_no_count():
    pool = source(
        {
            url_for("notebooks"): ArtifactsResponse(
                403, {"error": {"message": "Artifact read access denied"}}
            )
        }
    )

    _, outcome = pool.list_endpoint(api.NOTEBOOKS)

    assert outcome.issue.code is IssueCode.SOURCE_UNAVAILABLE
    assert "not authorized" in outcome.issue.message
    assert "No claim is made about whether any exist" in outcome.issue.message
    assert "Synapse Artifact User" in outcome.issue.message


def test_an_unsupported_route_is_unsupported_and_not_merely_unavailable():
    pool = source({url_for("sqlScripts"): ArtifactsResponse(404, {})})

    _, outcome = pool.list_endpoint(api.SQL_SCRIPTS)

    assert outcome.issue.code is IssueCode.UNSUPPORTED_CONSTRUCT
    assert "not served by this workspace" in outcome.issue.message


def test_a_server_error_is_unavailable():
    pool = source({url_for("datasets"): ArtifactsResponse(503, {})})

    _, outcome = pool.list_endpoint(api.DATASETS)

    assert outcome.issue.code is IssueCode.SOURCE_UNAVAILABLE
    assert "no claim is made about how many exist" in outcome.issue.message


def test_a_transport_failure_is_recorded_rather_than_raised():
    pool = source({url_for("pipelines"): SynapseConnectionError("connection reset")})

    artifacts, outcome = pool.list_endpoint(api.PIPELINES)

    assert artifacts == ()
    assert not outcome.reachable
    assert "connection reset" in outcome.issue.message


def test_a_payload_with_no_value_array_is_malformed_not_empty():
    pool = source({url_for("pipelines"): ArtifactsResponse(200, {"unexpected": True})})

    artifacts, outcome = pool.list_endpoint(api.PIPELINES)

    assert artifacts == ()
    assert outcome.issue.code is IssueCode.MALFORMED_ARTIFACT
    assert "no claim is made about how many" in outcome.issue.message.lower()


def test_an_unnamed_resource_is_counted_and_the_rest_still_reported():
    pool = source(
        {url_for("pipelines"): listing(resource("A"), {"properties": {}}, "not-an-object")}
    )

    artifacts, outcome = pool.list_endpoint(api.PIPELINES)

    assert [a.name for a in artifacts] == ["A"]
    assert outcome.issue.code is IssueCode.MALFORMED_ARTIFACT
    assert "2 pipelines resource(s) could not be identified" in outcome.issue.message


def test_an_artifact_with_no_name_cannot_be_constructed():
    with pytest.raises(ValueError, match="no name"):
        artifact_from_resource(api.PIPELINES, {"properties": {}})


# --- 3: the whole workspace --------------------------------------------------


def full_workspace():
    return source(
        {
            url_for("pipelines"): listing(
                resource("PL_Load", PIPELINE_PROPERTIES, "pipelines")
            ),
            url_for("datasets"): listing(
                resource(
                    "DS_In",
                    {
                        "type": "DelimitedText",
                        "linkedServiceName": {
                            "referenceName": "LS_Lake",
                            "type": "LinkedServiceReference",
                        },
                    },
                    "datasets",
                )
            ),
            url_for("linkedservices"): listing(
                resource(
                    "LS_Lake",
                    {"type": "AzureBlobFS", "typeProperties": {"url": "https://x"}},
                    "linkedservices",
                )
            ),
            url_for("notebooks"): listing(
                resource(
                    "NB_Explore",
                    {
                        "nbformat": 4,
                        "cells": [],
                        "metadata": {"language_info": {"name": "python"}},
                    },
                    "notebooks",
                )
            ),
            url_for("sqlScripts"): listing(
                resource(
                    "SQL_Check",
                    {"type": "SqlQuery", "content": {"query": "SELECT 1"}},
                    "sqlScripts",
                )
            ),
            url_for("sparkJobDefinitions"): listing(
                resource(
                    "SJD_Batch",
                    {
                        "targetBigDataPool": {
                            "referenceName": "sparkpool",
                            "type": "BigDataPoolReference",
                        },
                        "jobProperties": {"file": "abfss://a@b.dfs.core.windows.net/x.jar"},
                    },
                    "sparkJobDefinitions",
                )
            ),
        }
    )


def test_discovering_a_workspace_reads_every_registered_endpoint():
    discovery = full_workspace().discover()

    assert discovery.artifact_count == 6
    assert discovery.counts_by_artifact() == {
        "dataset": 1,
        "linkedService": 1,
        "notebook": 1,
        "pipeline": 1,
        "sparkJobDefinition": 1,
        "sqlscript": 1,
    }
    assert discovery.is_complete
    assert discovery.unreachable == ()


def test_one_endpoint_failing_costs_that_artifact_type_and_nothing_else():
    responses = dict(full_workspace().client.responses)
    responses[url_for("notebooks")] = ArtifactsResponse(403, {})
    discovery = source(responses).discover()

    assert discovery.artifact_count == 5
    assert "notebook" not in discovery.counts_by_artifact()
    assert not discovery.was_reachable(P0Artifact.NOTEBOOK)
    assert discovery.was_reachable(P0Artifact.PIPELINE)
    assert not discovery.is_complete


def test_an_unreachable_endpoint_never_contributes_a_zero():
    """A count of zero means "none exist". An unreachable endpoint has none."""
    responses = dict(full_workspace().client.responses)
    responses[url_for("sqlScripts")] = ArtifactsResponse(500, {})
    discovery = source(responses).discover()

    assert "sqlscript" not in discovery.counts_by_artifact()


def test_a_workspace_that_cannot_be_reached_at_all_marks_every_endpoint():
    discovery = unavailable_discovery(WORKSPACE, ENDPOINT, "the endpoint timed out")

    assert discovery.artifact_count == 0
    assert len(discovery.unreachable) == len(ARTIFACT_ENDPOINTS)
    assert discovery.counts_by_artifact() == {}
    assert not discovery.is_complete


def test_the_workspace_summary_carries_no_definition_content():
    summary = full_workspace().discover().summary()

    assert summary["artifact_count"] == 6
    assert "moves rows" not in json.dumps(summary)


def test_discovery_over_the_same_responses_is_deterministic():
    first = full_workspace().discover()
    second = full_workspace().discover()

    assert first.to_dict() == second.to_dict()


# --- 4: serving content to extractors ---------------------------------------


def test_the_source_serves_the_resource_shape_the_extractors_expect():
    pool = source({url_for("pipelines"): listing(resource("PL_Load", PIPELINE_PROPERTIES))})
    pool.discover()
    detected = pool.detected()[0]

    document = pool.read_json(detected)

    assert document["name"] == "PL_Load"
    assert document["properties"]["activities"][0]["name"] == "Copy"


def test_a_listed_artifact_is_served_without_a_second_request():
    """The list routes return complete definitions; re-reading is a lookup."""
    pool = source({url_for("pipelines"): listing(resource("PL_Load", PIPELINE_PROPERTIES))})
    pool.discover()
    before = len(pool.client.requested)

    pool.read_text(pool.detected()[0])

    assert len(pool.client.requested) == before


def test_an_unlisted_artifact_is_fetched_from_its_own_documented_route():
    pool = source(
        {
            f"{ENDPOINT}/pipelines/PL_Other?api-version={ARTIFACTS_API_VERSION}": (
                ArtifactsResponse(200, resource("PL_Other", PIPELINE_PROPERTIES))
            )
        }
    )
    artifact = artifact_from_resource(api.PIPELINES, resource("PL_Other")).detected()

    document = pool.read_json(artifact)

    assert document["name"] == "PL_Other"


def test_a_route_this_source_does_not_serve_is_refused():
    pool = source()
    artifact = artifact_from_resource(api.PIPELINES, resource("X")).detected()
    forged = type(artifact)(
        **{**artifact.__dict__, "source_path": "secrets/everything"}
    )

    with pytest.raises(MalformedArtifactError, match="not a route this source serves"):
        pool.read_text(forged)


def test_provenance_names_the_workspace_route_and_the_definition_hash():
    pool = source({url_for("pipelines"): listing(resource("PL_Load", PIPELINE_PROPERTIES))})
    pool.discover()
    detected = pool.detected()[0]

    provenance = pool.provenance_for(detected)

    assert provenance.source_type is SourceType.SYNAPSE
    assert provenance.source_path == "pipelines/PL_Load"
    assert provenance.sha256 == content_hash(PIPELINE_PROPERTIES)
    assert provenance.resource_id.endswith("/pipelines/PL_Load")


def test_the_definition_hash_ignores_key_order_and_whitespace():
    """Formatting is not a difference. Array order is."""
    reordered = {
        "description": "moves rows",
        "folder": {"name": "ingest"},
        "activities": PIPELINE_PROPERTIES["activities"],
    }

    assert content_hash(reordered) == content_hash(PIPELINE_PROPERTIES)
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_the_service_etag_does_not_change_the_definition_hash():
    """An artifact republished unchanged must not read as drifted."""
    one = artifact_from_resource(api.PIPELINES, resource("PL", PIPELINE_PROPERTIES, etag="e1"))
    two = artifact_from_resource(api.PIPELINES, resource("PL", PIPELINE_PROPERTIES, etag="e2"))

    assert one.definition_hash == two.definition_hash


def test_a_live_artifact_is_detected_as_the_service_classified_it():
    artifact = artifact_from_resource(api.NOTEBOOKS, resource("NB", {"cells": []}, "notebooks"))

    detected = artifact.detected()

    assert detected.artifact_type == AssetType.NOTEBOOK.value
    assert detected.category.value == "synapse"
    # The service answered on a typed route; that is stronger than any
    # repository heuristic, which has to infer the type.
    assert detected.confidence == 1.0
    assert [s.value for s in detected.evidence.signals] == ["notebooks", "properties"]


# --- 5: the same extractors read both sources -------------------------------


def test_the_shared_extractors_run_against_the_live_source_unchanged():
    pool = full_workspace()
    discovery = pool.discover()

    run = ExtractorOrchestrator(
        default_registry(), ExtractionContext(source=pool)
    ).run(tuple(a.detected() for a in discovery.artifacts))

    assert len(run.results) == 6
    assert run.counts_by_status() == {"success": 6}
    assert set(run.counts_by_type()) == {
        "pipeline",
        "dataset",
        "linkedService",
        "notebook",
        "sqlscript",
        "sparkJobDefinition",
    }


def test_a_live_pipeline_produces_the_same_references_a_committed_one_would():
    pool = full_workspace()
    discovery = pool.discover()
    run = ExtractorOrchestrator(
        default_registry(), ExtractionContext(source=pool)
    ).run(tuple(a.detected() for a in discovery.artifacts))

    pipeline = next(r for r in run.results if r.artifact_name == "PL_Load")

    assert pipeline.status is ExtractionStatus.SUCCESS
    assert {r.target_name for r in pipeline.references} == {"DS_In", "DS_Out"}
    assert all(not r.resolved for r in pipeline.references)


def test_every_live_result_carries_synapse_provenance():
    pool = full_workspace()
    discovery = pool.discover()
    run = ExtractorOrchestrator(
        default_registry(), ExtractionContext(source=pool)
    ).run(tuple(a.detected() for a in discovery.artifacts))

    assert {r.provenance.source_type for r in run.results} == {SourceType.SYNAPSE}
    assert all(r.provenance.resource_id for r in run.results)
    assert all(r.provenance.sha256 for r in run.results)
