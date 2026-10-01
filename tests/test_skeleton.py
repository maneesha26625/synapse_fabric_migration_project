"""Smoke tests for the Discovery Agent skeleton.

These pin down the pieces that already have behaviour: config validation,
stable asset IDs, and CLI argument parsing.
"""

from pathlib import Path

import pytest

from discovery_agent import discovery
from discovery_agent.cli import build_parser, config_from_args
from discovery_agent.config import DiscoveryConfig
from discovery_agent.errors import ConfigError
from discovery_agent.models import AssetType, asset_id


def test_asset_id_is_stable():
    assert asset_id(AssetType.PIPELINE, "PL_Load_Sales") == "synapse://pipeline/PL_Load_Sales"


def test_config_rejects_missing_source(tmp_path):
    config = DiscoveryConfig(source=tmp_path / "nope", out=tmp_path / "out")
    with pytest.raises(ConfigError):
        config.validate()


def test_config_accepts_existing_source(tmp_path):
    DiscoveryConfig(source=tmp_path, out=tmp_path / "out").validate()


def test_cli_parses_source_and_defaults():
    args = build_parser().parse_args(["--source", "repo"])
    config = config_from_args(args)
    assert config.source == Path("repo")
    assert config.out == Path("discovery-output")
    assert config.on_parse_error == "skip"


def test_run_over_an_empty_directory_reports_nothing_found(tmp_path):
    """The pipeline is implemented now, so an empty source is an empty run
    rather than a NotImplementedError. Nothing found is a valid answer; an
    exception would not be."""
    config = DiscoveryConfig(source=tmp_path, out=tmp_path / "out")

    result = discovery.run(config)

    assert result.walk.file_count == 0
    assert result.detection.artifacts == ()
    assert result.extraction.results == ()
    assert result.snapshot is None
    assert result.catalog is None
