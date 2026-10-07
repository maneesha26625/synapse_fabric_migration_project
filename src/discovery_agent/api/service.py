"""The state behind the API: one connection, one discovery.

A single-operator, single-process model on purpose. The migration accelerator
is a local tool today; the connection lives in this process's memory. Given a
``StateStore``, the sign-in, the connection and the discovery are also saved as
they change (names, ids and what discovery read; never a token or a secret),
so a restarted server picks up where it left off. Nothing here writes a
credential anywhere.

Two ways to prove an identity, both built by the connection layer:

* **Azure CLI** -- an interactive browser sign-in (not the ambient `az
  login` session); the operator authenticates each time and nothing is kept.
* **Interactive browser** -- a Microsoft sign-in window against the tenant
  named. The connection layer holds the signed-in identity for the process
  and keeps an authentication record (no token), so a restarted server signs
  in silently. Disconnecting forgets both. The Azure CLI session is never
  read or written.

No request field can carry a secret: an unknown field is refused.

After any of them the same flow follows: list resource groups, workspaces and
SQL pools from that identity, then test the connection.

Discovery reuses ``discovery.run`` unchanged, in workspace-only mode
(``scan_repository=False``): the live Artifacts API and, when a pool is given,
the dedicated SQL catalog.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from discovery_agent import discovery
from discovery_agent.api import mapping
from discovery_agent.config import DEFAULT_OUTPUT_DIR, DiscoveryConfig
from discovery_agent.connections.azure import (
    AzureConnection,
    AzureCredentialProvider,
    credential_provider,
    discover_tenant_id,
    forget_auth_record,
    reset_credentials,
)
from discovery_agent.connections.manager import ConnectionManager
from discovery_agent.connections.models import (
    AzureConnectionConfig,
    ConnectionSettings,
    CredentialMethod,
    SynapseConnectionConfig,
)
from discovery_agent.connections.validation import (
    ConnectionValidation,
    ErrorCategory,
    redact,
)
from discovery_agent.errors import (
    AzureAuthenticationError,
    AzureConnectionError,
    ConfigError,
    DiscoveryError,
    SynapseConnectionError,
)
from discovery_agent.mapping import component_table
from discovery_agent.migration.runner import MIGRATABLE_TYPES
from discovery_agent.mapping.waves import assign_waves, summarise_waves
from discovery_agent.mapping.synapse_fabric_mapping import (
    ASSESSMENT,
    DIRECT,
    MANUAL,
    RECONFIGURATION,
    REFACTORING,
    TRANSFORMATION,
)
from discovery_agent.extractors.models import SourceType

if TYPE_CHECKING:
    from discovery_agent.api.state import StateStore

SOURCE_PLATFORM = "Azure Synapse"
_GUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WORKSPACE_URL = re.compile(
    r"^https://(?P<name>[A-Za-z0-9-]+)\.dev\.azuresynapse\.net/?$", re.IGNORECASE
)

METHODS = ("azure_cli", "interactive_browser")

#: What the UI shows for each method. Plain language, no secret anywhere.
METHOD_NOTES = {
    "azure_cli": {
        "label": "Azure CLI",
        "detail": "Opens a sign-in window each time; nothing is kept between sign-ins.",
    },
    "interactive_browser": {
        "label": "Interactive browser",
        "detail": "Opens a sign-in window against the tenant you name, and leaves your Azure CLI session untouched.",
        "bestFor": "A tenant your `az login` cannot reach.",
        "caveat": "The window opens on the machine running this server.",
    },
}

#: Everything an authenticate request may contain: only what the sign-in
#: needs. Anything else -- a secret above all -- is refused, not dropped.
_AUTHENTICATE_FIELDS = frozenset(
    {"method", "tenantId", "subscriptionId", "resourceGroup", "workspace", "workspaceUrl", "sqlPool"}
)

#: What an operator should do about each failure category. Plain language; the
#: technical message from the connection layer is shown beneath it.
_CATEGORY_HELP = {
    ErrorCategory.AUTHENTICATION: (
        "Authentication failed",
        "The identity could not be authenticated. Finish the sign-in window on the "
        "machine running the server, within the time allowed.",
    ),
    ErrorCategory.AUTHORIZATION: (
        "Insufficient permissions",
        "The authenticated identity does not have access to the selected "
        "workspace. Reader on the resource group covers workspace metadata; "
        "reading artifacts also needs a Synapse role such as Synapse Artifact "
        "User.",
    ),
    ErrorCategory.NOT_FOUND: (
        "Workspace not found",
        "The subscription, resource group or workspace could not be found. "
        "Check the names and that the subscription is the right one.",
    ),
    ErrorCategory.CONFIGURATION: (
        "Invalid configuration",
        "A required setting is missing or malformed.",
    ),
    ErrorCategory.UNAVAILABLE: (
        "Resource unavailable",
        "The resource exists but cannot serve requests right now (for "
        "example, a paused SQL pool).",
    ),
    ErrorCategory.NETWORK: (
        "Network failure",
        "Azure could not be reached. Check the network connection and try again.",
    ),
    ErrorCategory.DEPENDENCY: (
        "Backend dependency missing",
        "A required package is not installed on the server running the "
        "accelerator API.",
    ),
    ErrorCategory.UNSUPPORTED: (
        "Not supported",
        "This authentication mechanism is not supported by the backend.",
    ),
    ErrorCategory.UNKNOWN: ("Connection failed", "The connection could not be verified."),
}

#: The order ``Session.test`` runs its checks in.
_CHECK_LABELS = (
    "Azure authentication",
    "Synapse workspace",
    "Workspace artifacts",
    "Dedicated SQL pool",
)


class ApiError(Exception):
    """An error with an HTTP status and a stable code the UI can switch on."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: Any, name: str, pattern: re.Pattern, required: bool = True) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ApiError(400, "invalid_configuration", f"{name} is required.")
        return None
    if not pattern.match(text):
        raise ApiError(400, "invalid_configuration", f"{name} is not in a valid format.")
    return text


