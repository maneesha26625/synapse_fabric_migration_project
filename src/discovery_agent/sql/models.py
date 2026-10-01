"""Typed model of what the SQL catalog says about a database object.

Deliberately separate from ``extractors.sql_models``. That module describes
*SQL text* — the objects a statement names, the features it uses. This one
describes *catalog rows* — what the database itself reports about an object
that exists. A table discovered here and a table named in a pre-copy script
are different kinds of knowledge and must not share a type.

Identity is three-part and always present. ``dbo.Customer`` in one pool and
``staging.Customer`` in another are different objects, and nothing in this
module lets them collapse into one.

Nothing here is inferred. A field the catalog did not supply is ``None``, and
the reason it is missing is carried as an issue rather than as a default.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from discovery_agent.discovery_models import SQL_ID_SCHEME
from discovery_agent.extractors.models import ExtractionIssue
from discovery_agent.extractors.sql_models import DynamicSqlSite, SqlObjectReference


class CatalogObjectType(str, Enum):
    """The kinds of database object discovery can enumerate.

    Only TABLE is implemented. The others are declared so the enumeration
    contract does not change shape when they land.
    """

    TABLE = "table"
    VIEW = "view"
    PROCEDURE = "procedure"


class DistributionPolicy(str, Enum):
    """How a dedicated pool spreads a table across its distributions.

    UNKNOWN is a real answer, not a placeholder: the catalog may return a
    code this version does not recognise, or no row at all. When that
    happens the raw code is kept on the table and an issue explains it.

    NOT_APPLICABLE is a different real answer, and the distinction is the
    whole point of keeping both. UNKNOWN means discovery could not establish
    the policy. NOT_APPLICABLE means there is no policy to establish: an
    external table's data lives outside the pool, so it is not distributed
    across anything. Collapsing the two would report a fact about the
    principal's grants as though it were a fact about the table.
    """

    HASH = "hash"
    ROUND_ROBIN = "round_robin"
    REPLICATE = "replicate"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


#: The policies that no ``distribution_policy`` code maps to, because they are
#: not things the distribution view reports. Kept next to the enum so the test
#: that pins the code map has something to exclude by name rather than by a
#: list it repeats.
UNDISTRIBUTED_POLICIES = frozenset(
    {DistributionPolicy.NOT_APPLICABLE, DistributionPolicy.UNKNOWN}
)


#: ``sys.pdw_table_distribution_properties.distribution_policy`` codes.
#:
#: Unrecognised codes are never guessed at — they map to UNKNOWN and the raw
#: value survives on ``SqlTable.distribution_policy_code`` so a later run can
#: interpret it without re-querying.
#: The codes are those documented for the view: 2 = HASH, 3 = REPLICATE,
#: 4 = ROUND_ROBIN. 3 and 4 are *not* in alphabetical or intuitive order, and
#: transposing them is silent — a round-robin table simply reports as
#: replicated — so they are asserted against the documented values in the
#: test suite rather than trusted to read correctly here.
DISTRIBUTION_POLICY_CODES = {
    2: DistributionPolicy.HASH,
    3: DistributionPolicy.REPLICATE,
    4: DistributionPolicy.ROUND_ROBIN,
}


class IndexKind(str, Enum):
    """What one ``sys.indexes`` row is, by its ``type`` code.

    UNKNOWN follows the same rule as everywhere else in this module: a code
    this version has no name for keeps its raw ``type_code`` and ``type_desc``
    on the index rather than being forced into the nearest known kind.
    """

    HEAP = "heap"
    CLUSTERED = "clustered"
    NONCLUSTERED = "nonclustered"
    CLUSTERED_COLUMNSTORE = "clustered_columnstore"
    NONCLUSTERED_COLUMNSTORE = "nonclustered_columnstore"
    UNKNOWN = "unknown"


#: ``sys.indexes.type`` codes. A heap is index_id 0 with type 0; every other
#: value describes an actual index structure.
INDEX_TYPE_CODES = {
    0: IndexKind.HEAP,
    1: IndexKind.CLUSTERED,
    2: IndexKind.NONCLUSTERED,
    5: IndexKind.CLUSTERED_COLUMNSTORE,
    6: IndexKind.NONCLUSTERED_COLUMNSTORE,
}

#: The index_ids that describe how the table's own rows are stored: 0 is a
#: heap, 1 is the clustered index. Anything higher is a secondary index and
#: does not change the base structure.
BASE_INDEX_IDS = (0, 1)


class TableStorage(str, Enum):
    """How a table's own rows are physically stored.

    Derived from the base index (``index_id`` 0 or 1), which every ordinary
    table has exactly one of. Secondary indexes are recorded on the table but
    do not change this.

    Two values need their meaning stated precisely, because a reader could
    otherwise take more from them than discovery established:

    * ``CLUSTERED_COLUMNSTORE`` asserts that the base structure is a clustered
      columnstore index. It does **not** assert that the index is unordered.
      Ordering lives in ``sys.index_columns``, so when that catalog could not
      be read the table keeps this value and carries an issue saying the
      ordering was not determined. A caller that needs certainty reads the
      issues, exactly as it does for a missing column list.
    * ``ORDERED_CLUSTERED_COLUMNSTORE`` is the strictly more specific answer
      and is used only when ordering columns were positively observed.

    NOT_APPLICABLE is for an external table, whose rows are not stored in the
    pool at all. UNKNOWN is for a table whose base index was not returned —
    a gap, not a shape.
    """

    HEAP = "heap"
    CLUSTERED_INDEX = "clustered_index"
    CLUSTERED_COLUMNSTORE = "clustered_columnstore"
    ORDERED_CLUSTERED_COLUMNSTORE = "ordered_clustered_columnstore"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SqlIndexColumn:
    """One column of one index.

    ``key_ordinal`` is 0 for an included column and for every column of a
    columnstore index, which is why it is not used to order columnstore
    output. ``column_store_order_ordinal`` is positive only on an ordered
    clustered columnstore index and is the sole catalog evidence that an
    index is ordered.

    ``name`` is optional for the same reason as elsewhere: the join to
    ``sys.columns`` is a LEFT JOIN, so the column_id survives a name that is
    not visible.
    """

    column_id: int
    name: Optional[str] = None
    key_ordinal: Optional[int] = None
    is_descending: Optional[bool] = None
    column_store_order_ordinal: Optional[int] = None

    @property
    def is_key_column(self) -> bool:
        """Whether this column is part of the index key rather than included."""
        return bool(self.key_ordinal)

    @property
    def is_ordering_column(self) -> bool:
        return bool(self.column_store_order_ordinal)

    def to_dict(self) -> dict:
        return {
            "column_id": self.column_id,
            "name": self.name,
            "key_ordinal": self.key_ordinal,
            "is_descending": self.is_descending,
            "column_store_order_ordinal": self.column_store_order_ordinal,
        }


@dataclass(frozen=True)
class SqlIndex:
    """One row of ``sys.indexes``, with its columns.

    Identified by ``index_id``, never by name: a heap's name is NULL, and a
    name is only unique within a table anyway.

    ``type_code`` and ``type_desc`` are kept alongside the mapped ``kind`` so
    the catalog's own answer survives a code this version does not map.
    """

    index_id: int
    kind: IndexKind = IndexKind.UNKNOWN
    name: Optional[str] = None
    type_code: Optional[int] = None
    type_desc: Optional[str] = None
    is_unique: Optional[bool] = None
    is_primary_key: Optional[bool] = None
    is_unique_constraint: Optional[bool] = None
    columns: Tuple[SqlIndexColumn, ...] = ()

    @property
    def is_base_structure(self) -> bool:
        """Whether this row describes how the table's own rows are stored."""
        return self.index_id in BASE_INDEX_IDS

    @property
    def key_columns(self) -> Tuple[SqlIndexColumn, ...]:
        """The key columns in key order. Empty for a columnstore index."""
        return tuple(
            sorted(
                (c for c in self.columns if c.is_key_column),
                key=lambda c: (c.key_ordinal or 0, c.column_id),
            )
        )

    @property
    def ordering_columns(self) -> Tuple[SqlIndexColumn, ...]:
        """The ORDER BY columns of an ordered clustered columnstore index."""
        return tuple(
            sorted(
                (c for c in self.columns if c.is_ordering_column),
                key=lambda c: (c.column_store_order_ordinal or 0, c.column_id),
            )
        )

    @property
    def is_ordered_columnstore(self) -> bool:
        return (
            self.kind is IndexKind.CLUSTERED_COLUMNSTORE
            and bool(self.ordering_columns)
        )

    def to_dict(self) -> dict:
        return {
            "index_id": self.index_id,
            "kind": self.kind.value,
            "name": self.name,
            "type_code": self.type_code,
            "type_desc": self.type_desc,
            "is_unique": self.is_unique,
            "is_primary_key": self.is_primary_key,
            "is_unique_constraint": self.is_unique_constraint,
            "columns": [c.to_dict() for c in self.columns],
        }


