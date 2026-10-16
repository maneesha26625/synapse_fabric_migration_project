"""One migration run: the plan's objects, moved into Fabric one at a time.

What this build migrates:

* **Notebooks** -> Fabric Notebook items, through the Fabric REST API.
* **The dedicated SQL pool** -> a Fabric Warehouse of the same name (created
  if missing, reused if it exists).
* **Schemas, tables, views, stored procedures** -> that Warehouse, as schema
  only: tables are created empty (with their keys, NOT ENFORCED), views and
  procedures from their own text after the T-SQL rules (``tsql_rules``) have
  removed what a Warehouse rejects.
* **Table data** -> Fabric data pipelines, one per wave (``stages``).
* **Functions** -> the Warehouse, like views (scalar and inline table-valued only).
* **Roles and users** -> Warehouse roles, members and GRANT/DENY permissions (``security``);
  dynamic data masks are written into each table's CREATE TABLE.

Everything else in the plan is marked DEFERRED with the reason, so a run never
pretends to have moved what it cannot move yet. An object that already exists
in Fabric is SKIPPED, never overwritten, which makes a re-run safe.

The runner talks to Fabric and SQL only through the two factories it is given,
which is what lets the tests drive it with fakes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from discovery_agent.diagnostics import record_unexpected, redacted
from discovery_agent.migration import environments, failures, notebooks, run_state, security, tsql_rules, warehouse_ddl
from discovery_agent.migration.common import (  # noqa: F401 - re-exported: tests and the API import these from here
    COMPLETED, CONNECTION, DATA, DATASET, DEFERRED, DEFERRED_STATUS, ENVIRONMENT, FAILED, FUNCTION, IN_PROGRESS, MISSING,
    NOTEBOOK, PENDING, PIPELINE, POOL, PROCEDURE, SCHEDULE, SCHEMA, SCRIPT, SECURITY, SHORTCUT, SKIPPED, SPARKJOB, TABLE,
    VIEW, WORKSPACE_ERRORS, MigrationError, Source, override_note,
)
from discovery_agent.migration.stages import StageMixin
from discovery_agent.migration.stages_fabric import FabricStageMixin
from discovery_agent.migration.fabric_rest import FabricApiError, FabricRestClient
from discovery_agent.state import context
from discovery_agent.state.context import fields as context_fields

_log = logging.getLogger("discovery_agent.run")

#: The UI types this build can migrate, and the kind each becomes.
MIGRATABLE = {
    "Dedicated SQL Pool": POOL, "Schema": SCHEMA, "Table": TABLE, "View": VIEW,
    "Stored Procedure": PROCEDURE, "Notebook": NOTEBOOK, "Spark Pool": ENVIRONMENT,
    "Linked Service": CONNECTION, "Pipeline": PIPELINE, "Dataset": DATASET,
    "Spark Job Definition": SPARKJOB, "SQL Script": SCRIPT, "Trigger": SCHEDULE, "External Table": SHORTCUT,
    "Function": FUNCTION, "Security Object": SECURITY,
}
MIGRATABLE_TYPES = tuple(MIGRATABLE)
#: Within one wave, in the order things must exist: the warehouse, schemas and connections, tables,
#: the views and procedures that read them, their rows, then notebooks, jobs, pipelines and schedules.
_KIND_ORDER = {
    # Functions before the views and procedures that call them; roles and grants once the objects exist.
    POOL: -2, SCHEMA: -1, CONNECTION: -0.5, TABLE: 0, FUNCTION: 0.5, VIEW: 1, PROCEDURE: 2, SECURITY: 2.1, DATA: 2.2, DATASET: 2.3,
    ENVIRONMENT: 2.5, NOTEBOOK: 3, SCRIPT: 3.2, SPARKJOB: 3.5, PIPELINE: 4, SHORTCUT: 4.5, SCHEDULE: 5,
    MISSING: 6, DEFERRED: 7,
}
_FABRIC_KINDS = {NOTEBOOK, ENVIRONMENT, POOL, SCHEMA, TABLE, VIEW, PROCEDURE, DATA, CONNECTION, PIPELINE,
                 SPARKJOB, SCRIPT, SCHEDULE, SHORTCUT, FUNCTION, SECURITY}

_DEFERRED_REASONS = {
    "Pipeline": "Pipelines move in a later session: their datasets and linked services need Fabric connections first.",
    "Dataset": "Datasets move in a later session, as part of the pipelines that use them.",
    "Linked Service": "Linked services become Fabric connections in a later session; they need credentials set up in Fabric.",
    "Spark Job Definition": "Spark job definitions move in a later session.",
    "SQL Script": "SQL scripts move in a later session.",
    "External Table": "External tables move in a later session, as OneLake shortcuts.",
}

SCHEMA_EXISTS_QUERY = "SELECT SCHEMA_ID(?)"
#: Warehouse tables without an IDENTITY column.
WITHOUT_IDENTITY_QUERY = ("SELECT s.name, t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
                          "WHERE NOT EXISTS (SELECT 1 FROM sys.identity_columns ic WHERE ic.object_id = t.object_id)")

#: Warehouse collations: Fabric's default is case-sensitive; Synapse's default is case-insensitive.
CASE_SENSITIVE = "Latin1_General_100_BIN2_UTF8"
CASE_INSENSITIVE = "Latin1_General_100_CI_AS_KS_WS_SC_UTF8"
COLLATIONS = ("match_synapse", "case_insensitive", "case_sensitive")


def warehouse_collation(choice: str, synapse_collation: Optional[str]) -> str:
    """The collation to create the Warehouse with, from the run option and the pool's own collation."""
    if choice == "case_sensitive":
        return CASE_SENSITIVE
    if choice == "case_insensitive":
        return CASE_INSENSITIVE
    source = (synapse_collation or "").upper()
    sensitive = "_CS" in source or "_BIN" in source
    return CASE_SENSITIVE if sensitive else CASE_INSENSITIVE  # an unknown pool collation is Synapse's default, case-insensitive


def deferred_reason(object_type: str) -> str:
    return _DEFERRED_REASONS.get(object_type, f"{object_type} objects move in a later session.")


