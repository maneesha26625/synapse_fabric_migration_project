"""The HTTP surface the UI talks to.

Standard library only: the project's core has no runtime dependencies and a
web framework would be the first. The surface is small and fixed.

    GET    /api/health
    GET    /api/connections               current connection state
    POST   /api/connections/authenticate  prove an Azure identity
    POST   /api/connections/test          prove workspace access; enables discovery
    DELETE /api/connections               forget the connection
    POST   /api/discovery/start
    GET    /api/discovery/status
    GET    /api/discovery/results         paged, filtered, sorted
    GET    /api/discovery/results/{id}    one object, in full
    GET    /api/dependencies              dependency graph + suggested waves
    GET    /api/mapping/components        Synapse -> Fabric component table
    GET    /api/discovery/export          the whole inventory, for download
    GET    /api/fabric/connection         Fabric target state (never a token)
    POST   /api/fabric/authenticate       Azure CLI / Fabric CLI login + workspace list
    POST   /api/fabric/workspaces         refresh the workspace list
    POST   /api/fabric/test               verify the selected workspace
    DELETE /api/fabric/connection         forget the Fabric target
    GET    /api/migration/capabilities    the migration stages, their options and the linked services
    GET    /api/migration/run             the current migration run
    POST   /api/migration/plan            planner: strategy, risks, effort, checks (read-only)
    POST   /api/migration/validate        compare Synapse with Fabric, object by object (read-only)
    POST   /api/migration/start           start a run from the migration plan
    POST   /api/migration/control         pause | resume | retry

Security posture, in order of importance:

* Binds to loopback by default, and rejects a ``Host`` header that is not
  loopback (DNS-rebinding defence).
* State-changing requests must be ``application/json``. A cross-site HTML form
  cannot send that without a CORS preflight, which is never granted, so a
  malicious page cannot drive the API through the operator's browser.
* Request bodies are capped, never logged, and never echoed. Error responses
  carry a stable code and a message written for a person -- no traceback.
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

from discovery_agent.api.fabric import FabricError, FabricTarget
from discovery_agent.api.migration import MigrationService
from discovery_agent.api.service import ApiError, Session

MAX_BODY_BYTES = 64 * 1024
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
RESULTS_PREFIX = "/api/discovery/results/"


def make_handler(
    session: Session,
    static_root: Optional[Path],
    fabric: Optional[FabricTarget] = None,
    migration: Optional[MigrationService] = None,
):
    fabric = fabric or FabricTarget()
    migration = migration or MigrationService(session, fabric)

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

        def _error(self, status: int, code: str, message: str) -> None:
            self._send(status, {"error": {"code": code, "message": message}})

        def _host_ok(self) -> bool:
            header = self.headers.get("Host") or ""
            host = header.split("]")[0] + "]" if header.startswith("[") else header.rsplit(":", 1)[0]
            return host in _LOOPBACK_HOSTS

        def _json_body(self) -> dict:
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ApiError(415, "unsupported_media_type", "Requests must be application/json.")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ApiError(413, "body_too_large", "The request is too large.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError as exc:
                raise ApiError(400, "invalid_json", "The request body is not valid JSON.") from exc
            if not isinstance(body, dict):
                raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
            return body

        def _dispatch(self, handler) -> None:
            if not self._host_ok():
                self._error(403, "forbidden_host", "Requests must use a loopback host name.")
                return
            try:
                self._send(200, handler())
            except (ApiError, FabricError) as exc:
                self._error(exc.status, exc.code, exc.message)
            except Exception:  # noqa: BLE001 - never surface a traceback to a browser
                self._error(500, "internal_error", "The server hit an unexpected problem.")

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
            elif path == "/api/migration/capabilities":
                self._dispatch(migration.capabilities)
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
                "/api/fabric/authenticate": lambda: fabric.authenticate(self._json_body()),
                "/api/fabric/workspaces": lambda: fabric.refresh_workspaces(self._json_body()),
                "/api/fabric/test": lambda: fabric.test(self._json_body()),
                "/api/migration/plan": lambda: migration.analyze(self._json_body()),
                "/api/migration/validate": lambda: migration.validate(self._json_body()),
                "/api/migration/start": lambda: migration.start(self._json_body()),
                "/api/migration/control": lambda: migration.control(self._json_body()),
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
    return ThreadingHTTPServer((host, port), make_handler(Session(), static_root))
