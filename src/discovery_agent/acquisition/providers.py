"""Git hosting providers, transports, and repository URL parsing.

One registry, here. ``connections.git`` reads it and does not keep a second
one -- provider identity is a property of the URL, and a URL is parsed in
exactly one place in this codebase.

Three things are derived from a URL and kept apart, because they answer
different questions:

* **Provider** -- who hosts it. Decided by *hostname*, never by the shape of
  the repository name. ``github.com`` is GitHub whatever the path looks like;
  a repository called ``gitlab`` on a corporate host is not GitLab.
* **Transport** -- how git will reach it: HTTPS or SSH.
* **Authentication mechanism** -- which of the machine's own credential
  systems the transport implies. HTTPS means the configured credential helper
  (Git Credential Manager on Windows); SSH means the user's SSH key and
  ``~/.ssh/config``. This module *names* the mechanism and never touches it:
  git performs the authentication, we only report which system it will use.

Nothing here stores, reads, or transports a credential. A URL with a username
and password embedded in it is recognised so it can be refused, which is the
only reason the parser looks at the userinfo at all.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Tuple
from urllib.parse import urlsplit

from discovery_agent.errors import AcquisitionError, UnsupportedProviderError

# scp-style remotes, e.g. git@github.com:Azure/some-repo.git
_SCP_LIKE = re.compile(r"^(?P<user>[^@/]+)@(?P<host>[^:/]+):(?P<path>.+)$")


class GitProvider(str, Enum):
    """The hosting services this build recognises.

    ``GENERIC`` is not a fallback for "we failed to parse it" -- it is a real
    answer, meaning a self-hosted or otherwise unknown git host, which is
    supported exactly as well as the named ones because every one of them is
    reached through the same git binary with the same credential mechanism.
    """

    GITHUB = "github"
    AZURE_DEVOPS = "azure_devops"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"
    GENERIC = "generic"


class GitTransport(str, Enum):
    """How git reaches the remote. Decides which credential system applies."""

    HTTPS = "https"
    SSH = "ssh"


class GitAuthMechanism(str, Enum):
    """Which of the machine's credential systems git will use.

    The *mechanism*, never a credential. This application holds no git secret
    of any kind: it names the system that holds one.
    """

    #: HTTPS: whatever ``credential.helper`` is configured to, which on
    #: Windows is normally Git Credential Manager.
    CREDENTIAL_MANAGER = "git_credential_manager"
    #: SSH: the user's key, agent and ``~/.ssh/config``.
    SSH = "ssh"


#: Transport to mechanism. A one-line table rather than a branch, so adding a
#: transport cannot silently leave the mechanism unanswered.
AUTH_MECHANISMS = {
    GitTransport.HTTPS: GitAuthMechanism.CREDENTIAL_MANAGER,
    GitTransport.SSH: GitAuthMechanism.SSH,
}


@dataclass(frozen=True)
class ParsedGitUrl:
    """The pieces of a git remote URL that provider logic cares about."""

    host: str
    path: str  # "org/repo", no leading slash, no .git suffix
    name: str  # "repo"
    has_credentials: bool
    transport: GitTransport = GitTransport.HTTPS

    @property
    def authentication(self) -> GitAuthMechanism:
        """The credential system this transport implies. Never a credential."""
        return AUTH_MECHANISMS[self.transport]


def parse_git_url(url: str) -> ParsedGitUrl:
    """Parse an https://, ssh:// or scp-style git remote URL.

    Raises AcquisitionError if the URL has no usable repository path, or names
    a transport this tool will not use.
    """
    raw = url.strip().rstrip("/")
    if not raw:
        raise AcquisitionError("repository URL is empty")

    scp = _SCP_LIKE.match(raw) if "://" not in raw else None
    if scp:
        host = scp.group("host")
        path = scp.group("path")
        transport = GitTransport.SSH
        # "git@host:org/repo" carries an SSH user, not an embedded secret.
        has_credentials = False
    else:
        split = urlsplit(raw)
        if not split.scheme or not split.netloc:
            raise AcquisitionError(f"unrecognized repository URL: {raw}")
        scheme = split.scheme.lower()
        if scheme in ("https", "http"):
            transport = GitTransport.HTTPS
        elif scheme == "ssh":
            transport = GitTransport.SSH
        else:
            # git:// is anonymous and unencrypted, and file:// is not a remote
            # at all. Refusing them is not a limitation to work around: they
            # cannot carry the authenticated access a private repository needs.
            raise AcquisitionError(
                f"unsupported git transport {scheme!r} in {raw}; use https or ssh"
            )
        netloc = split.netloc
        host = netloc.rsplit("@", 1)[-1]
        userinfo = netloc.rsplit("@", 1)[0] if "@" in netloc else ""
        # What counts as an embedded credential depends on the transport.
        # Over SSH, ``ssh://git@host/org/repo`` names the login account, just
        # as ``git@host:org/repo`` does -- refusing it would make every SSH
        # remote unusable. Over HTTPS there is no login-name concept, so any
        # userinfo at all is a token or a password.
        has_credentials = bool(userinfo) and (
            transport is GitTransport.HTTPS or ":" in userinfo
        )
        # ssh://git@host:22/org/repo -- the port is not part of the hostname.
        if ":" in host:
            host = host.rsplit(":", 1)[0]
        path = split.path

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path:
        raise AcquisitionError(f"repository URL has no repository path: {raw}")

    name = path.rsplit("/", 1)[-1]
    if not name:
        raise AcquisitionError(f"could not determine repository name from: {raw}")

    return ParsedGitUrl(
        host=host.lower(),
        path=path,
        name=name,
        has_credentials=has_credentials,
        transport=transport,
    )


def _bare_host(host: str) -> str:
    """A hostname with the ``www.`` prefix removed. Already lower-cased."""
    return host[4:] if host.startswith("www.") else host


#: Azure DevOps spells the same repository three ways: the modern HTTPS form
#: ``dev.azure.com/org/project/_git/repo``, its SSH form
#: ``ssh.dev.azure.com:v3/org/project/repo``, and the legacy
#: ``org.visualstudio.com/project/_git/repo``. Collapsing them matters because
#: the reuse check compares an existing clone's ``origin`` against the
#: configured URL, and a clone made over SSH must not look like a different
#: repository to a run configured with HTTPS.
_ADO_SSH_HOSTS = {"ssh.dev.azure.com": "dev.azure.com", "vs-ssh.visualstudio.com": None}


def normalize_for_compare(url: str) -> str:
    """Canonical form used to decide whether two remote URLs mean the same repo."""
    parsed = parse_git_url(url)
    host = _bare_host(parsed.host)
    segments = [s for s in parsed.path.lower().split("/") if s]

    if host in _ADO_SSH_HOSTS or host == "dev.azure.com" or host.endswith(".visualstudio.com"):
        mapped = _ADO_SSH_HOSTS.get(host)
        if mapped:
            host = mapped
        if segments and segments[0] == "v3":
            segments = segments[1:]  # the SSH form's version prefix
        segments = [s for s in segments if s != "_git"]

    return f"{host}/{'/'.join(segments)}"


class RepositoryProvider(ABC):
    """A git hosting service the tool knows how to acquire from."""

    #: Which service this is. ``name`` is derived from it so there is one
    #: spelling of "github" in the codebase rather than two.
    provider: GitProvider = GitProvider.GENERIC
    #: Hostnames this provider claims, lower-case. Matching is on the host and
    #: nothing else -- a repository's name never decides who hosts it.
    hosts: Tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.provider.value

    @abstractmethod
    def matches(self, url: str) -> bool:
        """Whether this provider handles the given repository URL."""

    def claims_host(self, host: str) -> bool:
        """Whether this provider owns a hostname, regardless of the path.

        Separate from ``matches`` on purpose: ``github.com/contoso`` is a
        GitHub URL that is not a repository. The host is claimed, so the
        generic provider must not pick it up and pretend it is a self-hosted
        remote -- it is a malformed GitHub URL and should be reported as one.
        """
        return _bare_host(host) in self.hosts

    def repository_name(self, url: str) -> str:
        """Directory name to clone this repository into."""
        return parse_git_url(url).name

    #: How many path segments a repository URL on this host must have.
    minimum_segments: int = 2

    def _well_formed(self, url: str) -> bool:
        try:
            parsed = parse_git_url(url)
        except AcquisitionError:
            return False
        if not self.claims_host(parsed.host):
            return False
        return len([s for s in parsed.path.split("/") if s]) >= self.minimum_segments


class GitHubProvider(RepositoryProvider):
    """github.com, over https or ssh."""

    provider = GitProvider.GITHUB
    hosts = ("github.com",)

    def matches(self, url: str) -> bool:
        # GitHub repositories are always owner/name.
        return self._well_formed(url)


class AzureDevOpsProvider(RepositoryProvider):
    """Azure DevOps, modern and legacy hosts, over https or ssh.

    ``*.visualstudio.com`` is matched by suffix because the organisation name
    is the subdomain, so the host set is not enumerable.
    """

    provider = GitProvider.AZURE_DEVOPS
    hosts = ("dev.azure.com", "ssh.dev.azure.com", "vs-ssh.visualstudio.com")
    host_suffixes = (".visualstudio.com",)

    def claims_host(self, host: str) -> bool:
        bare = _bare_host(host)
        return bare in self.hosts or bare.endswith(self.host_suffixes)

    def matches(self, url: str) -> bool:
        return self._well_formed(url)


class GitLabProvider(RepositoryProvider):
    """gitlab.com, over https or ssh.

    A self-hosted GitLab is deliberately *not* matched: nothing in its
    hostname says GitLab, and guessing from a name like ``gitlab.corp.example``
    would be exactly the repository-name inference this module avoids. It is
    reported as generic and works identically.
    """

    provider = GitProvider.GITLAB
    hosts = ("gitlab.com", "altssh.gitlab.com")

    def matches(self, url: str) -> bool:
        # Groups can nest, so two segments is the floor, not the shape.
        return self._well_formed(url)


class BitbucketProvider(RepositoryProvider):
    """bitbucket.org, over https or ssh."""

    provider = GitProvider.BITBUCKET
    hosts = ("bitbucket.org", "altssh.bitbucket.org")

    def matches(self, url: str) -> bool:
        return self._well_formed(url)


class GenericGitProvider(RepositoryProvider):
    """Any other git host: self-hosted GitLab, Gitea, Bitbucket Server, a bare
    SSH remote.

    Matches only hosts no named provider claims. That restriction is the whole
    design: without it, ``https://github.com/contoso`` -- a GitHub user page,
    not a repository -- would be silently accepted as a generic remote and
    fail much later, at clone time, with a confusing message.

    One path segment is enough. A bare ``git@host:repo.git`` is a perfectly
    ordinary self-hosted remote.
    """

    provider = GitProvider.GENERIC
    minimum_segments = 1

    def __init__(self, known: Sequence[RepositoryProvider] = ()) -> None:
        self._known = tuple(known)

    def claims_host(self, host: str) -> bool:
        return not any(provider.claims_host(host) for provider in self._known)

    def matches(self, url: str) -> bool:
        return self._well_formed(url)


_NAMED_PROVIDERS: Tuple[RepositoryProvider, ...] = (
    GitHubProvider(),
    AzureDevOpsProvider(),
    GitLabProvider(),
    BitbucketProvider(),
)

#: Order matters: the generic provider is last and claims only what no named
#: provider does, so a known host can never be reported as generic.
DEFAULT_PROVIDERS: Sequence[RepositoryProvider] = _NAMED_PROVIDERS + (
    GenericGitProvider(_NAMED_PROVIDERS),
)


def detect_provider(
    url: str, providers: Sequence[RepositoryProvider] = DEFAULT_PROVIDERS
) -> RepositoryProvider:
    """Return the provider that handles this URL.

    Raises UnsupportedProviderError when none does -- which, with the generic
    provider in the list, means the URL is malformed for the host it names
    rather than that the host is unsupported.
    """
    for provider in providers:
        if provider.matches(url):
            return provider
    supported = ", ".join(p.name for p in providers) or "none"
    raise UnsupportedProviderError(
        f"no repository provider handles this URL: {url} (supported: {supported})"
    )


def transport_of(url: str) -> GitTransport:
    """How git will reach this remote."""
    return parse_git_url(url).transport


def authentication_of(url: str) -> GitAuthMechanism:
    """Which credential system git will use for this remote. Never a secret."""
    return parse_git_url(url).authentication


def provider_of(
    url: str, providers: Sequence[RepositoryProvider] = DEFAULT_PROVIDERS
) -> GitProvider:
    """The hosting service for this URL, as an enum."""
    return detect_provider(url, providers).provider


def url_credentials(url: str) -> Optional[str]:
    """The kind of credential embedded in a URL, or None if there is none.

    Returns a *description*, never the value: the point of this function is to
    refuse such a URL, and quoting what was in it would write the secret into
    the very error that rejects it.
    """
    try:
        parsed = parse_git_url(url)
    except AcquisitionError:
        return None
    return "username and password or token" if parsed.has_credentials else None
