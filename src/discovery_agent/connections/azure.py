"""The one place in the application that holds an Azure identity.

Everything that needs to prove who it is to Azure -- the workspace
connection, the SQL connection, and whatever data-plane connection comes next
-- asks this module for a token. Nothing else acquires one, and no extractor,
catalog query or acquisition step may import it. That is the dependency rule
requirement 7 states, and it is the reason a token's blast radius is one
module rather than the whole codebase.

Three things are kept apart deliberately:

* **The credential** (``AzureCredentialProvider``) -- how the process obtains
  an identity. One implementation today: the Azure CLI's existing sign-in.
* **The token** (``AccessToken``) -- a value with an expiry, which knows how
  to keep itself out of a log line.
* **The connection** (``AzureConnection``) -- the configured subscription,
  what can be proved about access to it, and where a token for each audience
  comes from.

A token is never pasted in, never read from configuration, and never written
anywhere. It is acquired on demand and cached in memory for as long as it is
valid, which is what makes "the user never handles a token" true rather than
merely intended.

Note on the module name: this file is ``discovery_agent.connections.azure``
and the SDK package is the top-level ``azure``. Python 3 resolves ``import
azure.identity`` absolutely, so the two do not collide -- but only because
every import in this package is absolute. Do not add a relative one.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Mapping, Optional, Tuple

from discovery_agent.connections.models import AzureConnectionConfig, CredentialMethod
from discovery_agent.connections.validation import (
    ConnectionValidation,
    ErrorCategory,
    category_for_status,
    failed,
    ok,
)
from discovery_agent.errors import (
    AzureAuthenticationError,
    AzureConnectionError,
    AzureDependencyNotAvailableError,
    ConfigError,
)
from discovery_agent.extractors.models import SourceType

#: Azure Resource Manager: workspace and pool metadata, role assignments.
ARM_SCOPE = "https://management.azure.com/.default"
#: Azure SQL / Synapse SQL endpoints, over TDS. The audience is
#: ``database.windows.net`` even for a Synapse pool.
SQL_SCOPE = "https://database.windows.net/.default"
#: The Synapse Artifacts data plane. Declared for the Synapse discovery that
#: follows this task; nothing here requests it.
SYNAPSE_SCOPE = "https://dev.azuresynapse.net/.default"

ARM_ENDPOINT = "https://management.azure.com"
#: The subscriptions API version used for the access check. Pinned rather than
#: latest, so a service-side default change cannot alter what validation does.
SUBSCRIPTION_API_VERSION = "2022-12-01"

#: Re-acquire this long before expiry. A token that is valid when we check it
#: and expired when the server sees it fails a login for no discoverable
#: reason, so the margin is generous.
EXPIRY_MARGIN_SECONDS = 300

DEFAULT_HTTP_TIMEOUT_SECONDS = 30


class AccessToken:
    """An Entra access token and the moment it stops being one.

    Not a dataclass and not a ``str`` subclass, both on purpose: the value is
    reachable only through ``.value``, and ``repr``/``str`` are overridden to
    describe the token rather than to render it. Anything that formats one of
    these into a message, a traceback, a f-string or a ``pprint`` gets the
    description. Reaching the secret takes a deliberate attribute access.
    """

    __slots__ = ("_value", "expires_on", "scope")

    def __init__(self, value: str, expires_on: int, scope: str) -> None:
        if not value:
            raise AzureAuthenticationError(
                f"the credential returned an empty token for scope {scope}"
            )
        self._value = value
        self.expires_on = int(expires_on)
        self.scope = scope

    @property
    def value(self) -> str:
        """The token itself. The only way to reach it, and deliberately so."""
        return self._value

    def is_valid(self, now: Optional[float] = None, margin: int = EXPIRY_MARGIN_SECONDS) -> bool:
        current = time.time() if now is None else now
        return self.expires_on - margin > current

    def __repr__(self) -> str:
        return f"AccessToken(scope={self.scope!r}, expires_on={self.expires_on})"

    __str__ = __repr__


class AzureCredentialProvider(ABC):
    """Supplies Entra access tokens for the audiences this application uses.

    One method, ``token``, because one thing is needed. Callers name an
    audience; they never name a mechanism, so swapping the Azure CLI for a
    managed identity later changes this module and nothing else.
    """

    method: CredentialMethod

    @abstractmethod
    def token(self, scope: str) -> AccessToken:
        """A currently-valid token for ``scope``.

        Raises AzureAuthenticationError if no identity can be obtained.
        """

    @abstractmethod
    def describe(self) -> str:
        """A safe one-line description. Names a mechanism, never a credential."""

    def token_provider_for(self, scope: str):
        """A zero-argument callable returning the raw token for ``scope``.

        The adapter the SQL package's ``AccessTokenAuthentication`` takes. It
        is a function rather than a token so the SQL layer holds no credential
        between connections and re-acquires on each connect -- which is also
        what makes a long-running discovery survive an expiry.
        """

        def provide() -> str:
            return self.token(scope).value

        return provide


class _CachingCredentialProvider(AzureCredentialProvider):
    """Shared caching for providers that acquire one token per audience.

    In memory, for the life of the process, never written to disk. Caching is
    not an optimisation here: ``az account get-access-token`` shells out, and
    doing that per catalog query would make discovery visibly slow.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, AccessToken] = {}

    def token(self, scope: str) -> AccessToken:
        if not scope:
            raise ConfigError("a token request needs a scope")
        cached = self._cache.get(scope)
        if cached is not None and cached.is_valid():
            return cached
        acquired = self._acquire(scope)
        self._cache[scope] = acquired
        return acquired

    @abstractmethod
    def _acquire(self, scope: str) -> AccessToken:
        """Obtain a fresh token for one scope."""

    def __repr__(self) -> str:
        """Names the mechanism and the audiences held, never the tokens.

        The default dataclass-style repr of a caching object is exactly how a
        token reaches a log file, so this one is written out.
        """
        scopes = ", ".join(sorted(self._cache)) or "none"
        return f"{type(self).__name__}(method={self.method.value}, cached_scopes=[{scopes}])"


