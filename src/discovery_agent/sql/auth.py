"""How discovery proves who it is to a dedicated SQL pool.

Entra ID only. There is no SQL-login implementation here and no place to put
one: no class in this module has a password or token field, so the
"never retrieve a credential" rule is enforced by the shape of the types
rather than by a redaction step someone could forget.

Four mechanisms are anticipated and exactly **one** is implemented:

* ``access_token`` -- a token is acquired out of band and handed to the driver
  through its token attribute, never through the connection string. This
  module does not acquire it and cannot: it holds a *callable* supplied by
  ``discovery_agent.connections``, which owns every credential the
  application has. The direction matters -- connections depends on sql, and
  sql must never depend on connections.
* ``interactive``, ``managed_identity`` and ``service_principal`` -- declared
  so configuration can already name them, and refused at construction.

``interactive`` was implemented once and has been **deliberately removed**.
It worked, and that was the problem: the ODBC driver ran its own browser
sign-in, so a run that touched both ARM and SQL authenticated twice and could
end up holding two different identities without saying so. One application
run means one identity, and the only way to guarantee that is for this module
to have no way of obtaining a credential at all. Should a browser flow be
wanted again, it belongs behind ``AzureCredentialProvider`` as an
``InteractiveBrowserCredential`` and arrives here as an ``access_token`` like
everything else.

``describe()`` is what goes in a log line. It names a mechanism and never a
credential.
"""

from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional

from discovery_agent.errors import SqlAuthenticationError

#: A function that returns a freshly-valid Entra access token for the SQL
#: audience. The seam exists so this module never learns how to get one.
TokenProvider = Callable[[], str]


class EntraAuthMethod(str, Enum):
    """The Entra ID mechanisms discovery can be configured to use."""

    INTERACTIVE = "interactive"
    ACCESS_TOKEN = "access_token"
    MANAGED_IDENTITY = "managed_identity"
    SERVICE_PRINCIPAL = "service_principal"


@dataclass(frozen=True)
class EntraAuthentication(ABC):
    """One way of authenticating to the pool as an Entra principal.

    Two channels exist and they are not interchangeable. ``odbc_keywords``
    goes into the connection string, so only non-secret keywords may appear
    there. ``access_token`` is handed to the driver out of band, which is
    where anything sensitive belongs.
    """

    @property
    @abstractmethod
    def method(self) -> EntraAuthMethod:
        """Which mechanism this is."""

    @abstractmethod
    def odbc_keywords(self) -> Mapping[str, str]:
        """Connection-string keywords. Never a secret -- this string is logged."""

    def access_token(self) -> Optional[bytes]:
        """A token for the driver's out-of-band attribute, if this method uses one.

        None for interactive, where the driver acquires and holds the token
        itself and this process never sees it.
        """
        return None

    def describe(self) -> str:
        """A safe one-line description for a log or a summary."""
        return f"Entra ID ({self.method.value})"


def encode_odbc_access_token(token: str) -> bytes:
    """Pack a token the way the SQL Server ODBC driver requires it.

    A little-endian 32-bit byte count followed by the token in UTF-16-LE. The
    driver rejects anything else, and getting this wrong produces an opaque
    login failure rather than a decoding error -- which is why it lives in one
    named, tested function instead of inline at the call site.
    """
    if not token:
        raise SqlAuthenticationError(
            "the credential returned an empty access token for the SQL audience"
        )
    encoded = token.encode("utf-16-le")
    return struct.pack("<i", len(encoded)) + encoded


@dataclass(frozen=True)
class AccessTokenAuthentication(EntraAuthentication):
    """Entra sign-in with a token this process acquired elsewhere.

    The field is a *provider*, not a token: there is no attribute on this
    object a credential could rest in between calls, so nothing can print,
    pickle or log one. ``repr`` is defined anyway, because the default would
    render the closure's module and qualified name and that is noise in a
    traceback rather than information.

    The token is fetched on each connect. The provider is expected to cache
    and refresh; re-using a stale token would fail a login that a fresh call
    would have completed.
    """

    token_provider: TokenProvider = field(repr=False)

    @property
    def method(self) -> EntraAuthMethod:
        return EntraAuthMethod.ACCESS_TOKEN

    def odbc_keywords(self) -> Mapping[str, str]:
        """None at all.

        The token travels in the driver attribute, so the connection string
        needs no ``Authentication`` keyword -- and must not carry ``UID`` or
        ``PWD``, which is why nothing is returned here rather than something
        being carefully omitted.
        """
        return {}

    def access_token(self) -> Optional[bytes]:
        try:
            token = self.token_provider()
        except SqlAuthenticationError:
            raise
        except Exception as exc:  # whatever the credential library raises
            raise SqlAuthenticationError(
                f"could not acquire an Entra access token for the SQL "
                f"endpoint: {type(exc).__name__}: {exc}"
            ) from exc
        return encode_odbc_access_token(token)

    def __repr__(self) -> str:  # pragma: no cover - trivial, asserted in tests
        return "AccessTokenAuthentication(token_provider=<callable>)"


def authentication_for(
    method: EntraAuthMethod, token_provider: Optional[TokenProvider] = None
) -> EntraAuthentication:
    """The authentication implementation for a configured method.

    Unimplemented methods fail loudly at configuration time rather than
    falling back to something weaker. A silent downgrade to a different
    identity would be a security problem, not a convenience.
    """
    if method is EntraAuthMethod.ACCESS_TOKEN:
        if token_provider is None:
            raise SqlAuthenticationError(
                "access_token authentication needs a token provider; this "
                "package never acquires a credential itself. Build the "
                "connection through discovery_agent.connections, which owns "
                "the Azure credential."
            )
        return AccessTokenAuthentication(token_provider)
    raise SqlAuthenticationError(
        f"Entra authentication method {method.value!r} is not available; this "
        f"build authenticates to SQL only with "
        f"{EntraAuthMethod.ACCESS_TOKEN.value!r}, using the central Azure "
        f"credential in discovery_agent.connections. A second sign-in "
        f"mechanism here would mean a second identity per run."
    )
