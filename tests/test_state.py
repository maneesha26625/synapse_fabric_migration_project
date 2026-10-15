"""The state layer: a project's store, process identity, the projects, the run vocabulary, failure classes,
sign-off, and the audit trail. Offline; every file is under the test's own folder."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from discovery_agent.migration import failures, run_state
from discovery_agent.state import approvals, audit, context, processes
from discovery_agent.state.projects import DEFAULT_ID, LEGACY_FILES, ProjectError, Projects
from discovery_agent.state.store import LeaseHeld, StaleWrite, Store

PERSON = context.Actor(context.PERSON, "Asha Rao")


# -- the store: documents ------------------------------------------------------------------------------------

def test_a_document_keeps_its_version_and_refuses_a_write_from_an_older_read(tmp_path):
    store = Store(tmp_path)
    assert store.get("plan") is None and store.version("plan") == 0
    assert store.put("plan", {"a": 1}) == 1
    assert store.put("plan", {"a": 2}, expected=1) == 2
    with pytest.raises(StaleWrite):
        store.put("plan", {"a": 3}, expected=1)
    assert store.get("plan") == {"a": 2}
    saved = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert saved["schema"] == 1 and saved["version"] == 2 and saved["savedAt"]
    assert not list(tmp_path.glob("*.tmp"))  # written whole, then renamed into place


def test_a_name_can_never_reach_outside_the_store(tmp_path):
    store = Store(tmp_path)
    for bad in ("../x", "a/../b", "/abs", "a\\b", "", "a/b/c/d/e/f", "-x"):
        with pytest.raises(ValueError):
            store.put(bad, 1)
    assert not (tmp_path.parent / "x.json").exists()


# -- the store: streams ---------------------------------------------------------------------------------------

def test_a_stream_numbers_its_records_without_gaps_across_restarts_and_skips_a_line_a_crash_cut_short(tmp_path):
    store = Store(tmp_path)
    assert [store.append("s", {"n": n})["seq"] for n in range(3)] == [1, 2, 3]
    with open(tmp_path / "s.jsonl", "ab") as handle:
        handle.write(b'{"seq": 4, "n": "cut sh')  # the process died in the middle of this line
    again = Store(tmp_path)  # a restart
    assert [r["n"] for r in again.records("s")] == [0, 1, 2]
    assert again.append("s", {"n": 3})["seq"] == 4
    assert [r["seq"] for r in again.records("s")] == [1, 2, 3, 4]
    assert [r["seq"] for r in again.records("s", after=2)] == [3, 4]
    assert [r["seq"] for r in again.records("s", newest_first=True, limit=2)] == [4, 3]
    assert again.seq("s") == 4 and again.last("s")["n"] == 3


def test_blobs_are_kept_once_by_their_hash(tmp_path):
    store = Store(tmp_path)
    digest = store.put_blob(b"CREATE TABLE t (a int)")
    assert len(digest) == 64 and store.put_blob(b"CREATE TABLE t (a int)") == digest
    assert store.blob(digest) == b"CREATE TABLE t (a int)"
    assert store.blob("../../etc/passwd") is None and store.blob("0" * 64) is None


# -- the store: leases, and what names a process -----------------------------------------------------------

def _ended_process() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _write_lease(folder: Path, key: str, **lease) -> None:
    (folder / "leases").mkdir(parents=True, exist_ok=True)
    (folder / "leases" / f"{key}.json").write_text(json.dumps({"key": key, **lease}), encoding="utf-8")


def test_a_process_is_named_by_its_id_and_when_it_started():
    me = os.getpid()
    assert processes.alive(me) and processes.same(me, processes.SELF_STARTED)
    assert not processes.alive(0) and not processes.same(-1, None)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = processes.started(child.pid)
        assert processes.same(child.pid, started)
        if started is not None:
            assert not processes.same(child.pid, started + 1)  # the same id, another start: another process
    finally:
        child.kill()
        child.wait()
    assert not processes.alive(child.pid)


def test_a_lease_is_refused_to_another_owner_while_its_holder_lives(tmp_path):
    store = Store(tmp_path)
    lease = store.acquire("run", "run 001")
    assert lease["pid"] == os.getpid() and lease["reclaimed"] is None
    assert store.acquire("run", "run 001")["reclaimed"] is None  # the same owner may take it again
    with pytest.raises(LeaseHeld):
        store.acquire("run", "run 002")
    store.heartbeat("run", "run 001")
    assert not store.release("run", "run 002") and store.release("run", "run 001")
    assert store.holder("run") is None


def test_a_lease_left_by_a_process_that_ended_is_taken_over_and_the_caller_is_told(tmp_path):
    store = Store(tmp_path)
    _write_lease(tmp_path, "run", owner="run 009", pid=_ended_process(), started=None)
    taken = store.acquire("run", "run 010")
    assert taken["reclaimed"]["owner"] == "run 009" and taken["owner"] == "run 010"


def test_a_lease_whose_process_id_now_belongs_to_another_process_is_taken_over(tmp_path):
    """Ids are reused once a process ends: a living process with the id but another start time holds nothing."""
    parent = os.getppid()
    started = processes.started(parent)
    if started is None:
        pytest.skip("this system cannot tell when a process started")
    _write_lease(tmp_path, "process", owner="API on :8001", pid=parent, started=started + 1)
    assert Store(tmp_path).acquire("process", "API on :8011")["reclaimed"]["pid"] == parent
    _write_lease(tmp_path, "process", owner="API on :8001", pid=parent, started=started)
    with pytest.raises(LeaseHeld):
        Store(tmp_path).acquire("process", "API on :8011")


# -- projects ---------------------------------------------------------------------------------------------------

def test_the_first_start_adopts_what_the_single_project_code_saved_and_leaves_the_originals(tmp_path):
    for name in LEGACY_FILES:
        (tmp_path / name).write_bytes(b"saved " + name.encode())
    projects = Projects(tmp_path)
    listing = projects.listing()
    assert listing["active"] == DEFAULT_ID and [p["id"] for p in listing["projects"]] == [DEFAULT_ID]
    for name in LEGACY_FILES:
        assert (projects.folder(DEFAULT_ID) / name).read_bytes() == b"saved " + name.encode()
        assert (tmp_path / name).read_bytes() == b"saved " + name.encode()  # copied, never moved
    (projects.folder(DEFAULT_ID) / LEGACY_FILES[0]).write_bytes(b"newer")
    Projects(tmp_path)  # a second start adopts nothing again
    assert (projects.folder(DEFAULT_ID) / LEGACY_FILES[0]).read_bytes() == b"newer"


def test_projects_are_created_renamed_switched_and_archived_never_deleted(tmp_path):
    projects = Projects(tmp_path)
    read = projects.listing()["version"]
    assert projects.create("Finance", "pfinance", expected=read)["created"] == "pfinance"
    with pytest.raises(ProjectError) as stale:
        projects.rename("pfinance", "From an older page", expected=read)
    assert stale.value.code == "stale" and stale.value.status == 409
    projects.rename("pfinance", "  Finance   2026 ")
    assert projects.get("pfinance")["name"] == "Finance 2026"
    projects.activate("pfinance")
    assert projects.active_id() == "pfinance"
    with pytest.raises(ProjectError) as active:
        projects.remove("pfinance")
    assert active.value.code == "project_active"
    projects.store("pfinance").append("audit", {"action": "kept"})
    projects.activate(DEFAULT_ID)
    projects.remove("pfinance")
    assert [p["id"] for p in projects.listing()["projects"]] == [DEFAULT_ID]
    archived = list((tmp_path / "archive").iterdir())
    assert len(archived) == 1 and (archived[0] / "audit.jsonl").exists()
    with pytest.raises(ProjectError) as last:
        projects.remove(DEFAULT_ID)
    assert last.value.code == "last_project"


@pytest.mark.parametrize("name,ident,code", [("", None, "invalid_request"), ("x" * 81, None, "invalid_request"),
                                              ("Fine", "../evil", "invalid_request"), ("Twice", DEFAULT_ID, "project_exists")])
def test_a_project_needs_a_short_name_and_a_plain_unused_id(tmp_path, name, ident, code):
    with pytest.raises(ProjectError) as refused:
        Projects(tmp_path).create(name, ident)
    assert refused.value.code == code


def test_only_one_api_process_writes_a_state_folder(tmp_path):
    projects = Projects(tmp_path)
    projects.claim_process("API on :8001")
    parent = os.getppid()  # a living process, standing in for a second API that holds the folder
    _write_lease(tmp_path, "process", owner="API on :8001", pid=parent, started=processes.started(parent))
    with pytest.raises(ProjectError) as taken:
        Projects(tmp_path).claim_process("API on :8011")
    assert taken.value.code == "state_in_use" and "MIGRATION_STATE_DIR" in taken.value.message
    _write_lease(tmp_path, "process", owner="API on :8001", pid=_ended_process(), started=None)
    assert Projects(tmp_path).claim_process("API on :8011")["reclaimed"]["owner"] == "API on :8001"


def test_with_the_state_off_projects_live_in_a_folder_of_the_process_own(monkeypatch):
    monkeypatch.setenv("MIGRATION_STATE", "off")
    projects = Projects()
    assert not projects.persistent and projects.root.name.startswith("migration-state-")
    assert projects.root != Path(os.environ["MIGRATION_STATE_DIR"])


# -- the run vocabulary -------------------------------------------------------------------------------------------

def test_a_finished_run_has_no_way_out_and_every_move_is_checked():
    assert run_state.TERMINAL == {run_state.COMPLETED, run_state.CANCELLED, run_state.REJECTED}
    for status in run_state.TERMINAL:
        for target in run_state.STATUSES:
            with pytest.raises(run_state.IllegalTransition):
                run_state.check_run(status, target)
    run_state.check_run(run_state.RUNNING, run_state.PAUSED)
    with pytest.raises(run_state.IllegalTransition):
        run_state.check_run(run_state.RUNNING, run_state.CANCELLED)  # a working run is paused before it is closed
    run_state.check_item(run_state.ITEM_FAILED, run_state.PENDING)
    run_state.check_item(run_state.PENDING, run_state.BLOCKED)
    with pytest.raises(run_state.IllegalTransition):
        run_state.check_item(run_state.DONE, run_state.PENDING)
    assert run_state.legacy_state(run_state.AWAITING_APPROVAL) == "paused"
    assert run_state.legacy_state(run_state.QUEUED) == "running"
    assert run_state.legacy_state(run_state.COMPLETED_WITH_FAILURES) == "completed"
    described = run_state.describe()
    assert set(described["transitions"]) == set(run_state.STATUSES) and described["needsAttention"] == ["BLOCKED", "FAILED"]


@pytest.mark.parametrize("message,code,status,expected", [
    ("The workspace capacity is paused", None, None, failures.CAPACITY),
    ("x", "CapacityNotActive", 400, failures.CAPACITY),
    ("Forbidden", "Unauthorized", 401, failures.AUTH),
    ("The sign in timed out", None, None, failures.AUTH),  # sign-in comes before transient
    ("Enter credentials for LS_Sql in Plan", None, None, failures.CONFIG),
    ("x", "ItemDisplayNameAlreadyInUse", 400, failures.CONFLICT),
    ("Run the Warehouse & schema stage first", None, None, failures.DEPENDENCY),
    ("Fabric Warehouse rejected the create view: syntax", None, None, failures.UNSUPPORTED),
    ("Service unavailable", None, 503, failures.TRANSIENT),
    ("The Fabric API could not be reached.", "fabric_unreachable", 0, failures.TRANSIENT),
    ("The accelerator stopped while it was being created", None, None, failures.INTERRUPTED),
    ("Something nobody has seen before", None, None, failures.UNKNOWN),
])
def test_every_object_error_falls_in_one_class_and_only_a_transient_one_is_retried(message, code, status, expected):
    assert failures.classify(message, code, status) == expected
    assert (expected in failures.RETRYABLE) == (expected == failures.TRANSIENT)


# -- sign-off -----------------------------------------------------------------------------------------------------

def test_signoff_is_asked_only_for_runs_that_change_what_fabric_already_has(monkeypatch):
    monkeypatch.setenv("MIGRATION_SIGNOFF", "overwrite")
    assert approvals.required({"existing": "skip", "dataMode": "if_empty"}, ["notebooks", "data"]) is None
    assert "updates objects that are already in Fabric" in approvals.required({"existing": "update"}, [])
    assert "empties tables" in approvals.required({"dataMode": "replace"}, ["data"])
    assert approvals.required({"dataMode": "replace"}, ["notebooks"]) is None  # without the data stage nothing is emptied
    monkeypatch.setenv("MIGRATION_SIGNOFF", "always")
    assert approvals.required({}, [])
    monkeypatch.setenv("MIGRATION_SIGNOFF", "never")
    assert approvals.required({"existing": "update"}, []) is None
    monkeypatch.setenv("MIGRATION_SIGNOFF", "whatever")
    assert approvals.policy() == approvals.OVERWRITE


def test_an_approval_the_accelerator_gives_is_its_own_and_only_a_person_decides_one_that_is_asked_for():
    digest = approvals.subject_hash([{"id": "a", "wave": 1}], {"existing": "update"}, ["notebooks"])
    automatic = approvals.request(None, digest)
    assert automatic["decision"] == "approved" and automatic["automatic"] is True
    assert automatic["by"] == {"kind": "system", "id": "accelerator"}
    waiting = approvals.request("it updates objects", digest)
    assert waiting["decision"] is None and waiting["required"] is True
    with pytest.raises(PermissionError):
        approvals.decide(waiting, True)  # outside a request it is the accelerator, which never signs off
    with context.bound(actor=PERSON):
        decided = approvals.decide(waiting, True, "  looks   right ")
    assert decided["by"] == {"kind": "person", "id": "Asha Rao"} and decided["comment"] == "looks right"
    assert decided["decision"] == "approved" and decided["automatic"] is False
    assert approvals.subject_hash([{"id": "a", "wave": 2}], {"existing": "update"}, ["notebooks"]) != digest


# -- the audit trail ----------------------------------------------------------------------------------------------

def test_a_record_keeps_only_what_its_action_allows_with_secrets_masked_and_its_context(tmp_path, caplog):
    trail = audit.Auditor(Store(tmp_path))
    with context.bound(correlation="corr-12345678", actor=PERSON, project="default"):
        record = trail.record("run.requested", target={"type": "run", "id": "001"}, run_id="001", details={
            "objects": 3, "workspace": "Password=hunter2;", "credentials": {"LS": {"password": "hunter3"}}})
    assert record["details"] == {"objects": 3, "workspace": "[redacted]"}
    written = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "hunter2" not in written and "hunter3" not in written
    assert record["actor"] == {"kind": "person", "id": "Asha Rao"} and record["correlationId"] == "corr-12345678"
    assert record["project"] == "default" and record["run"] == "001" and record["outcome"] == "done"
    assert record["seq"] == 1 and record["prev"] == audit.GENESIS and record["hash"] == audit.digest(record)
    assert "left out field(s) it does not allow: credentials" in caplog.text


def test_outside_a_request_the_actor_is_the_accelerator(tmp_path):
    record = audit.Auditor(Store(tmp_path)).record("system.started", details={"pid": 1})
    assert record["actor"] == {"kind": "system", "id": "accelerator"}


def test_an_action_outside_the_catalog_is_recorded_without_details_and_logged(tmp_path, caplog):
    record = audit.Auditor(Store(tmp_path)).record("made.up", details={"anything": "x"})
    assert record["action"] == "made.up" and record["details"] == {}
    assert "not in the catalog" in caplog.text


def test_the_chain_shows_a_record_that_was_changed_removed_or_moved(tmp_path):
    trail = audit.Auditor(Store(tmp_path))
    for n in range(4):
        trail.record("journey.step_confirmed", details={"step": f"s{n}"})
    assert trail.verify() == {"ok": True, "records": 4, "brokenAt": None, "problem": None, "head": trail.records()[-1]["hash"]}
    path = tmp_path / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()

    def verify(changed):
        path.write_text("\n".join(changed) + "\n", encoding="utf-8")
        return audit.Auditor(Store(tmp_path)).verify()

    edited = verify([lines[0], lines[1].replace('"s1"', '"s9"'), *lines[2:]])
    assert not edited["ok"] and edited["brokenAt"] == 2 and "changed" in edited["problem"]
    removed = verify([lines[0], *lines[2:]])
    assert not removed["ok"] and removed["brokenAt"] == 3
    moved = verify([lines[1], lines[0], *lines[2:]])
    assert not moved["ok"]
    assert verify(lines)["ok"]


def test_records_written_after_a_restart_carry_the_chain_on(tmp_path):
    audit.Auditor(Store(tmp_path)).record("system.started")
    later = audit.Auditor(Store(tmp_path))  # a new process
    later.record("system.stopped")
    assert later.verify()["ok"] and later.verify()["records"] == 2


def test_the_trail_reads_newest_first_with_filters_and_pages_back(tmp_path):
    trail = audit.Auditor(Store(tmp_path))
    trail.record("system.started")
    with context.bound(actor=PERSON):
        for n in range(3):
            trail.record("run.requested", target={"type": "run", "id": f"00{n}"}, run_id=f"00{n}")
        trail.record("run.refused", outcome="refused", code="plan_blocked")
    page = trail.query(limit=2)
    assert [r["seq"] for r in page["records"]] == [5, 4] and page["next"] == 4 and page["matched"] == 5
    assert [r["seq"] for r in trail.query(before=page["next"], limit=2)["records"]] == [3, 2]
    assert [r["action"] for r in trail.query(action="run")["records"]] == ["run.refused"] + ["run.requested"] * 3
    assert [r["seq"] for r in trail.query(actor="system")["records"]] == [1]
    assert [r["seq"] for r in trail.query(actor="person:Asha Rao", target="run:001")["records"]] == [3]
    assert [r["seq"] for r in trail.query(outcome="refused")["records"]] == [5]
    assert trail.query(limit=1)["catalog"]["run.refused"]


def test_an_export_can_be_verified_elsewhere_and_a_csv_cell_never_becomes_a_formula(tmp_path):
    trail = audit.Auditor(Store(tmp_path))
    trail.record("run.refused", outcome="refused", reason="=HYPERLINK(\"http://example.invalid\")")
    body, media, count = trail.export("jsonl")
    assert media == "application/x-ndjson" and count == 1
    assert audit.verify([json.loads(line) for line in body.decode("utf-8").splitlines()])["ok"]
    text, media, _ = trail.export("csv")
    rows = list(csv.DictReader(io.StringIO(text.decode("utf-8-sig"))))
    assert media == "text/csv" and rows[0]["action"] == "run.refused" and rows[0]["reason"].startswith("'=")
    with pytest.raises(ValueError):
        trail.export("xml")


def test_recording_never_fails_what_it_records(tmp_path, caplog):
    class Full(Store):
        def append(self, stream, record):
            raise OSError("disk full")

    assert audit.Auditor(Full(tmp_path)).record("system.started") is None
    assert "disk full" in caplog.text
    assert audit.Auditor(None).record("system.started") is None


def test_every_action_the_code_records_is_in_the_catalog_and_every_catalogued_one_is_recorded():
    root = Path(audit.__file__).resolve().parents[1]
    used = set()
    for path in root.rglob("*.py"):
        used |= set(re.findall(r"""(?:_audit|\.audit|\.record)\(\s*["']([a-z]+\.[a-z_]+)["']""", path.read_text(encoding="utf-8")))
    assert used - set(audit.CATALOG) == set()
    assert set(audit.CATALOG) - used == set()


# -- found in review ------------------------------------------------------------------------------------------------

def test_a_line_cut_short_inside_a_character_costs_that_line_only(tmp_path):
    """A crash can cut a line inside a character that takes several bytes ("…"): the stream still reads and grows."""
    store = Store(tmp_path)
    store.append("events", {"message": "Committing…"})
    torn = json.dumps({"seq": 2, "message": "Loaded … rows"}, ensure_ascii=False).encode("utf-8")
    with open(tmp_path / "events.jsonl", "ab") as handle:
        handle.write(torn[:torn.index("…".encode("utf-8")) + 1])  # the first byte of "…" only
    again = Store(tmp_path)
    assert [r["message"] for r in again.records("events")] == ["Committing…"]
    assert again.append("events", {"message": "after"})["seq"] == 2
    trail = audit.Auditor(again)
    assert trail.record("system.started") is not None


def test_the_run_history_lists_runs_by_number_and_ignores_anything_else_in_the_folder(tmp_path):
    from discovery_agent.state.runs import RunRecorder

    store = Store(tmp_path)
    recorder = RunRecorder(store)
    for run_id in ("999", "1000", "001"):
        recorder.save_run({"runId": run_id, "status": "completed"})
    (tmp_path / "runs" / "not a run").mkdir()
    (tmp_path / "runs" / "..hidden").mkdir()
    assert [r["runId"] for r in recorder.runs()] == ["1000", "999", "001"]
    assert recorder.highest() == 1000


def test_project_ids_are_one_whatever_their_case(tmp_path):
    """On Windows `Default` and `default` are one folder: two projects there would corrupt each other."""
    projects = Projects(tmp_path)
    with pytest.raises(ProjectError) as twice:
        projects.create("Shadow", "Default")
    assert twice.value.code == "project_exists"
    assert projects.create("Mixed", "PMixed")["created"] == "pmixed"
