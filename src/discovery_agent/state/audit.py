"""The audit trail: who did what, to what, when, and how it ended.

One append-only stream per project (``audit.jsonl`` in the project's store). Each record says:

* ``action``: one of ``CATALOG``, a closed list. An action not in it is a mistake in the code: it is logged as an
  error and recorded with no details, never dropped;
* ``actor``: a **person** (the operator at this machine: ``MIGRATION_OPERATOR``, else the account the API runs
  as), the **agent** (an LLM draft), or the **system** (the accelerator itself: what it does on its own, such as
  approving a run no person needs to sign off, recovering a run after a restart, finishing a pass). A record
  never says a person did what the accelerator did;
* ``target`` ({type, id}), ``outcome`` (done | failed | refused), a stable ``code`` and a ``reason``;
* ``details``: only the fields its action allows (``CATALOG``), each cut to size and passed through the same
  redaction as the logs. No credential, token, secret or definition is ever recorded, by construction: a field
  that is not on the action's list is dropped before anything is written;
* the context it happened in: ``correlationId`` (the request, and everything it started), ``project``, ``run``.

**Tamper-evident**: every record carries the hash of the one before it (``prev``) and its own (``hash``, over
everything else in it, its ``seq`` included). Changing, removing or reordering a record breaks the chain from
there on, and ``verify`` says where. It shows that the file was changed, not who changed it: anyone who can
write the file can also write a new chain, so it is evidence for an honest reader, not a lock.

Recording never fails the action it records: ``Auditor.record`` never raises. When the trail cannot be written
the API's log says so, under ``discovery_agent.audit``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import threading
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from discovery_agent.state import context
from discovery_agent.state.store import Store, now

STREAM = "audit"
GENESIS = "0" * 64
OUTCOMES = ("done", "failed", "refused")
MAX_TEXT = 500
MAX_LIST = 50
_log = logging.getLogger("discovery_agent.audit")


def _fields(*names: str) -> FrozenSet[str]:
    return frozenset(names)


#: Every action the trail records: what it means, and the details it may carry. Nothing else is ever kept.
CATALOG: Dict[str, Tuple[str, FrozenSet[str]]] = {
    "system.started": ("The API started", _fields("pid", "stateDir", "logFile", "persistent", "projects")),
    "system.stopped": ("The API stopped", _fields("pid")),
    "project.created": ("A project was created", _fields("name", "adopted")),
    "project.renamed": ("A project was renamed", _fields("name", "previous")),
    "project.activated": ("Another project was made the active one", _fields("name", "previous")),
    "project.removed": ("A project was taken off the list; its folder is archived", _fields("name", "archive")),
    "source.signed_in": ("Signed in to Azure for the Synapse source", _fields("method", "tenant", "account", "subscriptions")),
    "source.connected": ("A Synapse source was connected and tested", _fields("workspace", "pool", "sourceKind", "checks")),
    "source.repository": ("A Synapse Git repository was chosen as the source", _fields(
        "label", "ref", "commit", "environment", "parameters", "unmatched", "secrets")),
    "source.uploaded": ("A ZIP of a Synapse repository was uploaded", _fields("name", "bytes")),
    "source.disconnected": ("The Synapse source was disconnected", _fields("workspace")),
    "discovery.started": ("Discovery started", _fields("workspace", "pool", "sourceKind")),
    "discovery.completed": ("Discovery finished", _fields(
        "snapshot", "fingerprint", "objects", "complete", "failedCategories", "warnings", "state", "message")),
    "discovery.reset": ("The last discovery's results were forgotten", _fields("snapshot")),
    "fabric.signed_in": ("Signed in to Fabric", _fields("method", "account", "tenant", "workspaces")),
    "fabric.connected": ("The Fabric target workspace was chosen and verified, or reconnected at start", _fields(
        "workspace", "method", "capacity", "checks", "restored")),
    "fabric.disconnected": ("The Fabric target was disconnected", _fields("workspace")),
    "consult.answered": ("A consultation question was answered", _fields("question", "value", "source", "note", "catalogVersion")),
    "consult.skipped": ("A consultation question was skipped; its recommendation stands as an assumption", _fields("question")),
    "consult.withdrawn": ("A consultation answer was withdrawn; the record keeps it", _fields("question")),
    "consult.accepted": ("Recommendations were accepted as answers", _fields("questions", "section", "count")),
    "consult.resolved": ("A contradiction between the answers and the estate was resolved", _fields(
        "conflict", "resolution", "reason", "question", "value")),
    "consult.proposed": ("Answers were proposed from notes or an imported questionnaire, for a person to confirm", _fields(
        "source", "model", "count", "questions", "textHash", "message")),
    "consult.proposal_confirmed": ("A person confirmed a proposed answer", _fields("proposal", "question", "value", "source")),
    "consult.proposal_rejected": ("A person rejected a proposed answer", _fields("proposal", "question", "value", "source")),
    "consult.explained": ("A consultation question was explained", _fields("question", "source", "model")),
    "consult.exported": ("The questionnaire was exported, to answer offline", _fields("format", "questions")),
    "brief.submitted": ("A migration brief version was submitted for approval", _fields(
        "version", "hash", "decisions", "assumptions", "kept", "catalogVersion", "factsFingerprint", "snapshot", "superseded")),
    "brief.approved": ("A migration brief version was approved: it is the policy in force", _fields(
        "version", "hash", "comment", "selfApproved", "separateApprover", "superseded", "targetWorkspace")),
    "brief.rejected": ("A migration brief version was rejected", _fields("version", "hash", "comment")),
    "brief.exported": ("A migration brief version was exported", _fields("version", "format")),
    "plan.deviation": ("A plan that differs from the approved brief was confirmed, with a reason", _fields(
        "briefVersion", "differences", "reason")),
    "llm.refused": ("An LLM call was refused: no consent, or the budget is spent", _fields("purpose", "reason", "spent", "budget")),
    "plan.recorded": ("A plan version was recorded", _fields("fingerprint", "objects", "readiness", "blocking", "snapshot")),
    "run.requested": ("A migration run was asked for", _fields(
        "objects", "stages", "existing", "dataMode", "dataRun", "credentialNames", "workspace", "warehouse",
        "plannerFingerprint", "snapshot", "brief", "briefHash", "differences", "signoffRule")),
    "run.wave_signed_off": ("A person signed off the next wave of a run", _fields("wave", "comment")),
    "run.refused": ("A migration run was refused before it started", _fields("message")),
    "run.signoff_requested": ("A run waits for a person to sign it off", _fields("reason", "policy")),
    "run.approved": ("A run was signed off", _fields("automatic", "policy", "comment", "subjectHash")),
    "run.rejected": ("A run was declined at sign-off", _fields("comment", "subjectHash")),
    "run.pause_requested": ("A pause was asked for", _fields()),
    "run.paused": ("A run paused", _fields("pausedReason", "reason")),
    "run.halted": ("A run stopped before a wave, on failures in an earlier one", _fields("pausedReason", "reason")),
    "run.resumed": ("A run was resumed", _fields()),
    "run.retried": ("A run's failed objects were retried", _fields("objects")),
    "run.completed": ("A run's pass finished", _fields(
        "status", "completed", "failed", "blocked", "skipped", "deferred", "passes")),
    "run.interrupted": ("A run was found cut short by a restart, and paused", _fields("working", "needsInput")),
    "run.closed": ("A run was closed; its record stays in the run history", _fields("status")),
    "validation.started": ("Validation started", _fields("probe", "run", "objects")),
    "validation.completed": ("Validation finished", _fields("summary", "message")),
    "git.commit_requested": ("A Fabric Git commit was asked for", _fields("comment")),
    "git.commit": ("A Fabric Git commit was made, or tried", _fields("state", "items", "branch", "commit", "others", "message")),
    "transform.requested": ("LLM drafts were asked for", _fields("objects", "model", "contextHash")),
    "transform.drafted": ("The model wrote a draft", _fields(
        "model", "confidence", "checks", "promptHash", "codeHash", "message", "contextHash", "briefVersion")),
    "transform.accepted": ("A person accepted a draft, which runs now use", _fields("edited", "codeHash", "sourceHash")),
    "transform.discarded": ("A draft, or an accepted transform, was dropped", _fields("scope")),
    "transform.requirements": ("The project requirements every prompt carries were changed", _fields("chars", "hash")),
    "journey.step_confirmed": ("A person confirmed a step of the journey", _fields("step", "note")),
    "orchestrator.plan_built": ("A new version of the execution plan was built, from the brief, the plan and the estate", _fields(
        "version", "hash", "briefVersion", "planVersion", "snapshot", "steps")),
    "orchestrator.step_marked": ("A person marked a step of the execution plan done, or not done", _fields("step", "done", "note")),
    "orchestrator.action_proposed": ("The agent proposed an action, for a person to confirm", _fields(
        "action", "tool", "tier", "summary", "step", "task")),
    "orchestrator.action_confirmed": ("A person confirmed a proposed action, which then ran as that person", _fields(
        "action", "tool", "tier", "note", "outcome")),
    "orchestrator.action_rejected": ("A person rejected a proposed action, with the reason", _fields("action", "tool", "reason")),
    "orchestrator.action_taken": ("The agent acted on its own, as the brief's autonomy allows on a development workspace", _fields(
        "action", "tool", "tier", "summary", "outcome")),
    "orchestrator.action_expired": ("A proposed action lapsed: it no longer applied, or nobody decided in time", _fields(
        "action", "tool", "reason")),
    "orchestrator.paused": ("A person paused the agent: it proposes nothing and does nothing until resumed", _fields()),
    "orchestrator.resumed": ("A person resumed the agent", _fields()),
    "assistant.asked": ("A person asked the assistant a question", _fields("task", "chars", "source")),
    "assistant.finished": ("The assistant finished with a question: answered, stopped at a limit, or cancelled", _fields(
        "task", "turns", "actions", "outcome", "costUsd")),
    "report.exported": ("The sign-off report was exported", _fields("format", "auditHead")),
    "audit.exported": ("The audit trail was exported", _fields("format", "records")),
}


def _canonical(record: Dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def digest(record: Dict[str, Any]) -> str:
    """The hash of a record: over everything in it but its own ``hash``."""
    return hashlib.sha256(_canonical({k: v for k, v in record.items() if k != "hash"})).hexdigest()


def _redact(text: str) -> str:
    from discovery_agent.diagnostics import redacted  # noqa: PLC0415 - diagnostics imports the connection layer

    return redacted(text)


def clean(value: Any, depth: int = 0) -> Any:
    """A details value, safe to keep: text redacted and cut to size, lists and maps bounded, anything else as text."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = _redact(value)
        return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
        return [clean(v, depth + 1) for v in items[:MAX_LIST]] + ([f"… and {len(items) - MAX_LIST} more"] if len(items) > MAX_LIST else [])
    if isinstance(value, dict) and depth < 3:
        return {str(k)[:80]: clean(v, depth + 1) for k, v in list(value.items())[:MAX_LIST]}
    return clean(str(value), depth)


