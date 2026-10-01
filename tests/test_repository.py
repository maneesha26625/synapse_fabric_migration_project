"""Tests for the repository walker.

Everything runs against temporary directories built by the tests themselves:
no network, no git, and no dependency on any real repository.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from discovery_agent.config import DiscoveryConfig
from discovery_agent.errors import ConfigError, ParseError
from discovery_agent.repository import (
    DEFAULT_EXCLUDED_DIRS,
    RepositoryWalker,
    sha256_file,
    walk_repository,
)

PIPELINE_JSON = '{"name": "PL_Load_Sales"}'
NOTEBOOK_JSON = '{"name": "NB_Transform"}'
DEEP_JSON = '{"name": "PL_Deep"}'


def make_config(source, **overrides) -> DiscoveryConfig:
    options = dict(source=Path(source), out=Path(source) / "out")
    options.update(overrides)
    return DiscoveryConfig(**options)


def build_repo(root: Path) -> Path:
    """A small repository resembling a Synapse Git-integrated layout."""
    repo = root / "repo"
    (repo / "pipeline" / "nested" / "deep").mkdir(parents=True)
    (repo / "notebook").mkdir(parents=True)
    (repo / ".git" / "objects").mkdir(parents=True)

    (repo / "pipeline" / "PL_Load_Sales.json").write_text(PIPELINE_JSON, encoding="utf-8")
    (repo / "pipeline" / "nested" / "deep" / "PL_Deep.json").write_text(
        DEEP_JSON, encoding="utf-8"
    )
    (repo / "notebook" / "NB_Transform.json").write_text(NOTEBOOK_JSON, encoding="utf-8")
    (repo / "README.md").write_text("# workspace", encoding="utf-8")
    (repo / ".gitignore").write_text("*.tmp", encoding="utf-8")
    (repo / "empty.txt").write_text("", encoding="utf-8")
    (repo / ".git" / "config").write_text("[core]", encoding="utf-8")
    (repo / ".git" / "objects" / "abcdef").write_text("blob", encoding="utf-8")
    return repo


def paths_of(result) -> list:
    return [f.relative_path for f in result.files]


# --- recursive discovery, nesting, relative paths ----------------------------


def test_walk_discovers_files_recursively(tmp_path):
    repo = build_repo(tmp_path)
    result = walk_repository(make_config(repo))

    assert paths_of(result) == [
        ".gitignore",
        "README.md",
        "empty.txt",
        "notebook/NB_Transform.json",
        "pipeline/PL_Load_Sales.json",
        "pipeline/nested/deep/PL_Deep.json",
    ]
    assert result.file_count == 6
    assert result.skipped == ()
    assert result.filtered_count == 0


def test_relative_paths_use_forward_slashes_at_every_depth(tmp_path):
    repo = build_repo(tmp_path)
    result = walk_repository(make_config(repo))

    assert all("\\" not in f.relative_path for f in result.files)
    assert "pipeline/nested/deep/PL_Deep.json" in paths_of(result)


def test_relative_paths_are_relative_to_the_repository_root(tmp_path):
    repo = build_repo(tmp_path)
    result = walk_repository(make_config(repo))

    assert result.root == repo.resolve()
    for entry in result.files:
        assert not Path(entry.relative_path).is_absolute()
        assert (repo / entry.relative_path).is_file()


def test_walk_of_an_empty_repository(tmp_path):
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    result = walk_repository(make_config(repo))

    assert result.files == ()
    assert result.skipped == ()


# --- per-file fields ---------------------------------------------------------


def test_file_name_and_extension(tmp_path):
    repo = build_repo(tmp_path)
    (repo / "SQL_Upper.JSON").write_text("{}", encoding="utf-8")
    by_path = {f.relative_path: f for f in walk_repository(make_config(repo)).files}

    assert by_path["pipeline/PL_Load_Sales.json"].file_name == "PL_Load_Sales.json"
    assert by_path["pipeline/PL_Load_Sales.json"].extension == ".json"
    assert by_path["README.md"].extension == ".md"
    # Extensions are lowercased so grouping is stable across platforms.
    assert by_path["SQL_Upper.JSON"].extension == ".json"
    assert by_path["SQL_Upper.JSON"].file_name == "SQL_Upper.JSON"
    # A dotfile has a name, not an extension.
    assert by_path[".gitignore"].extension == ""


def test_size_bytes(tmp_path):
    repo = build_repo(tmp_path)
    by_path = {f.relative_path: f for f in walk_repository(make_config(repo)).files}

    assert by_path["pipeline/PL_Load_Sales.json"].size_bytes == len(PIPELINE_JSON)
    assert by_path["empty.txt"].size_bytes == 0


def test_sha256_matches_the_file_contents(tmp_path):
    repo = build_repo(tmp_path)
    by_path = {f.relative_path: f for f in walk_repository(make_config(repo)).files}

    expected = hashlib.sha256(PIPELINE_JSON.encode("utf-8")).hexdigest()
    assert by_path["pipeline/PL_Load_Sales.json"].sha256 == expected

    empty = hashlib.sha256(b"").hexdigest()
    assert by_path["empty.txt"].sha256 == empty


def test_sha256_is_chunk_size_independent(tmp_path):
    payload = b"synapse" * 5000
    target = tmp_path / "big.bin"
    target.write_bytes(payload)

    expected = hashlib.sha256(payload).hexdigest()
    assert sha256_file(target) == expected
    assert sha256_file(target, chunk_bytes=7) == expected


def test_to_dict_exposes_the_required_fields(tmp_path):
    repo = build_repo(tmp_path)
    entry = walk_repository(make_config(repo)).files[0]

    assert set(entry.to_dict()) == {
        "relative_path",
        "file_name",
        "extension",
        "size_bytes",
        "sha256",
    }


# --- repository metadata exclusion -------------------------------------------


def test_git_directory_is_excluded(tmp_path):
    repo = build_repo(tmp_path)
    result = walk_repository(make_config(repo))

    assert all(not p.startswith(".git/") for p in paths_of(result))
    # Pruned, not skipped: its contents are never visited at all.
    assert all(not s.relative_path.startswith(".git") for s in result.skipped)
    # The .gitignore file is ordinary repository content and is kept.
    assert ".gitignore" in paths_of(result)


@pytest.mark.parametrize("metadata_dir", DEFAULT_EXCLUDED_DIRS)
def test_all_vcs_metadata_directories_are_excluded(tmp_path, metadata_dir):
    repo = tmp_path / "repo"
    (repo / metadata_dir).mkdir(parents=True)
    (repo / metadata_dir / "internal").write_text("x", encoding="utf-8")
    (repo / "kept.json").write_text("{}", encoding="utf-8")

    assert paths_of(walk_repository(make_config(repo))) == ["kept.json"]


def test_excluded_directories_are_configurable(tmp_path):
    repo = build_repo(tmp_path)
    walker = RepositoryWalker(make_config(repo), excluded_dirs=())
    paths = [f.relative_path for f in walker.walk().files]

    assert ".git/config" in paths


# --- include / exclude filters -----------------------------------------------


def test_include_filter_selects_matching_files(tmp_path):
    repo = build_repo(tmp_path)
    result = walk_repository(make_config(repo, include=("*.json",)))

    assert paths_of(result) == [
        "notebook/NB_Transform.json",
        "pipeline/PL_Load_Sales.json",
        "pipeline/nested/deep/PL_Deep.json",
    ]
    assert result.filtered_count == 3


def test_include_filter_can_target_a_subtree(tmp_path):
    repo = build_repo(tmp_path)
    result = walk_repository(make_config(repo, include=("pipeline/*",)))

    assert paths_of(result) == [
        "pipeline/PL_Load_Sales.json",
        "pipeline/nested/deep/PL_Deep.json",
    ]


def test_exclude_filter_drops_matching_files(tmp_path):
    repo = build_repo(tmp_path)
    result = walk_repository(make_config(repo, exclude=("*.md", "*.txt")))

    assert "README.md" not in paths_of(result)
    assert "empty.txt" not in paths_of(result)
    assert result.filtered_count == 2


def test_exclude_prunes_whole_directories(tmp_path):
    repo = build_repo(tmp_path)
    result = walk_repository(make_config(repo, exclude=("pipeline",)))

    assert all(not p.startswith("pipeline/") for p in paths_of(result))


def test_exclude_wins_over_include(tmp_path):
    repo = build_repo(tmp_path)
    result = walk_repository(
        make_config(repo, include=("*.json",), exclude=("notebook/*",))
    )

    assert paths_of(result) == [
        "pipeline/PL_Load_Sales.json",
        "pipeline/nested/deep/PL_Deep.json",
    ]


def test_pattern_matching_is_case_sensitive_on_every_platform(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Pipeline.JSON").write_text("{}", encoding="utf-8")

    assert paths_of(walk_repository(make_config(repo, include=("*.json",)))) == []
    assert paths_of(walk_repository(make_config(repo, include=("*.JSON",)))) == [
        "Pipeline.JSON"
    ]


# --- max-file-bytes ----------------------------------------------------------


def test_oversized_files_are_skipped_with_a_reason(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "small.json").write_text("{}", encoding="utf-8")
    (repo / "huge.json").write_bytes(b"x" * 500)

    result = walk_repository(make_config(repo, max_file_bytes=100))

    assert paths_of(result) == ["small.json"]
    assert len(result.skipped) == 1
    skipped = result.skipped[0]
    assert skipped.relative_path == "huge.json"
    assert skipped.size_bytes == 500
    assert "exceeds max_file_bytes" in skipped.reason
    assert "500 > 100" in skipped.reason


def test_oversized_file_is_never_opened(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "huge.json").write_bytes(b"x" * 500)

    import discovery_agent.repository as repository

    def fail_if_called(*args, **kwargs):
        raise AssertionError("oversized file must not be read")

    monkeypatch.setattr(repository, "sha256_file", fail_if_called)
    result = walk_repository(make_config(repo, max_file_bytes=100))

    assert result.files == ()
    assert len(result.skipped) == 1


def test_file_exactly_at_the_limit_is_included(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "edge.json").write_bytes(b"x" * 100)

    result = walk_repository(make_config(repo, max_file_bytes=100))

    assert paths_of(result) == ["edge.json"]


def test_oversized_file_raises_under_the_fail_policy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "huge.json").write_bytes(b"x" * 500)

    config = make_config(repo, max_file_bytes=100, on_parse_error="fail")
    with pytest.raises(ParseError) as excinfo:
        walk_repository(config)

    assert excinfo.value.source_file == "huge.json"
    assert "exceeds max_file_bytes" in excinfo.value.reason


# --- deterministic ordering --------------------------------------------------


def test_ordering_is_sorted_and_stable_across_runs(tmp_path):
    repo = tmp_path / "repo"
    (repo / "zeta").mkdir(parents=True)
    (repo / "alpha").mkdir(parents=True)
    for name in ["m.json", "b.json", "z.json", "a.json"]:
        (repo / "zeta" / name).write_text("{}", encoding="utf-8")
        (repo / "alpha" / name).write_text("{}", encoding="utf-8")
    (repo / "top.json").write_text("{}", encoding="utf-8")

    first = walk_repository(make_config(repo))
    second = walk_repository(make_config(repo))

    assert paths_of(first) == sorted(paths_of(first))
    assert first.files == second.files
    assert first.skipped == second.skipped


def test_skipped_entries_are_sorted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ["z.bin", "a.bin", "m.bin"]:
        (repo / name).write_bytes(b"x" * 500)

    result = walk_repository(make_config(repo, max_file_bytes=10))
    ordered = [s.relative_path for s in result.skipped]

    assert ordered == sorted(ordered)


# --- symlinks ----------------------------------------------------------------


def symlinks_supported(tmp_path) -> bool:
    probe = tmp_path / "probe"
    target = tmp_path / "probe-target"
    target.write_text("x", encoding="utf-8")
    try:
        probe.symlink_to(target)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


def test_symlink_pointing_outside_the_repository_is_skipped(tmp_path):
    if not symlinks_supported(tmp_path):
        pytest.skip("symlink creation is not permitted in this environment")

    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside.json").write_text("{}", encoding="utf-8")
    (repo / "escape.json").symlink_to(outside)

    result = walk_repository(make_config(repo))

    assert paths_of(result) == ["inside.json"]
    assert [s.relative_path for s in result.skipped] == ["escape.json"]
    assert "outside the repository" in result.skipped[0].reason


def test_directory_symlinks_are_not_followed(tmp_path):
    if not symlinks_supported(tmp_path):
        pytest.skip("symlink creation is not permitted in this environment")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leaked.json").write_text("{}", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "kept.json").write_text("{}", encoding="utf-8")
    try:
        (repo / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation is not permitted")

    result = walk_repository(make_config(repo))

    assert paths_of(result) == ["kept.json"]
    assert [s.relative_path for s in result.skipped] == ["link"]
    assert "not followed" in result.skipped[0].reason


# --- configuration -----------------------------------------------------------


def test_missing_repository_path_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        walk_repository(make_config(tmp_path / "does-not-exist"))


def test_walker_writes_nothing(tmp_path):
    repo = build_repo(tmp_path)
    before = sorted(str(p) for p in repo.rglob("*"))

    walk_repository(make_config(repo))

    assert sorted(str(p) for p in repo.rglob("*")) == before
    assert not (Path(repo) / "out").exists()
