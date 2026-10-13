"""Validation's second half: does each object run in Fabric? Offline.

The Warehouse is a catalog fake that can reject chosen statements; Fabric is a fake with run
histories and on-demand jobs that finish (or fail, or never finish) on the fake clock.
"""

from __future__ import annotations

import time

from discovery_agent.api.migration import MigrationService
from discovery_agent.migration import runtime_checks as rc
from discovery_agent.migration import validation as v
from discovery_agent.migration.common import NOTEBOOK, PIPELINE, PROCEDURE, Source, VIEW
from discovery_agent.sql.models import (
    CatalogObjectType, DefinitionState, SqlModuleDefinition, SqlObjectKey, SqlParameter, SqlProcedure, SqlView,
)

from test_migration import WS, FakeSession, fake_job
from test_validation import NB, CatalogCursor, CatalogDb, ValRest, b64, fabric, plain_table, tbl


class ProbeDb(CatalogDb):
    """A catalog that rejects any statement containing one of ``reject``'s keys, with its message."""

    def __init__(self, *a, reject=None, **kw):
        super().__init__(*a, **kw)
        self.reject = reject or {}
        self.statements = []

    def cursor(self):
        return ProbeCursor(self)


class ProbeCursor(CatalogCursor):
    def execute(self, sql, *params):
        self.db.statements.append(sql)
        for needle, message in self.db.reject.items():
            if needle in sql:
                raise RuntimeError(f"[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]{message}")
        super().execute(sql, *params)


class JobRest(ValRest):
    """Fabric with run histories, and on-demand runs that end with ``outcome`` after ``polls`` polls."""

    def __init__(self, history=None, outcome="Completed", polls=1, refuse=False, **seed):
        super().__init__(**seed)
        self.history = history or {}
        self.outcome, self.polls, self.refuse = outcome, polls, refuse
        self.started, self.seen = [], {}

    def list(self, path):
        if path.endswith("/jobs/instances"):
            return list(self.history.get(path.split("/")[4], []))
        return super().list(path)

    def run_job(self, path, body=None):
        if self.refuse:
            raise rc.FabricApiError(403, "Forbidden", "Not allowed to run this item.")
        self.started.append(path)
        item = path.split("/")[4]
        return f"/workspaces/{WS}/items/{item}/jobs/instances/run-{len(self.started)}"

    def get(self, path):
        if "/jobs/instances/" in path:
            self.seen[path] = self.seen.get(path, 0) + 1
            if self.seen[path] < self.polls:
                return {"status": "InProgress"}
            reason = {"message": "Notebook cell 2 raised NameError"} if self.outcome == "Failed" else None
            return {"status": self.outcome, "endTimeUtc": "2026-10-13T10:00:00Z", "failureReason": reason}
        return super().get(path)


def checker(rest=None, db=None, runtime=None, clock=None):
    rest = rest or JobRest()
    db = db or ProbeDb()
    return v.Validator(rest, WS, "Sales WS", "pool01", lambda h, n: db, None, None,
                       runtime=runtime or rc.RuntimeOptions(), sleep=lambda s: None, clock=clock)


def view_source():
    view = SqlView(key=SqlObjectKey("pool", "sales", "vO", CatalogObjectType.VIEW),
                   definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text="CREATE VIEW sales.vO AS SELECT 1"))
    return Source("v", "sales.vO", "View", 3, VIEW, payload=view, schema="sales", object_name="vO")


def proc_source(params=()):
    proc = SqlProcedure(key=SqlObjectKey("pool", "sales", "pLoad", CatalogObjectType.PROCEDURE), parameters=tuple(params),
                        definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text="CREATE PROCEDURE sales.pLoad AS SELECT 1"))
    return Source("p", "sales.pLoad", "Stored Procedure", 3, PROCEDURE, payload=proc, schema="sales", object_name="pLoad")


MODULES = {("sales", "vO"): ("V", "CREATE VIEW sales.vO AS SELECT 1"), ("sales", "pLoad"): ("P", "CREATE PROCEDURE sales.pLoad AS SELECT 1")}
RUN_PROCS = rc.RuntimeOptions(run=frozenset({PROCEDURE}))
RUN_NOTEBOOKS = rc.RuntimeOptions(run=frozenset({NOTEBOOK}))


# -- the Warehouse --------------------------------------------------------------------------------------

