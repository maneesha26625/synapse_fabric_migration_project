"""Performance replay, view and procedure equivalence, shortcut access, Spark Environments, and cutover. Offline."""

from __future__ import annotations

from discovery_agent.migration import cutover, environments, equivalence, notebooks, onelake, reconcile
from discovery_agent.migration import validation as v
from discovery_agent.migration.onelake import OneLakeError
from discovery_agent.sql.estate import SqlQuerySample
from discovery_agent.sql.models import SqlColumn

from test_migration import WS  # noqa: E402
from test_validation import ValRest, validator  # noqa: E402


class Cursor:
    """Answers by the first matching needle; fetchmany drains ``rows`` once."""

    def __init__(self, answers=None, rows=0, description=None, fail=None):
        self.answers, self.rows, self.description, self.fail = answers or {}, rows, description, fail
        self.executed, self._one, self._all, self._left = [], None, [], 0

    def execute(self, sql, *params):
        self.executed.append(sql)
        if self.fail and self.fail in sql:
            raise RuntimeError("[Microsoft][ODBC Driver 18][SQL Server]Invalid object name 'dbo.Gone'.")
        for needle, answer in self.answers.items():
            if needle in sql:
                self._one = answer[0] if answer else None
                self._all = list(answer)
                break
        self._left = self.rows

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def fetchmany(self, n):
        take = min(n, self._left)
        self._left -= take
        return [()] * take


def clock(*times):
    it = iter(times)
    return lambda: next(it)


# ---- performance ------------------------------------------------------------------------------


def sample(text="SELECT a FROM dbo.Sales", avg=1000):
    return SqlQuerySample(text, runs=12, avg_ms=avg, max_ms=avg * 2, min_ms=avg // 2)


def test_a_query_within_the_threshold_matches_and_a_slow_one_is_flagged():
    opts = equivalence.PerformanceOptions(enabled=True, threshold_pct=50)
    fast = equivalence.replay(Cursor(rows=10), sample(), opts, clock=clock(0, 2.0, 2.0, 2.8))  # warm run: 0.8 s
    assert fast["status"] == v.MATCH and "20% faster" in fast["detail"] and "10 rows" in fast["target"]
    slow = equivalence.replay(Cursor(rows=10), sample(), opts, clock=clock(0, 3.0, 3.0, 5.0))  # warm: 2 s, +100%
    assert slow["status"] == v.REVIEW and "100% slower" in slow["detail"]
    strict = equivalence.replay(Cursor(rows=10), sample(), equivalence.PerformanceOptions(True, 50, strict=True), clock=clock(0, 3.0, 3.0, 5.0))
    assert strict["status"] == v.MISMATCH


def test_writes_and_session_state_are_never_replayed_and_a_failure_is_a_mismatch():
    c = Cursor()
    out = equivalence.replay(c, sample("SELECT a INTO #t FROM dbo.Sales"), equivalence.PerformanceOptions(True))
    assert out["status"] == v.REVIEW and not c.executed
    failed = equivalence.replay(Cursor(fail="Gone"), sample("SELECT a FROM dbo.Gone"), equivalence.PerformanceOptions(True), clock=clock(0, 1))
    assert failed["status"] == v.MISMATCH and "Invalid object name" in failed["detail"]


def test_the_pool_name_in_a_query_points_at_the_warehouse():
    c = Cursor(rows=1)
    equivalence.replay(c, sample("SELECT a FROM [pool01].dbo.Sales"), equivalence.PerformanceOptions(True), "pool01", "pool01_wh",
                       clock=clock(0, 1, 1, 2))
    assert c.executed[0] == "SELECT a FROM [pool01_wh].dbo.Sales"


def test_validation_benchmarks_the_history_after_the_objects():
    val = validator(rest=ValRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}]))
    val.performance = equivalence.PerformanceOptions(enabled=True, limit=1)
    val.workload = (sample(avg=10_000), sample("SELECT b FROM dbo.Other", avg=5), sample("DELETE FROM x"))
    rows = val.run([])
    perf = [r for r in rows if r["category"] == "Performance"]
    assert len(perf) == 2 and perf[0]["object"].startswith("SELECT a") and perf[1]["object"] == "Not replayed"


# ---- views and procedures ------------------------------------------------------------------------


