"""What the API remembers across a restart.

The server restarts itself when its code changes (see ``supervisor``), and can
be restarted by hand. Without this module a restart would forget the sign-ins,
the discovery and the migration run, and every step would have to be done
again. Instead each part is written to its own file whenever it changes, and
read back when the server starts:

* ``source`` -- the Synapse sign-in and connection (names and ids only);
* ``discovery`` -- what the last discovery read, before it was indexed: the
  index is rebuilt from it by the code that is running now;
* ``fabric`` -- the Fabric target connection;
* ``migration`` -- the migration run's record and the planner's runs.

Never written: a token, a secret or a password. Sign-ins come back through what
already keeps them -- the interactive browser sign-in's authentication record
and encrypted token cache, the Azure and Fabric CLIs' own sessions. Connection
credentials typed for a run stay in memory; the page sends them again when it
resumes or retries a run.

The files live in ``$SYNAPSE_DISCOVERY_HOME`` (else ``~/.synapse-discovery``)
under ``api-state/<port>``, readable by this user only. They are pickles: the
discovery is a deep tree of frozen models, and pickle keeps it whole without a
second, hand-written serialisation of every one of them. Each file lists the
fields of every model class in it; a file whose classes have changed shape
since is set aside and that part starts fresh, while the other parts are still
restored. A write goes to a temporary file that is then renamed over the old
one, so a restart in the middle of a write keeps the previous copy.
"""

from __future__ import annotations

import dataclasses
import importlib
import io
import os
import pickle
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Optional, Tuple

from discovery_agent.connections.azure import ENV_HOME

#: The layout of the envelope around each part. Bumped only if the envelope itself changes.
FORMAT = 1
SUFFIX = ".state"
#: A snapshot that returns this leaves the saved part as it is (nothing changed since it was written).
UNCHANGED = object()


def default_directory(port: int) -> Path:
    """``$SYNAPSE_DISCOVERY_HOME`` (else ``~/.synapse-discovery``), then ``api-state/<port>``."""
    home = os.environ.get(ENV_HOME, "").strip()
    base = Path(home) if home else Path.home() / ".synapse-discovery"
    return base / "api-state" / str(port)


def _frozen(items: dict) -> MappingProxyType:
    """Rebuilds a read-only mapping: pickle cannot name the mappingproxy type by itself."""
    return MappingProxyType(items)


def _fields(cls: type) -> Tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(cls))


class _Pickler(pickle.Pickler):
    """Pickles read-only mappings, and notes the fields of every model class it writes."""

    def __init__(self, file: io.BytesIO) -> None:
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self.shapes: Dict[str, Tuple[str, ...]] = {}

    def reducer_override(self, obj: Any) -> Any:
        if isinstance(obj, MappingProxyType):
            return _frozen, (dict(obj),)
        cls = type(obj)
        if dataclasses.is_dataclass(cls):
            key = f"{cls.__module__}:{cls.__qualname__}"
            if key not in self.shapes:
                self.shapes[key] = _fields(cls)
        return NotImplemented


def _misfit(shapes: Dict[str, Tuple[str, ...]]) -> Optional[str]:
    """None when every class a part was saved with still has the same fields, else the first that changed."""
    for key, fields in shapes.items():
        module, _, qualname = key.partition(":")
        try:
            target: Any = importlib.import_module(module)
            for name in qualname.split("."):
                target = getattr(target, name)
        except Exception:  # noqa: BLE001 - moved, renamed or removed
            return f"{qualname} no longer exists"
        if not (isinstance(target, type) and dataclasses.is_dataclass(target)) or _fields(target) != tuple(fields):
            return f"{qualname} has changed"
    return None


def encode(value: Any) -> bytes:
    """One part, with the shapes of the classes it holds, ready to write."""
    buffer = io.BytesIO()
    pickler = _Pickler(buffer)
    pickler.dump(value)
    envelope = {
        "format": FORMAT,
        "savedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shapes": pickler.shapes,
        "body": buffer.getvalue(),
    }
    return pickle.dumps(envelope, protocol=pickle.HIGHEST_PROTOCOL)


def decode(data: bytes) -> Any:
    """The value ``encode`` wrote. Raises ValueError when it no longer fits the running code."""
    envelope = pickle.loads(data)
    if not isinstance(envelope, dict) or envelope.get("format") != FORMAT:
        raise ValueError("it was written by another version of the accelerator")
    problem = _misfit(envelope.get("shapes") or {})
    if problem:
        raise ValueError(problem)
    return pickle.loads(envelope["body"])


def _log(message: str) -> None:
    sys.stderr.write(message + "\n")


class StateStore:
    """One folder of saved parts. Safe from any thread; no method raises."""

    def __init__(self, directory: Path, log: Callable[[str], None] = _log) -> None:
        self.directory = Path(directory)
        self._log = log
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}

    def path(self, name: str) -> Path:
        return self.directory / f"{name}{SUFFIX}"

    def _part(self, name: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    def save(self, name: str, snapshot: Callable[[], Any]) -> bool:
        """Write what ``snapshot()`` returns as part ``name``; None removes the part,
        ``UNCHANGED`` keeps it as it is.

        The snapshot is taken under this part's lock, so the last save of a part
        always holds its latest state. A failed save costs the next restart its
        memory of this part, never the request that changed it."""
        with self._part(name):
            try:
                value = snapshot()
                if value is UNCHANGED:
                    return True
                if value is None:
                    self._remove(self.path(name))
                else:
                    self._write(self.path(name), encode(value))
                return True
            except Exception as exc:  # noqa: BLE001 - never fail the caller over a save
                self._log(f"Could not save the {name} state ({type(exc).__name__}: {str(exc)[:200]}). "
                          "A restart would start it fresh.")
                return False

    def load(self, name: str) -> Any:
        """The saved part, or None when there is none or it no longer fits this code (it is then set aside)."""
        path = self.path(name)
        with self._part(name):
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError as exc:
                self._log(f"Could not read the saved {name} state ({exc.strerror or exc}); it starts fresh.")
                return None
            try:
                return decode(data)
            except Exception as exc:  # noqa: BLE001 - an old or damaged file costs this part, nothing else
                reason = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
                self._set_aside(path)
                self._log(f"The saved {name} state does not fit this version of the code ({reason[:200]}); it starts fresh.")
                return None

    def clear(self, name: str) -> None:
        with self._part(name):
            self._remove(self.path(name))

    # -- files ---------------------------------------------------------------

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))  # mode 0600
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(5):
                try:
                    os.replace(temp, path)
                    return
                except PermissionError:  # Windows: a scanner can hold the old file for a moment
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError:
                    pass

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _set_aside(self, path: Path) -> None:
        try:
            os.replace(path, path.with_name(path.name + ".unreadable"))
        except OSError:
            self._remove(path)
