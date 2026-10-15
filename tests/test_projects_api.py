"""Projects the backend owns, over HTTP: create, switch, rename, archive; each one's audit trail; and the run
history routes. A real server on a free port, on a state folder of the test's own."""

from __future__ import annotations

import csv
import io
import json
import threading
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest

from discovery_agent.api.persist import StateStore
from discovery_agent.api.server import create_server
from discovery_agent.api.service import ApiError, DISCOVERY_STATE, _Job
from discovery_agent.state.projects import Projects

from test_api import call


def saved_discovery(projects: Projects, project_id: str, workspace: str) -> None:
    """A finished discovery saved for a project, as one would be after it ran there."""
    job = _Job(state="completed", started_at="2026-10-01T09:00:00+00:00", finished_at="2026-10-01T09:05:00+00:00",
               items=[{"id": "synapse://notebook/N", "name": "N", "type": "Notebook"}], workspace=workspace,
               summary={"failedCategories": [], "warnings": []})
    projects.folder(project_id).mkdir(parents=True, exist_ok=True)
    StateStore(projects.folder(project_id)).save(DISCOVERY_STATE, {"job": job, "workspace": workspace, "pool": None})


@pytest.fixture()
def api(tmp_path):
    projects = Projects(tmp_path / "state")
    saved_discovery(projects, "default", "ws-default")
    httpd = create_server("127.0.0.1", 0, projects=projects)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(port=httpd.server_address[1], projects=projects, hub=httpd.projects, root=tmp_path / "state")
    httpd.shutdown()
    httpd.server_close()


def discovered(api) -> str:
    return call(api.port, "GET", "/api/discovery/status")[1]["state"]


def trail(api, **query) -> list:
    path = "/api/audit" + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")
    status, body = call(api.port, "GET", path)
    assert status == 200
    return body["records"]


def test_the_backend_starts_on_the_default_project_and_records_its_start(api):
    status, listing = call(api.port, "GET", "/api/projects")
    assert status == 200 and listing["active"] == "default" and listing["persistent"] is True
    assert [p["id"] for p in listing["projects"]] == ["default"] and listing["version"] >= 1
    started = trail(api, action="system")[0]
    assert started["action"] == "system.started" and started["actor"]["kind"] == "system"
    assert started["details"]["persistent"] is True and started["project"] == "default"
    assert discovered(api) == "completed"


def test_each_project_holds_its_own_discovery_and_switching_back_brings_it_back(api):
    version = call(api.port, "GET", "/api/projects")[1]["version"]
    status, created = call(api.port, "POST", "/api/projects", {"name": "Finance", "id": "pfinance", "version": version,
                                                                "activate": True},
                           headers={"X-Correlation-Id": "switch-0001"})
    assert status == 200 and created["active"] == "pfinance" and created["created"] == "pfinance"
    assert discovered(api) == "idle"
    assert call(api.port, "GET", "/api/migration/run")[1]["state"] == "idle"
    switched = trail(api, action="project.activated")[0]
    assert switched["actor"] == {"kind": "person", "id": "Test Operator"} and switched["correlationId"] == "switch-0001"
    assert switched["project"] == "pfinance" and switched["details"] == {"name": "Finance", "previous": "default"}
    assert [r["action"] for r in trail(api)][-1] == "project.created"  # the new project's trail starts with its creation

    status, listing = call(api.port, "POST", "/api/projects/default/activate", {})
    assert status == 200 and listing["active"] == "default" and discovered(api) == "completed"
    assert api.projects.active_id() == "default"


def test_a_project_can_carry_on_the_discovery_another_one_saved(api):
    status, created = call(api.port, "POST", "/api/projects", {"name": "From this browser", "id": "pbrowser",
                                                                "copyFrom": "default", "activate": True})
    assert status == 200 and created["active"] == "pbrowser" and discovered(api) == "completed"
    assert trail(api, action="project.created")[0]["details"]["adopted"] == "default"


def test_a_change_made_from_an_older_list_is_refused(api):
    version = call(api.port, "GET", "/api/projects")[1]["version"]
    assert call(api.port, "POST", "/api/projects", {"name": "A", "id": "pa", "version": version})[0] == 200
    status, body = call(api.port, "POST", "/api/projects/pa/rename", {"name": "B", "version": version})
    assert status == 409 and body["error"]["code"] == "stale"
    status, body = call(api.port, "POST", "/api/projects/pa/rename", {"name": "  Better   name "})
    assert status == 200 and next(p for p in body["projects"] if p["id"] == "pa")["name"] == "Better name"
    # Recorded in the renamed project's own trail, which another project's page can read.
    renamed = trail(api, action="project.renamed", project="pa")[0]
    assert renamed["details"] == {"name": "Better name", "previous": "A"} and renamed["project"] == "pa"
    assert trail(api, action="project.renamed") == []
    assert call(api.port, "POST", "/api/projects/nope/activate", {})[0] == 404
    assert call(api.port, "POST", "/api/projects", {"name": "X", "unexpected": 1})[0] == 400


