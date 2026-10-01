"""The catalog queries discovery is allowed to run. All of them, and only these.

Callers name a query; they cannot compose one. That is the whole point of the
module: a ``CatalogSource`` accepts a ``CatalogQuery`` drawn from this
registry and rejects anything else, so "discovery is read-only" is a property
of the type system rather than a rule someone has to remember.

Each query is validated at import time — SELECT-only, single statement, no
mutating keyword — so a malformed addition fails the test suite rather than a
production run.

Versioning is per query, not per module. When ``sys.pdw_table_distribution_properties``
needs a second column, ``tables`` becomes v2 and the old text stays readable
in history; nothing else is disturbed. The registry is deliberately shaped to
grow: indexes, partitions, statistics, constraints, distribution columns,
security and row counts are all further entries here, not further modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Mapping, Tuple

#: Statement keywords a catalog query must never contain. Matched as whole
#: words against the query text, which is a literal in this module and never
#: built from caller input.
_FORBIDDEN = (
    "insert", "update", "delete", "merge", "drop", "create", "alter",
    "truncate", "grant", "revoke", "deny", "exec", "execute", "into",
    "backup", "restore", "shutdown", "waitfor", "openrowset", "openquery",
)

_FORBIDDEN_PATTERN = re.compile(
    r"\b(?:{0})\b".format("|".join(_FORBIDDEN)), re.IGNORECASE
)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


@dataclass(frozen=True)
class CatalogQuery:
    """One named, versioned, read-only catalog query.

    ``parameters`` names the placeholders in order. The text uses ``?``
    positional binding, so a value can never be concatenated into SQL.
    """

    name: str
    version: int
    sql: str
    description: str
    parameters: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a catalog query needs a name")
        if self.version < 1:
            raise ValueError(f"{self.name}: version must be 1 or greater")

        stripped = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", self.sql)).strip()
        if not stripped:
            raise ValueError(f"{self.name}: query text is empty")
        if not stripped.upper().startswith("SELECT"):
            raise ValueError(f"{self.name}: catalog queries must start with SELECT")
        if ";" in stripped:
            raise ValueError(
                f"{self.name}: catalog queries must be a single statement "
                f"with no statement separator"
            )
        forbidden = _FORBIDDEN_PATTERN.search(stripped)
        if forbidden:
            raise ValueError(
                f"{self.name}: catalog queries must be read-only; found "
                f"{forbidden.group(0)!r}"
            )
        if stripped.count("?") != len(self.parameters):
            raise ValueError(
                f"{self.name}: declares {len(self.parameters)} parameter(s) but "
                f"the text has {stripped.count('?')} placeholder(s)"
            )

    @property
    def qualified_name(self) -> str:
        return f"{self.name}.v{self.version}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "qualified_name": self.qualified_name,
            "description": self.description,
            "parameters": list(self.parameters),
        }


# --- the queries -------------------------------------------------------------
#
# Written against the Synapse dedicated pool catalog. Every column selected is
# aliased, so row mapping keys off names this module controls rather than off
# whatever the engine happens to call them.


DATABASE = CatalogQuery(
    name="database",
    version=1,
    description="The database this connection is scoped to, and its collation.",
    sql="""
        SELECT
            DB_NAME() AS database_name,
            CONVERT(nvarchar(128), DATABASEPROPERTYEX(DB_NAME(), 'Collation'))
                AS collation_name
    """,
)


PRINCIPAL = CatalogQuery(
    name="principal",
    version=1,
    description=(
        "Who discovery connected as. Catalog views are security-trimmed, so "
        "the principal bounds what can be discovered at all."
    ),
    sql="""
        SELECT
            CURRENT_USER AS user_name,
            SUSER_SNAME() AS login_name
    """,
)


TABLES = CatalogQuery(
    name="tables",
    version=2,
    description=(
        "Every user table visible to the connected principal, with its "
        "distribution policy and whether it is external. LEFT JOIN so a table "
        "whose distribution row is not readable is still discovered rather "
        "than silently dropped."
    ),
    # v2 adds is_external. External tables live in sys.tables alongside
    # distributed ones but have no row in the distribution view, so without
    # this column the LEFT JOIN's NULL is indistinguishable from a
    # distribution row the principal cannot read — and every external table
    # reports a gap that is not a gap.
    sql="""
        SELECT
            s.name AS schema_name,
            t.name AS table_name,
            t.object_id AS object_id,
            t.create_date AS create_date,
            t.modify_date AS modify_date,
            t.is_external AS is_external,
            d.distribution_policy AS distribution_policy
        FROM sys.tables AS t
        INNER JOIN sys.schemas AS s
            ON s.schema_id = t.schema_id
        LEFT JOIN sys.pdw_table_distribution_properties AS d
            ON d.object_id = t.object_id
        WHERE t.is_ms_shipped = 0
        ORDER BY s.name, t.name
    """,
)


COLUMNS = CatalogQuery(
    name="columns",
    version=1,
    description=(
        "Every column of every visible user table, in one round trip. Keyed "
        "by object_id, which the table query also returns."
    ),
    sql="""
        SELECT
            c.object_id AS object_id,
            c.column_id AS column_id,
            c.name AS column_name,
            ty.name AS data_type,
            c.max_length AS max_length,
            c.precision AS precision_value,
            c.scale AS scale_value,
            c.is_nullable AS is_nullable,
            c.is_identity AS is_identity
        FROM sys.columns AS c
        INNER JOIN sys.tables AS t
            ON t.object_id = c.object_id
        INNER JOIN sys.types AS ty
            ON ty.user_type_id = c.user_type_id
        WHERE t.is_ms_shipped = 0
        ORDER BY c.object_id, c.column_id
    """,
)


DISTRIBUTION_COLUMNS = CatalogQuery(
    name="distribution_columns",
    version=1,
    description=(
        "The hash key of every hash-distributed table: which column, and in "
        "what order for a multi-column key. Keyed by object_id, which the "
        "table query also returns."
    ),
    # A separate query rather than a join onto tables.v2, because the grain
    # differs: one table has zero, one or several distribution columns, and
    # folding that into the table query would multiply table rows and make a
    # missing distribution row indistinguishable from a missing table.
    #
    # distribution_ordinal > 0 is what makes a column a distribution column;
    # every other column of a hash-distributed table carries 0. Filtering here
    # rather than in Python means an ordinal of 0 arriving downstream is a
    # signal that this predicate did not apply, not an ordinary row.
    #
    # LEFT JOIN to sys.columns so an unreadable sys.columns costs the name but
    # not the evidence: the ordinal and column_id still identify the key.
    sql="""
        SELECT
            dc.object_id AS object_id,
            dc.column_id AS column_id,
            c.name AS column_name,
            dc.distribution_ordinal AS distribution_ordinal
        FROM sys.pdw_column_distribution_properties AS dc
        INNER JOIN sys.tables AS t
            ON t.object_id = dc.object_id
        LEFT JOIN sys.columns AS c
            ON c.object_id = dc.object_id
            AND c.column_id = dc.column_id
        WHERE t.is_ms_shipped = 0
            AND dc.distribution_ordinal > 0
        ORDER BY dc.object_id, dc.distribution_ordinal
    """,
)


INDEXES = CatalogQuery(
    name="indexes",
    version=1,
    description=(
        "Every index on every visible user table, including the base storage "
        "structure: index_id 0 is a heap and index_id 1 is the clustered "
        "index, columnstore or rowstore. Keyed by object_id."
    ),
    # type_desc is selected alongside the numeric type so the catalog's own
    # wording survives even for a type code this version has no name for. The
    # mapped kind is a convenience; type_code and type_desc are the evidence.
    #
    # A heap's row has a NULL name, which is why name is not used to identify
    # an index anywhere downstream — index_id is.
    sql="""
        SELECT
            i.object_id AS object_id,
            i.index_id AS index_id,
            i.name AS index_name,
            i.type AS type_code,
            i.type_desc AS type_desc,
            i.is_unique AS is_unique,
            i.is_primary_key AS is_primary_key,
            i.is_unique_constraint AS is_unique_constraint
        FROM sys.indexes AS i
        INNER JOIN sys.tables AS t
            ON t.object_id = i.object_id
        WHERE t.is_ms_shipped = 0
        ORDER BY i.object_id, i.index_id
    """,
)


INDEX_COLUMNS = CatalogQuery(
    name="index_columns",
    version=1,
    description=(
        "The columns of every index, with key order for rowstore indexes and "
        "the ordering ordinal that distinguishes an ordered clustered "
        "columnstore index from a plain one."
    ),
    # column_store_order_ordinal is the only way the catalog exposes ORDERED
    # CLUSTERED COLUMNSTORE: an ordered CCI is a type 5 index whose columns
    # carry a positive value here, and a plain CCI is the same index type with
    # zeroes. There is no separate type code for it.
    #
    # Selecting it is a deliberate risk. If an engine version does not have
    # the column this whole query fails, which costs key order for every index
    # rather than only the ordering. That is why indexes and index columns are
    # two queries: the index list, and with it the heap/clustered/columnstore
    # distinction, survives this one failing.
    #
    # LEFT JOIN to sys.columns so an unreadable sys.columns costs the name but
    # not the ordinal or the column_id.
    sql="""
        SELECT
            ic.object_id AS object_id,
            ic.index_id AS index_id,
            ic.column_id AS column_id,
            c.name AS column_name,
            ic.key_ordinal AS key_ordinal,
            ic.is_descending_key AS is_descending_key,
            ic.column_store_order_ordinal AS column_store_order_ordinal
        FROM sys.index_columns AS ic
        INNER JOIN sys.tables AS t
            ON t.object_id = ic.object_id
        LEFT JOIN sys.columns AS c
            ON c.object_id = ic.object_id
            AND c.column_id = ic.column_id
        WHERE t.is_ms_shipped = 0
        ORDER BY ic.object_id, ic.index_id, ic.key_ordinal, ic.column_id
    """,
)


VIEWS = CatalogQuery(
    name="views",
    version=1,
    description=(
        "Every user view visible to the connected principal. Identity and "
        "dates only: a view's text lives in sys.sql_modules, which is "
        "separately grantable and therefore separately queried."
    ),
    sql="""
        SELECT
            s.name AS schema_name,
            v.name AS object_name,
            v.object_id AS object_id,
            v.create_date AS create_date,
            v.modify_date AS modify_date
        FROM sys.views AS v
        INNER JOIN sys.schemas AS s
            ON s.schema_id = v.schema_id
        WHERE v.is_ms_shipped = 0
        ORDER BY s.name, v.name
    """,
)


PROCEDURES = CatalogQuery(
    name="procedures",
    version=1,
    description=(
        "Every user stored procedure visible to the connected principal. "
        "Identity and dates only, for the same reason views are: the body "
        "is a separate grant in sys.sql_modules."
    ),
    sql="""
        SELECT
            s.name AS schema_name,
            p.name AS object_name,
            p.object_id AS object_id,
            p.create_date AS create_date,
            p.modify_date AS modify_date,
            p.type AS type_code
        FROM sys.procedures AS p
        INNER JOIN sys.schemas AS s
            ON s.schema_id = p.schema_id
        WHERE p.is_ms_shipped = 0
        ORDER BY s.name, p.name
    """,
)


MODULES = CatalogQuery(
    name="modules",
    version=1,
    description=(
        "The body of every visible module, for views and procedures alike. "
        "Keyed by object_id, which both object queries also return."
    ),
    # One query for both object kinds because sys.sql_modules is one view and
    # the grain is the same: one row per module. Splitting it would double the
    # round trips and let a principal see a view's body but not a procedure's
    # for no reason the catalog recognises.
    #
    # A NULL definition is the important case and is *not* a module without a
    # body. It means the text was withheld -- the module is encrypted, or
    # VIEW DEFINITION was not granted -- and the mapping layer records it as
    # opaque rather than as absent. There is no is_encrypted column on this
    # view to tell the two apart, so neither is claimed.
    #
    # The join to sys.objects rather than to sys.views or sys.procedures keeps
    # this query independent of which object kind is being discovered.
    sql="""
        SELECT
            m.object_id AS object_id,
            m.definition AS definition,
            m.uses_ansi_nulls AS uses_ansi_nulls,
            m.uses_quoted_identifier AS uses_quoted_identifier,
            m.is_schema_bound AS is_schema_bound
        FROM sys.sql_modules AS m
        INNER JOIN sys.objects AS o
            ON o.object_id = m.object_id
        WHERE o.is_ms_shipped = 0
        ORDER BY m.object_id
    """,
)


PARAMETERS = CatalogQuery(
    name="parameters",
    version=1,
    description=(
        "Every parameter of every visible stored procedure, with its type, "
        "direction and whether it has a default. Keyed by object_id."
    ),
    # parameter_id 0 is the return value rather than a parameter, and is kept:
    # dropping it here would make a procedure with a return value look
    # identical to one without.
    sql="""
        SELECT
            pa.object_id AS object_id,
            pa.parameter_id AS parameter_id,
            pa.name AS parameter_name,
            ty.name AS data_type,
            pa.max_length AS max_length,
            pa.precision AS precision_value,
            pa.scale AS scale_value,
            pa.is_output AS is_output,
            pa.has_default_value AS has_default_value
        FROM sys.parameters AS pa
        INNER JOIN sys.procedures AS pr
            ON pr.object_id = pa.object_id
        INNER JOIN sys.types AS ty
            ON ty.user_type_id = pa.user_type_id
        WHERE pr.is_ms_shipped = 0
        ORDER BY pa.object_id, pa.parameter_id
    """,
)


#: Every query discovery may run, by name. A ``CatalogSource`` checks
#: membership by identity, so a lookalike built elsewhere is not accepted.
CATALOG_QUERIES: Mapping[str, CatalogQuery] = {
    query.name: query
    for query in (
        DATABASE,
        PRINCIPAL,
        TABLES,
        COLUMNS,
        DISTRIBUTION_COLUMNS,
        INDEXES,
        INDEX_COLUMNS,
        VIEWS,
        PROCEDURES,
        MODULES,
        PARAMETERS,
    )
}


def catalog_query(name: str) -> CatalogQuery:
    """The registered query with this name, or a clear failure."""
    try:
        return CATALOG_QUERIES[name]
    except KeyError:
        known = ", ".join(sorted(CATALOG_QUERIES))
        raise KeyError(f"no catalog query named {name!r}; known: {known}") from None


def is_registered(query: CatalogQuery) -> bool:
    """Whether this is *the* registered query object, not merely one like it.

    Identity rather than equality: an equal-looking ``CatalogQuery`` built by
    a caller is still caller-supplied SQL, and the point of the registry is
    that caller-supplied SQL never runs.
    """
    return CATALOG_QUERIES.get(query.name) is query


def registry_summary() -> Dict[str, dict]:
    """What discovery is able to ask, for a manifest or a review."""
    return {name: query.to_dict() for name, query in sorted(CATALOG_QUERIES.items())}
