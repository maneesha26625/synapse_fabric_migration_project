"""The model, reached through the Claude Code CLI in headless mode.

``claude -p`` reads the prompt on stdin and prints one JSON result. It runs with every tool switched
off, no MCP servers, no saved session, and from an empty temporary directory, so it can only answer:
it cannot read the accelerator's files, run commands or reach Synapse or Fabric. ``--json-schema``
makes it return the answer as ``structured_output`` in that shape.

It signs in the way the CLI on this machine already is (``claude`` once, interactively), so the
accelerator never holds an API key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

_log = logging.getLogger("discovery_agent.llm")
DEFAULT_TIMEOUT = 180
CONFIDENCE = ("high", "medium", "low")
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "unconverted": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
    },
    "required": ["code", "notes", "unconverted", "confidence"],
    "additionalProperties": False,
}
SYSTEM_PROMPT = ("You convert Azure Synapse objects to Microsoft Fabric for a migration accelerator. "
                 "Answer only with the JSON object the user asks for. You have no tools and need none.")


class ModelError(Exception):
    """The model could not be reached or gave no usable answer; the message is for a person."""


@dataclass
class Answer:
    code: str
    notes: List[str] = field(default_factory=list)
    unconverted: List[str] = field(default_factory=list)
    confidence: str = "low"
    model: str = ""
    cost_usd: Optional[float] = None
    duration_ms: int = 0


def executable() -> Optional[str]:
    configured = os.environ.get("CLAUDE_CLI")
    if configured:
        return configured if os.path.isfile(configured) else shutil.which(configured)
    return shutil.which("claude")


def _strip_fences(code: str) -> str:
    text = code.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


@dataclass
class Reply:
    """A structured answer to any question (``ClaudeCli.ask``): the JSON object asked for, and what it cost."""

    data: Dict[str, Any]
    model: str = ""
    cost_usd: Optional[float] = None
    duration_ms: int = 0


def switched_on() -> bool:
    """Whether this server may call the model at all (``MIGRATION_LLM``)."""
    return (os.environ.get("MIGRATION_LLM") or "on").strip().lower() not in ("off", "0", "false", "no", "disabled")


def parse_reply(stdout: str) -> Reply:
    """The CLI's ``--output-format json`` result -> the JSON object it answered with, the model and the cost."""
    try:
        envelope = json.loads(stdout)
    except ValueError as exc:
        raise ModelError("Claude Code did not return JSON. Run `claude -p \"hello\"` in a terminal to check it works.") from exc
    if not isinstance(envelope, dict):
        raise ModelError("Claude Code returned an unexpected result.")
    if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
        reason = str(envelope.get("result") or envelope.get("subtype") or "unknown error")
        raise ModelError(f"Claude Code reported an error: {reason[:300]}")
    data: Any = envelope.get("structured_output")
    if data is None:
        try:
            data = json.loads(_strip_fences(str(envelope.get("result") or "")))
        except ValueError as exc:
            raise ModelError("Claude Code answered, but not with the JSON object that was asked for.") from exc
    if not isinstance(data, dict):
        raise ModelError("Claude Code answered, but not with the JSON object that was asked for.")
    usage = envelope.get("modelUsage") or {}
    return Reply(data=data, model=next(iter(usage), "") if isinstance(usage, dict) else "",
                 cost_usd=envelope.get("total_cost_usd") if isinstance(envelope.get("total_cost_usd"), (int, float)) else None)


def parse(stdout: str) -> Answer:
    """The CLI's ``--output-format json`` result -> an Answer."""
    reply = parse_reply(stdout)
    data = reply.data
    if not isinstance(data.get("code"), str) or not data["code"].strip():
        raise ModelError("Claude Code's answer has no converted code.")
    confidence = str(data.get("confidence") or "low").lower()
    return Answer(
        code=_strip_fences(data["code"]),
        notes=[str(n) for n in data.get("notes") or [] if str(n).strip()],
        unconverted=[str(n) for n in data.get("unconverted") or [] if str(n).strip()],
        confidence=confidence if confidence in CONFIDENCE else "low",
        model=reply.model,
        cost_usd=reply.cost_usd,
    )


