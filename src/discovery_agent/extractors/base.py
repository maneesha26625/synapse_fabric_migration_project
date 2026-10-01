"""The extractor contract every artifact extractor implements.

An extractor declares what it handles and implements one method. Everything
else — selecting it, giving it content, catching its failures, ordering its
results — is the framework's job.

    class PipelineExtractor(Extractor[PipelineDefinition]):
        name = "pipeline"
        version = "1.0.0"
        supported_types = (AssetType.PIPELINE,)
        supported_sources = (SourceType.REPOSITORY,)

        def extract(self, artifact, context):
            document = context.source.read_json(artifact)
            ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, Optional, Tuple

from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.config import DiscoveryConfig
from discovery_agent.extractors.models import (
    ExtractionIssue,
    ExtractionResult,
    ExtractionStatus,
    ExtractorInfo,
    SourceType,
    T,
    resolve_asset_type,
)
from discovery_agent.extractors.sources import ArtifactSource
from discovery_agent.models import AssetType, asset_id


@dataclass(frozen=True)
class ExtractionContext:
    """Everything an extractor is given besides the artifact itself.

    Holding the source here rather than passing a path is what lets the same
    extractor run against a Git clone today and a live workspace later.
    """

    source: ArtifactSource
    config: Optional[DiscoveryConfig] = None


class Extractor(ABC, Generic[T]):
    """Extracts the contents of one family of Synapse artifacts.

    Subclasses declare ``supported_types`` and ``supported_sources`` and
    implement ``extract``. They must not walk the filesystem, contact a
    network service, or decide anything about migration.
    """

    #: Stable identifier recorded in every result's provenance.
    name: str = "extractor"
    #: Bumped when extraction behaviour changes, so results can be compared.
    version: str = "0.0.0"
    #: Artifact types this extractor handles.
    supported_types: Tuple[AssetType, ...] = ()
    #: Sources this extractor can read from.
    supported_sources: Tuple[SourceType, ...] = (SourceType.REPOSITORY,)

    @property
    def info(self) -> ExtractorInfo:
        return ExtractorInfo(name=self.name, version=self.version)

    def supports(self, artifact_type: AssetType, source_type: SourceType) -> bool:
        return artifact_type in self.supported_types and source_type in self.supported_sources

    @abstractmethod
    def extract(
        self, artifact: DetectedArtifact, context: ExtractionContext
    ) -> ExtractionResult[T]:
        """Extract one artifact's contents and the references it makes.

        Raise on failure or return a FAILED result — either is honest. What an
        extractor must never do is return SUCCESS with an empty model when the
        source could not be understood.
        """

    # -- helpers for building well-formed results -------------------------

    def result(
        self,
        artifact: DetectedArtifact,
        context: ExtractionContext,
        status: ExtractionStatus,
        content: Optional[T] = None,
        references: Tuple = (),
        warnings: Tuple[ExtractionIssue, ...] = (),
        errors: Tuple[ExtractionIssue, ...] = (),
    ) -> ExtractionResult[T]:
        """Assemble a result with identity and provenance already filled in.

        Subclasses are not required to use this, but it keeps every extractor
        from re-deriving artifact ids and provenance by hand.
        """
        asset_type = resolve_asset_type(artifact)
        if asset_type is None:
            raise ValueError(
                f"{artifact.source_path} is not a Synapse artifact "
                f"(type={artifact.artifact_type!r})"
            )
        return ExtractionResult(
            artifact_id=asset_id(asset_type, artifact.artifact_name),
            artifact_type=asset_type,
            artifact_name=artifact.artifact_name,
            status=status,
            provenance=context.source.provenance_for(artifact),
            extractor=self.info,
            content=content,
            references=tuple(references),
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    def success(self, artifact, context, content: T, references=(), warnings=()):
        """A complete extraction. PARTIAL is chosen automatically if warned."""
        status = ExtractionStatus.PARTIAL if warnings else ExtractionStatus.SUCCESS
        return self.result(
            artifact, context, status, content=content,
            references=references, warnings=warnings,
        )

    def failure(self, artifact, context, errors, warnings=()):
        """A failed extraction. Carries no content, by contract."""
        return self.result(
            artifact, context, ExtractionStatus.FAILED,
            errors=tuple(errors), warnings=tuple(warnings),
        )
