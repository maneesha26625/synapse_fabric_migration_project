"""The policy settings a brief can set: the one vocabulary between the catalog and the code.

A question ``drives`` one or more of these. The code that acts on a decision reads the setting by its name, so a
question can be reworded, split or replaced in the catalog without touching any code. Each setting says how far
the accelerator acts on it:

* ``enforced``: a consumer changes what it does (Plan pre-sets it, Migrate applies it, an LLM call is refused);
* ``recorded``: kept in the brief and shown, for a feature that does not act on it yet (the assistant's autonomy);
* ``note``: information for people (owners, cutover window), never acted on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

ENFORCED, RECORDED, NOTE = "enforced", "recorded", "note"
STATUSES = (ENFORCED, RECORDED, NOTE)

#: A setting's kinds, and the shape of its value.
CHOICE, LIST, BOOL, NUMBER, DATE, TEXT, MAP = "choice", "list", "bool", "number", "date", "text", "map"
KINDS = (CHOICE, LIST, BOOL, NUMBER, DATE, TEXT, MAP)


@dataclass(frozen=True)
class Setting:
    name: str
    kind: str
    status: str
    #: Who acts on it, in words, for the brief.
    applies: str
    #: The values a choice or a list may hold; empty for any value of its kind.
    values: Tuple[str, ...] = ()


def _s(name: str, kind: str, status: str, applies: str, values: Tuple[str, ...] = ()) -> Setting:
    return Setting(name, kind, status, applies, values)


_ALL = (
    _s("run.parts", LIST, ENFORCED, "Migrate: which stages are on", ("structure", "data", "orchestration")),
    _s("plan.scopeMode", CHOICE, ENFORCED, "Plan: which objects Add all puts in the plan", ("all", "include", "exclude")),
    _s("plan.scopeInclude", LIST, ENFORCED, "Plan: the schemas and folders in scope"),
    _s("plan.scopeExclude", LIST, ENFORCED, "Plan: name patterns left out of the plan"),
    _s("plan.serverless", CHOICE, ENFORCED, "Plan: scripts and pipelines written for the serverless pool", ("leave_out", "keep")),
    _s("run.stopOnFailure", BOOL, ENFORCED, "Migrate: stop at the end of a wave that has failures"),
    _s("run.waveGate", BOOL, ENFORCED, "Migrate: a person signs off each wave before the next one starts"),
    _s("run.existing", CHOICE, ENFORCED, "Migrate: objects already in Fabric", ("skip", "update")),
    _s("warehouse.collation", CHOICE, ENFORCED, "Migrate: the Warehouse collation, set when it is created",
       ("match_synapse", "case_insensitive", "case_sensitive")),
    _s("names.warehouse", TEXT, ENFORCED, "Migrate and Validate: the Warehouse's name"),
    _s("names.lakehouse", TEXT, ENFORCED, "Migrate: the Lakehouse's name (shortcuts, storage routes, Spark jobs)"),
    _s("llm.standards", TEXT, ENFORCED, "LLM drafts: rules every draft follows, added to the project requirements"),
    _s("data.historyMode", CHOICE, ENFORCED, "Plan: how much of each table's history loads", ("all", "since")),
    _s("data.historyFrom", DATE, ENFORCED, "Plan: the first date loaded, on each table's date column"),
    _s("data.sync", BOOL, ENFORCED, "Plan: tables with a change column are kept in sync after the first load"),
    _s("data.syncOverlap", CHOICE, ENFORCED, "Migrate: the re-read window of each sync", ("1h", "1d", "none")),
    _s("data.mode", CHOICE, ENFORCED, "Migrate: tables that already have rows", ("if_empty", "replace")),
    _s("data.run", CHOICE, ENFORCED, "Migrate: run the load pipelines, or only create them", ("run", "create")),
    _s("data.poolSignIn", CHOICE, ENFORCED, "Plan: how the load connection signs in to the pool (the secret is never kept)",
       ("workspaceIdentity", "basicKeyVault", "servicePrincipalKeyVault", "basic", "servicePrincipal")),
    _s("llm.consent", BOOL, ENFORCED, "LLM drafts, consultation help and the assistant: whether anything may be sent to Claude"),
    _s("llm.budgetUsd", NUMBER, ENFORCED, "LLM calls: the most this project may spend"),
    _s("code.multiStatement", CHOICE, ENFORCED, "Plan: multi-statement table-valued functions", ("llm", "manual", "leave_out")),
    _s("code.rebuild", CHOICE, ENFORCED, "Plan: pipelines with activities Fabric lacks, and .NET notebooks", ("by_hand", "leave_out")),
    _s("owners.rebuild", TEXT, NOTE, "People: who rebuilds what has no Fabric equivalent"),
    _s("security.migrate", BOOL, ENFORCED, "Migrate: whether the Roles & permissions stage is on"),
    _s("security.sqlUserMap", MAP, ENFORCED, "Migrate: the Microsoft Entra principal that replaces each SQL user"),
    _s("connections.defaultSignIn", CHOICE, ENFORCED, "Plan: how each linked service's connection signs in, preselected",
       ("workspaceIdentity", "keyVault")),
    _s("storage.routes", CHOICE, ENFORCED, "Plan: storage the code reads directly, routed through OneLake shortcuts", ("all", "none")),
    _s("release.gitCommit", BOOL, ENFORCED, "Migrate: commit each run to the target workspace's Fabric Git branch"),
    _s("release.commitScope", CHOICE, ENFORCED, "Migrate: what that commit takes", ("run", "all")),
    _s("release.signoff", CHOICE, ENFORCED, "Migrate: which runs a person signs off before they write", ("never", "overwrite", "always")),
    _s("owners.setup", TEXT, NOTE, "People: who sets up schedules, integration runtimes and event triggers"),
    _s("release.path", CHOICE, NOTE, "People: the path to Test and Prod", ("dev", "dev_test", "dev_test_prod")),
    _s("validate.depth", CHOICE, ENFORCED, "Validate: how deep it checks by default", ("compare", "probe", "run")),
    _s("release.cutover", TEXT, NOTE, "People: the cutover window and what a rollback means"),
    _s("assistant.autonomy", CHOICE, RECORDED, "Assistant: what it may do alone (the assistant is not built yet)",
       ("observe", "suggest", "act_dev")),
    _s("context.timeline", TEXT, NOTE, "People: the timeline"),
    _s("context.approver", TEXT, NOTE, "People: who approves the migration"),
    _s("context.consumers", TEXT, NOTE, "People: what reads the data downstream (Power BI, applications)"),
    _s("context.painPoints", TEXT, NOTE, "People: what hurts today"),
)

SETTINGS: Dict[str, Setting] = {s.name: s for s in _ALL}


def setting(name: str) -> Setting:
    return SETTINGS[name]
