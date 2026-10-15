"""Who is acting, and which chain of work an operation belongs to.

Two values travel with every operation: a **correlation id** that ties a request to everything it starts (a
discovery, a run, a validation, an LLM draft, a Fabric Git commit), and the **actor** that asked for it. They
live in context variables, so a log line or an audit record written deep inside a stage handler carries them
without every function in between passing them along.

A background thread does not inherit a request's context on its own: ``spawn`` starts one that does.

There is no sign-in to the accelerator itself, so the person is the operator of the machine: the
``MIGRATION_OPERATOR`` setting, else the account the API runs as. The Azure and Fabric accounts the session is
signed in with are added by the audit trail, which knows where to read them.
"""

from __future__ import annotations

import contextlib
import contextvars
import getpass
import os
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional

PERSON, AGENT, SYSTEM = "person", "agent", "system"
_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


@dataclass(frozen=True)
class Actor:
    kind: str  # person | agent | system
    id: str

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "id": self.id}


_SYSTEM = Actor(SYSTEM, "accelerator")
_correlation: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("migration_correlation", default=None)
_actor: contextvars.ContextVar[Optional[Actor]] = contextvars.ContextVar("migration_actor", default=None)
_project: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("migration_project", default=None)
_run: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("migration_run", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def accept_correlation_id(value: Optional[str]) -> str:
    """A correlation id a caller sent (``X-Correlation-Id``), if it is a plain id; otherwise a new one."""
    text = str(value or "").strip()
    return text if _ID.match(text) else new_correlation_id()


def correlation_id() -> Optional[str]:
    return _correlation.get()


def operator() -> Actor:
    """The person at this machine: ``MIGRATION_OPERATOR``, else the account the API runs as."""
    configured = " ".join(str(os.environ.get("MIGRATION_OPERATOR") or "").split())[:120]
    if configured:
        return Actor(PERSON, configured)
    try:
        return Actor(PERSON, getpass.getuser() or "operator")
    except Exception:  # noqa: BLE001 - no account name is an answer, not an error
        return Actor(PERSON, "operator")


def actor() -> Actor:
    """Who is acting now. Outside any request, the accelerator itself."""
    return _actor.get() or _SYSTEM


def project_id() -> Optional[str]:
    return _project.get()


def run_id() -> Optional[str]:
    return _run.get()


def fields() -> Dict[str, Optional[str]]:
    """The context a log line or an event carries."""
    who = actor()
    return {"correlationId": correlation_id(), "actor": f"{who.kind}:{who.id}", "project": project_id(), "run": run_id()}


@contextlib.contextmanager
def bound(correlation: Optional[str] = None, actor: Optional[Actor] = None, project: Optional[str] = None,
          run: Optional[str] = None) -> Iterator[None]:
    """Run a block with these values; any left as None keeps what is already set."""
    tokens = []
    for var, value in ((_correlation, correlation), (_actor, actor), (_project, project), (_run, run)):
        if value is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def system() -> Actor:
    return _SYSTEM


def thread(target: Callable[..., Any], *args: Any, name: Optional[str] = None, daemon: bool = True, **values: Any) -> threading.Thread:
    """A thread, not yet started, that carries the current context, with ``values`` (``run=``, ``actor=``...) bound on top."""
    snapshot = contextvars.copy_context()

    def work() -> None:
        with bound(**values):
            target(*args)

    return threading.Thread(target=snapshot.run, args=(work,), name=name, daemon=daemon)


def spawn(target: Callable[..., Any], *args: Any, name: Optional[str] = None, daemon: bool = True, **values: Any) -> threading.Thread:
    """Start a thread that carries the current context (``thread``), and return it."""
    started = thread(target, *args, name=name, daemon=daemon, **values)
    started.start()
    return started


def carried(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``fn`` wrapped to run in the context it was wrapped in: for an executor's worker threads."""
    snapshot = contextvars.copy_context()

    def call(*args: Any, **kwargs: Any) -> Any:
        return snapshot.copy().run(fn, *args, **kwargs)

    return call
