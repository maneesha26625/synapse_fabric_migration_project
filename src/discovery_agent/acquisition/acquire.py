"""Repository acquisition: produce a stable local snapshot to migrate from.

This is the only stage that talks to a git remote. It runs once per migration
run; Discovery, Assessment, and Migration all read the resulting local clone.

Reuse policy (deliberate, v1): if the target directory already exists it is
validated and reused as-is. Nothing is fetched, pulled, or checked out, so a
snapshot cannot shift under a run that is already in progress.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional, Sequence

from discovery_agent.acquisition.git import run_git
from discovery_agent.acquisition.models import RepositorySource
from discovery_agent.acquisition.providers import (
    DEFAULT_PROVIDERS,
    RepositoryProvider,
    detect_provider,
    normalize_for_compare,
    parse_git_url,
)
from discovery_agent.errors import (
    AcquisitionError,
    GitCommandError,
    RepositoryReuseError,
)

DEFAULT_INPUT_ROOT = Path("input") / "repository"

GitRunner = Callable[..., str]


def acquire_repository(
    repository_url: str,
    ref: Optional[str] = None,
    input_root: Path = DEFAULT_INPUT_ROOT,
    providers: Sequence[RepositoryProvider] = DEFAULT_PROVIDERS,
    git: GitRunner = run_git,
) -> RepositorySource:
    """Clone the repository locally, or reuse an existing clone, and describe it.

    ``git`` is injectable so the logic can be tested without a network or a
    real repository.
    """
    url = repository_url.strip()

    if parse_git_url(url).has_credentials:
        raise AcquisitionError(
            "repository URL must not embed credentials. Use a git credential "
            "helper or SSH key instead; this tool never stores secrets."
        )

    provider = detect_provider(url, providers)
    target = Path(input_root) / provider.repository_name(url)

    if target.exists():
        return _reuse_existing(provider, url, ref, target, git)
    return _clone_fresh(provider, url, ref, target, git)


def _reuse_existing(
    provider: RepositoryProvider,
    url: str,
    ref: Optional[str],
    target: Path,
    git: GitRunner,
) -> RepositorySource:
    """Validate and reuse a clone that is already on disk. No network access."""
    if not target.is_dir():
        raise RepositoryReuseError(
            f"{target} exists but is not a directory; remove it and retry"
        )

    try:
        git(["rev-parse", "--git-dir"], cwd=target)
    except GitCommandError:
        raise RepositoryReuseError(
            f"{target} exists but is not a git repository; remove it and retry"
        )

    try:
        origin = git(["remote", "get-url", "origin"], cwd=target)
    except GitCommandError:
        raise RepositoryReuseError(
            f"{target} has no 'origin' remote, so it cannot be verified against "
            f"{url}; remove it and retry"
        )

    if normalize_for_compare(origin) != normalize_for_compare(url):
        raise RepositoryReuseError(
            f"{target} is a clone of a different repository than {url}; "
            f"remove it or choose a different input root"
        )

    head = git(["rev-parse", "HEAD"], cwd=target)

    if ref:
        commit_expr = ref + "^{commit}"
        try:
            resolved = git(["rev-parse", "--verify", "--quiet", commit_expr], cwd=target)
        except GitCommandError:
            raise RepositoryReuseError(
                f"ref '{ref}' is not present in the existing clone at {target}. "
                f"This version never fetches; remove the directory to re-clone."
            )
        if resolved != head:
            raise RepositoryReuseError(
                f"the existing clone at {target} is checked out at {head}, but ref "
                f"'{ref}' is {resolved}. This version never updates an existing "
                f"clone; remove the directory to re-clone, or rerun with the ref "
                f"that matches the snapshot."
            )
        recorded_ref = ref
    else:
        recorded_ref = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=target)

    return RepositorySource(
        provider=provider.name,
        repository_url=url,
        ref=recorded_ref,
        local_path=target.resolve(),
        commit_sha=head,
        reused=True,
    )


def _clone_fresh(
    provider: RepositoryProvider,
    url: str,
    ref: Optional[str],
    target: Path,
    git: GitRunner,
) -> RepositorySource:
    """Clone into ``target`` and check out ``ref`` if one was requested."""
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        git(["clone", url, str(target)])
        if ref:
            git(["checkout", ref], cwd=target)
    except GitCommandError:
        # Never leave a half-built snapshot behind for the reuse path to find.
        shutil.rmtree(str(target), ignore_errors=True)
        raise

    head = git(["rev-parse", "HEAD"], cwd=target)
    recorded_ref = ref if ref else git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=target)

    return RepositorySource(
        provider=provider.name,
        repository_url=url,
        ref=recorded_ref,
        local_path=target.resolve(),
        commit_sha=head,
        reused=False,
    )
