"""SynapseArtifactSource: live artifact definitions from a workspace.

An ``ArtifactSource``, exactly like ``RepositoryArtifactSource``, and that is
the whole point: an extractor asks its source for content and provenance and
never learns whether the bytes came from a clone or from the data plane. The
pipeline, notebook, dataset, linked-service, SQL-script and Spark job
definition extractors run against this source unchanged.

**No artifact-specific parsing lives here.** This module lists routes, maps
resources onto identity, and serves JSON. It does not know what an activity
is, what a cell is, or what a linked service's type properties mean. Adding
any of that would put a second, divergent parser next to the extractors.

Degradation is per artifact type. One endpoint that a workspace does not
serve, or that this identity may not read, costs that artifact type and
nothing else -- the remaining listings still run, and the endpoint that failed
is recorded as unreachable rather than as empty. "There are no notebooks" and
"notebooks could not be listed" are different findings and never collapse.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.errors import MalformedArtifactError, SynapseConnectionError
from discovery_agent.extractors.models import (
    ExtractionIssue,
    ExtractionProvenance,
    IssueCode,
    SourceType,
)
from discovery_agent.extractors.sources import ArtifactSource
from discovery_agent.source_strategy import P0Artifact
from discovery_agent.synapse.api import (
    ARTIFACT_ENDPOINTS,
    ARTIFACTS_API_VERSION,
    MAX_PAGES,
    ArtifactEndpoint,
    is_registered,
)
from discovery_agent.synapse.client import ArtifactsClient, ArtifactsResponse
from discovery_agent.synapse.models import (
    SYNAPSE_SOURCE_FORMAT,
    EndpointOutcome,
    SynapseArtifact,
    SynapseWorkspaceDiscovery,
    artifact_from_resource,
)

SOURCE_NAME = "synapse_artifacts"
SOURCE_VERSION = "1.0.0"

#: Status codes that mean "this identity may not read this", as distinct from
#: "this does not exist". Reporting the first as the second would tell an
#: operator their workspace is empty when it is their role assignment that is.
_FORBIDDEN_STATUS = (401, 403)
#: Codes that mean the route itself is not served here.
_UNSUPPORTED_STATUS = (400, 404, 405, 501)


class SynapseArtifactSource(ArtifactSource):
    """Reads live artifact definitions from one Synapse workspace.

    One instance per workspace. The client is injected, so every behaviour in
    this class -- paging, an unreachable endpoint, a malformed resource, a
    permission failure -- is testable with no network, no subscription and no
    sign-in.

    Listings are cached for the life of the source because the list routes
    return complete definitions: Synapse answers ``GET /pipelines`` with every
    pipeline's full ``properties``, so re-reading one artifact is a dictionary
    lookup rather than a second request. ``read_text`` falls back to the
    documented single-artifact route only for an artifact that was never
    listed.
    """

    source_type = SourceType.SYNAPSE

    def __init__(
        self,
        client: ArtifactsClient,
        workspace: str,
        endpoints: Sequence[ArtifactEndpoint] = ARTIFACT_ENDPOINTS,
    ) -> None:
        self.client = client
        self.workspace = workspace
        self.endpoints = tuple(endpoints)
        #: route ("pipelines/Name") -> artifact, filled by listing.
        self._by_route: Dict[str, SynapseArtifact] = {}

    def describe(self) -> str:
        """A safe one-line description. Contains no credential."""
        return f"workspace {self.workspace} at {self.client.endpoint}"

    # -- the guard ---------------------------------------------------------

    @staticmethod
    def check_endpoint(endpoint: ArtifactEndpoint) -> None:
        """Refuse anything that is not a registered endpoint.

        Identity, not equality: an ``ArtifactEndpoint`` a caller constructed
        is a caller-supplied route however closely it resembles a registered
        one, and caller-supplied routes are exactly what this design exists to
        prevent. The same rule ``CatalogSource.check_query`` applies to SQL.
        """
        if not isinstance(endpoint, ArtifactEndpoint):
            raise SynapseConnectionError(
                f"artifact listings take an ArtifactEndpoint from the "
                f"registry, got {type(endpoint).__name__}"
            )
        if not is_registered(endpoint):
            raise SynapseConnectionError(
                f"{endpoint.path!r} is not a registered artifact endpoint; "
                f"discovery reads only the fixed routes in "
                f"discovery_agent.synapse.api"
            )

    # -- listing -----------------------------------------------------------

    def list_endpoint(
        self, endpoint: ArtifactEndpoint
    ) -> Tuple[Tuple[SynapseArtifact, ...], EndpointOutcome]:
        """Every artifact on one route, following the service's paging.

        Returns what was read together with the outcome, so a caller can tell
        a complete empty listing from one that stopped early. Never raises for
        a service-side failure: the outcome carries it. A programming error --
        an unregistered endpoint -- still raises, because that is a bug.
        """
        self.check_endpoint(endpoint)

        artifacts: List[SynapseArtifact] = []
        url: Optional[str] = endpoint.url(self.client.endpoint)
        pages = 0
        seen_urls = {url}

        while url is not None:
            pages += 1
            if pages > MAX_PAGES:
                return tuple(artifacts), EndpointOutcome(
                    endpoint.artifact,
                    endpoint.path,
                    reachable=False,
                    count=len(artifacts),
                    pages=pages - 1,
                    issue=ExtractionIssue(
                        IssueCode.SOURCE_UNAVAILABLE,
                        f"{endpoint.path} paged past {MAX_PAGES} pages and was "
                        f"abandoned; the listing is incomplete and its count "
                        f"is not a finding",
                        endpoint.path,
                    ),
                )

            try:
                response = self.client.get(url)
            except SynapseConnectionError as exc:
                return tuple(artifacts), EndpointOutcome(
                    endpoint.artifact,
                    endpoint.path,
                    reachable=False,
                    count=len(artifacts),
                    pages=pages - 1,
                    issue=ExtractionIssue(
                        IssueCode.SOURCE_UNAVAILABLE,
                        f"{endpoint.path} could not be listed: {exc}",
                        endpoint.path,
                    ),
                )

            if not response.ok:
                return tuple(artifacts), EndpointOutcome(
                    endpoint.artifact,
                    endpoint.path,
                    reachable=False,
                    count=len(artifacts),
                    pages=pages - 1,
                    issue=self._failure_issue(endpoint, response),
                )

            page, malformed = self._resources(endpoint, response.payload)
            artifacts.extend(page)
            if malformed is not None:
                # A page whose resources could not be identified is a gap in
                # this listing, not a reason to drop the ones that could.
                return tuple(artifacts), EndpointOutcome(
                    endpoint.artifact,
                    endpoint.path,
                    reachable=True,
                    count=len(artifacts),
                    pages=pages,
                    issue=malformed,
                )

            url = self._next_link(response.payload)
            if url is not None and url in seen_urls:
                return tuple(artifacts), EndpointOutcome(
                    endpoint.artifact,
                    endpoint.path,
                    reachable=False,
                    count=len(artifacts),
                    pages=pages,
                    issue=ExtractionIssue(
                        IssueCode.SOURCE_UNAVAILABLE,
                        f"{endpoint.path} returned a nextLink it had already "
                        f"served; paging was stopped and the listing is "
                        f"incomplete",
                        endpoint.path,
                    ),
                )
            if url is not None:
                seen_urls.add(url)

        for artifact in artifacts:
            self._by_route[artifact.route] = artifact

        return tuple(artifacts), EndpointOutcome(
            endpoint.artifact,
            endpoint.path,
            reachable=True,
            count=len(artifacts),
            pages=pages,
        )

    def discover(self) -> SynapseWorkspaceDiscovery:
        """List every registered endpoint. The composed live discovery.

        Endpoints are read in registry order and each one degrades on its
        own: a workspace where SQL scripts may not be read still reports its
        pipelines, and the SQL script endpoint is recorded as unreachable
        rather than as holding none.
        """
        artifacts: List[SynapseArtifact] = []
        outcomes: List[EndpointOutcome] = []

        for endpoint in self.endpoints:
            found, outcome = self.list_endpoint(endpoint)
            artifacts.extend(found)
            outcomes.append(outcome)

        return SynapseWorkspaceDiscovery(
            workspace=self.workspace,
            endpoint=self.client.endpoint,
            provenance=self.workspace_provenance(),
            artifacts=tuple(artifacts),
            outcomes=tuple(outcomes),
        )

    def artifacts(self) -> Tuple[SynapseArtifact, ...]:
        """Everything listed so far, in route order."""
        return tuple(self._by_route[route] for route in sorted(self._by_route))

    def detected(self) -> Tuple[DetectedArtifact, ...]:
        """Listed artifacts as the extraction framework expects them."""
        return tuple(a.detected() for a in self.artifacts())

    # -- the ArtifactSource contract --------------------------------------

    def provenance_for(self, artifact: DetectedArtifact) -> ExtractionProvenance:
        """Where this artifact's content comes from.

        Built from what was listed when the artifact is known, so the
        provenance carries the service's own resource id and the definition
        hash. An artifact this source never listed still gets a truthful
        provenance naming the route it would be read from.
        """
        known = self._by_route.get(artifact.source_path)
        if known is not None:
            return known.provenance(self.workspace)
        return ExtractionProvenance(
            source_type=SourceType.SYNAPSE,
            source_format=artifact.source_format or SYNAPSE_SOURCE_FORMAT,
            source_path=artifact.source_path,
            sha256=artifact.sha256,
            resource_id=f"{self.workspace}/{artifact.source_path}",
        )

    def read_text(self, artifact: DetectedArtifact) -> str:
        """The artifact's resource object, as JSON text.

        Served from the listing when it is there, because the list routes
        already returned complete definitions. Anything else is fetched from
        the documented single-artifact route.
        """
        known = self._by_route.get(artifact.source_path)
        if known is not None:
            return known.as_json()
        return self._fetch_one(artifact).as_json()

    # -- internals ---------------------------------------------------------

    def _fetch_one(self, artifact: DetectedArtifact) -> SynapseArtifact:
        """Read one artifact from its own route.

        Only reached for an artifact that was not in a listing -- a caller
        that constructed the identity itself. The route is rebuilt from the
        registry rather than from the caller's string, so an arbitrary path
        cannot be requested through this method.
        """
        route = artifact.source_path or ""
        path, _, name = route.partition("/")
        endpoint = next((e for e in self.endpoints if e.path == path), None)
        if endpoint is None or not name:
            raise MalformedArtifactError(
                route, "not a route this source serves"
            )

        url = (
            f"{self.client.endpoint}/{endpoint.path}/{name}"
            f"?api-version={ARTIFACTS_API_VERSION}"
        )
        try:
            response = self.client.get(url)
        except SynapseConnectionError as exc:
            raise MalformedArtifactError(route, f"unreadable: {exc}") from exc
        if not response.ok:
            raise MalformedArtifactError(route, f"unreadable: {response.message()}")
        try:
            return artifact_from_resource(endpoint, response.payload)
        except ValueError as exc:
            raise MalformedArtifactError(route, str(exc)) from exc

    def _resources(
        self, endpoint: ArtifactEndpoint, payload: Mapping[str, Any]
    ) -> Tuple[Tuple[SynapseArtifact, ...], Optional[ExtractionIssue]]:
        """One page's resources, and the issue if the page was not one.

        A payload with no ``value`` array is a malformed response, not an
        empty workspace, and is reported as such -- otherwise a service
        returning an error body with a 200 would read as "no pipelines".
        """
        value = payload.get("value")
        if value is None:
            return (), ExtractionIssue(
                IssueCode.MALFORMED_ARTIFACT,
                f"{endpoint.path} answered without a value array, so the "
                f"listing could not be read; no claim is made about how many "
                f"{endpoint.path} exist",
                endpoint.path,
            )
        if not isinstance(value, list):
            return (), ExtractionIssue(
                IssueCode.MALFORMED_ARTIFACT,
                f"{endpoint.path} answered with a value that is not a list "
                f"({type(value).__name__})",
                endpoint.path,
            )

        artifacts: List[SynapseArtifact] = []
        unnamed = 0
        for resource in value:
            if not isinstance(resource, Mapping):
                unnamed += 1
                continue
            try:
                artifacts.append(artifact_from_resource(endpoint, resource))
            except ValueError:
                unnamed += 1

        issue = None
        if unnamed:
            issue = ExtractionIssue(
                IssueCode.MALFORMED_ARTIFACT,
                f"{unnamed} {endpoint.path} resource(s) could not be "
                f"identified and were not reported",
                endpoint.path,
            )
        return tuple(artifacts), issue

    @staticmethod
    def _next_link(payload: Mapping[str, Any]) -> Optional[str]:
        """The next page's absolute URL, or None when the listing is done.

        The service supplies a complete URL with its own api-version and
        continuation token; it is followed as given and never rebuilt.
        """
        link = payload.get("nextLink")
        if not isinstance(link, str):
            return None
        link = link.strip()
        return link or None

    @staticmethod
    def _failure_issue(
        endpoint: ArtifactEndpoint, response: ArtifactsResponse
    ) -> ExtractionIssue:
        """The issue one failed listing produces.

        The status code decides the code, because the remedies differ and an
        operator reading the result has to know which one they need. What the
        issue never does is imply a count: a listing that failed says nothing
        about how many artifacts exist.
        """
        status = response.status_code
        if status in _FORBIDDEN_STATUS:
            return ExtractionIssue(
                IssueCode.SOURCE_UNAVAILABLE,
                f"{endpoint.path} could not be listed: the connected identity "
                f"is not authorized to read them (HTTP {status}: "
                f"{response.message()}). No claim is made about whether any "
                f"exist; grant the Synapse Artifact User role and retry",
                endpoint.path,
            )
        if status in _UNSUPPORTED_STATUS:
            return ExtractionIssue(
                IssueCode.UNSUPPORTED_CONSTRUCT,
                f"{endpoint.path} is not served by this workspace at api-version "
                f"{ARTIFACTS_API_VERSION} (HTTP {status}: {response.message()}); "
                f"this artifact type was not discovered from the live workspace",
                endpoint.path,
            )
        return ExtractionIssue(
            IssueCode.SOURCE_UNAVAILABLE,
            f"{endpoint.path} could not be listed (HTTP {status}: "
            f"{response.message()}); no claim is made about how many exist",
            endpoint.path,
        )

    def workspace_provenance(self) -> ExtractionProvenance:
        """Where the workspace's artifacts came from, as a whole."""
        return ExtractionProvenance(
            source_type=SourceType.SYNAPSE,
            source_format=SYNAPSE_SOURCE_FORMAT,
            source_path=None,
            resource_id=self.client.endpoint,
        )


def unavailable_discovery(
    workspace: str, endpoint: str, reason: str
) -> SynapseWorkspaceDiscovery:
    """The result for a workspace that could not be reached at all.

    Every registered endpoint is marked unreachable with the same reason, so
    a caller sees six explicit gaps rather than an empty artifact list that
    reads as "this workspace has nothing in it".
    """
    issue = ExtractionIssue(IssueCode.SOURCE_UNAVAILABLE, reason)
    return SynapseWorkspaceDiscovery(
        workspace=workspace,
        endpoint=endpoint,
        provenance=ExtractionProvenance(
            source_type=SourceType.SYNAPSE,
            source_format=SYNAPSE_SOURCE_FORMAT,
            resource_id=endpoint,
        ),
        outcomes=tuple(
            EndpointOutcome(e.artifact, e.path, reachable=False, pages=0, issue=issue)
            for e in ARTIFACT_ENDPOINTS
        ),
        issues=(issue,),
    )


def p0_artifacts_served() -> Tuple[P0Artifact, ...]:
    """The P0 artifacts this source can discover. The rest come from SQL."""
    return tuple(e.artifact for e in ARTIFACT_ENDPOINTS)
