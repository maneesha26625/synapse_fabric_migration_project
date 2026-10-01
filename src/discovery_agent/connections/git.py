"""The git repository, as a configured connection.

Thin on purpose. Repository acquisition already works, already parses URLs,
already refuses a URL with credentials in it, and already delegates
authentication to the user's own git. None of that is redesigned here. What
this module adds is a *connection object*: something a UI can hold alongside
the Azure and SQL connections, ask to validate itself, and hand to acquisition
without the caller assembling arguments.

**Git authentication is entirely independent of Azure authentication.** No
Azure credential, token or identity is consulted to reach a repository, and
nothing in this module can reach one -- it has no credential provider and no
way to obtain one. The two connections share a manager and nothing else.

How a private repository is reached:

* **HTTPS** -- git's configured ``credential.helper``, which on Windows is
  normally Git Credential Manager. The user has already signed in to it, or
  will be asked by GCM itself, outside this process.
* **SSH** -- the user's key, ssh-agent and ``~/.ssh/config``.

This application stores no PAT, no password, no token and no provider OAuth
client. It reports *which* mechanism applies and lets git do the rest. There
is deliberately no provider-specific authentication anywhere in this module:
GitHub, Azure DevOps, GitLab, Bitbucket and a self-hosted host are all reached
by the same git binary through the same two mechanisms, and the only thing
that differs between them is the name in the report.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

from discovery_agent.acquisition.acquire import (
    DEFAULT_INPUT_ROOT,
    GitRunner,
    acquire_repository,
)
from discovery_agent.acquisition.git import run_git
from discovery_agent.acquisition.models import RepositorySource
from discovery_agent.acquisition.providers import (
    DEFAULT_PROVIDERS,
    GitAuthMechanism,
    GitProvider,
    GitTransport,
    RepositoryProvider,
)
from discovery_agent.connections.models import GitRepositoryConfig
from discovery_agent.connections.validation import (
    ConnectionValidation,
    ErrorCategory,
    failed,
    ok,
    redact,
)
from discovery_agent.errors import (
    AcquisitionError,
    ConfigError,
    GitCommandError,
    UnsupportedProviderError,
)
from discovery_agent.extractors.models import SourceType

#: A probe must fail rather than hang, and the timeout -- not a credential
#: flag -- is what guarantees that. Generous because it has to be: Git
#: Credential Manager took 28-34 seconds to serve a cached GitHub token on the
#: machine this was measured on, while the network round trip itself was two.
#: A probe that timed out on a working credential would be worse than a slow
#: one.
PROBE_TIMEOUT_SECONDS = 120

#: The only git config a probe overrides: reset the credential helper list, so
#: the request asks "can this be read with no credential at all?" -- which is
#: how repository visibility is determined. An empty value resets the list
#: rather than adding to it, and with no helper there is nothing that could
#: prompt.
#:
#: Note what is deliberately *not* here. ``credential.interactive=never`` was,
#: and it broke private repositories: in Git Credential Manager it is a kill
#: switch evaluated before the credential store is consulted, so GCM aborts
#: rather than returning the token it already holds. It failed in one second
#: against a repository the same git could read -- the speed was the tell.
#: Non-interactivity comes from ``GIT_TERMINAL_PROMPT=0``, which
#: ``acquisition.git`` already sets on every invocation and which suppresses
#: the prompt without suppressing the helper.
_ANONYMOUS = ("-c", "credential.helper=")


class RepositoryVisibility(str):
    """Whether the remote can be read without a credential.

    A plain string subclass rather than an enum because ``UNKNOWN`` is the
    common answer and the value is only ever reported, never branched on.
    """

    PUBLIC = "public"
    PRIVATE = "private"
    UNKNOWN = "unknown"


class GitConnection:
    """One source repository, and whether it can be reached.

    ``SourceType.REPOSITORY`` is the connection kind rather than a new "git"
    enum member: the repository *is* the fourth source in the existing
    ``SourceType``, and adding a parallel enum would leave the codebase with
    two names for the same thing.
    """

    def __init__(
        self,
        config: GitRepositoryConfig,
        providers: Sequence[RepositoryProvider] = DEFAULT_PROVIDERS,
        git: GitRunner = run_git,
    ) -> None:
        config.validate()
        self.config = config
        self.providers = providers
        #: Injectable for the same reason acquisition makes it injectable:
        #: unit tests must not need a network or a real repository. It is the
        #: only way this class reaches git, and git is the only way it reaches
        #: a network -- there is no second execution path.
        self.git = git

    # -- identity ----------------------------------------------------------

    @property
    def repository_url(self) -> str:
        return self.config.repository_url

    @property
    def ref(self) -> Optional[str]:
        return self.config.ref

    @property
    def provider(self) -> str:
        """The provider handling this URL, as the acquisition registry says."""
        return self.config.detected_provider(self.providers)

    @property
    def provider_kind(self) -> GitProvider:
        return self.config.provider_kind(self.providers)

    @property
    def transport(self) -> GitTransport:
        """HTTPS or SSH, read off the URL."""
        return self.config.transport

    @property
    def authentication(self) -> GitAuthMechanism:
        """Which credential system git will use. Never a credential."""
        return self.config.authentication_mechanism

    def describe(self) -> str:
        """A safe one-line description. Contains no credential."""
        return (
            f"{self.config.safe_description()} via {self.provider} "
            f"over {self.transport.value}"
        )

    # -- acquisition -------------------------------------------------------

    def acquire(self, input_root: Path = DEFAULT_INPUT_ROOT) -> RepositorySource:
        """Clone the repository, or reuse the clone already on disk.

        A pass-through to the existing ``acquire_repository`` with the values
        this connection holds. The acquisition logic -- the reuse policy, the
        origin check, the ref verification, the cleanup on failure -- is
        untouched and is still the only implementation of any of it.
        """
        return acquire_repository(
            repository_url=self.config.repository_url,
            ref=self.config.ref,
            input_root=input_root,
            providers=self.providers,
            git=self.git,
        )

    # -- validation --------------------------------------------------------

    def validate(self, probe_remote: bool = True) -> ConnectionValidation:
        """Check the URL, the provider, the git client and -- optionally -- the remote.

        ``probe_remote`` performs ``git ls-remote``, which is the only step
        that proves what a connection check is supposed to prove: that the
        user's own git credentials actually authenticate to this repository.
        It is the default for that reason, and it is a parameter because the
        unit suite must stay offline and a caller validating a configuration
        form should not be made to wait on a network round trip.

        Nothing is cloned. Validation must not leave a snapshot behind that
        the reuse path would later find and adopt.
        """
        details = {
            "repository_url": self.config.repository_url,
            "ref": self.config.ref or "default branch",
        }

        try:
            details["provider"] = self.provider
            details["transport"] = self.transport.value
            details["authentication"] = self.authentication.value
        except UnsupportedProviderError as exc:
            return failed(
                SourceType.REPOSITORY,
                str(exc),
                ErrorCategory.CONFIGURATION,
                **details,
            )
        except AcquisitionError as exc:
            return failed(
                SourceType.REPOSITORY, str(exc), ErrorCategory.CONFIGURATION, **details
            )

        try:
            version = self._run(["--version"])
        except GitCommandError as exc:
            return failed(
                SourceType.REPOSITORY,
                f"the git client could not be run: {self._safe(exc)}",
                ErrorCategory.DEPENDENCY,
                **details,
            )
        details["git_client"] = version.strip()

        if not probe_remote:
            details["reachable"] = "not checked"
            return ok(
                SourceType.REPOSITORY,
                f"repository {self.config.repository_url} is well-formed and "
                f"handled by the {details['provider']} provider over "
                f"{details['transport']}; the remote was not contacted",
                **details,
            )

        try:
            listing = self._run(self._ls_remote_arguments())
        except GitCommandError as exc:
            details["reachable"] = "false"
            return failed(
                SourceType.REPOSITORY,
                f"could not reach {self.config.repository_url} using the "
                f"{self.authentication.value} configured on this machine: "
                f"{self._safe(exc)}{self._hint(exc)}",
                self._category_for(exc),
                **details,
            )

        details["reachable"] = "true"
        matched = [line for line in listing.splitlines() if line.strip()]
        details["refs_visible"] = str(len(matched))

        if self.config.ref and not matched:
            # An empty listing for a named ref is not a reachability failure;
            # the remote answered. It means the ref is not there, which is a
            # configuration problem and would fail the clone later anyway.
            return failed(
                SourceType.REPOSITORY,
                f"{self.config.repository_url} is reachable, but it has no "
                f"ref named {self.config.ref!r}",
                ErrorCategory.NOT_FOUND,
                **details,
            )

        details["repository"] = self.visibility()

        return ok(
            SourceType.REPOSITORY,
            f"{self.config.repository_url} ({details['repository']}) is "
            f"reachable over {self.transport.value} using the "
            f"{self.authentication.value} configured on this machine",
            **details,
        )

    def visibility(self) -> str:
        """Whether the remote can be read with no credential at all.

        Answerable only over HTTPS, and only by asking: a second ``ls-remote``
        with every credential helper switched off. If that succeeds the
        repository is public; if it is refused while the authenticated probe
        succeeded, it is private.

        Over SSH the question has no equivalent -- there is no "without a key"
        request to make, since the key is the transport -- so the answer is
        ``unknown`` rather than a guess. Reporting "public" for an SSH remote
        because we could not tell would be worse than reporting nothing.
        """
        if self.transport is not GitTransport.HTTPS:
            return RepositoryVisibility.UNKNOWN
        try:
            self._run(list(_ANONYMOUS) + self._ls_remote_arguments())
        except GitCommandError:
            return RepositoryVisibility.PRIVATE
        return RepositoryVisibility.PUBLIC

    # -- the single git boundary -------------------------------------------

    def _ls_remote_arguments(self) -> List[str]:
        """The read-only listing this class probes with. Never a clone."""
        arguments = ["ls-remote", "--heads", "--tags", self.config.repository_url]
        if self.config.ref:
            arguments.append(self.config.ref)
        return arguments

    def _run(self, arguments: Sequence[str]) -> str:
        """Every git invocation this class makes goes through here.

        One place, so the probe timeout is decided once rather than per call
        site. It still calls the injected runner, which is still
        ``acquisition.git.run_git`` -- this adds no second execution path and
        no second ``subprocess`` call site.

        The arguments are passed through unchanged. A probe that needs the
        credential helper switched off says so itself, with ``_ANONYMOUS``.
        """
        return self.git(list(arguments), timeout=PROBE_TIMEOUT_SECONDS)

    # -- failure reporting -------------------------------------------------

    def _hint(self, exc: GitCommandError) -> str:
        """What to actually do about it, when the cause is unambiguous.

        Git's own text is about the prompt it could not show, which describes
        our probe rather than the user's problem. The problem is that the
        machine has no usable credential for this host, and the fix is to sign
        in to the credential system the report already named.
        """
        text = (exc.stderr or "").lower()
        if any(
            marker in text
            for marker in (
                # What git says when the helper returned nothing and
                # GIT_TERMINAL_PROMPT=0 stopped it asking.
                "terminal prompts disabled",
                "could not read username",
                "could not read password",
                # Kept for a helper configured to refuse interaction itself.
                "cannot prompt",
                "user interactivity has been disabled",
            )
        ):
            return (
                f". No credential for this host is cached: sign in to your "
                f"git credential helper once (for example by running "
                f"`git ls-remote {self.config.repository_url}` yourself), "
                f"then retry. This tool never accepts a token directly."
            )
        if "permission denied (publickey" in text:
            return (
                ". No SSH key on this machine is accepted by the host; add "
                "your key to the agent and to your account, then retry."
            )
        if "host key verification failed" in text:
            return (
                ". The host is not in known_hosts; connect to it once with "
                "ssh to accept its key, then retry."
            )
        return ""

    @staticmethod
    def _safe(exc: GitCommandError) -> str:
        """A git failure as text, with anything credential-shaped removed.

        ``GitCommandError`` is built from the argument vector and git's
        stderr. Neither should ever hold a secret -- a credential-bearing URL
        is refused at configuration time, and the environment is never echoed
        -- but git's stderr is not ours to vouch for, and a misconfigured
        credential helper can print its protocol output on failure.
        ``ConnectionValidation`` redacts again on construction; this is the
        first of the two passes, applied where the text is produced.
        """
        return redact(str(exc))

    @staticmethod
    def _category_for(exc: GitCommandError) -> ErrorCategory:
        """Sort a git failure into something an operator can act on.

        The wordings differ per transport and per host, so the markers cover
        both HTTPS (credential helper, HTTP status text) and SSH (the ssh
        client's own refusals).
        """
        text = (exc.stderr or "").lower()
        if any(
            marker in text
            for marker in (
                "authentication failed",
                "could not read username",
                "could not read password",
                "no supported authentication methods",
                "permission denied (publickey",
                "host key verification failed",
                "terminal prompts disabled",
                # The probe suppresses interactive prompts, so a helper with
                # nothing cached says so rather than opening a window. That is
                # an authentication problem: sign in to the helper.
                "cannot prompt",
                "user interactivity has been disabled",
                "unable to get password",
                "unable to get username",
            )
        ):
            return ErrorCategory.AUTHENTICATION
        if any(
            marker in text
            for marker in ("permission denied", "access denied", "403", "forbidden")
        ):
            return ErrorCategory.AUTHORIZATION
        if any(
            marker in text
            for marker in (
                "not found",
                "repository does not exist",
                "does not appear to be a git repository",
            )
        ):
            return ErrorCategory.NOT_FOUND
        if any(
            marker in text
            for marker in (
                "could not resolve host",
                "timed out",
                "connection refused",
                "network is unreachable",
                "connection reset",
            )
        ):
            return ErrorCategory.NETWORK
        return ErrorCategory.UNKNOWN


def connection_from_url(
    repository_url: str, ref: Optional[str] = None, git: GitRunner = run_git
) -> GitConnection:
    """A git connection from a bare URL, for a caller that has nothing else.

    Raises ConfigError, not AcquisitionError, when the URL is unusable: at
    this point it is configuration that is wrong, and nothing has been
    acquired or attempted.
    """
    try:
        config = GitRepositoryConfig(repository_url=repository_url, ref=ref)
    except AcquisitionError as exc:  # pragma: no cover - parse errors are ConfigError
        raise ConfigError(str(exc)) from exc
    return GitConnection(config, git=git)
