"""The SQL endpoint, connected with the run's Entra identity.

This module is a *composition*, not a reimplementation. Everything it needs
already exists in ``discovery_agent.sql`` and stays there:

* ``SqlConnectionConfig`` -- where to connect
* ``AccessTokenAuthentication`` -- how the token reaches the driver
* ``PyodbcConnector`` -- opening the connection, with the token out of band
* ``DedicatedPoolSource`` -- the ``CatalogSource`` the discovery code takes

What changes is where the token comes from. Before this package existed, the
only implemented mechanism was interactive sign-in, which meant a browser
prompt per run and a second identity alongside the Azure one. Now the SQL
connection asks ``AzureConnection`` for a token for the
``database.windows.net`` audience, and the developer's existing ``az login``
covers every connection the run makes.

Three properties are worth stating because tests assert them:

* the connection string contains no token, no password and no ``UID``;
* nobody pastes a token in -- it is acquired, per connect, from the credential;
* no catalog query, model or mapping moved here. This module connects and
  hands back a source. What to ask the database is ``sql.queries``' business.
"""

from __future__ import annotations

from typing import Any, Optional

from discovery_agent.connections.azure import SQL_SCOPE, AzureCredentialProvider
from discovery_agent.connections.models import SynapseConnectionConfig
from discovery_agent.connections.validation import (
    ConnectionValidation,
    ErrorCategory,
    failed,
    ok,
)
from discovery_agent.errors import (
    AzureAuthenticationError,
    AzureDependencyNotAvailableError,
    ConfigError,
    SqlAuthenticationError,
    SqlConnectionError,
    SqlDriverNotAvailableError,
)
from discovery_agent.extractors.models import SourceType
from discovery_agent.sql.auth import EntraAuthMethod, authentication_for
from discovery_agent.sql.config import SqlConnectionConfig
from discovery_agent.sql.connection import Connector, PyodbcConnector
from discovery_agent.sql.dedicated_pool import DedicatedPoolSource

#: The one statement validation runs. It touches no user object, needs no
#: permission beyond connecting, and proves the thing being validated: that a
#: session was established against the expected database.
VALIDATION_STATEMENT = "SELECT DB_NAME() AS database_name, SUSER_SNAME() AS principal"


