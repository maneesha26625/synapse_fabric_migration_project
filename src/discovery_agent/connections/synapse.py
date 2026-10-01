"""The Synapse workspace, as a connection rather than as a data source.

What this module answers: does the workspace exist, can the configured
identity read it, what is its id, where are its endpoints, and is the
dedicated pool actually online. That is everything downstream Synapse
discovery needs before it starts, and nothing more.

What it deliberately does not do:

* **No catalog queries.** A table's columns come from the SQL connection, not
  from here. ARM knows a pool exists; it does not know what is in it.
* **No Artifacts data-plane calls.** Listing pipelines and notebooks is
  ``connections.synapse_artifacts``' job, on a different token audience and a
  different authorization system. The data-plane host is derived here so that
  connection has it; nothing here requests it.
* **No secrets.** The workspace payload is filtered down to named fields.
  A connection-string or key property arriving in a future API version cannot
  end up in a metadata object that nothing here declared a field for.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional

from discovery_agent.acquisition.providers import normalize_for_compare
from discovery_agent.connections.azure import AzureConnection
from discovery_agent.connections.models import SynapseConnectionConfig
from discovery_agent.connections.validation import (
    ConnectionValidation,
    ErrorCategory,
    category_for_status,
    failed,
    ok,
)
from discovery_agent.errors import (
    AcquisitionError,
    AzureConnectionError,
    AzureDependencyNotAvailableError,
    SynapseConnectionError,
)
from discovery_agent.extractors.models import SourceType

#: Pinned, for the same reason the subscription version is: validation must
#: not change behaviour because a service default moved.
WORKSPACE_API_VERSION = "2021-06-01"
SQL_POOL_API_VERSION = "2021-06-01"

#: Pool states that mean a SQL connection can actually be opened. A paused
#: pool is a perfectly healthy resource that will refuse every login, and
#: reporting it as "online" would send an operator to debug their driver.
ONLINE_POOL_STATES = ("online",)


#: ``workspaceRepositoryConfiguration.type`` values, as ARM reports them.
GITHUB_CONFIGURATION = "WorkspaceGitHubConfiguration"
DEVOPS_CONFIGURATION = "WorkspaceVSTSConfiguration"


@dataclass(frozen=True)
class WorkspaceRepository:
    """The repository a workspace is Git-integrated with, as ARM reports it.

    This is the one authoritative answer to a question cross-source discovery
    cannot otherwise settle: is the repository we cloned *this workspace's*
    repository? Two artifacts that share a name are the same artifact only if
    they come from the same workspace, and without this the best available
    evidence is that the names match -- which is a guess.

    Carries no credential and cannot: Git integration is configured with a
    tenant and an account name, and the token that performs it belongs to the
    service, not to us. There is no field here a secret could occupy.
    """

    kind: Optional[str] = None  # the ARM configuration type, verbatim
    host_name: Optional[str] = None
    account_name: Optional[str] = None
    project_name: Optional[str] = None
    repository_name: Optional[str] = None
    collaboration_branch: Optional[str] = None
    root_folder: Optional[str] = None
    last_commit_id: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.account_name and self.repository_name)

    @property
    def is_github(self) -> bool:
        return self.kind == GITHUB_CONFIGURATION

    @property
    def is_azure_devops(self) -> bool:
        return self.kind == DEVOPS_CONFIGURATION

    @property
    def repository_url(self) -> Optional[str]:
        """The clone URL this configuration describes, or None.

        Reconstructed from the parts ARM reports, because ARM does not return
        a URL. Only the two shapes Synapse Git integration supports are built;
        anything else returns None rather than a URL that was guessed at.
        """
        if not self.is_configured:
            return None
        if self.is_github:
            host = (self.host_name or "").strip().rstrip("/") or "https://github.com"
            if not host.startswith("http"):
                host = f"https://{host}"
            return f"{host}/{self.account_name}/{self.repository_name}"
        if self.is_azure_devops:
            if not self.project_name:
                return None
            host = (self.host_name or "").strip().rstrip("/") or "https://dev.azure.com"
            if not host.startswith("http"):
                host = f"https://{host}"
            return (
                f"{host}/{self.account_name}/{self.project_name}"
                f"/_git/{self.repository_name}"
            )
        return None

    def matches(self, repository_url: str) -> Optional[bool]:
        """Whether ``repository_url`` is this workspace's own repository.

        Three answers, and the third is the important one:

        * ``True`` -- the URLs name the same repository. Artifacts found in
          that clone and artifacts found in this workspace can be identified
          with each other.
        * ``False`` -- they name different repositories, so a shared artifact
          name proves nothing.
        * ``None`` -- undecidable, because the workspace reports no Git
          configuration or one whose URL cannot be reconstructed. Not the
          same as False, and never rounded to it.

        Comparison reuses ``acquisition.providers.normalize_for_compare``, the
        same canonicalisation that decides whether an existing clone may be
        reused -- so "the same repository" means one thing in this codebase.
        """
        mine = self.repository_url
        if not mine or not repository_url:
            return None
        try:
            return normalize_for_compare(mine) == normalize_for_compare(repository_url)
        except AcquisitionError:
            return None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "host_name": self.host_name,
            "account_name": self.account_name,
            "project_name": self.project_name,
            "repository_name": self.repository_name,
            "collaboration_branch": self.collaboration_branch,
            "root_folder": self.root_folder,
            "last_commit_id": self.last_commit_id,
            "repository_url": self.repository_url,
        }


def workspace_repository_from_payload(
    properties: Mapping[str, Any]
) -> Optional[WorkspaceRepository]:
    """The workspace's Git configuration, or None when it has none.

    None means live-only: the workspace is not Git-integrated, so every
    artifact in it exists nowhere else and no repository comparison applies.
    """
    raw = properties.get("workspaceRepositoryConfiguration")
    if not isinstance(raw, Mapping):
        return None
    repository = WorkspaceRepository(
        kind=_text(raw.get("type")),
        host_name=_text(raw.get("hostName")),
        account_name=_text(raw.get("accountName")),
        project_name=_text(raw.get("projectName")),
        repository_name=_text(raw.get("repositoryName")),
        collaboration_branch=_text(raw.get("collaborationBranch")),
        root_folder=_text(raw.get("rootFolder")),
        last_commit_id=_text(raw.get("lastCommitId")),
    )
    return repository if repository.is_configured else None


@dataclass(frozen=True)
class SynapseWorkspaceMetadata:
    """The non-secret facts ARM reports about a workspace.

    A fixed field list rather than the raw payload, so nothing a future API
    version adds -- a key, a connection string, a managed credential -- can
    arrive in an object that downstream code serializes.
    """

    name: str
    workspace_id: Optional[str]
    resource_id: str
    resource_group: str
    location: Optional[str]
    provisioning_state: Optional[str]
    sql_endpoint: Optional[str]
    development_endpoint: Optional[str]
    default_storage_account: Optional[str]
    default_filesystem: Optional[str]
    managed_identity_principal_id: Optional[str]
    repository: Optional[WorkspaceRepository] = None

    @property
    def is_git_integrated(self) -> bool:
        return self.repository is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "workspace_id": self.workspace_id,
            "resource_id": self.resource_id,
            "resource_group": self.resource_group,
            "location": self.location,
            "provisioning_state": self.provisioning_state,
            "sql_endpoint": self.sql_endpoint,
            "development_endpoint": self.development_endpoint,
            "default_storage_account": self.default_storage_account,
            "default_filesystem": self.default_filesystem,
            "managed_identity_principal_id": self.managed_identity_principal_id,
            "repository": self.repository.to_dict() if self.repository else None,
        }


@dataclass(frozen=True)
class SqlPoolMetadata:
    """The non-secret facts ARM reports about one dedicated SQL pool."""

    name: str
    resource_id: str
    status: Optional[str]
    sku: Optional[str]
    collation: Optional[str]
    max_size_bytes: Optional[int]

    @property
    def is_online(self) -> bool:
        return (self.status or "").lower() in ONLINE_POOL_STATES

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "resource_id": self.resource_id,
            "status": self.status,
            "sku": self.sku,
            "collation": self.collation,
            "max_size_bytes": self.max_size_bytes,
        }


def _text(value: Any) -> Optional[str]:
    """A payload value as a string, or None. Never the string "None"."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def workspace_metadata_from_payload(
    payload: Mapping[str, Any], config: SynapseConnectionConfig, resource_id: str
) -> SynapseWorkspaceMetadata:
    """Map an ARM workspace payload onto the metadata object.

    Pure, so the mapping is testable without a subscription -- the same split
    the SQL package makes between ``mapping`` and ``dedicated_pool``.

    Every field degrades to None rather than to a plausible default. A
    workspace whose payload omitted its id must report that it did, because
    the alternative is a discovery run that records a workspace id it made up.
    """
    properties = payload.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}

    storage = properties.get("defaultDataLakeStorage")
    storage = storage if isinstance(storage, Mapping) else {}

    identity = payload.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}

    connectivity = properties.get("connectivityEndpoints")
    connectivity = connectivity if isinstance(connectivity, Mapping) else {}

    return SynapseWorkspaceMetadata(
        name=_text(payload.get("name")) or config.workspace_name,
        workspace_id=_text(properties.get("workspaceUID")),
        resource_id=_text(payload.get("id")) or resource_id,
        resource_group=config.resource_group,
        location=_text(payload.get("location")),
        provisioning_state=_text(properties.get("provisioningState")),
        sql_endpoint=_text(connectivity.get("sql")) or config.resolved_sql_endpoint,
        development_endpoint=_text(connectivity.get("dev"))
        or config.development_endpoint,
        default_storage_account=_text(storage.get("accountUrl")),
        default_filesystem=_text(storage.get("filesystem")),
        managed_identity_principal_id=_text(identity.get("principalId")),
        repository=workspace_repository_from_payload(properties),
    )


