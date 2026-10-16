"""The one registry of what the agent may do: the same service calls the page's buttons make, each with a tier.

| Tier | Who may trigger it |
|---|---|
| read | always, at once: it changes nothing |
| prepare | writes nothing to Fabric (an LLM draft, a compare-only validation): always a request |
| change | writes to the target workspace (start, pause, resume, retry, a probing validation, a Git commit): a request, but at *act on Dev* the agent may resume an interrupted pass and retry failures that pass (timeouts, throttling) on its own, a few times |
| high | overwrites or really runs things (update in place, replace rows, run pipelines in Validate, close a run, discover again): always a request, each confirmed on its own. A start whose brief options overwrite is one of these, never a plain start |
| person | approving, accepting a draft, entering credentials, answering the brief, confirming a step: never the agent. They are not tools here at all |

Every tool has typed arguments, a ``check`` (why it cannot run now: re-checked when a person confirms it, so a request
that went stale is refused rather than carried out), a ``bind`` (what it acts on, as it stands when it is proposed: the
run and its version, the plan and what a start would send; a confirmed request runs only while that is still so), a
``preview`` (exactly what it would do, for the action card) and an idempotency ``key`` (one open request for the same
thing). Nothing here writes to Synapse, runs arbitrary SQL or a command, reads a credential, deletes a Fabric item or
touches the audit trail.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.migration import run_state

READ, PREPARE, CHANGE, HIGH, PERSON = "read", "prepare", "change", "high", "person"
TIERS = (READ, PREPARE, CHANGE, HIGH, PERSON)
#: What a person does, which no tool does: listed so the assistant and the page can say so.
PERSON_ONLY = ("approve a brief, a plan or a run", "accept or discard an LLM draft", "enter credentials",
               "answer or change the brief's questions (the agent may propose an answer)", "confirm a step of the journey")
#: The most a read tool returns to the model, in characters; the page gets it whole.
MAX_READ = 6000
#: The argument a confirmed request's binding is handed to its tool under: never one a model can give (``clean``
#: refuses any argument a tool does not declare).
BOUND = "_bound"
#: A quoted value in an error Fabric or SQL returned ('John Smith', "2024-01-01"): with G4 at minimal, the assistant's
#: model is given the error without them, as a draft is given no names.
_QUOTED = re.compile(r"'[^'\n]{1,200}'|\"[^\"\n]{1,200}\"")


class ToolError(ValueError):
    """The arguments do not fit the tool; the message says how."""


@dataclass(frozen=True)
class Arg:
    kind: str  # string | integer | boolean | strings
    description: str
    required: bool = False
    maximum: Optional[int] = None


@dataclass
class Tool:
    name: str
    tier: str
    description: str
    run: Callable[[Mapping[str, Any]], Any]
    args: Mapping[str, Arg] = field(default_factory=dict)
    #: Why it cannot run now (None when it can): checked when it is proposed, and again when a person confirms it.
    check: Callable[[Mapping[str, Any]], Optional[str]] = lambda args: None
    #: Exactly what it would do, for the action card.
    preview: Callable[[Mapping[str, Any]], Dict[str, Any]] = lambda args: {}
    #: One open request for the same thing: the tool and what it acts on.
    key: Callable[[Mapping[str, Any]], str] = lambda args: ""
    #: What it acts on, as it stands now: kept with a request when it is proposed, and compared when a person confirms
    #: it. A request for run 004 never closes run 005, and a start proposed for plan P001 never starts P002.
    bind: Callable[[Mapping[str, Any]], Dict[str, Any]] = lambda args: {}

    def clean(self, raw: Any) -> Dict[str, Any]:
        """The arguments, checked against the tool's: unknown ones refused, required ones present, kinds right."""
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ToolError(f"{self.name} takes its arguments as an object.")
        unknown = sorted(set(raw) - set(self.args))
        if unknown:
            raise ToolError(f"{self.name} has no argument {', '.join(unknown)}.")
        out: Dict[str, Any] = {}
        for name, spec in self.args.items():
            if name not in raw or raw[name] in (None, ""):
                if spec.required:
                    raise ToolError(f"{self.name} needs {name}: {spec.description}")
                continue
            value = raw[name]
            if spec.kind == "string":
                if not isinstance(value, str) or len(value) > 512:
                    raise ToolError(f"{name} is text, at most 512 characters.")
                out[name] = value.strip()
            elif spec.kind == "integer":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (spec.maximum is not None and value > spec.maximum):
                    raise ToolError(f"{name} is a whole number" + (f" up to {spec.maximum}." if spec.maximum is not None else "."))
                out[name] = value
            elif spec.kind == "boolean":
                if not isinstance(value, bool):
                    raise ToolError(f"{name} is true or false.")
                out[name] = value
            elif spec.kind == "strings":
                if not isinstance(value, list) or not all(isinstance(v, str) and 0 < len(v) <= 512 for v in value):
                    raise ToolError(f"{name} is a list of texts.")
                if spec.maximum is not None and len(value) > spec.maximum:
                    raise ToolError(f"{name} takes at most {spec.maximum}.")
                out[name] = list(dict.fromkeys(value))
            else:
                raise ToolError(f"{name}: not an argument kind ({spec.kind}).")
        return out

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "tier": self.tier, "description": self.description,
                "args": {k: {"kind": a.kind, "description": a.description, "required": a.required, "max": a.maximum}
                         for k, a in self.args.items()}}


