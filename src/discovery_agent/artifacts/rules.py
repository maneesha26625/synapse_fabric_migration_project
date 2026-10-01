"""What the detector knows about Synapse Git repositories.

Pure knowledge: folder conventions, JSON shape predicates, and the negative
rules that keep ordinary repository files out of the artifact inventory. No
I/O, no configuration, no state — so the rule set can be reviewed, extended,
and tested on its own.

Assumed repository layout::

    <root folder>/<artifact type>/<name>.json

The root folder is workspace-configurable and may be absent, so the path
signal reads the file's immediate parent directory at whatever depth it sits.
Synapse does not nest subdirectories under a type folder: the "folders" shown
in Synapse Studio are metadata in ``properties.folder``, not directories.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

from discovery_agent.models import AssetType


class Strength(str, Enum):
    """How much a structural match proves on its own.

    STRONG shapes are unique to one artifact type and classify without help.
    WEAK shapes are generic and are only accepted when the folder agrees.
    """

    STRONG = "strong"
    WEAK = "weak"


@dataclass(frozen=True)
class StructureRule:
    """A JSON shape that identifies one Synapse artifact type."""

    asset_type: AssetType
    folder: str  # canonical Synapse folder name for this type
    source_format: str
    strength: Strength
    signal_value: str  # the key path(s) observed, recorded as evidence
    predicate: Callable[[dict], bool]


@dataclass(frozen=True)
class ExtensionRule:
    """A Synapse artifact identified by file extension rather than JSON shape."""

    asset_type: AssetType
    folder: str
    extensions: Tuple[str, ...]
    source_format: str
    strength: Strength


@dataclass(frozen=True)
class NonSynapseRule:
    """A JSON shape that is definitely not a Synapse artifact."""

    source_format: str
    signal_value: str
    predicate: Callable[[dict], bool]


# --- predicate helpers -------------------------------------------------------

_DATAFLOW_TYPES = ("MappingDataFlow", "Flowlet", "WranglingDataFlow")
_INTEGRATION_RUNTIME_TYPES = ("Managed", "SelfHosted")
_CREDENTIAL_TYPES = ("ManagedIdentity", "ServicePrincipal")


def _properties(document: dict) -> Optional[dict]:
    if not isinstance(document, dict):
        return None
    properties = document.get("properties")
    return properties if isinstance(properties, dict) else None


def _property_type(document: dict) -> Optional[str]:
    properties = _properties(document)
    if properties is None:
        return None
    value = properties.get("type")
    return value if isinstance(value, str) else None


def _is_notebook(document: dict) -> bool:
    properties = _properties(document)
    if properties is None:
        return False
    return "nbformat" in properties or isinstance(properties.get("cells"), list)


def _is_pipeline(document: dict) -> bool:
    properties = _properties(document)
    return properties is not None and isinstance(properties.get("activities"), list)


def _is_dataflow(document: dict) -> bool:
    return _property_type(document) in _DATAFLOW_TYPES


def _is_trigger(document: dict) -> bool:
    property_type = _property_type(document)
    return property_type is not None and property_type.endswith("Trigger")


def _is_sql_script(document: dict) -> bool:
    properties = _properties(document)
    if properties is None:
        return False
    if properties.get("type") == "SqlQuery":
        return True
    content = properties.get("content")
    return isinstance(content, dict) and "query" in content


def _is_spark_job_definition(document: dict) -> bool:
    properties = _properties(document)
    if properties is None:
        return False
    return "targetBigDataPool" in properties or "jobProperties" in properties


def _is_dataset(document: dict) -> bool:
    properties = _properties(document)
    if properties is None or _property_type(document) is None:
        return False
    return "linkedServiceName" in properties


def _is_linked_service(document: dict) -> bool:
    """A linked service, and specifically not its many look-alikes.

    Integration runtimes, data flows, and triggers all share the
    ``{type, typeProperties}`` shape, so each is excluded explicitly rather
    than relying on rule ordering alone.
    """
    properties = _properties(document)
    property_type = _property_type(document)
    if properties is None or property_type is None:
        return False
    if "typeProperties" not in properties:
        return False
    if "linkedServiceName" in properties:  # that would be a dataset
        return False
    if property_type.endswith("Trigger"):
        return False
    if property_type in _DATAFLOW_TYPES:
        return False
    if property_type in _INTEGRATION_RUNTIME_TYPES:
        return False
    return True


def _is_managed_private_endpoint(document: dict) -> bool:
    properties = _properties(document)
    return properties is not None and "privateLinkResourceId" in properties


def _is_integration_runtime(document: dict) -> bool:
    return _property_type(document) in _INTEGRATION_RUNTIME_TYPES


def _is_credential(document: dict) -> bool:
    return _property_type(document) in _CREDENTIAL_TYPES


def _is_managed_virtual_network(document: dict) -> bool:
    if not isinstance(document, dict) or "properties" in document:
        return False
    return "name" in document and isinstance(document.get("type"), str)


def _is_arm_template(document: dict) -> bool:
    if not isinstance(document, dict):
        return False
    return (
        "$schema" in document
        and "resources" in document
        and "contentVersion" in document
    )


def _is_schema_document(document: dict) -> bool:
    """A JSON document governed by a published schema.

    Synapse artifacts never carry a top-level ``$schema``, so its presence on
    anything without the ``{name, properties}`` shape rules the file out.
    """
    if not isinstance(document, dict) or "$schema" not in document:
        return False
    return not ("name" in document and "properties" in document)


# --- the rule set ------------------------------------------------------------

# Evaluated in order, first match wins. The guards above make the outcome
# independent of ordering; the order is for readability and speed.
STRUCTURE_RULES: Tuple[StructureRule, ...] = (
    StructureRule(
        AssetType.NOTEBOOK, "notebook", "synapse_notebook_json", Strength.STRONG,
        "properties.nbformat | properties.cells[]", _is_notebook,
    ),
    StructureRule(
        AssetType.PIPELINE, "pipeline", "synapse_pipeline_json", Strength.STRONG,
        "properties.activities[]", _is_pipeline,
    ),
    StructureRule(
        AssetType.DATAFLOW, "dataflow", "synapse_dataflow_json", Strength.STRONG,
        "properties.type=MappingDataFlow", _is_dataflow,
    ),
    StructureRule(
        AssetType.TRIGGER, "trigger", "synapse_trigger_json", Strength.STRONG,
        "properties.type=*Trigger", _is_trigger,
    ),
    StructureRule(
        AssetType.SQL_SCRIPT, "sqlscript", "synapse_sql_script_json", Strength.STRONG,
        "properties.content.query", _is_sql_script,
    ),
    StructureRule(
        AssetType.SPARK_JOB_DEFINITION, "sparkJobDefinition",
        "synapse_spark_job_definition_json", Strength.STRONG,
        "properties.targetBigDataPool | properties.jobProperties",
        _is_spark_job_definition,
    ),
    StructureRule(
        AssetType.DATASET, "dataset", "synapse_dataset_json", Strength.STRONG,
        "properties.type + properties.linkedServiceName", _is_dataset,
    ),
    StructureRule(
        AssetType.MANAGED_PRIVATE_ENDPOINT, "managedPrivateEndpoint",
        "synapse_managed_private_endpoint_json", Strength.STRONG,
        "properties.privateLinkResourceId", _is_managed_private_endpoint,
    ),
    StructureRule(
        AssetType.LINKED_SERVICE, "linkedService", "synapse_linked_service_json",
        Strength.STRONG, "properties.type + properties.typeProperties",
        _is_linked_service,
    ),
    StructureRule(
        AssetType.INTEGRATION_RUNTIME, "integrationRuntime",
        "synapse_integration_runtime_json", Strength.WEAK,
        "properties.type=Managed|SelfHosted", _is_integration_runtime,
    ),
    StructureRule(
        AssetType.CREDENTIAL, "credential", "synapse_credential_json", Strength.WEAK,
        "properties.type=ManagedIdentity|ServicePrincipal", _is_credential,
    ),
    StructureRule(
        AssetType.MANAGED_VIRTUAL_NETWORK, "managedVirtualNetwork",
        "synapse_managed_virtual_network_json", Strength.WEAK,
        "name + type, no properties", _is_managed_virtual_network,
    ),
)

EXTENSION_RULES: Tuple[ExtensionRule, ...] = (
    ExtensionRule(
        AssetType.KQL_SCRIPT, "kqlscript", (".kql",), "kql_script", Strength.WEAK
    ),
)

NON_SYNAPSE_RULES: Tuple[NonSynapseRule, ...] = (
    NonSynapseRule(
        "arm_template_json", "$schema + resources + contentVersion", _is_arm_template
    ),
    NonSynapseRule(
        "json_schema_document", "$schema without name+properties", _is_schema_document
    ),
)

# Folder name (lowercased) -> the artifact type stored there. A folder mapped
# to None is a recognized Synapse folder whose contents we do not support yet.
SYNAPSE_FOLDERS: Dict[str, Optional[AssetType]] = {
    "notebook": AssetType.NOTEBOOK,
    "pipeline": AssetType.PIPELINE,
    "dataset": AssetType.DATASET,
    "linkedservice": AssetType.LINKED_SERVICE,
    "trigger": AssetType.TRIGGER,
    "sqlscript": AssetType.SQL_SCRIPT,
    "dataflow": AssetType.DATAFLOW,
    "sparkjobdefinition": AssetType.SPARK_JOB_DEFINITION,
    "integrationruntime": AssetType.INTEGRATION_RUNTIME,
    "credential": AssetType.CREDENTIAL,
    "managedvirtualnetwork": AssetType.MANAGED_VIRTUAL_NETWORK,
    "managedprivateendpoint": AssetType.MANAGED_PRIVATE_ENDPOINT,
    "kqlscript": AssetType.KQL_SCRIPT,
    "powerbitemplate": None,
    "database": None,
    "sparkconfiguration": None,
}

# Ordinary repository content: documentation, assets, and data files.
NON_SYNAPSE_EXTENSIONS: Tuple[str, ...] = (
    ".md", ".txt", ".rst", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".csv", ".tsv", ".parquet", ".yml", ".yaml", ".xml", ".html", ".css",
    ".sh", ".ps1", ".bat", ".gitignore", ".gitattributes", ".zip",
)

NON_SYNAPSE_FILE_NAMES: Tuple[str, ...] = ("LICENSE", "NOTICE", "CODEOWNERS")


def folder_asset_type(folder_name: str) -> Tuple[bool, Optional[AssetType]]:
    """Look up a directory name. Returns (is a Synapse folder, type or None)."""
    key = folder_name.lower()
    if key not in SYNAPSE_FOLDERS:
        return False, None
    return True, SYNAPSE_FOLDERS[key]
