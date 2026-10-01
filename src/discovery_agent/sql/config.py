"""Where to connect, and how. Explicit configuration, never inference.

Separate from ``DiscoveryConfig``, which documents itself as repository-only
and stays that way: a repository run needs no credentials and must keep
working with none configured.

Nothing in this dataclass can hold a secret. The server is a hostname, the
database is a name, and authentication is a mechanism — so
``safe_description`` is safe by construction rather than by redaction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from discovery_agent.errors import ConfigError
from discovery_agent.sql.auth import EntraAuthMethod

#: The driver discovery asks for by default. Version 18 is the first that
#: defaults to encrypted connections, which is why it is the floor.
DEFAULT_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
DEFAULT_PORT = 1433
DEFAULT_CONNECT_TIMEOUT_SECONDS = 60
DEFAULT_APPLICATION_NAME = "synapse-fabric-discovery"

#: Environment variables the dev command reads. Names only — no variable here
#: is expected to hold a secret, because no supported mechanism uses one.
ENV_SERVER = "SYNAPSE_SQL_SERVER"
ENV_DATABASE = "SYNAPSE_SQL_DATABASE"


@dataclass(frozen=True)
class SqlConnectionConfig:
    """One dedicated SQL pool database, and how to reach it.

    Scoped to a single database on purpose. A dedicated pool cannot query
    across databases, so discovering several means several configurations and
    several connections — not one connection that fans out and quietly
    attributes rows to the wrong place.
    """

    server: str
    database: str
    #: Fixed in practice: ``access_token`` is the only mechanism this build
    #: implements, and it is served by the central Azure credential. The field
    #: stays because the connection string is built from it and because a
    #: future mechanism will be named here -- not because there is a choice.
    authentication: EntraAuthMethod = EntraAuthMethod.ACCESS_TOKEN
    driver: str = DEFAULT_ODBC_DRIVER
    port: int = DEFAULT_PORT
    encrypt: bool = True
    trust_server_certificate: bool = False
    connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS
    application_name: str = DEFAULT_APPLICATION_NAME

    def validate(self) -> None:
        """Raise ConfigError if this configuration cannot open a connection."""
        if not self.server:
            raise ConfigError("a sql connection needs a server")
        if not self.database:
            raise ConfigError("a sql connection needs a database")
        if not self.driver:
            raise ConfigError("a sql connection needs an odbc driver name")
        if self.port <= 0:
            raise ConfigError(f"port must be positive, got: {self.port}")
        if self.connect_timeout_seconds <= 0:
            raise ConfigError(
                f"connect_timeout_seconds must be positive, got: "
                f"{self.connect_timeout_seconds}"
            )
        if not self.encrypt:
            raise ConfigError(
                "encryption cannot be disabled; discovery will not open an "
                "unencrypted connection to a SQL pool"
            )

    @property
    def endpoint(self) -> str:
        """Host and port, as the driver is asked for it."""
        return f"{self.server},{self.port}"

    @property
    def resource_id(self) -> str:
        """What provenance records as the origin of everything discovered here."""
        return f"{self.server}/{self.database}"

    def safe_description(self) -> str:
        """A one-line description fit for a log or a console summary."""
        return (
            f"server={self.server} database={self.database} "
            f"auth={self.authentication.value}"
        )

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "database": self.database,
            "authentication": self.authentication.value,
            "driver": self.driver,
            "port": self.port,
            "encrypt": self.encrypt,
            "trust_server_certificate": self.trust_server_certificate,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "application_name": self.application_name,
            "resource_id": self.resource_id,
        }


def config_from_environment(
    server: Optional[str] = None,
    database: Optional[str] = None,
) -> SqlConnectionConfig:
    """Build a connection config from arguments, falling back to the environment.

    Explicit arguments win; the environment is the convenience. Neither path
    accepts a credential, because no supported mechanism takes one -- and
    neither selects an authentication mechanism, because there is one. Letting
    a caller pick a mechanism here is how a second sign-in path grew last
    time; the identity is the connection layer's to decide, not the
    endpoint configuration's.
    """
    resolved_server = server or os.environ.get(ENV_SERVER, "")
    resolved_database = database or os.environ.get(ENV_DATABASE, "")

    if not resolved_server:
        raise ConfigError(
            f"no sql server configured; pass --server or set {ENV_SERVER}"
        )
    if not resolved_database:
        raise ConfigError(
            f"no sql database configured; pass --database or set {ENV_DATABASE}"
        )

    config = SqlConnectionConfig(
        server=resolved_server,
        database=resolved_database,
    )
    config.validate()
    return config
