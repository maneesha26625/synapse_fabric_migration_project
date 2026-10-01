"""Opening a session against the Synapse Artifacts data plane.

The data-plane sibling of ``sql.connection``, and shaped the same way:

* a ``Connector`` becomes an ``ArtifactsClient`` -- a seam with one method, so
  every caller above it is testable with no network and no credential;
* ``PyodbcConnector`` becomes ``HttpArtifactsClient`` -- the real one, which
  imports ``requests`` lazily because a repository-only run must keep working
  on a machine that has neither driver nor SDK installed;
* ``AccessTokenAuthentication`` holds a *callable* rather than a token, and so
  does this. Nothing here stores a credential between requests: the token is
  requested at the call, written into one header, and dropped. A long
  discovery run therefore survives an expiry without special handling.

This module acquires no credential of its own. The callable comes from
``discovery_agent.connections``, which is the only place in the application
that holds an Azure identity.

Only GET exists. Not "we only call GET" -- there is no other method on the
client, so a future caller cannot write one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Mapping, Optional

from discovery_agent.errors import (
    AzureDependencyNotAvailableError,
    SynapseConnectionError,
)

#: Reads can be slow on a large workspace, and a listing that times out costs
#: the whole artifact type. Generous on purpose.
DEFAULT_HTTP_TIMEOUT_SECONDS = 60

#: A zero-argument callable returning a raw bearer token. Never a token.
TokenProvider = Callable[[], str]


class ArtifactsResponse:
    """One data-plane reply, reduced to what this package acts on.

    ``__slots__`` and a written-out ``__repr__`` for the same reason the
    token class has them: the default repr of an object holding a response is
    how a header ends up in a log file. Nothing here holds a header.
    """

    __slots__ = ("status_code", "payload", "error")

    def __init__(
        self,
        status_code: int,
        payload: Optional[Mapping[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self.payload = dict(payload or {})
        self.error = error

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def message(self) -> str:
        """The service's own explanation, or a plain description of the status.

        Synapse answers with a CloudError, which nests the useful sentence
        under ``error.message``. Without this an operator gets "403" and no
        indication of which role is missing.
        """
        if self.error:
            return self.error
        error = self.payload.get("error")
        if isinstance(error, Mapping) and error.get("message"):
            return str(error["message"])
        return f"HTTP {self.status_code}"

    def __repr__(self) -> str:
        return f"ArtifactsResponse(status_code={self.status_code})"


class ArtifactsClient(ABC):
    """Performs one authenticated GET against a workspace development endpoint.

    The seam that keeps every data-plane read testable without a network, a
    subscription or a sign-in -- exactly what ``Connector`` does for SQL and
    ``ArmTransport`` does for the management plane.
    """

    #: The workspace development endpoint, e.g.
    #: ``https://myworkspace.dev.azuresynapse.net``. Never a credential.
    endpoint: str

    @abstractmethod
    def get(self, url: str) -> ArtifactsResponse:
        """GET one absolute URL on this workspace, authenticated."""

    def describe(self) -> str:
        """A safe one-line description. Contains no credential."""
        return f"synapse artifacts at {self.endpoint}"


class HttpArtifactsClient(ArtifactsClient):
    """The real client, over ``requests``.

    Holds a token *provider*, never a token. The distinction is the whole
    security argument for this class: there is no attribute a credential
    persists in, so no repr, no traceback and no pickle of one can carry a
    bearer token.
    """

    def __init__(
        self,
        endpoint: str,
        token_provider: TokenProvider,
        timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        if not endpoint:
            raise SynapseConnectionError(
                "a synapse artifacts client needs a workspace development endpoint"
            )
        if not callable(token_provider):
            raise SynapseConnectionError(
                "the artifacts client takes a token provider, not a token; "
                "nothing in this application accepts a pasted credential"
            )
        self.endpoint = endpoint.rstrip("/")
        self._token_provider = token_provider
        self.timeout_seconds = timeout_seconds

    def get(self, url: str) -> ArtifactsResponse:
        requests = self._http_module()
        try:
            response = requests.get(
                url,
                headers={
                    # Built at the call site from the provider's answer and
                    # never stored on this object.
                    "Authorization": f"Bearer {self._token_provider()}",
                    "Accept": "application/json",
                },
                timeout=self.timeout_seconds,
            )
        except Exception as exc:  # requests.RequestException and anything under it
            raise SynapseConnectionError(
                f"could not reach the synapse artifacts endpoint "
                f"{self.endpoint}: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return ArtifactsResponse(status_code=response.status_code, payload=payload)

    @staticmethod
    def _http_module() -> Any:
        try:
            import requests  # noqa: PLC0415 - optional dependency, imported on use
        except ImportError as exc:
            raise AzureDependencyNotAvailableError(
                "requests is required to read the Synapse Artifacts API and "
                "is not installed; install the optional extra with "
                "`pip install -e .[azure]`"
            ) from exc
        return requests

    def __repr__(self) -> str:
        """Names the endpoint, never the identity behind it."""
        return f"HttpArtifactsClient(endpoint={self.endpoint!r})"
