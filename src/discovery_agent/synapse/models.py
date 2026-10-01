"""Typed model of what the Synapse Artifacts API says about a workspace.

Deliberately separate from ``extractors.*_models``. Those describe what is
*inside* an artifact -- a pipeline's activities, a notebook's cells -- and are
produced by extractors. This one describes what the workspace *reports*: that
an artifact exists, under which name, at which route, with which etag. The
two are different kinds of knowledge and must not share a type.

Nothing here parses an artifact. ``payload`` is the service's own resource
object, kept whole and untouched, and the extractors read it through
``SynapseArtifactSource``. That is the rule that keeps pipeline, notebook,
dataset and linked-service parsing in one place instead of two.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from discovery_agent.artifacts.models import (
    ArtifactCategory,
    DetectedArtifact,
    DetectionEvidence,
    DiscoverySource,
    Signal,
    SignalType,
)
from discovery_agent.extractors.models import (
    ExtractionIssue,
    ExtractionProvenance,
    SourceType,
)
from discovery_agent.models import AssetType
from discovery_agent.source_strategy import P0Artifact
from discovery_agent.synapse.api import ArtifactEndpoint

#: Recorded on every provenance this source produces, so a record can say the
#: definition came from the live workspace rather than from a file.
SYNAPSE_SOURCE_FORMAT = "synapse_artifact"

#: The service told us the type by answering on a typed route. That is
#: stronger evidence than any repository heuristic, which has to infer the
#: type from a folder name and a JSON shape.
API_CONFIDENCE = 1.0


def canonical_json(value: Any) -> str:
    """A stable rendering of a JSON value.

    Sorted keys and fixed separators, so object key order and whitespace --
    which carry no meaning in JSON -- cannot make two identical artifacts
    look different. Array order is preserved, because it does carry meaning.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(properties: Any) -> str:
    """The sha256 of one artifact's definition, canonically rendered."""
    return hashlib.sha256(canonical_json(properties).encode("utf-8")).hexdigest()


#: Keys the service writes into an artifact that are not part of it. Stripped
#: before any cross-source comparison, for the same reason ``etag`` is: a
#: publish timestamp says when the workspace last saved the artifact, not what
#: the artifact is, and Git has no equivalent to compare it against.
SERVICE_MANAGED_KEYS = frozenset({"lastPublishTime"})


def normalize_definition(value: Any) -> Any:
    """A definition reduced to what it actually declares.

    Needed because the Artifacts API and the Git-integrated repository do not
    write the same JSON for the same artifact. The API materialises defaults
    that Git omits -- ``"parameters": {}``, ``"outputs": []``,
    ``"policy": {"elapsedTimeMetric": {}}``, ``"metadata": null`` -- and
    comparing raw documents reports every artifact in the workspace as
    drifted. That is not a finding; it is the two representations of the same
    artifact.

    Three rules, applied symmetrically to both sides:

    * service-managed keys are dropped;
    * a key whose value is null is dropped, because an omitted key and an
      explicit null declare the same thing in this JSON;
    * a key whose value is an empty object or array *after normalising* is
      dropped, for the same reason.

    What is deliberately *not* done: list items are never dropped or
    reordered. Order carries meaning -- a pipeline's activities run in it --
    and an empty element is still an element. So an artifact whose activity
    list was emptied still reports as drifted, which is the case that matters.

    This is canonicalisation, not a semantic diff. It knows nothing about what
    an activity or a cell means, and any difference in a value that was
    actually declared survives it.
    """
    if isinstance(value, Mapping):
        reduced: Dict[str, Any] = {}
        for key, item in value.items():
            if key in SERVICE_MANAGED_KEYS:
                continue
            normalized = normalize_definition(item)
            if normalized is None:
                continue
            if isinstance(normalized, (dict, list)) and not normalized:
                continue
            reduced[key] = normalized
        return reduced
    if isinstance(value, (list, tuple)):
        return [normalize_definition(item) for item in value]
    return value


def comparison_hash(properties: Any) -> str:
    """The hash two sources are compared on, after normalisation.

    Deliberately not the same as ``content_hash``. That one identifies the
    bytes a source served and belongs in provenance; this one answers whether
    two sources declare the same artifact.
    """
    return content_hash(normalize_definition(properties))


