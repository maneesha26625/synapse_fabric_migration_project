"""Shared fixtures for the smoke tests that run over the real local snapshot.

The four-stage pipeline used to be assembled by hand in every smoke test, and
each copy fabricated its own repository identity -- ``commit_sha="0" * 40``.
That is exactly what ``discovery.run`` exists to stop, so the helpers here
call it instead of rebuilding it.

``smoke_run(connected=True)`` goes through ``ConnectionManager`` and
``GitConnection``, which takes acquisition's *reuse* path: the clone is
already on disk, so nothing is fetched and no network is touched. What it
does produce is the snapshot's real commit SHA, ref and origin URL, which is
what provenance is supposed to carry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from discovery_agent import discovery
from discovery_agent.config import DiscoveryConfig

#: Where acquisition puts clones, and the one this repository's smoke tests use.
SMOKE_INPUT_ROOT = Path("input") / "repository"
SMOKE_REPOSITORY_NAME = "Test-Drive-Azure-Synapse-with-a-1-click-POC"
SMOKE_REPOSITORY = SMOKE_INPUT_ROOT / SMOKE_REPOSITORY_NAME
SMOKE_REPOSITORY_URL = f"https://github.com/Azure/{SMOKE_REPOSITORY_NAME}"

@pytest.fixture(autouse=True)
def _isolated_sign_in_state(tmp_path, monkeypatch):
    """No test reads, writes or deletes the operator's real authentication
    record, and no held identity leaks from one test into the next."""
    from discovery_agent.connections.azure import ENV_HOME, reset_credentials

    monkeypatch.setenv(ENV_HOME, str(tmp_path / "sign-in-home"))
    reset_credentials()
    yield
    reset_credentials()


@pytest.fixture(autouse=True)
def _isolated_local_state(tmp_path, monkeypatch):
    """No test reads, writes or deletes what a real session keeps on this machine: the API's saved discovery and
    Fabric target (``.accelerator/state``), accepted LLM transforms (``transforms/``), and uploaded ZIPs and
    environment working copies (``input/``). A test API started with ``create_server`` used to delete a running
    session's saved discovery, and leave its uploads behind."""
    from discovery_agent.api import repository_source

    monkeypatch.setenv("MIGRATION_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MIGRATION_TRANSFORMS_DIR", str(tmp_path / "transforms"))
    monkeypatch.setenv("MIGRATION_LOG_DIR", str(tmp_path / "logs"))
    # Runs start without a person's sign-off unless a test asks for one (``MIGRATION_SIGNOFF``, ``state.approvals``),
    # and the operator a test sees is a fixed name, never this machine's account.
    monkeypatch.setenv("MIGRATION_SIGNOFF", "never")
    monkeypatch.setenv("MIGRATION_OPERATOR", "Test Operator")
    # Runs start without an approved migration brief unless a test asks for one (``MIGRATION_CONSULTATION``, ``api.consult``),
    # and the consultation's help never asks the model: no test sends anything to Claude.
    monkeypatch.setenv("MIGRATION_CONSULTATION", "advisory")
    monkeypatch.delenv("MIGRATION_BRIEF_APPROVAL", raising=False)
    from discovery_agent.llm import claude_cli

    monkeypatch.setattr(claude_cli, "executable", lambda: None)  # a test that needs one gives a fake
    monkeypatch.setattr(repository_source, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(repository_source, "WORK_ROOT", tmp_path / "environments")


#: Every smoke module skips on this: the snapshot is gitignored, so a fresh
#: checkout has none until acquisition has been run once.
requires_snapshot = pytest.mark.skipif(
    not SMOKE_REPOSITORY.is_dir(),
    reason=f"local repository snapshot not present at {SMOKE_REPOSITORY}",
)


def smoke_config(**overrides) -> DiscoveryConfig:
    fields = {"source": SMOKE_REPOSITORY, "out": Path("discovery-output")}
    fields.update(overrides)
    return DiscoveryConfig(**fields)


def smoke_run(connected: bool = False) -> discovery.DiscoveryRun:
    """One discovery run over the local snapshot.

    ``connected=False`` is the repository-only path: a local directory, no
    manager, no connection of any kind.

    ``connected=True`` drives the same pipeline through the connection layer.
    Acquisition finds the clone already present and reuses it -- the reuse
    path never fetches, pulls or checks out -- so this stays offline while
    still producing the real snapshot identity.
    """
    if not connected:
        return discovery.run(smoke_config())

    from discovery_agent.connections.manager import ConnectionManager
    from discovery_agent.connections.models import (
        ConnectionSettings,
        GitRepositoryConfig,
    )

    connections = ConnectionManager(
        ConnectionSettings(
            git=GitRepositoryConfig(repository_url=SMOKE_REPOSITORY_URL)
        )
    )
    return discovery.run(
        smoke_config(), connections=connections, input_root=SMOKE_INPUT_ROOT
    )