@dataclass(frozen=True)
class SqlObjectKey:
    """Where an object lives, fully qualified.

    The SQL analogue of ``DetectedArtifact``: it says which object we are
    talking about without saying anything about its contents. ``object_id`` is
    the catalog's own handle and is only unique within one database, which is
    why it never appears in the identity.
    """

    database: str
    schema: str
    name: str
    object_type: CatalogObjectType = CatalogObjectType.TABLE
    object_id: Optional[int] = None

    def __post_init__(self) -> None:
        for field in ("database", "schema", "name"):
            if not getattr(self, field):
                raise ValueError(f"a sql object key needs a {field}")

    @property
    def qualified_name(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}"

    @property
    def logical_id(self) -> str:
        """The id a future discovery record joins on.

        Deliberately identical to ``ArtifactIdentity.logical_id`` for a SQL
        object, so the record layer needs no translation when it arrives.
        """
        return f"{SQL_ID_SCHEME}://{self.database}/{self.schema}/{self.name}"

    def to_dict(self) -> dict:
        return {
            "database": self.database,
            "schema": self.schema,
            "name": self.name,
            "object_type": self.object_type.value,
            "object_id": self.object_id,
            "qualified_name": self.qualified_name,
            "logical_id": self.logical_id,
        }


@dataclass(frozen=True)
class SqlColumn:
    """One column, exactly as ``sys.columns`` reported it.

    Types are kept as the catalog's own type name rather than normalised:
    rewriting ``nvarchar`` into some portable vocabulary is a migration
    judgement, and this stage does not make those.
    """

    column_id: int
    name: str
    data_type: Optional[str] = None
    max_length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    is_nullable: Optional[bool] = None
    is_identity: Optional[bool] = None

    def to_dict(self) -> dict:
        return {
            "column_id": self.column_id,
            "name": self.name,
            "data_type": self.data_type,
            "max_length": self.max_length,
            "precision": self.precision,
            "scale": self.scale,
            "is_nullable": self.is_nullable,
            "is_identity": self.is_identity,
        }


