"""The UI's HTTP API: routing, safety rails, and record mapping.

Offline like the rest of the suite. Connections are never opened: the service
is exercised through its own guard rails, and the mapping is driven with
records built directly.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from discovery_agent.api import mapping
from discovery_agent.api.server import create_server
from discovery_agent.api.service import ApiError, Session
from discovery_agent.discovery_models import (
    ArtifactIdentity,
    DefinitionFacet,
    DependencyFacet,
    UnifiedDiscoveryRecord,
)
from discovery_agent.extractors.models import (
    ArtifactReference,
    Evidence,
    ExtractionProvenance,
    ExtractionStatus,
    ReferenceKind,
    SourceType,
)
from discovery_agent.models import AssetType
from discovery_agent.source_strategy import P0Artifact


@pytest.fixture()
def server():
    httpd = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    payload = json.dumps(body) if body is not None else None
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    conn.request(method, path, body=payload, headers=hdrs)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, json.loads(data) if data else None


VALID = {
    "method": "azure_cli",
    "subscriptionId": "10eb96c3-ba3c-492e-b95b-e9f1d6d85d70",
    "resourceGroup": "rg",
    "workspace": "ws",
}


def test_health_advertises_the_three_source_methods(server):
    status, body = call(server, "GET", "/api/health")
    assert status == 200
    assert body["capabilities"]["authMethods"] == ["azure_cli", "service_principal", "managed_identity"]


def test_starts_disconnected(server):
    _, body = call(server, "GET", "/api/connections")
    assert body["status"] == "disconnected"


def test_discovery_cannot_start_before_a_connection_passes(server):
    status, body = call(server, "POST", "/api/discovery/start")
    assert status == 409
    assert body["error"]["code"] == "not_connected"


def test_results_before_discovery_are_refused_not_empty(server):
    status, body = call(server, "GET", "/api/discovery/results")
    assert status == 409
    assert body["error"]["code"] == "no_results"


SP = {**VALID, "method": "service_principal", "tenantId": "8a24d8ed-7a4b-45b3-b56b-d781dd225aa1", "clientId": "11111111-2222-3333-4444-555555555555"}


def test_service_principal_needs_a_secret_and_never_echoes_one(server):
    status, body = call(server, "POST", "/api/connections/authenticate", SP)
    assert status == 400 and "secret" in body["error"]["message"].lower()
    status, body = call(server, "POST", "/api/connections/authenticate", {**SP, "clientId": "bad", "clientSecret": "s3cr3t-value"})
    assert status == 400
    assert "s3cr3t-value" not in json.dumps(body)


def test_service_principal_client_id_must_be_a_guid(server):
    status, _ = call(server, "POST", "/api/connections/authenticate", {**SP, "clientId": "", "clientSecret": "x"})
    assert status == 400


@pytest.mark.parametrize("method", ["service_principal", "managed_identity"])
def test_testing_with_any_method_requires_authenticating_first(server, method):
    status, body = call(server, "POST", "/api/connections/test", {**VALID, "method": method})
    assert status == 409 and body["error"]["code"] == "sign_in_required"


@pytest.mark.parametrize("patch", [{"subscriptionId": "not-a-guid"}, {"subscriptionId": ""}, {"tenantId": "x"}])
def test_sign_in_input_is_validated_before_anything_is_contacted(server, patch):
    status, body = call(server, "POST", "/api/connections/authenticate", {**VALID, **patch})
    assert status == 400
    assert body["error"]["code"] == "invalid_configuration"


def test_testing_a_workspace_requires_a_sign_in_first(server):
    status, body = call(server, "POST", "/api/connections/test", VALID)
    assert status == 409 and body["error"]["code"] == "sign_in_required"


@pytest.mark.parametrize("kind", ["resource-groups", "workspaces", "sql-pools"])
def test_dropdown_lists_require_a_sign_in(server, kind):
    status, body = call(server, "GET", f"/api/azure/{kind}?resourceGroup=rg&workspace=ws")
    assert status == 409 and body["error"]["code"] == "sign_in_required"

def test_state_changing_requests_must_be_json(server):
    conn = HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request("POST", "/api/connections/test", body="x", headers={"Content-Type": "text/plain"})
    assert conn.getresponse().status == 415


def test_non_loopback_host_is_rejected(server):
    status, body = call(server, "GET", "/api/health", headers={"Host": "evil.example"})
    assert status == 403


def test_unknown_route_is_a_json_404(server):
    status, body = call(server, "GET", "/api/nope")
    assert status == 404 and body["error"]["code"] == "not_found"


def test_disconnect_returns_to_disconnected(server):
    status, body = call(server, "DELETE", "/api/connections")
    assert status == 200 and body["status"] == "disconnected"


def test_results_filter_sort_and_page():
    session = Session()
    job = session._job  # noqa: SLF001 - seeding state without a live workspace
    job.state = "completed"
    job.items = [
        {"id": f"i{n}", "name": name, "type": t, "category": c, "workspace": "w", "status": "Discovered", "dependencyCount": n, "sources": [],
         "fabricTarget": "T", "targetType": "x", "migrationPath": "Direct Target", "automationPotential": "Candidate",
         "assessmentRequired": True, "mappingStatus": "Mapped", "workstream": "Data Factory", "classification": "DIRECT", "wave": 1}
        for n, (name, t, c) in enumerate(
            [("b", "Pipeline", "Integration"), ("a", "Pipeline", "Integration"), ("c", "Table", "SQL")]
        )
    ]
    page = session.results({"type": "Pipeline", "sort": "name", "dir": "asc", "pageSize": "1"})
    assert page["total"] == 2
    assert [i["name"] for i in page["items"]] == ["a"]
    assert page["facets"]["categories"] == {"Integration": 2, "SQL": 1}
    assert session.results({"search": "TABLE"})["total"] == 1
    with pytest.raises(ApiError):
        session.result("missing")


# -- mapping --------------------------------------------------------------------


def _record(with_ref=True, status=ExtractionStatus.SUCCESS):
    prov = ExtractionProvenance(source_type=SourceType.SYNAPSE, source_format="json")
    refs = ()
    if with_ref:
        refs = (
            ArtifactReference(
                source_artifact_id="synapse://pipeline/P",
                source_artifact_type=AssetType.PIPELINE,
                kind=ReferenceKind.ARTIFACT,
                target_name="DS",
                location="activities[0]",
                evidence=Evidence(source_file="p.json", line=None, extractor="x"),
                target_type=AssetType.DATASET,
            ),
        )
    return UnifiedDiscoveryRecord(
        identity=ArtifactIdentity(P0Artifact.PIPELINE, "P"),
        definition=DefinitionFacet(source=SourceType.SYNAPSE, provenance=prov, status=status, content=object()),
        dependencies=DependencyFacet(source=SourceType.SYNAPSE, provenance=prov, references=refs) if refs else None,
    )


def test_mapping_reports_what_exists_and_nothing_about_migration():
    item = mapping.list_item(_record(), "ws", {})
    assert item["type"] == "Pipeline" and item["category"] == "Integration"
    assert item["status"] == "Discovered" and item["dependencyCount"] == 1
    forbidden = {"score", "difficulty", "compatibility", "fabric", "migratable"}
    assert not forbidden & {k.lower() for k in item}
    # ...but it does say where the object appears to land in Fabric, preliminarily.
    assert item["fabricTarget"] == "Fabric Data Factory Pipeline"
    assert item["assessmentRequired"] is True


def test_partial_definition_is_reported_as_partial():
    assert mapping.list_item(_record(status=ExtractionStatus.PARTIAL), "ws", {})["status"] == "Partial"


def test_dependency_joins_only_onto_objects_in_the_run():
    record = _record()
    joined = mapping.detail(record, "ws", {"synapse://dataset/DS": "synapse://dataset/DS"})
    assert joined["dependencies"][0]["objectId"] == "synapse://dataset/DS"
    unjoined = mapping.detail(record, "ws", {})
    assert unjoined["dependencies"][0]["objectId"] is None


def test_detail_never_carries_credential_shaped_text():
    secret = "eyJhbGciOi.eyJzdWIiOiJ4In0.c2ln"
    cleaned = mapping.sanitize({"note": f"Authorization: Bearer {secret}", "n": [f"password={secret}"]})
    assert secret not in json.dumps(cleaned)

