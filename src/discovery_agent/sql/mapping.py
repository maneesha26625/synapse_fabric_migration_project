"""Turning catalog rows into typed models.

Pure functions over plain mappings: no connection, no driver, no I/O. That is
deliberate — it is what lets every mapping rule be tested against
deterministic rows, and it keeps the interesting decisions out of the class
that holds a socket.

The rule throughout is that a missing value stays missing. A column the
catalog did not return becomes ``None`` and an issue explaining the gap;
it never becomes a default that a later stage would read as a fact.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.extractors.models import ExtractionIssue, IssueCode
from discovery_agent.extractors.sql_models import DynamicSqlSite, SqlObjectReference
from discovery_agent.extractors.sql_scanning import scan_dynamic_sql, scan_objects
from discovery_agent.sql.models import (
    DISTRIBUTION_POLICY_CODES,
    INDEX_TYPE_CODES,
    CatalogDatabase,
    CatalogObjectType,
    CatalogPrincipal,
    DefinitionState,
    DistributionPolicy,
    IndexKind,
    SqlColumn,
    SqlDistributionColumn,
    SqlIndex,
    SqlIndexColumn,
    SqlModuleDefinition,
    SqlObjectKey,
    SqlParameter,
    SqlProcedure,
    SqlTable,
    SqlView,
    TableStorage,
)

#: Policies that have no distribution key by definition, so an empty list of
#: distribution columns is the correct and complete answer rather than a gap.
_POLICIES_WITHOUT_A_KEY = frozenset(
    {
        DistributionPolicy.ROUND_ROBIN,
        DistributionPolicy.REPLICATE,
        DistributionPolicy.NOT_APPLICABLE,
    }
)


def _text(value: Any) -> Optional[str]:
    """A trimmed string, or None. An empty string is not a value."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any) -> Optional[int]:
    """An int, or None when the catalog gave something that is not one."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> Optional[bool]:
    """A bool from the catalog's bit columns, or None.

    Drivers return bits as bool or as 0/1 depending on version, so both are
    accepted. Anything else is not guessed at.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return None


def _timestamp(value: Any) -> Optional[str]:
    """A datetime rendered ISO-8601, or None.

    Rendered rather than kept as a ``datetime`` so the model stays
    serialisable and deterministic. Strings the driver already formatted are
    passed through untouched rather than reparsed and reformatted.
    """
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return _text(value)


def database_from_row(row: Optional[Mapping[str, Any]], server: str, fallback: str) -> CatalogDatabase:
    """The database identity, preferring what the server reported.

    ``fallback`` is the configured database name, used when the catalog query
    could not run. Discovery still knows which database it asked for.
    """
    row = row or {}
    return CatalogDatabase(
        name=_text(row.get("database_name")) or fallback,
        server=server,
        collation=_text(row.get("collation_name")),
    )


def principal_from_row(row: Optional[Mapping[str, Any]]) -> CatalogPrincipal:
    """Who the connection authenticated as. Names only; never a credential."""
    row = row or {}
    return CatalogPrincipal(
        user_name=_text(row.get("user_name")),
        login_name=_text(row.get("login_name")),
    )


def table_key_from_row(database: str, row: Mapping[str, Any]) -> SqlObjectKey:
    """The three-part identity of one table row.

    Raises when schema or name is absent: an object discovery cannot name is
    not an object it can report, and inventing a placeholder would put a
    fictional table into the inventory.
    """
    schema = _text(row.get("schema_name"))
    name = _text(row.get("table_name"))
    if not schema or not name:
        raise ValueError(
            f"catalog row is missing a schema or table name: "
            f"schema={row.get('schema_name')!r} table={row.get('table_name')!r}"
        )
    return SqlObjectKey(
        database=database,
        schema=schema,
        name=name,
        object_type=CatalogObjectType.TABLE,
        object_id=_integer(row.get("object_id")),
    )