@dataclass(frozen=True)
class SqlDistributionColumn:
    """One column of a hash-distributed table's distribution key.

    Deliberately not a flag on ``SqlColumn``. The distribution key is a
    property of the *table* — an ordered list that may have more than one
    member — and ``sys.pdw_column_distribution_properties`` is a different
    catalog view on a different grain from ``sys.columns``. Keeping it
    separate means the column query stays about columns and this stays
    joinable by ``column_id``.

    ``ordinal`` is the catalog's own ``distribution_ordinal``, preserved
    rather than replaced by list position, so a gap or a duplicate in what the
    engine reported survives into the output instead of being smoothed over.

    ``name`` is optional because the join to ``sys.columns`` is a LEFT JOIN: a
    principal who can read the distribution view but not the column it names
    still learns which ``column_id`` the key is on.
    """

    column_id: int
    ordinal: int
    name: Optional[str] = None

    def to_dict(self) -> dict:
        return {"column_id": self.column_id, "ordinal": self.ordinal, "name": self.name}


@dataclass(frozen=True)
class SqlTable:
    """One user table in a dedicated pool.

    ``issues`` are the object's own: a distribution the catalog did not
    report, or columns that were not visible. They stay on the table rather
    than being pooled at run level, so a partially-readable table says so
    itself.
    """

    key: SqlObjectKey
    create_date: Optional[str] = None  # ISO-8601, as reported
    modify_date: Optional[str] = None
    is_external: Optional[bool] = None
    distribution: DistributionPolicy = DistributionPolicy.UNKNOWN
    distribution_policy_code: Optional[int] = None
    distribution_columns: Tuple[SqlDistributionColumn, ...] = ()
    storage: TableStorage = TableStorage.UNKNOWN
    indexes: Tuple[SqlIndex, ...] = ()
    columns: Tuple[SqlColumn, ...] = ()
    issues: Tuple[ExtractionIssue, ...] = ()

    @property
    def column_count(self) -> int:
        return len(self.columns)

    @property
    def is_distributed(self) -> bool:
        """Whether this is a table the pool distributes across its nodes.

        A flag rather than a separate ``CatalogObjectType``: an external table
        is still a table, enumerated by the same query and identified the same
        way. What differs is where its data lives, which is a property of the
        table and not of its identity — so the migration path can branch on it
        without the identity layer having to know external tables exist.
        ``None`` (the catalog did not say) reads as not-external, matching
        ``tables.v1``, and is never asserted to be either.
        """
        return not self.is_external

    @property
    def base_index(self) -> Optional["SqlIndex"]:
        """The index describing how this table's own rows are stored.

        None when the index catalog returned no base row, which is a gap
        rather than a table without storage — the accompanying issue says so.
        """
        return next((i for i in self.indexes if i.is_base_structure), None)

    @property
    def secondary_indexes(self) -> Tuple["SqlIndex", ...]:
        """Indexes beyond the base structure, in index_id order."""
        return tuple(i for i in self.indexes if not i.is_base_structure)

    @property
    def has_columns(self) -> bool:
        return bool(self.columns)

    @property
    def distribution_column_names(self) -> Tuple[str, ...]:
        """The key's column names in ordinal order, skipping any not visible.

        Shorter than this tuple is not the same as shorter than the key —
        ``distribution_columns`` stays the complete record. This is for
        display, and a caller that needs completeness reads that instead.
        """
        return tuple(c.name for c in self.distribution_columns if c.name)

    def to_dict(self) -> dict:
        return {
            **self.key.to_dict(),
            "create_date": self.create_date,
            "modify_date": self.modify_date,
            "is_external": self.is_external,
            "distribution": self.distribution.value,
            "distribution_policy_code": self.distribution_policy_code,
            "distribution_columns": [c.to_dict() for c in self.distribution_columns],
            "storage": self.storage.value,
            "indexes": [i.to_dict() for i in self.indexes],
            "column_count": self.column_count,
            "columns": [c.to_dict() for c in self.columns],
            "issues": [i.to_dict() for i in self.issues],
        }


