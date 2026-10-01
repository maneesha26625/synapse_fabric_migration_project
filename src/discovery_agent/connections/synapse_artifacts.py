"""The Synapse Artifacts data plane, connected with the run's Entra identity.

A *composition*, not a reimplementation -- the same shape as
``connections.sql``. Everything it needs already exists and stays where it is:

* ``SynapseConnectionConfig`` -- which workspace, and its development endpoint
* ``AzureCredentialProvider`` -- the run's one identity, shared with ARM and SQL
* ``HttpArtifactsClient`` -- the GET-only transport, in ``synapse.client``
* ``SynapseArtifactSource`` -- the ``ArtifactSource`` discovery takes

What this module adds is the audience. The data plane does not accept an ARM
token: it wants ``https://dev.azuresynapse.net/.default``. That scope is
requested from the same credential every other connection uses, so a run
still signs in once.

Three properties tests assert:

* the client holds a token *provider*, never a token;
* no credential is constructed here -- one is passed in;
* no artifact route, model or parsing rule is defined here. This module
  connects and hands back a source. What to ask the data plane is
  ``synapse.api``'s business.
"""

from __future__ import annotations

from typing import Optional

from discovery_agent.connections.azure import SYNAPSE_SCOPE, AzureCredentialProvider
from discovery_agent.connections.models import SynapseConnectionConfig
from discovery_agent.connections.validation import (
    ConnectionValidation,
    ErrorCategory,
    category_for_status,
    failed,
    ok,
)
from discovery_agent.errors import (
    AzureAuthenticationError,
    AzureDependencyNotAvailableError,
    SynapseConnectionError,
)
from discovery_agent.extractors.models import SourceType
from discovery_agent.synapse.api import MAX_PAGES, triggers_url, workspace_url
from discovery_agent.synapse.client import ArtifactsClient, HttpArtifactsClient
from discovery_agent.synapse.models import SynapseWorkspaceDiscovery
from discovery_agent.synapse.source import SynapseArtifactSource, unavailable_discovery