def distribution_from_row(
    row: Mapping[str, Any], location: str
) -> Tuple[DistributionPolicy, Optional[int], Tuple[ExtractionIssue, ...]]:
    """The distribution policy, the raw code, and what could not be determined.

    Three outcomes, kept distinct because they have different causes and
    different fixes:

    * the table is external, so there is no distribution policy to report and
      nothing is missing;
    * no row at all for a table that should have one (the LEFT JOIN found
      nothing, or the distribution view is not readable);
    * a code this version has no name for.

    None of them is allowed to look like a plain ROUND_ROBIN, and the first is
    not allowed to look like either of the others.
    """
    raw = row.get("distribution_policy")
    code = _integer(raw)
    external = _boolean(row.get("is_external"))

    if external:
        # An external table's data is not in the pool, so it is not spread
        # across distributions and the view has no row for it. Reporting the
        # absent row as missing information would describe a grant problem
        # that does not exist. A code alongside is_external=1 is a
        # contradiction rather than a value to trust, so it is preserved and
        # flagged instead of being mapped.
        issues = ()
        if code is not None:
            issues = (
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"this table is external but the distribution view also "
                    f"returned policy code {code}; the raw code is preserved "
                    f"and was not interpreted as a distribution",
                    location,
                ),
            )
        return DistributionPolicy.NOT_APPLICABLE, code, issues

    if code is None:
        return (
            DistributionPolicy.UNKNOWN,
            None,
            (
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    "no distribution policy was returned for this table; the "
                    "distribution catalog view may not be readable by the "
                    "connected principal",
                    location,
                ),
            ),
        )

    policy = DISTRIBUTION_POLICY_CODES.get(code)
    if policy is None:
        return (
            DistributionPolicy.UNKNOWN,
            code,
            (
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"distribution policy code {code} is not one this version "
                    f"maps; the raw code is preserved and was not guessed at",
                    location,
                ),
            ),
        )
    return policy, code, ()


def distribution_column_from_row(
    row: Mapping[str, Any],
) -> Optional[SqlDistributionColumn]:
    """One distribution column, or None when the row cannot identify itself.

    An ordinal of 0 or less is treated as unusable rather than as a
    distribution column: the query filters on ``distribution_ordinal > 0``, so
    a non-positive ordinal arriving here means the row did not come through
    that predicate and nothing about it should be trusted.
    """
    column_id = _integer(row.get("column_id"))
    ordinal = _integer(row.get("distribution_ordinal"))
    if column_id is None or ordinal is None or ordinal < 1:
        return None
    return SqlDistributionColumn(
        column_id=column_id,
        ordinal=ordinal,
        name=_text(row.get("column_name")),
    )


def distribution_columns_by_object_id(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[int, Tuple[SqlDistributionColumn, ...]], int]:
    """Group distribution-column rows by table, and count the unusable ones.

    Sorted by ``ordinal`` so a multi-column key is in the order the engine
    hashes on, which is part of the key's meaning and not a presentation
    choice. ``column_id`` breaks a tie rather than leaving duplicate ordinals
    in driver order, so the result is deterministic even when the catalog is
    not.
    """
    grouped: Dict[int, List[SqlDistributionColumn]] = {}
    unusable = 0
    for row in rows:
        object_id = _integer(row.get("object_id"))
        column = distribution_column_from_row(row)
        if object_id is None or column is None:
            unusable += 1
            continue
        grouped.setdefault(object_id, []).append(column)
    return (
        {
            object_id: tuple(sorted(columns, key=lambda c: (c.ordinal, c.column_id)))
            for object_id, columns in grouped.items()
        },
        unusable,
    )


