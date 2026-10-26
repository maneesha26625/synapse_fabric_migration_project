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
        "The body of every visible module: views, procedures and functions alike. "
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


FUNCTIONS = CatalogQuery(
    name="functions",
    version=1,
    description=(
        "Every user-defined T-SQL function visible to the connected principal: "
        "scalar (FN), inline table-valued (IF) and multi-statement table-valued "
        "(TF). Identity, dates and kind only; the body comes from sys.sql_modules."
    ),
    sql="""
        SELECT
            s.name AS schema_name,
            o.name AS object_name,
            o.object_id AS object_id,
            o.create_date AS create_date,
            o.modify_date AS modify_date,
            o.type AS type_code
        FROM sys.objects AS o
        INNER JOIN sys.schemas AS s
            ON s.schema_id = o.schema_id
        WHERE o.is_ms_shipped = 0
            AND o.type IN ('FN', 'IF', 'TF')
        ORDER BY s.name, o.name
    """,
)


# --- security: what the Warehouse needs to recreate access -------------------
#
# Each of these is separately grantable and degrades on its own. Together they
# are what the Fabric Migration Assistant carries over as "security objects":
# roles and their members, object, schema and database permissions, and
# dynamic data masking.


DATABASE_PRINCIPALS = CatalogQuery(
    name="database_principals",
    version=1,
    description=(
        "Every database user and role. type: S = SQL user, E = Microsoft Entra "
        "user, X = Microsoft Entra group, R = database role."
    ),
    sql="""
        SELECT
            dp.principal_id AS principal_id,
            dp.name AS principal_name,
            dp.type AS type_code,
            dp.type_desc AS type_desc
        FROM sys.database_principals AS dp
        ORDER BY dp.name
    """,
)


ROLE_MEMBERS = CatalogQuery(
    name="role_members",
    version=1,
    description="Which user or role is a member of which database role.",
    sql="""
        SELECT
            r.name AS role_name,
            m.name AS member_name,
            m.type AS member_type
        FROM sys.database_role_members AS rm
        INNER JOIN sys.database_principals AS r
            ON r.principal_id = rm.role_principal_id
        INNER JOIN sys.database_principals AS m
            ON m.principal_id = rm.member_principal_id
        ORDER BY r.name, m.name
    """,
)


PERMISSIONS = CatalogQuery(
    name="permissions",
    version=2,
    description=(
        "Explicit permissions on the database (class 0), objects and columns "
        "(class 1) and schemas (class 3), with the principal they were given "
        "to and whether they were granted, granted with grant option or denied."
    ),
    # state: G granted, W granted with grant option, D denied. The securable is
    # resolved to names here so the rows survive without object ids. major_id is
    # kept because a negative one is a system object (public's built-in grants on
    # catalog views), which sys.objects does not list and no migration carries.
    sql="""
        SELECT
            pe.class AS class_code,
            pe.major_id AS major_id,
            pe.permission_name AS permission_name,
            pe.state AS state_code,
            gp.name AS grantee_name,
            gp.type AS grantee_type,
            COALESCE(os.name, ss.name) AS schema_name,
            o.name AS object_name,
            o.type AS object_type,
            c.name AS column_name
        FROM sys.database_permissions AS pe
        INNER JOIN sys.database_principals AS gp
            ON gp.principal_id = pe.grantee_principal_id
        LEFT JOIN sys.objects AS o
            ON pe.class = 1 AND o.object_id = pe.major_id
        LEFT JOIN sys.schemas AS os
            ON os.schema_id = o.schema_id
        LEFT JOIN sys.schemas AS ss
            ON pe.class = 3 AND ss.schema_id = pe.major_id
        LEFT JOIN sys.columns AS c
            ON pe.class = 1 AND pe.minor_id > 0
            AND c.object_id = pe.major_id AND c.column_id = pe.minor_id
        WHERE pe.class IN (0, 1, 3)
        ORDER BY gp.name, pe.class, pe.permission_name
    """,
)


MASKED_COLUMNS = CatalogQuery(
    name="masked_columns",
    version=1,
    description="Columns with a dynamic data mask, and the masking function.",
    sql="""
        SELECT
            s.name AS schema_name,
            t.name AS table_name,
            mc.name AS column_name,
            mc.masking_function AS masking_function
        FROM sys.masked_columns AS mc
        INNER JOIN sys.tables AS t
            ON t.object_id = mc.object_id
        INNER JOIN sys.schemas AS s
            ON s.schema_id = t.schema_id
        WHERE mc.is_masked = 1
        ORDER BY s.name, t.name, mc.column_id
    """,
)


# --- the estate: volume, row-level security, external sources, workload (``sql.estate``) ----------


