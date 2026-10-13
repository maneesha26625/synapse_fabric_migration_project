"""Does it run in Fabric? The second half of validation.

Comparing definitions shows an object arrived; it does not show that it works. These checks
ask Fabric itself:

* **Tables and views** are queried (``SELECT TOP (1) *``). A view that references something
  missing, or uses syntax the Warehouse rejects at run time, fails here.
* **Stored procedures** are compiled without being executed (``sp_describe_first_result_set``).
  On request, those with no required parameters are executed inside a transaction that is rolled
  back, so their Warehouse changes do not stay.
* **Pipelines, notebooks (including SQL-script notebooks) and Spark job definitions** report their
  latest run from Fabric's run history. On request they are run now and followed to the end:
  **a real run**, which reads and writes whatever the item reads and writes.

Each check gives ``{"status", "check", "detail", "runAt"}``:

* PASS         it ran (or compiled, or the query returned) without an error;
* FAIL         Fabric reported an error; the detail carries Fabric's message;
* NOT RUN      it has never run in Fabric, and was not asked to run now;
* NOT CHECKED  the check could not be made (the reason is in the detail).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

from discovery_agent.migration import datapipeline, warehouse_ddl
from discovery_agent.migration.common import NOTEBOOK, PIPELINE, PROCEDURE, SCRIPT, SPARKJOB, TABLE, VIEW, Source
from discovery_agent.migration.fabric_rest import FabricApiError

PASS, FAIL, NOT_RUN, NOT_CHECKED = "PASS", "FAIL", "NOT RUN", "NOT CHECKED"

#: Fabric item collection and on-demand job type for each kind that runs as a Fabric job.
JOBS: Dict[str, Tuple[str, str]] = {
    PIPELINE: ("dataPipelines", "Pipeline"),
    NOTEBOOK: ("notebooks", "RunNotebook"),
    SCRIPT: ("notebooks", "RunNotebook"),
    SPARKJOB: ("sparkJobDefinitions", "sparkjob"),
}
#: What may be run for real on request. Tables and views are only ever queried.
RUNNABLE = frozenset({PIPELINE, NOTEBOOK, SCRIPT, SPARKJOB, PROCEDURE})
DEFAULT_TIMEOUT_MINUTES = 30
MAX_TIMEOUT_MINUTES = 240
MAX_PARALLEL_RUNS = 3
POLL_SECONDS = 15
SQL_TIMEOUT_SECONDS = 300


@dataclass
class RuntimeOptions:
    """What the operator allowed. ``probe``: the read-only Warehouse checks. ``run``: kinds to run now."""

    probe: bool = True
    run: FrozenSet[str] = frozenset()
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES

    @classmethod
    def parse(cls, raw: Any) -> "RuntimeOptions":
        if not isinstance(raw, Mapping):
            return cls()
        run = raw.get("run") or []
        kinds = frozenset(str(k) for k in run if str(k) in RUNNABLE) if isinstance(run, list) else frozenset()
        try:
            minutes = int(raw.get("timeoutMinutes") or DEFAULT_TIMEOUT_MINUTES)
        except (TypeError, ValueError):
            minutes = DEFAULT_TIMEOUT_MINUTES
        return cls(probe=raw.get("probe", True) is not False, run=kinds, timeout_minutes=max(1, min(minutes, MAX_TIMEOUT_MINUTES)))


def result(status: str, check: str, detail: str, run_at: Optional[str] = None) -> Dict[str, Any]:
    return {"status": status, "check": check, "detail": detail, "runAt": run_at}


def _message(exc: BaseException) -> str:
    text = str(getattr(exc, "message", None) or exc)
    # The ODBC driver prefixes its messages with [Microsoft][ODBC Driver ...][SQL Server]; keep Fabric's own words.
    return text.split("]")[-1].strip()[:400] if "[SQL Server]" in text else text[:400]


# -- the Warehouse ------------------------------------------------------------------------------


def query_check(cursor: Any, source: Source) -> Dict[str, Any]:
    what = "view" if source.kind == VIEW else "table"
    name = warehouse_ddl.qualified(source.schema or "dbo", source.object_name or source.name)
    try:
        cursor.execute(f"SELECT TOP (1) * FROM {name}")
        cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - the server's message is the result
        return result(FAIL, f"Queried the {what}", f"The Warehouse rejected SELECT TOP (1) * FROM {name}: {_message(exc)}")
    return result(PASS, f"Queried the {what}", f"SELECT TOP (1) * FROM {name} ran without an error.")


def _required(source: Source) -> List[str]:
    params = getattr(source.payload, "parameters", ()) or ()
    return [str(p.name) for p in params if not p.is_return_value and not p.has_default and not p.is_output]


def _exec_text(source: Source) -> str:
    name = warehouse_ddl.qualified(source.schema or "dbo", source.object_name or source.name)
    params = [p for p in (getattr(source.payload, "parameters", ()) or ()) if not p.is_return_value and p.name and not p.has_default]
    args = ", ".join(f"{p.name} = NULL" for p in params)
    return f"EXEC {name}" + (f" {args}" if args else "")


def procedure_check(cursor: Any, source: Source, execute: bool) -> Dict[str, Any]:
    required = _required(source)
    if execute and not required:
        statement = _exec_text(source)
        try:
            cursor.execute(f"BEGIN TRANSACTION; {statement}; ROLLBACK TRANSACTION;")
            while _next_set(cursor):
                pass
        except Exception as exc:  # noqa: BLE001
            _rollback(cursor)
            return result(FAIL, "Executed (rolled back)", f"{statement} failed: {_message(exc)}")
        return result(PASS, "Executed (rolled back)", f"{statement} ran without an error; its changes were rolled back.")
    statement = _exec_text(source).replace("'", "''")
    try:
        cursor.execute(f"EXEC sys.sp_describe_first_result_set @tsql = N'{statement}'")
        cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        text = _message(exc)
        if "sp_describe_first_result_set" in text:
            return result(NOT_CHECKED, "Compiled", "This Warehouse cannot compile-check procedures. Execute it to prove it runs.")
        if "#" in text:
            return result(NOT_CHECKED, "Compiled", f"It uses temporary tables, which are only resolved when it runs ({text}). Execute it to prove it runs.")
        return result(FAIL, "Compiled", f"The Warehouse could not compile it: {text}")
    note = "" if not execute else f" Not executed: it needs {', '.join(required)}."
    return result(PASS, "Compiled", "It compiles against the Warehouse's current objects." + note
                  + ("" if execute else " Compiling does not prove every branch runs; execute it to be sure."))


def _next_set(cursor: Any) -> bool:
    nextset = getattr(cursor, "nextset", None)
    try:
        return bool(nextset()) if callable(nextset) else False
    except Exception:  # noqa: BLE001 - no more result sets
        return False


def _rollback(cursor: Any) -> None:
    try:
        cursor.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;")
    except Exception:  # noqa: BLE001 - best effort
        pass


# -- Fabric jobs ------------------------------------------------------------------------------------


def _when(instance: Mapping[str, Any]) -> str:
    return str(instance.get("endTimeUtc") or instance.get("startTimeUtc") or "")


def from_instance(instance: Mapping[str, Any], check: str) -> Dict[str, Any]:
    status = str(instance.get("status") or "")
    when = _when(instance) or None
    if status == "Completed":
        return result(PASS, check, f"Succeeded{f' ({when})' if when else ''}.", when)
    if status in ("Failed", "Cancelled", "Deduped"):
        return result(FAIL, check, f"{status}{f' ({when})' if when else ''}: {datapipeline.failure_reason(instance)}", when)
    return result(NOT_CHECKED, check, f"Still {status or 'running'}; validate again when it has finished.", when)


def last_run(rest: Any, workspace_id: str, item_id: str) -> Dict[str, Any]:
    try:
        runs = rest.list(f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances")
    except FabricApiError as exc:
        return result(NOT_CHECKED, "Last run in Fabric", f"The run history could not be read: {exc.message[:200]}")
    if not runs:
        return result(NOT_RUN, "Last run in Fabric", "It has never run in Fabric. Run it there, or choose to run it from this step.")
    latest = max(runs, key=lambda r: str(r.get("startTimeUtc") or ""))
    return from_instance(latest, "Last run in Fabric")


@dataclass
class _Run:
    key: str
    item_id: str
    job_type: str
    label: str
    instance: Optional[str] = None
    started: float = 0.0
    outcome: Optional[Dict[str, Any]] = None


@dataclass
class JobRunner:
    """Runs Fabric items a few at a time and follows each to the end."""

    rest: Any
    workspace_id: str
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    progress: Callable[[str], None] = lambda text: None
    parallel: int = MAX_PARALLEL_RUNS
    _queue: List[_Run] = field(default_factory=list)

    def add(self, key: str, item_id: str, job_type: str, label: str) -> None:
        self._queue.append(_Run(key, item_id, job_type, label))

    def run(self) -> Dict[str, Dict[str, Any]]:
        waiting, active, done = list(self._queue), [], {}
        total = len(waiting)
        limit = self.timeout_minutes * 60
        while waiting or active:
            while waiting and len(active) < self.parallel:
                job = waiting.pop(0)
                try:
                    job.instance = self.rest.run_job(
                        f"/workspaces/{self.workspace_id}/items/{job.item_id}/jobs/instances?jobType={job.job_type}", {})
                    job.started = self.clock()
                    active.append(job)
                except FabricApiError as exc:
                    done[job.key] = result(FAIL, "Ran now in Fabric", f"Fabric did not start it: {exc.message[:300]}")
            self.progress(f"Running in Fabric: {len(done)} of {total} finished, {len(active)} running")
            if not active:
                continue
            self.sleep(POLL_SECONDS)
            for job in list(active):
                try:
                    state = self.rest.get(job.instance)
                except FabricApiError as exc:
                    state = {"status": "Unknown", "failureReason": {"message": exc.message}}
                if str(state.get("status") or "") in datapipeline.FINISHED:
                    done[job.key] = from_instance(state, "Ran now in Fabric")
                    active.remove(job)
                elif self.clock() - job.started > limit:
                    done[job.key] = result(NOT_CHECKED, "Ran now in Fabric",
                                           f"Still running after {self.timeout_minutes} minutes; follow it in Fabric's Monitoring hub, then validate again.")
                    active.remove(job)
        return done
