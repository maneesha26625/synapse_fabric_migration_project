"""LLM-assisted transformation: prompts, the Claude Code call, review, and what a run does with it.

Offline. The model is a fake with the same ``complete`` / ``available`` surface as ``ClaudeCli``,
and the CLI itself is a fake ``subprocess.run``, so no prompt leaves the machine.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from discovery_agent.api.service import ApiError
from discovery_agent.api.transform import REPO_ROOT, TransformService
from discovery_agent.llm import checks, claude_cli, context, templates
from discovery_agent.llm.claude_cli import Answer, ClaudeCli, ModelError
from discovery_agent.llm.store import TransformStore
from discovery_agent.migration.common import NOTEBOOK, PIPELINE, PROCEDURE, SCRIPT, TABLE, VIEW, Source
from discovery_agent.migration.preflight import content_findings
from discovery_agent.sql.models import CatalogObjectType, DefinitionState, SqlModuleDefinition, SqlObjectKey, SqlProcedure

from test_migration import FakeDb, FakeRest, by_name, notebook_payload, run_with, table, view

PROMPTS = REPO_ROOT / "prompts" / "transform"
PROC_TEXT = ("CREATE PROCEDURE sales.LoadOrders AS\nBEGIN\n"
             "    SELECT * INTO #t FROM sys.pdw_nodes_tables;\n"
             "    CREATE TABLE sales.x WITH (DISTRIBUTION = ROUND_ROBIN) AS SELECT 1 AS a;\nEND")
GOOD_PROC = "CREATE PROCEDURE sales.LoadOrders AS\nBEGIN\n    -- Fabric: sys.pdw_* replaced\n    SELECT 1 AS a;\nEND"


def procedure(text=PROC_TEXT):
    return SqlProcedure(key=SqlObjectKey("pool", "sales", "LoadOrders", CatalogObjectType.PROCEDURE),
                        definition=SqlModuleDefinition(state=DefinitionState.AVAILABLE, text=text))


def pipeline_bundle(activity_type="ExecuteDataFlow"):
    return {"resource": {"name": "Daily", "properties": {"activities": [
        {"name": "Wait1", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}},
        {"name": "Flow", "type": activity_type, "typeProperties": {}},
    ]}}, "datasets": {}, "linkedServices": {}}


# -- a fake discovery and model ----------------------------------------------------------------

class FakeSession:
    def __init__(self, proc_text=PROC_TEXT):
        self.proc = procedure(proc_text)
        records = {
            "sql://p/sales/LoadOrders": SimpleNamespace(identity=SimpleNamespace(name="LoadOrders", schema="sales"), content=self.proc),
            "sql://p/sales/Orders": SimpleNamespace(identity=SimpleNamespace(name="Orders", schema="sales"), content=table()),
            "synapse://notebook/N": SimpleNamespace(identity=SimpleNamespace(name="N", schema=None), content=None),
        }
        self.job = SimpleNamespace(
            items=[{"id": "sql://p/sales/LoadOrders", "name": "sales.LoadOrders", "type": "Stored Procedure"},
                   {"id": "sql://p/sales/Orders", "name": "sales.Orders", "type": "Table"},
                   {"id": "synapse://notebook/N", "name": "N", "type": "Notebook"}],
            records_by_id=records, extras_by_id={}, run=None)

    def migration_snapshot(self):
        return self.job, "pool01"

    def result(self, object_id):
        return {"target": {"component": "Fabric Warehouse"},
                "overview": {"synapseSpecificConstructs": ["sys.pdw_nodes_tables"]},
                "issues": [],
                "dependencies": [{"name": "sales.Orders", "type": "Table", "kind": "sql", "objectId": "sql://p/sales/Orders"}]}


class FakeModel:
    def __init__(self, code=GOOD_PROC, error=None):
        self.code, self.error, self.prompts = code, error, []

    def available(self):
        return {"available": True, "model": "fake", "timeoutSeconds": 1, "message": None}

    def complete(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise ModelError(self.error)
        return Answer(code=self.code, notes=["replaced sys.pdw_nodes_tables"], unconverted=[], confidence="medium", model="fake")


@pytest.fixture()
def service(tmp_path):
    def make(model=None, session=None):
        return TransformService(session or FakeSession(), model=model or FakeModel(), store=TransformStore(tmp_path / "t"),
                                prompts=PROMPTS, synchronous=True)
    return make


OID = "sql://p/sales/LoadOrders"


# -- templates -------------------------------------------------------------------------------------

def test_placeholders_conditionals_and_comments_render():
    out = templates.render("<!-- for editors -->\nA {{x.y}} {{#if z}}Z{{else}}no z{{/if}} {{#if x}}[{{#if x.y}}inner{{/if}}]{{/if}}",
                           {"x": {"y": "1"}, "z": ""})
    assert out == "A 1 no z [inner]\n"


def test_values_are_inserted_once_and_never_rendered_again():
    assert templates.render("{{code}}", {"code": "SELECT '{{object.name}}' -- {{#if x}}"}) == "SELECT '{{object.name}}' -- {{#if x}}\n"


def test_a_list_becomes_bullets_and_an_unclosed_block_is_an_error():
    assert templates.render("{{items}}", {"items": ["a", "b"]}) == "- a\n- b\n"
    with pytest.raises(templates.TemplateError):
        templates.render("{{#if a}} open", {"a": 1})


# -- what goes into the prompt -----------------------------------------------------------------------

def test_a_procedure_prompt_carries_source_rule_output_problems_project_rules_and_columns(service):
    prompt = service().prompt(OID)["prompt"]
    assert PROC_TEXT in prompt
    assert "What the deterministic rules produce today" in prompt and "/* " in prompt      # the rules removed DISTRIBUTION
    assert "Synapse system views (sys.pdw_*)" in prompt                                     # what the rules could not fix
    assert "Project requirements" in prompt and "`pool01`" in prompt                       # project.md, with the warehouse filled in
    assert "OrderId int" in prompt and "Customer nvarchar(100)" in prompt                  # columns of what it reads
    assert "CREATE PROCEDURE" in prompt and "{{" not in prompt


def test_a_table_is_sent_as_its_synapse_ddl_with_the_rule_ddl_beside_it():
    source = Source("t", "sales.Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders")
    subject = context.extract(source)
    assert "DISTRIBUTION = HASH" in subject.code and "[Customer] nvarchar(100) NULL" in subject.code
    assert subject.rule_output.startswith("CREATE TABLE [sales].[Orders]") and "money stored as decimal(19,4)" in " ".join(subject.rule_notes)


def test_a_pipeline_reports_the_activities_the_converter_rejects():
    subject = context.extract(Source("p", "Daily", "Pipeline", 1, PIPELINE, payload=pipeline_bundle()))
    assert subject.language == "json" and json.loads(subject.code)["name"] == "Daily"
    assert subject.problems == ["No Fabric equivalent the converter knows: Flow (ExecuteDataFlow)"]


def test_objects_without_a_template_or_definition_are_refused(service):
    with pytest.raises(ApiError) as err:
        service().prompt("synapse://notebook/N")
    assert err.value.code == "not_transformable"
    with pytest.raises(context.NotTransformable):
        context.extract(Source("v", "v", "View", 1, VIEW, payload=view(text=None)))


def test_a_code_fence_is_longer_than_any_backtick_run_in_the_source():
    assert context._fence("a ```` b") == "`````"


# -- the Claude Code CLI ----------------------------------------------------------------------------

def cli_output(**overrides):
    envelope = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
                "modelUsage": {"claude-x": {}}, "result": "",
                "structured_output": {"code": "CREATE VIEW v AS SELECT 1", "notes": ["n"], "unconverted": [], "confidence": "HIGH"}}
    envelope.update(overrides)
    return json.dumps(envelope)


def test_the_cli_runs_without_tools_and_reads_the_prompt_from_stdin(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=cli_output(), stderr="")

    monkeypatch.setattr(claude_cli, "executable", lambda: "claude")
    answer = ClaudeCli(model="sonnet", timeout=5, run=fake_run).complete("the prompt")
    args, kwargs = calls[0]
    assert args[:2] == ["claude", "-p"] and args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args and args[args.index("--model") + 1] == "sonnet"
    assert kwargs["input"] == "the prompt" and kwargs["timeout"] == 5 and Path(kwargs["cwd"]).name.startswith("fabric-transform-")
    assert answer.code == "CREATE VIEW v AS SELECT 1" and answer.confidence == "high" and answer.model == "claude-x"


def test_a_plain_text_result_with_fences_is_still_read():
    text = "```json\n" + json.dumps({"code": "```sql\nSELECT 1\n```", "notes": [], "unconverted": [], "confidence": "low"}) + "\n```"
    assert claude_cli.parse(cli_output(structured_output=None, result=text)).code == "SELECT 1"


@pytest.mark.parametrize("stdout, message", [
    ("not json", "did not return JSON"),
    (cli_output(is_error=True, result="Not logged in"), "Not logged in"),
    (cli_output(structured_output={"code": " ", "notes": []}), "no converted code"),
])
def test_unusable_cli_output_is_a_model_error(stdout, message):
    with pytest.raises(ModelError, match=message):
        claude_cli.parse(stdout)


def test_a_timeout_and_a_missing_cli_are_reported_plainly(monkeypatch):
    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(claude_cli, "executable", lambda: "claude")
    with pytest.raises(ModelError, match="within 1 seconds"):
        ClaudeCli(timeout=1, run=slow).complete("p")
    monkeypatch.setattr(claude_cli, "executable", lambda: None)
    assert ClaudeCli().available()["available"] is False


# -- checks ---------------------------------------------------------------------------------------------

def test_sql_checks_catch_the_wrong_statement_leftovers_and_unbalanced_parentheses():
    assert checks.blocking(checks.run(VIEW, "CREATE PROCEDURE p AS SELECT 1", []))
    assert checks.blocking(checks.run(PROCEDURE, "CREATE PROCEDURE p AS SELECT * FROM sys.pdw_nodes", []))
    assert checks.blocking(checks.run(PROCEDURE, "CREATE PROCEDURE p AS SELECT (1", []))
    found = checks.run(PROCEDURE, "-- header\nCREATE OR ALTER PROC p AS SELECT ')' AS x", [])
    assert not checks.blocking(found) and found[0]["level"] == "ok"


def test_pipeline_checks_need_json_that_the_converter_accepts():
    assert checks.blocking(checks.run(PIPELINE, "{not json", []))
    assert checks.blocking(checks.run(PIPELINE, json.dumps(pipeline_bundle()["resource"]), []))
    assert not checks.blocking(checks.run(PIPELINE, json.dumps(pipeline_bundle("Wait")["resource"]), []))


# -- generate, review, accept ------------------------------------------------------------------------------

def test_a_draft_is_generated_checked_and_stored_but_not_used_until_accepted(service):
    svc = service()
    assert svc.generate({"ids": [OID]})["items"][0]["state"] == "queued"
    view_ = svc.object(OID)
    assert view_["state"] == "draft" and view_["draft"]["code"] == GOOD_PROC and view_["draft"]["stale"] is False
    assert view_["draft"]["checks"][0]["level"] == "ok" and view_["accepted"] is None
    assert view_["source"]["problems"]

    sources = [Source(OID, "LoadOrders", "Stored Procedure", 1, PROCEDURE, payload=svc._session.proc, schema="sales", object_name="LoadOrders")]
    svc.apply(sources)
    assert sources[0].override is None

    accepted = svc.accept({"id": OID})
    assert accepted["state"] == "accepted" and accepted["draft"] is None and accepted["accepted"]["edited"] is False
    svc.apply(sources)
    assert sources[0].override["code"] == GOOD_PROC


def test_an_edited_draft_is_checked_again_before_it_is_accepted(service):
    svc = service()
    svc.generate({"ids": [OID]})
    with pytest.raises(ApiError) as err:
        svc.accept({"id": OID, "code": "SELECT 1"})
    assert err.value.code == "checks_failed"
    assert svc.accept({"id": OID, "code": GOOD_PROC + "\n-- reviewed"})["accepted"]["edited"] is True


def test_a_failed_call_is_reported_on_the_object(service):
    svc = service(model=FakeModel(error="Claude Code did not answer within 1 seconds."))
    svc.generate({"ids": [OID]})
    view_ = svc.object(OID)
    assert view_["state"] == "failed" and "within 1 seconds" in view_["error"] and view_["draft"] is None


def test_when_the_source_changes_the_transform_is_stale_and_the_run_uses_the_rules(service, tmp_path):
    svc = service()
    svc.generate({"ids": [OID]})
    svc.accept({"id": OID})
    changed = FakeSession(proc_text=PROC_TEXT + "\n-- changed in Synapse")
    later = TransformService(changed, model=FakeModel(), store=TransformStore(tmp_path / "t"), prompts=PROMPTS, synchronous=True)
    assert later.object(OID)["accepted"]["stale"] is True
    sources = [Source(OID, "LoadOrders", "Stored Procedure", 1, PROCEDURE, payload=changed.proc, schema="sales", object_name="LoadOrders")]
    log = later.apply(sources)
    assert sources[0].override is None and "out of date" in log[0]


def test_discard_drops_the_draft_or_everything(service):
    svc = service()
    svc.generate({"ids": [OID]})
    svc.accept({"id": OID})
    svc.generate({"ids": [OID]})
    assert svc.discard({"id": OID, "scope": "draft"})["state"] == "accepted"
    assert svc.discard({"id": OID, "scope": "all"})["state"] == "none"
    assert svc.list()["items"] == []


def test_generation_can_be_switched_off(service, monkeypatch):
    monkeypatch.setenv("MIGRATION_LLM", "off")
    svc = service()
    assert svc.settings()["enabled"] is False
    with pytest.raises(ApiError) as err:
        svc.generate({"ids": [OID]})
    assert err.value.code == "llm_disabled"


def test_bulk_generation_reports_objects_it_cannot_send_without_failing_the_rest(service):
    items = service().generate({"ids": [OID, "synapse://notebook/N"]})["items"]
    assert [i["state"] for i in items] == ["queued", "rejected"]


# -- what a run does with an accepted transform ------------------------------------------------------------

def test_a_run_creates_the_accepted_procedure_and_says_so():
    source = Source(OID, "LoadOrders", "Stored Procedure", 1, PROCEDURE, payload=procedure(), schema="sales", object_name="LoadOrders",
                    override={"code": GOOD_PROC, "acceptedAt": "2026-10-13T10:00:00+00:00", "edited": True})
    run, _, db, _ = run_with([source], db=FakeDb())
    item = by_name(run)["LoadOrders"]
    assert item.status == "COMPLETED" and "LLM-assisted" in item.step
    assert GOOD_PROC in db.statements and not any("pdw_nodes_tables" in s for s in db.statements)
    assert "accepted and edited on 2026-10-13" in item.notes[0]


def test_a_run_creates_the_accepted_table_ddl_and_still_adds_the_keys():
    ddl = "CREATE TABLE [sales].[Orders] ([OrderId] int NOT NULL);"
    source = Source("t", "Orders", "Table", 1, TABLE, payload=table(), schema="sales", object_name="Orders",
                    override={"code": ddl, "acceptedAt": "x", "edited": False})
    run, _, db, _ = run_with([source], db=FakeDb())
    assert by_name(run)["Orders"].status == "COMPLETED" and ddl in db.statements


def test_the_plan_checks_the_accepted_code_not_the_original():
    original = Source("p", "Daily", "Pipeline", 1, PIPELINE, payload=pipeline_bundle())
    assert any(f.code == "NEEDS_REWRITE" for f in content_findings([original]))
    fixed = json.dumps(pipeline_bundle("Wait")["resource"])
    reviewed = Source("p", "Daily", "Pipeline", 1, PIPELINE, payload=pipeline_bundle(), override={"code": fixed, "acceptedAt": "x"})
    codes = [f.code for f in content_findings([reviewed])]
    assert "LLM_TRANSFORM" in codes and "NEEDS_REWRITE" not in codes


def test_every_transformable_kind_has_a_template_and_the_templates_render(service):
    for kind, name in context.TEMPLATES.items():
        assert (PROMPTS / name).is_file(), name
    assert set(context.TRANSFORMABLE_KINDS) == {PROCEDURE, VIEW, TABLE, SCRIPT, PIPELINE, NOTEBOOK, "function"}


def test_the_http_routes_reach_the_service(service):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from urllib.parse import quote

    from discovery_agent.api.server import make_handler

    svc = service()
    svc._session.start_discovery = lambda: {}  # the POST route table names it; never called here
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc._session, None, transforms=svc))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api/transform"

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    try:
        assert call("/settings")["enabled"] is True
        assert "CREATE PROCEDURE" in call("/prompt/" + quote(OID, safe=""))["prompt"]
        assert call("/generate", {"ids": [OID]})["items"][0]["state"] == "queued"
        assert call("/objects/" + quote(OID, safe=""))["state"] == "draft"
        assert call("/accept", {"id": OID})["state"] == "accepted"
        assert [i["state"] for i in call("")["items"]] == ["accepted"]
    finally:
        server.shutdown()


# -- notebooks ------------------------------------------------------------------------------------------------

def dotnet_notebook():
    return notebook_payload(metadata={"language_info": {"name": "csharp"}},
                            cells=[{"cell_type": "code", "source": ["var df = spark.Read().Parquet(\"x\");"], "metadata": {}}])


def test_a_notebook_is_sent_as_its_cells_with_the_rules_review_flags():
    payload = notebook_payload(cells=[{"cell_type": "code", "source": ["t = TokenLibrary.getSecret('kv', 's')\n", "mssparkutils.fs.ls('/')"], "metadata": {}}])
    subject = context.extract(Source("n", "LoadSales", "Notebook", 1, NOTEBOOK, payload=payload))
    sent = json.loads(subject.code)
    assert sent["language"] == "python" and sent["cells"][0]["source"].startswith("t = TokenLibrary")
    assert "notebookutils.fs.ls" in subject.rule_output and any("TokenLibrary" in p for p in subject.problems)


def test_a_dotnet_notebook_can_be_rewritten_and_then_runs():
    payload = dotnet_notebook()
    subject = context.extract(Source("n", "LoadSales", "Notebook", 1, NOTEBOOK, payload=payload))
    assert "no Fabric equivalent" in subject.problems[0]
    rewritten = json.dumps({"language": "python", "cells": [{"cell_type": "code", "source": "df = spark.read.parquet('x')"}]})
    assert not checks.blocking(checks.run(NOTEBOOK, rewritten, [], payload))
    assert checks.blocking(checks.run(NOTEBOOK, json.dumps({"cells": "nope"}), [], payload))
    source = Source("n", "LoadSales", "Notebook", 1, NOTEBOOK, payload=payload,
                    override={"code": rewritten, "acceptedAt": "x", "edited": False})
    run, rest, _, _ = run_with([source], rest=FakeRest())
    item = by_name(run)["LoadSales"]
    assert item.status == "COMPLETED" and "LLM-assisted" in item.notes[0]


def test_a_dropped_cell_is_a_warning():
    payload = notebook_payload()
    one_cell = json.dumps({"language": "python", "cells": [{"cell_type": "code", "source": "x = 1"}]})
    assert any("had 2 cells and now has 1" in c["message"] for c in checks.run(NOTEBOOK, one_cell, [], payload))


# -- the work queue and the project requirements --------------------------------------------------------------

def test_candidates_list_what_the_rules_cannot_convert_first(service):
    session = FakeSession()
    session.job.items.append({"id": "sql://p/sales/vOk", "name": "sales.vOk", "type": "View"})
    session.job.records_by_id["sql://p/sales/vOk"] = SimpleNamespace(identity=SimpleNamespace(name="vOk", schema="sales"), content=view(name="vOk"))
    svc = service(session=session)
    svc.generate({"ids": [OID]})
    result = svc.candidates()
    names = [i["name"] for i in result["items"]]
    assert names[0] == "sales.LoadOrders" and result["items"][0]["state"] == "draft" and result["items"][0]["problems"]
    assert "sales.vOk" in names and "N" in names    # the notebook (no cells kept) is listed as unavailable
    assert next(i for i in result["items"] if i["name"] == "N")["unavailable"]
    assert result["summary"]["needsWork"] == 1 and result["summary"]["drafts"] == 1


def test_project_requirements_are_saved_and_used_in_the_next_prompt(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for f in PROMPTS.glob("*.md"):
        (prompts / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    svc = TransformService(FakeSession(), model=FakeModel(), store=TransformStore(tmp_path / "t"), prompts=prompts, synchronous=True)
    saved = svc.save_project({"text": "- Prefix every schema with `fin_` in {{target.warehouse}}.\r\n"})
    assert saved["text"] == "- Prefix every schema with `fin_` in {{target.warehouse}}.\n" and "target.warehouse" in saved["placeholders"]
    assert "Prefix every schema with `fin_` in pool01." in svc.prompt(OID)["prompt"]
    with pytest.raises(ApiError) as err:
        svc.save_project({"text": "{{#if object.schema}} never closed"})
    assert err.value.code == "invalid_template"


def test_a_run_item_says_it_came_from_an_accepted_transform():
    source = Source(OID, "LoadOrders", "Stored Procedure", 1, PROCEDURE, payload=procedure(), schema="sales", object_name="LoadOrders",
                    override={"code": GOOD_PROC, "acceptedAt": "2026-10-13T10:00:00+00:00", "edited": False})
    run, _, _, _ = run_with([source], db=FakeDb())
    plain = Source("v", "vOrders", "View", 1, VIEW, payload=view(), schema="sales", object_name="vOrders")
    other, _, _, _ = run_with([plain], db=FakeDb())
    assert run.to_dict()["items"][0]["llm"] == {"acceptedAt": "2026-10-13T10:00:00+00:00", "edited": False}
    assert other.to_dict()["items"][0]["llm"] is None
