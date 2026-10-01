"""The Synapse Artifacts operations discovery is allowed to perform.

The data-plane sibling of ``sql.queries``: callers name an operation, they
cannot compose one. A ``SynapseArtifactSource`` accepts an ``ArtifactEndpoint``
drawn from this registry and rejects anything else, so "discovery is
read-only" is a property of the type system rather than a rule someone has to
remember. There is no verb here but GET and no way to supply one.

Endpoints are the documented Synapse Artifacts data-plane routes, pinned to
the ``2020-12-01`` API version for the same reason the ARM versions are
pinned: a service-side default moving must not change what discovery reads.

Capitalisation is the service's, not ours. ``/linkedservices`` is lowercase
and ``/sqlScripts`` and ``/sparkJobDefinitions`` are camel case; that is what
the REST reference documents, and normalising them here would be inventing an
endpoint.

Only the six P0 artifact types that have a Synapse representation are
registered. Data flows, triggers, KQL scripts, integration runtimes and
credentials all have data-plane routes, and none of them is in P0 -- adding
them would widen the discovery contract rather than complete it. Dedicated
SQL tables, views and stored procedures have no Artifacts route at all: their
definitions live in the database, which is why ``sql`` discovers them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

from discovery_agent.models import AssetType
from discovery_agent.source_strategy import P0Artifact

#: The Synapse client API version every route is requested with. Pinned.
ARTIFACTS_API_VERSION = "2020-12-01"

#: How many pages of one listing to follow before giving up. A server that
#: returns a nextLink pointing at itself would otherwise loop forever, and a
#: discovery run that never terminates is worse than one that reports a gap.
MAX_PAGES = 200


@dataclass(frozen=True)
class ArtifactEndpoint:
    """One named, versioned, read-only Artifacts listing route.

    ``path`` is relative to the workspace development endpoint and carries the
    service's own capitalisation. ``source_format`` matches what the
    repository detector records for the same artifact type, so a record
    assembled from Git and one assembled from the API describe their content
    the same way.
    """

    artifact: P0Artifact
    path: str
    source_format: str

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith("/") or "?" in self.path:
            raise ValueError(
                f"{self.artifact.value}: an artifact path is a bare relative "
                f"segment, got {self.path!r}"
            )
        if self.asset_type is None:
            raise ValueError(
                f"{self.artifact.value} has no repository asset type, so it "
                f"cannot be an artifact endpoint"
            )

    @property
    def asset_type(self) -> Optional[AssetType]:
        return self.artifact.asset_type

    @property
    def name(self) -> str:
        return self.path

    def url(self, endpoint: str) -> str:
        """The absolute listing URL for one workspace development endpoint."""
        return f"{endpoint.rstrip('/')}/{self.path}?api-version={ARTIFACTS_API_VERSION}"

    def resource_path(self, artifact_name: str) -> str:
        """The route one named artifact was read from, for provenance."""
        return f"{self.path}/{artifact_name}"

    def to_dict(self) -> dict:
        return {
            "artifact": self.artifact.value,
            "asset_type": self.asset_type.value if self.asset_type else None,
            "path": self.path,
            "api_version": ARTIFACTS_API_VERSION,
            "source_format": self.source_format,
        }


# --- the endpoints -----------------------------------------------------------
#
# Every route below is GET {endpoint}/<path>?api-version=2020-12-01 and
# returns {"value": [...], "nextLink": "..."}, where each element is
# {"id", "name", "type", "properties", "etag"} -- the same name+properties
# shape the Git-integrated repository stores, which is why the existing
# extractors read both without knowing which one they were given.

PIPELINES = ArtifactEndpoint(
    P0Artifact.PIPELINE, "pipelines", "synapse_pipeline_json"
)
DATASETS = ArtifactEndpoint(
    P0Artifact.DATASET, "datasets", "synapse_dataset_json"
)
LINKED_SERVICES = ArtifactEndpoint(
    # Lowercase, as documented. Not a typo and not ours to normalise.
    P0Artifact.LINKED_SERVICE, "linkedservices", "synapse_linked_service_json"
)
NOTEBOOKS = ArtifactEndpoint(
    P0Artifact.NOTEBOOK, "notebooks", "synapse_notebook_json"
)
SQL_SCRIPTS = ArtifactEndpoint(
    P0Artifact.SQL_SCRIPT, "sqlScripts", "synapse_sql_script_json"
)
SPARK_JOB_DEFINITIONS = ArtifactEndpoint(
    P0Artifact.SPARK_JOB_DEFINITION,
    "sparkJobDefinitions",
    "synapse_spark_job_definition_json",
)


#: Every listing discovery may perform, in a fixed order so two runs against
#: the same workspace produce results in the same sequence.
ARTIFACT_ENDPOINTS: Tuple[ArtifactEndpoint, ...] = (
    PIPELINES,
    DATASETS,
    LINKED_SERVICES,
    NOTEBOOKS,
    SQL_SCRIPTS,
    SPARK_JOB_DEFINITIONS,
)

_BY_ARTIFACT: Mapping[P0Artifact, ArtifactEndpoint] = {
    endpoint.artifact: endpoint for endpoint in ARTIFACT_ENDPOINTS
}

#: P0 artifacts the Artifacts API does not serve, and what does serve them.
#: Recorded rather than left implicit: "no endpoint" and "endpoint we have not
#: written yet" are different claims, and only one of them is true here.
NO_ARTIFACTS_API: Mapping[P0Artifact, str] = {
    P0Artifact.DEDICATED_SQL_TABLE: (
        "a physical table exists only inside the dedicated pool; the "
        "authoritative source is the SQL catalog"
    ),
    P0Artifact.SQL_VIEW: (
        "a view's definition lives in sys.sql_modules; the authoritative "
        "source is the SQL catalog"
    ),
    P0Artifact.STORED_PROCEDURE: (
        "a procedure's definition lives in sys.sql_modules; the "
        "authoritative source is the SQL catalog"
    ),
}


#: The workspace's own data-plane resource (``Workspace - Get``). Not an
#: artifact listing and deliberately not in the registry: it is read only to
#: prove the data plane is reachable and that this identity may read it,
#: which is a cheaper and more specific probe than listing pipelines.
WORKSPACE_PATH = "workspace"


#: Triggers are listed for the source inventory only. They are not a P0
#: artifact (no extractor, no record), so they are deliberately not in
#: ``ARTIFACT_ENDPOINTS``; this is the one extra, fixed, read-only route.
TRIGGERS_PATH = "triggers"


def triggers_url(endpoint: str) -> str:
    """The data-plane URL that lists the workspace's triggers."""
    return f"{endpoint.rstrip('/')}/{TRIGGERS_PATH}?api-version={ARTIFACTS_API_VERSION}"


