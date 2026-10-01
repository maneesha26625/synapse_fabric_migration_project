"""Tests for live SQL catalog discovery, without a database.

Every test here drives the real ``DedicatedPoolSource`` through a fake
connector returning deterministic rows. No network, no driver, no
credentials — the same discipline the repository extractor tests follow
against a local clone.

What is deliberately not tested here: that the catalog queries are correct
T-SQL for a Synapse dedicated pool. Only a real pool can answer that, and
``test_sql_dedicated_pool_integration.py`` is where it is asked.
"""

from __future__ import annotations

import datetime as dt
import inspect

import pytest

from discovery_agent.errors import (
    CatalogQueryError,
    ConfigError,
    SqlAuthenticationError,
    SqlConnectionError,
    SqlDiscoveryError,
)
from discovery_agent.extractors.models import IssueCode, SourceType
from discovery_agent.sql import queries
from discovery_agent.sql.auth import (
    AccessTokenAuthentication,
    EntraAuthMethod,
    authentication_for,
)
from discovery_agent.sql.config import SqlConnectionConfig, config_from_environment
from discovery_agent.sql.connection import Connector, build_connection_string
from discovery_agent.sql.dedicated_pool import DedicatedPoolSource
from discovery_agent.sql.mapping import (
    build_tables,
    column_from_row,
    distribution_from_row,
    table_key_from_row,
)
from discovery_agent.sql.models import (
    DISTRIBUTION_POLICY_CODES,
    UNDISTRIBUTED_POLICIES,
    CatalogObjectType,
    DefinitionState,
    IndexKind,
    TableStorage,
    CatalogPrincipal,
    DistributionPolicy,
    SqlObjectKey,
)
from discovery_agent.sql.queries import CATALOG_QUERIES, CatalogQuery, is_registered
from discovery_agent.sql.source import SQL_SOURCE_FORMAT

SERVER = "poc-ws.sql.azuresynapse.net"
DATABASE = "poolone"

TABLE_COLUMNS = (
    "schema_name",
    "table_name",
    "object_id",
    "create_date",
    "modify_date",
    "is_external",
    "distribution_policy",
)
COLUMN_COLUMNS = (
    "object_id",
    "column_id",
    "column_name",
    "data_type",
    "max_length",
    "precision_value",
    "scale_value",
    "is_nullable",
    "is_identity",
)
INDEX_ROW_COLUMNS = (
    "object_id",
    "index_id",
    "index_name",
    "type_code",
    "type_desc",
    "is_unique",
    "is_primary_key",
    "is_unique_constraint",
)
INDEX_COLUMN_COLUMNS = (
    "object_id",
    "index_id",
    "column_id",
    "column_name",
    "key_ordinal",
    "is_descending_key",
    "column_store_order_ordinal",
)
DISTRIBUTION_COLUMN_COLUMNS = (
    "object_id",
    "column_id",
    "column_name",
    "distribution_ordinal",
)
VIEW_COLUMNS = (
    "schema_name",
    "object_name",
    "object_id",
    "create_date",
    "modify_date",
)
PROCEDURE_COLUMNS = VIEW_COLUMNS + ("type_code",)
MODULE_COLUMNS = (
    "object_id",
    "definition",
    "uses_ansi_nulls",
    "uses_quoted_identifier",
    "is_schema_bound",
)
PARAMETER_COLUMNS = (
    "object_id",
    "parameter_id",
    "parameter_name",
    "data_type",
    "max_length",
    "precision_value",
    "scale_value",
    "is_output",
    "has_default_value",
)
CREATED = dt.datetime(2024, 3, 1, 9, 30, 0)
MODIFIED = dt.datetime(2024, 6, 2, 11, 0, 0)


# --- a fake driver -----------------------------------------------------------


class FakeCursor:
    def __init__(self, responses):
        self._responses = responses
        self._names = ()
        self._rows = ()
        self.closed = False
        self.executed = []

    def execute(self, sql, parameters=None):
        name = _registered_name(sql)
        self.executed.append((name, parameters))
        response = self._responses.get(name)
        if response is None:
            raise AssertionError(f"fake has no response for query {name!r}")
        if isinstance(response, Exception):
            raise response
        self._names, self._rows = response
        return self

    @property
    def description(self):
        return tuple((name, None, None, None, None, None, None) for name in self._names)

    def fetchall(self):
        return list(self._rows)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, responses):
        self._responses = responses
        self.closed = False
        self.cursors = []

    def cursor(self):
        cursor = FakeCursor(self._responses)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class FakeConnector(Connector):
    """A connector that hands back canned rows and counts connections."""

    def __init__(self, responses, fail_with=None):
        self.responses = responses
        self.fail_with = fail_with
        self.connections = []

    def connect(self):
        if self.fail_with is not None:
            raise self.fail_with
        connection = FakeConnection(self.responses)
        self.connections.append(connection)
        return connection

    def describe(self):
        return f"server={SERVER} database={DATABASE} auth=interactive"


def _registered_name(sql):
    for name, query in CATALOG_QUERIES.items():
        if query.sql == sql:
            return name
    raise AssertionError("a query reached the driver that is not in the registry")


def config(**overrides):
    return SqlConnectionConfig(
        server=overrides.pop("server", SERVER),
        database=overrides.pop("database", DATABASE),
        **overrides,
    )


def _default_heaps(table_rows):
    """A heap base index for every non-external table in the fixture.

    Positional because that is how these fixtures build rows: object_id is
    field 2 and is_external is field 5 of ``table_row``. External tables are
    skipped — their storage is NOT_APPLICABLE and a base row for one would be
    fiction.
    """
    if isinstance(table_rows, Exception):
        return ()  # the table query itself fails; there is nothing to derive from
    return [heap_row(row[2]) for row in table_rows if not row[5]]


def _response(names, rows):
    """Canned rows, or the exception the driver would raise instead."""
    if isinstance(rows, Exception):
        return rows
    return (names, list(rows))


def responses(
    tables=(),
    columns=(),
    database=None,
    principal=None,
    distribution_columns=(),
    indexes=None,
    index_columns=(),
    views=(),
    procedures=(),
    modules=(),
    parameters=(),
    **overrides,
):
    """A fake driver script. Any value may be an Exception to simulate failure.

    ``indexes`` defaults to None meaning "give every ordinary table a heap
    base row", which is what a real pool returns and what keeps a fixture that
    is not about storage from acquiring a storage gap. Pass a list — including
    an empty one — to say exactly what the index catalog returns, which is how
    a test asserts a missing base index row.
    """
    base = {
        "database": _response(
            ("database_name", "collation_name"),
            [(DATABASE, "Latin1_General_100_CI_AS_KS_WS")]
            if database is None
            else database,
        ),
        "principal": _response(
            ("user_name", "login_name"),
            [("discovery_reader", "discovery@contoso.example")]
            if principal is None
            else principal,
        ),
        "tables": _response(TABLE_COLUMNS, tables),
        "columns": _response(COLUMN_COLUMNS, columns),
        "distribution_columns": _response(
            DISTRIBUTION_COLUMN_COLUMNS, distribution_columns
        ),
        "indexes": _response(
            INDEX_ROW_COLUMNS,
            _default_heaps(tables) if indexes is None else indexes,
        ),
        "index_columns": _response(INDEX_COLUMN_COLUMNS, index_columns),
        "views": _response(VIEW_COLUMNS, views),
        "procedures": _response(PROCEDURE_COLUMNS, procedures),
        "modules": _response(MODULE_COLUMNS, modules),
        "parameters": _response(PARAMETER_COLUMNS, parameters),
    }
    base.update(overrides)
    return base


def table_row(
    schema,
    name,
    object_id,
    policy=2,
    created=CREATED,
    modified=MODIFIED,
    external=False,
):
    return (schema, name, object_id, created, modified, external, policy)


def external_table_row(schema, name, object_id, **kwargs):
    """An external table as the pool actually reports one.

    ``is_external`` set and *no* distribution row behind the LEFT JOIN — the
    combination that used to be indistinguishable from an unreadable
    distribution view.
    """
    kwargs.setdefault("policy", None)
    return table_row(schema, name, object_id, external=True, **kwargs)


#: sys.indexes type codes, for readable fixtures.
HEAP, CLUSTERED, NONCLUSTERED, CLUSTERED_COLUMNSTORE = 0, 1, 2, 5

#: What the catalog calls each of them, so fixtures carry the raw evidence a
#: real row would.
TYPE_DESCS = {
    HEAP: "HEAP",
    CLUSTERED: "CLUSTERED",
    NONCLUSTERED: "NONCLUSTERED",
    CLUSTERED_COLUMNSTORE: "CLUSTERED COLUMNSTORE",
}


def index_row(
    object_id,
    index_id,
    type_code,
    name=None,
    unique=False,
    primary_key=False,
    unique_constraint=False,
    type_desc=None,
):
    return (
        object_id,
        index_id,
        name,
        type_code,
        TYPE_DESCS.get(type_code) if type_desc is None else type_desc,
        unique,
        primary_key,
        unique_constraint,
    )


def heap_row(object_id):
    """The base index row of an ordinary heap table, name NULL as in life."""
    return index_row(object_id, 0, HEAP)


def index_column_row(
    object_id,
    index_id,
    column_id,
    name,
    key_ordinal=0,
    descending=False,
    order_ordinal=0,
):
    return (
        object_id,
        index_id,
        column_id,
        name,
        key_ordinal,
        descending,
        order_ordinal,
    )


