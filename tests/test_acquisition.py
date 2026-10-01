"""Tests for repository acquisition.

These run entirely offline. The clone/reuse logic is driven through an
injected fake git runner, so no real repository, host, or network is touched.
The one test that runs real git builds its own throwaway repository in tmp_path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from discovery_agent.acquisition import (
    GitHubProvider,
    RepositorySource,
    acquire_repository,
    detect_provider,
    parse_git_url,
)
from discovery_agent.acquisition.git import run_git
from discovery_agent.errors import (
    AcquisitionError,
    GitCommandError,
    RepositoryReuseError,
    UnsupportedProviderError,
)

URL = "https://github.com/contoso/synapse-workspace"
HEAD_SHA = "1111111111111111111111111111111111111111"
OTHER_SHA = "2222222222222222222222222222222222222222"


class FakeGit:
    """Stand-in for run_git that records calls and replays canned answers.

    Keys are the leading words of the git argument vector; the longest
    matching prefix wins, so a test can override one subcommand.
    """

    def __init__(self, responses=None, failures=(), clone_creates=True):
        self.responses = dict(responses or {})
        self.failures = set(failures)
        self.clone_creates = clone_creates
        self.calls = []

    def __call__(self, args, cwd=None, timeout=None):
        args = tuple(str(a) for a in args)
        self.calls.append((args, None if cwd is None else Path(cwd)))

        for length in range(len(args), 0, -1):
            prefix = args[:length]
            if prefix in self.failures:
                raise GitCommandError(args, 1, "fake failure")
            if prefix in self.responses:
                if args[0] == "clone" and self.clone_creates:
                    Path(args[2]).mkdir(parents=True, exist_ok=True)
                return self.responses[prefix]

        if args[0] == "clone":
            if self.clone_creates:
                Path(args[2]).mkdir(parents=True, exist_ok=True)
            return ""
        raise AssertionError(f"unexpected git call: {args}")

    @property
    def subcommands(self):
        return [args[0] for args, _ in self.calls]


def clone_git(overrides=None):
    """FakeGit primed for a successful fresh clone."""
    responses = {
        ("rev-parse", "HEAD"): HEAD_SHA,
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("checkout",): "",
    }
    responses.update(overrides or {})
    return FakeGit(responses)


def reuse_git(origin=URL, head=HEAD_SHA, overrides=None):
    """FakeGit primed for a valid existing clone."""
    responses = {
        ("rev-parse", "--git-dir"): ".git",
        ("remote", "get-url", "origin"): origin,
        ("rev-parse", "HEAD"): head,
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
    }
    responses.update(overrides or {})
    return FakeGit(responses)


# --- URL parsing and provider detection -------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/contoso/synapse-workspace", "synapse-workspace"),
        ("https://github.com/contoso/synapse-workspace.git", "synapse-workspace"),
        ("https://github.com/contoso/synapse-workspace/", "synapse-workspace"),
        ("  https://github.com/contoso/synapse-workspace  ", "synapse-workspace"),
        ("git@github.com:contoso/synapse-workspace.git", "synapse-workspace"),
        ("https://www.github.com/contoso/synapse-workspace", "synapse-workspace"),
    ],
)
def test_repository_name_is_extracted_from_url_forms(url, expected):
    assert parse_git_url(url).name == expected
    assert GitHubProvider().repository_name(url) == expected


def test_ssh_user_is_not_treated_as_credentials():
    assert parse_git_url("git@github.com:contoso/repo.git").has_credentials is False


@pytest.mark.parametrize(
    "url",
    [
        "https://token@github.com/contoso/repo",
        "https://user:pass@github.com/contoso/repo",
    ],
)
def test_credentials_in_url_are_detected(url):
    assert parse_git_url(url).has_credentials is True


def test_acquire_rejects_credentials_in_url(tmp_path):
    git = clone_git()
    with pytest.raises(AcquisitionError, match="must not embed credentials"):
        acquire_repository(
            "https://token@github.com/contoso/repo",
            ref="main",
            input_root=tmp_path,
            git=git,
        )
    assert git.calls == []


def test_empty_url_is_rejected():
    with pytest.raises(AcquisitionError):
        parse_git_url("   ")


def test_github_provider_detected():
    assert detect_provider(URL).name == "github"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/contoso/repo", "github"),
        ("https://dev.azure.com/org/project/_git/repo", "azure_devops"),
        ("https://gitlab.com/contoso/repo", "gitlab"),
        ("https://bitbucket.org/contoso/repo", "bitbucket"),
        ("https://git.corp.example.com/contoso/repo.git", "generic"),
    ],
)
def test_every_supported_provider_is_detected(url, expected):
    """Azure DevOps, GitLab and Bitbucket used to raise here. Supporting them
    is the point of the change; self-hosted hosts fall to ``generic`` and are
    reached exactly the same way."""
    assert detect_provider(url).name == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/contoso",  # a user page, not a repository
        "https://dev.azure.com/org",  # an organisation, not a repository
        "not-a-url",
    ],
)
def test_unsupported_urls_raise(url):
    """A known host with a malformed path stays an error rather than being
    quietly adopted by the generic provider -- which is why the generic
    provider claims only hosts no named provider does."""
    with pytest.raises((UnsupportedProviderError, AcquisitionError)):
        detect_provider(url)


# --- fresh clone ------------------------------------------------------------


def test_clone_when_not_present(tmp_path):
    git = clone_git()
    source = acquire_repository(URL, ref="main", input_root=tmp_path, git=git)

    target = tmp_path / "synapse-workspace"
    assert git.calls[0] == (("clone", URL, str(target)), None)
    assert (("checkout", "main"), target) in git.calls

    assert isinstance(source, RepositorySource)
    assert source.provider == "github"
    assert source.repository_url == URL
    assert source.ref == "main"
    assert source.commit_sha == HEAD_SHA
    assert source.local_path == target.resolve()
    assert source.reused is False


def test_clone_without_ref_records_default_branch(tmp_path):
    git = clone_git()
    source = acquire_repository(URL, input_root=tmp_path, git=git)

    assert "checkout" not in git.subcommands
    assert source.ref == "main"
    assert source.commit_sha == HEAD_SHA


def test_clone_accepts_a_commit_sha_as_ref(tmp_path):
    git = clone_git({("rev-parse", "HEAD"): OTHER_SHA})
    source = acquire_repository(URL, ref=OTHER_SHA, input_root=tmp_path, git=git)

    target = tmp_path / "synapse-workspace"
    assert (("checkout", OTHER_SHA), target) in git.calls
    assert source.ref == OTHER_SHA
    assert source.commit_sha == OTHER_SHA


def test_failed_clone_leaves_no_partial_snapshot(tmp_path):
    git = FakeGit(responses={}, failures={("clone",)})
    with pytest.raises(GitCommandError):
        acquire_repository(URL, ref="main", input_root=tmp_path, git=git)
    assert not (tmp_path / "synapse-workspace").exists()


def test_failed_checkout_removes_the_clone(tmp_path):
    git = clone_git()
    git.failures.add(("checkout",))
    with pytest.raises(GitCommandError):
        acquire_repository(URL, ref="nonexistent", input_root=tmp_path, git=git)
    assert not (tmp_path / "synapse-workspace").exists()


# --- reuse of an existing clone ---------------------------------------------


def test_existing_clone_is_reused_without_cloning(tmp_path):
    target = tmp_path / "synapse-workspace"
    target.mkdir(parents=True)
    git = reuse_git(overrides={("rev-parse", "--verify", "--quiet"): HEAD_SHA})

    source = acquire_repository(URL, ref="main", input_root=tmp_path, git=git)

    assert "clone" not in git.subcommands
    assert "fetch" not in git.subcommands
    assert "pull" not in git.subcommands
    assert "checkout" not in git.subcommands
    assert source.reused is True
    assert source.commit_sha == HEAD_SHA
    assert source.local_path == target.resolve()


def test_existing_clone_reused_without_ref(tmp_path):
    (tmp_path / "synapse-workspace").mkdir(parents=True)
    git = reuse_git()

    source = acquire_repository(URL, input_root=tmp_path, git=git)

    assert source.reused is True
    assert source.ref == "main"


def test_reuse_matches_origin_across_url_spellings(tmp_path):
    (tmp_path / "synapse-workspace").mkdir(parents=True)
    git = reuse_git(origin="git@github.com:Contoso/Synapse-Workspace.git")
    source = acquire_repository(URL, input_root=tmp_path, git=git)
    assert source.reused is True


def test_reuse_rejects_a_different_repository(tmp_path):
    (tmp_path / "synapse-workspace").mkdir(parents=True)
    git = reuse_git(origin="https://github.com/other/synapse-workspace")

    with pytest.raises(RepositoryReuseError, match="different repository"):
        acquire_repository(URL, input_root=tmp_path, git=git)


def test_reuse_rejects_ref_that_is_not_head(tmp_path):
    (tmp_path / "synapse-workspace").mkdir(parents=True)
    git = reuse_git(overrides={("rev-parse", "--verify", "--quiet"): OTHER_SHA})

    with pytest.raises(RepositoryReuseError, match="never updates an existing"):
        acquire_repository(URL, ref="release", input_root=tmp_path, git=git)


def test_reuse_rejects_ref_missing_from_the_clone(tmp_path):
    (tmp_path / "synapse-workspace").mkdir(parents=True)
    git = reuse_git()
    git.failures.add(("rev-parse", "--verify", "--quiet"))

    with pytest.raises(RepositoryReuseError, match="not present in the existing clone"):
        acquire_repository(URL, ref="release", input_root=tmp_path, git=git)


def test_reuse_rejects_directory_that_is_not_a_repository(tmp_path):
    (tmp_path / "synapse-workspace").mkdir(parents=True)
    git = reuse_git()
    git.failures.add(("rev-parse", "--git-dir"))

    with pytest.raises(RepositoryReuseError, match="not a git repository"):
        acquire_repository(URL, input_root=tmp_path, git=git)


def test_reuse_rejects_clone_without_origin(tmp_path):
    (tmp_path / "synapse-workspace").mkdir(parents=True)
    git = reuse_git()
    git.failures.add(("remote", "get-url", "origin"))

    with pytest.raises(RepositoryReuseError, match="no 'origin' remote"):
        acquire_repository(URL, input_root=tmp_path, git=git)


def test_reuse_rejects_a_plain_file_at_the_target_path(tmp_path):
    (tmp_path / "synapse-workspace").write_text("not a repo")

    with pytest.raises(RepositoryReuseError, match="not a directory"):
        acquire_repository(URL, input_root=tmp_path, git=reuse_git())


# --- the repository source object -------------------------------------------


def test_to_dict_is_json_ready(tmp_path):
    git = clone_git()
    source = acquire_repository(URL, ref="main", input_root=tmp_path, git=git)
    data = source.to_dict()

    assert set(data) == {
        "provider",
        "repository_url",
        "ref",
        "local_path",
        "commit_sha",
        "reused",
    }
    assert isinstance(data["local_path"], str)


# --- the real git wrapper ---------------------------------------------------

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


@requires_git
def test_run_git_reads_a_real_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(["init", "--quiet"], cwd=repo)
    run_git(
        [
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "user.name=tests",
            "commit",
            "--allow-empty",
            "--quiet",
            "-m",
            "initial",
        ],
        cwd=repo,
    )

    sha = run_git(["rev-parse", "HEAD"], cwd=repo)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


@requires_git
def test_run_git_raises_on_failure(tmp_path):
    with pytest.raises(GitCommandError) as excinfo:
        run_git(["rev-parse", "HEAD"], cwd=tmp_path)
    assert excinfo.value.returncode not in (0, None)
