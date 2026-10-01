"""The repository source object handed to every later stage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepositorySource:
    """A local snapshot of the source repository for one migration run.

    Once this exists, no later stage needs the network: everything downstream
    reads ``local_path`` and treats ``commit_sha`` as the identity of the
    snapshot it scanned.
    """

    provider: str
    repository_url: str
    ref: str
    local_path: Path
    commit_sha: str
    reused: bool = False

    def to_dict(self) -> dict:
        """JSON-serializable form, for run manifests and CLI output."""
        return {
            "provider": self.provider,
            "repository_url": self.repository_url,
            "ref": self.ref,
            "local_path": str(self.local_path),
            "commit_sha": self.commit_sha,
            "reused": self.reused,
        }
