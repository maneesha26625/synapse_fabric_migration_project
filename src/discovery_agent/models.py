"""Canonical data model for Discovery output.

This module is the contract between the Discovery Agent and everything
downstream (Assessment, reporting, migration workers). Only the stable core
lives here; per-asset detail fields are added incrementally as each extractor
is built.

Two rules govern this model:
  * Asset IDs are stable across runs, so two scans can be diffed.
  * Every derived claim carries Evidence pointing back at a source file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

SCHEMA_VERSION = "0.1.0"


class AssetType(str, Enum):
    """Asset kinds found in a Synapse Git-integrated repository.

    Values match the repository folder names that Synapse publishes, so a
    folder can be mapped to a type directly.
    """

    PIPELINE = "pipeline"
    NOTEBOOK = "notebook"
    SQL_SCRIPT = "sqlscript"
    KQL_SCRIPT = "kqlscript"
    DATASET = "dataset"
    LINKED_SERVICE = "linkedService"
    TRIGGER = "trigger"
    DATAFLOW = "dataflow"
    SPARK_JOB_DEFINITION = "sparkJobDefinition"
    INTEGRATION_RUNTIME = "integrationRuntime"
    CREDENTIAL = "credential"
    MANAGED_PRIVATE_ENDPOINT = "managedPrivateEndpoint"
    MANAGED_VIRTUAL_NETWORK = "managedVirtualNetwork"


def asset_id(asset_type: AssetType, name: str) -> str:
    """Build the stable identifier for an asset: ``synapse://<type>/<name>``."""
    return f"synapse://{asset_type.value}/{name}"


@dataclass(frozen=True)
class Evidence:
    """Where a piece of extracted information came from."""

    source_file: str
    line: int | None = None
    extractor: str = ""


@dataclass
class Asset:
    """One normalized asset from the source repository."""

    id: str
    type: AssetType
    name: str
    source_file: str
    source_sha256: str
    properties: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    """A directed dependency between two assets."""

    from_id: str
    to_id: str
    kind: str
    evidence: Evidence | None = None
