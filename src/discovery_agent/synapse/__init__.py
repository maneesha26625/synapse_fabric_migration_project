"""Live discovery of the workspace, straight from the Synapse Artifacts API.

Six of the nine P0 artifacts -- pipelines, datasets, linked services,
notebooks, SQL scripts and Spark job definitions -- exist both as files in a
Git-integrated repository and as objects in a running workspace. Git holds
what was committed; the workspace holds what is published and actually runs.
They can differ, so discovery reads both and keeps both observations. See
``discovery_agent.source_strategy``.

This package is deliberately parallel to ``discovery_agent.sql``: a source
contract, a fixed registry of the operations discovery may perform, a
transport seam, and pure mapping. It differs from ``sql`` in one way that
matters -- a Synapse artifact *is* a JSON document with a name and a
``properties`` object, exactly as the repository stores it, so the existing
extractors read it unchanged through ``ArtifactSource``. A catalog object has
no such document, which is why SQL needs its own ``CatalogSource``.

Modules:
  api     the fixed, versioned, GET-only artifact routes. All of them
  client  the ArtifactsClient seam, and the real HTTP one
  models  typed live artifacts and the workspace discovery result
  source  SynapseArtifactSource: an ArtifactSource over the data plane

Authentication is not here. The client is handed a token provider by
``discovery_agent.connections``, which is the one place that holds an Azure
identity.
"""

from discovery_agent.synapse.api import (
    ARTIFACT_ENDPOINTS,
    ARTIFACTS_API_VERSION,
    NO_ARTIFACTS_API,
    ArtifactEndpoint,
    endpoint_for,
    is_registered,
    registry_summary,
)
from discovery_agent.synapse.client import (
    ArtifactsClient,
    ArtifactsResponse,
    HttpArtifactsClient,
    TokenProvider,
)
from discovery_agent.synapse.models import (
    SYNAPSE_SOURCE_FORMAT,
    EndpointOutcome,
    SynapseArtifact,
    SynapseWorkspaceDiscovery,
    artifact_from_resource,
    canonical_json,
    comparison_hash,
    content_hash,
    normalize_definition,
)
from discovery_agent.synapse.source import (
    SynapseArtifactSource,
    p0_artifacts_served,
    unavailable_discovery,
)

__all__ = [
    "ARTIFACTS_API_VERSION",
    "ARTIFACT_ENDPOINTS",
    "NO_ARTIFACTS_API",
    "SYNAPSE_SOURCE_FORMAT",
    "ArtifactEndpoint",
    "ArtifactsClient",
    "ArtifactsResponse",
    "EndpointOutcome",
    "HttpArtifactsClient",
    "SynapseArtifact",
    "SynapseArtifactSource",
    "SynapseWorkspaceDiscovery",
    "TokenProvider",
    "artifact_from_resource",
    "canonical_json",
    "comparison_hash",
    "content_hash",
    "normalize_definition",
    "endpoint_for",
    "is_registered",
    "p0_artifacts_served",
    "registry_summary",
    "unavailable_discovery",
]