def view_row(schema, name, object_id, created=CREATED, modified=MODIFIED):
    return (schema, name, object_id, created, modified)


def procedure_row(
    schema, name, object_id, created=CREATED, modified=MODIFIED, type_code="P"
):
    return (schema, name, object_id, created, modified, type_code)


def module_row(
    object_id,
    definition,
    ansi_nulls=True,
    quoted_identifier=True,
    schema_bound=False,
):
    """A sys.sql_modules row. ``definition=None`` is an encrypted or
    permission-trimmed module, which is the case that matters."""
    return (object_id, definition, ansi_nulls, quoted_identifier, schema_bound)


def parameter_row(
    object_id,
    parameter_id,
    name,
    data_type="int",
    max_length=4,
    precision=10,
    scale=0,
    output=False,
    has_default=False,
):
    return (
        object_id,
        parameter_id,
        name,
        data_type,
        max_length,
        precision,
        scale,
        output,
        has_default,
    )


def distribution_column_row(object_id, column_id, name, ordinal=1):
    return (object_id, column_id, name, ordinal)


def column_row(
    object_id,
    column_id,
    name,
    data_type="int",
    max_length=4,
    precision=10,
    scale=0,
    nullable=False,
    identity=False,
):
    return (
        object_id,
        column_id,
        name,
        data_type,
        max_length,
        precision,
        scale,
        nullable,
        identity,
    )


def source(responses_map, **config_overrides):
    return DedicatedPoolSource(config(**config_overrides), FakeConnector(responses_map))


# --- 1: the query registry ---------------------------------------------------


def test_every_registered_query_is_select_only():
    for query in CATALOG_QUERIES.values():
        assert query.sql.strip().upper().startswith("SELECT")
        assert ";" not in query.sql


def test_every_registered_query_is_versioned_and_described():
    for name, query in CATALOG_QUERIES.items():
        assert query.name == name
        assert query.version >= 1
        assert query.description
        assert query.qualified_name == f"{name}.v{query.version}"


def test_the_registry_covers_this_slice():
    """Every query discovery may run, named. Adding one is a deliberate act."""
    assert set(CATALOG_QUERIES) == {
        "database",
        "principal",
        "tables",
        "columns",
        "distribution_columns",
        "indexes",
        "index_columns",
        "views",
        "procedures",
        "modules",
        "parameters",
    }


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE dbo.Trips",
        "SELECT 1; DROP TABLE dbo.Trips",
        "SELECT * FROM sys.tables WHERE 1=1 DELETE FROM dbo.Trips",
        "EXEC sp_who",
        "   ",
    ],
)
def test_a_query_that_is_not_read_only_cannot_be_constructed(sql):
    with pytest.raises(ValueError):
        CatalogQuery(name="x", version=1, sql=sql, description="d")


def test_a_query_must_declare_its_placeholders():
    with pytest.raises(ValueError):
        CatalogQuery(
            name="x",
            version=1,
            sql="SELECT name FROM sys.tables WHERE object_id = ?",
            description="d",
        )


def test_a_lookalike_query_is_not_the_registered_one():
    impostor = CatalogQuery(
        name="tables",
        version=1,
        sql=queries.TABLES.sql,
        description="looks identical",
    )

    assert not is_registered(impostor)
    assert is_registered(queries.TABLES)


def test_catalog_query_lookup_names_what_it_knows():
    assert queries.catalog_query("tables") is queries.TABLES
    with pytest.raises(KeyError, match="known:"):
        queries.catalog_query("row_counts")


# --- 2: arbitrary SQL cannot be submitted ------------------------------------


def test_an_unregistered_query_is_refused_before_connecting():
    connector = FakeConnector(responses())
    pool = DedicatedPoolSource(config(), connector)
    impostor = CatalogQuery(
        name="tables", version=1, sql=queries.TABLES.sql, description="d"
    )

    with pytest.raises(CatalogQueryError, match="not a registered catalog query"):
        pool.rows(impostor)

    assert connector.connections == [], "a refused query must not open a connection"


def test_a_raw_string_cannot_be_passed_as_a_query():
    pool = source(responses())

    with pytest.raises(CatalogQueryError, match="must be CatalogQuery instances"):
        pool.rows("SELECT 1")


def test_parameter_count_is_enforced():
    pool = source(responses())

    with pytest.raises(CatalogQueryError, match="takes 0 parameter"):
        pool.rows(queries.TABLES, 1)


# --- 3: row to model mapping -------------------------------------------------


def test_one_table_with_its_columns():
    pool = source(
        responses(
            tables=[table_row("dbo", "TripsData", 100)],
            columns=[
                column_row(100, 1, "trip_id"),
                column_row(100, 2, "vendor", data_type="nvarchar", max_length=100),
            ],
        )
    )

    discovery = pool.discover_tables()
    table = discovery.tables[0]

    assert discovery.table_count == 1
    assert table.key.database == DATABASE
    assert table.key.schema == "dbo"
    assert table.key.name == "TripsData"
    assert table.key.object_id == 100
    assert table.create_date == "2024-03-01T09:30:00"
    assert table.modify_date == "2024-06-02T11:00:00"
    assert table.distribution is DistributionPolicy.HASH
    assert [c.name for c in table.columns] == ["trip_id", "vendor"]


def test_column_attributes_are_mapped_faithfully():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1)],
            columns=[
                column_row(
                    1, 1, "id", data_type="bigint", max_length=8, precision=19,
                    scale=0, nullable=False, identity=True,
                ),
                column_row(
                    1, 2, "amount", data_type="decimal", max_length=9, precision=18,
                    scale=4, nullable=True, identity=False,
                ),
            ],
        )
    )

    columns = pool.discover_tables().tables[0].columns

    assert columns[0].data_type == "bigint"
    assert columns[0].is_identity is True
    assert columns[0].is_nullable is False
    assert columns[1].precision == 18
    assert columns[1].scale == 4
    assert columns[1].is_nullable is True


def test_columns_are_ordered_by_column_id_not_by_row_order():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1)],
            columns=[column_row(1, 3, "c"), column_row(1, 1, "a"), column_row(1, 2, "b")],
        )
    )

    assert [c.name for c in pool.discover_tables().tables[0].columns] == ["a", "b", "c"]


#: The documented meaning of
#: ``sys.pdw_table_distribution_properties.distribution_policy``. Written out
#: here, independently of the module under test, so that the test fails if the
#: map is edited rather than agreeing with whatever it happens to say.
DOCUMENTED_DISTRIBUTION_CODES = {
    2: DistributionPolicy.HASH,
    3: DistributionPolicy.REPLICATE,
    4: DistributionPolicy.ROUND_ROBIN,
}


@pytest.mark.parametrize(
    "code,expected", sorted(DOCUMENTED_DISTRIBUTION_CODES.items())
)
def test_distribution_codes_are_mapped(code, expected):
    pool = source(responses(tables=[table_row("dbo", "T", 1, policy=code)]))
    table = pool.discover_tables().tables[0]

    assert table.distribution is expected
    assert table.distribution_policy_code == code
    assert not [i for i in table.issues if i.code is IssueCode.UNSUPPORTED_CONSTRUCT]


def test_the_distribution_code_map_matches_the_documented_codes():
    """Regression: 3 and 4 were transposed, so ROUND_ROBIN read as REPLICATE.

    A transposition of two valid codes produces no issue and no UNKNOWN — every
    table still reports a plausible policy, just the wrong one, and the error
    only surfaces by comparing against the pool itself. Pinning the whole map
    (not just each key) also catches an entry being dropped or a code being
    invented for a policy the view does not report.
    """
    assert DISTRIBUTION_POLICY_CODES == DOCUMENTED_DISTRIBUTION_CODES


@pytest.mark.parametrize(
    "code,expected", sorted(DOCUMENTED_DISTRIBUTION_CODES.items())
)
def test_each_documented_code_maps_through_the_pure_mapping_layer(code, expected):
    """The same contract at the function that reads the row, with no source."""
    policy, raw, issues = distribution_from_row(
        {"distribution_policy": code}, "poolone.dbo.T"
    )

    assert policy is expected
    assert raw == code
    assert issues == ()


def test_every_distributed_policy_has_a_code_and_no_other_policy_does():
    """A policy the pool can report but discovery cannot decode is a gap.

    The two undistributed answers are excluded deliberately, and neither may
    be reachable from a code: UNKNOWN is what a code with no name becomes, and
    NOT_APPLICABLE is what a table with no distribution at all becomes. A code
    mapping to either would mean the view had reported a policy and discovery
    had then thrown it away.
    """
    decodable = set(DISTRIBUTION_POLICY_CODES.values())
    named = {p for p in DistributionPolicy if p not in UNDISTRIBUTED_POLICIES}

    assert decodable == named
    assert not decodable & UNDISTRIBUTED_POLICIES


# --- 3b: external tables vs. distributed tables ------------------------------
#
# An external table lives in sys.tables next to the distributed ones but has
# no row in sys.pdw_table_distribution_properties. Before is_external was
# selected, that missing row was read as "the distribution view may not be
# readable by the connected principal" — a statement about grants, made about
# a table that simply has no distribution. These tests pin the distinction
# from both directions: the external case must not raise that issue, and the
# genuinely-unreadable case must still raise it.