def test_a_view_compares_rows_and_values_on_both_sides():
    cols = (SqlColumn(1, "Id", "int", 4, 10, 0, False), SqlColumn(2, "Amount", "decimal", 9, 18, 2, True))
    same = equivalence.view_profile(Cursor({" AS m0": [(10, 550, 0, 99)]}), Cursor({" AS m0": [(10, 550, 0, 99)]}), "dbo", "vSales", cols,
                                    reconcile.ReconcileOptions())
    assert same.status == v.MATCH
    off = equivalence.view_profile(Cursor({" AS m0": [(10, 550, 0, 99)]}), Cursor({" AS m0": [(9, 500, 0, 90)]}), "dbo", "vSales", cols,
                                   reconcile.ReconcileOptions())
    assert off.status == v.MISMATCH and "rows: 10 in Synapse, 9 in Fabric" in off.detail


def test_a_procedure_result_shape_agrees_across_by_design_type_changes():
    desc = [("is_hidden",), ("name",), ("system_type_name",)]
    src = Cursor({"sp_describe": [(0, "Id", "int"), (0, "Name", "nvarchar(50)")]}, description=desc)
    same = Cursor({"sp_describe": [(0, "Id", "int"), (0, "Name", "varchar(200)")]}, description=desc)
    assert equivalence.procedure_shape(src, same, "EXEC [dbo].[p]")[0] == v.MATCH
    other = Cursor({"sp_describe": [(0, "Id", "bigint"), (0, "Name", "varchar(200)")]}, description=desc)
    status, _, _, detail = equivalence.procedure_shape(src, other, "EXEC [dbo].[p]")
    assert status == v.REVIEW and "Id (int -> bigint)" in detail
    fewer = Cursor({"sp_describe": [(0, "Id", "int")]}, description=desc)
    assert equivalence.procedure_shape(src, fewer, "EXEC [dbo].[p]")[0] == v.MISMATCH


# ---- access ---------------------------------------------------------------------------------------


class Lake:
    def __init__(self, refuse=()):
        self.refuse, self.probed = set(refuse), []

    def probe(self, workspace_id, lakehouse_id, path):
        self.probed.append(path)
        if path in self.refuse:
            raise OneLakeError(403, "This request is not authorized to perform this operation.")


class ShortcutRest(ValRest):
    def list(self, path):
        if path.endswith("/shortcuts"):
            return [{"name": "raw", "path": "Files", "target": {"adlsGen2": {"location": "https://acct.dfs.core.windows.net", "subpath": "/raw"}}},
                    {"name": "Sales", "path": "Files/ext", "target": {"adlsGen2": {"location": "https://acct.dfs.core.windows.net", "subpath": "/sales"}}}]
        return super().list(path)


def test_every_shortcut_is_read_through_its_connection():
    lake = Lake(refuse={"Files/ext/Sales"})
    val = validator(rest=ShortcutRest(lakehouses=[{"id": "lh-1", "displayName": "pool01_lakehouse"}]))
    val.onelake, val.access = lake, True
    rows = [r for r in val.run([]) if r["category"] == "Access"]
    assert [(r["object"], r["status"]) for r in rows] == [("pool01_lakehouse/Files/raw", v.MATCH), ("pool01_lakehouse/Files/ext/Sales", v.MISMATCH)]
    assert "Storage Blob Data Reader" in rows[1]["detail"] and rows[1]["source"] == "https://acct.dfs.core.windows.net/sales"


def test_the_onelake_probe_lists_one_entry_of_the_folder():
    sent = []
    files = onelake.OneLakeFiles(lambda: "t", send=lambda m, url, h, b: (sent.append((m, url)), (200, {}, b""))[1])
    files.probe(WS, "lh-1", "Files/ext/Sales")
    assert sent == [("GET", f"https://onelake.dfs.fabric.microsoft.com/{WS}?resource=filesystem&recursive=false&maxResults=1"
                            "&directory=lh-1/Files/ext/Sales")]


# ---- Spark Environments -----------------------------------------------------------------------------


