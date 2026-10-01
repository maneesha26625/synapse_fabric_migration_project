"""Discovery results, reshaped for the UI, with the preliminary Fabric mapping.

Two kinds of inventory entry meet here:

* **Records** -- the existing ``UnifiedDiscoveryRecord`` for the nine P0
  artifacts. Unchanged; this module only reads them.
* **Extras** -- objects the record model does not cover (Spark pools, SQL
  pools, triggers, libraries, integration runtimes, schemas, storage
  references). Plain data, built from facts the connection layer returned.

Both get the same treatment: a source type, a category, and a Fabric mapping
from ``discovery_agent.mapping``. The mapping rules live there and nowhere
else; this module only attaches the result.

Discovery reports what exists and where it appears to land in Fabric. It does
not judge compatibility, effort or readiness, and nothing here is invented:
the backend does not know when an artifact was created, so those fields are
absent rather than fabricated.

Every string in a detail payload goes through ``redact`` on the way out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from discovery_agent.connections.validation import redact
from discovery_agent.discovery_models import UnifiedDiscoveryRecord
from discovery_agent.extractors.models import ExtractionStatus, IssueCode
from discovery_agent.mapping import (
    FabricMapping,
    map_activity,
    map_synapse_object,
    reference_target,
)
from discovery_agent.mapping.synapse_fabric_mapping import SYNAPSE_SPECIFIC_NOTEBOOK_CATEGORIES

#: P0 artifact value -> (source type, category).
ARTIFACT_INFO: Dict[str, Tuple[str, str]] = {
    "dedicated_sql_table": ("Table", "SQL"),
    "sql_view": ("View", "SQL"),
    "stored_procedure": ("Stored Procedure", "SQL"),
    "sqlscript": ("SQL Script", "SQL"),
    "pipeline": ("Pipeline", "Integration"),
    "dataset": ("Dataset", "Integration"),
    "linkedService": ("Linked Service", "Integration"),
    "notebook": ("Notebook", "Spark"),
    "sparkJobDefinition": ("Spark Job Definition", "Spark"),
}

#: Source type -> category, for every type the mapping engine knows.
TYPE_CATEGORY: Dict[str, str] = {
    "Dedicated SQL Pool": "SQL", "Schema": "SQL", "Table": "SQL", "External Table": "SQL",
    "View": "SQL", "Stored Procedure": "SQL", "Function": "SQL", "SQL Script": "SQL",
    "External Data Source": "SQL", "External File Format": "SQL",
    "Spark Pool": "Spark", "Spark Library": "Spark", "Notebook": "Spark",
    "Spark Job Definition": "Spark", "Lake Database": "Spark",
    "Pipeline": "Integration", "Dataset": "Integration", "Linked Service": "Integration",
    "Trigger": "Integration",
    "Storage Reference": "Storage",
    "Security Object": "Security",
    "Integration Runtime": "Networking", "Networking Configuration": "Networking",
}

#: The Synapse component an object belongs to, for the "Synapse Component" column.
COMPONENT_BY_TYPE: Dict[str, str] = {
    "Dedicated SQL Pool": "Dedicated SQL Pool", "Schema": "Dedicated SQL Pool", "Table": "Dedicated SQL Pool",
    "External Table": "Dedicated SQL Pool", "View": "Dedicated SQL Pool", "Stored Procedure": "Dedicated SQL Pool",
    "Function": "Dedicated SQL Pool", "SQL Script": "Synapse SQL", "External Data Source": "Dedicated SQL Pool",
    "External File Format": "Dedicated SQL Pool", "Spark Pool": "Apache Spark", "Spark Library": "Apache Spark",
    "Notebook": "Apache Spark", "Spark Job Definition": "Apache Spark", "Lake Database": "Apache Spark",
    "Pipeline": "Synapse Pipelines", "Dataset": "Synapse Pipelines", "Linked Service": "Synapse Pipelines",
    "Trigger": "Synapse Pipelines", "Storage Reference": "Storage (ADLS Gen2)", "Security Object": "Security",
    "Integration Runtime": "Integration Runtime", "Networking Configuration": "Networking",
}

CATEGORIES = ("SQL", "Spark", "Integration", "Storage", "Security", "Networking", "Other")

#: Object types the discovery cannot list in this build, and why. The mapping
#: rules for them exist, so adding the discovery needs no mapping work.
NOT_DISCOVERED: Tuple[Tuple[str, str], ...] = (
    ("Function", "the SQL catalog queries cover tables, views and stored procedures only"),
    ("External Data Source", "not yet read from the SQL catalog"),
    ("External File Format", "not yet read from the SQL catalog"),
    ("Lake Database", "lake databases are not yet listed"),
    ("Security Object", "users, roles and permissions are not yet read"),
    ("Networking Configuration", "managed private endpoints and firewall rules are not yet read"),
)

STATUS_DISCOVERED = "Discovered"
STATUS_WARNING = "Warning"
STATUS_PARTIAL = "Partial"
STATUS_UNAVAILABLE = "Failed"

#: Issue codes meaning "found, but not fully understood". ``missing_information``
#: is deliberately absent: a workspace-only record legitimately lacks facets a
#: repository would have supplied, and flagging every one would be noise.
_WARNING_CODES = {
    IssueCode.OPAQUE_DEFINITION.value,
    IssueCode.MALFORMED_ARTIFACT.value,
    IssueCode.EXTRACTION_FAILURE.value,
    IssueCode.UNSUPPORTED_CONSTRUCT.value,
    "drift_detected",
}


def type_and_category(artifact: str) -> Tuple[str, str]:
    return ARTIFACT_INFO.get(artifact, (artifact, "Other"))


def sanitize(value: Any) -> Any:
    """Make a value JSON-safe and credential-free."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "value") and isinstance(getattr(value, "value"), (str, int)):
        return sanitize(value.value)  # an Enum
    return redact(str(value))


