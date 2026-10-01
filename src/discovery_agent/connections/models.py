"""What the application is pointed at: names, identifiers and endpoints.

This is the configuration half of the connection layer, kept apart from the
authentication half (``azure``), the connection half (``azure``, ``synapse``,
``sql``, ``git``) and the validation half (``validation``). A UI will
eventually populate these objects from a form; ``settings_from_environment``
exists so a developer can populate them from a shell today, and is a
convenience rather than the interface.

**Nothing here can hold a secret.** Not "is redacted before printing" --
cannot hold one. Every field is a name, an identifier or a hostname; there is
no password field, no token field and no client-secret field for one to be
assigned to, and a repository URL with credentials embedded in it is refused
at construction rather than carried and stripped later. That is the whole
security argument for this module, and it is checkable by reading the field
list.

Reuse, not duplication. ``SqlConnectionConfig`` already exists in
``discovery_agent.sql.config`` and stays there: ``SynapseConnectionConfig``
*derives* one rather than restating its fields, so there is exactly one
definition of how to reach a SQL endpoint. The same applies to the repository
provider registry, which stays in ``discovery_agent.acquisition``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from discovery_agent.acquisition.providers import (
    DEFAULT_PROVIDERS,
    GitAuthMechanism,
    GitProvider,
    GitTransport,
    detect_provider,
    parse_git_url,
)
from discovery_agent.errors import (
    AcquisitionError,
    ConfigError,
    UnsupportedProviderError,
)
from discovery_agent.sql.auth import EntraAuthMethod
from discovery_agent.sql.config import (
    DEFAULT_ODBC_DRIVER,
    ENV_DATABASE,
    ENV_SERVER,
    SqlConnectionConfig,
)

#: The SQL endpoint a Synapse workspace exposes for its dedicated pools. The
#: serverless endpoint is a different host (``-ondemand``) and is out of scope
#: for this task.
DEDICATED_SQL_SUFFIX = ".sql.azuresynapse.net"
#: The Synapse Artifacts data-plane host. Recorded for downstream Synapse
#: discovery; nothing in this task calls it.
DEVELOPMENT_SUFFIX = ".dev.azuresynapse.net"

#: Environment variables the dev command reads. The two SQL names are imported
#: from ``sql.config`` rather than redeclared, so a developer who already has
#: a working ``python -m discovery_agent.sql`` setup needs no new variables
#: for the endpoint and database.
ENV_TENANT_ID = "AZURE_TENANT_ID"
ENV_SUBSCRIPTION_ID = "AZURE_SUBSCRIPTION_ID"
ENV_CREDENTIAL_METHOD = "AZURE_CREDENTIAL_METHOD"
ENV_RESOURCE_GROUP = "SYNAPSE_RESOURCE_GROUP"
ENV_WORKSPACE_NAME = "SYNAPSE_WORKSPACE_NAME"
ENV_WORKSPACE_ID = "SYNAPSE_WORKSPACE_ID"
ENV_SQL_POOL = "SYNAPSE_SQL_POOL"
ENV_REPOSITORY_URL = "DISCOVERY_REPOSITORY_URL"
ENV_REPOSITORY_REF = "DISCOVERY_REPOSITORY_REF"


class CredentialMethod(str, Enum):
    """How the application obtains an Azure identity.

    * ``AZURE_CLI`` -- inherit whoever ran ``az login`` on this machine.
    * ``INTERACTIVE_BROWSER`` -- a Microsoft sign-in window, opened on the
      machine running the application, against the tenant named. Leaves the
      Azure CLI session untouched: it neither reads nor writes the CLI's
      cache. One sign-in serves every audience, and an authentication record
      (no token) lets a restarted process sign in silently.

    Adding a method means one more ``AzureCredentialProvider`` subclass, not a
    change of shape here.
    """

    AZURE_CLI = "azure_cli"
    INTERACTIVE_BROWSER = "interactive_browser"
    MANAGED_IDENTITY = "managed_identity"
    SERVICE_PRINCIPAL = "service_principal"


@dataclass(frozen=True)
class AzureConnectionConfig:
    """Which Azure tenant and subscription the run works against.

    ``tenant_id`` is optional because ``az login`` already selected one; it is
    accepted so a machine signed in to several tenants can be pinned to the
    right one rather than picking whichever is default. The interactive
    browser sign-in needs it in practice -- the window must open against the
    operator's tenant -- and the UI requires it for that method.

    ``client_id`` is an application id, a public identifier rather than a
    secret. It is needed only for a tenant that has not consented to
    Microsoft's default developer sign-in application.
    """

    subscription_id: str
    tenant_id: Optional[str] = None
    credential_method: CredentialMethod = CredentialMethod.AZURE_CLI
    client_id: Optional[str] = None

    def validate(self) -> None:
        if not self.subscription_id:
            raise ConfigError("an azure connection needs a subscription id")

    @property
    def subscription_resource_id(self) -> str:
        return f"/subscriptions/{self.subscription_id}"

    def safe_description(self) -> str:
        tenant = self.tenant_id or "default tenant"
        return (
            f"subscription={self.subscription_id} tenant={tenant} "
            f"credential={self.credential_method.value}"
        )

    def to_dict(self) -> dict:
        return {
            "subscription_id": self.subscription_id,
            "tenant_id": self.tenant_id,
            "credential_method": self.credential_method.value,
            "client_id": self.client_id,
        }


@dataclass(frozen=True)
class SynapseConnectionConfig:
    """One Synapse workspace, and the dedicated pool inside it to discover.

    ``workspace_id`` is the workspace's own GUID, which ARM returns and which
    is worth carrying because it identifies the workspace across renames. It
    is optional: it is an output of validation as much as an input to it, and
    requiring it up front would mean an operator could not validate a
    workspace they had only been given the name of.

    ``sql_endpoint`` is optional for the same reason -- it is derivable from
    the workspace name, and ARM returns the authoritative value. An explicit
    setting wins, for a workspace behind a private endpoint with a different
    host.
    """

    resource_group: str
    workspace_name: str
    workspace_id: Optional[str] = None
    sql_pool_name: Optional[str] = None
    database_name: Optional[str] = None
    sql_endpoint: Optional[str] = None

    def validate(self) -> None:
        if not self.resource_group:
            raise ConfigError("a synapse connection needs a resource group")
        if not self.workspace_name:
            raise ConfigError("a synapse connection needs a workspace name")

    @property
    def resolved_sql_endpoint(self) -> str:
        """The dedicated pool host, as configured or as the name implies."""
        return self.sql_endpoint or f"{self.workspace_name}{DEDICATED_SQL_SUFFIX}"

    @property
    def development_endpoint(self) -> str:
        """The Artifacts data-plane host. Recorded, not called, in this build."""
        return f"https://{self.workspace_name}{DEVELOPMENT_SUFFIX}"

    @property
    def resolved_database(self) -> Optional[str]:
        """The database to connect to.

        A dedicated pool's database has the same name as the pool, so one
        setting usually covers both. They are kept as separate fields because
        the equality is a convention rather than a guarantee, and a discovery
        run that silently connected to the wrong database would be worse than
        one that asked.
        """
        return self.database_name or self.sql_pool_name

    def workspace_resource_id(self, subscription_id: str) -> str:
        """The workspace's ARM resource id."""
        return (
            f"/subscriptions/{subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.Synapse/workspaces/{self.workspace_name}"
        )

    def sql_pool_resource_id(self, subscription_id: str) -> str:
        """The dedicated pool's ARM resource id."""
        if not self.sql_pool_name:
            raise ConfigError(
                "no sql pool is configured, so it has no resource id; set "
                f"sql_pool_name or {ENV_SQL_POOL}"
            )
        return (
            f"{self.workspace_resource_id(subscription_id)}"
            f"/sqlPools/{self.sql_pool_name}"
        )

    def sql_connection_config(
        self, driver: str = DEFAULT_ODBC_DRIVER
    ) -> SqlConnectionConfig:
        """The existing SQL connection configuration, derived from this one.

        The single point at which workspace-shaped configuration becomes
        endpoint-shaped configuration. ``SqlConnectionConfig`` is not
        redefined here and not subclassed; it is built, so the SQL package's
        validation, its connection-string rules and its existing tests all
        continue to apply unchanged.

        The mechanism is fixed to ``access_token``: the whole point of this
        layer is that the SQL connection takes its Entra token from the
        central credential rather than opening its own browser prompt.
        """
        database = self.resolved_database
        if not database:
            raise ConfigError(
                "no sql pool or database is configured for workspace "
                f"{self.workspace_name!r}; set sql_pool_name or database_name"
            )
        config = SqlConnectionConfig(
            server=self.resolved_sql_endpoint,
            database=database,
            authentication=EntraAuthMethod.ACCESS_TOKEN,
            driver=driver,
        )
        config.validate()
        return config

    def safe_description(self) -> str:
        return (
            f"workspace={self.workspace_name} "
            f"resource_group={self.resource_group} "
            f"pool={self.sql_pool_name or 'none'}"
        )

    def to_dict(self) -> dict:
        return {
            "resource_group": self.resource_group,
            "workspace_name": self.workspace_name,
            "workspace_id": self.workspace_id,
            "sql_pool_name": self.sql_pool_name,
            "database_name": self.database_name,
            "sql_endpoint": self.sql_endpoint,
            "resolved_sql_endpoint": self.resolved_sql_endpoint,
            "development_endpoint": self.development_endpoint,
        }


