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


def test_health_advertises_the_azure_sign_in_and_the_source_kinds(server):
    status, body = call(server, "GET", "/api/health")
    assert status == 200
    assert body["capabilities"]["authMethods"] == ["azure_cli"]
    assert body["capabilities"]["sourceKinds"] == ["workspace", "git", "zip"]
    details = {d["id"]: d for d in body["capabilities"]["authMethodDetails"]}
    assert set(details) == {"azure_cli"}
    assert "subscriptions" in details["azure_cli"]["detail"] and "server" in details["azure_cli"]["caveat"]


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


def test_the_retired_interactive_browser_method_is_refused(server):
    status, body = call(server, "POST", "/api/connections/authenticate", {"method": "interactive_browser"})
    assert status == 400 and body["error"]["code"] == "invalid_configuration"


@pytest.mark.parametrize("method", ["azure_cli"])
def test_testing_with_any_method_requires_authenticating_first(server, method):
    status, body = call(server, "POST", "/api/connections/test", {**VALID, "method": method})
    assert status == 409 and body["error"]["code"] == "sign_in_required"


def test_managed_identity_is_no_longer_offered(server):
    status, body = call(server, "POST", "/api/connections/authenticate", {"method": "managed_identity"})
    assert status == 400 and body["error"]["code"] == "invalid_configuration"


@pytest.mark.parametrize("field", ["subscriptionId", "tenantId", "clientId", "clientSecret", "password", "accessToken",
                                   "username", "loginHint", "anything"])
def test_the_sign_in_body_takes_only_the_method_and_never_echoes_a_refused_value(server, field):
    status, body = call(server, "POST", "/api/connections/authenticate", {"method": "azure_cli", field: "s3cr3t-value"})
    assert status == 400 and body["error"]["code"] == "invalid_configuration"
    assert field in body["error"]["message"]
    assert "s3cr3t-value" not in json.dumps(body)


# ---- sign in first, then choose a subscription, from every directory -------------------------

HOME = "436c36aa-85c9-4a5d-8b1e-7ba4285bce82"
GUEST = "b886bb14-1c62-46be-8466-473864afcd1c"
LOCKED = "c886bb14-1c62-46be-8466-473864afcd1d"
SUB_A = {"id": "10eb96c3-ba3c-492e-b95b-e9f1d6d85d70", "name": "Azure subscription 1", "tenantId": GUEST, "state": "Enabled"}
SUB_B = {"id": "20eb96c3-ba3c-492e-b95b-e9f1d6d85d71", "name": "Partner analytics", "tenantId": LOCKED, "state": "Enabled"}
DIRECTORIES = [{"id": GUEST, "name": "Contoso", "domain": "contoso.com"}, {"id": HOME, "name": "PAL", "domain": "pal.tech"},
               {"id": LOCKED, "name": "Partner", "domain": "partner.com"}]


def _jwt(claims):
    import base64

    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{part}.signature"


class _FakeSignIn:
    """One sign-in with no window: the home directory has no subscription, a guest directory has one,
    and a third directory answers only after its own sign-in, as an MFA policy would."""

    built = []

    def __init__(self, fail=None, locked=(LOCKED,)):
        self.fail, self.locked, self.windows = fail, set(locked), []
        _FakeSignIn.built.append(self)

    def sign_in(self, tenant_id=None):
        from discovery_agent.errors import AzureAuthenticationError

        if self.fail:
            raise AzureAuthenticationError(self.fail)
        self.windows.append(tenant_id)
        self.locked.discard(tenant_id)

    def token(self, scope, tenant_id=None):
        from discovery_agent.connections.azure import AccessToken, DirectorySignInRequired

        if tenant_id in self.locked:
            raise DirectorySignInRequired(tenant_id)
        return AccessToken(_jwt({"upn": "maneesha@pal.tech", "tid": tenant_id or HOME}), 9999999999, scope)

    def for_tenant(self, tenant_id):
        from discovery_agent.connections.azure import _DirectoryCredential

        return _DirectoryCredential(self, tenant_id)