# --- the mapping, attached -------------------------------------------------------


def _mapping_fields(m: FabricMapping) -> dict:
    """The flat fields every row carries, straight from the mapping engine."""
    return {
        "fabricTarget": m.fabric_target,
        "targetType": m.target_type,
        "migrationPath": m.migration_path,
        "automationPotential": m.automation_potential,
        "assessmentRequired": m.assessment_required,
        "mappingStatus": m.mapping_status,
        "workstream": m.workstream,
        "classification": m.classification,
        "action": m.action,
    }


def _record_source_type(record: UnifiedDiscoveryRecord) -> str:
    base = type_and_category(record.artifact.value)[0]
    if base == "Table" and getattr(record.content, "is_external", None) is True:
        return "External Table"
    return base


def _synapse_specific(record: UnifiedDiscoveryRecord) -> Tuple[str, ...]:
    """Synapse-only notebook constructs the extractor already found."""
    if record.artifact.value != "notebook" or record.content is None:
        return ()
    found = set()
    for cell in getattr(record.content, "cells", ()) or ():
        for finding in getattr(cell, "findings", ()) or ():
            if finding.category.value in SYNAPSE_SPECIFIC_NOTEBOOK_CATEGORIES:
                found.add(finding.construct)
    return tuple(sorted(found))


def record_mapping(record: UnifiedDiscoveryRecord) -> FabricMapping:
    return map_synapse_object(
        _record_source_type(record), {"synapse_specific": _synapse_specific(record)}
    )


def _status(record: UnifiedDiscoveryRecord) -> str:
    definition = record.definition
    if definition is None or not record.has_usable_definition:
        return STATUS_UNAVAILABLE
    if definition.status is ExtractionStatus.PARTIAL:
        return STATUS_PARTIAL
    if any(issue.code.value in _WARNING_CODES for issue in record.all_issues):
        return STATUS_WARNING
    return STATUS_DISCOVERED


def _dependencies(record: UnifiedDiscoveryRecord, known: Mapping[str, str]) -> List[dict]:
    """The references an artifact makes, joined onto the run's own objects.

    ``objectId`` is set only when the target is another object in this run.
    A reference that cannot be joined is still listed, unresolved. Each entry
    also names the Fabric component it conceptually lands on, for the
    mapping view; that is a label, not a created object.
    """
    seen = set()
    out: List[dict] = []
    for ref in record.references:
        key = (ref.kind.value, ref.target_name, ref.target_type.value if ref.target_type else None)
        if key in seen:
            continue
        seen.add(key)
        type_name = type_and_category(ref.target_type.value)[0] if ref.target_type else None
        object_id = known.get(ref.target_id) if ref.target_id else None
        if object_id is None and ref.kind.value == "compute":
            object_id = known.get(f"compute:{ref.target_name}")
            if object_id and type_name is None:
                type_name = "Spark Pool" if "synapse://spark_pool/" in object_id else "Integration Runtime"
        out.append(
            {
                "name": redact(ref.target_name),
                "kind": ref.kind.value,
                "type": type_name,
                "objectId": object_id,
                "location": redact(ref.location),
                "fabricTarget": reference_target(ref.kind.value, type_name),
            }
        )
    return out


