"""A migration run's record: written as it happens, brought back after a restart, signed off when it must be,
and never left half told. Offline: Fabric and the Warehouse are the fakes the other migration tests use, and a
"restart" is a new service on the same project store."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from discovery_agent.api.migration import MigrationService, run_needs
from discovery_agent.api.service import ApiError
from discovery_agent.migration import datapipeline, run_state
from discovery_agent.migration.fabric_rest import FabricApiError
from discovery_agent.migration.runner import (
    COMPLETED, DATA, FAILED, IN_PROGRESS, NOTEBOOK, PENDING, SPARKJOB, TABLE, VIEW, MigrationRun, Migrator, Source, _Recording,
)
from discovery_agent.state import context
from discovery_agent.state.audit import Auditor
from discovery_agent.state.runs import RunRecorder
from discovery_agent.state.store import Store

from test_migration import WS, FakeDb, FakeFabric, FakeRest, FakeSession, fake_job, view, wait_until
from test_migration_stages import ORDERS, POOL_CONN, POOL_CREDS, DataDb, PipelineRest, SourceCounts, data_item, load, rows

PERSON = context.Actor(context.PERSON, "Asha Rao")
NOTEBOOK_ONLY = {"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}], "options": {"stages": ["notebooks"]}}
ORDERS_ID, VIEW_ID = "sql://ws/pool/sales/Orders", "sql://ws/pool/sales/vOrders"


class Project:
    """One project's store, and a migration service on it as the API builds one. A restart is a new service."""

    def __init__(self, tmp_path: Path, job=None) -> None:
        self.store = Store(tmp_path / "project")
        self.trail = Auditor(self.store)
        self.job = job or fake_job()

    def service(self, rest=None, db=None, session=None) -> MigrationService:
        self.rest = rest or FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}])
        self.db = db or FakeDb()
        return MigrationService(session or FakeSession(self.job), FakeFabric(), rest_factory=lambda: self.rest,
                                sql_factory=lambda h, d: self.db, driver=lambda: "ODBC Driver 18 for SQL Server",
                                records=self.store, audit=self.trail.record)

    def actions(self):
        return [r["action"] for r in self.trail.records()]

    def record(self, action):
        return next(r for r in self.trail.records() if r["action"] == action)


def finished(svc: MigrationService) -> bool:
    """The pass is over and its thread has tidied up, and so has the Fabric Git commit after it (on Windows a file it is
    still writing cannot be read)."""
    return wait_until(lambda: svc.state()["status"] not in run_state.WORKING
                      and not (svc._thread is not None and svc._thread.is_alive())  # noqa: SLF001
                      and not (svc._git_thread is not None and svc._git_thread.is_alive()))  # noqa: SLF001


def crash_after_starting_an_object(self, run, trigger="start"):
    """Stands in for ``_spawn``: the process dies while its pass is making the first object. Nothing after that is written."""
    run.begin_pass(trigger)
    item = next(i for i in run.items if i.status == PENDING)
    run.update(item, status=IN_PROGRESS, step="Creating in Fabric", attempts=1)


def start_and_crash(project, monkeypatch, body, **service):
    real = MigrationService._spawn
    monkeypatch.setattr(MigrationService, "_spawn", crash_after_starting_an_object)
    try:
        with context.bound(actor=PERSON):
            project.service(**service).start(body)
    finally:
        monkeypatch.setattr(MigrationService, "_spawn", real)


class SessionWithPool(FakeSession):
    """A session connected to the Synapse pool, as the data stage needs."""

    def source_endpoint(self):
        return "ws", "ws.sql.azuresynapse.net", "pool01"

    def source_sql_factory(self):
        return lambda: SourceCounts({})


def job_with_a_view():
    job = fake_job()
    job.items.append({"id": VIEW_ID, "name": "sales.vOrders", "type": "View"})
    job.records_by_id[VIEW_ID] = SimpleNamespace(identity=SimpleNamespace(name="vOrders", schema="sales"), content=view())
    job.graph["nodes"].append({"id": VIEW_ID, "name": "sales.vOrders", "type": "View", "classification": "TRANSFORM",
                               "fabricTarget": "Warehouse view", "wave": 3, "dependedOnBy": 0})
    job.graph["edges"].append({"source": VIEW_ID, "target": ORDERS_ID})
    return job


