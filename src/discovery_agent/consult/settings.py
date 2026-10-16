"""The policy settings a brief can set: the one vocabulary between the catalog and the code.

A question ``drives`` one or more of these. The code that acts on a decision reads the setting by its name, so a
question can be reworded, split or replaced in the catalog without touching any code. Each setting says how far
the accelerator acts on it:

* ``enforced``: a consumer changes what it does (Plan pre-sets it, Migrate applies it, an LLM call is refused, the
  execution plan adds a task or a check);
* ``context``: sent to the AI with every draft, in the migration context (``MIGRATION_CONTEXT.md``), for the model
  to follow; a person reviews each draft before it is used, so it is followed, not enforced;
* ``recorded``: kept in the brief and shown, for a feature that does not act on it yet;
* ``note``: information for people (owners, the cutover window), never acted on.

``ai`` marks the settings the migration context carries, whatever their status: a Warehouse's collation is enforced
when it is created, and a draft is told it too, so it writes code that compares text the way the Warehouse will.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

ENFORCED, CONTEXT, RECORDED, NOTE = "enforced", "context", "recorded", "note"
STATUSES = (ENFORCED, CONTEXT, RECORDED, NOTE)

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
    #: Carried in the migration context sent with every draft (``consult.context_file``).
    ai: bool = False


def _s(name: str, kind: str, status: str, applies: str, values: Tuple[str, ...] = (), ai: bool = False) -> Setting:
    return Setting(name, kind, status, applies, values, ai)


_ALL = (
    # A. Scope and approach
    _s("run.parts", LIST, ENFORCED, "Migrate: which stages are on", ("structure", "data", "orchestration"), ai=True),
    _s("storage.routes", CHOICE, ENFORCED, "Plan: storage the code reads directly, routed through OneLake shortcuts",
       ("all", "none"), ai=True),
    _s("plan.scopeMode", CHOICE, ENFORCED, "Plan: which objects Add all puts in the plan", ("all", "include", "exclude")),
    _s("plan.scopeInclude", LIST, ENFORCED, "Plan: the schemas and folders in scope"),
    _s("plan.scopeExclude", LIST, ENFORCED, "Plan: name patterns left out of the plan"),
    _s("plan.sqlModules", CHOICE, ENFORCED, "Plan: whether Add all puts views, stored procedures and functions in the plan",
       ("convert", "leave_out")),
    _s("plan.serverless", CHOICE, ENFORCED, "Plan: scripts and pipelines written for the serverless pool", ("leave_out", "keep"), ai=True),
    _s("code.rebuild", CHOICE, ENFORCED, "Plan: Add all leaves them out; the execution plan gives a person a task to rebuild "
                                         "each one kept", ("by_hand", "leave_out")),
    _s("owners.rebuild", TEXT, NOTE, "People: who rebuilds what has no Fabric equivalent"),
    _s("data.historyMode", CHOICE, ENFORCED, "Plan: how much of each table's history loads", ("all", "since"), ai=True),
    _s("data.historyFrom", DATE, ENFORCED, "Plan: the first date loaded, on each table's date column", ai=True),
    _s("run.stopOnFailure", BOOL, ENFORCED, "Migrate: stop at the end of a wave that has failures"),
    _s("run.waveGate", BOOL, ENFORCED, "Migrate: a person signs off each wave before the next one starts"),
    _s("run.existing", CHOICE, ENFORCED, "Migrate: objects already in Fabric", ("skip", "update")),
    # B. Target and standards
    _s("warehouse.collation", CHOICE, ENFORCED, "Migrate: the Warehouse collation, set when it is created",
       ("match_synapse", "case_insensitive", "case_sensitive"), ai=True),
    _s("names.warehouse", TEXT, ENFORCED, "Migrate and Validate: the Warehouse's name", ai=True),
    _s("names.lakehouse", TEXT, ENFORCED, "Migrate: the Lakehouse's name (shortcuts, storage routes, Spark jobs)", ai=True),
    _s("llm.standards", TEXT, CONTEXT, "LLM drafts: the coding standards every draft follows", ai=True),
    _s("llm.naming", TEXT, CONTEXT, "LLM drafts: how anything new a draft creates is named", ai=True),
    # C. Data loading
    _s("data.sync", BOOL, ENFORCED, "Plan: tables with a change column are kept in sync after the first load", ai=True),
    _s("data.watermark", CHOICE, ENFORCED, "Plan: how each table's change column is found", ("detect", "named"), ai=True),
    _s("data.watermarkColumns", LIST, ENFORCED, "Plan: the change columns, by name, first found first", ai=True),
    _s("data.keylessSync", CHOICE, ENFORCED, "Plan: tables with a change column but no key", ("once", "append"), ai=True),
    _s("data.deletes", CHOICE, ENFORCED, "Execution plan: a cutover task to reload the synced tables in full",
       ("keep", "reload"), ai=True),
    _s("data.syncOverlap", CHOICE, ENFORCED, "Migrate: the re-read window of each sync", ("1h", "1d", "none")),
    _s("data.mode", CHOICE, ENFORCED, "Migrate: tables that already have rows", ("if_empty", "replace")),
    _s("data.run", CHOICE, ENFORCED, "Migrate: run the load pipelines, or only create them", ("run", "create")),
    _s("data.parallelCopies", NUMBER, ENFORCED, "Migrate: how many tables the load pipeline copies at the same time"),
    _s("data.poolSignIn", CHOICE, ENFORCED, "Plan: how the load connection signs in to the pool, preselected (the secret is never kept)",
       ("workspaceIdentity", "basicKeyVault", "servicePrincipalKeyVault", "basic", "servicePrincipal")),
    # D. Code conversion and AI
    _s("llm.consent", BOOL, ENFORCED, "LLM drafts, consultation help and the assistant: whether anything may be sent to Claude"),
    _s("llm.budgetUsd", NUMBER, ENFORCED, "LLM calls: once this much is spent, the next call is refused"),
    _s("code.multiStatement", CHOICE, ENFORCED, "Plan: Add all leaves them out; the execution plan asks to draft each with AI, "
                                                "or gives a person a task to rewrite it", ("llm", "manual", "leave_out"), ai=True),
    _s("types.lossy", CHOICE, ENFORCED, "Execution plan: a task to review those tables before the data loads",
       ("accept", "review"), ai=True),
    _s("code.unconvertible", CHOICE, ENFORCED, "Execution plan: asks to draft each with AI, or gives a person a task to rewrite it",
       ("ai", "by_hand")),
    _s("llm.scope", CHOICE, ENFORCED, "LLM drafts: which objects may be sent", ("needs_work", "any")),
    _s("code.notebookLanguage", CHOICE, CONTEXT, "LLM drafts: the language a .NET notebook is rewritten in", ("pyspark", "scala"), ai=True),
    # E. Environments and operations
    _s("release.gitCommit", BOOL, ENFORCED, "Migrate: commit each run to the target workspace's Fabric Git branch"),
    _s("release.commitScope", CHOICE, ENFORCED, "Migrate: what that commit takes", ("run", "all")),
    _s("target.role", CHOICE, ENFORCED, "Migrate and the assistant: a production workspace signs off every run, and the "
                                        "assistant never acts on its own there", ("dev", "test", "prod"), ai=True),
    _s("operations.identity", CHOICE, ENFORCED, "Execution plan: checks that the Fabric sign-in is this kind",
       ("person", "service_principal")),
    _s("release.signoff", CHOICE, ENFORCED, "Migrate: which runs a person signs off before they write", ("never", "overwrite", "always")),
    _s("schedules.enabled", CHOICE, ENFORCED, "Migrate: schedules created switched off, or as they are in Synapse",
       ("off", "match"), ai=True),
    _s("owners.setup", TEXT, NOTE, "People: who sets up integration runtimes and event triggers, and switches schedules on"),
    _s("assistant.autonomy", CHOICE, ENFORCED, "Assistant: what it may do without asking", ("observe", "suggest", "act_dev")),
    _s("release.path", CHOICE, NOTE, "People: the path to Test and Prod", ("dev", "dev_test", "dev_test_prod")),
    # G. Security and governance
    _s("security.sensitiveColumns", LIST, ENFORCED, "LLM drafts keep them out of logs and copies; Migrate masks them when "
                                                    "G1M says so", ai=True),
    _s("security.maskSensitive", CHOICE, ENFORCED, "Migrate: the default dynamic data mask on each sensitive column without one",
       ("keep", "mask"), ai=True),
    _s("security.migrate", BOOL, ENFORCED, "Migrate: whether the Roles & permissions stage is on"),
    _s("security.sqlUserMap", MAP, ENFORCED, "Migrate: the Microsoft Entra principal that replaces each SQL user", ai=True),
    _s("governance.residency", TEXT, ENFORCED, "Execution plan: a person checks the target capacity's region, and the agent "
                                               "starts no run until they have"),
    _s("llm.context", CHOICE, ENFORCED, "LLM drafts: what goes with each draft besides its definition", ("context", "minimal")),
    _s("connections.defaultSignIn", CHOICE, ENFORCED, "Plan: how each linked service's connection signs in, preselected",
       ("workspaceIdentity", "keyVault")),
    # H. Validation and cutover
    _s("validate.depth", CHOICE, ENFORCED, "Validate: how deep it checks by default", ("compare", "probe", "run")),
    _s("cutover.parallelRun", BOOL, ENFORCED, "Execution plan: a task for each side-by-side cycle before cutover"),
    _s("cutover.parallelCycles", NUMBER, ENFORCED, "Execution plan: how many side-by-side cycles"),
    _s("release.cutover", TEXT, NOTE, "People: the cutover window and what a rollback means"),
    _s("validate.externalWrites", CHOICE, ENFORCED, "Validate: runs pipelines and notebooks only once a person says it is "
                                                    "safe (K10)", ("no", "yes", "unsure"), ai=True),
    # I. Context
    _s("context.timeline", TEXT, CONTEXT, "LLM drafts and people: the timeline", ai=True),
    _s("context.painPoints", TEXT, CONTEXT, "LLM drafts and people: what hurts today", ai=True),
    _s("context.consumers", TEXT, CONTEXT, "LLM drafts and people: what reads the data downstream", ai=True),
    _s("context.teamSkills", TEXT, CONTEXT, "LLM drafts and people: what the team knows best", ai=True),
    _s("context.approver", TEXT, NOTE, "People: who approves the migration"),
)

SETTINGS: Dict[str, Setting] = {s.name: s for s in _ALL}


def setting(name: str) -> Setting:
    return SETTINGS[name]


def describe() -> Dict[str, Dict[str, object]]:
    """The vocabulary, as the Demo page's copy of the engine reads it (``frontend/src/mock/consultVocabulary.json``)."""
    return {s.name: {"kind": s.kind, "status": s.status, "applies": s.applies, "values": list(s.values), "ai": s.ai} for s in _ALL}
