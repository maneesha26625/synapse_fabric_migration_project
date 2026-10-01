"""DedicatedPoolSource: live catalog rows from a Synapse dedicated SQL pool.

The first source that reaches outside the local filesystem. It holds a
connection and runs registered queries; it assembles no models, because that
work is pure and belongs in ``mapping`` where it can be tested without a
database.

Degradation is explicit. A query that fails is recorded as an issue and the
run continues wherever continuing still produces truthful output — except for
the table enumeration itself, whose failure means there is nothing to report
and which therefore raises. Nothing is ever downgraded into a plausible
default.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, List, Optional, Sequence, Tuple

from discovery_agent.errors import CatalogQueryError, SqlDiscoveryError
from discovery_agent.extractors.models import (
    ExtractionIssue,
    ExtractionProvenance,
    IssueCode,
)
from discovery_agent.sql import queries
from discovery_agent.sql.config import SqlConnectionConfig
from discovery_agent.sql.connection import Connector
from discovery_agent.sql.models import (
    CatalogDatabase,
    CatalogObjectType,
    CatalogPrincipal,
    SqlObjectKey,
    SqlProcedure,
    SqlTable,
    SqlView,
)
from discovery_agent.sql.mapping import (
    build_procedures,
    build_tables,
    build_views,
    database_from_row,
    module_key_from_row,
    principal_from_row,
    table_key_from_row,
    visibility_issue,
)
from discovery_agent.sql.queries import CatalogQuery
from discovery_agent.sql.result import SqlCatalogDiscovery
from discovery_agent.sql.source import SQL_SOURCE_FORMAT, CatalogRow, CatalogSource

SOURCE_NAME = "dedicated_pool"
SOURCE_VERSION = "1.0.0"


class DedicatedPoolSource(CatalogSource):
    """Reads one database on a Synapse dedicated SQL pool.

    One instance per database, matching the engine: a dedicated pool cannot
    query across databases, so a workspace with several pools needs several
    sources rather than one that fans out.

    The connection is opened lazily and reused. Nothing here caches rows —
    a source answers questions; deciding how often to ask is the caller's.
    """

    def __init__(self, config: SqlConnectionConfig, connector: Connector) -> None:
        config.validate()
        self.config = config
        self.connector = connector
        self._connection: Any = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "DedicatedPoolSource":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _open(self) -> Any:
        if self._connection is None:
            self._connection = self.connector.connect()
        return self._connection

    def close(self) -> None:
        """Close the connection if one was opened. Safe to call twice."""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - a failed close must not mask results
            pass

    def describe(self) -> str:
        """A safe one-line description. Contains no credential."""
        return self.connector.describe()

    # -- the CatalogSource contract ---------------------------------------

    def rows(self, query: CatalogQuery, *parameters: Any) -> Tuple[CatalogRow, ...]:
        """Run one registered catalog query.

        The guard runs before the connection is touched, so a rejected query
        never reaches the database — the refusal is not merely a failed
        execution.
        """
        self.check_query(query, parameters)

        cursor = self._open().cursor()
        try:
            if parameters:
                cursor.execute(query.sql, tuple(parameters))
            else:
                cursor.execute(query.sql)
            description = cursor.description or ()
            names = [column[0] for column in description]
            fetched = cursor.fetchall()
        except CatalogQueryError:
            raise
        except Exception as exc:  # the driver's error type is not ours to know
            raise CatalogQueryError(
                f"catalog query {query.qualified_name} failed on "
                f"{self.config.resource_id}: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001 - never mask the real outcome
                pass

        return tuple(dict(zip(names, tuple(row))) for row in fetched)

    def database(self) -> CatalogDatabase:
        """The database this source is scoped to, as the server reports it."""
        try:
            fetched = self.rows(queries.DATABASE)
        except CatalogQueryError:
            fetched = ()
        return database_from_row(
            fetched[0] if fetched else None,
            server=self.config.server,
            fallback=self.config.database,
        )

    def principal(self) -> CatalogPrincipal:
        """Who this source connected as."""
        try:
            fetched = self.rows(queries.PRINCIPAL)
        except CatalogQueryError:
            fetched = ()
        return principal_from_row(fetched[0] if fetched else None)

    def objects(self, object_type: CatalogObjectType) -> Tuple[SqlObjectKey, ...]:
        """Enumerate visible objects of one kind.

        Identity only -- no columns, no definitions, no parameters. A caller
        that wants those asks for the composed discovery instead, which is
        where the degradation rules live.

        An enumeration query that fails raises rather than returning an empty
        tuple, because an empty tuple would read as "this database has none".
        """
        keys: List[SqlObjectKey] = []
        if object_type is CatalogObjectType.TABLE:
            for row in self.rows(queries.TABLES):
                try:
                    keys.append(table_key_from_row(self.config.database, row))
                except ValueError:
                    continue  # counted and reported by the full discovery path
            return tuple(keys)

        query = {
            CatalogObjectType.VIEW: queries.VIEWS,
            CatalogObjectType.PROCEDURE: queries.PROCEDURES,
        }.get(object_type)
        if query is None:  # pragma: no cover - the enum has three members
            raise SqlDiscoveryError(
                f"enumerating {object_type.value}s is not implemented"
            )
        for row in self.rows(query):
            try:
                keys.append(
                    module_key_from_row(self.config.database, row, object_type)
                )
            except ValueError:
                continue  # counted and reported by the full discovery path
        return tuple(keys)

    def provenance_for(self, key: Optional[SqlObjectKey] = None) -> ExtractionProvenance:
        """Where information came from, precise enough to fetch it again.

        ``resource_id`` names the server and database for the run, and the
        fully qualified object when one is given — so a record never has to
        guess which pool a table called ``dbo.Customer`` came from.
        """
        resource_id = self.config.resource_id
        if key is not None:
            resource_id = f"{resource_id}/{key.schema}/{key.name}"
        return ExtractionProvenance(
            source_type=self.source_type,
            source_format=SQL_SOURCE_FORMAT,
            source_path=None,  # a catalog object has no path
            resource_id=resource_id,
        )

    # -- the vertical slice ------------------------------------------------

    def discover_tables(self) -> SqlCatalogDiscovery:
        """Connect, enumerate every visible user table, and describe its columns.

        The one composed operation this slice provides. Returns what was
        found together with what could not be established, so a caller never
        has to infer completeness from a count.
        """
        issues: List[ExtractionIssue] = []

        database = self.database()
        principal = self.principal()

        try:
            table_rows: Sequence[CatalogRow] = self.rows(queries.TABLES)
        except CatalogQueryError as exc:
            # Without the enumeration there is nothing truthful to return:
            # an empty inventory would assert the database is empty.
            raise SqlDiscoveryError(
                f"could not enumerate tables in {self.config.resource_id}: {exc}"
            ) from exc

        columns_readable = True
        try:
            column_rows: Sequence[CatalogRow] = self.rows(queries.COLUMNS)
        except CatalogQueryError as exc:
            columns_readable = False
            column_rows = ()
            issues.append(
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    f"columns could not be retrieved: {exc}",
                    f"{database.name}.columns",
                )
            )

        distribution_columns_readable = True
        try:
            distribution_column_rows: Sequence[CatalogRow] = self.rows(
                queries.DISTRIBUTION_COLUMNS
            )
        except CatalogQueryError as exc:
            # Separately grantable from sys.columns, so it degrades on its own
            # rather than taking the column list down with it. Which tables
            # this actually costs is decided per table in the mapping layer:
            # a round-robin table loses nothing.
            distribution_columns_readable = False
            distribution_column_rows = ()
            issues.append(
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    f"distribution columns could not be retrieved: {exc}",
                    f"{database.name}.distribution_columns",
                )
            )

        indexes_readable = True
        try:
            index_rows: Sequence[CatalogRow] = self.rows(queries.INDEXES)
        except CatalogQueryError as exc:
            indexes_readable = False
            index_rows = ()
            issues.append(
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    f"indexes could not be retrieved: {exc}",
                    f"{database.name}.indexes",
                )
            )

        index_columns_readable = True
        try:
            index_column_rows: Sequence[CatalogRow] = self.rows(queries.INDEX_COLUMNS)
        except CatalogQueryError as exc:
            # Runs even when the index list failed. The two queries touch
            # different views and one can be denied or unsupported without the
            # other, so neither is made conditional on the other succeeding.
            index_columns_readable = False
            index_column_rows = ()
            issues.append(
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    f"index columns could not be retrieved: {exc}",
                    f"{database.name}.index_columns",
                )
            )

        tables, mapping_issues = build_tables(
            database=database.name,
            table_rows=table_rows,
            column_rows=column_rows,
            columns_readable=columns_readable,
            distribution_column_rows=distribution_column_rows,
            distribution_columns_readable=distribution_columns_readable,
            index_rows=index_rows,
            index_column_rows=index_column_rows,
            indexes_readable=indexes_readable,
            index_columns_readable=index_columns_readable,
        )
        issues.extend(mapping_issues)

        empty = visibility_issue(database.name, principal, len(tables))
        if empty is not None:
            issues.append(empty)

        return SqlCatalogDiscovery(
            database=database,
            principal=principal,
            tables=tables,
            provenance=self.provenance_for(),
            issues=tuple(issues),
        )

    def _optional_rows(
        self,
        query: CatalogQuery,
        issues: List[ExtractionIssue],
        location: str,
    ) -> Tuple[Tuple[CatalogRow, ...], bool]:
        """Run one query, recording a failure instead of raising.

        The pattern the table path already uses for columns, indexes and
        distribution columns, named once here because views and procedures
        need it four more times. Returns the rows and whether they are
        trustworthy -- ``False`` means the caller must not treat an empty
        result as a finding.
        """
        try:
            return self.rows(query), True
        except CatalogQueryError as exc:
            issues.append(
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    f"{location} could not be retrieved: {exc}",
                    location,
                )
            )
            return (), False

    def discover_views(
        self, database: Optional[str] = None
    ) -> Tuple[Tuple[SqlView, ...], Tuple[ExtractionIssue, ...]]:
        """Every visible view, with its definition where it can be read.

        The enumeration failing raises, for the same reason the table
        enumeration does: without it there is nothing truthful to return, and
        an empty list would assert the database has no views. The *body*
        failing does not -- ``sys.sql_modules`` is separately grantable, and a
        view whose text was withheld is still a view worth reporting.
        """
        name = database or self.config.database
        issues: List[ExtractionIssue] = []

        try:
            view_rows: Sequence[CatalogRow] = self.rows(queries.VIEWS)
        except CatalogQueryError as exc:
            raise SqlDiscoveryError(
                f"could not enumerate views in {self.config.resource_id}: {exc}"
            ) from exc

        module_rows, modules_readable = self._optional_rows(
            queries.MODULES, issues, f"{name}.modules"
        )
        views, mapping_issues = build_views(
            database=name,
            view_rows=view_rows,
            module_rows=module_rows,
            modules_readable=modules_readable,
        )
        return views, tuple(issues) + mapping_issues

    def discover_procedures(
        self, database: Optional[str] = None
    ) -> Tuple[Tuple[SqlProcedure, ...], Tuple[ExtractionIssue, ...]]:
        """Every visible stored procedure, with its body and signature.

        Bodies and parameters degrade independently of each other and of the
        enumeration: a principal may read ``sys.procedures`` and neither of
        the other two, and the result then names every procedure while saying
        plainly that it knows neither what they do nor what they take.
        """
        name = database or self.config.database
        issues: List[ExtractionIssue] = []

        try:
            procedure_rows: Sequence[CatalogRow] = self.rows(queries.PROCEDURES)
        except CatalogQueryError as exc:
            raise SqlDiscoveryError(
                f"could not enumerate procedures in {self.config.resource_id}: {exc}"
            ) from exc

        module_rows, modules_readable = self._optional_rows(
            queries.MODULES, issues, f"{name}.modules"
        )
        parameter_rows, parameters_readable = self._optional_rows(
            queries.PARAMETERS, issues, f"{name}.parameters"
        )
        procedures, mapping_issues = build_procedures(
            database=name,
            procedure_rows=procedure_rows,
            module_rows=module_rows,
            parameter_rows=parameter_rows,
            modules_readable=modules_readable,
            parameters_readable=parameters_readable,
        )
        return procedures, tuple(issues) + mapping_issues

    def discover(self) -> SqlCatalogDiscovery:
        """Every SQL-primary P0 object in this database: tables, views, procedures.

        The composed operation the discovery pipeline calls. Built on top of
        ``discover_tables`` rather than replacing it, so the table path and
        everything that depends on it are unchanged.

        Each of the three object kinds degrades on its own. A principal who
        may read tables but not views gets every table, no view, and an issue
        saying views could not be enumerated -- never an empty view list that
        reads as "this database has none".
        """
        discovery = self.discover_tables()
        issues: List[ExtractionIssue] = list(discovery.issues)

        views: Tuple[SqlView, ...] = ()
        try:
            views, view_issues = self.discover_views(discovery.database.name)
            issues.extend(view_issues)
        except SqlDiscoveryError as exc:
            issues.append(
                ExtractionIssue(
                    IssueCode.SOURCE_UNAVAILABLE,
                    f"views could not be enumerated: {exc}. No claim is made "
                    f"about how many views this database has",
                    f"{discovery.database.name}.views",
                )
            )

        procedures: Tuple[SqlProcedure, ...] = ()
        try:
            procedures, procedure_issues = self.discover_procedures(
                discovery.database.name
            )
            issues.extend(procedure_issues)
        except SqlDiscoveryError as exc:
            issues.append(
                ExtractionIssue(
                    IssueCode.SOURCE_UNAVAILABLE,
                    f"stored procedures could not be enumerated: {exc}. No "
                    f"claim is made about how many this database has",
                    f"{discovery.database.name}.procedures",
                )
            )

        return replace(
            discovery, views=views, procedures=procedures, issues=tuple(issues)
        )
