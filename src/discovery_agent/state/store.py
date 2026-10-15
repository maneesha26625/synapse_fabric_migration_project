"""A project's durable record, kept as plain files in one folder.

Four kinds of thing, and nothing else:

* **documents** (``<name>.json``): a value with a version. A write can say which version it read
  (``expected``); if the document has moved on since, the write is refused (``StaleWrite``), so two pages
  never silently overwrite each other. Written to a temporary file and renamed into place, so a crash leaves
  the old document or the new one, never half of either.
* **streams** (``<name>.jsonl``): append-only records, one JSON object per line, each with a contiguous
  ``seq``. There is no operation that changes or removes a record: a correction is a new record. A line cut
  short by a crash is ignored when the stream is read.
* **blobs** (``blobs/<hash>``): bytes stored under their SHA-256, such as a definition a run sent to Fabric.
* **leases** (``leases/<key>.json``): "this is being done, by this process". A lease whose process has ended
  is taken over (and the caller is told it was), so a crash never leaves work locked for ever.

Only one API process writes a state folder: ``projects.Projects.claim_process`` makes sure of that. Within
the process every operation holds a lock. The format is JSON throughout, so it outlives any change to the
code that wrote it, and every document says which schema it was written in.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from discovery_agent.state import processes

SCHEMA = 1
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}(/[A-Za-z0-9][A-Za-z0-9._-]{0,120}){0,4}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,160}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StaleWrite(Exception):
    """A document changed since it was read; the write was refused."""

    def __init__(self, name: str, expected: int, current: int) -> None:
        super().__init__(f"{name} changed since it was read (version {expected}; it is now {current}). Read it again.")
        self.name, self.expected, self.current = name, expected, current


class LeaseHeld(Exception):
    """Someone else is doing this now."""

    def __init__(self, key: str, holder: Dict[str, Any]) -> None:
        super().__init__(f"{key} is held by {holder.get('owner')} (process {holder.get('pid')}) since {holder.get('acquiredAt')}.")
        self.key, self.holder = key, holder


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        self._next: Dict[str, int] = {}

    # -- names ---------------------------------------------------------------------------------

    def _path(self, name: str, suffix: str) -> Path:
        if not _NAME.match(name) or ".." in name:
            raise ValueError(f"Not a store name: {name!r}")
        *folders, last = name.split("/")
        return self.root.joinpath(*folders) / (last + suffix)

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    # -- documents -----------------------------------------------------------------------------

    def _read_doc(self, name: str) -> Optional[Dict[str, Any]]:
        path = self._path(name, ".json")
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None  # unreadable: treated as absent, and written afresh by the next put
        return doc if isinstance(doc, dict) and "value" in doc else None

    def get(self, name: str, default: Any = None) -> Any:
        with self._lock:
            doc = self._read_doc(name)
        return default if doc is None else doc["value"]

    def version(self, name: str) -> int:
        with self._lock:
            doc = self._read_doc(name)
        return int(doc.get("version") or 0) if doc else 0

    def put(self, name: str, value: Any, expected: Optional[int] = None) -> int:
        """Write a document; with ``expected``, only if it is still at that version. Returns the new version."""
        with self._lock:
            doc = self._read_doc(name)
            current = int(doc.get("version") or 0) if doc else 0
            if expected is not None and int(expected) != current:
                raise StaleWrite(name, int(expected), current)
            version = current + 1
            body = {"schema": SCHEMA, "version": version, "savedAt": now(), "value": value}
            self._write_atomic(self._path(name, ".json"), json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"))
            return version

    def delete(self, name: str) -> None:
        with self._lock:
            try:
                self._path(name, ".json").unlink()
            except FileNotFoundError:
                pass

    def names(self, folder: str) -> List[str]:
        """The documents directly in ``folder`` (a store name), without their suffix."""
        base = self.root.joinpath(*folder.split("/")) if folder else self.root
        with self._lock:
            return sorted(p.stem for p in base.glob("*.json")) if base.is_dir() else []

    # -- streams -------------------------------------------------------------------------------

    def _lines(self, path: Path) -> Iterator[Dict[str, Any]]:
        # Read as bytes, and decode each line on its own: a line a crash cut short can end inside a character that
        # takes several bytes, and must cost that line only, never the stream.
        try:
            with open(path, "rb") as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw.decode("utf-8"))
                    except ValueError:  # UnicodeDecodeError is one: a line a crash cut short is never half-read
                        continue
                    if isinstance(record, dict) and isinstance(record.get("seq"), int):
                        yield record
        except FileNotFoundError:
            return

    def append(self, stream: str, record: Dict[str, Any]) -> Dict[str, Any]:
        """Add a record to a stream and return it with its ``seq``: 1, 2, 3 ... with no gaps."""
        path = self._path(stream, ".jsonl")
        with self._lock:
            if stream not in self._next:
                last = 0
                for existing in self._lines(path):
                    last = max(last, existing["seq"])
                self._next[stream] = last + 1
            entry = {**record, "seq": self._next[stream]}
            line = json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell():
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")  # after a line a crash cut short, start clean
                handle.write(line.encode("utf-8"))
                handle.flush()
            self._next[stream] += 1
            return entry

    def records(self, stream: str, after: int = 0, limit: Optional[int] = None, newest_first: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            found = [r for r in self._lines(self._path(stream, ".jsonl")) if r["seq"] > after]
        if newest_first:
            found.reverse()
        return found[:limit] if limit is not None else found

    def seq(self, stream: str) -> int:
        """The ``seq`` of the stream's last record (0 when it has none), without reading it again each time."""
        with self._lock:
            if stream not in self._next:
                last = 0
                for existing in self._lines(self._path(stream, ".jsonl")):
                    last = max(last, existing["seq"])
                self._next[stream] = last + 1
            return self._next[stream] - 1

    def last(self, stream: str) -> Optional[Dict[str, Any]]:
        found = self.records(stream, newest_first=True, limit=1)
        return found[0] if found else None

    # -- blobs ---------------------------------------------------------------------------------

    def put_blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "blobs" / digest[:2] / digest
        with self._lock:
            if not path.exists():
                self._write_atomic(path, data)
        return digest

    def blob(self, digest: str) -> Optional[bytes]:
        if not _HASH.match(digest or ""):
            return None
        try:
            return (self.root / "blobs" / digest[:2] / digest).read_bytes()
        except OSError:
            return None

    # -- leases --------------------------------------------------------------------------------

    def _lease_path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise ValueError(f"Not a lease key: {key!r}")
        return self.root / "leases" / (key.replace(":", "_") + ".json")

    def holder(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            held = json.loads(self._lease_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return held if isinstance(held, dict) else None

    @staticmethod
    def _mine(held: Dict[str, Any]) -> bool:
        """Taken by this process (not an earlier one that had the same id)."""
        return held.get("pid") == os.getpid() and held.get("started") in (None, processes.SELF_STARTED)

    @staticmethod
    def _living(held: Dict[str, Any]) -> bool:
        return processes.same(int(held.get("pid") or 0), held.get("started"))

    def acquire(self, key: str, owner: str) -> Dict[str, Any]:
        """Take ``key`` for ``owner``. Returns the lease, with ``reclaimed`` set to the previous holder when it took
        over one whose process had ended. Raises ``LeaseHeld`` when a living process holds it for someone else."""
        path = self._lease_path(key)
        with self._lock:
            reclaimed = None
            held = self.holder(key)
            if held is not None:
                same = held.get("owner") == owner and self._mine(held)
                if not same and self._living(held):
                    raise LeaseHeld(key, held)
                reclaimed = None if same else held
            lease = {"key": key, "owner": owner, "pid": os.getpid(), "started": processes.SELF_STARTED,
                     "acquiredAt": now(), "heartbeatAt": now()}
            self._write_atomic(path, json.dumps(lease).encode("utf-8"))
            return {**lease, "reclaimed": reclaimed}

    def heartbeat(self, key: str, owner: str) -> None:
        with self._lock:
            held = self.holder(key)
            if held and held.get("owner") == owner and self._mine(held):
                held["heartbeatAt"] = now()
                self._write_atomic(self._lease_path(key), json.dumps(held).encode("utf-8"))

    def release(self, key: str, owner: str) -> bool:
        """Give ``key`` back. Only its owner, in this process, can."""
        with self._lock:
            held = self.holder(key)
            if not held or held.get("owner") != owner or not self._mine(held):
                return False
            try:
                self._lease_path(key).unlink()
            except FileNotFoundError:
                pass
            return True

    def leases(self) -> List[Dict[str, Any]]:
        folder = self.root / "leases"
        out = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                held = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(held, dict):
                out.append({**held, "alive": self._living(held)})
        return out
