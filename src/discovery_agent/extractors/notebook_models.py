"""Typed model of a Synapse notebook.

A normalized, migration-oriented representation. It says what the notebook
*is* and what it contains; whether any of it survives the move to Fabric is
Assessment's question, not this model's.

Two rules shape it:

* **Source code is preserved exactly.** Never reformatted, never translated,
  never stripped of comments. The migration worker needs what was authored.
* **Findings are observations, not verdicts.** ``CodeFinding`` deliberately
  has no severity field: calling ``mssparkutils`` "hard to migrate" is an
  assessment, and putting it here would bake a judgement into extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from discovery_agent.extractors.common_models import ConfigEntry
from discovery_agent.extractors.models import ArtifactReference


class NotebookFormat(str, Enum):
    """The on-disk notebook format a reader understood."""

    SYNAPSE_NOTEBOOK_JSON = "synapse_notebook_json"
    IPYNB = "ipynb"  # declared for the future; no reader implemented
    UNKNOWN = "unknown"


class CellType(str, Enum):
    CODE = "code"
    MARKDOWN = "markdown"
    RAW = "raw"
    UNKNOWN = "unknown"


class NotebookLanguage(str, Enum):
    PYTHON = "python"
    SCALA = "scala"
    CSHARP = "csharp"
    SQL = "sql"
    R = "r"
    MARKDOWN = "markdown"
    UNKNOWN = "unknown"


class LanguageSource(str, Enum):
    """How a cell's language was determined.

    Recorded so a reviewer can tell a declared language from an inherited one.
    Nothing is ever guessed from what the code looks like.
    """

    KERNELSPEC = "kernelspec"
    LANGUAGE_INFO = "language_info"
    CELL_METADATA = "cell_metadata"
    MAGIC = "magic"
    NOTEBOOK_DEFAULT = "notebook_default"
    CELL_TYPE = "cell_type"
    UNKNOWN = "unknown"


class FindingCategory(str, Enum):
    """What kind of construct a finding records."""

    SYNAPSE_UTILS = "synapse_utils"  # mssparkutils / notebookutils
    SYNAPSE_SQL_CONNECTOR = "synapse_sql_connector"  # synapsesql
    LINKED_SERVICE_API = "linked_service_api"  # TokenLibrary, linked service conf
    NOTEBOOK_EXECUTION = "notebook_execution"  # notebook.run, %run, exit
    SPARK_API = "spark_api"  # spark.sql, spark.conf.set, hadoopConfiguration
    MAGIC_COMMAND = "magic_command"
    PACKAGE_INSTALL = "package_install"
    SESSION_CONFIG = "session_config"
    CREDENTIAL = "credential"
    RESOURCE_PATH = "resource_path"
    WORKSPACE_CONFIG = "workspace_config"


class DetectionMethod(str, Enum):
    """The deterministic rule that produced a finding."""

    CODE_PATTERN = "code_pattern"
    MAGIC = "magic"
    IMPORT = "import"
    URI_SCHEME = "uri_scheme"
    NOTEBOOK_METADATA = "notebook_metadata"
    SESSION_PROPERTY = "session_property"
    CONFIGURE_MAGIC = "configure_magic"


class SqlDetection(str, Enum):
    """How a SQL block was identified. Both are structural."""

    MAGIC_CELL = "magic_cell"  # a %%sql cell
    SPARK_SQL_CALL = "spark_sql_call"  # a string literal passed to spark.sql()


class MagicScope(str, Enum):
    CELL = "cell"  # %%name, applies to the whole cell
    LINE = "line"  # %name


class ResourceCategory(str, Enum):
    STORAGE = "storage"  # abfss, wasbs, adl, s3, gs, hdfs
    ENDPOINT = "endpoint"  # http, https
    MOUNT = "mount"  # synfs, dbfs, /synapse/workspaces/...
    LOCAL_PATH = "local_path"  # file://


class PackageKind(str, Enum):
    """How a package entered the notebook's environment."""

    PIP_INSTALL = "pip_install"
    CONDA_INSTALL = "conda_install"
    SPARK_PACKAGE = "spark_package"  # from %%configure jars/packages
    IMPORT = "import"  # an ordinary import statement


@dataclass(frozen=True)
class CodeFinding:
    """One migration-relevant construct found in notebook source.

    No severity, by design. Whether a construct is trivial or blocking is an
    assessment; extraction only reports that it is there, and where.
    """

    category: FindingCategory
    construct: str  # the canonical construct name, e.g. "mssparkutils.fs"
    cell_index: int
    location: str  # "properties.cells[3].source:L7"
    evidence: str  # the matched line, trimmed; redacted for CREDENTIAL
    detection: DetectionMethod

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "construct": self.construct,
            "cell_index": self.cell_index,
            "location": self.location,
            "evidence": self.evidence,
            "detection": self.detection.value,
        }


