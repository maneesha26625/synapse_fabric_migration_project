"""Live discovery of the SQL estate, straight from the database.

The three SQL-primary P0 artifacts — dedicated SQL tables, views and stored
procedures — have no representation in Git and none in the Synapse Artifacts
API. Their definitions exist only inside the database, so discovering them
means connecting to it. See ``discovery_agent.source_strategy``.

This package is deliberately parallel to ``discovery_agent.extractors`` rather
than part of it. That package is keyed on ``AssetType`` and shaped around
files that were walked, detected and read as bytes; a catalog object has no
file, no bytes and no ``AssetType``. Bending one into the other would damage
both, so the SQL estate gets its own source contract and the repository chain
is left exactly as it was.

Modules:
  queries        the fixed, named, SELECT-only catalog queries. All of them
  source         the CatalogSource contract: rows for one database
  models         typed catalog objects, identified by database.schema.name
  mapping        rows to models; pure functions, no I/O
  auth           Entra ID mechanisms; structurally unable to hold a secret.
                 It acquires no credential: the token arrives as a callable
                 from discovery_agent.connections
  config         where to connect, explicitly
  connection     opening the connection, behind a testable seam
  dedicated_pool the live implementation for a Synapse dedicated SQL pool
  result         what one discovery run found, and what it could not establish

Tables, views and stored procedures are implemented, with their columns,
distribution, base storage and index structure, module bodies and parameters.
Partitions, statistics, constraints, security and row counts are further
entries in ``queries`` and further fields on the models — not further
architecture.

Three distinctions run through the module discovery and are worth stating
once. A NULL ``sys.sql_modules.definition`` means the body was *withheld*, not
that there is none. ``sys.sql_modules`` and ``sys.parameters`` are separately
grantable, so each degrades on its own. And an enumeration that fails raises,
because an empty list would assert the database has none of that object kind.
"""

from discovery_agent.sql.auth import (
    AccessTokenAuthentication,
    EntraAuthentication,
    EntraAuthMethod,
    TokenProvider,
    authentication_for,
    encode_odbc_access_token,
)
from discovery_agent.sql.config import SqlConnectionConfig, config_from_environment
from discovery_agent.sql.connection import (
    Connector,
    PyodbcConnector,
    build_connection_string,
)
from discovery_agent.sql.dedicated_pool import DedicatedPoolSource
from discovery_agent.sql.models import (
    CatalogDatabase,
    CatalogObjectType,
    CatalogPrincipal,
    DefinitionState,
    DistributionPolicy,
    SqlColumn,
    SqlModuleDefinition,
    SqlObjectKey,
    SqlParameter,
    SqlProcedure,
    SqlTable,
    SqlView,
)
from discovery_agent.sql.queries import (
    CATALOG_QUERIES,
    CatalogQuery,
    catalog_query,
    is_registered,
    registry_summary,
)
from discovery_agent.sql.result import SqlCatalogDiscovery
from discovery_agent.sql.source import SQL_SOURCE_FORMAT, CatalogSource

__all__ = [
    "CATALOG_QUERIES",
    "AccessTokenAuthentication",
    "CatalogDatabase",
    "CatalogObjectType",
    "CatalogPrincipal",
    "CatalogQuery",
    "CatalogSource",
    "Connector",
    "DedicatedPoolSource",
    "DefinitionState",
    "DistributionPolicy",
    "EntraAuthMethod",
    "EntraAuthentication",
    "PyodbcConnector",
    "SQL_SOURCE_FORMAT",
    "SqlCatalogDiscovery",
    "SqlColumn",
    "SqlConnectionConfig",
    "SqlModuleDefinition",
    "SqlObjectKey",
    "SqlParameter",
    "SqlProcedure",
    "SqlTable",
    "SqlView",
    "TokenProvider",
    "authentication_for",
    "build_connection_string",
    "catalog_query",
    "config_from_environment",
    "encode_odbc_access_token",
    "is_registered",
    "registry_summary",
]