def _size_label(record: UnifiedDiscoveryRecord) -> str:
    """A short size for the table: columns, activities or cells. Empty when unknown."""
    content = record.content
    if content is None:
        return ""
    kind = record.artifact.value
    if kind == "dedicated_sql_table" and getattr(content, "columns", None) is not None:
        return f"{len(content.columns)} columns"
    if kind == "pipeline" and hasattr(content, "activity_count"):
        return f"{content.activity_count} activities"
    if kind == "notebook" and getattr(content, "cells", None) is not None:
        return f"{len(content.cells)} cells"
    return ""


def _checklist(
    status: str,
    dependencies: List[dict],
    referenced_by: Iterable[dict],
    constructs: Iterable[str],
) -> List[dict]:
    """What discovery established, and what it noticed will need attention.

    Facts only: each entry is something found in the object itself or in how
    other discovered objects reference it. It is not a recommendation.
    """
    n = len(dependencies)
    out = [
        {"label": "Definition discovered", "state": "ok" if status in (STATUS_DISCOVERED, STATUS_WARNING) else "warn"},
        {
            "label": (f"{n} dependency discovered" if n == 1 else f"{n} dependencies discovered") if n else "No dependencies discovered",
            "state": "ok",
        },
    ]
    if any(d.get("type") == "Linked Service" or d.get("kind") == "secret" for d in dependencies):
        out.append({"label": "Connection requires recreation", "state": "warn"})
    if any(r.get("type") == "Trigger" for r in referenced_by):
        out.append({"label": "Trigger requires recreation", "state": "warn"})
    if constructs:
        out.append({"label": "Synapse-specific code needs review", "state": "warn"})
    return out


def list_item(
    record: UnifiedDiscoveryRecord, workspace: Optional[str], known: Mapping[str, str]
) -> dict:
    """The row shape: enough for the table, and no more."""
    source_type = _record_source_type(record)
    category = type_and_category(record.artifact.value)[1]
    item = {
        "id": record.identity.scoped_id,
        "name": record.identity.qualified_name,
        "type": source_type,
        "category": category,
        "workspace": record.identity.workspace or workspace or "",
        "status": _status(record),
        "dependencyCount": len(_dependencies(record, known)),
        "sources": [s.value for s in record.sources],
        "schema": record.identity.schema or "",
        "size": _size_label(record),
        "component": COMPONENT_BY_TYPE.get(source_type, "Other"),
    }
    item.update(_mapping_fields(record_mapping(record)))
    return item


def _content_dict(record: UnifiedDiscoveryRecord) -> Optional[dict]:
    content = record.content
    if content is None:
        return None
    to_dict = getattr(content, "to_dict", None)
    return sanitize(to_dict()) if callable(to_dict) else None


def _activities(record: UnifiedDiscoveryRecord) -> List[dict]:
    """A pipeline's activities, each with its own preliminary Fabric mapping."""
    content = record.content
    if record.artifact.value != "pipeline" or content is None:
        return []
    out = []
    for activity in list(getattr(content, "all_activities", ()))[:500]:
        mapped = map_activity(activity.type)
        movement = activity.data_movement
        out.append(
            sanitize(
                {
                    "name": activity.name,
                    "type": activity.type,
                    "parent": activity.parent,
                    "source": (movement.source_type or movement.source_store_type) if movement else None,
                    "sink": (movement.sink_type or movement.sink_store_type) if movement else None,
                    "dependsOn": [d.activity for d in activity.depends_on] if activity.depends_on else [],
                    "references": sorted({r.target_name for r in activity.references}),
                    "expressionCount": len(activity.expressions),
                    **mapped.to_dict(),
                }
            )
        )
    return out


