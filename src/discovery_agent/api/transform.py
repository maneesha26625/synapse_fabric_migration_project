"""LLM-assisted transformation: drafts on request, a person accepts them, a run uses what was accepted.

    GET  /api/transform/settings           whether drafts can be generated here, and with what
    GET  /api/transform                    every object with a draft, an accepted transform or a job
    GET  /api/transform/objects/{id}       one object: its source, the rule output, the draft, the accepted code
    GET  /api/transform/prompt/{id}        the exact prompt a draft would be generated from
    GET  /api/transform/candidates         every object a draft can be made for, those the rules cannot convert first
    GET  /api/transform/project            the project requirements (prompts/transform/project.md)
    POST /api/transform/project            {"text"}: replace the project requirements
    POST /api/transform/generate           {"ids": [...]}: queue drafts (background; poll the object)
    POST /api/transform/accept             {"id", "code"?}: accept the draft, or an edited version of it
    POST /api/transform/discard            {"id", "scope": "draft" | "all"}: drop the draft, or go back to the rules

What this changes about the accelerator's principles, deliberately and only on request:

* The object's definition is sent to the model (Claude, through the Claude Code CLI on this
  machine). ``MIGRATION_LLM=off`` disables generation entirely.
* A model's answer is never deployed as it comes. It is checked, shown to a person next to the
  source and the rule output, and used by a run only once that person accepts it, and only while
  the source still has the hash it had when the draft was generated.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from discovery_agent.api.migration import sources_for
from discovery_agent.api.service import ApiError
from discovery_agent.llm import checks, context, templates
from discovery_agent.llm.claude_cli import ClaudeCli, ModelError
from discovery_agent.llm.store import TransformStore
from discovery_agent.llm.templates import TemplateError
from discovery_agent.migration.common import DEFERRED, MISSING, NOTEBOOK, Source
from discovery_agent.migration.runner import MIGRATABLE, warehouse_name_for

REPO_ROOT = Path(__file__).resolve().parents[3]
MAX_IDS = 50
MAX_ACCEPT_BYTES = 1024 * 1024
MAX_PROJECT_CHARS = 32 * 1024
#: The placeholders a template can use, for the requirements editor's help text.
PLACEHOLDERS = ("object.name", "object.type", "object.kind", "object.schema", "target.warehouse", "target.workspace",
                "target.component", "notebook.language")
_OFF = {"off", "0", "false", "no", "disabled"}
QUEUED, RUNNING, FAILED_JOB = "queued", "running", "failed"


def prompts_dir() -> Path:
    return Path(os.environ.get("MIGRATION_PROMPTS_DIR") or REPO_ROOT / "prompts" / "transform")


def transforms_dir() -> Path:
    return Path(os.environ.get("MIGRATION_TRANSFORMS_DIR") or REPO_ROOT / "transforms")


def _original(source: Source) -> Optional[Dict[str, Any]]:
    """The Synapse resource a notebook transform is applied to; None for other kinds."""
    return source.payload if source.kind == NOTEBOOK and isinstance(source.payload, dict) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TransformService:
    def __init__(self, session: Any, fabric: Any = None, model: Any = None, store: Optional[TransformStore] = None,
                 prompts: Optional[Path] = None, workers: Optional[int] = None, synchronous: bool = False) -> None:
        self._session = session
        self._fabric = fabric
        self._model = model or ClaudeCli()
        self._store = store or TransformStore(transforms_dir())
        self._prompts = prompts or prompts_dir()
        self._synchronous = synchronous
        self._pool = None if synchronous else ThreadPoolExecutor(
            max_workers=workers or int(os.environ.get("MIGRATION_LLM_WORKERS") or 2), thread_name_prefix="transform")
        self._lock = threading.Lock()
        #: Jobs in flight or failed, by object id: {"state", "error", "startedAt"}. Memory only.
        self._jobs: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def enabled() -> bool:
        return (os.environ.get("MIGRATION_LLM") or "on").strip().lower() not in _OFF

    # -- resolving one object -------------------------------------------------------

    def _source(self, object_id: str) -> Tuple[Source, Any, Optional[str]]:
        job, pool = self._session.migration_snapshot()
        source = sources_for([{"id": object_id, "wave": 1}], job, pool)[0]
        if source.kind == MISSING:
            raise ApiError(404, "object_not_found", "That object is not in the current discovery results.")
        if source.kind == DEFERRED:
            raise ApiError(400, "not_transformable", source.reason or "This object is not migrated by this build.")
        return source, job, pool

    def _subject(self, object_id: str) -> Tuple[Source, context.Subject, Any, Optional[str]]:
        source, job, pool = self._source(object_id)
        try:
            return source, context.extract(source), job, pool
        except context.NotTransformable as exc:
            raise ApiError(400, "not_transformable", str(exc)) from exc

    def _target(self, pool: Optional[str]) -> Dict[str, str]:
        workspace = ""
        if self._fabric is not None:
            try:
                workspace = str(self._fabric.state().get("workspaceName") or "")
            except Exception:  # noqa: BLE001 - the target is optional context
                workspace = ""
        return {"workspace": workspace, "warehouse": warehouse_name_for(pool)}

    def _prompt(self, source: Source, subject: context.Subject, job: Any, pool: Optional[str]) -> str:
        try:
            detail = self._session.result(source.id)
        except ApiError:
            detail = {}
        try:
            return context.build(subject, source, detail, self._target(pool), getattr(job, "records_by_id", {}), self._prompts)
        except TemplateError as exc:
            raise ApiError(500, "prompt_template_error", str(exc)) from exc

    # -- the view of one object -----------------------------------------------------

    def _view(self, object_id: str, record: Optional[Dict[str, Any]], current_hash: Optional[str]) -> Dict[str, Any]:
        with self._lock:
            job = dict(self._jobs.get(object_id) or {})
        draft = (record or {}).get("draft")
        accepted = (record or {}).get("accepted")
        if job.get("state") in (QUEUED, RUNNING, FAILED_JOB):
            state = job["state"]
        elif draft:
            state = "draft"
        elif accepted:
            state = "accepted"
        else:
            state = "none"

        def stale(part: Optional[Dict[str, Any]]) -> Optional[bool]:
            if not part or current_hash is None:
                return None
            return part.get("sourceHash") != current_hash

        if draft:
            draft = {**draft, "stale": stale(draft)}
        if accepted:
            accepted = {**accepted, "stale": stale(accepted)}
        return {
            "objectId": object_id, "name": (record or {}).get("name"), "type": (record or {}).get("type"),
            "kind": (record or {}).get("kind"), "state": state, "error": job.get("error"), "startedAt": job.get("startedAt"),
            "draft": draft, "accepted": accepted,
        }

    # -- routes -------------------------------------------------------------------------

    def settings(self) -> dict:
        return {
            "enabled": self.enabled(), **self._model.available(),
            "kinds": sorted(context.TRANSFORMABLE_KINDS),
            "promptsDir": str(self._prompts), "storeDir": str(self._store.directory),
            "disabledReason": None if self.enabled() else "Generation is switched off on this server (MIGRATION_LLM=off).",
        }

    def list(self) -> dict:
        with self._lock:
            job_ids = list(self._jobs)
        ids = {r["objectId"]: r for r in self._store.all()}
        items = []
        for oid in sorted(set(ids) | set(job_ids)):
            view = self._view(oid, ids.get(oid), None)
            for part in ("draft", "accepted"):
                if view[part]:
                    view[part] = {k: v for k, v in view[part].items() if k in ("confidence", "generatedAt", "acceptedAt", "edited")}
            items.append(view)
        return {"items": items}

    def object(self, object_id: str) -> dict:
        source, subject, _, _ = self._subject(object_id)
        view = self._view(object_id, self._store.get(object_id), subject.source_hash)
        view.update({"name": view["name"] or source.name, "type": view["type"] or source.type, "kind": subject.kind})
        view["source"] = {"code": subject.code, "language": subject.language, "hash": subject.source_hash,
                          "ruleOutput": subject.rule_output, "ruleNotes": subject.rule_notes, "problems": subject.problems}
        return view

    def candidates(self) -> dict:
        """Every object a draft can be made for, with what the rules leave unsolved. Those first: they need a person or a draft."""
        job, pool = self._session.migration_snapshot()
        plan = [{"id": i["id"], "wave": 1} for i in job.items if MIGRATABLE.get(str(i.get("type") or "")) in context.TRANSFORMABLE_KINDS]
        records = {r["objectId"]: r for r in self._store.all()}
        items = []
        for source in sources_for(plan, job, pool):
            if source.kind not in context.TRANSFORMABLE_KINDS:
                continue
            base = {"objectId": source.id, "name": source.name, "type": source.type, "kind": source.kind}
            try:
                subject = context.extract(source)
            except context.NotTransformable as exc:
                items.append({**base, "problems": [], "rulesChange": False, "state": "none", "error": None, "confidence": None,
                              "stale": None, "unavailable": str(exc)})
                continue
            view = self._view(source.id, records.get(source.id), subject.source_hash)
            latest = view["draft"] or view["accepted"] or {}
            items.append({**base, "problems": subject.problems, "rulesChange": bool(subject.rule_output), "state": view["state"],
                          "error": view["error"], "confidence": latest.get("confidence"), "stale": latest.get("stale"), "unavailable": None})
        items.sort(key=lambda i: (i["unavailable"] is not None, not i["problems"], i["name"].lower()))
        count = lambda f: sum(1 for i in items if f(i))  # noqa: E731
        return {"items": items, "summary": {
            "total": len(items), "needsWork": count(lambda i: bool(i["problems"])),
            "drafts": count(lambda i: i["state"] == "draft"), "accepted": count(lambda i: i["state"] == "accepted"),
            "working": count(lambda i: i["state"] in (QUEUED, RUNNING)), "failed": count(lambda i: i["state"] == FAILED_JOB),
        }}

    def project(self) -> dict:
        path = self._prompts / "project.md"
        try:
            text = templates.read(path)
        except TemplateError as exc:
            raise ApiError(500, "prompt_template_error", str(exc)) from exc
        return {"text": text, "path": str(path), "placeholders": list(PLACEHOLDERS)}

    def save_project(self, body: dict) -> dict:
        text = body.get("text")
        if not isinstance(text, str) or len(text) > MAX_PROJECT_CHARS:
            raise ApiError(400, "invalid_request", f"Send the requirements as text, at most {MAX_PROJECT_CHARS:,} characters.")
        try:
            templates.render(text, {})
        except TemplateError as exc:
            raise ApiError(400, "invalid_template", str(exc)) from exc
        path = self._prompts / "project.md"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            raise ApiError(500, "write_failed", f"The requirements could not be saved: {exc.strerror or exc}.") from exc
        return self.project()

    def prompt(self, object_id: str) -> dict:
        source, subject, job, pool = self._subject(object_id)
        text = self._prompt(source, subject, job, pool)
        return {"objectId": object_id, "prompt": text, "characters": len(text), "promptsDir": str(self._prompts)}

    def generate(self, body: dict) -> dict:
        if not self.enabled():
            raise ApiError(403, "llm_disabled", "Generation is switched off on this server (MIGRATION_LLM=off).")
        available = self._model.available()
        if not available.get("available"):
            raise ApiError(409, "llm_unavailable", available.get("message") or "Claude Code is not available on this machine.")
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS or not all(isinstance(i, str) and i for i in ids):
            raise ApiError(400, "invalid_request", f"Send between 1 and {MAX_IDS} object ids.")
        results = []
        for oid in dict.fromkeys(ids):
            with self._lock:
                busy = (self._jobs.get(oid) or {}).get("state") in (QUEUED, RUNNING)
            if busy:
                results.append({"objectId": oid, "state": "queued", "error": None})
                continue
            try:
                source, subject, job, pool = self._subject(oid)
                prompt = self._prompt(source, subject, job, pool)
            except ApiError as exc:
                if len(ids) == 1:
                    raise
                results.append({"objectId": oid, "state": "rejected", "error": exc.message})
                continue
            with self._lock:
                self._jobs[oid] = {"state": QUEUED, "error": None, "startedAt": _now()}
            if self._synchronous:
                self._work(source, subject, prompt)
            else:
                self._pool.submit(self._work, source, subject, prompt)
            results.append({"objectId": oid, "state": QUEUED, "error": None})
        return {"items": results}

    def _work(self, source: Source, subject: context.Subject, prompt: str) -> None:
        oid = source.id
        with self._lock:
            self._jobs[oid] = {**self._jobs.get(oid, {}), "state": RUNNING}
        try:
            answer = self._model.complete(prompt)
            found = checks.run(subject.kind, answer.code, answer.unconverted, _original(source))
            record = self._store.get(oid) or {"objectId": oid}
            record.update({"name": source.name, "type": source.type, "kind": subject.kind})
            record["draft"] = {
                "code": answer.code, "notes": answer.notes, "unconverted": answer.unconverted, "confidence": answer.confidence,
                "checks": found, "sourceHash": subject.source_hash, "generatedAt": _now(), "model": answer.model,
                "costUsd": answer.cost_usd, "durationMs": answer.duration_ms,
            }
            self._store.put(record)
        except ModelError as exc:
            with self._lock:
                self._jobs[oid] = {**self._jobs.get(oid, {}), "state": FAILED_JOB, "error": str(exc)}
            return
        except Exception as exc:  # noqa: BLE001 - one draft never takes the service down
            with self._lock:
                self._jobs[oid] = {**self._jobs.get(oid, {}), "state": FAILED_JOB, "error": f"Unexpected {type(exc).__name__}."}
            return
        with self._lock:
            self._jobs.pop(oid, None)

    def accept(self, body: dict) -> dict:
        oid = str(body.get("id") or "")
        source, subject, _, _ = self._subject(oid)
        record = self._store.get(oid) or {}
        draft = record.get("draft")
        if not draft:
            raise ApiError(409, "no_draft", "There is no draft to accept. Generate one first.")
        if draft.get("sourceHash") != subject.source_hash:
            raise ApiError(409, "draft_stale", "The source changed since this draft was generated. Generate a new draft.")
        code = body.get("code", draft["code"])
        if not isinstance(code, str) or not code.strip():
            raise ApiError(400, "invalid_request", "The code to accept is empty.")
        edited = code.strip() != draft["code"].strip()
        found = checks.run(subject.kind, code, draft.get("unconverted") or [], _original(source)) if edited else draft.get("checks") or []
        if checks.blocking(found):
            raise ApiError(400, "checks_failed", "It cannot be accepted yet: " + " ".join(c["message"] for c in found if c["level"] == "error"))
        record["accepted"] = {
            "code": code.strip(), "notes": draft.get("notes") or [], "unconverted": draft.get("unconverted") or [],
            "confidence": draft.get("confidence"), "checks": found, "sourceHash": subject.source_hash,
            "generatedAt": draft.get("generatedAt"), "acceptedAt": _now(), "edited": edited, "model": draft.get("model"),
        }
        record["draft"] = None
        record.update({"name": source.name, "type": source.type, "kind": subject.kind})
        self._store.put(record)
        return self.object(oid)

    def discard(self, body: dict) -> dict:
        oid = str(body.get("id") or "")
        scope = str(body.get("scope") or "draft")
        if scope not in ("draft", "all"):
            raise ApiError(400, "invalid_request", "scope must be draft or all.")
        with self._lock:
            if (self._jobs.get(oid) or {}).get("state") in (QUEUED, RUNNING):
                raise ApiError(409, "transform_running", "A draft is being generated for this object; wait for it to finish.")
            self._jobs.pop(oid, None)
        record = self._store.get(oid)
        if record is not None:
            if scope == "all" or not record.get("accepted"):
                self._store.delete(oid)
            else:
                record["draft"] = None
                self._store.put(record)
        return self.object(oid)

    # -- for a migration run ---------------------------------------------------------------

    def apply(self, sources: List[Source]) -> List[str]:
        """Attach each accepted, current transform to its plan item. Returns run-log lines."""
        log: List[str] = []
        for source in sources:
            if source.kind not in context.TRANSFORMABLE_KINDS:
                continue
            record = self._store.get(source.id)
            accepted = (record or {}).get("accepted")
            if not accepted:
                continue
            try:
                current = context.extract(source).source_hash
            except context.NotTransformable:
                continue
            if accepted.get("sourceHash") != current:
                log.append(f"{source.name}: the accepted LLM-assisted transform is out of date (the source changed); the rules are used instead.")
                continue
            source.override = {"code": accepted["code"], "acceptedAt": accepted.get("acceptedAt"), "edited": bool(accepted.get("edited"))}
        return log