def test_a_view_that_queries_passes_and_one_the_warehouse_rejects_is_a_mismatch():
    ok = checker(db=ProbeDb(modules=MODULES)).run([view_source()])[0]
    assert ok["status"] == v.MATCH and ok["runtime"]["status"] == rc.PASS
    assert "SELECT TOP (1) * FROM [sales].[vO]" in ok["runtime"]["detail"]
    bad = checker(db=ProbeDb(modules=MODULES, reject={"FROM [sales].[vO]": "Invalid object name 'sales.Orders'."})).run([view_source()])[0]
    assert bad["status"] == v.MISMATCH and bad["runtime"]["status"] == rc.FAIL
    assert bad["runtime"]["detail"].endswith("Invalid object name 'sales.Orders'.") and "ODBC" not in bad["runtime"]["detail"]


def test_a_table_is_queried_and_a_missing_one_gets_no_run_check():
    t = plain_table()
    ok = checker(db=ProbeDb({("sales", "Orders"): t})).run([tbl(t)])[0]
    assert ok["runtime"]["status"] == rc.PASS
    assert "runtime" not in checker(db=ProbeDb()).run([tbl(t)])[0]


def test_a_procedure_is_compiled_not_executed_by_default():
    db = ProbeDb(modules=MODULES)
    r = checker(db=db).run([proc_source()])[0]
    assert r["runtime"]["status"] == rc.PASS and r["runtime"]["check"] == "Compiled"
    assert any("sp_describe_first_result_set" in s for s in db.statements)
    assert not any("BEGIN TRANSACTION" in s for s in db.statements)
    broken = checker(db=ProbeDb(modules=MODULES, reject={"sp_describe": "Invalid column name 'Amt'."})).run([proc_source()])[0]
    assert broken["status"] == v.MISMATCH and "Invalid column name 'Amt'." in broken["runtime"]["detail"]
    temp = checker(db=ProbeDb(modules=MODULES, reject={"sp_describe": "Invalid object name '#stage'."})).run([proc_source()])[0]
    assert temp["runtime"]["status"] == rc.NOT_CHECKED and temp["status"] == v.MATCH


def test_on_request_a_procedure_without_required_parameters_runs_and_is_rolled_back():
    db = ProbeDb(modules=MODULES)
    r = checker(db=db, runtime=RUN_PROCS).run([proc_source()])[0]
    assert r["runtime"]["status"] == rc.PASS and "rolled back" in r["runtime"]["detail"]
    assert "BEGIN TRANSACTION; EXEC [sales].[pLoad]; ROLLBACK TRANSACTION;" in db.statements

    needs = [SqlParameter(1, "@RunDate", "date", has_default=False, is_output=False)]
    db2 = ProbeDb(modules=MODULES)
    only = checker(db=db2, runtime=RUN_PROCS).run([proc_source(needs)])[0]
    assert only["runtime"]["check"] == "Compiled" and "needs @RunDate" in only["runtime"]["detail"]
    assert "N'EXEC [sales].[pLoad] @RunDate = NULL'" in " ".join(db2.statements)

    failing = ProbeDb(modules=MODULES, reject={"BEGIN TRANSACTION": "Divide by zero error encountered."})
    bad = checker(db=failing, runtime=RUN_PROCS).run([proc_source()])[0]
    assert bad["status"] == v.MISMATCH and "Divide by zero" in bad["runtime"]["detail"]
    assert failing.statements[-1] == "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;"


# -- Fabric items ------------------------------------------------------------------------------------

def nb_rest(**kw):
    content = {"definition": {"parts": [{"path": "notebook-content.ipynb", "payload": b64({"cells": [{}, {}]})}]}}
    return JobRest(notebooks=[{"id": "nb-1", "displayName": "LoadSales"}], definitions={"nb-1": content}, **kw)


NOTEBOOK_SRC = Source("n", "LoadSales", "Notebook", 4, NOTEBOOK, payload=NB)


def test_a_notebook_is_judged_by_its_last_run_in_fabric():
    never = checker(rest=nb_rest()).run([NOTEBOOK_SRC])[0]
    assert never["status"] == v.REVIEW and never["runtime"]["status"] == rc.NOT_RUN
    history = {"nb-1": [{"status": "Failed", "startTimeUtc": "2026-10-01T09:00:00Z", "failureReason": {"message": "old"}},
                        {"status": "Completed", "startTimeUtc": "2026-10-12T09:00:00Z", "endTimeUtc": "2026-10-12T09:05:00Z"}]}
    ok = checker(rest=nb_rest(history=history)).run([NOTEBOOK_SRC])[0]
    assert ok["status"] == v.MATCH and ok["runtime"]["status"] == rc.PASS and ok["runtime"]["runAt"] == "2026-10-12T09:05:00Z"
    history["nb-1"].append({"status": "Failed", "startTimeUtc": "2026-10-13T09:00:00Z",
                            "failureReason": {"message": "ModuleNotFoundError: mssparkutils"}})
    bad = checker(rest=nb_rest(history=history)).run([NOTEBOOK_SRC])[0]
    assert bad["status"] == v.MISMATCH and "ModuleNotFoundError" in bad["runtime"]["detail"]


