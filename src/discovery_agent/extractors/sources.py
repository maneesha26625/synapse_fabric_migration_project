"""Artifact sources: where an extractor's bytes actually come from.

This is the seam that keeps the framework independent of Git. An extractor
asks its source for content and provenance; it never opens a file, never
knows a repository root, and never learns whether the bytes came from a clone
or from an API response.

Only ``RepositoryArtifactSource`` is implemented. A future
``SynapseWorkspaceSource`` or ``AzureResourceSource`` implements the same three
methods and every existing extractor keeps working.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from discovery_agent.acquisition.models import RepositorySource
from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.errors import MalformedArtifactError
from discovery_agent.extractors.models import ExtractionProvenance, SourceType


class ArtifactSource(ABC):
    """Supplies artifact content and provenance to extractors."""

    #: Which kind of source this is; extractors declare what they support.
    source_type: SourceType

    @abstractmethod
    def provenance_for(self, artifact: DetectedArtifact) -> ExtractionProvenance:
        """Describe where this artifact's content comes from."""

    @abstractmethod
    def read_text(self, artifact: DetectedArtifact) -> str:
        """Return the artifact's raw content as text.

        Raises MalformedArtifactError if the content cannot be retrieved.
        """

    def read_json(self, artifact: DetectedArtifact) -> Any:
        """Return the artifact's content parsed as JSON.

        Implemented on top of ``read_text`` so a new source only has to provide
        one method. Raises MalformedArtifactError on invalid JSON.
        """
        text = self.read_text(artifact)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedArtifactError(artifact.source_path, f"invalid json: {exc}")


class RepositoryArtifactSource(ArtifactSource):
    """Reads artifact content from the local repository snapshot.

    Never contacts the git host: acquisition already produced the snapshot,
    and the walker already enumerated it. ``repository`` is optional and is
    used only to enrich provenance with the URL, ref, and commit SHA that
    acquisition recorded — this class re-derives none of it.
    """

    source_type = SourceType.REPOSITORY

    def __init__(self, root: Path, repository: Optional[RepositorySource] = None) -> None:
        self.root = Path(root)
        self.repository = repository

    def path_of(self, artifact: DetectedArtifact) -> Path:
        return self.root / artifact.source_path

    def provenance_for(self, artifact: DetectedArtifact) -> ExtractionProvenance:
        repository = self.repository
        return ExtractionProvenance(
            source_type=SourceType.REPOSITORY,
            source_format=artifact.source_format,
            source_path=artifact.source_path,
            sha256=artifact.sha256,
            repository_url=repository.repository_url if repository else None,
            ref=repository.ref if repository else None,
            commit_sha=repository.commit_sha if repository else None,
        )

    def read_text(self, artifact: DetectedArtifact) -> str:
        """Read the file, tolerating the UTF-8 BOM that Synapse sometimes writes."""
        try:
            with open(self.path_of(artifact), "r", encoding="utf-8-sig") as handle:
                return handle.read()
        except OSError as exc:
            raise MalformedArtifactError(
                artifact.source_path, f"unreadable: {exc.strerror}"
            )
        except UnicodeDecodeError as exc:
            raise MalformedArtifactError(artifact.source_path, f"not utf-8 text: {exc}")