class SqlConnection:
    """Opens sessions against one dedicated SQL pool database.

    Scoped to a single database, because the pool is: discovering two
    databases means two of these, which is the same rule
    ``DedicatedPoolSource`` already follows.

    The connector is injectable so unit tests never need a driver, exactly as
    ``DedicatedPoolSource`` allows today.
    """

    def __init__(
        self,
        config: SqlConnectionConfig,
        credential: AzureCredentialProvider,
        connector: Optional[Connector] = None,
    ) -> None:
        config.validate()
        if config.authentication is not EntraAuthMethod.ACCESS_TOKEN:
            # Not a style preference. An interactive config routed through
            # this class would open a browser prompt and connect as whoever
            # answered it, which may not be the identity everything else in
            # the run used -- and the discovery output would not say so.
            raise ConfigError(
                f"the central SQL connection authenticates with an Entra "
                f"access token from the Azure credential; this configuration "
                f"asks for {config.authentication.value!r}. Use "
                f"SynapseConnectionConfig.sql_connection_config(), or the "
                f"standalone `python -m discovery_agent.sql` command for "
                f"interactive sign-in."
            )
        self.config = config
        #: The run's credential provider, not a connection. A SQL session
        #: needs a token and nothing else; handing this class an
        #: ``AzureConnection`` would give it a subscription and an ARM
        #: transport it has no business using, and would make a SQL-only
        #: caller invent a subscription id to get a token.
        self.credential = credential
        self._connector = connector

    @classmethod
    def for_workspace(
        cls,
        synapse: SynapseConnectionConfig,
        credential: AzureCredentialProvider,
        connector: Optional[Connector] = None,
    ) -> "SqlConnection":
        """The connection for a workspace's configured dedicated pool."""
        return cls(synapse.sql_connection_config(), credential, connector=connector)

    # -- the pieces --------------------------------------------------------

    @property
    def token_scope(self) -> str:
        return SQL_SCOPE

    def authentication(self):
        """Entra access-token authentication, backed by the run's credential.

        The authentication object holds a callable rather than a token, so
        nothing here retains a credential between connections and a long run
        picks up a refreshed token by itself.
        """
        return authentication_for(
            EntraAuthMethod.ACCESS_TOKEN,
            token_provider=self.credential.token_provider_for(SQL_SCOPE),
        )

    def connector(self) -> Connector:
        """The connector this connection opens sessions with."""
        if self._connector is not None:
            return self._connector
        return PyodbcConnector(self.config, self.authentication())

    def source(self) -> DedicatedPoolSource:
        """The existing ``CatalogSource`` for this database.

        The hand-off point between the connection layer and discovery. What
        comes back is the unchanged ``DedicatedPoolSource``, so every catalog
        query, mapping rule and test that exists today applies to it -- the
        only thing this package changed is how its connection was opened.
        """
        return DedicatedPoolSource(self.config, self.connector())

    def connect(self) -> Any:
        """Open one DB-API connection. The caller closes it."""
        return self.connector().connect()

    def describe(self) -> str:
        """A safe one-line description. Contains no credential."""
        return f"{self.config.safe_description()} via {self.config.driver}"

    # -- validation --------------------------------------------------------

    def validate(self) -> ConnectionValidation:
        """Open a session, ask the server who and where we are, close it.

        ``SELECT DB_NAME()`` rather than a catalog view on purpose: this
        method validates *connectivity*, and a query needing a grant would
        fail for a reason that has nothing to do with whether the connection
        works. Catalog visibility is discovery's problem and is already
        reported there.
        """
        details = {
            "server": self.config.server,
            "database": self.config.database,
            "driver": self.config.driver,
            "authentication": EntraAuthMethod.ACCESS_TOKEN.value,
            "token_audience": self.token_scope,
        }

        try:
            token = self.credential.token(SQL_SCOPE)
        except AzureDependencyNotAvailableError as exc:
            return failed(
                SourceType.SQL, str(exc), ErrorCategory.DEPENDENCY, **details
            )
        except AzureAuthenticationError as exc:
            return failed(
                SourceType.SQL, str(exc), ErrorCategory.AUTHENTICATION, **details
            )
        details["token_expires_on"] = str(token.expires_on)

        try:
            connection = self.connect()
        except SqlDriverNotAvailableError as exc:
            return failed(
                SourceType.SQL, str(exc), ErrorCategory.DEPENDENCY, **details
            )
        except SqlAuthenticationError as exc:
            return failed(
                SourceType.SQL, str(exc), ErrorCategory.AUTHENTICATION, **details
            )
        except SqlConnectionError as exc:
            return failed(
                SourceType.SQL, str(exc), self._category_for(exc), **details
            )

        try:
            cursor = connection.cursor()
            try:
                cursor.execute(VALIDATION_STATEMENT)
                row = cursor.fetchone()
            finally:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001 - never mask the real outcome
                    pass
        except Exception as exc:  # noqa: BLE001 - the driver's error type is not ours
            return failed(
                SourceType.SQL,
                f"connected to {self.config.safe_description()} but the "
                f"session check failed: {type(exc).__name__}: {exc}",
                ErrorCategory.UNKNOWN,
                **details,
            )
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - a failed close is not the result
                pass

        if not row:
            return failed(
                SourceType.SQL,
                f"connected to {self.config.safe_description()} but the "
                f"server returned no session information",
                ErrorCategory.UNKNOWN,
                **details,
            )

        connected_database = str(row[0]) if row[0] is not None else "unknown"
        principal = str(row[1]) if len(row) > 1 and row[1] is not None else "unknown"
        details["connected_database"] = connected_database
        details["principal"] = principal

        if connected_database.lower() != self.config.database.lower():
            # The endpoint accepted the login but put the session somewhere
            # else. Reporting success would attribute everything discovered
            # afterwards to a database it did not come from.
            return failed(
                SourceType.SQL,
                f"connected to {self.config.server} but the session is in "
                f"database {connected_database!r}, not "
                f"{self.config.database!r}",
                ErrorCategory.CONFIGURATION,
                **details,
            )

        return ok(
            SourceType.SQL,
            f"connected to {self.config.server}/{connected_database} as "
            f"{principal} using an Entra access token",
            **details,
        )

    @staticmethod
    def _category_for(exc: SqlConnectionError) -> ErrorCategory:
        """Sort a driver failure into something an operator can act on.

        Message matching, because a TDS login failure arrives as prose inside
        an ODBC diagnostic and there is no structured field to read instead.
        Anything unrecognised stays UNKNOWN rather than being guessed into a
        category that would send someone to the wrong fix.
        """
        text = str(exc).lower()
        if "login failed" in text or "requested by the login" in text:
            return ErrorCategory.AUTHORIZATION
        if "token" in text and ("expired" in text or "invalid" in text):
            return ErrorCategory.AUTHENTICATION
        if any(
            marker in text
            for marker in ("timeout", "timed out", "unable to connect", "network")
        ):
            return ErrorCategory.NETWORK
        if "data source name not found" in text or "im002" in text:
            # The ODBC manager's way of saying the driver is not installed.
            # Matched on these two phrases rather than on the word "driver",
            # which appears in plenty of messages that mean something else.
            return ErrorCategory.DEPENDENCY
        return ErrorCategory.UNKNOWN
