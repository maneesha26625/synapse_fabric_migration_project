"""The migration policy: one object, built from the approved brief, that every consumer reads.

Nothing outside this module looks at a question or an answer. Plan reads the run options and the scope rules it
pre-sets; Migrate reads the sign-off rule, the names, the wave gate, the principal map, the commit scope, the
parallel copies, the schedules and the masks; the LLM layer reads consent, budget, scope and what goes with a draft;
the execution plan reads the tasks and checks the brief asks for; Validate reads its depth. A consumer asks for a
setting by name and gets None when the brief does not decide it, and then behaves as it did before there were briefs.

**Freshness.** A policy built from a brief whose facts differ from the estate's now is *stale*: it still says what
was decided, and lists exactly what changed, but a run under a required consultation is refused until a person
reconfirms the brief. So is one approved against an earlier major version of the questions: it never answered the
ones added since. And so is one approved while another Fabric workspace was the target: what it says about the target
(what it is for, E1) is about that workspace. A target only disconnected never stales it.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from discovery_agent.consult.facts import changes, fingerprint

#: Which stages each part of a migration (A0) switches on. External tables (shortcuts) are part of the structure.
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


def major(version: Optional[str]) -> str:
    """A catalog version's major part: what has to change for an approved brief to need reconfirming."""
    return str(version or "").split(".")[0]


