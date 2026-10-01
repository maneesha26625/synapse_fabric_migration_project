"""The Unified Discovery Record: one logical artifact, many sources.

Discovery draws on four sources with different reach. A pipeline's definition
comes from Git, its published state from the Synapse API, and the integration
runtime it depends on from Azure. A dedicated SQL table has no Git
representation at all. Downstream stages — dependency graph, assessment,
migration — should not have to know any of that.

This module is the seam. One record per logical artifact, assembled from
whichever sources were reachable, with every fact traceable to the source
that supplied it.

Design rules:

* **Facets, not one flat model.** Each facet names its own source and carries
  its own provenance and issues, because the same artifact draws each facet
  from a different place.
* **Every facet is optional.** A repository-only run produces records with a
  definition facet and nothing else. That is a complete, valid record, not a
  broken one.
* **Absence is stated, never implied.** A facet nobody supplied is recorded
  as a MISSING_INFORMATION issue rather than left silently null.
* **Identity is separate from content.** The identity is what a future
  reconciler matches across sources; nothing here performs that matching.

Nothing in this module connects to anything, resolves any reference, or
decides anything about migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Optional, Tuple, TypeVar

from discovery_agent.extractors.common_models import (
    AuthenticationMetadata,
    AuthenticationType,
    ConfigEntry,
    SecretReference,
)
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionProvenance,
    ExtractionResult,
    ExtractionStatus,
    IssueCode,
    SourceType,
)
from discovery_agent.models import AssetType, asset_id
from discovery_agent.source_strategy import P0Artifact

#: The artifact-specific definition model: PipelineDefinition,
#: NotebookDefinition, DatasetDefinition, or a future TableDefinition. The
#: record never inspects it.
T = TypeVar("T")

SQL_ID_SCHEME = "sql"
SYNAPSE_ID_SCHEME = "synapse"


class FacetKind(str, Enum):
    """The facets a record can carry.

    Provenance and issues are deliberately absent: they are not standalone
    facets but properties *of* each facet, because a single provenance for a
    whole record could not say which source supplied which part.
    """

    DEFINITION = "definition"
    RUNTIME = "runtime"
    INFRASTRUCTURE = "infrastructure"
    DEPENDENCY = "dependency"
    SECURITY = "security"


class SourceKeyKind(str, Enum):
    """The shape of a natural key within one source."""

    REPOSITORY_PATH = "repository_path"  # pipeline/TripFares.json
    SYNAPSE_ARTIFACT_NAME = "synapse_artifact_name"  # TripFaresDataPipeline
    SQL_OBJECT_NAME = "sql_object_name"  # database.schema.table
    AZURE_RESOURCE_ID = "azure_resource_id"  # /subscriptions/.../pools/x


class DriftStatus(str, Enum):
    """Whether the sources agree about an artifact's definition."""

    UNKNOWN = "unknown"  # not compared; no second source was reached
    IN_SYNC = "in_sync"
    DRIFTED = "drifted"
    NOT_PUBLISHED = "not_published"  # committed but never published
    NOT_IN_SOURCE_CONTROL = "not_in_source_control"  # published but not committed


class InfrastructureRole(str, Enum):
    """What an Azure resource does for this artifact."""

    HOST = "host"  # the Synapse workspace itself
    COMPUTE = "compute"  # Spark pool, SQL pool, integration runtime
    STORAGE = "storage"  # ADLS / blob account
    NETWORK = "network"  # managed VNet, private endpoint
    IDENTITY = "identity"  # managed identity, service principal
    SECRET_STORE = "secret_store"  # Key Vault (metadata only)


# --- identity ----------------------------------------------------------------


@dataclass(frozen=True)
class SourceKey:
    """How one source names this artifact.

    The raw material for a future identity-resolution component: two records
    describe the same logical artifact when their source keys line up. This
    module stores the keys and performs no matching.
    """

    source: SourceType
    kind: SourceKeyKind
    key: str

    def to_dict(self) -> dict:
        return {"source": self.source.value, "kind": self.kind.value, "key": self.key}


