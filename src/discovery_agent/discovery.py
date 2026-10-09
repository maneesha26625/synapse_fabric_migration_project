"""Orchestration for a single discovery run.

The seam the CLI calls and the tests drive, and the one place the stages are
composed:

    acquire -> walk -> detect -> extract -.
    list live workspace -> extract --------+-> assemble records
    read sql catalog ---------------------'

Every stage already existed and none of them changed shape here. What this
module does is choose which of them to run, in what order, and hand the
results to ``records`` to be reconciled.

Three modes, and the difference between them is the whole point of the
``connections`` argument:

* **Repository-only.** ``run(config)`` with no manager walks the local path in
  ``config.source``. It constructs no connection, acquires no credential and
  needs no network -- a run over an existing clone must keep working on a
  machine with no Azure sign-in at all.
* **Connected to Git.** ``run(config, connections)`` asks the
  ``ConnectionManager`` for the repository snapshot, so provenance carries the
  real commit rather than the path the operator happened to type.
* **Connected to the workspace.** With a Synapse workspace configured, the
  live Artifacts API and the dedicated pool are read as well, and the three
  sources are reconciled into one record set.

Every source degrades on its own. Git unavailable does not stop the workspace
being read; the workspace being unreadable does not stop the repository being
scanned; a paused pool costs the SQL-primary artifacts and nothing else. What
never happens is a source failure turning into a count of zero.

What this module is not allowed to do, and does not: authenticate, construct a
credential, open an ODBC connection, run a git subprocess, or reach a network
by any route other than a connection the caller handed it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from discovery_agent.acquisition.models import RepositorySource
from discovery_agent.artifacts import detect_artifacts
from discovery_agent.artifacts.models import ArtifactDetectionResult
from discovery_agent.config import DiscoveryConfig
from discovery_agent.discovery_models import DiscoveryRecordSet
from discovery_agent.errors import (
    AcquisitionError,
    AzureConnectionError,
    ConfigError,
    SqlDiscoveryError,
)
from discovery_agent.extractors import (
    ExtractionContext,
    ExtractorOrchestrator,
    RepositoryArtifactSource,
    default_registry,
)
from discovery_agent.extractors.models import ExtractionIssue, ExtractionRun, IssueCode
from discovery_agent.records import (
    IdentityResolution,
    SourceDefinition,
    build_records,
    coverage,
    definitions_from_extraction,
    definitions_from_workspace,
    resolve_identity,
)
from discovery_agent.repository import WalkResult, walk_repository
from discovery_agent.source_strategy import P0Artifact
from discovery_agent.sql.result import SqlCatalogDiscovery
from discovery_agent.synapse.models import SynapseWorkspaceDiscovery

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from discovery_agent.connections.manager import ConnectionManager
    from discovery_agent.connections.synapse import WorkspaceRepository


@dataclass(frozen=True)
class DiscoveryRun:
    """What one discovery run found, stage by stage.

    A composition of the existing result objects, not a replacement for any of
    them: ``ExtractionRun``, ``WalkResult``, ``ArtifactDetectionResult``,
    ``SqlCatalogDiscovery`` and ``SynapseWorkspaceDiscovery`` are all held
    as-is. ``records`` is the reconciled view built from them, and is the
    existing ``DiscoveryRecordSet`` rather than a second record model.

    The ``None``s are meaningful and are all different from an empty result:

    * ``snapshot`` -- nothing was acquired; the caller already had the clone.
    * ``synapse`` -- no workspace was configured. A workspace that *was*
      configured and could not be read produces a discovery whose endpoints
      are each marked unreachable, never an empty artifact list.
    * ``catalog`` -- no SQL pool was configured. A pool that was configured and
      returned nothing produces a discovery with no tables and an issue.
    """

    walk: WalkResult
    detection: ArtifactDetectionResult
    extraction: ExtractionRun
    snapshot: Optional[RepositorySource] = None
    catalog: Optional[SqlCatalogDiscovery] = None
    synapse: Optional[SynapseWorkspaceDiscovery] = None
    workspace_extraction: Optional[ExtractionRun] = None
    records: DiscoveryRecordSet = DiscoveryRecordSet()
    resolution: IdentityResolution = IdentityResolution.NOT_APPLICABLE
    resolution_reason: str = ""
    issues: Tuple[ExtractionIssue, ...] = ()

    @property
    def root(self) -> Path:
        """The snapshot that was scanned."""
        return self.walk.root

    @property
    def is_connected(self) -> bool:
        """Whether this run acquired its repository rather than being given one."""
        return self.snapshot is not None

    @property
    def sources_read(self) -> Tuple[str, ...]:
        """Which sources actually produced something, in a fixed order."""
        read = ["repository"]
        if self.synapse is not None and self.synapse.artifact_count:
            read.append("synapse")
        if self.catalog is not None:
            read.append("sql")
        return tuple(read)

    @property
    def all_issues(self) -> Tuple[ExtractionIssue, ...]:
        """Run-level issues followed by each live source's own."""
        issues = list(self.issues)
        if self.synapse is not None:
            issues.extend(self.synapse.all_issues)
        if self.catalog is not None:
            issues.extend(self.catalog.all_issues)
        return tuple(issues)

    def p0_coverage(self) -> Dict[str, dict]:
        """Per-P0-artifact coverage. Every artifact appears, found or not."""
        return coverage(self.records)

    def summary(self) -> dict:
        """Counts only, for a console line or a manifest header.

        Contains no credential and cannot: every value is a count, a path, a
        commit SHA or a name that came from a result object.
        """
        payload = {
            "root": str(self.walk.root),
            "files_walked": self.walk.file_count,
            "files_skipped": len(self.walk.skipped),
            "artifacts_detected": len(self.detection.artifacts),
            "synapse_artifacts": len(self.detection.synapse_artifacts),
            "extraction_by_status": self.extraction.counts_by_status(),
            "extraction_by_type": self.extraction.counts_by_type(),
            "references_observed": len(self.extraction.references),
            "sources_read": list(self.sources_read),
            "identity_resolution": self.resolution.value,
            "records": self.records.summary(),
            "p0_coverage": self.p0_coverage(),
            "issue_count": len(self.all_issues),
        }
        payload["repository"] = (
            self.snapshot.to_dict() if self.snapshot is not None else None
        )
        payload["workspace"] = (
            self.synapse.summary() if self.synapse is not None else None
        )
        payload["catalog"] = (
            self.catalog.summary() if self.catalog is not None else None
        )
        return payload