def definition_of(payload: Mapping[str, Any]) -> Any:
    """The artifact's own definition: the resource's ``properties`` object.

    The service wraps every artifact as ``{id, name, type, properties,
    etag}``; only ``properties`` is the artifact. Comparing whole resources
    across sources would report drift on an etag.
    """
    properties = payload.get("properties")
    return properties if properties is not None else {}


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class SynapseArtifact:
    """One artifact as the live workspace reports it.

    ``payload`` is the service's resource object, unmodified and served to
    extractors as-is. It is exposed through a read-only mapping so nothing
    downstream can edit what a later comparison will be made against.
    """

    artifact: P0Artifact
    name: str
    endpoint_path: str  # the route it was listed from, e.g. "pipelines"
    source_format: str
    payload: Mapping[str, Any]
    resource_id: Optional[str] = None
    etag: Optional[str] = None
    folder: Optional[str] = None
    description: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a synapse artifact needs a name")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def asset_type(self) -> Optional[AssetType]:
        return self.artifact.asset_type

    @property
    def route(self) -> str:
        """Where this artifact was read from, relative to the endpoint."""
        return f"{self.endpoint_path}/{self.name}"

    @property
    def definition(self) -> Any:
        """The artifact's own ``properties`` object."""
        return definition_of(self.payload)

    @property
    def definition_hash(self) -> str:
        """A stable hash of the bytes this source served, for provenance.

        Not the hash a cross-source comparison uses: that one is
        ``comparison_hash``, which normalises away the defaults the service
        materialises. Identifying what was served and deciding whether two
        sources agree are different questions.
        """
        return content_hash(self.definition)

    def as_json(self) -> str:
        """The resource as the extractors receive it.

        The whole resource rather than only ``properties``: every extractor
        reads ``document["name"]`` and ``document["properties"]``, which is
        the shape both the API and the Git-integrated repository use. Serving
        a different shape here would mean the Synapse path exercised
        different extractor code than the repository path.
        """
        return canonical_json(dict(self.payload))

    def detected(self) -> DetectedArtifact:
        """This artifact, as the extraction framework expects to receive one.

        Category is SYNAPSE and confidence is 1.0 because the service
        classified it, not us: it was returned by a typed route. The evidence
        records that route rather than a folder name, so a reviewer can see
        the classification came from the API.
        """
        asset_type = self.asset_type
        if asset_type is None:  # pragma: no cover - the registry forbids it
            raise ValueError(
                f"{self.artifact.value} has no repository asset type and "
                f"cannot be extracted"
            )
        return DetectedArtifact(
            artifact_type=asset_type.value,
            artifact_name=self.name,
            source_path=self.route,
            source_format=self.source_format,
            confidence=API_CONFIDENCE,
            discovery_source=DiscoverySource.PATH,
            category=ArtifactCategory.SYNAPSE,
            evidence=DetectionEvidence(
                signals=(
                    Signal(SignalType.PATH, self.endpoint_path),
                    Signal(SignalType.JSON_STRUCTURE, "properties"),
                )
            ),
            sha256=self.definition_hash,
        )

    def provenance(self, workspace: str) -> ExtractionProvenance:
        """Where this artifact came from, precisely enough to fetch it again."""
        return ExtractionProvenance(
            source_type=SourceType.SYNAPSE,
            source_format=self.source_format,
            source_path=self.route,
            sha256=self.definition_hash,
            resource_id=self.resource_id or f"{workspace}/{self.route}",
        )

    def summary(self) -> dict:
        """Identity and shape only; never the definition, never a secret."""
        return {
            "artifact": self.artifact.value,
            "name": self.name,
            "route": self.route,
            "resource_id": self.resource_id,
            "etag": self.etag,
            "folder": self.folder,
            "definition_sha256": self.definition_hash,
        }


def artifact_from_resource(
    endpoint: ArtifactEndpoint, resource: Mapping[str, Any]
) -> SynapseArtifact:
    """Map one listed resource onto the artifact model. Pure.

    Raises when the resource has no name: an artifact discovery cannot name
    is not one it can report, and inventing a placeholder would put a
    fictional pipeline into the inventory. The caller counts the refusal.
    """
    name = _text(resource.get("name"))
    if not name:
        raise ValueError(
            f"a {endpoint.path} resource arrived with no name and cannot be "
            f"identified"
        )

    properties = resource.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    folder = properties.get("folder")
    folder_name = _text(folder.get("name")) if isinstance(folder, Mapping) else None

    return SynapseArtifact(
        artifact=endpoint.artifact,
        name=name,
        endpoint_path=endpoint.path,
        source_format=endpoint.source_format,
        payload=dict(resource),
        resource_id=_text(resource.get("id")),
        etag=_text(resource.get("etag")),
        folder=folder_name,
        description=_text(properties.get("description")),
    )


