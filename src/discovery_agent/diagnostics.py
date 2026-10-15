"""Unexpected errors, written down where someone can find them.

The API never shows a traceback to a browser, and a background thread (discovery, a migration run, validation,
an LLM draft, a Fabric Git commit) must never die of one. Both used to swallow it, so an unexpected error left
no trace anywhere. Now each one is logged, redacted, under a short reference, through the ``discovery_agent``
loggers: to the API's console (or ``.accelerator/api.log`` when it runs as the scheduled task) and its
structured log file, with the correlation id of the request or run it happened in. The message the person
sees quotes the reference.
"""

from __future__ import annotations

import logging
import traceback
import uuid

_log = logging.getLogger("discovery_agent.errors")


def redacted(text: str) -> str:
    """``text`` with anything credential-shaped masked, the way the connection layer masks its own messages."""
    from discovery_agent.connections.validation import redact  # noqa: PLC0415 - the connection layer is heavy

    return redact(text)


def record_unexpected(where: str, exc: BaseException) -> str:
    """Log ``exc`` with its traceback, redacted, under a new reference, and return the reference."""
    reference = uuid.uuid4().hex[:8]
    # Redacted here, not only by the log filter, so it is safe even where logging was never configured.
    detail = redacted("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
    try:
        _log.error("[%s] %s: unexpected %s\n%s", reference, redacted(where), type(exc).__name__, detail,
                   extra={"reference": reference, "where": redacted(where), "errorType": type(exc).__name__})
    except Exception:  # noqa: BLE001 - a broken log never turns one error into two
        pass
    return reference


def unexpected_message(what: str, exc: BaseException, reference: str) -> str:
    """The sentence a person sees: what stopped, the kind of error, and the reference to find it in the API's log."""
    return f"{what} ({type(exc).__name__}, reference {reference}). The details are in the API's log."
