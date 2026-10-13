"""What survives an API restart. Offline: a real discovery of a ZIP export, saved and loaded by a new Session."""

from __future__ import annotations

import pickle

import pytest

from discovery_agent.api import persist
from discovery_agent.api.persist import StateStore
from discovery_agent.api.service import DISCOVERY_STATE, Session

from test_repository_source import FILES, PROD_PARAMETERS, wait_for_discovery, zip_bytes


def discovered(store):
    session = Session(store)
    upload = session.upload_zip(zip_bytes({f"repo-main/synapse/{k}": v for k, v in FILES.items()}), "contoso-synapse.zip")
    session.connect_repository({"kind": "zip", "uploadId": upload["uploadId"], "fileName": upload["fileName"],
                                "environment": "Prod", "parameters": PROD_PARAMETERS, "parametersName": "prod.json"})
    session.start_discovery()
    assert wait_for_discovery(session)["state"].startswith("completed")
    return session


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("MIGRATION_STATE", raising=False)
    return StateStore(tmp_path / "state")


def test_a_finished_discovery_is_back_after_a_restart(store):
    before = discovered(store)
    items = {i["id"] for i in before.migration_snapshot()[0].items}

    after = Session(store)                      # the restart: a new process, nothing in memory
    status = after.discovery_status()
    assert status["state"].startswith("completed") and status["restored"] is True
    assert status["restoredFrom"] == {"workspace": "contoso-synapse", "pool": None}
    job, pool = after.migration_snapshot()
    assert {i["id"] for i in job.items} == items and pool is None
    assert after.results({"pageSize": "5"})["total"] == len(items)
    assert after.connection_state()["status"] == "disconnected"   # the source sign-in is never kept


def test_nothing_secret_is_saved(store):
    discovered(store)
    raw = (store.directory / f"{DISCOVERY_STATE}.pickle").read_bytes()
    saved = pickle.loads(raw)["value"]
    assert set(saved) == {"job", "workspace", "pool"}
    # Discovered definitions name Key Vault secrets ("secretName"), which is a reference, not a value. What must never
    # be here is sign-in or credential material.
    for needle in (b"accessToken", b"access_token", b"Bearer ", b"refresh_token", b"Password=", b"AccountKey="):
        assert needle not in raw


def test_resetting_or_signing_out_forgets_the_saved_discovery(store):
    session = discovered(store)
    session.reset_discovery()
    assert not Session(store).discovery_status()["state"].startswith("completed")
    session = discovered(store)
    session.disconnect()
    assert not Session(store).discovery_status()["state"].startswith("completed")


def test_a_file_from_an_older_shape_is_ignored(store, monkeypatch):
    discovered(store)
    monkeypatch.setattr(persist, "VERSION", persist.VERSION + 1)
    assert Session(store).discovery_status()["state"] == "idle"
    (store.directory / f"{DISCOVERY_STATE}.pickle").write_bytes(b"not a pickle")
    assert Session(store).discovery_status()["state"] == "idle"


def test_state_can_be_switched_off(store, monkeypatch):
    monkeypatch.setenv("MIGRATION_STATE", "off")
    discovered(store)
    assert not (store.directory / f"{DISCOVERY_STATE}.pickle").exists()


# -- the Fabric target ------------------------------------------------------------------------------

from discovery_agent.api import fabric as fabric_module  # noqa: E402
from discovery_agent.api.fabric import FabricError, FabricTarget, TARGET_STATE  # noqa: E402

WID = "0a1b2c3d-1111-4222-8333-444455556666"


def settle(target):
    import time
    for _ in range(3000):  # up to 30 s: a busy machine can be slow to schedule the thread
        if target.state()["status"] != "signing_in":
            break
        time.sleep(0.01)
    return target.state()


def test_the_fabric_target_reconnects_silently_after_a_restart(store, monkeypatch):
    calls = []

    def verify(self, method, wid, checks):
        calls.append((method, wid))
        self.workspace_id, self.workspace_name, self.capacity_id = wid, "Fabric_Migration", "cap"
        checks.append({"label": "Workspace accessible", "ok": True, "detail": ""})

    monkeypatch.setattr(FabricTarget, "_verify", verify)
    monkeypatch.setattr(fabric_module, "_require", lambda cli, name: cli)
    store.save(TARGET_STATE, {"method": "azure_cli", "account": "me@x", "workspaceId": WID, "workspaceName": "Fabric_Migration"})
    target = FabricTarget(store)
    state = settle(target)
    assert state["status"] == "connected" and state["workspaceName"] == "Fabric_Migration" and calls == [("azure_cli", WID)]
    assert target.migration_target() == (WID, "Fabric_Migration", "azure_cli")


def test_a_target_that_cannot_reconnect_asks_to_log_in_and_signing_out_forgets_it(store, monkeypatch):
    def verify(self, method, wid, checks):
        raise FabricError(401, "authentication_failed", "Azure CLI is no longer authenticated.")

    monkeypatch.setattr(FabricTarget, "_verify", verify)
    monkeypatch.setattr(fabric_module, "_require", lambda cli, name: cli)
    store.save(TARGET_STATE, {"method": "azure_cli", "workspaceId": WID, "workspaceName": "Fabric_Migration"})
    state = settle(FabricTarget(store))
    assert state["status"] == "disconnected" and "Log in again" in state["message"] and "Fabric_Migration" in state["message"]
    FabricTarget(store).disconnect()
    assert store.load(TARGET_STATE) is None


def test_no_saved_target_means_nothing_happens_at_start(store):
    assert FabricTarget(store).state()["status"] == "disconnected"
    assert FabricTarget().state()["status"] == "disconnected"


# -- --reload ----------------------------------------------------------------------------------------

def test_reload_notices_a_changed_added_or_removed_file():
    from discovery_agent.api.__main__ import _changed
    before = {"a.py": 1.0, "b.py": 1.0}
    assert _changed(before, dict(before)) == []
    assert _changed(before, {"a.py": 2.0, "b.py": 1.0}) == ["a.py"]
    assert _changed(before, {"a.py": 1.0}) == ["b.py"]
    assert _changed(before, {**before, "c.py": 1.0}) == ["c.py"]


# -- what a live discovery holds ----------------------------------------------------------------------

def test_read_only_mappings_from_a_live_discovery_are_saved_and_stay_read_only(store):
    """A live workspace's artifacts keep their payload in a MappingProxyType, which pickle refuses on its own."""
    from types import MappingProxyType

    from discovery_agent.connections.validation import ConnectionValidation, ValidationStatus
    from discovery_agent.extractors.models import SourceType
    from discovery_agent.source_strategy import P0Artifact
    from discovery_agent.synapse.models import SynapseArtifact

    artifact = SynapseArtifact(P0Artifact.PIPELINE, "PL_Load", "pipelines", "json", {"name": "PL_Load", "properties": {"activities": []}})
    check = ConnectionValidation(SourceType.SYNAPSE, ValidationStatus.OK, "ok", {"workspace": "ws"})
    assert type(artifact.payload) is MappingProxyType
    assert store.save("probe", {"artifact": artifact, "check": check}) is True
    back = store.load("probe")
    assert back["artifact"] == artifact and back["artifact"].payload["name"] == "PL_Load"
    assert type(back["artifact"].payload) is MappingProxyType and type(back["check"].details) is MappingProxyType
    with pytest.raises(TypeError):
        back["artifact"].payload["name"] = "changed"   # still read-only after the round trip
    assert not list(store.directory.glob("*.tmp"))
