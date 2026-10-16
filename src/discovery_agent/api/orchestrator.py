"""The orchestrator behind the plan board and the assistant: the execution plan, action requests, the supervisor.

    GET  /api/orchestrator/plan              the execution plan: its version, every step with its status, open actions
    POST /api/orchestrator/steps/mark        {step, done, note?}: a person marks a task of theirs done (or not)
    GET  /api/orchestrator/actions           ?status=open|all: what the agent proposed, and what became of it
    POST /api/orchestrator/actions/decide    {id, decision: confirm | reject, note?, reason?} (a person only)
    POST /api/orchestrator/supervisor        {action: pause | resume | wake}
    GET  /api/orchestrator/tools             the tool registry: every tool, its tier and arguments
    GET  /api/orchestrator/diagnosis/{id}    why an object of the current run failed, and what may be done
    GET  /api/orchestrator/report            ?format=md | json: the sign-off report
    GET  /api/assistant/tasks                the assistant's tasks, newest first
    GET  /api/assistant/tasks/{id}           one task, with its conversation
    POST /api/assistant/ask                  {text, taskId?}: a new question, or a reply to one waiting
    POST /api/assistant/tasks/{id}/cancel    stop a task
    POST /api/assistant/tasks/{id}/continue  carry on a task a restart paused

What it keeps, in the project's store:

* ``orchestrator/plans/<n>`` (documents): each version of the execution plan, never changed, with its hash;
* ``orchestrator/marks`` (stream): a person marking a task of the plan done, or not;
* ``orchestrator/actions`` (stream): every action request and every change to it, append-only (the latest by id is
  where it stands): proposed, then confirmed and done (or failed), rejected, stale or expired; or taken by the agent
  alone, as the brief's autonomy allows;
* ``orchestrator/wakeups`` (stream): each time the supervisor woke, why, and what it did or asked for;
* ``orchestrator/control`` (document): whether a person paused the agent (the kill switch);
* ``assistant/tasks/<id>`` (documents) and ``assistant/<id>`` (streams): each task and its conversation.

**The supervisor** wakes on what the audit trail records (a pass finished or halted, a run interrupted, a sign-off,
a brief approved, a validation finished), at once when it starts, and on a slow timer as a fallback. Each time it
rebuilds what it knows from the services and the store, so a restart loses nothing; it never holds state of its own
between wake-ups. The trail only signals it: it does its work in its own thread, never in the one that wrote the
record (which may hold a service's lock). At *observe* it proposes nothing; at *suggest* (the default) everything it
would do is a request; at *act on Dev*, only on the very workspace the brief was approved for as a development one, it
resumes a pass a restart cut short and retries failures that pass (timeouts, throttling) on its own, each at most twice
a run. In every mode: at most three new actions a wake-up, one open request for the same thing, a request bound to
what it acts on (re-checked, and refused if that moved, when a person confirms it), one a person rejected never asked
for again for the same thing, and requests expired once they no longer apply or wait too long. A project switch holds
it: the wake-up in flight finishes first, and none starts until the switch is done.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from discovery_agent.api.consult import _Memory
from discovery_agent.api.service import ApiError
from discovery_agent.migration import failures, run_state
from discovery_agent.orchestrator import assistant as talk
from discovery_agent.orchestrator import diagnosis as diagnoses
from discovery_agent.orchestrator import plan as plans
from discovery_agent.orchestrator.tools import BOUND, READ, Registry, Services, ToolError, capped, moved
from discovery_agent.state import context
from discovery_agent.state.store import now

_log = logging.getLogger("discovery_agent.orchestrator")

PLANS, MARKS, ACTIONS, WAKEUPS, CONTROL, TASKS = (
    "orchestrator/plans", "orchestrator/marks", "orchestrator/actions", "orchestrator/wakeups", "orchestrator/control",
    "assistant/tasks")
#: The agent, as the trail names it: what it starts is its own, never a person's.
AGENT = context.Actor(context.AGENT, "orchestrator")
ASSISTANT = context.Actor(context.AGENT, "assistant")
#: How long the supervisor sleeps with nothing to wake it: the audit trail wakes it on everything that matters.
FALLBACK_SECONDS = 300.0
#: How long the planner's finding of which connections need credentials is taken as read (it lists Fabric's connections).
CREDENTIALS_SECONDS = 300.0
#: In every mode: new actions a wake-up; and the agent's own retries, and its own resumes, of one run.
MAX_ACTIONS_PER_WAKE, MAX_AGENT_RETRIES, MAX_AGENT_RESUMES = 3, 2, 2
#: A request nobody decided on lapses after this long.
REQUEST_TTL = _dt.timedelta(hours=24)
#: How long a project switch waits for a wake-up in flight to finish.
HOLD_SECONDS = 30.0
#: What wakes the supervisor: the trail's records of these families.
WAKE_ON = ("run.", "validation.", "brief.", "git.", "transform.drafted", "transform.accepted", "plan.recorded", "discovery.completed",
           "fabric.connected", "source.connected")
PROPOSED, DONE, FAILED_ACTION, REJECTED, STALE, EXPIRED = "proposed", "done", "failed", "rejected", "stale", "expired"
TASK_RUNNING, TASK_PERSON, TASK_REPLY, TASK_DONE, TASK_CANCELLED, TASK_PAUSED, TASK_BLOCKED = (
    "running", "waiting_for_person", "waiting_for_reply", "done", "cancelled", "paused", "blocked")
_TASK_ID = re.compile(r"^[0-9a-f]{12}$")
_ACTION_ID = re.compile(r"^A[0-9a-f]{10}$")
MAX_QUESTION = 2000


def switched_on() -> bool:
    """Whether the agent may propose or act at all (``MIGRATION_AGENT``): off keeps the plan board, never the agent."""
    return (os.environ.get("MIGRATION_AGENT") or "on").strip().lower() not in ("off", "0", "false", "no", "disabled")


def _no_audit(*args: Any, **kwargs: Any) -> None:
    return None


def _parse(at: Optional[str]) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.fromisoformat(str(at)) if at else None
    except ValueError:
        return None


class OrchestratorService:
    def __init__(self, session: Any, migration: Any, consult: Any = None, transforms: Any = None, fabric: Any = None,
                 records: Any = None, audit: Optional[Callable[..., Any]] = None, model: Any = None,
                 trail: Optional[Callable[[], Mapping[str, Any]]] = None, project_of: Optional[Callable[[], Optional[str]]] = None,
                 synchronous: bool = False) -> None:
        self._session, self._migration, self._consult, self._transforms, self._fabric = session, migration, consult, transforms, fabric
        self._records: Any = records if records is not None else _Memory()
        #: Records what the agent and people do here (``state.audit``); never raises.
        self._audit = audit or _no_audit
        self._model = model
        #: The audit trail's verification (its head hash), for the sign-off report.
        self._trail = trail
        self._project_of = project_of or (lambda: None)
        #: Tasks and the supervisor's work run in the calling thread (the tests), not in threads of their own.
        self._synchronous = synchronous
        #: The store's bookkeeping (plan versions, requests): held only around reads and writes of the store, never
        #: across a call to another service.
        self._lock = threading.RLock()
        #: What woke the supervisor, kept by whoever wrote it; the only lock an audit listener ever takes.
        self._trigger_lock = threading.Lock()
        #: Held by a wake-up for all of it: one at a time, and what a project switch waits for.
        self._working = threading.Lock()
        #: Set while a project switch holds the supervisor: no wake-up starts.
        self._held = threading.Event()
        #: An assistant task's document, read and written by its thread and by a person's cancel or reply.
        self._task_lock = threading.RLock()
        self._wake = threading.Event()
        self._triggers: List[str] = []
        self._last_wake: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tasks: Dict[str, threading.Thread] = {}
        #: (plan version, when, the connections it needs credentials for): the planner is asked at most every few minutes.
        self._credentials_seen: Tuple[Optional[str], float, List[str]] = (None, 0.0, [])
        self.registry = Registry(Services(session, migration, consult, transforms, fabric, steps=self._steps_for_tools))
        self._pause_interrupted_tasks()

    # -- the project ------------------------------------------------------------------------------------------------

    def busy(self) -> Optional[str]:
        """Why the project cannot be switched now (an assistant task is talking to the model), or None."""
        with self._lock:
            working = [t for t, th in self._tasks.items() if th.is_alive()]
        return f"The assistant is working on {len(working)} question(s); wait for them, or cancel them." if working else None

    def hold(self, timeout: float = HOLD_SECONDS) -> None:
        """For a project switch: no wake-up starts from now on, and the one in flight (if any) finishes first. Raises
        TimeoutError when it does not finish in time (the switch is then refused, and the supervisor carries on)."""
        self._held.set()
        if not self._working.acquire(timeout=timeout):
            self._held.clear()
            raise TimeoutError("a wake-up of the supervisor is still running")
        self._working.release()

    def release(self) -> None:
        """The switch is over: wake-ups start again (the switch itself woke the supervisor for the new project)."""
        self._held.clear()
        self._wake.set()

    def switch(self, records: Any) -> None:
        with self._lock:
            self._records = records if records is not None else _Memory()
            self._tasks = {}
            self._credentials_seen = (None, 0.0, [])
        self._pause_interrupted_tasks()
        self.wake("project.activated")

    # -- what the plan is built from ----------------------------------------------------------------------------------

    def _policy(self) -> Any:
        if self._consult is None:
            return None
        try:
            return self._consult.in_force()
        except Exception as exc:  # noqa: BLE001 - a brief that cannot be read is no brief here; Consult says why
            _log.warning("Could not read the brief: %s: %s", type(exc).__name__, exc)
            return None

    def _inputs(self, policy: Any) -> plans.Inputs:
        plan = self._migration.latest_plan() or {}
        ids = [str(i.get("id")) for i in plan.get("items") or []]
        flags: Dict[str, List[str]] = {}
        names: Dict[str, str] = {}
        credentials: List[str] = []
        snapshot = None
        loads_data = "data" in ((plan.get("options") or {}).get("stages") or [])
        try:
            job, pool = self._session.migration_snapshot()
            snapshot = (getattr(job, "snapshot", None) or {}).get("id")
            names = {str(i.get("id")): str(i.get("name") or i.get("id")) for i in job.items}
            if self._consult is not None:
                _, estate = self._consult.facts_of(job, pool)
                for oid in ids:
                    for flag in estate.flags.get(oid) or []:
                        flags.setdefault(flag, []).append(oid)
            credentials = self._needed_credentials(plan)
        except ApiError:
            pass
        except Exception as exc:  # noqa: BLE001 - an estate that cannot be read leaves the person tasks out, never the plan
            _log.warning("Could not read the estate for the execution plan: %s: %s", type(exc).__name__, exc)
        unmapped: List[str] = []
        manual = 0
        if policy is not None:
            facts = policy.facts
            mapped = {str(k).lower() for k in (policy.sql_user_map() or {})}
            unmapped = sorted(u for u in facts.get("security.sqlUserNames") or [] if str(u).lower() not in mapped)
            manual = int(facts.get("manual.setupCount") or 0)
        return plans.Inputs(
            brief_version=policy.version if policy is not None else None, brief_hash=policy.hash if policy is not None else None,
            settings=dict(policy.settings) if policy is not None else {}, plan_version=plan.get("id"), plan_objects=len(ids),
            snapshot=snapshot, flags={k: sorted(set(v)) for k, v in flags.items()}, names=names, credentials=credentials,
            unmapped_users=unmapped, manual_setup=manual, loads_data=loads_data)

    def _needed_credentials(self, plan: Mapping[str, Any]) -> List[str]:
        """The connections a run of the plan asks a person for credentials for: the planner's own finding, taken as read
        for a few minutes (the planner lists Fabric's connections to see which exist already)."""
        if not plan.get("items"):
            return []
        held, at, names = self._credentials_seen
        if held == plan.get("id") and time.monotonic() - at < CREDENTIALS_SECONDS:
            return list(names)
        try:
            analysis = self._migration.analyze({"items": [dict(i) for i in plan.get("items") or []],
                                                "options": dict(plan.get("options") or {})})
        except Exception:  # noqa: BLE001 - unknown: the run's own check says so when it starts
            return []
        names = sorted({o for r in analysis.get("risks") or [] if r.get("code") == "NEEDS_CREDENTIALS" for o in r.get("objects") or []})
        self._credentials_seen = (plan.get("id"), time.monotonic(), names)
        return names

    def _live(self, policy: Any) -> plans.Live:
        fabric = {}
        try:
            fabric = self._fabric.state() if self._fabric is not None else {}
        except Exception:  # noqa: BLE001 - the target is context
            fabric = {}
        status = self._session.discovery_status()
        snapshot = status.get("snapshot") or {}
        connection = {}
        try:
            connection = self._session.connection_state()
        except Exception:  # noqa: BLE001 - not connected, then
            connection = {}
        run = self._migration.state()
        plan = self._migration.latest_plan() or {}
        drafts: Dict[str, str] = {}
        if self._transforms is not None:
            try:
                drafts = {str(i.get("objectId")): str(i.get("state")) for i in self._transforms.list().get("items") or []}
            except Exception:  # noqa: BLE001 - no drafts read is no drafts
                drafts = {}
        from discovery_agent.api.consult import REQUIRED, mode  # noqa: PLC0415 - the consultation's own setting

        identity = fabric.get("accountType")
        return plans.Live(
            source_connected=connection.get("status") == "connected",
            target_connected=fabric.get("status") == "connected", capacity=fabric.get("capacityAssigned"),
            discovery_state=str(status.get("state") or "idle"),
            discovery_complete=snapshot.get("complete") if snapshot else None,
            brief_version=policy.version if policy is not None else None, brief_stale=bool(policy is not None and policy.stale),
            required=mode() == REQUIRED, plan_version=plan.get("id"), plan_blocking=plan.get("blocking"),
            run=run if run.get("runId") else None, validation=self._migration.validation_state(), drafts=drafts,
            marks=self._marks(), fabric_identity={"user": "person", "servicePrincipal": "service_principal"}.get(str(identity)),
            run_credentials=list(run.get("credentialNames") or []))

    def _marks(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for r in self._records.records(MARKS):
            out[str(r.get("step"))] = r
        return out

    # -- the execution plan, versioned --------------------------------------------------------------------------------

    def _numbers(self) -> List[int]:
        out = []
        for name in self._records.names(PLANS):
            try:
                out.append(int(name))
            except ValueError:
                continue
        return sorted(out)

    def current(self, trigger: str = "read") -> Tuple[plans.ExecutionPlan, int, Dict[str, plans.StepState], Any]:
        """The execution plan for the project as it stands (a new version kept when its hash moved), where each step
        stands, and the policy it rests on."""
        policy = self._policy()
        built = plans.build(self._inputs(policy))
        with self._lock:
            numbers = self._numbers()
            latest = self._records.get(f"{PLANS}/{numbers[-1]:04d}") if numbers else None
            if latest is None or latest.get("hash") != built.hash:
                number = (numbers[-1] if numbers else 0) + 1
                self._records.put(f"{PLANS}/{number:04d}", {"version": number, "hash": built.hash, **built.to_dict(),
                                                           "createdAt": now(), "trigger": trigger,
                                                           "previous": latest.get("version") if latest else None})
                self._audit("orchestrator.plan_built", target={"type": "plan", "id": str(number)}, actor=context.system(), details={
                    "version": number, "hash": built.hash, "briefVersion": built.inputs.get("briefVersion"),
                    "planVersion": built.inputs.get("planVersion"), "snapshot": built.inputs.get("snapshot"), "steps": len(built.steps)})
            else:
                number = int(latest["version"])
        states = plans.state_of(built, self._live(policy))
        return built, number, states, policy

    def _steps_for_tools(self) -> Dict[str, Any]:
        built, number, states, _ = self.current()
        return {"version": number, "steps": [{"key": s.key, "title": s.title, "owner": s.owner, "phase": s.phase,
                                              "status": states[s.key].status, "message": states[s.key].message,
                                              "answers": list(s.answers)} for s in built.steps]}

    def plan_view(self) -> dict:
        built, number, states, policy = self.current()
        open_actions = self._actions(open_only=True)
        by_step: Dict[str, List[Dict[str, Any]]] = {}
        for a in open_actions:
            by_step.setdefault(str(a.get("step") or ""), []).append(a)
        control = self._records.get(CONTROL) or {}
        kept = self._records.last(WAKEUPS) if hasattr(self._records, "last") else (self._records.records(WAKEUPS)[-1:] or [None])[0]
        last = max([w for w in (kept, self._last_wake) if w], key=lambda w: str(w.get("at") or ""), default=None)
        model, why_no_ai = self._gate()
        return {
            # Whether the assistant's questions go to the model (consent in an approved brief, budget, a model here), or
            # are answered by its fixed intents.
            "assistant": {"ai": model is not None, "reason": why_no_ai},
            "version": number, "hash": built.hash, "inputs": dict(built.inputs),
            "phases": [{"id": p, "title": t} for p, t in plans.PHASES],
            "steps": [{**s.to_dict(), "status": states[s.key].status, "message": states[s.key].message,
                       "markedBy": states[s.key].marked_by, "actions": by_step.get(s.key, [])} for s in built.steps],
            "actions": open_actions,
            "agent": {"on": switched_on(), "paused": bool(control.get("paused")), "pausedBy": control.get("by"),
                      "autonomy": policy.autonomy() if policy is not None else "suggest",
                      "targetRole": policy.target_role() if policy is not None else None,
                      "actsAlone": self._acts_alone(policy),
                      # At act on Dev, why it still asks for everything here (another workspace, a stale brief...).
                      "aloneWhyNot": self._alone_why_not(policy) if policy is not None and policy.autonomy() == "act_dev" else None,
                      "lastWake": last},
        }

    def mark(self, body: dict) -> dict:
        """A person marks a task of the plan done, or not done, with a note. The accelerator and the agent never do."""
        if context.actor().kind != context.PERSON:
            raise ApiError(403, "person_only", "Only a person marks a step done.")
        key = str(body.get("step") or "")
        built, _, states, _ = self.current()
        step = built.step(key)
        if step is None:
            raise ApiError(404, "not_found", "There is no such step in the execution plan.")
        if step.owner != plans.PERSON and step.kind != plans.CHECK:
            raise ApiError(409, "not_a_task", "This step is done by the accelerator or the agent: it is not marked by hand.")
        done = bool(body.get("done", True))
        note = " ".join(str(body.get("note") or "").split())[:500] or None
        if step.kind == plans.CHECK and done and states[key].status == plans.FAILED:
            raise ApiError(409, "check_fails", f"The accelerator sees this check failing, so it is fixed, not confirmed: {states[key].message}")
        if step.kind == plans.CHECK and done and states[key].status != plans.WARN:
            raise ApiError(409, "nothing_to_confirm", "This check passes: there is nothing to confirm by hand.")
        self._records.append(MARKS, {"step": key, "digest": step.digest, "done": done, "note": note,
                                     "by": context.actor().to_dict(), "at": now()})
        self._audit("orchestrator.step_marked", target={"type": "step", "id": key}, details={"step": key, "done": done, "note": note})
        self.wake("step.marked")
        return self.plan_view()

    # -- action requests --------------------------------------------------------------------------------------------

    def _actions(self, open_only: bool = False) -> List[Dict[str, Any]]:
        latest: Dict[str, Dict[str, Any]] = {}
        for r in self._records.records(ACTIONS):
            latest[str(r.get("id"))] = {**latest.get(str(r.get("id")), {}), **r}
        out = sorted(latest.values(), key=lambda a: str(a.get("createdAt") or ""), reverse=True)
        return [a for a in out if a.get("status") == PROPOSED] if open_only else out

    def actions_view(self, query: Mapping[str, str]) -> dict:
        everything = str(query.get("status") or "open") == "all"
        return {"actions": self._actions(open_only=not everything)}

    def _record_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._records.append(ACTIONS, action)
        return action

    def _stopped(self) -> Optional[str]:
        """Why the agent may propose and do nothing now (switched off, or paused by a person), or None."""
        if not switched_on():
            return "The agent is switched off on this server (MIGRATION_AGENT=off): nothing is proposed or done by it."
        control = self._records.get(CONTROL) or {}
        if control.get("paused"):
            return f"The agent is paused (by {(control.get('by') or {}).get('id') or 'a person'}): resume it on the plan board first."
        return None

    def _decided(self, key: str, binding: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """A request for the same thing, acting on the same, that a person already decided (rejected it, or confirmed it
        and it ran or failed), or that lapsed unanswered: never asked for again until what it acts on moves."""
        return next((a for a in self._actions() if a.get("key") == key and a.get("status") in (REJECTED, DONE, FAILED_ACTION, EXPIRED)
                     and dict(a.get("binding") or {}) == dict(binding)), None)

    def propose(self, tool_name: str, args: Mapping[str, Any], rationale: str, proposer: context.Actor = AGENT,
                step: Optional[str] = None, task: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """A request for a person to confirm: (the action, None), or (an open one for the same thing, None), or (None, why
        it cannot be asked for now)."""
        stopped = self._stopped()
        if stopped:
            return None, stopped
        tool = self.registry.get(tool_name)
        if tool is None or tool.tier == READ:
            return None, f"There is no tool '{tool_name}' to propose."
        try:
            clean = tool.clean(dict(args))
        except ToolError as exc:
            return None, str(exc)
        why = tool.check(clean)
        if why:
            return None, why
        key = f"{tool.name}:{tool.key(clean)}"
        binding = tool.bind(clean)
        preview = tool.preview(clean)
        with self._lock:
            same = next((a for a in self._actions(open_only=True) if a.get("key") == key), None)
            if same is not None:
                if task and same.get("task") != task and task not in (same.get("watchers") or []):
                    # The question waits on that request too: its decision is told to this task as well.
                    watchers = list(same.get("watchers") or []) + [task]
                    self._record_action({"id": same["id"], "watchers": watchers})
                    same = {**same, "watchers": watchers}
                return same, None
            # What the supervisor proposes by itself: a person asking the assistant for it again is their own decision.
            decided = self._decided(key, binding) if proposer == AGENT else None
            if decided is not None:
                said = f": {decided.get('reason')}" if decided.get("reason") else ""
                return None, (f"The same request was {decided.get('status')} ({decided.get('id')}{said}), and what it acts on has "
                              "not moved since: it is not asked for again.")
            action = {"id": "A" + uuid.uuid4().hex[:10], "tool": tool.name, "tier": tool.tier, "args": clean,
                      "preview": preview, "rationale": " ".join(str(rationale or "").split())[:1000], "step": step,
                      "task": task, "status": PROPOSED, "key": key, "binding": binding, "proposedBy": proposer.to_dict(),
                      "createdAt": now(),
                      "expiresAt": (_dt.datetime.now(_dt.timezone.utc) + REQUEST_TTL).isoformat(timespec="seconds")}
            self._record_action(action)
        self._audit("orchestrator.action_proposed", target={"type": "action", "id": action["id"]}, actor=proposer, details={
            "action": action["id"], "tool": tool.name, "tier": tool.tier, "summary": action["preview"].get("summary"),
            "step": step, "task": task})
        return action, None

    def decide(self, body: dict) -> dict:
        """A person confirms a request (re-checked first; it then runs as that person) or rejects it with a reason."""
        if context.actor().kind != context.PERSON:
            raise ApiError(403, "person_only", "Only a person confirms or rejects what the agent proposes.")
        action_id = str(body.get("id") or "")
        decision = str(body.get("decision") or "")
        if decision not in ("confirm", "reject"):
            raise ApiError(400, "invalid_request", "Choose confirm or reject.")
        with self._lock:
            action = next((a for a in self._actions() if a.get("id") == action_id), None) if _ACTION_ID.match(action_id) else None
            if action is None:
                raise ApiError(404, "not_found", "There is no such action.")
            if action.get("status") != PROPOSED:
                raise ApiError(409, "not_open", f"This action is {action.get('status')}: there is nothing to decide.")
            who = context.actor().to_dict()
            tool = self.registry.get(str(action.get("tool")))
            if decision == "reject":
                reason = " ".join(str(body.get("reason") or "").split())[:500]
                if not reason:
                    raise ApiError(400, "reason_required", "Say why, so the agent knows (it is kept with the action).")
                self._record_action({"id": action_id, "status": REJECTED, "decidedBy": who, "decidedAt": now(), "reason": reason})
                self._audit("orchestrator.action_rejected", target={"type": "action", "id": action_id}, reason=reason,
                            details={"action": action_id, "tool": action.get("tool"), "reason": reason})
                outcome = {"decision": "rejected", "reason": reason}
            else:
                expires = _parse(action.get("expiresAt"))
                if expires is not None and expires < _dt.datetime.now(_dt.timezone.utc):
                    self._lapse(action, EXPIRED, "Nobody decided on it in time.", who)
                    raise ApiError(409, "action_expired", "It lapsed: nobody decided on it in time. Ask again for it as things are now.")
                why = self._still_applies(tool, action)
                if why:
                    self._lapse(action, STALE, why, who)
                    raise ApiError(409, "action_stale", f"It no longer applies: {why}")
                note = " ".join(str(body.get("note") or "").split())[:500] or None
                result, error = self._carry_out(tool, action.get("args") or {}, action.get("binding"))
                status = DONE if error is None else FAILED_ACTION
                self._record_action({"id": action_id, "status": status, "decidedBy": who, "decidedAt": now(), "note": note,
                                     "result": capped(result, 3000) if error is None else None, "error": error})
                self._audit("orchestrator.action_confirmed", target={"type": "action", "id": action_id}, reason=note,
                            outcome="done" if error is None else "failed", details={
                                "action": action_id, "tool": action.get("tool"), "tier": action.get("tier"), "note": note,
                                "outcome": "done" if error is None else error})
                outcome = {"decision": "confirmed", "result": capped(result, 3000) if error is None else None, "error": error}
        self._tell_tasks(action, outcome)
        self.wake("action.decided")
        return {"action": next(a for a in self._actions() if a.get("id") == action_id), "plan": self.plan_view()}

    def _still_applies(self, tool: Any, action: Mapping[str, Any]) -> Optional[str]:
        """Why a request may not be carried out now, or None: its tool's own check, and what it acts on still what it
        was bound to when it was proposed."""
        if tool is None:
            return "The tool is not there any more."
        why = tool.check(action.get("args") or {})
        if why:
            return why
        if "binding" in action:
            return moved(action.get("binding") or {}, tool.bind(action.get("args") or {}))
        return None

    def _lapse(self, action: Mapping[str, Any], status: str, reason: str, who: Optional[Mapping[str, Any]] = None) -> None:
        """A request that will not be carried out: stale (it no longer applies) or expired (nobody decided in time)."""
        self._record_action({"id": action["id"], "status": status, "decidedAt": now(), "reason": reason,
                             **({"decidedBy": dict(who)} if who else {})})
        self._audit("orchestrator.action_expired", target={"type": "action", "id": action["id"]}, actor=AGENT if who is None else None,
                    reason=reason, details={"action": action["id"], "tool": action.get("tool"), "reason": reason})

    def _tell_tasks(self, action: Mapping[str, Any], outcome: Mapping[str, Any]) -> None:
        """Every question waiting on the request hears what became of it, and carries on."""
        for task_id in dict.fromkeys([action.get("task")] + list(action.get("watchers") or [])):
            if task_id:
                self._task_decided(str(task_id), action, outcome)

    @staticmethod
    def _carry_out(tool: Any, args: Mapping[str, Any], binding: Optional[Mapping[str, Any]] = None) -> Tuple[Any, Optional[str]]:
        try:
            return tool.run({**args, BOUND: dict(binding)} if binding else dict(args)), None
        except ApiError as exc:
            return None, exc.message
        except ToolError as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001 - the action failed; the trail and the card say so, nothing more
            from discovery_agent.diagnostics import record_unexpected, unexpected_message  # noqa: PLC0415

            reference = record_unexpected(f"the action {tool.name}", exc)
            return None, unexpected_message("It could not be carried out", exc, reference)

    # -- the supervisor -------------------------------------------------------------------------------------------

    def notify(self, record: Mapping[str, Any]) -> None:
        """An audit record was written: wake the supervisor if it is one that could change the plan (an audit listener).

        It runs in the thread that wrote the record, which may hold a service's lock: it only signals, and returns at
        once. The wake-up itself happens in the supervisor's own thread."""
        action = str(record.get("action") or "")
        if action.startswith("orchestrator.") or action.startswith("assistant."):
            return
        if any(action.startswith(prefix) for prefix in WAKE_ON):
            self.wake(action)

    def wake(self, trigger: str) -> None:
        with self._trigger_lock:
            self._triggers = (self._triggers + [trigger])[-20:]
        self._wake.set()

    def start(self) -> None:
        """Start the supervisor's loop (once). Its first wake-up is at once: what happened while the accelerator was
        stopped (a run cut short) was written before anything listened."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.wake("started")
        self._thread = context.spawn(self._loop, name="orchestrator-supervisor", actor=AGENT)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(FALLBACK_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                return
            if self._held.is_set():
                continue  # a project switch: the switch wakes it again once it is done
            with self._trigger_lock:
                triggers, self._triggers = self._triggers, []
            try:
                with context.bound(correlation=context.new_correlation_id(), actor=AGENT, project=self._project_of()):
                    self.reconcile(", ".join(dict.fromkeys(triggers)) or "timer")
            except Exception as exc:  # noqa: BLE001 - one bad wake-up never stops the supervisor; the API's log says why
                from discovery_agent.diagnostics import record_unexpected  # noqa: PLC0415

                record_unexpected("the supervisor's wake-up", exc)

    def control(self, body: dict) -> dict:
        """pause | resume (a person only: the kill switch), wake (check now)."""
        action = str(body.get("action") or "")
        if action in ("pause", "resume"):
            if context.actor().kind != context.PERSON:
                raise ApiError(403, "person_only", "Only a person pauses or resumes the agent.")
            paused = action == "pause"
            self._records.put(CONTROL, {"paused": paused, "by": context.actor().to_dict(), "at": now()})
            if paused:
                self._audit("orchestrator.paused", target={"type": "agent", "id": "orchestrator"})
            else:
                self._audit("orchestrator.resumed", target={"type": "agent", "id": "orchestrator"})
                self.wake("agent.resumed")
            return self.plan_view()
        if action == "wake":
            if self._synchronous:
                self.reconcile("asked")
            else:
                self.wake("asked")
            return self.plan_view()
        raise ApiError(400, "invalid_request", "Choose pause, resume or wake.")

    def _alone_why_not(self, policy: Any) -> Optional[str]:
        """Why the agent does not act on its own here (None when it may): only at act on Dev, only on the very
        workspace the approved brief calls a development one, switched on and not paused."""
        if policy is None or policy.autonomy() != "act_dev":
            return "the brief does not let it act on its own (E6)"
        if policy.target_role() != "dev":
            return "the brief does not call this a development workspace (E1)"
        if policy.stale:
            return "the brief no longer fits: it is reconfirmed in Consult first"
        if not policy.on_approved_target:
            return ("the workspace connected is not the one the brief was approved for" if policy.target
                    else "the brief was approved with no Fabric workspace connected, so it names none")
        return self._stopped()

    def _acts_alone(self, policy: Any) -> bool:
        return self._alone_why_not(policy) is None

    def _expire(self) -> List[str]:
        """Requests nobody decided on that no longer apply (their check, or what they act on moved), or that waited too
        long: they lapse, saying why, and any question waiting on one hears it."""
        gone = []
        moment = _dt.datetime.now(_dt.timezone.utc)
        for a in self._actions(open_only=True):
            tool = self.registry.get(str(a.get("tool")))
            why = self._still_applies(tool, a)
            expires = _parse(a.get("expiresAt"))
            if why or (expires is not None and expires < moment):
                reason = why or "Nobody decided on it in time."
                self._lapse(a, STALE if why else EXPIRED, reason)
                self._tell_tasks(a, {"decision": "lapsed", "reason": reason})
                gone.append(a["id"])
        return gone

    def _agent_took(self, tool_name: str, run: Mapping[str, Any]) -> int:
        """How often the agent did this to this run on its own (a retry, a resume): never more than its limit."""
        return sum(1 for a in self._actions() if a.get("tool") == tool_name and a.get("autonomous") and a.get("runId") == run.get("runId"))

    def reconcile(self, trigger: str = "timer") -> Dict[str, Any]:
        """One wake-up: the plan rebuilt from what the services report, requests that lapsed expired, and what the brief's
        autonomy allows done or asked for. Returns the wake-up's record.

        One at a time (``_working``), and never while a project switch holds the supervisor. No lock of its own is held
        while it calls the services: only around what it reads and writes in its store."""
        with self._working:
            if self._held.is_set():
                return {"at": now(), "trigger": trigger, "skipped": "a project switch is under way"}
            built, number, states, policy = self.current(trigger)
            record: Dict[str, Any] = {"at": now(), "trigger": trigger, "plan": number, "acted": [], "proposed": [], "expired": []}
            # Requests lapse whatever the agent may do: one that no longer applies is never confirmed later.
            record["expired"] = self._expire()
            stopped = self._stopped()
            if stopped:
                record["skipped"] = "switched off" if not switched_on() else "paused by a person"
                return self._keep_wake(record)
            autonomy = policy.autonomy() if policy is not None else "suggest"
            if autonomy == "observe":
                record["skipped"] = "observe: the agent proposes nothing"
                return self._keep_wake(record)
            alone = self._acts_alone(policy)
            budget = MAX_ACTIONS_PER_WAKE
            open_keys = {a.get("key") for a in self._actions(open_only=True)}
            for tool_name, args, step, rationale, may_act_alone in self._candidates(built, states, policy):
                if budget <= 0:
                    break
                tool = self.registry.get(tool_name)
                if tool is None or tool.check(args):
                    continue
                key = f"{tool.name}:{tool.key(args)}"
                if key in open_keys:
                    continue  # one open request for the same thing
                if alone and may_act_alone:
                    taken = self._act_alone(tool, args, step, rationale)
                    if taken is not None:
                        record["acted"].append({"action": taken["id"], "tool": tool_name, "status": taken["status"]})
                        open_keys.add(key)
                        budget -= 1
                        continue
                    # Past what it may do alone (its retries or resumes of this run are spent): a person decides the rest.
                action, _ = self.propose(tool_name, args, rationale, AGENT, step)
                if action is not None:
                    record["proposed"].append({"action": action["id"], "tool": tool_name})
                    open_keys.add(key)
                    budget -= 1
            return self._keep_wake(record)

    def _keep_wake(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """A wake-up is kept when something woke it, or it did something: a quiet timer that did nothing is not."""
        if record.get("trigger") == "timer" and not (record.get("acted") or record.get("proposed") or record.get("expired")):
            self._last_wake = record
            return record
        return self._records.append(WAKEUPS, record)

    def _act_alone(self, tool: Any, args: Mapping[str, Any], step: Optional[str], rationale: str) -> Optional[Dict[str, Any]]:
        """What the brief lets the agent do without asking (act on Dev): done as the agent, recorded as its own."""
        run = self._migration.state()
        limit = {"run.retry": MAX_AGENT_RETRIES, "run.resume": MAX_AGENT_RESUMES}.get(tool.name)
        if limit is None or self._agent_took(tool.name, run) >= limit:
            return None
        binding = tool.bind(args)
        action = {"id": "A" + uuid.uuid4().hex[:10], "tool": tool.name, "tier": tool.tier, "args": dict(args),
                  "preview": tool.preview(args), "rationale": rationale, "step": step, "task": None, "key": f"{tool.name}:{tool.key(args)}",
                  "binding": binding, "proposedBy": AGENT.to_dict(), "createdAt": now(), "autonomous": True, "runId": run.get("runId")}
        with context.bound(actor=AGENT):
            result, error = self._carry_out(tool, args, binding)
        action.update(status=DONE if error is None else FAILED_ACTION, decidedAt=now(), error=error,
                      result=capped(result, 3000) if error is None else None)
        self._record_action(action)
        self._audit("orchestrator.action_taken", target={"type": "action", "id": action["id"]}, actor=AGENT,
                    outcome="done" if error is None else "failed", details={
                        "action": action["id"], "tool": tool.name, "tier": tool.tier, "summary": action["preview"].get("summary"),
                        "outcome": "done" if error is None else error})
        return action

    def _candidates(self, built: plans.ExecutionPlan, states: Mapping[str, plans.StepState], policy: Any
                    ) -> Iterable[Tuple[str, Dict[str, Any], str, str, bool]]:
        """What the plan calls for now, in its order: (tool, args, step, why, whether act-on-Dev may do it alone)."""
        run = self._migration.state()
        status = str(run.get("status") or "idle")
        status_of = {k: v.status for k, v in states.items()}
        # Prepare: drafts the brief sends to AI, ten at a time; only those a draft would be made for (D6). Always a
        # request: a draft sends a definition to Claude and spends the budget (D2), which only a person asks for.
        drafts_step = built.step("prepare.drafts")
        if drafts_step is not None and status_of.get("prepare.drafts") == plans.READY:
            live_drafts = self._drafts()
            missing = self.registry.draftable([o for o in drafts_step.params.get("objects") or []
                                               if live_drafts.get(o) not in ("draft", "accepted", "queued", "running")])
            if missing:
                yield ("drafts.generate", {"ids": missing[:10]}, "prepare.drafts",
                       f"The brief sends these to AI ({', '.join(drafts_step.answers)}): {len(missing)} without a draft.", False)
        # Deliver: start the run once everything it waits for is done; with the tool its options call for (one that
        # overwrites is high-tier). Never again a run a person declined or stopped, until a new plan is recorded.
        if status_of.get("deliver.run") == plans.READY and status in ("idle", "cancelled", "rejected"):
            plan = self._migration.latest_plan() or {}
            newer = bool(plan.get("createdAt") and run.get("createdAt")) and str(plan["createdAt"]) > str(run["createdAt"])
            declined = status in ("cancelled", "rejected") and not newer  # unless a plan is known to be newer than that run
            if not declined:
                yield (self.registry.start_tool(), {}, "deliver.run", "Every check the run waits for passes.", False)
        # A pass a restart cut short: resume it.
        if status == run_state.PAUSED and run.get("pausedReason") == run_state.INTERRUPTED:
            yield ("run.resume", {}, "deliver.run", "The accelerator stopped under the run: Resume carries it on from where it was.", True)
        # Failures: retried when every one can pass on a retry; on its own only when each one was a timeout or throttling
        # (a failure cut short by a restart is retried at a person's word). A draft proposed for what Fabric rejected.
        if status in (run_state.COMPLETED_WITH_FAILURES, run_state.FAILED) or (status == run_state.PAUSED and run.get("pausedReason") == run_state.HALTED):
            found = diagnoses.summary(list(run.get("items") or []), policy.llm()[0] if policy is not None else None,
                                      self.registry.failed_kinds(), self._drafts())
            retryable = [d for d in found if d.category in (failures.TRANSIENT, failures.INTERRUPTED)]
            if found and len(retryable) == len(found):
                alone = all(d.category in failures.RETRYABLE for d in found)
                yield ("run.retry", {}, "deliver.fix", f"{len(found)} failure(s), each one that passes on a retry (a timeout, "
                       "throttling, a restart): a retry should do.", alone)
            elif found:
                drafts = self.registry.draftable([d.object_id for d in found if d.recommended == diagnoses.DRAFT])
                if drafts:
                    yield ("drafts.generate", {"ids": drafts[:10]}, "deliver.fix",
                           f"{len(drafts)} object(s) Fabric does not accept as they are: a draft for a person to review, then a retry.", False)
                if any(d.recommended == diagnoses.RETRY for d in found) and all(d.recommended in (diagnoses.RETRY, diagnoses.DRAFT) for d in found) \
                        and not drafts:
                    yield ("run.retry", {}, "deliver.fix", "Every failure has what it needs now: retry them.", False)
        # Validate at the brief's depth, once the run completed with nothing left to fix: always a request.
        if status_of.get("validate.run") == plans.READY and status == run_state.COMPLETED:
            depth = (built.step("validate.run").params or {}).get("depth") or "probe"
            tool = {"compare": "validate.compare", "probe": "validate.probe", "run": "validate.run"}.get(depth, "validate.probe")
            yield (tool, {}, "validate.run", f"The run finished: validate at the brief's depth (H1: {depth}).", False)
        # Release: a commit that did not happen after the run.
        if status_of.get("release.commit") == plans.FAILED:
            yield ("git.commit", {}, "release.commit", "The commit after the run was not made; commit the run's items now.", False)

    def _drafts(self) -> Dict[str, str]:
        if self._transforms is None:
            return {}
        try:
            return {str(i.get("objectId")): str(i.get("state")) for i in self._transforms.list().get("items") or []}
        except Exception:  # noqa: BLE001 - no drafts read is no drafts
            return {}

    # -- reads for the page -------------------------------------------------------------------------------------------

    def tools_view(self) -> dict:
        from discovery_agent.orchestrator.tools import PERSON_ONLY  # noqa: PLC0415

        return {"tools": self.registry.describe(), "personOnly": list(PERSON_ONLY)}

    def diagnosis(self, object_id: str) -> dict:
        tool = self.registry.get("object.diagnosis")
        found = tool.run({"id": object_id})
        if found is None:
            raise ApiError(404, "not_found", "That object did not fail in the current run.")
        return found

    # -- the sign-off report ---------------------------------------------------------------------------------------------

    def report(self, query: Mapping[str, str]) -> Tuple[bytes, str, str]:
        fmt = str(query.get("format") or "md").lower()
        if fmt not in ("md", "json"):
            raise ApiError(400, "invalid_request", "Choose a format of md or json.")
        built, number, states, policy = self.current()
        trail = dict(self._trail()) if self._trail is not None else {}
        runs = self._migration.runs().get("runs") or []
        validation = self._migration.validation_state()
        run = self._migration.state()
        data = {
            "madeAt": now(), "madeBy": context.actor().to_dict(),
            "brief": policy.describe() if policy is not None else None,
            "executionPlan": {"version": number, "hash": built.hash, "inputs": dict(built.inputs),
                              "steps": [{"key": s.key, "title": s.title, "owner": s.owner, "status": states[s.key].status,
                                         "message": states[s.key].message, "answers": list(s.answers)} for s in built.steps]},
            "plan": {k: v for k, v in (self._migration.latest_plan() or {}).items() if k not in ("items", "options")},
            "runs": runs, "validation": {k: validation.get(k) for k in ("state", "summary", "finishedAt", "workspace", "warehouse")},
            "mismatches": [r for r in validation.get("rows") or [] if r.get("status") == "MISMATCH"][:100],
            "leftForAPerson": [{"name": i.get("name"), "type": i.get("type"), "reason": i.get("error") or i.get("step")}
                               for i in run.get("items") or [] if i.get("status") == "DEFERRED"][:200],
            "trail": {"records": trail.get("records"), "head": trail.get("head"), "ok": trail.get("ok")},
        }
        self._audit("report.exported", target={"type": "report", "id": str(number)}, details={"format": fmt, "auditHead": trail.get("head")})
        if fmt == "json":
            return json.dumps(data, indent=2, ensure_ascii=False, default=str).encode("utf-8"), "application/json", "migration-signoff-report.json"
        return _report_markdown(data).encode("utf-8"), "text/markdown; charset=utf-8", "migration-signoff-report.md"

    # -- the assistant ------------------------------------------------------------------------------------------------

    def _task(self, task_id: str) -> Dict[str, Any]:
        doc = self._records.get(f"{TASKS}/{task_id}") if _TASK_ID.match(task_id or "") else None
        if doc is None:
            raise ApiError(404, "not_found", "There is no such question.")
        return doc

    def _entries(self, task_id: str) -> List[Dict[str, Any]]:
        return self._records.records(f"assistant/{task_id}")

    def _say(self, task_id: str, entry: Dict[str, Any]) -> None:
        self._records.append(f"assistant/{task_id}", {**entry, "at": now()})

    def _save_task(self, task: Dict[str, Any]) -> None:
        self._records.put(f"{TASKS}/{task['id']}", task)

    def _pause_interrupted_tasks(self) -> None:
        """Tasks that were talking to the model when the process stopped: paused, so Continue carries them on."""
        try:
            for name in self._records.names(TASKS):
                task = self._records.get(f"{TASKS}/{name}") or {}
                if task.get("status") == TASK_RUNNING:
                    task["status"] = TASK_PAUSED
                    self._save_task(task)
                    self._say(task["id"], {"type": "notice", "text": "The accelerator stopped while this was being worked on. Continue carries it on."})
        except Exception as exc:  # noqa: BLE001 - a task left running is said when it is opened
            _log.warning("Could not pause the assistant's interrupted tasks: %s: %s", type(exc).__name__, exc)

    def _gate(self) -> Tuple[Any, Optional[str]]:
        """(the model, None) when the assistant may use it now, else (None, why not)."""
        from discovery_agent.llm import claude_cli  # noqa: PLC0415

        if not claude_cli.switched_on():
            return None, "the model is switched off on this server (MIGRATION_LLM=off)."
        if self._consult is None:
            return None, "there is no consultation to give consent."
        gate = self._consult.llm_gate("assistant")
        if not gate.get("allowed"):
            return None, str(gate.get("reason") or "the brief does not allow it.")
        model = self._model or claude_cli.ClaudeCli()
        if not model.available().get("available"):
            return None, str(model.available().get("message") or "Claude Code is not on this machine.")
        self._model = model
        return model, None

    def tasks_view(self) -> dict:
        tasks = [self._records.get(f"{TASKS}/{n}") or {} for n in self._records.names(TASKS)]
        tasks = sorted((t for t in tasks if t), key=lambda t: str(t.get("createdAt") or ""), reverse=True)[:50]
        return {"tasks": [{k: t.get(k) for k in ("id", "question", "status", "source", "createdAt", "updatedAt", "turns", "changes",
                                                 "costUsd", "answer")} for t in tasks]}

    def task_view(self, task_id: str) -> dict:
        task = self._task(task_id)
        actions = {a["id"]: a for a in self._actions() if a.get("task") == task_id or task_id in (a.get("watchers") or [])}
        return {**task, "entries": self._entries(task_id), "actions": list(actions.values())}

    def _update_task(self, task_id: str, only_if: Optional[str] = TASK_RUNNING, **changes: Any) -> Optional[Dict[str, Any]]:
        """Change a task's document as it is now in the store (a person may have cancelled it meanwhile): the changes are
        made only while it is still ``only_if`` (any status with None). Returns the task as saved, or None when it was
        not (it moved on)."""
        with self._task_lock:
            task = self._task(task_id)
            if only_if is not None and task.get("status") != only_if:
                return None
            task.update(changes, updatedAt=now())
            self._save_task(task)
            return task

    def ask(self, body: dict) -> dict:
        """A person's question (a new task), or their reply to a task that asked them something."""
        if context.actor().kind != context.PERSON:
            raise ApiError(403, "person_only", "Only a person asks the assistant.")
        text = " ".join(str(body.get("text") or "").split())[:MAX_QUESTION]
        if not text:
            raise ApiError(400, "invalid_request", "Write the question.")
        task_id = str(body.get("taskId") or "")
        if task_id:
            self._task(task_id)
            if self._update_task(task_id, only_if=TASK_REPLY, status=TASK_RUNNING) is None:
                raise ApiError(409, "not_waiting", "This question is not waiting for a reply.")
            self._say(task_id, {"type": "person", "text": text})
            self._continue(task_id)
            return self.task_view(task_id)
        model, why = self._gate()
        task = {"id": uuid.uuid4().hex[:12], "question": text, "status": TASK_RUNNING, "by": context.actor().to_dict(),
                "createdAt": now(), "updatedAt": now(), "turns": 0, "changes": 0, "costUsd": 0.0, "estimated": False,
                "source": "ai" if model is not None else "rules", "answer": None}
        self._save_task(task)
        self._say(task["id"], {"type": "person", "text": text})
        self._audit("assistant.asked", target={"type": "task", "id": task["id"]}, details={"task": task["id"], "chars": len(text),
                                                                                           "source": task["source"]})
        if model is None:
            answer, reads = talk.answer_by_rules(text, self.registry, why)
            for r in reads:
                self._say(task["id"], {"type": "result", "tool": r["tool"], "ok": True, "data": r["data"]})
            self._say(task["id"], {"type": "answer", "text": answer})
            task.update(status=TASK_DONE, answer=answer, updatedAt=now(), why=why)
            self._save_task(task)
            self._finished(task, "answered by the rules")
            return self.task_view(task["id"])
        self._continue(task["id"])
        return self.task_view(task["id"])

    def cancel(self, task_id: str) -> dict:
        """A person stops a question: at once, even in the middle of a model turn (whatever that turn answers is dropped,
        and nothing more is proposed for it); a request it made lapses."""
        if context.actor().kind != context.PERSON:
            raise ApiError(403, "person_only", "Only a person cancels a question.")
        with self._task_lock:
            task = self._task(task_id)
            if task.get("status") in (TASK_DONE, TASK_CANCELLED):
                return self.task_view(task_id)
            task = self._update_task(task_id, only_if=None, status=TASK_CANCELLED) or task
        self._say(task_id, {"type": "notice", "text": "Cancelled by a person."})
        for a in self._actions(open_only=True):
            if a.get("task") == task_id:
                self._record_action({"id": a["id"], "status": EXPIRED, "decidedAt": now(), "reason": "Its question was cancelled."})
                self._audit("orchestrator.action_expired", target={"type": "action", "id": a["id"]}, reason="Its question was cancelled.",
                            details={"action": a["id"], "tool": a.get("tool"), "reason": "Its question was cancelled."})
        self._finished(task, "cancelled")
        return self.task_view(task_id)

    def resume_task(self, task_id: str) -> dict:
        if context.actor().kind != context.PERSON:
            raise ApiError(403, "person_only", "Only a person carries a question on.")
        self._task(task_id)
        if (self._update_task(task_id, only_if=TASK_PAUSED, status=TASK_RUNNING) is None
                and self._update_task(task_id, only_if=TASK_BLOCKED, status=TASK_RUNNING) is None):
            raise ApiError(409, "not_paused", "This question is not paused.")
        self._continue(task_id)
        return self.task_view(task_id)

    def _continue(self, task_id: str) -> None:
        if self._synchronous:
            self._work(task_id)
            return
        with self._lock:
            running = self._tasks.get(task_id)
            if running is not None and running.is_alive():
                return
            self._tasks[task_id] = context.spawn(self._work, task_id, name=f"assistant-{task_id}", actor=ASSISTANT)

    def _task_decided(self, task_id: str, action: Mapping[str, Any], outcome: Mapping[str, Any]) -> None:
        """A request a task waited on was decided, or lapsed: the task hears it, and carries on if it was waiting."""
        try:
            task = self._task(task_id)
        except ApiError:
            return
        if task.get("status") in (TASK_DONE, TASK_CANCELLED):
            return
        self._say(task_id, {"type": "decision", "actionId": action.get("id"), "tool": action.get("tool"), "decision": outcome.get("decision"),
                            "reason": outcome.get("reason"), "data": outcome.get("result") or outcome.get("error")})
        if self._update_task(task_id, only_if=TASK_PERSON, status=TASK_RUNNING) is not None:
            self._continue(task_id)

    def _finished(self, task: Mapping[str, Any], outcome: str) -> None:
        self._audit("assistant.finished", target={"type": "task", "id": task.get("id")}, details={
            "task": task.get("id"), "turns": task.get("turns"), "actions": task.get("changes"), "outcome": outcome,
            "costUsd": task.get("costUsd")})

    def _work(self, task_id: str) -> None:
        """The task loop: one model turn at a time, each a checked step, until the task finishes or waits for a person.

        Anything unexpected stops the task (it is said, and Continue carries it on), never leaving it running."""
        try:
            self._turns(task_id)
        except Exception as exc:  # noqa: BLE001 - the task is stopped and says so; the API's log gets the traceback
            from discovery_agent.diagnostics import record_unexpected, unexpected_message  # noqa: PLC0415

            reference = record_unexpected(f"the assistant's question {task_id}", exc)
            stopped = self._update_task(task_id, status=TASK_BLOCKED)
            if stopped is not None:
                self._say(task_id, {"type": "error", "text": unexpected_message("It stopped", exc, reference) + " Continue carries it on."})
                self._finished(stopped, "error")

    def _stop_task(self, task_id: str, text: str, outcome: str, kind: str = "notice") -> None:
        """Stop a running task, saying why (a cancelled one stays cancelled, and is not said again)."""
        stopped = self._update_task(task_id, status=TASK_BLOCKED)
        if stopped is not None:
            self._say(task_id, {"type": kind, "text": text})
            self._finished(stopped, outcome)

    def _turns(self, task_id: str) -> None:
        started = time.monotonic()
        # Seconds worked on it before (in earlier continuations): the time limit is the question's, not this run's.
        before = float(self._task(task_id).get("workSeconds") or 0.0)
        worked = lambda: before + time.monotonic() - started  # noqa: E731
        invalid = 0
        while True:
            task = self._task(task_id)
            if task.get("status") != TASK_RUNNING:
                return
            turns, changes = int(task.get("turns") or 0), int(task.get("changes") or 0)
            stop = (f"It took the most turns a question may ({talk.MAX_TURNS})." if turns >= talk.MAX_TURNS else
                    "It ran out of time." if worked() > talk.MAX_SECONDS else
                    f"It spent the most a question may (${talk.MAX_COST:.2f})." if float(task.get("costUsd") or 0) >= talk.MAX_COST else None)
            if stop:
                self._stop_task(task_id, f"Stopped: {stop} Ask again, more narrowly, to carry on.", "limit")
                return
            model, why = self._gate()
            if model is None:
                self._stop_task(task_id, f"The AI cannot be used now: {why}", "blocked")
                return
            sent = talk.prompt(str(task.get("question")), self._entries(task_id), self.registry, talk.MAX_TURNS - turns,
                               talk.MAX_CHANGES - changes)
            from discovery_agent.llm import claude_cli  # noqa: PLC0415

            try:
                reply = model.ask(sent, talk.STEP_SCHEMA, talk.SYSTEM)
            except claude_cli.ModelError as exc:
                self._stop_task(task_id, f"Claude could not be asked: {exc}", "model error", kind="error")
                return
            cost, estimated = talk.model_cost(reply, sent)
            if self._consult is not None:
                self._consult.record_spend("assistant", getattr(reply, "model", None), cost, _short_hash(sent))
            # What the turn cost is the question's whatever happened meanwhile; the rest only while it still runs (a
            # person may have cancelled it while the model was answering: then the answer is dropped).
            with self._task_lock:
                current = self._task(task_id)
                current.update(turns=turns + 1, costUsd=round(float(current.get("costUsd") or 0) + cost, 6),
                               estimated=bool(current.get("estimated")) or estimated, workSeconds=round(worked(), 1), updatedAt=now())
                self._save_task(current)
                if current.get("status") != TASK_RUNNING:
                    return
            step, problem = talk.parse_step(getattr(reply, "data", {}) or {}, self.registry)
            self._say(task_id, {"type": "step", **step})
            if problem:
                invalid += 1
                self._say(task_id, {"type": "error", "text": f"That step cannot be taken: {problem}"})
                if invalid >= 2:
                    self._stop_task(task_id, "Stopped: two steps in a row could not be taken.", "invalid steps")
                    return
                continue
            invalid = 0
            if step["kind"] == talk.READ_STEP:
                tool = self.registry.get(step["tool"])
                try:
                    data = tool.run(step["args"])
                    self._say(task_id, {"type": "result", "tool": tool.name, "ok": True, "data": capped(data)})
                except Exception as exc:  # noqa: BLE001 - a read that failed is said to the model, which carries on
                    self._say(task_id, {"type": "result", "tool": tool.name, "ok": False, "error": str(getattr(exc, "message", None) or exc)[:500]})
                continue
            if step["kind"] == talk.CHANGE_STEP:
                if changes >= talk.MAX_CHANGES:
                    self._say(task_id, {"type": "error", "text": f"No more changes may be proposed for this question ({talk.MAX_CHANGES})."})
                    continue
                if self._task(task_id).get("status") != TASK_RUNNING:
                    return  # cancelled while it worked: nothing is proposed for it
                action, why_not = self.propose(step["tool"], step["args"], step["message"], ASSISTANT, None, task_id)
                if action is None:
                    self._say(task_id, {"type": "result", "tool": step["tool"], "ok": False, "error": why_not})
                    continue
                self._say(task_id, {"type": "action", "actionId": action["id"], "tool": action["tool"], "tier": action["tier"],
                                    "preview": action.get("preview")})
                if self._update_task(task_id, status=TASK_PERSON, changes=changes + 1) is None and action.get("task") == task_id:
                    self._lapse(action, EXPIRED, "Its question was cancelled.")  # cancelled in the moment it was proposed
                return
            if step["kind"] == talk.ASK_STEP:
                if self._update_task(task_id, status=TASK_REPLY) is not None:
                    self._say(task_id, {"type": "ask", "text": step["message"]})
                return
            done = self._update_task(task_id, status=TASK_DONE, answer=step["message"])
            if done is not None:
                self._say(task_id, {"type": "answer", "text": step["message"]})
                self._finished(done, "answered")
            return


def _short_hash(text: str) -> str:
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_MARK = {plans.DONE: "done", plans.READY: "to do", plans.WAITING: "waiting", plans.IN_PROGRESS: "in progress",
         plans.FAILED: "FAILED", plans.WARN: "needs a look", plans.SKIPPED: "not applicable"}


def _approval_words(approval: Optional[Mapping[str, Any]]) -> str:
    """How a run was let write: signed off by a person, or approved by the accelerator under the rule in force."""
    a = approval or {}
    by = a.get("by") or {}
    if not by:
        return ""
    if by.get("kind") != context.PERSON:
        return f"; approved automatically under the sign-off rule in force ({a.get('policy') or 'the brief, E4'}), with no person's sign-off"
    return f"; {'declined' if a.get('decision') == 'rejected' else 'signed off'} by {by.get('id')}"


def _report_markdown(data: Mapping[str, Any]) -> str:
    brief = data.get("brief") or {}
    plan = data.get("executionPlan") or {}
    lines = ["# Migration sign-off report", "",
             f"Made {data.get('madeAt')} by {(data.get('madeBy') or {}).get('id')}, from the accelerator's own record.", ""]
    trail = data.get("trail") or {}
    head = str(trail.get("head") or "")
    verdict = "verifies" if trail.get("ok") else "does NOT verify"
    lines += [f"The audit trail holds {trail.get('records')} records; its hash chain {verdict}" + (f", head `{head[:16]}`" if head else "") + ".", ""]
    lines += ["## The migration brief", ""]
    if brief:
        lines.append(f"Version {brief.get('version')} (content hash `{str(brief.get('hash'))[:16]}`), approved by "
                     f"{(brief.get('approvedBy') or {}).get('id', '—')} on {brief.get('approvedAt') or '—'}"
                     + (". It no longer fits the estate as it is now." if brief.get("stale") else "."))
    else:
        lines.append("No brief is approved.")
    p = data.get("plan") or {}
    lines += ["", "## The plan", "", f"Plan {p.get('id') or '—'}: {p.get('objects') or 0} objects, readiness {p.get('readiness') or '—'}, "
              f"{p.get('blocking') or 0} blocking risk(s), recorded {p.get('createdAt') or '—'}."]
    lines += ["", "## The execution plan", "", f"Version {plan.get('version')} (hash `{str(plan.get('hash'))[:16]}`).", "",
              "| Step | Owner | Status | From the brief |", "|---|---|---|---|"]
    for s in plan.get("steps") or []:
        lines.append(f"| {s['title']} | {s['owner']} | {_MARK.get(s['status'], s['status'])} | {', '.join(s.get('answers') or []) or '—'} |")
    lines += ["", "## Runs", ""]
    for r in data.get("runs") or []:
        counts = r.get("counts") or {}
        lines.append(f"- Run {r.get('runId')}: {str(r.get('status')).replace('_', ' ')}, created {r.get('createdAt')}, "
                     f"{counts.get('COMPLETED', 0)} completed, {counts.get('FAILED', 0)} failed, {counts.get('DEFERRED', 0)} left for a person"
                     + _approval_words(r.get("approval")) + ".")
    if not data.get("runs"):
        lines.append("No run yet.")
    v = data.get("validation") or {}
    lines += ["", "## Validation", "", f"{v.get('state') or 'not run'}" + (f", finished {v.get('finishedAt')}: " + ", ".join(
        f"{n} {k.lower()}" for k, n in sorted((v.get("summary") or {}).items()) if n) if v.get("summary") else "") + "."]
    for m in data.get("mismatches") or []:
        lines.append(f"- MISMATCH {m.get('category')} {m.get('object')}: {m.get('detail') or m.get('target')}")
    left = data.get("leftForAPerson") or []
    lines += ["", "## Left for a person", ""]
    lines += [f"- {i['name']} ({i['type']}): {i.get('reason') or '—'}" for i in left] or ["Nothing in the current run."]
    return "\n".join(lines) + "\n"
