"""Table data -> Fabric data pipelines.

The migration moves structure (Warehouse, schemas, tables, views, procedures)
with T-SQL, and moves *data* with Fabric data pipelines, the tool Fabric itself
provides for bulk copy. One pipeline per wave, metadata-driven:

* a ``tables`` array parameter, one entry per table: its schema, its name, the
  SELECT that reads it from the Synapse pool, and an optional pre-copy script;
* a ForEach over that array (four tables at a time), whose Copy activity reads
  through a Fabric SQL connection to the pool and writes the Warehouse table
  with the COPY command.

The SELECT casts the column types a Copy activity cannot carry (geography,
geometry, xml); every other type is converted by the Copy activity into the
type the Warehouse column was created with. Because the table list is a
parameter, a re-run passes only the tables that still need data, and the same
pipeline serves every run.

A table with a date filter gets a SELECT with a WHERE clause instead
(``datafilter``). Tables kept in sync share one more pipeline per Warehouse, the
sync pipeline, which reads its table list from the control table in the
Warehouse and copies each table's new and changed rows (``sync_definition``).
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from discovery_agent.migration import datacopy, datafilter, jobs, warehouse_ddl
from discovery_agent.sql.models import SqlTable

#: Tables copied at the same time inside one pipeline run.
PARALLEL_COPIES = 4
#: How long one pipeline run may take before the stage stops waiting for it.
RUN_TIMEOUT_SECONDS = 6 * 60 * 60
#: Seconds between status checks while a pipeline runs.
POLL_SECONDS = 20
#: Job states Fabric reports when a run is over.
FINISHED = {"Completed", "Failed", "Cancelled", "Deduped"}


@dataclass(frozen=True)
class WarehouseTarget:
    artifact_id: str
    endpoint: str
    name: str
    workspace_id: str


def pipeline_name(warehouse: str, wave: int) -> str:
    """A stable, valid item name: the same wave always maps to the same pipeline."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", warehouse) or "warehouse"
    return f"load_{base}_wave_{wave}"[:120]


def entry(table: SqlTable, replace: bool, query: str = "") -> Dict[str, str]:
    """One element of the pipeline's ``tables`` parameter. ``query`` overrides the full-table SELECT (a date filter)."""
    schema, name = table.key.schema, table.key.name
    return {
        "schema": schema,
        "table": name,
        "query": query or datacopy.select_sql(table),
        "preCopyScript": f"TRUNCATE TABLE {warehouse_ddl.qualified(schema, name)}" if replace else "",
    }


def _expression(value: Any) -> Dict[str, Any]:
    return {"value": value, "type": "Expression"}


_POLICY = {"timeout": "0.12:00:00", "retry": 2, "retryIntervalInSeconds": 60, "secureOutput": False, "secureInput": False}


def _after(activity: str) -> List[Dict[str, Any]]:
    return [{"activity": activity, "dependencyConditions": ["Succeeded"]}]


def _warehouse_link(warehouse: WarehouseTarget) -> Dict[str, Any]:
    return {
        "name": warehouse.name,
        "properties": {
            "annotations": [],
            "type": "DataWarehouse",
            "typeProperties": {"endpoint": warehouse.endpoint, "artifactId": warehouse.artifact_id,
                               "workspaceId": warehouse.workspace_id},
        },
    }


def _pool_source(query: Any, connection_id: str) -> Dict[str, Any]:
    """A read of the Synapse pool through the Fabric connection, by a SELECT."""
    return {
        "type": "SqlDWSource",
        "sqlReaderQuery": query,
        "queryTimeout": "02:00:00",
        "partitionOption": "None",
        "datasetSettings": {
            "annotations": [],
            "type": "AzureSqlDWTable",
            "schema": [],
            "typeProperties": {},
            "externalReferences": {"connection": connection_id},
        },
    }