def workspace_url(endpoint: str) -> str:
    """The data-plane URL of the workspace itself, for a reachability probe."""
    return (
        f"{endpoint.rstrip('/')}/{WORKSPACE_PATH}"
        f"?api-version={ARTIFACTS_API_VERSION}"
    )


def endpoint_for(artifact: P0Artifact) -> ArtifactEndpoint:
    """The registered endpoint for one P0 artifact, or a clear failure."""
    try:
        return _BY_ARTIFACT[artifact]
    except KeyError:
        reason = NO_ARTIFACTS_API.get(artifact)
        if reason:
            raise KeyError(
                f"{artifact.value} has no Synapse Artifacts endpoint: {reason}"
            ) from None
        known = ", ".join(sorted(e.artifact.value for e in ARTIFACT_ENDPOINTS))
        raise KeyError(
            f"no artifact endpoint for {artifact.value!r}; known: {known}"
        ) from None


def is_registered(endpoint: ArtifactEndpoint) -> bool:
    """Whether this is *the* registered endpoint, not merely one like it.

    Identity rather than equality, for the same reason ``sql.queries`` checks
    identity: an equal-looking endpoint built by a caller is still a
    caller-supplied route, and the point of the registry is that
    caller-supplied routes are never requested.
    """
    return _BY_ARTIFACT.get(endpoint.artifact) is endpoint


def registry_summary() -> Dict[str, dict]:
    """What discovery is able to ask the data plane, for a manifest."""
    return {e.path: e.to_dict() for e in ARTIFACT_ENDPOINTS}
