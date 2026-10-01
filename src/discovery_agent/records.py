"""Assembling one discovery run's sources into Unified Discovery Records.

Discovery reads three sources with different reach. Git holds what was
committed; the live workspace holds what is published and actually runs; the
SQL catalog holds objects that exist nowhere else. This module turns those
three streams into the record model that already exists in
``discovery_models`` -- it defines no second record type and changes none of
the facets.

Three decisions live here and nowhere else:

**Identity.** Two artifacts with the same name are the same artifact only if
they come from the same place. A pipeline called ``Load`` in a clone and a
pipeline called ``Load`` in a workspace are the same one *if that workspace is
Git-integrated with that repository* -- which ARM reports, so it is checked
rather than assumed. When it cannot be checked, the two observations are kept
as two records and the possible relationship is recorded. Nothing is merged
on a name alone.

**Drift.** Only ever computed between definitions that were proven to be the
same artifact, and only from a canonical rendering that ignores JSON object
key order and whitespace. Formatting is not drift. A comparison that cannot
be made is ``UNKNOWN`` with a reason, never ``IN_SYNC``.

**Absence.** A facet nobody supplied is a stated issue, not a null. Which of
absent, unavailable, not-applicable, opaque or unsupported applies is decided
at the source that knows, and carried here unchanged.

What this module does not do: resolve references, score anything, or decide
anything about migration. References arrive unresolved and leave unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.artifacts.models import ArtifactDetectionResult, DetectedArtifact
from discovery_agent.discovery_models import (
    ArtifactIdentity,
    DefinitionFacet,
    DependencyFacet,
    DiscoveryRecordSet,
    DriftStatus,
    FacetKind,
    RecordIssue,
    RuntimeFacet,
    SecurityFacet,
    SourceKey,
    SourceKeyKind,
    UnifiedDiscoveryRecord,
)
from discovery_agent.errors import MalformedArtifactError
from discovery_agent.extractors.common_models import ConfigEntry
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionProvenance,
    ExtractionResult,
    ExtractionRun,
    ExtractionStatus,
    IssueCode,
    ReferenceKind,
    SourceType,
)
from discovery_agent.extractors.sources import ArtifactSource
from discovery_agent.extractors.sql_findings import sql_object_references
from discovery_agent.models import AssetType, Evidence
from discovery_agent.source_strategy import P0Artifact, strategy_for
from discovery_agent.sql.models import (
    DefinitionState,
    SqlModuleObject,
    SqlProcedure,
    SqlTable,
    SqlView,
)
from discovery_agent.sql.result import SqlCatalogDiscovery
from discovery_agent.synapse.models import (
    SynapseArtifact,
    SynapseWorkspaceDiscovery,
    canonical_json,
    comparison_hash,
    normalize_definition,
)

#: Which P0 artifact each catalog object kind is.
_SQL_ARTIFACTS = {
    "table": P0Artifact.DEDICATED_SQL_TABLE,
    "view": P0Artifact.SQL_VIEW,
    "procedure": P0Artifact.STORED_PROCEDURE,
}

#: The extractor name recorded on references derived from SQL module text.
_SQL_OBSERVER = "sql_catalog"


class IdentityResolution(str, Enum):
    """How certain the run is that a Git artifact and a live one are one thing.

    ``PROVEN`` is the only value that permits merging. It requires a positive
    answer from the workspace itself: ARM reports which repository a workspace
    is Git-integrated with, and that repository is the one that was acquired.

    ``DIFFERENT_REPOSITORY`` and ``UNDETERMINED`` both forbid merging, and are
    kept apart because they mean different things. The first is a positive
    answer that the two are unrelated. The second is no answer at all -- the
    workspace has no Git configuration, or ARM could not be read -- and must
    not be reported as if it were the first.
    """

    PROVEN = "proven"
    DIFFERENT_REPOSITORY = "different_repository"
    UNDETERMINED = "undetermined"
    NOT_APPLICABLE = "not_applicable"  # only one source ran

    @property
    def permits_merging(self) -> bool:
        return self is IdentityResolution.PROVEN


def resolve_identity(
    workspace_repository: Any, repository_url: Optional[str]
) -> Tuple[IdentityResolution, str]:
    """Whether the acquired repository is this workspace's own, and why.

    ``workspace_repository`` is the ``WorkspaceRepository`` ARM reported, or
    None. Returning the reason alongside the verdict is not decoration: an
    operator who sees two records for what they believe is one artifact needs
    to know whether the tool failed to check or checked and disagreed.
    """
    if repository_url is None or workspace_repository is None:
        if repository_url is None:
            return (
                IdentityResolution.NOT_APPLICABLE,
                "no repository was acquired, so nothing is being identified "
                "across sources",
            )
        return (
            IdentityResolution.UNDETERMINED,
            "the workspace reports no Git integration, so there is no "
            "authoritative statement that these artifacts come from the "
            "repository that was scanned",
        )

    verdict = workspace_repository.matches(repository_url)
    configured = workspace_repository.repository_url
    if verdict is True:
        return (
            IdentityResolution.PROVEN,
            f"the workspace is Git-integrated with {configured}, which is the "
            f"repository that was scanned",
        )
    if verdict is False:
        return (
            IdentityResolution.DIFFERENT_REPOSITORY,
            f"the workspace is Git-integrated with {configured}, which is not "
            f"the repository that was scanned ({repository_url})",
        )
    return (
        IdentityResolution.UNDETERMINED,
        "the workspace's Git configuration could not be compared with the "
        "repository that was scanned",
    )


# --- one source's view of one artifact ---------------------------------------


@dataclass(frozen=True)
class SourceDefinition:
    """What one source produced for one artifact, before any merging.

    ``result`` is the shared extractor's output -- the same class whichever
    source it came from, because the same extractor read both. ``definition``
    is the raw ``properties`` object, kept only so two sources can be compared
    without re-running an extractor, and never serialized into a record.
    """

    artifact: P0Artifact
    name: str
    result: ExtractionResult
    definition: Any = None

    @property
    def source(self) -> SourceType:
        return self.result.provenance.source_type

    @property
    def provenance(self) -> ExtractionProvenance:
        return self.result.provenance

    @property
    def fingerprint(self) -> Optional[str]:
        """The hash this definition is compared to another source on.

        Canonical: sorted object keys and fixed separators, so whitespace and
        key order -- which carry no meaning in JSON -- cannot make two
        identical artifacts look different. Normalised: the defaults the
        Artifacts API materialises and Git omits are dropped from both sides.
        Array order is preserved, because it does carry meaning -- reordering
        a pipeline's activities changes the pipeline.
        """
        return None if self.definition is None else comparison_hash(self.definition)

    @property
    def key(self) -> Tuple[P0Artifact, str]:
        return (self.artifact, self.name)


def definitions_from_extraction(
    run: ExtractionRun,
    detection: Optional[ArtifactDetectionResult] = None,
    source: Optional[ArtifactSource] = None,
    comparable: Optional[Iterable[Tuple[P0Artifact, str]]] = None,
) -> Tuple[SourceDefinition, ...]:
    """Pair every P0 extraction result with its raw definition, where wanted.

    The raw definition is read back only for the artifacts named in
    ``comparable`` -- the ones some other source also saw. A run with no
    second source re-reads nothing, so the cost of being able to compare is
    paid only when there is something to compare against.

    Results for artifacts outside P0 -- a trigger, a data flow, a KQL script --
    produce no ``SourceDefinition``. They are real artifacts and out of scope,
    and inventing a record for them would misstate the scope.
    """
    wanted = set(comparable) if comparable is not None else None
    by_path: Dict[str, DetectedArtifact] = (
        {a.source_path: a for a in detection.artifacts} if detection else {}
    )

    definitions: List[SourceDefinition] = []
    for result in run.results:
        if result.artifact_type is None:
            continue
        artifact = P0Artifact.from_asset_type(result.artifact_type)
        if artifact is None:
            continue

        raw = None
        key = (artifact, result.artifact_name)
        if source is not None and (wanted is None or key in wanted):
            raw = _definition_of(result, by_path, source)

        definitions.append(
            SourceDefinition(
                artifact=artifact,
                name=result.artifact_name,
                result=result,
                definition=raw,
            )
        )
    return tuple(definitions)


def _definition_of(
    result: ExtractionResult,
    by_path: Mapping[str, DetectedArtifact],
    source: ArtifactSource,
) -> Any:
    """The artifact's raw ``properties`` object, or None if unreadable.

    None rather than raising: a definition that cannot be re-read costs the
    drift comparison and nothing else, and the comparison says so.
    """
    detected = by_path.get(result.provenance.source_path or "")
    if detected is None:
        return None
    try:
        document = source.read_json(detected)
    except MalformedArtifactError:
        return None
    if not isinstance(document, Mapping):
        return None
    properties = document.get("properties")
    return properties if properties is not None else {}


def definitions_from_workspace(
    run: ExtractionRun, discovery: SynapseWorkspaceDiscovery
) -> Tuple[SourceDefinition, ...]:
    """Pair every live extraction result with the artifact it was read from.

    The live definitions are already in memory -- the listing returned them --
    so nothing is fetched again here.
    """
    by_route: Dict[str, SynapseArtifact] = {a.route: a for a in discovery.artifacts}

    definitions: List[SourceDefinition] = []
    for result in run.results:
        if result.artifact_type is None:
            continue
        artifact = P0Artifact.from_asset_type(result.artifact_type)
        if artifact is None:
            continue
        live = by_route.get(result.provenance.source_path or "")
        definitions.append(
            SourceDefinition(
                artifact=artifact,
                name=result.artifact_name,
                result=result,
                definition=live.definition if live is not None else None,
            )
        )
    return tuple(definitions)


# --- drift -------------------------------------------------------------------


def compare(
    committed: SourceDefinition, live: SourceDefinition
) -> Tuple[DriftStatus, Tuple[str, ...], Optional[ExtractionIssue]]:
    """Whether two proven-identical definitions agree, and where they do not.

    Conservative by construction:

    * Comparison is of a canonical rendering, so indentation, line endings and
      JSON object key order never produce drift.
    * A definition that could not be read on either side is ``UNKNOWN`` with
      an issue saying so. It is never ``IN_SYNC``, because "we could not look"
      and "we looked and they match" are opposite claims.
    * ``drift_paths`` names the top-level properties that differ and stops
      there. A field-level semantic diff is a later stage's work, and guessing
      at one here would produce paths nobody verified.
    """
    if committed.definition is None or live.definition is None:
        missing = "the committed" if committed.definition is None else "the live"
        return (
            DriftStatus.UNKNOWN,
            (),
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                f"{missing} definition could not be read, so the two sources "
                f"were not compared; no claim is made about whether they agree",
            ),
        )

    if committed.fingerprint == live.fingerprint:
        return DriftStatus.IN_SYNC, (), None

    return (
        DriftStatus.DRIFTED,
        differing_paths(committed.definition, live.definition),
        ExtractionIssue(
            IssueCode.DRIFT_DETECTED,
            "the committed definition and the published definition differ; "
            "the repository is not what this workspace is running",
        ),
    )


def differing_paths(committed: Any, live: Any) -> Tuple[str, ...]:
    """Top-level property names whose normalised renderings differ.

    Normalised on both sides, so a property the service materialised and the
    repository omitted is not named here. Top level only, and deliberately:
    descending further would mean deciding what counts as a meaningful
    difference inside an activity or a cell, which is a semantic judgement
    this stage does not make.
    """
    committed = normalize_definition(committed)
    live = normalize_definition(live)
    if not isinstance(committed, Mapping) or not isinstance(live, Mapping):
        return ("properties",)
    names = sorted(set(committed) | set(live))
    return tuple(
        name
        for name in names
        if canonical_json(committed.get(name)) != canonical_json(live.get(name))
    )


# --- building records --------------------------------------------------------


def _source_key(definition: SourceDefinition, workspace: Optional[str]) -> SourceKey:
    """How this source names the artifact."""
    provenance = definition.provenance
    if definition.source is SourceType.SYNAPSE:
        route = provenance.source_path or definition.name
        return SourceKey(
            source=SourceType.SYNAPSE,
            kind=SourceKeyKind.SYNAPSE_ARTIFACT_NAME,
            key=f"{workspace}/{route}" if workspace else route,
        )
    return SourceKey(
        source=SourceType.REPOSITORY,
        kind=SourceKeyKind.REPOSITORY_PATH,
        key=provenance.source_path or definition.name,
    )


def _security_facet(definition: SourceDefinition) -> Optional[SecurityFacet]:
    """Authentication mechanisms and secret *references*, when the model has them.

    Read by attribute rather than by artifact type, because only some models
    carry them and the ones that do all spell them the same way. Both member
    types are structurally value-free -- neither has a field a secret could
    occupy -- so this cannot carry one however the extractor behaved.
    """
    content = definition.result.content
    if content is None:
        return None
    authentication = tuple(getattr(content, "authentication", ()) or ())
    secrets = tuple(getattr(content, "secrets", ()) or ())
    if not authentication and not secrets:
        return None
    return SecurityFacet(
        source=definition.source,
        provenance=definition.provenance,
        authentication=authentication,
        secret_references=secrets,
    )


def record_for(
    definition: SourceDefinition,
    workspace: Optional[str] = None,
    scoped: bool = False,
    issues: Sequence[RecordIssue] = (),
) -> UnifiedDiscoveryRecord:
    """One source's observation, as a record.

    ``scoped`` puts the workspace into the identity. It is set for a live
    artifact that could *not* be identified with a repository one, so the two
    records keep distinct ids instead of colliding on a shared name -- which
    is what preserving both observations requires.
    """
    identity = ArtifactIdentity(
        artifact=definition.artifact,
        name=definition.name,
        workspace=workspace if scoped else None,
        source_keys=(_source_key(definition, workspace),),
    )

    result = definition.result
    dependencies = (
        DependencyFacet(
            source=definition.source,
            provenance=definition.provenance,
            references=result.references,
        )
        if result.references
        else None
    )

    return UnifiedDiscoveryRecord(
        identity=identity,
        definition=DefinitionFacet.from_extraction_result(result),
        dependencies=dependencies,
        security=_security_facet(definition),
        issues=tuple(issues),
    )


def merged_record(
    committed: SourceDefinition,
    live: SourceDefinition,
    workspace: Optional[str],
    reason: str,
) -> UnifiedDiscoveryRecord:
    """One record for an artifact both sources saw and identity proved is one.

    The definition facet comes from Git, which the source strategy names
    primary for all six repository artifacts. The live observation is not
    discarded: it becomes the runtime facet, carrying its own provenance, its
    own published state and the drift verdict. Both source keys stay on the
    identity, so nothing about where each half came from is lost.
    """
    identity = ArtifactIdentity(
        artifact=committed.artifact,
        name=committed.name,
        workspace=workspace,
        source_keys=(
            _source_key(committed, workspace),
            _source_key(live, workspace),
        ),
    )

    status, paths, issue = compare(committed, live)
    # Named for what they are: the hash the comparison was made on, after
    # normalisation. The provenance sha256 identifies the bytes each source
    # served, which is a different question and stays on the provenance.
    observations = [ConfigEntry("identity_resolution", reason)]
    if live.fingerprint:
        observations.append(
            ConfigEntry("published_comparison_sha256", live.fingerprint)
        )
    if committed.fingerprint:
        observations.append(
            ConfigEntry("committed_comparison_sha256", committed.fingerprint)
        )

    runtime = RuntimeFacet(
        source=SourceType.SYNAPSE,
        provenance=live.provenance,
        issues=(issue,) if issue else (),
        published=True,
        drift=status,
        drift_paths=paths,
        observations=tuple(observations),
    )

    references = committed.result.references
    dependencies = (
        DependencyFacet(
            source=committed.source,
            provenance=committed.provenance,
            references=references,
        )
        if references
        else None
    )

    return UnifiedDiscoveryRecord(
        identity=identity,
        definition=DefinitionFacet.from_extraction_result(committed.result),
        runtime=runtime,
        dependencies=dependencies,
        security=_security_facet(committed),
    )


def _unresolved_issue(
    other: SourceDefinition, workspace: Optional[str], reason: str
) -> RecordIssue:
    """The record of a relationship that is possible but was not established."""
    where = "the live workspace" if other.source is SourceType.SYNAPSE else "the repository"
    return RecordIssue(
        issue=ExtractionIssue(
            IssueCode.MISSING_INFORMATION,
            f"{where} also holds a {other.artifact.value} named "
            f"{other.name!r}; they may be the same artifact, but this was not "
            f"established: {reason}. Both observations are reported separately",
            other.provenance.source_path,
        ),
        source=other.source,
    )


def _not_published_record(
    committed: SourceDefinition, workspace: Optional[str], reason: str
) -> UnifiedDiscoveryRecord:
    """A Git artifact whose proven-matching workspace has no such artifact."""
    record = record_for(committed, workspace=workspace)
    runtime = RuntimeFacet(
        source=SourceType.SYNAPSE,
        provenance=ExtractionProvenance(
            source_type=SourceType.SYNAPSE,
            source_format="synapse_artifact",
            resource_id=workspace,
        ),
        published=False,
        drift=DriftStatus.NOT_PUBLISHED,
        observations=(ConfigEntry("identity_resolution", reason),),
    )
    return UnifiedDiscoveryRecord(
        identity=record.identity,
        definition=record.definition,
        runtime=runtime,
        dependencies=record.dependencies,
        security=record.security,
    )


def _not_in_source_control_record(
    live: SourceDefinition, workspace: Optional[str], reason: str
) -> UnifiedDiscoveryRecord:
    """A live artifact the proven-matching repository does not contain."""
    record = record_for(live, workspace=workspace)
    runtime = RuntimeFacet(
        source=SourceType.SYNAPSE,
        provenance=live.provenance,
        published=True,
        drift=DriftStatus.NOT_IN_SOURCE_CONTROL,
        observations=(ConfigEntry("identity_resolution", reason),),
    )
    return UnifiedDiscoveryRecord(
        identity=record.identity,
        definition=record.definition,
        runtime=runtime,
        dependencies=record.dependencies,
        security=record.security,
    )


# --- SQL records -------------------------------------------------------------


def _sql_identity(key, workspace: Optional[str]) -> ArtifactIdentity:
    artifact = _SQL_ARTIFACTS[key.object_type.value]
    return ArtifactIdentity(
        artifact=artifact,
        name=key.name,
        workspace=workspace,
        database=key.database,
        schema=key.schema,
        source_keys=(
            SourceKey(
                source=SourceType.SQL,
                kind=SourceKeyKind.SQL_OBJECT_NAME,
                key=key.qualified_name,
            ),
        ),
    )


def _module_references(module: SqlModuleObject) -> Tuple[ArtifactReference, ...]:
    """The objects a view's or procedure's body names, as observations.

    Reuses the same conversion the SQL script extractor uses, so a table named
    by a view and the same table named by a pre-copy script arrive on the
    reference channel identically. ``resolved`` stays False throughout: naming
    an object is not proof it exists.

    The module's own name is dropped. ``CREATE VIEW dbo.v AS ...`` names
    ``dbo.v``, and a view that depends on itself is not a finding.
    """
    identity = _sql_identity(module.key, None)
    own = {module.key.name.lower(), f"{module.key.schema}.{module.key.name}".lower()}
    references = sql_object_references(
        module.referenced_objects,
        source_artifact_id=identity.logical_id,
        source_artifact_type=AssetType.SQL_SCRIPT,
        source_path=module.key.qualified_name,
        extractor=_SQL_OBSERVER,
    )
    return tuple(r for r in references if r.target_name.lower() not in own)


def records_from_catalog(
    catalog: SqlCatalogDiscovery, workspace: Optional[str] = None
) -> Tuple[UnifiedDiscoveryRecord, ...]:
    """The three SQL-primary P0 artifacts, as records.

    A catalog object has no extractor and no ``ExtractionResult``: its
    definition is a set of catalog rows, and the typed model the SQL package
    assembled *is* the definition. It is carried in the definition facet
    directly, which is what that facet is for.

    The run-level provenance is used for every record, qualified per object,
    so a record never has to guess which pool a ``dbo.Customer`` came from.
    """
    records: List[UnifiedDiscoveryRecord] = []

    for table in catalog.tables:
        records.append(_sql_record(table, catalog, workspace))
    for view in catalog.views:
        records.append(_sql_record(view, catalog, workspace, references=_module_references(view)))
    for procedure in catalog.procedures:
        records.append(
            _sql_record(
                procedure, catalog, workspace, references=_module_references(procedure)
            )
        )
    return tuple(records)


def _sql_record(
    obj: Any,
    catalog: SqlCatalogDiscovery,
    workspace: Optional[str],
    references: Tuple[ArtifactReference, ...] = (),
) -> UnifiedDiscoveryRecord:
    """One catalog object as a record, with its own gaps attached to it."""
    provenance = ExtractionProvenance(
        source_type=SourceType.SQL,
        source_format=catalog.provenance.source_format,
        source_path=None,  # a catalog object has no path
        resource_id=f"{catalog.provenance.resource_id}/{obj.key.schema}/{obj.key.name}",
    )

    status = ExtractionStatus.SUCCESS
    if isinstance(obj, SqlModuleObject) and not obj.has_definition:
        # The object exists and its body could not be read. PARTIAL, not
        # FAILED: identity, dates and -- for a procedure -- the signature are
        # real findings, and a FAILED facet would be forbidden from carrying
        # them.
        status = ExtractionStatus.PARTIAL
    elif obj.issues:
        status = ExtractionStatus.PARTIAL

    definition = DefinitionFacet(
        source=SourceType.SQL,
        provenance=provenance,
        issues=obj.issues,
        status=status,
        content=obj,
        content_type=type(obj).__name__,
        extractor=_SQL_OBSERVER,
        extractor_version="1.0.0",
    )

    dependencies = (
        DependencyFacet(
            source=SourceType.SQL, provenance=provenance, references=references
        )
        if references
        else None
    )

    return UnifiedDiscoveryRecord(
        identity=_sql_identity(obj.key, workspace),
        definition=definition,
        dependencies=dependencies,
    )


# --- the entry point ---------------------------------------------------------


def build_records(
    repository: Sequence[SourceDefinition] = (),
    workspace_artifacts: Sequence[SourceDefinition] = (),
    catalog: Optional[SqlCatalogDiscovery] = None,
    workspace: Optional[str] = None,
    resolution: IdentityResolution = IdentityResolution.NOT_APPLICABLE,
    reason: str = "",
) -> DiscoveryRecordSet:
    """Every discovered artifact, from whichever sources were reachable.

    Merging happens only when ``resolution`` permits it. Otherwise each source
    keeps its own record and each record states that the other source holds a
    same-named artifact whose relationship was not established -- which is the
    honest output, and the one a reviewer can act on.
    """
    committed = {d.key: d for d in repository}
    live = {d.key: d for d in workspace_artifacts}
    records: List[UnifiedDiscoveryRecord] = []

    if resolution.permits_merging:
        for key, definition in committed.items():
            counterpart = live.get(key)
            if counterpart is None:
                records.append(_not_published_record(definition, workspace, reason))
            else:
                records.append(
                    merged_record(definition, counterpart, workspace, reason)
                )
        for key, definition in live.items():
            if key not in committed:
                records.append(
                    _not_in_source_control_record(definition, workspace, reason)
                )
    else:
        for key, definition in committed.items():
            counterpart = live.get(key)
            issues = (
                (_unresolved_issue(counterpart, workspace, reason),)
                if counterpart is not None
                else ()
            )
            records.append(record_for(definition, workspace=None, issues=issues))
        for key, definition in live.items():
            counterpart = committed.get(key)
            issues = (
                (_unresolved_issue(counterpart, workspace, reason),)
                if counterpart is not None
                else ()
            )
            # Scoped by workspace, so an unmerged live record keeps an id of
            # its own instead of colliding with the repository one.
            records.append(
                record_for(
                    definition,
                    workspace=workspace,
                    scoped=counterpart is not None,
                    issues=issues,
                )
            )

    if catalog is not None:
        records.extend(records_from_catalog(catalog, workspace))

    # P0 order, then id. Alphabetical would interleave the SQL artifacts with
    # the repository ones, and the P0 list is the order every report uses.
    order = {artifact: index for index, artifact in enumerate(P0Artifact)}
    return DiscoveryRecordSet(
        records=tuple(
            sorted(records, key=lambda r: (order[r.artifact], r.identity.scoped_id))
        )
    )


def coverage(records: DiscoveryRecordSet) -> Dict[str, dict]:
    """Per-P0-artifact coverage: how many records, and from which sources.

    Every P0 artifact appears, including the ones with no records, because
    "none were found" is a finding and an absent key is not.
    """
    summary: Dict[str, dict] = {}
    for artifact in P0Artifact:
        found = records.of_artifact(artifact)
        sources: Dict[str, int] = {}
        for record in found:
            for source in record.sources:
                sources[source.value] = sources.get(source.value, 0) + 1
        summary[artifact.value] = {
            "records": len(found),
            "primary_source": strategy_for(artifact).primary_source.value,
            "sources": dict(sorted(sources.items())),
            "with_drift": sum(
                1
                for r in found
                if r.runtime is not None and r.runtime.drift is DriftStatus.DRIFTED
            ),
            "unresolved": sum(
                1
                for r in found
                for issue in r.issues
                if "may be the same artifact" in issue.message
            ),
        }
    return summary