class AzureCliCredentialProvider(_CachingCredentialProvider):
    """The identity the developer already has: whoever ran ``az login``.

    The right first mechanism for this POC, for the same reason interactive
    ODBC authentication was the right first mechanism for SQL -- no secret is
    configured, none is stored, and the tool inherits a sign-in the user
    performed themselves and can revoke themselves.

    ``azure-identity`` is imported at acquisition time rather than at module
    import, matching how ``PyodbcConnector`` treats ``pyodbc``: a
    repository-only run must keep working on a machine that has neither.
    """

    method = CredentialMethod.AZURE_CLI

    def __init__(self, tenant_id: Optional[str] = None, credential: Any = None) -> None:
        super().__init__()
        self.tenant_id = tenant_id
        #: Injectable so unit tests never shell out to the CLI. Production
        #: passes nothing and the real credential is built on first use.
        self._credential = credential

    def describe(self) -> str:
        tenant = f" (tenant {self.tenant_id})" if self.tenant_id else ""
        return f"Azure CLI sign-in{tenant}"

    def _build_credential(self) -> Any:
        if self._credential is not None:
            return self._credential
        try:
            from azure.identity import (  # noqa: PLC0415 - optional dependency
                AzureCliCredential,
            )
        except ImportError as exc:
            raise AzureDependencyNotAvailableError(
                "azure-identity is required to authenticate to Azure and is "
                "not installed; install the optional extra with "
                "`pip install -e .[azure]`"
            ) from exc
        kwargs = {"tenant_id": self.tenant_id} if self.tenant_id else {}
        self._credential = AzureCliCredential(**kwargs)
        return self._credential

    def _acquire(self, scope: str) -> AccessToken:
        credential = self._build_credential()
        try:
            acquired = credential.get_token(scope)
        except AzureAuthenticationError:
            raise
        except Exception as exc:  # CredentialUnavailableError, ClientAuthenticationError
            # The exception text is the SDK's and may quote a command line;
            # it is passed through because it is genuinely the actionable
            # part ("run az login"), and every result object redacts.
            raise AzureAuthenticationError(
                f"could not obtain an Azure CLI token for {scope}: "
                f"{type(exc).__name__}: {exc}. Check that the Azure CLI is "
                f"installed and that `az login` has been run."
            ) from exc
        return AccessToken(
            value=acquired.token, expires_on=acquired.expires_on, scope=scope
        )