@dataclass(frozen=True)
class GitRepositoryConfig:
    """Which repository to acquire, and at which ref.

    No credential field, and none is needed: acquisition shells out to the
    user's own ``git``, so authentication is whatever their credential helper,
    SSH key or Git Credential Manager already does. A URL with a PAT embedded
    in it is the one way a secret could get in here, and it is rejected at
    construction -- ``acquire_repository`` rejects it too, so the rule is
    enforced at both ends and neither relies on the other.

    ``provider``, ``transport`` and ``authentication_mechanism`` are all
    *derived from the URL*, not configured. A user gives a URL and a ref; the
    rest is a fact about that URL, and making any of it a settable field would
    mean it could disagree with reality. ``provider`` remains an optional
    field only so a caller that already knows the answer can have it checked.

    This is entirely independent of Azure. A git credential is the machine's,
    held by git, and no Azure identity is consulted to reach a repository.
    """

    repository_url: str
    ref: Optional[str] = None
    provider: Optional[str] = None

    def __post_init__(self) -> None:
        url = self.repository_url.strip()
        object.__setattr__(self, "repository_url", url)
        if not url:
            return  # an empty URL is a configuration error, reported by validate()
        try:
            parsed = parse_git_url(url)
        except AcquisitionError:
            return  # likewise: an unparseable URL is validate()'s to report
        if parsed.has_credentials:
            # The message describes what was found and never quotes it: an
            # error that echoed the URL would write the token into the very
            # report that rejected it, and into whatever log holds that report.
            raise ConfigError(
                "a repository URL must not embed credentials. This tool never "
                "stores or accepts a PAT, password or token; remove the "
                "credential from the URL and let git authenticate with the "
                "credential helper or SSH key this machine already has."
            )

    def validate(self) -> None:
        if not self.repository_url:
            raise ConfigError("a git connection needs a repository url")
        try:
            parse_git_url(self.repository_url)
            # A URL no provider can route is a configuration error, and it is
            # worth raising here rather than at clone time: the generic
            # provider covers every unknown *host*, so reaching this means the
            # path is malformed for the host it names.
            detected = self.detected_provider()
        except AcquisitionError as exc:
            raise ConfigError(str(exc)) from exc
        if self.provider:
            if detected.lower() != self.provider.strip().lower():
                raise ConfigError(
                    f"repository url {self.repository_url} is handled by the "
                    f"{detected!r} provider, but {self.provider!r} was configured"
                )

    def detected_provider(self, providers=DEFAULT_PROVIDERS) -> str:
        """The provider that handles this URL, whatever was declared.

        Detection stays in ``acquisition.providers``; this is a lookup, not a
        second registry.
        """
        return detect_provider(self.repository_url, providers).name

    def provider_kind(self, providers=DEFAULT_PROVIDERS) -> GitProvider:
        """The hosting service, as an enum rather than a string."""
        return detect_provider(self.repository_url, providers).provider

    @property
    def transport(self) -> GitTransport:
        """HTTPS or SSH, read off the URL."""
        return parse_git_url(self.repository_url).transport

    @property
    def authentication_mechanism(self) -> GitAuthMechanism:
        """Which of the machine's credential systems git will use.

        The mechanism, never a credential: HTTPS means whatever
        ``credential.helper`` is configured to, SSH means the user's key and
        agent. This application reads neither.
        """
        return parse_git_url(self.repository_url).authentication

    @property
    def repository_name(self) -> str:
        return parse_git_url(self.repository_url).name

    def safe_description(self) -> str:
        return f"repository={self.repository_url} ref={self.ref or 'default branch'}"

    def to_dict(self) -> dict:
        """Serializable form.

        Carries the derived provider, transport and mechanism as well as the
        declared ones, because that is what a UI shows back to the user after
        they paste a URL. Every value is a name; none is a credential.
        """
        payload = {
            "provider": self.provider,
            "repository_url": self.repository_url,
            "ref": self.ref,
        }
        try:
            payload["detected_provider"] = self.detected_provider()
            payload["transport"] = self.transport.value
            payload["authentication"] = self.authentication_mechanism.value
        except (AcquisitionError, ConfigError, UnsupportedProviderError):
            # An unusable URL is validate()'s to report, not to_dict()'s to
            # crash on: a form that cannot serialize what the user typed
            # cannot show them what is wrong with it.
            pass
        return payload


