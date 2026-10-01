"""Authentication and connectivity for every source the agent reads.

One package owns every credential the application has. Discovery, extraction
and acquisition receive an open connection or a ready source; none of them
authenticates, and none of them may import a credential library. The
dependency runs one way:

    connections  ->  sources / acquisition / discovery  ->  extractors

Five connections exist today:

    AzureConnection             the subscription, and the identity for it
    SynapseWorkspaceConnection  the workspace, over the management plane
    SynapseArtifactsConnection  the workspace's artifacts, over the data plane
    SqlConnection               the dedicated pool, over TDS with an Entra token
    GitConnection               the source repository, over the user's own git

The two Synapse connections are separate because their authorization is:
Reader on the resource group satisfies the management plane and grants
nothing on the data plane, so a workspace can be readable in ARM while every
pipeline listing returns 403. An operator needs to be told which of the two
is missing.

``ConnectionManager`` holds all five and validates them, returning structured
results rather than printing. ``python -m discovery_agent.connections`` is the
developer command that renders those results.

Fabric is not here. It is a migration target, no abstraction for it exists in
this codebase yet, and an empty placeholder would be a guess about a shape
that has not been designed.

Modules:
  models              what the application is pointed at. Cannot hold a secret
  validation          what a check found, as a value. Redacts on construction
  azure               the credential, the tokens, the ARM transport
  synapse             the workspace, its metadata and its dedicated pool
  synapse_artifacts   the Artifacts data plane, composed from the synapse package
  sql                 the SQL endpoint, composed from the existing sql package
  git                 the repository, wrapping the existing acquisition
  manager             all five, and validate_all()
"""

from discovery_agent.connections.azure import (
    ARM_SCOPE,
    SQL_SCOPE,
    SYNAPSE_SCOPE,
    AccessToken,
    ArmResponse,
    ArmTransport,
    AzureCliCredentialProvider,
    AzureConnection,
    AzureCredentialProvider,
    InteractiveBrowserCredentialProvider,
    RequestsArmTransport,
    auth_record_path,
    credential_provider_for,
    forget_auth_record,
    reset_credentials,
)
from discovery_agent.connections.git import GitConnection, connection_from_url
from discovery_agent.connections.manager import ConnectionManager
from discovery_agent.connections.models import (
    AzureConnectionConfig,
    ConnectionSettings,
    CredentialMethod,
    GitRepositoryConfig,
    SynapseConnectionConfig,
    settings_from_environment,
)
from discovery_agent.connections.sql import SqlConnection
from discovery_agent.connections.synapse_artifacts import SynapseArtifactsConnection
from discovery_agent.connections.synapse import (
    SqlPoolMetadata,
    SynapseWorkspaceConnection,
    SynapseWorkspaceMetadata,
)
from discovery_agent.connections.validation import (
    ConnectionValidation,
    ErrorCategory,
    ValidationReport,
    ValidationStatus,
    redact,
)

__all__ = [
    "ARM_SCOPE",
    "AccessToken",
    "ArmResponse",
    "ArmTransport",
    "AzureCliCredentialProvider",
    "AzureConnection",
    "AzureConnectionConfig",
    "AzureCredentialProvider",
    "ConnectionManager",
    "ConnectionSettings",
    "ConnectionValidation",
    "CredentialMethod",
    "ErrorCategory",
    "GitConnection",
    "GitRepositoryConfig",
    "InteractiveBrowserCredentialProvider",
    "RequestsArmTransport",
    "SQL_SCOPE",
    "SYNAPSE_SCOPE",
    "SqlConnection",
    "SqlPoolMetadata",
    "SynapseConnectionConfig",
    "SynapseWorkspaceConnection",
    "SynapseWorkspaceMetadata",
    "ValidationReport",
    "ValidationStatus",
    "auth_record_path",
    "connection_from_url",
    "credential_provider_for",
    "forget_auth_record",
    "redact",
    "reset_credentials",
    "settings_from_environment",
]
