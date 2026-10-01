"""Structured results of artifact detection.

The detector answers one question per file: "what is this?" It never answers
"what does it do?" — that belongs to the extractors, a stage later. So these
models carry identity and evidence, never artifact contents.

Nothing here serializes itself to disk; the writers handle that later.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Tuple


class ArtifactCategory(str, Enum):
    """What kind of answer the detector arrived at."""

    SYNAPSE = "synapse"  # a recognized Synapse artifact
    NON_SYNAPSE = "non_synapse"  # confidently something else (docs, ARM, data)
    UNSUPPORTED = "unsupported"  # Synapse-adjacent, but no rule for it yet
    UNKNOWN = "unknown"  # could not be determined; never guessed


class SignalType(str, Enum):
    """The kind of observation a piece of evidence records."""

    PATH = "path"
    JSON_STRUCTURE = "json_structure"
    EXTENSION = "extension"
    FILE_NAME = "file_name"
    PATH_CONFLICT = "path_conflict"
    PARSE = "parse"


class DiscoverySource(str, Enum):
    """Which signals actually supported the classification."""

    PATH_AND_STRUCTURE = "path_and_structure"
    STRUCTURE = "structure"
    PATH = "path"
    EXTENSION = "extension"
    NONE = "none"


@dataclass(frozen=True)
class Signal:
    """One structured observation about a file.

    ``value`` is a precise, machine-comparable token (a folder name, a JSON
    key path, an extension) — deliberately not a free-text explanation.
    """

    type: SignalType
    value: str

    def to_dict(self) -> dict:
        return {"type": self.type.value, "value": self.value}


@dataclass(frozen=True)
class DetectionEvidence:
    """Why a file was classified the way it was.

    Named DetectionEvidence to stay distinct from ``models.Evidence``, which
    records extraction provenance (file and line) rather than classification.
    """

    signals: Tuple[Signal, ...] = ()

    def to_dict(self) -> dict:
        return {"signals": [s.to_dict() for s in self.signals]}

    def of_type(self, signal_type: SignalType) -> Tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.type is signal_type)


@dataclass(frozen=True)
class DetectedArtifact:
    """The detector's verdict on one file from the repository walk."""

    artifact_type: str  # AssetType value, or the category value when not Synapse
    artifact_name: str
    source_path: str  # repository-relative, "/"-separated
    source_format: str
    confidence: float
    discovery_source: DiscoverySource
    category: ArtifactCategory
    evidence: DetectionEvidence
    sha256: Optional[str] = None  # links the verdict back to exact file bytes

    @property
    def is_synapse_artifact(self) -> bool:
        return self.category is ArtifactCategory.SYNAPSE

    def to_dict(self) -> dict:
        return {
            "artifact_type": self.artifact_type,
            "artifact_name": self.artifact_name,
            "source_path": self.source_path,
            "source_format": self.source_format,
            "confidence": self.confidence,
            "discovery_source": self.discovery_source.value,
            "category": self.category.value,
            "evidence": self.evidence.to_dict(),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ArtifactDetectionResult:
    """Every walked file, classified. Ordered by source path."""

    root: Path
    artifacts: Tuple[DetectedArtifact, ...]

    def by_category(self, category: ArtifactCategory) -> Tuple[DetectedArtifact, ...]:
        return tuple(a for a in self.artifacts if a.category is category)

    @property
    def synapse_artifacts(self) -> Tuple[DetectedArtifact, ...]:
        return self.by_category(ArtifactCategory.SYNAPSE)

    @property
    def unknown(self) -> Tuple[DetectedArtifact, ...]:
        return self.by_category(ArtifactCategory.UNKNOWN)

    @property
    def unsupported(self) -> Tuple[DetectedArtifact, ...]:
        return self.by_category(ArtifactCategory.UNSUPPORTED)

    @property
    def non_synapse(self) -> Tuple[DetectedArtifact, ...]:
        return self.by_category(ArtifactCategory.NON_SYNAPSE)

    def counts_by_type(self) -> Dict[str, int]:
        """Artifact type -> count, for every classified file."""
        counts: Dict[str, int] = {}
        for artifact in self.artifacts:
            counts[artifact.artifact_type] = counts.get(artifact.artifact_type, 0) + 1
        return dict(sorted(counts.items()))

    def synapse_counts_by_type(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for artifact in self.synapse_artifacts:
            counts[artifact.artifact_type] = counts.get(artifact.artifact_type, 0) + 1
        return dict(sorted(counts.items()))