def distribution_column_issues(
    policy: DistributionPolicy,
    distribution_columns: Tuple[SqlDistributionColumn, ...],
    readable: bool,
    location: str,
) -> Tuple[ExtractionIssue, ...]:
    """What an empty or surprising distribution key means for this table.

    The whole point of this function is that an empty tuple is not one fact.
    For ROUND_ROBIN, REPLICATE and an external table it is the complete and
    correct answer. For HASH it is either a catalog that could not be read or
    a hash table whose key did not come back — both gaps, and neither may be
    reported as "this table has no distribution column".
    """
    issues: List[ExtractionIssue] = []

    if not readable:
        # Only for tables that would have had a key. Saying the key of a
        # round-robin table could not be read would invent a gap: there was
        # nothing there to read.
        if policy not in _POLICIES_WITHOUT_A_KEY:
            issues.append(
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    "the distribution column catalog could not be read, so "
                    "this table's distribution key is unknown; this is not a "
                    "statement that it has none",
                    location,
                )
            )
        return tuple(issues)

    if policy is DistributionPolicy.HASH and not distribution_columns:
        issues.append(
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                "this table is hash-distributed but no distribution column "
                "was returned; a hash-distributed table has at least one, so "
                "the key is missing rather than absent",
                location,
            )
        )

    if policy in _POLICIES_WITHOUT_A_KEY and distribution_columns:
        issues.append(
            ExtractionIssue(
                IssueCode.UNSUPPORTED_CONSTRUCT,
                f"this table reports policy {policy.value} yet the catalog "
                f"returned {len(distribution_columns)} distribution column(s); "
                f"they are preserved and were not interpreted as a key",
                location,
            )
        )

    unnamed = [c.column_id for c in distribution_columns if not c.name]
    if unnamed:
        issues.append(
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                f"the distribution key names column_id(s) {unnamed} that "
                f"could not be resolved to a column name; the ordinal and id "
                f"are preserved",
                location,
            )
        )

    return tuple(issues)


def index_column_from_row(row: Mapping[str, Any]) -> Optional[SqlIndexColumn]:
    """One index column, or None when the row cannot identify itself."""
    column_id = _integer(row.get("column_id"))
    if column_id is None:
        return None
    return SqlIndexColumn(
        column_id=column_id,
        name=_text(row.get("column_name")),
        key_ordinal=_integer(row.get("key_ordinal")),
        is_descending=_boolean(row.get("is_descending_key")),
        column_store_order_ordinal=_integer(row.get("column_store_order_ordinal")),
    )


def index_columns_by_index(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[Tuple[int, int], Tuple[SqlIndexColumn, ...]], int]:
    """Group index-column rows by (object_id, index_id), counting unusable ones.

    Two-part key because index_id is only unique within a table, so grouping
    on it alone would merge the clustered index of one table with the
    clustered index of the next.
    """
    grouped: Dict[Tuple[int, int], List[SqlIndexColumn]] = {}
    unusable = 0
    for row in rows:
        object_id = _integer(row.get("object_id"))
        index_id = _integer(row.get("index_id"))
        column = index_column_from_row(row)
        if object_id is None or index_id is None or column is None:
            unusable += 1
            continue
        grouped.setdefault((object_id, index_id), []).append(column)
    return (
        {
            key: tuple(
                sorted(columns, key=lambda c: (c.key_ordinal or 0, c.column_id))
            )
            for key, columns in grouped.items()
        },
        unusable,
    )


def index_from_row(
    row: Mapping[str, Any], columns: Tuple[SqlIndexColumn, ...] = ()
) -> Optional[SqlIndex]:
    """One index, or None when the row has no index_id to identify it.

    An unmapped ``type`` code becomes ``IndexKind.UNKNOWN`` and keeps both the
    raw code and the catalog's own ``type_desc``, so a structure this version
    does not know is still reported as the engine described it.
    """
    index_id = _integer(row.get("index_id"))
    if index_id is None:
        return None
    type_code = _integer(row.get("type_code"))
    return SqlIndex(
        index_id=index_id,
        kind=INDEX_TYPE_CODES.get(type_code, IndexKind.UNKNOWN),
        name=_text(row.get("index_name")),
        type_code=type_code,
        type_desc=_text(row.get("type_desc")),
        is_unique=_boolean(row.get("is_unique")),
        is_primary_key=_boolean(row.get("is_primary_key")),
        is_unique_constraint=_boolean(row.get("is_unique_constraint")),
        columns=columns,
    )


