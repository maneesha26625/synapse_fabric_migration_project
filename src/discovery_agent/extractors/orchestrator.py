"""Runs extractors over the artifacts the detector found.

The orchestrator is deliberately dull: iterate in a fixed order, ask the
registry which extractor handles each artifact, call it, and record what came
back. It never scans the repository, never re-reads git, never calls a model,
and never reaches a network service — its only input is the detector's output
and its only reader is the ``ArtifactSource``.

Sequential by design. Extraction is I/O-light and the ordering guarantee is
worth more than the wall-clock; parallelism can be added later behind the same
interface if a real workspace proves it necessary.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from discovery_agent.artifacts.models import (
    ArtifactCategory,
    ArtifactDetectionResult,
    DetectedArtifact,
)
from discovery_agent.errors import (
    DiscoveryError,
    ExtractionContractError,
    NoExtractorError,
    UnsupportedSourceError,
)
from discovery_agent.extractors.base import ExtractionContext, Extractor
from discovery_agent.extractors.models import (
    ExtractionIssue,
    ExtractionResult,
    ExtractionRun,
    ExtractionStatus,
    ExtractorInfo,
    IssueCode,
    resolve_asset_type,
)
from discovery_agent.extractors.registry import ExtractorRegistry
from discovery_agent.models import AssetType, asset_id

FRAMEWORK_INFO = ExtractorInfo(name="framework", version="0.1.0")


class ExtractorOrchestrator:
    """Drives the registry and the extractors over a set of artifacts."""

    def __init__(
        self,
        registry: ExtractorRegistry,
        context: ExtractionContext,
        fail_fast: Optional[bool] = None,
    ) -> None:
        """``fail_fast`` defaults to the run's existing ``on_parse_error`` policy."""
        self.registry = registry
        self.context = context
        if fail_fast is None:
            config = context.config
            fail_fast = bool(config is not None and config.on_parse_error == "fail")
        self.fail_fast = fail_fast

    def run(self, artifacts: Iterable[DetectedArtifact]) -> ExtractionRun:
        """Extract every Synapse artifact in the input, in source-path order.

        Files the detector classified as non-Synapse, unknown, or unsupported
        are not artifacts, so they are not the extraction stage's business and
        produce no result at all.
        """
        candidates = sorted(
            (a for a in artifacts if a.category is ArtifactCategory.SYNAPSE),
            key=lambda a: a.source_path,
        )
        results: List[ExtractionResult] = [
            self.extract_one(artifact) for artifact in candidates
        ]
        return ExtractionRun(results=tuple(results))

    def run_detection(self, detection: ArtifactDetectionResult) -> ExtractionRun:
        """Convenience for the normal pipeline: detector output straight in."""
        return self.run(detection.artifacts)

    def extract_one(self, artifact: DetectedArtifact) -> ExtractionResult:
        """Extract one artifact, converting every failure into a typed result."""
        asset_type = resolve_asset_type(artifact)
        if asset_type is None:
            return self._skipped(
                artifact,
                None,
                IssueCode.NO_EXTRACTOR,
                f"{artifact.artifact_type!r} is not a Synapse artifact type",
            )

        source_type = self.context.source.source_type
        try:
            extractor = self.registry.require(asset_type, source_type)
        except UnsupportedSourceError as exc:
            if self.fail_fast:
                raise
            return self._skipped(
                artifact, asset_type, IssueCode.UNSUPPORTED_SOURCE, str(exc)
            )
        except NoExtractorError as exc:
            if self.fail_fast:
                raise
            return self._skipped(
                artifact, asset_type, IssueCode.NO_EXTRACTOR, str(exc)
            )

        return self._invoke(extractor, artifact, asset_type)

    # -- internals ---------------------------------------------------------

    def _invoke(
        self, extractor: Extractor, artifact: DetectedArtifact, asset_type: AssetType
    ) -> ExtractionResult:
        """Call an extractor and make sure a result comes back either way."""
        try:
            result = extractor.extract(artifact, self.context)
        except ExtractionContractError:
            # An extractor broke the framework's contract. That is a bug in the
            # extractor, not bad source data, so it is never swallowed.
            raise
        except DiscoveryError as exc:
            if self.fail_fast:
                raise
            return self._failed(
                artifact, asset_type, extractor, IssueCode.MALFORMED_ARTIFACT, str(exc)
            )
        except Exception as exc:  # an extractor bug must not end the whole run
            if self.fail_fast:
                raise
            return self._failed(
                artifact,
                asset_type,
                extractor,
                IssueCode.EXTRACTION_FAILURE,
                f"{type(exc).__name__}: {exc}",
            )

        if not isinstance(result, ExtractionResult):
            raise ExtractionContractError(
                f"extractor {extractor.name!r} returned "
                f"{type(result).__name__}, not an ExtractionResult"
            )
        return result

    def _skipped(
        self,
        artifact: DetectedArtifact,
        asset_type: Optional[AssetType],
        code: IssueCode,
        message: str,
    ) -> ExtractionResult:
        """A gap in coverage, recorded rather than hidden."""
        return ExtractionResult(
            artifact_id=(
                asset_id(asset_type, artifact.artifact_name)
                if asset_type
                else f"unclassified://{artifact.source_path}"
            ),
            artifact_type=asset_type,
            artifact_name=artifact.artifact_name,
            status=ExtractionStatus.SKIPPED,
            provenance=self.context.source.provenance_for(artifact),
            extractor=FRAMEWORK_INFO,
            errors=(ExtractionIssue(code, message, artifact.source_path),),
        )

    def _failed(
        self,
        artifact: DetectedArtifact,
        asset_type: AssetType,
        extractor: Extractor,
        code: IssueCode,
        message: str,
    ) -> ExtractionResult:
        return ExtractionResult(
            artifact_id=asset_id(asset_type, artifact.artifact_name),
            artifact_type=asset_type,
            artifact_name=artifact.artifact_name,
            status=ExtractionStatus.FAILED,
            provenance=self.context.source.provenance_for(artifact),
            extractor=extractor.info,
            errors=(ExtractionIssue(code, message, artifact.source_path),),
        )


def run_extraction(
    detection: ArtifactDetectionResult,
    registry: ExtractorRegistry,
    context: ExtractionContext,
) -> ExtractionRun:
    """Detector output plus a registry and a source, in; extraction results, out."""
    return ExtractorOrchestrator(registry, context).run_detection(detection)