def _target(target: Optional[Dict[str, Any]]) -> Optional[Dict[str, Optional[str]]]:
    if not target:
        return None
    return {"type": clean(str(target.get("type") or "")), "id": None if target.get("id") is None else clean(str(target.get("id")))}


class Auditor:
    """Writes one project's trail. One per project store: it keeps the chain's head while the process runs."""

    def __init__(self, store: Optional[Store]) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._head: Optional[str] = None

    # -- writing -----------------------------------------------------------------------------------

    def record(self, action: str, *, target: Optional[Dict[str, Any]] = None, outcome: str = "done", code: Optional[str] = None,
               reason: Optional[str] = None, details: Optional[Dict[str, Any]] = None, run_id: Optional[str] = None,
               actor: Optional[context.Actor] = None) -> Optional[Dict[str, Any]]:
        """Add a record. Returns it, or None when there is no trail (state off for this process) or it could not be
        written. Never raises: an action is never undone because its record failed."""
        try:
            return self._record(action, target, outcome, code, reason, details, run_id, actor)
        except Exception as exc:  # noqa: BLE001 - recording never fails what it records; the log says what was lost
            _log.error("Could not write the audit record %s: %s: %s", action, type(exc).__name__, exc)
            return None

    def _record(self, action: str, target: Optional[Dict[str, Any]], outcome: str, code: Optional[str], reason: Optional[str],
                details: Optional[Dict[str, Any]], run_id: Optional[str], actor: Optional[context.Actor]) -> Optional[Dict[str, Any]]:
        if self.store is None:
            return None
        known = CATALOG.get(action)
        if known is None:
            _log.error("Audit action %r is not in the catalog; recorded without details", action)
        allowed = known[1] if known else frozenset()
        given = details or {}
        kept = {k: clean(v) for k, v in given.items() if k in allowed}
        dropped = sorted(k for k in given if k not in allowed)
        if dropped:
            _log.warning("Audit action %s: left out field(s) it does not allow: %s", action, ", ".join(dropped))
        who = actor or context.actor()
        fields = context.fields()
        record: Dict[str, Any] = {
            "at": now(), "action": action, "actor": who.to_dict(), "target": _target(target),
            "outcome": outcome if outcome in OUTCOMES else "done", "code": clean(code) if code else None,
            "reason": clean(reason) if reason else None, "details": kept,
            "correlationId": fields.get("correlationId"), "project": fields.get("project"),
            "run": run_id or fields.get("run"),
        }
        # The hash covers the record as it will be read back: through JSON, as the stream keeps it.
        record = json.loads(json.dumps(record, ensure_ascii=False, default=str))
        with self._lock:
            head = self._head if self._head is not None else self._last_hash()
            record["seq"] = self.store.seq(STREAM) + 1
            record["prev"] = head
            record["hash"] = digest(record)
            written = self.store.append(STREAM, record)
            if written.get("seq") != record["seq"]:  # someone else wrote the stream: the chain is told, never forged
                _log.error("The audit trail's numbering moved under it (expected %s, got %s)", record["seq"], written.get("seq"))
                self._head = None
                return written
            self._head = record["hash"]
        return written

    def _last_hash(self) -> str:
        last = self.store.last(STREAM) if self.store is not None else None
        return str(last.get("hash")) if last and last.get("hash") else GENESIS

    # -- reading -----------------------------------------------------------------------------------

    def records(self) -> List[Dict[str, Any]]:
        return self.store.records(STREAM) if self.store is not None else []

    def query(self, *, before: Optional[int] = None, limit: int = 100, action: Optional[str] = None, actor: Optional[str] = None,
              target: Optional[str] = None, run: Optional[str] = None, outcome: Optional[str] = None,
              since: Optional[str] = None, until: Optional[str] = None, correlation: Optional[str] = None) -> Dict[str, Any]:
        """Records newest first, filtered; ``before`` (a seq) pages back through older ones.

        ``action`` matches a whole action or a family (``run`` matches ``run.*``); ``actor`` matches the kind
        (``person``) or ``kind:id``; ``target`` matches ``type`` or ``type:id``; ``since`` / ``until`` are ISO times."""
        limit = max(1, min(int(limit), 500))

        def keep(r: Dict[str, Any]) -> bool:
            if before is not None and r["seq"] >= before:
                return False
            if action and r.get("action") != action and not str(r.get("action") or "").startswith(action.rstrip(".") + "."):
                return False
            who = r.get("actor") or {}
            if actor and actor not in (who.get("kind"), f"{who.get('kind')}:{who.get('id')}"):
                return False
            what = r.get("target") or {}
            if target and target not in (what.get("type"), f"{what.get('type')}:{what.get('id')}"):
                return False
            if run and r.get("run") != run:
                return False
            if outcome and r.get("outcome") != outcome:
                return False
            if correlation and r.get("correlationId") != correlation:
                return False
            at = str(r.get("at") or "")
            return not (since and at < since) and not (until and at > until)

        matched = [r for r in reversed(self.records()) if keep(r)]
        page = matched[:limit]
        return {"records": page, "next": page[-1]["seq"] if len(matched) > limit else None, "matched": len(matched),
                "catalog": {k: v[0] for k, v in CATALOG.items()}}

    def verify(self) -> Dict[str, Any]:
        """Walk the chain from the first record. Says how many records it holds, and the first one that breaks it."""
        return verify(self.records())

    def export(self, fmt: str) -> Tuple[bytes, str, int]:
        """The whole trail, oldest first: ``jsonl`` (every record as written, hashes and all, so it can be verified
        elsewhere) or ``csv`` (one row per record, for a spreadsheet). Returns (bytes, media type, records)."""
        records = self.records()
        if fmt == "jsonl":
            body = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"), default=str) + "\n" for r in records)
            return body.encode("utf-8"), "application/x-ndjson", len(records)
        if fmt == "csv":
            return to_csv(records).encode("utf-8-sig"), "text/csv", len(records)
        raise ValueError(f"Not an export format: {fmt}")