# -- the record, as it happens ----------------------------------------------------------------------------------

def test_a_run_is_recorded_as_it_happens_and_its_record_alone_gives_it_back(tmp_path):
    project = Project(tmp_path)
    svc = project.service()
    with context.bound(actor=PERSON):
        svc.start(NOTEBOOK_ONLY)
    assert finished(svc)
    run = svc.state()
    assert run["status"] == "completed" and run["completed"] == 1 and [p["status"] for p in run["passes"]] == ["finished"]

    events = project.store.records("runs/001/events")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert [(e["from"], e["to"]) for e in events if e["type"] == "status"] == [("queued", "running"), ("running", "completed")]
    assert [e["item"]["status"] for e in events if e["type"] == "item"] == ["IN PROGRESS", "COMPLETED"]
    assert all(e["at"] and e["actor"] for e in events)

    loaded = RunRecorder(project.store).load("001")
    assert loaded["run"]["status"] == "completed" and [i["status"] for i in loaded["items"]] == ["COMPLETED"]
    assert loaded["run"]["requestedBy"] == {"kind": "person", "id": "Asha Rao"}
    assert loaded["run"]["approval"]["automatic"] is True and loaded["run"]["approval"]["by"]["kind"] == "system"
    assert loaded["run"]["pins"]["plan"] == [{"id": "synapse://notebook/LoadSales", "wave": 1}]

    # Asked for by a person; approved and finished by the accelerator, and said so.
    assert project.record("run.requested")["actor"] == {"kind": "person", "id": "Asha Rao"}
    approved = project.record("run.approved")
    assert approved["actor"]["kind"] == "system" and approved["details"]["automatic"] is True
    completed = project.record("run.completed")
    assert completed["actor"]["kind"] == "system" and completed["details"]["completed"] == 1 and completed["run"] == "001"
    assert project.trail.verify()["ok"]


def test_run_needs_holds_back_only_what_cannot_be_made_without_the_other():
    sources = [Source("t", "t", "Table", 1, TABLE), Source("v", "v", "View", 2, VIEW), Source("n", "n", "Notebook", 2, NOTEBOOK),
               Source("t#data", "t (data)", "Table data", 1, DATA)]
    job = SimpleNamespace(graph={"edges": [{"source": "v", "target": "t"}, {"source": "n", "target": "t"}]})
    assert run_needs(sources, job) == {"v": ["t"], "t#data": ["t"]}  # a notebook only reads the table when it runs


# -- a restart ----------------------------------------------------------------------------------------------------

def test_a_run_a_restart_cut_short_is_paused_as_interrupted_and_resumes_without_making_anything_twice(tmp_path, monkeypatch):
    project = Project(tmp_path)
    start_and_crash(project, monkeypatch, NOTEBOOK_ONLY)
    # The restart. The attempt the process died in did make the notebook, but never heard back.
    svc = project.service(rest=FakeRest(notebooks=["LoadSales"], warehouses=[{"id": "wh-1", "displayName": "pool01"}]))
    run = svc.state()
    assert run["status"] == "paused" and run["pausedReason"] == "interrupted" and run["state"] == "paused"
    item = run["items"][0]
    assert item["status"] == "PENDING" and item["maybeCreated"] is True and item["category"] == "INTERRUPTED"
    assert run["passes"][-1]["status"] == "interrupted"
    assert any("RECOVERED" in line and "LoadSales" in line for line in run["logs"])
    interrupted = project.record("run.interrupted")
    assert interrupted["actor"]["kind"] == "system" and interrupted["details"]["working"] == ["LoadSales"]

    with context.bound(actor=PERSON):
        svc.control({"action": "resume"})
    assert finished(svc)
    item = svc.state()["items"][0]
    assert item["status"] == "COMPLETED" and "earlier attempt in this run" in item["notes"][0]
    assert not [path for path, _ in project.rest.created if path.endswith("/notebooks")]  # found there, never made twice
    assert [p["trigger"] for p in svc.state()["passes"]] == ["start", "resume"]