class InteractiveBrowserCredentialProvider(_CachingCredentialProvider):
    """A fresh, explicit sign-in: the operator authenticates in a browser window.

    Unlike the Azure CLI mechanism this does not inherit whoever ran ``az
    login``; every new provider asks. The browser opens on the machine running
    the application, which for this tool is the operator's own. After the first
    sign-in the SDK's in-memory cache supplies tokens for the other audiences
    (Synapse, SQL) without a second prompt. Nothing is written to disk.
    """

    method = CredentialMethod.INTERACTIVE_BROWSER

    def __init__(
        self,
        tenant_id: Optional[str] = None,
        credential: Any = None,
        timeout_seconds: int = 300,
    ) -> None:
        super().__init__()
        self.tenant_id = tenant_id
        self.timeout_seconds = timeout_seconds
        self._credential = credential

    def describe(self) -> str:
        tenant = f" (tenant {self.tenant_id})" if self.tenant_id else ""
        return f"Interactive Azure sign-in{tenant}"

    def _build_credential(self) -> Any:
        if self._credential is not None:
            return self._credential
        try:
            from azure.identity import (  # noqa: PLC0415 - optional dependency
                InteractiveBrowserCredential,
            )
        except ImportError as exc:
            raise AzureDependencyNotAvailableError(
                "azure-identity is required to authenticate to Azure and is "
                "not installed; install the optional extra with "
                "`pip install -e .[azure]`"
            ) from exc
        kwargs: Dict[str, Any] = {"timeout": self.timeout_seconds}
        if self.tenant_id:
            kwargs["tenant_id"] = self.tenant_id
        self._credential = InteractiveBrowserCredential(**kwargs)
        return self._credential

    def _acquire(self, scope: str) -> AccessToken:
        credential = self._build_credential()
        try:
            acquired = credential.get_token(scope)
        except AzureAuthenticationError:
            raise
        except Exception as exc:  # ClientAuthenticationError, timeout, user cancelled
            raise AzureAuthenticationError(
                f"the Azure sign-in did not complete for {scope}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return AccessToken(
            value=acquired.token, expires_on=acquired.expires_on, scope=scope
        )


class ServicePrincipalCredentialProvider(_CachingCredentialProvider):
    """An application identity: tenant, client id and client secret.

    The one place a client secret is held, and only in this object's memory
    for the life of the process. It is never a configuration field, never in
    ``repr``/``describe``, and is dropped from this object as soon as the SDK
    credential has been built. The client *id* is not a secret and is named in
    ``describe`` so an operator can see which application is in use.
    """

    method = CredentialMethod.SERVICE_PRINCIPAL

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        credential: Any = None,
    ) -> None:
        super().__init__()
        if not (tenant_id and client_id and (client_secret or credential is not None)):
            raise ConfigError("a service principal needs a tenant id, a client id and a client secret")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._client_secret: Optional[str] = client_secret
        self._credential = credential

    def describe(self) -> str:
        return f"Service principal {self.client_id} (tenant {self.tenant_id})"

    def _build_credential(self) -> Any:
        if self._credential is not None:
            return self._credential
        try:
            from azure.identity import ClientSecretCredential  # noqa: PLC0415
        except ImportError as exc:
            raise AzureDependencyNotAvailableError(
                "azure-identity is required to authenticate to Azure and is "
                "not installed; install the optional extra with "
                "`pip install -e .[azure]`"
            ) from exc
        self._credential = ClientSecretCredential(
            tenant_id=self.tenant_id,
            client_id=self.client_id,
            client_secret=self._client_secret or "",
        )
        self._client_secret = None  # the SDK object holds it now; this copy is gone
        return self._credential

    def _acquire(self, scope: str) -> AccessToken:
        credential = self._build_credential()
        try:
            acquired = credential.get_token(scope)
        except AzureAuthenticationError:
            raise
        except Exception as exc:  # ClientAuthenticationError: bad id, bad secret, wrong tenant
            raise AzureAuthenticationError(
                f"the service principal could not obtain a token for {scope}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return AccessToken(
            value=acquired.token, expires_on=acquired.expires_on, scope=scope
        )


