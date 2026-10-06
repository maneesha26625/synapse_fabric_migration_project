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
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from discovery_agent.migration import datacopy, warehouse_ddl
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


def entry(table: SqlTable, replace: bool) -> Dict[str, str]:
    """One element of the pipeline's ``tables`` parameter."""
    schema, name = table.key.schema, table.key.name
    return {
        "schema": schema,
        "table": name,
        "query": datacopy.select_sql(table),
        "preCopyScript": f"TRUNCATE TABLE {warehouse_ddl.qualified(schema, name)}" if replace else "",
    }


def _expression(value: str) -> Dict[str, str]:
    return {"value": value, "type": "Expression"}


def definition(entries: List[Mapping[str, str]], connection_id: str, warehouse: WarehouseTarget) -> Dict[str, Any]:
    """The ``pipeline-content.json`` of one wave's data pipeline."""
    sink: Dict[str, Any] = {
        "type": "DataWarehouseSink",
        "allowCopyCommand": True,
        "tableOption": "None",
        "preCopyScript": _expression("@item().preCopyScript"),
        "datasetSettings": {
            "annotations": [],
            "type": "DataWarehouseTable",
            "schema": [],
            "typeProperties": {"schema": _expression("@item().schema"), "table": _expression("@item().table")},
            "linkedService": {
                "name": warehouse.name,
                "properties": {
                    "annotations": [],
                    "type": "DataWarehouse",
                    "typeProperties": {"endpoint": warehouse.endpoint, "artifactId": warehouse.artifact_id,
                                       "workspaceId": warehouse.workspace_id},
                },
            },
        },
    }
    copy = {
        "name": "Copy table",
        "type": "Copy",
        "dependsOn": [],
        "policy": {"timeout": "0.12:00:00", "retry": 2, "retryIntervalInSeconds": 60, "secureOutput": False, "secureInput": False},
        "typeProperties": {
            "source": {
                "type": "SqlDWSource",
                "sqlReaderQuery": _expression("@item().query"),
                "queryTimeout": "02:00:00",
                "partitionOption": "None",
                "datasetSettings": {
                    "annotations": [],
                    "type": "AzureSqlDWTable",
                    "schema": [],
                    "typeProperties": {},
                    "externalReferences": {"connection": connection_id},
                },
            },
            "sink": sink,
            # A Synapse source cannot be read by the Warehouse's COPY command directly,
            # so the Copy activity stages the rows in the workspace first.
            "enableStaging": True,
            "translator": {"type": "TabularTranslator", "typeConversion": True,
                           "typeConversionSettings": {"allowDataTruncation": True, "treatBooleanAsNumber": False}},
        },
    }
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
