"""Cutover: the switch from Synapse to Fabric, made safe to take and to undo.

What it reads (nothing written):

* **Freshness**: for each table kept in sync, the watermark in the Warehouse's control table against the newest
  change in Synapse now. A table whose Synapse side has moved on is behind, and says by how much.
* **Schedules**: every pipeline schedule in the Fabric workspace and whether it is on. The run creates them
  switched off, so nothing runs before cutover.
* **The sync pipeline's last run**, and the latest validation's result.

What it does, only when a person asks (the one place besides a run and Validate that writes to Fabric):

* **Final sync**: runs the sync pipeline now, so the last changes arrive before consumers move.
* **Switch schedules on** at cutover, or **off** again: the rollback lever. Synapse is never touched; its
  triggers are stopped by a person, and pointing consumers back at Synapse is the rest of a rollback.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from discovery_agent.migration import datafilter, datapipeline, runtime_checks
from discovery_agent.migration.fabric_rest import FabricApiError

#: The only shape of watermark query the run registers (``datafilter.watermark_sql``); anything else is not run on Synapse.
_WATERMARK_QUERY = re.compile(r"^SELECT COALESCE\(CONVERT\(varchar\(40\), MAX\(\[[^;]*$", re.IGNORECASE)
CONTROL_ROWS_SQL = f"SELECT table_name, watermark, watermark_query FROM {datafilter.CONTROL} ORDER BY table_name"


def _when(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text).strip().replace("Z", ""))
    except ValueError:
        return None


def freshness(warehouse_cursor: Any, source_cursor: Optional[Any]) -> Dict[str, Any]:
    """Each synced table: its watermark in Fabric, the newest change in Synapse, and whether Fabric is behind."""
    try:
        warehouse_cursor.execute(CONTROL_ROWS_SQL)
        rows = [tuple(r) for r in warehouse_cursor.fetchall()]
    except Exception:  # noqa: BLE001 - no control table: nothing is kept in sync
        return {"tables": [], "behind": 0, "note": "No table is kept in sync: every table was loaded once."}
    out: List[Dict[str, Any]] = []
    for name, watermark, query in rows:
        entry: Dict[str, Any] = {"table": str(name), "watermark": str(watermark or ""), "newest": None, "behind": None, "lagMinutes": None}
        if source_cursor is None:
            entry["detail"] = "The Synapse source is not connected, so its newest change could not be read."
        elif not _WATERMARK_QUERY.match(str(query or "")):
            entry["detail"] = "The registered query is not one this tool wrote, so it was not run on Synapse."
        else:
            try:
                source_cursor.execute(str(query))
                found = source_cursor.fetchone()
                newest = datafilter.clean_watermark(found[0]) if found and found[0] else ""
            except Exception as exc:  # noqa: BLE001
                entry["detail"] = f"Synapse could not be read: {str(exc)[:160]}"
            else:
                entry["newest"] = newest or None
                a, b = _when(newest), _when(str(watermark or ""))
                entry["behind"] = bool(a and b and a > b)
                if a and b and a > b:
                    entry["lagMinutes"] = round((a - b).total_seconds() / 60)
                entry["detail"] = "Behind Synapse: run the final sync." if entry["behind"] else "In step with Synapse."
        out.append(entry)
    return {"tables": out, "behind": sum(1 for t in out if t["behind"])}


def schedules(rest: Any, workspace_id: str) -> List[Dict[str, Any]]:
    """Every pipeline schedule in the workspace, with whether it is on."""
    out: List[Dict[str, Any]] = []
    for p in rest.list(f"/workspaces/{workspace_id}/dataPipelines"):
        pid, name = str(p.get("id") or ""), str(p.get("displayName") or "")
        try:
            items = rest.list(f"/workspaces/{workspace_id}/items/{pid}/jobs/Pipeline/schedules")
        except FabricApiError:
            continue
        for s in items:
            config = s.get("configuration") or {}
            out.append({"pipeline": name, "pipelineId": pid, "scheduleId": str(s.get("id") or ""), "enabled": bool(s.get("enabled")),
                        "type": config.get("type"), "times": config.get("times"), "interval": config.get("interval"),
                        "sync": name.startswith("sync_")})
    return sorted(out, key=lambda s: (not s["sync"], s["pipeline"].lower()))


def set_schedules(rest: Any, workspace_id: str, enabled: bool, pipelines: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Switch schedules on or off, keeping each one's timing. ``pipelines``: only these (by name); None for all."""
    wanted = {p.lower() for p in pipelines} if pipelines else None
    changed: List[str] = []
    failed: List[str] = []
    for s in schedules(rest, workspace_id):
        if wanted is not None and s["pipeline"].lower() not in wanted:
            continue
        if s["enabled"] == enabled:
            continue
        path = f"/workspaces/{workspace_id}/items/{s['pipelineId']}/jobs/Pipeline/schedules/{s['scheduleId']}"
        try:
            current = rest.get(path)
            rest.patch(path, {"enabled": enabled, "configuration": current.get("configuration") or {}})
            changed.append(s["pipeline"])
        except FabricApiError as exc:
            failed.append(f"{s['pipeline']}: {exc.message[:160]}")
    return {"changed": changed, "failed": failed, "enabled": enabled}


