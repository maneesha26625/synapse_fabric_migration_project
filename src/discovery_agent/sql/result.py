"""What one live SQL discovery run found, and what it could not establish.

Shaped like an ``ExtractionResult`` without pretending to be one: it carries
content, provenance and issues, but it is not keyed on an ``AssetType`` and
does not enter the extractor registry. Bridging these tables into
``UnifiedDiscoveryRecord`` is the next step, not this one.

``issues`` are run-level — the things that are true of the discovery rather
than of any one table. A table's own gaps stay on the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from discovery_agent.extractors.models import ExtractionIssue, ExtractionProvenance
from discovery_agent.sql.models import (
    CatalogDatabase,
    CatalogPrincipal,
    SqlObjectKey,
    SqlProcedure,
    SqlTable,
    SqlView,
)


@dataclass(frozen=True)
class SqlCatalogDiscovery:
    """Every SQL-primary object discovered in one database.

    Three of the nine P0 artifacts live here -- tables, views and stored
    procedures -- because none of them has a Git or a Synapse Artifacts
    representation at all.

    ``views`` and ``procedures`` default to empty so the table-only discovery
    path keeps producing a valid result. An empty tuple therefore means
    "nothing was asked for or nothing was found", and which of the two it was
    is answered by ``issues``, never by the count.
    """

    database: CatalogDatabase
    principal: CatalogPrincipal
    provenance: ExtractionProvenance
    tables: Tuple[SqlTable, ...] = ()
    views: Tuple[SqlView, ...] = ()
    procedures: Tuple[SqlProcedure, ...] = ()
    issues: Tuple[ExtractionIssue, ...] = ()

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def view_count(self) -> int:
        return len(self.views)

    @property
    def procedure_count(self) -> int:
        return len(self.procedures)

    @property
    def object_count(self) -> int:
        """Every discovered catalog object, of any kind."""
        return self.table_count + self.view_count + self.procedure_count

    @property
    def opaque_modules(self) -> Tuple[str, ...]:
        """Views and procedures whose body exists but could not be read.

        Named rather than counted, because each one is a specific gap a
        migration plan has to account for.
        """
        return tuple(
            module.key.qualified_name
            for module in self.views + self.procedures
            if module.is_opaque
        )

    def view(self, schema: str, name: str) -> Optional[SqlView]:
        return next(
            (v for v in self.views if v.key.schema == schema and v.key.name == name),
            None,
        )

    def procedure(self, schema: str, name: str) -> Optional[SqlProcedure]:
        return next(
            (p for p in self.procedures if p.key.schema == schema and p.key.name == name),
            None,
        )

    @property
    def column_count(self) -> int:
        return sum(table.column_count for table in self.tables)

    @property
    def schemas(self) -> Tuple[str, ...]:
        """Every schema holding a discovered object, of any kind."""
        return tuple(
            sorted(
                {table.key.schema for table in self.tables}
                | {view.key.schema for view in self.views}
                | {procedure.key.schema for procedure in self.procedures}
            )
        )

    @property
    def all_issues(self) -> Tuple[ExtractionIssue, ...]:
        """Run-level issues followed by every object's own, in object order."""
        return (
            self.issues
            + tuple(issue for table in self.tables for issue in table.issues)
            + tuple(issue for view in self.views for issue in view.issues)
            + tuple(issue for p in self.procedures for issue in p.issues)
        )

    @property
    def is_complete(self) -> bool:
        """Whether anything at all went unestablished.

        False does not mean the run failed — it means the output has gaps
        that a reader must not mistake for findings.
        """
        return not self.all_issues

    def table(self, schema: str, name: str) -> Optional[SqlTable]:
        """One table by schema and name. Both are needed; names repeat."""
        return next(
            (
                t
                for t in self.tables
                if t.key.schema == schema and t.key.name == name
            ),
            None,
        )

    def by_logical_id(self, logical_id: str) -> Optional[SqlTable]:
        return next((t for t in self.tables if t.key.logical_id == logical_id), None)

    @property
    def keys(self) -> Tuple[SqlObjectKey, ...]:
        """Every discovered object's identity, tables then views then procedures."""
        return (
            tuple(table.key for table in self.tables)
            + tuple(view.key for view in self.views)
            + tuple(procedure.key for procedure in self.procedures)
        )

    def summary(self) -> dict:
        """A compact overview. Carries no column detail and no identity secret."""
        return {
            "database": self.database.name,
            "server": self.database.server,
            "collation": self.database.collation,
            "table_count": self.table_count,
            "view_count": self.view_count,
            "procedure_count": self.procedure_count,
            "column_count": self.column_count,
            "opaque_modules": list(self.opaque_modules),
            "schemas": list(self.schemas),
            "issue_count": len(self.all_issues),
            "complete": self.is_complete,
            "resource_id": self.provenance.resource_id,
        }

    def to_dict(self) -> dict:
        return {
            "database": self.database.to_dict(),
            "principal": self.principal.to_dict(),
            "provenance": self.provenance.to_dict(),
            "tables": [table.to_dict() for table in self.tables],
            "views": [view.to_dict() for view in self.views],
            "procedures": [p.to_dict() for p in self.procedures],
            "issues": [issue.to_dict() for issue in self.issues],
        }
