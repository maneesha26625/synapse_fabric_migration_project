"""The artifact detector.

Consumes the repository walker's output and decides what each file is. It
never walks the filesystem itself, never reaches the network, and never uses
a model: classification is a fixed set of rules over the path, the file name,
the extension, and the JSON shape.

Structure is authoritative and the folder corroborates it. A folder name on
its own never classifies a file — otherwise a README dropped into
``notebook/`` would be reported as a notebook, and in a migration tool a false
positive is worse than an honest "unknown".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

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
    NON_SYNAPSE_EXTENSIONS,
    NON_SYNAPSE_FILE_NAMES,
    NON_SYNAPSE_RULES,
    STRUCTURE_RULES,
    ExtensionRule,
    NonSynapseRule,
    Strength,
    StructureRule,
    folder_asset_type,
)
from discovery_agent.config import DiscoveryConfig
from discovery_agent.errors import ParseError
from discovery_agent.models import AssetType
from discovery_agent.repository import DiscoveredFile, WalkResult

# Fixed confidence levels. Deterministic by construction: the same evidence
# always yields the same number.
CONFIDENCE_STRUCTURE_AND_PATH = 1.0
CONFIDENCE_STRUCTURE_ONLY = 0.8
CONFIDENCE_STRUCTURE_PATH_CONFLICT = 0.6
CONFIDENCE_WEAK_WITH_PATH = 0.7
CONFIDENCE_NONE = 0.0

FORMAT_UNKNOWN_JSON = "unknown_json"
FORMAT_UNPARSEABLE_JSON = "unparseable_json"
FORMAT_UNKNOWN = "unknown"
FORMAT_DOCUMENT_OR_ASSET = "repository_file"
FORMAT_UNSUPPORTED_SYNAPSE = "unsupported_synapse_file"


class ArtifactDetector:
    """Classifies the files found by the repository walker."""

    def __init__(
        self,
        config: Optional[DiscoveryConfig] = None,
        structure_rules: Sequence[StructureRule] = STRUCTURE_RULES,
        extension_rules: Sequence[ExtensionRule] = EXTENSION_RULES,
        non_synapse_rules: Sequence[NonSynapseRule] = NON_SYNAPSE_RULES,
    ) -> None:
        self.config = config
        self.structure_rules = tuple(structure_rules)
        self.extension_rules = tuple(extension_rules)
        self.non_synapse_rules = tuple(non_synapse_rules)

    @property
    def on_parse_error(self) -> str:
        return self.config.on_parse_error if self.config is not None else "skip"

    def detect(self, walk_result: WalkResult) -> ArtifactDetectionResult:
        """Classify every file in a walk result."""
        detected = [
            self.detect_file(discovered, walk_result.root)
            for discovered in walk_result.files
        ]
        return ArtifactDetectionResult(
            root=walk_result.root,
            artifacts=tuple(sorted(detected, key=lambda a: a.source_path)),
        )

    def detect_file(self, discovered: DiscoveredFile, root: Path) -> DetectedArtifact:
        """Classify one walked file."""
        folder = self._parent_folder(discovered.relative_path)
        is_synapse_folder, folder_type = folder_asset_type(folder) if folder else (False, None)

        path_signal = (
            Signal(SignalType.PATH, f"{folder}/") if is_synapse_folder else None
        )

        if discovered.extension == ".json":
            return self._detect_json(
                discovered, root, path_signal, folder_type, is_synapse_folder
            )
        return self._detect_non_json(
            discovered, path_signal, folder_type, is_synapse_folder
        )

    # -- JSON ---------------------------------------------------------------

    def _detect_json(
        self,
        discovered: DiscoveredFile,
        root: Path,
        path_signal: Optional[Signal],
        folder_type: Optional[AssetType],
        is_synapse_folder: bool,
    ) -> DetectedArtifact:
        document, parse_signal = self._load_json(discovered, root)

        if document is None:
            return self._verdict(
                discovered,
                artifact_type=ArtifactCategory.UNKNOWN.value,
                name=Path(discovered.file_name).stem,
                source_format=FORMAT_UNPARSEABLE_JSON,
                confidence=CONFIDENCE_NONE,
                source=DiscoverySource.NONE,
                category=ArtifactCategory.UNKNOWN,
                signals=self._compact(path_signal, parse_signal),
            )

        for rule in self.structure_rules:
            if not self._safe(rule.predicate, document):
                continue
            return self._classify_by_structure(
                discovered, document, rule, path_signal, folder_type
            )

        for negative in self.non_synapse_rules:
            if self._safe(negative.predicate, document):
                return self._verdict(
                    discovered,
                    artifact_type=ArtifactCategory.NON_SYNAPSE.value,
                    name=self._name_of(document, discovered),
                    source_format=negative.source_format,
                    confidence=CONFIDENCE_STRUCTURE_ONLY,
                    source=DiscoverySource.STRUCTURE,
                    category=ArtifactCategory.NON_SYNAPSE,
                    signals=self._compact(
                        path_signal,
                        Signal(SignalType.JSON_STRUCTURE, negative.signal_value),
                    ),
                )

        # No rule matched. A Synapse folder is a hint, never a classification.
        category = (
            ArtifactCategory.UNSUPPORTED if is_synapse_folder else ArtifactCategory.UNKNOWN
        )
        source_format = (
            FORMAT_UNSUPPORTED_SYNAPSE if is_synapse_folder else FORMAT_UNKNOWN_JSON
        )
        return self._verdict(
            discovered,
            artifact_type=category.value,
            name=self._name_of(document, discovered),
            source_format=source_format,
            confidence=CONFIDENCE_NONE,
            source=DiscoverySource.NONE,
            category=category,
            signals=self._compact(
                path_signal,
                Signal(SignalType.JSON_STRUCTURE, "no matching artifact structure"),
            ),
        )

    def _classify_by_structure(
        self,
        discovered: DiscoveredFile,
        document: dict,
        rule: StructureRule,
        path_signal: Optional[Signal],
        folder_type: Optional[AssetType],
    ) -> DetectedArtifact:
        """Combine a structural match with whatever the folder says."""
        structure_signal = Signal(SignalType.JSON_STRUCTURE, rule.signal_value)
        path_agrees = folder_type is rule.asset_type
        path_conflicts = folder_type is not None and folder_type is not rule.asset_type

        if path_agrees:
            confidence = (
                CONFIDENCE_STRUCTURE_AND_PATH
                if rule.strength is Strength.STRONG
                else CONFIDENCE_WEAK_WITH_PATH
            )
            signals = self._compact(path_signal, structure_signal)
            return self._synapse_verdict(
                discovered,
                document,
                rule,
                confidence,
                DiscoverySource.PATH_AND_STRUCTURE,
                signals,
            )

        if rule.strength is Strength.WEAK:
            # A generic shape with no corroborating folder proves nothing.
            signals = self._compact(
                path_signal,
                structure_signal,
                Signal(
                    SignalType.PARSE,
                    f"weak structure for {rule.asset_type.value} without matching folder",
                ),
            )
            return self._verdict(
                discovered,
                artifact_type=ArtifactCategory.UNKNOWN.value,
                name=Path(discovered.file_name).stem,
                source_format=FORMAT_UNKNOWN_JSON,
                confidence=CONFIDENCE_NONE,
                source=DiscoverySource.NONE,
                category=ArtifactCategory.UNKNOWN,
                signals=signals,
            )

        if path_conflicts:
            # Structure wins, but the disagreement is recorded and the
            # confidence drops so a reviewer can find these.
            signals = self._compact(
                path_signal,
                structure_signal,
                Signal(
                    SignalType.PATH_CONFLICT,
                    f"folder={folder_type.value}, structure={rule.asset_type.value}",
                ),
            )
            return self._synapse_verdict(
                discovered,
                document,
                rule,
                CONFIDENCE_STRUCTURE_PATH_CONFLICT,
                DiscoverySource.STRUCTURE,
                signals,
            )

        return self._synapse_verdict(
            discovered,
            document,
            rule,
            CONFIDENCE_STRUCTURE_ONLY,
            DiscoverySource.STRUCTURE,
            self._compact(structure_signal),
        )

    # -- non-JSON -----------------------------------------------------------

    def _detect_non_json(
        self,
        discovered: DiscoveredFile,
        path_signal: Optional[Signal],
        folder_type: Optional[AssetType],
        is_synapse_folder: bool,
    ) -> DetectedArtifact:
        extension_signal = Signal(
            SignalType.EXTENSION, discovered.extension or "(none)"
        )

        for rule in self.extension_rules:
            if discovered.extension not in rule.extensions:
                continue
            if folder_type is rule.asset_type:
                return self._verdict(
                    discovered,
                    artifact_type=rule.asset_type.value,
                    name=Path(discovered.file_name).stem,
                    source_format=rule.source_format,
                    confidence=CONFIDENCE_WEAK_WITH_PATH,
                    source=DiscoverySource.PATH_AND_STRUCTURE,
                    category=ArtifactCategory.SYNAPSE,
                    signals=self._compact(path_signal, extension_signal),
                )
            break

        if (
            discovered.extension in NON_SYNAPSE_EXTENSIONS
            or discovered.file_name in NON_SYNAPSE_FILE_NAMES
        ):
            signal = (
                Signal(SignalType.FILE_NAME, discovered.file_name)
                if discovered.file_name in NON_SYNAPSE_FILE_NAMES
                else extension_signal
            )
            return self._verdict(
                discovered,
                artifact_type=ArtifactCategory.NON_SYNAPSE.value,
                name=Path(discovered.file_name).stem,
                source_format=FORMAT_DOCUMENT_OR_ASSET,
                confidence=CONFIDENCE_STRUCTURE_ONLY,
                source=DiscoverySource.EXTENSION,
                category=ArtifactCategory.NON_SYNAPSE,
                signals=self._compact(path_signal, signal),
            )

        category = (
            ArtifactCategory.UNSUPPORTED if is_synapse_folder else ArtifactCategory.UNKNOWN
        )
        source_format = (
            FORMAT_UNSUPPORTED_SYNAPSE if is_synapse_folder else FORMAT_UNKNOWN
        )
        return self._verdict(
            discovered,
            artifact_type=category.value,
            name=Path(discovered.file_name).stem,
            source_format=source_format,
            confidence=CONFIDENCE_NONE,
            source=DiscoverySource.NONE,
            category=category,
            signals=self._compact(path_signal, extension_signal),
        )

    # -- internals ----------------------------------------------------------

    def _load_json(
        self, discovered: DiscoveredFile, root: Path
    ) -> Tuple[Optional[dict], Optional[Signal]]:
        """Parse a walked JSON file, honoring the configured error policy.

        Synapse writes some artifacts with a UTF-8 BOM, so utf-8-sig is used.
        """
        path = Path(root) / discovered.relative_path
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                document = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return None, self._parse_failure(discovered, f"invalid json: {exc}")
        except OSError as exc:
            return None, self._parse_failure(discovered, f"unreadable: {exc.strerror}")

        if not isinstance(document, dict):
            return None, self._parse_failure(
                discovered, f"json root is {type(document).__name__}, not an object"
            )
        return document, None

    def _parse_failure(self, discovered: DiscoveredFile, reason: str) -> Signal:
        if self.on_parse_error == "fail":
            raise ParseError(discovered.relative_path, reason)
        return Signal(SignalType.PARSE, reason)

    @staticmethod
    def _safe(predicate, document: dict) -> bool:
        """Rule predicates read untrusted JSON; a malformed shape is a no-match."""
        try:
            return bool(predicate(document))
        except (AttributeError, TypeError, KeyError, ValueError):
            return False

    @staticmethod
    def _parent_folder(relative_path: str) -> Optional[str]:
        parts = relative_path.split("/")
        return parts[-2] if len(parts) >= 2 else None

    @staticmethod
    def _name_of(document: dict, discovered: DiscoveredFile) -> str:
        """Artifact name: the document's own name, else the file stem."""
        name = document.get("name")
        if isinstance(name, str) and name:
            return name
        return Path(discovered.file_name).stem

    @staticmethod
    def _compact(*signals: Optional[Signal]) -> Tuple[Signal, ...]:
        return tuple(s for s in signals if s is not None)

    def _synapse_verdict(
        self,
        discovered: DiscoveredFile,
        document: dict,
        rule: StructureRule,
        confidence: float,
        source: DiscoverySource,
        signals: Tuple[Signal, ...],
    ) -> DetectedArtifact:
        return DetectedArtifact(
            artifact_type=rule.asset_type.value,
            artifact_name=self._name_of(document, discovered),
            source_path=discovered.relative_path,
            source_format=rule.source_format,
            confidence=confidence,
            discovery_source=source,
            category=ArtifactCategory.SYNAPSE,
            evidence=DetectionEvidence(signals),
            sha256=discovered.sha256,
        )

    @staticmethod
    def _verdict(
        discovered: DiscoveredFile,
        artifact_type: str,
        name: str,
        source_format: str,
        confidence: float,
        source: DiscoverySource,
        category: ArtifactCategory,
        signals: Tuple[Signal, ...],
    ) -> DetectedArtifact:
        return DetectedArtifact(
            artifact_type=artifact_type,
            artifact_name=name,
            source_path=discovered.relative_path,
            source_format=source_format,
            confidence=confidence,
            discovery_source=source,
            category=category,
            evidence=DetectionEvidence(signals),
            sha256=discovered.sha256,
        )


def detect_artifacts(
    walk_result: WalkResult, config: Optional[DiscoveryConfig] = None
) -> ArtifactDetectionResult:
    """Classify a walk result with the default rule set."""
    return ArtifactDetector(config).detect(walk_result)