TABLE_SIZES = CatalogQuery(
    name="table_sizes",
    version=1,
    description=(
        "Rows and reserved pages per user table and compute node, from the distribution-level partition "
        "stats. Needs VIEW DATABASE STATE. Rows count the heap or clustered index only (index_id 0 or 1)."
    ),
    sql="""
        SELECT
            s.name AS schema_name,
            t.name AS table_name,
            nt.pdw_node_id AS node_id,
            SUM(CASE WHEN ps.index_id <= 1 THEN ps.row_count ELSE 0 END) AS row_count,
            SUM(ps.reserved_page_count) AS reserved_pages
        FROM sys.tables AS t
        INNER JOIN sys.schemas AS s
            ON s.schema_id = t.schema_id
        INNER JOIN sys.pdw_table_mappings AS tm
            ON tm.object_id = t.object_id
        INNER JOIN sys.pdw_nodes_tables AS nt
            ON nt.name = tm.physical_name
        INNER JOIN sys.dm_pdw_nodes_db_partition_stats AS ps
            ON ps.object_id = nt.object_id
            AND ps.pdw_node_id = nt.pdw_node_id
            AND ps.distribution_id = nt.distribution_id
        WHERE t.is_external = 0
        GROUP BY s.name, t.name, nt.pdw_node_id
    """,
)


SECURITY_POLICIES = CatalogQuery(
    name="security_policies",
    version=1,
    description="Row-level security policies, one row per predicate, with the table each one guards.",
    sql="""
        SELECT
            ps.name AS policy_schema,
            sp.name AS policy_name,
            sp.is_enabled AS is_enabled,
            pr.predicate_type_desc AS predicate_type,
            pr.operation_desc AS operation,
            pr.predicate_definition AS predicate_definition,
            ts.name AS target_schema,
            o.name AS target_name
        FROM sys.security_policies AS sp
        INNER JOIN sys.schemas AS ps
            ON ps.schema_id = sp.schema_id
        INNER JOIN sys.security_predicates AS pr
            ON pr.object_id = sp.object_id
        INNER JOIN sys.objects AS o
            ON o.object_id = pr.target_object_id
        INNER JOIN sys.schemas AS ts
            ON ts.schema_id = o.schema_id
        ORDER BY ps.name, sp.name, pr.security_predicate_id
    """,
)


EXTERNAL_DATA_SOURCES = CatalogQuery(
    name="external_data_sources",
    version=1,
    description="External data sources: name, location, type and the name of the credential they use (never a secret).",
    sql="""
        SELECT
            ds.name AS source_name,
            ds.location AS location,
            ds.type_desc AS source_type,
            c.name AS credential_name
        FROM sys.external_data_sources AS ds
        LEFT JOIN sys.database_scoped_credentials AS c
            ON c.credential_id = ds.credential_id
        ORDER BY ds.name
    """,
)


EXTERNAL_FILE_FORMATS = CatalogQuery(
    name="external_file_formats",
    version=1,
    description="External file formats: name, format type, field terminator and compression.",
    sql="""
        SELECT
            ff.name AS format_name,
            ff.format_type AS format_type,
            ff.field_terminator AS field_terminator,
            ff.data_compression AS data_compression
        FROM sys.external_file_formats AS ff
        ORDER BY ff.name
    """,
)


QUERY_HISTORY = CatalogQuery(
    name="query_history",
    version=1,
    description=(
        "The most demanding recent user SELECT statements (runs x average time) from the request history, "
        "with how long they took. Needs VIEW DATABASE STATE; the pool keeps its last 10,000 requests only. "
        "Catalog and DMV queries are left out."
    ),
    sql="""
        SELECT TOP 200
            r.command AS command_text,
            COUNT_BIG(*) AS run_count,
            AVG(CAST(r.total_elapsed_time AS bigint)) AS avg_ms,
            MAX(CAST(r.total_elapsed_time AS bigint)) AS max_ms,
            MIN(CAST(r.total_elapsed_time AS bigint)) AS min_ms,
            MAX(r.end_time) AS last_run,
            MAX(r.[label]) AS query_label
        FROM sys.dm_pdw_exec_requests AS r
        WHERE r.status = 'Completed'
            AND r.session_id <> SESSION_ID()
            AND (r.resource_class IS NOT NULL OR r.importance IS NOT NULL)
            AND LTRIM(r.command) LIKE 'SELECT%'
            AND r.command NOT LIKE '%sys.%'
            AND r.command NOT LIKE '%INFORMATION_SCHEMA%'
        GROUP BY r.command
        ORDER BY COUNT_BIG(*) * AVG(CAST(r.total_elapsed_time AS bigint)) DESC
    """,
)


REQUEST_WINDOW = CatalogQuery(
    name="request_window",
    version=1,
    description=(
        "When each completed user request in the history started and ended, to find how many ran at once. "
        "Needs VIEW DATABASE STATE."
    ),
    sql="""
        SELECT
            r.start_time AS start_time,
            r.end_time AS end_time,
            r.total_elapsed_time AS elapsed_ms
        FROM sys.dm_pdw_exec_requests AS r
        WHERE r.status = 'Completed'
            AND r.start_time IS NOT NULL
            AND r.end_time IS NOT NULL
            AND r.session_id <> SESSION_ID()
            AND (r.resource_class IS NOT NULL OR r.importance IS NOT NULL)
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
        FUNCTIONS,
        DATABASE_PRINCIPALS,
        ROLE_MEMBERS,
        PERMISSIONS,
        MASKED_COLUMNS,
        TABLE_SIZES,
        SECURITY_POLICIES,
        EXTERNAL_DATA_SOURCES,
        EXTERNAL_FILE_FORMATS,
        QUERY_HISTORY,
        REQUEST_WINDOW,
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