@dataclass(frozen=True)
class MagicCommand:
    """A notebook magic, captured structurally and never translated."""

    name: str  # "sql", "pip", "configure"
    scope: MagicScope
    arguments: Optional[str]
    cell_index: int
    location: str
    recognized: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "scope": self.scope.value,
            "arguments": self.arguments,
            "cell_index": self.cell_index,
            "location": self.location,
            "recognized": self.recognized,
        }


@dataclass(frozen=True)
class NotebookSqlBlock:
    """SQL carried inside a notebook, kept verbatim.

    Parsing it into table dependencies belongs to the later SQL analysis
    stage, which needs the original text to work from.
    """

    cell_index: int
    sql: str
    detection: SqlDetection
    location: str

    def to_dict(self) -> dict:
        return {
            "cell_index": self.cell_index,
            "sql": self.sql,
            "detection": self.detection.value,
            "location": self.location,
        }


@dataclass(frozen=True)
class ResourceReference:
    """An explicitly written resource path or endpoint.

    Recorded as observed. What its Fabric equivalent should be is a migration
    decision, not an extraction one.
    """

    uri: str
    scheme: str
    category: ResourceCategory
    location: str
    detection: DetectionMethod
    cell_index: Optional[int] = None  # None for notebook-level metadata

    def to_dict(self) -> dict:
        return {
            "uri": self.uri,
            "scheme": self.scheme,
            "category": self.category.value,
            "location": self.location,
            "detection": self.detection.value,
            "cell_index": self.cell_index,
        }


@dataclass(frozen=True)
class PackageFinding:
    """A library the notebook imports, installs, or configures.

    ``specifier`` is whatever version constraint was written; nothing is
    resolved against a package index.
    """

    name: str
    kind: PackageKind
    location: str
    specifier: Optional[str] = None
    cell_index: Optional[int] = None
    is_standard_library: Optional[bool] = None  # only meaningful for IMPORT

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "specifier": self.specifier,
            "location": self.location,
            "cell_index": self.cell_index,
            "is_standard_library": self.is_standard_library,
        }


@dataclass(frozen=True)
class CellOutput:
    """That a cell had an output, and of what kind.

    Output *content* is deliberately not captured: it is execution residue
    rather than source, it can be very large, and it is a common place for
    credentials and customer data to sit in a committed notebook.
    """

    output_type: str
    execution_count: Optional[int] = None
    size_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "output_type": self.output_type,
            "execution_count": self.execution_count,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class NotebookCell:
    """One cell, with its source preserved exactly as authored."""

    index: int
    cell_type: CellType
    raw_cell_type: str  # the original string, even when unrecognized
    language: NotebookLanguage
    language_source: LanguageSource
    source: str  # verbatim: not reformatted, translated, or stripped
    line_count: int
    location: str
    execution_count: Optional[int] = None
    tags: Tuple[str, ...] = ()
    metadata: Tuple[ConfigEntry, ...] = ()
    outputs: Tuple[CellOutput, ...] = ()
    magics: Tuple[MagicCommand, ...] = ()
    findings: Tuple[CodeFinding, ...] = ()
    sql_blocks: Tuple[NotebookSqlBlock, ...] = ()
    resources: Tuple[ResourceReference, ...] = ()
    packages: Tuple[PackageFinding, ...] = ()

    @property
    def is_code(self) -> bool:
        return self.cell_type is CellType.CODE

    def to_dict(self, include_source: bool = True) -> dict:
        payload = {
            "index": self.index,
            "cell_type": self.cell_type.value,
            "raw_cell_type": self.raw_cell_type,
            "language": self.language.value,
            "language_source": self.language_source.value,
            "line_count": self.line_count,
            "location": self.location,
            "execution_count": self.execution_count,
            "tags": list(self.tags),
            "metadata": [m.to_dict() for m in self.metadata],
            "outputs": [o.to_dict() for o in self.outputs],
            "magics": [m.to_dict() for m in self.magics],
            "findings": [f.to_dict() for f in self.findings],
            "sql_blocks": [s.to_dict() for s in self.sql_blocks],
            "resources": [r.to_dict() for r in self.resources],
            "packages": [p.to_dict() for p in self.packages],
        }
        if include_source:
            payload["source"] = self.source
        return payload


@dataclass(frozen=True)
class ComputeBinding:
    """The Spark pool a notebook is attached to, and what it looks like."""

    pool_name: Optional[str] = None
    reference_type: Optional[str] = None
    spark_version: Optional[str] = None
    node_count: Optional[int] = None
    cores: Optional[int] = None
    memory: Optional[int] = None
    resource_id: Optional[str] = None  # ARM id from a365ComputeOptions
    endpoint: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "pool_name": self.pool_name,
            "reference_type": self.reference_type,
            "spark_version": self.spark_version,
            "node_count": self.node_count,
            "cores": self.cores,
            "memory": self.memory,
            "resource_id": self.resource_id,
            "endpoint": self.endpoint,
        }


