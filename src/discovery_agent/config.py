"""Run configuration for the Discovery Agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from discovery_agent.errors import ConfigError

DEFAULT_OUTPUT_DIR = "discovery-output"
DEFAULT_MAX_FILE_BYTES = 5_000_000


@dataclass(frozen=True)
class DiscoveryConfig:
    """Everything a single discovery run needs to know.

    Deliberately repository-only: there is no workspace, credential, or model
    configuration here. Those belong to components we have not built yet.
    """

    source: Path
    out: Path
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    on_parse_error: str = "skip"  # "skip" | "fail"

    def validate(self) -> None:
        """Raise ConfigError if this configuration cannot produce a run."""
        if not self.source.exists():
            raise ConfigError(f"source path does not exist: {self.source}")
        if not self.source.is_dir():
            raise ConfigError(f"source path is not a directory: {self.source}")
        if self.on_parse_error not in ("skip", "fail"):
            raise ConfigError(
                f"on_parse_error must be 'skip' or 'fail', got: {self.on_parse_error}"
            )
        if self.max_file_bytes <= 0:
            raise ConfigError(f"max_file_bytes must be positive, got: {self.max_file_bytes}")