def test_a_regular_table_is_distributed_and_not_external():
    pool = source(
        responses(
            tables=[table_row("dbo", "TripsData", 1, policy=2)],
            columns=[column_row(1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.is_external is False
    assert table.is_distributed is True
    assert table.distribution is DistributionPolicy.HASH


def test_an_external_table_reports_no_distribution_rather_than_a_gap():
    pool = source(
        responses(
            tables=[external_table_row("dbo", "ExtSales", 1)],
            columns=[column_row(1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.is_external is True
    assert table.is_distributed is False
    assert table.distribution is DistributionPolicy.NOT_APPLICABLE
    assert table.distribution_policy_code is None
    assert table.issues == (), (
        "an external table has no distribution to be missing; reporting one "
        "describes a permission problem that does not exist"
    )


def test_the_same_null_policy_means_different_things_for_the_two_kinds():
    """The crux of the fix, asserted as one comparison.

    Both rows carry ``distribution_policy = NULL``. The only difference is
    ``is_external``, and it must be enough to tell a table with no
    distribution apart from a distribution that could not be read.
    """
    pool = source(
        responses(
            tables=[
                external_table_row("dbo", "ExtSales", 1),
                table_row("dbo", "Trips", 2, policy=None),
            ],
            columns=[column_row(1, 1, "a"), column_row(2, 1, "b")],
        )
    )
    discovery = pool.discover_tables()

    external = discovery.table("dbo", "ExtSales")
    unreadable = discovery.table("dbo", "Trips")

    assert external.distribution is DistributionPolicy.NOT_APPLICABLE
    assert unreadable.distribution is DistributionPolicy.UNKNOWN
    assert not [i for i in external.issues if i.code is IssueCode.MISSING_INFORMATION]
    assert [i for i in unreadable.issues if i.code is IssueCode.MISSING_INFORMATION]


def test_an_external_table_is_still_enumerated_and_fully_identified():
    """Distinguished, not dropped. An external table is in scope for
    discovery; what changes is the migration path, which is a later decision
    than this one."""
    pool = source(responses(tables=[external_table_row("staging", "ExtRaw", 7)]))
    discovery = pool.discover_tables()

    assert discovery.table_count == 1
    key = discovery.tables[0].key
    assert key.logical_id == "sql://poolone/staging/ExtRaw"
    assert key.object_type is CatalogObjectType.TABLE
    assert pool.objects(CatalogObjectType.TABLE) == (key,)


def test_an_external_table_that_also_has_a_distribution_code_is_flagged():
    """A contradiction is surfaced, not silently resolved either way."""
    pool = source(
        responses(tables=[table_row("dbo", "Odd", 1, policy=2, external=True)])
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution is DistributionPolicy.NOT_APPLICABLE
    assert table.distribution_policy_code == 2
    assert any(i.code is IssueCode.UNSUPPORTED_CONSTRUCT for i in table.issues)


def test_an_external_table_still_reports_its_own_column_gaps():
    """The degradation model is unchanged for everything except distribution."""
    pool = source(responses(tables=[external_table_row("dbo", "ExtSales", 1)]))
    table = pool.discover_tables().tables[0]

    assert table.distribution is DistributionPolicy.NOT_APPLICABLE
    assert [i.code for i in table.issues] == [IssueCode.MISSING_INFORMATION]
    assert "no columns were returned" in table.issues[0].message


@pytest.mark.parametrize(
    "external,expected",
    [
        (True, DistributionPolicy.NOT_APPLICABLE),
        (False, DistributionPolicy.UNKNOWN),
        (None, DistributionPolicy.UNKNOWN),
    ],
)
def test_the_external_flag_decides_the_policy_in_the_pure_mapping_layer(
    external, expected
):
    """Including the absent case: a row with no ``is_external`` at all keeps
    the ``tables.v1`` reading, rather than being guessed either way."""
    row = {"distribution_policy": None}
    if external is not None:
        row["is_external"] = external

    policy, code, issues = distribution_from_row(row, "poolone.dbo.T")

    assert policy is expected
    assert code is None
    assert bool(issues) is (expected is DistributionPolicy.UNKNOWN)


def test_the_bit_column_is_accepted_as_an_integer_or_a_bool():
    """Drivers return bits either way; both must mean external."""
    for value in (True, 1):
        policy, _, issues = distribution_from_row(
            {"is_external": value, "distribution_policy": None}, "poolone.dbo.T"
        )
        assert policy is DistributionPolicy.NOT_APPLICABLE
        assert issues == ()

    for value in (False, 0):
        policy, _, issues = distribution_from_row(
            {"is_external": value, "distribution_policy": 2}, "poolone.dbo.T"
        )
        assert policy is DistributionPolicy.HASH


# --- 3c: distribution columns (the hash key) ---------------------------------
#
# sys.pdw_column_distribution_properties carries distribution_ordinal > 0 for
# the columns a hash-distributed table hashes on. The central rule under test
# is that an empty result is not one fact: for ROUND_ROBIN, REPLICATE and an
# external table it is the complete answer, and for HASH it is a gap.


def test_a_hash_table_exposes_its_distribution_column():
    pool = source(
        responses(
            tables=[table_row("dbo", "Trips", 1, policy=2)],
            columns=[column_row(1, 4, "CustomerKey")],
            distribution_columns=[distribution_column_row(1, 4, "CustomerKey")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution is DistributionPolicy.HASH
    assert table.distribution_column_names == ("CustomerKey",)
    assert table.distribution_columns[0].column_id == 4
    assert table.distribution_columns[0].ordinal == 1
    assert table.issues == ()


def test_a_multi_column_distribution_key_keeps_the_catalog_ordinal_order():
    """Ordinal order is the key's meaning, not a presentation choice, so it
    survives rows arriving in any order."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Trips", 1, policy=2)],
            columns=[column_row(1, i, n) for i, n in ((7, "c"), (3, "a"), (5, "b"))],
            distribution_columns=[
                distribution_column_row(1, 7, "c", ordinal=3),
                distribution_column_row(1, 3, "a", ordinal=1),
                distribution_column_row(1, 5, "b", ordinal=2),
            ],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution_column_names == ("a", "b", "c")
    assert [c.ordinal for c in table.distribution_columns] == [1, 2, 3]
    assert table.issues == ()


@pytest.mark.parametrize(
    "policy,expected",
    [(4, DistributionPolicy.ROUND_ROBIN), (3, DistributionPolicy.REPLICATE)],
)
def test_an_undistributed_table_has_no_distribution_columns_and_no_gap(
    policy, expected
):
    """Empty here is the complete answer, so it must not raise an issue."""
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1, policy=policy)],
            columns=[column_row(1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution is expected
    assert table.distribution_columns == ()
    assert table.distribution_column_names == ()
    assert table.issues == ()


def test_an_external_table_has_no_distribution_columns_and_no_gap():
    pool = source(
        responses(
            tables=[external_table_row("dbo", "ExtSales", 1)],
            columns=[column_row(1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution is DistributionPolicy.NOT_APPLICABLE
    assert table.distribution_columns == ()
    assert table.issues == ()


def test_a_hash_table_with_no_distribution_column_is_a_gap_not_an_empty_key():
    """A hash-distributed table has at least one distribution column, so an
    empty result means the key is missing rather than absent."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Trips", 1, policy=2)],
            columns=[column_row(1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution is DistributionPolicy.HASH
    assert table.distribution_columns == ()
    assert any(i.code is IssueCode.MISSING_INFORMATION for i in table.issues)
    assert any("hash-distributed" in i.message for i in table.issues)


def test_an_unreadable_distribution_column_catalog_is_missing_information():
    """Permissions, not emptiness. The run continues and says what it lost."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Trips", 1, policy=2)],
            columns=[column_row(1, 1, "a")],
            distribution_columns=PermissionError("SELECT permission was denied"),
        )
    )
    discovery = pool.discover_tables()
    table = discovery.tables[0]

    assert discovery.table_count == 1, "the run continues without the key"
    assert table.distribution is DistributionPolicy.HASH
    assert table.distribution_columns == ()
    assert any(
        i.code is IssueCode.MISSING_INFORMATION
        and "not a statement that it has none" in i.message
        for i in table.issues
    )
    assert any(
        "distribution columns could not be retrieved" in i.message
        for i in discovery.issues
    ), "the run-level cause is recorded once, not only per table"


def test_an_unreadable_catalog_costs_only_the_tables_that_have_a_key():
    """A round-robin table loses nothing when this catalog is denied, so
    claiming its key is unknown would invent a gap."""
    pool = source(
        responses(
            tables=[
                table_row("dbo", "Hashed", 1, policy=2),
                table_row("dbo", "Robin", 2, policy=4),
                external_table_row("dbo", "ExtSales", 3),
            ],
            columns=[column_row(i, 1, "a") for i in (1, 2, 3)],
            distribution_columns=PermissionError("SELECT permission was denied"),
        )
    )
    discovery = pool.discover_tables()

    assert discovery.table("dbo", "Hashed").issues, "the hash key is genuinely lost"
    assert discovery.table("dbo", "Robin").issues == ()
    assert discovery.table("dbo", "ExtSales").issues == ()


def test_a_distribution_key_on_an_undistributed_table_is_flagged_not_dropped():
    """A contradiction is surfaced and its evidence preserved, as elsewhere."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Robin", 1, policy=4)],
            columns=[column_row(1, 1, "a")],
            distribution_columns=[distribution_column_row(1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution is DistributionPolicy.ROUND_ROBIN
    assert table.distribution_column_names == ("a",), "the evidence survives"
    assert any(i.code is IssueCode.UNSUPPORTED_CONSTRUCT for i in table.issues)


def test_a_distribution_column_with_no_visible_name_keeps_its_ordinal_and_id():
    """The LEFT JOIN to sys.columns means the name can be absent while the
    identity of the key is not."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Trips", 1, policy=2)],
            distribution_columns=[distribution_column_row(1, 9, None)],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution_columns[0].column_id == 9
    assert table.distribution_columns[0].ordinal == 1
    assert table.distribution_columns[0].name is None
    assert table.distribution_column_names == ()
    assert any(
        "could not be resolved to a column name" in i.message for i in table.issues
    )


def test_distribution_columns_are_attributed_to_the_right_table():
    pool = source(
        responses(
            tables=[
                table_row("dbo", "A", 1, policy=2),
                table_row("staging", "B", 2, policy=2),
            ],
            columns=[column_row(1, 1, "a"), column_row(2, 1, "b")],
            distribution_columns=[
                distribution_column_row(2, 1, "b"),
                distribution_column_row(1, 1, "a"),
            ],
        )
    )
    discovery = pool.discover_tables()

    assert discovery.table("dbo", "A").distribution_column_names == ("a",)
    assert discovery.table("staging", "B").distribution_column_names == ("b",)


def test_an_unusable_distribution_column_row_is_counted_not_attributed():
    """A non-positive ordinal means the query's own predicate did not apply,
    so the row is not a distribution column whatever else it says."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Trips", 1, policy=2)],
            columns=[column_row(1, 1, "a")],
            distribution_columns=[
                distribution_column_row(1, 1, "a"),
                distribution_column_row(1, 2, "b", ordinal=0),
                distribution_column_row(None, 3, "c"),
            ],
        )
    )
    discovery = pool.discover_tables()

    assert discovery.tables[0].distribution_column_names == ("a",)
    assert any(
        "2 distribution column row(s) could not be identified" in i.message
        for i in discovery.issues
    )


def test_the_distribution_key_is_serializable_and_ordered():
    import json

    discovery = source(
        responses(
            tables=[table_row("dbo", "Trips", 1, policy=2)],
            columns=[column_row(1, 1, "a"), column_row(1, 2, "b")],
            distribution_columns=[
                distribution_column_row(1, 2, "b", ordinal=2),
                distribution_column_row(1, 1, "a", ordinal=1),
            ],
        )
    ).discover_tables()

    payload = json.loads(json.dumps(discovery.to_dict()))["tables"][0]

    assert payload["distribution_columns"] == [
        {"column_id": 1, "ordinal": 1, "name": "a"},
        {"column_id": 2, "ordinal": 2, "name": "b"},
    ]


def test_the_summary_prints_the_distribution_key(capsys):
    from discovery_agent.sql import __main__ as dev

    connector = FakeConnector(
        responses(
            tables=[
                table_row("dbo", "Trips", 1, policy=2),
                table_row("dbo", "Robin", 2, policy=4),
            ],
            columns=[column_row(1, 4, "CustomerKey"), column_row(2, 1, "a")],
            distribution_columns=[distribution_column_row(1, 4, "CustomerKey")],
        )
    )
    exit_code = dev.main(
        ["--server", SERVER, "--database", DATABASE], connector=connector
    )
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert "  distribution key: CustomerKey" in printed
    assert printed.count("distribution key:") == 1, (
        "a round-robin table has no key, so no line is printed for it"
    )


# --- 3d: indexes and storage structure ---------------------------------------
#
# sys.indexes gives one row per index; index_id 0 (heap) and index_id 1
# (clustered) are the base structure that says how the table's own rows are
# stored. ORDERED CLUSTERED COLUMNSTORE has no type code of its own — it is a
# type 5 index whose columns carry column_store_order_ordinal > 0 — so it can
# only be told from a plain CCI through sys.index_columns. That makes the two
# catalogs fail independently, which is what most of these tests are about.


def test_a_heap_table_reports_heap_storage():
    pool = source(
        responses(
            tables=[table_row("dbo", "Staging", 1, policy=4)],
            columns=[column_row(1, 1, "a")],
            indexes=[heap_row(1)],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.HEAP
    assert table.base_index.kind is IndexKind.HEAP
    assert table.base_index.index_id == 0
    assert table.base_index.name is None, "a heap's name is NULL in the catalog"
    assert table.secondary_indexes == ()
    assert table.issues == ()


def test_a_clustered_columnstore_table_reports_columnstore_storage():
    pool = source(
        responses(
            tables=[table_row("dbo", "Facts", 1, policy=2)],
            columns=[column_row(1, 1, "a")],
            distribution_columns=[distribution_column_row(1, 1, "a")],
            indexes=[index_row(1, 1, CLUSTERED_COLUMNSTORE, name="cci")],
            index_columns=[index_column_row(1, 1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.CLUSTERED_COLUMNSTORE
    assert table.base_index.kind is IndexKind.CLUSTERED_COLUMNSTORE
    assert table.base_index.type_code == 5
    assert table.base_index.type_desc == "CLUSTERED COLUMNSTORE"
    assert table.base_index.ordering_columns == ()
    assert table.issues == ()


def test_an_ordered_clustered_columnstore_is_told_apart_by_its_ordering_ordinals():
    """The only catalog evidence is column_store_order_ordinal > 0; the index
    type code is identical to a plain CCI."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Facts", 1, policy=2)],
            columns=[column_row(1, 1, "a"), column_row(1, 2, "b")],
            distribution_columns=[distribution_column_row(1, 1, "a")],
            indexes=[index_row(1, 1, CLUSTERED_COLUMNSTORE, name="occi")],
            index_columns=[
                index_column_row(1, 1, 2, "b", order_ordinal=2),
                index_column_row(1, 1, 1, "a", order_ordinal=1),
            ],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.ORDERED_CLUSTERED_COLUMNSTORE
    assert table.base_index.type_code == 5, "the type code is still a plain CCI"
    assert table.base_index.is_ordered_columnstore
    assert [c.name for c in table.base_index.ordering_columns] == ["a", "b"]
    assert table.issues == ()


def test_a_clustered_rowstore_index_reports_clustered_storage_with_key_order():
    pool = source(
        responses(
            tables=[table_row("dbo", "Dim", 1, policy=3)],
            columns=[column_row(1, 1, "a"), column_row(1, 2, "b")],
            indexes=[index_row(1, 1, CLUSTERED, name="ci")],
            index_columns=[
                index_column_row(1, 1, 2, "b", key_ordinal=2, descending=True),
                index_column_row(1, 1, 1, "a", key_ordinal=1),
            ],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.CLUSTERED_INDEX
    assert [c.name for c in table.base_index.key_columns] == ["a", "b"]
    assert table.base_index.key_columns[1].is_descending is True
    assert table.issues == ()


def test_a_nonclustered_index_is_secondary_and_does_not_change_storage():
    """A dedicated pool's PRIMARY KEY is NONCLUSTERED NOT ENFORCED, so this is
    the shape a primary key actually arrives in."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Dim", 1, policy=3)],
            columns=[column_row(1, 1, "a")],
            indexes=[
                heap_row(1),
                index_row(
                    1, 2, NONCLUSTERED, name="pk_dim", unique=True, primary_key=True
                ),
            ],
            index_columns=[index_column_row(1, 2, 1, "a", key_ordinal=1)],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.HEAP, "a secondary index is not storage"
    assert len(table.secondary_indexes) == 1
    secondary = table.secondary_indexes[0]
    assert secondary.kind is IndexKind.NONCLUSTERED
    assert secondary.name == "pk_dim"
    assert secondary.is_primary_key is True
    assert secondary.is_unique is True
    assert secondary.is_unique_constraint is False
    assert [c.name for c in secondary.key_columns] == ["a"]
    assert table.issues == ()


def test_a_unique_constraint_index_is_distinguished_from_a_primary_key():
    pool = source(
        responses(
            tables=[table_row("dbo", "Dim", 1, policy=3)],
            columns=[column_row(1, 1, "a")],
            indexes=[
                heap_row(1),
                index_row(
                    1, 2, NONCLUSTERED, name="uq", unique=True, unique_constraint=True
                ),
            ],
        )
    )
    secondary = pool.discover_tables().tables[0].secondary_indexes[0]

    assert secondary.is_unique is True
    assert secondary.is_unique_constraint is True
    assert secondary.is_primary_key is False


def test_an_external_table_reports_not_applicable_storage_and_no_gap():
    """An external table's rows are not stored in the pool, so an absent base
    index row is the correct answer rather than missing information."""
    pool = source(
        responses(
            tables=[external_table_row("dbo", "ExtSales", 1)],
            columns=[column_row(1, 1, "a")],
            indexes=[],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.NOT_APPLICABLE
    assert table.base_index is None
    assert table.issues == ()


def test_an_external_table_keeps_any_index_rows_as_evidence_without_inferring():
    """If the pool does report index rows for an external table they are
    preserved, but nothing about ordinary table storage is concluded."""
    pool = source(
        responses(
            tables=[external_table_row("dbo", "ExtSales", 1)],
            columns=[column_row(1, 1, "a")],
            indexes=[heap_row(1)],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.NOT_APPLICABLE
    assert [i.index_id for i in table.indexes] == [0]
    assert table.issues == ()


def test_a_missing_base_index_row_is_a_gap_not_a_heap():
    """Every ordinary table has a heap or a clustered index, so nothing coming
    back means the row was not visible — not that the table has no storage."""
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1, policy=4)],
            columns=[column_row(1, 1, "a")],
            indexes=[],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.UNKNOWN
    assert table.base_index is None
    assert any(i.code is IssueCode.MISSING_INFORMATION for i in table.issues)
    assert any("missing rather than absent" in i.message for i in table.issues)


def test_an_unreadable_index_catalog_is_missing_information_not_a_heap():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1, policy=4)],
            columns=[column_row(1, 1, "a")],
            indexes=PermissionError("SELECT permission was denied"),
        )
    )
    discovery = pool.discover_tables()
    table = discovery.tables[0]

    assert table.storage is TableStorage.UNKNOWN
    assert any(
        "this is not a statement that it is a heap" in i.message for i in table.issues
    )
    assert any(
        "indexes could not be retrieved" in i.message for i in discovery.issues
    ), "the run-level cause is recorded once"


def test_an_unreadable_index_catalog_costs_nothing_for_an_external_table():
    pool = source(
        responses(
            tables=[external_table_row("dbo", "ExtSales", 1)],
            columns=[column_row(1, 1, "a")],
            indexes=PermissionError("SELECT permission was denied"),
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.NOT_APPLICABLE
    assert table.issues == ()


def test_unreadable_index_columns_still_leave_the_storage_structure_known():
    """The two index catalogs fail independently: losing index columns must not
    cost the heap-versus-columnstore answer."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Staging", 1, policy=4)],
            columns=[column_row(1, 1, "a")],
            indexes=[heap_row(1)],
            index_columns=PermissionError("SELECT permission was denied"),
        )
    )
    discovery = pool.discover_tables()
    table = discovery.tables[0]

    assert table.storage is TableStorage.HEAP
    assert table.issues == (), "a heap has no index columns to lose"
    assert any(
        "index columns could not be retrieved" in i.message for i in discovery.issues
    )


def test_unreadable_index_columns_leave_columnstore_ordering_undetermined():
    """The case the ORDERED refinement depends on. Reporting a plain CCI here
    would assert 'not ordered', which the catalog did not say."""
    pool = source(
        responses(
            tables=[table_row("dbo", "Facts", 1, policy=2)],
            columns=[column_row(1, 1, "a")],
            distribution_columns=[distribution_column_row(1, 1, "a")],
            indexes=[index_row(1, 1, CLUSTERED_COLUMNSTORE, name="cci")],
            index_columns=PermissionError("SELECT permission was denied"),
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.CLUSTERED_COLUMNSTORE
    assert any(
        i.code is IssueCode.MISSING_INFORMATION
        and "whether this clustered columnstore index is ORDERED" in i.message
        for i in table.issues
    )


def test_an_unmapped_index_type_keeps_the_catalog_description():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1, policy=4)],
            columns=[column_row(1, 1, "a")],
            indexes=[index_row(1, 1, 97, name="odd", type_desc="SOMETHING NEW")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.storage is TableStorage.UNKNOWN
    assert table.base_index.kind is IndexKind.UNKNOWN
    assert table.base_index.type_code == 97
    assert table.base_index.type_desc == "SOMETHING NEW"
    assert any(i.code is IssueCode.UNSUPPORTED_CONSTRUCT for i in table.issues)


def test_indexes_and_their_columns_are_attributed_to_the_right_table():
    """index_id is only unique within a table, so grouping must be two-part."""
    pool = source(
        responses(
            tables=[
                table_row("dbo", "A", 1, policy=4),
                table_row("staging", "B", 2, policy=4),
            ],
            columns=[column_row(1, 1, "a"), column_row(2, 1, "b")],
            indexes=[
                index_row(1, 1, CLUSTERED, name="ci_a"),
                index_row(2, 1, CLUSTERED, name="ci_b"),
            ],
            index_columns=[
                index_column_row(2, 1, 1, "b", key_ordinal=1),
                index_column_row(1, 1, 1, "a", key_ordinal=1),
            ],
        )
    )
    discovery = pool.discover_tables()

    a = discovery.table("dbo", "A").base_index
    b = discovery.table("staging", "B").base_index
    assert a.name == "ci_a" and [c.name for c in a.key_columns] == ["a"]
    assert b.name == "ci_b" and [c.name for c in b.key_columns] == ["b"]


def test_included_columns_are_not_reported_as_key_columns():
    """key_ordinal is 0 for an included column, and for every columnstore
    column, so it must not be read as position zero of the key."""
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1, policy=4)],
            columns=[column_row(1, 1, "a"), column_row(1, 2, "b")],
            indexes=[heap_row(1), index_row(1, 2, NONCLUSTERED, name="ix")],
            index_columns=[
                index_column_row(1, 2, 1, "a", key_ordinal=1),
                index_column_row(1, 2, 2, "b", key_ordinal=0),
            ],
        )
    )
    index = pool.discover_tables().tables[0].secondary_indexes[0]

    assert [c.name for c in index.key_columns] == ["a"]
    assert len(index.columns) == 2, "the included column is still recorded"


def test_an_unusable_index_row_is_counted_not_attributed():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1, policy=4)],
            columns=[column_row(1, 1, "a")],
            indexes=[heap_row(1), index_row(None, 2, NONCLUSTERED, name="orphan")],
            index_columns=[index_column_row(1, None, 1, "a")],
        )
    )
    discovery = pool.discover_tables()

    assert discovery.tables[0].storage is TableStorage.HEAP
    assert any(
        "1 index row(s) could not be identified" in i.message
        for i in discovery.issues
    )
    assert any(
        "1 index column row(s) could not be identified" in i.message
        for i in discovery.issues
    )


def test_the_storage_structure_is_serializable_with_its_raw_evidence():
    import json

    discovery = source(
        responses(
            tables=[table_row("dbo", "Facts", 1, policy=2)],
            columns=[column_row(1, 1, "a")],
            distribution_columns=[distribution_column_row(1, 1, "a")],
            indexes=[index_row(1, 1, CLUSTERED_COLUMNSTORE, name="occi")],
            index_columns=[index_column_row(1, 1, 1, "a", order_ordinal=1)],
        )
    ).discover_tables()

    payload = json.loads(json.dumps(discovery.to_dict()))["tables"][0]

    assert payload["storage"] == "ordered_clustered_columnstore"
    assert payload["indexes"][0]["type_code"] == 5
    assert payload["indexes"][0]["type_desc"] == "CLUSTERED COLUMNSTORE"
    assert payload["indexes"][0]["columns"][0]["column_store_order_ordinal"] == 1


def test_the_summary_prints_storage_ordering_and_secondary_indexes(capsys):
    from discovery_agent.sql import __main__ as dev

    connector = FakeConnector(
        responses(
            tables=[
                table_row("dbo", "Facts", 1, policy=2),
                table_row("dbo", "Dim", 2, policy=3),
            ],
            columns=[column_row(1, 1, "a"), column_row(2, 1, "b")],
            distribution_columns=[distribution_column_row(1, 1, "a")],
            indexes=[
                index_row(1, 1, CLUSTERED_COLUMNSTORE, name="occi"),
                heap_row(2),
                index_row(2, 2, NONCLUSTERED, name="pk_dim", unique=True,
                          primary_key=True),
            ],
            index_columns=[index_column_row(1, 1, 1, "a", order_ordinal=1)],
        )
    )
    exit_code = dev.main(
        ["--server", SERVER, "--database", DATABASE], connector=connector
    )
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert "  storage: ordered_clustered_columnstore" in printed
    assert "  order by: a" in printed
    assert "  storage: heap" in printed
    assert "  index: pk_dim [nonclustered] (primary key)" in printed, (
        "a primary key is unique by definition, so the redundant flag is "
        "suppressed rather than printed alongside it"
    )


# --- 4: multiple schemas and duplicate names ---------------------------------


def test_multiple_tables_across_multiple_schemas():
    pool = source(
        responses(
            tables=[
                table_row("dbo", "TripsData", 100),
                table_row("dbo", "FaresData", 101, policy=3),
                table_row("staging", "TripsData", 200, policy=4),
            ],
            columns=[column_row(100, 1, "a"), column_row(101, 1, "b"), column_row(200, 1, "c")],
        )
    )

    discovery = pool.discover_tables()

    assert discovery.table_count == 3
    assert discovery.schemas == ("dbo", "staging")


def test_the_same_table_name_in_two_schemas_does_not_collide():
    pool = source(
        responses(
            tables=[
                table_row("dbo", "TripsData", 100, policy=2),
                table_row("staging", "TripsData", 200, policy=4),
            ],
            columns=[
                column_row(100, 1, "production_only"),
                column_row(200, 1, "staging_only"),
                column_row(200, 2, "extra"),
            ],
        )
    )
    discovery = pool.discover_tables()

    production = discovery.table("dbo", "TripsData")
    staging = discovery.table("staging", "TripsData")

    assert production is not staging
    assert production.key.logical_id == "sql://poolone/dbo/TripsData"
    assert staging.key.logical_id == "sql://poolone/staging/TripsData"
    assert production.distribution is DistributionPolicy.HASH
    assert staging.distribution is DistributionPolicy.ROUND_ROBIN
    assert [c.name for c in production.columns] == ["production_only"]
    assert [c.name for c in staging.columns] == ["staging_only", "extra"]


def test_identity_needs_every_part():
    for missing in ({"database": ""}, {"schema": ""}, {"name": ""}):
        fields = {"database": "d", "schema": "s", "name": "n"}
        fields.update(missing)
        with pytest.raises(ValueError):
            SqlObjectKey(**fields)


def test_logical_id_matches_the_record_scheme():
    key = SqlObjectKey(database="poolone", schema="dbo", name="TripsData")

    assert key.logical_id == "sql://poolone/dbo/TripsData"
    assert key.qualified_name == "poolone.dbo.TripsData"


# --- 5: missing and NULL catalog fields --------------------------------------


def test_a_null_distribution_is_unknown_with_an_issue_not_a_default():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1, policy=None)],
            columns=[column_row(1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution is DistributionPolicy.UNKNOWN
    assert table.distribution_policy_code is None
    assert any(i.code is IssueCode.MISSING_INFORMATION for i in table.issues)


def test_an_unrecognised_distribution_code_keeps_the_raw_value():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1, policy=97)],
            columns=[column_row(1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.distribution is DistributionPolicy.UNKNOWN
    assert table.distribution_policy_code == 97
    assert any(i.code is IssueCode.UNSUPPORTED_CONSTRUCT for i in table.issues)


def test_null_dates_stay_none():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1, created=None, modified=None)],
            columns=[column_row(1, 1, "a")],
        )
    )
    table = pool.discover_tables().tables[0]

    assert table.create_date is None
    assert table.modify_date is None


def test_null_column_attributes_stay_none():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1)],
            columns=[
                column_row(
                    1, 1, "c", data_type=None, max_length=None, precision=None,
                    scale=None, nullable=None, identity=None,
                )
            ],
        )
    )
    column = pool.discover_tables().tables[0].columns[0]

    assert column.data_type is None
    assert column.max_length is None
    assert column.is_nullable is None
    assert column.is_identity is None


def test_a_table_row_without_a_name_is_reported_not_invented():
    pool = source(
        responses(
            tables=[table_row("dbo", None, 1), table_row("dbo", "Real", 2)],
            columns=[column_row(2, 1, "a")],
        )
    )
    discovery = pool.discover_tables()

    assert [t.key.name for t in discovery.tables] == ["Real"]
    assert any(i.code is IssueCode.MALFORMED_ARTIFACT for i in discovery.issues)


def test_a_column_row_that_cannot_identify_itself_is_counted():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1)],
            columns=[column_row(1, 1, "good"), column_row(1, None, None)],
        )
    )
    discovery = pool.discover_tables()

    assert [c.name for c in discovery.tables[0].columns] == ["good"]
    assert any("could not be identified" in i.message for i in discovery.issues)


def test_bit_columns_are_accepted_as_ints_or_bools():
    assert column_from_row({"column_id": 1, "column_name": "c", "is_nullable": 1}).is_nullable is True
    assert column_from_row({"column_id": 1, "column_name": "c", "is_nullable": 0}).is_nullable is False
    assert column_from_row({"column_id": 1, "column_name": "c", "is_nullable": True}).is_nullable is True


# --- 6: permissions and catalog visibility -----------------------------------


def test_no_visible_tables_is_reported_as_ambiguous_not_as_empty():
    """The security-trimming problem: absence of rows is not absence of tables."""
    discovery = source(responses(tables=[], columns=[])).discover_tables()

    assert discovery.table_count == 0
    issue = discovery.issues[0]
    assert issue.code is IssueCode.MISSING_INFORMATION
    assert "security-trimmed" in issue.message
    assert "cannot be distinguished" in issue.message
    assert not discovery.is_complete


def test_a_table_with_no_visible_columns_says_so():
    pool = source(responses(tables=[table_row("dbo", "T", 1)], columns=[]))
    table = pool.discover_tables().tables[0]

    assert table.columns == ()
    assert any(
        "may not have visibility of its columns" in i.message for i in table.issues
    )


def test_a_failing_column_query_degrades_rather_than_fabricating():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1)],
            columns=PermissionError("The SELECT permission was denied"),
        )
    )
    discovery = pool.discover_tables()
    table = discovery.tables[0]

    assert discovery.table_count == 1, "tables are still discovered"
    assert table.columns == ()
    assert any("is not a statement that the table has no columns" in i.message
               for i in table.issues)
    assert any(i.code is IssueCode.MISSING_INFORMATION for i in discovery.issues)


def test_a_failing_table_query_raises_rather_than_reporting_an_empty_database():
    pool = source(responses(tables=PermissionError("denied")))

    with pytest.raises(SqlDiscoveryError, match="could not enumerate tables"):
        pool.discover_tables()


def test_an_unreadable_database_query_falls_back_to_configuration():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1)],
            columns=[column_row(1, 1, "a")],
            database=PermissionError("denied"),
        )
    )
    discovery = pool.discover_tables()

    assert discovery.database.name == DATABASE, "we still know what we asked for"
    assert discovery.database.collation is None


def test_an_unreadable_principal_query_is_not_fatal():
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1)],
            columns=[column_row(1, 1, "a")],
            principal=PermissionError("denied"),
        )
    )
    discovery = pool.discover_tables()

    assert discovery.principal == CatalogPrincipal()
    assert discovery.principal.described == "unknown principal"


def test_a_complete_run_reports_no_issues():
    """A hash table carries its distribution key here, because that is what a
    complete hash table looks like: without the key the run genuinely does
    have a gap, and this test would be asserting the wrong thing."""
    pool = source(
        responses(
            tables=[table_row("dbo", "T", 1)],
            columns=[column_row(1, 1, "a")],
            distribution_columns=[distribution_column_row(1, 1, "a")],
        )
    )
    discovery = pool.discover_tables()

    assert discovery.is_complete
    assert discovery.all_issues == ()


# --- 7: provenance -----------------------------------------------------------


def test_provenance_identifies_the_server_and_database():
    discovery = source(
        responses(tables=[table_row("dbo", "T", 1)], columns=[column_row(1, 1, "a")])
    ).discover_tables()

    provenance = discovery.provenance
    assert provenance.source_type is SourceType.SQL
    assert provenance.source_format == SQL_SOURCE_FORMAT
    assert provenance.resource_id == f"{SERVER}/{DATABASE}"
    assert provenance.source_path is None, "a catalog object has no path"


def test_object_provenance_identifies_the_object():
    pool = source(responses())
    key = SqlObjectKey(database=DATABASE, schema="dbo", name="TripsData")

    assert pool.provenance_for(key).resource_id == (
        f"{SERVER}/{DATABASE}/dbo/TripsData"
    )


def test_two_pools_with_the_same_table_name_have_different_provenance():
    first = source(responses(), database="poolone")
    second = source(responses(), database="pooltwo")
    key_one = SqlObjectKey(database="poolone", schema="dbo", name="Customer")
    key_two = SqlObjectKey(database="pooltwo", schema="dbo", name="Customer")

    assert first.provenance_for(key_one).resource_id != (
        second.provenance_for(key_two).resource_id
    )
    assert key_one.logical_id != key_two.logical_id


# --- 8: enumeration contract -------------------------------------------------


def test_objects_enumerates_table_keys():
    pool = source(
        responses(tables=[table_row("dbo", "A", 1), table_row("staging", "B", 2)])
    )

    keys = pool.objects(CatalogObjectType.TABLE)

    assert [k.qualified_name for k in keys] == [
        "poolone.dbo.A",
        "poolone.staging.B",
    ]


@pytest.mark.parametrize(
    "object_type, expected",
    [
        (CatalogObjectType.VIEW, "poolone.dbo.vSales"),
        (CatalogObjectType.PROCEDURE, "poolone.dbo.pLoad"),
    ],
)
def test_every_object_type_can_be_enumerated(object_type, expected):
    """Views and procedures enumerate like tables: identity, nothing more."""
    pool = source(
        responses(
            views=[view_row("dbo", "vSales", 20)],
            procedures=[procedure_row("dbo", "pLoad", 30)],
        )
    )

    keys = pool.objects(object_type)

    assert [k.qualified_name for k in keys] == [expected]
    assert {k.object_type for k in keys} == {object_type}


def test_an_enumeration_that_fails_raises_rather_than_returning_empty():
    """An empty tuple would read as "this database has none"."""
    pool = source(responses(views=CatalogQueryError("SELECT permission denied")))

    with pytest.raises(CatalogQueryError):
        pool.objects(CatalogObjectType.VIEW)


def test_the_connection_is_opened_once_and_closed():
    connector = FakeConnector(
        responses(tables=[table_row("dbo", "T", 1)], columns=[column_row(1, 1, "a")])
    )
    with DedicatedPoolSource(config(), connector) as pool:
        pool.discover_tables()

    assert len(connector.connections) == 1
    assert connector.connections[0].closed


def test_closing_twice_is_safe():
    pool = source(responses())
    pool.close()
    pool.close()


# --- 9: configuration and authentication -------------------------------------


def test_the_only_authentication_is_an_entra_access_token():
    """One mechanism, served by the central credential.

    The browser-based ``interactive`` mechanism was implemented here once and
    was removed: it signed in separately from the rest of the application, so
    a run could hold two identities without saying so.
    """
    authentication = authentication_for(
        EntraAuthMethod.ACCESS_TOKEN, token_provider=lambda: "a-token"
    )

    assert isinstance(authentication, AccessTokenAuthentication)
    assert authentication.describe() == "Entra ID (access_token)"


@pytest.mark.parametrize(
    "method",
    [
        EntraAuthMethod.INTERACTIVE,
        EntraAuthMethod.MANAGED_IDENTITY,
        EntraAuthMethod.SERVICE_PRINCIPAL,
    ],
)
def test_every_other_mechanism_is_refused_rather_than_quietly_added(method):
    """Including ``interactive``: a second sign-in path is the thing this
    module must not grow back."""
    with pytest.raises(SqlAuthenticationError, match="not available"):
        authentication_for(method)


def test_this_package_cannot_acquire_a_credential_of_its_own():
    """The structural guarantee. There is nothing here that could call an
    identity library, and nothing that could hold what one returned."""
    import discovery_agent.sql.auth as auth_module

    source = inspect.getsource(auth_module)

    assert "azure.identity" not in source
    assert "AzureCliCredential" not in source


def test_authentication_has_nowhere_to_hold_a_credential():
    authentication = authentication_for(
        EntraAuthMethod.ACCESS_TOKEN, token_provider=lambda: "a-token"
    )

    for forbidden in ("password", "secret", "token", "client_secret"):
        assert not hasattr(authentication, forbidden)
    assert "a-token" not in repr(authentication)


def test_the_connection_string_carries_no_credential():
    authentication = authentication_for(
        EntraAuthMethod.ACCESS_TOKEN, token_provider=lambda: "a-token"
    )
    connection_string = build_connection_string(config(), authentication)

    assert "a-token" not in connection_string
    assert f"Server=tcp:{SERVER},1433" in connection_string
    assert f"Database={DATABASE}" in connection_string
    assert "Encrypt=yes" in connection_string
    for forbidden in ("PWD=", "Password", "AccessToken", "Secret"):
        assert forbidden not in connection_string


def test_a_delimiter_in_a_connection_setting_is_refused_not_escaped():
    injected = config(database="poolone;Uid=admin")
    authentication = authentication_for(
        EntraAuthMethod.ACCESS_TOKEN, token_provider=lambda: "a-token"
    )

    with pytest.raises(SqlConnectionError, match="not valid in a connection string"):
        build_connection_string(injected, authentication)


def test_encryption_cannot_be_turned_off():
    with pytest.raises(ConfigError, match="encryption cannot be disabled"):
        SqlConnectionConfig(server=SERVER, database=DATABASE, encrypt=False).validate()


@pytest.mark.parametrize(
    "overrides", [{"server": ""}, {"database": ""}, {"port": 0}, {"driver": ""}]
)
def test_incomplete_configuration_is_rejected(overrides):
    fields = {"server": SERVER, "database": DATABASE}
    fields.update(overrides)
    with pytest.raises(ConfigError):
        SqlConnectionConfig(**fields).validate()


def test_configuration_describes_itself_without_secrets():
    described = config().safe_description()

    assert described == f"server={SERVER} database={DATABASE} auth=access_token"


def test_configuration_comes_from_arguments_or_environment(monkeypatch):
    monkeypatch.setenv("SYNAPSE_SQL_SERVER", "env-host")
    monkeypatch.setenv("SYNAPSE_SQL_DATABASE", "env-db")

    from_env = config_from_environment()
    from_args = config_from_environment(server="arg-host", database="arg-db")

    assert (from_env.server, from_env.database) == ("env-host", "env-db")
    assert (from_args.server, from_args.database) == ("arg-host", "arg-db")


def test_missing_configuration_names_the_variable(monkeypatch):
    monkeypatch.delenv("SYNAPSE_SQL_SERVER", raising=False)
    monkeypatch.delenv("SYNAPSE_SQL_DATABASE", raising=False)

    with pytest.raises(ConfigError, match="SYNAPSE_SQL_SERVER"):
        config_from_environment()


def test_endpoint_configuration_cannot_choose_an_authentication_mechanism():
    """Which identity connects is the connection layer's decision.

    ``config_from_environment`` used to take an ``authentication`` argument,
    which is how a second sign-in path grew. It takes an endpoint and a
    database now, and nothing else.
    """
    parameters = inspect.signature(config_from_environment).parameters

    assert set(parameters) == {"server", "database"}


# --- 10: the dev command -----------------------------------------------------


def test_the_summary_prints_what_was_discovered(capsys, monkeypatch):
    from discovery_agent.sql import __main__ as dev

    connector = FakeConnector(
        responses(
            tables=[table_row("dbo", "TripsData", 100), table_row("staging", "Raw", 200, policy=4)],
            columns=[
                column_row(100, 1, "trip_id"),
                column_row(100, 2, "vendor"),
                column_row(200, 1, "payload"),
            ],
        )
    )
    exit_code = dev.main(
        ["--server", SERVER, "--database", DATABASE], connector=connector
    )
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert f"database: {DATABASE}" in printed
    assert "tables discovered: 2" in printed
    assert "dbo.TripsData" in printed
    assert "  columns: 2" in printed
    assert "  distribution: hash" in printed
    assert "staging.Raw" in printed
    assert "  distribution: round_robin" in printed
    assert "external" not in printed, (
        "no table here is external; the marker is printed only when it is true"
    )


def test_the_summary_marks_external_tables_and_reports_no_false_gap(capsys):
    from discovery_agent.sql import __main__ as dev

    connector = FakeConnector(
        responses(
            tables=[
                table_row("dbo", "TripsData", 100),
                external_table_row("dbo", "ExtSales", 200),
            ],
            columns=[column_row(100, 1, "a"), column_row(200, 1, "b")],
            distribution_columns=[distribution_column_row(100, 1, "a")],
        )
    )
    exit_code = dev.main(
        ["--server", SERVER, "--database", DATABASE], connector=connector
    )
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert "  external: yes" in printed
    assert "  distribution: not_applicable" in printed
    assert "may not be readable by the connected principal" not in printed
    assert "note: this run has gaps" not in printed, (
        "an external table is not a gap; a clean run must still read as clean"
    )


def test_the_summary_never_prints_a_connection_string_or_credential(capsys):
    from discovery_agent.sql import __main__ as dev

    connector = FakeConnector(
        responses(tables=[table_row("dbo", "T", 1)], columns=[column_row(1, 1, "a")])
    )
    dev.main(["--server", SERVER, "--database", DATABASE], connector=connector)
    printed = capsys.readouterr().out

    for forbidden in ("Driver=", "Encrypt=", "PWD", "Authentication=", "token"):
        assert forbidden not in printed


def test_the_summary_flags_an_incomplete_run(capsys):
    from discovery_agent.sql import __main__ as dev

    connector = FakeConnector(responses(tables=[], columns=[]))
    dev.main(["--server", SERVER, "--database", DATABASE], connector=connector)
    printed = capsys.readouterr().out

    assert "tables discovered: 0" in printed
    assert "this run has gaps" in printed
    assert "security-trimmed" in printed


def test_a_configuration_error_exits_non_zero(capsys, monkeypatch):
    from discovery_agent.sql import __main__ as dev

    monkeypatch.delenv("SYNAPSE_SQL_SERVER", raising=False)
    monkeypatch.delenv("SYNAPSE_SQL_DATABASE", raising=False)

    assert dev.main([]) == 2
    assert "SYNAPSE_SQL_SERVER" in capsys.readouterr().err


# --- 11: serialization -------------------------------------------------------


def test_the_discovery_is_json_serializable():
    import json

    discovery = source(
        responses(
            tables=[table_row("dbo", "T", 1, policy=None)],
            columns=[column_row(1, 1, "a")],
        )
    ).discover_tables()

    json.dumps(discovery.to_dict())
    json.dumps(discovery.summary())

    payload = discovery.to_dict()["tables"][0]
    assert payload["logical_id"] == "sql://poolone/dbo/T"
    assert payload["distribution"] == "unknown"
    assert payload["is_external"] is False


def test_an_external_table_serializes_its_kind_and_its_policy():
    import json

    discovery = source(
        responses(tables=[external_table_row("dbo", "ExtSales", 1)])
    ).discover_tables()

    payload = json.loads(json.dumps(discovery.to_dict()))["tables"][0]

    assert payload["is_external"] is True
    assert payload["distribution"] == "not_applicable"
    assert payload["distribution_policy_code"] is None


def test_mapping_functions_work_on_plain_dicts():
    """The mapping layer is pure, so it needs no source at all."""
    tables, issues = build_tables(
        database="poolone",
        table_rows=[{"schema_name": "dbo", "table_name": "T", "object_id": 7,
                     "distribution_policy": 2}],
        column_rows=[{"object_id": 7, "column_id": 1, "column_name": "a"}],
    )

    assert issues == ()
    assert tables[0].key == table_key_from_row(
        "poolone", {"schema_name": "dbo", "table_name": "T", "object_id": 7}
    )
    assert tables[0].distribution is DistributionPolicy.HASH


# --- views and stored procedures ---------------------------------------------
#
# The two remaining SQL-primary P0 artifacts. Neither has a Git or a Synapse
# Artifacts representation, so the catalog is the only source there is.


VIEW_BODY = "CREATE VIEW dbo.vSales AS SELECT t.Amount FROM dbo.Sales AS t"
PROCEDURE_BODY = (
    "CREATE PROCEDURE dbo.pLoad @day date AS\n"
    "INSERT INTO dbo.Sales SELECT * FROM staging.SalesRaw WHERE d = @day"
)


def view_source(**kwargs):
    kwargs.setdefault("views", [view_row("dbo", "vSales", 20)])
    kwargs.setdefault("modules", [module_row(20, VIEW_BODY)])
    return source(responses(**kwargs))


def procedure_source(**kwargs):
    kwargs.setdefault("procedures", [procedure_row("dbo", "pLoad", 30)])
    kwargs.setdefault("modules", [module_row(30, PROCEDURE_BODY)])
    kwargs.setdefault("parameters", [parameter_row(30, 1, "@day", "date")])
    return source(responses(**kwargs))


def test_a_view_is_discovered_with_its_identity_and_dates():
    views, issues = view_source().discover_views()

    view = views[0]
    assert view.key.qualified_name == "poolone.dbo.vSales"
    assert view.key.object_type is CatalogObjectType.VIEW
    assert view.key.object_id == 20
    assert view.create_date == CREATED.isoformat()
    assert view.modify_date == MODIFIED.isoformat()
    assert issues == ()


def test_a_view_keeps_its_definition_exactly_as_the_catalog_returned_it():
    """Never reformatted: a migration needs the original text."""
    views, _ = view_source().discover_views()

    definition = views[0].definition
    assert definition.state is DefinitionState.AVAILABLE
    assert definition.text == VIEW_BODY
    assert definition.uses_ansi_nulls is True
    assert views[0].has_definition


def test_a_view_records_the_objects_its_body_names():
    views, _ = view_source().discover_views()

    assert "dbo.Sales" in views[0].referenced_names


def test_an_encrypted_view_is_opaque_and_never_a_view_without_a_definition():
    """The distinction the whole DefinitionState enum exists for."""
    views, _ = view_source(modules=[module_row(20, None)]).discover_views()

    view = views[0]
    assert view.definition.state is DefinitionState.OPAQUE
    assert view.definition.text is None
    assert view.is_opaque
    assert [i.code for i in view.issues] == [IssueCode.OPAQUE_DEFINITION]
    assert "withheld" in view.issues[0].message
    # The view is still discovered. Its absence would be the real loss.
    assert view.key.qualified_name == "poolone.dbo.vSales"


def test_an_opaque_view_makes_no_dependency_claims():
    """No body means no observations -- not an empty set of them."""
    views, _ = view_source(modules=[module_row(20, None)]).discover_views()

    assert views[0].referenced_objects == ()
    assert views[0].referenced_names == ()


def test_an_unreadable_module_catalog_is_distinguished_from_an_encrypted_one():
    views, issues = view_source(
        modules=CatalogQueryError("SELECT permission denied on sys.sql_modules")
    ).discover_views()

    assert views[0].definition.state is DefinitionState.UNAVAILABLE
    assert [i.code for i in issues] == [IssueCode.MISSING_INFORMATION]
    assert [i.code for i in views[0].issues] == [IssueCode.MISSING_INFORMATION]


def test_a_view_with_no_module_row_is_absent_not_opaque():
    views, _ = view_source(modules=[]).discover_views()

    assert views[0].definition.state is DefinitionState.ABSENT


def test_view_enumeration_failing_raises_rather_than_reporting_none():
    pool = view_source(views=CatalogQueryError("SELECT permission denied"))

    with pytest.raises(SqlDiscoveryError, match="could not enumerate views"):
        pool.discover_views()


def test_a_procedure_is_discovered_with_its_signature():
    procedures, issues = procedure_source().discover_procedures()

    procedure = procedures[0]
    assert procedure.key.qualified_name == "poolone.dbo.pLoad"
    assert procedure.key.object_type is CatalogObjectType.PROCEDURE
    assert procedure.type_code == "P"
    assert procedure.parameter_count == 1
    assert procedure.parameters[0].name == "@day"
    assert procedure.parameters[0].data_type == "date"
    assert procedure.parameters[0].direction == "in"
    assert issues == ()


def test_a_procedure_return_value_is_kept_and_not_counted_as_a_parameter():
    procedures, _ = procedure_source(
        parameters=[
            parameter_row(30, 0, ""),
            parameter_row(30, 1, "@day", "date"),
        ]
    ).discover_procedures()

    procedure = procedures[0]
    assert len(procedure.parameters) == 2
    assert procedure.parameters[0].is_return_value
    assert procedure.parameters[0].direction == "return"
    assert procedure.parameter_count == 1


def test_an_output_parameter_reports_its_direction():
    procedures, _ = procedure_source(
        parameters=[parameter_row(30, 1, "@rows", "int", output=True)]
    ).discover_procedures()

    assert procedures[0].output_parameters[0].name == "@rows"
    assert procedures[0].parameters[0].direction == "out"


def test_a_procedure_records_the_objects_its_body_names():
    procedures, _ = procedure_source().discover_procedures()

    names = procedures[0].referenced_names
    assert "dbo.Sales" in names
    assert "staging.SalesRaw" in names


def test_dynamic_sql_is_recorded_as_an_observation_and_never_evaluated():
    body = "CREATE PROCEDURE dbo.pLoad AS EXEC sp_executesql @statement"
    procedures, _ = procedure_source(
        modules=[module_row(30, body)]
    ).discover_procedures()

    procedure = procedures[0]
    assert procedure.uses_dynamic_sql
    codes = [i.code for i in procedure.issues]
    assert IssueCode.UNSUPPORTED_CONSTRUCT in codes
    message = next(
        i.message for i in procedure.issues if i.code is IssueCode.UNSUPPORTED_CONSTRUCT
    )
    assert "not statically determinable" in message


def test_unreadable_parameters_do_not_make_a_procedure_look_argumentless():
    procedures, issues = procedure_source(
        parameters=CatalogQueryError("SELECT permission denied on sys.parameters")
    ).discover_procedures()

    procedure = procedures[0]
    assert procedure.parameters == ()
    assert "is not known to take no parameters" in " ".join(
        i.message for i in procedure.issues
    )
    assert any(i.code is IssueCode.MISSING_INFORMATION for i in issues)


def test_procedure_enumeration_failing_raises_rather_than_reporting_none():
    pool = procedure_source(procedures=CatalogQueryError("denied"))

    with pytest.raises(SqlDiscoveryError, match="could not enumerate procedures"):
        pool.discover_procedures()


# --- the composed discovery --------------------------------------------------


def full_responses(**overrides):
    base = dict(
        # Round-robin: this fixture is about views and procedures, and a
        # hash-distributed table would acquire a distribution-key gap that has
        # nothing to do with what these tests assert.
        tables=[table_row("dbo", "Sales", 10, policy=4)],
        columns=[column_row(10, 1, "Amount")],
        views=[view_row("dbo", "vSales", 20)],
        procedures=[procedure_row("dbo", "pLoad", 30)],
        modules=[module_row(20, VIEW_BODY), module_row(30, PROCEDURE_BODY)],
        parameters=[parameter_row(30, 1, "@day", "date")],
    )
    base.update(overrides)
    return responses(**base)


def test_discover_returns_all_three_sql_primary_artifacts():
    discovery = source(full_responses()).discover()

    assert discovery.table_count == 1
    assert discovery.view_count == 1
    assert discovery.procedure_count == 1
    assert discovery.object_count == 3
    assert discovery.is_complete


def test_discover_leaves_the_table_path_exactly_as_it_was():
    """Views and procedures are additions, not a rewrite of table discovery."""
    only_tables = source(full_responses()).discover_tables()
    everything = source(full_responses()).discover()

    assert everything.tables == only_tables.tables
    assert everything.database == only_tables.database
    assert everything.principal == only_tables.principal


def test_discover_names_every_schema_holding_any_object_kind():
    discovery = source(
        full_responses(
            views=[view_row("reporting", "vSales", 20)],
            procedures=[procedure_row("staging", "pLoad", 30)],
        )
    ).discover()

    assert discovery.schemas == ("dbo", "reporting", "staging")


def test_one_object_kind_failing_does_not_cost_the_others():
    discovery = source(
        full_responses(views=CatalogQueryError("SELECT permission denied"))
    ).discover()

    assert discovery.table_count == 1
    assert discovery.procedure_count == 1
    assert discovery.views == ()
    assert not discovery.is_complete
    assert "No claim is made about how many views" in " ".join(
        i.message for i in discovery.issues
    )


def test_opaque_modules_are_named_rather_than_counted():
    discovery = source(
        full_responses(modules=[module_row(20, None), module_row(30, None)])
    ).discover()

    assert discovery.opaque_modules == ("poolone.dbo.vSales", "poolone.dbo.pLoad")


def test_the_summary_reports_every_object_kind_without_a_definition_in_it():
    summary = source(full_responses()).discover().summary()

    assert summary["table_count"] == 1
    assert summary["view_count"] == 1
    assert summary["procedure_count"] == 1
    assert VIEW_BODY not in repr(summary)


def test_discovery_over_the_same_rows_is_deterministic():
    first = source(full_responses()).discover().to_dict()
    second = source(full_responses()).discover().to_dict()

    assert first == second
