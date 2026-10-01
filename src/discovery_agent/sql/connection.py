"""Opening a connection to a dedicated SQL pool.

The seam that keeps ``DedicatedPoolSource`` testable without a database: the
source asks a ``Connector`` for a DB-API connection and never imports a
driver. Tests supply a connector returning deterministic rows; production
supplies ``PyodbcConnector``.

Two rules about credentials shape this module:

* **Secrets never enter the connection string.** Non-secret keywords are
  formatted into it; a token, when a future mechanism uses one, is handed to
  the driver through its out-of-band attribute instead. A connection string
  ends up in logs and exception text; an attribute does not.
* **Values are checked before they are formatted.** A connection string is
  delimited by semicolons and braces, so a value containing either could
  smuggle in another keyword. Such a value is rejected rather than escaped.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Tuple

from discovery_agent.errors import SqlConnectionError, SqlDriverNotAvailableError
from discovery_agent.sql.auth import EntraAuthentication
from discovery_agent.sql.config import SqlConnectionConfig

#: SQL Server's ODBC attribute for a pre-acquired access token. Named here so
#: the token path is visible now and is obviously not the string path, even
#: though no implemented mechanism uses it yet.
SQL_COPT_SS_ACCESS_TOKEN = 1256

_UNSAFE_IN_VALUE = (";", "{", "}", "\x00", "\n", "\r")


class Connector(ABC):
    """Supplies an open DB-API connection to one database."""

    @abstractmethod
    def connect(self) -> Any:
        """Open a connection. Raises SqlConnectionError if it cannot."""

    @abstractmethod
    def describe(self) -> str:
        """A safe one-line description, containing no credential."""


def _checked(keyword: str, value: str) -> str:
    """A connection-string value, or a refusal to build one.

    Escaping is deliberately not attempted. These values come from explicit
    configuration, so a delimiter in one is a mistake worth surfacing rather
    than a case worth handling.
    """
    text = str(value)
    for character in _UNSAFE_IN_VALUE:
        if character in text:
            raise SqlConnectionError(
                f"connection setting {keyword!r} contains a character that is "
                f"not valid in a connection string"
            )
    return text


def build_connection_string(
    config: SqlConnectionConfig, authentication: EntraAuthentication
) -> str:
    """The ODBC connection string, carrying no credential.

    Safe to log as-is, and that is not an accident: every keyword placed here
    is a hostname, a database name, a driver name or a mechanism name. The
    function has no parameter a secret could arrive through.
    """
    keywords: Tuple[Tuple[str, str], ...] = (
        ("Driver", f"{{{config.driver}}}"),
        ("Server", f"tcp:{_checked('server', config.endpoint)}"),
        ("Database", _checked("database", config.database)),
        ("Encrypt", "yes" if config.encrypt else "no"),
        (
            "TrustServerCertificate",
            "yes" if config.trust_server_certificate else "no",
        ),
        ("Connection Timeout", str(config.connect_timeout_seconds)),
        ("APP", _checked("application_name", config.application_name)),
    )

    parts = [f"{key}={value}" for key, value in keywords]
    for key, value in authentication.odbc_keywords().items():
        parts.append(f"{_checked(key, key)}={_checked(key, value)}")
    return ";".join(parts) + ";"


class PyodbcConnector(Connector):
    """Opens the real connection, through pyodbc.

    pyodbc is an optional dependency: the repository-only discovery run must
    keep working on a machine that has no ODBC driver installed, so the import
    happens here, at connect time, rather than at module import.
    """

    def __init__(
        self, config: SqlConnectionConfig, authentication: EntraAuthentication
    ) -> None:
        config.validate()
        self.config = config
        self.authentication = authentication

    def describe(self) -> str:
        return f"{self.config.safe_description()} via {self.config.driver}"

    def connect(self) -> Any:
        pyodbc = self._driver_module()
        connection_string = build_connection_string(self.config, self.authentication)
        attributes: Mapping[int, Any] = {}
        token = self.authentication.access_token()
        if token is not None:
            # Out of band, never in the string. No implemented mechanism
            # reaches this today; the path exists so the next one cannot be
            # tempted to take the easy, leaky route.
            attributes = {SQL_COPT_SS_ACCESS_TOKEN: token}

        try:
            return pyodbc.connect(
                connection_string,
                timeout=self.config.connect_timeout_seconds,
                attrs_before=dict(attributes),
                readonly=True,
            )
        except Exception as exc:  # pyodbc.Error and anything the driver raises
            # The message is the driver's, and the string it was built from
            # carries no credential — but it is still not echoed here, so that
            # this stays true if a future mechanism changes the string.
            raise SqlConnectionError(
                f"could not connect to {self.config.safe_description()}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _driver_module() -> Any:
        try:
            import pyodbc  # noqa: PLC0415 - optional dependency, imported on use
        except ImportError as exc:
            raise SqlDriverNotAvailableError(
                "pyodbc is required for live SQL discovery and is not "
                "installed; install the optional extra with "
                "`pip install -e .[sql]`, and ensure a Microsoft ODBC driver "
                "for SQL Server is present"
            ) from exc
        return pyodbc