def warehouse_name_for(pool: Optional[str]) -> str:
    """A valid Fabric Warehouse name derived from the dedicated SQL pool's name."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", pool or "") or "SynapseMigration"
    return name if name[0].isalpha() else "wh_" + name


@dataclass
class Item:
    source: Source
    status: str = PENDING
    step: str = "Waiting"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    target: Optional[str] = None
    #: What kind of failure ``error`` is (``failures``): what may be done about it.
    category: Optional[str] = None
    #: How many passes have started on it.
    attempts: int = 0
    #: An earlier attempt in this run may already have made it in Fabric (the process stopped under it, or the
    #: answer to the request was lost): found there on the next try, it was made by this run, not left unchanged.
    maybe_created: bool = False
    #: What was sent to Fabric for it: {"hash", "kind", "bytes"}; the definition itself is kept in the project's store.
    artifact: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "id": self.source.id, "name": self.source.name, "type": self.source.type,
            "wave": self.source.wave, "step": self.step, "status": self.status,
            "startedAt": self.started_at, "completedAt": self.completed_at,
            "error": self.error, "notes": list(self.notes), "target": self.target,
            "category": self.category, "attempts": self.attempts, "maybeCreated": self.maybe_created,
            "artifact": dict(self.artifact) if self.artifact else None,
            # Created from an accepted LLM-assisted transform rather than the rule-based conversion.
            "llm": ({"acceptedAt": self.source.override.get("acceptedAt"), "edited": bool(self.source.override.get("edited"))}
                    if self.source.override else None),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: How often a pass retries an object by itself when Fabric failed for a moment (a 5xx, a timeout).
MAX_AUTO_RETRIES = 2
#: The kinds that are made, or found and left, by name: retrying one can never make it twice.
_RETRY_SAFE_KINDS = {NOTEBOOK, PIPELINE, SPARKJOB, SCRIPT, POOL, SCHEMA, TABLE, VIEW, PROCEDURE, FUNCTION}
_ITEM_FIELDS = {"status", "step", "started_at", "completed_at", "error", "notes", "target", "category", "attempts",
                "maybe_created", "artifact"}


def _backoff(attempt: int) -> float:
    return min(60.0, 10.0 * 2 ** (attempt - 1)) + random.uniform(0.0, 3.0)


class MigrationRun:
    """The state of one run, shared by the worker thread and HTTP requests.

    Every change of status goes through ``transition`` (the run) or ``update`` (one of its objects). Both check the
    move against ``run_state`` and write it to the run's record (``state.runs``) in the same step, so the record
    is complete by construction: nothing changes a status without leaving an event. A run made without a
    recorder (the tests that drive the runner directly) keeps everything in memory, as before."""

    def __init__(self, run_id: str, sources: List[Source], workspace_id: str, workspace_name: str, warehouse: str,
                 recorder: Any = None, needs: Optional[Dict[str, Any]] = None) -> None:
        self.lock = threading.RLock()
        self.run_id = run_id
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.warehouse = warehouse
        ordered = sorted(sources, key=lambda s: (s.wave, _KIND_ORDER.get(s.kind, 9)))
        self.items = [Item(s) for s in ordered]
        self.logs: List[str] = []
        self.status = run_state.QUEUED
        #: Why it is paused (``run_state.PAUSE_REASONS``), while it is.
        self.paused_reason: Optional[str] = None
        #: The connections whose credentials a resume needs, by name: they were lost with a restart.
        self.needs_input: List[str] = []
        self.pause = threading.Event()
        #: Run options: ``scope`` is what was asked for, ``stop_on_failure`` halts at a wave boundary.
        self.scope = "all"
        self.stop_on_failure = False
        self.halt_waived = False  # set when the operator resumes past a halt
        #: The waves a person has signed off, when the run has each wave signed off (setting ``waveGate``).
        self.released_waves: List[int] = []
        #: Strategy settings for the stages (data mode, data run, collation, sync re-read window). Never credentials.
        self.settings: Dict[str, Any] = {"dataMode": "if_empty", "dataRun": "run", "collation": "match_synapse", "syncOverlap": "1h",
                                         "gitCommit": "auto"}
        self.stages: List[str] = []
        self.pool_name: str = ""  # the Synapse pool being migrated, for retargeting pipelines
        #: The pool's SQL endpoint and the Fabric connection the data pipelines read it through.
        self.source_server: str = ""
        self.source_database: str = ""
        self.source_connection_name: str = ""
        self.source_collation: Optional[str] = None
        self.halted_reason: Optional[str] = None
        #: What the commit to the workspace's Fabric Git branch did once the run completed (None: not tried yet).
        self.git: Optional[Dict[str, Any]] = None
        #: Each pass over the run's pending objects: {n, trigger (start | resume | retry), status, startedAt, finishedAt, by}.
        self.passes: List[Dict[str, Any]] = []
        self.version = 0
        self.created_at = _now()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        #: When it stopped being the project's current run (Reset); its record stays, as history.
        self.closed_at: Optional[str] = None
        self.requested_by = context.actor().to_dict()
        #: The sign-off before it wrote to Fabric (``state.approvals``): required or not, and who decided.
        self.approval: Optional[Dict[str, Any]] = None
        #: What it was started from: the plan entries, the discovery snapshot, the planner's fingerprint, and the
        #: names (never the values) of the connections it was given credentials for.
        self.pins: Dict[str, Any] = {}
        #: Fabric executions it started and may need to follow again after a restart: {kind, id, path, wave, ...}.
        self.executions: List[Dict[str, Any]] = []
        self.resumable = True
        self.unresumable_reason: Optional[str] = None
        #: For each object, the objects in this run it needs: one of them failing blocks it for the pass.
        self.needs: Dict[str, Tuple[str, ...]] = {str(k): tuple(v) for k, v in (needs or {}).items()}
        self.recorder = recorder
        #: The objects that failed or were blocked, so what needs them is blocked too.
        self.bad: set = set()
        self._by_id = {i.source.id: i for i in self.items}
        for item in self.items:
            if item.source.kind == DEFERRED:
                item.status, item.step, item.error = DEFERRED_STATUS, "Not migrated in this session", item.source.reason

    @property
    def state(self) -> str:
        """The page's coarser vocabulary: running | paused | completed."""
        return run_state.legacy_state(self.status)

    def item(self, object_id: str) -> Optional[Item]:
        return self._by_id.get(object_id)

    def waiting_wave(self) -> Optional[int]:
        """The wave a run paused for a wave's sign-off waits to start, or None."""
        if self.status != run_state.PAUSED or self.paused_reason != run_state.WAVE_SIGNOFF:
            return None
        pending = [i.source.wave for i in self.items if i.status == PENDING]
        return min(pending) if pending else None

    # -- the only ways a status changes -------------------------------------------------------------

    def transition(self, target: str, reason: str = "", paused_reason: Optional[str] = None) -> None:
        with self.lock:
            run_state.check_run(self.status, target)
            previous, self.status = self.status, target
            self.paused_reason = paused_reason if target == run_state.PAUSED else None
            if target == run_state.RUNNING and self.started_at is None:
                self.started_at = _now()
            if target in (run_state.COMPLETED, run_state.COMPLETED_WITH_FAILURES, run_state.FAILED,
                          run_state.CANCELLED, run_state.REJECTED):
                self.finished_at = _now()
            elif target == run_state.QUEUED:
                self.finished_at = None
            self.version += 1
            self._event({"type": "status", "from": previous, "to": target, "reason": reason, "pausedReason": self.paused_reason})
        self.save()

    def update(self, item: Item, **fields: Any) -> None:
        unknown = set(fields) - _ITEM_FIELDS
        if unknown:
            raise TypeError(f"Not an object field: {', '.join(sorted(unknown))}")
        if isinstance(fields.get("error"), str):
            fields["error"] = redacted(fields["error"])
        if isinstance(fields.get("notes"), list):
            fields["notes"] = [redacted(n) if isinstance(n, str) else n for n in fields["notes"]]
        with self.lock:
            target = fields.get("status")
            if target is not None:
                run_state.check_item(item.status, target)
            for key, value in fields.items():
                setattr(item, key, value)
            if target in run_state.NEEDS_ATTENTION:
                self.bad.add(item.source.id)
            elif target is not None:
                self.bad.discard(item.source.id)
            self._event({"type": "item", "item": item.to_dict()})

    def log(self, level: str, name: str, message: str) -> None:
        message = redacted(message)  # a driver's or Fabric's message is kept, shown and written: never with a secret
        with self.lock:
            # Wide enough that the longest levels (COMPLETED, RECOVERED) keep a space before the name.
            self.logs.append(f"{_now()}  {level:<10}{name}: {message}")
            self._event({"type": "log", "level": level, "object": name, "message": message})
        _log.info("run %s %s %s: %s", self.run_id, level, name, message, extra={"runId": self.run_id, "event": level, "object": name})

    # -- passes -----------------------------------------------------------------------------------

    def begin_pass(self, trigger: str) -> None:
        """A pass starts: from a pause, a completion with failures or a failure it is queued again, then runs."""
        with self.lock:
            if self.status in (run_state.PAUSED, run_state.COMPLETED_WITH_FAILURES, run_state.FAILED):
                self.transition(run_state.QUEUED, f"{trigger} requested")
            if self.status == run_state.QUEUED:
                self.transition(run_state.RUNNING, trigger)
            self.passes.append({"n": len(self.passes) + 1, "trigger": trigger, "status": "running", "startedAt": _now(),
                                "finishedAt": None, "by": context.actor().to_dict()})
            self.version += 1
        self.save()

    def end_pass(self, outcome: str) -> None:
        with self.lock:
            if self.passes and self.passes[-1].get("status") == "running":
                self.passes[-1].update(status=outcome, finishedAt=_now())
            self.version += 1
        self.save()
        self.save_items()

    def interrupt(self) -> List[str]:
        """The process stopped under this run. What it was working on goes back in the queue, marked so the next
        pass knows it may already be in Fabric (it checks there first, so nothing is made twice); the pass ends as
        interrupted; and the run is paused as interrupted. Returns the names of the objects it was working on."""
        names: List[str] = []
        with self.lock:
            for item in self.items:
                if item.status == IN_PROGRESS:
                    self.update(item, status=PENDING, step="Interrupted: checked again when the run resumes",
                                category=failures.INTERRUPTED, maybe_created=True)
                    names.append(item.source.name)
            if self.passes and self.passes[-1].get("status") == "running":
                self.passes[-1].update(status="interrupted", finishedAt=_now())
            if self.status in run_state.WORKING:
                self.transition(run_state.PAUSED, "the accelerator stopped while the run was working", paused_reason=run_state.INTERRUPTED)
        self.save_items()
        return names

    def abort(self, reason: str) -> None:
        """The pass stopped on a fault of the accelerator's own. Like ``interrupt``, what it was working on goes back
        in the queue to be checked again; the run ends the pass as failed, so Retry carries it on."""
        with self.lock:
            for item in self.items:
                if item.status == IN_PROGRESS:
                    self.update(item, status=PENDING, step="Stopped: checked again on the next pass",
                                category=failures.INTERRUPTED, maybe_created=True)
            if self.passes and self.passes[-1].get("status") == "running":
                self.passes[-1].update(status="failed", finishedAt=_now())
            if self.status == run_state.RUNNING:
                self.transition(run_state.FAILED, reason)
            elif self.status == run_state.QUEUED:
                self.transition(run_state.PAUSED, reason, paused_reason=run_state.INTERRUPTED)
        self.log("ERROR", "run", reason)
        self.save_items()

    # -- what it started in Fabric ------------------------------------------------------------------------

    def record_execution(self, entry: Dict[str, Any]) -> str:
        """A job this run started in Fabric (a data pipeline's run), kept so that, after a restart, the run follows
        it to its end instead of starting another beside it. Returns its id."""
        with self.lock:
            ident = f"E{len(self.executions) + 1}"
            self.executions.append({**entry, "id": ident, "status": "running", "startedAt": _now(), "finishedAt": None,
                                    "pass": len(self.passes)})
            self.version += 1
            self._event({"type": "execution", "execution": dict(self.executions[-1])})
        self.save()
        return ident

    def finish_execution(self, ident: str, status: str, note: Optional[str] = None) -> None:
        with self.lock:
            for execution in self.executions:
                if execution.get("id") == ident:
                    execution.update(status=status, finishedAt=_now(), **({"note": note} if note else {}))
                    self._event({"type": "execution", "execution": dict(execution)})
            self.version += 1
        self.save()

    # -- the record ---------------------------------------------------------------------------------

    def _event(self, record: Dict[str, Any]) -> None:
        if self.recorder is None:
            return
        try:
            self.recorder.event(self.run_id, {**record, "at": _now(), "pass": len(self.passes), **context_fields()})
        except OSError as exc:  # the run carries on; its record has a gap the log names
            _log.warning("Could not record an event of run %s: %s", self.run_id, exc)

    def save(self) -> None:
        if self.recorder is None:
            return
        # Read and written under the lock, so two threads saving (a pass and the commit after it) can never leave
        # an older record written last.
        with self.lock:
            try:
                self.recorder.save_run(self.document())
            except OSError as exc:
                _log.warning("Could not save run %s: %s", self.run_id, exc)

    def save_items(self) -> None:
        if self.recorder is None:
            return
        with self.lock:
            try:
                self.recorder.save_items(self.run_id, [i.to_dict() for i in self.items], self.recorder.last_seq(self.run_id))
            except OSError as exc:
                _log.warning("Could not save the objects of run %s: %s", self.run_id, exc)

    def counts(self) -> Dict[str, int]:
        counts = {s: 0 for s in (PENDING, IN_PROGRESS, COMPLETED, FAILED, SKIPPED, DEFERRED_STATUS, run_state.BLOCKED)}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def document(self) -> dict:
        """The run's own record, without its objects (they are in its events and items snapshot)."""
        with self.lock:
            return {
                "runId": self.run_id, "status": self.status, "pausedReason": self.paused_reason, "needsInput": list(self.needs_input),
                "workspaceId": self.workspace_id, "workspace": self.workspace_name, "warehouse": self.warehouse,
                "scope": self.scope, "stopOnFailure": self.stop_on_failure, "haltWaived": self.halt_waived,
                "releasedWaves": list(self.released_waves),
                "settings": dict(self.settings), "stages": list(self.stages), "poolName": self.pool_name,
                "sourceServer": self.source_server, "sourceDatabase": self.source_database,
                "sourceConnectionName": self.source_connection_name, "sourceCollation": self.source_collation,
                "haltedReason": self.halted_reason, "git": dict(self.git) if self.git else None,
                "passes": [dict(p) for p in self.passes], "version": self.version, "createdAt": self.created_at,
                "startedAt": self.started_at, "finishedAt": self.finished_at, "closedAt": self.closed_at,
                "requestedBy": dict(self.requested_by), "approval": dict(self.approval) if self.approval else None,
                "pins": dict(self.pins), "executions": [dict(e) for e in self.executions],
                "needs": {k: list(v) for k, v in self.needs.items()}, "counts": self.counts(),
            }

    @classmethod
    def restore(cls, document: dict, items: List[dict], sources: Optional[List[Source]], recorder: Any) -> "MigrationRun":
        """A run as its record left it. Given its sources (rebuilt from the project's discovery) it can carry on;
        without them it is shown as it stood, and cannot be resumed."""
        resumable = sources is not None
        if sources is None:
            sources = [Source(str(i.get("id")), str(i.get("name") or i.get("id")), str(i.get("type") or ""),
                              int(i.get("wave") or 1), MISSING) for i in items]
        run = cls(str(document["runId"]), sources, str(document.get("workspaceId") or ""), str(document.get("workspace") or ""),
                  str(document.get("warehouse") or ""), recorder=recorder, needs=document.get("needs") or {})
        run.status = str(document.get("status") or run_state.PAUSED)
        run.paused_reason = document.get("pausedReason")
        run.needs_input = list(document.get("needsInput") or [])
        run.scope, run.stop_on_failure = str(document.get("scope") or "all"), bool(document.get("stopOnFailure"))
        run.halt_waived = bool(document.get("haltWaived"))
        run.released_waves = [int(w) for w in document.get("releasedWaves") or []]
        run.settings = {**run.settings, **(document.get("settings") or {})}
        run.stages = list(document.get("stages") or [])
        run.pool_name = str(document.get("poolName") or "")
        run.source_server, run.source_database = str(document.get("sourceServer") or ""), str(document.get("sourceDatabase") or "")
        run.source_connection_name = str(document.get("sourceConnectionName") or "")
        run.source_collation = document.get("sourceCollation")
        run.halted_reason, run.git = document.get("haltedReason"), document.get("git")
        run.passes = [dict(p) for p in document.get("passes") or []]
        run.version = int(document.get("version") or 0)
        run.created_at = document.get("createdAt") or run.created_at
        run.started_at, run.finished_at, run.closed_at = document.get("startedAt"), document.get("finishedAt"), document.get("closedAt")
        run.requested_by = dict(document.get("requestedBy") or run.requested_by)
        run.approval = document.get("approval")
        run.pins = dict(document.get("pins") or {})
        run.executions = [dict(e) for e in document.get("executions") or []]
        saved = {str(i.get("id")): i for i in items}
        for item in run.items:
            was = saved.get(item.source.id)
            if was is None:
                continue
            item.status = str(was.get("status") or item.status)
            item.step = str(was.get("step") or item.step)
            item.started_at, item.completed_at = was.get("startedAt"), was.get("completedAt")
            item.error, item.notes, item.target = was.get("error"), list(was.get("notes") or []), was.get("target")
            item.category, item.attempts = was.get("category"), int(was.get("attempts") or 0)
            item.maybe_created, item.artifact = bool(was.get("maybeCreated")), was.get("artifact")
        run.bad = {i.source.id for i in run.items if i.status in run_state.NEEDS_ATTENTION}
        if not resumable:
            run.resumable = False
            run.unresumable_reason = ("The discovery this run was made from is no longer here, so it cannot carry on. "
                                      "Its record is kept; run discovery again and start a new run.")
        return run

    def to_dict(self) -> dict:
        with self.lock:
            counts = self.counts()
            return {
                "runId": self.run_id, "state": self.state, "status": self.status, "pausedReason": self.paused_reason,
                "needsInput": list(self.needs_input), "total": len(self.items),
                "completed": counts[COMPLETED], "inProgress": counts[IN_PROGRESS], "failed": counts[FAILED],
                "pending": counts[PENDING], "skipped": counts[SKIPPED], "deferred": counts[DEFERRED_STATUS],
                "blocked": counts[run_state.BLOCKED],
                "workspace": self.workspace_name, "warehouse": self.warehouse,
                "options": {"scope": self.scope, "stopOnFailure": self.stop_on_failure, "stages": list(self.stages), **self.settings},
                "haltedReason": self.halted_reason, "releasedWaves": list(self.released_waves),
                "waitingWave": self.waiting_wave(),
                # The migration brief it started under, and how it differs from it (``pins.brief``).
                "brief": dict(self.pins["brief"]) if isinstance(self.pins.get("brief"), dict) else None,
                "git": dict(self.git) if self.git else None,
                "passes": [dict(p) for p in self.passes], "version": self.version,
                "createdAt": self.created_at, "startedAt": self.started_at, "finishedAt": self.finished_at,
                "closedAt": self.closed_at, "requestedBy": dict(self.requested_by),
                "approval": dict(self.approval) if self.approval else None,
                "resumable": self.resumable, "unresumableReason": self.unresumable_reason,
                "executions": [dict(e) for e in self.executions],
                "items": [i.to_dict() for i in self.items], "logs": list(self.logs[-2000:]),
            }