def _copy(name: str, query: Any, schema: Any, table: Any, pre_copy: Any, connection_id: str, warehouse: WarehouseTarget,
          depends_on: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """A Copy activity from the Synapse pool into a Warehouse table."""
    sink: Dict[str, Any] = {
        "type": "DataWarehouseSink",
        "allowCopyCommand": True,
        "tableOption": "None",
        "preCopyScript": pre_copy,
        "datasetSettings": {
            "annotations": [],
            "type": "DataWarehouseTable",
            "schema": [],
            "typeProperties": {"schema": schema, "table": table},
            "linkedService": _warehouse_link(warehouse),
        },
    }
    return {
        "name": name,
        "type": "Copy",
        "dependsOn": depends_on or [],
        "policy": dict(_POLICY),
        "typeProperties": {
            "source": _pool_source(query, connection_id),
            "sink": sink,
            # A Synapse source cannot be read by the Warehouse's COPY command directly,
            # so the Copy activity stages the rows in the workspace first.
            "enableStaging": True,
            "translator": {"type": "TabularTranslator", "typeConversion": True,
                           "typeConversionSettings": {"allowDataTruncation": True, "treatBooleanAsNumber": False}},
        },
    }


def definition(entries: List[Mapping[str, str]], connection_id: str, warehouse: WarehouseTarget) -> Dict[str, Any]:
    """The ``pipeline-content.json`` of one wave's data pipeline."""
    copy = _copy("Copy table", _expression("@item().query"), _expression("@item().schema"), _expression("@item().table"),
                 _expression("@item().preCopyScript"), connection_id, warehouse)
    return {"properties": {
        "parameters": {"tables": {"type": "array", "defaultValue": [dict(e) for e in entries]}},
        "activities": [{
            "name": "For each table",
            "type": "ForEach",
            "dependsOn": [],
            "typeProperties": {
                "items": _expression("@pipeline().parameters.tables"),
                "isSequential": False,
                "batchCount": PARALLEL_COPIES,
                "activities": [copy],
            },
        }],
        "annotations": [],
    }}


def sync_name(warehouse: str) -> str:
    """The one sync pipeline of a Warehouse: it serves every table registered for sync."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", warehouse) or "warehouse"
    return f"sync_{base}"[:120]


#: The sync pipeline's table list: every row of the control table, read when the pipeline starts.
SYNC_TABLES_SQL = (f"SELECT table_name, staging_table, watermark, watermark_query, copy_query, apply_script "
                   f"FROM {datafilter.CONTROL} ORDER BY table_name")
#: The newest change this run copies up to: Synapse's, or the last watermark when the scope is empty.
_NEWEST = ("if(empty(activity('Newest change').output.firstRow.wm), item().watermark, "
           "activity('Newest change').output.firstRow.wm)")


def sync_definition(connection_id: str, warehouse: WarehouseTarget) -> Dict[str, Any]:
    """The ``pipeline-content.json`` of the sync pipeline.

    It holds no table list of its own. It reads the control table, then for each synced table:
    looks up the newest change in Synapse, copies the rows changed since the table's watermark
    into its staging table, and runs the table's apply batch, which replaces matched rows, adds
    the rest and moves the watermark, in one Warehouse transaction. A step that fails leaves the
    watermark where it was, so the next run copies the same window again.
    """
    lookup_tables = {
        "name": "Tables to sync",
        "type": "Lookup",
        "dependsOn": [],
        "policy": dict(_POLICY),
        "typeProperties": {
            "source": {"type": "DataWarehouseSource", "sqlReaderQuery": SYNC_TABLES_SQL, "queryTimeout": "02:00:00",
                       "partitionOption": "None"},
            "firstRowOnly": False,
            "datasetSettings": {"annotations": [], "linkedService": _warehouse_link(warehouse), "type": "DataWarehouseTable",
                                "schema": [], "typeProperties": {}},
        },
    }
    newest = {
        "name": "Newest change",
        "type": "Lookup",
        "dependsOn": [],
        "policy": dict(_POLICY),
        "typeProperties": {"source": _pool_source(_expression("@item().watermark_query"), connection_id), "firstRowOnly": True},
    }
    query = (f"@replace(replace(item().copy_query, '{datafilter.FROM_MARK}', item().watermark), "
             f"'{datafilter.TO_MARK}', {_NEWEST})")
    truncate = (f"@concat('TRUNCATE TABLE {warehouse_ddl.quote(datafilter.SYNC_SCHEMA)}.[', "
                "replace(item().staging_table, ']', ']]'), ']')")
    copy = _copy("Copy changes", _expression(query), datafilter.SYNC_SCHEMA, _expression("@item().staging_table"),
                 _expression(truncate), connection_id, warehouse, _after("Newest change"))
    apply = {
        "name": "Apply changes",
        "type": "Script",
        "dependsOn": _after("Copy changes"),
        "policy": dict(_POLICY),
        "linkedService": _warehouse_link(warehouse),
        "typeProperties": {
            "scripts": [{"type": "NonQuery", "text": _expression(f"@replace(item().apply_script, '{datafilter.TO_MARK}', {_NEWEST})")}],
            "scriptBlockExecutionTimeout": "02:00:00",
        },
    }
    return {"properties": {
        "activities": [lookup_tables, {
            "name": "For each synced table",
            "type": "ForEach",
            "dependsOn": _after("Tables to sync"),
            "typeProperties": {
                "items": _expression("@activity('Tables to sync').output.value"),
                # One table at a time: every apply batch updates the control table, and the Warehouse
                # detects write conflicts per table, so two at once would fail one of them.
                "isSequential": True,
                "activities": [newest, copy, apply],
            },
        }],
        "annotations": [],
    }}


#: When the sync pipeline's schedule runs, once created (switched off) and until someone changes it.
SYNC_TIME = "02:00"


def sync_schedule_body(now: datetime) -> Dict[str, Any]:
    """A daily schedule for the sync pipeline, created switched off like every schedule this tool makes."""
    start = now.replace(tzinfo=None, microsecond=0)
    end = start + timedelta(days=365 * jobs.FAR_FUTURE_YEARS)
    return {"enabled": False, "configuration": {
        "type": "Daily", "times": [SYNC_TIME], "localTimeZoneId": "UTC",
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
    }}


def create_body(name: str, content: Mapping[str, Any], description: str) -> Dict[str, Any]:
    """The ``POST /workspaces/{id}/dataPipelines`` body."""
    payload = base64.b64encode(json.dumps(content).encode("utf-8")).decode("ascii")
    return {"displayName": name, "description": description[:256],
            "definition": {"parts": [{"path": "pipeline-content.json", "payload": payload, "payloadType": "InlineBase64"}]}}


def run_body(entries: List[Mapping[str, str]]) -> Dict[str, Any]:
    """The on-demand job body: this run's table list, overriding the pipeline's default."""
    return {"executionData": {"parameters": {"tables": [dict(e) for e in entries]}}}


def failure_reason(instance: Mapping[str, Any]) -> str:
    reason = instance.get("failureReason") or {}
    if isinstance(reason, Mapping):
        text = str(reason.get("message") or reason.get("errorCode") or "")
    else:
        text = str(reason)
    return text[:400] or "Fabric did not give a reason. Open the pipeline's run history in Fabric."


def source_connection_name(workspace: str, pool: str) -> str:
    """The display name the accelerator gives the Fabric connection to a Synapse pool."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"synapse-{workspace}-{pool}")[:200]


def matches_pool(connection: Mapping[str, Any], server: str, database: str) -> bool:
    """Whether an existing Fabric connection already reaches this pool (server and database)."""
    details = connection.get("connectionDetails") or {}
    path = str(details.get("path") or "").lower()
    return bool(server) and server.lower() in path and (not database or database.lower() in path)


def find_pool_connection(connections: Mapping[str, Mapping[str, Any]], name: str, server: str, database: str) -> Optional[Mapping[str, Any]]:
    """The connection the data pipelines read through: one with the accelerator's name, else any that reaches the pool."""
    for display, connection in connections.items():
        if display.lower() == name.lower() and connection.get("id"):
            return connection
    for connection in connections.values():
        if connection.get("id") and matches_pool(connection, server, database):
            return connection
    return None
