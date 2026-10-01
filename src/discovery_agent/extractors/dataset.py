"""DatasetExtractor: what a Synapse dataset is and what it points at.

Answers four questions — what the dataset is, what data it addresses, how
that data is reached, and what else it references — and stops there. Whether
any of it maps onto a Fabric item is Assessment's question.

Structure mirrors the pipeline extractor so the two read alike:

* ``synapse_json.scan`` finds ``*Reference`` objects and expressions
  structurally, one shared rule for every artifact type.
* ``_LOCATION_CATEGORIES`` normalizes Synapse's location types.
* ``_DATASET_HANDLERS`` holds one small function per dataset family; adding a
  type is one function and one dict entry, not a new branch in a long method.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.errors import MalformedArtifactError
from discovery_agent.extractors.base import ExtractionContext, Extractor
from discovery_agent.extractors.common_models import (
    ConfigEntry,
    SynapseExpression,
)
from discovery_agent.extractors.dataset_models import (
    DatasetColumn,
    DatasetDefinition,
    DatasetLocation,
    DatasetParameter,
    DatasetResource,
    DatasetSchema,
    DetectionMethod,
    LocationCategory,
    ResourceCategory,
    TextFormatSettings,
)
from discovery_agent.extractors.secret_scanning import (
    SECRET_PROPERTY_NAMES as _SECRET_KEYS,
    scan_secrets,
)
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionResult,
    IssueCode,
    SourceType,
)
from discovery_agent.extractors.synapse_json import (
    classify_reference,
    render_scalar,
    scan,
)
from discovery_agent.models import AssetType, Evidence, asset_id

EXTRACTOR_NAME = "dataset"
EXTRACTOR_VERSION = "1.0.0"
ROOT_PATH = "properties"
TYPE_PATH = "properties.typeProperties"

# Dataset properties this extractor understands. Anything else earns a warning
# rather than disappearing.
KNOWN_DATASET_PROPERTIES = frozenset(
    {
        "type", "typeProperties", "linkedServiceName", "parameters", "schema",
        "structure", "annotations", "folder", "description",
    }
)

# Synapse location type -> normalized category.
_LOCATION_CATEGORIES: Dict[str, LocationCategory] = {
    "AzureBlobFSLocation": LocationCategory.DATA_LAKE,
    "AzureDataLakeStoreLocation": LocationCategory.DATA_LAKE,
    "AzureBlobStorageLocation": LocationCategory.BLOB,
    "HttpServerLocation": LocationCategory.HTTP,
    "FileServerLocation": LocationCategory.FILE_SERVER,
    "AzureFileStorageLocation": LocationCategory.FILE_SERVER,
}

# Property names that denote a secret *value*. Names ending in "name" are
# excluded on purpose: keyVaultName and secretName are identifiers, not
# secrets, and redacting them would destroy real migration information.
_URI = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://", re.IGNORECASE)
_STORAGE_SCHEMES = frozenset(
    {"abfss", "abfs", "wasbs", "wasb", "adl", "s3", "s3a", "gs", "hdfs"}
)


@dataclass(frozen=True)
class DatasetSpecifics:
    """What a per-type handler contributes."""

    settings: Tuple[ConfigEntry, ...] = ()
    format_settings: Optional[TextFormatSettings] = None
    location_overrides: Dict[str, Any] = None  # SQL table/schema/database


def _value_of(node: Any) -> Optional[str]:
    """A property's value as a string, unwrapping an expression object.

    An expression-valued field holds the expression text verbatim, which is
    what the dataset actually declares. The matching entry in ``expressions``
    records the JSON path, so a reader can tell static from dynamic.
    """
    if isinstance(node, dict):
        inner = node.get("value")
        return inner if isinstance(inner, str) else None
    return render_scalar(node)


def _settings(properties: dict, *keys: str) -> Tuple[ConfigEntry, ...]:
    """Curated scalar settings by dotted key path, skipping absent ones."""
    collected: List[ConfigEntry] = []
    for key in keys:
        node: Any = properties
        for part in key.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if node is None or isinstance(node, list):
            continue
        rendered = _value_of(node) if isinstance(node, dict) else render_scalar(node)
        if rendered is not None:
            collected.append(ConfigEntry(key, rendered))
    return tuple(collected)


# --- dataset-type handlers ---------------------------------------------------
# One function per family. Location, references, expressions, parameters, and
# schema are handled once for every dataset, whatever its type.


def _delimited_text(type_properties: dict) -> DatasetSpecifics:
    def _text(key: str) -> Optional[str]:
        return _value_of(type_properties.get(key))

    header = type_properties.get("firstRowAsHeader")
    return DatasetSpecifics(
        format_settings=TextFormatSettings(
            column_delimiter=_text("columnDelimiter"),
            row_delimiter=_text("rowDelimiter"),
            quote_char=_text("quoteChar"),
            escape_char=_text("escapeChar"),
            first_row_as_header=header if isinstance(header, bool) else None,
            encoding_name=_text("encodingName"),
            null_value=_text("nullValue"),
            compression_codec=_text("compressionCodec"),
        ),
        settings=_settings(type_properties, "compressionLevel"),
    )


def _file_format(type_properties: dict) -> DatasetSpecifics:
    """Parquet, Json, Avro, Orc, Binary: a location plus a compression codec.

    Registered for these types because they share DelimitedText's structure
    exactly; nothing type-specific is invented for them.
    """
    return DatasetSpecifics(
        settings=_settings(type_properties, "compressionCodec", "encodingName")
    )


def _sql_table(type_properties: dict) -> DatasetSpecifics:
    """A SQL dataset addresses a table, not a file location.

    ``typeProperties.schema`` here is the *database schema name* — not the
    column list, which lives at ``properties.schema``.
    """
    return DatasetSpecifics(
        location_overrides={
            "table": _value_of(type_properties.get("table")),
            "schema": _value_of(type_properties.get("schema")),
            "database": _value_of(type_properties.get("database")),
        },
        settings=_settings(type_properties, "tableName"),
    )


_DATASET_HANDLERS: Dict[str, Callable[[dict], DatasetSpecifics]] = {
    # Present in the repository.
    "DelimitedText": _delimited_text,
    "AzureSqlDWTable": _sql_table,
    # Same structure, registered as aliases rather than new logic.
    "Parquet": _file_format,
    "Json": _file_format,
    "Avro": _file_format,
    "Orc": _file_format,
    "Binary": _file_format,
    "Excel": _file_format,
    "AzureSqlTable": _sql_table,
    "SqlServerTable": _sql_table,
    "AzureSynapseAnalyticsTable": _sql_table,
    "AzurePostgreSqlTable": _sql_table,
    "AzureMySqlTable": _sql_table,
}

_SQL_TYPES = frozenset(
    key for key, handler in _DATASET_HANDLERS.items() if handler is _sql_table
)


class DatasetExtractor(Extractor[DatasetDefinition]):
    """Extracts the declaration and references of a Synapse dataset artifact."""

    name = EXTRACTOR_NAME
    version = EXTRACTOR_VERSION
    supported_types = (AssetType.DATASET,)
    # Git and the live workspace serve the identical {name, properties}
    # document, so one extractor reads both. See discovery_agent.synapse.source.
    supported_sources = (SourceType.REPOSITORY, SourceType.SYNAPSE)

    def extract(
        self, artifact: DetectedArtifact, context: ExtractionContext
    ) -> ExtractionResult[DatasetDefinition]:
        try:
            document = context.source.read_json(artifact)
        except MalformedArtifactError as exc:
            return self._malformed(artifact, context, exc.reason, artifact.source_path)

        if not isinstance(document, dict):
            return self._malformed(
                artifact, context, "dataset json root is not an object", ""
            )

        properties = document.get("properties")
        if not isinstance(properties, dict):
            return self._malformed(
                artifact, context, "dataset has no properties object", ROOT_PATH
            )

        dataset_type = properties.get("type")
        if not isinstance(dataset_type, str) or not dataset_type:
            return self._malformed(
                artifact,
                context,
                "dataset properties has no type",
                f"{ROOT_PATH}.type",
            )

        warnings: List[ExtractionIssue] = []
        for unknown in sorted(
            key for key in properties if key not in KNOWN_DATASET_PROPERTIES
        ):
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"dataset property {unknown!r} is not modelled; it was not "
                    f"extracted",
                    f"{ROOT_PATH}.{unknown}",
                )
            )

        name = (
            document["name"]
            if isinstance(document.get("name"), str) and document["name"]
            else artifact.artifact_name
        )
        dataset_id = asset_id(AssetType.DATASET, name)

        type_properties = properties.get("typeProperties")
        type_properties = type_properties if isinstance(type_properties, dict) else {}

        handler = _DATASET_HANDLERS.get(dataset_type)
        recognized = handler is not None
        if recognized:
            specifics = handler(type_properties)
        else:
            specifics = self._unrecognized(type_properties)
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"dataset type {dataset_type!r} has no type handler; generic "
                    f"configuration, references, and expressions were still "
                    f"extracted",
                    f"{ROOT_PATH}.type",
                )
            )

        references, expressions = self._observe(
            properties, artifact, dataset_id, warnings
        )

        definition = DatasetDefinition(
            name=name,
            type=dataset_type,
            description=self._optional_str(properties.get("description")),
            folder=self._folder_name(properties.get("folder")),
            annotations=self._annotations(properties.get("annotations")),
            parameters=self._parameters(properties.get("parameters")),
            linked_service=self._linked_service(references),
            location=self._location(dataset_type, type_properties, specifics),
            schema=self._schema(properties),
            format_settings=specifics.format_settings,
            settings=specifics.settings,
            references=references,
            expressions=expressions,
            resources=self._resources(type_properties),
            secrets=scan_secrets(properties, ROOT_PATH),
            recognized=recognized,
        )

        return self.success(
            artifact,
            context,
            definition,
            references=definition.references,
            warnings=tuple(warnings),
        )

    # -- structural observation --------------------------------------------

    def _observe(
        self,
        properties: dict,
        artifact: DetectedArtifact,
        dataset_id: str,
        warnings: List[ExtractionIssue],
    ) -> Tuple[Tuple[ArtifactReference, ...], Tuple[SynapseExpression, ...]]:
        """References and expressions, found by the shared structural scanner.

        A ``*Reference`` object is a reference; an arbitrary string is not.
        This is the same rule the pipeline and notebook extractors use, so a
        dataset's ``linkedServiceName`` is recognized by exactly the mechanism
        that recognizes a pipeline's dataset reference.
        """
        found_references, found_expressions = scan(properties, ROOT_PATH)

        references: List[ArtifactReference] = []
        for found in found_references:
            target_type, kind, recognized = classify_reference(found.reference_type)
            if not recognized:
                warnings.append(
                    ExtractionIssue(
                        IssueCode.UNSUPPORTED_CONSTRUCT,
                        f"reference type {found.reference_type!r} is not modelled; "
                        f"recorded with an unknown target type",
                        found.location,
                    )
                )
            references.append(
                ArtifactReference(
                    source_artifact_id=dataset_id,
                    source_artifact_type=AssetType.DATASET,
                    kind=kind,
                    target_type=target_type,
                    target_name=found.reference_name,
                    location=found.location,
                    evidence=Evidence(artifact.source_path, None, EXTRACTOR_NAME),
                )
            )

        expressions = tuple(
            SynapseExpression(found.expression, found.location, found.form)
            for found in found_expressions
        )
        return (
            tuple(sorted(references, key=lambda r: (r.location, r.target_name))),
            expressions,
        )

    @staticmethod
    def _linked_service(
        references: Tuple[ArtifactReference, ...]
    ) -> Optional[ArtifactReference]:
        """The dataset's own linked service, promoted out of the reference list.

        Still present in ``references`` — this is a convenience view, not a
        second source of truth.
        """
        return next(
            (
                reference
                for reference in references
                if reference.location == f"{ROOT_PATH}.linkedServiceName"
                and reference.target_type is AssetType.LINKED_SERVICE
            ),
            None,
        )

    # -- location ----------------------------------------------------------

    def _location(
        self, dataset_type: str, type_properties: dict, specifics: DatasetSpecifics
    ) -> Optional[DatasetLocation]:
        """Normalize the dataset's location, whatever shape its type uses."""
        raw = type_properties.get("location")
        raw = raw if isinstance(raw, dict) else {}
        kind = raw.get("type") if isinstance(raw.get("type"), str) else None

        category = _LOCATION_CATEGORIES.get(kind, LocationCategory.UNKNOWN)
        overrides = specifics.location_overrides or {}
        if not raw and (dataset_type in _SQL_TYPES or any(overrides.values())):
            category = LocationCategory.SQL_TABLE

        location = DatasetLocation(
            category=category,
            kind=kind,
            file_system=_value_of(raw.get("fileSystem")),
            container=_value_of(raw.get("container")),
            folder_path=_value_of(raw.get("folderPath")),
            file_name=_value_of(raw.get("fileName")),
            relative_url=_value_of(raw.get("relativeUrl")),
            url=_value_of(raw.get("url")),
            database=overrides.get("database"),
            schema=overrides.get("schema"),
            table=overrides.get("table"),
        )
        return None if location.is_empty else location

    @staticmethod
    def _resources(type_properties: dict) -> Tuple[DatasetResource, ...]:
        """External resources the dataset writes out as a URI.

        Only fields that structurally hold a location are inspected, and only
        values that actually carry a URI scheme are recorded.
        """
        raw = type_properties.get("location")
        raw = raw if isinstance(raw, dict) else {}
        resources: List[DatasetResource] = []
        for key in ("url", "relativeUrl", "folderPath", "fileName"):
            value = _value_of(raw.get(key))
            if not value:
                continue
            match = _URI.match(value)
            if not match:
                continue
            scheme = match.group("scheme").lower()
            resources.append(
                DatasetResource(
                    uri=value,
                    scheme=scheme,
                    category=ResourceCategory.STORAGE
                    if scheme in _STORAGE_SCHEMES
                    else ResourceCategory.ENDPOINT
                    if scheme in ("http", "https")
                    else ResourceCategory.UNKNOWN,
                    location=f"{TYPE_PATH}.location.{key}",
                    detection=DetectionMethod.URI_SCHEME,
                )
            )
        return tuple(resources)

    # -- schema ------------------------------------------------------------

    @staticmethod
    def _schema(properties: dict) -> DatasetSchema:
        """The schema the dataset declares — not the physical table schema.

        ``schema: []`` is a real, valid declaration of no columns and is kept
        distinct from the key being absent.
        """
        declared = properties.get("schema")
        if declared is None and "structure" in properties:
            declared = properties.get("structure")  # the older ADF spelling
        if not isinstance(declared, list):
            return DatasetSchema(declared=False)

        columns: List[DatasetColumn] = []
        for ordinal, entry in enumerate(declared):
            if not isinstance(entry, dict):
                continue
            column_name = entry.get("name")
            if not isinstance(column_name, str) or not column_name:
                continue
            columns.append(
                DatasetColumn(
                    name=column_name,
                    type=entry.get("type") if isinstance(entry.get("type"), str) else None,
                    ordinal=ordinal,
                    precision=entry.get("precision")
                    if isinstance(entry.get("precision"), int)
                    else None,
                    scale=entry.get("scale") if isinstance(entry.get("scale"), int) else None,
                )
            )
        return DatasetSchema(declared=True, columns=tuple(columns))

    # -- parameters and metadata -------------------------------------------

    @staticmethod
    def _parameters(declared: Any) -> Tuple[DatasetParameter, ...]:
        """Dataset parameters, sorted by name — a JSON object has no order."""
        if not isinstance(declared, dict):
            return ()
        parameters: List[DatasetParameter] = []
        for key in sorted(declared.keys()):
            spec = declared[key]
            if not isinstance(spec, dict):
                parameters.append(DatasetParameter(name=key))
                continue
            has_default = "defaultValue" in spec
            is_secret = bool(_SECRET_KEYS.match(key))
            default = render_scalar(spec.get("defaultValue")) if has_default else None
            parameters.append(
                DatasetParameter(
                    name=key,
                    type=spec.get("type") if isinstance(spec.get("type"), str) else None,
                    # A secret-named parameter keeps its shape, not its value.
                    default_value=None if is_secret else default,
                    has_default=has_default,
                    is_secret=is_secret,
                )
            )
        return tuple(parameters)

    @staticmethod
    def _optional_str(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _folder_name(folder: Any) -> Optional[str]:
        if isinstance(folder, dict) and isinstance(folder.get("name"), str):
            return folder["name"]
        return None

    @staticmethod
    def _annotations(annotations: Any) -> Tuple[str, ...]:
        if not isinstance(annotations, list):
            return ()
        rendered = [render_scalar(a) for a in annotations]
        return tuple(sorted(r for r in rendered if r is not None))

    # -- secrets -----------------------------------------------------------


    # -- fallbacks ---------------------------------------------------------

    @staticmethod
    def _unrecognized(type_properties: dict) -> DatasetSpecifics:
        """Preserve an unmodelled type's top-level scalars, nothing more."""
        settings = tuple(
            ConfigEntry(key, rendered)
            for key in sorted(type_properties)
            if not isinstance(type_properties[key], (dict, list))
            for rendered in [render_scalar(type_properties[key])]
            if rendered is not None
        )
        return DatasetSpecifics(settings=settings)

    def _malformed(
        self,
        artifact: DetectedArtifact,
        context: ExtractionContext,
        reason: str,
        location: str,
    ) -> ExtractionResult[DatasetDefinition]:
        """A malformed dataset fails. It never becomes an empty definition."""
        return self.failure(
            artifact,
            context,
            errors=(
                ExtractionIssue(IssueCode.MALFORMED_ARTIFACT, reason, location or None),
            ),
        )
