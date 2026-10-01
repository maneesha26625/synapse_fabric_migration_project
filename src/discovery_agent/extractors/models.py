"""The extraction contract: sources, provenance, references, and results.

The detector answers "what is this artifact?". An extractor answers "what is
inside it, and what does it reference?". Deciding how to migrate it belongs to
Assessment, later; nothing in this module encodes a migration opinion.

Two rules shape these models:

* **Nothing here assumes a file.** An artifact's bytes may come from a Git
  clone today and from a live Synapse or Azure API later. Everything that
  touches a concrete source sits behind ``ArtifactSource``.
* **A failure is never an empty success.** A malformed pipeline must not
  extract as ``activities = []``. ``ExtractionResult`` enforces that in
  ``__post_init__`` rather than trusting each extractor to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Optional, Tuple, TypeVar

from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.errors import ExtractionContractError
from discovery_agent.models import AssetType, Evidence, asset_id

# The artifact-specific model an extractor produces (PipelineDefinition,
# NotebookDefinition, ...). The framework never inspects it.
T = TypeVar("T")


class SourceType(str, Enum):
    """Where an artifact's content is read from.

    Only REPOSITORY is implemented. The others exist so a future extractor can
    declare ``supported_sources = (SourceType.SYNAPSE,)`` without the framework
    changing shape.

    SQL is the dedicated or serverless SQL endpoint, reached over TDS rather
    than HTTP. It is a separate source because some artifacts -- tables,
    views, stored procedures -- have no representation in Git or in the
    Synapse REST API at all; their definition lives only in the database.
    See ``discovery_agent.source_strategy``.
    """

    REPOSITORY = "repository"
    SYNAPSE = "synapse"
    SQL = "sql"
    AZURE = "azure"


class ExtractionStatus(str, Enum):
    """The outcome of one extraction attempt."""

    SUCCESS = "success"  # complete, nothing to report
    PARTIAL = "partial"  # usable content, but something was not understood
    FAILED = "failed"  # no usable content; errors explain why
    SKIPPED = "skipped"  # never attempted (no extractor, unsupported source)


class IssueCode(str, Enum):
    """The distinguishable ways discovery can fall short.

    The first five arise during extraction. The rest arise when assembling a
    unified discovery record from several sources, where a source may be
    unreachable or a definition may exist but be unreadable.
    """

    NO_EXTRACTOR = "no_extractor"
    UNSUPPORTED_SOURCE = "unsupported_source"
    MALFORMED_ARTIFACT = "malformed_artifact"
    EXTRACTION_FAILURE = "extraction_failure"
    UNSUPPORTED_CONSTRUCT = "unsupported_construct"
    SOURCE_UNAVAILABLE = "source_unavailable"  # the source was not reachable
    OPAQUE_DEFINITION = "opaque_definition"  # exists but cannot be read
    MISSING_INFORMATION = "missing_information"  # a facet nobody supplied
    DRIFT_DETECTED = "drift_detected"  # sources disagree


class ReferenceKind(str, Enum):
    """What kind of link an extractor observed.

    Deliberately about the *shape* of the link, not what it means for
    migration.
    """

    ARTIFACT = "artifact"  # a named Synapse artifact (dataset, pipeline, ...)
    COMPUTE = "compute"  # a Spark pool, SQL pool, integration runtime
    SECRET = "secret"  # a Key Vault secret or credential
    STORAGE_PATH = "storage_path"  # abfss://, wasbs://, a mount point
    SQL_OBJECT = "sql_object"  # a table, view, or stored procedure
    EXTERNAL_ENDPOINT = "external_endpoint"  # REST, JDBC, third-party service
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExtractionIssue:
    """One thing an extractor could not do, or did not fully understand.

    ``location`` points inside the artifact — a JSON key path, a notebook cell
    index, a line number — so a reviewer can find it without re-deriving it.
    """

    code: IssueCode
    message: str
    location: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "message": self.message,
            "location": self.location,
        }


@dataclass(frozen=True)
class ExtractorInfo:
    """Which extractor produced a result, and which version of it.

    Recorded so a re-run after an extractor changes can be told apart from a
    re-run after the source changed.
    """

    name: str
    version: str

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True)
class ExtractionProvenance:
    """Where extracted information came from.

    Repository fields are optional because a Synapse or Azure source will fill
    in ``resource_id`` instead. Nothing here re-derives acquisition state: the
    repository fields are copied from the existing ``RepositorySource``.
    """

    source_type: SourceType
    source_format: str
    source_path: Optional[str] = None  # repository-relative path, or API route
    sha256: Optional[str] = None
    repository_url: Optional[str] = None
    ref: Optional[str] = None
    commit_sha: Optional[str] = None
    resource_id: Optional[str] = None  # ARM id / workspace item id, for live sources

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type.value,
            "source_format": self.source_format,
            "source_path": self.source_path,
            "sha256": self.sha256,
            "repository_url": self.repository_url,
            "ref": self.ref,
            "commit_sha": self.commit_sha,
            "resource_id": self.resource_id,
        }


@dataclass(frozen=True)
class ArtifactReference:
    """A link an extractor observed from one artifact to something else.

    A reference is an *observation*, never a resolved edge. Two names matching
    does not make a reference valid — the target may not exist, may be
    ambiguous, or may be built at runtime from an expression. ``resolved``
    stays False until the future dependency-graph stage proves otherwise, and
    ``evidence`` records where the claim came from so that stage can check it.
    """

    source_artifact_id: str
    source_artifact_type: AssetType
    kind: ReferenceKind
    target_name: str
    location: str  # where inside the source artifact, e.g. "activities[0].inputs[0]"
    evidence: Evidence
    target_type: Optional[AssetType] = None  # None when the target is not a Synapse asset
    resolved: bool = False

    @property
    def target_id(self) -> Optional[str]:
        """The stable id of the target, when the target is a Synapse asset."""
        if self.target_type is None:
            return None
        return asset_id(self.target_type, self.target_name)

    def to_dict(self) -> dict:
        return {
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_type": self.source_artifact_type.value,
            "kind": self.kind.value,
            "target_type": self.target_type.value if self.target_type else None,
            "target_name": self.target_name,
            "target_id": self.target_id,
            "location": self.location,
            "resolved": self.resolved,
            "evidence": {
                "source_file": self.evidence.source_file,
                "line": self.evidence.line,
                "extractor": self.evidence.extractor,
            },
        }


@dataclass(frozen=True)
class ExtractionResult(Generic[T]):
    """What one extractor produced for one artifact.

    ``content`` is the artifact-specific model — ``ExtractionResult[PipelineDefinition]``
    for a pipeline extractor. The framework never looks inside it.
    """

    artifact_id: str
    # None only for an artifact the detector could not map to an AssetType;
    # every real extraction result carries a concrete type.
    artifact_type: Optional[AssetType]
    artifact_name: str
    status: ExtractionStatus
    provenance: ExtractionProvenance
    extractor: ExtractorInfo
    content: Optional[T] = None
    references: Tuple[ArtifactReference, ...] = ()
    warnings: Tuple[ExtractionIssue, ...] = ()
    errors: Tuple[ExtractionIssue, ...] = ()

    def __post_init__(self) -> None:
        """Enforce the contract that keeps failures from looking like successes."""
        if self.status is ExtractionStatus.SUCCESS and self.errors:
            raise ExtractionContractError(
                "a successful extraction cannot carry errors; use PARTIAL or FAILED"
            )
        if self.status is ExtractionStatus.PARTIAL and not (self.warnings or self.errors):
            raise ExtractionContractError(
                "a partial extraction must explain itself with warnings or errors"
            )
        if self.status is ExtractionStatus.FAILED:
            if not self.errors:
                raise ExtractionContractError("a failed extraction must carry errors")
            if self.content is not None:
                raise ExtractionContractError(
                    "a failed extraction must not return content; an empty model "
                    "is indistinguishable from an artifact that really is empty"
                )
        if self.status is ExtractionStatus.SKIPPED:
            if self.content is not None or self.references:
                raise ExtractionContractError(
                    "a skipped extraction must not return content or references"
                )

    @property
    def succeeded(self) -> bool:
        """Whether usable content was produced, with or without warnings."""
        return self.status in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL)

    def to_dict(self) -> dict:
        """Framework-level fields only; ``content`` is the writer's problem."""
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type.value if self.artifact_type else None,
            "artifact_name": self.artifact_name,
            "status": self.status.value,
            "provenance": self.provenance.to_dict(),
            "extractor": self.extractor.to_dict(),
            "references": [r.to_dict() for r in self.references],
            "warnings": [w.to_dict() for w in self.warnings],
            "errors": [e.to_dict() for e in self.errors],
        }