def detail(
    record: UnifiedDiscoveryRecord,
    workspace: Optional[str],
    known: Mapping[str, str],
    referenced_by: Iterable[dict] = (),
) -> dict:
    """Everything the detail drawer shows, for one object."""
    mapped = record_mapping(record)
    item = list_item(record, workspace, known)
    definition = record.definition
    constructs = _synapse_specific(record)
    item.update(mapped.to_dict())
    item.update(
        {
            "overview": sanitize(
                {
                    "artifact": record.artifact.value,
                    "qualifiedName": record.identity.qualified_name,
                    "logicalId": record.logical_id,
                    "database": record.identity.database,
                    "schema": record.identity.schema,
                    "sources": [s.value for s in record.sources],
                    "definitionStatus": definition.status.value if definition else None,
                    "contentType": definition.content_type if definition else None,
                    "extractor": definition.extractor if definition else None,
                    "published": record.runtime.published if record.runtime else None,
                    "drift": record.runtime.drift.value if record.runtime else None,
                    "synapseSpecificConstructs": list(constructs) or None,
                    "sourceKeys": [k.to_dict() for k in record.identity.source_keys],
                    "provenance": record.provenance_by_facet(),
                }
            ),
            "configuration": _content_dict(record),
            "dependencies": _dependencies(record, known),
            "referencedBy": list(referenced_by),
            "actions": _checklist(item["status"], _dependencies(record, known), list(referenced_by), constructs),
            "activities": _activities(record),
            "issues": sanitize(
                [
                    {
                        "code": i.code.value,
                        "message": i.message,
                        "source": i.source.value if i.source else None,
                        "facet": i.facet.value if i.facet else None,
                    }
                    for i in record.all_issues
                ]
            ),
            "rawMetadata": sanitize(record.to_dict()),
        }
    )
    return item


# --- extras ------------------------------------------------------------------------


@dataclass(frozen=True)
class Extra:
    """An inventory entry that is not a ``UnifiedDiscoveryRecord``."""

    id: str
    name: str
    source_type: str
    metadata: Mapping[str, Any]
    #: Each: {name, kind, type, targetId?, computeName?}. Joined at read time.
    dependencies: Tuple[Mapping[str, Any], ...] = ()
    referenced_by: Tuple[Mapping[str, Any], ...] = ()
    status: str = STATUS_DISCOVERED
    raw: Optional[Mapping[str, Any]] = None

    @property
    def category(self) -> str:
        return TYPE_CATEGORY.get(self.source_type, "Other")


def _pick(source: Optional[Mapping[str, Any]], keys: Iterable[str]) -> Dict[str, Any]:
    source = source or {}
    return {k: source[k] for k in keys if source.get(k) not in (None, "", [], {})}


def _extra_dependencies(extra: Extra, known: Mapping[str, str]) -> List[dict]:
    out = []
    for dep in extra.dependencies:
        object_id = None
        if dep.get("targetId"):
            object_id = known.get(dep["targetId"])
        elif dep.get("computeName"):
            object_id = known.get(f"compute:{dep['computeName']}")
        out.append(
            {
                "name": redact(str(dep["name"])),
                "kind": dep.get("kind", "artifact"),
                "type": dep.get("type"),
                "objectId": object_id,
                "location": dep.get("location", ""),
                "fabricTarget": reference_target(dep.get("kind", "artifact"), dep.get("type")),
            }
        )
    return out


def _extra_size(extra: Extra) -> str:
    m = extra.metadata
    if extra.source_type == "Spark Pool" and m.get("nodeCount"):
        return f"{m['nodeCount']} nodes"
    if extra.source_type == "Schema":
        return f"{sum((m.get('objectCounts') or {}).values())} objects"
    if extra.source_type == "Storage Reference" and m.get("pathCount"):
        return f"{m['pathCount']} paths"
    return ""


def extra_item(extra: Extra, workspace: Optional[str], known: Mapping[str, str]) -> dict:
    item = {
        "id": extra.id,
        "name": extra.name,
        "type": extra.source_type,
        "category": extra.category,
        "workspace": workspace or "",
        "status": extra.status,
        "dependencyCount": len(extra.dependencies),
        "sources": [],
        "schema": str(extra.metadata.get("schemaName") or ""),
        "size": _extra_size(extra),
        "component": COMPONENT_BY_TYPE.get(extra.source_type, "Other"),
    }
    item.update(_mapping_fields(map_synapse_object(extra.source_type)))
    return item


