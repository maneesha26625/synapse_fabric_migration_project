"""Surviving a restart: what the API saves as it goes, and what it takes back.

Offline. Discovery reads fakes, every store lives in a temporary folder, and no
sign-in window can open: the credential factory is replaced where an identity
is rebuilt.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from http.client import HTTPConnection
from types import MappingProxyType, SimpleNamespace

import pytest

from discovery_agent.api import service as service_module
from discovery_agent.api.fabric import FabricTarget
from discovery_agent.api.server import create_server, restore_state
from discovery_agent.api.service import Session, _Connection, _Job, _SignIn
from discovery_agent.api.state import UNCHANGED, StateStore
from discovery_agent.connections.azure import ArmResponse, AzureCliCredentialProvider
from discovery_agent.connections.manager import ConnectionManager
from discovery_agent.connections.models import AzureConnectionConfig, ConnectionSettings, SynapseConnectionConfig
from discovery_agent.synapse.client import ArtifactsResponse

SUB = "10eb96c3-ba3c-492e-b95b-e9f1d6d85d70"
TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
WS = "11111111-2222-3333-4444-555555555555"
TOKEN = "fake-token-value"


# --- the store ------------------------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    name: str
    payload: object


def test_a_part_comes_back_whole_read_only_mappings_included(tmp_path):
    value = {"shape": Shape("a", MappingProxyType({"k": [1, 2]}))}
    assert StateStore(tmp_path).save("part", lambda: value)
    back = StateStore(tmp_path).load("part")
    assert back == value and isinstance(back["shape"].payload, MappingProxyType)


def test_none_removes_a_part_and_unchanged_leaves_it_alone(tmp_path):
    store = StateStore(tmp_path)
    store.save("part", lambda: {"n": 1})
    store.save("part", lambda: UNCHANGED)
    assert store.load("part") == {"n": 1}
    store.save("part", lambda: None)
    assert store.load("part") is None and not store.path("part").exists()


def test_a_part_saved_before_its_classes_changed_shape_starts_fresh(tmp_path, monkeypatch):
    said = []
    store = StateStore(tmp_path, log=said.append)
    store.save("part", lambda: Shape("a", 1))

    @dataclass(frozen=True)
    class Grown:  # the same class after an update gave it a field
        name: str
        payload: object
        added: int = 0

    monkeypatch.setattr(sys.modules[__name__], "Shape", Grown)
    assert store.load("part") is None
    assert store.path("part").with_name("part.state.unreadable").exists()
    assert "Shape has changed" in said[0] and "starts fresh" in said[0]


def test_a_damaged_file_costs_only_its_own_part(tmp_path):
    store = StateStore(tmp_path, log=lambda message: None)
    store.save("good", lambda: {"n": 1})
    store.path("bad").write_bytes(b"not a pickle")
    assert store.load("bad") is None and store.load("good") == {"n": 1}


def test_a_failed_save_is_reported_never_raised(tmp_path):
    said = []
    store = StateStore(tmp_path, log=said.append)
    assert store.save("part", lambda: threading.Lock()) is False  # a lock cannot be written
    assert "Could not save the part state" in said[0]


# --- the Synapse source ---------------------------------------------------------------


class Artifacts:
    """The Synapse data plane, answering from a dict."""

    endpoint = "https://ws.dev.azuresynapse.net"

    def __init__(self, resources):
        self.resources = resources

    def get(self, url):
        listed = self.resources.get(url.split("?", 1)[0].rsplit("/", 1)[-1])
        if listed is None:
            return ArtifactsResponse(404, {"error": {"message": "not found"}})
        return ArtifactsResponse(200, {"value": listed})

    def describe(self):
        return "fake artifacts"


class Arm:
    """Azure Resource Manager: one Spark pool, nothing else."""

    def get(self, url, token):
        if "bigDataPools" in url:
            return ArmResponse(200, {"value": [{"name": "sparkpool1", "properties": {
                "nodeSize": "Small", "sparkVersion": "3.3", "autoScale": {"enabled": True, "minNodeCount": 3, "maxNodeCount": 10}}}]})
        return ArmResponse(200, {"value": []})


class Sdk:
    def get_token(self, *scopes, **kwargs):
        return SimpleNamespace(token=TOKEN, expires_on=int(time.time()) + 3600)


RESOURCES = {
    "pipelines": [{"name": "PL_Copy", "properties": {"activities": [{
        "name": "Copy", "type": "Copy", "typeProperties": {}, "outputs": [],
        "inputs": [{"referenceName": "DS_In", "type": "DatasetReference"}]}]}}],
    "datasets": [{"name": "DS_In", "properties": {
        "type": "Parquet", "linkedServiceName": {"referenceName": "LS_Lake", "type": "LinkedServiceReference"}}}],
    "linkedservices": [{"name": "LS_Lake", "properties": {
        "type": "AzureBlobFS", "typeProperties": {"url": "https://lake.dfs.core.windows.net"}}}],
    "notebooks": [{"name": "NB_Load", "properties": {"nbformat": 4, "nbformat_minor": 2, "cells": [], "metadata": {}}}],
}


@pytest.fixture()
def no_sign_in(monkeypatch):
    """Restoring rebuilds an identity; here, one that can never open a window. Records how it was asked for."""
    made = []

    def factory(method, tenant_id=None, remember=True, **kwargs):
        made.append({"tenant": tenant_id, "remember": remember})
        return AzureCliCredentialProvider(credential=Sdk())

    monkeypatch.setattr(service_module, "credential_provider", factory)
    return made


def discovered(store, method="interactive_browser"):
    """A session signed in, connected to ``ws`` and with a finished discovery, saved as it went."""
    session = Session(store)
    settings = ConnectionSettings(azure=AzureConnectionConfig(subscription_id=SUB),
                                  synapse=SynapseConnectionConfig(resource_group="rg", workspace_name="ws"))
    manager = ConnectionManager(settings, credential=AzureCliCredentialProvider(credential=Sdk()), transport=Arm(),
                                artifacts_client=Artifacts(RESOURCES))
    session._signin = _SignIn(method=method, provider=None, azure=None, subscription_id=SUB,  # noqa: SLF001
                              tenant_id=TENANT, subscription_name="Contoso")
    conn = _Connection(method=method, manager=manager, tenant_id=TENANT, subscription_id=SUB, resource_group="rg",
                       workspace="ws", sql_pool=None, subscription_name="Contoso", connected=True,
                       tested_at="2026-10-07T10:00:00+00:00",
                       checks=[{"name": "Azure authentication", "status": "ok", "message": "ok", "category": None}])
    session._connection = conn  # noqa: SLF001
    job = _Job(state="running", started_at="2026-10-07T10:01:00+00:00", workspace="ws")
    session._job = job  # noqa: SLF001
    session._discover(conn, job)  # noqa: SLF001 - in this thread; it saves as it finishes
    assert job.state.startswith("completed"), job.error
    return session


def test_a_restarted_server_has_its_sign_in_connection_and_discovery_back(tmp_path, no_sign_in):
    before = discovered(StateStore(tmp_path))
    after = Session(StateStore(tmp_path))
    kept, notes = after.restore()

    assert kept == ["the Synapse sign-in", "the connection to ws", "the discovery (5 objects)"] and notes == []
    assert no_sign_in == [{"tenant": TENANT, "remember": True}]  # the interactive browser's own record: silent
    assert after.connection_state() == before.connection_state()
    assert after.source_identity() == ("ws", "")
    assert after.discovery_status() == before.discovery_status()
    page = {"pageSize": "200"}
    assert after.results(page)["items"] == before.results(page)["items"]
    assert after.dependency_graph() == before.dependency_graph()
    first = before.results(page)["items"][0]["id"]
    assert after.result(first) == before.result(first)
    for path in tmp_path.iterdir():  # never a token
        assert TOKEN.encode() not in path.read_bytes()


def test_the_restored_discovery_is_indexed_by_the_code_that_runs_now(tmp_path, no_sign_in, monkeypatch):
    discovered(StateStore(tmp_path))
    original = service_module.mapping.list_item
    monkeypatch.setattr(service_module.mapping, "list_item",
                        lambda *args, **kwargs: {**original(*args, **kwargs), "added": "by the update"})
    after = Session(StateStore(tmp_path))
    after.restore()
    records = [i for i in after.results({"pageSize": "200"})["items"] if i["type"] != "Spark Pool"]
    assert [i.get("added") for i in records] == ["by the update"] * 4


def test_an_azure_cli_sign_in_comes_back_and_asks_once_more_on_first_use(tmp_path, no_sign_in):
    discovered(StateStore(tmp_path), method="azure_cli")
    after = Session(StateStore(tmp_path))
    kept, notes = after.restore()
    assert "the Synapse sign-in" in kept and after.connection_state()["signedIn"] is True
    assert no_sign_in == [{"tenant": TENANT, "remember": False}]  # keeps nothing, as it always has
    assert any("sign in once more" in note for note in notes)


def test_a_discovery_cut_off_by_a_restart_comes_back_failed_not_running(tmp_path, no_sign_in):
    session = Session(StateStore(tmp_path))
    session._job = _Job(state="running", started_at="2026-10-07T10:01:00+00:00", workspace="ws")  # noqa: SLF001
    session.persist()
    after = Session(StateStore(tmp_path))
    after.restore()
    status = after.discovery_status()
    assert status["state"] == "failed" and "restarted while discovery was running" in status["error"]
    assert after.busy() == []


def test_a_discovery_the_update_cannot_rebuild_is_dropped_with_a_note(tmp_path, no_sign_in, monkeypatch):
    discovered(StateStore(tmp_path))

    def cannot(cls, *args):
        raise KeyError("a field the update removed")

    monkeypatch.setattr(Session, "_indexed", classmethod(cannot))
    after = Session(StateStore(tmp_path))
    kept, notes = after.restore()
    assert kept == ["the Synapse sign-in", "the connection to ws"]
    assert any("Run discovery again" in note for note in notes)
    assert after.discovery_status()["state"] == "idle"
    assert not StateStore(tmp_path).path("discovery").exists()


def test_signing_out_leaves_nothing_to_restore(tmp_path, no_sign_in):
    session = discovered(StateStore(tmp_path))
    session.disconnect()
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    assert Session(StateStore(tmp_path)).restore() == ([], [])


def test_a_discovery_is_written_once_per_change_not_on_every_save(tmp_path, no_sign_in):
    session = discovered(StateStore(tmp_path))
    written = StateStore(tmp_path).path("discovery").stat().st_mtime_ns
    time.sleep(0.02)
    session.persist()  # the connection was tested again, say: same discovery
    assert StateStore(tmp_path).path("discovery").stat().st_mtime_ns == written


# --- the Fabric target ------------------------------------------------------------------


def connected_fabric(store):
    fabric = FabricTarget(store)
    with fabric._lock:  # noqa: SLF001
        fabric.status, fabric.method, fabric.account, fabric.tenant = "connected", "azure_cli", "ops@contoso.com", TENANT
        fabric.workspaces = [{"id": WS, "name": "Sales WS"}]
        fabric.workspace_id, fabric.workspace_name, fabric.capacity_id = WS, "Sales WS", "cap-1"
        fabric.checks = [{"label": "Workspace accessible", "ok": True, "detail": "Sales WS"}]
    fabric.persist()
    return fabric


def test_the_fabric_connection_comes_back_and_a_sign_in_in_progress_does_not(tmp_path):
    before = connected_fabric(StateStore(tmp_path))
    after = FabricTarget(StateStore(tmp_path))
    assert after.restore() == (["the Fabric connection to Sales WS"], [])
    assert after.state() == before.state()
    assert after.migration_target() == (WS, "Sales WS", "azure_cli")

    with before._lock:  # noqa: SLF001
        before.status = "signing_in"  # a restart would cut the browser sign-in off
    before.persist()
    assert not StateStore(tmp_path).path("fabric").exists()
    assert FabricTarget(StateStore(tmp_path)).restore() == ([], [])


def test_fabric_reports_a_sign_in_as_work_a_restart_waits_for(tmp_path):
    fabric = connected_fabric(StateStore(tmp_path))
    assert fabric.busy() == []
    with fabric._lock:  # noqa: SLF001
        fabric.status = "signing_in"
    assert fabric.busy() == ["a Fabric sign-in"]


# --- the server ---------------------------------------------------------------------------


def get(port, path):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    response = conn.getresponse()
    body = json.loads(response.read())
    conn.close()
    return body


@pytest.fixture()
def running():
    servers = []

    def start(server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_health_says_which_process_answers_what_it_is_doing_and_what_came_back(tmp_path, no_sign_in, running):
    discovered(StateStore(tmp_path))
    server = create_server("127.0.0.1", 0, store=StateStore(tmp_path), supervised=True, boot_id="b007")
    port = running(server)
    health = get(port, "/api/health")
    assert (health["bootId"], health["supervised"], health["busy"]) == ("b007", True, [])
    assert health["restored"]["kept"] == ["the Synapse sign-in", "the connection to ws", "the discovery (5 objects)"]
    assert get(port, "/api/discovery/status")["state"] == "completed_with_warnings"

    session = server.parts[0]
    with session._lock:  # noqa: SLF001
        session._job = _Job(state="running")  # noqa: SLF001
    assert get(port, "/api/health")["busy"] == ["discovery"]


def test_without_a_store_nothing_is_written_and_nothing_restored(tmp_path, monkeypatch, running):
    monkeypatch.setenv("SYNAPSE_DISCOVERY_HOME", str(tmp_path))
    server = create_server("127.0.0.1", 0)
    health = get(running(server), "/api/health")
    assert health["supervised"] is False and health["restored"] is None and len(health["bootId"]) == 16
    server.save_state()
    assert list(tmp_path.iterdir()) == []


def test_a_port_in_use_is_refused_and_its_saved_state_left_alone(tmp_path, no_sign_in):
    store = StateStore(tmp_path)
    session = Session(store)
    session._job = _Job(state="running", started_at="2026-10-07T10:01:00+00:00", workspace="ws")  # noqa: SLF001
    session.persist()
    saved = store.path("discovery").read_bytes()
    first = create_server("127.0.0.1", 0)
    try:
        with pytest.raises(OSError):  # on Windows too: two servers never share a port
            create_server("127.0.0.1", first.server_address[1], store=StateStore(tmp_path))
        assert store.path("discovery").read_bytes() == saved  # still "running": the other server's to change
    finally:
        first.server_close()


def test_one_part_that_cannot_come_back_does_not_stop_the_others():
    class Broken:
        def restore(self):
            raise RuntimeError("boom")

    class Fine:
        def restore(self):
            return ["the thing"], []

    restored = restore_state(Broken(), Fine())
    assert restored["kept"] == ["the thing"] and "could not be restored" in restored["notes"][0]
