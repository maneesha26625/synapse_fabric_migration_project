"""What survives an API restart: the last discovery and the Fabric target's selection.

A restart (by hand, or by ``--reload`` after a code change) used to lose everything held in memory: the
discovery results, which took minutes to produce, and the Fabric target, which took a sign-in. Now:

* the finished discovery is saved when it completes and loaded at start, so Assess, Waves, Plan, the LLM
  transforms and Migrate's Fabric stages work straight away;
* the Fabric target's *selection* (method, account, workspace) is saved after a passed test, and checked
  again silently at start with the CLI session the machine already has, never a new sign-in window.

Never saved: tokens, passwords, connection credentials, or the Synapse sign-in. The Synapse sign-in opens a
Microsoft window by design, so after a restart the source shows as signed out until someone signs in again;
it is only needed to run discovery again, load table data and count Synapse rows in Validate.

Files live in ``.accelerator/state`` beside the repository (``MIGRATION_STATE_DIR`` to move them;
``MIGRATION_STATE=off`` to keep nothing: the folder is then never read, written or emptied). They hold the
discovered definitions, like the ``transforms`` folder does, so the folder is git-ignored. They are Python
pickles: only this process writes them, and only from this folder are they read. The tests point
``MIGRATION_STATE_DIR`` at a folder of their own, so a test run never touches a real session's files.
"""

from __future__ import annotations

import logging
import os
import pickle
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
_log = logging.getLogger("discovery_agent.state")
#: Bump when a saved object's shape changes so an old file is ignored rather than half-loaded.
VERSION = 1


def _read_only(values: dict) -> MappingProxyType:
    return MappingProxyType(values)


class _Pickler(pickle.Pickler):
    """Saves read-only mappings (``MappingProxyType``), which pickle refuses on its own. A live discovery holds them:
    each Synapse artifact keeps its payload read-only, and so does each connection check."""

    def reducer_override(self, obj: Any) -> Any:
        if type(obj) is MappingProxyType:
            return _read_only, (dict(obj),)
        return NotImplemented


class StateStore:
    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = directory or Path(os.environ.get("MIGRATION_STATE_DIR") or REPO_ROOT / ".accelerator" / "state")

    @staticmethod
    def enabled() -> bool:
        return (os.environ.get("MIGRATION_STATE") or "on").strip().lower() not in ("off", "0", "false", "no")

    def _path(self, name: str) -> Path:
        return self.directory / f"{name}.pickle"

    def save(self, name: str, value: Any) -> bool:
        if not self.enabled():
            return False
        path = self._path(name)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as handle:
                _Pickler(handle, protocol=pickle.HIGHEST_PROTOCOL).dump({"version": VERSION, "value": value})
            os.replace(tmp, path)
            return True
        except Exception as exc:  # noqa: BLE001 - saving is a convenience; the API works without it
            _log.warning("Could not save %s for the next start: %s: %s", name, type(exc).__name__, str(exc)[:200])
            try:
                path.with_suffix(".tmp").unlink()
            except OSError:
                pass
            return False

    def load(self, name: str) -> Any:
        if not self.enabled():
            return None
        path = self._path(name)
        if not path.is_file():
            return None
        try:
            with open(path, "rb") as handle:
                saved = pickle.load(handle)  # noqa: S301 - written by this process, in its own folder
            if isinstance(saved, dict) and saved.get("version") == VERSION:
                return saved.get("value")
        except Exception as exc:  # noqa: BLE001 - an unreadable file (code changed shape) is ignored, not fatal
            _log.warning("Ignored the saved %s (%s); it will be saved again.", name, type(exc).__name__)
        return None

    def forget(self, name: str) -> None:
        if not self.enabled():
            return  # switched off, the folder is never read, written or emptied
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
