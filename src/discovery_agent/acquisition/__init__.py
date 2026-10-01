"""Repository acquisition.

Turns a remote repository URL plus a branch/ref into a stable local snapshot,
once per migration run. Every later stage reads that snapshot from disk and
never contacts the git host again.
"""

from discovery_agent.acquisition.acquire import DEFAULT_INPUT_ROOT, acquire_repository
from discovery_agent.acquisition.models import RepositorySource
from discovery_agent.acquisition.providers import (
    DEFAULT_PROVIDERS,
    GitHubProvider,
    RepositoryProvider,
    detect_provider,
    parse_git_url,
)

__all__ = [
    "DEFAULT_INPUT_ROOT",
    "DEFAULT_PROVIDERS",
    "GitHubProvider",
    "RepositoryProvider",
    "RepositorySource",
    "acquire_repository",
    "detect_provider",
    "parse_git_url",
]
