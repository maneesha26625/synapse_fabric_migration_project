"""Unexpected errors, written down where someone can find them.

The API never shows a traceback to a browser, and a background thread (discovery, a migration run, validation,
an LLM draft, a Fabric Git commit) must never die of one. Both used to swallow it, so an unexpected error left
no trace anywhere. Now each one is written, redacted, to the API's own output (the console it runs in, or
``.accelerator/api.log`` when it runs as the scheduled task) under a short reference, and the message the
person sees quotes that reference.
"""

from __future__ import annotations

import sys
import traceback
import uuid
from datetime import datetime, timezone


def record_unexpected(where: str, exc: BaseException) -> str:
    """Write ``exc`` with its traceback, redacted, to stderr under a new reference, and return the reference."""
    # Imported here: the connection layer is heavy, and only an error needs it.
    from discovery_agent.connections.validation import redact  # noqa: PLC0415

    reference = uuid.uuid4().hex[:8]
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        sys.stderr.write(f"{stamp} ERROR [{reference}] {redact(where)}: unexpected {type(exc).__name__}\n{redact(detail)}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - a closed console never turns one error into two
        pass
    return reference


def redacted(text: str) -> str:
    """``text`` with anything credential-shaped masked, the way the connection layer masks its own messages."""
    from discovery_agent.connections.validation import redact  # noqa: PLC0415

    return redact(text)


def unexpected_message(what: str, exc: BaseException, reference: str) -> str:
    """The sentence a person sees: what stopped, the kind of error, and the reference to find it in the API's log."""
    return f"{what} ({type(exc).__name__}, reference {reference}). The details are in the API's log."
