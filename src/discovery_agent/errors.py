"""Exception hierarchy for the Discovery Agent.

Every error the agent raises on purpose derives from DiscoveryError, so the
CLI can distinguish an expected failure (bad config, unparseable asset) from
an unexpected crash.
"""


class DiscoveryError(Exception):
    """Base class for all expected Discovery Agent failures."""


class ConfigError(DiscoveryError):
    """The run configuration is invalid or points at something unusable."""


class ParseError(DiscoveryError):
    """A source file could not be parsed into a canonical asset."""

    def __init__(self, source_file: str, reason: str) -> None:
        super().__init__(f"{source_file}: {reason}")
        self.source_file = source_file
        self.reason = reason


class AcquisitionError(DiscoveryError):
    """The source repository could not be acquired."""


class UnsupportedProviderError(AcquisitionError):
    """No repository provider handles the given repository URL."""


class RepositoryReuseError(AcquisitionError):
    """An existing local clone cannot be reused as this run's snapshot."""


class ExtractionError(DiscoveryError):
    """An artifact could not be extracted."""


class ExtractionContractError(ExtractionError):
    """An extractor returned a result that breaks the framework's contract.

    A programming error in an extractor, not a problem with the source data —
    for example a FAILED result that still carries content.
    """


class NoExtractorError(ExtractionError):
    """No registered extractor handles this artifact type and source."""


class UnsupportedSourceError(ExtractionError):
    """An extractor exists for this artifact type, but not for this source."""


class DuplicateExtractorError(ExtractionError):
    """Two extractors claim the same artifact type and source type."""


class MalformedArtifactError(ExtractionError):
    """An artifact's content could not be read or parsed for extraction."""

    def __init__(self, source_path: str, reason: str) -> None:
        super().__init__(f"{source_path}: {reason}")
        self.source_path = source_path
        self.reason = reason


class GitCommandError(AcquisitionError):
    """A git command failed, timed out, or could not be run."""

    def __init__(self, args, returncode, stderr: str) -> None:
        # Never include the command's environment or any credential material;
        # git handles authentication and we only ever pass a URL and a ref.
        rendered = " ".join(str(a) for a in args)
        code = "n/a" if returncode is None else str(returncode)
        super().__init__(f"git {rendered} failed (exit {code}): {stderr}")
        self.args_vector = tuple(str(a) for a in args)
        self.returncode = returncode
        self.stderr = stderr


class SqlDiscoveryError(DiscoveryError):
    """Live SQL discovery could not produce a truthful result."""


class SqlConnectionError(SqlDiscoveryError):
    """A connection to a SQL endpoint could not be opened.

    Never carries a connection string or a credential: the message names the
    server, the database and the mechanism, which is what an operator needs
    and all they should get.
    """


class SqlAuthenticationError(SqlDiscoveryError):
    """The configured authentication mechanism cannot be used."""


class SqlDriverNotAvailableError(SqlDiscoveryError):
    """The ODBC driver needed for live SQL discovery is not installed."""


class CatalogQueryError(SqlDiscoveryError):
    """A catalog query was refused, or failed against the endpoint.

    Refusal is the important half: only the fixed, registered queries run, so
    anything else is rejected before a connection is touched.
    """


class ConnectionValidationError(DiscoveryError):
    """A connection could not be validated.

    Base class for the connection layer's failures. Never carries a token, a
    password or a connection string: a message here names an endpoint, a
    resource and a mechanism, which is what an operator needs and all they
    should get.
    """


class AzureConnectionError(ConnectionValidationError):
    """Azure could not be reached, or answered that it would not serve us."""


class AzureAuthenticationError(AzureConnectionError):
    """No Azure credential could be obtained for the configured method.

    Raised when the mechanism itself is unusable -- not signed in, method not
    implemented, the azure-identity package missing. A token that is acquired
    but then refused by a resource is an authorization problem and surfaces as
    an AzureConnectionError instead.
    """


class AzureDependencyNotAvailableError(AzureConnectionError):
    """A package needed to talk to Azure is not installed."""


class SynapseConnectionError(ConnectionValidationError):
    """The Synapse workspace could not be reached or read.

    Carries the management-plane status code when there was one, so a caller
    can tell "no such workspace" from "not authorized" without parsing the
    message text.
    """

    def __init__(self, message: str, status_code=None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitConnectionError(ConnectionValidationError):
    """The configured git repository could not be reached with the user's own
    git credentials."""
