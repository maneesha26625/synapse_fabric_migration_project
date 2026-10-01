"""Live discovery against a real Synapse Dedicated SQL Pool.

**Opt-in only.** Skipped unless all three of these are set:

    SYNAPSE_SQL_INTEGRATION=1
    SYNAPSE_SQL_SERVER=<workspace>.sql.azuresynapse.net
    SYNAPSE_SQL_DATABASE=<pool>

The opt-in flag is separate from the connection settings on purpose: having a
server configured for the dev command must never be enough to make the normal
test suite open a network connection and trigger an interactive sign-in.

These tests assert the *shape* of what comes back, never its content. The
pool belongs to a client, its tables are theirs, and a test that asserted a
particular table exists would fail for everyone else.

What this file is really for: the catalog queries in
``discovery_agent.sql.queries`` are written against the documented dedicated
pool catalog but have never been executed by the unit tests, which fake the
driver. This is the only place that proves they parse and run.

The connection comes from ``discovery_agent.connections``. This file used to
build its own ``PyodbcConnector`` with the ODBC driver's interactive sign-in,
which meant the live SQL suite authenticated differently from the rest of the
application; it now uses the same Azure CLI identity everything else does, so
what it proves about the catalog queries is proved over the real connection
path.
"""

from __future__ import annotations

import os

import pytest

from discovery_agent.connections.azure import credential_provider
from discovery_agent.connections.sql import SqlConnection
from discovery_agent.extractors.models import SourceType
from discovery_agent.sql.config import config_from_environment
from discovery_agent.sql.models import (
    CatalogObjectType,
    DistributionPolicy,
    IndexKind,
    TableStorage,
)
from discovery_agent.sql.source import SQL_SOURCE_FORMAT

ENABLED = os.environ.get("SYNAPSE_SQL_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLED
    or not os.environ.get("SYNAPSE_SQL_SERVER")
    or not os.environ.get("SYNAPSE_SQL_DATABASE"),
    reason=(
        "live pool integration is opt-in: set SYNAPSE_SQL_INTEGRATION=1 plus "
        "SYNAPSE_SQL_SERVER and SYNAPSE_SQL_DATABASE"
    ),
)


@pytest.fixture(scope="module")
def pool_source():
    """The connection layer's source for the configured pool.

    Module-scoped so one identity and one token cache serve every assertion
    below, which is the same "log in once" rule the application follows.
    """
    pytest.importorskip("pyodbc", reason="pyodbc is required for live discovery")

    connection = SqlConnection(config_from_environment(), credential_provider())
    return connection.source()


@pytest.fixture(scope="module")
def discovery(pool_source):
    """One real discovery run, shared by every assertion in this module."""
    with pool_source as source:
        yield source.discover_tables()


def test_the_catalog_queries_run_against_a_real_pool(discovery):
    """The one thing no fake can establish: that this T-SQL is valid here."""
    assert discovery.database.name
    assert discovery.database.server


def test_the_connected_principal_is_identified(discovery):
    """Needed to interpret everything else: visibility is bounded by identity."""
    assert discovery.principal.described != "unknown principal"


def test_provenance_names_the_real_pool_and_database(discovery):
    provenance = discovery.provenance

    assert provenance.source_type is SourceType.SQL
    assert provenance.source_format == SQL_SOURCE_FORMAT
    assert provenance.resource_id == (
        f"{os.environ['SYNAPSE_SQL_SERVER']}/{discovery.database.name}"
    )


def test_every_discovered_table_is_fully_identified(discovery):
    for table in discovery.tables:
        assert table.key.database == discovery.database.name
        assert table.key.schema
        assert table.key.name
        assert table.key.object_id is not None
        assert table.key.logical_id.startswith("sql://")


def test_table_identities_are_unique(discovery):
    """Schema qualification must actually prevent collisions on a real estate."""
    ids = [table.key.logical_id for table in discovery.tables]

    assert len(ids) == len(set(ids))


