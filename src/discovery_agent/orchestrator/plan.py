"""The execution plan: what a migration does, in order, and who does each part. Pure and deterministic.

``build`` turns the approved brief, the recorded plan version and the discovery snapshot into steps. The same inputs
always give the same steps, so the plan's hash names it: a new version is made only when something it rests on moves
(the brief, the plan, the estate), and a person's task done under one version stays done in the next while the task
itself is unchanged (its own hash, ``Step.digest``).

Each step has

* an **owner**: a person (sign-offs, credentials, reviews, work by hand), the agent (what it may propose or, at *act on
  Dev*, do: start, resume, retry, draft, validate, commit), or the accelerator (checks it makes itself);
* a **kind**: a *check* the accelerator makes, a *task* a person marks done, an *action* a tool carries out, or the
  *report*;
* what it **waits for** (other steps), and the brief **answers** it came from.

``state_of`` says where each step stands now, from ``Live``: what the services report this moment. Nothing here
reads a service or a clock: the API gathers ``Live``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

PERSON, AGENT, SYSTEM = "person", "agent", "system"
CHECK, TASK, ACTION, REPORT = "check", "task", "action", "report"
#: The phases, in order, as the plan board shows them.
PHASES = (("readiness", "Readiness"), ("person", "Before the run"), ("prepare", "Prepare"), ("deliver", "Deliver"),
          ("validate", "Validate"), ("release", "Release"), ("cutover", "Cutover"), ("report", "Sign-off"))
#: Where a step stands: waiting for another; ready to do; being done; done; failed (or a check that does not pass);
#: needs a look (a check that warns); not applicable here.
WAITING, READY, IN_PROGRESS, DONE, FAILED, WARN, SKIPPED = "waiting", "ready", "in_progress", "done", "failed", "warn", "skipped"
STATUSES = (WAITING, READY, IN_PROGRESS, DONE, FAILED, WARN, SKIPPED)
#: The most objects a step lists by name; the count is always whole.
MAX_LISTED = 25


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Step:
    key: str
    phase: str
    title: str
    owner: str
    kind: str
    detail: str = ""
    depends: Tuple[str, ...] = ()
    answers: Tuple[str, ...] = ()
    #: What the step is about, exactly: the objects, the counts, the tool and its arguments. Part of its hash.
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        """The step's own hash: a person's mark carries over to a new plan version while this is unchanged."""
        return _digest([self.key, self.phase, self.title, self.owner, self.kind, self.detail, list(self.depends),
                        list(self.answers), dict(self.params)])

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "phase": self.phase, "title": self.title, "owner": self.owner, "kind": self.kind,
                "detail": self.detail, "depends": list(self.depends), "answers": list(self.answers),
                "params": dict(self.params), "digest": self.digest}


@dataclass(frozen=True)
class Inputs:
    """What the plan is built from: the brief in force, the recorded plan and the estate. Gathered by the API."""

    brief_version: Optional[int] = None
    brief_hash: Optional[str] = None
    #: The brief's settings (``consult.policy.Policy.settings``): empty without an approved brief.
    settings: Mapping[str, Any] = field(default_factory=dict)
    plan_version: Optional[str] = None
    plan_objects: int = 0
    snapshot: Optional[str] = None
    #: A flag (multiStatement, unconvertible, rebuild, lossy) -> the plan's object ids that carry it.
    flags: Mapping[str, Sequence[str]] = field(default_factory=dict)
    #: Object id -> its name, for the ids above.
    names: Mapping[str, str] = field(default_factory=dict)
    #: The connections (linked services, the pool's) whose credentials a run of this plan asks for.
    credentials: Sequence[str] = ()
    #: SQL users the brief maps to no Microsoft Entra principal.
    unmapped_users: Sequence[str] = ()
    #: Triggers and integration runtimes someone sets up or switches on.
    manual_setup: int = 0
    #: The plan loads table data.
    loads_data: bool = False

    def identity(self) -> Dict[str, Any]:
        return {"briefVersion": self.brief_version, "briefHash": self.brief_hash, "planVersion": self.plan_version,
                "snapshot": self.snapshot}


@dataclass(frozen=True)
class ExecutionPlan:
    steps: Tuple[Step, ...]
    inputs: Mapping[str, Any]
    hash: str

    def step(self, key: str) -> Optional[Step]:
        return next((s for s in self.steps if s.key == key), None)

    def to_dict(self) -> Dict[str, Any]:
        return {"hash": self.hash, "inputs": dict(self.inputs), "steps": [s.to_dict() for s in self.steps]}


def _listed(ids: Sequence[str], names: Mapping[str, str]) -> List[str]:
    return [names.get(i, i) for i in sorted(ids, key=lambda i: names.get(i, i).lower())[:MAX_LISTED]]


def _count(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


_DELIVERY = {"one_run": "straight through", "stop_on_failures": "wave by wave, stopping at a wave with failures",
             "signoff_each_wave": "wave by wave, a person signing off each wave"}
_DEPTH = {"compare": "comparing definitions, columns and row counts", "probe": "also querying and compiling",
          "run": "also running pipelines, notebooks and Spark jobs"}


def build(inputs: Inputs) -> ExecutionPlan:
    """The execution plan for these inputs. Deterministic: the same inputs give the same steps, in the same order."""
    s = inputs.settings
    get = s.get
    steps: List[Step] = []

    def add(step: Step) -> None:
        steps.append(step)

    # -- readiness: what the accelerator checks before anything is written -------------------------------------------
    add(Step("ready.source", "readiness", "The Synapse source is connected", SYSTEM, CHECK,
             "Discovery read it; a run that loads data or makes shortcuts reads the pool again."))
    add(Step("ready.target", "readiness", "The Fabric target is connected, on a capacity", SYSTEM, CHECK,
             "Fabric items are made in the target workspace; without a capacity it holds none."))
    add(Step("ready.discovery", "readiness", "Discovery read every part of the source", SYSTEM, CHECK,
             "A part it could not read is a contradiction in the brief (K5): kept with a reason, or discovery is run again."))
    add(Step("ready.brief", "readiness", "The migration brief is approved, and still fits the estate", SYSTEM, CHECK,
             "Every run is checked against it; a required consultation refuses a run without one."))
    add(Step("ready.plan", "readiness", "A plan is recorded, with no blocking risk", SYSTEM, CHECK,
             "The planner's analysis of the plan Migrate runs.",
             params={"planVersion": inputs.plan_version, "objects": inputs.plan_objects}))
    identity = get("operations.identity")
    if identity:
        add(Step("ready.identity", "readiness",
                 f"Migrate signs in to Fabric as {'a service principal' if identity == 'service_principal' else 'a person'}",
                 SYSTEM, CHECK, "The identity that creates an item owns it, and schedules and pipelines run with its access.",
                 answers=("E2",), params={"identity": identity}))
    residency = str(get("governance.residency") or "").strip()
    if residency:
        add(Step("ready.residency", "readiness", f"The target capacity is in {residency}", PERSON, TASK,
                 "OneLake keeps the data in the capacity's region. Check it in the Fabric admin portal, then mark this done.",
                 answers=("G3",), params={"region": residency}))

    # -- what a person does before the run -------------------------------------------------------------------------------
    gate: List[str] = []  # what the run waits for, beyond the readiness checks
    if residency:
        gate.append("ready.residency")  # nothing is written before a person has checked where the data will be kept
    if inputs.credentials:
        add(Step("person.credentials", "person", f"Enter the credentials for {_count(len(inputs.credentials), 'connection')} in Plan",
                 PERSON, TASK, "Sent once with the run, held in memory for it and never kept. The agent never enters them.",
                 answers=("C9", "G5"), params={"connections": sorted(inputs.credentials)}))
        gate.append("person.credentials")
    if inputs.unmapped_users and get("security.migrate") is not False:
        add(Step("person.sqlUsers", "person", f"Map {_count(len(inputs.unmapped_users), 'SQL user')} to Microsoft Entra principals",
                 PERSON, TASK, "Fabric has no SQL authentication: a SQL user left unmapped is listed for a person, with its statements.",
                 answers=("G2U",), params={"users": sorted(inputs.unmapped_users)}))
    lossy = list(inputs.flags.get("lossy") or [])
    if lossy and get("types.lossy") == "review" and inputs.loads_data:
        add(Step("person.lossy", "person", f"Review {_count(len(lossy), 'table')} whose types change, before their data loads",
                 PERSON, TASK, "7-digit time, datetimeoffset, rowversion, XML and spatial columns are stored with Fabric's closest types.",
                 answers=("D4",), params={"objects": _listed(lossy, inputs.names), "count": len(lossy)}))
        gate.append("person.lossy")
    multi = list(inputs.flags.get("multiStatement") or [])
    if multi and get("code.multiStatement") == "manual":
        add(Step("person.multiStatement", "person", f"Rewrite {_count(len(multi), 'multi-statement function')} by hand",
                 PERSON, TASK, "Each becomes an inline table-valued function or a view, then the run creates it.",
                 answers=("D3",), params={"objects": _listed(multi, inputs.names), "count": len(multi)}))
    rejected = list(inputs.flags.get("unconvertible") or [])
    if rejected and get("code.unconvertible") == "by_hand":
        add(Step("person.unconvertible", "person", f"Rewrite {_count(len(rejected), 'object')} Fabric rejects, by hand",
                 PERSON, TASK, "What the rules cannot fix: a person's rewrite, accepted as an LLM transform would be.",
                 answers=("D5",), params={"objects": _listed(rejected, inputs.names), "count": len(rejected)}))
    rebuild = list(inputs.flags.get("rebuild") or [])
    if rebuild and get("code.rebuild") == "by_hand":
        owner = str(get("owners.rebuild") or "").strip()
        add(Step("person.rebuild", "person", f"Rebuild {_count(len(rebuild), 'pipeline or notebook', 'pipelines and notebooks')} by hand",
                 PERSON, TASK, "No Fabric equivalent as they are." + (f" Who: {owner}." if owner else ""),
                 answers=("A4", "A4O"), params={"objects": _listed(rebuild, inputs.names), "count": len(rebuild)}))

    # -- prepare: LLM drafts the brief sends to AI ----------------------------------------------------------------------
    to_draft: List[str] = []
    if get("llm.consent") is True:
        if get("code.multiStatement") == "llm":
            to_draft += multi
        if get("code.unconvertible") == "ai":
            to_draft += rejected
    to_draft = sorted(set(to_draft))
    if to_draft:
        answers = tuple(a for a, on in (("D3", get("code.multiStatement") == "llm"), ("D5", get("code.unconvertible") == "ai")) if on)
        add(Step("prepare.drafts", "prepare", f"Draft {_count(len(to_draft), 'object')} with AI", AGENT, ACTION,
                 "Each draft is sent the migration context (MIGRATION_CONTEXT.md) and checked; it is used only once a person accepts it.",
                 answers=("D1",) + answers, params={"tool": "drafts.generate", "objects": to_draft[:50], "count": len(to_draft)}))
        add(Step("prepare.review", "prepare", "Review and accept each draft", PERSON, TASK,
                 "Accepted drafts are what the run creates; a draft nobody accepts leaves the rules' output.",
                 depends=("prepare.drafts",), answers=("D1",), params={"objects": to_draft[:50], "count": len(to_draft)}))
        gate.append("prepare.review")

    # -- deliver ------------------------------------------------------------------------------------------------------------
    delivery = "signoff_each_wave" if get("run.waveGate") else "stop_on_failures" if get("run.stopOnFailure") else "one_run" \
        if "run.stopOnFailure" in s else None
    add(Step("deliver.run", "deliver", "Run the migration" + (f", {_DELIVERY[delivery]}" if delivery else ""), AGENT, ACTION,
             "Started with the brief's options; a run that changes what Fabric has, or any run in production, waits for a "
             "person's sign-off.",
             depends=("ready.source", "ready.target", "ready.brief", "ready.plan") + tuple(gate), answers=("A0", "A6", "E4"),
             params={"tool": "run.start", "delivery": delivery, "planVersion": inputs.plan_version}))
    add(Step("deliver.fix", "deliver", "Fix what failed, and retry it", AGENT, ACTION,
             "Each failure's category says what may be done: a retry, a draft, a person's fix. A retry after a fix waits for a person.",
             depends=("deliver.run",), params={"tool": "run.retry"}))
    if inputs.manual_setup:
        add(Step("person.setup", "person", f"Set up {_count(inputs.manual_setup, 'trigger or integration runtime', 'triggers and integration runtimes')} by hand",
                 PERSON, TASK, "Event and tumbling-window triggers and integration runtimes have no automatic equivalent"
                 + (", and the schedules are created switched off (E5)" if get("schedules.enabled") != "match" else "") + "."
                 + (f" Who: {get('owners.setup')}." if str(get("owners.setup") or "").strip() else ""),
                 depends=("deliver.run",), answers=("E5", "E5O"), params={"count": inputs.manual_setup}))

    # -- validate -----------------------------------------------------------------------------------------------------------
    depth = get("validate.depth") or "probe"
    add(Step("validate.run", "validate", f"Validate, {_DEPTH.get(depth, depth)}", AGENT, ACTION,
             "Synapse and Fabric compared object by object; a run only at 'also run them' changes anything in Fabric.",
             depends=("deliver.run",), answers=("H1",), params={"tool": "validate.start", "depth": depth}))
    add(Step("validate.mismatches", "validate", "Resolve each mismatch, or accept it with a reason", PERSON, TASK,
             "A mismatch goes into the fix loop; one that is explained is accepted with the reason.",
             depends=("validate.run",)))

    # -- release ------------------------------------------------------------------------------------------------------------
    if get("release.gitCommit") is True:
        scope = "everything waiting in the workspace" if get("release.commitScope") == "all" else "only what the run made"
        add(Step("release.commit", "release", f"Commit to the Fabric Git branch: {scope}", AGENT, ACTION,
                 "Made after each completed run; the pull request link follows.",
                 depends=("deliver.run",), answers=("E0", "E0S"), params={"tool": "git.commit", "scope": get("release.commitScope") or "run"}))
        add(Step("release.review", "release", "Review and merge the pull request", PERSON, TASK,
                 "Promotion beyond Dev is from Git, by a person." + (f" The path: {get('release.path')}." if get("release.path") else ""),
                 depends=("release.commit",), answers=("E7",)))

    # -- cutover ------------------------------------------------------------------------------------------------------------
    if get("data.sync") is True and get("data.deletes") == "reload":
        add(Step("cutover.reload", "cutover", "Reload the synced tables in full, removing rows deleted in Synapse", PERSON, TASK,
                 "A run with Replace their rows (C6) for the synced tables: it empties each and loads it again. It asks for sign-off.",
                 depends=("validate.run",), answers=("C4",)))
    if get("cutover.parallelRun") is True:
        cycles = int(get("cutover.parallelCycles") or 3)
        add(Step("cutover.parallel", "cutover", f"Run Synapse and Fabric side by side for {_count(cycles, 'cycle')}, comparing their results",
                 PERSON, TASK, "Both run on their schedules; the results are compared each cycle before Synapse is switched off.",
                 depends=("validate.run",), answers=("H2", "H2A"), params={"cycles": cycles}))
    window = str(get("release.cutover") or "").strip()
    if window:
        add(Step("cutover.window", "cutover", "Cut over in the agreed window", PERSON, TASK, f"The window and what a rollback means: {window}",
                 depends=("validate.run",), answers=("H3",)))

    # -- the sign-off report ---------------------------------------------------------------------------------------------
    add(Step("report.signoff", "report", "The sign-off report: brief, plan, runs, validation, what was left for a person, the trail's hash",
             SYSTEM, REPORT, "Made from the record as it stands; download it for the client's sign-off.", depends=("deliver.run",)))

    identity_of_inputs = inputs.identity()
    body = {"inputs": identity_of_inputs, "steps": [s.digest for s in steps]}
    return ExecutionPlan(tuple(steps), identity_of_inputs, _digest(body))


# -- where each step stands --------------------------------------------------------------------------------------

@dataclass
class Live:
    """What the services report now, for ``state_of``. Gathered by the API, never cached between wake-ups."""

    source_connected: bool = False
    target_connected: bool = False
    capacity: Optional[bool] = None
    discovery_state: str = "idle"
    discovery_complete: Optional[bool] = None
    brief_version: Optional[int] = None
    brief_stale: bool = False
    required: bool = True
    plan_version: Optional[str] = None
    plan_blocking: Optional[int] = None
    #: The current run (``MigrationService.state``), or None.
    run: Optional[Mapping[str, Any]] = None
    #: The last validation (``MigrationService.validation_state``).
    validation: Mapping[str, Any] = field(default_factory=dict)
    #: Object id -> the state of its LLM transform (none, queued, running, failed, draft, accepted).
    drafts: Mapping[str, str] = field(default_factory=dict)
    #: Step key -> the latest mark a person made on it: {done, digest, by, at, note}.
    marks: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: What kind of identity Fabric was signed in with: person, service_principal, or None when it cannot be told.
    fabric_identity: Optional[str] = None
    #: The connections the current run was given credentials for.
    run_credentials: Sequence[str] = ()


@dataclass
class StepState:
    status: str
    message: str = ""
    #: Done by a person's mark, and who.
    marked_by: Optional[Mapping[str, Any]] = None


def _run_status(run: Optional[Mapping[str, Any]]) -> str:
    return str((run or {}).get("status") or "idle")


def state_of(plan: ExecutionPlan, live: Live) -> Dict[str, StepState]:
    """Where every step stands. Checks are always evaluated; anything else waits until what it depends on is done."""
    out: Dict[str, StepState] = {}
    run = live.run or None
    status = _run_status(run)
    for step in plan.steps:
        mark = live.marks.get(step.key) or {}
        marked = bool(mark.get("done")) and mark.get("digest") == step.digest
        if step.kind == CHECK:
            out[step.key] = _check(step, live)
            # Only what the accelerator cannot tell is confirmed by hand: a check it sees failing is fixed, never confirmed.
            if marked and out[step.key].status == WARN:
                out[step.key] = StepState(DONE, "Confirmed by a person.", mark.get("by"))
            continue
        # A check that only warns (an advisory brief missing, a part of discovery kept) never holds anything up.
        waiting = [d for d in step.depends if d in out and out[d].status not in (DONE, SKIPPED, WARN)]
        if step.kind == TASK:
            computed = _task(step, live)
            if computed is not None:
                out[step.key] = computed
            elif marked:
                out[step.key] = StepState(DONE, mark.get("note") or "Marked done.", mark.get("by"))
            else:
                out[step.key] = StepState(WAITING if waiting else READY,
                                          f"Waits for: {', '.join(waiting)}." if waiting else "For a person to do, then mark done.")
            continue
        if step.kind == REPORT:
            out[step.key] = StepState(WAITING if waiting else READY, "" if waiting else "Download it from the plan board.")
            continue
        out[step.key] = _action(step, live, run, status, waiting)
    return out


def _check(step: Step, live: Live) -> StepState:
    key = step.key
    if key == "ready.source":
        return StepState(DONE, "Connected.") if live.source_connected else StepState(
            FAILED if live.discovery_state not in ("completed", "completed_with_warnings") else WARN,
            "Not connected: sign in on Connections. Discovery results are kept, but a run that reads the pool needs it.")
    if key == "ready.target":
        if not live.target_connected:
            return StepState(FAILED, "The Fabric target is not connected: connect it on Connections.")
        if live.capacity is False:
            return StepState(FAILED, "The workspace is on no capacity, so it cannot hold Fabric items.")
        return StepState(DONE, "Connected.")
    if key == "ready.discovery":
        if live.discovery_state not in ("completed", "completed_with_warnings"):
            return StepState(FAILED, "No discovery: run it on Discover.")
        return StepState(DONE, "Every part was read.") if live.discovery_complete is not False else StepState(
            WARN, "Some parts could not be read: keep the contradiction in Consult (K5) or run discovery again.")
    if key == "ready.brief":
        if live.brief_version is None:
            return StepState(FAILED if live.required else WARN, "No brief is approved: answer the questions in Consult, submit the brief "
                             "and have it approved." + ("" if live.required else " (Advisory: runs start without one.)"))
        if live.brief_stale:
            return StepState(FAILED if live.required else WARN, f"Version {live.brief_version} no longer fits: reconfirm it in Consult.")
        return StepState(DONE, f"Version {live.brief_version} is in force.")
    if key == "ready.plan":
        if not live.plan_version:
            return StepState(FAILED, "No plan is recorded: build it on Plan.")
        if live.plan_blocking:
            return StepState(FAILED, f"Plan {live.plan_version} has {_count(live.plan_blocking, 'blocking risk')}: resolve them on Plan.")
        return StepState(DONE, f"Plan {live.plan_version}, no blocking risk.")
    if key == "ready.identity":
        wanted = step.params.get("identity")
        if live.fabric_identity is None:
            return StepState(WARN, "The Fabric sign-in does not say which kind of identity it is: check it, then mark this done.")
        if live.fabric_identity != wanted:
            return StepState(FAILED, f"Fabric is signed in as {'a service principal' if live.fabric_identity == 'service_principal' else 'a person'}, "
                             f"and the brief (E2) says {'a service principal' if wanted == 'service_principal' else 'a person'}.")
        return StepState(DONE, "As the brief says.")
    return StepState(WARN, "Not a check this build knows.")


def _task(step: Step, live: Live) -> Optional[StepState]:
    """A person's task the accelerator can tell is done by itself; None when only a person's mark says so."""
    if step.key == "person.credentials":
        needed = set(step.params.get("connections") or [])
        if needed and needed <= set(live.run_credentials):
            return StepState(DONE, "Given with the current run.")
    if step.key == "person.sqlUsers":
        return None
    if step.key == "prepare.review":
        objects = list(step.params.get("objects") or [])
        accepted = [o for o in objects if live.drafts.get(o) == "accepted"]
        if objects and len(accepted) == len(objects):
            return StepState(DONE, "Every draft is accepted.")
        drafted = [o for o in objects if live.drafts.get(o) in ("draft", "accepted")]
        if drafted:
            return StepState(IN_PROGRESS, f"{len(accepted)} of {len(objects)} accepted; {len(drafted) - len(accepted)} to review.")
        return StepState(WAITING, "Waits for the drafts.")
    if step.key == "validate.mismatches":
        summary = (live.validation or {}).get("summary") or {}
        if (live.validation or {}).get("state") == "completed" and not summary.get("MISMATCH"):
            return StepState(DONE, "No mismatch.")
    return None


def _action(step: Step, live: Live, run: Optional[Mapping[str, Any]], status: str, waiting: List[str]) -> StepState:
    key = step.key
    if key == "prepare.drafts":
        objects = list(step.params.get("objects") or [])
        done = [o for o in objects if live.drafts.get(o) in ("draft", "accepted")]
        working = [o for o in objects if live.drafts.get(o) in ("queued", "running")]
        if objects and len(done) == len(objects):
            return StepState(DONE, "Every object has a draft.")
        if working:
            return StepState(IN_PROGRESS, f"{len(working)} being drafted.")
        return StepState(READY, f"{len(objects) - len(done)} to draft." if not done else f"{len(done)} drafted, {len(objects) - len(done)} to go.")
    if key == "deliver.run":
        if status == "idle" or status in ("cancelled", "rejected"):
            return StepState(WAITING, f"Waits for: {', '.join(waiting)}.") if waiting else StepState(READY, "Ready to start.")
        if status in ("queued", "running"):
            return StepState(IN_PROGRESS, f"Run {run.get('runId')} is working.")
        if status == "awaiting_approval":
            return StepState(IN_PROGRESS, f"Run {run.get('runId')} waits for a person's sign-off.")
        if status == "paused":
            reason = run.get("pausedReason")
            words = {"awaiting_wave_signoff": f"waits for a person to sign off wave {run.get('waitingWave')}",
                     "interrupted": "was cut short by a restart: Resume carries it on",
                     "halted_on_failures": "stopped at a wave with failures", "needs_input": "needs credentials again",
                     "by_person": "was paused"}.get(str(reason), "is paused")
            return StepState(IN_PROGRESS, f"Run {run.get('runId')} {words}.")
        if status == "completed":
            return StepState(DONE, f"Run {run.get('runId')} completed.")
        if status in ("completed_with_failures", "failed"):
            return StepState(DONE if status == "completed_with_failures" else FAILED,
                             f"Run {run.get('runId')} finished with failures: see Fix what failed." if status == "completed_with_failures"
                             else f"Run {run.get('runId')} stopped: see its log.")
    if key == "deliver.fix":
        if waiting:
            return StepState(WAITING, "Waits for the run.")
        failed = int((run or {}).get("failed") or 0) + int((run or {}).get("blocked") or 0)
        if not run or status == "idle":
            return StepState(WAITING, "Waits for the run.")
        if failed:
            return StepState(READY, f"{_count(failed, 'object')} failed or blocked.")
        return StepState(DONE if status == "completed" else WAITING, "Nothing failed." if status == "completed" else "Waits for the run to finish.")
    if key == "validate.run":
        state = (live.validation or {}).get("state") or "idle"
        if state == "running":
            return StepState(IN_PROGRESS, "Validation is running.")
        if waiting:
            return StepState(WAITING, "Waits for the run.")
        if state == "completed":
            summary = (live.validation or {}).get("summary") or {}
            return StepState(DONE, ", ".join(f"{v} {k.lower()}" for k, v in sorted(summary.items()) if v) or "Validated.")
        if state == "failed":
            return StepState(FAILED, str(((live.validation or {}).get("error") or {}).get("message") or "Validation failed."))
        return StepState(READY, "Ready to validate.")
    if key == "release.commit":
        git = (run or {}).get("git") or {}
        state = git.get("state")
        if waiting:
            return StepState(WAITING, "Waits for the run.")
        if state in ("committed", "nothing"):
            return StepState(DONE, str(git.get("message") or "Committed."))
        if state == "committing":
            return StepState(IN_PROGRESS, "Committing.")
        if state in ("failed", "blocked", "skipped"):
            return StepState(FAILED, str(git.get("message") or "The commit was not made."))
        return StepState(READY, "Commit the run's items.")
    return StepState(WAITING if waiting else READY, "")
