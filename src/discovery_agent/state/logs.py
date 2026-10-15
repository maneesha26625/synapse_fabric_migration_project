"""One place that decides what the accelerator's logs look like.

``configure`` is called once, by the API's entry point, and is safe to call again (handlers are replaced, not
stacked). It gives the ``discovery_agent`` loggers two outputs:

* the **console**: readable lines, ``time LEVEL [correlation] logger: message``;
* a **file**: one JSON object per line, rotated by size (``.accelerator/logs/api.jsonl``, 10 MB, 5 kept;
  ``MIGRATION_LOG_DIR`` to move it, ``MIGRATION_LOG=off`` for the console only).

Every line carries the correlation id, the actor, the project and the run it belongs to (``state.context``),
and every line, tracebacks included, passes through the same redaction the connection layer uses before it
is written. Nothing here is a substitute for not logging secrets in the first place: request bodies, headers
and credentials are never logged, and SQL statements and prompts appear only as their kind and their hash.

Without ``configure`` (the tests, or the library used on its own) nothing is set up: warnings and errors reach
the console through Python's default, and everything else is quiet.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from discovery_agent.state import context

ROOT = "discovery_agent"
REPO_ROOT = Path(__file__).resolve().parents[3]
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
_CONSOLE = "%(asctime)s %(levelname)-7s [%(correlation_id)s] %(name)s: %(message)s"
_OFF = {"off", "0", "false", "no"}
#: Attributes every log record has; anything else on a record was passed in ``extra`` and goes into the JSON line.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "correlation_id",
                                                                             "actor", "project", "run_id"}


def get(name: str) -> logging.Logger:
    """The logger for one part of the accelerator: ``get("api")`` is ``discovery_agent.api``."""
    return logging.getLogger(f"{ROOT}.{name}")


def _redact(text: str) -> str:
    from discovery_agent.connections.validation import redact  # noqa: PLC0415 - the connection layer is heavy

    return redact(text)


class ContextFilter(logging.Filter):
    """Put the correlation id, actor, project and run on every record, so the formats can always name them."""

    def filter(self, record: logging.LogRecord) -> bool:
        values = context.fields()
        record.correlation_id = values["correlationId"] or "-"
        record.actor = values["actor"]
        record.project = values["project"] or "-"
        record.run_id = values["run"] or "-"
        return True


class RedactFilter(logging.Filter):
    """Mask anything credential-shaped in a record's message, its traceback and its extra fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _redact(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001 - a record that cannot be formatted is kept as it came, without its arguments
            record.msg, record.args = _redact(str(record.msg)), ()
        if record.exc_info:
            record.exc_text = _redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        for key, value in list(vars(record).items()):
            if key not in _STANDARD and isinstance(value, str):
                setattr(record, key, _redact(value))
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, the context, and any extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        line: Dict[str, Any] = {
            "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name, "message": record.getMessage(),
            "correlationId": getattr(record, "correlation_id", None), "actor": getattr(record, "actor", None),
            "project": getattr(record, "project", None), "run": getattr(record, "run_id", None),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                line[key] = value if isinstance(value, (str, int, float, bool, type(None), list, dict)) else str(value)
        if record.exc_text:
            line["exception"] = record.exc_text
        return json.dumps(line, ensure_ascii=False, default=str)


def log_dir() -> Path:
    return Path(os.environ.get("MIGRATION_LOG_DIR") or REPO_ROOT / ".accelerator" / "logs")


def configure(directory: Optional[Path] = None, level: Optional[str] = None) -> Optional[Path]:
    """Install the console and file handlers on the ``discovery_agent`` loggers. Returns the log file, if any."""
    logger = logging.getLogger(ROOT)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    resolved = str(level or os.environ.get("MIGRATION_LOG_LEVEL") or "INFO").upper()
    logger.setLevel(getattr(logging, resolved, logging.INFO))
    logger.propagate = False
    shared = (ContextFilter(), RedactFilter())

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_CONSOLE))
    for f in shared:
        console.addFilter(f)
    logger.addHandler(console)

    if str(os.environ.get("MIGRATION_LOG") or "on").strip().lower() in _OFF:
        return None
    folder = directory or log_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "api.jsonl"
        rotating = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    except OSError as exc:
        logger.warning("The log file could not be opened in %s (%s); logging to the console only.", folder, exc)
        return None
    rotating.setFormatter(JsonFormatter())
    for f in shared:
        rotating.addFilter(f)
    logger.addHandler(rotating)
    return path


def reset() -> None:
    """Take the handlers ``configure`` installed off again (for the tests that configure)."""
    logger = logging.getLogger(ROOT)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
