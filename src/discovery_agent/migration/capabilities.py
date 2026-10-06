"""The migration stages this build offers: one registry, read by the planner, the
run and the UI, so they can never disagree about what exists.

A **stage** is a capability the operator switches on or off. Each owns some
object types, may need earlier stages, may have options (a strategy), and may
need input the tool cannot discover (credentials).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from discovery_agent.migration.common import (
    CONNECTION, DATA, DATASET, ENVIRONMENT, NOTEBOOK, PIPELINE, POOL, PROCEDURE, SCHEDULE, SCHEMA,
    SCRIPT, SHORTCUT, SPARKJOB, TABLE, VIEW,
)


@dataclass(frozen=True)
class Option:
    """One strategy choice a stage offers."""

    key: str
    label: str
    choices: Tuple[Tuple[str, str, str], ...]  # (value, short label, description)
    default: str


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    summary: str
    #: Plan object types this stage migrates (the stage's inputs).
    types: Tuple[str, ...]
    #: Runner kinds it produces.
    kinds: Tuple[str, ...]
    #: Stages that must run first (in this run or an earlier one).
    needs: Tuple[str, ...] = ()
    options: Tuple[Option, ...] = ()
    #: Input the tool cannot discover, asked for in the UI and never stored.
    needs_input: Optional[str] = None
    #: What this stage writes in Fabric, for the card.
    creates: str = ""


STAGES: Tuple[Stage, ...] = (
    Stage("warehouse", "Warehouse & schema", "The SQL pool becomes a Fabric Warehouse; its schemas, tables (empty, with their keys), views and stored procedures are created in it. "
          "Synapse-only T-SQL in views and procedures is converted first.",
          ("Dedicated SQL Pool", "Schema", "Table", "View", "Stored Procedure"), (POOL, SCHEMA, TABLE, VIEW, PROCEDURE), creates="Warehouse, schemas, tables, views, procedures",
          options=(
              Option("collation", "Warehouse collation (set once, when it is created)", (
                  ("match_synapse", "Same as Synapse", "Case-sensitive only if the Synapse pool was; Synapse's default is case-insensitive."),
                  ("case_insensitive", "Case-insensitive", "'ABC' equals 'abc' in comparisons and object names, as in most Synapse pools."),
                  ("case_sensitive", "Case-sensitive", "Fabric's own default: 'ABC' and 'abc' are different."),
              ), "match_synapse"),
          )),
    Stage("data", "Table data", "Loads the rows of each migrated table with Fabric data pipelines: one pipeline per wave, reading the Synapse pool "
          "through a Fabric connection and writing the Warehouse. The run then compares row counts.",
          ("Table",), (DATA,), needs=("warehouse",), needs_input="credentials", creates="Data pipelines; rows in the Warehouse tables",
          options=(
              Option("dataRun", "What the stage does", (
                  ("run", "Create and run", "Creates each wave's pipeline, runs it, waits for it, and compares row counts."),
                  ("create", "Create only", "Creates the pipelines; you run them from Fabric (or a schedule) when you choose."),
              ), "run"),
              Option("dataMode", "If a table already has rows", (
                  ("if_empty", "Skip it (safe)", "Never touches data that is already there, so re-runs are safe."),
                  ("replace", "Replace it", "The pipeline empties the table (TRUNCATE) before loading it again."),
              ), "if_empty"),
          )),
    Stage("spark", "Spark pool & environment", "A Spark pool becomes a custom Fabric Spark pool plus a published Environment.",
          ("Spark Pool",), (ENVIRONMENT,), creates="Spark pool, Environment"),
    Stage("notebooks", "Notebooks", "Synapse notebooks become Fabric notebooks.",
          ("Notebook",), (NOTEBOOK,), creates="Notebooks"),
    Stage("connections", "Connections", "Linked services become Fabric connections. Fabric needs their credentials, which Synapse does not give up.",
          ("Linked Service",), (CONNECTION,), needs_input="credentials", creates="Fabric connections"),
    Stage("pipelines", "Pipelines & datasets", "Pipelines become Fabric data pipelines. Datasets are folded into the pipelines that use them.",
          ("Pipeline", "Dataset"), (PIPELINE, DATASET), needs=("connections", "notebooks", "warehouse"), creates="Data pipelines"),
    Stage("spark_jobs", "Spark job definitions", "Spark job definitions are recreated as Fabric Spark job definitions.",
          ("Spark Job Definition",), (SPARKJOB,), creates="Spark job definitions"),
    Stage("sql_scripts", "SQL scripts", "Each SQL script becomes a notebook with a T-SQL cell, bound to the Warehouse. Review them before running.",
          ("SQL Script",), (SCRIPT,), needs=("warehouse",), creates="Notebooks (T-SQL)"),
    Stage("schedules", "Schedules", "Triggers become pipeline schedules, created switched off so nothing runs before you are ready.",
          ("Trigger",), (SCHEDULE,), needs=("pipelines",), creates="Pipeline schedules"),
    Stage("shortcuts", "External tables", "External tables become OneLake shortcuts in a Lakehouse, pointing at the same storage.",
          ("External Table",), (SHORTCUT,), needs=("connections",), creates="Lakehouse, shortcuts"),
)

STAGE_KEYS: Tuple[str, ...] = tuple(s.key for s in STAGES)
_BY_KEY: Dict[str, Stage] = {s.key: s for s in STAGES}
KIND_STAGE: Dict[str, str] = {k: s.key for s in STAGES for k in s.kinds}
TYPE_KIND: Dict[str, str] = {}  # filled by the runner: object type -> kind
#: Object types no stage handles at all (always manual).
UNHANDLED = ("Integration Runtime",)


def stage(key: str) -> Stage:
    return _BY_KEY[key]


def stage_of_kind(kind: str) -> Optional[str]:
    return KIND_STAGE.get(kind)


def default_settings() -> Dict[str, str]:
    return {o.key: o.default for s in STAGES for o in s.options}


def describe() -> List[dict]:
    """The registry as the UI reads it."""
    return [
        {
            "key": s.key, "label": s.label, "summary": s.summary, "types": list(s.types), "needs": list(s.needs),
            "needsInput": s.needs_input, "creates": s.creates,
            "options": [
                {"key": o.key, "label": o.label, "default": o.default,
                 "choices": [{"value": v, "label": label, "description": d} for v, label, d in o.choices]}
                for o in s.options
            ],
        }
        for s in STAGES
    ]