@dataclass(frozen=True)
class ConnectionSettings:
    """Every connection one run is configured with.

    All four are optional so a partially-configured run can still validate
    what it does have: a repository-only run needs no Azure, and an operator
    checking their Azure access should not have to invent a repository URL
    first. ``ConnectionManager`` reports an absent connection as skipped,
    never as failed.

    Fabric is deliberately absent. It is a migration *target*, no abstraction
    for it exists anywhere in this codebase yet, and inventing an empty one
    here would be a guess about a shape we have not built.
    """

    azure: Optional[AzureConnectionConfig] = None
    synapse: Optional[SynapseConnectionConfig] = None
    git: Optional[GitRepositoryConfig] = None

    def validate(self) -> None:
        """Validate whatever is configured. Absence is not an error here."""
        for config in (self.azure, self.synapse, self.git):
            if config is not None:
                config.validate()

    def to_dict(self) -> dict:
        return {
            "azure": self.azure.to_dict() if self.azure else None,
            "synapse": self.synapse.to_dict() if self.synapse else None,
            "git": self.git.to_dict() if self.git else None,
        }


def settings_from_environment(environ=None) -> ConnectionSettings:
    """Build settings from environment variables, for developer testing.

    A convenience, not the interface: the UI will construct these objects
    directly, and every function in this package takes the objects rather than
    reading the environment itself. A section left unset produces ``None``
    rather than a half-built configuration, so an unconfigured connection
    reports as skipped instead of failing with a confusing message about an
    empty name.
    """
    environment = os.environ if environ is None else environ

    def value(name: str) -> Optional[str]:
        text = environment.get(name, "").strip()
        return text or None

    azure = None
    subscription_id = value(ENV_SUBSCRIPTION_ID)
    if subscription_id:
        method_name = value(ENV_CREDENTIAL_METHOD) or CredentialMethod.AZURE_CLI.value
        try:
            method = CredentialMethod(method_name)
        except ValueError:
            known = ", ".join(m.value for m in CredentialMethod)
            raise ConfigError(
                f"unknown credential method {method_name!r}; known: {known}"
            ) from None
        azure = AzureConnectionConfig(
            subscription_id=subscription_id,
            tenant_id=value(ENV_TENANT_ID),
            credential_method=method,
        )

    synapse = None
    workspace_name = value(ENV_WORKSPACE_NAME)
    resource_group = value(ENV_RESOURCE_GROUP)
    if workspace_name and resource_group:
        synapse = SynapseConnectionConfig(
            resource_group=resource_group,
            workspace_name=workspace_name,
            workspace_id=value(ENV_WORKSPACE_ID),
            sql_pool_name=value(ENV_SQL_POOL),
            database_name=value(ENV_DATABASE),
            sql_endpoint=value(ENV_SERVER),
        )

    git = None
    repository_url = value(ENV_REPOSITORY_URL)
    if repository_url:
        git = GitRepositoryConfig(
            repository_url=repository_url, ref=value(ENV_REPOSITORY_REF)
        )

    return ConnectionSettings(azure=azure, synapse=synapse, git=git)