class Policy:
    def __init__(self, version: Mapping[str, Any], facts_now: Mapping[str, Any], approval: Mapping[str, Any],
                 catalog_version: Optional[str] = None) -> None:
        content = version["content"]
        self.version = int(version["version"])
        self.hash = str(version["hash"])
        self.settings: Dict[str, Any] = dict(content.get("settings") or {})
        self.facts: Dict[str, Any] = dict(content.get("facts") or {})
        self._kept: List[Mapping[str, Any]] = [k for k in content.get("keptContradictions") or [] if isinstance(k, Mapping)]
        self.fingerprint = str(content.get("factsFingerprint") or "")
        self.catalog_version = str(content.get("catalogVersion") or "")
        self.approved_by = approval.get("by")
        self.approved_at = approval.get("at")
        now = fingerprint(facts_now) if facts_now else self.fingerprint
        #: Approved against an earlier major version of the questions: those added since were never answered.
        self.outdated = catalog_version is not None and major(catalog_version) != major(self.catalog_version)
        #: The Fabric workspace that was the target when a person approved the brief ({workspaceId, workspaceName}),
        #: or None when none was connected then.
        pinned = dict(approval.get("target") or {})
        self.target: Optional[Dict[str, Any]] = pinned or None
        pinned_id, now_id = pinned.get("workspaceId"), (facts_now or {}).get("target.workspaceId")
        #: Another workspace is the target now: the brief's answers about the target are about the one it was approved for.
        self.target_moved = bool(pinned_id and now_id and now_id != pinned_id)
        #: The workspace connected now is the very one the brief was approved for (what lets the agent act on its own).
        self.on_approved_target = bool(pinned_id and now_id == pinned_id)
        moved = bool(facts_now) and now != self.fingerprint
        self.stale = moved or self.outdated or self.target_moved
        self.changes = changes(self.facts, facts_now) if moved else []
        if self.outdated:
            self.changes.append({"fact": "catalog.version", "description": "The consultation's questions (catalog version)",
                                 "before": self.catalog_version or None, "after": catalog_version})
        if self.target_moved:
            self.changes.append({"fact": "target.workspace", "description": "The Fabric target workspace",
                                 "before": pinned.get("workspaceName") or pinned_id,
                                 "after": (facts_now or {}).get("target.workspace") or now_id})

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

    def change_column_of(self, dates: List[str]) -> Optional[str]:
        """The column a sync follows on a table with these date columns: the first one the brief names (C2N), matched
        ignoring case, or the one detected (C2); None when the table has neither."""
        if self.get("data.watermark") == "named":
            named = [str(n).lower() for n in self.get("data.watermarkColumns") or []]
            return next((d for n in named for d in dates if d.lower() == n), None)
        return change_column(dates)

    def data_filters(self, tables: List[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Each table's date filter, as the brief's history and sync decisions make it."""
        since = self.get("data.historyFrom") if self.get("data.historyMode") == "since" else None
        sync = bool(self.get("data.sync"))
        out: Dict[str, Dict[str, Any]] = {}
        for t in tables:
            dates = [str(c.get("name")) for c in t.get("dateColumns") or []]
            keys = list(t.get("keyColumns") or [])
            entry: Optional[Dict[str, Any]] = None
            if since and dates:
                entry = {"column": history_column(dates), "from": since, "to": "", "sync": False, "changeColumn": "", "keys": []}
            change = self.change_column_of(dates) if sync else None
            if change and not keys and self.get("data.keylessSync") == "once":
                change = None  # without a key a sync could only append: the brief loads these once (C3)
            if change:
                entry = entry or {"column": "", "from": "", "to": "", "sync": False, "changeColumn": "", "keys": []}
                entry.update(sync=True, changeColumn=change, keys=keys)
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

    def excluded(self, item: Mapping[str, Any], group_of: Mapping[str, List[str]], flags: Mapping[str, List[str]]) -> Optional[str]:
        """Why the brief leaves this object out of the plan (Plan's Add all), or None when it is in scope.

        ``group_of``: object id -> its scope groups (``schema:x``, ``folder:a/b``); ``flags``: object id -> what the
        estate says about it (``serverless``, ``multiStatement``, ``rebuild``)."""
        oid, name = str(item.get("id")), str(item.get("name") or "")
        marks = flags.get(oid) or []
        if "serverless" in marks and self.get("plan.serverless") == "leave_out":
            return "Written for the serverless SQL pool: the brief leaves it out (A3S)."
        if "multiStatement" in marks and self.get("code.multiStatement") == "leave_out":
            return "A multi-statement table-valued function: the brief leaves these out (D3)."
        if "rebuild" in marks and self.get("code.rebuild") == "leave_out":
            return "No Fabric equivalent as it is: the brief leaves these out (A4)."
        if "sqlModule" in marks and self.get("plan.sqlModules") == "leave_out":
            return "A view, procedure or function: the brief moves the tables first and leaves these for a later run (A3)."
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

    def target_role(self) -> Optional[str]:
        """What the target workspace is for (E1): dev, test or prod."""
        return self.get("target.role")

    def parallel_copies(self) -> Optional[int]:
        value = self.get("data.parallelCopies")
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def schedules(self) -> Optional[str]:
        """off | match: schedules created switched off, or as they are in Synapse (E5)."""
        return self.get("schedules.enabled")

    def mask_columns(self) -> List[str]:
        """The column patterns a run masks (G1 and G1M), as schema.table.column; empty unless the brief masks them."""
        if self.get("security.maskSensitive") != "mask":
            return []
        return [str(p) for p in self.get("security.sensitiveColumns") or []]

    def autonomy(self) -> str:
        """What the assistant may do alone (E6): observe, suggest (the default) or act_dev."""
        return str(self.get("assistant.autonomy") or "suggest")

    def llm_scope(self) -> Optional[str]:
        return self.get("llm.scope")

    def llm_context(self) -> str:
        """What goes to the model with a draft besides its definition (G4): context (the default) or minimal."""
        return str(self.get("llm.context") or "context")

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

    def external_writes_refusal(self, runs: List[str]) -> Optional[str]:
        """Why Validate may not really run these kinds of object (pipelines, notebooks, scripts, Spark jobs) under this
        brief, or None: refused while the brief says they may write to production systems, or nobody knows (H4), unless
        a person kept running them anyway when the brief was approved (K10)."""
        writes = self.get("validate.externalWrites")
        if not runs or writes not in ("yes", "unsure") or "K10" in self.kept():
            return None
        said = "write to production systems outside Fabric" if writes == "yes" else "may write to production systems outside Fabric (nobody is sure)"
        return (f"The approved brief says the pipelines and notebooks {said} (H4), so Validate does not run them. Query "
                "and compile instead; or, once they write only to test copies, answer H4 so, or keep running them (K10), and "
                "have the brief approved again.")

    def kept(self) -> List[str]:
        """The contradictions a person kept, with a reason, when the brief was submitted (K1, K10...)."""
        return [str(k.get("conflict")) for k in self._kept]

    def describe(self) -> Dict[str, Any]:
        return {"version": self.version, "hash": self.hash, "approvedBy": self.approved_by, "approvedAt": self.approved_at,
                "stale": self.stale, "outdated": self.outdated, "catalogVersion": self.catalog_version,
                "changes": self.changes, "settings": self.settings, "target": self.target, "targetMoved": self.target_moved}


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
