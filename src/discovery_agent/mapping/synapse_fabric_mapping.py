"""Synapse -> Microsoft Fabric: the preliminary, rule-based mapping.

Discovery answers two questions about every object: *what is it* and *which
Fabric area does it appear to correspond to*. This module answers the second,
and only as far as it can be answered from the object's type (plus a few
facts already extracted, such as whether a notebook uses Synapse-specific
APIs).

Ground rules, all enforced by the shape of the code rather than by comments:

* **Deterministic.** A fixed table keyed by source type. No model, no
  heuristics over names, no randomness. The same input gives the same output.
* **Preliminary.** ``assessment_required`` is True for every rule. Nothing
  here says an object *will* migrate, is compatible, or needs no changes;
  those are Assessment's verdicts. A "Direct Target" means a recognisable
  Fabric component exists, not that the object moves across unchanged.
* **No invention.** An unknown type, or one whose Fabric home depends on how
  the data will be consumed, maps to "Requires Assessment" rather than to a
  guess.
* **Source facts only.** Nothing in this module reads, creates or changes any
  Synapse or Fabric object.

The UI never decides any of this; it renders what this module returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Tuple

# --- vocabulary --------------------------------------------------------------

DIRECT = "Direct Target"
TRANSFORMATION = "Target With Transformation"
REFACTORING = "Target With Refactoring"
RECONFIGURATION = "Requires Reconfiguration"
ASSESSMENT = "Requires Assessment"
MANUAL = "Manual / Special Handling"

MIGRATION_PATHS: Tuple[str, ...] = (
    DIRECT,
    TRANSFORMATION,
    REFACTORING,
    RECONFIGURATION,
    ASSESSMENT,
    MANUAL,
)

#: The short classification codes the UI shows as badges. A display vocabulary
#: over the six paths -- it adds no information and is owned here so the UI
#: never maps anything itself. ``NOT_SUPPORTED`` is never assigned by
#: Discovery: only Assessment can say an object has no Fabric route.
CLASS_DIRECT = "DIRECT"
CLASS_TRANSFORM = "TRANSFORM"
CLASS_RECONFIGURE = "RECONFIGURE"
CLASS_REVIEW = "REVIEW"
CLASS_MANUAL = "MANUAL"
CLASS_NOT_SUPPORTED = "NOT SUPPORTED"

CLASSIFICATIONS: Tuple[str, ...] = (
    CLASS_DIRECT, CLASS_RECONFIGURE, CLASS_TRANSFORM, CLASS_MANUAL, CLASS_REVIEW, CLASS_NOT_SUPPORTED,
)

_CLASS_FOR_PATH: Mapping[str, str] = {
    DIRECT: CLASS_DIRECT,
    TRANSFORMATION: CLASS_TRANSFORM,
    REFACTORING: CLASS_TRANSFORM,
    RECONFIGURATION: CLASS_RECONFIGURE,
    ASSESSMENT: CLASS_REVIEW,
    MANUAL: CLASS_MANUAL,
}

#: The second, separate status the UI shows: how far the *mapping* got. It is
#: derived from the path and says nothing about whether migration will work.
STATUS_MAPPED = "Mapped"
STATUS_MAPPED_TRANSFORM = "Mapped with Transformation"
STATUS_NEEDS_ASSESSMENT = "Requires Assessment"
STATUS_NO_AUTOMATIC = "No Automatic Mapping"

_STATUS_FOR_PATH: Mapping[str, str] = {
    DIRECT: STATUS_MAPPED,
    TRANSFORMATION: STATUS_MAPPED_TRANSFORM,
    REFACTORING: STATUS_MAPPED_TRANSFORM,
    RECONFIGURATION: STATUS_MAPPED_TRANSFORM,
    ASSESSMENT: STATUS_NEEDS_ASSESSMENT,
    MANUAL: STATUS_NO_AUTOMATIC,
}

#: Whether an automated route could plausibly exist, stated neutrally. Not a
#: difficulty rating and not a promise.
AUTOMATION_CANDIDATE = "Candidate"
AUTOMATION_PARTIAL = "Partial"
AUTOMATION_MANUAL = "Manual"
AUTOMATION_UNDETERMINED = "Not determined"

WS_WAREHOUSE = "Data Warehouse"
WS_ENGINEERING = "Data Engineering"
WS_FACTORY = "Data Factory"
WS_STORAGE = "OneLake / Storage"
WS_CONNECTIONS = "Connections"
WS_SECURITY = "Security & Governance"
WS_UNASSIGNED = "Unassigned"

WORKSTREAMS: Tuple[str, ...] = (
    WS_WAREHOUSE,
    WS_ENGINEERING,
    WS_FACTORY,
    WS_STORAGE,
    WS_CONNECTIONS,
    WS_SECURITY,
    WS_UNASSIGNED,
)

PLATFORM = "Microsoft Fabric"


@dataclass(frozen=True)
class Rule:
    fabric_target: str
    target_type: str
    workstream: str
    path: str
    automation: str
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FabricMapping:
    """The preliminary target for one source object."""

    source_type: str
    fabric_target: str
    target_type: str
    workstream: str
    migration_path: str
    automation_potential: str
    assessment_required: bool
    mapping_status: str
    migration_route: str  # "Synapse Notebook -> Fabric Notebook"
    notes: Tuple[str, ...]
    classification: str = CLASS_REVIEW
    action: str = ""
    steps: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "target": {
                "platform": PLATFORM,
                "component": self.fabric_target,
                "componentType": self.target_type,
            },
            "migration": {
                "path": self.migration_path,
                "route": self.migration_route,
                "assessmentRequired": self.assessment_required,
                "automationPotential": self.automation_potential,
                "mappingStatus": self.mapping_status,
                "workstream": self.workstream,
                "classification": self.classification,
                "action": self.action,
            },
            "notes": list(self.notes),
            "steps": list(self.steps),
        }


# --- the rules ---------------------------------------------------------------

_T_SQL_NOTE = (
    "Fabric Warehouse supports a different T-SQL surface area and different "
    "data types than a Synapse dedicated SQL pool; compatibility is decided "
    "in Assessment."
)

RULES: Dict[str, Rule] = {
    # ---- SQL -----------------------------------------------------------
    "Dedicated SQL Pool": Rule(
        "Fabric Data Warehouse", "Warehouse", WS_WAREHOUSE, TRANSFORMATION, AUTOMATION_PARTIAL,
        (_T_SQL_NOTE, "Microsoft documents a migration route from dedicated SQL pools to Fabric Data Warehouse."),
    ),
    "Schema": Rule(
        "Fabric Warehouse Schema", "Warehouse Schema", WS_WAREHOUSE, DIRECT, AUTOMATION_CANDIDATE,
        ("A target schema exists; objects inside it carry their own mapping.",),
    ),
    "Table": Rule(
        "Fabric Warehouse Table", "Warehouse Table", WS_WAREHOUSE, TRANSFORMATION, AUTOMATION_PARTIAL,
        (
            "Synapse distribution, partition and index options are recorded; Fabric Warehouse manages physical design differently.",
            _T_SQL_NOTE,
        ),
    ),
    "View": Rule(
        "Fabric Warehouse View", "Warehouse View", WS_WAREHOUSE, DIRECT, AUTOMATION_CANDIDATE,
        ("A target exists; the view's SQL is not verified here.", _T_SQL_NOTE),
    ),
    "Stored Procedure": Rule(
        "Fabric Warehouse Stored Procedure", "Warehouse Stored Procedure", WS_WAREHOUSE, REFACTORING, AUTOMATION_PARTIAL,
        ("Procedure code may need changes because of T-SQL surface-area differences.",),
    ),
    "Function": Rule(
        "Fabric Warehouse Function", "Warehouse Function", WS_WAREHOUSE, REFACTORING, AUTOMATION_PARTIAL,
        ("Supported only where Fabric Warehouse supports the function type.",),
    ),
    "SQL Script": Rule(
        "Fabric Data Warehouse (SQL query)", "Warehouse SQL Query", WS_WAREHOUSE, ASSESSMENT, AUTOMATION_UNDETERMINED,
        ("The target depends on which pool the script runs against and what it does.",),
    ),
    "External Table": Rule(
        "Requires Assessment (Fabric Warehouse or Fabric Lakehouse)", "Undetermined", WS_STORAGE, ASSESSMENT, AUTOMATION_UNDETERMINED,
        ("The right Fabric target depends on how the data will be consumed; no single component is assumed.",),
    ),
    "External Data Source": Rule(
        "Fabric Connection / OneLake shortcut / Data Factory connection", "Connection or shortcut", WS_CONNECTIONS, ASSESSMENT, AUTOMATION_UNDETERMINED,
        ("No one-to-one Fabric object exists; the mapping depends on the workload.",),
    ),
    "External File Format": Rule(
        "Ingestion configuration (Data Factory / Lakehouse / Warehouse)", "Configuration", WS_STORAGE, ASSESSMENT, AUTOMATION_UNDETERMINED,
        ("Fabric has no workspace item for a file format; it becomes part of ingestion configuration.",),
    ),
    # ---- Spark ---------------------------------------------------------
    "Spark Pool": Rule(
        "Fabric Spark pool / Environment configuration", "Environment", WS_ENGINEERING, TRANSFORMATION, AUTOMATION_PARTIAL,
        ("Spark version, node sizes, custom Spark configuration and libraries may need manual configuration in Fabric.",),
    ),
    "Spark Library": Rule(
        "Fabric Environment libraries", "Environment library", WS_ENGINEERING, ASSESSMENT, AUTOMATION_UNDETERMINED,
        ("Package availability and versions are checked in Assessment.",),
    ),
    "Notebook": Rule(
        "Fabric Notebook", "Notebook", WS_ENGINEERING, DIRECT, AUTOMATION_CANDIDATE,
        ("A Fabric Notebook is the target. Synapse-specific code may need changes; the notebook is not modified by Discovery.",),
    ),
    "Spark Job Definition": Rule(
        "Fabric Spark Job Definition", "Spark Job Definition", WS_ENGINEERING, DIRECT, AUTOMATION_CANDIDATE,
        ("The main file and referenced libraries are checked in Assessment.",),
    ),
    "Lake Database": Rule(
        "Fabric Lakehouse", "Lakehouse", WS_ENGINEERING, TRANSFORMATION, AUTOMATION_PARTIAL,
        ("Lake database schemas can map to schemas within a Fabric Lakehouse.",),
    ),
    # ---- Integration ---------------------------------------------------
    "Pipeline": Rule(
        "Fabric Data Factory Pipeline", "Data Pipeline", WS_FACTORY, TRANSFORMATION, AUTOMATION_PARTIAL,
        ("Activities, datasets and linked-service references are re-pointed to Fabric connections; each activity is mapped separately.",),
    ),
    "Dataset": Rule(
        "Fabric Connection / pipeline activity configuration", "Connection settings", WS_CONNECTIONS, RECONFIGURATION, AUTOMATION_PARTIAL,
        ("Fabric Data Factory does not use a one-to-one dataset object; the settings move into connections and activities.",),
    ),
    "Linked Service": Rule(
        "Fabric Connection", "Connection", WS_CONNECTIONS, RECONFIGURATION, AUTOMATION_PARTIAL,
        (
            "Fabric uses a different connection model than Synapse linked services.",
            "In Spark code, linked-service references may need direct authentication instead.",
        ),
    ),
    "Trigger": Rule(
        "Fabric Data Factory pipeline schedule / trigger", "Schedule or trigger", WS_FACTORY, ASSESSMENT, AUTOMATION_PARTIAL,
        ("Trigger types do not all have the same Fabric equivalent; checked in Assessment.",),
    ),
    # ---- Storage -------------------------------------------------------
    "Storage Reference": Rule(
        "OneLake (shortcut or copy)", "OneLake", WS_STORAGE, ASSESSMENT, AUTOMATION_PARTIAL,
        ("Could become an ADLS Gen2 shortcut in OneLake, or be copied into a Lakehouse; Assessment decides.",),
    ),
    # ---- Security / networking ------------------------------------------
    "Security Object": Rule(
        "Fabric security model", "Security", WS_SECURITY, ASSESSMENT, AUTOMATION_MANUAL,
        ("Synapse and Fabric security models differ; migration is a separate assessment.",),
    ),
    "Integration Runtime": Rule(
        "Fabric connection / data gateway configuration", "Network configuration", WS_CONNECTIONS, MANUAL, AUTOMATION_MANUAL,
        ("Network access (managed VNet, self-hosted runtimes) is configured differently in Fabric.",),
    ),
    "Networking Configuration": Rule(
        "Fabric network configuration", "Network configuration", WS_SECURITY, MANUAL, AUTOMATION_MANUAL,
        ("Private endpoints and firewall settings need separate handling.",),
    ),
}

RULES["Serverless SQL"] = Rule(
    "Fabric SQL analytics endpoint / Warehouse / Lakehouse SQL endpoint", "SQL endpoint", WS_WAREHOUSE, ASSESSMENT, AUTOMATION_UNDETERMINED,
    ("Serverless workloads depend on OPENROWSET and external data; the right Fabric endpoint depends on the workload.",),
)

#: What migrating each type involves, in one line (the "migration action").
ACTIONS: Dict[str, str] = {
    "Dedicated SQL Pool": "Migrate schema + tables + SQL",
    "Serverless SQL": "Review workload and redesign queries where required",
    "Schema": "Create the schema in the Fabric Warehouse",
    "Table": "Recreate the table definition and load its data",
    "External Table": "Assess storage and access pattern",
    "View": "Recreate the view after its tables exist",
    "Stored Procedure": "Recreate the procedure, adjusting T-SQL where Fabric differs",
    "Function": "Recreate the function where Fabric supports it",
    "SQL Script": "Review the script and decide where it runs in Fabric",
    "External Data Source": "Reconfigure as a Fabric connection or OneLake shortcut",
    "External File Format": "Carry the format into ingestion configuration",
    "Spark Pool": "Reconfigure compute/runtime",
    "Spark Library": "Add the package to a Fabric Environment",
    "Notebook": "Migrate notebook + dependencies",
    "Spark Job Definition": "Reconfigure execution",
    "Lake Database": "Map to a Fabric Lakehouse and its schemas",
    "Pipeline": "Migrate pipeline activities and connections",
    "Dataset": "Review based on usage (pipeline, Dataflow Gen2 or connection)",
    "Linked Service": "Recreate connection",
    "Trigger": "Recreate schedule or event trigger",
    "Storage Reference": "Choose between an OneLake shortcut and a copy",
    "Security Object": "Remap users, groups and permissions",
    "Integration Runtime": "Manual / reconfiguration assessment",
    "Networking Configuration": "Manual / reconfiguration assessment",
}

GENERIC_STEPS: Tuple[str, ...] = (
    "Review the discovered definition",
    "Recreate or reconfigure the object in Fabric",
    "Validate the result against the source",
)

_SQL_CODE_STEPS: Tuple[str, ...] = (
    "Export the object definition from Synapse",
    "Adjust T-SQL that Fabric Warehouse does not support",
    "Create the object in the Fabric Warehouse",
    "Validate results against the source",
)

#: The ordered steps shown in the object detail panel. Guidance only: nothing
#: here is executed, and Assessment still decides what each step involves.
STEPS: Dict[str, Tuple[str, ...]] = {
    "Pipeline": (
        "Export the Synapse pipeline definition",
        "Convert supported activities",
        "Map linked services to Fabric connections",
        "Recreate unsupported activities",
        "Deploy to Fabric",
        "Validate execution",
    ),
    "Notebook": (
        "Export the notebook",
        "Replace Synapse-specific APIs where present",
        "Attach a Fabric Environment and Lakehouse",
        "Import into the Fabric workspace",
        "Validate a run",
    ),
    "Table": (
        "Export the table definition",
        "Create the table in the Fabric Warehouse",
        "Load the data (copy or shortcut)",
        "Compare row counts and data types",
    ),
    "View": _SQL_CODE_STEPS,
    "Stored Procedure": _SQL_CODE_STEPS,
    "Function": _SQL_CODE_STEPS,
    "Linked Service": (
        "Identify the target system and authentication",
        "Create a Fabric connection",
        "Re-point the dependent pipelines and notebooks",
        "Test the connection",
    ),
    "Dataset": (
        "Identify the pipelines that use the dataset",
        "Move its settings into the pipeline activity or connection",
        "Validate the activity",
    ),
    "Spark Pool": (
        "Record Spark version, node size and autoscale settings",
        "Create a Fabric Environment with matching settings",
        "Add libraries and custom configuration",
    ),
    "Spark Job Definition": (
        "Export the job definition and main file",
        "Create the Fabric Spark Job Definition",
        "Attach libraries and an Environment",
        "Validate a run",
    ),
    "Trigger": (
        "Record the schedule or event source",
        "Recreate it on the migrated pipeline",
        "Keep it disabled until validation passes",
    ),
}
#: Synapse-specific notebook constructs that point at code changes. Matches the
#: categories the notebook extractor already reports.
SYNAPSE_SPECIFIC_NOTEBOOK_CATEGORIES = frozenset(
    {"synapse_utils", "synapse_sql_connector", "linked_service_api"}
)

#: Used for any type not in ``RULES``. Deliberately the least committal answer.
_FALLBACK = Rule(
    "Requires Assessment", "Undetermined", WS_UNASSIGNED, ASSESSMENT, AUTOMATION_UNDETERMINED,
    ("No mapping rule exists for this object type.",),
)


def map_synapse_object(
    source_type: str, facts: Optional[Mapping[str, object]] = None
) -> FabricMapping:
    """The preliminary Fabric target for one source object.

    ``facts`` carries only what was already extracted and changes the path in
    two named cases:

    * ``synapse_specific`` (a notebook): constructs such as ``mssparkutils``
      were found, so the path is "Target With Refactoring".
    * ``is_external`` (a table): handled by the caller choosing the
      "External Table" source type, not here.
    """
    facts = facts or {}
    rule = RULES.get(source_type, _FALLBACK)
    path = rule.path
    notes = list(rule.notes)

    if source_type == "Notebook":
        constructs = tuple(facts.get("synapse_specific") or ())
        if constructs:
            path = REFACTORING
            notes.append(
                "Synapse-specific constructs were found: "
                + ", ".join(sorted(set(constructs)))
                + ". They are identified only; the notebook is not changed."
            )

    return FabricMapping(
        source_type=source_type,
        fabric_target=rule.fabric_target,
        target_type=rule.target_type,
        workstream=rule.workstream,
        migration_path=path,
        automation_potential=rule.automation,
        assessment_required=True,  # always: Discovery never decides compatibility
        mapping_status=_STATUS_FOR_PATH[path],
        migration_route=f"Synapse {source_type} → {rule.fabric_target}",
        notes=tuple(notes),
        classification=_CLASS_FOR_PATH[path],
        action=ACTIONS.get(source_type, "Review the object and decide how it is recreated in Fabric."),
        steps=STEPS.get(source_type, GENERIC_STEPS),
    )


# --- pipeline activities -------------------------------------------------------


@dataclass(frozen=True)
class ActivityMapping:
    fabric_equivalent: str
    equivalence: str  # "Known equivalent" | "Requires Assessment"
    requires_transformation: bool
    requires_manual_review: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "fabricEquivalent": self.fabric_equivalent,
            "equivalence": self.equivalence,
            "requiresTransformation": self.requires_transformation,
            "requiresManualReview": self.requires_manual_review,
            "note": self.note,
        }


_KNOWN = "Known equivalent"
_ASSESS = "Requires Assessment"
_RECONNECT = "Connection references are re-pointed to Fabric connections."


def _a(name: str, transform: bool, manual: bool, note: str) -> ActivityMapping:
    return ActivityMapping(name, _KNOWN, transform, manual, note)


#: Synapse activity type -> Fabric Data Factory activity. Only equivalents that
#: are well established are listed; everything else is "Requires Assessment".
ACTIVITY_RULES: Dict[str, ActivityMapping] = {
    "Copy": _a("Copy activity", True, False, _RECONNECT),
    "Lookup": _a("Lookup activity", True, False, _RECONNECT),
    "GetMetadata": _a("Get Metadata activity", True, False, _RECONNECT),
    "Script": _a("Script activity", True, False, _RECONNECT),
    "Delete": _a("Delete data activity", True, False, _RECONNECT),
    "SqlPoolStoredProcedure": _a(
        "Stored procedure activity", True, True,
        "The procedure must exist in the target Fabric Warehouse; the connection is re-pointed.",
    ),
    "SqlServerStoredProcedure": _a(
        "Stored procedure activity", True, True,
        "Runs in the migrated Fabric Warehouse when its linked service is the dedicated pool; "
        "otherwise through the Fabric connection made from that linked service.",
    ),
    "SynapseNotebook": _a(
        "Notebook activity", True, True,
        "References a Fabric Notebook, which must exist as a migrated target.",
    ),
    "SparkJob": _a(
        "Spark Job Definition activity", True, True,
        "References a Fabric Spark Job Definition, which must exist as a migrated target.",
    ),
    "WebActivity": _a("Web activity", False, True, "URL, headers and authentication need review."),
    "WebHook": _a("Webhook activity", False, True, "URL and authentication need review."),
    "ExecutePipeline": _a("Invoke pipeline activity", True, False, "Points at another pipeline, which is migrated separately."),
    "ForEach": _a("ForEach activity", False, False, "Control-flow activity; its children are mapped individually."),
    "IfCondition": _a("If Condition activity", False, False, "Control-flow activity; its children are mapped individually."),
    "Switch": _a("Switch activity", False, False, "Control-flow activity; its children are mapped individually."),
    "Until": _a("Until activity", False, False, "Control-flow activity; its children are mapped individually."),
    "Wait": _a("Wait activity", False, False, ""),
    "SetVariable": _a("Set Variable activity", False, False, ""),
    "AppendVariable": _a("Append Variable activity", False, False, ""),
    "Filter": _a("Filter activity", False, False, ""),
    "Fail": _a("Fail activity", False, False, ""),
}

_ACTIVITY_FALLBACK = ActivityMapping(
    "Requires Assessment",
    _ASSESS,
    False,
    True,
    "No established Fabric equivalent is recorded for this activity type.",
)


def map_activity(activity_type: str) -> ActivityMapping:
    return ACTIVITY_RULES.get(activity_type, _ACTIVITY_FALLBACK)


# --- references (for the conceptual target tree) ----------------------------------

_REFERENCE_TARGETS: Mapping[str, str] = {
    "compute": "Fabric Spark / Environment (or Fabric-managed compute)",
    "storage_path": "OneLake",
    "secret": "Fabric Connection credential",
    "sql_object": "Fabric Warehouse object",
    "external_endpoint": "Fabric Connection",
}


def reference_target(kind: str, source_type: Optional[str]) -> Optional[str]:
    """The Fabric component a dependency conceptually lands on.

    An artifact reference uses its own type's rule; a non-artifact reference
    (storage, compute, secret...) uses the kind table. None when unknown.
    """
    if source_type and source_type in RULES:
        return RULES[source_type].fabric_target
    return _REFERENCE_TARGETS.get(kind)


def supported_source_types() -> Iterable[str]:
    return RULES.keys()


#: Display order for the component mapping table, following how a migration
#: is usually discussed: SQL first, then Spark, then Data Factory, then the rest.
_COMPONENT_ORDER: Tuple[str, ...] = (
    "Dedicated SQL Pool", "Serverless SQL", "Schema", "Table", "External Table", "View",
    "Stored Procedure", "Function", "SQL Script", "External Data Source", "External File Format",
    "Pipeline", "Dataset", "Linked Service", "Trigger",
    "Spark Pool", "Notebook", "Spark Job Definition", "Spark Library", "Lake Database",
    "Storage Reference", "Security Object", "Integration Runtime", "Networking Configuration",
)


def component_table() -> list:
    """Every Synapse component with its preliminary Fabric component and action."""
    rows = []
    for source_type in _COMPONENT_ORDER:
        m = map_synapse_object(source_type)
        rows.append(
            {
                "sourceType": source_type,
                "fabricTarget": m.fabric_target,
                "targetType": m.target_type,
                "migrationPath": m.migration_path,
                "classification": m.classification,
                "action": m.action,
                "workstream": m.workstream,
                "notes": list(m.notes),
            }
        )
    return rows
