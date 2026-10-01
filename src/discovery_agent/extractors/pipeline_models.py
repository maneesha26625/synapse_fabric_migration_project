"""Typed model of a Synapse pipeline.

A normalized, migration-oriented representation — not a second copy of the
source JSON. Fields exist because a later assessment or migration stage needs
them, not because the raw document happens to contain them.

Nothing here encodes a migration opinion. The model says what the pipeline
*is*; whether a Copy activity maps cleanly onto Fabric is Assessment's
question.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional, Tuple

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
)


class ScriptLanguage(str, Enum):
    """The language of code embedded directly in an activity."""

    SQL = "sql"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActivityDependency:
    """One edge of the pipeline's internal execution order."""

    activity: str  # the upstream activity's name
    conditions: Tuple[str, ...] = ()  # Succeeded | Failed | Skipped | Completed

    def to_dict(self) -> dict:
        return {"activity": self.activity, "conditions": list(self.conditions)}


@dataclass(frozen=True)
class ActivityPolicy:
    """Retry and timeout behaviour declared on an activity."""

    timeout: Optional[str] = None
    retry: Optional[int] = None
    retry_interval_seconds: Optional[int] = None
    secure_input: Optional[bool] = None
    secure_output: Optional[bool] = None

    def to_dict(self) -> dict:
        return {
            "timeout": self.timeout,
            "retry": self.retry,
            "retry_interval_seconds": self.retry_interval_seconds,
            "secure_input": self.secure_input,
            "secure_output": self.secure_output,
        }


@dataclass(frozen=True)
class UserProperty:
    """A user-defined property attached to an activity."""

    name: str
    value: str

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value}


@dataclass(frozen=True)
class EmbeddedScript:
    """Code carried inside an activity — a pre-copy script, a lookup query.

    ``text`` and ``location`` are the source of record and are carried
    through untouched. Everything after them is an observation *about* that
    text, produced by the shared SQL scanner.

    The observations live on the script, not pooled on the activity, because
    an activity can carry more than one: a Copy has both a pre-copy script
    and a source query, and a Script activity has one entry per statement.
    Pooling them would lose which statement named which table.
    """

    language: ScriptLanguage
    text: str
    location: str
    objects: Tuple[SqlObjectReference, ...] = ()
    features: Tuple[SqlFeature, ...] = ()
    dynamic_sql: Tuple[DynamicSqlSite, ...] = ()
    secrets: Tuple[SecretReference, ...] = ()

    @property
    def has_dynamic_sql(self) -> bool:
        return bool(self.dynamic_sql)

    @property
    def durable_objects(self) -> Tuple[SqlObjectReference, ...]:
        """Objects that outlive the batch, interpolated names included."""
        return tuple(o for o in self.objects if o.is_durable)

    def to_dict(self) -> dict:
        return {
            "language": self.language.value,
            "text": self.text,
            "location": self.location,
            "objects": [o.to_dict() for o in self.objects],
            "features": [f.to_dict() for f in self.features],
            "dynamic_sql": [d.to_dict() for d in self.dynamic_sql],
            "secrets": [s.to_dict() for s in self.secrets],
        }


@dataclass(frozen=True)
class DataMovement:
    """The shape of a Copy activity: what moves from where to where."""

    source_type: Optional[str] = None  # DelimitedTextSource, SqlDWSource, ...
    sink_type: Optional[str] = None  # SqlDWSink, DelimitedTextSink, ...
    source_store_type: Optional[str] = None  # HttpReadSettings, ...
    sink_store_type: Optional[str] = None  # AzureBlobFSWriteSettings, ...
    source_format_type: Optional[str] = None
    sink_format_type: Optional[str] = None
    translator_type: Optional[str] = None
    staging_enabled: Optional[bool] = None

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "sink_type": self.sink_type,
            "source_store_type": self.source_store_type,
            "sink_store_type": self.sink_store_type,
            "source_format_type": self.source_format_type,
            "sink_format_type": self.sink_format_type,
            "translator_type": self.translator_type,
            "staging_enabled": self.staging_enabled,
        }


@dataclass(frozen=True)
class ComputeRequest:
    """Compute an activity asks for, where the pipeline declares it."""

    compute_type: Optional[str] = None  # General, MemoryOptimized, ...
    core_count: Optional[int] = None

    def to_dict(self) -> dict:
        return {"compute_type": self.compute_type, "core_count": self.core_count}