def test_a_project_is_archived_never_deleted_and_never_while_active(api):
    call(api.port, "POST", "/api/projects", {"name": "Old", "id": "pold", "activate": True})
    status, body = call(api.port, "DELETE", "/api/projects/pold")
    assert status == 409 and body["error"]["code"] == "project_active"
    call(api.port, "POST", "/api/projects/default/activate", {})
    status, body = call(api.port, "DELETE", "/api/projects/pold")
    assert status == 200 and [p["id"] for p in body["projects"]] == ["default"]
    archived = list((api.root / "archive").iterdir())
    assert len(archived) == 1 and archived[0].name.startswith("pold-") and (archived[0] / "audit.jsonl").exists()
    assert trail(api, action="project.removed")[0]["details"]["name"] == "Old"


def test_switching_is_refused_while_the_project_left_is_still_working(api, monkeypatch):
    call(api.port, "POST", "/api/projects", {"name": "Other", "id": "pother"})
    monkeypatch.setattr(api.hub._migration, "busy_reason", lambda: "A migration run is working.")  # noqa: SLF001
    status, body = call(api.port, "POST", "/api/projects/pother/activate", {})
    assert status == 409 and body["error"]["code"] == "project_busy" and "migration run" in body["error"]["message"]
    assert call(api.port, "GET", "/api/projects")[1]["active"] == "default" and discovered(api) == "completed"


def test_a_switch_that_fails_half_way_is_undone_everywhere(api, monkeypatch):
    call(api.port, "POST", "/api/projects", {"name": "Other", "id": "pother"})

    def refuse(records):
        raise ApiError(409, "migration_busy", "A run started in the meantime.")

    real = api.hub._migration.switch  # noqa: SLF001
    calls = []
    monkeypatch.setattr(api.hub._migration, "switch", lambda records: (calls.append(records), refuse(records) if len(calls) == 1 else real(records)))  # noqa: SLF001
    status, body = call(api.port, "POST", "/api/projects/pother/activate", {})
    assert status == 409 and body["error"]["code"] == "migration_busy"
    assert call(api.port, "GET", "/api/projects")[1]["active"] == "default"
    assert api.hub.active_id() == "default" and discovered(api) == "completed"


def test_the_trail_is_filtered_verified_and_exported(api):
    call(api.port, "POST", "/api/journey/confirm", {"step": "plan", "note": "Reviewed   with the team"})
    status, body = call(api.port, "POST", "/api/journey/confirm", {"step": "elsewhere"})
    assert status == 400
    confirmed = trail(api, action="journey.step_confirmed")[0]
    assert confirmed["details"] == {"step": "plan", "note": "Reviewed with the team"} and confirmed["actor"]["kind"] == "person"
    assert [r["action"] for r in trail(api, actor="system")] == ["system.started"]

    status, verified = call(api.port, "GET", "/api/audit/verify")
    assert status == 200 and verified["ok"] is True and verified["records"] >= 2

    conn = HTTPConnection("127.0.0.1", api.port, timeout=5)
    conn.request("GET", "/api/audit/export?format=csv")
    response = conn.getresponse()
    text = response.read().decode("utf-8-sig")
    conn.close()
    assert response.status == 200 and response.getheader("Content-Type") == "text/csv"
    assert 'attachment; filename="audit-default.csv"' == response.getheader("Content-Disposition")
    assert {row["action"] for row in csv.DictReader(io.StringIO(text))} >= {"system.started", "journey.step_confirmed"}
    assert trail(api, action="audit.exported")[0]["details"] == {"format": "csv", "records": 2}

    conn = HTTPConnection("127.0.0.1", api.port, timeout=5)
    conn.request("GET", "/api/audit/export?format=jsonl")
    lines = conn.getresponse().read().decode("utf-8").splitlines()
    conn.close()
    assert all(json.loads(line)["hash"] for line in lines)
    assert call(api.port, "GET", "/api/audit/export?format=xml")[0] == 400
    assert call(api.port, "GET", "/api/audit?project=nope")[0] == 404


def test_the_run_history_routes_answer_before_any_run(api):
    status, policy = call(api.port, "GET", "/api/migration/policy")
    assert status == 200 and "awaiting_approval" in policy["statuses"] and policy["signoff"] == "never"
    assert set(policy["terminal"]) == {"completed", "cancelled", "rejected"} and "TRANSIENT" in policy["failureCategories"]
    assert call(api.port, "GET", "/api/migration/runs")[1] == {"runs": [], "current": None}
    assert call(api.port, "GET", "/api/migration/runs/001")[0] == 404
    assert call(api.port, "GET", "/api/migration/runs/001/events")[0] == 404
    assert call(api.port, "GET", "/api/migration/runs/001/artifacts/" + "0" * 64)[0] == 404
    assert call(api.port, "GET", "/api/migration/plans/P001")[0] == 404
    status, body = call(api.port, "POST", "/api/migration/approve", {"decision": "approve"})
    assert status == 409 and body["error"]["code"] == "no_run"


