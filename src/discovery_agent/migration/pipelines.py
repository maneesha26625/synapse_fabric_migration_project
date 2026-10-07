"""Synapse pipeline -> Fabric data pipeline.

Fabric pipelines use the same JSON family as Synapse/ADF, with three real
differences this module handles:

* **Datasets are inline.** A Copy/Lookup/GetMetadata/Delete activity carries its
  dataset's settings itself (``datasetSettings``) and points at a Fabric
  *connection* by id, not at a dataset and linked service by name.
* **Fabric items are referenced by id.** A notebook, Spark job or pipeline an
  activity runs is named by its Fabric id, so those must exist first.
* **The SQL pool is gone.** Anything that read or wrote the Synapse pool is
  pointed at the migrated Warehouse instead: datasets, Script and Stored
  procedure activities, including those that reach the pool through the
  workspace's default SQL linked service with the database as a parameter.

An activity this module does not know is never guessed at: the pipeline is
reported as needing a rewrite, with the activity names, and not created.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: Control-flow and pass-through activities that need no conversion beyond their children.
_PASS_THROUGH = {"Wait", "SetVariable", "AppendVariable", "Fail", "WebActivity", "WebHook", "Filter", "Validation", "Script", "Delete", "GetMetadata", "Lookup"}
#: Activities that call another service through one linked service. Fabric has the same activities;
#: each points at a Fabric connection, found by the linked service's name.
_EXTERNAL_SERVICE = {"AzureFunctionActivity", "DatabricksNotebook", "DatabricksSparkJar", "DatabricksSparkPython", "Custom",
                     "HDInsightHive", "HDInsightPig", "HDInsightMapReduce", "HDInsightSpark", "HDInsightStreaming",
                     "AzureMLExecutePipeline"}
_CONTAINERS = {"ForEach": ("activities",), "Until": ("activities",), "IfCondition": ("ifTrueActivities", "ifFalseActivities")}
_DATASET_HOLDERS = {"Lookup", "GetMetadata", "Delete", "Validation"}
_SQL_LS = {"AzureSqlDW", "AzureSynapseAnalytics"}
_SOURCE_SINK_MAP = {"SqlDWSource": "DataWarehouseSource", "SqlDWSink": "DataWarehouseSink"}
_DATASET_REF = re.compile(r"dataset\(\)\.([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class Warehouse:
    artifact_id: str
    endpoint: str
    name: str


@dataclass
class Context:
    """Everything a pipeline refers to, resolved to Fabric where it exists."""

    datasets: Mapping[str, Mapping[str, Any]]
    linked_services: Mapping[str, Mapping[str, Any]]
    connections: Mapping[str, str]       # linked service name -> Fabric connection id
    notebooks: Mapping[str, str]         # Synapse notebook name -> Fabric notebook id
    pipelines: Mapping[str, str]         # pipeline name -> Fabric pipeline id
    spark_jobs: Mapping[str, str]        # Spark job definition name -> Fabric id
    workspace_id: str
    warehouse: Optional[Warehouse]
    pool_name: str = ""
    #: The pipeline's own parameters and their default values, filled in by ``convert``.
    pipeline_parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Converted:
    definition: Dict[str, Any]
    notes: List[str] = field(default_factory=list)
    #: Activities with no faithful Fabric form: the pipeline cannot be created.
    unsupported: List[str] = field(default_factory=list)
    #: Things that must exist in Fabric first (a connection, a notebook).
    missing: List[str] = field(default_factory=list)


# -- parameters --------------------------------------------------------------------


def _expr(value: Any) -> str:
    """The expression text of a Synapse value (an Expression object or a literal)."""
    if isinstance(value, Mapping) and "value" in value:
        value = value["value"]
    return value if isinstance(value, str) else json.dumps(value)


def _substitute(node: Any, params: Mapping[str, Any]) -> Any:
    """Replace ``@dataset().x`` with the value the activity passed for ``x``."""
    if isinstance(node, str):
        whole = re.fullmatch(r"@dataset\(\)\.([A-Za-z_][A-Za-z0-9_]*)", node.strip())
        if whole and whole.group(1) in params:
            return params[whole.group(1)]
        if node.startswith("@") and "dataset()" in node:
            def repl(m: "re.Match[str]") -> str:
                if m.group(1) not in params:
                    return m.group(0)
                v = _expr(params[m.group(1)])
                return "(" + (v[1:] if v.startswith("@") else json.dumps(v)) + ")"
            return _DATASET_REF.sub(repl, node)
        return node
    if isinstance(node, list):
        return [_substitute(n, params) for n in node]
    if isinstance(node, dict):
        return {k: _substitute(v, params) for k, v in node.items()}
    return node


# -- the Synapse pool, wherever it is reached from ----------------------------------------

_LS_PARAMETER = re.compile(r"@\{\s*linkedService\(\)\.([A-Za-z_]\w*)\s*\}|@linkedService\(\)\.([A-Za-z_]\w*)")
_PIPELINE_PARAMETER = re.compile(r"@\{?\s*pipeline\(\)\.parameters\.([A-Za-z_]\w*)\s*\}?")


def _literal(value: Any, pipeline_parameters: Mapping[str, Any]) -> Optional[str]:
    """The text a value stands for, or None when only the running pipeline knows it.
    ``@pipeline().parameters.x`` stands for that parameter's default value."""
    if isinstance(value, Mapping):
        value = value.get("value")
    if not isinstance(value, str):
        return None
    m = _PIPELINE_PARAMETER.fullmatch(value.strip())
    if m:
        default = pipeline_parameters.get(m.group(1))
        return default if isinstance(default, str) else None
    return None if value.startswith("@") else value


