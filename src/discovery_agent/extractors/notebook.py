"""NotebookExtractor: what is inside a Synapse notebook.

Answers what the notebook contains and which migration-relevant constructs it
uses. Whether any of those survive the move to Fabric is Assessment's
question, and nothing here decides it.

Three layers, so a second notebook format costs one reader rather than a
rewrite:

* ``NotebookReader`` turns a format-specific document into a ``ParsedNotebook``.
  ``SynapseNotebookReader`` is the only implementation; an ``.ipynb`` reader
  would sit beside it, and the layers above would not change.
* ``NotebookCodeScanner`` analyses cell source, which is format-independent.
* ``NotebookExtractor`` assembles the normalized model and the framework result.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.errors import MalformedArtifactError
from discovery_agent.extractors.base import ExtractionContext, Extractor
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionResult,
    IssueCode,
    SourceType,
)
from discovery_agent.extractors.common_models import ConfigEntry
from discovery_agent.extractors.notebook_models import (
    CellOutput,
    CellType,
    ComputeBinding,
    DetectionMethod,
    LanguageSource,
    NotebookCell,
    NotebookDefinition,
    NotebookFormat,
    NotebookLanguage,
    ResourceCategory,
    ResourceReference,
    SessionConfiguration,
)
from discovery_agent.extractors.notebook_scanners import NotebookCodeScanner
from discovery_agent.extractors.synapse_json import (
    classify_reference,
    render_scalar,
    scan,
)
from discovery_agent.models import AssetType, Evidence, asset_id

EXTRACTOR_NAME = "notebook"
EXTRACTOR_VERSION = "1.0.0"
ROOT_PATH = "properties"

# Kernel name -> the language it runs. Declared by the notebook, never guessed
# from what the code looks like.
KERNEL_LANGUAGES: Dict[str, NotebookLanguage] = {
    "synapse_pyspark": NotebookLanguage.PYTHON,
    "python": NotebookLanguage.PYTHON,
    "python3": NotebookLanguage.PYTHON,
    "synapse_spark": NotebookLanguage.SCALA,
    "scala": NotebookLanguage.SCALA,
    "synapse_sparkdotnet": NotebookLanguage.CSHARP,
    "csharp": NotebookLanguage.CSHARP,
    "synapse_sparkr": NotebookLanguage.R,
    "sparkr": NotebookLanguage.R,
    "synapse_sql": NotebookLanguage.SQL,
    "sql": NotebookLanguage.SQL,
}

LANGUAGE_INFO_NAMES: Dict[str, NotebookLanguage] = {
    "python": NotebookLanguage.PYTHON,
    "scala": NotebookLanguage.SCALA,
    "csharp": NotebookLanguage.CSHARP,
    "r": NotebookLanguage.R,
    "sql": NotebookLanguage.SQL,
}

# Notebook properties this extractor understands. Anything else earns a
# warning rather than being dropped in silence.
KNOWN_NOTEBOOK_PROPERTIES = frozenset(
    {
        "cells", "nbformat", "nbformat_minor", "metadata", "bigDataPool",
        "sessionProperties", "folder", "description", "targetSparkConfiguration",
        "annotations",
    }
)

# Notebook metadata keys captured as curated scalars.
METADATA_SCALARS = (
    "saveOutput",
    "sessionKeepAliveTimeout",
    "synapse_widget.version",
    "kernelspec.name",
    "kernelspec.display_name",
    "language_info.name",
)


@dataclass(frozen=True)
class ParsedNotebook:
    """A notebook document after format-specific parsing, before analysis."""

    format: NotebookFormat
    name: Optional[str]
    raw_cells: Tuple[Any, ...]
    metadata: dict = field(default_factory=dict)
    properties: dict = field(default_factory=dict)
    nbformat: Optional[str] = None
    unknown_properties: Tuple[str, ...] = ()


class NotebookReader(ABC):
    """Turns one notebook format into a ParsedNotebook."""

    format: NotebookFormat

    @abstractmethod
    def matches(self, document: dict) -> bool:
        """Whether this reader recognizes the document's shape."""

    @abstractmethod
    def read(self, document: dict) -> ParsedNotebook:
        """Parse the document. Raises ValueError when the shape is wrong."""


