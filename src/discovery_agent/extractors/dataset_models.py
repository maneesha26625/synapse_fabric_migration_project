"""Typed model of a Synapse dataset.

A dataset says what data exists and how it is reached. This model normalizes
that: where the data lives, which linked service opens the connection, what
shape the author declared, and which parts are computed at runtime.

What it does not do is judge any of it. Whether an ``AzureSqlDWTable`` maps
onto a Fabric warehouse table is Assessment's question.

One distinction the Synapse schema makes confusingly, preserved carefully
here: ``properties.schema`` is the *declared column list*, while
``typeProperties.schema`` on a SQL dataset is the *database schema name*.
They are different things and land in different fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from discovery_agent.extractors.common_models import (
    ConfigEntry,
    SecretKind,
    SecretReference,
    SynapseExpression,
    ValueDeclaration,
)
from discovery_agent.extractors.models import ArtifactReference


class LocationCategory(str, Enum):
    """Where a dataset's data physically sits, normalized across types."""

    DATA_LAKE = "data_lake"  # AzureBlobFSLocation (ADLS Gen2)
    BLOB = "blob"  # AzureBlobStorageLocation
    HTTP = "http"  # HttpServerLocation
    FILE_SERVER = "file_server"
    SQL_TABLE = "sql_table"  # table / schema / database, no location object
    REST = "rest"
    UNKNOWN = "unknown"


class ResourceCategory(str, Enum):
    STORAGE = "storage"
    ENDPOINT = "endpoint"
    UNKNOWN = "unknown"


class DetectionMethod(str, Enum):
    STRUCTURE = "structure"  # a declared JSON field
    URI_SCHEME = "uri_scheme"
    PROPERTY_NAME = "property_name"


@dataclass(frozen=True)
class DatasetParameter(ValueDeclaration):
    """A dataset parameter.

    The shared ``ValueDeclaration`` plus the one thing only datasets need: a
    flag marking a parameter whose name denotes a secret, whose default is
    dropped rather than recorded.
    """

    is_secret: bool = False

    def to_dict(self) -> dict:
        payload = super().to_dict()
        payload["is_secret"] = self.is_secret
        return payload


@dataclass(frozen=True)
class DatasetColumn:
    """One column of the schema the dataset *declares*.

    This is the author's declaration, not the physical table schema. Reading
    the real catalog needs live Synapse metadata, which this stage never
    touches.
    """

    name: str
    type: Optional[str] = None
    ordinal: int = 0  # declaration order, which is meaningful for a schema
    precision: Optional[int] = None
    scale: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "ordinal": self.ordinal,
            "precision": self.precision,
            "scale": self.scale,
        }


@dataclass(frozen=True)
class DatasetSchema:
    """The declared schema, if the dataset declares one.

    ``declared`` separates "no schema key at all" from "schema: []", which the
    real repository contains and which is valid, not malformed.
    """

    declared: bool = False
    columns: Tuple[DatasetColumn, ...] = ()

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def to_dict(self) -> dict:
        return {
            "declared": self.declared,
            "column_count": self.column_count,
            "columns": [c.to_dict() for c in self.columns],
        }


@dataclass(frozen=True)
class DatasetLocation:
    """Where the dataset's data lives.

    Only the fields a given dataset type actually uses are populated. A field
    whose value was written as an expression holds the expression text
    verbatim; the matching entry in ``DatasetDefinition.expressions`` says
    exactly which JSON path was dynamic.
    """

    category: LocationCategory = LocationCategory.UNKNOWN
    kind: Optional[str] = None  # the raw location "type", e.g. AzureBlobFSLocation
    file_system: Optional[str] = None
    container: Optional[str] = None
    folder_path: Optional[str] = None
    file_name: Optional[str] = None
    relative_url: Optional[str] = None
    url: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None  # SQL schema name, not the column list
    table: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return self.kind is None and not any(
            (
                self.file_system,
                self.container,
                self.folder_path,
                self.file_name,
                self.relative_url,
                self.url,
                self.database,
                self.schema,
                self.table,
            )
        )

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "kind": self.kind,
            "file_system": self.file_system,
            "container": self.container,
            "folder_path": self.folder_path,
            "file_name": self.file_name,
            "relative_url": self.relative_url,
            "url": self.url,
            "database": self.database,
            "schema": self.schema,
            "table": self.table,
        }