class SynapseArtifactsConnection:
    """Reads the live workspace, on the run's Azure identity.

    Scoped to a single workspace, because the data-plane endpoint is: two
    workspaces means two of these, which is the same rule
    ``SynapseArtifactSource`` already follows.

    The client is injectable so unit tests need no network and no sign-in,
    exactly as ``SqlConnection`` allows a connector to be.
    """

    def __init__(
        self,
        config: SynapseConnectionConfig,
        credential: AzureCredentialProvider,
        client: Optional[ArtifactsClient] = None,
    ) -> None:
        config.validate()
        self.config = config
        #: The run's credential provider, not a token. The client is given a
        #: callable and re-acquires per request, so a long discovery survives
        #: an expiry and nothing here holds a credential between calls.
        self.credential = credential
        self._client = client
        self._source: Optional[SynapseArtifactSource] = None

    # -- identity ----------------------------------------------------------

    @property
    def endpoint(self) -> str:
        """The workspace development endpoint. Derived, never configured."""
        return self.config.development_endpoint

    @property
    def workspace(self) -> str:
        return self.config.workspace_name

    def authentication(self) -> str:
        """How this connection authenticates. A mechanism, never a credential."""
        return f"entra access token for {SYNAPSE_SCOPE} via {self.credential.describe()}"

    def describe(self) -> str:
        """A safe one-line description. Contains no credential."""
        return f"synapse artifacts for {self.workspace} at {self.endpoint}"

    # -- the connection ----------------------------------------------------

    def client(self) -> ArtifactsClient:
        """The data-plane client, built once and reused."""
        if self._client is None:
            self._client = HttpArtifactsClient(
                endpoint=self.endpoint,
                token_provider=self.credential.token_provider_for(SYNAPSE_SCOPE),
            )
        return self._client

    def source(self) -> SynapseArtifactSource:
        """The unchanged ``SynapseArtifactSource``, ready to list.

        Built once so a run that lists and then reads an artifact reuses the
        listing rather than requesting it twice.
        """
        if self._source is None:
            self._source = SynapseArtifactSource(
                client=self.client(), workspace=self.workspace
            )
        return self._source

    def discover(self) -> SynapseWorkspaceDiscovery:
        """Every registered artifact type in this workspace.

        Degrades rather than raises. A data plane that cannot be reached at
        all produces a result whose endpoints are each marked unreachable,
        because an empty artifact list would read as "this workspace is
        empty" -- which is precisely the claim that cannot be made.
        """
        try:
            return self.source().discover()
        except (SynapseConnectionError, AzureDependencyNotAvailableError) as exc:
            return unavailable_discovery(self.workspace, self.endpoint, str(exc))
        except AzureAuthenticationError as exc:
            return unavailable_discovery(
                self.workspace,
                self.endpoint,
                f"no Azure identity for the Synapse data plane: {exc}",
            )

    def list_triggers(self) -> list:
        """The workspace's triggers, as the service returns them.

        One fixed GET route, paged by following ``nextLink`` -- and only a
        ``nextLink`` on this workspace's own endpoint, so the bearer token is
        never sent anywhere else. Raises ``SynapseConnectionError`` when the
        listing cannot be read, so a caller reports a gap rather than "none".
        """
        url: Optional[str] = triggers_url(self.endpoint)
        items: list = []
        for _ in range(MAX_PAGES):
            if not url:
                return items
            if not url.startswith(self.endpoint.rstrip("/") + "/"):
                raise SynapseConnectionError("the service returned a paging link outside this workspace")
            response = self.client().get(url)
            if not response.ok:
                raise SynapseConnectionError(
                    f"triggers could not be listed: {response.message()} (HTTP {response.status_code})"
                )
            items.extend(response.payload.get("value") or [])
            url = response.payload.get("nextLink")
        raise SynapseConnectionError("the trigger listing did not end")

    # -- validation --------------------------------------------------------

    def validate(self) -> ConnectionValidation:
        """Prove the data plane answers and that this identity may read it.

        Reads the workspace's own data-plane resource rather than listing
        pipelines: it is the cheapest call that proves the same two things,
        and it does not make the validation cost grow with the size of the
        workspace.

        Data-plane authorization is separate from management-plane
        authorization, and that is why this check exists at all: Reader on
        the resource group lets an identity see the workspace in ARM and does
        not let it read a single pipeline. An operator who saw Synapse
        validate green and then discovered nothing would have no way to tell
        which of the two was missing.
        """
        details = {
            "workspace_name": self.workspace,
            "endpoint": self.endpoint,
            "authentication": self.authentication(),
        }

        try:
            client = self.client()
        except SynapseConnectionError as exc:
            return failed(
                SourceType.SYNAPSE, str(exc), ErrorCategory.CONFIGURATION, **details
            )

        try:
            response = client.get(workspace_url(self.endpoint))
        except AzureDependencyNotAvailableError as exc:
            return failed(
                SourceType.SYNAPSE, str(exc), ErrorCategory.DEPENDENCY, **details
            )
        except AzureAuthenticationError as exc:
            return failed(
                SourceType.SYNAPSE,
                f"no Azure identity for the Synapse data plane: {exc}",
                ErrorCategory.AUTHENTICATION,
                **details,
            )
        except SynapseConnectionError as exc:
            return failed(
                SourceType.SYNAPSE, str(exc), ErrorCategory.NETWORK, **details
            )

        if not response.ok:
            return failed(
                SourceType.SYNAPSE,
                f"the synapse artifacts endpoint for {self.workspace!r} "
                f"refused the request: {response.message()}. Reading "
                f"artifacts needs a data-plane role such as Synapse Artifact "
                f"User; Reader on the resource group is not enough",
                category_for_status(response.status_code),
                status_code=str(response.status_code),
                **details,
            )

        name = str(response.payload.get("name", "")) or self.workspace
        details["reported_name"] = name
        return ok(
            SourceType.SYNAPSE,
            f"the synapse artifacts endpoint for {name!r} is readable",
            **details,
        )
