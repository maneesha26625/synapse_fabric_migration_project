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


def test_health_advertises_the_two_source_methods(server):
    status, body = call(server, "GET", "/api/health")
    assert status == 200
    assert body["capabilities"]["authMethods"] == ["azure_cli", "interactive_browser"]


def test_the_catalog_lists_interactive_browser_without_a_client_id(server):
    _, body = call(server, "GET", "/api/health")
    details = {d["id"]: d for d in body["capabilities"]["authMethodDetails"]}
    browser = details["interactive_browser"]
    assert browser["label"] == "Interactive browser"
    assert "Azure CLI session untouched" in browser["detail"]
    assert "server" in browser["caveat"] and "Client ID" not in browser["caveat"]
    assert "managed_identity" not in details


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


def test_resetting_discovery_returns_it_to_idle(server):
    status, body = call(server, "DELETE", "/api/discovery")
    assert status == 200
    assert body["state"] == "idle" and body["summary"] is None


def test_a_reset_forgets_the_results_but_not_the_connection():
    session = Session()
    job = session._job  # noqa: SLF001 - seeding state without a live workspace
    job.state, job.items, job.finished_at = "completed", [{"id": "x"}], "2026-10-06T12:00:00+00:00"
    session._connection = object()  # noqa: SLF001
    assert session.reset_discovery()["state"] == "idle"
    with pytest.raises(ApiError) as gone:
        session.results({})
    assert gone.value.code == "no_results"
    assert session._connection is not None  # noqa: SLF001


def test_a_running_discovery_cannot_be_reset():
    session = Session()
    session._job.state = "running"  # noqa: SLF001
    with pytest.raises(ApiError) as busy:
        session.reset_discovery()
    assert busy.value.code == "discovery_running"


SP = {**VALID, "method": "service_principal", "tenantId": "8a24d8ed-7a4b-45b3-b56b-d781dd225aa1", "clientId": "11111111-2222-3333-4444-555555555555"}


def test_service_principal_is_not_offered_and_never_echoes_a_secret(server):
    status, body = call(server, "POST", "/api/connections/authenticate", {**SP, "clientSecret": "s3cr3t-value"})
    assert status == 400 and body["error"]["code"] == "invalid_configuration"
    assert "s3cr3t-value" not in json.dumps(body)


@pytest.mark.parametrize("method", ["interactive_browser"])
def test_testing_with_any_method_requires_authenticating_first(server, method):
    status, body = call(server, "POST", "/api/connections/test", {**VALID, "method": method})
    assert status == 409 and body["error"]["code"] == "sign_in_required"


@pytest.mark.parametrize("patch", [{"subscriptionId": "not-a-guid"}, {"subscriptionId": ""}, {"tenantId": "x"}])
def test_sign_in_input_is_validated_before_anything_is_contacted(server, patch):
    status, body = call(server, "POST", "/api/connections/authenticate", {**VALID, **patch})
    assert status == 400
    assert body["error"]["code"] == "invalid_configuration"


BROWSER = {**VALID, "method": "interactive_browser", "tenantId": "8a24d8ed-7a4b-45b3-b56b-d781dd225aa1"}


def test_managed_identity_is_no_longer_offered(server):
    status, body = call(server, "POST", "/api/connections/authenticate", {**VALID, "method": "managed_identity"})
    assert status == 400 and body["error"]["code"] == "invalid_configuration"


def test_interactive_browser_needs_the_tenant_it_opens_against(server):
    status, body = call(server, "POST", "/api/connections/authenticate", {**BROWSER, "tenantId": ""})
    assert status == 400 and "Tenant ID" in body["error"]["message"]


def test_the_sign_in_body_refuses_a_client_id(server):
    status, body = call(server, "POST", "/api/connections/authenticate", {**BROWSER, "clientId": "11111111-2222-3333-4444-555555555555"})
    assert status == 400 and "clientId" in body["error"]["message"]


@pytest.mark.parametrize("field", ["clientSecret", "password", "accessToken", "username", "loginHint", "anything"])
def test_the_sign_in_body_refuses_unknown_and_secret_fields_without_echoing_them(server, field):
    status, body = call(server, "POST", "/api/connections/authenticate", {**BROWSER, field: "s3cr3t-value"})
    assert status == 400 and body["error"]["code"] == "invalid_configuration"
    assert field in body["error"]["message"]
    assert "s3cr3t-value" not in json.dumps(body)


class _FakeAzure:
    """Stands in for AzureConnection so a sign-in completes with no network."""

    providers = []

    def __init__(self, config, credential=None):
        self.config = config
        self.credential = credential
        _FakeAzure.providers.append(credential)

    def validate(self):
        from discovery_agent.connections.validation import ok

        return ok(SourceType.AZURE, "signed in", tenant_id=self.config.tenant_id, subscription_name="Demo")


def test_interactive_browser_reuses_the_held_identity_and_azure_cli_keeps_nothing(monkeypatch):
    from discovery_agent.api import service as service_module

    monkeypatch.setattr(service_module, "AzureConnection", _FakeAzure)
    _FakeAzure.providers = []
    session = Session()
    for _ in range(2):
        assert session.authenticate(dict(BROWSER))["signedIn"] is True
    assert _FakeAzure.providers[0] is _FakeAzure.providers[1]
    assert _FakeAzure.providers[0].remember is True

    _FakeAzure.providers = []
    for _ in range(2):
        session.authenticate({**VALID, "tenantId": BROWSER["tenantId"]})
    assert _FakeAzure.providers[0] is not _FakeAzure.providers[1]
    assert _FakeAzure.providers[0].remember is False


def test_sign_out_forgets_the_record_and_the_held_identity_but_not_the_cli(monkeypatch):
    from discovery_agent.api import service as service_module
    from discovery_agent.connections.azure import auth_record_path

    monkeypatch.setattr(service_module, "AzureConnection", _FakeAzure)
    _FakeAzure.providers = []
    session = Session()
    session.authenticate(dict(BROWSER))
    record = auth_record_path()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{}", encoding="utf-8")

    payload = session.disconnect()

    assert not record.exists()
    assert "Azure CLI session is untouched" in payload["note"]
    session.authenticate(dict(BROWSER))
    assert _FakeAzure.providers[0] is not _FakeAzure.providers[1]  # a fresh identity, so a fresh window


def test_testing_a_workspace_requires_a_sign_in_first(server):
    status, body = call(server, "POST", "/api/connections/test", VALID)
    assert status == 409 and body["error"]["code"] == "sign_in_required"


def test_azure_cli_is_found_in_standard_windows_install_location(monkeypatch, tmp_path):
    from discovery_agent.api import fabric

    cli_path = tmp_path / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin" / "az.cmd"
    cli_path.parent.mkdir(parents=True)
    cli_path.touch()
    monkeypatch.setattr(fabric.shutil, "which", lambda _: None)
    monkeypatch.setattr(fabric.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))

    assert fabric._require("az", "Azure CLI") == str(cli_path)


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

