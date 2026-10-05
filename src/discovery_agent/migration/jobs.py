"""Smaller conversions: Spark job definitions, SQL scripts, trigger schedules and external-table shortcuts.

Each is a pure function from the Synapse resource to a Fabric request body (or
a reason there is none), so the rules are testable without Fabric.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from discovery_agent.migration.notebooks import create_body as notebook_create_body

# ---- Spark job definitions ---------------------------------------------------------

_LANGUAGES = {"python": "Python", "pyspark": "Python", "scala": "Scala", "spark": "Scala", "java": "Scala", "r": "R", "sparkr": "R"}


class NotConvertible(Exception):
    """The resource has no faithful Fabric form; the message says why."""


def spark_job(payload: Mapping[str, Any], environment_id: Optional[str] = None) -> Tuple[Dict[str, Any], List[str]]:
    """(``SparkJobDefinitionV1`` content, notes) for a Synapse Spark job definition."""
    props = payload.get("properties") or {}
    job = props.get("jobProperties") or {}
    language = str(props.get("language") or job.get("language") or "python").strip().lower()
    if language not in _LANGUAGES:
        raise NotConvertible(f"A {language} Spark job has no Fabric equivalent. Rewrite it in PySpark or Scala.")
    file = str(job.get("file") or "")
    if not file:
        raise NotConvertible("The job definition has no main file, so there is nothing to run.")
    notes: List[str] = []
    if job.get("conf") or any(job.get(k) for k in ("driverMemory", "driverCores", "executorMemory", "executorCores", "numExecutors")):
        notes.append("Spark settings (executor and driver sizing, conf) were dropped: set them in the job's Fabric Environment.")
    if props.get("targetBigDataPool"):
        notes.append(f"Target Spark pool '{(props['targetBigDataPool'] or {}).get('referenceName')}' maps to the Environment of the same name"
                     + ("." if environment_id else ", which does not exist yet; run the Spark pool stage."))
    if not str(file).startswith(("abfss://", "https://")):
        notes.append("The main file is not an abfss:// path; check that Fabric can read it.")
    content = {
        "executableFile": file,
        "defaultLakehouseArtifactId": None,
        "mainClass": str(job.get("className") or ""),
        "additionalLakehouseIds": [],
        "retryPolicy": None,
        "commandLineArguments": " ".join(str(a) for a in (job.get("args") or [])),
        "additionalLibraryUris": [str(u) for u in (job.get("jars") or []) + (job.get("files") or [])],
        "language": _LANGUAGES[language],
        "environmentArtifactId": environment_id,
    }
    return content, notes


def spark_job_body(name: str, content: Mapping[str, Any], description: str = "") -> Dict[str, Any]:
    payload = base64.b64encode(json.dumps(content).encode("utf-8")).decode("ascii")
    body: Dict[str, Any] = {"displayName": name, "definition": {"format": "SparkJobDefinitionV1", "parts": [
        {"path": "SparkJobDefinitionV1.json", "payload": payload, "payloadType": "InlineBase64"}]}}
    if description:
        body["description"] = description[:256]
    return body


# ---- SQL scripts -> notebooks --------------------------------------------------------


def sql_script_notebook(payload: Mapping[str, Any], warehouse_id: Optional[str], warehouse_name: str) -> Tuple[Dict[str, Any], List[str]]:
    """(ipynb document, notes): the script in one T-SQL cell, bound to the Warehouse."""
    props = payload.get("properties") or {}
    content = props.get("content") or {}
    query = str(content.get("query") or "")
    if not query.strip():
        raise NotConvertible("The script has no query text.")
    language = str((content.get("metadata") or {}).get("language") or "sql").lower()
    if language != "sql":
        raise NotConvertible(f"The script is {language}, not SQL.")
    notes = ["Review before running: the script ran on the Synapse pool and may use T-SQL a Fabric Warehouse does not support."]
    pool = (content.get("currentConnection") or {}).get("poolName") or (content.get("currentConnection") or {}).get("databaseName")
    if pool:
        notes.append(f"It was written against '{pool}'; it is bound to the Warehouse '{warehouse_name}'.")
    metadata: Dict[str, Any] = {"language_info": {"name": "sql"}}
    if warehouse_id:
        metadata["dependencies"] = {"warehouse": {"default_warehouse": warehouse_id,
                                                  "known_warehouses": [{"id": warehouse_id, "type": "Datawarehouse"}]}}
    document = {
        "nbformat": 4, "nbformat_minor": 5, "metadata": metadata,
        "cells": [{"cell_type": "code", "source": query.splitlines(keepends=True), "outputs": [], "execution_count": None,
                   "metadata": {"microsoft": {"language": "sql", "language_group": "sqldatawarehouse"}}}],
    }
    return document, notes


def sql_script_body(name: str, document: Mapping[str, Any]) -> Dict[str, Any]:
    return notebook_create_body(name, document, "Migrated from an Azure Synapse SQL script")


# ---- triggers -> schedules -------------------------------------------------------------

_WEEKDAYS = {"monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday", "thursday": "Thursday",
             "friday": "Friday", "saturday": "Saturday", "sunday": "Sunday"}
_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})")
FAR_FUTURE_YEARS = 10


def _stamp(value: Any, fallback: datetime) -> datetime:
    m = _ISO.match(str(value or ""))
    if not m:
        return fallback
    return datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}", "%Y-%m-%d %H:%M:%S")


def _times(schedule: Mapping[str, Any], start: datetime) -> List[str]:
    hours = [int(h) for h in schedule.get("hours") or [start.hour]]
    minutes = [int(m) for m in schedule.get("minutes") or [start.minute]]
    return sorted({f"{h:02d}:{m:02d}" for h in hours for m in minutes})


def schedule_bodies(payload: Mapping[str, Any], now: Optional[datetime] = None) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[str]]:
    """([(pipeline name, Fabric schedule body)], notes) for one Synapse trigger.

    Schedules are always created switched off: a trigger that fires before the
    data and pipelines it feeds are ready does harm, and enabling is one click.
    """
    props = payload.get("properties") or {}
    kind = str(props.get("type") or "")
    if kind != "ScheduleTrigger":
        raise NotConvertible(f"A {kind or 'this kind of'} trigger has no pipeline schedule equivalent (event triggers use Fabric Activator). Recreate it by hand.")
    tp = props.get("typeProperties") or {}
    rec = tp.get("recurrence") or {}
    frequency = str(rec.get("frequency") or "").lower()
    interval = int(rec.get("interval") or 1)
    sched = rec.get("schedule") or {}
    start = _stamp(rec.get("startTime"), (now or datetime.now(timezone.utc)).replace(tzinfo=None, microsecond=0))
    end = _stamp(rec.get("endTime"), start + timedelta(days=365 * FAR_FUTURE_YEARS))
    notes: List[str] = ["Created switched off. Enable it in Fabric when the pipeline and its data are ready."]
    config: Dict[str, Any] = {
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "localTimeZoneId": str(rec.get("timeZone") or "UTC"),
    }
    if frequency == "minute":
        config.update(type="Cron", interval=max(1, interval))
    elif frequency == "hour":
        config.update(type="Cron", interval=max(1, interval) * 60)
        if sched.get("minutes"):
            notes.append("The minute offset within the hour was not carried over.")
    elif frequency == "day":
        if interval != 1:
            raise NotConvertible(f"Every {interval} days has no Fabric schedule form (Fabric daily schedules run every day). Recreate it by hand.")
        config.update(type="Daily", times=_times(sched, start))
    elif frequency == "week":
        if interval != 1:
            raise NotConvertible(f"Every {interval} weeks has no Fabric schedule form. Recreate it by hand.")
        days = [_WEEKDAYS[d.lower()] for d in sched.get("weekDays") or [] if str(d).lower() in _WEEKDAYS] or [start.strftime("%A")]
        config.update(type="Weekly", weekdays=days, times=_times(sched, start))
    else:
        raise NotConvertible(f"A {frequency or 'unknown'}-frequency trigger has no Fabric schedule form. Recreate it by hand.")
    refs = [str((p.get("pipelineReference") or {}).get("referenceName") or "") for p in props.get("pipelines") or []]
    legacy = str(((props.get("pipeline") or {}).get("pipelineReference") or {}).get("referenceName") or "")
    names = [r for r in refs if r] or ([legacy] if legacy else [])
    if not names:
        raise NotConvertible("The trigger does not run any pipeline.")
    if any(p.get("parameters") for p in props.get("pipelines") or []):
        notes.append("Parameter values passed by the trigger were not carried over: set them as the pipeline's defaults.")
    return [(n, {"enabled": False, "configuration": dict(config)}) for n in names], notes


# ---- external tables -> shortcuts --------------------------------------------------------

EXTERNAL_LOCATION_SQL = (
    "SELECT ds.location, t.location FROM sys.external_tables t "
    "JOIN sys.schemas s ON s.schema_id = t.schema_id "
    "JOIN sys.external_data_sources ds ON ds.data_source_id = t.data_source_id "
    "WHERE s.name = ? AND t.name = ?"
)
_STORAGE = re.compile(r"^(?:abfss?|wasbs?)://(?P<container>[^@/]+)@(?P<account>[^./]+)\.(?:dfs|blob)\.core\.windows\.net(?P<path>/.*)?$", re.I)


def shortcut_target(data_source_location: str, table_location: str) -> Tuple[str, str]:
    """(storage endpoint, subpath) for an external table's data. Raises NotConvertible when it is not Azure storage."""
    m = _STORAGE.match((data_source_location or "").strip())
    if not m:
        raise NotConvertible(f"The data source location '{(data_source_location or '')[:80]}' is not Azure Data Lake or Blob storage, so it cannot become a OneLake shortcut.")
    base = (m.group("path") or "/").rstrip("/")
    sub = "/" + "/".join(p for p in (m.group("container"), base.strip("/"), (table_location or "").strip("/")) if p)
    return f"https://{m.group('account')}.dfs.core.windows.net", sub
