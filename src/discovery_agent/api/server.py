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
    GET    /api/consult/facts             features discovery found, for the consultation's questions
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
    POST   /api/migration/start           start a run from the migration plan
    POST   /api/migration/control         pause | resume | retry | reset
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

import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlsplit

from discovery_agent.api.repository_source import MAX_PARAMETERS_BYTES, MAX_ZIP_BYTES, sweep
from discovery_agent.diagnostics import record_unexpected

from discovery_agent.api.fabric import FabricError, FabricTarget
from discovery_agent.api.persist import StateStore
from discovery_agent.api.migration import MigrationService
from discovery_agent.api.service import ApiError, Session
from discovery_agent.api.transform import MAX_ACCEPT_BYTES, TransformService

MAX_BODY_BYTES = 64 * 1024
#: A repository request may carry an environment's parameters file.
MAX_REPOSITORY_BODY_BYTES = MAX_PARAMETERS_BYTES + 64 * 1024
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
RESULTS_PREFIX = "/api/discovery/results/"
TRANSFORM_OBJECT_PREFIX = "/api/transform/objects/"
TRANSFORM_PROMPT_PREFIX = "/api/transform/prompt/"


def make_handler(
    session: Session,
    static_root: Optional[Path],
    fabric: Optional[FabricTarget] = None,
    migration: Optional[MigrationService] = None,
    transforms: Optional[TransformService] = None,
):
    fabric = fabric or FabricTarget()
    transforms = transforms or TransformService(session, fabric)
    migration = migration or MigrationService(session, fabric, transforms=transforms)

    class Handler(BaseHTTPRequestHandler):
        server_version = "MigrationAcceleratorAPI"
        sys_version = ""

        # -- plumbing ------------------------------------------------------

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            # Method, path and status only. Never a body, never a header.
            sys.stderr.write(f"{self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}\n")

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _error(self, status: int, code: str, message: str, reference: Optional[str] = None) -> None:
            error = {"code": code, "message": message}
            if reference:
                error["reference"] = reference
            self._send(status, {"error": error})

        def _host_ok(self) -> bool:
            header = self.headers.get("Host") or ""
            host = header.split("]")[0] + "]" if header.startswith("[") else header.rsplit(":", 1)[0]
            return host in _LOOPBACK_HOSTS

        def _json_body(self, limit: int = MAX_BODY_BYTES) -> dict:
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ApiError(415, "unsupported_media_type", "Requests must be application/json.")
            length = int(self.headers.get("Content-Length") or 0)
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
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/zip":
                raise ApiError(415, "unsupported_media_type", "Upload the export as application/zip.")
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                raise ApiError(400, "invalid_zip", "The upload is empty.")
            if length > MAX_ZIP_BYTES:
                raise ApiError(413, "body_too_large", f"The ZIP is larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB.")
            return self.rfile.read(length)

        def _dispatch(self, handler) -> None:
            if not self._host_ok():
                self._error(403, "forbidden_host", "Requests must use a loopback host name.")
                return
            try:
                self._send(200, handler())
            except (ApiError, FabricError) as exc:
                self._error(exc.status, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - never surface a traceback to a browser
                # The traceback goes to the API's log, redacted; the browser gets only the reference to find it by.
                reference = record_unexpected(f"{self.command} {urlsplit(self.path).path}", exc)
                self._error(500, "internal_error", f"The server hit an unexpected problem (reference {reference}). "
                            "The details are in the API's log.", reference)

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
            elif path == "/api/consult/facts":
                self._dispatch(session.consultation_facts)
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
                "/api/transform/generate": lambda: transforms.generate(self._json_body()),
                "/api/transform/accept": lambda: transforms.accept(self._json_body(MAX_ACCEPT_BYTES)),
                "/api/transform/discard": lambda: transforms.discard(self._json_body()),
                "/api/transform/project": lambda: transforms.save_project(self._json_body()),
                "/api/discovery/start": session.start_discovery,
            }
            handler = routes.get(path)
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


def create_server(host: str, port: int, static_root: Optional[Path] = None) -> ThreadingHTTPServer:
    """The API as it runs for real: the last discovery and Fabric target are restored from, and saved to, disk."""
    # The uploads and working copies of an API process that has ended (a restart, or --reload) are read by nobody now.
    removed = sweep()
    if removed:
        sys.stderr.write(f"Removed {removed} uploaded ZIP and working-copy folder(s) left by API processes that have ended.\n")
    store = StateStore()
    return ThreadingHTTPServer((host, port), make_handler(Session(store), static_root, fabric=FabricTarget(store)))
