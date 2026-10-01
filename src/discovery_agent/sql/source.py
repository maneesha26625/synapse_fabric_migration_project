"""The CatalogSource contract: where SQL catalog rows come from.

A sibling of ``extractors.sources.ArtifactSource``, not a subclass, and the
difference is the point. An artifact source answers "give me this file's
bytes". A catalog source answers "give me these rows" — because a table has
no bytes. Its definition is a set of rows across several catalog views, and
pretending otherwise would push the assembling work into the source.

The contract is scoped to **one database**. A dedicated pool cannot query
across databases, so several databases means several sources.

``rows`` takes a ``CatalogQuery`` from the registry and nothing else. That is
what makes "discovery cannot run arbitrary SQL" checkable rather than merely
intended: there is no parameter a caller could pass SQL text through.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Tuple

from discovery_agent.errors import CatalogQueryError
from discovery_agent.extractors.models import ExtractionProvenance, SourceType
from discovery_agent.sql.models import (
    CatalogDatabase,
    CatalogObjectType,
    CatalogPrincipal,
    SqlObjectKey,
)
from discovery_agent.sql.queries import CatalogQuery, is_registered

#: Recorded on every provenance this source produces, so a record can say the
#: rows came from a live catalog rather than from a file.
SQL_SOURCE_FORMAT = "sql_catalog"

#: A row as the source hands it back: column alias to value, already decoded
#: from whatever the driver returned. Mapping rather than a driver row object,
#: so nothing downstream depends on pyodbc.
CatalogRow = Mapping[str, Any]


class CatalogSource(ABC):
    """Supplies catalog rows for one database to whatever assembles models."""

    #: Every catalog source reads the SQL endpoint, by definition.
    source_type: SourceType = SourceType.SQL

    @abstractmethod
    def database(self) -> CatalogDatabase:
        """The database this source is scoped to."""

    @abstractmethod
    def principal(self) -> CatalogPrincipal:
        """Who this source connected as.

        Not decoration: catalog views are security-trimmed, so the principal
        is the boundary of what discovery is able to see at all.
        """

    @abstractmethod
    def objects(self, object_type: CatalogObjectType) -> Tuple[SqlObjectKey, ...]:
        """Enumerate visible objects of one kind.

        The SQL equivalent of the repository walk plus the detector: there is
        no file to find and no verdict to reach, so enumeration is a query.
        """

    @abstractmethod
    def rows(self, query: CatalogQuery, *parameters: Any) -> Tuple[CatalogRow, ...]:
        """Run one registered catalog query and return its rows."""

    @abstractmethod
    def provenance_for(self, key: SqlObjectKey) -> ExtractionProvenance:
        """Where one object's information came from, precisely enough to re-fetch."""

    # -- shared guard ------------------------------------------------------

    @staticmethod
    def check_query(query: CatalogQuery, parameters: Tuple[Any, ...]) -> None:
        """Refuse anything that is not a registered query, correctly bound.

        Identity, not equality: a ``CatalogQuery`` a caller constructed is
        caller-supplied SQL however closely it resembles a registered one, and
        caller-supplied SQL is exactly what this design exists to prevent.

        Implementations call this before executing. It lives on the base class
        so no implementation can forget it and still look correct.
        """
        if not isinstance(query, CatalogQuery):
            raise CatalogQueryError(
                f"catalog queries must be CatalogQuery instances from the "
                f"registry, got {type(query).__name__}"
            )
        if not is_registered(query):
            raise CatalogQueryError(
                f"{query.name!r} is not a registered catalog query; discovery "
                f"runs only the fixed queries in discovery_agent.sql.queries"
            )
        if len(parameters) != len(query.parameters):
            raise CatalogQueryError(
                f"{query.qualified_name} takes {len(query.parameters)} "
                f"parameter(s), got {len(parameters)}"
            )
