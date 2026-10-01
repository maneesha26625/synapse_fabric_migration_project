"""What a Synapse Spark job definition declares.

A Spark job definition is not a notebook, and the difference matters for
migration. A notebook carries its code inline as cells and is authored
interactively. A job definition carries no code at all: it names a *file* --
a jar, a Python script, an R script -- that lives in storage, plus the class
to invoke, the arguments to pass, and the Spark resources to request. So the
interesting content is a set of pointers and a resource request, not a
program.

Two things are deliberately absent:

* **The referenced file's contents.** ``jobProperties.file`` is a storage URI.
  Discovery records the pointer; fetching and parsing a jar is not discovery.
* **Schedules.** A trigger references a Spark job definition, not the other
  way round, so nothing in this artifact says when it runs. That relationship
  is observable from the trigger, which is outside P0.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from discovery_agent.extractors.common_models import (
    ConfigEntry,
    SecretReference,
    SynapseExpression,
    ValueDeclaration,
)
from discovery_agent.extractors.models import ArtifactReference


class SparkArtifactKind(str, Enum):
    """What role a file plays in the job.

    ``MAIN_FILE`` is the application itself; the rest are dependencies the
    pool is told to place on the job's path. Kept apart because a migration
    has to move the main file and may be able to resolve the others from a
    package feed instead.
    """

    MAIN_FILE = "main_file"
    JAR = "jar"
    PYTHON_FILE = "python_file"
    FILE = "file"
    ARCHIVE = "archive"


@dataclass(frozen=True)
class SparkJobArtifact:
    """One file the job needs, exactly as the definition names it.

    ``uri`` is preserved verbatim, including any expression. ``scheme`` is
    the URI scheme when there is one (``abfss``, ``wasbs``, ``https``) and
    None for a bare relative path -- which is itself worth knowing, because a
    relative path depends on a workspace default that will not survive a
    migration.
    """

    kind: SparkArtifactKind
    uri: str
    location: str
    scheme: Optional[str] = None

    @property
    def is_absolute(self) -> bool:
        return self.scheme is not None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "uri": self.uri,
            "location": self.location,
            "scheme": self.scheme,
            "is_absolute": self.is_absolute,
        }


@dataclass(frozen=True)
class SparkResourceRequest:
    """The compute the job asks the pool for.

    Every field is optional because every field may be omitted, in which case
    the pool's default applies -- and the default is a property of the pool,
    not of this artifact. ``None`` here means "not declared", never "zero".
    """

    driver_memory: Optional[str] = None
    driver_cores: Optional[int] = None
    executor_memory: Optional[str] = None
    executor_cores: Optional[int] = None
    executor_count: Optional[int] = None

    @property
    def is_declared(self) -> bool:
        return any(
            value is not None
            for value in (
                self.driver_memory,
                self.driver_cores,
                self.executor_memory,
                self.executor_cores,
                self.executor_count,
            )
        )

    def to_dict(self) -> dict:
        return {
            "driver_memory": self.driver_memory,
            "driver_cores": self.driver_cores,
            "executor_memory": self.executor_memory,
            "executor_cores": self.executor_cores,
            "executor_count": self.executor_count,
        }


@dataclass(frozen=True)
class SparkJobProperties:
    """The ``jobProperties`` block: what to run, with what, and how.

    ``job_name`` is the name inside ``jobProperties`` and is *not* the
    artifact's name. Synapse usually sets them the same and does not require
    it, so both are kept rather than one being assumed to stand for the other.
    """

    job_name: Optional[str] = None
    main_file: Optional[SparkJobArtifact] = None
    class_name: Optional[str] = None
    arguments: Tuple[str, ...] = ()
    artifacts: Tuple[SparkJobArtifact, ...] = ()
    configuration: Tuple[ConfigEntry, ...] = ()
    resources: SparkResourceRequest = SparkResourceRequest()

    @property
    def has_main_file(self) -> bool:
        return self.main_file is not None

    def artifacts_of(self, kind: SparkArtifactKind) -> Tuple[SparkJobArtifact, ...]:
        return tuple(a for a in self.artifacts if a.kind is kind)

    def to_dict(self) -> dict:
        return {
            "job_name": self.job_name,
            "main_file": self.main_file.to_dict() if self.main_file else None,
            "class_name": self.class_name,
            "arguments": list(self.arguments),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "configuration": [c.to_dict() for c in self.configuration],
            "resources": self.resources.to_dict(),
        }


@dataclass(frozen=True)
class SparkJobDefinition:
    """One Synapse Spark job definition, as authored.

    ``compute`` is the reference to the Big Data pool the job targets, kept
    in ``references`` as well: this is a convenience view, not a second source
    of truth, which is the same rule the dataset extractor follows for its
    linked service.
    """

    name: str
    language: Optional[str] = None
    required_spark_version: Optional[str] = None
    description: Optional[str] = None
    folder: Optional[str] = None
    job: SparkJobProperties = SparkJobProperties()
    compute: Optional[ArtifactReference] = None
    spark_configuration: Optional[ArtifactReference] = None
    parameters: Tuple[ValueDeclaration, ...] = ()
    references: Tuple[ArtifactReference, ...] = ()
    expressions: Tuple[SynapseExpression, ...] = ()
    secrets: Tuple[SecretReference, ...] = ()

    @property
    def targets_a_pool(self) -> bool:
        return self.compute is not None

    @property
    def artifact_count(self) -> int:
        return len(self.job.artifacts) + (1 if self.job.has_main_file else 0)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "language": self.language,
            "required_spark_version": self.required_spark_version,
            "description": self.description,
            "folder": self.folder,
            "job": self.job.to_dict(),
            "compute": self.compute.to_dict() if self.compute else None,
            "spark_configuration": (
                self.spark_configuration.to_dict() if self.spark_configuration else None
            ),
            "parameters": [p.to_dict() for p in self.parameters],
            "references": [r.to_dict() for r in self.references],
            "expressions": [e.to_dict() for e in self.expressions],
            "secrets": [s.to_dict() for s in self.secrets],
        }