def indexes_by_object_id(
    index_rows: Sequence[Mapping[str, Any]],
    index_column_rows: Sequence[Mapping[str, Any]] = (),
) -> Tuple[Dict[int, Tuple[SqlIndex, ...]], int, int]:
    """Assemble indexes with their columns, grouped by table.

    Returns the grouping plus two counts: index rows that could not be
    identified, and index-column rows that could not be. They are counted
    separately because they come from different queries and a reader needs to
    know which catalog was ragged.
    """
    columns_by_index, unusable_columns = index_columns_by_index(index_column_rows)

    grouped: Dict[int, List[SqlIndex]] = {}
    unusable_indexes = 0
    for row in index_rows:
        object_id = _integer(row.get("object_id"))
        index = index_from_row(row)
        if object_id is None or index is None:
            unusable_indexes += 1
            continue
        index = replace(
            index, columns=columns_by_index.get((object_id, index.index_id), ())
        )
        grouped.setdefault(object_id, []).append(index)

    return (
        {
            object_id: tuple(sorted(indexes, key=lambda i: i.index_id))
            for object_id, indexes in grouped.items()
        },
        unusable_indexes,
        unusable_columns,
    )


def storage_from_indexes(
    indexes: Tuple[SqlIndex, ...],
    is_external: Optional[bool],
    indexes_readable: bool,
    index_columns_readable: bool,
    location: str,
) -> Tuple[TableStorage, Tuple[ExtractionIssue, ...]]:
    """How this table's rows are stored, and what could not be established.

    An external table is handled first and separately: its rows are not stored
    in the pool, so it has no base structure to find and an absent index row
    is the correct answer rather than a gap. Whatever the catalog did return
    for it is still kept on the table as evidence, but nothing about ordinary
    table storage is inferred for it.
    """
    if is_external:
        return TableStorage.NOT_APPLICABLE, ()

    if not indexes_readable:
        return (
            TableStorage.UNKNOWN,
            (
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    "the index catalog could not be read, so this table's "
                    "storage structure is unknown; this is not a statement "
                    "that it is a heap",
                    location,
                ),
            ),
        )

    base = next((i for i in indexes if i.is_base_structure), None)
    if base is None:
        return (
            TableStorage.UNKNOWN,
            (
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    "no base index row was returned for this table; every "
                    "table has a heap or a clustered index, so the storage "
                    "structure is missing rather than absent",
                    location,
                ),
            ),
        )

    if base.kind is IndexKind.HEAP:
        return TableStorage.HEAP, ()
    if base.kind is IndexKind.CLUSTERED:
        return TableStorage.CLUSTERED_INDEX, ()
    if base.kind is IndexKind.CLUSTERED_COLUMNSTORE:
        if base.ordering_columns:
            return TableStorage.ORDERED_CLUSTERED_COLUMNSTORE, ()
        if not index_columns_readable:
            # Ordering lives only in sys.index_columns. Without it the index is
            # still known to be columnstore, but calling it unordered would be
            # a guess, so the gap is stated rather than resolved.
            return (
                TableStorage.CLUSTERED_COLUMNSTORE,
                (
                    ExtractionIssue(
                        IssueCode.MISSING_INFORMATION,
                        "the index column catalog could not be read, so "
                        "whether this clustered columnstore index is ORDERED "
                        "was not determined",
                        location,
                    ),
                ),
            )
        return TableStorage.CLUSTERED_COLUMNSTORE, ()

    # A base index of some other kind: reported as unknown storage with the
    # catalog's own description preserved on the index itself.
    return (
        TableStorage.UNKNOWN,
        (
            ExtractionIssue(
                IssueCode.UNSUPPORTED_CONSTRUCT,
                f"the base index of this table has type "
                f"{base.type_code!r} ({base.type_desc or 'no description'}), "
                f"which this version does not map to a storage structure",
                location,
            ),
        ),
    )


def column_from_row(row: Mapping[str, Any]) -> Optional[SqlColumn]:
    """One column, or None when the row cannot identify itself.

    A column with no id or no name is dropped rather than invented; the
    caller counts what it dropped and raises an issue.
    """
    column_id = _integer(row.get("column_id"))
    name = _text(row.get("column_name"))
    if column_id is None or not name:
        return None
    return SqlColumn(
        column_id=column_id,
        name=name,
        data_type=_text(row.get("data_type")),
        max_length=_integer(row.get("max_length")),
        precision=_integer(row.get("precision_value")),
        scale=_integer(row.get("scale_value")),
        is_nullable=_boolean(row.get("is_nullable")),
        is_identity=_boolean(row.get("is_identity")),
    )