@dataclass(frozen=True)
class EndpointOutcome:
    """What happened when one artifact type was listed.

    Kept per endpoint rather than pooled, because "there are no notebooks"
    and "notebooks could not be listed" are different findings and an
    operator must never see the second reported as the first.
    """

    artifact: P0Artifact
    path: str
    reachable: bool
    count: int = 0
    pages: int = 1
    issue: Optional[ExtractionIssue] = None

    def to_dict(self) -> dict:
        return {
            "artifact": self.artifact.value,
            "path": self.path,
            "reachable": self.reachable,
            "count": self.count,
            "pages": self.pages,
            "issue": self.issue.to_dict() if self.issue else None,
        }


@dataclass(frozen=True)
class SynapseWorkspaceDiscovery:
    """Every artifact discovered in one live workspace.

    The data-plane counterpart of ``SqlCatalogDiscovery``, and shaped the same
    way: what was found, together with what could not be established, so a
    caller never has to infer completeness from a count.
    """

    workspace: str
    endpoint: str
    provenance: ExtractionProvenance
    artifacts: Tuple[SynapseArtifact, ...] = ()
    outcomes: Tuple[EndpointOutcome, ...] = ()
    issues: Tuple[ExtractionIssue, ...] = ()

    @property
    def artifact_count(self) -> int:
        return len(self.artifacts)

    def of(self, artifact: P0Artifact) -> Tuple[SynapseArtifact, ...]:
        return tuple(a for a in self.artifacts if a.artifact is artifact)

    def named(self, artifact: P0Artifact, name: str) -> Optional[SynapseArtifact]:
        return next(
            (a for a in self.artifacts if a.artifact is artifact and a.name == name),
            None,
        )

    def outcome_for(self, artifact: P0Artifact) -> Optional[EndpointOutcome]:
        return next((o for o in self.outcomes if o.artifact is artifact), None)

    def was_reachable(self, artifact: P0Artifact) -> bool:
        """Whether this artifact type was actually listed.

        False means the count is not a finding. A caller reporting "0
        notebooks" must check this first, because an unreachable endpoint and
        an empty workspace produce the same number.
        """
        outcome = self.outcome_for(artifact)
        return bool(outcome and outcome.reachable)

    @property
    def unreachable(self) -> Tuple[P0Artifact, ...]:
        return tuple(o.artifact for o in self.outcomes if not o.reachable)

    @property
    def all_issues(self) -> Tuple[ExtractionIssue, ...]:
        """Run-level issues followed by each endpoint's own, in endpoint order."""
        return self.issues + tuple(
            o.issue for o in self.outcomes if o.issue is not None
        )

    @property
    def is_complete(self) -> bool:
        """Whether every registered endpoint answered and nothing was dropped."""
        return not self.all_issues

    def counts_by_artifact(self) -> Dict[str, int]:
        """Counts for the endpoints that answered. Unreachable ones are absent.

        Deliberately not zero-filled for an unreachable endpoint: a zero here
        means the workspace has none, and an artifact type that could not be
        listed must not be able to produce that number.
        """
        counts = {o.artifact.value: o.count for o in self.outcomes if o.reachable}
        return dict(sorted(counts.items()))

    def summary(self) -> dict:
        """A compact overview. Carries no definition content and no secrets."""
        return {
            "workspace": self.workspace,
            "endpoint": self.endpoint,
            "artifact_count": self.artifact_count,
            "by_artifact": self.counts_by_artifact(),
            "unreachable": [a.value for a in self.unreachable],
            "issue_count": len(self.all_issues),
            "complete": self.is_complete,
        }

    def to_dict(self) -> dict:
        return {
            "workspace": self.workspace,
            "endpoint": self.endpoint,
            "provenance": self.provenance.to_dict(),
            "artifacts": [a.summary() for a in self.artifacts],
            "outcomes": [o.to_dict() for o in self.outcomes],
            "issues": [i.to_dict() for i in self.issues],
        }
