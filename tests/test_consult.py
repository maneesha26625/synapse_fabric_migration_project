"""The consultation: the catalog, the engine, the brief, the policy in force, and what each consumer does with it.

Offline. The discovery is the migration tests' fake job, Fabric a fake, and the model a fake with the same ``ask``
surface as ``ClaudeCli``, so nothing here reaches Azure or Claude.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from discovery_agent.api import consult as consult_api
from discovery_agent.api import migration as migration_api
from discovery_agent.api.consult import ConsultService, estate_facts, rule_proposals
from discovery_agent.api.migration import MigrationService, sources_for
from discovery_agent.api.service import ApiError
from discovery_agent.api.transform import REPO_ROOT, TransformService
from discovery_agent.consult import brief as briefs
from discovery_agent.consult import catalog as catalogs
from discovery_agent.consult import conditions as cond
from discovery_agent.consult import context_file as context_files
from discovery_agent.consult import engine
from discovery_agent.consult import facts as fact_names
from discovery_agent.consult import settings as setting_names
from discovery_agent.consult.facts import fingerprint
from discovery_agent.consult.policy import Policy, history_column
from discovery_agent.llm import claude_cli
from discovery_agent.llm.claude_cli import Reply
from discovery_agent.llm.store import TransformStore
from discovery_agent.migration import datapipeline, fabric_git, jobs, run_state, security
from discovery_agent.migration.common import DEFERRED, NOTEBOOK, SECURITY, Source
from discovery_agent.migration.runner import MigrationRun, Migrator
from discovery_agent.sql.models import SqlColumn
from discovery_agent.state import approvals, context
from discovery_agent.state.store import Store

from test_migration import WS, FakeDb, FakeFabric, FakeRest, FakeSession, fake_job, notebook_payload, table, wait_until
from test_warehouse_security import plan_job

PROMPTS = REPO_ROOT / "prompts" / "transform"
CATALOG = catalogs.load()
NOTEBOOK_ID, TABLE_ID = "synapse://notebook/LoadSales", "sql://ws/pool/sales/Orders"
#: The stages the brief turns on when it moves the structure and the orchestration, but not the data.
NO_DATA = ["warehouse", "security", "spark", "notebooks", "connections", "pipelines", "spark_jobs", "sql_scripts", "schedules", "shortcuts"]
AGENT = context.Actor(context.AGENT, "assistant")


@pytest.fixture()
def person():
    """Requests are made by the operator, a person: what the API binds every request to."""
    with context.bound(actor=context.operator()):
        yield


class Trail:
    def __init__(self):
        self.records = []

    def __call__(self, action, **kwargs):
        self.records.append((action, kwargs))

    def actions(self):
        return [a for a, _ in self.records]

    def of(self, action):
        return [k for a, k in self.records if a == action]


def consult(job=None, store=None, trail=None, model=None, git=False, fabric=None):
    session = FakeSession(job if job is not None else fake_job())
    return ConsultService(session, fabric or FakeFabric(), records=store, audit=trail, model=model, git_check=lambda: git)


class Workspace(FakeFabric):
    """The Fabric target saying which workspace it is, by id, as the real one does."""

    def __init__(self, workspace="11111111-2222-3333-4444-555555555555", name="Sales WS"):
        super().__init__()
        self.workspace, self.name = workspace, name

    def state(self):
        return {**super().state(), "workspaceId": self.workspace, "workspaceName": self.name}


def approved(svc, answers=(), accept=True):
    """Answer, accept the remaining recommendations, submit and approve: a brief in force."""
    for qid, value in answers:
        svc.answer({"question": qid, "value": value})
    if accept:
        svc.accept({})
    svc.submit({})
    number = max(v["version"] for v in svc.brief()["versions"])
    svc.approve({"version": number, "decision": "approve"})
    return svc


def migration(svc, rest=None, db=None, trail=None):
    rest = rest or FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}])
    return MigrationService(svc._session, FakeFabric(), rest_factory=lambda: rest, sql_factory=lambda h, d: db or FakeDb(),
                            driver=lambda: "ODBC Driver 18 for SQL Server", consult=svc, audit=trail)


# -- conditions ---------------------------------------------------------------------------------------

def test_a_condition_over_something_unknown_never_holds_and_neither_does_its_negation():
    facts = {"tables.count": 3}
    assert cond.holds({">": [{"fact": "tables.count"}, 0]}, facts, {})
    assert not cond.holds({">": [{"fact": "tables.withDateColumn"}, 0]}, facts, {})
    assert not cond.holds({"not": {">": [{"fact": "tables.withDateColumn"}, 0]}}, facts, {})
    assert cond.evaluate({"and": [True, {"fact": "nope"}]}, facts, {}) is cond.UNKNOWN
    assert cond.holds({"or": [True, {"fact": "nope"}]}, facts, {})
    assert cond.holds({"in": ["data", {"answer": "A0"}]}, {}, {"A0": ["structure", "data"]})
    assert cond.evaluate({"len": {"answer": "A0"}}, {}, {"A0": ["a", "b"]}) == 2
    assert cond.holds(None, {}, {})


def test_a_fact_discovery_could_not_work_out_is_unknown_and_so_is_a_comparison_of_unlike_things():
    # None is what estate_facts leaves for a fact it could not work out: it switches nothing on, "!=" included.
    assert cond.evaluate({"fact": "pool.collation"}, {"pool.collation": None}, {}) is cond.UNKNOWN
    assert not cond.holds({"!=": [{"fact": "target.gitConnected"}, True]}, {"target.gitConnected": None}, {})
    assert cond.evaluate({"in": [["a"], "abc"]}, {}, {}) is cond.UNKNOWN  # a list looked for in a text
    assert cond.evaluate({"in": [["a"], {"fact": "objects.byType"}]}, {"objects.byType": {"x": 1}}, {}) is cond.UNKNOWN


def test_a_condition_in_another_shape_is_refused_and_references_are_listed():
    with pytest.raises(cond.ConditionError):
        cond.references({"exec": ["rm"]})
    facts, answers = cond.references({"and": [{">": [{"fact": "waves.count"}, 1]}, {"==": [{"answer": "A2"}, "all"]}]})
    assert facts == {"waves.count"} and answers == {"A2"}


def test_a_template_quotes_facts_and_answers_in_words():
    text = cond.render("{fact:pool.collation}; {fact:missing}; {answer:C1}; {answer:A0}", {"pool.collation": "CI_AS"},
                       {"C1": True, "A0": ["structure", "data"]})
    assert text == "CI_AS; unknown; yes; structure, data"


# -- the catalog ----------------------------------------------------------------------------------------

def test_the_shipped_catalog_loads_and_accepting_every_recommendation_is_enough():
    catalog = consult_api.load_catalog()
    assert catalog.version == "2.0.0" and len(catalog.questions) >= 55 and catalog.conflicts
    assert engine.accept_all_problems(catalog, consult_api.SELF_CHECK_FACTS) == []
    # A discovery that could not read everything is a contradiction a person keeps, with a reason: never accepted away.
    left = engine.accept_all_problems(catalog, [{**consult_api.SELF_CHECK_FACTS[0], "discovery.complete": False,
                                                 "discovery.failedCategories": ["Triggers"]}])
    assert left and "Triggers" in left[0]


def test_the_catalog_follows_the_consultations_sections_a_to_i():
    letters = {q.id[0] for q in CATALOG.questions}
    assert letters == {"A", "B", "C", "D", "E", "G", "H", "I"}
    by_section = {}
    for q in CATALOG.questions:
        by_section.setdefault(q.section, set()).add(q.id[0])
    assert all(len(found) == 1 for found in by_section.values()), by_section  # one letter for each section
    for qid in ("A0", "A5", "A5D", "A6", "B6", "C2", "C3", "C4", "C8", "D4", "D5", "D6", "D7", "E1", "E2", "E5", "G1", "G3",
                "G4", "H2", "H2A", "H4", "I4"):
        assert CATALOG.question(qid) is not None, qid


def test_a_catalog_with_mistakes_is_refused_whole_with_every_problem_named():
    raw = json.loads(catalogs.PATH.read_text(encoding="utf-8"))
    q = raw["questions"]
    q[0]["drives"] = ["no.such.setting"]
    q[1]["appliesWhen"] = {">": [{"fact": "no.such.fact"}, 0]}
    q[2]["appliesWhen"] = {"==": [{"answer": "E4"}, "never"]}  # reads a later question
    q[3]["defaults"] = [{"value": ["nope"], "reason": ""}]
    q[4]["separateApprover"] = {"when": True, "reason": ""}
    raw["conflicts"][0]["resolutions"] = []
    raw["history"][0]["renamed"]["A1"] = "Z9"
    raw["history"][0]["removedConflicts"] = ["K2"]
    with pytest.raises(catalogs.CatalogError) as refused:
        catalogs.parse(raw)
    text = str(refused.value)
    for expected in ("no setting 'no.such.setting'", "no fact 'no.such.fact'", "reads E4, which does not come before it",
                     "a recommendation without a reason", "a separate approver without a reason", "has no resolution a person can take",
                     "renamed to Z9, which is not a question", "contradiction K2 is said to be removed"):
        assert expected in text, expected


def test_answers_are_checked_and_cleaned_by_their_type():
    q = CATALOG.question
    assert q("A0").check(["orchestration", "structure"], {}) == ["structure", "orchestration"]  # catalog order
    assert q("A2X").check("tmp_*,  *_old\n*_old", {}) == ["tmp_*", "*_old"]
    assert q("B2").check(" Sales_WH ", {}) == "Sales_WH"
    assert q("D2").check(40.0, {}) == 40
    assert q("A5D").check("2024-02-29", {}) == "2024-02-29"
    rows = {"security.sqlUserNames": ["etl_login", "app_user"]}
    assert q("G2U").check({"ETL_LOGIN": " etl@contoso.com ", "app_user": ""}, rows) == {"etl_login": "etl@contoso.com"}
    # A blank entry is dropped, even for a user a new discovery no longer finds: the page may still hold it.
    assert q("G2U").check({"etl_login": "etl@contoso.com", "gone_user": "  "}, rows) == {"etl_login": "etl@contoso.com"}
    for question, value in (("B2", "1bad name"), ("D2", 100000), ("D2", float("nan")), ("D2", float("inf")), ("C8", 51),
                            ("A5D", "2024-13-01"), ("A0", ["everything"]), ("G2U", {"stranger": "x@contoso.com"}),
                            ("G2U", {"etl_login": "[x]"}), ("D1", "yes")):
        with pytest.raises(ValueError):
            q(question).check(value, rows)


# -- the engine -----------------------------------------------------------------------------------------

def rec(qid, value, source=engine.PERSON, **extra):
    return {"type": "answer", "question": qid, "value": value, "source": source, **extra}


def test_with_nothing_answered_the_required_questions_come_first_and_block_the_brief():
    st = engine.evaluate(CATALOG, {}, [])
    assert st.next == "D1"
    assert [b.split(":")[0] for b in st.blockers] == ["D1", "E4"]
    assert st.question("A2").status == engine.ASSUMED and st.question("A2").value == "all"
    assert not st.question("C5").applies  # C1 does not apply, so neither does the sync window


def test_an_answer_withdrawn_falls_back_to_the_recommendation_and_a_skip_keeps_it_as_an_assumption():
    records = [rec("A7", "update")]
    assert engine.evaluate(CATALOG, {}, records).question("A7").status == engine.ANSWERED
    records.append({"type": "withdraw", "question": "A7"})
    st = engine.evaluate(CATALOG, {}, records)
    assert st.question("A7").status == engine.ASSUMED and st.question("A7").value == "skip"
    st = engine.evaluate(CATALOG, {}, records + [{"type": "skip", "question": "A7"}])
    assert st.question("A7").status == engine.SKIPPED and st.question("A7").value == "skip"


def test_a_skip_after_an_answer_brings_the_recommendation_back():
    st = engine.evaluate(CATALOG, {}, [rec("A7", "update"), {"type": "skip", "question": "A7"}])
    assert st.question("A7").status == engine.SKIPPED and st.question("A7").value == "skip"


def test_an_answer_on_a_branch_that_closes_is_parked_and_comes_back_when_it_opens_again():
    facts = {"tables.withDateColumn": 2}
    records = [rec("A5", "since"), rec("A5D", "2023-01-01")]
    st = engine.evaluate(CATALOG, facts, records)
    assert st.question("A5D").status == engine.ANSWERED and st.values["A5D"] == "2023-01-01"
    st = engine.evaluate(CATALOG, facts, records + [rec("A5", "all")])
    assert st.question("A5D").status == engine.PARKED and "A5D" not in st.values
    st = engine.evaluate(CATALOG, facts, records + [rec("A5", "all"), rec("A5", "since")])
    assert st.question("A5D").status == engine.ANSWERED and st.values["A5D"] == "2023-01-01"


def test_an_answer_whose_evidence_changed_is_stale_until_a_person_confirms_it():
    facts = {"code.serverlessObjects": 2}
    records = [rec("A3S", "keep", facts={"code.serverlessObjects": 1})]
    st = engine.evaluate(CATALOG, facts, records)
    s = st.question("A3S")
    assert s.status == engine.STALE and s.changed == [{"fact": "code.serverlessObjects", "description": s.changed[0]["description"],
                                                         "before": 1, "after": 2}]
    assert any(b.startswith("A3S:") for b in st.blockers)
    again = engine.evaluate(CATALOG, facts, records + [rec("A3S", "keep", facts={"code.serverlessObjects": 2})])
    assert again.question("A3S").status == engine.ANSWERED


def test_a_new_sql_user_in_a_new_discovery_reopens_the_principal_map():
    before = {"security.principals": 3, "security.sqlUsers": 1, "security.sqlUserNames": ["etl_login"]}
    q = CATALOG.question("G2U")
    records = [rec("G2U", {"etl_login": "etl@contoso.com"}, facts=engine.evidence_of(q, before))]
    assert engine.evaluate(CATALOG, before, records).question("G2U").status == engine.ANSWERED
    after = {**before, "security.sqlUsers": 2, "security.sqlUserNames": ["etl_login", "report_login"]}
    st = engine.evaluate(CATALOG, after, records)
    assert st.question("G2U").status == engine.STALE and st.question("G2U").changed[0]["fact"] == "security.sqlUserNames"
    assert any(b.startswith("G2U:") for b in st.blockers)


def test_a_contradiction_comes_first_and_a_kept_one_lapses_when_its_answers_change():
    records = [rec("C6", "replace"), rec("E4", "never"), rec("D1", False)]
    facts = {"tables.count": 4}
    st = engine.evaluate(CATALOG, facts, records)
    k2 = next(c for c in st.conflicts if c.id == "K2")
    assert k2.status == "open" and st.next == "C6" and any(b.startswith("Contradiction") for b in st.blockers)
    keep = {"type": "resolve", "conflict": "K2", "resolution": "keep", "reason": "Test data only",
            "facts": engine.conflict_evidence(CATALOG.conflict("K2"), facts), "answers": {"C6": "replace", "E4": "never"}}
    st = engine.evaluate(CATALOG, facts, records + [keep])
    assert next(c for c in st.conflicts if c.id == "K2").status == "kept" and st.submittable
    # Kept for the answers it was kept for: answers that differ from those open it again.
    st = engine.evaluate(CATALOG, facts, records + [{**keep, "answers": {"C6": "replace", "E4": "overwrite"}}])
    assert next(c for c in st.conflicts if c.id == "K2").status == "open"


def test_a_preview_says_what_an_answer_would_open_and_move_without_recording_it():
    records: list = []
    out = engine.preview(CATALOG, {}, records, rec("A2", "include"))
    assert out["opens"] == ["A2I"] and {"setting": "plan.scopeMode", "before": "all", "after": "include"} in out["settings"]
    assert records == []


def test_an_option_can_set_several_settings():
    st = engine.evaluate(CATALOG, {"waves.count": 3}, [rec("A6", "signoff_each_wave")])
    settings = engine.settings_of(CATALOG, st)
    assert settings["run.stopOnFailure"] is True and settings["run.waveGate"] is True


def test_a_production_target_raises_the_sign_off_and_contradicts_a_looser_one():
    st = engine.evaluate(CATALOG, {}, [rec("E1", "prod")])
    assert st.question("E4").recommendation["value"] == "always" and st.question("H2").recommendation["value"] is True
    st = engine.evaluate(CATALOG, {}, [rec("E1", "prod"), rec("E4", "overwrite"), rec("E6", "act_dev")])
    assert {c.id for c in st.conflicts} == {"K9", "K11"}
    assert approvals.rule_for("never", "prod") == "always" and approvals.rule_for("overwrite", "dev") == "overwrite"


def test_running_pipelines_in_validate_while_they_may_write_to_production_is_a_contradiction():
    facts = {"pipelines.count": 2, "notebooks.count": 0}
    st = engine.evaluate(CATALOG, facts, [rec("H1", "run")])
    assert st.question("H4").value == "unsure" and [c.id for c in st.conflicts] == ["K10"]
    st = engine.evaluate(CATALOG, facts, [rec("H1", "run"), rec("H4", "no")])
    assert st.conflicts == []


# -- answers given under the first catalog ------------------------------------------------------------------------

def test_answers_made_against_the_first_catalog_are_read_under_the_questions_they_answer_now():
    old = [{**rec("A1", ["structure"]), "catalogVersion": "1.0.0"},       # what moves: A0 now
           {**rec("E4", "none"), "catalogVersion": "1.0.0"},              # storage routes: A1 now
           {**rec("G3", "always"), "catalogVersion": "1.0.0"},            # sign-off: E4 now
           {**rec("A1", ["structure", "data"]), "catalogVersion": "2.0.0"}]  # made under this catalog: A1 is storage routes
    translated = engine.translate(CATALOG, old)
    assert [r["question"] for r in translated] == ["A0", "A1", "E4", "A1"]
    assert translated[0]["madeWithCatalog"] == "1.0.0" and engine.translate(CATALOG, translated) == translated  # once only
    st = engine.evaluate(CATALOG, {"storage.locations": 2}, old[:3])
    assert st.question("A0").value == ["structure"] and st.question("A1").value == "none" and st.question("E4").value == "always"
    # A contradiction kept under the first catalog is kept for the answers under their new names.
    keep = {"type": "resolve", "conflict": "K2", "resolution": "keep", "reason": "Test data", "catalogVersion": "1.0.0",
            "facts": engine.conflict_evidence(CATALOG.conflict("K2"), {"tables.count": 4}), "answers": {"C4": "replace", "G3": "never"}}
    records = [{**rec("C4", "replace"), "catalogVersion": "1.0.0"}, {**rec("G3", "never"), "catalogVersion": "1.0.0"}, keep]
    st = engine.evaluate(CATALOG, {"tables.count": 4}, records)
    assert next(c for c in st.conflicts if c.id == "K2").status == "kept"
    gone = engine.translate(CATALOG, [{"type": "resolve", "conflict": "K7", "catalogVersion": "1.0.0"}])
    assert gone[0]["conflict"] is None


def test_a_brief_approved_against_the_first_catalog_needs_reconfirming(person, monkeypatch):
    monkeypatch.setenv("MIGRATION_CONSULTATION", "required")
    svc = approved(consult())
    doc = svc._version(1)
    svc._records.put("consult/briefs/0001", {**doc, "content": {**doc["content"], "catalogVersion": "1.0.0"}})
    policy = svc.in_force()
    assert policy.stale and policy.outdated and policy.changes[-1]["fact"] == "catalog.version"
    with pytest.raises(ApiError) as refused:
        migration(svc).start({"items": [{"id": NOTEBOOK_ID, "wave": 4}], "options": {"stopOnFailure": True}})
    assert refused.value.code == "brief_stale" and "consultation's questions" in refused.value.message
    # A version submitted under the first catalog cannot be approved under this one.
    svc.submit({})
    doc = svc._version(2)
    svc._records.put("consult/briefs/0002", {**doc, "content": {**doc["content"], "catalogVersion": "1.0.0"}})
    with pytest.raises(ApiError) as old:
        svc.approve({"version": 2, "decision": "approve"})
    assert old.value.code == "brief_stale" and "questions changed" in old.value.message


def test_what_changed_since_an_older_approval_reads_its_decisions_under_the_new_ids(person):
    svc = approved(consult())
    doc = svc._version(1)
    renamed = {v: k for k, v in CATALOG.renamed["1.0.0"].items()}
    kept = {"A2", "A2I", "A2X", "B1", "B2", "B2L", "B3", "D1", "D2", "D3", "H1"}  # the first catalog's ids this one kept
    first = set(renamed) | kept
    older = {**doc["content"], "catalogVersion": "1.0.0",
             "decisions": [{**d, "question": renamed.get(d["question"], d["question"])} for d in doc["content"]["decisions"]
                           if d["question"] in first]}
    changes = svc._changes(older, doc["content"])
    # Only the questions this catalog added are new; a renamed one is the same decision.
    assert changes and {c["kind"] for c in changes} == {"added"}
    assert {c["question"] for c in changes} == {d["question"] for d in doc["content"]["decisions"]} - first


# -- the brief ----------------------------------------------------------------------------------------

def test_the_brief_says_who_decided_what_and_what_nobody_confirmed():
    st = engine.evaluate(CATALOG, {}, [rec("D1", True, by={"kind": "person", "id": "ana"}, at="2026-01-01T00:00:00+00:00"),
                                       rec("E4", "always", source=engine.RECOMMENDATION)])
    content = briefs.draft(CATALOG, {}, st)
    d1 = next(d for d in content["decisions"] if d["question"] == "D1")
    assert d1["assumed"] is False and d1["by"]["id"] == "ana" and d1["display"] == "Yes"
    assert "A2" in content["assumptions"] and content["settings"]["llm.consent"] is True
    assert [x["question"] for x in content["separateApprover"]] == ["D1"]
    assert any(r["setting"] == "release.path" and r["status"] == "note" for r in content["recordedOnly"])
    assert any(r["setting"] == "llm.standards" for r in content["contextOnly"])
    again = briefs.draft(CATALOG, {}, engine.evaluate(CATALOG, {}, [rec("D1", True), rec("E4", "always", source=engine.RECOMMENDATION)]))
    assert briefs.content_hash(again) == briefs.content_hash(content)  # who and when are kept, the hash is what was decided
    other = briefs.draft(CATALOG, {}, engine.evaluate(CATALOG, {}, [rec("D1", False), rec("E4", "always")]))
    assert briefs.content_hash(other) != briefs.content_hash(content)


def test_a_decision_that_turned_the_recommendation_down_never_reads_as_resting_on_it():
    content = briefs.draft(CATALOG, {}, engine.evaluate(CATALOG, {}, [rec("A7", "update"), rec("E7", "dev")]))
    a7 = next(d for d in content["decisions"] if d["question"] == "A7")
    e7 = next(d for d in content["decisions"] if d["question"] == "E7")
    assert a7["reason"] is None and e7["reason"].startswith("Deploying from Git")
    noted = briefs.draft(CATALOG, {}, engine.evaluate(CATALOG, {}, [rec("A7", "update", note="Fixes are redeployed")]))
    assert next(d for d in noted["decisions"] if d["question"] == "A7")["reason"] == "Fixes are redeployed"


def test_the_version_in_force_is_the_last_approved_one_not_superseded():
    events = [{"event": "submitted", "version": 1}, {"event": "approved", "version": 1}, {"event": "submitted", "version": 2},
              {"event": "rejected", "version": 2}, {"event": "submitted", "version": 3}]
    assert briefs.approved_version(events) == 1
    events += [{"event": "superseded", "version": 1}, {"event": "approved", "version": 3}]
    assert briefs.approved_version(events) == 3 and briefs.status_of(events, 1) == "superseded"


# -- the policy -------------------------------------------------------------------------------------

def policy_of(settings, facts=None, now=None):
    content = {"settings": settings, "facts": facts or {}, "factsFingerprint": fingerprint(facts or {}), "catalogVersion": "2.0.0"}
    return Policy({"version": 1, "hash": "h" * 64, "content": content}, now if now is not None else (facts or {}), {"by": None},
                  catalog_version="2.0.0")


def test_the_parts_of_a_migration_switch_its_stages_and_the_options_carry_over():
    p = policy_of({"run.parts": ["structure"], "security.migrate": False, "run.stopOnFailure": True, "run.waveGate": True,
                   "release.gitCommit": False, "release.commitScope": "all", "warehouse.collation": "case_sensitive"})
    out = p.run_options(("warehouse", "data", "security", "notebooks", "shortcuts"))
    assert out == {"stages": ["warehouse", "shortcuts"], "stopOnFailure": True, "waveGate": True, "commitScope": "all",
                   "collation": "case_sensitive", "gitCommit": "off"}


def test_history_and_sync_become_each_tables_date_filter():
    tables = [{"key": "sales.orders", "dateColumns": [{"name": "ShippedAt"}, {"name": "OrderDate"}, {"name": "ModifiedAt"}],
               "keyColumns": ["OrderId"]},
              {"key": "dbo.log", "dateColumns": [{"name": "At"}], "keyColumns": []}]
    p = policy_of({"data.historyMode": "since", "data.historyFrom": "2023-01-01", "data.sync": True})
    filters = p.data_filters(tables)
    assert filters["sales.orders"] == {"column": "OrderDate", "from": "2023-01-01", "to": "", "sync": True,
                                       "changeColumn": "ModifiedAt", "keys": ["OrderId"]}
    assert filters["dbo.log"] == {"column": "At", "from": "2023-01-01", "to": "", "sync": False, "changeColumn": "", "keys": []}
    assert history_column(["CreatedOn", "UpdatedAt"]) == "CreatedOn" and history_column(["LastModified"]) == "LastModified"


def test_the_brief_names_each_tables_change_column_and_loads_a_keyless_one_once():
    tables = [{"key": "sales.orders", "dateColumns": [{"name": "ModifiedAt"}, {"name": "ChangedOn"}], "keyColumns": ["OrderId"]},
              {"key": "sales.audit", "dateColumns": [{"name": "ModifiedAt"}], "keyColumns": []},
              {"key": "sales.plain", "dateColumns": [{"name": "OrderDate"}], "keyColumns": ["Id"]}]
    named = policy_of({"data.sync": True, "data.watermark": "named", "data.watermarkColumns": ["changedon"], "data.keylessSync": "once"})
    filters = named.data_filters(tables)
    assert filters["sales.orders"]["changeColumn"] == "ChangedOn" and "sales.audit" not in filters and "sales.plain" not in filters
    appended = policy_of({"data.sync": True, "data.watermark": "detect", "data.keylessSync": "append"})
    assert appended.data_filters(tables)["sales.audit"] == {"column": "", "from": "", "to": "", "sync": True,
                                                            "changeColumn": "ModifiedAt", "keys": []}


def test_the_scope_rules_say_why_an_object_is_left_out():
    item = lambda oid, name: {"id": oid, "name": name}  # noqa: E731
    groups = {"a": ["schema:sales"], "b": ["schema:hr"], "c": ["folder:Reports/Daily"]}
    p = policy_of({"plan.scopeMode": "include", "plan.scopeInclude": ["schema:sales", "folder:Reports"]})
    assert p.excluded(item("a", "sales.Orders"), groups, {}) is None
    assert "Outside the schemas" in p.excluded(item("b", "hr.People"), groups, {})
    assert p.excluded(item("c", "Daily"), groups, {}) is None  # a folder under one chosen
    assert p.excluded(item("z", "ls_blob"), groups, {}) is None  # in no group: always in scope
    p = policy_of({"plan.scopeMode": "exclude", "plan.scopeExclude": ["*_bak"], "plan.serverless": "leave_out",
                   "plan.sqlModules": "leave_out"})
    assert "*_bak" in p.excluded(item("a", "sales.Orders_bak"), groups, {})
    assert "serverless" in p.excluded(item("s", "Query1"), {}, {"s": ["serverless"]})
    assert "(A3)" in p.excluded(item("v", "sales.vOrders"), {}, {"v": ["sqlModule"]})


def test_where_a_run_differs_from_the_brief_is_spelled_out():
    p = policy_of({"run.parts": ["structure"], "run.stopOnFailure": True, "storage.routes": "none"})
    out = p.differences({"stages": ["warehouse", "security", "shortcuts", "notebooks"], "stopOnFailure": False,
                         "storageRoutes": [{"account": "a", "container": "c"}]},
                        ("warehouse", "security", "shortcuts", "notebooks"), {"x": "Its name matches '*_bak'."})
    assert [d["option"] for d in out] == ["stages", "stopOnFailure", "items", "storageRoutes"]
    assert out[1]["message"] == "Stop at a wave with failures: the brief says yes, the plan has no."
    assert p.differences({"stages": ["warehouse", "security", "shortcuts"], "stopOnFailure": True},
                         ("warehouse", "security", "shortcuts", "notebooks"), {}) == []


def test_a_policy_made_on_other_facts_is_stale_and_says_what_changed():
    p = policy_of({}, {"tables.count": 3, "target.connected": True}, {"tables.count": 4, "target.connected": False})
    assert p.stale and p.changes == [{"fact": "tables.count", "description": p.changes[0]["description"], "before": 3, "after": 4}]
    assert not policy_of({}, {"tables.count": 3}, {"tables.count": 3, "target.connected": False}).stale  # environment never stales


# -- the facts --------------------------------------------------------------------------------------

def test_facts_are_counts_and_names_from_the_discovery_never_definitions():
    facts, estate = estate_facts(fake_job(), "pool01")
    assert facts["objects.total"] == 3 and facts["waves.count"] == 3 and facts["pool.warehouseName"] == "pool01"
    assert (facts["tables.count"], facts["tables.withDateColumn"], facts["tables.withoutKeys"]) == (1, 1, 1)
    assert facts["scope.groups"] == ["schema:sales", "folder:(top level)"] and estate.group_of[TABLE_ID] == ["schema:sales"]
    assert facts["discovery.complete"] is True and facts["storage.locations"] == 1
    assert (facts["pipelines.count"], facts["notebooks.count"], facts["code.sqlModules"], facts["tables.lossyTypes"]) == (1, 1, 0, 0)
    assert all(not isinstance(v, (bytes,)) and len(json.dumps(v, default=str)) < 400 for v in facts.values())


def test_a_column_fabric_stores_with_less_marks_its_table():
    job = fake_job()
    lossy = table(columns=(SqlColumn(1, "OrderId", "int", 4, 10, 0, False), SqlColumn(2, "At", "datetimeoffset", 10, 34, 7, True),
                           SqlColumn(3, "Fine", "datetime2", 8, 27, 6, True)))
    job.items[1]["schema"] = "sales"
    job.records_by_id[TABLE_ID] = SimpleNamespace(identity=SimpleNamespace(name="Orders", schema="sales"), content=lossy)
    facts, estate = estate_facts(job, "pool01")
    assert facts["tables.lossyTypes"] == 1 and "lossy" in estate.flags[TABLE_ID]
    st = engine.evaluate(CATALOG, facts, [])
    assert st.question("D4").applies and st.question("D4").value == "accept"


def test_sql_users_are_facts_and_the_brief_maps_them_to_entra():
    job, _ = plan_job()
    facts, _ = estate_facts(SimpleNamespace(**{**vars(job), "summary": {}, "snapshot": {}, "graph": {}}), "pool01")
    assert facts["security.sqlUserNames"] == ["etl_login"] and facts["security.sqlUsers"] == 1
    assert facts["security.principals"] == 5


# -- the service: answering, the brief's lifecycle ----------------------------------------------------------

def test_without_a_discovery_there_is_nothing_to_consult():
    svc = ConsultService(FakeSession(None), FakeFabric())
    with pytest.raises(ApiError) as refused:
        svc.state()
    assert refused.value.code == "no_results"


def test_an_answer_is_checked_previewed_recorded_and_audited(person):
    trail = Trail()
    svc = consult(trail=trail)
    with pytest.raises(ApiError) as bad:
        svc.answer({"question": "A0", "value": ["everything"]})
    assert bad.value.code == "invalid_answer"
    with pytest.raises(ApiError) as closed:
        svc.answer({"question": "A5D", "value": "2024-01-01"})
    assert closed.value.code == "not_applicable"
    preview = svc.answer({"question": "A2", "value": "include", "preview": True})
    assert preview["preview"]["opens"] == ["A2I"] and svc._answers() == []
    state = svc.answer({"question": "A2", "value": "include", "note": "Only sales for now"})
    a2 = next(q for q in state["questions"] if q["id"] == "A2")
    assert a2["status"] == "answered" and a2["by"] == {"kind": "person", "id": "Test Operator"} and a2["note"] == "Only sales for now"
    a2i = next(q for q in state["questions"] if q["id"] == "A2I")
    assert a2i["applies"] and a2i["choices"] == ["schema:sales", "folder:(top level)"]
    assert trail.of("consult.answered")[0]["details"]["value"] == "These schemas and folders only"
    record = svc._answers()[-1]
    assert record["catalogVersion"] == CATALOG.version and record["source"] == "person"


def test_a_page_that_read_the_answers_before_another_changed_one_is_told(person):
    svc = consult()
    seq = svc.state()["seq"]
    svc.answer({"question": "A7", "value": "update"})
    with pytest.raises(ApiError) as moved:
        svc.answer({"question": "A7", "value": "skip", "seq": seq})
    assert moved.value.code == "answer_changed"
    # A sequence number in other digits is no number: never mistaken for one ("٣" is a digit to isdigit).
    with pytest.raises(ApiError) as moved:
        svc.answer({"question": "A7", "value": "skip", "seq": "٣"})
    assert moved.value.code == "answer_changed"


def test_only_a_person_answers_resolves_or_submits_and_the_agent_may_only_propose(person):
    svc = consult()
    with context.bound(actor=AGENT):
        for call, body in ((svc.answer, {"question": "A7", "value": "update"}), (svc.skip, {"question": "A7"}),
                           (svc.accept, {}), (svc.submit, {}), (svc.decide, {"ids": ["x"], "decision": "confirm"}),
                           (svc.deviation, {"items": [], "reason": "x"}), (svc.resolve, {"conflict": "K2", "resolution": "keep"})):
            with pytest.raises(ApiError) as refused:
                call(body)
            assert refused.value.code == "person_only", call
        out = svc.propose({"text": "Update objects already in Fabric.", "useAi": False})
    assert [p["question"] for p in out["proposals"]] == ["A7"] and svc._answers() == []


def test_accepting_every_recommendation_gives_a_brief_a_person_can_submit_and_approve(person):
    trail = Trail()
    svc = consult(trail=trail)
    state = svc.accept({})
    assert state["submittable"] and "D1" in state["accepted"] and state["blockers"] == []
    out = svc.submit({"note": "First pass"})
    v1 = out["versions"][0]
    assert v1["version"] == 1 and v1["status"] == "submitted" and v1["hash"]
    with context.bound(actor=context.system()):
        with pytest.raises(ApiError) as machine:
            svc.approve({"version": 1, "decision": "approve"})
    assert machine.value.code == "person_only"
    out = svc.approve({"version": 1, "decision": "approve", "comment": "Agreed with the client"})
    assert out["versions"][0]["status"] == "approved" and out["versions"][0]["selfApproved"] is True
    policy = svc.in_force()
    assert policy.version == 1 and not policy.stale and policy.signoff() == "overwrite"
    assert trail.actions()[-2:] == ["brief.submitted", "brief.approved"]
    md, media, name = svc.export({"format": "md"})
    assert name == "migration-brief-v1.md" and media.startswith("text/markdown")
    text = md.decode("utf-8")
    assert "# Migration brief, version 1" in text and "**Approved** by Test Operator" in text and "Agreed with the client" in text
    assert "## What the accelerator acts on" in text and "| `release.signoff` | overwrite |" in text
    assert "## Sent to the AI with each draft" in text


def test_accepting_one_question_answers_only_that_one(person):
    svc = consult()
    out = svc.accept({"questions": ["A7"]})
    assert out["accepted"] == ["A7"] and [r["question"] for r in svc._answers()] == ["A7"]


def test_a_newer_submission_supersedes_the_one_waiting_and_approving_supersedes_the_one_in_force(person):
    svc = approved(consult())
    svc.answer({"question": "E4", "value": "always"})
    svc.submit({})
    svc.answer({"question": "A7", "value": "update"})
    svc.submit({})
    statuses = {v["version"]: v["status"] for v in svc.brief()["versions"]}
    assert statuses == {1: "approved", 2: "superseded", 3: "submitted"}
    with pytest.raises(ApiError) as old:
        svc.approve({"version": 2, "decision": "approve"})
    assert old.value.code == "not_waiting"
    svc.approve({"version": 3, "decision": "approve"})
    statuses = {v["version"]: v["status"] for v in svc.brief()["versions"]}
    assert statuses == {1: "superseded", 2: "superseded", 3: "approved"} and svc.in_force().version == 3
    summary = svc.state()["brief"]
    assert summary["approved"] == 3 and summary["sameAsApproved"] and summary["changes"] == []


def test_a_brief_that_cannot_be_submitted_says_why_and_a_rejected_one_is_never_in_force(person):
    svc = consult()
    with pytest.raises(ApiError) as early:
        svc.submit({})
    assert early.value.code == "brief_not_ready" and "D1" in early.value.message
    svc.accept({})
    svc.submit({})
    svc.approve({"version": 1, "decision": "reject", "comment": "Budget too high"})
    assert svc.in_force() is None and svc.brief()["versions"][0]["status"] == "rejected"


def test_a_version_made_on_an_estate_that_has_changed_cannot_be_approved(person):
    svc = consult()
    svc.accept({})
    svc.submit({})
    job = fake_job()
    job.items = job.items[:2]  # a new discovery: one object fewer
    svc._session.job = job
    with pytest.raises(ApiError) as stale:
        svc.approve({"version": 1, "decision": "approve"})
    assert stale.value.code == "brief_stale"


def test_a_decision_that_loosens_a_safeguard_can_require_a_separate_approver(person, monkeypatch):
    svc = consult()
    svc.answer({"question": "D1", "value": True})
    svc.accept({})
    svc.submit({})
    monkeypatch.setenv("MIGRATION_BRIEF_APPROVAL", "separate")
    with pytest.raises(ApiError) as same:
        svc.approve({"version": 1, "decision": "approve"})
    assert same.value.code == "separate_approver" and "D1" in same.value.message
    with context.bound(actor=context.Actor(context.PERSON, "Second Person")):
        out = svc.approve({"version": 1, "decision": "approve"})
    assert out["versions"][0]["selfApproved"] is False


def test_a_contradiction_is_resolved_by_taking_the_finding_or_keeping_the_answers_with_a_reason(person):
    trail = Trail()
    svc = consult(trail=trail)
    svc.answer({"question": "C6", "value": "replace"})
    state = svc.answer({"question": "E4", "value": "never"})
    assert [c["id"] for c in state["conflicts"] if c["status"] == "open"] == ["K2"]
    with pytest.raises(ApiError) as why:
        svc.resolve({"conflict": "K2", "resolution": "keep"})
    assert why.value.code == "reason_required"
    state = svc.resolve({"conflict": "K2", "resolution": "keep", "reason": "Only test data is replaced"})
    assert state["conflicts"][0]["status"] == "kept" and state["conflicts"][0]["reason"] == "Only test data is replaced"
    svc.answer({"question": "A0", "value": ["data", "orchestration"]})
    state = svc.resolve({"conflict": "K1", "resolution": "take"})
    a0 = next(q for q in state["questions"] if q["id"] == "A0")
    assert a0["value"] == ["structure", "data", "orchestration"] and a0["source"] == "finding"
    assert [d["resolution"] for d in trail.of("consult.resolved") for d in [d["details"]]] == ["keep", "take"]


def test_answers_belong_to_their_project(tmp_path, person):
    one, two = Store(tmp_path / "one"), Store(tmp_path / "two")
    svc = consult(store=one)
    svc.answer({"question": "A7", "value": "update"})
    svc.switch(two)
    assert next(q for q in svc.state()["questions"] if q["id"] == "A7")["status"] == "assumed"
    svc.switch(one)
    assert next(q for q in svc.state()["questions"] if q["id"] == "A7")["status"] == "answered"


def test_a_projects_answers_from_the_first_catalog_carry_over(tmp_path, person):
    store = Store(tmp_path / "p")
    for seq, (qid, value) in enumerate((("A1", ["structure", "orchestration"]), ("G3", "always"), ("I1", "observe")), start=1):
        store.append(consult_api.ANSWERS, {**rec(qid, value, source=engine.RECOMMENDATION), "catalogVersion": "1.0.0",
                                           "by": {"kind": "person", "id": "ana"}, "at": "2026-10-16T07:12:19+00:00"})
    svc = consult(store=store)
    questions = {q["id"]: q for q in svc.state()["questions"]}
    assert questions["A0"]["value"] == ["structure", "orchestration"] and questions["A0"]["status"] == "answered"
    assert questions["E4"]["value"] == "always" and questions["E6"]["value"] == "observe"
    svc.withdraw({"question": "E4"})  # written under this catalog's id, and read so
    assert next(q for q in svc.state()["questions"] if q["id"] == "E4")["status"] == "assumed"


# -- the run starts under the brief ---------------------------------------------------------------------

def start(svc, mig, options=None, items=None, **extra):
    body = {"items": items or [{"id": NOTEBOOK_ID, "wave": 4}], "options": {"stopOnFailure": True, **(options or {})}, **extra}
    out = mig.start(body)
    wait_until(lambda: mig.state()["state"] != "running")
    return out


def test_a_required_consultation_refuses_a_run_without_an_approved_brief(person, monkeypatch):
    monkeypatch.setenv("MIGRATION_CONSULTATION", "required")
    svc = consult()
    mig = migration(svc)
    with pytest.raises(ApiError) as none:
        start(svc, mig)
    assert none.value.code == "brief_required"
    approved(svc)
    start(svc, mig)
    brief = mig._run.pins["brief"]
    assert brief["version"] == 1 and brief["hash"] == svc.in_force().hash and brief["differences"] == []


def test_a_run_under_a_brief_that_no_longer_fits_the_estate_is_refused(person, monkeypatch):
    monkeypatch.setenv("MIGRATION_CONSULTATION", "required")
    svc = approved(consult())
    job = fake_job()
    job.items = job.items + [{"id": "synapse://notebook/Extra", "name": "Extra", "type": "Notebook"}]
    svc._session.job = job
    with pytest.raises(ApiError) as stale:
        start(svc, migration(svc))
    assert stale.value.code == "brief_stale" and "Objects discovered: 3 → 4" in stale.value.message


def test_a_run_that_differs_from_the_brief_needs_a_reason_and_the_reason_is_kept(person, monkeypatch):
    monkeypatch.setenv("MIGRATION_CONSULTATION", "required")
    trail = Trail()
    svc = approved(consult(trail=trail))
    mig = migration(svc, trail=trail)
    with pytest.raises(ApiError) as unexplained:
        start(svc, mig, options={"stopOnFailure": False})
    assert unexplained.value.code == "deviation_unexplained" and "Stop at a wave with failures" in unexplained.value.message
    run = start(svc, mig, options={"stopOnFailure": False}, deviationReason="One object; no waves to stop between")
    pinned = mig._run.pins["brief"]
    assert pinned["differences"][0]["option"] == "stopOnFailure" and pinned["deviation"]["reason"].startswith("One object")
    assert pinned["deviation"]["seq"] == 1
    assert trail.of("plan.deviation")[0]["details"]["reason"] == "One object; no waves to stop between"
    assert trail.of("run.requested")[-1]["details"]["brief"] == 1 and run["runId"] == "001"
    # Recorded once with the run, it covers the same differences at the next Start.
    mig.reset()
    start(svc, mig, options={"stopOnFailure": False})
    assert mig._run.pins["brief"]["deviation"]["reason"].startswith("One object")


def test_a_reason_given_with_a_start_that_is_refused_is_not_kept(person, monkeypatch):
    monkeypatch.setenv("MIGRATION_CONSULTATION", "required")
    trail = Trail()
    svc = approved(consult(trail=trail))
    mig = migration(svc, rest=FakeRest(capacity=None), trail=trail)  # a workspace with no capacity: refused at Start
    with pytest.raises(ApiError) as refused:
        start(svc, mig, options={"stopOnFailure": False}, deviationReason="A pilot")
    assert refused.value.code == "no_fabric_capacity"
    assert trail.of("plan.deviation") == [] and svc._records.records(consult_api.DEVIATIONS) == []


def test_plan_says_how_it_stands_against_the_brief_and_a_deviation_can_be_recorded_for_it(person, monkeypatch):
    monkeypatch.setenv("MIGRATION_CONSULTATION", "required")
    svc = approved(consult())
    mig = migration(svc)
    out = mig.analyze({"items": [{"id": NOTEBOOK_ID, "wave": 4}], "options": {"stopOnFailure": False}})
    assert out["brief"]["version"] == 1 and out["brief"]["differences"][0]["option"] == "stopOnFailure"
    assert out["brief"]["explained"] is False
    with pytest.raises(ApiError) as why:
        svc.deviation({"items": [{"id": NOTEBOOK_ID, "wave": 4}], "options": {"stopOnFailure": False}})
    assert why.value.code == "reason_required"
    recorded = svc.deviation({"items": [{"id": NOTEBOOK_ID, "wave": 4}], "options": {"stopOnFailure": False}, "reason": "Pilot run"})
    assert recorded["recorded"]
    assert mig.analyze({"items": [{"id": NOTEBOOK_ID, "wave": 4}], "options": {"stopOnFailure": False}})["brief"]["explained"]


def test_an_advisory_consultation_records_differences_but_refuses_nothing(person):
    svc = approved(consult())
    mig = migration(svc)
    start(svc, mig, options={"stopOnFailure": False})
    assert mig._run.pins["brief"]["differences"][0]["option"] == "stopOnFailure" and mig._run.pins["brief"]["deviation"] is None
    mig.reset()
    bare = consult()
    start(bare, migration(bare))  # no brief at all


def test_the_brief_names_the_warehouse_and_lakehouse_and_sets_the_signoff_rule(person, monkeypatch):
    svc = approved(consult(), [("B2", "Sales_WH"), ("B2L", "Sales_LH"), ("E4", "always")])
    mig = migration(svc)
    monkeypatch.delenv("MIGRATION_SIGNOFF")
    run = start(svc, mig)
    assert run["warehouse"] == "Sales_WH" and mig._run.settings["lakehouse"] == "Sales_LH"
    assert run["status"] == "awaiting_approval" and run["approval"]["policy"] == "always"
    assert mig.policy()["signoff"] == "always"


def test_a_production_target_signs_off_every_run_whatever_the_rest_says(person, monkeypatch):
    svc = consult()
    svc.answer({"question": "E1", "value": "prod"})
    svc.answer({"question": "E4", "value": "never"})
    svc.accept({})
    with pytest.raises(ApiError) as open_one:
        svc.submit({})
    assert "production workspace" in open_one.value.message  # K9
    svc.resolve({"conflict": "K9", "resolution": "keep", "reason": "Production signs off every run anyway"})
    approved(svc)
    monkeypatch.delenv("MIGRATION_SIGNOFF")
    run = start(svc, migration(svc))
    assert run["status"] == "awaiting_approval" and run["approval"]["policy"] == "always"


def test_the_brief_never_loosens_a_signoff_rule_this_machine_is_set_to(monkeypatch):
    monkeypatch.setenv("MIGRATION_SIGNOFF", "always")
    assert approvals.rule_for("never") == "always"
    monkeypatch.setenv("MIGRATION_SIGNOFF", "never")
    assert approvals.rule_for("overwrite") == "overwrite"
    monkeypatch.delenv("MIGRATION_SIGNOFF")
    assert approvals.rule_for("never") == "never" and approvals.rule_for(None) == "overwrite"


def test_a_run_waiting_for_sign_off_is_not_started_once_the_brief_has_changed(person, monkeypatch):
    monkeypatch.setenv("MIGRATION_CONSULTATION", "required")
    monkeypatch.delenv("MIGRATION_SIGNOFF")
    svc = approved(consult(), [("E4", "always")])
    mig = migration(svc)
    assert start(svc, mig)["status"] == "awaiting_approval"
    approved(svc, [("A7", "update")], accept=False)  # another brief is approved while the run waits
    with pytest.raises(ApiError) as changed:
        mig.approve({"decision": "approve"})
    assert changed.value.code == "brief_changed" and "Decline this run" in changed.value.message
    assert mig.approve({"decision": "reject"})["status"] == "rejected"  # declining it is always possible


def test_a_brief_that_signs_off_each_wave_pauses_the_run_until_a_person_releases_the_next(person):
    trail = Trail()
    svc = approved(consult(trail=trail), [("A0", ["structure", "orchestration"]), ("A6", "signoff_each_wave")])
    mig = migration(svc, trail=trail)
    items = [{"id": TABLE_ID, "wave": 1}, {"id": NOTEBOOK_ID, "wave": 2}]
    run = start(svc, mig, options={"stages": NO_DATA, "waveGate": True}, items=items)
    assert run["options"]["waveGate"] is True
    assert wait_until(lambda: mig.state()["pausedReason"] == run_state.WAVE_SIGNOFF)
    state = mig.state()
    assert state["waitingWave"] == 2 and [i["status"] for i in state["items"]] == ["COMPLETED", "PENDING"]
    with context.bound(actor=AGENT):
        with pytest.raises(ApiError) as agent:
            mig.control({"action": "resume"})
    assert agent.value.code == "person_only" and mig.state()["pausedReason"] == run_state.WAVE_SIGNOFF
    mig.control({"action": "resume", "comment": "Tables checked"})
    assert wait_until(lambda: mig.state()["state"] == "completed")
    assert mig.state()["releasedWaves"] == [2]
    assert trail.of("run.wave_signed_off")[0]["details"] == {"wave": 2, "comment": "Tables checked"}


def test_the_first_wave_with_work_in_it_is_never_held_for_a_sign_off():
    deferred = Source("ls://x", "SomeLinkedService", "Linked Service", 1, DEFERRED, reason="Set up by hand")
    notebook = Source(NOTEBOOK_ID, "LoadSales", "Notebook", 2, NOTEBOOK, payload=notebook_payload())
    run = MigrationRun("001", [deferred, notebook], WS, "Sales WS", "pool01")
    run.settings = {"waveGate": True}
    Migrator(run, lambda: FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}]), lambda h, d: FakeDb()).execute()
    assert run.status == run_state.COMPLETED and run.paused_reason is None


def test_a_sql_user_the_brief_maps_is_migrated_as_its_entra_principal():
    job, plan = plan_job()
    by = {s.name: s for s in sources_for(plan, job, "pool01", {"ETL_LOGIN": "etl@contoso.com"})}
    etl = by["etl_login"]
    assert etl.kind == SECURITY and etl.object_name == "etl@contoso.com"
    grants = [s for _, s in security.plan_principal(etl.payload, etl.object_name).permissions]
    assert grants == ["GRANT SELECT ON OBJECT::[sales].[Orders] TO [etl@contoso.com];"]
    role = security.plan_principal(by["reporting"].payload, "reporting")
    assert "ALTER ROLE [reporting] ADD MEMBER [etl@contoso.com];" in [s for _, s in role.members]
    assert not any("SQL user" in n for n in role.notes)
    unmapped = {s.name: s for s in sources_for(plan, job, "pool01")}
    assert unmapped["etl_login"].kind == DEFERRED and "Consult" in unmapped["etl_login"].reason


def test_sensitive_columns_the_brief_masks_get_the_default_mask():
    job = fake_job()
    plain = sources_for([{"id": TABLE_ID, "wave": 2}], job, "pool01")[0]
    masked = sources_for([{"id": TABLE_ID, "wave": 2}], job, "pool01", masks=["sales.orders.customer", "*.*.amount"])[0]
    assert plain.masks == () and [(m.column, m.function) for m in masked.masks] == [("Customer", "default()"), ("Amount", "default()")]


def test_the_brief_sets_the_parallel_copies_the_schedules_and_the_masks_of_a_run(person):
    svc = approved(consult(), [("C8", 12), ("G1", ["sales.Orders.Customer"]), ("G1M", "mask")])
    mig = migration(svc)
    start(svc, mig, options={"stages": ["warehouse"]}, items=[{"id": TABLE_ID, "wave": 2}])
    settings = mig._run.settings
    assert settings["parallelCopies"] == 12 and settings["schedules"] == "off" and settings["maskColumns"] == ["sales.Orders.Customer"]
    definition = datapipeline.definition([], "conn", datapipeline.WarehouseTarget("a", "e", "w", WS), settings["parallelCopies"])
    assert definition["properties"]["activities"][0]["typeProperties"]["batchCount"] == 12
    assert datapipeline.definition([], "conn", datapipeline.WarehouseTarget("a", "e", "w", WS), 500)["properties"]["activities"][0][
        "typeProperties"]["batchCount"] == datapipeline.MAX_PARALLEL_COPIES


def test_schedules_are_switched_on_only_where_the_brief_says_so_and_synapse_had_started_them():
    trigger = {"name": "Nightly", "properties": {"type": "ScheduleTrigger", "runtimeState": "Started",
                                                 "typeProperties": {"recurrence": {"frequency": "Day", "interval": 1}},
                                                 "pipelines": [{"pipelineReference": {"referenceName": "Load"}}]}}
    assert jobs.schedule_bodies(trigger)[0][0][1]["enabled"] is False
    (_, body), = jobs.schedule_bodies(trigger, as_in_synapse=True)[0]
    assert body["enabled"] is True
    stopped = {**trigger, "properties": {**trigger["properties"], "runtimeState": "Stopped"}}
    assert jobs.schedule_bodies(stopped, as_in_synapse=True)[0][0][1]["enabled"] is False


def test_validate_checks_what_the_run_made_its_lakehouse_and_its_principals(person, monkeypatch):
    seen = {}

    class Validator:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs, warehouse=args[3])

        def run(self, sources):
            seen["sources"] = sources
            return []

    svc = approved(consult(), [("B2", "Sales_WH"), ("B2L", "Sales_LH")])
    mig = migration(svc)
    monkeypatch.setattr(migration_api, "Validator", Validator)
    mig.validate({})
    assert seen["warehouse"] == "Sales_WH" and seen["lakehouse_name"] == "Sales_LH"
    start(svc, mig)
    mig._run.settings["sqlUserMap"] = {}  # the run mapped nobody: that is what is checked, not the brief now
    mig.validate({})
    assert seen["lakehouse_name"] == "Sales_LH"


def test_the_brief_can_have_the_commit_take_everything_waiting(person, monkeypatch):
    captured = {}

    def fake_commit(client, workspace_id, comment, only=None, exclude=()):
        captured["only"] = only
        return SimpleNamespace(state="committed", items=1, skipped=[], others=[], commit="abc")

    monkeypatch.setattr(fabric_git, "connection", lambda client, ws: SimpleNamespace(connected=True, branch="main"))
    monkeypatch.setattr(fabric_git, "commit", fake_commit)
    monkeypatch.setattr(fabric_git, "commit_url", lambda conn, sha: None)
    monkeypatch.setattr(fabric_git, "pull_request_url", lambda conn: None)
    svc = approved(consult(git=True), [("E0S", "all")])
    mig = migration(svc)
    start(svc, mig, options={"commitScope": "all"})
    assert wait_until(lambda: "only" in captured) and captured["only"] is None


# -- the LLM gate ---------------------------------------------------------------------------------------

class Model:
    def __init__(self, data=None):
        self.prompts, self.data = [], data or {"proposals": []}

    def available(self):
        return {"available": True, "model": "fake", "timeoutSeconds": 1, "message": None}

    def complete(self, prompt):
        self.prompts.append(prompt)
        return claude_cli.Answer(code="CREATE PROCEDURE sales.LoadOrders AS BEGIN SELECT 1 AS a; END", confidence="high",
                                 model="fake", cost_usd=3.0, context=["Case-insensitive Warehouse"])

    def ask(self, prompt, schema, system):
        self.prompts.append(prompt)
        return Reply(data=self.data, model="fake", cost_usd=0.5)


def transforms(svc, model, tmp_path, trail=None):
    from test_transform import FakeSession as TransformSession

    return TransformService(TransformSession(), model=model, store=TransformStore(tmp_path / "t"), prompts=PROMPTS,
                            synchronous=True, consult=svc, audit=trail)


def test_drafts_need_consent_from_the_approved_brief_when_consultation_is_required(person, monkeypatch, tmp_path):
    monkeypatch.setenv("MIGRATION_CONSULTATION", "required")
    trail = Trail()
    svc = consult(trail=trail)
    model = Model()
    t = transforms(svc, model, tmp_path)
    with pytest.raises(ApiError) as asked:
        t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    assert asked.value.code == "llm_no_consent" and asked.value.status == 403
    svc.answer({"question": "D1", "value": True})
    with pytest.raises(ApiError) as unapproved:
        t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    assert "no brief that says so is approved" in unapproved.value.message
    approved(svc, [("D2", 5)])
    t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    assert model.prompts and svc._spent() == 3.0
    t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    with pytest.raises(ApiError) as spent:
        t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    assert spent.value.code == "llm_budget_reached" and "$6.00 of its $5.00" in spent.value.message
    assert trail.of("llm.refused")[-1]["details"]["budget"] == 5.0


def test_a_budget_a_person_sets_holds_even_with_no_brief(person, tmp_path):
    svc = consult()
    svc.answer({"question": "D1", "value": True})
    svc.answer({"question": "D2", "value": 2})
    t = transforms(svc, Model(), tmp_path)
    t.generate({"ids": ["sql://p/sales/LoadOrders"]})  # advisory, no brief: allowed, and it costs 3.0
    with pytest.raises(ApiError) as spent:
        t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    assert spent.value.code == "llm_budget_reached"


def test_withdrawing_consent_stops_drafts_at_once_even_with_no_brief(person, tmp_path):
    svc = consult()
    model = Model()
    t = transforms(svc, model, tmp_path)
    t.generate({"ids": ["sql://p/sales/LoadOrders"]})  # advisory, nobody asked yet: as before there were briefs
    svc.answer({"question": "D1", "value": False})
    with pytest.raises(ApiError) as withdrawn:
        t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    assert withdrawn.value.code == "llm_no_consent"
    assert t.settings()["consent"]["allowed"] is False


def test_a_later_yes_never_loosens_an_approved_no_for_the_consultations_help(person, monkeypatch):
    monkeypatch.setattr(claude_cli, "executable", lambda: "claude")
    model = Model({"proposals": []})
    svc = approved(consult(model=model))  # D1 accepted as recommended: no
    svc.answer({"question": "D1", "value": True})  # a yes given since, in no approved brief
    assert svc.llm_gate("consult")["allowed"] is False
    assert svc.propose({"text": "Every run must be signed off."})["source"] == "rules" and model.prompts == []


# -- the migration context, sent with every draft ------------------------------------------------------------

BRIEFED = [("D1", True), ("D2", 50), ("B3", "Prefix every view with v_. Never use SELECT *."), ("B6", "fn_<area>_<what>"),
           ("G1", ["sales.Orders.Customer"]), ("G1M", "mask"), ("I3", "The Sales Power BI report"), ("I4", "T-SQL first")]


def test_the_migration_context_is_made_from_the_approved_brief_and_says_what_it_decided(person):
    svc = approved(consult(), BRIEFED)
    made = svc.context_file()
    text = made.text
    assert text.startswith("# Migration context") and f"version {made.brief_version}" in text
    for expected in ("The Fabric Warehouse is `pool01`", "Prefix every view with v_", "fn_<area>_<what>", "`sales.Orders.Customer`",
                     "default dynamic data mask", "The Sales Power BI report", "T-SQL first", "In your answer, list in `context`"):
        assert expected in text, expected
    assert made.hash == context_files.render(svc.in_force()).hash  # the same brief makes the same file, byte for byte
    view = svc.context_view()
    assert view["hash"] == made.hash and view["file"] == "MIGRATION_CONTEXT.md" and view["text"] == text
    body, media, name = svc.context_export(made.hash)
    assert body.decode("utf-8") == text and media.startswith("text/markdown") and name.startswith("MIGRATION_CONTEXT-")
    with pytest.raises(ApiError):
        svc.context_export("0" * 64)


def test_without_an_approved_brief_there_is_no_migration_context():
    view = consult().context_view()
    assert view["hash"] is None and "No migration brief" in view["reason"]


def test_every_draft_is_sent_the_migration_context_and_records_which_one(person, tmp_path):
    trail = Trail()
    svc = approved(consult(), BRIEFED)
    model = Model()
    t = transforms(svc, model, tmp_path, trail=trail)
    made = svc.context_file()
    t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    prompt = model.prompts[-1]
    assert "## The migration context (MIGRATION_CONTEXT.md)" in prompt and made.text.strip() in prompt
    assert "Standards from the approved migration brief" not in prompt  # the standards are in the context now
    draft = t.object("sql://p/sales/LoadOrders")["draft"]
    assert draft["context"] == made.reference() and draft["contextApplied"] == ["Case-insensitive Warehouse"]
    assert draft["contextStale"] is False
    assert trail.of("transform.drafted")[-1]["details"]["contextHash"] == made.hash
    assert t.settings()["context"]["hash"] == made.hash
    assert t.prompt("sql://p/sales/LoadOrders")["context"]["hash"] == made.hash
    accepted = t.accept({"id": "sql://p/sales/LoadOrders"})["accepted"]
    assert accepted["context"]["hash"] == made.hash and accepted["contextApplied"] == ["Case-insensitive Warehouse"]


def test_a_draft_made_with_an_earlier_migration_context_is_flagged(person, tmp_path):
    svc = approved(consult(), BRIEFED)
    t = transforms(svc, Model(), tmp_path)
    t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    approved(svc, [("B3", "Keywords in lower case.")], accept=False)
    assert t.object("sql://p/sales/LoadOrders")["draft"]["contextStale"] is True
    assert next(i for i in t.list()["items"] if i["objectId"] == "sql://p/sales/LoadOrders")["draft"]["contextStale"] is True


def test_a_brief_approved_again_with_the_same_decisions_leaves_every_draft_current(person, tmp_path):
    svc = approved(consult(), BRIEFED)
    t = transforms(svc, Model(), tmp_path)
    t.generate({"ids": ["sql://p/sales/LoadOrders"]})
    first = svc.context_file()
    approved(svc, [("E4", "always")], accept=False)  # a decision that never reaches the model: who signs runs off
    again = svc.context_file()
    assert again.hash != first.hash and again.decisions_hash == first.decisions_hash  # only the version line moved
    assert t.object("sql://p/sales/LoadOrders")["draft"]["contextStale"] is False
    assert t.object("sql://p/sales/LoadOrders")["draft"]["context"]["decisionsHash"] == first.decisions_hash


def test_a_brief_stops_fitting_once_another_fabric_workspace_is_the_target(person, monkeypatch):
    fabric = Workspace()
    svc = approved(consult(fabric=fabric), [("E1", "dev")])
    assert svc.in_force().target == {"workspaceId": fabric.workspace, "workspaceName": "Sales WS"} and not svc.in_force().stale
    fabric.ready = False  # disconnected for a while: it still fits
    assert not svc.in_force().stale
    fabric.ready, fabric.workspace, fabric.name = True, "99999999-8888-7777-6666-555555555555", "Sales PROD"
    policy = svc.in_force()
    assert policy.stale and policy.target_moved and not policy.outdated
    assert svc.brief()["summary"]["factChanges"][-1]["fact"] == "target.workspace"
    monkeypatch.setenv("MIGRATION_CONSULTATION", "required")
    job, pool = svc._session.migration_snapshot()
    with pytest.raises(ApiError) as refused:
        svc.run_gate([{"id": NOTEBOOK_ID, "wave": 4}], {"stages": ["notebooks"]}, job, pool)
    assert refused.value.code == "brief_stale" and "The Fabric target changed since brief version" in refused.value.message
    assert "Sales WS → Sales PROD" in refused.value.message
    approved(svc, accept=False)  # reconfirmed with the new workspace connected
    assert svc.in_force().target["workspaceName"] == "Sales PROD" and not svc.in_force().stale


def test_validate_runs_nothing_that_may_write_to_production_unless_a_person_kept_it(person):
    svc = approved(consult(), [("H4", "unsure")])
    mig = migration(svc)
    runtime = {"probe": True, "run": ["pipeline", "notebook"]}
    with pytest.raises(ApiError) as refused:
        mig.start_validation({"runtime": runtime})
    assert refused.value.code == "external_writes" and "(H4)" in refused.value.message
    assert mig.runtime_refusal({"probe": True, "run": []}) is None  # querying and compiling changes nothing
    kept = consult()
    for qid, value in (("H1", "run"), ("H4", "unsure")):
        kept.answer({"question": qid, "value": value})
    kept.resolve({"conflict": "K10", "resolution": "keep", "reason": "They write to test copies only"})
    approved(kept)
    assert migration(kept).runtime_refusal(runtime) is None


def test_validate_counts_rows_as_the_run_loaded_them_when_the_request_names_no_filters(person, monkeypatch):
    seen = {}
    real = migration_api.apply_stages

    def capture(sources, options):
        seen["filters"] = dict(options.get("dataFilters") or {})
        return real(sources, options)

    class Validator:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, sources):
            return []

    monkeypatch.setattr(migration_api, "apply_stages", capture)
    monkeypatch.setattr(migration_api, "Validator", Validator)
    svc = approved(consult())
    mig = migration(svc)
    start(svc, mig)
    mig._run.pins["dataFilters"] = {"sales.orders": {"column": "OrderDate", "from": "2024-01-01"}}
    mig.validate({})  # what the agent's validations send: no filters of their own
    assert list(seen["filters"]) == ["sales.orders"]
    mig.validate({"options": {"dataFilters": {}}})  # the page's own choice stands
    assert seen["filters"] == {}


def test_credentials_come_only_with_a_persons_request(monkeypatch):
    svc = consult()
    mig = migration(svc)
    secret = {"LS_Sql": {"authType": "Basic", "username": "u", "password": "p"}}
    with context.bound(actor=context.Actor(context.AGENT, "orchestrator")):
        with pytest.raises(ApiError) as agent:
            mig.start({"items": [{"id": NOTEBOOK_ID, "wave": 4}], "options": {"stages": ["notebooks"]}, "credentials": secret})
        with pytest.raises(ApiError) as signoff:
            mig.approve({"decision": "approve", "credentials": secret})
    assert agent.value.code == "person_only" and signoff.value.code == "person_only" and mig._credentials == {}


def test_a_minimal_context_keeps_names_and_context_answers_from_the_model(person, tmp_path):
    svc = approved(consult(), BRIEFED + [("G4", "minimal")])
    made = svc.context_file()
    assert made.minimal and "sales.Orders.Customer" not in made.text and "Power BI" not in made.text
    assert "Some columns hold personal or sensitive data" in made.text
    model = Model()
    transforms(svc, model, tmp_path).generate({"ids": ["sql://p/sales/LoadOrders"]})
    assert "`sales.Orders` (Table)" in model.prompts[-1] and "OrderId int" not in model.prompts[-1]  # named, no columns


def test_the_brief_can_keep_drafts_to_objects_the_rules_cannot_convert(person, tmp_path):
    svc = approved(consult(), [("D1", True), ("D6", "needs_work")])
    t = transforms(svc, Model(), tmp_path)
    with pytest.raises(ApiError) as clean:
        t.generate({"ids": ["sql://p/sales/Orders"]})  # a table the rules convert as it is
    assert clean.value.code == "llm_scope" and "(D6)" in clean.value.message
    t.generate({"ids": ["sql://p/sales/LoadOrders"]})  # uses sys.pdw_* and CTAS: the rules leave problems
    assert t.object("sql://p/sales/LoadOrders")["needsWork"] is True
    anything = approved(consult(), [("D1", True), ("D6", "any")])
    transforms(anything, Model(), tmp_path / "any").generate({"ids": ["sql://p/sales/Orders"]})


# -- help: answers from notes, explanations, the questionnaire ----------------------------------------------

def test_the_rules_read_plain_answers_from_notes():
    found = {p["question"]: p["value"] for p in rule_proposals(
        "We want structure only for now. Every run must be signed off. No AI please. Load the last 3 years. "
        "Budget up to $40. Run both side by side for 4 cycles, and copy 8 tables at a time.",
        today=__import__("datetime").date(2026, 10, 16))}
    assert found == {"A0": ["structure"], "E4": "always", "D1": False, "A5": "since", "A5D": "2023-10-16", "D2": 40,
                     "H2": True, "H2A": 4, "C8": 8}


def test_proposals_from_notes_wait_for_a_person_and_say_where_they_came_from(person):
    trail = Trail()
    svc = consult(trail=trail)
    out = svc.propose({"text": "Every run must be signed off, and update objects already in Fabric."})
    assert out["source"] == "rules" and {p["question"] for p in out["proposals"]} == {"E4", "A7"}
    assert "rules" in (out["message"] or "")
    e4 = next(p for p in out["proposals"] if p["question"] == "E4")
    state = svc.decide({"ids": [e4["id"]], "decision": "confirm"})
    answer = next(q for q in state["questions"] if q["id"] == "E4")
    assert answer["status"] == "answered" and answer["source"] == "rules" and answer["value"] == "always"
    a7 = next(p for p in state["proposals"] if p["question"] == "A7")
    state = svc.decide({"ids": [a7["id"]], "decision": "reject"})
    assert state["proposals"] == [] and next(q for q in state["questions"] if q["id"] == "A7")["status"] == "assumed"
    assert trail.actions() == ["consult.proposed", "consult.proposal_confirmed", "consult.proposal_rejected"]
    with pytest.raises(ApiError) as gone:
        svc.decide({"ids": [a7["id"]], "decision": "confirm"})
    assert gone.value.code == "not_pending"


def test_confirming_proposals_is_all_or_nothing_and_a_bad_one_is_named(person):
    svc = consult()
    out = svc.propose({"text": "Every run must be signed off.", "useAi": False})
    good = out["proposed"][0]
    svc._records.append(consult_api.PROPOSALS, {"type": "proposal", "id": "zz-1", "batch": "zz", "question": "A7", "value": "maybe",
                                                "reason": "?", "source": "import", "catalogVersion": CATALOG.version})
    with pytest.raises(ApiError) as bad:
        svc.decide({"ids": [good, "zz-1"], "decision": "confirm"})
    assert bad.value.status == 400 and bad.value.code == "invalid_answer" and bad.value.message.startswith("A7:")
    assert svc._answers() == [] and not [r for r in svc._records.records(consult_api.PROPOSALS) if r.get("type") == "decision"]


def test_with_consent_the_model_reads_the_notes_and_is_never_told_a_name(person, monkeypatch):
    monkeypatch.setattr(claude_cli, "executable", lambda: "claude")
    model = Model({"proposals": [{"question": "C6", "value": "\"replace\"", "reason": "reload everything"},
                                 {"question": "Z9", "value": "1", "reason": "?"}]})
    svc = consult(model=model)
    svc.answer({"question": "D1", "value": True})  # a person's own yes is enough for the consultation's help, before any brief
    out = svc.propose({"text": "Reload everything each time."})
    assert out["source"] == "ai" and [p["question"] for p in out["proposals"]] == ["C6"]
    assert out["rejected"][0]["question"] == "Z9" and svc._spent() == 0.5
    sent = model.prompts[0]
    assert "pool01" not in sent and "sales" not in sent and "LoadSales" not in sent
    for question in ("C6", "B2", "B2L", "A2", "G2U"):  # a name in a recommendation, or made of names, is never sent
        explained = svc.explain({"question": question})
        assert explained["source"] == "ai"
        assert "pool01" not in model.prompts[-1] and "Named after" not in model.prompts[-1], question
    assert json.loads(model.prompts[-1])["question"]["recommended"] is None
    svc.explain({"question": "C6"})
    assert json.loads(model.prompts[-1])["question"]["recommended"] == "if_empty"


def test_with_consent_withheld_the_model_is_never_asked_even_when_it_is_there(person, monkeypatch):
    monkeypatch.setattr(claude_cli, "executable", lambda: "claude")
    model = Model({"proposals": [{"question": "C6", "value": "\"replace\"", "reason": "x"}]})
    svc = consult(model=model)
    svc.answer({"question": "D1", "value": False})
    out = svc.propose({"text": "Every run must be signed off."})
    assert out["source"] == "rules" and svc.explain({"question": "C6"})["source"] == "rules"
    assert model.prompts == [] and svc._spent() == 0


def test_without_the_model_an_explanation_comes_from_the_catalog():
    svc = consult()
    out = svc.explain({"question": "C6", "useAi": False})
    assert out["source"] == "rules" and "Recommended:" in out["text"] and "tables that already have rows" in out["text"]


def test_a_questionnaire_goes_out_and_its_answers_come_back_as_proposals(person):
    svc = consult()
    md, media, name = svc.questionnaire({"format": "md"})
    assert media.startswith("text/markdown") and "### D1." in md.decode("utf-8") and "(required)" in md.decode("utf-8")
    raw, _, _ = svc.questionnaire({"format": "json"})
    form = json.loads(raw)
    form["answers"].update({"D1": True, "A7": "update", "B2": "not a name!"})
    out = svc.import_answers({"answers": form["answers"]})
    assert {p["question"] for p in out["proposals"]} >= {"D1", "A7"} and out["rejected"][0]["question"] == "B2"
    ids = [p["id"] for p in out["proposals"] if p["question"] in ("D1", "A7")]
    state = svc.decide({"ids": ids, "decision": "confirm"})
    assert next(q for q in state["questions"] if q["id"] == "D1")["source"] == "import"


# -- the page's copy of the catalog and of the vocabulary -------------------------------------------------------

def test_the_demo_pages_catalog_is_the_same_as_the_servers():
    copy = REPO_ROOT / "frontend" / "src" / "mock" / "consultCatalog.json"
    assert json.loads(copy.read_text(encoding="utf-8")) == json.loads(catalogs.PATH.read_text(encoding="utf-8")), (
        "Copy src/discovery_agent/consult/catalog.json to frontend/src/mock/consultCatalog.json")


VOCABULARY = REPO_ROOT / "frontend" / "src" / "mock" / "consultVocabulary.json"


def test_the_demo_pages_settings_and_facts_are_the_servers():
    expected = {"settings": setting_names.describe(), "facts": fact_names.describe()}
    if __import__("os").environ.get("UPDATE_CONSULT_PARITY"):
        VOCABULARY.write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert VOCABULARY.is_file() and json.loads(VOCABULARY.read_text(encoding="utf-8")) == expected, (
        "The Demo page's vocabulary is out of date: run the tests once with UPDATE_CONSULT_PARITY=1.")


# -- the Demo page's copy of the engine gives the same answers ----------------------------------------------------

PARITY = REPO_ROOT / "frontend" / "src" / "test" / "consultParity.json"


def parity_cases():
    """Estates and answers the Demo page's TypeScript engine is checked against, with what this engine makes of them."""
    rich = dict(consult_api.SELF_CHECK_FACTS[1])

    def answer(seq, qid, value, **extra):
        return {"type": "answer", "question": qid, "value": value, "source": "person", "seq": seq, **extra}

    serverless = {**rich, "code.serverlessObjects": 2}
    keep = {"type": "resolve", "conflict": "K2", "resolution": "keep", "reason": "Test data only", "seq": 3,
            "facts": engine.conflict_evidence(CATALOG.conflict("K2"), serverless), "answers": {"C6": "replace", "E4": "never"}}
    cases = [
        ("nothing known", {}, []),
        ("a rich estate, nothing answered", rich, []),
        ("answers that open, park and contradict", rich, [
            answer(1, "A2", "include"), answer(2, "A2I", ["schema:dbo"]), answer(3, "C6", "replace"), answer(4, "E4", "never"),
            answer(5, "A6", "signoff_each_wave"), answer(6, "D1", True), answer(7, "A5", "since"), answer(8, "A5D", "2024-01-01"),
            answer(9, "A5", "all"), {"type": "skip", "question": "A7", "seq": 10}]),
        ("a contradiction kept, and an answer whose evidence moved", serverless, [
            answer(1, "C6", "replace"), answer(2, "E4", "never"), keep, answer(4, "A3S", "keep", facts={"code.serverlessObjects": 1})]),
        ("a case-insensitive pool and a case-sensitive Warehouse", {**rich, "discovery.complete": False,
                                                                     "discovery.failedCategories": ["Triggers", "Pools"]},
         [answer(1, "B1", "case_sensitive"), answer(2, "A0", ["data"]), {"type": "withdraw", "question": "B1", "seq": 3},
          answer(4, "B1", "case_sensitive")]),
        ("a production target, a sync and the AI", {**rich, "pool.collation": None}, [
            answer(1, "E1", "prod"), answer(2, "C1", True), answer(3, "C2", "named"), answer(4, "C2N", ["ModifiedDate"]),
            answer(5, "D1", True), answer(6, "H1", "run"), answer(7, "G1", ["*.*.email"]), answer(8, "E6", "act_dev"),
            answer(9, "A7", "update"), {"type": "skip", "question": "A7", "seq": 10}]),
    ]
    out = []
    for name, facts, records in cases:
        st = engine.evaluate(CATALOG, facts, records)
        out.append({"name": name, "facts": facts, "records": records, "expected": {
            "questions": {qid: {"applies": s.applies, "status": s.status, "value": s.value, "recommendation": s.recommendation}
                          for qid, s in st.questions.items()},
            "next": st.next, "blockers": st.blockers,
            "conflicts": [{"id": c.id, "status": c.status, "message": c.message} for c in st.conflicts],
            "settings": engine.settings_of(CATALOG, st)}})
    return out