@dataclass(frozen=True)
class ArtifactIdentity:
    """What this record is about, independently of where it was found.

    ``logical_id`` is deliberately compatible with ``models.asset_id`` for the
    six repository artifacts, so an ``ArtifactReference.target_id`` emitted by
    an extractor joins straight onto a record without translation. SQL objects
    get their own scheme because they have no ``AssetType``.
    """

    artifact: P0Artifact
    name: str
    workspace: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None
    source_keys: Tuple[SourceKey, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("an artifact identity needs a name")

    @property
    def asset_type(self) -> Optional[AssetType]:
        return self.artifact.asset_type

    @property
    def logical_id(self) -> str:
        """The stable identifier other records and references join on."""
        asset_type = self.asset_type
        if asset_type is not None:
            return asset_id(asset_type, self.name)
        database = self.database or "?"
        schema = self.schema or "dbo"
        return f"{SQL_ID_SCHEME}://{database}/{schema}/{self.name}"

    @property
    def scoped_id(self) -> str:
        """``logical_id`` qualified by workspace, for multi-workspace runs.

        Not used for joining today: extractors emit unscoped ids, so the graph
        joins on ``logical_id``. This is the migration path for when a single
        run covers more than one workspace.
        """
        if not self.workspace:
            return self.logical_id
        scheme, _, rest = self.logical_id.partition("://")
        return f"{scheme}://{self.workspace}/{rest}"

    @property
    def qualified_name(self) -> str:
        """A human-readable name, qualified as far as the source allows."""
        parts = [p for p in (self.database, self.schema, self.name) if p]
        return ".".join(parts) if self.asset_type is None else self.name

    def key_for(self, source: SourceType) -> Optional[SourceKey]:
        return next((k for k in self.source_keys if k.source is source), None)

    def with_key(self, key: SourceKey) -> "ArtifactIdentity":
        """A copy carrying one more source key. Identities are immutable."""
        if any(k.source is key.source and k.key == key.key for k in self.source_keys):
            return self
        return ArtifactIdentity(
            artifact=self.artifact,
            name=self.name,
            workspace=self.workspace,
            database=self.database,
            schema=self.schema,
            source_keys=self.source_keys + (key,),
        )

    def to_dict(self) -> dict:
        return {
            "logical_id": self.logical_id,
            "scoped_id": self.scoped_id,
            "artifact": self.artifact.value,
            "asset_type": self.asset_type.value if self.asset_type else None,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "workspace": self.workspace,
            "database": self.database,
            "schema": self.schema,
            "source_keys": [k.to_dict() for k in self.source_keys],
        }


# --- facets ------------------------------------------------------------------


@dataclass(frozen=True)
class Facet:
    """Common to every facet: which source supplied it, and what went wrong.

    Provenance lives here rather than once per record precisely so a record
    can say "definition from Git at commit abc, infrastructure from ARM".
    """

    source: SourceType
    provenance: ExtractionProvenance
    issues: Tuple[ExtractionIssue, ...] = ()

    @property
    def kind(self) -> FacetKind:  # pragma: no cover - overridden by subclasses
        raise NotImplementedError

    def _base_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "source": self.source.value,
            "provenance": self.provenance.to_dict(),
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass(frozen=True)
class DefinitionFacet(Facet, Generic[T]):
    """The artifact itself, from its authoritative source.

    ``content`` is the extractor's typed model. The invariant from
    ``ExtractionResult`` is repeated here because a facet can be built
    directly: a failed extraction never carries content, so an empty model can
    never be mistaken for an artifact that really is empty.
    """

    status: ExtractionStatus = ExtractionStatus.SUCCESS
    content: Optional[T] = None
    content_type: Optional[str] = None  # e.g. "PipelineDefinition"
    extractor: Optional[str] = None
    extractor_version: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status is ExtractionStatus.FAILED and self.content is not None:
            raise ValueError(
                "a failed definition facet must not carry content; an empty "
                "model is indistinguishable from an artifact that is empty"
            )

    @property
    def kind(self) -> FacetKind:
        return FacetKind.DEFINITION

    @property
    def is_usable(self) -> bool:
        return self.content is not None and self.status in (
            ExtractionStatus.SUCCESS,
            ExtractionStatus.PARTIAL,
        )

    @classmethod
    def from_extraction_result(
        cls, result: ExtractionResult
    ) -> "DefinitionFacet":
        """Build the facet from what the extractor framework already produced.

        The bridge from today's pipeline: every existing extractor returns an
        ``ExtractionResult``, which already holds the content, the status, the
        provenance and the issues this facet needs.
        """
        return cls(
            source=result.provenance.source_type,
            provenance=result.provenance,
            issues=tuple(result.errors) + tuple(result.warnings),
            status=result.status,
            content=result.content,
            content_type=type(result.content).__name__ if result.content else None,
            extractor=result.extractor.name,
            extractor_version=result.extractor.version,
        )

    def to_dict(self) -> dict:
        payload = self._base_dict()
        payload.update(
            {
                "status": self.status.value,
                "content_type": self.content_type,
                "extractor": self.extractor,
                "extractor_version": self.extractor_version,
                "has_content": self.content is not None,
            }
        )
        return payload


@dataclass(frozen=True)
class RuntimeFacet(Facet):
    """Published and live state — what the workspace is actually running.

    Almost always sourced from Synapse; for a SQL object, from SQL itself
    (row counts and statistics are runtime facts about a table).
    """

    published: Optional[bool] = None
    state: Optional[str] = None  # e.g. a trigger's Started / Stopped
    drift: DriftStatus = DriftStatus.UNKNOWN
    drift_paths: Tuple[str, ...] = ()  # which fields differ, when drifted
    observations: Tuple[ConfigEntry, ...] = ()  # last run status, row counts, ...

    @property
    def kind(self) -> FacetKind:
        return FacetKind.RUNTIME

    def to_dict(self) -> dict:
        payload = self._base_dict()
        payload.update(
            {
                "published": self.published,
                "state": self.state,
                "drift": self.drift.value,
                "drift_paths": list(self.drift_paths),
                "observations": [o.to_dict() for o in self.observations],
            }
        )
        return payload


@dataclass(frozen=True)
class AzureResource:
    """One Azure resource this artifact depends on."""

    resource_id: str  # the ARM id
    resource_type: str  # Microsoft.Synapse/workspaces/bigDataPools
    name: str
    role: InfrastructureRole
    location: Optional[str] = None
    sku: Optional[str] = None
    properties: Tuple[ConfigEntry, ...] = ()

    def to_dict(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "name": self.name,
            "role": self.role.value,
            "location": self.location,
            "sku": self.sku,
            "properties": [p.to_dict() for p in self.properties],
        }


@dataclass(frozen=True)
class InfrastructureFacet(Facet):
    """The Azure resources underneath: compute, storage, network, identity.

    The facet that explains why an artifact which parses perfectly can still
    not work — a missing role assignment, a paused pool, a firewall.
    """

    resources: Tuple[AzureResource, ...] = ()
    observations: Tuple[ConfigEntry, ...] = ()

    @property
    def kind(self) -> FacetKind:
        return FacetKind.INFRASTRUCTURE

    def resources_with_role(self, role: InfrastructureRole) -> Tuple[AzureResource, ...]:
        return tuple(r for r in self.resources if r.role is role)

    def to_dict(self) -> dict:
        payload = self._base_dict()
        payload.update(
            {
                "resources": [r.to_dict() for r in self.resources],
                "observations": [o.to_dict() for o in self.observations],
            }
        )
        return payload


@dataclass(frozen=True)
class DependencyFacet(Facet):
    """What this artifact references, as observed — never as resolved.

    Holds ``ArtifactReference`` values exactly as the extractors emit them,
    with ``resolved`` still False. Turning them into graph edges is the
    dependency-resolution stage's job, and it needs them unresolved.
    """

    references: Tuple[ArtifactReference, ...] = ()

    def __post_init__(self) -> None:
        resolved = [r for r in self.references if r.resolved]
        if resolved:
            raise ValueError(
                "a discovery record must not contain resolved references; "
                "resolution happens in the dependency graph stage"
            )

    @property
    def kind(self) -> FacetKind:
        return FacetKind.DEPENDENCY

    @property
    def target_ids(self) -> Tuple[str, ...]:
        """Logical ids of the targets that are named Synapse artifacts.

        What the future graph stage joins against ``ArtifactIdentity.logical_id``.
        """
        return tuple(sorted({r.target_id for r in self.references if r.target_id}))

    def to_dict(self) -> dict:
        payload = self._base_dict()
        payload.update(
            {
                "references": [r.to_dict() for r in self.references],
                "target_ids": list(self.target_ids),
            }
        )
        return payload


@dataclass(frozen=True)
class SecurityFacet(Facet):
    """Safe authentication metadata. Never secret values.

    Both member types are structurally value-free: ``SecretReference`` records
    that a secret is referenced and names the vault and the secret, and
    ``AuthenticationMetadata`` records the mechanism. Neither has a field a
    secret could occupy.
    """

    authentication: Tuple[AuthenticationMetadata, ...] = ()
    secret_references: Tuple[SecretReference, ...] = ()

    @property
    def kind(self) -> FacetKind:
        return FacetKind.SECURITY

    def to_dict(self) -> dict:
        payload = self._base_dict()
        payload.update(
            {
                "authentication": [a.to_dict() for a in self.authentication],
                "secret_references": [s.to_dict() for s in self.secret_references],
            }
        )
        return payload


# --- issues ------------------------------------------------------------------


@dataclass(frozen=True)
class RecordIssue:
    """An issue, with the facet and source it belongs to.

    Wraps ``ExtractionIssue`` so a record-level problem — a source that was
    unreachable, a facet nobody supplied — carries the same vocabulary as an
    extractor-level one.
    """

    issue: ExtractionIssue
    source: Optional[SourceType] = None
    facet: Optional[FacetKind] = None

    @property
    def code(self) -> IssueCode:
        return self.issue.code

    @property
    def message(self) -> str:
        return self.issue.message

    def to_dict(self) -> dict:
        return {
            "issue": self.issue.to_dict(),
            "source": self.source.value if self.source else None,
            "facet": self.facet.value if self.facet else None,
        }


def missing_facet(
    facet: FacetKind, source: SourceType, reason: Optional[str] = None
) -> RecordIssue:
    """State that a facet was not collected, rather than leaving it silent."""
    return RecordIssue(
        issue=ExtractionIssue(
            code=IssueCode.MISSING_INFORMATION,
            message=reason
            or f"no {facet.value} facet: {source.value} was not consulted",
        ),
        source=source,
        facet=facet,
    )


def source_unavailable(
    facet: FacetKind, source: SourceType, reason: str
) -> RecordIssue:
    """State that a source was expected but could not be reached."""
    return RecordIssue(
        issue=ExtractionIssue(code=IssueCode.SOURCE_UNAVAILABLE, message=reason),
        source=source,
        facet=facet,
    )


# --- the record --------------------------------------------------------------


@dataclass(frozen=True)
class UnifiedDiscoveryRecord(Generic[T]):
    """One logical artifact, assembled from whichever sources were reachable.

    Every facet is optional. A record with only a definition facet is the
    normal output of a repository-only run and is complete for what it claims
    to be; what is missing is stated in ``issues`` rather than implied by a
    null.
    """

    identity: ArtifactIdentity
    definition: Optional[DefinitionFacet] = None
    runtime: Optional[RuntimeFacet] = None
    infrastructure: Optional[InfrastructureFacet] = None
    dependencies: Optional[DependencyFacet] = None
    security: Optional[SecurityFacet] = None
    issues: Tuple[RecordIssue, ...] = field(default_factory=tuple)

    # -- shape -------------------------------------------------------------

    @property
    def logical_id(self) -> str:
        return self.identity.logical_id

    @property
    def artifact(self) -> P0Artifact:
        return self.identity.artifact

    @property
    def facets(self) -> Tuple[Facet, ...]:
        """Present facets, in a fixed order."""
        return tuple(
            f
            for f in (
                self.definition,
                self.runtime,
                self.infrastructure,
                self.dependencies,
                self.security,
            )
            if f is not None
        )

    @property
    def facet_kinds(self) -> Tuple[FacetKind, ...]:
        return tuple(f.kind for f in self.facets)

    def facet(self, kind: FacetKind) -> Optional[Facet]:
        return next((f for f in self.facets if f.kind is kind), None)

    @property
    def sources(self) -> Tuple[SourceType, ...]:
        """Every source that contributed something, in a stable order."""
        contributing = {f.source for f in self.facets}
        return tuple(
            s
            for s in (
                SourceType.REPOSITORY,
                SourceType.SYNAPSE,
                SourceType.SQL,
                SourceType.AZURE,
            )
            if s in contributing
        )

    @property
    def is_definition_only(self) -> bool:
        return self.facet_kinds == (FacetKind.DEFINITION,)

    @property
    def has_usable_definition(self) -> bool:
        return self.definition is not None and self.definition.is_usable

    # -- content -----------------------------------------------------------

    @property
    def content(self) -> Optional[T]:
        """The artifact's definition model, if one was extracted."""
        return self.definition.content if self.definition else None

    @property
    def references(self) -> Tuple[ArtifactReference, ...]:
        return self.dependencies.references if self.dependencies else ()

    # -- provenance and issues --------------------------------------------

    def provenance_by_facet(self) -> dict:
        """Which source supplied each facet, and from where."""
        return {f.kind.value: f.provenance.to_dict() for f in self.facets}

    @property
    def all_issues(self) -> Tuple[RecordIssue, ...]:
        """Record-level issues plus every facet's own, with context attached."""
        from_facets = tuple(
            RecordIssue(issue=issue, source=facet.source, facet=facet.kind)
            for facet in self.facets
            for issue in facet.issues
        )
        return self.issues + from_facets

    def issues_with_code(self, code: IssueCode) -> Tuple[RecordIssue, ...]:
        return tuple(i for i in self.all_issues if i.code is code)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_extraction_result(
        cls,
        result: ExtractionResult,
        workspace: Optional[str] = None,
    ) -> Optional["UnifiedDiscoveryRecord"]:
        """Build a repository-only record from an extractor's output.

        Returns None when the result is not a P0 artifact — a trigger or data
        flow is a real artifact but out of P0 scope, and inventing a record for
        it would misrepresent the scope.
        """
        if result.artifact_type is None:
            return None
        artifact = P0Artifact.from_asset_type(result.artifact_type)
        if artifact is None:
            return None

        identity = ArtifactIdentity(
            artifact=artifact,
            name=result.artifact_name,
            workspace=workspace,
            source_keys=(
                (
                    SourceKey(
                        source=result.provenance.source_type,
                        kind=SourceKeyKind.REPOSITORY_PATH,
                        key=result.provenance.source_path,
                    ),
                )
                if result.provenance.source_path
                else ()
            ),
        )

        definition = DefinitionFacet.from_extraction_result(result)
        dependencies = (
            DependencyFacet(
                source=result.provenance.source_type,
                provenance=result.provenance,
                references=result.references,
            )
            if result.references
            else None
        )
        return cls(
            identity=identity,
            definition=definition,
            dependencies=dependencies,
        )

    # -- serialization -----------------------------------------------------

    def summary(self) -> dict:
        """A compact overview. Carries no definition content and no secrets."""
        return {
            "logical_id": self.logical_id,
            "artifact": self.artifact.value,
            "name": self.identity.name,
            "qualified_name": self.identity.qualified_name,
            "facets": [k.value for k in self.facet_kinds],
            "sources": [s.value for s in self.sources],
            "definition_only": self.is_definition_only,
            "has_usable_definition": self.has_usable_definition,
            "reference_count": len(self.references),
            "issue_count": len(self.all_issues),
        }

    def to_dict(self) -> dict:
        return {
            "identity": self.identity.to_dict(),
            "definition": self.definition.to_dict() if self.definition else None,
            "runtime": self.runtime.to_dict() if self.runtime else None,
            "infrastructure": self.infrastructure.to_dict()
            if self.infrastructure
            else None,
            "dependencies": self.dependencies.to_dict() if self.dependencies else None,
            "security": self.security.to_dict() if self.security else None,
            "issues": [i.to_dict() for i in self.all_issues],
            "sources": [s.value for s in self.sources],
        }


@dataclass(frozen=True)
class DiscoveryRecordSet:
    """Every record from one discovery run, in logical-id order."""

    records: Tuple[UnifiedDiscoveryRecord, ...] = ()

    def __post_init__(self) -> None:
        """Records are unique by ``scoped_id``, not by ``logical_id``.

        The two are identical for every record whose identity carries no
        workspace, which is every repository-only record -- so this is the
        same check it has always been for that case.

        The difference matters when a live artifact could *not* be identified
        with a same-named repository one. Preserving both observations means
        emitting two records, and two records need two ids; the workspace is
        the real fact that distinguishes them. Keying on ``logical_id`` would
        force a choice between discarding an observation and refusing to
        build the set, and both are worse than carrying both.
        """
        ids = [r.identity.scoped_id for r in self.records]
        if len(ids) != len(set(ids)):
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate artifact ids in record set: {duplicates}")

    def by_id(self, logical_id: str) -> Optional[UnifiedDiscoveryRecord]:
        """The record with this logical id, or its scoped id."""
        return next(
            (
                r
                for r in self.records
                if logical_id in (r.logical_id, r.identity.scoped_id)
            ),
            None,
        )

    def of_artifact(self, artifact: P0Artifact) -> Tuple[UnifiedDiscoveryRecord, ...]:
        return tuple(r for r in self.records if r.artifact is artifact)

    @property
    def all_references(self) -> Tuple[ArtifactReference, ...]:
        """Every observed reference, unresolved, for the graph stage."""
        return tuple(ref for record in self.records for ref in record.references)

    def counts_by_artifact(self) -> dict:
        counts: dict = {}
        for record in self.records:
            counts[record.artifact.value] = counts.get(record.artifact.value, 0) + 1
        return dict(sorted(counts.items()))

    def counts_by_source(self) -> dict:
        counts: dict = {}
        for record in self.records:
            for source in record.sources:
                counts[source.value] = counts.get(source.value, 0) + 1
        return dict(sorted(counts.items()))

    def summary(self) -> dict:
        return {
            "record_count": len(self.records),
            "by_artifact": self.counts_by_artifact(),
            "by_source": self.counts_by_source(),
            "reference_count": len(self.all_references),
            "definition_only": sum(1 for r in self.records if r.is_definition_only),
        }