def capped(value: Any, limit: int = MAX_READ) -> str:
    """A read tool's result as the model is given it: JSON, redacted, and cut to size, saying so."""
    from discovery_agent.diagnostics import redacted  # noqa: PLC0415 - diagnostics imports the connection layer

    text = redacted(json.dumps(value, ensure_ascii=False, default=str))
    return text if len(text) <= limit else text[:limit] + f"… ({len(text) - limit:,} more characters left out)"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def moved(before: Mapping[str, Any], now: Mapping[str, Any]) -> Optional[str]:
    """Why a request no longer acts on what its card showed (its binding then, and now), or None when it still does."""
    if dict(before or {}) == dict(now or {}):
        return None
    if before.get("runId") != now.get("runId"):
        return (f"It was for run {before.get('runId') or '(none)'}, and the current run is {now.get('runId') or '(none)'} now. "
                "Ask again for the run as it is.")
    if before.get("plan") != now.get("plan"):
        return f"It was for plan {before.get('plan') or '(none)'}, and plan {now.get('plan') or '(none)'} is the latest now."
    if before.get("after") != now.get("after"):
        return f"A run was started since it was proposed (run {now.get('after')})."
    if before.get("subject") != now.get("subject"):
        return "What the run would do changed since it was proposed (the plan's objects, or the brief's options)."
    if before.get("version") != now.get("version"):
        return f"Run {now.get('runId')} moved on since it was proposed: read it again."
    if before.get("snapshot") != now.get("snapshot"):
        return "Discovery was run again since it was proposed."
    return "What it acts on changed since it was proposed."


def without_values(text: Any) -> Any:
    """An error's text without the values it quotes (G4 at minimal); anything else as it is."""
    return _QUOTED.sub("'…'", text) if isinstance(text, str) else text


@dataclass
class Services:
    """What the tools call: the same services the routes call."""

    session: Any
    migration: Any
    consult: Any = None
    transforms: Any = None
    fabric: Any = None
    #: The orchestrator's own reads (the plan's steps), set by it.
    steps: Optional[Callable[[], Any]] = None


def _try(call: Callable[[], Any]) -> Tuple[Any, Optional[str]]:
    """(result, None), or (None, why) when the service refused: what a check reads a service with."""
    try:
        return call(), None
    except Exception as exc:  # noqa: BLE001 - a refusal is the answer here
        return None, str(getattr(exc, "message", None) or exc)


