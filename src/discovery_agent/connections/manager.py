"""The one object the rest of the application asks for a connection.

    manager = ConnectionManager(settings)
    report = manager.validate_all()
    source = manager.sql().source()      # a CatalogSource, ready to query
    snapshot = manager.git().acquire()   # a RepositorySource, ready to walk

Everything above this layer -- discovery, extraction, assessment -- takes a
connection or a source that a manager built. Nothing above this layer
constructs a credential, reads a connection environment variable, or knows
what an ODBC driver is. That is the dependency rule, and this class is where
it is enforced: there is a single place that holds the identity, so there is a
single place that can leak one.

Connections are built once and reused, because the credential they share
caches its tokens. Two managers means two token caches and, for the CLI
mechanism, two ``az`` invocations per audience.
"""

from __future__ import annotations

from typing import Any, List, Optional

from discovery_agent.connections.azure import (
    ArmTransport,
    AzureConnection,
    AzureCredentialProvider,
    credential_provider,
)
from discovery_agent.connections.git import GitConnection
from discovery_agent.connections.models import ConnectionSettings, CredentialMethod
from discovery_agent.connections.sql import SqlConnection
from discovery_agent.connections.synapse import SynapseWorkspaceConnection
from discovery_agent.connections.synapse_artifacts import SynapseArtifactsConnection
from discovery_agent.connections.validation import (
    ConnectionValidation,
    ErrorCategory,
    ValidationReport,
    failed,
    skipped,
)
from discovery_agent.errors import (
    ConfigError,
    ConnectionValidationError,
    DiscoveryError,
)
from discovery_agent.extractors.models import SourceType
from discovery_agent.sql.connection import Connector

#: The order ``validate_all`` runs its checks in, and the order a report
#: renders. Azure first because Synapse and SQL both depend on it; git last
#: because it depends on none of them.
VALIDATION_ORDER = (
    SourceType.AZURE,
    SourceType.SYNAPSE,
    SourceType.SQL,
    SourceType.REPOSITORY,
)