def test_a_switch_waiting_on_a_busy_service_never_deadlocks_with_that_services_audit_record(api):
    """A service records actions while it holds its own lock, and a switch asks that service whether it is busy:
    neither may wait for the other."""
    import time

    call(api.port, "POST", "/api/projects", {"name": "Other", "id": "pother"})
    migration = api.hub._migration  # noqa: SLF001
    holding, release = threading.Event(), threading.Event()

    def records_under_its_lock():
        with migration._lock:  # noqa: SLF001 - as control() does: the service's lock held while it records
            holding.set()
            release.wait(5)
            api.hub.audit("run.pause_requested", target={"type": "run", "id": "001"})

    worker = threading.Thread(target=records_under_its_lock, daemon=True)
    worker.start()
    assert holding.wait(5)
    switcher = threading.Thread(target=lambda: api.hub.activate("pother", {}), daemon=True)
    switcher.start()
    time.sleep(0.3)  # the switch now waits for the service, which is about to record
    release.set()
    worker.join(5)
    switcher.join(10)
    assert not worker.is_alive() and not switcher.is_alive()
    assert api.hub.active_id() == "pother"


def test_a_record_goes_to_the_trail_of_the_project_its_work_belongs_to(api):
    """Work started in one project and finishing after a switch is recorded in that project, not the next one."""
    from discovery_agent.state import context

    call(api.port, "POST", "/api/projects", {"name": "Other", "id": "pother", "activate": True})
    with context.bound(project="default", actor=context.system()):
        api.hub.audit("fabric.connected", target={"type": "workspace", "id": "w"}, details={"restored": True})
    with context.bound(project="gone-or-never-was", actor=context.system()):
        api.hub.audit("system.stopped", details={"pid": 1})
    assert trail(api, action="fabric.connected", project="default")[0]["details"] == {"restored": True}
    assert trail(api, action="fabric.connected") == []  # the active project's trail
    assert [r["action"] for r in trail(api, action="system")] == ["system.stopped"]  # unknown project: the active one


# -- found in review ----------------------------------------------------------------------------------------------

def test_a_switch_runs_alone_waiting_for_requests_being_answered_and_holding_new_ones():
    import time

    from discovery_agent.api.projects import RequestGate

    gate = RequestGate()
    order = []
    answering, release = threading.Event(), threading.Event()

    def slow_request():
        with gate.request():
            answering.set()
            release.wait(5)
            order.append("request")

    def switch():
        with gate.alone(5):
            order.append("switch")

    def late_request():
        with gate.request():
            order.append("late request")

    first = threading.Thread(target=slow_request, daemon=True)
    first.start()
    assert answering.wait(5)
    switcher = threading.Thread(target=switch, daemon=True)
    switcher.start()
    time.sleep(0.2)
    late = threading.Thread(target=late_request, daemon=True)
    late.start()
    time.sleep(0.2)
    assert order == []  # the switch waits for the request being answered; the new request waits for the switch
    release.set()
    for t in (first, switcher, late):
        t.join(5)
    assert order == ["request", "switch", "late request"]

    with gate.request():  # a switch asked for from a request does not wait for that request itself
        with gate.alone(0.5):
            order.append("from a request")
    assert order[-1] == "from a request"


def test_a_switch_that_cannot_run_alone_in_time_is_refused_and_lets_requests_on(api):
    holding, release = threading.Event(), threading.Event()

    def held():
        with api.hub.gate.request():  # a request still being answered: a sign-in window, say
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=held, daemon=True)
    worker.start()
    assert holding.wait(5)
    call(api.port, "POST", "/api/projects", {"name": "Other", "id": "pother"})  # requests go on while none switches
    api.hub.switch_wait = 0.3
    status, body = call(api.port, "POST", "/api/projects/pother/activate", {})
    assert status == 409 and body["error"]["code"] == "project_busy" and "still being answered" in body["error"]["message"]
    assert call(api.port, "GET", "/api/projects")[1]["active"] == "default"  # the door is open again, nothing switched
    release.set()
    worker.join(5)
    assert call(api.port, "POST", "/api/projects/pother/activate", {})[1]["active"] == "pother"


def test_a_switch_that_fails_undoes_only_the_services_that_had_switched(api, monkeypatch):
    call(api.port, "POST", "/api/projects", {"name": "Other", "id": "pother"})
    touched = []

    def refuse(store, records):
        raise ApiError(409, "session_busy", "Discovery started in the meantime.")

    monkeypatch.setattr(api.hub._session, "switch", refuse)  # noqa: SLF001
    monkeypatch.setattr(api.hub._fabric, "switch", lambda store: touched.append("fabric"))  # noqa: SLF001
    monkeypatch.setattr(api.hub._migration, "switch", lambda records: touched.append("migration"))  # noqa: SLF001
    status, body = call(api.port, "POST", "/api/projects/pother/activate", {})
    assert status == 409 and body["error"]["code"] == "session_busy"
    assert touched == []  # a sign-in waiting in a window, and credentials in memory, were left alone
    assert call(api.port, "GET", "/api/projects")[1]["active"] == "default" and api.hub.active_id() == "default"