class DefinitionState(str, Enum):
    """Whether a module's body could be read, and if not, why not.

    The distinction this enum exists for: a view whose ``definition`` is NULL
    is **not** a view without a body. Every view has one. NULL means the text
    was withheld -- the module is encrypted, or the principal lacks VIEW
    DEFINITION -- and reporting that as "no definition" would tell a migration
    planner there is nothing to move.

    ``sys.sql_modules`` has no column distinguishing encryption from a missing
    grant, so OPAQUE covers both and neither is claimed. ABSENT is different
    again: no module row exists at all, which for a view or procedure means
    the catalog did not return one rather than that the object has no body.
    """

    AVAILABLE = "available"  # the text was returned
    OPAQUE = "opaque"  # the row exists, the text was withheld
    UNAVAILABLE = "unavailable"  # sys.sql_modules could not be queried
    ABSENT = "absent"  # no module row for this object


@dataclass(frozen=True)
class SqlModuleDefinition:
    """The body of a view or stored procedure, as the catalog reported it.

    ``text`` is preserved exactly: never reformatted, never normalised, never
    re-parsed into a canonical form. A migration needs the original, and a
    rewrite is a judgement this stage does not make.
    """

    state: DefinitionState = DefinitionState.UNAVAILABLE
    text: Optional[str] = None
    uses_ansi_nulls: Optional[bool] = None
    uses_quoted_identifier: Optional[bool] = None
    is_schema_bound: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.state is DefinitionState.AVAILABLE and not self.text:
            raise ValueError(
                "an available definition must carry its text; an empty body "
                "is indistinguishable from one that was withheld"
            )
        if self.state is not DefinitionState.AVAILABLE and self.text:
            raise ValueError(
                f"a {self.state.value} definition must not carry text"
            )

    @property
    def is_readable(self) -> bool:
        return self.state is DefinitionState.AVAILABLE

    @property
    def line_count(self) -> Optional[int]:
        return None if self.text is None else self.text.count("\n") + 1

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "text": self.text,
            "line_count": self.line_count,
            "uses_ansi_nulls": self.uses_ansi_nulls,
            "uses_quoted_identifier": self.uses_quoted_identifier,
            "is_schema_bound": self.is_schema_bound,
        }


@dataclass(frozen=True)
class SqlParameter:
    """One parameter of a stored procedure, as ``sys.parameters`` reported it.

    ``parameter_id`` 0 is the procedure's return value rather than a
    parameter, and it is kept rather than filtered: dropping it would make a
    procedure that returns a value look identical to one that does not.

    ``name`` is optional because the return value's name is empty.
    """

    parameter_id: int
    name: Optional[str] = None
    data_type: Optional[str] = None
    max_length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    is_output: Optional[bool] = None
    has_default: Optional[bool] = None

    @property
    def is_return_value(self) -> bool:
        return self.parameter_id == 0

    @property
    def direction(self) -> str:
        """IN, OUT or RETURN. ``UNKNOWN`` when the catalog did not say.

        Derived rather than stored, so it cannot disagree with ``is_output``.
        """
        if self.is_return_value:
            return "return"
        if self.is_output is None:
            return "unknown"
        return "out" if self.is_output else "in"

    def to_dict(self) -> dict:
        return {
            "parameter_id": self.parameter_id,
            "name": self.name,
            "data_type": self.data_type,
            "max_length": self.max_length,
            "precision": self.precision,
            "scale": self.scale,
            "is_output": self.is_output,
            "has_default": self.has_default,
            "direction": self.direction,
            "is_return_value": self.is_return_value,
        }