@dataclass
class _SignIn:
    """A completed interactive sign-in for one subscription. Holds no token
    itself: the provider caches those in memory, inside the connection layer."""

    method: str
    provider: AzureCredentialProvider
    azure: AzureConnection
    subscription_id: str
    tenant_id: Optional[str]
    subscription_name: Optional[str]


@dataclass
class _Connection:
    method: str
    manager: ConnectionManager
    tenant_id: Optional[str]
    subscription_id: str
    resource_group: str
    workspace: str
    sql_pool: Optional[str]
    subscription_name: Optional[str] = None
    checks: List[dict] = field(default_factory=list)
    connected: bool = False
    tested_at: Optional[str] = None



def _identity(conn: "_Connection") -> Tuple[str, str]:
    return conn.workspace.lower(), (conn.sql_pool or "").lower()


def _sign_in_for(method: str, subscription: str, tenant: Optional[str]) -> Tuple[AzureCredentialProvider, AzureConnection]:
    """The identity for a sign-in, built without opening anything: a token is asked for on first use.

    Both methods are a browser sign-in. "azure_cli" keeps nothing, as it always
    has; "interactive_browser" uses the identity the connection layer holds,
    whose authentication record lets a restarted server sign in silently."""
    config = AzureConnectionConfig(
        subscription_id=subscription,
        tenant_id=tenant,
        credential_method=CredentialMethod.INTERACTIVE_BROWSER,
    )
    provider = credential_provider(
        method=CredentialMethod.INTERACTIVE_BROWSER,
        tenant_id=tenant,
        remember=method == "interactive_browser",
    )
    return provider, AzureConnection(config, credential=provider)


@dataclass
class _Job:
    state: str = "idle"  # idle | running | completed | completed_with_warnings | failed
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    run: Optional[discovery.DiscoveryRun] = None
    items: List[dict] = field(default_factory=list)
    records_by_id: Dict[str, Any] = field(default_factory=dict)
    extras_by_id: Dict[str, "mapping.Extra"] = field(default_factory=dict)
    #: What discovery read beyond ``run``: with it, the rest is rebuilt after a restart.
    extras: List["mapping.Extra"] = field(default_factory=list)
    extra_failures: List[dict] = field(default_factory=list)
    known: Dict[str, str] = field(default_factory=dict)
    referenced_by: Dict[str, List[dict]] = field(default_factory=dict)
    graph: Optional[dict] = None
    summary: Optional[dict] = None
    workspace: Optional[str] = None


#: Raised whenever the API changes in a way the page relies on. The page compares it
#: with the version it needs and tells the operator to restart an older backend.
API_VERSION = 3
#: The layout of the saved discovery. Bumped when it changes, so an older file is not misread.
DISCOVERY_STATE = 1
_DONE = ("completed", "completed_with_warnings")