class Registry:
    """Every tool, bound to the services. Built once per orchestrator; the services hold the project."""

    def __init__(self, services: Services) -> None:
        self.s = services
        self._tools: Dict[str, Tool] = {}
        for tool in self._build():
            self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self, tiers: Sequence[str] = TIERS) -> List[str]:
        return [t.name for t in self._tools.values() if t.tier in tiers]

    def describe(self, tiers: Sequence[str] = TIERS) -> List[Dict[str, Any]]:
        return [t.describe() for t in self._tools.values() if t.tier in tiers]

    # -- what the tools read -----------------------------------------------------------------------------------

    def _run(self) -> Optional[Dict[str, Any]]:
        state = self.s.migration.state()
        return state if state.get("runId") else None

    def _plan(self) -> Optional[Dict[str, Any]]:
        return self.s.migration.latest_plan()

    def _brief(self) -> Any:
        if self.s.consult is None:
            return None
        try:
            return self.s.consult.in_force()
        except Exception:  # noqa: BLE001 - no brief that can be read is no brief
            return None

    def _start_body(self, overrides: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """A run of the latest recorded plan, with the options it was recorded with, made to match the brief where the
        brief decides them: what the agent starts. None without a recorded plan."""
        plan = self._plan()
        if not plan:
            return None
        options = dict(plan.get("options") or {})
        brief = self._brief()
        if brief is not None:
            from discovery_agent.migration import capabilities  # noqa: PLC0415

            options.update(brief.run_options(capabilities.STAGE_KEYS))
        options.update(overrides or {})
        return {"items": [dict(i) for i in plan.get("items") or []], "options": options}

    def _start_check(self, overrides: Optional[Mapping[str, Any]] = None) -> Optional[str]:
        body = self._start_body(overrides)
        if body is None:
            return "No plan is recorded yet: build it on Plan (recording it is a person's step)."
        run = self._run()
        if run and run.get("status") in run_state.WORKING:
            return "A run is already working."
        if run and run.get("status") not in (run_state.COMPLETED, run_state.CANCELLED, run_state.REJECTED):
            return f"Run {run.get('runId')} is {str(run.get('status')).replace('_', ' ')}: carry it on, or close it, first."
        analysis, why = _try(lambda: self.s.migration.analyze({k: v for k, v in body.items()}))
        if why:
            return why
        blocking = [r for r in analysis.get("risks") or [] if r.get("severity") == "BLOCKING"]
        if blocking:
            return f"The plan has {len(blocking)} blocking risk(s): {blocking[0].get('message')}"
        needs = sorted({o for r in analysis.get("risks") or [] if r.get("code") == "NEEDS_CREDENTIALS" for o in r.get("objects") or []})
        if needs:
            return (f"{len(needs)} connection(s) need credentials, which only a person enters (in Plan, sent with the run): "
                    "a person starts this run.")
        differs = (analysis.get("brief") or {}).get("differences") or []
        if differs and not (analysis.get("brief") or {}).get("explained"):
            return f"The run would differ from the approved brief: {differs[0].get('message')} A person decides that."
        return None

    def _overwrites(self, overrides: Optional[Mapping[str, Any]] = None) -> List[str]:
        """What a start with these options would change of what Fabric already has (``approvals.reasons``)."""
        from discovery_agent.state import approvals  # noqa: PLC0415

        options = (self._start_body(overrides) or {}).get("options") or {}
        return approvals.reasons({"existing": options.get("existing", "skip"), "dataMode": options.get("dataMode", "if_empty")},
                                 list(options.get("stages") or []))

    def start_tool(self) -> str:
        """The tool that starts the latest plan as the brief's options make it: a plain start only when it changes
        nothing already in Fabric; one that replaces rows or updates in place is high-tier, confirmed on its own."""
        options = (self._start_body() or {}).get("options") or {}
        if options.get("dataMode") == "replace" and "data" in (options.get("stages") or []):
            return "run.start_replace"
        if options.get("existing") == "update":
            return "run.start_update"
        return "run.start"

    def _plain_start_check(self) -> Optional[str]:
        why = self._start_check()
        if why:
            return why
        over = self._overwrites()
        if over:
            return (f"With the brief's options {'; and '.join(over)}: so it is started with {self.start_tool()}, a "
                    "high-tier request confirmed on its own, never as a plain start.")
        return None

    def _start_binding(self, overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """A start acts on the latest plan, as the brief's options make it, after the run current now."""
        body = self._start_body(overrides)
        return {"plan": (self._plan() or {}).get("id"), "subject": _digest(body) if body else None,
                "after": (self._run() or {}).get("runId")}

    def _start_preview(self, overrides: Optional[Mapping[str, Any]] = None, label: str = "Start a run") -> Dict[str, Any]:
        body = self._start_body(overrides) or {}
        plan = self._plan() or {}
        options = body.get("options") or {}
        brief = self._brief()
        from discovery_agent.state import approvals  # noqa: PLC0415

        rule = approvals.rule_for_policy(brief)
        settings = {"existing": options.get("existing", "skip"), "dataMode": options.get("dataMode", "if_empty")}
        stages = list(options.get("stages") or [])
        over = approvals.reasons(settings, stages)
        signoff = approvals.required(settings, stages, rule=rule)
        if signoff:
            words = f"A person signs it off before it writes. {signoff}"
        elif over:
            words = (f"No sign-off: the rule in force ({rule}) lets the accelerator approve it, recorded as the accelerator's, "
                     f"although {'; and '.join(over)}.")
        else:
            words = "No sign-off: it only adds what Fabric does not have yet. The accelerator approves it, recorded as its own."
        return {"summary": f"{label} of plan {plan.get('id') or '?'}: {len(body.get('items') or [])} object(s), "
                           f"{len(stages)} stage(s).",
                "plan": plan.get("id"), "objects": len(body.get("items") or []), "stages": options.get("stages"),
                "options": {k: v for k, v in options.items() if k not in ("dataFilters", "storageRoutes")},
                "overwrites": over, "signoff": words}

    def _run_binding(self, with_version: bool = True) -> Dict[str, Any]:
        run = self._run() or {}
        return {"runId": run.get("runId"), **({"version": run.get("version")} if with_version else {})}

    def _bound_run(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        """The current run, refused when it is not the one the request was confirmed for."""
        bound = args.get(BOUND) or {}
        run = self._run() or {}
        if "runId" in bound and bound.get("runId") != run.get("runId"):
            raise ToolError(moved(bound, {**bound, "runId": run.get("runId")}) or "The run changed.")
        return run

    def _control(self, action: str) -> Callable[[Mapping[str, Any]], Any]:
        def run(args: Mapping[str, Any]) -> Any:
            run_now = self._bound_run(args)
            bound = args.get(BOUND) or {}
            # The version the request was confirmed for, so the run's own check refuses one that moved in between. A
            # pause takes none: a working run moves all the time, and pausing it is always safe.
            version = None if action == "pause" else bound.get("version", run_now.get("version"))
            return self.s.migration.control({"action": action, **({"version": version} if version is not None else {})})
        return run

    def _resume_check(self, args: Mapping[str, Any]) -> Optional[str]:
        run = self._run()
        if not run or run.get("status") != run_state.PAUSED:
            return "No run is paused."
        if run.get("pausedReason") == run_state.WAVE_SIGNOFF:
            return f"Run {run.get('runId')} waits for a person to sign off wave {run.get('waitingWave')}: only a person does that."
        if run.get("needsInput"):
            return f"It needs the credentials for {', '.join(run['needsInput'])} again: only a person enters them."
        if run.get("resumable") is False:
            return str(run.get("unresumableReason") or "It cannot carry on.")
        return None

    def _retry_check(self, args: Mapping[str, Any]) -> Optional[str]:
        run = self._run()
        if not run:
            return "There is no run."
        if run.get("status") in run_state.WORKING:
            return "The run is working."
        if run.get("status") not in (run_state.COMPLETED_WITH_FAILURES, run_state.FAILED, run_state.PAUSED):
            return "Nothing in the run waits to be retried."
        if not (int(run.get("failed") or 0) + int(run.get("blocked") or 0)) and run.get("status") != run_state.FAILED:
            return "Nothing failed."
        if run.get("needsInput"):
            return f"It needs the credentials for {', '.join(run['needsInput'])} again: only a person enters them."
        if run.get("resumable") is False:
            return str(run.get("unresumableReason") or "It cannot carry on.")
        return None

    def _validation_body(self, depth: str) -> Dict[str, Any]:
        from discovery_agent.consult.policy import DEPTHS  # noqa: PLC0415

        runtime = DEPTHS[depth]
        plan = self._plan() or {}
        return {"items": [dict(i) for i in plan.get("items") or []] or None,
                "runtime": {"probe": runtime["probe"], "run": list(runtime["run"])}}

    def _validation_check(self, args: Mapping[str, Any], depth: str = "probe") -> Optional[str]:
        state = self.s.migration.validation_state()
        if state.get("state") == "running":
            return "Validation is already running."
        run = self._run()
        if run and run.get("status") in run_state.WORKING:
            return "A run is working: validate once it has finished."
        # Really running pipelines and notebooks: refused as the Validate page is, while the brief says they may write
        # to production systems (H4) and nobody kept running them anyway (K10).
        refusal = getattr(self.s.migration, "runtime_refusal", None)
        return refusal(self._validation_body(depth)["runtime"]) if callable(refusal) else None

    def _drafts_check(self, args: Mapping[str, Any]) -> Optional[str]:
        if self.s.transforms is None:
            return "LLM drafts are not served here."
        if self.s.consult is not None:
            gate = self.s.consult.llm_gate("transform")
            if not gate.get("allowed"):
                return str(gate.get("reason") or "Drafts may not be made now.")
        if not args.get("ids"):
            return "Name the objects to draft."
        refusal = getattr(self.s.transforms, "refusal", None)
        if callable(refusal):
            refused = [refusal(i) for i in args["ids"]]
            if all(refused):
                return f"No draft would be made for {'it' if len(refused) == 1 else 'any of them'}: {refused[0]}"
        return None

    def draftable(self, ids: Sequence[str]) -> List[str]:
        """The objects a draft would be made for now: a kind drafts are made for, within what the brief sends (D6)."""
        refusal = getattr(self.s.transforms, "refusal", None)
        return [i for i in ids if not callable(refusal) or refusal(i) is None]

    def failed_kinds(self) -> Dict[str, str]:
        """The kind of each failed or blocked object of the current run (procedure, view, connection...)."""
        return self._kinds()

    def _commit_check(self, args: Mapping[str, Any]) -> Optional[str]:
        run = self._run()
        if not run:
            return "There is no run whose items could be committed."
        if run.get("status") in run_state.WORKING:
            return "The run is working: it commits by itself when it completes."
        state, why = _try(self.s.migration.git_state)
        if why:
            return why
        if state.get("committing"):
            return "A commit is already running."
        if state.get("state") in ("not_connected", "no_target"):
            return str(state.get("message") or "The workspace is not connected to Git.")
        return None

    # -- the tools -------------------------------------------------------------------------------------------------

    def _build(self) -> List[Tool]:
        s = self.s
        tools: List[Tool] = [
            # read ---------------------------------------------------------------------------------------------------
            Tool("journey.status", READ, "Where the migration stands: discovery, the brief, the plan, the run, validation.",
                 lambda args: self._journey()),
            Tool("plan.steps", READ, "The execution plan's steps, each with its owner, status and the brief answers it came from.",
                 lambda args: s.steps() if s.steps else {"steps": []}),
            Tool("discovery.summary", READ, "What discovery found: counts by type and how each moves.",
                 lambda args: {k: v for k, v in s.session.discovery_status().items() if k in ("state", "finishedAt", "summary", "snapshot", "workspace")}),
            Tool("objects.find", READ, "Find discovered objects by name or type.",
                 lambda args: self._find(args), args={"text": Arg("string", "Part of the name, or a type such as View", True),
                                                      "limit": Arg("integer", "How many, at most 25", maximum=25)}),
            Tool("object.detail", READ, "One object: its type, how it moves, its issues, what it depends on and what references "
                 "it (never its definition).", lambda args: self._detail(args["id"]), args={"id": Arg("string", "The object's id", True)}),
            Tool("plan.analysis", READ, "The latest recorded plan: its readiness, effort, and its risks.",
                 lambda args: self._analysis()),
            Tool("run.status", READ, "The current run, or one by its id: its status, counts and how it differs from the brief.",
                 lambda args: self._run_view(args.get("runId")), args={"runId": Arg("string", "A run id such as 004; the current run if left out")}),
            Tool("run.failures", READ, "Each failed or blocked object of the current run, with its diagnosis.",
                 lambda args: self._failures()),
            Tool("run.events", READ, "A run's log lines, oldest first (they quote Fabric's own error text).",
                 lambda args: self._events(args), args={"runId": Arg("string", "A run id; the current run if left out"),
                                                        "after": Arg("integer", "Only events after this number"),
                                                        "limit": Arg("integer", "How many, at most 60", maximum=60)}),
            Tool("validation.results", READ, "The last validation: its summary, and its rows (all, or of one status).",
                 lambda args: self._validation(args), args={"status": Arg("string", "MATCH, REVIEW or MISMATCH"),
                                                            "limit": Arg("integer", "How many rows, at most 60", maximum=60)}),
            Tool("git.status", READ, "The target workspace's Fabric Git branch, what waits to be committed, the pull-request link.",
                 lambda args: s.migration.git_state()),
            Tool("drafts.states", READ, "Every object an LLM draft can be made for, with what the rules leave unsolved and its draft's state.",
                 lambda args: self._drafts()),
            Tool("brief.policy", READ, "The approved migration brief in force: its version, whether it still fits, and its "
                 "decisions (as G4 lets them go to the AI).", lambda args: self._policy()),
            Tool("object.diagnosis", READ, "Why one object of the current run failed, and what may be done about it.",
                 lambda args: self._diagnosis(args["id"]), args={"id": Arg("string", "The object's id", True)}),
            # prepare ------------------------------------------------------------------------------------------------
            Tool("drafts.generate", PREPARE, "Draft objects with Claude (each is sent the migration context), for a person to review and accept.",
                 lambda args: s.transforms.generate({"ids": list(args["ids"])}),
                 args={"ids": Arg("strings", "The objects' ids, at most 10", True, maximum=10)},
                 check=self._drafts_check,
                 preview=lambda args: {"summary": f"Draft {len(args.get('ids') or [])} object(s) with Claude.", "objects": list(args.get("ids") or []),
                                       "note": "Each is sent its definition and MIGRATION_CONTEXT.md, and counted against the budget (D2). Nothing is used until a person accepts it."},
                 key=lambda args: "drafts:" + ",".join(sorted(args.get("ids") or []))),
            Tool("validate.compare", PREPARE, "Validate by comparing only (definitions, columns, row counts): nothing changes.",
                 lambda args: self._validate(args, "compare"), check=lambda args: self._validation_check(args, "compare"),
                 preview=lambda args: {"summary": "Validate, comparing only.", "depth": "compare"},
                 key=lambda args: f"validate:compare:{(self._run() or {}).get('runId')}", bind=lambda args: self._validation_binding()),
            # change -------------------------------------------------------------------------------------------------
            Tool("run.start", CHANGE, "Start a run of the latest recorded plan, with the approved brief's options, when it changes "
                 "nothing already in Fabric (a run that overwrites is run.start_update or run.start_replace).",
                 lambda args: self._start(args), check=lambda args: self._plain_start_check(),
                 preview=lambda args: self._start_preview(), key=lambda args: f"start:{(self._plan() or {}).get('id')}",
                 bind=lambda args: self._start_binding()),
            Tool("run.pause", CHANGE, "Pause the current run after the object it is working on.", self._control("pause"),
                 check=lambda args: None if (self._run() or {}).get("status") in run_state.WORKING else "No run is working.",
                 preview=lambda args: {"summary": f"Pause run {(self._run() or {}).get('runId')}."},
                 key=lambda args: f"pause:{(self._run() or {}).get('runId')}", bind=lambda args: self._run_binding(False)),
            Tool("run.resume", CHANGE, "Resume the current run where it stopped (never a wave waiting for a person's sign-off).",
                 self._control("resume"), check=self._resume_check,
                 preview=lambda args: {"summary": f"Resume run {(self._run() or {}).get('runId')}: it carries on from where it stopped, "
                                                  "checking Fabric first for anything it was in the middle of."},
                 key=lambda args: f"resume:{(self._run() or {}).get('runId')}:{(self._run() or {}).get('version')}",
                 bind=lambda args: self._run_binding()),
            Tool("run.retry", CHANGE, "Retry what failed in the current run: a new pass over the failed and blocked objects.",
                 self._control("retry"), check=self._retry_check,
                 preview=lambda args: {"summary": f"Retry the {int((self._run() or {}).get('failed') or 0) + int((self._run() or {}).get('blocked') or 0)} "
                                                  f"failed or blocked object(s) of run {(self._run() or {}).get('runId')}."},
                 key=lambda args: f"retry:{(self._run() or {}).get('runId')}:{len((self._run() or {}).get('passes') or [])}",
                 bind=lambda args: self._run_binding()),
            Tool("validate.probe", CHANGE, "Validate, also querying tables and views and compiling procedures (nothing is changed).",
                 lambda args: self._validate(args, "probe"), check=lambda args: self._validation_check(args, "probe"),
                 preview=lambda args: {"summary": "Validate: compare, query and compile.", "depth": "probe"},
                 key=lambda args: f"validate:probe:{(self._run() or {}).get('runId')}", bind=lambda args: self._validation_binding()),
            Tool("git.commit", CHANGE, "Commit what the current run made to the target workspace's Fabric Git branch.",
                 lambda args: (self._bound_run(args), s.migration.git_commit({}))[1], check=self._commit_check,
                 preview=lambda args: {"summary": f"Commit run {(self._run() or {}).get('runId')}'s items to Fabric Git."},
                 key=lambda args: f"commit:{(self._run() or {}).get('runId')}", bind=lambda args: self._run_binding(False)),
            # high ---------------------------------------------------------------------------------------------------
            Tool("run.start_update", HIGH, "Start a run that updates objects already in Fabric in place (it overwrites changes made there).",
                 lambda args: self._start(args, {"existing": "update"}),
                 check=lambda args: self._start_check({"existing": "update"}),
                 preview=lambda args: self._start_preview({"existing": "update"}, "Start a run that updates in place"),
                 key=lambda args: f"start-update:{(self._plan() or {}).get('id')}", bind=lambda args: self._start_binding({"existing": "update"})),
            Tool("run.start_replace", HIGH, "Start a run that empties tables that have rows and loads them again.",
                 lambda args: self._start(args, {"dataMode": "replace"}),
                 check=lambda args: self._start_check({"dataMode": "replace"}),
                 preview=lambda args: self._start_preview({"dataMode": "replace"}, "Start a run that replaces rows"),
                 key=lambda args: f"start-replace:{(self._plan() or {}).get('id')}", bind=lambda args: self._start_binding({"dataMode": "replace"})),
            Tool("validate.run", HIGH, "Validate, also really running pipelines, notebooks and Spark jobs in Fabric.",
                 lambda args: self._validate(args, "run"), check=lambda args: self._validation_check(args, "run"),
                 preview=lambda args: {"summary": "Validate, running pipelines, notebooks and Spark jobs for real.", "depth": "run",
                                       "note": "What they write, they write: to Fabric, and to any system outside it they write to (H4)."},
                 key=lambda args: f"validate:run:{(self._run() or {}).get('runId')}", bind=lambda args: self._validation_binding()),
            Tool("run.close", HIGH, "Close the current run (Reset): it stops being the current one; its record stays in the history.",
                 lambda args: (self._bound_run(args), s.migration.reset())[1],
                 check=lambda args: "There is no run." if not self._run() else "The run is working." if (self._run() or {}).get("status") in run_state.WORKING else None,
                 preview=lambda args: {"summary": f"Close run {(self._run() or {}).get('runId')}. Objects it made stay in Fabric."},
                 key=lambda args: f"close:{(self._run() or {}).get('runId')}", bind=lambda args: self._run_binding()),
            Tool("discovery.rerun", HIGH, "Run discovery again (the brief may need reconfirming if the estate moved).",
                 lambda args: s.session.start_discovery(),
                 check=lambda args: "Discovery is running." if s.session.discovery_status().get("state") == "running" else None,
                 preview=lambda args: {"summary": "Run discovery again: the estate is read afresh; a brief approved on the old one may need reconfirming."},
                 key=lambda args: "rediscover",
                 bind=lambda args: {"snapshot": (s.session.discovery_status().get("snapshot") or {}).get("id")}),
        ]
        return tools

    def _start(self, args: Mapping[str, Any], overrides: Optional[Mapping[str, Any]] = None) -> Any:
        """Start the run the request was confirmed for: refused when what it would send moved in between."""
        bound = args.get(BOUND) or {}
        if bound:
            why = moved(bound, self._start_binding(overrides))
            if why:
                raise ToolError(why)
        return self.s.migration.start(self._start_body(overrides) or {})

    def _validation_binding(self) -> Dict[str, Any]:
        return {"runId": (self._run() or {}).get("runId"), "plan": (self._plan() or {}).get("id")}

    def _validate(self, args: Mapping[str, Any], depth: str) -> Any:
        bound = args.get(BOUND) or {}
        if bound:
            why = moved(bound, self._validation_binding())
            if why:
                raise ToolError(why)
        return self.s.migration.start_validation(self._validation_body(depth))

    # -- read tools' bodies -----------------------------------------------------------------------------------------

    def _journey(self) -> Dict[str, Any]:
        status = self.s.session.discovery_status()
        run = self._run() or {}
        brief = self._brief()
        plan = self._plan() or {}
        validation = self.s.migration.validation_state()
        return {"discovery": {"state": status.get("state"), "objects": (status.get("summary") or {}).get("total")},
                "brief": {"version": brief.version, "stale": brief.stale} if brief is not None else None,
                "plan": {"version": plan.get("id"), "objects": plan.get("objects"), "readiness": plan.get("readiness"),
                         "blocking": plan.get("blocking")} if plan else None,
                "run": {k: run.get(k) for k in ("runId", "status", "pausedReason", "waitingWave", "completed", "failed", "blocked",
                                                "skipped", "deferred", "total")} if run else None,
                "validation": {"state": validation.get("state"), "summary": validation.get("summary")}}

    def _find(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        text = str(args.get("text") or "")
        page = self.s.session.results({"search": text, "pageSize": str(int(args.get("limit") or 10))})
        return {"total": page.get("total"), "items": [{k: i.get(k) for k in ("id", "name", "type", "classification", "fabricTarget", "wave")}
                                                      for i in (page.get("items") or [])[:int(args.get("limit") or 10)]]}

    def _detail(self, object_id: str) -> Dict[str, Any]:
        """What discovery says about one object. Never its definition: a definition goes to the model only in a draft,
        with the brief's consent and within its scope (D1, D6)."""
        d = self.s.session.result(object_id)
        overview = d.get("overview") or {}
        return {"id": object_id, "name": d.get("name"), "type": d.get("type"), "classification": d.get("classification"),
                "fabricTarget": (d.get("target") or {}).get("component") or d.get("fabricTarget"),
                "issues": [i.get("message") for i in d.get("issues") or []][:10],
                "dependsOn": [x.get("name") for x in d.get("dependencies") or []][:30],
                "referencedBy": [x.get("name") for x in d.get("referencedBy") or []][:30],
                "constructs": overview.get("synapseSpecificConstructs")}

    def _minimal(self) -> bool:
        """G4 at minimal: the model gets only the decisions, no names and no values."""
        brief = self._brief()
        return brief is not None and brief.llm_context() == "minimal"

    def _analysis(self) -> Optional[Dict[str, Any]]:
        plan = self._plan()
        if not plan:
            return None
        body = {"items": [dict(i) for i in plan.get("items") or []], "options": dict(plan.get("options") or {})}
        result, why = _try(lambda: self.s.migration.analyze(body))
        if why:
            return {"plan": plan.get("id"), "error": why}
        return {"plan": plan.get("id"), "readiness": result.get("readiness"), "effortDays": result.get("effortDays"),
                "blocking": result.get("blocking"), "risks": [{k: r.get(k) for k in ("code", "severity", "title", "message")}
                                                              for r in (result.get("risks") or [])[:15]],
                "brief": result.get("brief")}

    def _run_view(self, run_id: Optional[str]) -> Optional[Dict[str, Any]]:
        run = self.s.migration.run_record(run_id) if run_id else self._run()
        if not run:
            return None
        return {k: run.get(k) for k in ("runId", "status", "state", "pausedReason", "waitingWave", "haltedReason", "total", "completed",
                                        "failed", "blocked", "skipped", "deferred", "warehouse", "workspace", "createdAt", "finishedAt",
                                        "approval", "brief", "git", "passes")}

    def _kinds(self) -> Dict[str, str]:
        from discovery_agent.api.migration import sources_for  # noqa: PLC0415

        run = self._run() or {}
        ids = [{"id": str(i.get("id")), "wave": 1} for i in run.get("items") or [] if i.get("status") in ("FAILED", "BLOCKED")]
        if not ids:
            return {}
        try:
            job, pool = self.s.session.migration_snapshot()
            return {src.id: src.kind for src in sources_for(ids, job, pool)}
        except Exception:  # noqa: BLE001 - without the kinds a diagnosis only never offers a draft
            return {}

    def _drafts_by_id(self) -> Dict[str, str]:
        if self.s.transforms is None:
            return {}
        try:
            return {str(i.get("objectId")): str(i.get("state")) for i in self.s.transforms.list().get("items") or []}
        except Exception:  # noqa: BLE001 - no drafts read is no drafts
            return {}

    def _consent(self) -> Optional[bool]:
        brief = self._brief()
        return brief.llm()[0] if brief is not None else None

    def _failures(self) -> List[Dict[str, Any]]:
        from discovery_agent.orchestrator import diagnosis  # noqa: PLC0415

        run = self._run() or {}
        found = [d.to_dict() for d in diagnosis.summary(list(run.get("items") or []), self._consent(), self._kinds(), self._drafts_by_id())]
        if self._minimal():
            for d in found:
                d["error"] = without_values(d.get("error"))
        return found

    def _diagnosis(self, object_id: str) -> Optional[Dict[str, Any]]:
        from discovery_agent.orchestrator import diagnosis  # noqa: PLC0415

        run = self._run() or {}
        item = next((i for i in run.get("items") or [] if str(i.get("id")) == object_id), None)
        if item is None:
            return None
        return diagnosis.diagnose(item, self._consent(), self._kinds().get(object_id), self._drafts_by_id().get(object_id)).to_dict()

    def _events(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        run_id = args.get("runId") or (self._run() or {}).get("runId")
        if not run_id:
            return {"events": []}
        out = self.s.migration.run_events(str(run_id), args.get("after"), args.get("limit") or 40)
        minimal = self._minimal()
        return {"runId": run_id, "last": out.get("last"),
                "events": [{k: (without_values(e.get(k)) if minimal and k in ("message", "error") else e.get(k))
                            for k in ("seq", "type", "at", "object", "stage", "message", "status", "error")}
                           for e in out.get("events") or []]}

    def _validation(self, args: Mapping[str, Any]) -> Dict[str, Any]:
        state = self.s.migration.validation_state()
        rows = [r for r in state.get("rows") or [] if not args.get("status") or r.get("status") == args.get("status")]
        return {"state": state.get("state"), "summary": state.get("summary"), "finishedAt": state.get("finishedAt"),
                "rows": rows[:int(args.get("limit") or 30)], "rowsTotal": len(rows)}

    def _drafts(self) -> Dict[str, Any]:
        if self.s.transforms is None:
            return {"items": []}
        out = self.s.transforms.candidates()
        return {"summary": out.get("summary"), "items": [{k: i.get(k) for k in ("objectId", "name", "type", "problems", "state", "stale",
                                                                                  "contextStale", "unavailable")}
                                                         for i in (out.get("items") or [])[:40]]}

    def _policy(self) -> Optional[Dict[str, Any]]:
        """The brief in force, as G4 lets it go to the model: at minimal only the decisions (yes or no, a choice, a
        number), never a name, a column, a principal or a context answer; else what a draft's MIGRATION_CONTEXT.md holds."""
        brief = self._brief()
        if brief is None:
            return None
        from discovery_agent.consult.settings import BOOL, CHOICE, LIST, NUMBER, SETTINGS  # noqa: PLC0415

        def decision(name: str) -> bool:
            """A yes or no, a choice, a number, or a list of fixed values: never a name a person wrote."""
            s = SETTINGS.get(name)
            return s is not None and (s.kind in (BOOL, CHOICE, NUMBER) or (s.kind == LIST and bool(s.values)))

        minimal = brief.llm_context() == "minimal"
        settings = {k: v for k, v in brief.settings.items() if not minimal or decision(k)}
        return {"version": brief.version, "stale": brief.stale, "outdated": brief.outdated, "minimal": minimal,
                "settings": settings}