def columns_by_object_id(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[int, Tuple[SqlColumn, ...]], int]:
    """Group column rows by their table, and count the ones that were unusable.

    Ordered by ``column_id`` so the result is the table's column order rather
    than the order the driver happened to return.
    """
    grouped: Dict[int, List[SqlColumn]] = {}
    unusable = 0
    for row in rows:
        object_id = _integer(row.get("object_id"))
        column = column_from_row(row)
        if object_id is None or column is None:
            unusable += 1
            continue
        grouped.setdefault(object_id, []).append(column)
    return (
        {
            object_id: tuple(sorted(columns, key=lambda c: c.column_id))
            for object_id, columns in grouped.items()
        },
        unusable,
    )


def build_tables(
    database: str,
    table_rows: Sequence[Mapping[str, Any]],
    column_rows: Sequence[Mapping[str, Any]],
    columns_readable: bool = True,
    distribution_column_rows: Sequence[Mapping[str, Any]] = (),
    distribution_columns_readable: bool = True,
    index_rows: Sequence[Mapping[str, Any]] = (),
    index_column_rows: Sequence[Mapping[str, Any]] = (),
    indexes_readable: bool = True,
    index_columns_readable: bool = True,
) -> Tuple[Tuple[SqlTable, ...], Tuple[ExtractionIssue, ...]]:
    """Assemble tables and their columns, with everything unresolved stated.

    ``columns_readable`` is False when the column query itself failed. The
    distinction matters: a table with no columns because nothing was returned
    is a different fact from a table whose columns were never asked for, and
    neither may be reported as a table that genuinely has none.

    ``distribution_columns_readable`` carries the same distinction for the
    distribution key, and is separate because the two catalogs are separately
    grantable: a principal can readily see one and not the other.
    ``indexes_readable`` and ``index_columns_readable`` do the same for the
    two index catalogs, which fail independently: losing index columns costs
    key order and the ORDERED refinement but still leaves the table's
    heap-versus-columnstore structure known.

    Returns the tables in catalog order, plus the issues that belong to the
    run rather than to any one table.
    """
    grouped, unusable_columns = columns_by_object_id(column_rows)
    distribution_grouped, unusable_distribution_columns = (
        distribution_columns_by_object_id(distribution_column_rows)
    )
    index_grouped, unusable_indexes, unusable_index_columns = indexes_by_object_id(
        index_rows, index_column_rows
    )

    tables: List[SqlTable] = []
    run_issues: List[ExtractionIssue] = []
    unnamed = 0

    for row in table_rows:
        try:
            key = table_key_from_row(database, row)
        except ValueError as exc:
            unnamed += 1
            run_issues.append(
                ExtractionIssue(
                    IssueCode.MALFORMED_ARTIFACT, str(exc), f"{database}.tables"
                )
            )
            continue

        location = key.qualified_name
        external = _boolean(row.get("is_external"))
        policy, code, issues = distribution_from_row(row, location)
        columns = grouped.get(key.object_id, ()) if key.object_id is not None else ()
        distribution_columns = (
            distribution_grouped.get(key.object_id, ())
            if key.object_id is not None
            else ()
        )
        issues = issues + distribution_column_issues(
            policy, distribution_columns, distribution_columns_readable, location
        )

        indexes = (
            index_grouped.get(key.object_id, ()) if key.object_id is not None else ()
        )
        storage, storage_issues = storage_from_indexes(
            indexes, external, indexes_readable, index_columns_readable, location
        )
        issues = issues + storage_issues

        if not columns_readable:
            issues = issues + (
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    "columns were not retrieved for this table because the "
                    "column catalog query could not be run; this is not a "
                    "statement that the table has no columns",
                    location,
                ),
            )
        elif not columns:
            issues = issues + (
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    "no columns were returned for this table; the connected "
                    "principal may not have visibility of its columns",
                    location,
                ),
            )

        tables.append(
            SqlTable(
                key=key,
                create_date=_timestamp(row.get("create_date")),
                modify_date=_timestamp(row.get("modify_date")),
                is_external=external,
                distribution=policy,
                distribution_policy_code=code,
                distribution_columns=distribution_columns,
                storage=storage,
                indexes=indexes,
                columns=columns,
                issues=issues,
            )
        )

    if unusable_columns:
        run_issues.append(
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                f"{unusable_columns} column row(s) could not be identified and "
                f"were not attributed to a table",
                f"{database}.columns",
            )
        )

    if unusable_distribution_columns:
        run_issues.append(
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                f"{unusable_distribution_columns} distribution column row(s) "
                f"could not be identified and were not attributed to a table",
                f"{database}.distribution_columns",
            )
        )

    if unusable_indexes:
        run_issues.append(
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                f"{unusable_indexes} index row(s) could not be identified and "
                f"were not attributed to a table",
                f"{database}.indexes",
            )
        )

    if unusable_index_columns:
        run_issues.append(
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                f"{unusable_index_columns} index column row(s) could not be "
                f"identified and were not attributed to an index",
                f"{database}.index_columns",
            )
        )

    return tuple(tables), tuple(run_issues)