# --- the legs ----------------------------------------------------------------


def _acquire(
    connections: "ConnectionManager", input_root: Optional[Path]
) -> Optional[RepositorySource]:
    """The repository snapshot, when one is configured.

    Returns None rather than raising when no git repository is configured: a
    run may legitimately be pointed at a local path while still using the
    manager for the workspace and the pool.
    """
    if connections.settings.git is None:
        return None
    connection = connections.git()
    if input_root is None:
        return connection.acquire()
    return connection.acquire(input_root=input_root)


def config_for_snapshot(
    config: DiscoveryConfig, snapshot: RepositorySource
) -> DiscoveryConfig:
    """The run configuration, pointed at what acquisition actually produced.

    Acquisition has to happen first and this is why: ``DiscoveryConfig``
    validates that ``source`` exists, and the snapshot path is not known until
    the clone or the reuse check has completed. Building the configuration up
    front and hoping the directory appears would validate a path that is not
    yet there.

    Every other setting the caller chose -- include, exclude, size limit,
    parse-error policy -- is preserved.
    """
    return replace(config, source=snapshot.local_path)


def discover_catalog(
    connections: "ConnectionManager",
) -> Tuple[Optional[SqlCatalogDiscovery], Optional[ExtractionIssue]]:
    """Tables, views and stored procedures, when a dedicated pool is configured.

    A pass-through to the existing implementation: the connection layer opens
    the session and hands back the unchanged ``DedicatedPoolSource``, which
    runs the unchanged registered catalog queries. Nothing about SQL discovery
    is decided here, and no query is defined here.

    A pool that is configured but cannot be reached -- paused, firewalled, or
    refusing this identity -- costs the three SQL-primary artifacts and
    nothing else. It is returned as ``(None, issue)`` rather than raised,
    because the repository and the live workspace are still perfectly
    discoverable and a whole run should not end on one source.

    The issue is what keeps ``None`` unambiguous: no pool configured returns
    ``(None, None)``, and the two must never read the same.
    """
    if connections.settings.synapse is None:
        return None, None
    if connections.settings.synapse.resolved_database is None:
        return None, None

    database = connections.settings.synapse.resolved_database
    try:
        with connections.sql().source() as source:
            return source.discover(), None
    except (SqlDiscoveryError, AzureConnectionError, ConfigError) as exc:
        return None, ExtractionIssue(
            IssueCode.SOURCE_UNAVAILABLE,
            f"the dedicated sql pool {database!r} could not be read: {exc}. "
            f"Tables, views and stored procedures were not discovered, and no "
            f"claim is made about how many exist",
            database,
        )


