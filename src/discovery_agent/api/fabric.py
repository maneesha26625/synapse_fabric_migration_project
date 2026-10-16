"""The Microsoft Fabric *target* connection.

Kept apart from the Synapse source connection: different identity, different
service, never mixed. Two ways to prove an identity, both through a CLI the
operator already uses:

* **Azure CLI** -- ``az login`` every time (browser account picker); the Fabric API token is
  then requested from ``az account get-access-token`` and used inside this
  process only.
* **Fabric CLI** -- ``fab auth logout`` then ``fab auth login`` (interactive browser); workspaces are
  read through ``fab api``. The CLI holds the token, not this process.

No token is ever returned, logged or placed in an error message. Only the
*selection* is kept between starts (method, account, workspace; through
``persist``): at start it is checked again silently with the CLI session the
machine already has, never a new sign-in window.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from discovery_agent.diagnostics import record_unexpected
from discovery_agent.state import context

FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
FABRIC_API = FABRIC_RESOURCE + "/v1"
LOGIN_TIMEOUT = 180  # seconds the operator has to finish a browser sign-in
CALL_TIMEOUT = 60
METHODS = ("azure_cli", "fabric_cli")
#: The saved target selection's name in the state store (``persist``).
TARGET_STATE = "fabric_target"

_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\\u001b\[[0-9;]*m")
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-\.]{10,}")


class FabricError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _redact(text: str) -> str:
    return _JWT.sub("[token]", _ANSI.sub("", text or "")).strip()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a CLI and everything it spawned (az.cmd leaves a python child behind)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


def _run(args: List[str], timeout: int = CALL_TIMEOUT, raw: bool = False) -> Tuple[int, str]:
    """Run a CLI without a shell. Returns (exit code, redacted combined output).

    ``raw`` returns stdout untouched and is only for reading a token that stays
    in this process; the result must never be returned or logged."""
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        raise FabricError(500, "cli_failed", f"`{args[0]}` could not be started.") from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)
        raise FabricError(504, "cli_timeout", f"`{args[0]}` did not finish in time. Try again.") from exc
    if raw:
        return proc.returncode, (out or "").strip()
    return proc.returncode, _redact((out or "") + " " + (err or ""))


def _require(cli: str, install: str) -> str:
    path = shutil.which(cli)
    if not path and cli == "az" and sys.platform == "win32":
        for program_files in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if program_files:
                candidate = Path(program_files) / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin" / "az.cmd"
                if candidate.is_file():
                    path = str(candidate)
                    break
    if not path:
        raise FabricError(400, "cli_not_installed", f"{install} is not installed (or not on PATH) on the machine running the API.")
    return path


def _json_in(text: str) -> Any:
    start = text.find("{")
    if start < 0:
        raise ValueError("no json")
    return json.loads(text[start:])


def _clean_workspaces(items: List[dict]) -> List[dict]:
    out = [{"id": w["id"], "name": w.get("displayName") or w["id"]} for w in items if w.get("id")]
    return sorted(out, key=lambda w: w["name"].lower())


class FabricTarget:
    """One Fabric target connection, in memory, guarded by a lock."""

    def __init__(self, store: Any = None, audit: Optional[Callable[..., Any]] = None) -> None:
        self._lock = threading.RLock()
        self._gen = 0  # bumps on every authenticate/disconnect so a stale sign-in is ignored
        self._login_proc: Optional[subprocess.Popen] = None
        self._store = store
        #: Records sign-ins and the target chosen (``state.audit``); never raises.
        self._audit = audit or (lambda *args, **kwargs: None)
        self._reset()
        self._restore_saved()

    def _restore_saved(self) -> None:
        """Reconnect to the selection saved for this project, in the background, with no sign-in window."""
        saved = self._store.load(TARGET_STATE) if self._store is not None else None
        if isinstance(saved, dict) and saved.get("method") in METHODS and _GUID.match(str(saved.get("workspaceId") or "")):
            self.method = saved["method"]
            self.account = saved.get("account")
            self.workspace_id = saved["workspaceId"]
            self.workspace_name = saved.get("workspaceName")
            self.status = "signing_in"
            self.message = "Reconnecting to the Fabric workspace from the last session…"
            self._gen += 1
            context.spawn(self._restore, saved["method"], saved["workspaceId"], self._gen, name="fabric-restore")

    def switch(self, store: Any) -> None:
        """Hold another project's Fabric target instead: as after a restart into that project, its saved selection
        is checked again with the CLI session the machine already has. A sign-in waiting in a window is dropped."""
        with self._lock:
            self._gen += 1
            if self._login_proc is not None and self._login_proc.poll() is None:
                _kill_tree(self._login_proc)
            self._reset()
            self._store = store
            self._restore_saved()

    def _restore(self, method: str, wid: str, gen: int) -> None:
        """Check the saved selection again with the CLI session already on this machine. No sign-in window."""
        checks: List[dict] = []
        try:
            _require("az" if method == "azure_cli" else "fab", "Azure CLI" if method == "azure_cli" else "Fabric CLI")
            with self._lock:
                if gen != self._gen:
                    return
                self._verify(method, wid, checks)
                self.checks, self.status, self.message = checks, "connected", None
                name = self.workspace_name or wid
            self._audit("fabric.connected", target={"type": "workspace", "id": wid}, actor=context.system(),
                        details={"workspace": name, "method": method, "restored": True,
                                 "checks": [{"label": c.get("label"), "ok": c.get("ok")} for c in checks]})
        except FabricError as exc:
            with self._lock:
                if gen != self._gen:
                    return
                name = self.workspace_name or wid
                self._reset()
                self.message = f"Could not reconnect to {name} from the last session ({exc.message}) Log in again."
            self._audit("fabric.connected", target={"type": "workspace", "id": wid}, actor=context.system(), outcome="failed",
                        code=exc.code, reason=exc.message, details={"workspace": name, "method": method, "restored": True})
        except Exception as exc:  # noqa: BLE001 - a restore never takes the API down; the API's log gets the traceback
            reference = record_unexpected("restoring the Fabric target", exc)
            with self._lock:
                if gen == self._gen:
                    self._reset()
                    self.message = (f"Could not reconnect to the Fabric workspace from the last session ({type(exc).__name__}, "
                                    f"reference {reference}). Log in again.")

    def _reset(self) -> None:
        self.status = "disconnected"  # disconnected | signing_in | authenticated | connected | failed
        self.method: Optional[str] = None
        self.account: Optional[str] = None
        self.account_type: Optional[str] = None
        self.tenant: Optional[str] = None
        self.workspaces: List[dict] = []
        self.workspace_id: Optional[str] = None
        self.workspace_name: Optional[str] = None
        self.checks: List[dict] = []
        self.message: Optional[str] = None
        self.capacity_id: Optional[str] = None

    # -- public --------------------------------------------------------

    def state(self) -> dict:
        with self._lock:
            return {
                "status": self.status, "method": self.method, "account": self.account, "accountType": self.account_type,
                "tenantId": self.tenant, "workspaces": list(self.workspaces),
                "workspaceId": self.workspace_id, "workspaceName": self.workspace_name,
                "checks": list(self.checks), "message": self.message,
                "capacityAssigned": bool(self.capacity_id) if self.status == "connected" else None,
            }

    def disconnect(self) -> dict:
        with self._lock:
            self._gen += 1
            if self._login_proc is not None and self._login_proc.poll() is None:
                _kill_tree(self._login_proc)
            workspace = self.workspace_name or self.workspace_id
            self._reset()
            if self._store is not None:
                self._store.forget(TARGET_STATE)
            state = self.state()
        self._audit("fabric.disconnected", target={"type": "workspace", "id": workspace}, details={"workspace": workspace})
        return state

    def authenticate(self, body: dict) -> dict:
        """Start a browser sign-in and return at once; the UI polls ``state``."""
        method = self._method(body)
        cli, name = ("az", "Azure CLI") if method == "azure_cli" else ("fab", "Fabric CLI")
        _require(cli, name)
        with self._lock:
            if self.status == "signing_in":
                raise FabricError(409, "sign_in_in_progress", "A sign-in is already waiting for you in a browser window.")
            self._reset()
            self.method = method
            self.status = "signing_in"
            self.message = "Waiting for you to finish signing in in the browser window…"
            self._gen += 1
            gen = self._gen
        context.spawn(self._sign_in, method, gen, name="fabric-sign-in")
        return self.state()

    def _sign_in(self, method: str, gen: int) -> None:
        try:
            self._browser_login(method, gen)
            with self._lock:
                if gen != self._gen:
                    return
                if method == "azure_cli":
                    if not self._az_account(_require("az", "Azure CLI")):
                        raise FabricError(401, "authentication_failed", "Azure CLI sign-in did not complete. Try again.")
                elif not self._fab_status(_require("fab", "Fabric CLI")):
                    raise FabricError(401, "authentication_failed", "Fabric CLI sign-in did not complete. Finish the prompt in the console window that opens. If none appeared, run `fab auth login` in a terminal, then click Login again.")
                self.workspaces = self._list(method)
                self.status = "authenticated"
                self.message = None
                account, tenant, found = self.account, self.tenant, len(self.workspaces)
            self._audit("fabric.signed_in", target={"type": "account", "id": account},
                        details={"method": method, "account": account, "tenant": tenant, "workspaces": found})
        except FabricError as exc:
            with self._lock:
                if gen == self._gen:
                    self.status = "failed"
                    self.message = exc.message
            self._audit("fabric.signed_in", target={"type": "account", "id": None}, outcome="failed", code=exc.code,
                        reason=exc.message, details={"method": method})

    def _browser_login(self, method: str, gen: int) -> None:
        """Always sign in: an existing CLI session is never reused silently, so the
        operator picks the account in the browser each time."""
        if method == "azure_cli":
            args = [_require("az", "Azure CLI"), "login", "--allow-no-subscriptions"]
            flags = 0
        else:
            fab = _require("fab", "Fabric CLI")
            _run([fab, "auth", "logout"])  # drop the cached session so login must prompt
            args = [fab, "auth", "login"]
            # fab refuses to log in without a console, so give it its own window.
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            # fab needs the console's own stdio, so only az gets its output discarded.
            quiet = {} if method == "fabric_cli" else {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            proc = subprocess.Popen(args, creationflags=flags, **quiet)
        except OSError as exc:
            raise FabricError(500, "cli_failed", f"`{args[0]}` could not be started.") from exc
        with self._lock:
            self._login_proc = proc
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while proc.poll() is None:
            if gen != self._gen:  # cancelled
                return
            if time.monotonic() > deadline:
                _kill_tree(proc)
                raise FabricError(504, "cli_timeout", "The browser sign-in was not completed in time. Try again.")
            time.sleep(0.5)

    def refresh_workspaces(self, body: dict) -> dict:
        method = self._method(body)
        with self._lock:
            if self.method != method or self.status == "disconnected":
                raise FabricError(409, "not_authenticated", "Log in first.")
            self.workspaces = self._list(method)
            return self.state()

    def test(self, body: dict) -> dict:
        method = self._method(body)
        wid = str(body.get("workspaceId") or "").strip()
        if not _GUID.match(wid):
            raise FabricError(400, "invalid_configuration", "Select a Fabric workspace first.")
        with self._lock:
            if self.method != method or self.status == "disconnected":
                raise FabricError(409, "not_authenticated", "Log in first.")
            checks: List[dict] = []
            self.checks = checks
            try:
                self._verify(method, wid, checks)
            except FabricError as exc:
                self.status = "failed"
                self.message = exc.message
                checks.append({"label": "Workspace accessible" if checks else "Authentication", "ok": False, "detail": exc.message})
                self._audit("fabric.connected", target={"type": "workspace", "id": wid}, outcome="failed", code=exc.code,
                            reason=exc.message, details={"method": method,
                                                         "checks": [{"label": c.get("label"), "ok": c.get("ok")} for c in checks]})
                raise FabricError(exc.status, exc.code, exc.message) from exc
            self.status = "connected"
            self.message = None
            if self._store is not None:
                self._store.save(TARGET_STATE, {"method": method, "account": self.account, "workspaceId": self.workspace_id,
                                                "workspaceName": self.workspace_name})
            self._audit("fabric.connected", target={"type": "workspace", "id": wid},
                        details={"workspace": self.workspace_name or wid, "method": method, "capacity": bool(self.capacity_id),
                                 "checks": [{"label": c.get("label"), "ok": c.get("ok")} for c in checks]})
            return self.state()

    # -- for the migration run ----------------------------------------

    def migration_target(self) -> Tuple[str, str, str]:
        """(workspace id, workspace name, sign-in method) the migration writes with."""
        with self._lock:
            if self.status != "connected" or not self.workspace_id or not self.method:
                raise FabricError(409, "target_not_connected", "Connect the Fabric target and pass its connection test first.")
            return self.workspace_id, self.workspace_name or self.workspace_id, self.method

    def rest_client(self) -> Any:
        """A Fabric REST client signed in the way the operator chose.

        Azure CLI: plain HTTPS with a token from ``az``. Fabric CLI: every call
        goes through ``fab api``, which holds its own token."""
        from discovery_agent.migration.fabric_rest import FabricRestClient  # noqa: PLC0415

        if self.method == "fabric_cli":
            return FabricRestClient(lambda: "", send=self._fab_send)
        return FabricRestClient(self.token_provider(FABRIC_RESOURCE))

    def sql_token_provider(self) -> Callable[[], str]:
        """Tokens for the Warehouse SQL endpoint (audience database.windows.net).

        The Azure CLI can issue those. The Fabric CLI cannot (its audiences are
        fabric, storage, azure and powerbi), so with it the accelerator's own
        interactive browser sign-in is used against the same tenant: one
        Microsoft window the first time, silent afterwards."""
        if self.method == "azure_cli":
            return self.token_provider("https://database.windows.net/")
        from discovery_agent.connections.azure import SQL_SCOPE, credential_provider  # noqa: PLC0415
        from discovery_agent.connections.models import CredentialMethod  # noqa: PLC0415

        provider = credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=self.tenant)
        return provider.token_provider_for(SQL_SCOPE)

    def storage_token_provider(self) -> Callable[[], str]:
        """Tokens for OneLake files (audience storage.azure.com), in the Fabric tenant. Like the SQL tokens: from the Azure
        CLI, or with the Fabric CLI the accelerator's own browser sign-in against the same tenant."""
        if self.method == "azure_cli":
            return self.token_provider("https://storage.azure.com/")
        from discovery_agent.connections.azure import credential_provider  # noqa: PLC0415
        from discovery_agent.connections.models import CredentialMethod  # noqa: PLC0415

        provider = credential_provider(CredentialMethod.INTERACTIVE_BROWSER, tenant_id=self.tenant)
        return provider.token_provider_for("https://storage.azure.com/.default")

    def _fab_send(self, method: str, url: str, headers: Any, body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
        """``fab api`` as a transport: (status, lower-cased headers, body bytes)."""
        fab = _require("fab", "Fabric CLI")
        parsed = urllib.parse.urlsplit(url)
        endpoint = parsed.path[len("/v1/"):] if parsed.path.startswith("/v1/") else parsed.path.lstrip("/")
        args = [fab, "api", endpoint, "-X", method.lower(), "--show_headers"]
        params = urllib.parse.parse_qsl(parsed.query)
        if params:
            args += ["-P", ",".join(f"{k}={v}" for k, v in params)]
        temp = None
        try:
            if body:
                # A notebook body is too large for a command line; fab reads a .json path.
                fd, temp = tempfile.mkstemp(suffix=".json", prefix="fabric-migration-")
                with os.fdopen(fd, "wb") as handle:
                    handle.write(body)
                args += ["-i", temp]
            code, out = _run(args, raw=True)
        finally:
            if temp:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
        try:
            data = _json_in(out)
        except ValueError:
            return 401, {}, json.dumps({"errorCode": "FabricCliNotSignedIn", "message": "The Fabric CLI did not answer. Sign in with the Fabric CLI again on the Connections page."}).encode()
        status = int(data.get("status_code") or (500 if code else 200))
        response_headers = {str(k).lower(): str(v) for k, v in (data.get("headers") or {}).items()}
        text = data.get("text")
        return status, response_headers, (json.dumps(text).encode() if isinstance(text, (dict, list)) else b"")

    def token_provider(self, resource: str) -> Callable[[], str]:
        """A callable returning a token for ``resource`` from the Azure CLI, cached until
        five minutes before it expires. The token stays in this process."""
        az = _require("az", "Azure CLI")
        cache: Dict[str, Any] = {"token": None, "until": 0.0}
        guard = threading.Lock()

        def provide() -> str:
            with guard:
                if cache["token"] and time.time() < cache["until"]:
                    return cache["token"]
                code, out = _run([az, "account", "get-access-token", "--resource", resource, "-o", "json"], raw=True)
                try:
                    data = json.loads(out)
                except ValueError:
                    data = {}
                token = data.get("accessToken")
                if code != 0 or not token:
                    raise FabricError(401, "token_unavailable", "Could not get a token from the Azure CLI. Sign in again on the Connections page.")
                expires = data.get("expires_on")
                cache["token"] = token
                cache["until"] = (float(expires) - 300) if expires else time.time() + 1800
                return token

        return provide

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _method(body: dict) -> str:
        method = str(body.get("method") or "")
        if method not in METHODS:
            raise FabricError(400, "invalid_configuration", "Choose Azure CLI or Fabric CLI.")
        return method

    def _az_account(self, az: str) -> bool:
        code, out = _run([az, "account", "show", "-o", "json"])
        if code != 0:
            return False
        try:
            data = _json_in(out)
        except ValueError:
            return False
        self.account = (data.get("user") or {}).get("name")
        #: user | servicePrincipal: the kind of identity Fabric items are created by (and owned by).
        self.account_type = (data.get("user") or {}).get("type")
        self.tenant = data.get("tenantId")
        return True

    def _az_token(self, az: str) -> str:
        code, out = _run([az, "account", "get-access-token", "--resource", FABRIC_RESOURCE, "--query", "accessToken", "-o", "tsv"], raw=True)
        token = out.strip()
        if code != 0 or not token:
            raise FabricError(401, "token_unavailable", "Could not get a Fabric token from the Azure CLI. Run `az login` again.")
        return token

    def _rest(self, token: str, path: str) -> dict:
        req = urllib.request.Request(FABRIC_API + path, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as resp:  # noqa: S310 - fixed https host
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                try:
                    detail = json.loads(exc.read() or b"{}")
                except ValueError:
                    detail = {}
                reason = _redact(str(detail.get("message") or detail.get("errorCode") or ""))[:200]
                who = f" ({self.account})" if self.account else ""
                raise FabricError(403, "forbidden", f"Fabric rejected the Azure CLI identity{who}" + (f": {reason}." if reason else ".") + " Run `az login` with the account that has Fabric access.") from exc
            if exc.code == 404:
                raise FabricError(404, "not_found", "The Fabric workspace was not found.") from exc
            raise FabricError(502, "fabric_error", f"The Fabric API returned HTTP {exc.code}.") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise FabricError(502, "fabric_unreachable", "The Fabric API could not be reached. Check the network connection.") from exc

    def _list(self, method: str) -> List[dict]:
        if method == "azure_cli":
            token = self._az_token(_require("az", "Azure CLI"))
            items: List[dict] = []
            path = "/workspaces"
            while True:
                page = self._rest(token, path)
                items += page.get("value", [])
                cont = page.get("continuationToken")
                if not cont:
                    break
                path = "/workspaces?continuationToken=" + urllib.request.quote(cont, safe="")
            return _clean_workspaces(items)
        return _clean_workspaces(self._fab_api("workspaces").get("value", []))

    def _fab_api(self, path: str) -> dict:
        fab = _require("fab", "Fabric CLI")
        code, out = _run([fab, "api", path])
        try:
            data = _json_in(out)
        except ValueError:
            raise FabricError(401, "authentication_failed", "The Fabric CLI session is not valid. Log in with the Fabric CLI.") from None
        status = data.get("status_code", 200)
        if status in (401, 403):
            raise FabricError(403, "forbidden", "The signed-in identity has no access to this Fabric resource.")
        if status == 404:
            raise FabricError(404, "not_found", "The Fabric workspace was not found.")
        if status >= 400 or code != 0:
            raise FabricError(502, "fabric_error", f"The Fabric API returned HTTP {status}.")
        text = data.get("text")
        return text if isinstance(text, dict) else {}

    def _fab_status(self, fab: str) -> bool:
        _, out = _run([fab, "auth", "status"])
        if not re.search(r"Logged In:\s*True", out):
            return False
        for key, attr in (("Account", "account"), ("Tenant ID", "tenant")):
            m = re.search(rf"^{key}:\s*(.+)$", out, re.MULTILINE)
            if m:
                setattr(self, attr, m.group(1).strip())
        return True

    def _verify(self, method: str, wid: str, checks: List[dict]) -> None:
        ok: Callable[[str, str], None] = lambda label, detail="": checks.append({"label": label, "ok": True, "detail": detail})
        if method == "azure_cli":
            az = _require("az", "Azure CLI")
            if not self._az_account(az):
                raise FabricError(401, "authentication_failed", "Azure CLI is no longer authenticated. Log in again.")
            ok("Azure CLI authenticated", self.account or "")
            token = self._az_token(az)
            listed = self._list("azure_cli")
            ok("Fabric API accessible", "")
            ws = self._rest(token, f"/workspaces/{wid}")
            ok("Workspace accessible", ws.get("displayName", ""))
        else:
            fab = _require("fab", "Fabric CLI")
            if not self._fab_status(fab):
                raise FabricError(401, "authentication_failed", "The Fabric CLI session is not valid. Log in again.")
            ok("Fabric CLI authenticated", self.account or "")
            listed = self._list("fabric_cli")
            if not any(w["id"].lower() == wid.lower() for w in listed):
                raise FabricError(404, "not_found", "The workspace was not found among those this identity can see.")
            ok("Fabric workspace discovered", "")
            ws = self._fab_api(f"workspaces/{wid}")
            ok("Workspace accessible", ws.get("displayName", ""))
        self.workspaces = listed
        self.workspace_id = wid
        self.workspace_name = ws.get("displayName") or next((w["name"] for w in listed if w["id"].lower() == wid.lower()), wid)
        # Not a connection failure, but migration cannot create items without it.
        self.capacity_id = ws.get("capacityId")
        if self.capacity_id:
            ok("Fabric capacity assigned", "")
        else:
            checks.append({"label": "Fabric capacity assigned", "ok": False, "detail": "No Fabric capacity: migration cannot create items here until one is assigned (Workspace settings, License info)."})