def extra_detail(
    extra: Extra,
    workspace: Optional[str],
    known: Mapping[str, str],
    referenced_by: Iterable[dict] = (),
) -> dict:
    mapped = map_synapse_object(extra.source_type)
    item = extra_item(extra, workspace, known)
    item.update(mapped.to_dict())
    refs = list(extra.referenced_by) + list(referenced_by)
    item.update(
        {
            "overview": sanitize({"logicalId": extra.id, "sources": [], **{"discoveredFrom": extra.metadata.get("_from")}}),
            "configuration": sanitize({k: v for k, v in extra.metadata.items() if not k.startswith("_")}),
            "dependencies": _extra_dependencies(extra, known),
            "referencedBy": sanitize(refs),
            "actions": _checklist(extra.status, _extra_dependencies(extra, known), refs, ()),
            "activities": [],
            "issues": [],
            "rawMetadata": sanitize(extra.raw if extra.raw is not None else dict(extra.metadata)),
        }
    )
    return item


# -- builders: each turns facts the connection layer returned into Extras --------------


def sql_pool_extras(items: Iterable[Mapping[str, Any]]) -> List[Extra]:
    out = []
    for i in items:
        name = str(i.get("name") or "")
        if not name:
            continue
        props = i.get("properties") or {}
        sku = _pick(i.get("sku"), ("name", "tier", "capacity"))
        meta = {
            **_pick(props, ("status", "collation", "maxSizeBytes", "creationDate", "provisioningState")),
            **({"sku": sku} if sku else {}),
            "location": i.get("location"),
            "_from": "Azure Resource Manager",
        }
        out.append(Extra(f"synapse://dedicated_sql_pool/{name}", name, "Dedicated SQL Pool", meta, raw=i))
    return out


def spark_pool_extras(items: Iterable[Mapping[str, Any]]) -> List[Extra]:
    out = []
    for i in items:
        name = str(i.get("name") or "")
        if not name:
            continue
        p = i.get("properties") or {}
        meta = {
            **_pick(p, ("sparkVersion", "nodeSize", "nodeSizeFamily", "nodeCount", "autoScale", "autoPause",
                        "dynamicExecutorAllocation", "sessionLevelPackagesEnabled", "provisioningState")),
            "customLibraries": [
                _pick(c, ("name", "type")) for c in (p.get("customLibraries") or [])
            ] or None,
            "libraryRequirementsFile": (p.get("libraryRequirements") or {}).get("filename"),
            "location": i.get("location"),
            "_from": "Azure Resource Manager",
        }
        meta = {k: v for k, v in meta.items() if v not in (None, [], {})}
        out.append(Extra(f"synapse://spark_pool/{name}", name, "Spark Pool", meta, raw=i))
    return out


def library_extras(items: Iterable[Mapping[str, Any]]) -> List[Extra]:
    out = []
    for i in items:
        name = str(i.get("name") or "")
        if not name:
            continue
        p = i.get("properties") or {}
        meta = {
            "scope": "Workspace",
            **_pick(p, ("type", "path", "uploadedTimestamp", "provisioningStatus")),
            "_from": "Azure Resource Manager",
        }
        out.append(Extra(f"synapse://spark_library/{name}", name, "Spark Library", meta, raw=i))
    return out


def runtime_extras(items: Iterable[Mapping[str, Any]]) -> List[Extra]:
    out = []
    for i in items:
        name = str(i.get("name") or "")
        if not name:
            continue
        p = i.get("properties") or {}
        meta = {
            **_pick(p, ("type", "description", "state")),
            **_pick(p.get("typeProperties"), ("computeProperties", "managedVirtualNetwork")),
            "_from": "Azure Resource Manager",
        }
        out.append(Extra(f"synapse://integration_runtime/{name}", name, "Integration Runtime", meta, raw=i))
    return out


