"""SQL analysis types, shared by every extractor that reads SQL text.

These describe *SQL*, not any one artifact that happens to carry it. A
``TRUNCATE TABLE`` in a standalone SQL script and the same statement in a
Copy activity's ``preCopyScript`` are the same observation and must not be
modelled twice.

What stays out of this module: anything that describes the *container*. A SQL
script's pool binding lives with the SQL script model, because a pipeline
activity has no such thing.

Three honesty constraints are built into the types:

* ``SqlObjectKind.UNKNOWN`` is the normal answer for a ``FROM`` target. Only a
  DDL keyword can distinguish a table from a view, so the model has a value
  that means "referenced, kind not determinable" rather than defaulting to
  TABLE.
* ``SqlObjectKind.INTERPOLATED`` says the written name contains a Synapse
  expression, so the real object is chosen at runtime. The dependency is
  still recorded — dropping it would hide it — but it is never guessed at.
* ``DynamicSqlSite`` records that SQL is built at runtime without pretending
  to know what it builds.

``SqlFeature`` deliberately has no severity. Whether ``DISTRIBUTION = HASH``
is easy or hard to migrate is Assessment's question.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SqlObjectKind(str, Enum):
    """What kind of database object a reference points at.

    UNKNOWN is the honest default: ``FROM dbo.Sales`` could be a table, a
    view or a synonym, and no amount of static analysis settles it.
    """

    TABLE = "table"
    VIEW = "view"
    PROCEDURE = "procedure"
    FUNCTION = "function"
    EXTERNAL_TABLE = "external_table"
    TEMPORARY = "temporary"  # #temp or @table variable; not a durable object
    INTERPOLATED = "interpolated"  # name built from an expression at runtime
    UNKNOWN = "unknown"


class SqlOperation(str, Enum):
    """What the statement does to the object."""

    READ = "read"
    WRITE = "write"
    CREATE = "create"
    ALTER = "alter"
    DROP = "drop"
    TRUNCATE = "truncate"
    MERGE = "merge"
    EXECUTE = "execute"
    UNKNOWN = "unknown"


class SqlFeatureKind(str, Enum):
    """Migration-relevant SQL constructs, by category."""

    DISTRIBUTION = "distribution"  # HASH / ROUND_ROBIN / REPLICATE
    INDEX = "index"  # clustered columnstore, clustered, heap
    PARTITIONING = "partitioning"
    CTAS = "ctas"  # CREATE TABLE AS SELECT
    EXTERNAL_TABLE = "external_table"
    EXTERNAL_DATA_SOURCE = "external_data_source"
    EXTERNAL_FILE_FORMAT = "external_file_format"
    OPENROWSET = "openrowset"
    COPY_INTO = "copy_into"
    STATISTICS = "statistics"
    LABEL = "label"
    TRANSACTION = "transaction"
    PERMISSION = "permission"
    CREDENTIAL = "credential"
    CROSS_DATABASE = "cross_database"


@dataclass(frozen=True)
class SqlObjectReference:
    """A database object some SQL text names.

    Not an ``ArtifactReference``: a SQL object is not a repository artifact,
    and this model carries the schema and database qualifiers that a future
    SQL-source record will be identified by.
    """

    name: str  # exactly as written, e.g. "[dbo].[TripsData]"
    object_name: str  # unquoted, e.g. "TripsData"
    kind: SqlObjectKind
    operation: SqlOperation
    statement: str  # the keyword that produced it, e.g. "CREATE TABLE"
    location: str  # "properties.content.query:L12"
    schema: Optional[str] = None
    database: Optional[str] = None

    @property
    def qualified_name(self) -> str:
        parts = [p for p in (self.database, self.schema, self.object_name) if p]
        return ".".join(parts)

    @property
    def is_durable(self) -> bool:
        """Whether this names a persistent object rather than a temp one.

        An interpolated name is durable: the object it resolves to outlives
        the batch, even though which object that is cannot be read here.
        """
        return self.kind is not SqlObjectKind.TEMPORARY

    @property
    def is_interpolated(self) -> bool:
        """Whether the name is completed by an expression at runtime."""
        return self.kind is SqlObjectKind.INTERPOLATED

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "object_name": self.object_name,
            "qualified_name": self.qualified_name,
            "schema": self.schema,
            "database": self.database,
            "kind": self.kind.value,
            "operation": self.operation.value,
            "statement": self.statement,
            "location": self.location,
        }


@dataclass(frozen=True)
class SqlFeature:
    """One migration-relevant construct observed in the SQL.

    No severity, by design — presence is a fact, difficulty is a judgement.
    """

    kind: SqlFeatureKind
    construct: str
    location: str
    evidence: str  # the source line, redacted for CREDENTIAL

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "construct": self.construct,
            "location": self.location,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class DynamicSqlSite:
    """A place where SQL is assembled at runtime.

    Its dependencies are not statically knowable. Recording the site is the
    honest outcome; guessing the targets would not be.
    """

    construct: str  # "sp_executesql", "EXEC(...)", "EXEC @variable"
    location: str
    evidence: str

    def to_dict(self) -> dict:
        return {
            "construct": self.construct,
            "location": self.location,
            "evidence": self.evidence,
        }