def test_pool_requirements_become_public_libraries_and_spark_settings_carry_over():
    plan = environments.plan({"nodeSize": "Small", "nodeCount": 3, "sparkVersion": "3.4",
                              "_libraryRequirements": {"content": "pandas==2.1.0\n# comment\nrequests>=2.31\n--index-url https://x\n"},
                              "_sparkConfig": {"content": "spark.sql.shuffle.partitions 64\nspark.executor.extraJavaOptions=-Dx=1\nnot.spark 1\n"},
                              "customLibraries": [{"name": "mylib-1.0-py3-none-any.whl", "type": "whl"}]}, "pool")
    assert plan.libraries_yml == "dependencies:\n  - pip:\n    - pandas==2.1.0\n    - requests>=2.31\n"
    assert plan.compute["sparkProperties"] == {"spark.sql.shuffle.partitions": "64", "spark.executor.extraJavaOptions": "-Dx=1"}
    text = " ".join(plan.notes)
    assert "--index-url" in text and "mylib-1.0-py3-none-any.whl" in text
    conda = environments.environment_yml("name: x\ndependencies:\n  - numpy\n")
    assert conda[0].startswith("name: x") and environments.libraries_part("a")["path"] == "Libraries/PublicLibraries/environment.yml"


def test_a_notebook_is_attached_to_its_pools_environment():
    payload = {"properties": {"bigDataPool": {"referenceName": "pool"}, "cells": [], "metadata": {"language_info": {"name": "python"}}}}
    doc, notes = notebooks.to_fabric_ipynb(payload, environment=("env-1", "pool", WS))
    assert doc["metadata"]["dependencies"]["environment"] == {"environmentId": "env-1", "workspaceId": WS}
    assert "Attached to the Environment 'pool'" in notes[0]
    _, unbound = notebooks.to_fabric_ipynb(payload)
    assert "no Environment of that name" in unbound[0]


# ---- cutover ---------------------------------------------------------------------------------------


QUERY = "SELECT COALESCE(CONVERT(varchar(40), MAX([ModifiedAt]), 126), '') AS wm FROM [dbo].[Sales]"


def test_freshness_compares_each_watermark_with_synapse_and_runs_only_queries_this_tool_wrote():
    wh = Cursor({"sync_tables": [("dbo.sales", "2026-10-01T00:00:00", QUERY), ("dbo.odd", "2026-10-01T00:00:00", "DROP TABLE x")]})
    src = Cursor({"MAX([ModifiedAt])": [("2026-10-01T02:30:00",)]})
    fresh = cutover.freshness(wh, src)
    sales, odd = fresh["tables"]
    assert sales["behind"] is True and sales["lagMinutes"] == 150 and fresh["behind"] == 1
    assert odd["behind"] is None and "not one this tool wrote" in odd["detail"] and "DROP" not in " ".join(src.executed)


class ScheduleRest:
    def __init__(self):
        self.patched = []

    def list(self, path):
        if path.endswith("/dataPipelines"):
            return [{"id": "p1", "displayName": "load_orders"}, {"id": "p2", "displayName": "sync_pool01"}]
        return [{"id": f"s-{path.split('/')[4]}", "enabled": False, "configuration": {"type": "Daily", "times": ["02:00"]}}]

    def get(self, path):
        return {"configuration": {"type": "Daily", "times": ["02:00"]}}

    def patch(self, path, body):
        self.patched.append((path, body))
        return {}


def test_schedules_switch_on_at_cutover_and_off_again_keeping_their_timing():
    rest = ScheduleRest()
    listed = cutover.schedules(rest, WS)
    assert [s["pipeline"] for s in listed] == ["sync_pool01", "load_orders"] and listed[0]["sync"]
    out = cutover.set_schedules(rest, WS, True, ["load_orders"])
    assert out["changed"] == ["load_orders"]
    assert rest.patched == [(f"/workspaces/{WS}/items/p1/jobs/Pipeline/schedules/s-p1",
                             {"enabled": True, "configuration": {"type": "Daily", "times": ["02:00"]}})]


def test_readiness_needs_validation_current_tables_and_a_person_for_synapse():
    checks = cutover.readiness({"state": "completed", "summary": {"MISMATCH": 2}, "finishedAt": "now"},
                               {"tables": [{"behind": True}], "behind": 1}, [{"enabled": False}], {"status": "PASS", "detail": "ok"})
    by = {c["label"]: c["status"] for c in checks}
    assert by["Validation has no mismatches"] == "fail" and by["Synced tables are current"] == "fail"
    assert by["The last sync succeeded"] == "pass" and by["Synapse triggers stopped"] == "person"
    assert cutover.readiness({}, {"tables": []}, [], None)[0]["status"] == "fail"