def test_every_table_reports_whether_it_is_external(discovery):
    """Proves ``tables.v2`` actually returns ``sys.tables.is_external`` here.

    This is the assertion that needs a real pool: the column is documented,
    but only the engine can say whether it is selectable on this version. A
    None would mean the query ran yet the flag never arrived, and every table
    would silently fall back to the v1 reading.

    Requires no external table to exist. It asserts that the question is
    answered for every table, not that any particular answer comes back.
    """
    unanswered = [
        table.key.qualified_name
        for table in discovery.tables
        if table.is_external is None
    ]

    assert not unanswered, (
        f"is_external was not returned for: {unanswered}; tables.v2 selects "
        f"the column, so a None means the engine did not supply it"
    )


def test_distribution_policies_are_recognised(discovery):
    """The code-to-policy map is the least verified part of this slice.

    An UNKNOWN here with a raw code attached means the map needs extending —
    which is exactly the signal this test exists to give, so it reports the
    codes rather than silently passing.

    Scoped to distributed tables: an external table has no policy to
    recognise, and including it would make this fail for an estate that uses
    PolyBase.
    """
    unmapped = {
        table.key.qualified_name: table.distribution_policy_code
        for table in discovery.tables
        if table.is_distributed
        and table.distribution is DistributionPolicy.UNKNOWN
    }

    assert not unmapped, f"unmapped distribution policy codes: {unmapped}"


def test_external_tables_are_not_reported_as_missing_distribution(discovery):
    """The regression this fix exists to prevent, checked against real rows.

    Vacuous on a pool with no external tables, and deliberately so — it costs
    nothing there and needs no object to be created. On a pool that does have
    them it is the assertion that matters, so it reports which tables failed
    rather than only that something did.
    """
    external = [t for t in discovery.tables if t.is_external]
    if not external:
        pytest.skip("no external tables are visible to this principal")

    wrong = {
        table.key.qualified_name: table.distribution.value
        for table in external
        if table.distribution is not DistributionPolicy.NOT_APPLICABLE
    }
    assert not wrong, f"external tables reported a distribution policy: {wrong}"

    false_gaps = {
        table.key.qualified_name: [i.message for i in table.issues]
        for table in external
        if any("distribution" in i.message for i in table.issues)
    }
    assert not false_gaps, (
        f"external tables raised a distribution gap that is not a gap: "
        f"{false_gaps}"
    )


def test_the_distribution_column_catalog_is_readable(discovery):
    """Proves ``distribution_columns.v1`` parses and runs on a real pool.

    Needs no hash-distributed table to exist. If the query had been rejected
    the source would have recorded a run-level failure, so the assertion is
    that no such failure is present — which is exactly what a pool of
    round-robin tables can tell us.
    """
    failures = [
        issue.message
        for issue in discovery.issues
        if "distribution columns could not be retrieved" in issue.message
    ]

    assert not failures, (
        f"the distribution column query did not run against this pool: "
        f"{failures}. Either the T-SQL is wrong for this engine version or "
        f"the principal lacks rights on sys.pdw_column_distribution_properties"
    )


def test_undistributed_tables_report_no_distribution_key(discovery):
    """The assertion that runs today against an ordinary pool.

    Round-robin, replicated and external tables have no distribution key, and
    reporting one would mean the ordinal filter or the per-table attribution
    is wrong. Requires nothing to be created: any pool with a single
    round-robin table exercises it.
    """
    undistributed = [
        table
        for table in discovery.tables
        if table.distribution
        in (
            DistributionPolicy.ROUND_ROBIN,
            DistributionPolicy.REPLICATE,
            DistributionPolicy.NOT_APPLICABLE,
        )
    ]
    if not undistributed:
        pytest.skip("no round-robin, replicated or external tables are visible")

    wrong = {
        table.key.qualified_name: table.distribution_column_names
        for table in undistributed
        if table.distribution_columns
    }

    assert not wrong, f"tables with no distribution key reported one: {wrong}"