def test_credentials_a_run_was_given_are_asked_for_again_after_a_restart_and_never_written(tmp_path, monkeypatch):
    project = Project(tmp_path)
    name = datapipeline.source_connection_name("ws", "pool01")
    secret = {name: {"authType": "Basic", "username": "loader", "password": "TOPSECRET-1"}}
    body = {"items": [{"id": ORDERS_ID, "wave": 1}], "options": {"stages": ["warehouse", "data"]}, "credentials": secret}
    start_and_crash(project, monkeypatch, body, session=SessionWithPool(project.job))
    svc = project.service(session=SessionWithPool(project.job))
    assert svc.state()["needsInput"] == [name]
    with pytest.raises(ApiError) as missing:
        svc.control({"action": "resume"})
    assert missing.value.code == "needs_input" and name in missing.value.message
    with context.bound(actor=PERSON):
        svc.control({"action": "resume", "credentials": secret})
    assert finished(svc)
    assert svc.state()["needsInput"] == [] and len(svc.state()["passes"]) == 2
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"TOPSECRET-1" not in path.read_bytes(), path


def test_a_run_made_from_an_older_discovery_cannot_carry_on_but_its_record_stays_in_the_history(tmp_path, monkeypatch):
    first = fake_job()
    first.snapshot = {"id": "S001", "fingerprint": "aaaa"}
    project = Project(tmp_path, first)
    start_and_crash(project, monkeypatch, NOTEBOOK_ONLY)
    project.job = fake_job()
    project.job.snapshot = {"id": "S002", "fingerprint": "bbbb"}  # discovery was run again in between
    svc = project.service()
    run = svc.state()
    assert run["status"] == "paused" and run["resumable"] is False and "Discovery has been run again" in run["unresumableReason"]
    with pytest.raises(ApiError) as refused:
        svc.control({"action": "resume"})
    assert refused.value.code == "not_resumable"

    with context.bound(actor=PERSON):
        assert svc.control({"action": "reset"})["state"] == "idle"
    history = svc.runs()
    assert history["current"] is None
    assert [(r["runId"], r["status"]) for r in history["runs"]] == [("001", "cancelled")] and history["runs"][0]["closedAt"]
    kept = svc.run_record("001")
    assert kept["status"] == "cancelled" and kept["items"][0]["name"] == "LoadSales" and kept["resumable"] is False
    page = svc.run_events("001", after=0, limit=2)
    assert [e["seq"] for e in page["events"]] == [1, 2] and page["last"] == 2
    assert svc.run_events("001", after=page["last"])["events"][0]["seq"] == 3
    with pytest.raises(ApiError):
        svc.run_events("../etc")
    assert project.record("run.closed")["details"]["status"] == "cancelled"

    # After another restart nothing is current, and the next run is numbered after the last.
    again = project.service()
    assert again.state()["state"] == "idle"
    with context.bound(actor=PERSON):
        again.start(NOTEBOOK_ONLY)
    assert again.state()["runId"] == "002" and finished(again)
    assert [r["runId"] for r in again.runs()["runs"]] == ["002", "001"]


# -- sign-off -----------------------------------------------------------------------------------------------------

def test_a_run_that_changes_what_fabric_has_waits_for_a_person_and_starts_once_signed_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGRATION_SIGNOFF", "overwrite")
    project = Project(tmp_path)
    svc = project.service()
    with context.bound(actor=PERSON):
        waiting = svc.start({**NOTEBOOK_ONLY, "options": {"stages": ["notebooks"], "existing": "update"}})
    assert waiting["status"] == "awaiting_approval" and waiting["state"] == "paused"
    assert "updates objects that are already in Fabric" in waiting["approval"]["reason"]
    assert not project.rest.created and svc._thread is None  # noqa: SLF001 - nothing written, nothing running

    with pytest.raises(ApiError) as accelerator:
        svc.approve({"decision": "approve"})  # outside a request: the accelerator, which never signs a run off
    assert accelerator.value.status == 403
    with pytest.raises(ApiError) as early:
        svc.control({"action": "retry"})
    assert early.value.code == "not_signed_off"
    with context.bound(actor=PERSON):
        with pytest.raises(ApiError) as stale:
            svc.approve({"decision": "approve", "subjectHash": "0" * 64})
        assert stale.value.code == "stale"
        svc.approve({"decision": "approve", "comment": "Checked with the data owner",
                     "subjectHash": waiting["approval"]["subjectHash"]})
    assert finished(svc)
    run = svc.state()
    assert run["status"] == "completed" and run["approval"]["by"] == {"kind": "person", "id": "Asha Rao"}
    assert run["approval"]["comment"] == "Checked with the data owner" and run["approval"]["automatic"] is False
    signoffs = project.store.records("approvals")
    assert [(s["decision"], s["subject"]["id"]) for s in signoffs] == [("approved", "001")]
    actions = project.actions()
    assert actions.index("run.signoff_requested") < actions.index("run.approved") < actions.index("run.completed")
    assert project.record("run.approved")["actor"] == {"kind": "person", "id": "Asha Rao"}


