"""Repository walker: a deterministic file-level inventory of a local clone.

This stage reads the local snapshot produced by repository acquisition and
nothing else. It never touches a network, and it deliberately knows nothing
about Synapse: every file is just a path, a size, and a hash. Deciding which
files are Synapse assets is artifact detection's job, one stage later.

Determinism is the contract. Walking the same commit twice must produce
identical results, so directory traversal is sorted, relative paths are always
``/``-separated, and glob matching is case-sensitive on every platform.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from discovery_agent.config import DiscoveryConfig
from discovery_agent.errors import ParseError

# Version-control metadata. Pruned during the walk, so their contents are
# never stat'd or hashed. Anything else belongs in --exclude.
DEFAULT_EXCLUDED_DIRS: Tuple[str, ...] = (".git", ".hg", ".svn")

HASH_CHUNK_BYTES = 1024 * 1024

REASON_TOO_LARGE = "exceeds max_file_bytes"
REASON_SYMLINK_OUTSIDE = "symlink target is outside the repository"
REASON_SYMLINK_DIRECTORY = "directory symlinks are not followed"
REASON_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class DiscoveredFile:
    """One file that belongs to the repository snapshot."""

    relative_path: str  # relative to the repository root, always "/"-separated
    file_name: str
    extension: str  # lowercased, leading dot included; "" when there is none
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict:
        return {
            "relative_path": self.relative_path,
            "file_name": self.file_name,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class SkippedFile:
    """A path the walker saw but could not inventory, and why.

    These feed the coverage report: a file missing from the inventory must
    always be explainable, never silently absent.
    """

    relative_path: str
    size_bytes: Optional[int]
    reason: str

    def to_dict(self) -> dict:
        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WalkResult:
    """The outcome of one walk. Structured objects only; serialization is a
    later stage's concern."""

    root: Path
    files: Tuple[DiscoveredFile, ...]
    skipped: Tuple[SkippedFile, ...]
    filtered_count: int = 0  # files dropped by --include/--exclude

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


def sha256_file(path: Path, chunk_bytes: int = HASH_CHUNK_BYTES) -> str:
    """SHA-256 of a file's contents, streamed so memory stays bounded."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class RepositoryWalker:
    """Enumerates the files of a local repository snapshot.

    The repository path is ``config.source``; the walker reads nothing outside
    it and writes nothing at all.
    """

    def __init__(
        self,
        config: DiscoveryConfig,
        excluded_dirs: Sequence[str] = DEFAULT_EXCLUDED_DIRS,
    ) -> None:
        self.config = config
        self.excluded_dirs = tuple(excluded_dirs)

    def walk(self) -> WalkResult:
        """Walk the repository and return its file-level inventory."""
        self.config.validate()
        root = Path(self.config.source).resolve()

        files: List[DiscoveredFile] = []
        skipped: List[SkippedFile] = []
        filtered = 0

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)

            keep: List[str] = []
            for name in sorted(dirnames):
                directory = current / name
                if name in self.excluded_dirs:
                    continue
                relative = self._relative(root, directory)
                if self._is_excluded(relative):
                    continue
                if directory.is_symlink():
                    # Not followed at all: an in-repo symlink can loop, and an
                    # out-of-repo one is not part of the snapshot.
                    skipped.append(SkippedFile(relative, None, REASON_SYMLINK_DIRECTORY))
                    continue
                keep.append(name)
            dirnames[:] = keep

            for name in sorted(filenames):
                path = current / name
                relative = self._relative(root, path)

                if not self._is_selected(relative):
                    filtered += 1
                    continue

                entry = self._inspect(path, relative, name, skipped)
                if entry is not None:
                    files.append(entry)

        return WalkResult(
            root=root,
            files=tuple(sorted(files, key=lambda f: f.relative_path)),
            skipped=tuple(sorted(skipped, key=lambda s: s.relative_path)),
            filtered_count=filtered,
        )

    # -- internals ---------------------------------------------------------

    def _inspect(
        self,
        path: Path,
        relative: str,
        name: str,
        skipped: List[SkippedFile],
    ) -> Optional[DiscoveredFile]:
        """Stat and hash one file, or record why it was skipped."""
        if path.is_symlink() and not self._resolves_inside(path):
            skipped.append(SkippedFile(relative, None, REASON_SYMLINK_OUTSIDE))
            return None

        try:
            size = path.stat().st_size
        except OSError as exc:
            self._on_error(relative, None, f"{REASON_UNREADABLE}: {exc.strerror}", skipped)
            return None

        if size > self.config.max_file_bytes:
            # Deliberately not opened: the whole point is to avoid reading it.
            reason = f"{REASON_TOO_LARGE} ({size} > {self.config.max_file_bytes})"
            self._on_error(relative, size, reason, skipped)
            return None

        try:
            digest = sha256_file(path)
        except OSError as exc:
            self._on_error(relative, size, f"{REASON_UNREADABLE}: {exc.strerror}", skipped)
            return None

        return DiscoveredFile(
            relative_path=relative,
            file_name=name,
            extension=Path(name).suffix.lower(),
            size_bytes=size,
            sha256=digest,
        )

    def _on_error(
        self,
        relative: str,
        size: Optional[int],
        reason: str,
        skipped: List[SkippedFile],
    ) -> None:
        """Apply the configured parse-error policy to one unusable file."""
        if self.config.on_parse_error == "fail":
            raise ParseError(relative, reason)
        skipped.append(SkippedFile(relative, size, reason))

    def _resolves_inside(self, path: Path) -> bool:
        """Whether a symlink's target stays within the repository root."""
        root = Path(self.config.source).resolve()
        try:
            target = path.resolve()
        except OSError:
            return False
        try:
            target.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        return path.relative_to(root).as_posix()

    def _is_excluded(self, relative: str) -> bool:
        return any(fnmatch.fnmatchcase(relative, p) for p in self.config.exclude)

    def _is_selected(self, relative: str) -> bool:
        """Whether a file survives the --include and --exclude filters.

        Patterns match the ``/``-separated relative path with fnmatch
        semantics, where ``*`` also matches ``/`` — so ``*.json`` selects JSON
        files at any depth. Matching is case-sensitive on every platform so
        results do not differ between Windows and Linux.
        """
        if self.config.include and not any(
            fnmatch.fnmatchcase(relative, p) for p in self.config.include
        ):
            return False
        return not self._is_excluded(relative)


def walk_repository(config: DiscoveryConfig) -> WalkResult:
    """Walk the local repository at ``config.source``."""
    return RepositoryWalker(config).walk()