def trigger_extras(items: Iterable[Mapping[str, Any]]) -> List[Extra]:
    out = []
    for i in items:
        name = str(i.get("name") or "")
        if not name:
            continue
        p = i.get("properties") or {}
        tp = p.get("typeProperties") or {}
        pipelines = []
        deps = []
        for ref in p.get("pipelines") or []:
            target = (ref.get("pipelineReference") or {}).get("referenceName")
            if target:
                # Parameter *names* only: values may be expressions or literals.
                pipelines.append({"pipeline": target, "parameters": sorted((ref.get("parameters") or {}).keys())})
                deps.append({"name": target, "kind": "artifact", "type": "Pipeline",
                             "targetId": f"synapse://pipeline/{target}", "location": "properties.pipelines"})
        # An older single-pipeline shape.
        legacy = (p.get("pipeline") or {}).get("pipelineReference", {}).get("referenceName")
        if legacy and not deps:
            deps.append({"name": legacy, "kind": "artifact", "type": "Pipeline",
                         "targetId": f"synapse://pipeline/{legacy}", "location": "properties.pipeline"})
        meta = {
            "triggerType": p.get("type"),
            "runtimeState": p.get("runtimeState"),
            "description": p.get("description"),
            **_pick(tp.get("recurrence") or tp, ("frequency", "interval", "startTime", "endTime", "timeZone")),
            **_pick(tp, ("events", "blobPathBeginsWith", "blobPathEndsWith", "scope", "maxConcurrency")),
            "pipelines": pipelines or None,
            "_from": "Synapse Artifacts API",
        }
        meta = {k: v for k, v in meta.items() if v not in (None, [], {})}
        out.append(Extra(f"synapse://trigger/{name}", name, "Trigger", meta, tuple(deps), raw=i))
    return out


def schema_extras(records: Iterable[UnifiedDiscoveryRecord]) -> List[Extra]:
    """Schemas, derived from the SQL objects discovered. Factual, not queried."""
    groups: Dict[Tuple[str, str], List[UnifiedDiscoveryRecord]] = {}
    for r in records:
        if r.identity.database and r.artifact.value in ("dedicated_sql_table", "sql_view", "stored_procedure"):
            groups.setdefault((r.identity.database, r.identity.schema or "dbo"), []).append(r)
    out = []
    for (db, schema), members in sorted(groups.items()):
        counts: Dict[str, int] = {}
        for m in members:
            t = _record_source_type(m)
            counts[t] = counts.get(t, 0) + 1
        deps = [
            {"name": m.identity.qualified_name, "kind": "sql_object", "type": _record_source_type(m),
             "targetId": m.identity.scoped_id, "location": "contains"}
            for m in members[:500]
        ]
        meta = {"database": db, "schemaName": schema, "objectCounts": counts,
                "_from": "derived from the discovered SQL objects"}
        out.append(Extra(f"sql://{db}/{schema}", f"{db}.{schema}", "Schema", meta, tuple(deps)))
    return out


_ABFS = re.compile(r"^(?:abfss?|wasbs?)://(?P<container>[^@/]+)@(?P<account>[^./]+)\.", re.IGNORECASE)
_HTTPS_STORE = re.compile(r"^https://(?P<account>[^./]+)\.(?:dfs|blob)\.core\.windows\.net/(?P<container>[^/]+)", re.IGNORECASE)


def _storage_key(target: str) -> Tuple[str, str]:
    m = _ABFS.match(target) or _HTTPS_STORE.match(target)
    if m:
        return m.group("account").lower(), m.group("container")
    scheme, _, rest = target.partition("://")
    return (rest.split("/", 1)[0] or scheme or "unknown"), ""


def storage_extras(records: Iterable[UnifiedDiscoveryRecord]) -> List[Extra]:
    """Storage locations the discovered objects point at, grouped by account/container."""
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in records:
        for ref in r.references:
            if ref.kind.value != "storage_path":
                continue
            g = groups.setdefault(_storage_key(ref.target_name), {"paths": set(), "by": {}})
            g["paths"].add(ref.target_name)
            g["by"][r.identity.scoped_id] = {
                "name": r.identity.qualified_name, "type": _record_source_type(r), "objectId": r.identity.scoped_id,
            }
    out = []
    for (account, container), g in sorted(groups.items()):
        name = f"{account}/{container}" if container else account
        meta = {
            "account": account,
            "container": container or None,
            "paths": sorted(g["paths"])[:25],
            "pathCount": len(g["paths"]),
            "referencedByCount": len(g["by"]),
            "_from": "references found in discovered objects",
        }
        meta = {k: v for k, v in meta.items() if v is not None}
        out.append(Extra(f"storage://{name}", name, "Storage Reference", meta,
                         referenced_by=tuple(g["by"].values())))
    return out


def to_json(payload: Any) -> bytes:
    return json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
