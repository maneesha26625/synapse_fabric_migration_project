"""MIGRATION_CONTEXT.md: the approved migration brief, written for the model that drafts code.

Every LLM draft carries it, fenced off beside the object's own definition, and every draft records which one it was
made with (its content hash and the brief version), so a reviewer can see what the draft was told and a draft made
under an older brief is flagged. It is made from the policy in force, never from a draft: only decisions a person
approved go to the model.

It holds the decisions that shape code (the settings marked ``ai``), each as a plain instruction, then the coding
standards and the naming convention as they were written, then the context answers. With G4 at *minimal* it keeps
only the decisions: no context answers, and no names of sensitive columns or Microsoft Entra principals.

Deterministic: the same policy gives the same text, byte for byte, so its hash names it. Nothing in it is a secret:
the brief never holds one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

FILE_NAME = "MIGRATION_CONTEXT.md"


@dataclass(frozen=True)
class ContextFile:
    text: str
    hash: str
    brief_version: int
    brief_hash: str
    minimal: bool
    #: The hash of what the file tells the model, without the line naming the brief version it came from: a brief
    #: approved again with the same decisions (or with others that never reach the model) leaves every draft current.
    decisions_hash: str = ""

    def reference(self) -> Dict[str, Any]:
        """What a draft records about the context it was made with."""
        return {"file": FILE_NAME, "hash": self.hash, "decisionsHash": self.decisions_hash, "briefVersion": self.brief_version,
                "briefHash": self.brief_hash, "minimal": self.minimal}


def _code(value: Any) -> str:
    return "`" + str(value).replace("`", "'") + "`"


def _list(values: Any) -> str:
    items = [str(v) for v in values or []]
    return ", ".join(_code(v) for v in items) if items else "none"


def _collation(policy: Any) -> Optional[str]:
    choice = policy.get("warehouse.collation")
    facts = policy.facts
    if choice == "case_sensitive":
        return ("The Warehouse is case-sensitive: 'ABC' and 'abc' differ, in object names and in text. Write every "
                "identifier exactly as it is declared, and compare text knowing case counts.")
    if choice == "case_insensitive" or (choice == "match_synapse" and facts.get("pool.caseInsensitive") is True):
        return ("The Warehouse compares names and text case-insensitively, as the Synapse pool did ('ABC' equals 'abc'). "
                "Code may rely on that, as it did in Synapse.")
    if choice == "match_synapse" and facts.get("pool.caseInsensitive") is False:
        return "The Warehouse is case-sensitive, as the Synapse pool was: write identifiers exactly as they are declared."
    if choice == "match_synapse":
        return "The Warehouse has the Synapse pool's collation."
    return None


def _decisions(policy: Any, minimal: bool) -> Dict[str, List[str]]:
    """The instructions each decision gives, by section. A section with none is left out."""
    get = policy.get
    out: Dict[str, List[str]] = {"Target": [], "What moves": [], "Data": [], "Code": [], "Security": [], "Operations": []}
    warehouse, lakehouse = get("names.warehouse"), get("names.lakehouse")
    if warehouse:
        out["Target"].append(f"The Fabric Warehouse is {_code(warehouse)}. Use it wherever a database name is needed; every "
                             "schema keeps its Synapse name.")
    if lakehouse:
        out["Target"].append(f"The Lakehouse is {_code(lakehouse)}: the OneLake shortcuts are in it, and Spark jobs use it as "
                             "their default lakehouse.")
    collation = _collation(policy)
    if collation:
        out["Target"].append(collation)
    role = get("target.role")
    if role:
        out["Target"].append({"dev": "The target workspace is for development.",
                              "test": "The target workspace is for testing: what a draft does there is checked by testers.",
                              "prod": "The target workspace is production: change nothing that Fabric does not require."}[role])

    parts = get("run.parts")
    if parts is not None:
        out["What moves"].append("This migration moves: " + (", ".join(parts) or "nothing") + ".")
    routes = get("storage.routes")
    if routes == "all":
        out["What moves"].append("Storage the code reads directly is reached through OneLake shortcuts in the Lakehouse: read "
                                 "it by its OneLake path, not the storage account's abfss:// path.")
    elif routes == "none":
        out["What moves"].append("The code keeps reading the data lake directly, as the signed-in user: keep its storage "
                                 "paths as they are.")
    serverless = get("plan.serverless")
    if serverless == "keep":
        out["What moves"].append("Scripts written for the serverless SQL pool are kept: in Fabric they run on the Lakehouse's "
                                 "SQL endpoint, over the shortcuts.")
    elif serverless == "leave_out":
        out["What moves"].append("Scripts written for the serverless SQL pool are not migrated.")

    if get("data.historyMode") == "since" and get("data.historyFrom"):
        out["Data"].append(f"Only rows dated from {get('data.historyFrom')} are loaded, on each table's date column.")
    elif get("data.historyMode") == "all":
        out["Data"].append("Every row of every table is loaded.")
    if get("data.sync") is True:
        line = "After the first load, tables with a change column are kept in sync by a sync pipeline"
        if get("data.watermark") == "named":
            line += f"; the change column is the first of {_list(get('data.watermarkColumns'))} a table has"
        else:
            line += "; each table's change column is the one named like ModifiedAt, UpdatedAt or LastModified"
        out["Data"].append(line + ".")
        if get("data.keylessSync") == "once":
            out["Data"].append("A table with a change column but no key is loaded once and not synced.")
        elif get("data.keylessSync") == "append":
            out["Data"].append("A table with a change column but no key is synced by appending: changed rows appear again.")
        if get("data.deletes") == "keep":
            out["Data"].append("Rows deleted in Synapse after the first load stay in Fabric: a sync never removes rows.")
        elif get("data.deletes") == "reload":
            out["Data"].append("Synced tables are reloaded in full at cutover, which removes rows deleted in Synapse.")
    elif get("data.sync") is False:
        out["Data"].append("Tables are loaded once; nothing is kept in sync.")

    multi = get("code.multiStatement")
    if multi:
        out["Code"].append("Multi-statement table-valued functions become an inline table-valued function or a view, with "
                           "exactly the same result set." + {"llm": "", "manual": " A person rewrites them.",
                                                             "leave_out": " They are not migrated."}[multi])
    lossy = get("types.lossy")
    if lossy:
        out["Code"].append("Columns are stored with Fabric's closest types: datetime2(7) and time(7) keep 6 fractional digits, "
                           "datetimeoffset becomes datetime2 without its offset, rowversion becomes a varbinary(8) that never "
                           "changes, XML becomes text and spatial types binary. Never rely on what they lost."
                           + (" A person reviews those tables before the data loads." if lossy == "review" else ""))
    language = get("code.notebookLanguage")
    if language:
        out["Code"].append(f".NET for Spark code is rewritten in {'PySpark (Python)' if language == 'pyspark' else 'Scala'}.")
    writes = get("validate.externalWrites")
    if writes in ("yes", "unsure"):
        out["Code"].append("The pipelines and notebooks may write to production systems outside Fabric: never add a write, "
                           "and never change where one goes.")

    sensitive = [str(p) for p in get("security.sensitiveColumns") or []]
    if sensitive:
        if minimal:
            out["Security"].append("Some columns hold personal or sensitive data: never log, print or return a column's "
                                   "values in a message, and never copy them anywhere new.")
        else:
            out["Security"].append(f"These columns hold personal or sensitive data: {_list(sensitive)} (schema.table.column, "
                                   "* for any text). Never log, print or return their values in a message, and never copy "
                                   "them anywhere new.")
    if get("security.maskSensitive") == "mask":
        out["Security"].append("Those columns are masked in the Warehouse with the default dynamic data mask: keep every "
                               "mask, and never grant UNMASK.")
    users = get("security.sqlUserMap") or {}
    if users and not minimal:
        shown = "; ".join(f"{_code(user)} becomes {_code(principal)}" for user, principal in sorted(users.items()))
        out["Security"].append(f"SQL users are replaced by Microsoft Entra principals: {shown}. Use the Entra name wherever "
                               "code names the user (GRANT, EXECUTE AS).")

    schedules = get("schedules.enabled")
    if schedules == "off":
        out["Operations"].append("Schedules are created switched off: a person switches each on in Fabric.")
    elif schedules == "match":
        out["Operations"].append("Schedules are switched on as they were in Synapse.")
    return {k: v for k, v in out.items() if v}


#: The context answers (I1 to I4), as the file names them.
_CONTEXT = (("context.timeline", "Timeline"), ("context.painPoints", "What hurts today"),
            ("context.consumers", "What reads the data downstream: keep the names and shapes it depends on"),
            ("context.teamSkills", "What the team knows best: code a draft adds is written that way"))


def render(policy: Any, minimal: Optional[bool] = None) -> ContextFile:
    """The migration context for the policy in force (``consult.policy.Policy``)."""
    minimal = policy.llm_context() == "minimal" if minimal is None else minimal
    lines = [
        "# Migration context",
        "",
        f"Made from the approved migration brief, version {policy.version} (content hash {policy.hash[:16]}), for the model "
        "that drafts code. It is what the client approved for this migration: follow it wherever it applies to the "
        "object. Where it and the project requirements disagree, follow this. Where it and the object's own code disagree, "
        "keep what the code does and say so in `notes`.",
        "",
        "In your answer, list in `context` each instruction below that the draft applies, in a few words.",
    ]
    for section, items in _decisions(policy, minimal).items():
        lines += ["", f"## {section}", ""] + [f"- {item}" for item in items]
    standards = str(policy.get("llm.standards") or "").strip()
    if standards:
        lines += ["", "## Coding standards", "", standards]
    naming = str(policy.get("llm.naming") or "").strip()
    if naming:
        lines += ["", "## Naming anything new", "", "Existing objects keep their Synapse names. Anything new a draft has to "
                  "create is named this way:", "", naming]
    if not minimal:
        told = [(label, str(policy.get(name) or "").strip()) for name, label in _CONTEXT]
        told = [(label, text) for label, text in told if text]
        if told:
            lines += ["", "## About this migration", ""] + [f"- {label}: {text}" for label, text in told]
    text = "\n".join(lines) + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    told = "\n".join(lines[:2] + lines[3:]) + "\n"  # all but the line naming the brief's version and hash
    return ContextFile(text, digest, policy.version, policy.hash, bool(minimal), hashlib.sha256(told.encode("utf-8")).hexdigest())