def _subscriptions_of(credential):
    credential.token("arm")  # a locked directory refuses here, as the real one would
    return {GUEST: [dict(SUB_A)], LOCKED: [dict(SUB_B)]}.get(credential.tenant_id, [])


class _FakeAzure:
    """Stands in for AzureConnection: lists per subscription, records the directory it was given."""

    built = []

    def __init__(self, config, credential=None):
        self.config, self.credential = config, credential
        _FakeAzure.built.append((config.subscription_id, credential.tenant_id))

    def list_resource_groups(self):
        return ["rg-" + self.config.subscription_id[:2]]


@pytest.fixture()
def signed_in(monkeypatch):
    from discovery_agent.api import service as service_module

    _FakeSignIn.built, _FakeAzure.built = [], []
    monkeypatch.setattr(service_module, "MultiTenantSignIn", lambda: _FakeSignIn())
    monkeypatch.setattr(service_module, "list_tenants", lambda credential: [dict(d) for d in DIRECTORIES])
    monkeypatch.setattr(service_module, "list_subscriptions", _subscriptions_of)
    monkeypatch.setattr(service_module, "AzureConnection", _FakeAzure)
    session = Session()
    return session, session.authenticate({"method": "azure_cli"})


def test_one_sign_in_lists_subscriptions_from_every_directory_not_just_the_home_one(signed_in):
    session, payload = signed_in
    # The home directory (PAL) has none; the guest directory's subscription is found anyway.
    assert payload["ok"] and payload["signedIn"] and payload["account"] == "maneesha@pal.tech"
    assert [(d["name"], d["subscriptions"], d["needsSignIn"]) for d in payload["directories"]] == [
        ("PAL", 0, False), ("Contoso", 1, False), ("Partner", 0, True)]
    assert payload["checks"][0]["message"] == (
        "Signed in as maneesha@pal.tech; 1 subscription in 3 directories; Partner needs its own sign-in")
    items = session.azure_options("subscriptions", {})["items"]
    assert [(s["name"], s["tenantName"]) for s in items] == [("Azure subscription 1", "Contoso")]
    assert _FakeSignIn.built[0].windows == [None]  # one window, for the home directory only


def test_a_subscription_is_read_with_its_own_directorys_tokens(signed_in):
    session, _ = signed_in
    assert session.azure_options("resource-groups", {"subscriptionId": SUB_A["id"]})["items"] == ["rg-10"]
    assert _FakeAzure.built == [(SUB_A["id"], GUEST)]


def test_a_directory_that_needs_its_own_sign_in_is_opened_on_request_and_adds_its_subscriptions(signed_in):
    session, _ = signed_in
    payload = session.authenticate_directory({"tenantId": LOCKED})
    assert payload["ok"] and payload["checks"][0]["message"] == "1 subscription in Partner"
    assert _FakeSignIn.built[0].windows == [None, LOCKED]
    assert [s["name"] for s in session.azure_options("subscriptions", {})["items"]] == ["Azure subscription 1", "Partner analytics"]
    assert next(d for d in payload["directories"] if d["id"] == LOCKED)["needsSignIn"] is False
    with pytest.raises(ApiError, match="Choose a directory from the list"):
        session.authenticate_directory({"tenantId": "00000000-0000-0000-0000-000000000000"})
    with pytest.raises(ApiError, match="Unexpected field"):
        session.authenticate_directory({"tenantId": LOCKED, "password": "x"})


def test_a_subscription_the_account_did_not_list_is_refused(signed_in):
    session, _ = signed_in
    with pytest.raises(ApiError, match="Choose a subscription from the list"):
        session.azure_options("resource-groups", {"subscriptionId": SUB_B["id"]})  # its directory is not signed in yet
    with pytest.raises(ApiError, match="Subscription is required"):
        session.azure_options("workspaces", {"resourceGroup": "rg"})


