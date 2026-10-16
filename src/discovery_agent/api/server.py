"""The HTTP surface the UI talks to.

Standard library only: the project's core has no runtime dependencies and a
web framework would be the first. The surface is small and fixed.

    GET    /api/health
    GET    /api/connections               current connection state
    POST   /api/connections/authenticate  prove an Azure identity
    POST   /api/connections/test          prove workspace access; enables discovery
    POST   /api/connections/authenticate-directory  sign in to one more directory the account belongs to
    POST   /api/connections/upload        a ZIP of a Synapse repository (application/zip)
    POST   /api/connections/repository    use one environment's definitions from Git or an uploaded ZIP
    DELETE /api/connections               forget the connection
    POST   /api/discovery/start
    GET    /api/discovery/status
    DELETE /api/discovery                 forget the last discovery's results
    GET    /api/discovery/results         paged, filtered, sorted
    GET    /api/discovery/results/{id}    one object, in full
    GET    /api/dependencies              dependency graph + suggested waves
    GET    /api/mapping/components        Synapse -> Fabric component table
    GET    /api/discovery/export          the whole inventory, for download
    GET    /api/azure/{subscriptions|resource-groups|workspaces|sql-pools}   pickers, from the Azure sign-in
    GET    /api/azure/workspace-git       the Git repository a Synapse workspace is integrated with
    GET    /api/fabric/connection         Fabric target state (never a token)
    POST   /api/fabric/authenticate       Azure CLI / Fabric CLI login + workspace list
    POST   /api/fabric/workspaces         refresh the workspace list
    POST   /api/fabric/test               verify the selected workspace
    DELETE /api/fabric/connection         forget the Fabric target
    GET    /api/fabric/git                the target workspace's Fabric Git branch, changes, pull-request link
    POST   /api/fabric/git/commit         commit the migrated items to that branch (background)
    GET    /api/migration/capabilities    the migration stages, their options and the linked services
    GET    /api/migration/run             the current migration run
    POST   /api/migration/plan            planner: strategy, risks, effort, checks (read-only)
    POST   /api/migration/validate        start validation in the background: compare Synapse with Fabric, and check what runs
    GET    /api/migration/validation      the validation's progress, then its results
    POST   /api/migration/start           start a run from the migration plan (or ask for its sign-off)
    POST   /api/migration/approve         a person signs a waiting run off, or declines it
    POST   /api/migration/control         pause | resume | retry | reset
    GET    /api/migration/runs            every run of the project, newest first
    GET    /api/migration/runs/{id}       one run, as its record has it
    GET    /api/migration/runs/{id}/events  its events in order (after=, limit=)
    GET    /api/migration/runs/{id}/artifacts/{hash}  what it sent to Fabric for an object (a definition, a statement)
    GET    /api/migration/plans/{id}      one recorded plan version
    GET    /api/migration/policy          run and object statuses, their moves, the sign-off rule
    GET    /api/consult                   the consultation: every question as it stands, what is next, the brief
    GET    /api/consult/catalog           the questions, as the page shows them
    POST   /api/consult/answer            answer a question (or preview what the answer would change)
    POST   /api/consult/skip              skip a question: its recommendation stands, as an assumption
    POST   /api/consult/withdraw          withdraw an answer (the record keeps it)
    POST   /api/consult/accept            accept the recommendations, all or one section's
    POST   /api/consult/resolve           resolve a contradiction: take the finding, or keep the answers with a reason
    GET    /api/consult/brief             the brief's draft, versions and status
    POST   /api/consult/brief/submit      submit the brief: a new, immutable version
    POST   /api/consult/brief/approve     a person approves or rejects the version waiting
    GET    /api/consult/brief/export      one version as Markdown or JSON
    GET    /api/consult/policy            the policy in force: what Plan pre-sets and Migrate enforces
    POST   /api/consult/deviation         confirm a plan that differs from the brief, with the reason
    POST   /api/consult/propose           answers read from notes (AI, or rules), for a person to confirm
    POST   /api/consult/proposals         confirm or reject proposed answers
    POST   /api/consult/explain           what a question means for this estate
    GET    /api/consult/questionnaire     the questions, to answer offline (Markdown or JSON)
    GET    /api/consult/context           MIGRATION_CONTEXT.md: the approved brief, as every LLM draft is sent it
    GET    /api/consult/context/{hash}    one that a draft was made with, by its hash (Markdown)
    POST   /api/consult/questionnaire     import a questionnaire's answers, proposed for a person to confirm
    GET    /api/projects                  the projects, and which is active
    POST   /api/projects                  create one (optionally carrying on another's saved discovery)
    POST   /api/projects/{id}/rename      rename one
    POST   /api/projects/{id}/activate    make it the active project
    DELETE /api/projects/{id}             take it off the list (its folder is archived)
    GET    /api/audit                     the project's audit trail, newest first
    GET    /api/audit/export              the trail, as JSON lines or CSV
    GET    /api/audit/verify              check the trail's hash chain
    POST   /api/journey/confirm           a person confirmed a step of the journey
    GET    /api/transform/settings        whether LLM drafts can be generated here (Claude Code CLI)
    GET    /api/transform                 every object with a draft, an accepted transform or a job
    GET    /api/transform/objects/{id}    one object: source, rule output, draft, accepted code
    GET    /api/transform/prompt/{id}     the exact prompt a draft is generated from
    GET    /api/transform/candidates      objects a draft can be made for, those the rules cannot convert first
    GET    /api/transform/project         the project requirements every prompt carries
    POST   /api/transform/project         replace the project requirements
    POST   /api/transform/generate        queue drafts for objects (background)
    POST   /api/transform/accept          accept a draft, optionally edited
    POST   /api/transform/discard         drop a draft, or go back to the rule-based conversion
    GET    /api/orchestrator/plan         the execution plan: every step, its status, what the agent proposes
    POST   /api/orchestrator/steps/mark   a person marks a task of the plan done (or not)
    GET    /api/orchestrator/actions      the agent's requests (?status=open | all)
    POST   /api/orchestrator/actions/decide  a person confirms (it runs as them) or rejects a request
    POST   /api/orchestrator/supervisor   pause | resume the agent (the kill switch), or wake it now
    GET    /api/orchestrator/tools        the tool registry: every tool, its tier and arguments
    GET    /api/orchestrator/diagnosis/{id}  why an object of the current run failed, and what may be done
    GET    /api/orchestrator/report       the sign-off report (?format=md | json)
    GET    /api/assistant/tasks           the assistant's questions, newest first
    GET    /api/assistant/tasks/{id}      one question, with its conversation and action cards
    POST   /api/assistant/ask             ask a question, or reply to one waiting ({text, taskId?})
    POST   /api/assistant/tasks/{id}/cancel    stop a question
    POST   /api/assistant/tasks/{id}/continue  carry on a question a restart paused

Security posture, in order of importance:

* Binds to loopback by default, and rejects a ``Host`` header that is not
  loopback (DNS-rebinding defence).
* State-changing requests must be ``application/json`` (a ZIP upload,
  ``application/zip``). A cross-site HTML form cannot send either without a
  CORS preflight, which is never granted, so a malicious page cannot drive
  the API through the operator's browser.
* Request bodies are capped, never logged, and never echoed. Error responses
  carry a stable code and a message written for a person -- no traceback. An
  unexpected error's traceback is written, redacted, to the API's log under a
  short reference, and the response carries only that reference.
* No token, secret or authorization header is ever returned: the connection
  layer that holds them is not reachable from here.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, unquote, urlsplit

from discovery_agent.api.repository_source import MAX_PARAMETERS_BYTES, MAX_ZIP_BYTES, sweep
from discovery_agent.diagnostics import record_unexpected
from discovery_agent.state import context, logs

_log = logs.get("api")

from discovery_agent.api.consult import ConsultService
from discovery_agent.api.fabric import FabricError, FabricTarget
from discovery_agent.api.migration import MigrationService
from discovery_agent.api.orchestrator import OrchestratorService, switched_on as agent_switched_on
from discovery_agent.api.projects import ProjectsService, describe_start
from discovery_agent.api.service import ApiError, Session
from discovery_agent.api.transform import MAX_ACCEPT_BYTES, TransformService
from discovery_agent.state.projects import ProjectError, Projects

MAX_BODY_BYTES = 64 * 1024
#: A repository request may carry an environment's parameters file.
MAX_REPOSITORY_BODY_BYTES = MAX_PARAMETERS_BYTES + 64 * 1024
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
RESULTS_PREFIX = "/api/discovery/results/"
TRANSFORM_OBJECT_PREFIX = "/api/transform/objects/"
TRANSFORM_PROMPT_PREFIX = "/api/transform/prompt/"
_RUN_PATH = re.compile(r"^/api/migration/runs/([0-9]{1,9})(/events)?$")
_ARTIFACT_PATH = re.compile(r"^/api/migration/runs/([0-9]{1,9})/artifacts/([0-9a-f]{64})$")
_PLAN_PATH = re.compile(r"^/api/migration/plans/(P[0-9]{1,9})$")
_CONTEXT_PATH = re.compile(r"^/api/consult/context/([0-9a-f]{64})$")
_TASK_PATH = re.compile(r"^/api/assistant/tasks/([0-9a-f]{12})(?:/(cancel|continue))?$")
DIAGNOSIS_PREFIX = "/api/orchestrator/diagnosis/"
_PROJECT_PATH = re.compile(r"^/api/projects/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:/(rename|activate))?$")


class RawResponse:
    """A reply that is not JSON: a file to download."""

    def __init__(self, body: bytes, content_type: str, filename: Optional[str] = None) -> None:
        self.body, self.content_type, self.filename = body, content_type, filename


def make_handler(
    session: Session,
    static_root: Optional[Path],
    fabric: Optional[FabricTarget] = None,
    migration: Optional[MigrationService] = None,
    transforms: Optional[TransformService] = None,
    project_of: Optional[Callable[[], Optional[str]]] = None,
    projects: Optional[ProjectsService] = None,
    consult: Optional[ConsultService] = None,
    orchestrator: Optional[OrchestratorService] = None,
):
    fabric = fabric or FabricTarget()
    consult = consult or ConsultService(session, fabric)
    transforms = transforms or TransformService(session, fabric, consult=consult)
    migration = migration or MigrationService(session, fabric, transforms=transforms, consult=consult)
    orchestrator = orchestrator or OrchestratorService(session, migration, consult=consult, transforms=transforms, fabric=fabric)
    if projects is not None and project_of is None:
        project_of = projects.active_id
    # Every request passes the projects' gate, so a switch of project runs alone (``ProjectsService.activate``).
    gate = projects.gate.request if projects is not None else contextlib.nullcontext

    def need_projects() -> ProjectsService:
        if projects is None:
            raise ApiError(404, "not_found", "Projects are not served by this API.")
        return projects

    class Handler(BaseHTTPRequestHandler):
        server_version = "MigrationAcceleratorAPI"
        sys_version = ""

        # -- plumbing ------------------------------------------------------

        _correlation: Optional[str] = None
        _status = 0

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            # The request line is logged by _dispatch, with its duration and correlation id. Never a body or a header.
            pass

        def log_error(self, format: str, *args) -> None:  # noqa: A002
            _log.warning("HTTP %s", format % args)

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
            self._status = status
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if self._correlation:
                self.send_header("X-Correlation-Id", self._correlation)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_raw(self, reply: RawResponse) -> None:
            self._status = 200
            self.send_response(200)
            self.send_header("Content-Type", reply.content_type)
            self.send_header("Content-Length", str(len(reply.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if reply.filename:
                self.send_header("Content-Disposition", f'attachment; filename="{reply.filename}"')
            if self._correlation:
                self.send_header("X-Correlation-Id", self._correlation)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(reply.body)

        def _error(self, status: int, code: str, message: str, reference: Optional[str] = None) -> None:
            error = {"code": code, "message": message}
            if reference:
                error["reference"] = reference
            if self._correlation:
                error["correlationId"] = self._correlation
            self._send(status, {"error": error})

        def _host_ok(self) -> bool:
            header = self.headers.get("Host") or ""
            host = header.split("]")[0] + "]" if header.startswith("[") else header.rsplit(":", 1)[0]
            return host in _LOOPBACK_HOSTS

        def _length(self) -> int:
            try:
                return max(0, int(self.headers.get("Content-Length") or 0))
            except ValueError:
                raise ApiError(400, "invalid_request", "Content-Length is not a number.") from None

        def _drain(self, length: int) -> None:
            """Read a body that is refused, when it is small: a reply sent over unread bytes can reach the client as a
            reset connection (Windows), so the refusal would never be seen."""
            if 0 < length <= MAX_BODY_BYTES:
                self.rfile.read(length)

        def _json_body(self, limit: int = MAX_BODY_BYTES) -> dict:
            length = self._length()
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._drain(length)
                raise ApiError(415, "unsupported_media_type", "Requests must be application/json.")
            if length > limit:
                raise ApiError(413, "body_too_large", "The request is too large.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError as exc:
                raise ApiError(400, "invalid_json", "The request body is not valid JSON.") from exc
            if not isinstance(body, dict):
                raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
            return body

        def _zip_body(self) -> bytes:
            length = self._length()
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/zip":
                self._drain(length)
                raise ApiError(415, "unsupported_media_type", "Upload the export as application/zip.")
            if not length:
                raise ApiError(400, "invalid_zip", "The upload is empty.")
            if length > MAX_ZIP_BYTES:
                raise ApiError(413, "body_too_large", f"The ZIP is larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB.")
            return self.rfile.read(length)

        def _dispatch(self, handler) -> None:
            # One correlation id per request (the caller's X-Correlation-Id when it sends a plain one): it is in the
            # response's header, on every log line the request writes, and on the background work it starts.
            started = time.monotonic()
            self._correlation = context.accept_correlation_id(self.headers.get("X-Correlation-Id"))
            path = urlsplit(self.path).path
            with context.bound(correlation=self._correlation, actor=context.operator()):
                if not self._host_ok():
                    self._error(403, "forbidden_host", "Requests must use a loopback host name.")
                    self._logged(path, started)
                    return
                # Let in past a switch of project that may be running; only then is the project it works in read.
                with gate(), context.bound(project=project_of() if project_of else None):
                    self._answer(handler, path)
                    self._logged(path, started)

        def _answer(self, handler, path: str) -> None:
            try:
                result = handler()
                if isinstance(result, RawResponse):
                    self._send_raw(result)
                else:
                    self._send(200, result)
            except (ApiError, FabricError, ProjectError) as exc:
                self._error(exc.status, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - never surface a traceback to a browser
                # The traceback goes to the API's log, redacted; the browser gets only the reference to find it by.
                reference = record_unexpected(f"{self.command} {path}", exc)
                self._error(500, "internal_error", f"The server hit an unexpected problem (reference {reference}). "
                            "The details are in the API's log.", reference)

        def _logged(self, path: str, started: float) -> None:
            elapsed = int((time.monotonic() - started) * 1000)
            _log.info("%s %s -> %s (%d ms)", self.command, path, self._status, elapsed,
                      extra={"method": self.command, "path": path, "status": self._status, "durationMs": elapsed})

        # -- verbs ---------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            path = parts.path
            if not path.startswith("/api/"):
                self._static(path)
                return
            query = dict(parse_qsl(parts.query))
            if path == "/api/health":
                self._dispatch(session.health)
            elif path == "/api/connections":
                self._dispatch(session.connection_state)
            elif path == "/api/fabric/connection":
                self._dispatch(fabric.state)
            elif path == "/api/fabric/git":
                self._dispatch(migration.git_state)
            elif path == "/api/migration/capabilities":
                self._dispatch(migration.capabilities)
            elif path == "/api/migration/validation":
                self._dispatch(migration.validation_state)
            elif path == "/api/migration/run":
                self._dispatch(migration.state)
            elif path == "/api/migration/runs":
                self._dispatch(migration.runs)
            elif path == "/api/migration/policy":
                self._dispatch(migration.policy)
            elif path == "/api/consult":
                self._dispatch(consult.state)
            elif path == "/api/consult/catalog":
                self._dispatch(consult.catalog_view)
            elif path == "/api/consult/brief":
                self._dispatch(consult.brief)
            elif path == "/api/consult/brief/export":
                self._dispatch(lambda: RawResponse(*consult.export(query)))
            elif path == "/api/consult/policy":
                self._dispatch(consult.policy_view)
            elif path == "/api/consult/questionnaire":
                self._dispatch(lambda: RawResponse(*consult.questionnaire(query)))
            elif path == "/api/consult/context":
                self._dispatch(consult.context_view)
            elif path == "/api/orchestrator/plan":
                self._dispatch(orchestrator.plan_view)
            elif path == "/api/orchestrator/actions":
                self._dispatch(lambda: orchestrator.actions_view(query))
            elif path == "/api/orchestrator/tools":
                self._dispatch(orchestrator.tools_view)
            elif path == "/api/orchestrator/report":
                self._dispatch(lambda: RawResponse(*orchestrator.report(query)))
            elif path.startswith(DIAGNOSIS_PREFIX):
                object_id = unquote(path[len(DIAGNOSIS_PREFIX):])
                self._dispatch(lambda: orchestrator.diagnosis(object_id))
            elif path == "/api/assistant/tasks":
                self._dispatch(orchestrator.tasks_view)
            elif _TASK_PATH.match(path) and not _TASK_PATH.match(path).group(2):
                task_id = _TASK_PATH.match(path).group(1)
                self._dispatch(lambda: orchestrator.task_view(task_id))
            elif _CONTEXT_PATH.match(path):
                digest = _CONTEXT_PATH.match(path).group(1)
                self._dispatch(lambda: RawResponse(*consult.context_export(digest)))
            elif _RUN_PATH.match(path):
                run_id, events = _RUN_PATH.match(path).groups()
                if events:
                    self._dispatch(lambda: migration.run_events(run_id, query.get("after"), query.get("limit")))
                else:
                    self._dispatch(lambda: migration.run_record(run_id))
            elif _ARTIFACT_PATH.match(path):
                run_id, digest = _ARTIFACT_PATH.match(path).groups()
                self._dispatch(lambda: RawResponse(*migration.artifact(run_id, digest)))
            elif _PLAN_PATH.match(path):
                version_id = _PLAN_PATH.match(path).group(1)
                self._dispatch(lambda: migration.plan_version(version_id))
            elif path == "/api/projects":
                self._dispatch(lambda: need_projects().listing())
            elif path == "/api/audit":
                self._dispatch(lambda: need_projects().audit_query(query))
            elif path == "/api/audit/verify":
                self._dispatch(lambda: need_projects().audit_verify(query))
            elif path == "/api/audit/export":
                self._dispatch(lambda: RawResponse(*need_projects().audit_export(query)))
            elif path.startswith("/api/azure/"):
                self._dispatch(lambda: session.azure_options(path[len("/api/azure/"):], query))
            elif path == "/api/dependencies":
                self._dispatch(session.dependency_graph)
            elif path == "/api/mapping/components":
                self._dispatch(session.components)
            elif path == "/api/discovery/export":
                self._dispatch(session.export)
            elif path == "/api/discovery/status":
                self._dispatch(session.discovery_status)
            elif path == "/api/discovery/results":
                self._dispatch(lambda: session.results(query))
            elif path == "/api/transform/settings":
                self._dispatch(transforms.settings)
            elif path == "/api/transform/candidates":
                self._dispatch(transforms.candidates)
            elif path == "/api/transform/project":
                self._dispatch(transforms.project)
            elif path == "/api/transform":
                self._dispatch(transforms.list)
            elif path.startswith(TRANSFORM_OBJECT_PREFIX):
                object_id = unquote(path[len(TRANSFORM_OBJECT_PREFIX):])
                self._dispatch(lambda: transforms.object(object_id))
            elif path.startswith(TRANSFORM_PROMPT_PREFIX):
                object_id = unquote(path[len(TRANSFORM_PROMPT_PREFIX):])
                self._dispatch(lambda: transforms.prompt(object_id))
            elif path.startswith(RESULTS_PREFIX):
                object_id = unquote(path[len(RESULTS_PREFIX):])
                self._dispatch(lambda: session.result(object_id))
            else:
                self._error(404, "not_found", "No such endpoint.")

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            routes = {
                "/api/connections/authenticate": lambda: session.authenticate(self._json_body()),
                "/api/connections/test": lambda: session.test(self._json_body()),
                "/api/connections/authenticate-directory": lambda: session.authenticate_directory(self._json_body()),
                "/api/connections/repository": lambda: session.connect_repository(self._json_body(MAX_REPOSITORY_BODY_BYTES)),
                "/api/connections/upload": lambda: session.upload_zip(self._zip_body(), dict(parse_qsl(urlsplit(self.path).query)).get("name", "")),
                "/api/fabric/authenticate": lambda: fabric.authenticate(self._json_body()),
                "/api/fabric/workspaces": lambda: fabric.refresh_workspaces(self._json_body()),
                "/api/fabric/test": lambda: fabric.test(self._json_body()),
                "/api/fabric/git/commit": lambda: migration.git_commit(self._json_body()),
                "/api/migration/plan": lambda: migration.analyze(self._json_body()),
                "/api/migration/validate": lambda: migration.start_validation(self._json_body()),
                "/api/migration/start": lambda: migration.start(self._json_body()),
                "/api/migration/control": lambda: migration.control(self._json_body()),
                "/api/migration/approve": lambda: migration.approve(self._json_body()),
                "/api/consult/answer": lambda: consult.answer(self._json_body()),
                "/api/consult/skip": lambda: consult.skip(self._json_body()),
                "/api/consult/withdraw": lambda: consult.withdraw(self._json_body()),
                "/api/consult/accept": lambda: consult.accept(self._json_body()),
                "/api/consult/resolve": lambda: consult.resolve(self._json_body()),
                "/api/consult/brief/submit": lambda: consult.submit(self._json_body()),
                "/api/consult/brief/approve": lambda: consult.approve(self._json_body()),
                "/api/consult/deviation": lambda: consult.deviation(self._json_body()),
                "/api/consult/propose": lambda: consult.propose(self._json_body()),
                "/api/consult/proposals": lambda: consult.decide(self._json_body()),
                "/api/consult/explain": lambda: consult.explain(self._json_body()),
                "/api/consult/questionnaire": lambda: consult.import_answers(self._json_body()),
                "/api/projects": lambda: need_projects().create(self._json_body()),
                "/api/journey/confirm": lambda: need_projects().confirm_step(self._json_body()),
                "/api/transform/generate": lambda: transforms.generate(self._json_body()),
                "/api/transform/accept": lambda: transforms.accept(self._json_body(MAX_ACCEPT_BYTES)),
                "/api/transform/discard": lambda: transforms.discard(self._json_body()),
                "/api/transform/project": lambda: transforms.save_project(self._json_body()),
                "/api/discovery/start": session.start_discovery,
                "/api/orchestrator/steps/mark": lambda: orchestrator.mark(self._json_body()),
                "/api/orchestrator/actions/decide": lambda: orchestrator.decide(self._json_body()),
                "/api/orchestrator/supervisor": lambda: orchestrator.control(self._json_body()),
                "/api/assistant/ask": lambda: orchestrator.ask(self._json_body()),
            }
            handler = routes.get(path)
            task = _TASK_PATH.match(path)
            if handler is None and task and task.group(2):
                task_id, verb = task.groups()
                handler = (lambda: orchestrator.cancel(task_id)) if verb == "cancel" else (lambda: orchestrator.resume_task(task_id))
            found = _PROJECT_PATH.match(path)
            if handler is None and found and found.group(2):
                project_id, verb = found.groups()
                handler = (lambda: need_projects().rename(project_id, self._json_body())) if verb == "rename" else \
                    (lambda: need_projects().activate(project_id, self._json_body()))
            if handler is None:
                self._error(404, "not_found", "No such endpoint.")
                return
            self._dispatch(handler)

        def do_DELETE(self) -> None:  # noqa: N802
            if urlsplit(self.path).path == "/api/connections":
                self._dispatch(session.disconnect)
            elif urlsplit(self.path).path == "/api/discovery":
                self._dispatch(session.reset_discovery)
            elif urlsplit(self.path).path == "/api/fabric/connection":
                self._dispatch(fabric.disconnect)
            elif _PROJECT_PATH.match(urlsplit(self.path).path) and not _PROJECT_PATH.match(urlsplit(self.path).path).group(2):
                project_id = _PROJECT_PATH.match(urlsplit(self.path).path).group(1)
                query = dict(parse_qsl(urlsplit(self.path).query))
                self._dispatch(lambda: need_projects().remove(project_id, query))
            else:
                self._error(404, "not_found", "No such endpoint.")

        # -- built frontend --------------------------------------------------

        def _static(self, path: str) -> None:
            if static_root is None or not static_root.is_dir():
                self._error(404, "not_found", "The frontend has not been built. Run `npm run build` in frontend/, or use the Vite dev server.")
                return
            relative = unquote(path).lstrip("/") or "index.html"
            target = (static_root / relative).resolve()
            root = static_root.resolve()
            if root not in target.parents and target != root:
                self._error(403, "forbidden", "Not allowed.")
                return
            if not target.is_file():
                target = root / "index.html"  # SPA fallback for client-side routes
            data = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def create_server(host: str, port: int, static_root: Optional[Path] = None, projects: Optional[Projects] = None) -> ThreadingHTTPServer:
    """The API as it runs for real. Everything a project keeps (its discovery, Fabric target, runs, plan versions,
    validation and audit trail) is restored from, and saved to, the active project's folder."""
    # The uploads and working copies of an API process that has ended (a restart, or --reload) are read by nobody now.
    removed = sweep()
    if removed:
        _log.info("Removed %d uploaded ZIP and working-copy folder(s) left by API processes that have ended.", removed)
    projects = projects or Projects()
    hub = ProjectsService(projects)
    active = hub.active_id()
    with context.bound(actor=context.system(), project=active):
        session = Session(projects.pickle_store(active), records=projects.store(active), audit=hub.audit)
        fabric = FabricTarget(projects.pickle_store(active), audit=hub.audit)
        consult = ConsultService(session, fabric, records=projects.store(active), audit=hub.audit)
        transforms = TransformService(session, fabric, audit=hub.audit, consult=consult)
        migration = MigrationService(session, fabric, transforms=transforms, records=projects.store(active), audit=hub.audit,
                                     consult=consult)
        orchestrator = OrchestratorService(session, migration, consult=consult, transforms=transforms, fabric=fabric,
                                           records=projects.store(active), audit=hub.audit,
                                           trail=lambda: hub.audit_verify({}), project_of=hub.active_id)
        hub.attach(session, fabric, migration, transforms, consult, orchestrator)
        # The supervisor wakes on what the trail records: runs, sign-offs, briefs, validations. Nothing calls it directly.
        hub.listen(orchestrator.notify)
        hub.audit("system.started", details=describe_start(projects))
    if agent_switched_on():
        orchestrator.start()
    server = ThreadingHTTPServer((host, port), make_handler(session, static_root, fabric=fabric, migration=migration,
                                                            transforms=transforms, projects=hub, consult=consult,
                                                            orchestrator=orchestrator))
    server.projects = hub  # type: ignore[attr-defined] - for the entry point, which records the stop
    server.orchestrator = orchestrator  # type: ignore[attr-defined] - stopped with the server
    return server
