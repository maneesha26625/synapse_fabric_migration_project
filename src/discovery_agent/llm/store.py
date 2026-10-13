"""Drafts and accepted transforms, one JSON file per object.

Kept on disk so a restart, or a new discovery of the same workspace, does not lose reviewed work,
and so the team can keep the accepted code under version control. Nothing secret is stored here:
only object names, source hashes, generated code and the reviewer's notes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


def _filename(object_id: str, name: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:60].strip("._") or "object"
    return f"{readable}.{hashlib.sha1(object_id.encode('utf-8')).hexdigest()[:12]}.json"


class TransformStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.Lock()
        self._index: Optional[Dict[str, Path]] = None

    def _load_index(self) -> Dict[str, Path]:
        if self._index is None:
            index: Dict[str, Path] = {}
            if self.directory.is_dir():
                for path in self.directory.glob("*.json"):
                    try:
                        oid = json.loads(path.read_text(encoding="utf-8")).get("objectId")
                    except (OSError, ValueError):
                        continue
                    if isinstance(oid, str):
                        index[oid] = path
            self._index = index
        return self._index

    def get(self, object_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            path = self._load_index().get(object_id)
            if path is None:
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

    def put(self, record: Dict[str, Any]) -> None:
        with self._lock:
            index = self._load_index()
            path = index.get(record["objectId"]) or self.directory / _filename(record["objectId"], str(record.get("name") or ""))
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            index[record["objectId"]] = path

    def delete(self, object_id: str) -> bool:
        with self._lock:
            path = self._load_index().pop(object_id, None)
            if path is None:
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return True

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(self._load_index())
        return [r for r in (self.get(i) for i in ids) if r is not None]