def test_run_now_starts_the_item_follows_it_and_reports_fabrics_reason():
    rest = nb_rest(outcome="Failed", polls=3)
    r = checker(rest=rest, runtime=RUN_NOTEBOOKS).run([NOTEBOOK_SRC])[0]
    assert rest.started == [f"/workspaces/{WS}/items/nb-1/jobs/instances?jobType=RunNotebook"]
    assert r["status"] == v.MISMATCH and r["runtime"]["check"] == "Ran now in Fabric" and "NameError" in r["runtime"]["detail"]
    ok = checker(rest=nb_rest(), runtime=RUN_NOTEBOOKS).run([NOTEBOOK_SRC])[0]
    assert ok["status"] == v.MATCH and ok["runtime"]["status"] == rc.PASS


def test_a_run_that_outlasts_the_timeout_is_not_judged_and_a_refused_start_is_a_failure():
    ticks = iter(range(0, 10 ** 7, 600))
    slow = nb_rest(polls=10 ** 6)
    opts = rc.RuntimeOptions(run=frozenset({NOTEBOOK}), timeout_minutes=30)
    r = checker(rest=slow, runtime=opts, clock=lambda: next(ticks)).run([NOTEBOOK_SRC])[0]
    assert r["runtime"]["status"] == rc.NOT_CHECKED and "after 30 minutes" in r["runtime"]["detail"] and r["status"] == v.MATCH
    refused = checker(rest=nb_rest(refuse=True), runtime=RUN_NOTEBOOKS).run([NOTEBOOK_SRC])[0]
    assert refused["status"] == v.MISMATCH and "did not start it" in refused["runtime"]["detail"]


def test_runs_go_a_few_at_a_time():
    names = [f"nb{i}" for i in range(7)]
    rest = JobRest(notebooks=[{"id": f"id-{n}", "displayName": n} for n in names], polls=2)
    running, peak = set(), [0]
    original_get = rest.get

    def get(path):
        state = original_get(path)
        if "/jobs/instances/" in path:
            running.add(path)
            if state["status"] == "Completed":
                running.discard(path)
            peak[0] = max(peak[0], len(running))
        return state

    rest.get = get
    sources = [Source(n, n, "Notebook", 1, NOTEBOOK, payload=NB) for n in names]
    rows = checker(rest=rest, runtime=RUN_NOTEBOOKS).run(sources)
    assert len(rest.started) == 7 and peak[0] <= rc.MAX_PARALLEL_RUNS
    assert all(r["runtime"]["status"] == rc.PASS for r in rows)


def test_options_are_parsed_safely():
    o = rc.RuntimeOptions.parse({"probe": False, "run": ["pipeline", "notebook", "table", "rm -rf"], "timeoutMinutes": 9999})
    assert o.probe is False and o.run == {PIPELINE, NOTEBOOK} and o.timeout_minutes == rc.MAX_TIMEOUT_MINUTES
    assert rc.RuntimeOptions.parse(None) == rc.RuntimeOptions()


# -- in the background -------------------------------------------------------------------------------

def test_validation_runs_in_the_background_and_reports_progress_then_rows():
    rest = nb_rest(history={"nb-1": [{"status": "Completed", "startTimeUtc": "2026-10-12T09:00:00Z"}]})
    db = ProbeDb({("sales", "Orders"): plain_table()}, rows={"[sales].[Orders]": 4})
    svc = MigrationService(FakeSession(fake_job()), fabric(), rest_factory=lambda: rest, sql_factory=lambda h, d: db)
    started = svc.start_validation({"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}], "runtime": {"probe": True}})
    assert started["state"] == "running"
    state = svc.validation_state()
    for _ in range(3000):  # up to 30 s: a busy machine can be slow to schedule the thread
        state = svc.validation_state()
        if state["state"] != "running":
            break
        time.sleep(0.01)
    assert state["state"] == "completed" and state["rows"][0]["runtime"]["status"] == rc.PASS
    assert state["summary"][v.MATCH] == 1
