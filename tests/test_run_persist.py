"""A migration run survives an API restart: its record is saved as it goes and loaded by a new MigrationService.

Offline, with the fakes of ``test_migration``: the "restart" is a second service built on the same state folder."""

from __future__ import annotations

import threading

import pytest

from discovery_agent.api.migration import RUN_STATE, MigrationService
from discovery_agent.api.persist import StateStore
from discovery_agent.api.service import ApiError

from test_migration import FakeDb, FakeFabric, FakeRest, FakeSession, fake_job, wait_until

ORDERS = {"id": "sql://ws/pool/sales/Orders", "wave": 1}
NOTEBOOK = {"id": "synapse://notebook/LoadSales", "wave": 2}
SECRET = "p@ssw0rd-never-saved"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("MIGRATION_STATE", raising=False)
    return StateStore(tmp_path / "state")


def service(store, rest=None, db=None, fabric=None):
    rest = rest or FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}])
    return MigrationService(FakeSession(fake_job()), fabric or FakeFabric(), rest_factory=lambda: rest,
                            sql_factory=lambda h, d: db or FakeDb(), driver=lambda: "ODBC Driver 18 for SQL Server", store=store)


def saved(store):
    return store.load(RUN_STATE)


def test_a_finished_run_is_back_after_a_restart_and_run_numbers_continue(store):
    before = service(store)
    before.start({"items": [ORDERS, NOTEBOOK], "options": {"stages": ["warehouse", "notebooks"]}})
    assert wait_until(lambda: before.state()["state"] == "completed")
    assert wait_until(lambda: (saved(store) or {}).get("run") is not None and saved(store)["run"].state == "completed")
    before.cutover_complete({"approvedBy": "Data lead"})
    assert wait_until(lambda: (saved(store) or {}).get("cutover"))

    after = service(store)                       # the restart: a new process, nothing in memory
    state = after.state()
    assert state["state"] == "completed" and state["completed"] == 2 and state["restoredAt"]
    assert [i["name"] for i in state["items"]] == ["sales.Orders", "LoadSales"]
    assert any("RESTORED" in line for line in state["logs"])
    assert after._cutover["approvedBy"] == "Data lead"  # noqa: SLF001
    after.control({"action": "reset"})
    assert after.start({"items": [NOTEBOOK], "options": {"stages": ["notebooks"]}})["runId"] == "002"


def test_an_object_interrupted_by_a_restart_is_failed_and_retry_finishes_it(store):
    gate = threading.Event()

    class SlowRest(FakeRest):
        def create(self, path, body):
            gate.wait(5)
            return super().create(path, body)

    before = service(store, rest=SlowRest())
    before.start({"items": [ORDERS, NOTEBOOK], "options": {"stages": ["warehouse", "notebooks"]},
                  "credentials": {"LS_Sql": {"authType": "Basic", "username": "u", "password": SECRET}}})
    assert wait_until(lambda: before.state()["inProgress"] == 1)
    before._save_now()  # noqa: SLF001 - the moment the API stops: Orders is half done

    after = service(store)
    state = after.state()
    assert state["state"] == "paused" and state["restoredAt"]
    assert "API restarted" in state["haltedReason"]
    orders = next(i for i in state["items"] if i["name"] == "sales.Orders")
    assert orders["status"] == "FAILED" and orders["step"] == "Interrupted"
    assert state["pending"] == 1 and after._credentials == {}  # noqa: SLF001 - credentials never come back from disk

    # Resume and Retry take the credentials again; Retry re-runs the interrupted object, then the rest.
    after.control({"action": "retry", "credentials": {"LS_Sql": {"authType": "Basic", "username": "u", "password": "again"}}})
    assert after._credentials["LS_Sql"]["password"] == "again"  # noqa: SLF001
    assert wait_until(lambda: after.state()["state"] == "completed")
    assert (after.state()["completed"], after.state()["failed"]) == (2, 0)
    gate.set()


def test_credentials_are_never_in_the_saved_record(store):
    svc = service(store)
    svc.start({"items": [NOTEBOOK], "options": {"stages": ["notebooks"]},
               "credentials": {"LS_Sql": {"authType": "Basic", "username": "u", "password": SECRET}}})
    assert wait_until(lambda: svc.state()["state"] == "completed")
    svc._save_now()  # noqa: SLF001
    raw = (store.directory / f"{RUN_STATE}.pickle").read_bytes()
    assert SECRET.encode() not in raw and b"Bearer " not in raw


def test_a_restored_run_is_not_continued_into_another_workspace(store):
    gate = threading.Event()

    class SlowRest(FakeRest):
        def create(self, path, body):
            gate.wait(5)
            return super().create(path, body)

    before = service(store, rest=SlowRest())
    before.start({"items": [ORDERS, NOTEBOOK], "options": {"stages": ["warehouse", "notebooks"]}})
    assert wait_until(lambda: before.state()["inProgress"] == 1)
    before._save_now()  # noqa: SLF001

    class OtherFabric(FakeFabric):
        def migration_target(self):
            return "other-ws-id", "Other WS", "azure_cli"

    after = service(store, fabric=OtherFabric())
    with pytest.raises(ApiError) as refused:
        after.control({"action": "resume"})
    assert refused.value.code == "other_workspace"
    assert after.state()["state"] == "paused"
    gate.set()


def test_reset_is_saved_too(store):
    svc = service(store)
    svc.start({"items": [NOTEBOOK], "options": {"stages": ["notebooks"]}})
    assert wait_until(lambda: svc.state()["state"] == "completed")
    svc.control({"action": "reset"})
    assert wait_until(lambda: (saved(store) or {"run": 1}).get("run") is None)
    assert service(store).state()["state"] == "idle"


def test_switched_off_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGRATION_STATE", "off")
    store = StateStore(tmp_path / "state")
    svc = service(store)
    svc.start({"items": [NOTEBOOK], "options": {"stages": ["notebooks"]}})
    assert wait_until(lambda: svc.state()["state"] == "completed")
    svc._save_now()  # noqa: SLF001
    assert not (tmp_path / "state").exists()