class ManagedIdentityCredentialProvider(_CachingCredentialProvider):
    """The identity assigned to the Azure compute the application runs on.

    Only works where the platform provides one (a VM, App Service, container
    app...). ``client_id`` selects a user-assigned identity; omit it for the
    system-assigned one. Nothing secret is involved: the platform vouches.
    """

    method = CredentialMethod.MANAGED_IDENTITY

    def __init__(self, client_id: Optional[str] = None, credential: Any = None) -> None:
        super().__init__()
        self.client_id = client_id
        self._credential = credential

    def describe(self) -> str:
        which = f"user-assigned {self.client_id}" if self.client_id else "system-assigned"
        return f"Managed identity ({which})"

    def _build_credential(self) -> Any:
        if self._credential is not None:
            return self._credential
        try:
            from azure.identity import ManagedIdentityCredential  # noqa: PLC0415
        except ImportError as exc:
            raise AzureDependencyNotAvailableError(
                "azure-identity is required to authenticate to Azure and is "
                "not installed; install the optional extra with "
                "`pip install -e .[azure]`"
            ) from exc
        kwargs = {"client_id": self.client_id} if self.client_id else {}
        self._credential = ManagedIdentityCredential(**kwargs)
        return self._credential

    def _acquire(self, scope: str) -> AccessToken:
        credential = self._build_credential()
        try:
            acquired = credential.get_token(scope)
        except AzureAuthenticationError:
            raise
        except Exception as exc:  # CredentialUnavailableError off Azure compute
            raise AzureAuthenticationError(
                f"no managed identity could supply a token for {scope}: "
                f"{type(exc).__name__}: {exc}. A managed identity exists only "
                f"when the accelerator runs on Azure compute that has one assigned."
            ) from exc
        return AccessToken(
            value=acquired.token, expires_on=acquired.expires_on, scope=scope
        )


_TENANT_IN_CHALLENGE = re.compile(
    r"(?:login\.windows\.net|login\.microsoftonline\.com)/"
    r"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})"
)


def discover_tenant_id(subscription_id: str, timeout_seconds: int = 15) -> Optional[str]:
    """The tenant that owns a subscription, without signing in.

    ARM answers an unauthenticated request with a 401 whose
    ``WWW-Authenticate`` header names the tenant. That lets an operator give
    only a subscription id and still be sent to the right tenant's sign-in
    page. Sends no credential; returns None if anything goes wrong, in which
    case the SDK's default tenant is used.
    """
    try:
        import requests  # noqa: PLC0415 - optional dependency, imported on use

        response = requests.get(
            arm_url(f"/subscriptions/{subscription_id}", SUBSCRIPTION_API_VERSION),
            timeout=timeout_seconds,
        )
    except Exception:  # noqa: BLE001 - discovery of the tenant is best effort
        return None
    match = _TENANT_IN_CHALLENGE.search(response.headers.get("WWW-Authenticate", ""))
    return match.group(1) if match else None


