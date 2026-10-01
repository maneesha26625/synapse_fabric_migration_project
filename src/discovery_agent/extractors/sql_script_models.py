"""Typed model of a Synapse SQL script artifact.

The SQL text *is* the artifact definition, so it is preserved byte for byte
and never rewritten. Everything else in this model is an observation about
that text, derived by keyword matching rather than parsing.

Only what is specific to the *artifact* lives here. The SQL analysis types
themselves — object references, features, dynamic SQL sites — are in
``sql_models``, because a pipeline activity's embedded SQL produces exactly
the same observations and must not model them a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from discovery_agent.extractors.common_models import (
    ConfigEntry,
    SecretReference,
    SynapseExpression,
    ValueDeclaration,
)
from discovery_agent.extractors.models import ArtifactReference
from discovery_agent.extractors.sql_models import (
    DynamicSqlSite,
    SqlFeature,
    SqlObjectReference,
    SqlOperation,
)


@dataclass(frozen=True)
class SqlScriptConnection:
    """The pool and database the script is bound to in the workspace."""

    pool_name: Optional[str] = None
    database_name: Optional[str] = None
    connection_type: Optional[str] = None

    @property
    def is_serverless(self) -> bool:
        """Synapse names the serverless endpoint's pool "Built-in"."""
        return (self.pool_name or "").strip().lower() == "built-in"

    @property
    def is_empty(self) -> bool:
        return not any((self.pool_name, self.database_name, self.connection_type))

    def to_dict(self) -> dict:
        return {
            "pool_name": self.pool_name,
            "database_name": self.database_name,
            "connection_type": self.connection_type,
            "is_serverless": self.is_serverless,
        }


@dataclass(frozen=True)
class SQLScriptDefinition:
    """What one Synapse SQL script artifact contains.

    ``sql_text`` is the authoritative definition and is preserved verbatim —
    never reformatted, never redacted. Redaction applies only to derived
    evidence and to ``summary()``, which never carries the text at all.
    """

    name: str
    type: str  # the artifact's declared type, normally "SqlQuery"
    sql_text: str = ""
    language: Optional[str] = None
    description: Optional[str] = None
    folder: Optional[str] = None
    annotations: Tuple[str, ...] = ()
    connection: Optional[SqlScriptConnection] = None
    parameters: Tuple[ValueDeclaration, ...] = ()
    objects: Tuple[SqlObjectReference, ...] = ()
    features: Tuple[SqlFeature, ...] = ()
    dynamic_sql: Tuple[DynamicSqlSite, ...] = ()
    settings: Tuple[ConfigEntry, ...] = ()
    references: Tuple[ArtifactReference, ...] = ()
    expressions: Tuple[SynapseExpression, ...] = ()
    secrets: Tuple[SecretReference, ...] = ()
    recognized: bool = True

    @property
    def line_count(self) -> int:
        return len(self.sql_text.splitlines())

    @property
    def character_count(self) -> int:
        return len(self.sql_text)

    @property
    def is_empty(self) -> bool:
        return not self.sql_text.strip()

    @property
    def has_dynamic_sql(self) -> bool:
        return bool(self.dynamic_sql)

    @property
    def durable_objects(self) -> Tuple[SqlObjectReference, ...]:
        return tuple(o for o in self.objects if o.is_durable)

    @property
    def objects_read(self) -> Tuple[SqlObjectReference, ...]:
        return tuple(o for o in self.objects if o.operation is SqlOperation.READ)

    @property
    def objects_written(self) -> Tuple[SqlObjectReference, ...]:
        written = (
            SqlOperation.WRITE,
            SqlOperation.CREATE,
            SqlOperation.ALTER,
            SqlOperation.DROP,
            SqlOperation.TRUNCATE,
            SqlOperation.MERGE,
        )
        return tuple(o for o in self.objects if o.operation in written)

    @property
    def referenced_names(self) -> Tuple[str, ...]:
        """Distinct qualified names of durable objects, sorted."""
        return tuple(sorted({o.qualified_name for o in self.durable_objects}))

    def features_by_kind(self) -> dict:
        counts: dict = {}
        for feature in self.features:
            counts[feature.kind.value] = counts.get(feature.kind.value, 0) + 1
        return dict(sorted(counts.items()))

    def summary(self) -> dict:
        """A compact overview that never carries the SQL text."""
        return {
            "name": self.name,
            "type": self.type,
            "recognized": self.recognized,
            "folder": self.folder,
            "language": self.language,
            "line_count": self.line_count,
            "character_count": self.character_count,
            "pool": self.connection.pool_name if self.connection else None,
            "database": self.connection.database_name if self.connection else None,
            "is_serverless": self.connection.is_serverless if self.connection else None,
            "object_count": len(self.durable_objects),
            "objects_read": len(self.objects_read),
            "objects_written": len(self.objects_written),
            "referenced_names": list(self.referenced_names),
            "features": self.features_by_kind(),
            "dynamic_sql_sites": len(self.dynamic_sql),
            "secret_count": len(self.secrets),
            "reference_count": len(self.references),
        }

    def to_dict(self, include_sql: bool = True) -> dict:
        payload = {
            "name": self.name,
            "type": self.type,
            "language": self.language,
            "description": self.description,
            "folder": self.folder,
            "annotations": list(self.annotations),
            "connection": self.connection.to_dict() if self.connection else None,
            "parameters": [p.to_dict() for p in self.parameters],
            "objects": [o.to_dict() for o in self.objects],
            "features": [f.to_dict() for f in self.features],
            "dynamic_sql": [d.to_dict() for d in self.dynamic_sql],
            "settings": [s.to_dict() for s in self.settings],
            "references": [r.to_dict() for r in self.references],
            "expressions": [e.to_dict() for e in self.expressions],
            "secrets": [s.to_dict() for s in self.secrets],
            "line_count": self.line_count,
            "recognized": self.recognized,
        }
        if include_sql:
            payload["sql_text"] = self.sql_text
        return payload
