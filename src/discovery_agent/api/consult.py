"""The consultation behind the Consult step: the questions, the answers, the migration brief, the policy in force.

    GET  /api/consult                    every question as it stands, the contradictions, what is next, the brief
    GET  /api/consult/catalog            the questions, as the page shows them
    POST /api/consult/answer             {question, value, note?, preview?, seq?} -> record an answer, or say what it would change
    POST /api/consult/skip               {question}: its recommendation stands, as an assumption
    POST /api/consult/withdraw           {question}: the answer no longer stands (the record keeps it)
    POST /api/consult/accept             {section?, questions?}: accept the recommendations as answers
    POST /api/consult/resolve            {conflict, resolution: take | keep, reason?}
    GET  /api/consult/brief              the draft, every version and its status, what changed since approval
    POST /api/consult/brief/submit       {note?} -> a new numbered, immutable version
    POST /api/consult/brief/approve      {version, decision: approve | reject, comment?, hash?} (a person only)
    GET  /api/consult/brief/export       ?version=&format=md | json
    GET  /api/consult/policy             the policy in force: what Plan pre-sets and Migrate enforces
    POST /api/consult/deviation          {items, options, reason}: a plan that differs from the brief, and why
    POST /api/consult/propose            {text} -> answers read from free text, for a person to confirm
    POST /api/consult/proposals          {ids, decision: confirm | reject}
    POST /api/consult/explain            {question} -> what the question means for this estate
    GET  /api/consult/questionnaire      ?format=md | json: the questions, to answer offline
    POST /api/consult/questionnaire      {answers: {id: value}}: a questionnaire's answers, proposed for a person to confirm
    GET  /api/consult/context            MIGRATION_CONTEXT.md for the brief in force: what every LLM draft is sent
    GET  /api/consult/context/{hash}     one that was, by its hash (each draft records the one it was made with)

What a project's consultation keeps, in its store (``state.store``):

* ``consult/answers`` (stream): every answer, skip, withdrawal and resolution, append-only, each with who, when, its
  source, the catalog version and the evidence it was made against;
* ``consult/proposals`` (stream): answers proposed from free text or an import, and what a person decided on each;
* ``consult/briefs/<n>`` (documents): each submitted version, never changed, with its content hash;
* ``consult/brief_events`` (stream): each version's lifecycle: submitted, approved, rejected, superseded;
* ``consult/deviations`` (stream): plans confirmed although they differ from the brief, with the reason;
* ``llm/spend`` (stream): what each LLM call cost, which the brief's budget is measured against;
* blobs: every ``MIGRATION_CONTEXT.md`` a draft was sent, by its content hash.

**Consultation mode** (``MIGRATION_CONSULTATION``): ``required`` (the default) refuses a run without an approved
brief that still fits the estate, and a run that differs from the brief without a reason; ``advisory`` records both
and refuses neither.

**The LLM gate.** Every call to the model asks ``llm_gate`` first. Consent (D1) and the budget (D2) are what the
approved brief says, with two exceptions that only ever make the gate stricter, and so take effect at once: a
person's latest answer that withdraws consent or lowers the budget. The consultation's own help (reading answers
from free text, explaining a question) sends only what a person typed, the questions and counts about the estate:
never a definition, and never a name from the estate. Before any brief is approved, a person's own "yes" to D1 is
enough for that help; once one is, the approved brief decides, as it does for everything else.

**Who writes.** Answers, skips, withdrawals, resolutions, submissions, approvals, confirmed proposals and deviations
are a person's: anything else acting (the accelerator, the agent) is refused (``person_only``). The agent may only
propose answers, which wait for a person like any other proposal.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from discovery_agent.api.migration import (
    _artifacts,
    _catalog,
    apply_stages,
    filterable_tables,
    parse_options,
    rewritable,
    sources_for,
)
from discovery_agent.api.service import ApiError
from discovery_agent.consult import brief as briefs
from discovery_agent.consult import catalog as catalogs
from discovery_agent.consult import context_file as context_files
from discovery_agent.consult import engine
from discovery_agent.consult.catalog import Catalog, Question
from discovery_agent.consult.facts import ENVIRONMENT, ESTATE, FACTS, fingerprint
from discovery_agent.consult.policy import Policy, change_column, major
from discovery_agent.consult.settings import SETTINGS
from discovery_agent.llm import claude_cli
from discovery_agent.migration import capabilities, datafilter, fabric_git, storage_remap, warehouse_ddl
from discovery_agent.migration.common import DATASET, NOTEBOOK, PIPELINE, SCRIPT, SPARKJOB
from discovery_agent.migration.preflight import code_text, content_findings
from discovery_agent.migration.runner import MIGRATABLE, warehouse_name_for
from discovery_agent.source_strategy import P0Artifact
from discovery_agent.sql.models import SqlTable
from discovery_agent.state import context
from discovery_agent.state.store import Store, now

_log = logging.getLogger("discovery_agent.consult")

REQUIRED, ADVISORY = "required", "advisory"
ANSWERS, PROPOSALS, EVENTS, DEVIATIONS, SPEND = (
    "consult/answers", "consult/proposals", "consult/brief_events", "consult/deviations", "llm/spend")
BRIEFS = "consult/briefs"
MAX_NOTE, MAX_REASON, MAX_TEXT = 500, 500, 8000
#: Artifacts discovered in folders, by the kind the run makes of them.
_FOLDER_KINDS = {NOTEBOOK: P0Artifact.NOTEBOOK, PIPELINE: P0Artifact.PIPELINE, SCRIPT: P0Artifact.SQL_SCRIPT,
                 SPARKJOB: P0Artifact.SPARK_JOB_DEFINITION, DATASET: P0Artifact.DATASET}
TOP_FOLDER = "folder:(top level)"
#: SQL objects that belong to a schema, for the scope groups.
_SCHEMA_TYPES = ("Table", "View", "Stored Procedure", "Function", "External Table")
#: The SQL code a brief can bring across or leave for later (A3).
_MODULE_TYPES = ("View", "Stored Procedure", "Function")
#: How long the Fabric workspace's Git connection is taken as read.
GIT_CHECK_SECONDS = 60.0
#: Estates the catalog is checked against when it loads: accepting every recommendation must give a brief that can
#: be submitted, whether every question applies or almost none does.
SELF_CHECK_FACTS = (
    {"objects.total": 3, "pool.present": False, "waves.count": 1, "discovery.complete": True},
    {"objects.total": 40, "waves.count": 3, "pool.present": True, "pool.name": "sqlpool1", "pool.warehouseName": "sqlpool1",
     "pool.collation": "SQL_Latin1_General_CP1_CI_AS", "pool.caseInsensitive": True, "tables.count": 10,
     "tables.withDateColumn": 5, "tables.withChangeColumn": 3, "tables.changeWithoutKeys": 1, "tables.lossyTypes": 1,
     "storage.locations": 2, "security.principals": 4, "security.sqlUsers": 1, "security.sqlUserNames": ["app_user"],
     "code.sqlModules": 6, "code.unconvertibleSql": 1, "code.multiStatementFunctions": 1, "code.needsRewrite": 2,
     "code.unsupportedPipelines": 1, "code.dotnetNotebooks": 1, "code.serverlessObjects": 1, "pipelines.count": 3,
     "notebooks.count": 2, "linkedServices.count": 3, "linkedServices.keyVault": 1, "triggers.schedule": 1,
     "manual.setupCount": 2, "scope.groups": ["schema:dbo"], "discovery.complete": True, "target.gitConnected": True,
     "llm.available": True},
)


def mode() -> str:
    value = str(os.environ.get("MIGRATION_CONSULTATION") or REQUIRED).strip().lower()
    return value if value in (REQUIRED, ADVISORY) else REQUIRED


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    """The catalog, checked, and checked again with the engine: accepting every recommendation must be enough."""
    catalog = catalogs.load()
    problems = engine.accept_all_problems(catalog, SELF_CHECK_FACTS)
    if problems:
        raise catalogs.CatalogError("The consultation catalog is not valid:\n- " + "\n- ".join(problems))
    return catalog


def _no_audit(*args: Any, **kwargs: Any) -> None:
    return None


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class _Memory:
    """The few store operations the consultation uses, in memory: for a service built without a project store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._docs: Dict[str, Any] = {}
        self._streams: Dict[str, List[Dict[str, Any]]] = {}
        self._blobs: Dict[str, bytes] = {}

    def get(self, name: str, default: Any = None) -> Any:
        with self._lock:
            return json.loads(json.dumps(self._docs[name])) if name in self._docs else default

    def put(self, name: str, value: Any, expected: Optional[int] = None) -> int:
        with self._lock:
            self._docs[name] = json.loads(json.dumps(value, default=str))
            return 1

    def names(self, folder: str) -> List[str]:
        with self._lock:
            return sorted(n[len(folder) + 1:] for n in self._docs if n.startswith(folder + "/") and "/" not in n[len(folder) + 1:])

    def append(self, stream: str, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            rows = self._streams.setdefault(stream, [])
            entry = json.loads(json.dumps({**record, "seq": len(rows) + 1}, default=str))
            rows.append(entry)
            return dict(entry)

    def records(self, stream: str, after: int = 0, limit: Optional[int] = None, newest_first: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            found = [dict(r) for r in self._streams.get(stream, []) if r["seq"] > after]
        if newest_first:
            found.reverse()
        return found[:limit] if limit is not None else found

    def put_blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            self._blobs[digest] = bytes(data)
        return digest

    def blob(self, digest: str) -> Optional[bytes]:
        with self._lock:
            return self._blobs.get(digest)


# -- the facts ------------------------------------------------------------------------------------------

@dataclass
class Estate:
    """What the facts were computed from, kept beside them for the scope rules (Plan's Add all) and differences."""

    items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: object id -> its scope groups (``schema:x``, ``folder:a/b``); objects in none are always in scope.
    group_of: Dict[str, List[str]] = field(default_factory=dict)
    #: object id -> what the estate says about it: serverless, multiStatement, rebuild.
    flags: Dict[str, List[str]] = field(default_factory=dict)
    filterable: List[Dict[str, Any]] = field(default_factory=list)


def _schema_of(item: Mapping[str, Any], extra: Any) -> Optional[str]:
    if item.get("type") == "Schema":
        meta = getattr(extra, "metadata", None) or {}
        return str(meta.get("schemaName") or str(item.get("name") or "").split(".")[-1]) or None
    if item.get("type") not in _SCHEMA_TYPES:
        return None
    if item.get("schema"):
        return str(item["schema"])
    meta = getattr(extra, "metadata", None) or {}
    if meta.get("schemaName"):
        return str(meta["schemaName"])
    parts = str(item.get("name") or "").split(".")
    return parts[-2] if len(parts) >= 2 else None


def _uses_key_vault(node: Any) -> bool:
    if isinstance(node, dict):
        return node.get("type") == "AzureKeyVaultSecret" or any(_uses_key_vault(v) for v in node.values())
    if isinstance(node, list):
        return any(_uses_key_vault(v) for v in node)
    return False


def estate_facts(job: Any, pool: Optional[str]) -> Tuple[Dict[str, Any], Estate]:
    """Every estate fact, from a finished discovery. Metadata only: counts, names, short lists. A part that cannot be
    worked out is left unknown (None), which never switches a question or a contradiction on."""
    facts: Dict[str, Any] = {}
    estate = Estate()
    items = list(getattr(job, "items", None) or [])
    summary = getattr(job, "summary", None) or {}
    snapshot = getattr(job, "snapshot", None) or {}
    extras = getattr(job, "extras_by_id", None) or {}
    records = getattr(job, "records_by_id", None) or {}
    estate.items = {str(i.get("id")): dict(i) for i in items}

    facts["objects.total"] = len(items)
    facts["objects.byType"] = dict(summary.get("byType") or {})
    modules = [str(i.get("id")) for i in items if i.get("type") in _MODULE_TYPES]
    facts["code.sqlModules"] = len(modules)
    for oid in modules:
        estate.flags.setdefault(oid, []).append("sqlModule")
    kinds_of = [MIGRATABLE.get(str(i.get("type") or "")) for i in items]
    facts["pipelines.count"] = sum(1 for k in kinds_of if k == PIPELINE)
    facts["notebooks.count"] = sum(1 for k in kinds_of if k == NOTEBOOK)
    facts["objects.byClass"] = dict(summary.get("byClassification") or {})
    graph = getattr(job, "graph", None) or {}
    facts["waves.count"] = len(graph.get("waves") or []) or len({n.get("wave") for n in graph.get("nodes") or [] if n.get("wave")})

    # The pool, and the Warehouse it becomes.
    facts["pool.present"] = bool(pool)
    facts["pool.name"] = pool or None
    facts["pool.warehouseName"] = warehouse_name_for(pool) if pool else None
    collation = None
    for extra in extras.values():
        if getattr(extra, "source_type", "") == "Dedicated SQL Pool" and pool and str(extra.name).lower() == pool.lower():
            collation = (extra.metadata or {}).get("collation")
    facts["pool.collation"] = str(collation) if collation else None
    facts["pool.caseInsensitive"] = ("_CI" in str(collation).upper()) if collation else None

    # Tables, their dates and keys: what history, sync and replace decisions rest on.
    tables = [r.content for r in records.values() if isinstance(getattr(r, "content", None), SqlTable)]
    internal = [t for t in tables if not t.is_external]
    facts["tables.count"] = len(internal)
    facts["tables.external"] = len(tables) - len(internal)
    try:
        estate.filterable = filterable_tables(job)
    except Exception as exc:  # noqa: BLE001 - a fact that cannot be worked out is unknown, never a failure
        _log.warning("Could not read the tables' date columns for the consultation: %s: %s", type(exc).__name__, exc)
    changing = [t for t in estate.filterable if change_column([str(c.get("name")) for c in t.get("dateColumns") or []])]
    facts["tables.withDateColumn"] = len(estate.filterable)
    facts["tables.withChangeColumn"] = len(changing)
    keyed = sum(1 for t in internal if datafilter.table_keys(t))
    facts["tables.withKeys"] = keyed
    facts["tables.withoutKeys"] = len(internal) - keyed
    facts["tables.changeWithoutKeys"] = sum(1 for t in changing if not t.get("keyColumns"))
    lossy = [t for t in internal if any(warehouse_ddl.loses(c) for c in t.columns)]
    facts["tables.lossyTypes"] = len(lossy)
    by_key = {(str(i.get("schema") or "").lower(), str(i.get("name") or "").split(".")[-1].lower()): str(i.get("id")) for i in items
              if i.get("type") == "Table"}
    for table in lossy:
        oid = by_key.get((table.key.schema.lower(), table.key.name.lower()))
        if oid:
            estate.flags.setdefault(oid, []).append("lossy")

    # The code: what the rules cannot convert, what reads storage, what ran on the serverless pool.
    try:
        plan = [{"id": i["id"], "wave": int(i.get("wave") or 1)} for i in items]
        every = sources_for(plan, job, pool)
        sources = apply_stages(every, {"stages": list(capabilities.STAGE_KEYS), "scope": "automated", "dataFilters": {}})
        findings = content_findings(sources, set(), pool or "", [])
        kinds = {s.id: s.kind for s in every}
        by_code: Dict[str, Set[str]] = {}
        dotnet: Set[str] = set()
        for f in findings:
            by_code.setdefault(f.code, set()).add(f.object_id)
            if f.code == "NEEDS_REWRITE" and kinds.get(f.object_id) == NOTEBOOK and ".NET" in f.message:
                dotnet.add(f.object_id)
        rebuild = by_code.get("NEEDS_REWRITE", set())
        multi = {s.id for s in every if rewritable(s)}
        serverless = by_code.get("SERVERLESS_SQL", set())
        unconvertible = by_code.get("SYNAPSE_TSQL", set())
        facts["code.multiStatementFunctions"] = len(multi)
        facts["code.unconvertibleSql"] = len(unconvertible)
        facts["code.dotnetNotebooks"] = len(dotnet)
        facts["code.unsupportedPipelines"] = sum(1 for oid in rebuild if kinds.get(oid) == PIPELINE)
        facts["code.needsRewrite"] = len(rebuild)
        facts["code.serverlessObjects"] = len(serverless)
        facts["storage.locations"] = len(storage_remap.locations([code_text(s) for s in sources]))
        for name, ids in (("serverless", serverless), ("multiStatement", multi), ("rebuild", rebuild),
                          ("unconvertible", unconvertible)):
            for oid in ids:
                estate.flags.setdefault(oid, []).append(name)
    except Exception as exc:  # noqa: BLE001 - unknown, as above; the API's log says why
        _log.warning("Could not assess the code for the consultation: %s: %s", type(exc).__name__, exc)

    # Security.
    sec = getattr(_catalog(job), "security", None)
    if sec is not None:
        carried = [e for e in extras.values() if getattr(e, "source_type", "") == "Security Object"]
        facts["security.principals"] = len(carried)
        facts["security.roles"] = sum(1 for e in carried if "role" in str((e.metadata or {}).get("principalKind") or "").lower())
        users = sorted(p.name for p in sec.principals if p.is_sql_user and not p.is_built_in)
        facts["security.sqlUsers"] = len(users)
        facts["security.sqlUserNames"] = users
        facts["security.masks"] = len(sec.masks)

    # Linked services, triggers and integration runtimes.
    artifacts = _artifacts(job)
    services = artifacts.get(P0Artifact.LINKED_SERVICE, {}) or {}
    facts["linkedServices.count"] = len(services)
    by_type: Dict[str, int] = {}
    for ls in services.values():
        kind = str(((ls or {}).get("properties") or {}).get("type") or "Unknown")
        by_type[kind] = by_type.get(kind, 0) + 1
    facts["linkedServices.byType"] = dict(sorted(by_type.items()))
    facts["linkedServices.keyVault"] = sum(1 for ls in services.values() if _uses_key_vault(ls))
    triggers = [e for e in extras.values() if getattr(e, "source_type", "") == "Trigger"]
    schedules = sum(1 for t in triggers if (t.metadata or {}).get("triggerType") == "ScheduleTrigger")
    runtimes = [e for e in extras.values() if getattr(e, "source_type", "") == "Integration Runtime"
                and str(e.name) != "AutoResolveIntegrationRuntime"]
    facts["triggers.count"] = len(triggers)
    facts["triggers.schedule"] = schedules
    facts["triggers.event"] = len(triggers) - schedules
    facts["integrationRuntimes.count"] = len(runtimes)
    facts["manual.setupCount"] = len(triggers) + len(runtimes)

    # Scope groups: each SQL object's schema, each workspace artifact's folder.
    groups: Dict[str, str] = {}
    for item in items:
        oid = str(item.get("id"))
        schema = _schema_of(item, extras.get(oid))
        group = None
        if schema:
            group = f"schema:{schema}"
        else:
            kind = MIGRATABLE.get(str(item.get("type") or ""))
            record = records.get(oid)
            if kind in _FOLDER_KINDS and record is not None:
                payload = (artifacts.get(_FOLDER_KINDS[kind]) or {}).get(record.identity.name) or {}
                folder = str((((payload or {}).get("properties") or {}).get("folder") or {}).get("name") or "").strip("/")
                group = f"folder:{folder}" if folder else TOP_FOLDER
        if group:
            group = groups.setdefault(group.lower(), group)  # one spelling for each, whatever the case
            estate.group_of[oid] = [group]
    facts["scope.groups"] = sorted(set(groups.values()), key=lambda g: (not g.startswith("schema:"), g.lower()))

    # Where it all came from.
    repository = snapshot.get("repository") or {}
    facts["source.kind"] = snapshot.get("sourceKind")
    facts["source.workspace"] = snapshot.get("workspace") or getattr(job, "workspace", None)
    facts["source.ref"] = repository.get("ref")
    facts["source.commit"] = repository.get("commit")
    facts["source.environment"] = repository.get("environment")
    failed = snapshot.get("failedCategories")
    if failed is None:
        failed = [str(f.get("name") if isinstance(f, dict) else f) for f in summary.get("failedCategories") or []]
    facts["discovery.complete"] = not failed
    facts["discovery.failedCategories"] = list(failed)
    facts["discovery.snapshot"] = snapshot.get("id")
    return facts, estate


# -- the rules that read answers from free text, when the model is not used ---------------------------------

_YEARS = re.compile(r"\b(?:last|past|previous)\s+(\d{1,2})\s+years?\b", re.IGNORECASE)
_SINCE = re.compile(r"\b(?:since|from|after)\s+(\d{4}-\d{2}-\d{2}|\d{4})\b", re.IGNORECASE)
_CYCLES = re.compile(r"\b(?:parallel|side[- ]by[- ]side)\D{0,30}?(\d{1,2}) (?:cycles?|runs?)\b", re.IGNORECASE)
_COPIES = re.compile(r"\b(\d{1,2}) (?:tables? )?(?:copies|loads|tables) (?:at a time|at once|in parallel)\b", re.IGNORECASE)
_BUDGET = re.compile(r"(?:budget|spend|up to|at most|max(?:imum)?)\D{0,20}\$\s?(\d{1,5})|\$\s?(\d{1,5})\s*(?:budget|for ai|for the ai|on ai)",
                     re.IGNORECASE)
#: (question, value, pattern): an answer the text gives, plainly. The first match for a question wins.
_RULES: Tuple[Tuple[str, Any, re.Pattern], ...] = tuple((q, v, re.compile(p, re.IGNORECASE)) for q, v, p in (
    ("A0", ["structure"], r"\b(structure|schema|tables?) only\b|\bno data\b|\bwithout (the )?data\b"),
    ("A0", ["structure", "data"], r"\b(no|without|skip) (the )?(pipelines|orchestration|notebooks)\b"),
    ("A1", "none", r"\b(leave|keep) (the )?(storage )?paths( as they are)?\b|\bno shortcuts\b"),
    ("A1", "all", r"\b(use|through|via) (onelake )?shortcuts\b"),
    ("A3", "leave_out", r"\b(tables|data) first\b|\b(views|procedures|procs) (later|in a later run)\b"),
    ("A6", "signoff_each_wave", r"\b(sign[- ]off|approve) (each|every) wave\b|\bwave by wave\b"),
    ("A6", "one_run", r"\b(one|a single|single) (run|cutover|go)\b|\bbig[- ]bang\b"),
    ("A7", "update", r"\b(update|overwrite) (what is |objects |items )?(already )?(there|in fabric|existing)\b|\bupdate in place\b"),
    ("A7", "skip", r"\b(never|don'?t|do not) (overwrite|update)\b"),
    ("B1", "case_sensitive", r"\bcase[- ]sensitive\b(?! *(?:is|was) not)"),
    ("B1", "case_insensitive", r"\bcase[- ]insensitive\b"),
    ("C1", True, r"\b(keep (it |them |the tables )?in sync|incremental(ly)?|sync (it|them|the tables))\b"),
    ("C1", False, r"\b(one[- ]off|one[- ]time|single) (load|copy|migration)\b|\bfull reload\b"),
    ("C4", "reload", r"\b(propagate|carry over|remove) (the )?delet\w*\b|\bdeletes? (must|should) (go|propagate)\b"),
    ("C6", "replace", r"\b(replace|truncate|reload) (the )?(rows|data|tables)\b"),
    ("C7", "create", r"\b(only create|create only|don'?t run|do not run) (the )?(load )?pipelines\b"),
    ("D1", False, r"\b(no (ai|llm|claude|gen ?ai)|don'?t send|do not send|never send|not (allowed|permitted) to send)\b"),
    ("D1", True, r"\b(ai|llm|claude) (is )?(fine|ok|okay|allowed|welcome)\b|\buse (ai|an llm|claude)\b"),
    ("D7", "scala", r"\b(rewrite|convert)\w* (\w+ )?(in|to) scala\b"),
    ("D7", "pyspark", r"\b(rewrite|convert)\w* (\w+ )?(in|to) (pyspark|python)\b"),
    ("E0", False, r"\b(no|without|skip) (fabric )?git\b|\bdon'?t commit\b"),
    ("E0", True, r"\bcommit (it |each run |every run )?to (fabric )?git\b"),
    ("E1", "prod", r"\b(target|fabric) (workspace )?is (the )?prod(uction)?\b|\bstraight (in)?to prod(uction)?\b"),
    ("E1", "test", r"\b(target|fabric) (workspace )?is (the )?test\b"),
    ("E2", "service_principal", r"\b(as|with|under) a service principal\b|\bservice principal (owns|should own)\b"),
    ("E4", "always", r"\b(sign[- ]off|approv\w*) (for |on |before )?(every|each|all) runs?\b|\balways (sign[- ]off|approve)\b"
                     r"|\b(every|each) run (must|should|has to|needs to) be (signed[- ]off|approved)\b"),
    ("E4", "never", r"\bno (sign[- ]off|approvals?)\b"),
    ("E5", "match", r"\b(switch|turn) (the )?schedules on\b|\bschedules (switched |turned )?on\b"),
    ("E7", "dev_test_prod", r"\b(test and prod|dev,? test,? (and )?prod)\b"),
    ("E7", "dev_test", r"\bdev and test\b"),
    ("G1M", "mask", r"\bmask (the |those |these )?(pii|personal|sensitive)\b"),
    ("G2", False, r"\b(skip|no|without) (the )?(security|permissions|roles)\b"),
    ("H1", "run", r"\b(run (the )?(pipelines|notebooks) (in|during|to) valid\w*|full validation)\b"),
    ("H1", "compare", r"\b(compare only|just compare)\b"),
    ("H2", True, r"\b(parallel run|side[- ]by[- ]side|run both)\b"),
    ("H4", "yes", r"\b(write|writes|writing) to (a )?prod(uction)?\b"),
))


def rule_proposals(text: str, today: Optional[_dt.date] = None) -> List[Dict[str, Any]]:
    """Answers the text gives plainly, each with the words it was read from. What the consultation's help proposes
    when the model is not used: a person still confirms each one."""
    out: Dict[str, Dict[str, Any]] = {}

    def add(question: str, value: Any, match: re.Match) -> None:
        if question not in out:
            out[question] = {"question": question, "value": value, "reason": f"You wrote: \"{match.group(0).strip()}\"."}

    for question, value, pattern in _RULES:
        found = pattern.search(text)
        if found:
            add(question, value, found)
    today = today or _dt.date.today()
    years, since = _YEARS.search(text), _SINCE.search(text)
    if years:
        start = today.replace(year=today.year - int(years.group(1)), day=min(today.day, 28))
        add("A5", "since", years)
        add("A5D", start.isoformat(), years)
    elif since:
        raw = since.group(1)
        add("A5", "since", since)
        add("A5D", raw if len(raw) == 10 else f"{raw}-01-01", since)
    budget = _BUDGET.search(text)
    if budget:
        add("D2", int(budget.group(1) or budget.group(2)), budget)
    cycles = _CYCLES.search(text)
    if cycles:
        add("H2", True, cycles)
        add("H2A", int(cycles.group(1)), cycles)
    copies = _COPIES.search(text)
    if copies:
        add("C8", int(copies.group(1)), copies)
    return list(out.values())


PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {"proposals": {"type": "array", "items": {
        "type": "object",
        "properties": {"question": {"type": "string"}, "value": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["question", "value", "reason"], "additionalProperties": False}}},
    "required": ["proposals"], "additionalProperties": False,
}
EXPLAIN_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
CONSULT_SYSTEM = ("You help a consultant fill in a migration brief for moving an Azure Synapse workspace to Microsoft Fabric. "
                  "Answer only with the JSON object asked for. You have no tools and need none. Never invent facts about "
                  "the estate: use only the counts you are given.")


class ConsultService:
    def __init__(self, session: Any, fabric: Any = None, records: Optional[Store] = None, audit: Optional[Callable[..., Any]] = None,
                 model: Any = None, git_check: Optional[Callable[[], Optional[bool]]] = None) -> None:
        self._session = session
        self._fabric = fabric
        self._records: Any = records if records is not None else _Memory()
        #: Records what people decide here (``state.audit``); never raises.
        self._audit = audit or _no_audit
        self._model = model
        #: Whether the Fabric target workspace is connected to Git; the tests give one. Read at most once a minute.
        self._git_check = git_check
        self._lock = threading.RLock()
        self._facts_lock = threading.Lock()
        self._estate: Optional[Tuple[Tuple[Any, ...], Dict[str, Any], Estate]] = None
        self._git: Tuple[Optional[str], float, Optional[bool]] = (None, 0.0, None)
        #: The migration contexts already kept in this project's store, by hash.
        self._contexts: Set[str] = set()
        self.catalog = load_catalog()

    # -- the project ----------------------------------------------------------------------------------------

    def switch(self, records: Optional[Store]) -> None:
        """Hold another project's consultation instead. Nothing runs in the background here, so nothing waits."""
        with self._lock:
            self._records = records if records is not None else _Memory()
            self._estate = None
            self._git = (None, 0.0, None)
            self._contexts = set()

    def busy(self) -> Optional[str]:
        return None

    # -- facts ----------------------------------------------------------------------------------------------

    def _environment(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {}
        if self._fabric is not None:
            try:
                state = self._fabric.state() or {}
            except Exception:  # noqa: BLE001 - the target is context, never a reason to fail
                state = {}
        connected = state.get("status") == "connected"
        out = {"target.connected": connected, "target.workspace": state.get("workspaceName") if connected else None,
               "target.workspaceId": state.get("workspaceId") if connected else None,
               "target.capacity": state.get("capacityAssigned") if connected else None,
               "target.gitConnected": self._git_connected(state.get("workspaceId")) if connected else None,
               "llm.available": claude_cli.switched_on() and claude_cli.executable() is not None}
        return out

    def _git_connected(self, workspace_id: Optional[str]) -> Optional[bool]:
        if self._git_check is not None:
            return self._git_check()
        if not workspace_id:
            return None
        held, at, value = self._git
        if held == workspace_id and time.monotonic() - at < GIT_CHECK_SECONDS:
            return value
        try:
            value = bool(fabric_git.connection(self._fabric.rest_client(), workspace_id).connected)
        except Exception:  # noqa: BLE001 - unknown: the Git questions stay closed until it can be read
            value = None
        self._git = (workspace_id, time.monotonic(), value)
        return value

    def facts_of(self, job: Any, pool: Optional[str]) -> Tuple[Dict[str, Any], Estate]:
        """The facts for this discovery (worked out once for each snapshot) and the environment as it is now."""
        key = (id(job), (getattr(job, "snapshot", None) or {}).get("fingerprint"), getattr(job, "finished_at", None), pool)
        with self._facts_lock:
            cached = self._estate
            if cached is None or cached[0] != key:
                facts, estate = estate_facts(job, pool)
                cached = (key, facts, estate)
                self._estate = cached
        return {**cached[1], **self._environment()}, cached[2]

    def _facts(self) -> Tuple[Dict[str, Any], Estate]:
        job, pool = self._session.migration_snapshot()  # 409 no_results until there is a discovery
        return self.facts_of(job, pool)

    # -- the record ------------------------------------------------------------------------------------------

    def _answers(self) -> List[Dict[str, Any]]:
        """Every answer, skip, withdrawal and resolution, as this catalog reads them: one made against an earlier
        version is under the question it answers now (``Catalog.translate``). The record itself is never changed."""
        return [dict(self.catalog.translate(r)) for r in self._records.records(ANSWERS)]

    def _events(self) -> List[Dict[str, Any]]:
        return self._records.records(EVENTS)

    def _version(self, number: int) -> Optional[Dict[str, Any]]:
        return self._records.get(f"{BRIEFS}/{number:04d}")

    def _numbers(self) -> List[int]:
        out = []
        for name in self._records.names(BRIEFS):
            try:
                out.append(int(name))
            except ValueError:
                continue
        return sorted(out)

    def _base(self, facts: Mapping[str, Any]) -> Dict[str, Any]:
        """What every record keeps: who, when, the catalog it was made against and the snapshot it rests on."""
        return {"by": context.actor().to_dict(), "at": now(), "catalogVersion": self.catalog.version,
                "snapshot": facts.get("discovery.snapshot")}

    def _state(self, facts: Mapping[str, Any], extra: Iterable[Mapping[str, Any]] = ()) -> engine.State:
        return engine.evaluate(self.catalog, facts, self._answers() + list(extra))

    def _question(self, question_id: Any) -> Question:
        q = self.catalog.question(str(question_id or ""))
        if q is None:
            raise ApiError(404, "not_found", "There is no such question.")
        return q

    def _applicable(self, state: engine.State, q: Question) -> None:
        if not state.questions[q.id].applies:
            raise ApiError(409, "not_applicable", f"{q.id} does not apply to this estate as it is answered now.")

    @staticmethod
    def _person(what: str) -> None:
        """Refuse a write that is a person's to make, when anything else (the accelerator, the agent) asks for it."""
        if context.actor().kind != context.PERSON:
            raise ApiError(403, "person_only", f"Only a person can {what}.")

    # -- reading ----------------------------------------------------------------------------------------------

    def catalog_view(self) -> dict:
        return self.catalog.describe()

    def _question_view(self, q: Question, s: engine.QuestionState, facts: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "id": q.id, "applies": s.applies, "status": s.status, "value": s.value,
            "display": briefs.display(q, s.value) if s.value is not None else None,
            "source": s.source, "by": s.by, "at": s.at, "note": s.note, "recommendation": s.recommendation,
            "changed": s.changed, "conflicts": list(s.conflicts),
            "choices": q.choices(facts) if q.options_from else None,
            "rows": list(facts.get(q.rows_from) or []) if q.rows_from else None,
            "evidence": [{"fact": n, "description": FACTS[n].description, "value": facts.get(n)} for n in q.evidence if n in FACTS],
        }

    def state(self) -> dict:
        facts, _ = self._facts()
        records = self._answers()
        st = engine.evaluate(self.catalog, facts, records)
        return {
            "mode": mode(), "catalogVersion": self.catalog.version, "snapshot": facts.get("discovery.snapshot"),
            "factsFingerprint": fingerprint(facts),
            "facts": [{"name": n, "description": f.description, "scope": f.scope, "value": facts.get(n)}
                      for n, f in FACTS.items() if not f.informational],
            "questions": [self._question_view(q, st.questions[q.id], facts) for q in self.catalog.questions],
            "conflicts": [{"id": c.id, "status": c.status, "message": c.message, "questions": c.questions, "evidence": c.evidence,
                           "reason": c.reason, "by": c.by, "at": c.at,
                           "resolutions": [{"kind": r.kind, "label": r.label, "question": r.question}
                                           for r in (self.catalog.conflict(c.id).resolutions if self.catalog.conflict(c.id) else ())]}
                          for c in st.conflicts],
            "next": st.next, "blockers": st.blockers, "submittable": st.submittable, "progress": st.progress(),
            "seq": records[-1]["seq"] if records else 0,
            "brief": self._brief_summary(facts, st),
            "proposals": self._pending(st),
            "llm": self.llm_gate("consult"),
        }

    # -- answering ---------------------------------------------------------------------------------------------

    def _write(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._records.append(ANSWERS, r) for r in records]

    def answer(self, body: dict) -> dict:
        facts, _ = self._facts()
        q = self._question(body.get("question"))
        if "value" not in body:
            raise ApiError(400, "invalid_answer", "Send the answer as value.")
        records = self._answers()
        st = engine.evaluate(self.catalog, facts, records)
        self._applicable(st, q)
        try:
            value = q.check(body.get("value"), facts)
        except ValueError as exc:
            raise ApiError(400, "invalid_answer", f"{q.id}: {exc}") from None
        if body.get("seq") not in (None, ""):
            seen = _whole(body["seq"], -1)
            moved = [r for r in records if r["seq"] > seen and r.get("question") == q.id]
            if moved:
                raise ApiError(409, "answer_changed", f"{q.id} was answered on another page since this one read it. Read it again.")
        record = {"type": "answer", "question": q.id, "value": value, "source": engine.PERSON,
                  "note": _text(body.get("note"), MAX_NOTE) or None, "facts": engine.evidence_of(q, facts), **self._base(facts)}
        if body.get("preview"):
            return {"question": q.id, "value": value, "preview": engine.preview(self.catalog, facts, records, record)}
        self._person("answer a consultation question")
        self._write([record])
        self._audit("consult.answered", target={"type": "question", "id": q.id}, details={
            "question": q.id, "value": briefs.display(q, value), "source": engine.PERSON, "note": record["note"],
            "catalogVersion": self.catalog.version})
        return self.state()

    def skip(self, body: dict) -> dict:
        self._person("skip a consultation question")
        facts, _ = self._facts()
        q = self._question(body.get("question"))
        self._applicable(self._state(facts), q)
        self._write([{"type": "skip", "question": q.id, **self._base(facts)}])
        self._audit("consult.skipped", target={"type": "question", "id": q.id}, details={"question": q.id})
        return self.state()

    def withdraw(self, body: dict) -> dict:
        self._person("withdraw a consultation answer")
        facts, _ = self._facts()
        q = self._question(body.get("question"))
        answers, skips, _ = engine.fold(self._answers())
        if q.id not in answers and q.id not in skips:
            raise ApiError(409, "nothing_to_withdraw", f"{q.id} has no answer to withdraw.")
        self._write([{"type": "withdraw", "question": q.id, **self._base(facts)}])
        self._audit("consult.withdrawn", target={"type": "question", "id": q.id}, details={"question": q.id})
        return self.state()

    def accept(self, body: dict) -> dict:
        """Accept the recommendations, as a person's answers (*Accept all recommendations*, one section, or the
        questions named)."""
        self._person("accept recommendations as answers")
        facts, _ = self._facts()
        section = str(body.get("section") or "") or None
        if section is not None and section not in dict(self.catalog.sections):
            raise ApiError(404, "not_found", "There is no such section.")
        wanted = body.get("questions")
        if wanted is not None and (not isinstance(wanted, list) or not all(isinstance(w, str) for w in wanted)):
            raise ApiError(400, "invalid_request", "questions must be a list of question ids.")
        accepted: List[str] = []
        with self._lock:
            # Accepting one can open the next (its recommendation then applies too): until nothing more is open to accept.
            for _ in range(len(self.catalog.questions)):
                st = self._state(facts)
                found = engine.acceptable(self.catalog, st, section, wanted)
                found = [(q, rec) for q, rec in found if q.id not in accepted]
                if not found:
                    break
                self._write([{"type": "answer", "question": q.id, "value": rec["value"], "source": engine.RECOMMENDATION,
                              "note": None, "facts": engine.evidence_of(q, facts), **self._base(facts)} for q, rec in found])
                accepted += [q.id for q, _ in found]
        if accepted:
            self._audit("consult.accepted", target={"type": "section", "id": section}, details={
                "questions": accepted, "section": section, "count": len(accepted)})
        return {**self.state(), "accepted": accepted}

    def resolve(self, body: dict) -> dict:
        self._person("resolve a contradiction")
        facts, _ = self._facts()
        conflict = self.catalog.conflict(str(body.get("conflict") or ""))
        if conflict is None:
            raise ApiError(404, "not_found", "There is no such contradiction.")
        st = self._state(facts)
        current = next((c for c in st.conflicts if c.id == conflict.id), None)
        if current is None:
            raise ApiError(409, "not_open", "That contradiction does not hold any more.")
        kind = str(body.get("resolution") or "")
        if kind == "take":
            take = next((r for r in conflict.resolutions if r.kind == "take"), None)
            if take is None:
                raise ApiError(400, "invalid_request", "This contradiction has no finding to take; keep it with a reason.")
            q = self._question(take.question)
            self._applicable(st, q)
            raw = (list(st.values.get(q.id) or []) + [take.add]) if take.add is not None else take.value
            try:
                value = q.check(raw, facts)
            except ValueError as exc:
                raise ApiError(400, "invalid_answer", f"{q.id}: {exc}") from None
            note = f"Taken to resolve: {current.message}"[:MAX_NOTE]
            self._write([{"type": "answer", "question": q.id, "value": value, "source": engine.FINDING, "note": note,
                          "facts": engine.evidence_of(q, facts), "conflict": conflict.id, **self._base(facts)},
                         {"type": "resolve", "conflict": conflict.id, "resolution": "take", **self._base(facts)}])
            self._audit("consult.resolved", target={"type": "conflict", "id": conflict.id}, details={
                "conflict": conflict.id, "resolution": "take", "question": q.id, "value": briefs.display(q, value)})
            return self.state()
        if kind == "keep":
            if not any(r.kind == "keep" for r in conflict.resolutions):
                raise ApiError(400, "invalid_request", "This contradiction cannot be kept: take the finding, or change the answer.")
            reason = _text(body.get("reason"), MAX_REASON)
            if not reason:
                raise ApiError(400, "reason_required", "Say why the answers stand as they are.")
            self._write([{"type": "resolve", "conflict": conflict.id, "resolution": "keep", "reason": reason,
                          "facts": engine.conflict_evidence(conflict, facts),
                          "answers": {qid: st.values.get(qid) for qid in conflict.questions}, **self._base(facts)}])
            self._audit("consult.resolved", target={"type": "conflict", "id": conflict.id}, details={
                "conflict": conflict.id, "resolution": "keep", "reason": reason})
            return self.state()
        raise ApiError(400, "invalid_request", "Choose a resolution of take or keep.")

    # -- the brief ----------------------------------------------------------------------------------------------

    def _versions(self) -> List[Dict[str, Any]]:
        events = self._events()
        out = []
        for n in self._numbers():
            doc = self._version(n) or {}
            mine = [e for e in events if e.get("version") == n]
            decided = next((e for e in reversed(mine) if e.get("event") in (briefs.APPROVED, briefs.REJECTED)), None)
            content = doc.get("content") or {}
            out.append({
                "version": n, "hash": doc.get("hash"), "status": str(mine[-1]["event"]) if mine else briefs.SUBMITTED,
                "submittedAt": doc.get("submittedAt"), "submittedBy": doc.get("submittedBy"), "note": doc.get("note"),
                "decidedBy": (decided or {}).get("by"), "decidedAt": (decided or {}).get("at"),
                "comment": (decided or {}).get("comment"), "selfApproved": (decided or {}).get("selfApproved"),
                "catalogVersion": content.get("catalogVersion"), "factsFingerprint": content.get("factsFingerprint"),
                "snapshot": content.get("snapshot"), "decisions": len(content.get("decisions") or []),
                "assumptions": len(content.get("assumptions") or []),
                "separateApprover": [x.get("question") for x in content.get("separateApprover") or []],
            })
        return out

    def in_force(self, facts: Optional[Mapping[str, Any]] = None) -> Optional[Policy]:
        """The policy every consumer reads: the approved brief, judged against the estate as it is now."""
        events = self._events()
        number = briefs.approved_version(events)
        doc = self._version(number) if number is not None else None
        if not doc:
            return None
        approval = next((e for e in reversed(events) if e.get("version") == number and e.get("event") == briefs.APPROVED), {})
        if facts is None:
            try:
                facts, _ = self._facts()
            except ApiError:
                facts = {}
        return Policy(doc, facts, approval, catalog_version=self.catalog.version)

    def _changes(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """The decisions that differ between two brief contents. A brief made against an earlier catalog is read under
        this one's question ids, so a renamed question is not a decision removed and another added."""
        names = self.catalog.renamed.get(str(before.get("catalogVersion") or ""), {})
        old = {names.get(d["question"], d["question"]): d for d in before.get("decisions") or []}
        new = {d["question"]: d for d in after.get("decisions") or []}
        out = []
        for qid in list(old) + [q for q in new if q not in old]:
            a, b = old.get(qid), new.get(qid)
            if a and b and a["value"] == b["value"] and a["assumed"] == b["assumed"]:
                continue
            ref = b or a
            out.append({"question": qid, "text": ref["text"], "before": a["display"] if a else None,
                        "after": b["display"] if b else None,
                        "kind": "added" if a is None else "removed" if b is None else "changed"})
        return out

    def _brief_summary(self, facts: Mapping[str, Any], st: engine.State) -> Dict[str, Any]:
        content = briefs.draft(self.catalog, facts, st)
        draft_hash = briefs.content_hash(content)
        versions = self._versions()
        policy = self.in_force(facts)
        pending = next((v["version"] for v in reversed(versions) if v["status"] == briefs.SUBMITTED), None)
        approved = self._version(policy.version) if policy is not None else None
        return {
            "draftHash": draft_hash, "versions": len(versions), "pending": pending,
            "approved": policy.version if policy else None, "approvedHash": policy.hash if policy else None,
            "approvedBy": policy.approved_by if policy else None, "approvedAt": policy.approved_at if policy else None,
            "stale": bool(policy and policy.stale), "factChanges": policy.changes if policy else [],
            "sameAsApproved": bool(policy and policy.hash == draft_hash),
            "changes": self._changes((approved or {}).get("content") or {}, content) if approved else [],
        }

    def brief(self) -> dict:
        facts, _ = self._facts()
        st = self._state(facts)
        content = briefs.draft(self.catalog, facts, st)
        return {"draft": content, "draftHash": briefs.content_hash(content), "submittable": st.submittable,
                "blockers": st.blockers, "versions": self._versions(), "summary": self._brief_summary(facts, st)}

    def submit(self, body: dict) -> dict:
        self._person("submit the migration brief")
        facts, _ = self._facts()
        st = self._state(facts)
        if not st.submittable:
            shown = " ".join(st.blockers[:3])
            more = f" ({len(st.blockers) - 3} more in Consult.)" if len(st.blockers) > 3 else ""
            raise ApiError(409, "brief_not_ready", f"The brief cannot be submitted yet: {shown}{more}")
        content = briefs.draft(self.catalog, facts, st)
        digest = briefs.content_hash(content)
        who = context.actor().to_dict()
        with self._lock:
            numbers = self._numbers()
            number = (numbers[-1] if numbers else 0) + 1
            doc = {"version": number, "hash": digest, "content": content, "submittedAt": now(), "submittedBy": who,
                   "note": _text(body.get("note"), MAX_NOTE) or None, "answersSeq": (self._answers() or [{"seq": 0}])[-1]["seq"]}
            self._records.put(f"{BRIEFS}/{number:04d}", doc)
            # A version still waiting for a decision cannot be approved once a newer one is submitted.
            superseded = [v["version"] for v in self._versions() if v["status"] == briefs.SUBMITTED and v["version"] != number]
            for old in superseded:
                self._records.append(EVENTS, {"event": briefs.SUPERSEDED, "version": old, "by": who, "at": now(), "supersededBy": number})
            self._records.append(EVENTS, {"event": briefs.SUBMITTED, "version": number, "hash": digest, "by": who, "at": now()})
        self._audit("brief.submitted", target={"type": "brief", "id": str(number)}, details={
            "version": number, "hash": digest, "decisions": len(content["decisions"]), "assumptions": len(content["assumptions"]),
            "kept": len(content["keptContradictions"]), "catalogVersion": content["catalogVersion"],
            "factsFingerprint": content["factsFingerprint"], "snapshot": content["snapshot"], "superseded": superseded})
        return self.brief()

    def approve(self, body: dict) -> dict:
        """A person approves the brief version waiting for a decision, or rejects it. The accelerator never does."""
        decision = str(body.get("decision") or "")
        if decision not in ("approve", "reject"):
            raise ApiError(400, "invalid_request", "Choose approve or reject.")
        who = context.actor()
        if who.kind != context.PERSON:
            raise ApiError(403, "person_only", "Only a person can approve a migration brief.")
        try:
            number = int(body.get("version"))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_request", "Say which version (version).") from None
        facts, _ = self._facts()
        comment = _text(body.get("comment"), MAX_NOTE) or None
        with self._lock:
            doc = self._version(number)
            versions = {v["version"]: v for v in self._versions()}
            if doc is None or number not in versions:
                raise ApiError(404, "not_found", "There is no such brief version.")
            if versions[number]["status"] != briefs.SUBMITTED:
                raise ApiError(409, "not_waiting", f"Version {number} is {versions[number]['status']}, not waiting for a decision.")
            if body.get("hash") and body["hash"] != doc["hash"]:
                raise ApiError(409, "stale", "What is being approved is not this version. Read the brief again.")
            if decision == "reject":
                self._records.append(EVENTS, {"event": briefs.REJECTED, "version": number, "hash": doc["hash"],
                                              "by": who.to_dict(), "at": now(), "comment": comment})
                self._audit("brief.rejected", target={"type": "brief", "id": str(number)}, reason=comment,
                            details={"version": number, "hash": doc["hash"], "comment": comment})
                return self.brief()
            content = doc.get("content") or {}
            if major(content.get("catalogVersion")) != major(self.catalog.version):
                raise ApiError(409, "brief_stale", f"The questions changed since version {number} was submitted (catalog "
                               f"{content.get('catalogVersion') or '?'} to {self.catalog.version}). Review them in Consult, "
                               "submit the brief again, then approve that version.")
            if content.get("factsFingerprint") != fingerprint(facts):
                raise ApiError(409, "brief_stale", f"The estate changed since version {number} was submitted. Submit the brief "
                                                   "again, from the estate as it is now, then approve that version.")
            loosened = [x.get("question") for x in content.get("separateApprover") or []]
            self_approved = (doc.get("submittedBy") or {}).get("id") == who.id
            if loosened and self_approved and os.environ.get("MIGRATION_BRIEF_APPROVAL", "").strip().lower() == "separate":
                raise ApiError(403, "separate_approver", f"Version {number} asks for an approver other than the person who "
                               f"submitted it ({', '.join(loosened)}). Someone else approves it.")
            previous = briefs.approved_version(self._events())
            if previous is not None:
                self._records.append(EVENTS, {"event": briefs.SUPERSEDED, "version": previous, "by": who.to_dict(), "at": now(),
                                              "supersededBy": number})
            # The Fabric workspace that is the target as the person approves: what the brief says about the target (E1)
            # is about this one, and the brief stops fitting once another is connected (``Policy.target_moved``).
            pinned = ({"workspaceId": facts["target.workspaceId"], "workspaceName": facts.get("target.workspace")}
                      if facts.get("target.workspaceId") else None)
            self._records.append(EVENTS, {"event": briefs.APPROVED, "version": number, "hash": doc["hash"], "by": who.to_dict(),
                                          "at": now(), "comment": comment, "selfApproved": self_approved,
                                          "separateApprover": loosened, "target": pinned})
        self._audit("brief.approved", target={"type": "brief", "id": str(number)}, reason=comment, details={
            "version": number, "hash": doc["hash"], "comment": comment, "selfApproved": self_approved,
            "separateApprover": loosened, "superseded": previous, "targetWorkspace": (pinned or {}).get("workspaceName")})
        return self.brief()

    def export(self, query: Mapping[str, str]) -> Tuple[bytes, str, str]:
        """(body, media type, file name) of one version, or the one in force: Markdown for people, JSON as kept."""
        fmt = str(query.get("format") or "md").lower()
        if fmt not in ("md", "json"):
            raise ApiError(400, "invalid_request", "Choose a format of md or json.")
        raw = query.get("version")
        if raw in (None, ""):
            policy = self.in_force({})
            numbers = self._numbers()
            number = policy.version if policy else (numbers[-1] if numbers else None)
        else:
            number = _whole(raw, None)
        doc = self._version(number) if number is not None else None
        if doc is None:
            raise ApiError(404, "not_found", "There is no such brief version. Submit the brief first.")
        events = self._events()
        self._audit("brief.exported", target={"type": "brief", "id": str(number)}, details={"version": number, "format": fmt})
        if fmt == "json":
            body = json.dumps({**doc, "events": [e for e in events if e.get("version") == number]}, indent=2, ensure_ascii=False, default=str)
            return body.encode("utf-8"), "application/json", f"migration-brief-v{number}.json"
        text = briefs.to_markdown(doc, briefs.status_of(events, number), events)
        return text.encode("utf-8"), "text/markdown; charset=utf-8", f"migration-brief-v{number}.md"

    # -- the policy, as Plan and Migrate read it ------------------------------------------------------------------

    def excluded(self, policy: Policy, estate: Estate, ids: Optional[Iterable[str]] = None) -> Dict[str, str]:
        """The objects the brief leaves out of the plan, by id, with why."""
        out = {}
        for oid in ids if ids is not None else estate.items:
            item = estate.items.get(oid)
            if item is not None:
                why = policy.excluded(item, estate.group_of, estate.flags)
                if why:
                    out[oid] = why
        return out

    def policy_view(self) -> dict:
        facts, estate = self._facts()
        policy = self.in_force(facts)
        out: Dict[str, Any] = {"mode": mode(), "inForce": None, "llm": self.llm_gate("transform")}
        if policy is None:
            return out
        out.update({
            "inForce": policy.describe(), "runOptions": policy.run_options(capabilities.STAGE_KEYS),
            "dataFilters": policy.data_filters(estate.filterable), "storageRoutes": policy.get("storage.routes"),
            "excluded": self.excluded(policy, estate), "signoff": policy.signoff(),
            "names": dict(zip(("warehouse", "lakehouse"), policy.names())), "validate": policy.validate_runtime(),
            "signIn": policy.sign_in(), "sqlUserMap": policy.sql_user_map(),
            # Taken by the run from the brief itself, as the names are: shown in Plan, never changed there.
            "run": {"parallelCopies": policy.parallel_copies(), "schedules": policy.schedules(),
                    "maskColumns": policy.mask_columns(), "targetRole": policy.target_role()},
            "assistant": {"autonomy": policy.autonomy()},
            "llmScope": policy.llm_scope(), "llmContext": policy.llm_context(),
        })
        return out

    def differences(self, policy: Policy, entries: List[Mapping[str, Any]], options: Mapping[str, Any], estate: Estate) -> List[Dict[str, Any]]:
        ids = [str(e.get("id") or "") for e in entries]
        wanted = set(ids)
        tables = [t for t in estate.filterable if t["id"] in wanted] if "data" in (options.get("stages") or []) else None
        filters = options.get("dataFilters") or {}
        given = {**options, "dataFilters": {k: (f.to_dict() if hasattr(f, "to_dict") else f) for k, f in filters.items()}}
        return policy.differences(given, capabilities.STAGE_KEYS, self.excluded(policy, estate, ids), tables)

    def check_plan(self, entries: List[Mapping[str, Any]], options: Mapping[str, Any], job: Any, pool: Optional[str]) -> Dict[str, Any]:
        """How a plan stands against the brief, for the Plan step. Reads only, never refuses."""
        facts, estate = self.facts_of(job, pool)
        policy = self.in_force(facts)
        if policy is None:
            return {"mode": mode(), "version": None, "stale": False, "differences": [], "explained": False}
        differences = self.differences(policy, entries, options, estate)
        return {"mode": mode(), "version": policy.version, "hash": policy.hash, "stale": policy.stale,
                "changes": policy.changes, "differences": differences,
                "explained": bool(differences) and self._covering(policy, differences) is not None}

    def run_gate(self, entries: List[Mapping[str, Any]], options: Mapping[str, Any], job: Any, pool: Optional[str],
                 reason: Optional[str] = None) -> Tuple[Dict[str, Any], Optional[Policy], Optional[str]]:
        """What a run starts under: the brief in force, and where the run differs from it, explained. Under a required
        consultation, refuses a run without an approved brief, with one that no longer fits the estate, and one that
        differs from it with no reason given (``reason``, or one recorded when Plan was confirmed).

        Returns (what the run pins, the policy, a reason still to record): a reason given with the request is recorded
        (``record_deviation``) by the caller once the run exists, so a run refused later leaves no deviation behind."""
        facts, estate = self.facts_of(job, pool)
        policy = self.in_force(facts)
        required = mode() == REQUIRED
        if policy is None:
            if required:
                raise ApiError(409, "brief_required", "No migration brief is approved yet. Answer the questions in Consult, "
                                                      "submit the brief and have it approved, then start the run.")
            return {"mode": mode(), "version": None, "differences": []}, None, None
        if policy.stale and required:
            raise ApiError(409, "brief_stale", self.stale_message(policy))
        differences = self.differences(policy, entries, options, estate)
        deviation, pending = None, None
        if differences:
            deviation = self._covering(policy, differences)
            if deviation is None:
                pending = _text(reason, MAX_REASON) or None
            if deviation is None and pending is None and required:
                shown = " ".join(d["message"] for d in differences[:3])
                raise ApiError(409, "deviation_unexplained", f"The run differs from the approved brief (version {policy.version}): "
                               f"{shown} Confirm the plan with a reason, or change it to match the brief.")
        if pending:
            deviation = {"seq": None, "reason": pending, "by": context.actor().to_dict(), "at": now()}
        return {"mode": mode(), "version": policy.version, "hash": policy.hash, "approvedBy": policy.approved_by,
                "approvedAt": policy.approved_at, "stale": policy.stale, "differences": differences,
                "deviation": {k: deviation.get(k) for k in ("seq", "reason", "by", "at")} if deviation else None}, policy, pending

    @staticmethod
    def stale_message(policy: Policy) -> str:
        shown = "; ".join(f"{c['description']}: {_shown(c['before'])} → {_shown(c['after'])}" for c in policy.changes[:3])
        what = "The Fabric target changed" if policy.target_moved and len(policy.changes) == 1 else "The estate changed"
        return (f"{what} since brief version {policy.version} was approved ({shown}). "
                "Reconfirm it in Consult: submit the brief again and have it approved.")

    def still_holds(self, pinned: Optional[Mapping[str, Any]]) -> Optional[str]:
        """Why a run asked for under the brief it pinned may not start now, or None: under a required consultation, the
        brief in force must still be the one the run was checked against, and still fit the estate. (A sign-off can come
        long after the request: a brief approved, rejected or made stale in between would otherwise go unnoticed.)"""
        if mode() != REQUIRED:
            return None
        policy = self.in_force()
        if policy is None:
            return "No migration brief is in force any more. Have one approved in Consult, then start the run again."
        if policy.stale:
            return self.stale_message(policy)
        if not pinned or pinned.get("hash") != policy.hash:
            return (f"The migration brief changed since this run was asked for: version {policy.version} is in force now. "
                    "Decline this run and start it again, so it is checked against the brief in force.")
        return None

    @staticmethod
    def _key(differences: Iterable[Mapping[str, Any]]) -> Set[str]:
        return {json.dumps([d.get("option"), d.get("plan")], sort_keys=True, default=str) for d in differences}

    def _covering(self, policy: Policy, differences: List[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        """The latest deviation recorded against these decisions (the brief's content hash, so a version approved again
        only because the estate moved keeps it) that explains every one of these differences."""
        wanted = self._key(differences)
        for record in reversed(self._records.records(DEVIATIONS)):
            if record.get("briefHash") == policy.hash and wanted <= self._key(record.get("differences") or []):
                return record
        return None

    def record_deviation(self, policy: Policy, differences: List[Mapping[str, Any]], reason: str) -> Dict[str, Any]:
        record = self._records.append(DEVIATIONS, {
            "briefVersion": policy.version, "briefHash": policy.hash, "reason": reason, "by": context.actor().to_dict(),
            "at": now(), "differences": [{k: d.get(k) for k in ("option", "setting", "brief", "plan", "message")} for d in differences]})
        self._audit("plan.deviation", target={"type": "brief", "id": str(policy.version)}, reason=reason, details={
            "briefVersion": policy.version, "differences": [d.get("message") for d in differences], "reason": reason})
        return record

    def deviation(self, body: dict) -> dict:
        """A person confirms a plan that differs from the brief, and says why: what lets a required run start."""
        self._person("confirm a plan that differs from the brief")
        plan = body.get("items")
        if not isinstance(plan, list) or not all(isinstance(p, dict) for p in plan):
            raise ApiError(400, "invalid_plan", "The migration plan is not in a valid format.")
        options = parse_options(body)
        job, pool = self._session.migration_snapshot()
        facts, estate = self.facts_of(job, pool)
        policy = self.in_force(facts)
        if policy is None:
            raise ApiError(409, "brief_required", "No migration brief is approved yet, so there is nothing to differ from.")
        differences = self.differences(policy, plan, options, estate)
        if not differences:
            return {"differences": [], "recorded": False}
        reason = _text(body.get("reason"), MAX_REASON)
        if not reason:
            raise ApiError(400, "reason_required", "Say why the plan differs from the approved brief.")
        record = self.record_deviation(policy, differences, reason)
        return {"differences": differences, "recorded": True, "seq": record.get("seq")}

    # -- the migration context, sent with every LLM draft ---------------------------------------------------------

    def context_file(self, policy: Optional[Policy] = None) -> Optional[context_files.ContextFile]:
        """MIGRATION_CONTEXT.md for the brief in force (or ``policy``), kept in the project's store by its hash so the
        one any draft was made with can be read back; None when no brief is approved."""
        policy = policy if policy is not None else self.in_force()
        if policy is None:
            return None
        made = context_files.render(policy)
        if made.hash not in self._contexts:
            try:
                self._records.put_blob(made.text.encode("utf-8"))
                self._contexts.add(made.hash)
            except Exception as exc:  # noqa: BLE001 - the draft still goes with it; only reading it back later is lost
                _log.warning("Could not keep the migration context %s: %s: %s", made.hash[:12], type(exc).__name__, exc)
        return made

    def context_view(self) -> dict:
        """The migration context every LLM draft is sent now, as the page shows it."""
        made = self.context_file()
        if made is None:
            return {"file": context_files.FILE_NAME, "hash": None, "decisionsHash": None, "briefVersion": None, "briefHash": None,
                    "minimal": None, "text": None,
                    "reason": "No migration brief is approved yet, so drafts are made without a migration context."}
        return {**made.reference(), "text": made.text, "reason": None}

    def context_export(self, digest: str) -> Tuple[bytes, str, str]:
        """(body, media type, file name) of the migration context with this hash: the one a draft was made with."""
        data = self._records.blob(digest) if re.fullmatch(r"[0-9a-f]{64}", digest or "") else None
        if data is None:
            raise ApiError(404, "not_found", "There is no migration context with that hash in this project.")
        return data, "text/markdown; charset=utf-8", f"MIGRATION_CONTEXT-{digest[:12]}.md"

    # -- the LLM gate ------------------------------------------------------------------------------------------

    def _spent(self) -> float:
        return round(sum(float(r.get("costUsd") or 0) for r in self._records.records(SPEND)), 4)

    def record_spend(self, purpose: str, model: Optional[str], cost: Optional[float], prompt_hash: str,
                     object_id: Optional[str] = None) -> None:
        """What one LLM call cost, against the project's budget. Never raises."""
        try:
            self._records.append(SPEND, {"at": now(), "purpose": purpose, "model": model, "costUsd": cost,
                                         "promptHash": prompt_hash, "object": object_id, "by": context.actor().to_dict()})
        except Exception as exc:  # noqa: BLE001 - a lost line of the ledger is logged, never a failed draft
            _log.warning("Could not record what an LLM call cost: %s: %s", type(exc).__name__, exc)

    def llm_gate(self, purpose: str = "transform") -> Dict[str, Any]:
        """Whether an LLM call may be made now (``purpose``: transform | consult), and why not. See the module notes."""
        answers, _, _ = engine.fold(self._answers())
        live = answers.get("D1", {}).get("value") if "D1" in answers else None
        live_budget = answers.get("D2", {}).get("value") if "D2" in answers else None
        policy = self.in_force({})
        approved, budget = policy.llm() if policy is not None else (None, None)
        if purpose == "consult" and live is not None and policy is None:
            # Before any brief is approved, the consultation's own help follows a person's latest answer to D1. Once one
            # is, the brief decides: a "yes" given since loosens nothing until a brief with it is approved.
            approved = live
        if live_budget is not None and (budget is None or live_budget < budget):
            budget = float(live_budget)  # a lower budget holds at once; a higher one once a brief with it is approved
        if budget is None and approved:
            d2 = self.catalog.question("D2")
            rec = engine.recommendation(d2, {}, {"D1": True}) if d2 is not None else None
            budget = float(rec["value"]) if rec else None
        spent = self._spent()
        out = {"purpose": purpose, "mode": mode(), "consent": approved, "liveConsent": live, "budgetUsd": budget,
               "spentUsd": spent, "allowed": True, "reason": None, "code": None}
        if live is False:
            return {**out, "allowed": False, "code": "llm_no_consent",
                    "reason": "D1 says object definitions may not be sent to Claude. Change the answer in Consult to use AI."}
        if purpose == "assistant" and policy is None:
            # The assistant's task loop reads the estate and proposes changes: only ever with an approved brief's consent.
            return {**out, "allowed": False, "code": "llm_no_consent",
                    "reason": "The assistant uses the model only with consent from an approved brief (D1)."}
        spent_all = None if budget is None or spent < budget else {
            **out, "allowed": False, "code": "llm_budget_reached",
            "reason": f"This project has spent ${spent:,.2f} of its ${budget:,.2f} AI budget (D2). Raise the budget in "
                      "Consult to go on."}
        if approved is None:
            if mode() == REQUIRED or purpose == "consult":
                return {**out, "allowed": False, "code": "llm_no_consent", "reason": (
                    "Answer D1 in Consult first: whether object definitions may be sent to Claude."
                    if purpose == "consult" or live is None else
                    "D1 allows sending definitions to Claude, but no brief that says so is approved yet. Submit the brief "
                    "and have it approved first.")}
            # Advisory, and no brief decides: as before there were briefs, within a budget a person has set (D2).
            return spent_all or out
        if approved is False:
            return {**out, "allowed": False, "code": "llm_no_consent",
                    "reason": f"The approved brief (version {policy.version if policy else '?'}) does not allow sending "
                              "definitions to Claude (D1)."}
        return spent_all or out

    def refuse_llm(self, gate: Mapping[str, Any]) -> None:
        """Record that an LLM call was refused, and why."""
        self._audit("llm.refused", target={"type": "llm", "id": gate.get("purpose")}, outcome="refused", code=gate.get("code"),
                    reason=gate.get("reason"), details={"purpose": gate.get("purpose"), "reason": gate.get("reason"),
                                                        "spent": gate.get("spentUsd"), "budget": gate.get("budgetUsd")})

    def _model_or_none(self) -> Any:
        """The model for the consultation's help, when it may be used now; None means the rules answer instead."""
        if not claude_cli.switched_on():
            return None
        model = self._model or claude_cli.ClaudeCli()
        if not model.available().get("available"):
            return None
        gate = self.llm_gate("consult")
        if not gate["allowed"]:
            return None
        self._model = model
        return model

    # -- help: answers read from free text, explanations ----------------------------------------------------------

    def _summary_for_model(self, facts: Mapping[str, Any]) -> Dict[str, Any]:
        """What the model is told about the estate: counts and yes/no only, never a name or a definition."""
        return {n: v for n, v in facts.items() if n in FACTS and isinstance(v, (bool, int, float)) and not FACTS[n].informational}

    @staticmethod
    def _recommended_for_model(q: Question, s: engine.QuestionState) -> Any:
        """The recommendation, as the model may see it: only a value that is one of the catalog's own (an option, yes or
        no, a number). Never its reason, which quotes facts such as the pool's name, and never a value made of names
        (a Warehouse name, the schemas in scope, a principal map)."""
        value = (s.recommendation or {}).get("value")
        if q.type in ("choice", "boolean", "number") or (q.type == "multi" and not q.options_from):
            return value
        return None

    def _questions_for_model(self, st: engine.State) -> List[Dict[str, Any]]:
        out = []
        for q in self.catalog.questions:
            if not st.questions[q.id].applies or q.options_from or q.rows_from:
                continue  # a question whose choices are names from the estate is answered on the page
            out.append({"id": q.id, "type": q.type, "text": q.text, "options": [o.value for o in q.options] or None})
        return out

    def _propose_with_model(self, model: Any, text: str, facts: Mapping[str, Any], st: engine.State) -> Tuple[List[Dict[str, Any]], str]:
        prompt = json.dumps({
            "task": ("Read the consultant's notes and propose answers to the questions they answer plainly. For each, give "
                     "the question id, the value as JSON text (a string in quotes, true or false, a number, or a list), and "
                     "the words of the notes it rests on. Leave out any question the notes do not answer."),
            "today": _dt.date.today().isoformat(), "estate": self._summary_for_model(facts),
            "questions": self._questions_for_model(st), "notes": text}, ensure_ascii=False)
        reply = model.ask(prompt, PROPOSE_SCHEMA, CONSULT_SYSTEM)
        self.record_spend("consult", reply.model, reply.cost_usd, _hash(prompt))
        out = []
        for p in (reply.data.get("proposals") or [])[:40]:
            try:
                value = json.loads(str(p.get("value")))
            except ValueError:
                value = str(p.get("value"))
            out.append({"question": str(p.get("question") or ""), "value": value, "reason": _text(p.get("reason"), MAX_NOTE)})
        return out, reply.model or "Claude"

    def _keep_valid(self, proposed: List[Dict[str, Any]], facts: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """The proposals that are valid answers to questions that apply, once they are all taken together."""
        kept, rejected, hypothetical = [], [], []
        seen: Set[str] = set()
        for p in proposed:
            if p["question"] in seen:
                continue  # one proposal for each question: the first
            seen.add(p["question"])
            q = self.catalog.question(p["question"])
            if q is None:
                rejected.append({**p, "message": "There is no such question."})
                continue
            try:
                value = q.check(p["value"], facts)
            except ValueError as exc:
                rejected.append({**p, "message": str(exc)})
                continue
            kept.append({**p, "value": value})
            hypothetical.append({"type": "answer", "question": q.id, "value": value, "source": engine.PERSON})
        st = self._state(facts, hypothetical)
        applies = [p for p in kept if st.questions[p["question"]].applies]
        rejected += [{**p, "message": "It does not apply to this estate."} for p in kept if p not in applies]
        return applies, rejected

    def _store_proposals(self, proposed: List[Dict[str, Any]], source: str, model: Optional[str], facts: Mapping[str, Any]) -> List[Dict[str, Any]]:
        batch = uuid.uuid4().hex[:8]
        out = []
        with self._lock:
            for p in proposed:
                out.append(self._records.append(PROPOSALS, {
                    "type": "proposal", "id": f"{batch}-{len(out) + 1}", "batch": batch, "question": p["question"],
                    "value": p["value"], "reason": p.get("reason"), "source": source, "model": model,
                    "requestedBy": context.actor().to_dict(), "at": now(), "catalogVersion": self.catalog.version,
                    "snapshot": facts.get("discovery.snapshot")}))
        return out

    def _pending(self, st: engine.State) -> List[Dict[str, Any]]:
        """Proposals no person has decided on yet: the latest for each question that applies."""
        decided = set()
        latest: Dict[str, Dict[str, Any]] = {}
        for r in (dict(self.catalog.translate(x)) for x in self._records.records(PROPOSALS)):
            if r.get("type") == "decision":
                decided.add(r.get("id"))
            elif r.get("type") == "proposal":
                latest[str(r.get("question"))] = r
        out = []
        for qid, p in latest.items():
            s = st.questions.get(qid)
            if p.get("id") in decided or s is None or not s.applies:
                continue
            if s.status == engine.ANSWERED and s.value == p.get("value"):
                continue  # answered so already: nothing left to confirm
            q = self.catalog.question(qid)
            out.append({**{k: p.get(k) for k in ("id", "question", "value", "reason", "source", "model", "at")},
                        "display": briefs.display(q, p.get("value")) if q is not None else str(p.get("value")),
                        "current": briefs.display(q, s.value) if q is not None and s.value is not None else None})
        order = {q.id: n for n, q in enumerate(self.catalog.questions)}
        return sorted(out, key=lambda p: order.get(p["question"], 999))

    def propose(self, body: dict) -> dict:
        """Answers read from what a person wrote (meeting notes, an email), for that person to confirm one by one."""
        text = str(body.get("text") or "").strip()
        if not text:
            raise ApiError(400, "invalid_request", "Paste the notes to read answers from.")
        if len(text) > MAX_TEXT:
            raise ApiError(413, "body_too_large", f"At most {MAX_TEXT:,} characters of notes.")
        facts, _ = self._facts()
        st = self._state(facts)
        model = self._model_or_none() if body.get("useAi", True) else None
        message, source, name = None, "rules", None
        if model is not None:
            try:
                proposed, name = self._propose_with_model(model, text, facts, st)
                source = engine.AI
            except claude_cli.ModelError as exc:
                message = f"Claude could not be asked ({exc}); the notes were read with the built-in rules instead."
                proposed = rule_proposals(text)
        else:
            proposed = rule_proposals(text)
            gate = self.llm_gate("consult")
            if not gate["allowed"] and body.get("useAi", True):
                message = f"The notes were read with the built-in rules: {gate['reason']}"
        kept, rejected = self._keep_valid(proposed, facts)
        stored = self._store_proposals(kept, source, name, facts)
        self._audit("consult.proposed", target={"type": "proposals", "id": stored[0]["batch"] if stored else None}, details={
            "source": source, "model": name, "count": len(stored), "questions": [p["question"] for p in stored],
            "textHash": _hash(text), "message": message})
        return {**self.state(), "proposed": [p["id"] for p in stored], "rejected": rejected, "source": source, "message": message}

    def decide(self, body: dict) -> dict:
        """A person confirms proposed answers (each becomes their answer, saying where it came from), or rejects them."""
        decision = str(body.get("decision") or "")
        if decision not in ("confirm", "reject"):
            raise ApiError(400, "invalid_request", "Choose confirm or reject.")
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ApiError(400, "invalid_request", "Send the proposals' ids.")
        self._person("decide on proposed answers")
        facts, _ = self._facts()
        answered = []
        with self._lock:
            pending = {p["id"]: p for p in self._pending(self._state(facts))}
            missing = [i for i in ids if i not in pending]
            if missing:
                raise ApiError(409, "not_pending", f"Proposal {missing[0]} is not waiting for a decision any more. Read the page again.")
            # Every one is checked before anything is written, in order (confirming one can open or close the next):
            # a batch is decided whole, or not at all.
            planned: List[Tuple[str, Dict[str, Any], Question, Optional[Dict[str, Any]]]] = []
            extra: List[Dict[str, Any]] = []
            for pid in dict.fromkeys(ids):
                p = pending[pid]
                q = self._question(p["question"])
                record = None
                if decision == "confirm":
                    self._applicable(self._state(facts, extra), q)
                    try:
                        value = q.check(p["value"], facts)
                    except ValueError as exc:
                        raise ApiError(400, "invalid_answer", f"{q.id}: {exc}") from None
                    source = {"ai": engine.AI, "import": engine.IMPORT}.get(str(p.get("source")), engine.RULES)
                    record = {"type": "answer", "question": q.id, "value": value, "source": source, "note": p.get("reason"),
                              "facts": engine.evidence_of(q, facts), "proposal": pid, **self._base(facts)}
                    extra.append(record)
                planned.append((pid, p, q, record))
            for pid, p, q, record in planned:
                if record is not None:
                    self._write([record])
                    answered.append(q.id)
                self._records.append(PROPOSALS, {"type": "decision", "id": pid, "decision": "confirmed" if record is not None else "rejected",
                                                 "by": context.actor().to_dict(), "at": now()})
                details = {"proposal": pid, "question": q.id, "value": p.get("display"), "source": p.get("source")}
                if record is not None:
                    self._audit("consult.proposal_confirmed", target={"type": "question", "id": q.id}, details=details)
                else:
                    self._audit("consult.proposal_rejected", target={"type": "question", "id": q.id}, details=details)
        return {**self.state(), "answered": answered}

    def explain(self, body: dict) -> dict:
        """What a question means for this estate: from the model when it may be asked, else from the catalog itself."""
        facts, _ = self._facts()
        q = self._question(body.get("question"))
        st = self._state(facts)
        s = st.questions[q.id]
        rules = self._explain_by_rules(q, s, facts)
        model = self._model_or_none() if body.get("useAi", True) else None
        if model is None:
            self._audit("consult.explained", target={"type": "question", "id": q.id}, details={"question": q.id, "source": "rules"})
            return {"question": q.id, "text": rules, "source": "rules", "model": None}
        prompt = json.dumps({
            "task": ("Explain to the consultant, in a short paragraph of plain words, what this question decides for this "
                     "estate, what each choice would do, and what the recommendation rests on. Do not repeat the question."),
            "question": {"id": q.id, "text": q.text, "help": q.help, "options": [{"value": o.value, "label": o.label,
                                                                                    "description": o.description} for o in q.options],
                         "recommended": self._recommended_for_model(q, s)},
            "estate": self._summary_for_model(facts)}, ensure_ascii=False)
        try:
            reply = model.ask(prompt, EXPLAIN_SCHEMA, CONSULT_SYSTEM)
        except claude_cli.ModelError as exc:
            return {"question": q.id, "text": rules, "source": "rules", "model": None,
                    "message": f"Claude could not be asked ({exc}); this is the catalog's own explanation."}
        self.record_spend("consult", reply.model, reply.cost_usd, _hash(prompt))
        self._audit("consult.explained", target={"type": "question", "id": q.id},
                    details={"question": q.id, "source": "ai", "model": reply.model})
        return {"question": q.id, "text": _text(reply.data.get("text"), 3000) or rules, "source": "ai", "model": reply.model or "Claude"}

    @staticmethod
    def _explain_by_rules(q: Question, s: engine.QuestionState, facts: Mapping[str, Any]) -> str:
        parts = [q.help] if q.help else []
        if q.options:
            parts.append(" ".join(f"{o.label}: {o.description.rstrip('.')}." if o.description else f"{o.label}." for o in q.options))
        if s.recommendation:
            parts.append(f"Recommended: {s.recommendation.get('reason')}")
        shown = [f"{FACTS[n].description}: {_shown(facts.get(n))}" for n in q.evidence if n in FACTS]
        if shown:
            parts.append("What it rests on: " + "; ".join(shown) + ".")
        drives = "; ".join(f"{SETTINGS[n].applies} ({SETTINGS[n].status})" for n in q.drives if n in SETTINGS)
        parts.append(f"It decides: {drives}.")
        return " ".join(p.strip() for p in parts if p and p.strip())

    # -- the questionnaire, offline -------------------------------------------------------------------------------

    def questionnaire(self, query: Mapping[str, str]) -> Tuple[bytes, str, str]:
        fmt = str(query.get("format") or "md").lower()
        if fmt not in ("md", "json"):
            raise ApiError(400, "invalid_request", "Choose a format of md or json.")
        facts, _ = self._facts()
        st = self._state(facts)
        live = [(q, st.questions[q.id]) for q in self.catalog.questions if st.questions[q.id].applies]
        self._audit("consult.exported", target={"type": "questionnaire", "id": None}, details={"format": fmt, "questions": len(live)})
        name = f"migration-questionnaire-{facts.get('discovery.snapshot') or 'draft'}.{fmt}"
        if fmt == "json":
            body = {"catalogVersion": self.catalog.version, "snapshot": facts.get("discovery.snapshot"),
                    "instructions": "Fill in answers (each a JSON value, as in 'choices'), then import this file in Consult.",
                    "questions": [{"id": q.id, "section": q.section, "type": q.type, "text": q.text, "help": q.help,
                                   "choices": q.choices(facts) if q.type in ("choice", "multi") else None,
                                   "rows": list(facts.get(q.rows_from) or []) if q.rows_from else None,
                                   "recommended": (s.recommendation or {}).get("value"), "current": s.value, "status": s.status}
                                  for q, s in live],
                    "answers": {q.id: s.value for q, s in live if s.value is not None}}
            return json.dumps(body, indent=2, ensure_ascii=False, default=str).encode("utf-8"), "application/json", name
        lines = ["# Migration questionnaire", "", f"Catalog {self.catalog.version}, discovery snapshot "
                 f"{facts.get('discovery.snapshot') or '—'}. Answer each question; the recommendation is what stands if you "
                 "do not. Return it, or import the JSON version in Consult.", ""]
        titles = dict(self.catalog.sections)
        section = None
        for q, s in live:
            if q.section != section:
                section = q.section
                lines += [f"## {titles.get(section, section)}", ""]
            lines.append(f"### {q.id}. {q.text}" + (" (required)" if q.required else ""))
            if q.help:
                lines += ["", q.help]
            if q.options:
                lines.append("")
                lines += [f"- [{'x' if s.value == o.value else ' '}] {o.label}" + (f": {o.description}" if o.description else "")
                          for o in q.options]
            if s.recommendation:
                lines += ["", f"Recommended: {briefs.display(q, s.recommendation['value'])}. {s.recommendation['reason']}"]
            lines += ["", f"Your answer: {briefs.display(q, s.value) if s.status == engine.ANSWERED else '______'}", ""]
        return "\n".join(lines).encode("utf-8"), "text/markdown; charset=utf-8", name

    def import_answers(self, body: dict) -> dict:
        """A questionnaire's answers, each proposed for a person to confirm: nothing is answered by a file alone."""
        answers = body.get("answers")
        if not isinstance(answers, dict) or not answers or len(answers) > len(self.catalog.questions):
            raise ApiError(400, "invalid_request", "Send the questionnaire's answers, by question id.")
        facts, _ = self._facts()
        st = self._state(facts)
        proposed = []
        for qid, value in answers.items():
            s = st.questions.get(str(qid))
            if s is not None and s.status == engine.ANSWERED and s.value == value:
                continue  # already answered so
            proposed.append({"question": str(qid), "value": value, "reason": "From the imported questionnaire."})
        kept, rejected = self._keep_valid(proposed, facts)
        stored = self._store_proposals(kept, engine.IMPORT, None, facts)
        self._audit("consult.proposed", target={"type": "proposals", "id": stored[0]["batch"] if stored else None}, details={
            "source": "import", "model": None, "count": len(stored), "questions": [p["question"] for p in stored],
            "textHash": None, "message": f"{len(rejected)} answer(s) could not be used" if rejected else None})
        return {**self.state(), "proposed": [p["id"] for p in stored], "rejected": rejected, "source": "import"}


def _whole(value: Any, default: Any) -> Any:
    """A whole number sent as a number or as plain ASCII digits, else ``default`` (``isdigit`` also takes digits such as
    '²' or '٣', which ``int`` then refuses)."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    text = str(value if value is not None else "").strip()
    return int(text) if re.fullmatch(r"[0-9]{1,12}", text) else default


def _shown(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value[:5]) + (f" and {len(value) - 5} more" if len(value) > 5 else "") if value else "none"
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in list(value.items())[:5]) or "none"
    return str(value)