def test_a_declined_run_never_writes_and_nothing_carries_it_on(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGRATION_SIGNOFF", "always")
    project = Project(tmp_path)
    svc = project.service()
    with context.bound(actor=PERSON):
        svc.start(NOTEBOOK_ONLY)
        declined = svc.approve({"decision": "reject", "comment": "Wrong workspace"})
    assert declined["status"] == "rejected" and declined["state"] == "completed" and not project.rest.created
    assert svc.control({"action": "retry"})["status"] == "rejected"
    assert project.record("run.rejected")["reason"] == "Wrong workspace"


def test_a_run_waiting_for_sign_off_still_waits_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGRATION_SIGNOFF", "always")
    project = Project(tmp_path)
    with context.bound(actor=PERSON):
        project.service().start(NOTEBOOK_ONLY)
    svc = project.service()
    assert svc.state()["status"] == "awaiting_approval"
    with context.bound(actor=PERSON):
        svc.approve({"decision": "approve"})
    assert finished(svc) and svc.state()["status"] == "completed"


# -- what a pass does on its own ----------------------------------------------------------------------------------

def test_what_needs_a_failed_object_is_blocked_not_attempted_and_retry_runs_both(tmp_path):
    project = Project(tmp_path, job_with_a_view())
    svc = project.service(db=FakeDb(reject="CREATE TABLE"))
    plan = {"items": [{"id": ORDERS_ID, "wave": 1}, {"id": VIEW_ID, "wave": 2}], "options": {"stages": ["warehouse"]}}
    with context.bound(actor=PERSON):
        svc.start(plan)
    assert finished(svc)
    run = svc.state()
    items = {i["name"]: i for i in run["items"]}
    assert run["status"] == "completed_with_failures" and run["blocked"] == 1 and run["failed"] == 1
    assert items["sales.Orders"]["status"] == "FAILED"
    blocked = items["sales.vOrders"]
    assert blocked["status"] == "BLOCKED" and blocked["category"] == "DEPENDENCY" and "sales.Orders" in blocked["error"]
    assert not any("CREATE VIEW" in s for s in project.db.statements)

    project.db.reject = None
    with context.bound(actor=PERSON):
        svc.control({"action": "retry"})
    assert finished(svc)
    assert svc.state()["status"] == "completed" and {i["status"] for i in svc.state()["items"]} == {"COMPLETED"}


def test_a_fault_in_the_runner_itself_ends_the_pass_as_failed_never_running(tmp_path, monkeypatch):
    project = Project(tmp_path)
    svc = project.service()
    real = Migrator._halt_reason

    def broken(self, item):
        raise RuntimeError("a bug in the runner")

    monkeypatch.setattr(Migrator, "_halt_reason", broken)
    with context.bound(actor=PERSON):
        svc.start(NOTEBOOK_ONLY)
    assert finished(svc)
    run = svc.state()
    assert run["status"] == "failed" and run["passes"][-1]["status"] == "failed"
    assert any("unexpected RuntimeError" in line for line in run["logs"])
    monkeypatch.setattr(Migrator, "_halt_reason", real)
    with context.bound(actor=PERSON):
        svc.control({"action": "retry"})
    assert finished(svc) and svc.state()["status"] == "completed"


# -- what a run started in Fabric -----------------------------------------------------------------------------------

def test_each_pipeline_run_a_pass_starts_is_recorded_before_it_is_followed():
    db = DataDb({ORDERS: []})
    run = load([data_item("Orders")], db, PipelineRest(db, {ORDERS: rows(2)}), SourceCounts({ORDERS: 2}))
    assert run.items[0].status == COMPLETED
    assert [(e["kind"], e["wave"], e["status"], e["tables"]) for e in run.executions] == [("pipelineRun", 2, "Completed", ["Orders#data"])]
    assert run.executions[0]["instance"].endswith("/jobs/instances/job-1") and run.to_dict()["executions"][0]["id"] == "E1"


def earlier_load_run(tmp_path):
    run = MigrationRun("001", [data_item("Orders", wave=1)], WS, "Sales WS", "pool01", recorder=RunRecorder(Store(tmp_path)))
    run.pool_name, run.source_server, run.source_database, run.source_connection_name = "pool01", "ws.sql.azuresynapse.net", "pool01", POOL_CONN
    run.executions = [{"id": "E1", "kind": "pipelineRun", "wave": 1, "pipeline": "load_pool01_wave_1", "status": "running",
                       "instance": f"/workspaces/{WS}/items/pl-1/jobs/instances/job-0", "tables": ["Orders#data"]}]
    return run


def test_a_load_started_before_a_restart_is_followed_to_its_end_and_never_started_again(tmp_path):
    db = DataDb({ORDERS: rows(3)})  # it filled the table while the accelerator was down
    rest = PipelineRest(db)
    run = earlier_load_run(tmp_path)
    Migrator(run, lambda: rest, lambda h, d: db, credentials=POOL_CREDS).execute()
    item = run.items[0]
    assert item.status == COMPLETED and "started before the accelerator stopped" in item.step
    assert rest.runs == []  # never a second load beside the first
    assert run.executions[0]["status"] == "Completed" and run.executions[0]["finishedAt"]


def test_a_load_started_before_a_restart_that_failed_is_left_for_a_person(tmp_path):
    db = DataDb({ORDERS: []})
    rest = PipelineRest(db, outcome="Failed")
    run = earlier_load_run(tmp_path)
    Migrator(run, lambda: rest, lambda h, d: db, credentials=POOL_CREDS).execute()
    assert run.items[0].status == FAILED and "before the accelerator stopped ended as Failed" in run.items[0].error
    assert rest.runs == [] and run.executions[0]["status"] == "Failed"


def test_a_load_fabric_no_longer_knows_is_never_started_again_blind(tmp_path):
    db = DataDb({ORDERS: []})

    class Forgotten(PipelineRest):
        def get(self, path):
            if "/jobs/instances/" in path:
                raise FabricApiError(404, "ItemNotFound", "The job instance was not found.")
            return super().get(path)

    rest = Forgotten(db)
    run = earlier_load_run(tmp_path)
    Migrator(run, lambda: rest, lambda h, d: db, credentials=POOL_CREDS).execute()
    assert run.items[0].status == FAILED and "Monitoring hub" in run.items[0].error
    assert rest.runs == [] and run.executions[0]["status"] == "unknown"


# -- what a run sent to Fabric ------------------------------------------------------------------------------------

def test_what_a_run_sends_is_kept_by_its_hash_and_can_be_read_back(tmp_path):
    project = Project(tmp_path)
    svc = project.service()
    with context.bound(actor=PERSON):
        svc.start({"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}, {"id": ORDERS_ID, "wave": 1}],
                   "options": {"stages": ["notebooks", "warehouse"]}})
    assert finished(svc)
    items = {i["name"]: i for i in svc.state()["items"]}
    notebook = items["LoadSales"]["artifact"]["parts"][0]
    assert notebook["kind"] == "fabric" and notebook["what"].endswith("/notebooks")
    body, media, name = svc.artifact("001", notebook["hash"])
    assert json.loads(body)["displayName"] == "LoadSales" and media == "application/json" and name.endswith(".json")
    create = next(p for p in items["sales.Orders"]["artifact"]["parts"] if p["what"] == "create table")
    assert create["kind"] == "sql" and b"CREATE TABLE" in svc.artifact("001", create["hash"])[0]
    with pytest.raises(ApiError):
        svc.artifact("001", "0" * 64)  # only what the run's record names


def test_a_connections_body_is_never_kept_it_carries_credentials():
    kept = []
    client = _Recording(SimpleNamespace(create=lambda path, body: {}), lambda kind, what, content: kept.append(what))
    client.create("/connections", {"displayName": "c", "credentialDetails": {"credentials": {"password": "x"}}})
    client.create(f"/workspaces/{WS}/notebooks", {"displayName": "nb", "definition": {"parts": []}})
    client.create(f"/workspaces/{WS}/items", {"displayName": "x", "clientSecret": "s"})
    client.create(f"/workspaces/{WS}/git/commitToGit", {"comment": "c"})
    assert kept == [f"POST /workspaces/{WS}/notebooks"]


# -- found in review ----------------------------------------------------------------------------------------------

def test_a_refused_start_leaves_the_current_run_as_it_was(tmp_path):
    project = Project(tmp_path, job_with_a_view())
    svc = project.service(db=FakeDb(reject="CREATE TABLE"))
    with context.bound(actor=PERSON):
        svc.start({"items": [{"id": ORDERS_ID, "wave": 1}, {"id": VIEW_ID, "wave": 2}], "options": {"stages": ["warehouse"]}})
    assert finished(svc) and svc.state()["status"] == "completed_with_failures"
    with context.bound(actor=PERSON):
        with pytest.raises(ApiError) as refused:
            svc.start({"items": [{"id": "not-in-discovery", "wave": "x"}], "options": {"stages": ["warehouse"]}})
    assert refused.value.status == 400
    assert svc.state()["runId"] == "001" and svc.state()["status"] == "completed_with_failures"  # still there to retry
    project.db.reject = None
    with context.bound(actor=PERSON):
        svc.control({"action": "retry"})
    assert finished(svc) and svc.state()["status"] == "completed"


def test_run_numbers_carry_on_after_the_highest_run_recorded_whatever_the_pointer_says(tmp_path):
    project = Project(tmp_path)
    svc = project.service()
    with context.bound(actor=PERSON):
        svc.start(NOTEBOOK_ONLY)
    assert finished(svc)
    RunRecorder(project.store).set_pointer(None, 0)  # a pointer that lost count must never let run 001 be written over
    again = project.service()
    with context.bound(actor=PERSON):
        again.start(NOTEBOOK_ONLY)
    assert again.state()["runId"] == "002" and finished(again)
    assert [r["runId"] for r in again.runs()["runs"]] == ["002", "001"]


def test_a_start_prepared_before_a_switch_of_project_is_refused_never_written_into_the_next(tmp_path, monkeypatch):
    project = Project(tmp_path)
    svc = project.service()
    other = Store(tmp_path / "other")
    real = MigrationService._check_capacity

    def switched_meanwhile(self, workspace_id):
        real(self, workspace_id)
        self.switch(other)  # another page switched the project while this Start was being prepared

    monkeypatch.setattr(MigrationService, "_check_capacity", switched_meanwhile)
    with context.bound(actor=PERSON):
        with pytest.raises(ApiError) as refused:
            svc.start(NOTEBOOK_ONLY)
    assert refused.value.code == "project_switched"
    assert RunRecorder(other).runs() == [] and RunRecorder(project.store).runs() == []


def test_a_run_that_cannot_carry_on_cannot_be_signed_off_but_can_be_declined(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGRATION_SIGNOFF", "always")
    first = fake_job()
    first.snapshot = {"id": "S001", "fingerprint": "aaaa"}
    project = Project(tmp_path, first)
    with context.bound(actor=PERSON):
        project.service().start(NOTEBOOK_ONLY)
    project.job = fake_job()
    project.job.snapshot = {"id": "S002", "fingerprint": "bbbb"}
    svc = project.service()
    assert svc.state()["status"] == "awaiting_approval" and svc.state()["resumable"] is False
    with context.bound(actor=PERSON):
        with pytest.raises(ApiError) as refused:
            svc.approve({"decision": "approve"})
        assert refused.value.code == "not_resumable"
        assert svc.approve({"decision": "reject", "comment": "Discovery changed"})["status"] == "rejected"
    assert not project.rest.created


def test_what_a_run_keeps_and_shows_never_holds_a_secret(tmp_path):
    store = Store(tmp_path)
    run = MigrationRun("001", [Source("n", "N", "Notebook", 1, NOTEBOOK)], WS, "WS", "pool01", recorder=RunRecorder(store))
    run.log("NOTE", "N", "the driver said: Password=hunter2; Server=x")
    run.update(run.items[0], status=IN_PROGRESS)
    run.update(run.items[0], status=FAILED, error="Login failed: AccountKey=hunter3==", notes=["api_key: hunter4"])
    shown = json.dumps(run.to_dict())
    kept = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*") if p.is_file())
    for secret in ("hunter2", "hunter3", "hunter4"):
        assert secret not in shown and secret not in kept


class FlakyCounts(DataDb):
    """A Warehouse whose driver fails, unexpectedly, while counting one table's rows."""

    def cursor(self):
        cursor = super().cursor()
        execute = cursor.execute

        def flaky(sql, *params):
            if sql.startswith("SELECT COUNT_BIG") and "Lines" in sql:
                raise RuntimeError("The connection was reset by the driver")
            return execute(sql, *params)

        cursor.execute = flaky
        return cursor


def test_an_unexpected_error_in_a_wave_leaves_none_of_its_tables_in_progress(tmp_path):
    lines = "[sales].[Lines]"
    db = FlakyCounts({ORDERS: [], lines: []})
    run = load([data_item("Orders"), data_item("Lines")], db, PipelineRest(db, {ORDERS: rows(1)}), SourceCounts({ORDERS: 1}))
    assert [i.status for i in run.items] == [FAILED, FAILED]
    assert all("Unexpected RuntimeError" in i.error and "reference" in i.error for i in run.items)
    assert run.status == run_state.COMPLETED_WITH_FAILURES  # and Retry failed runs both again


def test_a_short_outage_while_following_an_earlier_load_never_lets_a_second_one_start(tmp_path):
    db = DataDb({ORDERS: []})

    class Unanswered(PipelineRest):
        def get(self, path):
            if "/jobs/instances/" in path:
                raise FabricApiError(503, "ServiceUnavailable", "Fabric did not answer in time.")
            return super().get(path)

    rest = Unanswered(db)
    run = earlier_load_run(tmp_path)
    Migrator(run, lambda: rest, lambda h, d: db, credentials=POOL_CREDS).execute()
    assert run.items[0].status == FAILED and "did not answer" in run.items[0].error
    assert rest.runs == [] and run.executions[0]["status"] == "running"  # followed again by the next pass


def test_no_load_starts_while_the_pipeline_of_its_wave_is_running_in_fabric_for_something_else():
    db = DataDb({ORDERS: []})
    rest = PipelineRest(db, {ORDERS: rows(2)}, dataPipelines=[{"id": "pl-7", "displayName": "load_pool01_wave_2"}],
                        items=[{"id": "job-x", "status": "InProgress", "startTimeUtc": "2026-10-15T10:00:00Z"}])
    run = load([data_item("Orders")], db, rest, SourceCounts({ORDERS: 2}))
    item = run.items[0]
    assert item.status == FAILED and "already running in Fabric" in item.error and "2026-10-15T10:00:00Z" in item.error
    assert rest.runs == [] and not [p for p, _ in rest.created if p.endswith("/dataPipelines")]


def test_a_retry_after_a_lost_answer_finds_what_the_lost_attempt_made_instead_of_making_it_twice():
    """A Spark job definition is found by name in a listing the pass keeps: the retry must read it again."""
    from test_migration_stages import SJD, StageRest, migrate

    class LostAnswer(StageRest):
        attempts = 0

        def create(self, path, body):
            if not path.endswith("/sparkJobDefinitions"):
                return super().create(path, body)
            self.attempts += 1
            if any(i["displayName"] == body["displayName"] for i in self.store["sparkJobDefinitions"]):
                raise FabricApiError(409, "ItemDisplayNameAlreadyInUse", "The item name is already in use.")
            made = super().create(path, body)  # it was made...
            if self.attempts == 1:
                raise FabricApiError(503, "ServiceUnavailable", "Fabric did not answer in time.")  # ...but the answer was lost
            return made

    rest = LostAnswer(environments=[{"id": "env-7", "displayName": "transportation"}])
    run, rest, _ = migrate([Source("j", "job1", "Spark Job Definition", 4, SPARKJOB, payload=SJD)], rest=rest)
    item = run.items[0]
    assert item.status == COMPLETED and "earlier attempt in this run" in item.notes[0]
    assert rest.attempts == 1
