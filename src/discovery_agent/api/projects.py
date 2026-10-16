"""The projects the backend owns, switching between them, and each one's audit trail.

    GET    /api/projects                  {active, projects, version, persistent}
    POST   /api/projects                  {name, id?, version?, activate?, copyFrom?} -> a new project
    POST   /api/projects/{id}/rename      {name, version?}
    POST   /api/projects/{id}/activate    {version?} -> every service now holds that project
    DELETE /api/projects/{id}?version=    take it off the list; its folder is archived, never deleted
    GET    /api/audit                     the active project's trail, newest first (filters; ``before`` pages back)
    GET    /api/audit/export?format=      jsonl (verifiable elsewhere) | csv
    GET    /api/audit/verify              walk the trail's hash chain
    POST   /api/journey/confirm           {step, note?}: a person confirmed a step of the journey

Switching is refused while anything of the project being left is still working (a discovery, a sign-in window,
a run's pass, a validation, a Fabric Git commit, an LLM draft), so nothing that finishes later lands in the
wrong project. A switch runs alone, like the restart it stands for: it waits for the requests already being
answered, and holds new ones until it is done, so no request reads one project and writes the next. A switch
that fails half way is undone: each service that switched goes back to the project it held.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from discovery_agent.api.service import ApiError
from discovery_agent.state import context
from discovery_agent.state.audit import Auditor
from discovery_agent.state.projects import LEGACY_FILES, ProjectError, Projects

#: The steps of the journey a person confirms, as the page names them.
JOURNEY_STEPS = ("discover", "assess", "consult", "waves", "plan", "migrate", "validate")
#: How long a switch waits for the requests already being answered before it gives up (and says so).
SWITCH_WAIT_SECONDS = 15.0
_log = logging.getLogger("discovery_agent.projects")


class RequestGate:
    """Requests are answered side by side; a project switch runs alone.

    ``request`` wraps each request the API answers. ``alone`` waits until no other request is being answered
    (the caller's own, if it is one, aside), and holds new ones at the door until it is done. Background work
    (a run's pass, a discovery) is not a request: a switch is refused while any of it works, by each service."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._answering = 0
        self._alone = False
        self._mine = threading.local()

    @contextlib.contextmanager
    def request(self) -> Iterator[None]:
        with self._cond:
            while self._alone:
                self._cond.wait()
            self._answering += 1
        self._mine.inside = getattr(self._mine, "inside", 0) + 1
        try:
            yield
        finally:
            self._mine.inside -= 1
            with self._cond:
                self._answering -= 1
                self._cond.notify_all()

    @contextlib.contextmanager
    def alone(self, timeout: float) -> Iterator[None]:
        """Run the block with no other request being answered. Raises ``TimeoutError`` when they do not finish in time."""
        own = getattr(self._mine, "inside", 0)
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._alone:  # another switch: one at a time
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self._cond.wait(left)
            self._alone = True
            try:
                while self._answering > own:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError
                    self._cond.wait(left)
            except BaseException:
                self._alone = False
                self._cond.notify_all()
                raise
        try:
            yield
        finally:
            with self._cond:
                self._alone = False
                self._cond.notify_all()
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _version(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_request", "version must be a whole number.") from None


def _api(exc: ProjectError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


class ProjectsService:
    def __init__(self, projects: Projects) -> None:
        self._projects = projects
        #: One switch at a time. Never taken by anything a service calls while holding a lock of its own (an audit
        #: record is written under the services' locks), or a switch waiting on a busy service would deadlock with it.
        self._switching = threading.Lock()
        #: Guards only the cache of each project's Auditor.
        self._cache = threading.Lock()
        self._auditors: Dict[str, Auditor] = {}
        self._active = projects.active_id()
        #: The projects on the list, so a record is never written for one that has been archived.
        self._known = {p["id"] for p in projects.listing()["projects"]}
        self._session: Any = None
        self._fabric: Any = None
        self._migration: Any = None
        self._transforms: Any = None
        self._consult: Any = None
        #: Every request the API answers passes through it, so a switch can run alone.
        self.gate = RequestGate()
        self.switch_wait = SWITCH_WAIT_SECONDS

    def attach(self, session: Any, fabric: Any, migration: Any, transforms: Any = None, consult: Any = None) -> None:
        """The services that hold the active project's state, and are switched with it."""
        self._session, self._fabric, self._migration, self._transforms = session, fabric, migration, transforms
        self._consult = consult

    @property
    def projects(self) -> Projects:
        return self._projects

    def active_id(self) -> str:
        return self._active

    # -- the audit trail ------------------------------------------------------------------------------

    def auditor(self, project_id: Optional[str] = None) -> Auditor:
        ident = project_id or self._active
        with self._cache:
            if ident not in self._auditors:
                self._auditors[ident] = Auditor(self._projects.store(ident))
            return self._auditors[ident]

    def audit(self, action: str, **kwargs: Any) -> Optional[dict]:
        """Record in the trail of the project the work belongs to: the one its request was made in, which travels
        with everything the request started. So work that finishes after a switch (a Fabric reconnect, say) is never
        written into the next project's trail. What the services are given; never raises."""
        try:
            ident = context.project_id()
            with self._cache:
                known = ident in self._known
            return self.auditor(ident if known else None).record(action, **kwargs)
        except Exception:  # noqa: BLE001 - recording never fails what it records (Auditor.record logs its own failures)
            return None

    def _trail_of(self, query: Dict[str, str]) -> Auditor:
        ident = str(query.get("project") or self._active)
        try:
            self._projects.get(ident)
        except ProjectError as exc:
            raise _api(exc) from exc
        return self.auditor(ident)

    def audit_query(self, query: Dict[str, str]) -> dict:
        def number(name: str, default: Optional[int]) -> Optional[int]:
            raw = query.get(name)
            if raw in (None, ""):
                return default
            try:
                return int(raw)
            except ValueError:
                raise ApiError(400, "invalid_request", f"{name} must be a whole number.") from None

        trail = self._trail_of(query)
        out = trail.query(before=number("before", None), limit=number("limit", 100) or 100, action=query.get("action") or None,
                          actor=query.get("actor") or None, target=query.get("target") or None, run=query.get("run") or None,
                          outcome=query.get("outcome") or None, since=query.get("since") or None, until=query.get("until") or None,
                          correlation=query.get("correlationId") or None)
        return {**out, "project": str(query.get("project") or self._active)}

    def audit_export(self, query: Dict[str, str]) -> Tuple[bytes, str, str]:
        """(body, media type, file name) of the trail, for download."""
        fmt = str(query.get("format") or "jsonl").lower()
        if fmt not in ("jsonl", "csv"):
            raise ApiError(400, "invalid_request", "Choose an export format of jsonl or csv.")
        ident = str(query.get("project") or self._active)
        trail = self._trail_of(query)
        body, media, count = trail.export(fmt)
        trail.record("audit.exported", target={"type": "project", "id": ident}, details={"format": fmt, "records": count})
        return body, media, f"audit-{ident}.{fmt}"

    def audit_verify(self, query: Dict[str, str]) -> dict:
        return {**self._trail_of(query).verify(), "project": str(query.get("project") or self._active)}

    def confirm_step(self, body: dict) -> dict:
        """A person confirmed a step of the journey: the trail keeps who, which step and when."""
        step = str(body.get("step") or "")
        if step not in JOURNEY_STEPS:
            raise ApiError(400, "invalid_request", f"step must be one of {', '.join(JOURNEY_STEPS)}.")
        note = " ".join(str(body.get("note") or "").split())[:500] or None
        record = self.audit("journey.step_confirmed", target={"type": "step", "id": step}, details={"step": step, "note": note})
        return {"step": step, "recorded": record is not None, "seq": (record or {}).get("seq")}

    # -- the registry -----------------------------------------------------------------------------------

    def listing(self) -> dict:
        return {**self._projects.listing(), "persistent": self._projects.persistent}

    def create(self, body: dict) -> dict:
        unknown = sorted(set(body) - {"name", "id", "version", "activate", "copyFrom"})
        if unknown:
            raise ApiError(400, "invalid_request", f"Unexpected field(s): {', '.join(unknown)}.")
        source = str(body.get("copyFrom") or "") or None
        try:
            if source is not None:
                self._projects.get(source)
            created = self._projects.create(body.get("name"), str(body.get("id") or "") or None, _version(body.get("version")))
        except ProjectError as exc:
            raise _api(exc) from exc
        ident = created["created"]
        with self._cache:
            self._known.add(ident)
        adopted = self._copy_saved(source, ident) if source else None
        name = next((p["name"] for p in created["projects"] if p["id"] == ident), "")
        # A project's trail starts with its own creation.
        with context.bound(project=ident):
            self.auditor(ident).record("project.created", target={"type": "project", "id": ident},
                                       details={"name": name, "adopted": adopted})
        if body.get("activate"):
            return {**self.activate(ident, {}), "created": ident}
        return {**self.listing(), "created": ident}

    def _copy_saved(self, source: str, target: str) -> Optional[str]:
        """Copy what a project saved between starts (its discovery and Fabric target) into a new one, so work a browser
        held under its own project carries on there. Its runs and trail are its own and are not copied."""
        if source == target:
            return None
        copied = []
        for name in LEGACY_FILES:
            origin = self._projects.folder(source) / name
            if origin.is_file():
                try:
                    shutil.copy2(origin, self._projects.folder(target) / name)
                    copied.append(name)
                except OSError:
                    pass
        return source if copied else None

    def rename(self, project_id: str, body: dict) -> dict:
        try:
            previous = self._projects.get(project_id)["name"]
            listing = self._projects.rename(project_id, body.get("name"), _version(body.get("version")))
        except ProjectError as exc:
            raise _api(exc) from exc
        name = next((p["name"] for p in listing["projects"] if p["id"] == project_id), "")
        with context.bound(project=project_id):
            self.auditor(project_id).record("project.renamed", target={"type": "project", "id": project_id},
                                            details={"name": name, "previous": previous})
        return {**listing, "persistent": self._projects.persistent}

    def remove(self, project_id: str, query: Dict[str, str]) -> dict:
        # Never beside a switch, which may be moving the services to or from this project right now.
        with self._switching:
            try:
                name = self._projects.get(project_id)["name"]
                listing = self._projects.remove(project_id, _version(query.get("version")))
            except ProjectError as exc:
                raise _api(exc) from exc
        with self._cache:
            self._auditors.pop(project_id, None)
            self._known.discard(project_id)
        # Recorded where the person is working: the project removed is archived with its own trail as it stood.
        self.audit("project.removed", target={"type": "project", "id": project_id}, details={"name": name, "archive": "archive/"})
        return {**listing, "persistent": self._projects.persistent}

    # -- switching ----------------------------------------------------------------------------------------

    def _busy(self) -> Optional[str]:
        for reason in (self._session.busy() if self._session is not None else None,
                       self._migration.busy_reason() if self._migration is not None else None,
                       self._transforms.busy() if self._transforms is not None and hasattr(self._transforms, "busy") else None):
            if reason:
                return reason
        return None

    def activate(self, project_id: str, body: dict) -> dict:
        """Make another project the active one: every service now holds what that project keeps, as a restart into it
        would. The Synapse sign-in and the CLI sessions on this machine stay as they are."""
        if not _PROJECT_ID.match(project_id or ""):
            raise ApiError(404, "project_not_found", "There is no such project.")
        try:
            self._projects.get(project_id)
        except ProjectError as exc:
            raise _api(exc) from exc
        if project_id == self._active:
            return self.listing()
        reason = self._busy()  # said at once, before waiting for anything
        if reason:
            raise ApiError(409, "project_busy", f"Not switched: {reason}")
        # The requests being answered finish first, then the switch takes the lock a delete also takes: never the
        # other way round, or a delete waiting for the lock would hold the switch up until it gave up.
        try:
            with self.gate.alone(self.switch_wait), self._switching:
                try:
                    target = self._projects.get(project_id)  # again: it may have gone while the requests finished
                except ProjectError as exc:
                    raise _api(exc) from exc
                if project_id == self._active:
                    return self.listing()
                return self._activate(project_id, target, body)
        except TimeoutError:
            raise ApiError(409, "project_busy", "Not switched: other requests are still being answered (a sign-in "
                                                "window, perhaps). Try again in a moment.") from None

    def _activate(self, project_id: str, target: Dict[str, Any], body: dict) -> dict:
        """The switch itself, with no other request being answered."""
        reason = self._busy()  # again, now that nothing else can start
        if reason:
            raise ApiError(409, "project_busy", f"Not switched: {reason}")
        previous = self._active
        try:
            self._projects.activate(project_id, _version(body.get("version")))
        except ProjectError as exc:
            raise _api(exc) from exc
        done: List[str] = []
        try:
            with context.bound(project=project_id):
                self._switch(project_id, done)
        except Exception:
            # Back to the project that was active, before the error goes to the page: the registry, and each
            # service that had switched (one that had not is left as it is, sign-in and credentials and all).
            try:
                self._projects.activate(previous)
            except ProjectError:
                pass
            with context.bound(project=previous):
                self._undo(previous, done)
            raise
        self._active = project_id
        with context.bound(project=project_id):
            self.audit("project.activated", target={"type": "project", "id": project_id},
                       details={"name": target["name"], "previous": previous})
        return self.listing()

    def _steps(self, project_id: str) -> Tuple[Tuple[str, Callable[[], None]], ...]:
        steps: Tuple[Tuple[str, Callable[[], None]], ...] = (
            ("session", lambda: self._session.switch(self._projects.pickle_store(project_id), self._projects.store(project_id))),
            ("fabric", lambda: self._fabric.switch(self._projects.pickle_store(project_id))),
            ("migration", lambda: self._migration.switch(self._projects.store(project_id))),
        )
        if self._consult is not None:
            steps += (("consult", lambda: self._consult.switch(self._projects.store(project_id))),)
        return steps

    def _switch(self, project_id: str, done: List[str]) -> None:
        for name, step in self._steps(project_id):
            step()
            done.append(name)

    def _undo(self, previous: str, done: List[str]) -> None:
        for name, step in reversed(self._steps(previous)):
            if name in done:
                try:
                    step()
                except Exception as exc:  # noqa: BLE001 - the undo goes as far as it can; the first error is what is said
                    _log.error("Could not switch the %s back to project %s: %s: %s", name, previous, type(exc).__name__, exc)


def describe_start(projects: Projects) -> Dict[str, Any]:
    """What ``system.started`` records about this start."""
    return {"pid": os.getpid(), "stateDir": str(projects.root), "persistent": projects.persistent,
            "projects": len(projects.listing()["projects"])}