#: The Fabric requests whose bodies are item definitions, kept as the record of what was deployed. Never a
#: connection's: its body carries credentials.
_DEFINITION_PATHS = re.compile(r"/(notebooks|dataPipelines|sparkJobDefinitions|environments|lakehouses|warehouses|shortcuts)"
                               r"(/|$)|/updateDefinition$|/items$")
_SECRET_KEYS = re.compile(r"credential|password|secret|token|accountkey|sharedaccess", re.IGNORECASE)


def _carries_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_SECRET_KEYS.search(str(k)) or _carries_secret(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_carries_secret(v) for v in value)
    return False


class _Recording:
    """The Fabric client a pass uses when the run is recorded: each item definition it sends, once Fabric has taken
    it, is also kept (``Migrator._keep``). Everything else passes straight through."""

    def __init__(self, client: Any, keep: Callable[[str, str, Any], None]) -> None:
        self._client, self._keep = client, keep

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _note(self, method: str, path: str, body: Any) -> None:
        bare = str(path).split("?", 1)[0]
        if body is None or "/connections" in bare or not _DEFINITION_PATHS.search(bare) or _carries_secret(body):
            return
        self._keep("fabric", f"{method} {bare}", body)

    def create(self, path: str, body: dict, *args: Any, **kwargs: Any) -> dict:
        out = self._client.create(path, body, *args, **kwargs)
        self._note("POST", path, body)
        return out

    def patch(self, path: str, body: dict) -> dict:
        out = self._client.patch(path, body)
        self._note("PATCH", path, body)
        return out

    def request(self, method: str, path_or_url: str, body: Optional[dict] = None) -> Any:
        out = self._client.request(method, path_or_url, body)
        if method != "GET":
            self._note(method, path_or_url, body)
        return out