def credential_provider(
    method: CredentialMethod = CredentialMethod.AZURE_CLI,
    tenant_id: Optional[str] = None,
    credential: Any = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> AzureCredentialProvider:
    """The one factory for an Azure identity. Everything goes through here.

    Deliberately takes no subscription. An identity is not scoped to one:
    acquiring a token for the SQL audience needs a tenant at most, and
    requiring a subscription id for it would push callers who only want a SQL
    session into inventing one -- or, worse, into building their own
    credential. That is the hole the old interactive ODBC path went through.

    An unimplemented method fails loudly rather than quietly falling back to
    one that works: a silent switch to a different identity would change what
    discovery can see, which is a correctness problem as much as a security
    one.
    """
    if method is CredentialMethod.AZURE_CLI:
        return AzureCliCredentialProvider(tenant_id=tenant_id, credential=credential)
    if method is CredentialMethod.INTERACTIVE_BROWSER:
        return InteractiveBrowserCredentialProvider(
            tenant_id=tenant_id, credential=credential
        )
    if method is CredentialMethod.SERVICE_PRINCIPAL:
        # The secret is passed straight through to the provider and is never
        # part of any configuration object.
        if credential is None and not (tenant_id and client_id and client_secret):
            raise AzureAuthenticationError(
                "a service principal needs a tenant id, a client id and a client secret"
            )
        return ServicePrincipalCredentialProvider(
            tenant_id=tenant_id or "",
            client_id=client_id or "",
            client_secret=client_secret or "",
            credential=credential,
        )
    if method is CredentialMethod.MANAGED_IDENTITY:
        return ManagedIdentityCredentialProvider(client_id=client_id, credential=credential)
    raise AzureAuthenticationError(f"credential method {method.value!r} is not supported")


def credential_provider_for(
    config: AzureConnectionConfig, credential: Any = None
) -> AzureCredentialProvider:
    """The credential for a configured Azure connection."""
    return credential_provider(
        method=config.credential_method,
        tenant_id=config.tenant_id,
        credential=credential,
    )


# --- the ARM transport seam --------------------------------------------------


class ArmResponse:
    """One ARM reply, reduced to what this package acts on."""

    __slots__ = ("status_code", "payload", "error")

    def __init__(
        self,
        status_code: int,
        payload: Optional[Mapping[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self.payload = dict(payload or {})
        self.error = error

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def message(self) -> str:
        """The service's own explanation, or a plain description of the status.

        ARM nests the useful sentence under ``error.message``; without this an
        operator gets "403" and no indication of which role is missing.
        """
        if self.error:
            return self.error
        error = self.payload.get("error")
        if isinstance(error, Mapping) and error.get("message"):
            return str(error["message"])
        return f"HTTP {self.status_code}"

    def __repr__(self) -> str:
        return f"ArmResponse(status_code={self.status_code})"


class ArmTransport(ABC):
    """Performs one authenticated GET against Azure Resource Manager.

    The seam that keeps every ARM-backed validation testable without a
    network or a subscription, exactly as ``Connector`` does for SQL. Only GET
    exists, and that is the point: this layer reads metadata and has no verb
    with which to change anything.
    """

    @abstractmethod
    def get(self, url: str, token: AccessToken) -> ArmResponse:
        """GET ``url`` with ``token`` as the bearer credential."""


class RequestsArmTransport(ArmTransport):
    """The real transport, over ``requests``.

    Imported lazily for the same reason as ``pyodbc`` and ``azure-identity``:
    the offline repository run must not acquire a dependency it never uses.
    """

    def __init__(self, timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    def get(self, url: str, token: AccessToken) -> ArmResponse:
        requests = self._http_module()
        try:
            response = requests.get(
                url,
                headers={
                    # The one place a token is written into a header. It is
                    # built at the call and never stored on this object.
                    "Authorization": f"Bearer {token.value}",
                    "Accept": "application/json",
                },
                timeout=self.timeout_seconds,
            )
        except Exception as exc:  # requests.RequestException and anything under it
            raise AzureConnectionError(
                f"could not reach Azure Resource Manager: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return ArmResponse(status_code=response.status_code, payload=payload)

    @staticmethod
    def _http_module() -> Any:
        try:
            import requests  # noqa: PLC0415 - optional dependency, imported on use
        except ImportError as exc:
            raise AzureDependencyNotAvailableError(
                "requests is required to call Azure Resource Manager and is "
                "not installed; install the optional extra with "
                "`pip install -e .[azure]`"
            ) from exc
        return requests


def arm_url(resource_id: str, api_version: str) -> str:
    """The management URL for one ARM resource id."""
    return f"{ARM_ENDPOINT}{resource_id}?api-version={api_version}"


# --- the connection ----------------------------------------------------------


class AzureConnection:
    """The configured subscription, and the identity used to reach it.

    Holds the credential so nothing downstream has to. ``SynapseWorkspaceConnection``
    and ``SqlConnection`` are both built from one of these rather than each
    constructing its own credential, which is what keeps a single sign-in
    behind every connection in a run.
    """

    def __init__(
        self,
        config: AzureConnectionConfig,
        credential: Optional[AzureCredentialProvider] = None,
        transport: Optional[ArmTransport] = None,
    ) -> None:
        config.validate()
        self.config = config
        self.credential = credential or credential_provider_for(config)
        self.transport = transport or RequestsArmTransport()

    # -- tokens ------------------------------------------------------------

    def management_token(self) -> AccessToken:
        """A token for Azure Resource Manager."""
        return self.credential.token(ARM_SCOPE)

    def sql_token(self) -> AccessToken:
        """A token for the SQL endpoint's audience."""
        return self.credential.token(SQL_SCOPE)

    def sql_token_provider(self):
        """The callable the SQL connection authenticates with."""
        return self.credential.token_provider_for(SQL_SCOPE)

    # -- ARM ---------------------------------------------------------------

    def get_resource(self, resource_id: str, api_version: str) -> ArmResponse:
        """Read one ARM resource. The only Azure verb this package has."""
        return self.transport.get(
            arm_url(resource_id, api_version), self.management_token()
        )

    def list_values(self, resource_path: str, api_version: str) -> List[Mapping[str, Any]]:
        """Every item of an ARM collection, following ``nextLink`` paging.

        GET only, like the rest of this class. A ``nextLink`` that does not
        point at ARM is refused: the bearer token is only ever sent there.
        """
        url: Optional[str] = arm_url(resource_path, api_version)
        values: List[Mapping[str, Any]] = []
        while url:
            if not url.startswith(ARM_ENDPOINT + "/"):
                raise AzureConnectionError("Azure returned a paging link outside Resource Manager")
            response = self.transport.get(url, self.management_token())
            if not response.ok:
                raise AzureConnectionError(
                    f"could not list {resource_path}: {response.message()} "
                    f"(HTTP {response.status_code})"
                )
            values.extend(response.payload.get("value") or [])
            url = response.payload.get("nextLink")
        return values

    def list_resource_groups(self) -> List[str]:
        """Names of the resource groups in the subscription."""
        items = self.list_values(
            f"{self.config.subscription_resource_id}/resourcegroups", "2021-04-01"
        )
        return sorted((str(i["name"]) for i in items if i.get("name")), key=str.lower)

    def list_synapse_workspaces(self, resource_group: str) -> List[str]:
        """Names of the Synapse workspaces in one resource group."""
        items = self.list_values(
            f"{self.config.subscription_resource_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Synapse/workspaces",
            "2021-06-01",
        )
        return sorted((str(i["name"]) for i in items if i.get("name")), key=str.lower)

    def list_sql_pools(self, resource_group: str, workspace: str) -> List[str]:
        """Names of the dedicated SQL pools in one workspace."""
        items = self.list_values(
            f"{self.config.subscription_resource_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Synapse/workspaces/{workspace}/sqlPools",
            "2021-06-01",
        )
        return sorted((str(i["name"]) for i in items if i.get("name")), key=str.lower)

    def describe(self) -> str:
        """A safe one-line description. Contains no credential."""
        return f"{self.config.safe_description()} via {self.credential.describe()}"

    # -- validation --------------------------------------------------------

    def validate(self) -> ConnectionValidation:
        """Prove an identity exists and that it can read the subscription.

        Two steps, reported separately, because they fail for different
        reasons and need different fixes: acquiring a token is authentication
        (``az login``), and reading the subscription is authorization (a role
        assignment). Collapsing them into "Azure failed" would cost an
        operator the one piece of information that tells them what to do.
        """
        details = {
            "subscription_id": self.config.subscription_id,
            "credential": self.credential.describe(),
        }
        if self.config.tenant_id:
            details["tenant_id"] = self.config.tenant_id

        try:
            token = self.management_token()
        except AzureDependencyNotAvailableError as exc:
            return failed(
                SourceType.AZURE, str(exc), ErrorCategory.DEPENDENCY, **details
            )
        except AzureAuthenticationError as exc:
            return failed(
                SourceType.AZURE, str(exc), ErrorCategory.AUTHENTICATION, **details
            )

        details["token_audience"] = ARM_SCOPE
        details["token_expires_on"] = str(token.expires_on)

        try:
            response = self.get_resource(
                self.config.subscription_resource_id, SUBSCRIPTION_API_VERSION
            )
        except AzureDependencyNotAvailableError as exc:
            return failed(
                SourceType.AZURE, str(exc), ErrorCategory.DEPENDENCY, **details
            )
        except AzureConnectionError as exc:
            return failed(SourceType.AZURE, str(exc), ErrorCategory.NETWORK, **details)

        if not response.ok:
            return failed(
                SourceType.AZURE,
                f"subscription {self.config.subscription_id} could not be read: "
                f"{response.message()}",
                category_for_status(response.status_code),
                status_code=str(response.status_code),
                **details,
            )

        display_name = str(response.payload.get("displayName", "")) or "unnamed"
        state = str(response.payload.get("state", "")) or "unknown"
        # ARM's answer is authoritative about the tenant; a configured
        # tenant_id is only what we asked for.
        details["tenant_id"] = (
            str(response.payload.get("tenantId", ""))
            or details.get("tenant_id", "")
            or "unknown"
        )
        details["subscription_name"] = display_name
        details["subscription_state"] = state
        return ok(
            SourceType.AZURE,
            f"authenticated to subscription {display_name} "
            f"({self.config.subscription_id}), state {state}",
            **details,
        )


def token_audiences() -> Tuple[str, ...]:
    """Every audience this build acquires tokens for. Named, never a secret."""
    return (ARM_SCOPE, SQL_SCOPE)