def discover_workspace(
    connections: "ConnectionManager",
) -> Tuple[Optional[SynapseWorkspaceDiscovery], Optional[ExtractionRun], Any]:
    """The live workspace: its artifacts, their extraction, and its Git config.

    Returns ``(None, None, None)`` when no workspace is configured, which is
    different from a workspace that was configured and could not be read --
    that produces a discovery whose endpoints are each marked unreachable.

    The extraction is the *same* orchestrator over the *same* registry that
    the repository leg uses. That is the point of ``SynapseArtifactSource``
    being an ``ArtifactSource``: a pipeline read from the API is parsed by
    ``PipelineExtractor``, not by a second implementation that could disagree
    with it.
    """
    if connections.settings.synapse is None:
        return None, None, None

    artifacts = connections.synapse_artifacts()
    discovery = artifacts.discover()

    source = artifacts.source()
    detected = tuple(a.detected() for a in discovery.artifacts)
    extraction = ExtractorOrchestrator(
        default_registry(), ExtractionContext(source=source)
    ).run(detected)

    return discovery, extraction, _workspace_repository(connections)


def _workspace_repository(connections: "ConnectionManager") -> Any:
    """Which repository the workspace is Git-integrated with, per ARM.

    The one authoritative answer to "are these the same artifacts?". Returns
    None when it cannot be obtained -- no Azure subscription configured, or
    the workspace unreadable -- which the identity resolution reports as
    undetermined rather than as a mismatch.
    """
    if connections.settings.azure is None:
        return None
    try:
        return connections.synapse().metadata().repository
    except Exception:  # noqa: BLE001 - an unreadable workspace is a gap, not a crash
        return None


# --- the run -----------------------------------------------------------------


def _no_repository(config: DiscoveryConfig) -> Tuple[
    WalkResult, ArtifactDetectionResult, ExtractionRun, ExtractionIssue
]:
    """The repository stages for a run that has no repository.

    Empty results plus an issue saying why, rather than zeros on their own.
    A workspace-only run genuinely found no committed artifacts because it
    did not look for any, and "none were committed" is a different claim.
    """
    walk = WalkResult(root=config.source, files=(), skipped=())
    return (
        walk,
        ArtifactDetectionResult(root=config.source, artifacts=()),
        ExtractionRun(),
        ExtractionIssue(
            IssueCode.SOURCE_UNAVAILABLE,
            "no repository was scanned, so no committed definition was read. "
            "Artifacts whose primary source is Git are reported only as the "
            "live workspace sees them",
        ),
    )