def verify(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    head, count = GENESIS, 0
    for record in records:
        count += 1
        problem = None
        if record.get("seq") != count:
            problem = f"record {record.get('seq')} is where record {count} should be: one is missing or out of order"
        elif record.get("prev") != head:
            problem = f"record {count} does not follow the one before it: a record was changed, removed or inserted"
        elif record.get("hash") != digest(record):
            problem = f"record {count} was changed after it was written"
        if problem:
            return {"ok": False, "records": count, "brokenAt": record.get("seq"), "problem": problem, "head": head}
        head = str(record["hash"])
    return {"ok": True, "records": count, "brokenAt": None, "problem": None, "head": head}


_CSV_COLUMNS = ("seq", "at", "action", "actorKind", "actor", "targetType", "target", "outcome", "code", "reason", "details",
                "correlationId", "project", "run", "hash")


def _cell(value: Any) -> str:
    text = "" if value is None else (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str))
    # A spreadsheet runs a cell that starts with one of these as a formula: never let a record become one.
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(records: Iterable[Dict[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    for r in records:
        who, what = r.get("actor") or {}, r.get("target") or {}
        writer.writerow([_cell(v) for v in (
            r.get("seq"), r.get("at"), r.get("action"), who.get("kind"), who.get("id"), what.get("type"), what.get("id"),
            r.get("outcome"), r.get("code"), r.get("reason"), r.get("details") or {}, r.get("correlationId"),
            r.get("project"), r.get("run"), r.get("hash"))])
    return out.getvalue()
