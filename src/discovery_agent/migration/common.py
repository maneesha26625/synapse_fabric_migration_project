"""Names shared by the runner and the stage handlers.

Kept apart from ``runner`` so a stage module can import them without importing
the runner that imports the stage (which would be a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

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
