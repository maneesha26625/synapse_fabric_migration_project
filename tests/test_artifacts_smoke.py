"""Smoke test of the detector against the real local repository snapshot.

Reads only the local clone produced by repository acquisition — no network, no
git. It skips cleanly when the snapshot is absent, so the suite still passes on
a fresh checkout of this project.

Assertions are about which artifact types are recognized, not how many of each
there are: the upstream repository may legitimately gain or lose files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from discovery_agent.artifacts import ArtifactCategory, detect_artifacts
from discovery_agent.config import DiscoveryConfig
from discovery_agent.repository import walk_repository

REPOSITORY = (
    Path("input") / "repository" / "Test-Drive-Azure-Synapse-with-a-1-click-POC"
)

pytestmark = pytest.mark.skipif(
    not REPOSITORY.is_dir(),
    reason=f"local repository snapshot not present at {REPOSITORY}",
)


@pytest.fixture(scope="module")
def detection():
    config = DiscoveryConfig(source=REPOSITORY, out=Path("discovery-output"))
    return detect_artifacts(walk_repository(config), config)


def test_synapse_artifacts_are_found(detection):
    assert detection.synapse_artifacts, "no Synapse artifacts detected"


def test_expected_artifact_types_are_present(detection):
    found = set(detection.synapse_counts_by_type())

    for expected in ("notebook", "pipeline", "dataset", "linkedService", "dataflow"):
        assert expected in found, f"{expected} not detected"


def test_notebook_is_classified_confidently(detection):
    notebooks = [a for a in detection.synapse_artifacts if a.artifact_type == "notebook"]

    assert notebooks
    for notebook in notebooks:
        assert notebook.confidence == 1.0
        assert notebook.source_format == "synapse_notebook_json"
        assert "notebook/" in notebook.source_path


def test_arm_templates_are_not_reported_as_artifacts(detection):
    arm = [a for a in detection.artifacts if a.source_format == "arm_template_json"]

    assert arm, "expected the deployment templates to be recognized"
    for template in arm:
        assert template.category is ArtifactCategory.NON_SYNAPSE


def test_no_artifact_lacks_evidence(detection):
    for artifact in detection.artifacts:
        assert artifact.evidence.signals, f"no evidence for {artifact.source_path}"


def test_synapse_artifacts_live_under_the_workspace_root(detection):
    """Every detected artifact should sit in a Synapse workspace folder."""
    for artifact in detection.synapse_artifacts:
        assert "/" in artifact.source_path
        assert artifact.confidence >= 0.7


def test_detection_is_deterministic(detection):
    config = DiscoveryConfig(source=REPOSITORY, out=Path("discovery-output"))
    again = detect_artifacts(walk_repository(config), config)

    assert again.artifacts == detection.artifacts