class Migrator(StageMixin, FabricStageMixin):
    """One pass over a run's PENDING items. A retry is a new pass."""

    def __init__(
        self,
        run: MigrationRun,
        rest_factory: Callable[[], FabricRestClient],
        sql_factory: Callable[[str, str], Any],
        sleep: Callable[[float], None] = lambda s: None,
        source_factory: Optional[Callable[[], Any]] = None,
        credentials: Optional[Dict[str, Dict[str, str]]] = None,
        onelake: Any = None,
        trigger: str = "start",
        artifacts: Any = None,
    ) -> None:
        self.run = run
        #: Files in the Lakehouse (``onelake.OneLakeFiles``): copies of Spark job main files with rerouted storage paths.
        self._onelake = onelake
        self._source_factory = source_factory
        self._source: Any = None
        self._item: Optional["Item"] = None
        #: Secrets for connections, by linked-service name. In memory for the run's life; never logged or returned.
        self._credentials = credentials or {}
        self._rest_factory = rest_factory
        self._sql_factory = sql_factory
        self._sleep = sleep
        self._rest: Optional[FabricRestClient] = None
        self._notebook_names: Optional[set] = None
        self._sql: Any = None
        self._sql_error: Optional[str] = None
        self._warehouse_created = False
        self._collation: Optional[str] = None
        #: A workspace-level failure; every later Fabric object fails with it at once.
        self._fatal: Optional[str] = None
        #: Log references of the unexpected errors this pass has written down, by (type, message).
        self._unexpected: Dict[Tuple[str, str], str] = {}
        #: Why this pass runs (start | resume | retry), and where it keeps what it sends to Fabric (a ``state.store.Store``).
        self._trigger = trigger
        self._artifacts = artifacts
        #: How the pass ended, (run status, pause reason), as it set it: read after ``execute`` without racing a
        #: Resume that may already have started the next pass.
        self.outcome: Optional[Tuple[str, Optional[str]]] = None
        #: What this pass sent to Fabric for each object, as kept: {object id: [{kind, what, hash, bytes}]}.
        self._sent: Dict[str, List[Dict[str, Any]]] = {}

    def _reference(self, source: Source, exc: Exception) -> str:
        """The log reference for an unexpected error. Each distinct error is written once per pass, so a fault
        that hits every object leaves one traceback in the log, not thousands."""
        key = (type(exc).__name__, str(exc)[:300])
        if key not in self._unexpected:
            self._unexpected[key] = record_unexpected(f"migration run {self.run.run_id}: {source.name}", exc)
        return self._unexpected[key]

    # -- the loop ----------------------------------------------------------

    def execute(self) -> None:
        self.run.begin_pass(self._trigger)
        try:
            self._pass()
        except Exception as exc:  # noqa: BLE001 - a fault in the runner itself ends the pass; it never leaves the run "running"
            reference = record_unexpected(f"migration run {self.run.run_id}", exc)
            self.run.abort(f"The pass stopped on an unexpected {type(exc).__name__} (reference {reference}). "
                           "The details are in the API's log; Retry failed carries the run on.")
            with self.run.lock:
                self.outcome = (self.run.status, self.run.paused_reason)
        finally:
            self._close()

    def _end(self, outcome: str, pass_status: str, reason: str, paused_reason: Optional[str] = None) -> None:
        self.outcome = (outcome, paused_reason)
        self.run.end_pass(pass_status)
        self.run.transition(outcome, reason, paused_reason=paused_reason)

    def _pass(self) -> None:
        run = self.run
        for item in run.items:
            if item.status != PENDING:
                continue
            if run.pause.is_set():
                self._end(run_state.PAUSED, "paused", "pause requested", run_state.BY_PERSON)
                run.log("PAUSED", "run", "paused before the next object")
                return
            reason = self._halt_reason(item)
            if reason:
                with run.lock:
                    run.halted_reason = reason
                self._end(run_state.PAUSED, "halted", reason, run_state.HALTED)
                run.log("HALTED", "run", reason)
                return
            gate = self._wave_gate(item)
            if gate:
                self._end(run_state.PAUSED, "awaiting wave sign-off", gate, run_state.WAVE_SIGNOFF)
                run.log("SIGN-OFF", "run", gate)
                return
            blocker = self._blocker(item)
            if blocker:
                run.update(item, status=run_state.BLOCKED, step="Not started: something it needs failed", error=blocker,
                           completed_at=_now(), category=failures.DEPENDENCY)
                run.log("BLOCKED", item.source.name, blocker)
                continue
            self._one(item)
        self._leftovers()
        with run.lock:
            outcome = (run_state.FAILED if self._fatal else
                       run_state.COMPLETED_WITH_FAILURES if run.bad else run_state.COMPLETED)
        self._end(outcome, "finished", self._fatal or "every pending object was processed")
        run.log("DONE", "run", "every pending object was processed")

    def _leftovers(self) -> None:
        """Nothing is left "in progress" when a pass ends: an object a stage started but never finished (a fault
        the stage did not expect) fails, with that said, so Retry failed runs it again."""
        run = self.run
        for item in [i for i in run.items if i.status == IN_PROGRESS]:
            run.update(item, status=FAILED, step="Failed", completed_at=_now(), category=failures.UNKNOWN,
                       error=item.error or "The pass ended before it finished this object. Retry failed runs it again.")
            run.log("FAILED", item.source.name, "the pass ended before it finished this object")

    def _halt_reason(self, item: Item) -> Optional[str]:
        """Stop-on-failure: do not start a wave while an earlier one has failures."""
        run = self.run
        if not run.stop_on_failure or run.halt_waived:
            return None
        wave = item.source.wave
        failed = sorted({i.source.wave for i in run.items if i.status in run_state.NEEDS_ATTENTION and i.source.wave < wave})
        if not failed:
            return None
        return f"Stopped before wave {wave}: wave {failed[0]} has failed objects. Fix them and Retry Failed, or Resume to continue anyway."

    def _wave_gate(self, item: Item) -> Optional[str]:
        """A wave after the run's first waits for a person to sign it off, when the brief says so (``waveGate``)."""
        run = self.run
        if not run.settings.get("waveGate"):
            return None
        wave = item.source.wave
        first = min((i.source.wave for i in run.items), default=wave)
        if wave <= first or wave in run.released_waves:
            return None
        return (f"Wave {wave} waits for a person to sign it off: check what the waves before it made in Fabric, then "
                f"Resume to start wave {wave}.")

    def _blocker(self, item: Item) -> Optional[str]:
        """Why ``item`` cannot start in this pass: something in this run it needs failed or was blocked itself."""
        run = self.run
        needed = [n for n in run.needs.get(item.source.id, ()) if n in run.bad]
        if not needed:
            return None
        names = sorted(run.item(n).source.name for n in needed if run.item(n) is not None)
        shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        return (f"It needs {shown}, which did not complete in this run, so it was not started. "
                "Retry failed runs it again once that is fixed.")

    def _one(self, item: Item) -> None:
        run, source = self.run, item.source
        run.update(item, status=IN_PROGRESS, started_at=_now(), completed_at=None, error=None, notes=[], category=None,
                   step="Creating in Fabric", attempts=item.attempts + 1)
        self._item = item
        run.log("START", source.name, f"{source.type} ({source.kind})")
        retries = 0
        while True:
            try:
                if self._fatal and source.kind in _FABRIC_KINDS:
                    raise MigrationError(self._fatal)
                handler = {
                    NOTEBOOK: self._notebook, POOL: self._pool, SCHEMA: self._schema,
                    TABLE: self._table, VIEW: self._module, PROCEDURE: self._module, FUNCTION: self._module,
                    SECURITY: self._security, ENVIRONMENT: self._environment,
                    DATA: self._data, CONNECTION: self._connection, DATASET: self._dataset, PIPELINE: self._pipeline,
                    SPARKJOB: self._sparkjob, SCRIPT: self._script, SCHEDULE: self._schedule, SHORTCUT: self._shortcut,
                }.get(source.kind)
                if handler is None:
                    raise MigrationError(source.reason or "This object is not in the current discovery results. Run discovery again.")
                status, step, target, notes = handler(source)
                break
            except (MigrationError, FabricApiError) as exc:
                message = exc.message if isinstance(exc, FabricApiError) else str(exc)
                cause = exc if isinstance(exc, FabricApiError) else exc.__cause__
                api = cause if isinstance(cause, FabricApiError) else None
                if api is not None and api.code in WORKSPACE_ERRORS:
                    self._fatal = message
                category = failures.CAPACITY if self._fatal and message == self._fatal else \
                    failures.classify(message, api.code if api else None, api.status if api else None)
                if (category in failures.RETRYABLE and api is not None and retries < MAX_AUTO_RETRIES
                        and source.kind in _RETRY_SAFE_KINDS and not self._sql_error):
                    retries += 1
                    delay = _backoff(retries)
                    run.update(item, maybe_created=True)  # the attempt that failed may have made it before its answer was lost
                    self._forget_listings()
                    run.log("RETRY", source.name, f"{message} Trying again in {delay:.0f} s ({retries} of {MAX_AUTO_RETRIES}).")
                    self._sleep(delay)
                    continue
                self._fail(item, message, category)
                return
            except Exception as exc:  # noqa: BLE001 - one object never stops the run; the API's log gets the traceback
                self._fail(item, f"Unexpected {type(exc).__name__}: {redacted(str(exc)[:300])} (reference {self._reference(source, exc)})",
                           failures.UNKNOWN)
                return
        if status == SKIPPED and item.maybe_created:
            status, notes = COMPLETED, ["Found in Fabric on this try: an earlier attempt in this run most likely made it (the "
                                        "accelerator stopped under it, or the answer to the request was lost).", *notes]
        run.update(item, status=status, step=step, target=target, notes=notes, completed_at=_now(), maybe_created=False,
                   artifact=self._artifact_of(item))
        run.log(status, source.name, step + (f" -> {target}" if target else ""))
        for note in notes:
            run.log("NOTE", source.name, note)

    def _fail(self, item: Item, message: str, category: str) -> None:
        # ``maybe_created`` is kept: an earlier attempt whose answer was lost may still have made it, and the next pass
        # that finds it should say so.
        self.run.update(item, status=FAILED, step="Failed", error=message, completed_at=_now(), category=category,
                        artifact=self._artifact_of(item))
        self.run.log("FAILED", item.source.name, message)

    # -- notebooks ---------------------------------------------------------

    def _forget_listings(self) -> None:
        """Drop what this pass read of Fabric's item lists, so the next attempt reads them again."""
        self._notebook_names = None
        for cache in ("_id_cache", "_conn_cache"):
            self.__dict__.pop(cache, None)

    def _client(self) -> FabricRestClient:
        if self._rest is None:
            client = self._rest_factory()
            self._rest = _Recording(client, self._keep) if self._artifacts is not None else client
        return self._rest

    def _keep(self, kind: str, what: str, content: Any) -> None:
        """Keep what Fabric took for the current object (a definition, a statement) in the project's store, by its
        hash: the record of exactly what was deployed. A failure to keep it never fails the object."""
        if self._artifacts is None or self._item is None:
            return
        try:
            data = content.encode("utf-8") if isinstance(content, str) else \
                json.dumps(content, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            digest = self._artifacts.put_blob(data)
        except Exception as exc:  # noqa: BLE001 - the record of what was sent is a convenience; the object is what matters
            _log.warning("Could not keep the %s sent for %s: %s", what, self._item.source.name, exc)
            return
        self._sent.setdefault(self._item.source.id, []).append({"kind": kind, "what": what, "hash": digest, "bytes": len(data)})

    def _artifact_of(self, item: Item) -> Optional[Dict[str, Any]]:
        parts = self._sent.pop(item.source.id, None)
        return {"parts": parts} if parts else item.artifact

    def _notebook(self, source: Source):
        if not isinstance(source.payload, dict) or not source.payload:
            raise MigrationError("The notebook's definition was not kept by discovery. Run discovery again.")
        try:
            payload = notebooks.with_cells(source.payload, source.override["code"]) if source.override else source.payload
            document, notes = notebooks.to_fabric_ipynb(payload, pool_name=self.run.pool_name, warehouse=self.run.warehouse)
        except notebooks.NotebookNotMigratable as exc:
            if source.override:
                raise MigrationError(str(exc)) from exc
            return DEFERRED_STATUS, "Not migrated: needs a rewrite", None, [str(exc)]
        if source.override:
            notes.insert(0, override_note(source))
        remapped: List[str] = []
        for cell in document.get("cells") or []:
            if cell.get("cell_type") != "code":
                continue
            text = "".join(cell.get("source") or [])
            routed, cell_notes = self._remap(text)
            if routed != text:
                cell["source"] = routed.splitlines(keepends=True)
            remapped.extend(n for n in cell_notes if n not in remapped)
        notes.extend(remapped)
        client = self._client()
        wid = self.run.workspace_id
        if self._notebook_names is None:
            self._notebook_names = {str(n.get("displayName", "")).lower() for n in client.list(f"/workspaces/{wid}/notebooks")}
        target = f"{self.run.workspace_name} / {source.name}"
        description = str((source.payload.get("properties") or {}).get("description") or "Migrated from Azure Synapse")
        if source.name.lower() in self._notebook_names:
            if not self._updating():
                return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
            item_id = self._find(self._ids("notebooks"), source.name)
            if not item_id:
                raise MigrationError("The notebook is in the workspace but its id could not be found to update it. Retry.")
            self._update_definition(item_id, notebooks.create_body(source.name, document, description))
            return COMPLETED, "Updated in Fabric (definition replaced)", target, notes
        try:
            self._client().create(f"/workspaces/{wid}/notebooks", notebooks.create_body(source.name, document, description))
        except FabricApiError as exc:
            if exc.code == "ItemDisplayNameAlreadyInUse":
                self._notebook_names.add(source.name.lower())
                return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
            raise
        self._notebook_names.add(source.name.lower())
        return COMPLETED, "Created as a Fabric notebook", target, notes

    # -- spark pool -> custom pool + environment ---------------------------

    def _environment(self, source: Source):
        client, wid = self._client(), self.run.workspace_id
        target = f"{self.run.workspace_name} / {source.name}"
        existing = {str(e.get("displayName", "")).lower() for e in client.list(f"/workspaces/{wid}/environments")}
        if source.name.lower() in existing:
            return SKIPPED, "Environment already in the Fabric workspace; left unchanged", target, []
        wanted = environments.plan(source.payload if isinstance(source.payload, dict) else {}, source.name)
        notes = list(wanted.notes)
        compute = dict(wanted.compute)

        pool_names = {str(p.get("name", "")).lower() for p in client.list(f"/workspaces/{wid}/spark/pools")}
        try:
            if source.name.lower() not in pool_names:
                client.create(f"/workspaces/{wid}/spark/pools", wanted.pool)
                scale = wanted.pool["autoScale"]
                notes.append(f"Created Fabric Spark pool '{source.name}' ({wanted.pool['nodeSize']}, "
                             f"{scale['minNodeCount']}-{scale['maxNodeCount']} nodes).")
            compute["instancePool"] = {"name": source.name, "type": "Workspace"}
        except FabricApiError as exc:
            if exc.code in WORKSPACE_ERRORS:
                raise
            notes.append(f"A custom Spark pool could not be created ({exc.message[:160]}); the Environment uses the Starter Pool.")
            compute = environments.starter_compute(wanted.compute)
            compute["instancePool"] = dict(environments.STARTER_POOL)

        created = client.create(f"/workspaces/{wid}/environments", {
            "displayName": source.name, "description": "Migrated from an Azure Synapse Spark pool",
        })
        env_id = str(created.get("id") or "")
        if not env_id:
            for env in client.list(f"/workspaces/{wid}/environments"):
                if str(env.get("displayName", "")).lower() == source.name.lower():
                    env_id = str(env.get("id") or "")
        if not env_id:
            raise MigrationError("The Environment was created but Fabric did not return its id. Check the workspace.")
        base = f"/workspaces/{wid}/environments/{env_id}/staging"
        try:
            client.patch(f"{base}/sparkcompute", compute)
            client.create(f"{base}/publish", {})
        except FabricApiError as exc:
            raise MigrationError(f"The Environment was created but its compute settings were not applied or published: {exc.message} Finish it in Fabric.") from exc
        return COMPLETED, "Created as a Fabric Environment and published", target, notes

    # -- warehouse ---------------------------------------------------------

    def _warehouse(self) -> Any:
        if self._sql is not None:
            return self._sql
        if self._sql_error:
            raise MigrationError(self._sql_error)
        try:
            self._sql = self._open_warehouse()
        except MigrationError as exc:
            self._sql_error = str(exc)
            raise
        except FabricApiError as exc:
            self._sql_error = f"The warehouse could not be prepared: {exc.message}"
            raise MigrationError(self._sql_error) from exc
        except Exception as exc:  # noqa: BLE001 - driver, token or network failure
            self._sql_error = f"Could not connect to warehouse {self.run.warehouse}: {type(exc).__name__}: {str(exc)[:300]}"
            raise MigrationError(self._sql_error) from exc
        return self._sql

    def _find_warehouse(self) -> Optional[dict]:
        name = self.run.warehouse.lower()
        for warehouse in self._client().list(f"/workspaces/{self.run.workspace_id}/warehouses"):
            if str(warehouse.get("displayName", "")).lower() == name:
                return warehouse
        return None

    def _open_warehouse(self) -> Any:
        run, client = self.run, self._client()
        warehouse = self._find_warehouse()
        if warehouse is None:
            collation = warehouse_collation(str(run.settings.get("collation") or "match_synapse"), run.source_collation)
            run.log("CREATE", run.warehouse, f"creating the Fabric Warehouse with collation {collation}")
            client.create(f"/workspaces/{run.workspace_id}/warehouses", {
                "displayName": run.warehouse, "description": "Migrated from an Azure Synapse dedicated SQL pool",
                "creationPayload": {"defaultCollation": collation},
            })
            self._warehouse_created = True
            self._collation = collation
        host = ""
        for _ in range(20):  # a new warehouse takes a moment to publish its SQL endpoint
            warehouse = warehouse or self._find_warehouse()
            if warehouse is not None:
                detail = client.get(f"/workspaces/{run.workspace_id}/warehouses/{warehouse['id']}")
                host = str((detail.get("properties") or {}).get("connectionString") or "")
                if host:
                    break
            self._sleep(3)
        if not host:
            raise MigrationError(f"Warehouse {run.warehouse} has no SQL endpoint yet. Wait a minute, then Retry Failed.")
        host = host.replace("tcp:", "").split(",")[0].strip()
        run.log("CONNECT", run.warehouse, f"SQL endpoint {host}")
        return self._sql_factory(host, run.warehouse)

    def _exists(self, cursor: Any, schema: str, name: str) -> bool:
        cursor.execute(warehouse_ddl.EXISTS_QUERY, warehouse_ddl.qualified(schema, name))
        row = cursor.fetchone()
        return bool(row) and row[0] is not None

    def _prepare(self, source: Source):
        schema = source.schema or "dbo"
        name = source.object_name or source.name
        cursor = self._warehouse().cursor()
        target = f"{self.run.warehouse}.{schema}.{name}"
        return cursor, schema, name, target

    def _pool(self, source: Source):
        self._warehouse()
        notes: List[str] = []
        if self._warehouse_created:
            step = "Fabric Warehouse created"
            sensitive = self._collation == CASE_SENSITIVE
            notes.append(f"Collation {self._collation} ({'case-sensitive' if sensitive else 'case-insensitive'}"
                         f"{', like the Synapse pool' if self.run.settings.get('collation', 'match_synapse') == 'match_synapse' else ''}).")
        else:
            step = "Fabric Warehouse already existed; reused"
            notes.append("An existing Warehouse keeps the collation it was created with; it cannot be changed afterwards.")
        return COMPLETED, step, f"{self.run.workspace_name} / {self.run.warehouse}", notes

    def _schema(self, source: Source):
        schema = source.schema or source.name
        cursor = self._warehouse().cursor()
        target = f"{self.run.warehouse}.{schema}"
        cursor.execute(SCHEMA_EXISTS_QUERY, schema)
        row = cursor.fetchone()
        if row and row[0] is not None:
            return SKIPPED, "Schema already in the warehouse", target, []
        self._run_sql(cursor, warehouse_ddl.ensure_schema(schema), "create schema " + schema)
        return COMPLETED, "Schema created", target, []

    def _table(self, source: Source):
        cursor, schema, name, target = self._prepare(source)
        if self._exists(cursor, schema, name):
            added = self._add_missing_masks(cursor, source)
            if added:
                return COMPLETED, "Already in the warehouse; dynamic data masks added", target, added
            return SKIPPED, "Already in the warehouse; left unchanged", target, []
        sql, notes = self._ddl(source)
        self._run_sql(cursor, warehouse_ddl.ensure_schema(schema), "create schema " + schema)
        self._run_sql(cursor, sql, "create table")
        for statement in warehouse_ddl.key_constraints(source.payload):
            try:
                cursor.execute(statement)
                notes.append("Key kept as NOT ENFORCED: " + statement.split(" ADD CONSTRAINT ", 1)[1].rstrip(";"))
            except Exception as exc:  # noqa: BLE001 - a key is a hint; the table stands without it
                notes.append(f"Key not recreated ({str(exc)[:160]}): {statement}")
        return COMPLETED, "Table created (empty: the Table data stage loads it with a pipeline)", target, notes

    @staticmethod
    def _ddl(source: Source):
        if source.override:
            notes = [override_note(source)]
            if source.masks:
                notes.append("Dynamic data masks are not added to a reviewed CREATE TABLE: check they are in it.")
            return source.override["code"], notes
        clauses, unsupported = [], []
        for mask in source.masks:
            clause = security.mask_clause(mask)
            if clause:
                clauses.append((mask.column, clause))
            else:
                unsupported.append(mask)
        if unsupported:
            names = ", ".join(f"{m.column} ({m.function})" for m in unsupported)
            raise MigrationError(f"Masking function(s) Fabric does not have: {names}. The table is not created, so its rows "
                                 "are never loaded unmasked. Change the mask in Synapse or create the table by hand.")
        try:
            return warehouse_ddl.create_table(source.payload, clauses)
        except warehouse_ddl.UnsupportedColumn as exc:
            raise MigrationError(str(exc)) from exc

    def _add_missing_masks(self, cursor: Any, source: Source) -> List[str]:
        """An existing table gets the dynamic data masks it lacks (a mask only hides; adding one never exposes)."""
        if not source.masks:
            return []
        try:
            cursor.execute(security.MASKS_SQL)
            present = {(str(r[0]).lower(), str(r[1]).lower(), str(r[2]).lower()) for r in cursor.fetchall()}
        except Exception as exc:  # noqa: BLE001 - masks cannot be compared; say so rather than guess
            raise MigrationError(f"The table exists, but its dynamic data masks could not be read to check them: {str(exc)[:200]}") from exc
        notes = []
        for mask in source.masks:
            if (mask.schema.lower(), mask.table.lower(), mask.column.lower()) in present:
                continue
            statement = security.add_mask_statement(mask)
            if statement is None:
                raise MigrationError(f"Column {mask.column} has masking function {mask.function}, which Fabric does not have. Mask it by hand.")
            self._run_sql(cursor, statement, f"dynamic data mask on {mask.column}")
            notes.append(f"{mask.column}: dynamic data mask added ({mask.function})")
        return notes

    # -- roles, members and permissions ---------------------------------------

    def _security(self, source: Source):
        # A SQL user a Microsoft Entra principal replaces is planned as that principal (``object_name``).
        plan = security.plan_principal(source.payload, source.object_name or source.name)
        principal = plan.principal
        target = f"{self.run.warehouse} / {principal.kind_label.lower()} {principal.name}"
        if source.object_name and source.object_name != source.name:
            target += f" (replaces SQL user {source.name})"
        if principal.is_sql_user:
            # A plan never sends one (sources_for defers SQL users), but a hand-built run must not grant to it.
            raise MigrationError("A SQL user cannot be recreated in Fabric. Replace it with a Microsoft Entra principal.")
        cursor = self._warehouse().cursor()
        notes = list(plan.notes)
        created = False
        if plan.create_role:
            cursor.execute(security.ROLE_EXISTS_QUERY, principal.name)
            found = cursor.fetchone()
            if not (found and found[0] is not None):
                self._run_sql(cursor, plan.create_role, f"role {principal.name}")
                created = True
        rejected: List[str] = []
        for _, statement in plan.members + plan.permissions:
            try:
                cursor.execute(statement)
            except Exception as exc:  # noqa: BLE001 - collect every rejection, like the Migration Assistant's "Fix problems" list
                rejected.append(f"{statement} -> {str(exc)[:160]}")
        total = len(plan.members) + len(plan.permissions)
        if rejected:
            raise MigrationError(f"Fabric rejected {len(rejected)} of {total} statement(s); the others are applied and Retry Failed "
                                 "re-applies them all safely: " + " | ".join(rejected[:5]))
        if principal.is_role:
            step = ("Role created" if created else "Role already in the warehouse") + \
                f"; {len(plan.members)} member(s) and {len(plan.permissions)} permission(s) applied"
        elif plan.permissions:
            step = f"{len(plan.permissions)} permission(s) applied (Fabric creates the user on its first GRANT or DENY)"
        else:
            step = "Nothing granted directly: this principal's access comes from its role memberships"
        if total:
            notes.append("Workspace roles and item permissions still decide who can connect: give these principals "
                         "Read on the Warehouse in Fabric.")
        return COMPLETED, step, target, notes

    def _module(self, source: Source):
        definition = getattr(source.payload, "definition", None)
        text = getattr(definition, "text", None)
        if not text or not getattr(definition, "is_readable", False):
            raise MigrationError("The definition could not be read from the pool (encrypted, or VIEW DEFINITION is not granted), so it cannot be recreated.")
        cursor, schema, name, target = self._prepare(source)
        exists = self._exists(cursor, schema, name)
        if exists and not self._updating():
            return SKIPPED, "Already in the warehouse; left unchanged", target, []
        # An accepted transform still goes through the rules: they are safe to repeat and catch a slip.
        converted = tsql_rules.rewrite(source.override["code"] if source.override else text.strip(), self._tables_without_identity(cursor))
        routed, remap_notes = self._remap(converted.text)
        converted.notes.extend(remap_notes)
        statement = routed
        if exists:
            statement = tsql_rules.create_or_alter(routed)
            if statement is None:
                return SKIPPED, "Already in the warehouse; not updated (its definition has no CREATE statement)", target, []
        self._run_sql(cursor, warehouse_ddl.ensure_schema(schema), "create schema " + schema)
        self._run_sql(cursor, statement, ("update " if exists else "create ") + source.type.lower())
        what = {VIEW: "View", FUNCTION: "Function"}.get(source.kind, "Stored procedure")
        done = "updated in place (CREATE OR ALTER)" if exists else "created"
        if source.override:
            return COMPLETED, f"{what} {done} (LLM-assisted, reviewed)", target, [override_note(source), *converted.notes]
        return COMPLETED, f"{what} {done}" + (" (converted for Fabric)" if converted.changed else ""), target, converted.notes

    def _tables_without_identity(self, cursor: Any) -> set:
        """``schema.table`` of every Warehouse table with no IDENTITY column (e.g. created before IDENTITY was kept),
        where ``SET IDENTITY_INSERT`` is rejected. Read when first needed; an unreadable catalog removes nothing."""
        if "_no_identity" not in self.__dict__:
            try:
                cursor.execute(WITHOUT_IDENTITY_QUERY)
                self.__dict__["_no_identity"] = {f"{r[0]}.{r[1]}".lower() for r in cursor.fetchall()}
            except Exception:  # noqa: BLE001 - keep the statement; Fabric reports it if it is wrong
                self.__dict__["_no_identity"] = set()
        return self.__dict__["_no_identity"]

    def _run_sql(self, cursor: Any, statement: Optional[str], what: str) -> None:
        if not statement:
            return
        started = time.monotonic()
        # The statement itself is never logged: only what it is, how long it took, and its hash.
        fields = {"what": what, "sqlHash": hashlib.sha256(statement.encode("utf-8")).hexdigest()[:12]}
        try:
            cursor.execute(statement)
        except Exception as exc:  # noqa: BLE001 - the driver's message is the useful part
            _log.warning("Warehouse rejected the %s", what, extra={**fields, "durationMs": int((time.monotonic() - started) * 1000)})
            raise MigrationError(f"Fabric Warehouse rejected the {what}: {str(exc)[:400]}. Review it by hand.") from exc
        _log.debug("Warehouse ran the %s", what, extra={**fields, "durationMs": int((time.monotonic() - started) * 1000)})
        self._keep("sql", what, statement)

    def _close(self) -> None:
        if self._source is not None:
            try:
                self._source.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
            self._source = None
        if self._sql is not None:
            try:
                self._sql.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
            self._sql = None