def sql_pool_metadata_from_payload(
    payload: Mapping[str, Any], name: str, resource_id: str
) -> SqlPoolMetadata:
    """Map an ARM sqlPools payload onto the pool metadata object. Pure."""
    properties = payload.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    sku = payload.get("sku")
    sku = sku if isinstance(sku, Mapping) else {}

    raw_size = properties.get("maxSizeBytes")
    try:
        max_size = int(raw_size) if raw_size is not None else None
    except (TypeError, ValueError):
        max_size = None

    return SqlPoolMetadata(
        name=_text(payload.get("name")) or name,
        resource_id=_text(payload.get("id")) or resource_id,
        status=_text(properties.get("status")),
        sku=_text(sku.get("name")),
        collation=_text(properties.get("collation")),
        max_size_bytes=max_size,
    )


class SynapseWorkspaceConnection:
    """Access to one Synapse workspace, through the Azure management plane.

    Built from an ``AzureConnection`` rather than from a credential: the
    workspace does not get its own sign-in, it uses the run's. That is what
    makes "one identity per run" structural instead of a convention.
    """

    def __init__(
        self, config: SynapseConnectionConfig, azure: AzureConnection
    ) -> None:
        config.validate()
        self.config = config
        self.azure = azure

    # -- identity ----------------------------------------------------------

    @property
    def resource_id(self) -> str:
        return self.config.workspace_resource_id(self.azure.config.subscription_id)

    @property
    def name(self) -> str:
        return self.config.workspace_name

    @property
    def resource_group(self) -> str:
        return self.config.resource_group

    def describe(self) -> str:
        """A safe one-line description. Contains no credential."""
        return f"{self.config.safe_description()} in {self.azure.config.subscription_id}"

    # -- metadata ----------------------------------------------------------

    def metadata(self) -> SynapseWorkspaceMetadata:
        """Read the workspace from ARM.

        Raises rather than returning a partial object: a caller that asked for
        the workspace's identity and got a stand-in built from configuration
        would have no way to tell the two apart. ``validate`` is the method
        that turns this into a reportable result.
        """
        response = self.azure.get_resource(self.resource_id, WORKSPACE_API_VERSION)
        if not response.ok:
            raise SynapseConnectionError(
                f"workspace {self.name!r} in resource group "
                f"{self.resource_group!r} could not be read: {response.message()}",
                status_code=response.status_code,
            )
        return workspace_metadata_from_payload(
            response.payload, self.config, self.resource_id
        )

    def sql_pool_metadata(self) -> SqlPoolMetadata:
        """Read the configured dedicated SQL pool from ARM."""
        pool_name = self.config.sql_pool_name
        if not pool_name:
            raise SynapseConnectionError(
                f"no dedicated sql pool is configured for workspace {self.name!r}"
            )
        resource_id = self.config.sql_pool_resource_id(
            self.azure.config.subscription_id
        )
        response = self.azure.get_resource(resource_id, SQL_POOL_API_VERSION)
        if not response.ok:
            raise SynapseConnectionError(
                f"sql pool {pool_name!r} could not be read: {response.message()}",
                status_code=response.status_code,
            )
        return sql_pool_metadata_from_payload(response.payload, pool_name, resource_id)

    def resolved_config(self) -> SynapseConnectionConfig:
        """The configuration with anything ARM could confirm filled in.

        What a UI would save after a successful validation: the operator typed
        a workspace name, and this is the same configuration carrying the
        workspace id and the endpoint the service itself reported. Falls back
        to the configuration unchanged if the workspace cannot be read, since
        an unreachable workspace is not a reason to discard what was entered.
        """
        try:
            metadata = self.metadata()
        except (SynapseConnectionError, AzureConnectionError):
            return self.config

        return replace(
            self.config,
            workspace_id=metadata.workspace_id or self.config.workspace_id,
            sql_endpoint=metadata.sql_endpoint or self.config.sql_endpoint,
        )

    # -- validation --------------------------------------------------------

    def validate(self) -> ConnectionValidation:
        """Prove the workspace is readable, and the pool present and online.

        The pool check is part of workspace validation rather than of SQL
        validation on purpose: "the pool is paused" is an ARM fact, and
        discovering it here means an operator is told why the SQL connection
        will fail instead of watching it time out.
        """
        details = {
            "workspace_name": self.name,
            "resource_group": self.resource_group,
            "resource_id": self.resource_id,
        }

        try:
            metadata = self.metadata()
        except AzureDependencyNotAvailableError as exc:
            return failed(
                SourceType.SYNAPSE,
                str(exc),
                ErrorCategory.DEPENDENCY,
                **details,
            )
        except SynapseConnectionError as exc:
            return failed(
                SourceType.SYNAPSE,
                str(exc),
                self._category_for(exc),
                **details,
            )
        except AzureConnectionError as exc:
            return failed(
                SourceType.SYNAPSE, str(exc), ErrorCategory.NETWORK, **details
            )

        details["workspace_id"] = metadata.workspace_id or "not reported"
        details["location"] = metadata.location or "unknown"
        details["provisioning_state"] = metadata.provisioning_state or "unknown"
        details["sql_endpoint"] = metadata.sql_endpoint or "unknown"
        details["development_endpoint"] = metadata.development_endpoint or "unknown"

        if self.config.workspace_id and metadata.workspace_id:
            if self.config.workspace_id.lower() != metadata.workspace_id.lower():
                # A configured id that disagrees with the live one means the
                # run is pointed at a workspace that was deleted and recreated
                # under the same name. Silently preferring either value would
                # attribute discovered artifacts to the wrong workspace.
                return failed(
                    SourceType.SYNAPSE,
                    f"workspace {self.name!r} reports id "
                    f"{metadata.workspace_id}, but {self.config.workspace_id} "
                    f"was configured; the name may have been reused",
                    ErrorCategory.CONFIGURATION,
                    **details,
                )

        if not self.config.sql_pool_name:
            return ok(
                SourceType.SYNAPSE,
                f"workspace {self.name!r} is readable; no dedicated sql pool "
                f"is configured",
                **details,
            )

        try:
            pool = self.sql_pool_metadata()
        except SynapseConnectionError as exc:
            return failed(
                SourceType.SYNAPSE, str(exc), self._category_for(exc), **details
            )
        except AzureConnectionError as exc:
            return failed(
                SourceType.SYNAPSE, str(exc), ErrorCategory.NETWORK, **details
            )

        details["sql_pool"] = pool.name
        details["sql_pool_status"] = pool.status or "unknown"
        if pool.sku:
            details["sql_pool_sku"] = pool.sku

        if not pool.is_online:
            # UNAVAILABLE, not CONFIGURATION: the name is right, the rights
            # are right, and nothing needs correcting. The pool is paused.
            return failed(
                SourceType.SYNAPSE,
                f"dedicated sql pool {pool.name!r} is "
                f"{pool.status or 'in an unknown state'}, so a SQL connection "
                f"will be refused; resume the pool and retry",
                ErrorCategory.UNAVAILABLE,
                **details,
            )

        return ok(
            SourceType.SYNAPSE,
            f"workspace {self.name!r} is readable and dedicated sql pool "
            f"{pool.name!r} is online",
            **details,
        )

    @staticmethod
    def _category_for(exc: SynapseConnectionError) -> ErrorCategory:
        """The category ARM's status code implies.

        The status code travels on the exception rather than being recovered
        from the message text, so "workspace does not exist" and "you may not
        read this workspace" stay distinguishable -- they are the two most
        common failures here and they need opposite remedies.
        """
        if exc.status_code is None:
            return ErrorCategory.UNKNOWN
        return category_for_status(exc.status_code)
