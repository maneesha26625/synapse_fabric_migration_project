"""Artifact detection.

Takes the repository walker's file-level inventory and decides which files are
Azure Synapse artifacts, which are ordinary repository content, and which
cannot be determined. Deterministic and rule-based: path, file name,
extension, and JSON structure only.

The detector answers "what is this file?" — not "what does it do?" or "what
does it depend on?". Those belong to the extractors, a stage later.
"""

from discovery_agent.artifacts.detector import ArtifactDetector, detect_artifacts
from discovery_agent.artifacts.models import (
    ArtifactCategory,
    ArtifactDetectionResult,
    DetectedArtifact,
    DetectionEvidence,
    DiscoverySource,
    Signal,
    SignalType,
)
from discovery_agent.artifacts.rules import (
    EXTENSION_RULES,
    NON_SYNAPSE_RULES,
    STRUCTURE_RULES,
    SYNAPSE_FOLDERS,
    Strength,
)

__all__ = [
    "ArtifactCategory",
    "ArtifactDetectionResult",
    "ArtifactDetector",
    "DetectedArtifact",
    "DetectionEvidence",
    "DiscoverySource",
    "EXTENSION_RULES",
    "NON_SYNAPSE_RULES",
    "STRUCTURE_RULES",
    "SYNAPSE_FOLDERS",
    "Signal",
    "SignalType",
    "Strength",
    "detect_artifacts",
]