def run(
    config: DiscoveryConfig,
    connections: Optional["ConnectionManager"] = None,
    input_root: Optional[Path] = None,
    scan_repository: bool = True,
    acquire: bool = True,
    read_workspace: bool = True,
) -> DiscoveryRun:
    """Execute a discovery run against a Synapse estate.

    ``connections`` is optional and is never constructed here. Without it this
    walks ``config.source`` and touches nothing else; with it, the repository
    is acquired through ``GitConnection``, the live workspace is read through
    ``SynapseArtifactsConnection``, and the pool through ``SqlConnection``.

    ``scan_repository=False`` is for a run pointed only at a live workspace.
    It is an explicit argument rather than something inferred from the
    configuration, because "there is no repository" and "the repository path
    is wrong" must not produce the same behaviour: the second is an error and
    stays one.

    The order of the first two steps is fixed by a real constraint rather than
    by preference: acquisition must complete before the configuration is
    validated, because the configuration names a path that acquisition
    creates.

    ``acquire=False`` walks ``config.source`` as it stands, for a repository
    already on disk (a clone made earlier, or an extracted ZIP).
    ``read_workspace=False`` reads the dedicated pool through ``connections``
    but not the live workspace's artifacts: the definitions then come from the
    repository alone, as they do when an operator chose one environment's
    repository as the source.
    """
    snapshot: Optional[RepositorySource] = None
    issues: List[ExtractionIssue] = []

    if connections is not None and scan_repository and acquire:
        # Before config.validate(): the snapshot path does not exist yet.
        try:
            snapshot = _acquire(connections, input_root)
        except AcquisitionError as exc:
            if connections.settings.synapse is None:
                # Git was the only source asked for. There is nothing left to
                # report, so the failure is the result.
                raise
            # A live workspace is still reachable, and reporting what it holds
            # is better than reporting nothing. What the repository would have
            # contributed is stated rather than left as an absence.
            scan_repository = False
            issues.append(
                ExtractionIssue(
                    IssueCode.SOURCE_UNAVAILABLE,
                    f"the repository could not be acquired: {exc}. Artifacts "
                    f"whose primary source is Git are reported only as the "
                    f"live workspace sees them",
                )
            )
        if snapshot is not None:
            config = config_for_snapshot(config, snapshot)

    repository_source: Optional[RepositoryArtifactSource] = None
    if scan_repository:
        config.validate()
        walk = walk_repository(config)
        detection = detect_artifacts(walk, config)

        # The real snapshot, not a stand-in. This is what puts the acquired
        # commit SHA, ref and repository URL into every extraction result's
        # provenance instead of a placeholder.
        repository_source = RepositoryArtifactSource(walk.root, snapshot)
        extraction = ExtractorOrchestrator(
            default_registry(),
            ExtractionContext(source=repository_source, config=config),
        ).run_detection(detection)
    else:
        walk, detection, extraction, absent = _no_repository(config)
        issues.append(absent)

    workspace_discovery: Optional[SynapseWorkspaceDiscovery] = None
    workspace_extraction: Optional[ExtractionRun] = None
    workspace_repository = None
    catalog: Optional[SqlCatalogDiscovery] = None

    if connections is not None:
        if read_workspace:
            workspace_discovery, workspace_extraction, workspace_repository = (
                discover_workspace(connections)
            )
        catalog, catalog_issue = discover_catalog(connections)
        if catalog_issue is not None:
            issues.append(catalog_issue)

    live_definitions: Tuple[SourceDefinition, ...] = ()
    if workspace_extraction is not None and workspace_discovery is not None:
        live_definitions = definitions_from_workspace(
            workspace_extraction, workspace_discovery
        )

    # The committed definitions are re-read only for artifacts the workspace
    # also holds, so a run with no live leg re-reads nothing.
    repository_definitions = definitions_from_extraction(
        extraction,
        detection=detection,
        source=repository_source,
        comparable={d.key for d in live_definitions},
    )

    workspace_name = (
        connections.settings.synapse.workspace_name
        if connections is not None and connections.settings.synapse is not None
        else None
    )
    resolution, reason = resolve_identity(
        workspace_repository,
        snapshot.repository_url if snapshot is not None else None,
    )
    if live_definitions and not resolution.permits_merging:
        issues.append(
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                f"repository and live workspace artifacts were not identified "
                f"with each other: {reason}. Both sets are reported separately",
            )
        )

    records = build_records(
        repository=repository_definitions,
        workspace_artifacts=live_definitions,
        catalog=catalog,
        workspace=workspace_name,
        resolution=resolution,
        reason=reason,
    )

    return DiscoveryRun(
        walk=walk,
        detection=detection,
        extraction=extraction,
        snapshot=snapshot,
        catalog=catalog,
        synapse=workspace_discovery,
        workspace_extraction=workspace_extraction,
        records=records,
        resolution=resolution,
        resolution_reason=reason,
        issues=tuple(issues),
    )