class Session:
    """The connection and the last discovery, guarded by one lock."""

    def __init__(self, store: Optional["StateStore"] = None) -> None:
        self._lock = threading.RLock()
        self._connection: Optional[_Connection] = None
        self._signin: Optional[_SignIn] = None
        self._signing_in = False
        self._job = _Job()
        self._detail_cache: Dict[str, dict] = {}
        self._store = store
        #: The discovery last written to the store, and its state then.
        self._saved_discovery: Optional[Tuple[_Job, str]] = None

    # -- capabilities ------------------------------------------------------

    @staticmethod
    def health() -> dict:
        """What this build can do, so the UI never offers what would fail."""
        return {
            "status": "ok",
            "apiVersion": API_VERSION,
            "capabilities": {
                # Only what ``credential_provider`` actually implements.
                "authMethods": list(METHODS),
                "authMethodDetails": [{"id": m, **METHOD_NOTES[m]} for m in METHODS],
                "discoveryScope": [
                    f"{t}s" if not t.endswith("s") else t
                    for t, _ in mapping.ARTIFACT_INFO.values()
                ],
                # What a migration run moves into Fabric today; the rest of
                # a plan is reported as deferred to a later session.
                "migratableTypes": list(MIGRATABLE_TYPES),
            },
        }

    # -- surviving a restart -----------------------------------------------

    def busy(self) -> List[str]:
        """What is working now. A supervised restart waits for it."""
        with self._lock:
            working = ["discovery"] if self._job.state == "running" else []
            if self._signing_in:
                working.append("a Synapse sign-in")
            return working

    def persist(self) -> None:
        """Save the sign-in, the connection and the discovery. Without a store, nothing."""
        if self._store is None:
            return
        self._store.save("source", self._source_snapshot)
        taken: Dict[str, Any] = {}

        def discovery_snapshot() -> Any:
            from discovery_agent.api.state import UNCHANGED  # noqa: PLC0415 - only with a store

            with self._lock:
                job, state = self._job, self._job.state
            saved = self._saved_discovery
            if saved is not None and saved[0] is job and saved[1] == state:
                return UNCHANGED  # a discovery can be large: written once per change, not per request
            taken["marker"] = (job, state)
            return self._discovery_value(job, state)

        if self._store.save("discovery", discovery_snapshot) and "marker" in taken:
            self._saved_discovery = taken["marker"]

    def _source_snapshot(self) -> Optional[dict]:
        with self._lock:
            signin, conn = self._signin, self._connection
            if signin is None and conn is None:
                return None
            return {
                "signIn": None if signin is None else {
                    "method": signin.method, "subscriptionId": signin.subscription_id,
                    "tenantId": signin.tenant_id, "subscriptionName": signin.subscription_name,
                },
                "connection": None if conn is None else {
                    "method": conn.method, "tenantId": conn.tenant_id, "subscriptionId": conn.subscription_id,
                    "resourceGroup": conn.resource_group, "workspace": conn.workspace, "sqlPool": conn.sql_pool,
                    "subscriptionName": conn.subscription_name, "checks": [dict(c) for c in conn.checks],
                    "connected": conn.connected, "testedAt": conn.tested_at,
                },
            }

    @staticmethod
    def _discovery_value(job: _Job, state: str) -> Optional[dict]:
        """What discovery read, not the index built from it: the index is rebuilt on restore."""
        if state == "idle":
            return None
        saved = {
            "version": DISCOVERY_STATE, "state": state, "startedAt": job.started_at,
            "finishedAt": job.finished_at, "error": job.error, "workspace": job.workspace,
        }
        if state in _DONE:
            saved.update({"run": job.run, "extras": list(job.extras), "extraFailures": list(job.extra_failures)})
        return saved

    def restore(self) -> Tuple[List[str], List[str]]:
        """Take back what was saved before the server last stopped.

        Returns what came back, and notes on what did not or needs the operator.
        Builds no connection: a token is asked for on first use, silently where
        the sign-in method keeps one."""
        kept: List[str] = []
        notes: List[str] = []
        if self._store is None:
            return kept, notes
        source = self._store.load("source")
        source = source if isinstance(source, dict) else {}
        saved_signin, saved_conn = source.get("signIn"), source.get("connection")
        signin: Optional[_SignIn] = None
        conn: Optional[_Connection] = None
        if saved_signin:
            try:
                method = str(saved_signin.get("method") or "")
                if method not in METHODS:
                    raise ValueError(method)
                provider, azure = _sign_in_for(method, saved_signin["subscriptionId"], saved_signin.get("tenantId"))
                signin = _SignIn(method=method, provider=provider, azure=azure,
                                 subscription_id=saved_signin["subscriptionId"], tenant_id=saved_signin.get("tenantId"),
                                 subscription_name=saved_signin.get("subscriptionName"))
                kept.append("the Synapse sign-in")
                if method == "azure_cli":
                    notes.append("Synapse asks you to sign in once more the next time it is read: "
                                 "the Azure CLI method keeps no sign-in between restarts (Interactive browser does).")
            except Exception:  # noqa: BLE001 - a sign-in that cannot be rebuilt is asked for again
                notes.append("Sign in to Synapse again: the last sign-in could not be restored.")
        if saved_conn and signin is not None:
            try:
                settings = ConnectionSettings(
                    azure=signin.azure.config,
                    synapse=SynapseConnectionConfig(resource_group=saved_conn["resourceGroup"],
                                                    workspace_name=saved_conn["workspace"],
                                                    sql_pool_name=saved_conn.get("sqlPool")),
                )
                conn = _Connection(
                    method=signin.method, manager=ConnectionManager(settings, credential=signin.provider),
                    tenant_id=saved_conn.get("tenantId"), subscription_id=saved_conn["subscriptionId"],
                    resource_group=saved_conn["resourceGroup"], workspace=saved_conn["workspace"],
                    sql_pool=saved_conn.get("sqlPool"), subscription_name=saved_conn.get("subscriptionName"),
                    checks=[dict(c) for c in saved_conn.get("checks") or []],
                    connected=bool(saved_conn.get("connected")), tested_at=saved_conn.get("testedAt"),
                )
                if conn.connected:
                    kept.append(f"the connection to {conn.workspace}" + (f" / {conn.sql_pool}" if conn.sql_pool else ""))
            except Exception:  # noqa: BLE001
                notes.append("Test the Synapse connection again: it could not be restored.")
        job: Optional[_Job] = None
        saved_job = self._store.load("discovery")
        saved_job = saved_job if isinstance(saved_job, dict) else None
        if saved_job:
            try:
                job = self._job_from(saved_job)
            except Exception:  # noqa: BLE001 - the new code cannot read what the old one found
                self._store.clear("discovery")
                notes.append("Run discovery again: the updated code could not rebuild the last one.")
        with self._lock:
            self._signin, self._connection = signin, conn
            if job is not None:
                self._job = job
        if job is not None and job.state in _DONE:
            kept.append(f"the discovery ({len(job.items):,} objects)")
        if job is not None and saved_job is not None and job.state == saved_job.get("state"):
            self._saved_discovery = (job, job.state)  # already on disk as it is
        else:
            self.persist()
        return kept, notes

    def _job_from(self, saved: dict) -> _Job:
        if saved.get("version") != DISCOVERY_STATE:
            raise ValueError("saved in another layout")
        job = _Job(state=str(saved.get("state") or "idle"), started_at=saved.get("startedAt"),
                   finished_at=saved.get("finishedAt"), error=saved.get("error"), workspace=saved.get("workspace"))
        if job.state == "running":
            job.state, job.finished_at = "failed", _now()
            job.error = "The server restarted while discovery was running. Run it again."
        elif job.state in _DONE:
            index = self._indexed(job.workspace or "", saved["run"], list(saved.get("extras") or []),
                                  list(saved.get("extraFailures") or []))
            self._fill(job, index)
        return job

    # -- connections -------------------------------------------------------

    def connection_state(self) -> dict:
        with self._lock:
            return self._connection_payload(self._connection)

    def disconnect(self) -> dict:
        """Sign out: forget the connection, the sign-in, every held identity and
        the authentication record. The next sign-in opens a window again. The
        Azure CLI session is not touched."""
        with self._lock:
            if self._job.state == "running":
                raise ApiError(409, "discovery_running", "Discovery is running; wait for it to finish.")
            self._connection = None
            self._signin = None
            self._job = _Job()
            self._detail_cache.clear()
            reset_credentials()
            forget_auth_record()
            payload = self._connection_payload(None)
        self.persist()
        payload["note"] = "Signed out of this accelerator. Your Azure CLI session is untouched."
        return payload

    def authenticate(self, body: dict) -> dict:
        """Prove an Azure identity for one subscription.

        * ``azure_cli`` opens a sign-in window on the machine running the API
          and blocks until the operator finishes. It deliberately does not
          reuse an existing ``az login`` session: every call asks.
        * ``interactive_browser`` signs in against the named tenant through
          the identity the connection layer holds, so a repeat -- or a
          restarted server with a stored record -- does not prompt again.
        """
        unknown = sorted(set(body) - _AUTHENTICATE_FIELDS)
        if unknown:
            # Names only, never values: a refused field may be a secret.
            raise ApiError(400, "invalid_configuration", f"Unexpected field(s): {', '.join(unknown)}.")
        method = self._method(body)
        browser = method == "interactive_browser"
        subscription = _clean(body.get("subscriptionId"), "Subscription ID", _GUID)
        tenant = _clean(body.get("tenantId"), "Tenant ID", _GUID, required=browser)
        with self._lock:
            if self._signing_in:
                raise ApiError(409, "sign_in_in_progress", "A sign-in is already waiting for you in a browser window.")
            if self._job.state == "running":
                raise ApiError(409, "discovery_running", "Discovery is running; wait for it to finish.")
            self._signing_in = True
        try:
            # Sending the operator to the right tenant's sign-in page is what
            # lets a subscription id alone be enough.
            tenant = tenant or discover_tenant_id(subscription)
            try:
                provider, azure = _sign_in_for(method, subscription, tenant)
                provider.start_attempt()  # an explicit attempt retries a refused one
            except (ConfigError, AzureAuthenticationError) as exc:
                raise ApiError(400, "invalid_configuration", redact(str(exc))) from exc
            result = azure.validate()  # for azure_cli, this call opens the browser
        finally:
            with self._lock:
                self._signing_in = False

        with self._lock:
            self._connection = None
            self._job = _Job()
            self._detail_cache.clear()
            if result.ok:
                self._signin = _SignIn(
                    method=method,
                    provider=provider,
                    azure=azure,
                    subscription_id=subscription,
                    tenant_id=result.details.get("tenant_id") or tenant,
                    subscription_name=result.details.get("subscription_name"),
                )
            else:
                self._signin = None
            payload = self._connection_payload(None)
        self.persist()
        payload["checks"] = [self._check(result)]
        return self._with_error(payload, [] if result.ok else [result], tenant_hint=not tenant)

    def _signed_in(self) -> "_SignIn":
        with self._lock:
            if self._signin is None:
                raise ApiError(409, "sign_in_required", "Sign in to Azure first.")
            return self._signin

    def azure_options(self, kind: str, query: Dict[str, str]) -> dict:
        """Dropdown contents: resource groups, then workspaces, then SQL pools."""
        signin = self._signed_in()
        resource_group = _clean(query.get("resourceGroup"), "Resource group", _NAME, required=kind != "resource-groups")
        workspace = _clean(query.get("workspace"), "Synapse workspace", _NAME, required=kind == "sql-pools")
        try:
            if kind == "resource-groups":
                items = signin.azure.list_resource_groups()
            elif kind == "workspaces":
                items = signin.azure.list_synapse_workspaces(resource_group)
            elif kind == "sql-pools":
                items = signin.azure.list_sql_pools(resource_group, workspace)
            else:
                raise ApiError(404, "not_found", "No such list.")
        except AzureAuthenticationError as exc:
            raise ApiError(401, "authentication", redact(str(exc))) from exc
        except AzureConnectionError as exc:
            raise ApiError(502, "azure_error", redact(str(exc))) from exc
        return {"items": items}

    def test(self, body: dict) -> dict:
        """The full chain: Azure, then the workspace, its artifacts, and the pool."""
        method = self._method(body)
        signin = self._signed_in()
        if signin.method != method:
            raise ApiError(409, "sign_in_required", "Authenticate with the selected method first.")
        resource_group = _clean(body.get("resourceGroup"), "Resource group", _NAME)
        workspace = _clean(body.get("workspace"), "Synapse workspace", _NAME)
        sql_pool = _clean(body.get("sqlPool"), "Dedicated SQL pool", _NAME, required=False)
        try:
            settings = ConnectionSettings(
                azure=signin.azure.config,
                synapse=SynapseConnectionConfig(
                    resource_group=resource_group, workspace_name=workspace, sql_pool_name=sql_pool
                ),
            )
            # The manager is handed the signed-in credential, so nothing below
            # can prompt again or fall back to a different identity.
            manager = ConnectionManager(settings, credential=signin.provider)
        except ConfigError as exc:
            raise ApiError(400, "invalid_configuration", redact(str(exc))) from exc
        conn = _Connection(
            method=signin.method,
            manager=manager,
            tenant_id=signin.tenant_id,
            subscription_id=signin.subscription_id,
            resource_group=resource_group,
            workspace=workspace,
            sql_pool=sql_pool,
            subscription_name=signin.subscription_name,
        )

        results: List[ConnectionValidation] = []
        azure = manager.validate_azure()
        results.append(azure)
        if azure.ok:
            synapse = manager.validate_synapse()
            results.append(synapse)
            if synapse.ok:
                results.append(manager.validate_synapse_artifacts())
                if conn.sql_pool:
                    results.append(manager.validate_sql())

        conn.checks = [self._check(r, i) for i, r in enumerate(results)]
        # Connected means discovery can run: identity, workspace and artifact
        # access all passed. The SQL pool is optional and degrades on its own
        # (a paused pool costs the SQL objects, not the run), so it is reported
        # but does not gate.
        required = results[:3]
        conn.connected = len(required) == 3 and all(r.ok for r in required)
        conn.tested_at = _now()
        failures = [r for r in results if not r.ok and r.connection is not SourceType.SQL]
        with self._lock:
            self._replace_connection(conn)
        self.persist()
        return self._with_error(self._connection_payload(conn), failures)

    def _replace_connection(self, conn: "_Connection") -> None:
        """Hold a newly tested connection. The last discovery is dropped when the test failed,
        or when it reached another workspace or pool: it describes a different source."""
        previous = self._connection
        changed = previous is not None and _identity(previous) != _identity(conn)
        self._connection = conn
        if not conn.connected or changed:
            self._job = _Job()
            self._detail_cache.clear()

    def source_identity(self) -> Optional[Tuple[str, str]]:
        """(workspace, pool) of the connected source, to tell one connection from another."""
        with self._lock:
            conn = self._connection
            return _identity(conn) if conn is not None and conn.connected else None

    @staticmethod
    def _method(body: dict) -> str:
        method = str(body.get("method") or "")
        if method not in METHODS:
            raise ApiError(400, "invalid_configuration", "Choose an authentication method.")
        return method

    @staticmethod
    def _check(result: ConnectionValidation, position: int = 0) -> dict:
        # Positional, not by source: the workspace and its artifacts endpoint
        # are both SourceType.SYNAPSE and are different checks.
        return {
            "name": _CHECK_LABELS[position],
            "status": result.status.value,
            "message": result.message,
            "category": result.category.value if result.category else None,
        }

    @staticmethod
    def _with_error(payload: dict, failures: List[ConnectionValidation], tenant_hint: bool = False) -> dict:
        payload["ok"] = not failures
        if failures:
            first = failures[0]
            title, hint = _CATEGORY_HELP.get(
                first.category or ErrorCategory.UNKNOWN, _CATEGORY_HELP[ErrorCategory.UNKNOWN]
            )
            if tenant_hint and first.category in (ErrorCategory.AUTHENTICATION, ErrorCategory.NOT_FOUND):
                hint += " If your account belongs to several tenants, enter the Tenant ID and sign in again."
            payload["error"] = {
                "code": (first.category or ErrorCategory.UNKNOWN).value,
                "title": title,
                "hint": hint,
                "message": first.message,
            }
        return payload

    def _connection_payload(self, conn: Optional[_Connection]) -> dict:
        signin = self._signin
        base = {
            "ok": True,
            "signedIn": signin is not None,
            "signingIn": self._signing_in,
        }
        if conn is None:
            base.update({"status": "disconnected", "checks": []})
            if signin is not None:
                base.update(
                    {
                        "method": signin.method,
                        "subscriptionId": signin.subscription_id,
                        "subscriptionName": signin.subscription_name,
                        "tenantId": signin.tenant_id,
                    }
                )
            return base
        base.update(
            {
                "status": "connected" if conn.connected else "not_connected",
                "method": conn.method,
                "sourcePlatform": SOURCE_PLATFORM,
                "workspace": conn.workspace,
                "resourceGroup": conn.resource_group,
                "subscriptionId": conn.subscription_id,
                "subscriptionName": conn.subscription_name,
                "tenantId": conn.tenant_id,
                "sqlPool": conn.sql_pool,
                "testedAt": conn.tested_at,
                "checks": conn.checks,
            }
        )
        return base

    # -- discovery ---------------------------------------------------------

    def start_discovery(self) -> dict:
        with self._lock:
            conn = self._connection
            if conn is None or not conn.connected:
                raise ApiError(
                    409,
                    "not_connected",
                    "Connect to a Synapse workspace and pass the connection test first.",
                )
            if self._job.state == "running":
                raise ApiError(409, "discovery_running", "Discovery is already running.")
            self._job = _Job(state="running", started_at=_now(), workspace=conn.workspace)
            self._detail_cache.clear()
            job = self._job
        self.persist()
        thread = threading.Thread(target=self._discover, args=(conn, job), daemon=True)
        thread.start()
        return self.discovery_status()

    def reset_discovery(self) -> dict:
        """Forget the last discovery's results, so the next one starts from
        nothing. The connection and its sign-in are kept."""
        with self._lock:
            if self._job.state == "running":
                raise ApiError(409, "discovery_running", "Discovery is running; wait for it to finish.")
            self._job = _Job()
            self._detail_cache.clear()
        self.persist()
        return self.discovery_status()

    def _discover(self, conn: _Connection, job: _Job) -> None:
        try:
            config = DiscoveryConfig(source=Path("."), out=Path(DEFAULT_OUTPUT_DIR))
            run = discovery.run(config, connections=conn.manager, scan_repository=False)
            extras, extra_failures = self._collect_extras(conn, list(run.records.records))
            index = self._indexed(conn.workspace, run, extras, extra_failures)
            with self._lock:
                self._fill(job, index)
                job.finished_at = _now()
        except DiscoveryError as exc:
            self._fail(job, redact(str(exc)))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the UI
            self._fail(job, f"Discovery stopped unexpectedly ({type(exc).__name__}).")
        self.persist()

    @classmethod
    def _indexed(cls, workspace: str, run: discovery.DiscoveryRun, extras: list, extra_failures: List[dict]) -> dict:
        """Everything the API serves about a discovery, built from what it read.

        Uses no connection, so a restarted server rebuilds it from the saved
        discovery with the code it runs now."""
        records = list(run.records.records)
        # Every id a dependency can join onto, in one place.
        known: Dict[str, str] = {}
        for r in records:
            known[r.logical_id] = r.identity.scoped_id
            known[r.identity.scoped_id] = r.identity.scoped_id
        for e in extras:
            known[e.id] = e.id
            if e.source_type in ("Spark Pool", "Integration Runtime"):
                known[f"compute:{e.name}"] = e.id

        items = [mapping.list_item(r, workspace, known) for r in records]
        items += [mapping.extra_item(e, workspace, known) for e in extras]
        items.sort(key=lambda i: (i["category"], i["type"], i["name"].lower()))

        # "Referenced by": the reverse of every dependency that joined.
        referenced_by: Dict[str, List[dict]] = {}

        def add_reverse(source_id: str, source_name: str, source_type: str, deps: List[dict]) -> None:
            for dep in deps:
                if dep["objectId"] and dep.get("location") != "contains":
                    referenced_by.setdefault(dep["objectId"], []).append(
                        {"name": source_name, "type": source_type, "objectId": source_id}
                    )

        for r in records:
            add_reverse(r.identity.scoped_id, r.identity.qualified_name,
                        mapping._record_source_type(r), mapping._dependencies(r, known))
        for e in extras:
            add_reverse(e.id, e.name, e.source_type, mapping._extra_dependencies(e, known))

        graph = cls._build_graph(items, records, extras, known)
        wave_of = {n["id"]: n["wave"] for n in graph["nodes"]}
        for item in items:
            item["wave"] = wave_of.get(item["id"], 0)

        summary = cls._summarise(run, items, extra_failures)
        return {
            "graph": graph,
            "run": run,
            "items": items,
            "records_by_id": {r.identity.scoped_id: r for r in records},
            "extras_by_id": {e.id: e for e in extras},
            "extras": list(extras),
            "extra_failures": list(extra_failures),
            "known": known,
            "referenced_by": referenced_by,
            "summary": summary,
            "state": "completed_with_warnings" if summary["failedCategories"] or summary["warnings"] else "completed",
        }

    @staticmethod
    def _fill(job: _Job, index: dict) -> None:
        """Put an index on a job; its state goes last, so nothing reads it half-filled."""
        for name, value in index.items():
            if name != "state":
                setattr(job, name, value)
        job.state = index["state"]

    @staticmethod
    def _build_graph(items: List[dict], records: list, extras: list, known: Dict[str, str]) -> dict:
        """Nodes, edges and suggested waves for the dependency view.

        An edge ``source -> target`` means *source needs target*. Only
        dependencies that joined onto another discovered object become edges;
        a schema's "contains" links are not dependencies and are left out.
        """
        edges: List[Tuple[str, str]] = []
        for r in records:
            for d in mapping._dependencies(r, known):
                if d["objectId"] and d.get("location") != "contains":
                    edges.append((r.identity.scoped_id, d["objectId"]))
        for e in extras:
            for d in mapping._extra_dependencies(e, known):
                if d["objectId"] and d.get("location") != "contains":
                    edges.append((e.id, d["objectId"]))
        edges = sorted(set(edges))
        types = {i["id"]: i["type"] for i in items}
        waves = assign_waves(types, edges)
        out_degree: Dict[str, int] = {}
        in_degree: Dict[str, int] = {}
        for source, target in edges:
            out_degree[source] = out_degree.get(source, 0) + 1
            in_degree[target] = in_degree.get(target, 0) + 1
        nodes = [
            {
                "id": i["id"], "name": i["name"], "type": i["type"], "category": i["category"],
                "classification": i["classification"], "fabricTarget": i["fabricTarget"], "wave": waves.get(i["id"], 1),
                "dependsOn": out_degree.get(i["id"], 0), "dependedOnBy": in_degree.get(i["id"], 0),
            }
            for i in items
        ]
        return {
            "nodes": nodes,
            "edges": [{"source": s, "target": t} for s, t in edges],
            "waves": summarise_waves(waves, types),
        }

    @staticmethod
    def _collect_extras(conn: _Connection, records: list) -> Tuple[List["mapping.Extra"], List[dict]]:
        """Inventory entries beyond the nine P0 artifacts.

        Each source is read independently and a failure is reported by name,
        never turned into an empty list: "could not read triggers" and "there
        are no triggers" are different claims.
        """
        extras: List[mapping.Extra] = []
        failures: List[dict] = []
        workspace_id = conn.manager.settings.synapse.workspace_resource_id(conn.subscription_id)

        def attempt(label: str, read, build) -> None:
            try:
                extras.extend(build(read()))
            except (AzureConnectionError, AzureAuthenticationError, SynapseConnectionError, DiscoveryError) as exc:
                failures.append({"name": label, "reason": redact(str(exc))})

        def arm(suffix: str):
            return lambda: conn.manager.azure().list_values(f"{workspace_id}/{suffix}", "2021-06-01")

        attempt("Dedicated SQL pools", arm("sqlPools"), mapping.sql_pool_extras)
        attempt("Spark pools", arm("bigDataPools"), mapping.spark_pool_extras)
        attempt("Spark libraries", arm("libraries"), mapping.library_extras)
        attempt("Integration runtimes", arm("integrationRuntimes"), mapping.runtime_extras)
        attempt("Triggers", lambda: conn.manager.synapse_artifacts().list_triggers(), mapping.trigger_extras)
        extras.extend(mapping.schema_extras(records))
        extras.extend(mapping.storage_extras(records))
        return extras, failures

    def _fail(self, job: _Job, message: str) -> None:
        with self._lock:
            job.error = message
            job.finished_at = _now()
            job.state = "failed"

    @staticmethod
    def _summarise(run: discovery.DiscoveryRun, items: List[dict], extra_failures: List[dict]) -> dict:
        def tally(field_name: str) -> Dict[str, int]:
            out: Dict[str, int] = {}
            for item in items:
                out[item[field_name]] = out.get(item[field_name], 0) + 1
            return dict(sorted(out.items()))

        failed: List[dict] = list(extra_failures)
        if run.synapse is not None:
            for artifact in run.synapse.unreachable:
                type_name, category = mapping.type_and_category(artifact.value)
                failed.append(
                    {
                        "name": f"{type_name}s ({category})",
                        "reason": "The workspace endpoint could not be read (permission or availability). "
                        "This is not a count of zero.",
                    }
                )
        for issue in run.all_issues:
            if issue.code.value == "source_unavailable" and "sql pool" in issue.message:
                failed.append({"name": "Dedicated SQL pool objects", "reason": redact(issue.message)})

        warnings = [
            redact(i.message)
            for i in run.all_issues
            if i.code.value != "source_unavailable" or "sql pool" not in i.message
        ]
        # Run-level notes that only restate "no repository was scanned" are not
        # warnings about the workspace and would alarm on every healthy run.
        # The same goes for "not identified with each other": this UI runs
        # workspace-only, so there is no second source to identify with.
        warnings = [
            w
            for w in warnings
            if not w.startswith(
                ("no repository was scanned", "repository and live workspace artifacts were not identified")
            )
        ]
        mapped_paths = {DIRECT, TRANSFORMATION, REFACTORING, RECONFIGURATION}
        discovered_types = sorted({i["type"] for i in items})
        return {
            "total": len(items),
            "byCategory": tally("category"),
            "byType": tally("type"),
            "byWorkstream": tally("workstream"),
            "byClassification": tally("classification"),
            "byPath": tally("migrationPath"),
            "byFabricTarget": tally("fabricTarget"),
            # "Mapped" = a named Fabric component exists for the path; it says
            # nothing about whether the object will migrate.
            "withFabricMapping": sum(1 for i in items if i["migrationPath"] in mapped_paths),
            "requiringAssessment": sum(1 for i in items if i["assessmentRequired"]),
            "manualOrAssessment": sum(1 for i in items if i["migrationPath"] in (ASSESSMENT, MANUAL)),
            "failedCategories": failed,
            "warnings": warnings[:50],
            "warningCount": len(warnings),
            "coverage": {
                "discoveredTypes": discovered_types,
                "notDiscovered": [{"type": t, "reason": r} for t, r in mapping.NOT_DISCOVERED],
            },
        }

    def discovery_status(self) -> dict:
        with self._lock:
            job = self._job
            return {
                "state": job.state,
                "startedAt": job.started_at,
                "finishedAt": job.finished_at,
                "error": job.error,
                "workspace": job.workspace,
                # The discovery run is one call into the existing engine and
                # exposes no per-stage callbacks, so no progress is reported
                # rather than a fabricated one.
                "progress": None,
                "summary": job.summary,
            }

    def results(self, query: Dict[str, str]) -> dict:
        with self._lock:
            job = self._job
            if job.state not in ("completed", "completed_with_warnings"):
                raise ApiError(409, "no_results", "There are no discovery results yet.")
            items = job.items

        def csv(name: str) -> set:
            return {v for v in (query.get(name) or "").split(",") if v}

        categories, types, statuses = csv("category"), csv("type"), csv("status")
        targets, paths, streams, mapping_statuses = csv("fabricTarget"), csv("path"), csv("workstream"), csv("mappingStatus")
        classes = csv("classification")
        assessment = (query.get("assessment") or "").lower()
        search = (query.get("search") or "").strip().lower()

        def matches(i: dict) -> bool:
            if categories and i["category"] not in categories: return False
            if types and i["type"] not in types: return False
            if statuses and i["status"] not in statuses: return False
            if targets and i["fabricTarget"] not in targets: return False
            if paths and i["migrationPath"] not in paths: return False
            if streams and i["workstream"] not in streams: return False
            if mapping_statuses and i["mappingStatus"] not in mapping_statuses: return False
            if classes and i["classification"] not in classes: return False
            if assessment == "yes" and not i["assessmentRequired"]: return False
            if assessment == "no" and i["assessmentRequired"]: return False
            if search and not any(search in str(i[k]).lower() for k in ("name", "type", "fabricTarget", "migrationPath")):
                return False
            return True

        filtered = [i for i in items if matches(i)]

        sort = query.get("sort") or "name"
        columns = {"name": "name", "type": "type", "category": "category", "status": "status",
                   "workspace": "workspace", "target": "fabricTarget", "targetType": "targetType",
                   "path": "migrationPath", "automation": "automationPotential",
                   "assessment": "assessmentRequired", "mappingStatus": "mappingStatus",
                   "classification": "classification"}
        if sort in ("dependencies", "wave"):
            field = "dependencyCount" if sort == "dependencies" else "wave"
            key = lambda i: (i[field], i["name"].lower())  # noqa: E731
        else:
            column = columns.get(sort, "name")
            key = lambda i: (str(i[column]).lower(), i["name"].lower())  # noqa: E731
        filtered.sort(key=key, reverse=(query.get("dir") == "desc"))

        page_size = max(1, min(int(query.get("pageSize") or 25), 200))
        page = max(1, int(query.get("page") or 1))
        start = (page - 1) * page_size

        def count(field_name: str) -> Dict[str, int]:
            out: Dict[str, int] = {}
            for i in items:
                out[str(i[field_name])] = out.get(str(i[field_name]), 0) + 1
            return dict(sorted(out.items()))

        return {
            "items": filtered[start : start + page_size],
            "total": len(filtered),
            "page": page,
            "pageSize": page_size,
            "facets": {
                "categories": count("category"),
                "types": count("type"),
                "statuses": count("status"),
                "fabricTargets": count("fabricTarget"),
                "paths": count("migrationPath"),
                "workstreams": count("workstream"),
                "mappingStatuses": count("mappingStatus"),
                "classifications": count("classification"),
                "assessment": {"Yes": sum(1 for i in items if i["assessmentRequired"]),
                               "No": sum(1 for i in items if not i["assessmentRequired"])},
            },
            "discoveredAt": job.finished_at,
        }

    def _completed_job(self) -> _Job:
        with self._lock:
            job = self._job
            if job.state not in ("completed", "completed_with_warnings"):
                raise ApiError(409, "no_results", "There are no discovery results yet.")
            return job

    def source_sql_factory(self) -> Optional[Callable[[], Any]]:
        """Opens a read-only session on the connected Synapse pool, for copying table data. None if not connected.

        The session reuses the sign-in discovery used; nothing here holds a credential."""
        with self._lock:
            conn = self._connection
        if conn is None:
            return None
        return lambda: conn.manager.sql().connect()

    def source_endpoint(self) -> Optional[Tuple[str, str, str]]:
        """(workspace, SQL endpoint host, pool) of the connected Synapse source, for the data pipelines' connection. None if unknown."""
        with self._lock:
            conn = self._connection
        if conn is None or not conn.sql_pool:
            return None
        return conn.workspace, conn.manager.settings.synapse.resolved_sql_endpoint, conn.sql_pool

    def migration_snapshot(self) -> Tuple["_Job", Optional[str]]:
        """The finished discovery and the SQL pool it read, for a migration run.

        The job is replaced, never mutated, once it has finished, so handing it
        out is safe while a new discovery starts."""
        job = self._completed_job()
        with self._lock:
            pool = self._connection.sql_pool if self._connection else None
        return job, pool

    def dependency_graph(self) -> dict:
        """The dependency graph and suggested migration waves."""
        job = self._completed_job()
        return {**job.graph, "discoveredAt": job.finished_at}

    @staticmethod
    def components() -> dict:
        """Every Synapse component with its preliminary Fabric component and action."""
        return {"components": component_table()}

    def export(self) -> dict:
        """The whole inventory, for download. Read-only and credential-free."""
        job = self._completed_job()
        names = {i["id"]: i["name"] for i in job.items}
        needs: Dict[str, List[str]] = {}
        for edge in job.graph["edges"]:
            needs.setdefault(edge["source"], []).append(names.get(edge["target"], edge["target"]))
        objects = [{**i, "dependsOn": sorted(needs.get(i["id"], []))} for i in job.items]
        return {
            "platform": SOURCE_PLATFORM,
            "workspace": job.workspace,
            "discoveredAt": job.finished_at,
            "summary": job.summary,
            "objects": mapping.sanitize(objects),
        }

    def result(self, object_id: str) -> dict:
        with self._lock:
            job = self._job
            record = job.records_by_id.get(object_id)
            extra = job.extras_by_id.get(object_id)
            if record is None and extra is None:
                raise ApiError(404, "object_not_found", "That object is not in the current discovery results.")
            cached = self._detail_cache.get(object_id)
            if cached is not None:
                return cached
            reverse = job.referenced_by.get(object_id, [])
            if record is not None:
                payload = mapping.detail(record, job.workspace, job.known, reverse)
            else:
                payload = mapping.extra_detail(extra, job.workspace, job.known, reverse)
            payload["discoveredAt"] = job.finished_at
            self._detail_cache[object_id] = payload
            return payload