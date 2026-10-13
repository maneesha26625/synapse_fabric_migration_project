"""Names shared by the runner and the stage handlers.

Kept apart from ``runner`` so a stage module can import them without importing
the runner that imports the stage (which would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# -- kinds: what the runner does with a plan item --------------------------------
NOTEBOOK, TABLE, VIEW, PROCEDURE, DEFERRED, MISSING = "notebook", "table", "view", "procedure", "deferred", "missing"
ENVIRONMENT = "environment"
POOL, SCHEMA = "pool", "schema"
DATA = "data"               # the rows of one table
CONNECTION = "connection"   # a linked service -> a Fabric connection
PIPELINE = "pipeline"
DATASET = "dataset"         # folded into the pipelines that use it
SPARKJOB = "sparkjob"
SCRIPT = "script"           # a SQL script -> a notebook
SCHEDULE = "schedule"       # a trigger -> a pipeline schedule
SHORTCUT = "shortcut"       # an external table -> a OneLake shortcut
FUNCTION = "function"       # a user-defined T-SQL function
SECURITY = "security"       # a database role or user -> Warehouse roles, members and permissions

# -- statuses an item can have ----------------------------------------------------
PENDING, IN_PROGRESS, COMPLETED, FAILED, SKIPPED, DEFERRED_STATUS = (
    "PENDING", "IN PROGRESS", "COMPLETED", "FAILED", "SKIPPED", "DEFERRED")

#: Fabric errors about the workspace itself, not one object: after the first,
#: the rest of the run fails fast with the same message instead of repeating it.
WORKSPACE_ERRORS = {"FeatureNotAvailable", "WorkspaceNotFound", "CapacityNotActive", "CapacityLimitExceeded"}


class MigrationError(Exception):
    """One object could not be migrated; the message says why, in plain words."""


@dataclass
class Source:
    """What the run needs to know about one plan item."""

    id: str
    name: str
    type: str
    wave: int
    kind: str
    payload: Any = None  # a Synapse resource, or a SqlTable / SqlView / SqlProcedure
    schema: Optional[str] = None
    object_name: Optional[str] = None
    reason: Optional[str] = None
    #: Linked services only: the parameter values each pipeline activity or dataset that uses it passes.
    uses: Tuple[Dict[str, Any], ...] = ()
    #: Table data only: the ``datafilter.DataFilter`` that limits which rows load, or None for every row.
    data_filter: Any = None
    #: An accepted LLM-assisted transform for this object, used instead of the rule-based conversion:
    #: ``{"code", "acceptedAt", "edited"}``. Set only while the source still matches what was accepted.
    override: Optional[Dict[str, Any]] = None
    #: Tables only: ``sql.security.SqlMaskedColumn`` entries, recreated as dynamic data masks with the table.
    masks: Tuple[Any, ...] = ()


def override_note(source: Source) -> str:
    """The run-log note that says an object was created from reviewed, LLM-assisted code."""
    edited = " and edited" if (source.override or {}).get("edited") else ""
    return f"Created from the LLM-assisted transform accepted{edited} on {(source.override or {}).get('acceptedAt') or 'an earlier date'}."