def test_each_sign_in_is_a_fresh_identity_and_sign_out_forgets_it(signed_in):
    session, _ = signed_in
    session.authenticate({"method": "azure_cli"})
    assert _FakeSignIn.built[0] is not _FakeSignIn.built[1]
    payload = session.disconnect()
    assert payload["signedIn"] is False and "Azure CLI session is untouched" in payload["note"]
    with pytest.raises(ApiError, match="Sign in to Azure first"):
        session.azure_options("subscriptions", {})


def test_a_refused_sign_in_and_an_account_without_any_subscription_say_what_to_do(monkeypatch):
    from discovery_agent.api import service as service_module

    monkeypatch.setattr(service_module, "MultiTenantSignIn", lambda: _FakeSignIn(fail="user cancelled"))
    session = Session()
    refused = session.authenticate({"method": "azure_cli"})
    assert not refused["ok"] and not refused["signedIn"] and refused["error"]["title"] == "Authentication failed"

    monkeypatch.setattr(service_module, "MultiTenantSignIn", lambda: _FakeSignIn(locked=()))
    monkeypatch.setattr(service_module, "list_tenants", lambda credential: [{"id": HOME, "name": "PAL", "domain": ""}])
    monkeypatch.setattr(service_module, "list_subscriptions", lambda credential: [])
    empty = session.authenticate({"method": "azure_cli"})
    assert not empty["ok"] and empty["error"]["title"] == "No subscriptions"
    assert "belongs to 1 directory" in empty["error"]["hint"] and "Reader" in empty["error"]["hint"]


def test_the_subscriptions_helper_lists_usable_ones_by_name_and_reads_the_account():
    from discovery_agent.connections.azure import AccessToken, ArmResponse, list_subscriptions, token_identity

    class Transport:
        def get(self, url, token):
            return ArmResponse(200, {"value": [
                {"subscriptionId": "b", "displayName": "Zeta", "tenantId": "t", "state": "Enabled"},
                {"subscriptionId": "a", "displayName": "alpha", "tenantId": "t", "state": "Warned"},
                {"subscriptionId": "c", "displayName": "Old", "tenantId": "t", "state": "Disabled"},
            ]})

    provider = _FakeSignIn().for_tenant(None)
    assert [s["name"] for s in list_subscriptions(provider, Transport())] == ["alpha", "Zeta"]
    assert token_identity(provider.token("x")) == ("maneesha@pal.tech", HOME)
    assert token_identity(AccessToken("not-a-jwt", 1, "x")) == (None, None)


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


@pytest.mark.parametrize("kind", ["subscriptions", "resource-groups", "workspaces", "sql-pools"])
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



def test_the_multi_directory_sign_in_opens_one_window_and_never_prompts_for_another_directory():
    from types import SimpleNamespace

    from discovery_agent.connections.azure import DirectorySignInRequired, MultiTenantSignIn

    class AuthenticationRequiredError(Exception):
        pass

    class FakeSdk:
        def __init__(self):
            self.windows, self.calls = [], []

        def authenticate(self, scopes, tenant_id=None):
            self.windows.append(tenant_id)

        def get_token(self, scope, tenant_id=None):
            self.calls.append((scope, tenant_id))
            if tenant_id == LOCKED and LOCKED not in self.windows:
                raise AuthenticationRequiredError("interaction required")
            return SimpleNamespace(token=f"t-{tenant_id}", expires_on=9999999999)

    sdk = FakeSdk()
    signer = MultiTenantSignIn(credential=sdk)
    signer.sign_in()
    assert signer.token("arm").value == "t-None" and signer.for_tenant(GUEST).token("arm").value == f"t-{GUEST}"
    signer.for_tenant(GUEST).token("arm")
    assert sdk.calls.count(("arm", GUEST)) == 1  # cached per directory
    with pytest.raises(DirectorySignInRequired) as locked:
        signer.for_tenant(LOCKED).token("arm")
    assert locked.value.tenant_id == LOCKED and sdk.windows == [None]  # no window opened by a token request
    signer.sign_in(LOCKED)
    assert signer.for_tenant(LOCKED).token("arm").value == f"t-{LOCKED}" and sdk.windows == [None, LOCKED]
