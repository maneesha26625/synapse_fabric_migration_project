"""The migration policy: one object, built from the approved brief, that every consumer reads.

Nothing outside this module looks at a question or an answer. Plan reads the run options and the scope rules it
pre-sets; Migrate reads the sign-off rule, the names, the wave gate, the principal map and the commit scope; the
LLM layer reads consent, budget and standards; Validate reads its depth. A consumer asks for a setting by name and
gets None when the brief does not decide it, and then behaves as it did before there were briefs.

**Freshness.** A policy built from a brief whose facts differ from the estate's now is *stale*: it still says what
was decided, and lists exactly what changed, but a run under a required consultation is refused until a person
reconfirms the brief.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from discovery_agent.consult.facts import changes, fingerprint

#: Which stages each part of a migration (A1) switches on. External tables (shortcuts) are part of the structure.
PARTS = {
    "structure": ("warehouse", "security", "shortcuts"),
    "data": ("data",),
    "orchestration": ("spark", "notebooks", "connections", "pipelines", "spark_jobs", "sql_scripts", "schedules"),
}
#: A date column that moves when a row changes: what a sync can follow.
CHANGE_COLUMN = re.compile(r"(modif|updat|chang|last_?mod|_ts$|^ts$|timestamp)", re.IGNORECASE)
#: A date column a row is dated by (when it happened, or was made): what "rows since a date" reads first.
EVENT_COLUMN = re.compile(r"(creat|insert|load|order|transaction|event|posted|booked|_date$|^date)", re.IGNORECASE)
#: What Validate checks at each depth.
DEPTHS = {"compare": {"probe": False, "run": []}, "probe": {"probe": True, "run": []},
          "run": {"probe": True, "run": ["pipeline", "notebook", "script", "sparkjob"]}}
#: The run option each enforced Migrate setting is, by the names the page and the run use.
RUN_OPTIONS = (("run.stopOnFailure", "stopOnFailure"), ("run.waveGate", "waveGate"), ("run.existing", "existing"),
               ("warehouse.collation", "collation"), ("data.mode", "dataMode"), ("data.run", "dataRun"),
               ("data.syncOverlap", "syncOverlap"), ("release.commitScope", "commitScope"))


def change_column(names: List[str]) -> Optional[str]:
    return next((n for n in names if CHANGE_COLUMN.search(n)), None)


def history_column(names: List[str]) -> Optional[str]:
    """The column "rows since a date" filters on: one a row is dated by, else one that moves when it changes, else
    the first. Plan shows it on each table, so a person can choose another."""
    return next((n for n in names if EVENT_COLUMN.search(n)), None) or change_column(names) or (names[0] if names else None)


class Policy:
    def __init__(self, version: Mapping[str, Any], facts_now: Mapping[str, Any], approval: Mapping[str, Any]) -> None:
        content = version["content"]
        self.version = int(version["version"])
        self.hash = str(version["hash"])
        self.settings: Dict[str, Any] = dict(content.get("settings") or {})
        self.facts: Dict[str, Any] = dict(content.get("facts") or {})
        self.fingerprint = str(content.get("factsFingerprint") or "")
        self.approved_by = approval.get("by")
        self.approved_at = approval.get("at")
        now = fingerprint(facts_now) if facts_now else self.fingerprint
        self.stale = bool(facts_now) and now != self.fingerprint
        self.changes = changes(self.facts, facts_now) if self.stale else []

    def get(self, name: str, default: Any = None) -> Any:
        return self.settings.get(name, default)

    # -- Plan --------------------------------------------------------------------------------------------

    def stages(self, known: Tuple[str, ...]) -> Optional[List[str]]:
        parts = self.get("run.parts")
        if parts is None:
            return None
        on = {k for p in parts for k in PARTS.get(p, ())}
        if self.get("security.migrate") is False:
            on.discard("security")
        return [k for k in known if k in on]

    def run_options(self, known_stages: Tuple[str, ...]) -> Dict[str, Any]:
        """The run options the brief decides, by the names the page and the run use. A key is left out when the
        brief does not decide it."""
        out: Dict[str, Any] = {}
        stages = self.stages(known_stages)
        if stages is not None:
            out["stages"] = stages
        for setting, key in RUN_OPTIONS:
            if setting in self.settings:
                out[key] = self.settings[setting]
        if "release.gitCommit" in self.settings:
            out["gitCommit"] = "auto" if self.settings["release.gitCommit"] else "off"
        return out

    def data_filters(self, tables: List[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Each table's date filter, as the brief's history and sync decisions make it."""
        since = self.get("data.historyFrom") if self.get("data.historyMode") == "since" else None
        sync = bool(self.get("data.sync"))
        out: Dict[str, Dict[str, Any]] = {}
        for t in tables:
            dates = [str(c.get("name")) for c in t.get("dateColumns") or []]
            entry: Optional[Dict[str, Any]] = None
            if since and dates:
                entry = {"column": history_column(dates), "from": since, "to": "", "sync": False, "changeColumn": "", "keys": []}
            change = change_column(dates) if sync else None
            if change:
                entry = entry or {"column": "", "from": "", "to": "", "sync": False, "changeColumn": "", "keys": []}
                entry.update(sync=True, changeColumn=change, keys=list(t.get("keyColumns") or []))
            if entry is not None:
                out[str(t["key"])] = entry
        return out

    def differences(self, options: Mapping[str, Any], known_stages: Tuple[str, ...], left_out: Mapping[str, str],
                    tables: Optional[List[Mapping[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Where a run's options and plan differ from the brief: each ``{option, setting, brief, plan, message}``.

        ``left_out``: the plan's objects the brief leaves out, by id, with why (``excluded``). ``tables``: the plan's
        tables a date filter can apply to (``filterable_tables``), to compare the run's filters with the brief's."""
        out: List[Dict[str, Any]] = []
        for key, value in self.run_options(known_stages).items():
            have = options.get(key)
            same = set(have or []) == set(value) if key == "stages" else have == value
            if not same:
                out.append({"option": key, "setting": SETTING_OF.get(key), "brief": value, "plan": have,
                            "message": f"{LABELS.get(key, key)}: the brief says {_said(value)}, the plan has {_said(have)}."})
        if left_out:
            ids = sorted(left_out)
            out.append({"option": "items", "setting": "plan.scope", "brief": [], "plan": ids[:50],
                        "message": f"{len(ids)} object{'s' if len(ids) != 1 else ''} the brief leaves out "
                                   f"{'are' if len(ids) != 1 else 'is'} in the plan: {left_out[ids[0]]}"
                                   + (" (and others)" if len(ids) > 1 else "")})
        if self.get("storage.routes") == "none" and options.get("storageRoutes"):
            out.append({"option": "storageRoutes", "setting": "storage.routes", "brief": [], "plan": len(options["storageRoutes"]),
                        "message": "Storage routes: the brief leaves storage where it is, the plan routes it through OneLake."})
        if tables is not None and ("data.historyMode" in self.settings or "data.sync" in self.settings):
            wanted = self.data_filters(tables)
            given = {k: _filter(v) for k, v in (options.get("dataFilters") or {}).items()}
            keys = {str(t["key"]) for t in tables}
            moved = sorted(k for k in keys if _filter(wanted.get(k)) != given.get(k, _filter(None)))
            if moved:
                out.append({"option": "dataFilters", "setting": "data.historyMode", "brief": len(wanted), "plan": moved[:50],
                            "message": f"Date filters: {len(moved)} table{'s' if len(moved) != 1 else ''} "
                                       f"load{'s' if len(moved) == 1 else ''} differently from the brief (such as {moved[0]})."})
        return out

    def storage_routes(self, suggestions: List[Mapping[str, Any]]) -> Optional[List[Dict[str, str]]]:
        mode = self.get("storage.routes")
        if mode is None:
            return None
        if mode == "none":
            return []
        out = []
        for s in suggestions:
            route = {"account": str(s.get("account")), "container": str(s.get("container"))}
            if s.get("connectionId"):
                route["connectionId"] = str(s["connectionId"])
            elif s.get("linkedService"):
                route["linkedService"] = str(s["linkedService"])
            else:
                continue  # nothing can reach it: Plan shows it, and leaves it for a person
            out.append(route)
        return out

    def excluded(self, item: Mapping[str, Any], group_of: Mapping[str, List[str]], flags: Mapping[str, List[str]]) -> Optional[str]:
        """Why the brief leaves this object out of the plan (Plan's Add all), or None when it is in scope.

        ``group_of``: object id -> its scope groups (``schema:x``, ``folder:a/b``); ``flags``: object id -> what the
        estate says about it (``serverless``, ``multiStatement``, ``rebuild``)."""
        oid, name = str(item.get("id")), str(item.get("name") or "")
        marks = flags.get(oid) or []
        if "serverless" in marks and self.get("plan.serverless") == "leave_out":
            return "Written for the serverless SQL pool: the brief leaves it out (A3)."
        if "multiStatement" in marks and self.get("code.multiStatement") == "leave_out":
            return "A multi-statement table-valued function: the brief leaves these out (D3)."
        if "rebuild" in marks and self.get("code.rebuild") == "leave_out":
            return "No Fabric equivalent as it is: the brief leaves these out (D4)."
        mode = self.get("plan.scopeMode")
        if mode == "include":
            chosen = list(self.get("plan.scopeInclude") or [])
            groups = group_of.get(oid) or []
            # An object in no schema or folder (a linked service, a trigger, a pool) is never left out by it.
            if groups and not any(_within(g, c) for g in groups for c in chosen):
                return "Outside the schemas and folders the brief puts in scope (A2)."
        if mode == "exclude":
            short = name.split(".")[-1]
            for pattern in self.get("plan.scopeExclude") or []:
                if fnmatch.fnmatchcase(short.lower(), pattern.lower()) or fnmatch.fnmatchcase(name.lower(), pattern.lower()):
                    return f"Its name matches '{pattern}', which the brief leaves out (A2)."
        return None

    # -- Migrate, LLM, Validate -----------------------------------------------------------------------------

    def signoff(self) -> Optional[str]:
        return self.get("release.signoff")

    def names(self) -> Tuple[Optional[str], Optional[str]]:
        return self.get("names.warehouse"), self.get("names.lakehouse")

    def sql_user_map(self) -> Dict[str, str]:
        return dict(self.get("security.sqlUserMap") or {})

    def llm(self) -> Tuple[Optional[bool], Optional[float]]:
        consent = self.get("llm.consent")
        budget = self.get("llm.budgetUsd") if consent else None
        return consent, (float(budget) if budget is not None else None)

    def standards(self) -> str:
        return str(self.get("llm.standards") or "")

    def validate_runtime(self) -> Optional[Dict[str, Any]]:
        depth = self.get("validate.depth")
        return dict(DEPTHS[depth], run=list(DEPTHS[depth]["run"])) if depth in DEPTHS else None

    def sign_in(self) -> Dict[str, Optional[str]]:
        return {"pool": self.get("data.poolSignIn"), "linkedServices": self.get("connections.defaultSignIn")}

    def describe(self) -> Dict[str, Any]:
        return {"version": self.version, "hash": self.hash, "approvedBy": self.approved_by, "approvedAt": self.approved_at,
                "stale": self.stale, "changes": self.changes, "settings": self.settings}


def _within(group: str, chosen: str) -> bool:
    """A group is in a chosen one when it is the same, or a folder under it."""
    if group == chosen:
        return True
    return chosen.startswith("folder:") and group.startswith(chosen + "/")


#: The setting behind each run option, and how the page names it.
SETTING_OF = {**{key: setting for setting, key in RUN_OPTIONS}, "stages": "run.parts", "gitCommit": "release.gitCommit"}
LABELS = {"stages": "Stages", "stopOnFailure": "Stop at a wave with failures", "waveGate": "Sign off each wave",
          "existing": "Objects already in Fabric", "collation": "Warehouse collation", "dataMode": "Tables that already have rows",
          "dataRun": "Load pipelines", "syncOverlap": "Sync re-read window", "commitScope": "What the Fabric Git commit takes",
          "gitCommit": "Commit to Fabric Git"}


def _said(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "none"
    return "nothing" if value in (None, "") else str(value)


def _filter(value: Optional[Mapping[str, Any]]) -> Tuple[Any, ...]:
    """A date filter, comparable whatever its shape: no filter and an empty one are the same."""
    v = value or {}
    return (str(v.get("column") or "").lower(), str(v.get("from") or ""), str(v.get("to") or ""), bool(v.get("sync")),
            str(v.get("changeColumn") or "").lower() if v.get("sync") else "",
            tuple(str(k).lower() for k in v.get("keys") or []) if v.get("sync") else ())