def visibility_issue(
    database: str, principal: CatalogPrincipal, table_count: int
) -> Optional[ExtractionIssue]:
    """The issue that says an empty result may not mean an empty database.

    Catalog views are security-trimmed, so a principal without visibility sees
    a shorter list and no error. Discovery genuinely cannot tell the two apart
    from inside the connection, and says so rather than reporting zero tables
    as a finding.
    """
    if table_count:
        return None
    return ExtractionIssue(
        IssueCode.MISSING_INFORMATION,
        f"no user tables are visible to {principal.described}; catalog views "
        f"are security-trimmed, so this may mean the database has none or "
        f"that this principal cannot see them. The two cannot be "
        f"distinguished from this connection",
        f"{database}.tables",
    )


# --- views and stored procedures ---------------------------------------------
#
# Both are "a named object whose content is SQL text", so the parts that are
# genuinely the same -- identity, the body, the observations made about the
# body -- are written once and used by both builders.


def module_key_from_row(
    database: str, row: Mapping[str, Any], object_type: CatalogObjectType
) -> SqlObjectKey:
    """The three-part identity of one view or procedure row.

    Raises when schema or name is absent, for the same reason
    ``table_key_from_row`` does: an object discovery cannot name is not an
    object it can report.
    """
    schema = _text(row.get("schema_name"))
    name = _text(row.get("object_name"))
    if not schema or not name:
        raise ValueError(
            f"catalog row is missing a schema or object name: "
            f"schema={row.get('schema_name')!r} object={row.get('object_name')!r}"
        )
    return SqlObjectKey(
        database=database,
        schema=schema,
        name=name,
        object_type=object_type,
        object_id=_integer(row.get("object_id")),
    )


def definition_from_row(
    row: Optional[Mapping[str, Any]], modules_readable: bool
) -> SqlModuleDefinition:
    """One module body, with the reason it is missing when it is.

    The three "no text" outcomes are kept apart because their remedies are:

    * ``UNAVAILABLE`` -- ``sys.sql_modules`` itself could not be queried. Every
      module in the database is affected and the grant is the fix.
    * ``ABSENT`` -- the catalog returned no row for this object. Affects this
      object only.
    * ``OPAQUE`` -- a row came back with a NULL definition. The body exists and
      was withheld, by encryption or by a missing VIEW DEFINITION grant, and
      the catalog does not say which.

    None of the three is ever rendered as "this object has no definition".
    """
    if not modules_readable:
        return SqlModuleDefinition(state=DefinitionState.UNAVAILABLE)
    if row is None:
        return SqlModuleDefinition(state=DefinitionState.ABSENT)

    body = row.get("definition")
    body = body if isinstance(body, str) and body.strip() else None
    return SqlModuleDefinition(
        state=DefinitionState.AVAILABLE if body else DefinitionState.OPAQUE,
        text=body,
        uses_ansi_nulls=_boolean(row.get("uses_ansi_nulls")),
        uses_quoted_identifier=_boolean(row.get("uses_quoted_identifier")),
        is_schema_bound=_boolean(row.get("is_schema_bound")),
    )