def test_the_demo_engine_is_checked_against_what_this_engine_makes_of_the_same_answers():
    cases = parity_cases()
    text = json.dumps(cases, indent=1, ensure_ascii=False, default=str) + "\n"
    if __import__("os").environ.get("UPDATE_CONSULT_PARITY"):
        PARITY.write_text(text, encoding="utf-8")
    assert PARITY.is_file() and json.loads(PARITY.read_text(encoding="utf-8")) == json.loads(text), (
        "The Demo engine's parity cases are out of date: run the tests once with UPDATE_CONSULT_PARITY=1, then the frontend tests.")


# -- the HTTP surface ------------------------------------------------------------------------------------------------

def test_the_consultation_is_served_over_http_end_to_end():
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer

    from discovery_agent.api.server import make_handler

    class Served(FakeSession):  # the routes table names this one directly
        def start_discovery(self):
            return {}

    svc = ConsultService(Served(fake_job()), FakeFabric(), git_check=lambda: False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc._session, None, fabric=FakeFabric(), migration=migration(svc), consult=svc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def call(method, path, body=None):
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"} if body is not None else {})
        response = conn.getresponse()
        data, media = response.read(), response.getheader("Content-Type") or ""
        conn.close()
        return response.status, (json.loads(data) if media.startswith("application/json") and data else data)

    try:
        status, state = call("GET", "/api/consult")
        assert status == 200 and state["next"] == "D1" and state["mode"] == "advisory"
        status, catalog = call("GET", "/api/consult/catalog")
        assert status == 200 and catalog["version"] == CATALOG.version and catalog["conflicts"]
        status, refused = call("POST", "/api/consult/answer", {"question": "D2", "value": 5})
        assert status == 409 and refused["error"]["code"] == "not_applicable"
        status, refused = call("POST", "/api/consult/answer", {"question": "A7", "value": "maybe"})
        assert status == 400 and refused["error"]["code"] == "invalid_answer"
        status, preview = call("POST", "/api/consult/answer", {"question": "A2", "value": "include", "preview": True})
        assert status == 200 and preview["preview"]["opens"] == ["A2I"]
        status, context_view = call("GET", "/api/consult/context")
        assert status == 200 and context_view["hash"] is None
        assert call("POST", "/api/consult/accept", {})[1]["submittable"]
        status, view = call("POST", "/api/consult/brief/submit", {"note": "over HTTP"})
        assert status == 200 and view["versions"][0]["status"] == "submitted"
        status, view = call("POST", "/api/consult/brief/approve", {"version": 1, "decision": "approve", "hash": view["versions"][0]["hash"]})
        assert status == 200 and view["versions"][0]["status"] == "approved"
        status, policy = call("GET", "/api/consult/policy")
        assert status == 200 and policy["inForce"]["version"] == 1 and policy["runOptions"]["stopOnFailure"] is True
        assert policy["run"]["parallelCopies"] == 4 and policy["assistant"]["autonomy"] == "suggest"
        status, context_view = call("GET", "/api/consult/context")
        assert status == 200 and context_view["text"].startswith("# Migration context")
        status, file = call("GET", f"/api/consult/context/{context_view['hash']}")
        assert status == 200 and file.decode("utf-8") == context_view["text"]
        status, md = call("GET", "/api/consult/brief/export?format=md&version=1")
        assert status == 200 and md.decode("utf-8").startswith("# Migration brief, version 1")
        status, form = call("GET", "/api/consult/questionnaire?format=json")
        assert status == 200 and "answers" in form
        status, proposed = call("POST", "/api/consult/propose", {"text": "Every run must be signed off."})
        assert status == 200 and [p["question"] for p in proposed["proposals"]] == ["E4"]
        status, decided = call("POST", "/api/consult/proposals", {"ids": proposed["proposed"], "decision": "confirm"})
        assert status == 200 and decided["answered"] == ["E4"]
        status, explained = call("POST", "/api/consult/explain", {"question": "E4", "useAi": False})
        assert status == 200 and explained["source"] == "rules"
        status, differs = call("POST", "/api/consult/deviation", {"items": [{"id": NOTEBOOK_ID, "wave": 4}],
                                                                   "options": {"stopOnFailure": False}, "reason": "A pilot"})
        assert status == 200 and differs["recorded"] and differs["differences"][0]["option"] == "stopOnFailure"
        status, brief = call("GET", "/api/consult/brief")
        assert status == 200 and brief["summary"]["changes"][0]["question"] == "E4"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_switch_of_project_switches_the_consultation_with_the_rest():
    from discovery_agent.api.server import create_server

    httpd = create_server("127.0.0.1", 0)
    try:
        hub = httpd.projects
        assert [name for name, _ in hub._steps(hub.active_id())] == ["session", "fabric", "migration", "consult", "orchestrator"]
        assert hub._migration._consult is hub._consult and hub._transforms._consult is hub._consult
    finally:
        httpd.orchestrator.stop()
        httpd.server_close()