def _host(server: str) -> str:
    """``tcp:name.sql.azuresynapse.net,1433`` -> ``name.sql.azuresynapse.net``."""
    return re.sub(r"^tcp:", "", server.strip(), flags=re.I).split(",")[0].strip().lower()


def sql_target(ls: Mapping[str, Any], values: Optional[Mapping[str, Any]] = None,
               pipeline_parameters: Optional[Mapping[str, Any]] = None) -> Tuple[str, str]:
    """(host, database) a SQL linked service reaches. Its ``linkedService()`` parameters are
    filled in from ``values`` (what an activity or a dataset passes to it) or their defaults;
    one that stays unknown keeps its ``@{linkedService().x}`` text."""
    props = ls.get("properties") or {}
    tp = props.get("typeProperties") or {}
    cs = tp.get("connectionString")
    if isinstance(cs, Mapping):
        cs = cs.get("value")
    pairs = {k.strip().lower(): v.strip() for k, _, v in (part.partition("=") for part in (cs if isinstance(cs, str) else "").split(";")) if k.strip()}
    server = tp.get("server") if isinstance(tp.get("server"), str) else ""
    database = tp.get("database") if isinstance(tp.get("database"), str) else ""
    server = server or pairs.get("server") or pairs.get("data source") or pairs.get("address") or ""
    database = database or pairs.get("database") or pairs.get("initial catalog") or ""
    defaults = {k: v.get("defaultValue") for k, v in (props.get("parameters") or {}).items() if isinstance(v, Mapping)}
    given, known = values or {}, pipeline_parameters or {}

    def fill(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(2)
        value = _literal(given[name], known) if name in given else None
        if value is None and isinstance(defaults.get(name), str):
            value = defaults[name]
        return value if value is not None else m.group(0)

    return _host(_LS_PARAMETER.sub(fill, server)), _LS_PARAMETER.sub(fill, database).strip()


def is_pool_service(ls: Mapping[str, Any], pool_name: str, values: Optional[Mapping[str, Any]] = None, pool_server: str = "",
                    pipeline_parameters: Optional[Mapping[str, Any]] = None, any_database: bool = False) -> bool:
    """Whether a SQL linked service reaches the dedicated pool being migrated, whose part the
    migrated Warehouse now plays. With ``any_database`` a database left to a parameter on the
    pool's own server counts too: the workspace's default SQL linked service, whose only use in
    a migrated pipeline is reaching this pool."""
    props = ls.get("properties") or {}
    if props.get("type") not in _SQL_LS | {"AzureSqlDatabase"} or not pool_name:
        return False
    host, database = sql_target(ls, values, pipeline_parameters)
    if "-ondemand." in host:
        return False  # the serverless endpoint holds other databases, never the dedicated pool
    if pool_server and host and host != _host(pool_server):
        return False
    if database.lower() == pool_name.lower():
        return True
    if not any_database or not _LS_PARAMETER.fullmatch(database):
        return False
    return bool(pool_server) or host.endswith(".sql.azuresynapse.net")


def _is_pool_service(ls: Mapping[str, Any], ctx: Context, values: Optional[Mapping[str, Any]] = None) -> bool:
    """A linked service that, with the values this use passes it, points at the pool being migrated."""
    return is_pool_service(ls, ctx.pool_name, values, "", ctx.pipeline_parameters)


# -- datasets ------------------------------------------------------------------------


def _warehouse_ref(ctx: Context) -> Dict[str, Any]:
    w = ctx.warehouse
    assert w is not None
    return {"name": w.name, "properties": {"type": "DataWarehouse", "annotations": [],
            "typeProperties": {"endpoint": w.endpoint, "artifactId": w.artifact_id, "workspaceId": ctx.workspace_id}}}


def dataset_settings(ref: Mapping[str, Any], ctx: Context, out: Converted) -> Tuple[Optional[Dict[str, Any]], bool]:
    """(Fabric datasetSettings, retargeted-to-warehouse) for a dataset reference, or (None, False) if unresolved."""
    name = str(ref.get("referenceName") or "")
    ds = ctx.datasets.get(name)
    if ds is None:
        out.missing.append(f"dataset {name}")
        return None, False
    props = ds.get("properties") or {}
    params = {k: v for k, v in (ref.get("parameters") or {}).items()}
    ls_name = str((props.get("linkedServiceName") or {}).get("referenceName") or "")
    ls = ctx.linked_services.get(ls_name)
    # What the dataset passes its linked service (say, DBName), with the activity's dataset parameters filled in.
    ls_values = _substitute(copy.deepcopy((props.get("linkedServiceName") or {}).get("parameters") or {}), params)
    typeprops = _substitute(copy.deepcopy(props.get("typeProperties") or {}), params)
    schema = props.get("schema") or []
    if ls is not None and ctx.warehouse is not None and _is_pool_service(ls, ctx, ls_values):
        table = typeprops.get("table") or typeprops.get("tableName")
        settings = {"annotations": [], "type": "DataWarehouseTable", "schema": schema,
                    "typeProperties": {k: v for k, v in {"schema": typeprops.get("schema"), "table": table}.items() if v},
                    "linkedService": _warehouse_ref(ctx)}
        out.notes.append(f"Dataset {name} now reads and writes the migrated Warehouse '{ctx.warehouse.name}', not the Synapse pool.")
        return settings, True
    if ls_values:
        out.notes.append(f"Dataset {name} passes {', '.join(sorted(ls_values))} to linked service {ls_name}; Fabric connections take no "
                         f"parameters, so it uses the single connection '{ls_name}'. Check it points at the right place.")
    connection = ctx.connections.get(ls_name)
    if not connection:
        out.missing.append(f"connection {ls_name or '(none)'}")
        return None, False
    return {"annotations": [], "type": props.get("type"), "schema": schema, "typeProperties": typeprops,
            "externalReferences": {"connection": connection}}, False


# -- activities --------------------------------------------------------------------------


def _base(act: Mapping[str, Any]) -> Dict[str, Any]:
    keep = ("name", "description", "state", "onInactiveMarkAs", "dependsOn", "policy", "userProperties")
    return {k: copy.deepcopy(act[k]) for k in keep if k in act}


def _copy(act: Mapping[str, Any], ctx: Context, out: Converted) -> Dict[str, Any]:
    result = _base(act)
    result["type"] = "Copy"
    tp = copy.deepcopy(act.get("typeProperties") or {})
    inputs, outputs = act.get("inputs") or [], act.get("outputs") or []
    for side, refs in (("source", inputs), ("sink", outputs)):
        node = tp.get(side) or {}
        if refs:
            settings, retargeted = dataset_settings(refs[0], ctx, out)
            if settings is not None:
                node["datasetSettings"] = settings
                if retargeted and node.get("type") in _SOURCE_SINK_MAP:
                    node["type"] = _SOURCE_SINK_MAP[node["type"]]
        tp[side] = node
    if tp.pop("enableStaging", False):
        out.notes.append(f"Copy '{act.get('name')}': staging was switched off; Fabric Copy stages through the workspace itself.")
    tp.pop("stagingSettings", None)
    result["typeProperties"] = tp
    return result


def _holder(act: Mapping[str, Any], ctx: Context, out: Converted) -> Dict[str, Any]:
    """Lookup / GetMetadata / Delete / Validation: ``dataset`` becomes ``datasetSettings``."""
    result = _base(act)
    result["type"] = act["type"]
    tp = copy.deepcopy(act.get("typeProperties") or {})
    ref = tp.pop("dataset", None)
    if ref:
        settings, _ = dataset_settings(ref, ctx, out)
        if settings is not None:
            tp["datasetSettings"] = settings
    for key in ("source",):
        node = tp.get(key)
        if isinstance(node, dict) and node.get("type") in _SOURCE_SINK_MAP and isinstance(tp.get("datasetSettings"), dict) \
                and tp["datasetSettings"].get("type") == "DataWarehouseTable":
            node["type"] = _SOURCE_SINK_MAP[node["type"]]
    result["typeProperties"] = tp
    return result


def _notebook(act: Mapping[str, Any], ctx: Context, out: Converted) -> Dict[str, Any]:
    result = _base(act)
    result["type"] = "TridentNotebook"
    tp = act.get("typeProperties") or {}
    name = str((tp.get("notebook") or {}).get("referenceName") or "")
    notebook_id = ctx.notebooks.get(name)
    if not notebook_id:
        out.missing.append(f"notebook {name}")
    result["typeProperties"] = {"notebookId": notebook_id or "", "workspaceId": ctx.workspace_id,
                                "parameters": copy.deepcopy(tp.get("parameters") or {})}
    if tp.get("sparkPool") or tp.get("executorSize") or tp.get("conf"):
        out.notes.append(f"Notebook '{act.get('name')}': Spark pool and size settings were dropped; the notebook uses its Fabric Environment.")
    return result


def _invoke(act: Mapping[str, Any], ctx: Context, out: Converted) -> Dict[str, Any]:
    result = _base(act)
    result["type"] = "InvokePipeline"
    tp = act.get("typeProperties") or {}
    name = str((tp.get("pipeline") or {}).get("referenceName") or "")
    pipeline_id = ctx.pipelines.get(name)
    if not pipeline_id:
        out.missing.append(f"pipeline {name}")
    result["typeProperties"] = {"pipelineId": pipeline_id or "", "workspaceId": ctx.workspace_id, "operationType": "InvokeFabricPipeline",
                                "waitOnCompletion": tp.get("waitOnCompletion", True), "parameters": copy.deepcopy(tp.get("parameters") or {})}
    return result


def _sparkjob(act: Mapping[str, Any], ctx: Context, out: Converted) -> Dict[str, Any]:
    result = _base(act)
    result["type"] = "SparkJobDefinition"
    tp = act.get("typeProperties") or {}
    name = str((tp.get("sparkJob") or {}).get("referenceName") or "")
    job_id = ctx.spark_jobs.get(name)
    if not job_id:
        out.missing.append(f"Spark job definition {name}")
    result["typeProperties"] = {"sparkJobDefinitionId": job_id or "", "workspaceId": ctx.workspace_id}
    return result


def _stored_procedure(act: Mapping[str, Any], ctx: Context, out: Converted) -> Dict[str, Any]:
    result = _base(act)
    result["type"] = "SqlServerStoredProcedure"
    tp = copy.deepcopy(act.get("typeProperties") or {})
    if ctx.warehouse is None:
        out.missing.append("the migrated Warehouse")
    else:
        result["linkedService"] = _warehouse_ref(ctx)
        out.notes.append(f"Stored procedure '{act.get('name')}' runs in the migrated Warehouse '{ctx.warehouse.name}'.")
    result["typeProperties"] = tp
    return result


def _sql_stored_procedure(act: Mapping[str, Any], ctx: Context, out: Converted) -> Dict[str, Any]:
    """A stored procedure called through a SQL linked service. When that service reached the
    pool being migrated (often the workspace's default one, with the pool's name as DBName),
    the procedure runs in the migrated Warehouse, where the procedure itself was migrated;
    otherwise it runs through the Fabric connection of the linked service's name."""
    result = _base(act)
    result["type"] = "SqlServerStoredProcedure"
    result["typeProperties"] = copy.deepcopy(act.get("typeProperties") or {})
    ref = act.get("linkedServiceName") or {}
    ls_name = str(ref.get("referenceName") or "")
    ls = ctx.linked_services.get(ls_name)
    if ls is not None and ctx.warehouse is not None and _is_pool_service(ls, ctx, ref.get("parameters") or {}):
        result["linkedService"] = _warehouse_ref(ctx)
        out.notes.append(f"Stored procedure '{act.get('name')}' runs in the migrated Warehouse '{ctx.warehouse.name}'.")
    elif ctx.connections.get(ls_name):
        result["externalReferences"] = {"connection": ctx.connections[ls_name]}
    else:
        out.missing.append(f"connection {ls_name or '(none)'}")
    return result


def _script(act: Mapping[str, Any], ctx: Context, out: Converted) -> Dict[str, Any]:
    result = _base(act)
    result["type"] = "Script"
    result["typeProperties"] = copy.deepcopy(act.get("typeProperties") or {})
    ref = act.get("linkedServiceName") or {}
    ls_name = str(ref.get("referenceName") or "")
    ls = ctx.linked_services.get(ls_name)
    if ls is not None and ctx.warehouse is not None and _is_pool_service(ls, ctx, ref.get("parameters") or {}):
        result["linkedService"] = _warehouse_ref(ctx)
    elif ctx.connections.get(ls_name):
        result["externalReferences"] = {"connection": ctx.connections[ls_name]}
    else:
        out.missing.append(f"connection {ls_name or '(none)'}")
    return result


def _external(act: Mapping[str, Any], ctx: Context, out: Converted) -> Optional[Dict[str, Any]]:
    """Azure Function, Databricks, Batch, HDInsight and Machine Learning activities: same type, connection by name."""
    kind, name = str(act.get("type")), str(act.get("name") or act.get("type"))
    tp = act.get("typeProperties") or {}
    extra = sorted(k for k, v in tp.items() if isinstance(v, Mapping) and v.get("type") == "LinkedServiceReference")
    if extra:
        out.unsupported.append(f"{name} ({kind}, which also uses {', '.join(extra)})")
        return None
    result = copy.deepcopy(dict(act))
    ls_name = str((result.pop("linkedServiceName", None) or {}).get("referenceName") or "")
    connection = ctx.connections.get(ls_name)
    if not connection:
        out.missing.append(f"connection {ls_name or '(none)'}")
    else:
        result["externalReferences"] = {"connection": connection}
    out.notes.append(f"{kind} '{name}' runs through the Fabric connection '{ls_name}'; check its settings in Fabric before the first run.")
    return result


def convert_activity(act: Mapping[str, Any], ctx: Context, out: Converted) -> Optional[Dict[str, Any]]:
    kind = str(act.get("type") or "")
    name = str(act.get("name") or kind)
    if kind == "Copy":
        return _copy(act, ctx, out)
    if kind in _DATASET_HOLDERS:
        return _holder(act, ctx, out)
    if kind == "SynapseNotebook":
        return _notebook(act, ctx, out)
    if kind == "ExecutePipeline":
        return _invoke(act, ctx, out)
    if kind == "SparkJob":
        return _sparkjob(act, ctx, out)
    if kind == "SqlPoolStoredProcedure":
        return _stored_procedure(act, ctx, out)
    if kind == "SqlServerStoredProcedure":
        return _sql_stored_procedure(act, ctx, out)
    if kind == "Script":
        return _script(act, ctx, out)
    if kind == "Switch":
        result = _base(act)
        result["type"] = kind
        tp = copy.deepcopy(act.get("typeProperties") or {})
        tp["cases"] = [{**c, "activities": _convert_list(c.get("activities") or [], ctx, out)} for c in tp.get("cases") or []]
        tp["defaultActivities"] = _convert_list(tp.get("defaultActivities") or [], ctx, out)
        result["typeProperties"] = tp
        return result
    if kind in _CONTAINERS:
        result = _base(act)
        result["type"] = kind
        tp = copy.deepcopy(act.get("typeProperties") or {})
        for key in _CONTAINERS[kind]:
            tp[key] = _convert_list(tp.get(key) or [], ctx, out)
        result["typeProperties"] = tp
        return result
    if kind in _EXTERNAL_SERVICE:
        return _external(act, ctx, out)
    if kind in _PASS_THROUGH:
        result = copy.deepcopy(dict(act))
        for noisy in ("linkedServiceName",):
            result.pop(noisy, None)
        return result
    out.unsupported.append(f"{name} ({kind})")
    return None


def _convert_list(activities: List[Mapping[str, Any]], ctx: Context, out: Converted) -> List[Dict[str, Any]]:
    converted = (convert_activity(a, ctx, out) for a in activities)
    return [c for c in converted if c is not None]


def convert(payload: Mapping[str, Any], ctx: Context) -> Converted:
    """The Fabric pipeline definition for one Synapse pipeline resource."""
    props = payload.get("properties") or {}
    out = Converted(definition={})
    # A value passed as @pipeline().parameters.x is read as that parameter's default.
    ctx = replace(ctx, pipeline_parameters={k: v.get("defaultValue") for k, v in (props.get("parameters") or {}).items() if isinstance(v, Mapping)})
    activities = _convert_list(props.get("activities") or [], ctx, out)
    content: Dict[str, Any] = {"properties": {"activities": activities}}
    for key in ("parameters", "variables", "concurrency", "annotations"):
        if props.get(key):
            content["properties"][key] = copy.deepcopy(props[key])
    if (props.get("folder") or {}).get("name"):
        out.notes.append(f"Synapse folder '{props['folder']['name']}' not recreated: the pipeline is placed at the workspace root.")
    out.definition = content
    out.missing = sorted(set(out.missing))
    return out


def create_body(name: str, content: Mapping[str, Any], description: str = "") -> Dict[str, Any]:
    """The ``POST /workspaces/{id}/dataPipelines`` body."""
    payload = base64.b64encode(json.dumps(content).encode("utf-8")).decode("ascii")
    body: Dict[str, Any] = {"displayName": name, "definition": {"parts": [
        {"path": "pipeline-content.json", "payload": payload, "payloadType": "InlineBase64"}]}}
    if description:
        body["description"] = description[:256]
    return body


def referenced_names(payload: Mapping[str, Any]) -> Dict[str, List[str]]:
    """Names a pipeline depends on, by kind, from its raw JSON. Used for planning and ordering."""
    found: Dict[str, List[str]] = {"datasets": [], "notebooks": [], "pipelines": [], "sparkJobs": []}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref, kind = node.get("referenceName"), node.get("type")
            if isinstance(ref, str):
                bucket = {"DatasetReference": "datasets", "NotebookReference": "notebooks", "PipelineReference": "pipelines",
                          "SparkJobDefinitionReference": "sparkJobs"}.get(str(kind))
                if bucket:
                    found[bucket].append(ref)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return {k: sorted(set(v)) for k, v in found.items()}