class ConnectionManager:
    """Holds the configured connections for one run, and validates them.

    Every connection is lazy. A repository-only run that never touches Azure
    must not acquire a credential, and it does not: ``azure()`` is the first
    thing that builds one, and nothing calls it unless an Azure-backed
    connection is actually used.

    The seams -- ``credential``, ``transport``, ``sql_connector``, ``git`` --
    exist so the whole class is testable with no subscription, no driver and
    no network. Production passes none of them.
    """

    def __init__(
        self,
        settings: ConnectionSettings,
        credential: Optional[AzureCredentialProvider] = None,
        transport: Optional[ArmTransport] = None,
        sql_connector: Optional[Connector] = None,
        git: Any = None,
        artifacts_client: Any = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self._credential = credential
        self._transport = transport
        self._sql_connector = sql_connector
        self._git = git
        self._artifacts_client = artifacts_client

        self._azure: Optional[AzureConnection] = None
        self._synapse: Optional[SynapseWorkspaceConnection] = None
        self._synapse_artifacts: Optional[SynapseArtifactsConnection] = None
        self._sql: Optional[SqlConnection] = None
        self._git_connection: Optional[GitConnection] = None

    # -- the identity ------------------------------------------------------

    def credential(self) -> AzureCredentialProvider:
        """The one Azure identity for this run.

        Built at most once and handed to every Azure-backed connection, which
        is what "log in once" means here: one authenticated session, one token
        cache, one `az login`. Audiences still get their own tokens -- that is
        how OAuth works -- but they come from the same identity.

        Built lazily. A repository-only run must not sign in to anything, and
        does not: nothing calls this until an Azure-backed connection is used.

        The tenant comes from the Azure configuration when there is one. A run
        with no Azure section can still reach a SQL endpoint -- a token needs a
        tenant at most, never a subscription -- so this does not require one.
        """
        if self._credential is None:
            config = self.settings.azure
            self._credential = credential_provider(
                method=config.credential_method if config else CredentialMethod.AZURE_CLI,
                tenant_id=config.tenant_id if config else None,
                client_id=config.client_id if config else None,
            )
        return self._credential

    # -- the connections ---------------------------------------------------

    def azure(self) -> AzureConnection:
        """The Azure connection. Raises ConfigError if none is configured."""
        if self._azure is None:
            if self.settings.azure is None:
                raise ConfigError(
                    "no azure connection is configured; a subscription id is "
                    "needed before anything can authenticate"
                )
            self._azure = AzureConnection(
                self.settings.azure,
                credential=self.credential(),
                transport=self._transport,
            )
        return self._azure

    def synapse(self) -> SynapseWorkspaceConnection:
        """The Synapse workspace connection, on the run's Azure identity."""
        if self._synapse is None:
            if self.settings.synapse is None:
                raise ConfigError("no synapse workspace is configured")
            self._synapse = SynapseWorkspaceConnection(
                self.settings.synapse, self.azure()
            )
        return self._synapse

    def synapse_artifacts(self) -> SynapseArtifactsConnection:
        """The Synapse Artifacts data-plane connection for the workspace.

        Takes the credential, not the Azure connection, for the same reason
        ``sql`` does: reading artifacts needs a token for the
        ``dev.azuresynapse.net`` audience and nothing else. A run with no
        subscription configured can still read a workspace it knows the name
        of, and keeping ARM out of reach is what stops this growing a second
        management client.
        """
        if self._synapse_artifacts is None:
            if self.settings.synapse is None:
                raise ConfigError(
                    "no synapse workspace is configured, so there is no "
                    "artifacts endpoint to read"
                )
            self._synapse_artifacts = SynapseArtifactsConnection(
                self.settings.synapse,
                self.credential(),
                client=self._artifacts_client,
            )
        return self._synapse_artifacts

    def sql(self) -> SqlConnection:
        """The SQL connection for the workspace's dedicated pool.

        Built from the Synapse configuration rather than from a SQL
        configuration of its own: the endpoint, the database and the pool are
        facts about the workspace, and duplicating them would let the two
        drift.

        Takes the credential, not the Azure connection. A SQL session needs a
        token for the ``database.windows.net`` audience and nothing else, and
        keeping the subscription out of reach is what stops this growing a
        second ARM client later.
        """
        if self._sql is None:
            if self.settings.synapse is None:
                raise ConfigError(
                    "no synapse workspace is configured, so there is no sql "
                    "endpoint to connect to"
                )
            self._sql = SqlConnection.for_workspace(
                self.settings.synapse,
                self.credential(),
                connector=self._sql_connector,
            )
        return self._sql

    def git(self) -> GitConnection:
        """The source repository connection."""
        if self._git_connection is None:
            if self.settings.git is None:
                raise ConfigError("no git repository is configured")
            kwargs = {"git": self._git} if self._git is not None else {}
            self._git_connection = GitConnection(self.settings.git, **kwargs)
        return self._git_connection

    # -- validation --------------------------------------------------------

    def validate_azure(self) -> ConnectionValidation:
        """Can this process authenticate to Azure and read the subscription?"""
        if self.settings.azure is None:
            return skipped(SourceType.AZURE, "no azure connection is configured")
        return self._guarded(SourceType.AZURE, lambda: self.azure().validate())

    def validate_synapse(self) -> ConnectionValidation:
        """Is the workspace readable, and its dedicated pool present and online?"""
        if self.settings.synapse is None:
            return skipped(SourceType.SYNAPSE, "no synapse workspace is configured")
        return self._guarded(SourceType.SYNAPSE, lambda: self.synapse().validate())

    def validate_synapse_artifacts(self) -> ConnectionValidation:
        """Can this identity read the workspace's artifacts, not just see it?

        A separate check from ``validate_synapse`` because they test separate
        permission systems. Reader on the resource group satisfies the
        management plane and grants nothing on the data plane, so a workspace
        can validate green while every pipeline listing returns 403.
        """
        if self.settings.synapse is None:
            return skipped(SourceType.SYNAPSE, "no synapse workspace is configured")
        return self._guarded(
            SourceType.SYNAPSE, lambda: self.synapse_artifacts().validate()
        )

    def validate_sql(self) -> ConnectionValidation:
        """Does an Entra-authenticated session open against the pool database?"""
        if self.settings.synapse is None:
            return skipped(
                SourceType.SQL,
                "no synapse workspace is configured, so there is no sql "
                "endpoint to validate",
            )
        return self._guarded(SourceType.SQL, lambda: self.sql().validate())

    def validate_git(self, probe_remote: bool = True) -> ConnectionValidation:
        """Is the repository reachable with the user's own git credentials?"""
        if self.settings.git is None:
            return skipped(
                SourceType.REPOSITORY, "no git repository is configured"
            )
        return self._guarded(
            SourceType.REPOSITORY,
            lambda: self.git().validate(probe_remote=probe_remote),
        )

    def validate_all(self, probe_remote: bool = True) -> ValidationReport:
        """Validate every configured connection, in dependency order.

        Synapse and SQL are *skipped* rather than attempted when Azure has
        already failed. Running them anyway would produce two more failures
        with the same root cause, and an operator reading three red lines has
        to work out which one to fix; one red line and two skips says it.

        Git is attempted regardless. It depends on the user's git credentials,
        not on Azure, and holding it back would hide a second problem behind
        the first.
        """
        azure_result = self.validate_azure()
        results: List[ConnectionValidation] = [azure_result]

        azure_blocked = not azure_result.ok and self.settings.azure is not None
        reason = (
            f"azure validation did not succeed "
            f"({azure_result.category.value if azure_result.category else 'skipped'}), "
            f"so this check was not attempted"
        )

        if azure_blocked:
            results.append(skipped(SourceType.SYNAPSE, reason))
            results.append(skipped(SourceType.SQL, reason))
        else:
            results.append(self.validate_synapse())
            synapse_result = results[-1]
            if synapse_result.ok or self.settings.synapse is None:
                results.append(self.validate_sql())
            else:
                # A workspace that cannot be read, or a paused pool, means the
                # SQL failure is already explained. Attempting it would add a
                # timeout to the operator's wait for no new information.
                results.append(
                    skipped(
                        SourceType.SQL,
                        "synapse validation did not succeed, so the sql "
                        "connection was not attempted",
                    )
                )

        results.append(self.validate_git(probe_remote=probe_remote))
        return ValidationReport(results)

    # -- shared failure handling -------------------------------------------

    @staticmethod
    def _guarded(connection: SourceType, check) -> ConnectionValidation:
        """Run one check, turning an expected failure into a result.

        A validation method must return rather than raise: a report with one
        red line is useful, and a traceback halfway through validating four
        connections is not. Only the agent's own expected errors are caught --
        an unexpected exception is a bug and still surfaces as one.
        """
        try:
            return check()
        except ConfigError as exc:
            return failed(connection, str(exc), ErrorCategory.CONFIGURATION)
        except (ConnectionValidationError, DiscoveryError) as exc:
            return failed(connection, str(exc), ErrorCategory.UNKNOWN)