class SynapseNotebookReader(NotebookReader):
    """Synapse Git format: nbformat 4 cells nested under ``properties``."""

    format = NotebookFormat.SYNAPSE_NOTEBOOK_JSON

    def matches(self, document: dict) -> bool:
        properties = document.get("properties")
        return isinstance(properties, dict) and isinstance(properties.get("cells"), list)

    def read(self, document: dict) -> ParsedNotebook:
        properties = document["properties"]
        cells = properties["cells"]
        metadata = properties.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}

        major = properties.get("nbformat")
        minor = properties.get("nbformat_minor")
        nbformat = f"{major}.{minor}" if major is not None else None

        unknown = tuple(
            sorted(key for key in properties if key not in KNOWN_NOTEBOOK_PROPERTIES)
        )
        name = document.get("name") if isinstance(document.get("name"), str) else None
        return ParsedNotebook(
            format=self.format,
            name=name,
            raw_cells=tuple(cells),
            metadata=metadata,
            properties=properties,
            nbformat=nbformat,
            unknown_properties=unknown,
        )


DEFAULT_READERS: Tuple[NotebookReader, ...] = (SynapseNotebookReader(),)


class NotebookExtractor(Extractor[NotebookDefinition]):
    """Extracts the contents and constructs of a Synapse notebook artifact."""

    name = EXTRACTOR_NAME
    version = EXTRACTOR_VERSION
    supported_types = (AssetType.NOTEBOOK,)
    # Git and the live workspace serve the identical {name, properties}
    # document, so one extractor reads both. See discovery_agent.synapse.source.
    supported_sources = (SourceType.REPOSITORY, SourceType.SYNAPSE)

    def __init__(
        self,
        readers: Sequence[NotebookReader] = DEFAULT_READERS,
        scanner: Optional[NotebookCodeScanner] = None,
    ) -> None:
        self.readers = tuple(readers)
        self.scanner = scanner or NotebookCodeScanner()

    def extract(
        self, artifact: DetectedArtifact, context: ExtractionContext
    ) -> ExtractionResult[NotebookDefinition]:
        try:
            document = context.source.read_json(artifact)
        except MalformedArtifactError as exc:
            return self._malformed(artifact, context, exc.reason, artifact.source_path)

        if not isinstance(document, dict):
            return self._malformed(
                artifact, context, "notebook json root is not an object", ""
            )

        reader = next((r for r in self.readers if r.matches(document)), None)
        if reader is None:
            return self._malformed(
                artifact,
                context,
                "no notebook reader recognizes this document; expected a "
                "properties.cells list",
                ROOT_PATH,
            )

        try:
            parsed = reader.read(document)
        except (KeyError, TypeError, ValueError) as exc:
            return self._malformed(
                artifact, context, f"notebook structure is unusable: {exc}", ROOT_PATH
            )

        warnings: List[ExtractionIssue] = []
        for unknown in parsed.unknown_properties:
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"notebook property {unknown!r} is not modelled; it was not "
                    f"extracted",
                    f"{ROOT_PATH}.{unknown}",
                )
            )

        name = parsed.name or artifact.artifact_name
        language, language_source = self._notebook_language(parsed.metadata, warnings)
        cells = self._parse_cells(parsed, language, warnings)

        definition = NotebookDefinition(
            name=name,
            format=parsed.format,
            language=language,
            language_source=language_source,
            description=self._optional_str(parsed.properties.get("description")),
            nbformat=parsed.nbformat,
            kernel=self._kernel_name(parsed.metadata),
            save_output=parsed.metadata.get("saveOutput")
            if isinstance(parsed.metadata.get("saveOutput"), bool)
            else None,
            folder=self._folder_name(parsed.properties.get("folder")),
            compute=self._compute(parsed),
            session=self._session(parsed),
            metadata=self._metadata_entries(parsed.metadata),
            cells=cells,
            references=self._notebook_references(parsed, artifact, name, warnings),
            resources=self._notebook_resources(parsed),
        )

        return self.success(
            artifact,
            context,
            definition,
            references=definition.references,
            warnings=tuple(warnings),
        )

    # -- language ----------------------------------------------------------

    @staticmethod
    def _kernel_name(metadata: dict) -> Optional[str]:
        kernelspec = metadata.get("kernelspec")
        if isinstance(kernelspec, dict) and isinstance(kernelspec.get("name"), str):
            return kernelspec["name"]
        return None

    def _notebook_language(
        self, metadata: dict, warnings: List[ExtractionIssue]
    ) -> Tuple[NotebookLanguage, LanguageSource]:
        """Take the notebook's declared language. Never infer one from code."""
        kernel = self._kernel_name(metadata)
        if kernel and kernel.lower() in KERNEL_LANGUAGES:
            return KERNEL_LANGUAGES[kernel.lower()], LanguageSource.KERNELSPEC

        language_info = metadata.get("language_info")
        if isinstance(language_info, dict):
            declared = language_info.get("name")
            if isinstance(declared, str) and declared.lower() in LANGUAGE_INFO_NAMES:
                return (
                    LANGUAGE_INFO_NAMES[declared.lower()],
                    LanguageSource.LANGUAGE_INFO,
                )

        warnings.append(
            ExtractionIssue(
                IssueCode.UNSUPPORTED_CONSTRUCT,
                "notebook declares no recognizable language; kernelspec and "
                "language_info were absent or unknown",
                f"{ROOT_PATH}.metadata",
            )
        )
        return NotebookLanguage.UNKNOWN, LanguageSource.UNKNOWN

    # -- cells -------------------------------------------------------------

    def _parse_cells(
        self,
        parsed: ParsedNotebook,
        notebook_language: NotebookLanguage,
        warnings: List[ExtractionIssue],
    ) -> Tuple[NotebookCell, ...]:
        """Parse every cell, in source order. Cell order is never sorted."""
        cells: List[NotebookCell] = []
        for index, raw in enumerate(parsed.raw_cells):
            location = f"{ROOT_PATH}.cells[{index}].source"
            if not isinstance(raw, dict):
                warnings.append(
                    ExtractionIssue(
                        IssueCode.MALFORMED_ARTIFACT,
                        "cell is not an object; it was not extracted",
                        f"{ROOT_PATH}.cells[{index}]",
                    )
                )
                continue
            cells.append(
                self._parse_cell(raw, index, location, notebook_language, warnings)
            )
        return tuple(cells)

    def _parse_cell(
        self,
        raw: dict,
        index: int,
        location: str,
        notebook_language: NotebookLanguage,
        warnings: List[ExtractionIssue],
    ) -> NotebookCell:
        raw_type = raw.get("cell_type")
        raw_type = raw_type if isinstance(raw_type, str) else ""
        try:
            cell_type = CellType(raw_type)
        except ValueError:
            cell_type = CellType.UNKNOWN
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"cell {index} has type {raw_type!r}, which this extractor does "
                    f"not model; its source was preserved",
                    f"{ROOT_PATH}.cells[{index}]",
                )
            )

        source = self._source_text(raw.get("source"))
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}

        scan_result = (
            self.scanner.scan(source, index, location)
            if cell_type is not CellType.MARKDOWN
            else None
        )
        for unknown in scan_result.unknown_magics if scan_result else ():
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"magic command {unknown!r} in cell {index} is not recognized; "
                    f"it was preserved but not interpreted",
                    location,
                )
            )

        language, language_source = self._cell_language(
            cell_type, metadata, scan_result, notebook_language
        )

        return NotebookCell(
            index=index,
            cell_type=cell_type,
            raw_cell_type=raw_type,
            language=language,
            language_source=language_source,
            source=source,
            line_count=len(source.splitlines()),
            location=location,
            execution_count=raw.get("execution_count")
            if isinstance(raw.get("execution_count"), int)
            else None,
            tags=self._tags(metadata),
            metadata=self._cell_metadata(metadata),
            outputs=self._outputs(raw.get("outputs")),
            magics=scan_result.magics if scan_result else (),
            findings=scan_result.findings if scan_result else (),
            sql_blocks=scan_result.sql_blocks if scan_result else (),
            resources=scan_result.resources if scan_result else (),
            packages=scan_result.packages if scan_result else (),
        )

    @staticmethod
    def _cell_language(cell_type, metadata, scan_result, notebook_language):
        """A cell's language: declared, set by a magic, or inherited.

        Never inferred from the shape of the code.
        """
        if cell_type is CellType.MARKDOWN:
            return NotebookLanguage.MARKDOWN, LanguageSource.CELL_TYPE
        declared = metadata.get("language")
        if isinstance(declared, str):
            try:
                return NotebookLanguage(declared.lower()), LanguageSource.CELL_METADATA
            except ValueError:
                pass
        if scan_result is not None and scan_result.magic_language is not None:
            return scan_result.magic_language, LanguageSource.MAGIC
        if notebook_language is NotebookLanguage.UNKNOWN:
            return NotebookLanguage.UNKNOWN, LanguageSource.UNKNOWN
        return notebook_language, LanguageSource.NOTEBOOK_DEFAULT

    @staticmethod
    def _source_text(source: Any) -> str:
        """Join nbformat's line list into text, byte for byte as authored."""
        if isinstance(source, list):
            return "".join(line for line in source if isinstance(line, str))
        if isinstance(source, str):
            return source
        return ""

    @staticmethod
    def _tags(metadata: dict) -> Tuple[str, ...]:
        tags = metadata.get("tags")
        if not isinstance(tags, list):
            return ()
        return tuple(sorted(t for t in tags if isinstance(t, str)))

    @staticmethod
    def _cell_metadata(metadata: dict) -> Tuple[ConfigEntry, ...]:
        """Cell metadata scalars, sorted; nested objects are not copied."""
        entries = []
        for key in sorted(metadata):
            if key == "tags":
                continue
            value = metadata[key]
            if isinstance(value, (dict, list)):
                continue
            rendered = render_scalar(value)
            if rendered is not None:
                entries.append(ConfigEntry(key, rendered))
        return tuple(entries)

    @staticmethod
    def _outputs(outputs: Any) -> Tuple[CellOutput, ...]:
        """Record that outputs exist and of what kind, never their content."""
        if not isinstance(outputs, list):
            return ()
        captured = []
        for entry in outputs:
            if not isinstance(entry, dict):
                continue
            output_type = entry.get("output_type")
            captured.append(
                CellOutput(
                    output_type=output_type if isinstance(output_type, str) else "unknown",
                    execution_count=entry.get("execution_count")
                    if isinstance(entry.get("execution_count"), int)
                    else None,
                    size_bytes=len(render_scalar(entry) or ""),
                )
            )
        return tuple(captured)

    # -- notebook-level configuration --------------------------------------

    @staticmethod
    def _optional_str(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _folder_name(folder: Any) -> Optional[str]:
        if isinstance(folder, dict) and isinstance(folder.get("name"), str):
            return folder["name"]
        return None

    @staticmethod
    def _compute(parsed: ParsedNotebook) -> Optional[ComputeBinding]:
        """The attached Spark pool, from bigDataPool and a365ComputeOptions."""
        pool = parsed.properties.get("bigDataPool")
        pool = pool if isinstance(pool, dict) else {}
        options = parsed.metadata.get("a365ComputeOptions")
        options = options if isinstance(options, dict) else {}
        if not pool and not options:
            return None

        def _int(source: dict, key: str) -> Optional[int]:
            value = source.get(key)
            return value if isinstance(value, int) else None

        return ComputeBinding(
            pool_name=pool.get("referenceName")
            if isinstance(pool.get("referenceName"), str)
            else (options.get("name") if isinstance(options.get("name"), str) else None),
            reference_type=pool.get("type") if isinstance(pool.get("type"), str) else None,
            spark_version=options.get("sparkVersion")
            if isinstance(options.get("sparkVersion"), str)
            else None,
            node_count=_int(options, "nodeCount"),
            cores=_int(options, "cores"),
            memory=_int(options, "memory"),
            resource_id=options.get("id") if isinstance(options.get("id"), str) else None,
            endpoint=options.get("endpoint")
            if isinstance(options.get("endpoint"), str)
            else None,
        )

    @staticmethod
    def _session(parsed: ParsedNotebook) -> Optional[SessionConfiguration]:
        properties = parsed.properties.get("sessionProperties")
        properties = properties if isinstance(properties, dict) else {}
        conf = properties.get("conf")
        conf = conf if isinstance(conf, dict) else {}

        def _int(key: str) -> Optional[int]:
            value = properties.get(key)
            return value if isinstance(value, int) else None

        def _str(key: str) -> Optional[str]:
            value = properties.get(key)
            return value if isinstance(value, str) else None

        keep_alive = parsed.metadata.get("sessionKeepAliveTimeout")
        session = SessionConfiguration(
            driver_memory=_str("driverMemory"),
            driver_cores=_int("driverCores"),
            executor_memory=_str("executorMemory"),
            executor_cores=_int("executorCores"),
            num_executors=_int("numExecutors"),
            keep_alive_timeout=keep_alive if isinstance(keep_alive, int) else None,
            conf=tuple(
                ConfigEntry(key, render_scalar(conf[key]) or "")
                for key in sorted(conf)
                if render_scalar(conf[key]) is not None
            ),
        )
        return None if session.is_empty else session

    @staticmethod
    def _metadata_entries(metadata: dict) -> Tuple[ConfigEntry, ...]:
        """Curated notebook metadata scalars, by dotted key. Sorted."""
        entries = []
        for key in METADATA_SCALARS:
            node: Any = metadata
            for part in key.split("."):
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(part)
            if node is None or isinstance(node, (dict, list)):
                continue
            rendered = render_scalar(node)
            if rendered is not None:
                entries.append(ConfigEntry(key, rendered))
        return tuple(sorted(entries, key=lambda e: e.key))

    def _notebook_references(
        self,
        parsed: ParsedNotebook,
        artifact: DetectedArtifact,
        name: str,
        warnings: List[ExtractionIssue],
    ) -> Tuple[ArtifactReference, ...]:
        """Structurally declared ``*Reference`` objects outside the cells.

        Reuses the shared Synapse scanner, so a notebook's ``bigDataPool``
        is recognized by exactly the rule that recognizes a pipeline's dataset
        reference. Constructs written in *code* never become references — they
        become findings, because a string in code cannot be proven to be a
        reference.
        """
        outside_cells = {
            key: value for key, value in parsed.properties.items() if key != "cells"
        }
        found, _ = scan(outside_cells, ROOT_PATH)
        notebook_id = asset_id(AssetType.NOTEBOOK, name)

        references = []
        for reference in found:
            target_type, kind, recognized = classify_reference(reference.reference_type)
            if not recognized:
                warnings.append(
                    ExtractionIssue(
                        IssueCode.UNSUPPORTED_CONSTRUCT,
                        f"reference type {reference.reference_type!r} is not "
                        f"modelled; recorded with an unknown target type",
                        reference.location,
                    )
                )
            references.append(
                ArtifactReference(
                    source_artifact_id=notebook_id,
                    source_artifact_type=AssetType.NOTEBOOK,
                    kind=kind,
                    target_type=target_type,
                    target_name=reference.reference_name,
                    location=reference.location,
                    evidence=Evidence(artifact.source_path, None, EXTRACTOR_NAME),
                )
            )
        return tuple(
            sorted(references, key=lambda r: (r.location, r.target_name))
        )

    @staticmethod
    def _notebook_resources(parsed: ParsedNotebook) -> Tuple[ResourceReference, ...]:
        """Workspace endpoints declared in notebook metadata."""
        options = parsed.metadata.get("a365ComputeOptions")
        if not isinstance(options, dict):
            return ()
        endpoint = options.get("endpoint")
        if not isinstance(endpoint, str) or "://" not in endpoint:
            return ()
        return (
            ResourceReference(
                uri=endpoint,
                scheme=endpoint.split("://", 1)[0].lower(),
                category=ResourceCategory.ENDPOINT,
                location=f"{ROOT_PATH}.metadata.a365ComputeOptions.endpoint",
                detection=DetectionMethod.NOTEBOOK_METADATA,
            ),
        )

    def _malformed(
        self,
        artifact: DetectedArtifact,
        context: ExtractionContext,
        reason: str,
        location: str,
    ) -> ExtractionResult[NotebookDefinition]:
        """A malformed notebook fails. It never comes back as an empty notebook."""
        return self.failure(
            artifact,
            context,
            errors=(
                ExtractionIssue(IssueCode.MALFORMED_ARTIFACT, reason, location or None),
            ),
        )