def test_hash_distributed_tables_expose_a_well_formed_key(discovery):
    """Skipped rather than vacuous when the estate has no hash table.

    Asserts the shape the catalog guarantees — at least one column, and
    ordinals numbered 1..n with no gap or repeat — so a mis-sorted or
    partially-attributed key fails here rather than reaching an assessment.
    """
    hashed = [
        table
        for table in discovery.tables
        if table.distribution is DistributionPolicy.HASH
    ]
    if not hashed:
        pytest.skip("no hash-distributed tables are visible to this principal")

    malformed = {}
    for table in hashed:
        ordinals = [column.ordinal for column in table.distribution_columns]
        if not ordinals or ordinals != list(range(1, len(ordinals) + 1)):
            malformed[table.key.qualified_name] = ordinals

    assert not malformed, f"hash tables with a malformed distribution key: {malformed}"


def test_a_distribution_key_names_columns_of_its_own_table(discovery):
    """Cross-checks the two catalogs against each other.

    ``distribution_columns.v1`` and ``columns.v1`` are separate queries joined
    in Python on object_id, so a key naming a column_id the table does not
    have would mean the attribution is wrong.
    """
    hashed = [
        table
        for table in discovery.tables
        if table.distribution is DistributionPolicy.HASH and table.has_columns
    ]
    if not hashed:
        pytest.skip("no hash-distributed table with visible columns")

    orphaned = {}
    for table in hashed:
        known = {column.column_id for column in table.columns}
        stray = [
            column.column_id
            for column in table.distribution_columns
            if column.column_id not in known
        ]
        if stray:
            orphaned[table.key.qualified_name] = stray

    assert not orphaned, (
        f"distribution keys naming column_ids their table does not have: "
        f"{orphaned}"
    )


def test_the_index_catalog_is_readable(discovery):
    """Proves ``indexes.v1`` parses and runs here. Needs no particular index."""
    failures = [
        issue.message
        for issue in discovery.issues
        if "indexes could not be retrieved" in issue.message
    ]

    assert not failures, f"the index query did not run against this pool: {failures}"


def test_the_index_column_catalog_is_readable(discovery):
    """The riskier of the two: ``index_columns.v1`` selects
    ``column_store_order_ordinal``, which is how ORDERED CLUSTERED COLUMNSTORE
    is detected but which an older engine version may not expose. A failure
    here means the ORDERED refinement is unavailable on this pool and the
    query needs splitting, not that the pool has no indexes.
    """
    failures = [
        issue.message
        for issue in discovery.issues
        if "index columns could not be retrieved" in issue.message
    ]

    assert not failures, (
        f"the index column query did not run against this pool: {failures}. "
        f"If the cause is column_store_order_ordinal, ordered-columnstore "
        f"detection needs its own query so the rest of index_columns survives"
    )


def test_every_ordinary_table_reports_a_storage_structure(discovery):
    """The assertion that runs today against any pool with one ordinary table.

    Every non-external table has a heap or a clustered index, so an UNKNOWN
    here means the base index row did not come back — which is exactly the
    gap the storage rules exist to surface rather than paper over.
    """
    ordinary = [t for t in discovery.tables if not t.is_external]
    if not ordinary:
        pytest.skip("no ordinary (non-external) tables are visible")

    unresolved = {
        table.key.qualified_name: [i.message for i in table.issues]
        for table in ordinary
        if table.storage is TableStorage.UNKNOWN
    }

    assert not unresolved, f"tables with no storage structure: {unresolved}"


def test_a_base_index_is_present_and_unique_per_ordinary_table(discovery):
    """index_id 0 and 1 are mutually exclusive: a table is a heap or it is
    clustered, never both. Two base rows would mean the attribution is wrong.
    """
    ordinary = [t for t in discovery.tables if not t.is_external]
    if not ordinary:
        pytest.skip("no ordinary (non-external) tables are visible")

    wrong = {
        table.key.qualified_name: [i.index_id for i in table.indexes]
        for table in ordinary
        if len([i for i in table.indexes if i.is_base_structure]) != 1
    }

    assert not wrong, f"tables without exactly one base index row: {wrong}"