class ClaudeCli:
    """One ``claude -p`` call per prompt."""

    def __init__(self, model: Optional[str] = None, timeout: Optional[int] = None,
                 run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> None:
        self.model = model if model is not None else (os.environ.get("MIGRATION_LLM_MODEL") or None)
        self.timeout = timeout or int(os.environ.get("MIGRATION_LLM_TIMEOUT") or DEFAULT_TIMEOUT)
        self._run = run

    def available(self) -> Dict[str, Any]:
        exe = executable()
        return {"available": exe is not None, "model": self.model or "Claude Code default", "timeoutSeconds": self.timeout,
                "message": None if exe else "The Claude Code CLI (`claude`) is not on this machine's PATH. Install it and sign in once, or set CLAUDE_CLI."}

    def command(self, exe: str, schema: Optional[Dict[str, Any]] = None, system: Optional[str] = None) -> List[str]:
        args = [exe, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                "--system-prompt", system or SYSTEM_PROMPT,
                "--json-schema", json.dumps(schema or RESULT_SCHEMA, separators=(",", ":"))]
        if self.model:
            args += ["--model", self.model]
        return args

    def ask(self, prompt: str, schema: Dict[str, Any], system: str) -> Reply:
        """One question answered in the JSON shape ``schema`` gives, for the consultation's help. Run like a draft
        (no tools, an empty folder) and logged like one: by the prompt's hash, never the prompt or the answer."""
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        exe = executable()
        if exe is None:
            raise ModelError(self.available()["message"])
        started = time.monotonic()
        stdout = self._invoke(self.command(exe, schema, system), prompt)
        reply = parse_reply(stdout)
        reply.duration_ms = int((time.monotonic() - started) * 1000)
        _log.info("LLM question answered by %s in %d ms", reply.model or self.model or "default", reply.duration_ms,
                  extra={"promptHash": digest, "model": reply.model or self.model or "default", "costUsd": reply.cost_usd,
                         "durationMs": reply.duration_ms})
        return reply

    def _invoke(self, args: List[str], prompt: str) -> str:
        """Run the CLI once and return what it printed; ``ModelError`` when it could not answer."""
        with tempfile.TemporaryDirectory(prefix="fabric-transform-") as workdir:
            try:
                done = self._run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                 timeout=self.timeout, cwd=workdir, check=False)
            except subprocess.TimeoutExpired as exc:
                raise ModelError(f"Claude Code did not answer within {self.timeout} seconds. Try again, or raise MIGRATION_LLM_TIMEOUT.") from exc
            except OSError as exc:
                raise ModelError(f"Claude Code could not be started: {exc.strerror or exc}.") from exc
        if done.returncode != 0 and not (done.stdout or "").strip():
            detail = (done.stderr or "").strip().splitlines()
            hint = detail[-1][:300] if detail else f"exit code {done.returncode}"
            raise ModelError(f"Claude Code failed ({hint}). If it asks you to sign in, run `claude` once in a terminal.")
        return done.stdout or ""

    def complete(self, prompt: str) -> Answer:
        """One draft. Logged by its prompt's hash, the model, the time and the cost: never the prompt or the answer."""
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        started = time.monotonic()
        try:
            answer = self._complete(prompt)
        except ModelError as exc:
            _log.warning("LLM call failed after %d ms: %s", int((time.monotonic() - started) * 1000), exc,
                         extra={"promptHash": digest, "model": self.model or "default"})
            raise
        _log.info("LLM call answered by %s in %d ms", answer.model or self.model or "default", answer.duration_ms,
                  extra={"promptHash": digest, "model": answer.model or self.model or "default", "costUsd": answer.cost_usd,
                         "durationMs": answer.duration_ms, "confidence": answer.confidence})
        return answer

    def _complete(self, prompt: str) -> Answer:
        exe = executable()
        if exe is None:
            raise ModelError(self.available()["message"])
        started = time.monotonic()
        answer = parse(self._invoke(self.command(exe), prompt))
        answer.duration_ms = int((time.monotonic() - started) * 1000)
        return answer
