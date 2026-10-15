"""The projects the backend owns, one active at a time.

A project is one migration engagement: its own discovery, plan versions, runs, validation and audit trail,
each in the project's folder (``<state>/projects/<id>/``). Switching the active project is, for everything
the backend keeps, like restarting into that project: its saved discovery and Fabric target come back the
way they do after a restart, and the Synapse sign-in stays where it is. The browser keeps only what it
always kept per project (the plan being edited, stage options and step confirmations), under the same id.

The registry (``<state>/projects.json``) is a versioned document, so a rename or a delete sent from a page
that read an older list is refused rather than applied over someone else's change.

First start with this code: the registry is created with the ``default`` project, and the discovery and
Fabric target saved by the earlier, single-project code are copied into it. The originals are left where
they were, untouched.

``MIGRATION_STATE=off``: everything is kept in a temporary folder for the life of the process, so nothing
survives a restart, as before.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from discovery_agent.state.store import LeaseHeld, StaleWrite, Store, now

DEFAULT_ID = "default"
DEFAULT_NAME = "Synapse to Fabric migration"
REPO_ROOT = Path(__file__).resolve().parents[3]
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_OFF = {"off", "0", "false", "no"}
#: The files the single-project code kept in the state folder itself, adopted by the default project.
LEGACY_FILES = ("discovery.pickle", "fabric_target.pickle")
MAX_NAME = 80


class ProjectError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def state_root() -> Path:
    return Path(os.environ.get("MIGRATION_STATE_DIR") or REPO_ROOT / ".accelerator" / "state")


def enabled() -> bool:
    return (os.environ.get("MIGRATION_STATE") or "on").strip().lower() not in _OFF


def clean_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ProjectError(400, "invalid_request", "Give the project a name.")
    if len(name) > MAX_NAME:
        raise ProjectError(400, "invalid_request", f"A project name is at most {MAX_NAME} characters.")
    return name


class Projects:
    REGISTRY = "projects"

    def __init__(self, root: Optional[Path] = None) -> None:
        self.persistent = enabled()
        if root is not None:
            self.root = Path(root)
        elif self.persistent:
            self.root = state_root()
        else:
            # Kept for this process only, and removed when it exits.
            self.root = Path(tempfile.mkdtemp(prefix="migration-state-"))
            atexit.register(shutil.rmtree, self.root, True)
        self._registry = Store(self.root)
        self._lock = threading.RLock()
        self._stores: Dict[str, Store] = {}
        if self._registry.get(self.REGISTRY) is None:
            self._adopt()

    # -- the registry ------------------------------------------------------------------------------

    def _adopt(self) -> None:
        """Create the registry with the default project, copying in what the single-project code saved."""
        folder = self.folder(DEFAULT_ID)
        folder.mkdir(parents=True, exist_ok=True)
        for name in LEGACY_FILES:
            legacy = self.root / name
            if legacy.is_file() and not (folder / name).exists():
                try:
                    shutil.copy2(legacy, folder / name)
                except OSError:
                    pass  # the project starts without it, as after a restart that could not read it
        project = {"id": DEFAULT_ID, "name": DEFAULT_NAME, "createdAt": now(), "updatedAt": now()}
        try:
            self._registry.put(self.REGISTRY, {"active": DEFAULT_ID, "projects": [project]}, expected=0)
        except StaleWrite:
            pass  # another start made it first

    def _value(self) -> Dict[str, Any]:
        value = self._registry.get(self.REGISTRY) or {}
        projects = [p for p in value.get("projects") or [] if isinstance(p, dict) and _ID.match(str(p.get("id") or ""))]
        if not projects:
            projects = [{"id": DEFAULT_ID, "name": DEFAULT_NAME, "createdAt": now(), "updatedAt": now()}]
        active = value.get("active") if any(p["id"] == value.get("active") for p in projects) else projects[0]["id"]
        return {"active": active, "projects": projects}

    def listing(self) -> Dict[str, Any]:
        with self._lock:
            value = self._value()
            return {**value, "version": self._registry.version(self.REGISTRY)}

    def active_id(self) -> str:
        return self.listing()["active"]

    def get(self, project_id: str) -> Dict[str, Any]:
        found = next((p for p in self.listing()["projects"] if p["id"] == project_id), None)
        if found is None:
            raise ProjectError(404, "project_not_found", "There is no such project.")
        return found

    def _save(self, value: Dict[str, Any], expected: Optional[int]) -> Dict[str, Any]:
        try:
            self._registry.put(self.REGISTRY, value, expected=expected)
        except StaleWrite as exc:
            raise ProjectError(409, "stale", "The project list changed since this page read it. It has been read again; "
                                             "try once more.") from exc
        return self.listing()

    def create(self, name: Any, project_id: Optional[str] = None, expected: Optional[int] = None) -> Dict[str, Any]:
        """A new project. ``project_id`` keeps a browser's own id, so what it already holds under it carries on."""
        name = clean_name(name)
        with self._lock:
            value = self._value()
            # Lower case, and compared that way: on Windows `Default` and `default` are one folder, and two projects
            # sharing a folder would corrupt each other's records (or archive the other one's on a delete).
            ident = (str(project_id or "").strip() or f"p{uuid.uuid4().hex[:10]}").lower()
            if not _ID.match(ident):
                raise ProjectError(400, "invalid_request", "A project id is letters, digits, '-' and '_' only.")
            if any(p["id"].lower() == ident for p in value["projects"]):
                raise ProjectError(409, "project_exists", "There is already a project with this id.")
            value["projects"].append({"id": ident, "name": name, "createdAt": now(), "updatedAt": now()})
            self.folder(ident).mkdir(parents=True, exist_ok=True)
            listing = self._save(value, expected)
            return {**listing, "created": ident}

    def rename(self, project_id: str, name: Any, expected: Optional[int] = None) -> Dict[str, Any]:
        name = clean_name(name)
        with self._lock:
            value = self._value()
            project = next((p for p in value["projects"] if p["id"] == project_id), None)
            if project is None:
                raise ProjectError(404, "project_not_found", "There is no such project.")
            project.update(name=name, updatedAt=now())
            return self._save(value, expected)

    def remove(self, project_id: str, expected: Optional[int] = None) -> Dict[str, Any]:
        """Take a project off the list. Its folder is archived, never deleted: its audit trail is kept."""
        with self._lock:
            value = self._value()
            if not any(p["id"] == project_id for p in value["projects"]):
                raise ProjectError(404, "project_not_found", "There is no such project.")
            if len(value["projects"]) == 1:
                raise ProjectError(409, "last_project", "The last project cannot be deleted.")
            if value["active"] == project_id:
                raise ProjectError(409, "project_active", "Switch to another project before deleting this one.")
            value["projects"] = [p for p in value["projects"] if p["id"] != project_id]
            listing = self._save(value, expected)
            source = self.folder(project_id)
            if source.exists():
                target = self.root / "archive" / f"{project_id}-{now().replace(':', '').replace('-', '')}"
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
                except OSError:
                    pass  # left where it is: still never read again, since the project is off the list
            with self._lock:
                self._stores.pop(project_id, None)
            return listing

    def activate(self, project_id: str, expected: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            value = self._value()
            if not any(p["id"] == project_id for p in value["projects"]):
                raise ProjectError(404, "project_not_found", "There is no such project.")
            value["active"] = project_id
            return self._save(value, expected)

    # -- each project's folder ---------------------------------------------------------------------

    def folder(self, project_id: Optional[str] = None) -> Path:
        ident = project_id or self.active_id()
        if not _ID.match(ident):
            raise ProjectError(400, "invalid_request", "Not a project id.")
        return self.root / "projects" / ident

    def store(self, project_id: Optional[str] = None) -> Store:
        """The durable record of a project (the active one by default)."""
        ident = project_id or self.active_id()
        with self._lock:
            if ident not in self._stores:
                self._stores[ident] = Store(self.folder(ident))
            return self._stores[ident]

    def pickle_store(self, project_id: Optional[str] = None) -> Any:
        """Where the project's discovery and Fabric target are cached between starts (``api.persist``)."""
        from discovery_agent.api.persist import StateStore  # noqa: PLC0415 - persist imports nothing of ours

        return StateStore(self.folder(project_id))

    # -- one API process per state folder ----------------------------------------------------------

    def claim_process(self, description: str) -> Dict[str, Any]:
        """Make this process the one that writes this state folder. Raises ``ProjectError`` when another API that
        is still running holds it (a second copy, on another port): two writers would corrupt each other."""
        try:
            return self._registry.acquire("process", description)
        except LeaseHeld as exc:
            raise ProjectError(409, "state_in_use", (
                f"Another Migration Accelerator API (process {exc.holder.get('pid')}, {exc.holder.get('owner')}) is using "
                f"{self.root}. Stop it, or start this one with MIGRATION_STATE_DIR set to another folder.")) from exc

    def release_process(self, description: str) -> None:
        self._registry.release("process", description)

    def stores(self) -> List[Store]:
        return [self.store(p["id"]) for p in self.listing()["projects"]]