@dataclass(frozen=True)
class SessionConfiguration:
    """Declared Spark session sizing and configuration."""

    driver_memory: Optional[str] = None
    driver_cores: Optional[int] = None
    executor_memory: Optional[str] = None
    executor_cores: Optional[int] = None
    num_executors: Optional[int] = None
    keep_alive_timeout: Optional[int] = None
    conf: Tuple[ConfigEntry, ...] = ()  # sorted: a JSON object has no order

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.driver_memory,
                self.driver_cores,
                self.executor_memory,
                self.executor_cores,
                self.num_executors,
                self.keep_alive_timeout,
                self.conf,
            )
        )

    def to_dict(self) -> dict:
        return {
            "driver_memory": self.driver_memory,
            "driver_cores": self.driver_cores,
            "executor_memory": self.executor_memory,
            "executor_cores": self.executor_cores,
            "num_executors": self.num_executors,
            "keep_alive_timeout": self.keep_alive_timeout,
            "conf": [c.to_dict() for c in self.conf],
        }


@dataclass(frozen=True)
class NotebookDefinition:
    """What one Synapse notebook contains.

    ``cells`` keeps source order, which is semantically meaningful and is
    never sorted. Everything derived from an unordered JSON object —
    configuration, metadata — is sorted by key instead.
    """

    name: str
    format: NotebookFormat
    language: NotebookLanguage
    language_source: LanguageSource
    description: Optional[str] = None
    nbformat: Optional[str] = None
    kernel: Optional[str] = None
    save_output: Optional[bool] = None
    folder: Optional[str] = None
    compute: Optional[ComputeBinding] = None
    session: Optional[SessionConfiguration] = None
    metadata: Tuple[ConfigEntry, ...] = ()
    cells: Tuple[NotebookCell, ...] = ()
    references: Tuple[ArtifactReference, ...] = ()  # notebook-level only
    resources: Tuple[ResourceReference, ...] = ()  # notebook-level only

    # -- aggregates over cells, in cell order -----------------------------

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    @property
    def code_cells(self) -> Tuple[NotebookCell, ...]:
        return tuple(c for c in self.cells if c.cell_type is CellType.CODE)

    @property
    def cell_types(self) -> Tuple[str, ...]:
        return tuple(sorted({c.raw_cell_type for c in self.cells}))

    @property
    def cell_languages(self) -> Tuple[str, ...]:
        return tuple(sorted({c.language.value for c in self.cells}))

    @property
    def findings(self) -> Tuple[CodeFinding, ...]:
        return tuple(f for cell in self.cells for f in cell.findings)

    @property
    def sql_blocks(self) -> Tuple[NotebookSqlBlock, ...]:
        return tuple(s for cell in self.cells for s in cell.sql_blocks)

    @property
    def magics(self) -> Tuple[MagicCommand, ...]:
        return tuple(m for cell in self.cells for m in cell.magics)

    @property
    def packages(self) -> Tuple[PackageFinding, ...]:
        return tuple(p for cell in self.cells for p in cell.packages)

    @property
    def all_resources(self) -> Tuple[ResourceReference, ...]:
        return self.resources + tuple(
            r for cell in self.cells for r in cell.resources
        )

    @property
    def all_references(self) -> Tuple[ArtifactReference, ...]:
        """Structurally declared references. Code-derived links are findings."""
        return self.references

    def findings_by_category(self) -> dict:
        counts: dict = {}
        for finding in self.findings:
            counts[finding.category.value] = counts.get(finding.category.value, 0) + 1
        return dict(sorted(counts.items()))

    def summary(self) -> dict:
        """A source-free overview, safe to log or put in a report.

        Never contains cell source, so a credential written as a literal in a
        committed notebook cannot leak through a summary. The preserved source
        still holds it — see ``to_dict(include_source=False)``.
        """
        return {
            "name": self.name,
            "format": self.format.value,
            "language": self.language.value,
            "nbformat": self.nbformat,
            "kernel": self.kernel,
            "cell_count": self.cell_count,
            "code_cell_count": len(self.code_cells),
            "cell_types": list(self.cell_types),
            "cell_languages": list(self.cell_languages),
            "findings_by_category": self.findings_by_category(),
            "sql_block_count": len(self.sql_blocks),
            "magic_count": len(self.magics),
            "package_count": len(self.packages),
            "resource_count": len(self.all_resources),
            "reference_count": len(self.all_references),
            "compute": self.compute.to_dict() if self.compute else None,
        }

    def to_dict(self, include_source: bool = True) -> dict:
        return {
            "name": self.name,
            "format": self.format.value,
            "language": self.language.value,
            "language_source": self.language_source.value,
            "description": self.description,
            "nbformat": self.nbformat,
            "kernel": self.kernel,
            "save_output": self.save_output,
            "folder": self.folder,
            "compute": self.compute.to_dict() if self.compute else None,
            "session": self.session.to_dict() if self.session else None,
            "metadata": [m.to_dict() for m in self.metadata],
            "cells": [c.to_dict(include_source) for c in self.cells],
            "references": [r.to_dict() for r in self.references],
            "resources": [r.to_dict() for r in self.resources],
        }