def sync_pipeline(rest: Any, workspace_id: str, warehouse: str) -> Optional[str]:
    """The id of the Warehouse's sync pipeline, or None when no table is kept in sync."""
    name = datapipeline.sync_name(warehouse).lower()
    return next((str(p.get("id")) for p in rest.list(f"/workspaces/{workspace_id}/dataPipelines")
                 if str(p.get("displayName") or "").lower() == name), None)


def last_sync(rest: Any, workspace_id: str, warehouse: str) -> Optional[Dict[str, Any]]:
    pid = sync_pipeline(rest, workspace_id, warehouse)
    return runtime_checks.last_run(rest, workspace_id, pid) if pid else None


def run_final_sync(rest: Any, workspace_id: str, warehouse: str) -> str:
    """Start the sync pipeline now. Returns the job instance path to follow."""
    pid = sync_pipeline(rest, workspace_id, warehouse)
    if not pid:
        raise LookupError("There is no sync pipeline: no table is kept in sync, so there is nothing to sync.")
    return rest.run_job(f"/workspaces/{workspace_id}/items/{pid}/jobs/instances?jobType=Pipeline", {})


def readiness(validation: Mapping[str, Any], fresh: Mapping[str, Any], scheds: Sequence[Mapping[str, Any]],
              sync_run: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The checks a person reads before cutting over, each passed, failed or not applicable."""
    checks: List[Dict[str, Any]] = []
    summary = (validation or {}).get("summary") or {}
    if (validation or {}).get("state") != "completed":
        checks.append({"label": "Validation has run", "status": "fail", "detail": "Run Validate first: cutover needs evidence the data agrees."})
    else:
        mism = int(summary.get("MISMATCH") or summary.get("mismatch") or 0)
        checks.append({"label": "Validation has no mismatches", "status": "pass" if not mism else "fail",
                       "detail": f"{mism} mismatch(es) in the last validation ({validation.get('finishedAt') or 'time unknown'})."
                       if mism else f"Last validated {validation.get('finishedAt') or ''}."})
    tables = fresh.get("tables") or []
    if tables:
        behind = int(fresh.get("behind") or 0)
        checks.append({"label": "Synced tables are current", "status": "pass" if not behind else "fail",
                       "detail": f"{behind} of {len(tables)} table(s) behind Synapse: run the final sync." if behind
                       else f"All {len(tables)} synced table(s) are in step with Synapse."})
    if sync_run is not None:
        ok = sync_run.get("status") == runtime_checks.PASS
        checks.append({"label": "The last sync succeeded", "status": "pass" if ok else "fail", "detail": str(sync_run.get("detail") or "")})
    off = [s for s in scheds if not s.get("enabled")]
    checks.append({"label": "Schedules ready to switch on", "status": "info",
                   "detail": f"{len(off)} of {len(scheds)} schedule(s) are off; switch them on as the cutover's last step." if scheds
                   else "No pipeline schedules in the workspace."})
    checks.append({"label": "Synapse triggers stopped", "status": "person",
                   "detail": "Stop the matching Synapse triggers so both sides do not load the same data. This tool never changes Synapse."})
    return checks