def modules_by_object_id(
    rows: Sequence[Mapping[str, Any]]
) -> Dict[int, Mapping[str, Any]]:
    """Module rows keyed by object_id. One row per module, by definition."""
    indexed: Dict[int, Mapping[str, Any]] = {}
    for row in rows:
        object_id = _integer(row.get("object_id"))
        if object_id is not None:
            indexed[object_id] = row
    return indexed


def observe_definition(
    definition: SqlModuleDefinition, location: str
) -> Tuple[Tuple[SqlObjectReference, ...], Tuple[DynamicSqlSite, ...]]:
    """What the body statically names, and where it builds SQL at runtime.

    Reuses the scanner the SQL script extractor already uses, so a table named
    by a view and the same table named by a pre-copy script are recognised by
    identical rules. Nothing is executed and nothing is resolved: a name in a
    FROM clause is an observation, and whether the object exists is the
    dependency stage's question.

    A body that was not readable yields nothing, rather than an empty tuple
    that would read as "this view references nothing".
    """
    if not definition.is_readable or definition.text is None:
        return (), ()
    return (
        scan_objects(definition.text, location),
        scan_dynamic_sql(definition.text, location),
    )


def definition_issue(
    definition: SqlModuleDefinition, key: SqlObjectKey
) -> Optional[ExtractionIssue]:
    """The issue an unreadable body produces, or None when it was readable."""
    location = f"{key.qualified_name}.definition"
    if definition.state is DefinitionState.AVAILABLE:
        return None
    if definition.state is DefinitionState.OPAQUE:
        return ExtractionIssue(
            IssueCode.OPAQUE_DEFINITION,
            f"{key.qualified_name} exists and its definition was withheld: "
            f"sys.sql_modules returned no text, which means the module is "
            f"encrypted or VIEW DEFINITION was not granted. This is not an "
            f"object without a definition",
            location,
        )
    if definition.state is DefinitionState.UNAVAILABLE:
        return ExtractionIssue(
            IssueCode.MISSING_INFORMATION,
            f"sys.sql_modules could not be read, so the definition of "
            f"{key.qualified_name} was not retrieved",
            location,
        )
    return ExtractionIssue(
        IssueCode.MISSING_INFORMATION,
        f"the catalog returned no sys.sql_modules row for "
        f"{key.qualified_name}, so its definition was not retrieved",
        location,
    )


def parameter_from_row(row: Mapping[str, Any]) -> Optional[SqlParameter]:
    """One ``sys.parameters`` row, or None when it has no usable identity."""
    parameter_id = _integer(row.get("parameter_id"))
    if parameter_id is None:
        return None
    return SqlParameter(
        parameter_id=parameter_id,
        name=_text(row.get("parameter_name")),
        data_type=_text(row.get("data_type")),
        max_length=_integer(row.get("max_length")),
        precision=_integer(row.get("precision_value")),
        scale=_integer(row.get("scale_value")),
        is_output=_boolean(row.get("is_output")),
        has_default=_boolean(row.get("has_default_value")),
    )


def parameters_by_object_id(
    rows: Sequence[Mapping[str, Any]]
) -> Dict[int, Tuple[SqlParameter, ...]]:
    """Parameters grouped by procedure, in declaration order."""
    grouped: Dict[int, List[SqlParameter]] = {}
    for row in rows:
        object_id = _integer(row.get("object_id"))
        if object_id is None:
            continue
        parameter = parameter_from_row(row)
        if parameter is not None:
            grouped.setdefault(object_id, []).append(parameter)
    return {
        object_id: tuple(sorted(found, key=lambda p: p.parameter_id))
        for object_id, found in grouped.items()
    }