def test_external_tables_report_not_applicable_storage(discovery):
    external = [t for t in discovery.tables if t.is_external]
    if not external:
        pytest.skip("no external tables are visible to this principal")

    wrong = {
        table.key.qualified_name: table.storage.value
        for table in external
        if table.storage is not TableStorage.NOT_APPLICABLE
    }

    assert not wrong, f"external tables reported ordinary storage: {wrong}"


@pytest.mark.parametrize(
    "storage",
    [
        TableStorage.HEAP,
        TableStorage.CLUSTERED_COLUMNSTORE,
        TableStorage.ORDERED_CLUSTERED_COLUMNSTORE,
        TableStorage.CLUSTERED_INDEX,
    ],
)
def test_each_storage_structure_is_well_formed_where_the_pool_has_one(
    discovery, storage
):
    """One case per structure, each skipping explicitly when this pool has no
    table of that kind.

    Parametrised so the report names which structures were actually exercised
    and which were skipped, rather than a single test passing while silently
    covering only whatever happens to exist. Nothing needs to be created: the
    skips are the honest result on a pool that has only one kind of table.
    """
    matching = [t for t in discovery.tables if t.storage is storage]
    if not matching:
        pytest.skip(f"no {storage.value} tables are visible on this pool")

    for table in matching:
        base = table.base_index
        assert base is not None

        if storage is TableStorage.HEAP:
            assert base.index_id == 0
            assert base.kind is IndexKind.HEAP
        else:
            assert base.index_id == 1, "a clustered structure is index_id 1"

        if storage is TableStorage.ORDERED_CLUSTERED_COLUMNSTORE:
            ordinals = [c.column_store_order_ordinal for c in base.ordering_columns]
            assert ordinals == list(range(1, len(ordinals) + 1)), (
                f"{table.key.qualified_name} has malformed ordering ordinals: "
                f"{ordinals}"
            )

        if storage is TableStorage.CLUSTERED_INDEX:
            ordinals = [c.key_ordinal for c in base.key_columns]
            assert ordinals == list(range(1, len(ordinals) + 1)), (
                f"{table.key.qualified_name} has malformed key ordinals: "
                f"{ordinals}"
            )


def test_index_columns_name_columns_of_their_own_table(discovery):
    """Cross-checks index_columns.v1 against columns.v1, which are separate
    queries joined in Python on object_id."""
    candidates = [t for t in discovery.tables if t.has_columns and t.indexes]
    if not candidates:
        pytest.skip("no table with both visible columns and indexes")

    orphaned = {}
    for table in candidates:
        known = {c.column_id for c in table.columns}
        stray = sorted(
            {
                column.column_id
                for index in table.indexes
                for column in index.columns
                if column.column_id not in known
            }
        )
        if stray:
            orphaned[table.key.qualified_name] = stray

    assert not orphaned, (
        f"indexes naming column_ids their table does not have: {orphaned}"
    )


def test_columns_are_discovered_for_visible_tables(discovery):
    if not discovery.tables:
        pytest.skip("no tables are visible to this principal")

    assert any(table.has_columns for table in discovery.tables), (
        "no table returned any columns; check VIEW DEFINITION or SELECT grants"
    )
    for table in discovery.tables:
        if not table.has_columns:
            continue
        ids = [column.column_id for column in table.columns]
        assert ids == sorted(ids)
        assert all(column.name for column in table.columns)
        assert all(column.data_type for column in table.columns)


def test_enumeration_agrees_with_full_discovery(discovery, request):
    """``objects()`` and ``discover_tables()`` must not see different estates."""
    connection = SqlConnection(config_from_environment(), credential_provider())
    with connection.source() as source:
        keys = source.objects(CatalogObjectType.TABLE)

    assert {k.qualified_name for k in keys} == {
        t.key.qualified_name for t in discovery.tables
    }


def test_the_run_reports_its_own_gaps(discovery):
    """Not an assertion that the run is clean — an assertion that it is honest."""
    for issue in discovery.all_issues:
        assert issue.code is not None
        assert issue.message
        assert issue.location