@dataclass(frozen=True)
class SqlModuleObject:
    """Common to the two catalog objects whose content is SQL text.

    A view and a stored procedure differ in what they are for and in what
    else they carry -- a procedure has parameters, a view does not -- but they
    are identified the same way, their bodies come from the same catalog view,
    and the same static observations are made about both. That shared part
    lives here so it is stated once.

    ``referenced_objects`` and ``dynamic_sql`` are *observations about the
    text*, produced by the existing deterministic scanner in
    ``extractors.sql_scanning``. They are never proof: a name in a FROM clause
    is a name, and whether the object exists is the dependency-resolution
    stage's question. A module whose body was withheld has neither, which is
    why ``definition.state`` must be read before either is interpreted.
    """

    key: SqlObjectKey
    create_date: Optional[str] = None  # ISO-8601, as reported
    modify_date: Optional[str] = None
    definition: SqlModuleDefinition = SqlModuleDefinition()
    referenced_objects: Tuple[SqlObjectReference, ...] = ()
    dynamic_sql: Tuple[DynamicSqlSite, ...] = ()
    issues: Tuple[ExtractionIssue, ...] = ()

    @property
    def has_definition(self) -> bool:
        return self.definition.is_readable

    @property
    def is_opaque(self) -> bool:
        """Whether the body exists but could not be read."""
        return self.definition.state is DefinitionState.OPAQUE

    @property
    def uses_dynamic_sql(self) -> bool:
        return bool(self.dynamic_sql)

    @property
    def referenced_names(self) -> Tuple[str, ...]:
        """Distinct durable objects the body names, in sorted order."""
        return tuple(
            sorted({r.qualified_name for r in self.referenced_objects if r.is_durable})
        )

    def _base_dict(self) -> dict:
        return {
            **self.key.to_dict(),
            "create_date": self.create_date,
            "modify_date": self.modify_date,
            "definition": self.definition.to_dict(),
            "referenced_objects": [r.to_dict() for r in self.referenced_objects],
            "referenced_names": list(self.referenced_names),
            "dynamic_sql": [d.to_dict() for d in self.dynamic_sql],
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass(frozen=True)
class SqlView(SqlModuleObject):
    """One user view in a dedicated pool."""

    def to_dict(self) -> dict:
        return self._base_dict()


@dataclass(frozen=True)
class SqlProcedure(SqlModuleObject):
    """One user stored procedure in a dedicated pool.

    ``type_code`` is the catalog's own ``type`` value (``P`` for a Transact-SQL
    procedure), kept so a kind this build has no name for survives.
    """

    parameters: Tuple[SqlParameter, ...] = ()
    type_code: Optional[str] = None

    @property
    def parameter_count(self) -> int:
        """Declared parameters, excluding the return value."""
        return len([p for p in self.parameters if not p.is_return_value])

    @property
    def output_parameters(self) -> Tuple[SqlParameter, ...]:
        return tuple(p for p in self.parameters if p.is_output and not p.is_return_value)

    def to_dict(self) -> dict:
        payload = self._base_dict()
        payload.update(
            {
                "type_code": self.type_code,
                "parameter_count": self.parameter_count,
                "parameters": [p.to_dict() for p in self.parameters],
            }
        )
        return payload


@dataclass(frozen=True)
class CatalogDatabase:
    """The database a catalog source is scoped to.

    One instance per database, because a dedicated pool cannot query across
    databases: enumerating several means several connections, not one
    connection that fans out.
    """

    name: str
    server: str
    collation: Optional[str] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "server": self.server, "collation": self.collation}


@dataclass(frozen=True)
class CatalogPrincipal:
    """Who discovery connected as.

    Recorded because catalog views are security-trimmed: what this principal
    can see *is* what discovery finds, and a short list of tables may mean a
    small database or a narrow grant. Names an identity, never a credential —
    there is no field a token or password could occupy.
    """

    user_name: Optional[str] = None
    login_name: Optional[str] = None

    @property
    def described(self) -> str:
        return self.user_name or self.login_name or "unknown principal"

    def to_dict(self) -> dict:
        return {"user_name": self.user_name, "login_name": self.login_name}