def build_views(
    database: str,
    view_rows: Sequence[Mapping[str, Any]],
    module_rows: Sequence[Mapping[str, Any]] = (),
    modules_readable: bool = True,
) -> Tuple[Tuple[SqlView, ...], Tuple[ExtractionIssue, ...]]:
    """Assemble every visible view from its catalog rows. Pure.

    A view whose row cannot be identified is counted and reported rather than
    dropped silently, which is the rule ``build_tables`` follows.
    """
    modules = modules_by_object_id(module_rows)
    views: List[SqlView] = []
    issues: List[ExtractionIssue] = []
    unidentified = 0

    for row in view_rows:
        try:
            key = module_key_from_row(database, row, CatalogObjectType.VIEW)
        except ValueError:
            unidentified += 1
            continue

        definition = definition_from_row(
            modules.get(key.object_id) if key.object_id is not None else None,
            modules_readable,
        )
        referenced, dynamic = observe_definition(
            definition, f"{key.qualified_name}.definition"
        )
        own_issues = [i for i in (definition_issue(definition, key),) if i is not None]

        views.append(
            SqlView(
                key=key,
                create_date=_timestamp(row.get("create_date")),
                modify_date=_timestamp(row.get("modify_date")),
                definition=definition,
                referenced_objects=referenced,
                dynamic_sql=dynamic,
                issues=tuple(own_issues),
            )
        )

    if unidentified:
        issues.append(
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                f"{unidentified} view row(s) could not be identified and were "
                f"not reported",
                f"{database}.views",
            )
        )
    return tuple(views), tuple(issues)


def build_procedures(
    database: str,
    procedure_rows: Sequence[Mapping[str, Any]],
    module_rows: Sequence[Mapping[str, Any]] = (),
    parameter_rows: Sequence[Mapping[str, Any]] = (),
    modules_readable: bool = True,
    parameters_readable: bool = True,
) -> Tuple[Tuple[SqlProcedure, ...], Tuple[ExtractionIssue, ...]]:
    """Assemble every visible stored procedure from its catalog rows. Pure.

    ``sys.parameters`` degrades on its own: a procedure whose parameters could
    not be read is still discovered, with its body and its dependencies, and
    carries an issue saying the signature is unknown. An empty parameter tuple
    would otherwise read as "takes no parameters".
    """
    modules = modules_by_object_id(module_rows)
    parameters = parameters_by_object_id(parameter_rows)
    procedures: List[SqlProcedure] = []
    issues: List[ExtractionIssue] = []
    unidentified = 0

    for row in procedure_rows:
        try:
            key = module_key_from_row(database, row, CatalogObjectType.PROCEDURE)
        except ValueError:
            unidentified += 1
            continue

        definition = definition_from_row(
            modules.get(key.object_id) if key.object_id is not None else None,
            modules_readable,
        )
        referenced, dynamic = observe_definition(
            definition, f"{key.qualified_name}.definition"
        )

        own_issues = [i for i in (definition_issue(definition, key),) if i is not None]
        if not parameters_readable:
            own_issues.append(
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    f"sys.parameters could not be read, so the signature of "
                    f"{key.qualified_name} is unknown; it is not known to "
                    f"take no parameters",
                    f"{key.qualified_name}.parameters",
                )
            )

        dynamic_issue = _dynamic_sql_issue(dynamic, key)
        if dynamic_issue is not None:
            own_issues.append(dynamic_issue)

        procedures.append(
            SqlProcedure(
                key=key,
                create_date=_timestamp(row.get("create_date")),
                modify_date=_timestamp(row.get("modify_date")),
                definition=definition,
                referenced_objects=referenced,
                dynamic_sql=dynamic,
                parameters=(
                    parameters.get(key.object_id, ()) if parameters_readable else ()
                ),
                type_code=_text(row.get("type_code")),
                issues=tuple(own_issues),
            )
        )

    if unidentified:
        issues.append(
            ExtractionIssue(
                IssueCode.MISSING_INFORMATION,
                f"{unidentified} procedure row(s) could not be identified and "
                f"were not reported",
                f"{database}.procedures",
            )
        )
    return tuple(procedures), tuple(issues)


def _dynamic_sql_issue(
    sites: Sequence[DynamicSqlSite], key: SqlObjectKey
) -> Optional[ExtractionIssue]:
    """The admission that runtime-assembled SQL hid some dependencies.

    An observation, never an evaluation: discovery records that the procedure
    builds SQL at runtime and how many places it does so, and does not attempt
    to work out what those statements would touch.
    """
    if not sites:
        return None
    return ExtractionIssue(
        IssueCode.UNSUPPORTED_CONSTRUCT,
        f"{key.qualified_name} assembles SQL at runtime in {len(sites)} "
        f"place(s); the objects those statements touch are not statically "
        f"determinable and were not resolved",
        sites[0].location,
    )