@dataclass(frozen=True)
class ExtractionRun:
    """Every extraction attempt from one orchestrator pass, in path order."""

    results: Tuple[ExtractionResult, ...] = ()

    @property
    def succeeded(self) -> Tuple[ExtractionResult, ...]:
        return tuple(r for r in self.results if r.succeeded)

    @property
    def failed(self) -> Tuple[ExtractionResult, ...]:
        return tuple(r for r in self.results if r.status is ExtractionStatus.FAILED)

    @property
    def skipped(self) -> Tuple[ExtractionResult, ...]:
        return tuple(r for r in self.results if r.status is ExtractionStatus.SKIPPED)

    @property
    def references(self) -> Tuple[ArtifactReference, ...]:
        """Every observed reference, for the future dependency-graph stage."""
        return tuple(ref for result in self.results for ref in result.references)

    def counts_by_status(self) -> dict:
        counts: dict = {}
        for result in self.results:
            counts[result.status.value] = counts.get(result.status.value, 0) + 1
        return dict(sorted(counts.items()))

    def counts_by_type(self) -> dict:
        counts: dict = {}
        for result in self.results:
            key = result.artifact_type.value if result.artifact_type else "unclassified"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))


def resolve_asset_type(artifact: DetectedArtifact) -> Optional[AssetType]:
    """The AssetType of a detected artifact, or None if it is not a Synapse one.

    ``DetectedArtifact.artifact_type`` is a plain string because it also
    carries the detector's non-Synapse categories.
    """
    try:
        return AssetType(artifact.artifact_type)
    except ValueError:
        return None