@dataclass(frozen=True)
class TextFormatSettings:
    """Delimited-text parsing rules, which have to survive a migration intact."""

    column_delimiter: Optional[str] = None
    row_delimiter: Optional[str] = None
    quote_char: Optional[str] = None
    escape_char: Optional[str] = None
    first_row_as_header: Optional[bool] = None
    encoding_name: Optional[str] = None
    null_value: Optional[str] = None
    compression_codec: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "column_delimiter": self.column_delimiter,
            "row_delimiter": self.row_delimiter,
            "quote_char": self.quote_char,
            "escape_char": self.escape_char,
            "first_row_as_header": self.first_row_as_header,
            "encoding_name": self.encoding_name,
            "null_value": self.null_value,
            "compression_codec": self.compression_codec,
        }


@dataclass(frozen=True)
class DatasetResource:
    """An external resource the dataset names outright."""

    uri: str
    scheme: str
    category: ResourceCategory
    location: str
    detection: DetectionMethod = DetectionMethod.STRUCTURE

    def to_dict(self) -> dict:
        return {
            "uri": self.uri,
            "scheme": self.scheme,
            "category": self.category.value,
            "location": self.location,
            "detection": self.detection.value,
        }


@dataclass(frozen=True)
class DatasetDefinition:
    """What one Synapse dataset declares."""

    name: str
    type: str
    description: Optional[str] = None
    folder: Optional[str] = None
    annotations: Tuple[str, ...] = ()
    parameters: Tuple[DatasetParameter, ...] = ()
    linked_service: Optional[ArtifactReference] = None
    location: Optional[DatasetLocation] = None
    schema: DatasetSchema = DatasetSchema()
    format_settings: Optional[TextFormatSettings] = None
    settings: Tuple[ConfigEntry, ...] = ()
    references: Tuple[ArtifactReference, ...] = ()
    expressions: Tuple[SynapseExpression, ...] = ()
    resources: Tuple[DatasetResource, ...] = ()
    secrets: Tuple[SecretReference, ...] = ()
    recognized: bool = True  # False when no type handler matched

    @property
    def linked_service_name(self) -> Optional[str]:
        return self.linked_service.target_name if self.linked_service else None

    @property
    def is_parameterized(self) -> bool:
        return bool(self.parameters) or bool(self.expressions)

    def summary(self) -> dict:
        """A compact overview. Carries no secret values, by construction."""
        return {
            "name": self.name,
            "type": self.type,
            "recognized": self.recognized,
            "folder": self.folder,
            "linked_service": self.linked_service_name,
            "location_category": self.location.category.value if self.location else None,
            "location_kind": self.location.kind if self.location else None,
            "parameter_count": len(self.parameters),
            "declared_columns": self.schema.column_count,
            "schema_declared": self.schema.declared,
            "expression_count": len(self.expressions),
            "reference_count": len(self.references),
            "resource_count": len(self.resources),
            "secret_count": len(self.secrets),
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "folder": self.folder,
            "annotations": list(self.annotations),
            "parameters": [p.to_dict() for p in self.parameters],
            "linked_service": self.linked_service.to_dict() if self.linked_service else None,
            "location": self.location.to_dict() if self.location else None,
            "schema": self.schema.to_dict(),
            "format_settings": self.format_settings.to_dict()
            if self.format_settings
            else None,
            "settings": [s.to_dict() for s in self.settings],
            "references": [r.to_dict() for r in self.references],
            "expressions": [e.to_dict() for e in self.expressions],
            "resources": [r.to_dict() for r in self.resources],
            "secrets": [s.to_dict() for s in self.secrets],
            "recognized": self.recognized,
        }