@dataclass(frozen=True)
class PipelineActivity:
    """One activity, with its children if it is a container.

    Nesting is preserved structurally through ``children``: a ForEach holding
    an IfCondition holding a Copy is three levels deep in this model, exactly
    as in the source. ``parent`` and ``branch`` additionally let a single
    activity be understood without walking back up the tree.
    """

    name: str
    type: str
    path: str  # JSON location, e.g. "properties.activities[2]"
    description: Optional[str] = None
    parent: Optional[str] = None  # enclosing activity's name; None at top level
    branch: Optional[str] = None  # which branch of the parent: ifTrue, case=A, ...
    depends_on: Tuple[ActivityDependency, ...] = ()
    policy: Optional[ActivityPolicy] = None
    user_properties: Tuple[UserProperty, ...] = ()
    settings: Tuple[ConfigEntry, ...] = ()
    scripts: Tuple[EmbeddedScript, ...] = ()
    data_movement: Optional[DataMovement] = None
    compute: Optional[ComputeRequest] = None
    references: Tuple[ArtifactReference, ...] = ()
    expressions: Tuple[SynapseExpression, ...] = ()
    children: Tuple["PipelineActivity", ...] = ()
    recognized: bool = True  # False when the extractor has no handler for the type

    @property
    def is_container(self) -> bool:
        return bool(self.children)

    @property
    def sql_objects(self) -> Tuple[SqlObjectReference, ...]:
        """Objects named by this activity's own scripts; children keep theirs."""
        return tuple(obj for script in self.scripts for obj in script.objects)

    def walk(self) -> Iterator["PipelineActivity"]:
        """This activity and every descendant, depth-first in source order."""
        yield self
        for child in self.children:
            for nested in child.walk():
                yield nested

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "path": self.path,
            "description": self.description,
            "parent": self.parent,
            "branch": self.branch,
            "depends_on": [d.to_dict() for d in self.depends_on],
            "policy": self.policy.to_dict() if self.policy else None,
            "user_properties": [p.to_dict() for p in self.user_properties],
            "settings": [s.to_dict() for s in self.settings],
            "scripts": [s.to_dict() for s in self.scripts],
            "data_movement": self.data_movement.to_dict() if self.data_movement else None,
            "compute": self.compute.to_dict() if self.compute else None,
            "references": [r.to_dict() for r in self.references],
            "expressions": [e.to_dict() for e in self.expressions],
            "children": [c.to_dict() for c in self.children],
            "recognized": self.recognized,
        }


@dataclass(frozen=True)
class PipelineDefinition:
    """What one Synapse pipeline contains.

    ``activities`` keeps source order, which is the order Synapse Studio shows
    and the order a reviewer will read; true execution order lives in each
    activity's ``depends_on``. Parameters and variables are sorted by name,
    because a JSON object has no meaningful order.
    """

    name: str
    description: Optional[str] = None
    folder: Optional[str] = None
    concurrency: Optional[int] = None
    annotations: Tuple[str, ...] = ()
    parameters: Tuple[ValueDeclaration, ...] = ()
    variables: Tuple[ValueDeclaration, ...] = ()
    activities: Tuple[PipelineActivity, ...] = ()

    def walk(self) -> Iterator[PipelineActivity]:
        """Every activity at every depth, depth-first in source order."""
        for activity in self.activities:
            for nested in activity.walk():
                yield nested

    @property
    def all_activities(self) -> Tuple[PipelineActivity, ...]:
        return tuple(self.walk())

    @property
    def activity_count(self) -> int:
        """Every activity including nested ones."""
        return len(self.all_activities)

    @property
    def activity_types(self) -> Tuple[str, ...]:
        return tuple(sorted({a.type for a in self.walk()}))

    @property
    def unrecognized_activities(self) -> Tuple[PipelineActivity, ...]:
        return tuple(a for a in self.walk() if not a.recognized)

    @property
    def max_depth(self) -> int:
        """1 for a flat pipeline, 2 when a container holds activities, and so on."""

        def depth(activity: PipelineActivity) -> int:
            if not activity.children:
                return 1
            return 1 + max(depth(child) for child in activity.children)

        return max((depth(a) for a in self.activities), default=0)

    @property
    def references(self) -> Tuple[ArtifactReference, ...]:
        """Every reference observed anywhere in the pipeline, nested included."""
        return tuple(ref for activity in self.walk() for ref in activity.references)

    @property
    def expressions(self) -> Tuple[SynapseExpression, ...]:
        return tuple(expr for activity in self.walk() for expr in activity.expressions)

    @property
    def scripts(self) -> Tuple[EmbeddedScript, ...]:
        """Every embedded script anywhere in the pipeline, nested included."""
        return tuple(script for activity in self.walk() for script in activity.scripts)

    @property
    def sql_objects(self) -> Tuple[SqlObjectReference, ...]:
        """Every SQL object named by any embedded script, occurrences intact."""
        return tuple(obj for script in self.scripts for obj in script.objects)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "folder": self.folder,
            "concurrency": self.concurrency,
            "annotations": list(self.annotations),
            "parameters": [p.to_dict() for p in self.parameters],
            "variables": [v.to_dict() for v in self.variables],
            "activities": [a.to_dict() for a in self.activities],
        }
