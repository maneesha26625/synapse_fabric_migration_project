"""Where a project's migration runs are kept, so a restart, or switching projects, loses none of them.

One folder per run, under the project's store:

* ``runs/<id>/run.json``: the run's own record (its status, options, passes, sign-off, what it was started
  from), rewritten at every change of status;
* ``runs/<id>/events.jsonl``: append-only, in order: every log line, and every change to an object;
* ``runs/<id>/items.json``: the objects as they stood after the last pass (or pause), with the event they stood
  at. Loading a run reads it, then applies the object events written after it.

``migration.json`` says which run is the project's current one (the one Migrate shows) and how many runs it
has had, so run numbers carry on across restarts. Closed runs stay where they are, as the run history.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from discovery_agent.state.store import Store

POINTER = "migration"
#: A run's id: its number in the project, three digits or more.
RUN_ID = re.compile(r"^[0-9]{3,9}$")


class RunRecorder:
    def __init__(self, store: Store) -> None:
        self.store = store

    # -- which run is current -------------------------------------------------------------------

    def pointer(self) -> Dict[str, Any]:
        value = self.store.get(POINTER) or {}
        return {"current": value.get("current"), "counter": int(value.get("counter") or 0)}

    def set_pointer(self, current: Optional[str], counter: int) -> None:
        self.store.put(POINTER, {"current": current, "counter": int(counter)})

    # -- one run -------------------------------------------------------------------------------

    def save_run(self, document: Dict[str, Any]) -> None:
        self.store.put(f"runs/{document['runId']}/run", document)

    def save_items(self, run_id: str, items: List[Dict[str, Any]], seq: int) -> None:
        self.store.put(f"runs/{run_id}/items", {"seq": int(seq), "items": items})

    def event(self, run_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
        return self.store.append(f"runs/{run_id}/events", record)

    def events(self, run_id: str, after: int = 0, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.store.records(f"runs/{run_id}/events", after=after, limit=limit)

    def last_seq(self, run_id: str) -> int:
        return self.store.seq(f"runs/{run_id}/events")

    def load(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The run's record and its objects as they stand now (the snapshot, then every object event after it)."""
        document = self.store.get(f"runs/{run_id}/run")
        if not isinstance(document, dict):
            return None
        snapshot = self.store.get(f"runs/{run_id}/items") or {}
        items = {str(i.get("id")): dict(i) for i in snapshot.get("items") or [] if isinstance(i, dict)}
        order = [str(i.get("id")) for i in snapshot.get("items") or [] if isinstance(i, dict)]
        for event in self.events(run_id, after=int(snapshot.get("seq") or 0)):
            item = event.get("item")
            if event.get("type") == "item" and isinstance(item, dict) and item.get("id"):
                if str(item["id"]) not in items:
                    order.append(str(item["id"]))
                items[str(item["id"])] = dict(item)
        return {"run": document, "items": [items[i] for i in order if i in items]}

    def runs(self) -> List[Dict[str, Any]]:
        """Every run of the project, newest first, as a summary."""
        out = []
        for run_id in self._run_ids():
            document = self.store.get(f"runs/{run_id}/run")
            if isinstance(document, dict):
                out.append({k: document.get(k) for k in (
                    "runId", "status", "pausedReason", "createdAt", "startedAt", "finishedAt", "closedAt", "workspace",
                    "warehouse", "requestedBy", "counts", "passes", "approval")})
        out.sort(key=lambda r: int(r.get("runId") or 0), reverse=True)  # by number: run 1000 comes after 999
        for summary in out:
            summary["passes"] = len(summary.get("passes") or [])
        return out

    def highest(self) -> int:
        """The highest run number recorded in the project: the next run is numbered after it, whatever the pointer says."""
        return max((int(i) for i in self._run_ids()), default=0)

    def _run_ids(self) -> List[str]:
        """The runs recorded in the project. Anything else in the folder (a name that is not a run's) is left alone."""
        folder = self.store.root / "runs"
        return sorted(p.name for p in folder.iterdir() if p.is_dir() and RUN_ID.match(p.name)) if folder.is_dir() else []
