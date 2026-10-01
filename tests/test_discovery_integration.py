"""Tests for the discovery composition seam.

``discovery.run`` is where the four stages that already existed are finally
joined, and where a ``ConnectionManager`` is consumed. These tests are about
the *joining*: that acquisition happens before the walk, that the real
snapshot identity reaches provenance, that the SQL leg is optional, and --
most importantly -- that a repository-only run constructs no connection at
all.

Everything is offline. The git runner, the SQL connector and the Azure
credential are all fakes, and the repository-only cases use a directory built
in ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from discovery_agent import discovery
from discovery_agent.config import DiscoveryConfig
from discovery_agent.connections.manager import ConnectionManager
from discovery_agent.connections.models import (
    AzureConnectionConfig,
    ConnectionSettings,
    GitRepositoryConfig,
    SynapseConnectionConfig,
)
from discovery_agent.errors import ConfigError, GitCommandError, RepositoryReuseError
from discovery_agent.extractors.models import ExtractionStatus
from discovery_agent.models import AssetType

REPOSITORY_URL = "https://github.com/contoso/synapse-workspace"
HEAD_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SUBSCRIPTION = "e65a4ad3-3054-4391-947c-2a601d0bbf99"
POOL = "poolone"


# --- fixtures ----------------------------------------------------------------


PIPELINE_JSON = {
    "name": "PL_Load_Sales",
    "properties": {
        "activities": [
            {
                "name": "Copy",
                "type": "Copy",
                "inputs": [{"referenceName": "DS_Source", "type": "DatasetReference"}],
            }
        ]
    },
}


def make_repository(root: Path) -> Path:
    """A directory shaped like a Synapse Git-integrated repository."""
    (root / "pipeline").mkdir(parents=True, exist_ok=True)
    (root / "pipeline" / "PL_Load_Sales.json").write_text(
        json.dumps(PIPELINE_JSON), encoding="utf-8"
    )
    (root / "README.md").write_text("# not an artifact", encoding="utf-8")
    return root


def config_for(root: Path, out: Path) -> DiscoveryConfig:
    return DiscoveryConfig(source=root, out=out)


class RecordingGit:
    """A git runner that records calls and serves a reuse-path clone.

    Acquisition's reuse path is what a connected run takes when the snapshot
    is already present, and it is entirely local -- which is what keeps these
    tests offline.
    """

    def __init__(self, target: Path, head: str = HEAD_SHA, fail_on=None):
        self.target = target
        self.head = head
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, args, cwd=None, timeout=None):
        args = tuple(str(a) for a in args)
        self.calls.append(args)
        if self.fail_on is not None and args[:1] == (self.fail_on,):
            raise GitCommandError(list(args), 128, "fake git failure")
        if args[:1] == ("clone",):
            make_repository(Path(args[2]))
            return ""
        if args[:2] == ("rev-parse", "--git-dir"):
            return ".git"
        if args[:3] == ("remote", "get-url", "origin"):
            return REPOSITORY_URL
        if args[:2] == ("rev-parse", "HEAD"):
            return self.head
        if args[:3] == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args[:1] == ("checkout",):
            return ""
        if args[:1] == ("--version",):
            return "git version 2.45.1"
        raise AssertionError(f"unexpected git call: {args}")

    @property
    def subcommands(self):
        return [call[0] for call in self.calls]


class FakeCursor:
    def __init__(self, rows_by_sql):
        self.rows_by_sql = rows_by_sql
        self.description = None
        self._rows = ()

    def execute(self, sql, *parameters):
        names, rows = self.rows_by_sql(sql)
        self.description = tuple((name,) for name in names)
        self._rows = rows

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeSqlConnection:
    def __init__(self, rows_by_sql):
        self.rows_by_sql = rows_by_sql
        self.closed = False

    def cursor(self):
        return FakeCursor(self.rows_by_sql)

    def close(self):
        self.closed = True


class FakeConnector:
    """A ``Connector`` serving one table, through the real DedicatedPoolSource."""

    def __init__(self):
        self.connections = []

    def connect(self):
        connection = FakeSqlConnection(self._rows)
        self.connections.append(connection)
        return connection

    def describe(self):
        return "fake connector"

    @staticmethod
    def _rows(sql):
        text = sql.lower()
        if "db_name" in text:
            return ("database_name", "server_name"), [(POOL, "poc.sql.azuresynapse.net")]
        if "suser_sname" in text or "user_name" in text:
            return ("login_name", "user_name"), [("user@contoso.com", "dbo")]
        if "sys.tables" in text:
            return (
                ("schema_name", "table_name", "object_id", "is_external", "distribution_policy"),
                [("dbo", "Customer", 100, 0, 2)],
            )
        if "sys.columns" in text:
            return (
                ("object_id", "column_id", "column_name", "data_type", "is_nullable"),
                [(100, 1, "customer_id", "int", 0)],
            )
        return (), []


class FakeArtifactsClient:
    """A Synapse data plane that answers from a dict. Never a socket.

    Threaded through ``ConnectionManager(artifacts_client=...)`` so these
    tests keep their promise of being offline: without it the live leg would
    resolve a real hostname, which is not something a unit test may do.
    """

    def __init__(self, resources=None, endpoint="https://ws.dev.azuresynapse.net"):
        from discovery_agent.synapse.client import ArtifactsResponse

        self.endpoint = endpoint
        self.resources = resources if resources is not None else {}
        self.requested = []
        self._response = ArtifactsResponse

    def get(self, url):
        self.requested.append(url)
        route = url.split("?", 1)[0].rsplit("/", 1)[-1]
        listed = self.resources.get(route)
        if listed is None:
            return self._response(404, {"error": {"message": "not found"}})
        return self._response(200, {"value": listed})

    def describe(self):
        return f"synapse artifacts at {self.endpoint}"


def live_pipeline(name="PL_Load_Sales", properties=None):
    """The same pipeline the repository fixture holds, as the API returns it."""
    return {
        "id": f"/subscriptions/{SUBSCRIPTION}/pipelines/{name}",
        "name": name,
        "type": "Microsoft.Synapse/workspaces/pipelines",
        "properties": properties if properties is not None else PIPELINE_JSON["properties"],
        "etag": "live-etag",
    }


class FakeArmTransport:
    """ARM, answering a workspace payload. Records every URL it was given."""

    def __init__(self, repository_url=REPOSITORY_URL, status_code=200):
        self.repository_url = repository_url
        self.status_code = status_code
        self.requested = []

    def get(self, url, token):
        from discovery_agent.connections.azure import ArmResponse

        self.requested.append(url)
        if self.status_code != 200:
            return ArmResponse(self.status_code, {})
        configuration = {}
        if self.repository_url:
            owner, repo = self.repository_url.rstrip("/").rsplit("/", 2)[-2:]
            configuration = {
                "workspaceRepositoryConfiguration": {
                    "type": "WorkspaceGitHubConfiguration",
                    "accountName": owner,
                    "repositoryName": repo,
                    "collaborationBranch": "main",
                }
            }
        return ArmResponse(
            200,
            {
                "id": url.split("?", 1)[0].replace("https://management.azure.com", ""),
                "name": "ws",
                "location": "eastus",
                "properties": {"provisioningState": "Succeeded", **configuration},
            },
        )


class FakeSdkToken:
    def __init__(self, token, expires_on):
        self.token = token
        self.expires_on = expires_on


class FakeSdkCredential:
    def __init__(self):
        self.scopes = []

    def get_token(self, *scopes, **kwargs):
        import time

        self.scopes.append(scopes[0])
        return FakeSdkToken("fake-token-value", int(time.time()) + 3600)


def credential():
    from discovery_agent.connections.azure import AzureCliCredentialProvider

    return AzureCliCredentialProvider(credential=FakeSdkCredential())


def git_settings():
    return ConnectionSettings(
        git=GitRepositoryConfig(repository_url=REPOSITORY_URL, ref="main")
    )


def full_settings():
    return ConnectionSettings(
        azure=AzureConnectionConfig(subscription_id=SUBSCRIPTION),
        synapse=SynapseConnectionConfig(
            resource_group="rg", workspace_name="ws", sql_pool_name=POOL
        ),
        git=GitRepositoryConfig(repository_url=REPOSITORY_URL, ref="main"),
    )


# --- 1: the repository-only path --------------------------------------------


def test_a_repository_only_run_scans_the_local_path(tmp_path):
    root = make_repository(tmp_path / "repo")

    result = discovery.run(config_for(root, tmp_path / "out"))

    assert result.walk.root == root
    assert result.walk.file_count == 2
    assert len(result.detection.synapse_artifacts) == 1
    assert result.extraction.results


def test_a_repository_only_run_constructs_no_connection_at_all(tmp_path, monkeypatch):
    """The rule that matters most: a scan of a clone you already have must not
    acquire a credential requirement.

    Enforced by breaking the constructor rather than by inspecting a call
    count -- if discovery reached for a manager, this would fail loudly.
    """
    import discovery_agent.connections.manager as manager_module

    def refuse(*args, **kwargs):
        raise AssertionError("discovery built a ConnectionManager for a local run")

    monkeypatch.setattr(manager_module, "ConnectionManager", refuse)
    root = make_repository(tmp_path / "repo")

    result = discovery.run(config_for(root, tmp_path / "out"))

    assert result.snapshot is None
    assert result.catalog is None


def test_the_discovery_module_imports_without_the_connection_layer():
    """``connections`` is a type-checking import only.

    A repository-only run must not even load the module that could
    authenticate, which is a stronger guarantee than not calling it.
    """
    import subprocess
    import sys

    probe = (
        "import discovery_agent.discovery, sys; "
        "print('discovery_agent.connections' in sys.modules)"
    )
    output = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert output.stdout.strip() == "False"


def test_a_missing_source_is_still_a_configuration_error(tmp_path):
    with pytest.raises(ConfigError, match="source path does not exist"):
        discovery.run(config_for(tmp_path / "nope", tmp_path / "out"))


# --- 2: the connected path ---------------------------------------------------


def connected_run(
    tmp_path,
    git=None,
    settings=None,
    connector=None,
    artifacts_client=None,
    transport=None,
):
    """One connected run with every live surface faked.

    ``artifacts_client`` and ``transport`` default to fakes rather than to
    None, because None would let the real ones through and these tests must
    not touch a network.
    """
    input_root = tmp_path / "input"
    target = input_root / "synapse-workspace"
    runner = git if git is not None else RecordingGit(target)
    connections = ConnectionManager(
        settings or git_settings(),
        credential=credential(),
        sql_connector=connector,
        git=runner,
        artifacts_client=(
            artifacts_client if artifacts_client is not None else FakeArtifactsClient()
        ),
        transport=transport if transport is not None else FakeArmTransport(),
    )
    result = discovery.run(
        config_for(tmp_path / "placeholder", tmp_path / "out"),
        connections=connections,
        input_root=input_root,
    )
    return result, runner


def test_a_connected_run_acquires_the_repository_and_scans_it(tmp_path):
    result, git = connected_run(tmp_path)

    assert result.snapshot is not None
    assert result.snapshot.repository_url == REPOSITORY_URL
    assert result.walk.root == result.snapshot.local_path
    assert "clone" in git.subcommands
    assert len(result.detection.synapse_artifacts) == 1


def test_acquisition_happens_before_the_walk(tmp_path):
    """Not a preference: ``DiscoveryConfig.validate`` requires the source to
    exist, and the snapshot path does not exist until acquisition creates it.

    The configuration handed in names a directory that is never created, so a
    run that validated before acquiring could not have reached the walk.
    """
    input_root = tmp_path / "input"
    target = input_root / "synapse-workspace"
    git = RecordingGit(target)
    connections = ConnectionManager(git_settings(), credential=credential(), git=git)
    absent = tmp_path / "does-not-exist"

    result = discovery.run(
        config_for(absent, tmp_path / "out"),
        connections=connections,
        input_root=input_root,
    )

    assert not absent.exists(), "the placeholder source was never a real path"
    assert result.walk.root == target.resolve()
    assert git.subcommands[0] == "clone", "the clone was not the first git call"


def test_the_acquired_snapshot_identity_reaches_provenance(tmp_path):
    """The bug this integration exists to fix.

    Every extraction result used to carry whatever the caller invented,
    because nothing connected acquisition to extraction.
    """
    result, _ = connected_run(tmp_path)

    extracted = [r for r in result.extraction.results if r.content is not None]
    assert extracted, "nothing was extracted"
    for one in extracted:
        assert one.provenance.commit_sha == HEAD_SHA
        assert one.provenance.ref == "main"
        assert one.provenance.repository_url == REPOSITORY_URL


def test_a_failed_acquisition_aborts_before_the_walk(tmp_path):
    """Scanning whatever happened to be on disk after a failed clone would
    report an inventory for a snapshot that was never established."""
    input_root = tmp_path / "input"
    git = RecordingGit(input_root / "synapse-workspace", fail_on="clone")
    connections = ConnectionManager(git_settings(), credential=credential(), git=git)

    with pytest.raises(GitCommandError):
        discovery.run(
            config_for(tmp_path / "placeholder", tmp_path / "out"),
            connections=connections,
            input_root=input_root,
        )

    assert "rev-parse" not in git.subcommands, "the walk stage was reached"


def test_a_refused_reuse_aborts_rather_than_scanning_the_wrong_clone(tmp_path):
    """Acquisition's own safety rules still apply; discovery adds none and
    weakens none."""
    input_root = tmp_path / "input"
    target = input_root / "synapse-workspace"
    make_repository(target)

    class WrongOrigin(RecordingGit):
        def __call__(self, args, cwd=None, timeout=None):
            args = tuple(str(a) for a in args)
            if args[:3] == ("remote", "get-url", "origin"):
                self.calls.append(args)
                return "https://github.com/someone/else"
            return super().__call__(args, cwd=cwd, timeout=timeout)

    connections = ConnectionManager(
        git_settings(), credential=credential(), git=WrongOrigin(target)
    )

    with pytest.raises(RepositoryReuseError):
        discovery.run(
            config_for(tmp_path / "placeholder", tmp_path / "out"),
            connections=connections,
            input_root=input_root,
        )


def test_the_run_uses_one_connection_manager_and_one_git_connection(tmp_path):
    """One connection lifecycle per run: discovery composes connections, it
    does not create them."""
    input_root = tmp_path / "input"
    git = RecordingGit(input_root / "synapse-workspace")
    connections = ConnectionManager(git_settings(), credential=credential(), git=git)

    discovery.run(
        config_for(tmp_path / "placeholder", tmp_path / "out"),
        connections=connections,
        input_root=input_root,
    )

    assert connections.git() is connections.git()
    assert git.subcommands.count("clone") == 1


def test_run_settings_survive_the_switch_to_the_acquired_path(tmp_path):
    """include/exclude/size/parse-policy are the caller's and must not be lost
    when the source path is replaced."""
    input_root = tmp_path / "input"
    git = RecordingGit(input_root / "synapse-workspace")
    connections = ConnectionManager(git_settings(), credential=credential(), git=git)
    config = DiscoveryConfig(
        source=tmp_path / "placeholder",
        out=tmp_path / "out",
        exclude=("README.md",),
        on_parse_error="fail",
        max_file_bytes=1234,
    )

    result = discovery.run(config, connections=connections, input_root=input_root)

    assert [f.relative_path for f in result.walk.files] == [
        "pipeline/PL_Load_Sales.json"
    ]


# --- 3: the SQL catalog leg --------------------------------------------------


def test_the_catalog_is_read_when_a_pool_is_configured(tmp_path):
    connector = FakeConnector()

    result, _ = connected_run(tmp_path, settings=full_settings(), connector=connector)

    assert result.catalog is not None
    assert result.catalog.table_count == 1
    assert result.catalog.tables[0].key.name == "Customer"
    assert connector.connections, "no SQL session was opened"


def test_the_catalog_is_absent_rather_than_empty_when_no_pool_is_configured(tmp_path):
    """None and "a pool with no tables" are different answers, and a report
    that confused them would claim an empty estate."""
    result, _ = connected_run(tmp_path)

    assert result.catalog is None


def test_the_catalog_session_is_closed(tmp_path):
    connector = FakeConnector()

    connected_run(tmp_path, settings=full_settings(), connector=connector)

    assert connector.connections[0].closed


def test_the_repository_leg_runs_even_with_a_pool_configured(tmp_path):
    result, _ = connected_run(
        tmp_path, settings=full_settings(), connector=FakeConnector()
    )

    assert result.snapshot is not None
    assert len(result.detection.synapse_artifacts) == 1
    assert result.catalog is not None


# --- 4: the DiscoveryRun value object ----------------------------------------


def test_the_run_composes_the_existing_results_rather_than_replacing_them(tmp_path):
    from discovery_agent.artifacts.models import ArtifactDetectionResult
    from discovery_agent.extractors.models import ExtractionRun
    from discovery_agent.repository import WalkResult

    result, _ = connected_run(
        tmp_path, settings=full_settings(), connector=FakeConnector()
    )

    assert isinstance(result.walk, WalkResult)
    assert isinstance(result.detection, ArtifactDetectionResult)
    assert isinstance(result.extraction, ExtractionRun)
    assert result.is_connected


def test_the_summary_is_serializable_and_carries_no_credential(tmp_path):
    result, _ = connected_run(
        tmp_path, settings=full_settings(), connector=FakeConnector()
    )

    serialized = json.dumps(result.summary(), sort_keys=True)

    assert "fake-token-value" not in serialized
    for forbidden in ("password", "Bearer", "eyJ", "access_token"):
        assert forbidden not in serialized
    assert HEAD_SHA in serialized


def test_two_runs_over_the_same_snapshot_agree(tmp_path):
    """Determinism is the contract; the composition must not weaken it."""
    root = make_repository(tmp_path / "repo")
    config = config_for(root, tmp_path / "out")

    first = discovery.run(config)
    second = discovery.run(config)

    assert [f.sha256 for f in first.walk.files] == [
        f.sha256 for f in second.walk.files
    ]
    assert first.detection.counts_by_type() == second.detection.counts_by_type()
    assert [r.content for r in first.extraction.results] == [
        r.content for r in second.extraction.results
    ]


def test_the_pipeline_actually_extracted_something(tmp_path):
    """A composition that wired the stages in the wrong order could still
    produce a well-shaped, empty result."""
    root = make_repository(tmp_path / "repo")

    result = discovery.run(config_for(root, tmp_path / "out"))

    extracted = [r for r in result.extraction.results if r.content is not None]
    assert len(extracted) == 1
    assert extracted[0].artifact_type is AssetType.PIPELINE
    assert extracted[0].status is ExtractionStatus.SUCCESS
    assert extracted[0].references, "the pipeline's dataset reference was not observed"


# --- 5: the architectural boundary -------------------------------------------


def test_discovery_authenticates_nothing_and_opens_nothing_itself():
    """Discovery composes connections; it does not create them.

    Checked against the module's code rather than its prose, so a docstring
    describing the rule cannot mask a violation of it.
    """
    import ast
    import inspect

    from discovery_agent import discovery as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    code = ast.unparse(tree)

    for forbidden in (
        "azure.identity",
        "AzureCliCredential",
        "credential_provider",
        "pyodbc",
        "subprocess",
        "acquire_repository",
        "PyodbcConnector",
        "ConnectionManager(",
    ):
        assert forbidden not in code, f"discovery.py reaches for {forbidden}"


def test_discovery_reaches_git_and_sql_only_through_the_connection_manager():
    import ast
    import inspect

    from discovery_agent import discovery as module

    code = ast.unparse(ast.parse(inspect.getsource(module)))

    assert "connections.git()" in code
    assert "connections.sql().source()" in code


# --- 6: the CLI --------------------------------------------------------------


def test_the_local_mode_still_works_exactly_as_before(tmp_path, capsys):
    from discovery_agent import cli

    root = make_repository(tmp_path / "repo")

    exit_code = cli.main(["--source", str(root), "--out", str(tmp_path / "out")])
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert f"source: {root}" in printed
    assert "artifacts detected: 2 (1 synapse)" in printed
    assert "repository:" not in printed, "nothing was acquired, so nothing to report"


def test_the_local_mode_never_loads_the_connection_layer(tmp_path):
    """``--source`` must not acquire a credential requirement, and the
    cheapest guarantee is that the code that could remains unimported."""
    import subprocess
    import sys

    root = make_repository(tmp_path / "repo")
    probe = (
        "import sys; from discovery_agent import cli; "
        f"cli.main(['--source', {str(root)!r}, '--out', {str(tmp_path / 'out')!r}]); "
        "print('LOADED' if 'discovery_agent.connections' in sys.modules else 'CLEAN')"
    )
    output = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert output.stdout.strip().endswith("CLEAN")


@pytest.mark.parametrize(
    "argv, expected",
    [
        ([], "nothing to scan"),
        (
            ["--source", "repo", "--repository-url", REPOSITORY_URL],
            "alternatives",
        ),
        (["--source", "repo", "--ref", "main"], "--ref applies to --repository-url"),
        (
            ["--source", "repo", "--input-root", "x"],
            "--input-root applies to --repository-url",
        ),
    ],
)
def test_incomplete_or_contradictory_arguments_are_refused(argv, expected, capsys):
    from discovery_agent import cli

    assert cli.main(argv) == 2
    assert expected in capsys.readouterr().err


def test_a_git_only_run_needs_no_azure_configuration(tmp_path):
    """Acquiring a repository requires no subscription, and none is asked for."""
    from discovery_agent import cli

    args = cli.build_parser().parse_args(["--repository-url", REPOSITORY_URL])
    connections = cli.connections_for(args)

    assert connections is not None
    assert connections.settings.git is not None
    assert connections.settings.azure is None
    assert connections.settings.synapse is None


def test_a_local_run_builds_no_manager():
    from discovery_agent import cli

    args = cli.build_parser().parse_args(["--source", "repo"])

    assert cli.connections_for(args) is None


def test_the_cli_reports_the_acquired_snapshot(tmp_path, capsys, monkeypatch):
    from discovery_agent import cli

    input_root = tmp_path / "input"
    git = RecordingGit(input_root / "synapse-workspace")
    real_manager = ConnectionManager

    def manager_with_fake_git(settings, **kwargs):
        return real_manager(settings, credential=credential(), git=git, **kwargs)

    monkeypatch.setattr(
        "discovery_agent.connections.manager.ConnectionManager", manager_with_fake_git
    )

    exit_code = cli.main(
        [
            "--repository-url",
            REPOSITORY_URL,
            "--ref",
            "main",
            "--input-root",
            str(input_root),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    printed = capsys.readouterr().out

    assert exit_code == 0
    assert f"repository: {REPOSITORY_URL}" in printed
    assert f"commit: {HEAD_SHA}" in printed
    assert "ref: main" in printed


def test_the_cli_summary_is_machine_readable(tmp_path, capsys):
    from discovery_agent import cli

    root = make_repository(tmp_path / "repo")

    cli.main(["--source", str(root), "--out", str(tmp_path / "out"), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["synapse_artifacts"] == 1
    assert payload["repository"] is None
    assert payload["catalog"] is None


def test_the_cli_prints_no_credential(tmp_path, capsys, monkeypatch):
    from discovery_agent import cli

    input_root = tmp_path / "input"
    git = RecordingGit(input_root / "synapse-workspace")
    real_manager = ConnectionManager
    monkeypatch.setattr(
        "discovery_agent.connections.manager.ConnectionManager",
        lambda settings, **kw: real_manager(
            settings, credential=credential(), git=git, **kw
        ),
    )

    cli.main(
        [
            "--repository-url",
            REPOSITORY_URL,
            "--input-root",
            str(input_root),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    printed = capsys.readouterr().out

    for forbidden in ("fake-token-value", "Bearer", "eyJ", "password", "PWD="):
        assert forbidden not in printed


# --- 7: the live workspace leg ------------------------------------------------


def workspace_resources(pipelines=None, **others):
    """A data plane holding one pipeline, and whatever else a test needs."""
    resources = {
        "pipelines": pipelines if pipelines is not None else [live_pipeline()],
    }
    resources.update(others)
    return resources


def live_run(tmp_path, resources=None, transport=None, connector=None):
    return connected_run(
        tmp_path,
        settings=full_settings(),
        connector=connector or FakeConnector(),
        artifacts_client=FakeArtifactsClient(
            resources if resources is not None else workspace_resources()
        ),
        transport=transport,
    )[0]


def test_the_workspace_is_listed_when_one_is_configured(tmp_path):
    result = live_run(tmp_path)

    assert result.synapse is not None
    assert result.synapse.workspace == "ws"
    assert result.synapse.counts_by_artifact()["pipeline"] == 1


def test_no_workspace_configured_is_absent_rather_than_empty(tmp_path):
    result, _ = connected_run(tmp_path)  # git settings only

    assert result.synapse is None
    assert result.workspace_extraction is None


def test_the_live_leg_uses_the_same_extractors_as_the_repository_leg(tmp_path):
    result = live_run(tmp_path)

    live = result.workspace_extraction.results[0]
    committed = next(
        r for r in result.extraction.results if r.artifact_name == "PL_Load_Sales"
    )
    assert live.extractor == committed.extractor
    assert live.artifact_type is AssetType.PIPELINE


def test_an_unreadable_endpoint_costs_that_type_and_not_the_run(tmp_path):
    """Only pipelines are served; every other route answers 404."""
    result = live_run(tmp_path)

    from discovery_agent.source_strategy import P0Artifact

    assert result.synapse.was_reachable(P0Artifact.PIPELINE)
    assert not result.synapse.was_reachable(P0Artifact.NOTEBOOK)
    # The repository leg is untouched by any of it.
    assert result.walk.file_count > 0
    assert result.extraction.results


def test_an_unreachable_endpoint_never_reports_a_count_of_zero(tmp_path):
    result = live_run(tmp_path)

    assert "notebook" not in result.synapse.counts_by_artifact()
    assert any(
        "not served by this workspace" in i.message for i in result.synapse.all_issues
    )


def test_the_three_sources_all_reach_the_record_set(tmp_path):
    from discovery_agent.extractors.models import SourceType

    result = live_run(tmp_path)

    assert result.sources_read == ("repository", "synapse", "sql")
    sources = {s for r in result.records.records for s in r.sources}
    assert sources == {SourceType.REPOSITORY, SourceType.SYNAPSE, SourceType.SQL}


# --- 8: cross-source identity at the run level -------------------------------


def test_a_workspace_that_owns_the_repository_merges_its_artifacts(tmp_path):
    result = live_run(tmp_path, transport=FakeArmTransport(REPOSITORY_URL))

    assert result.resolution is discovery.IdentityResolution.PROVEN
    pipelines = [
        r for r in result.records.records if r.artifact.value == "pipeline"
    ]
    assert len(pipelines) == 1
    assert pipelines[0].runtime is not None


def test_a_workspace_pointed_at_another_repository_does_not_merge(tmp_path):
    result = live_run(
        tmp_path, transport=FakeArmTransport("https://github.com/contoso/other")
    )

    assert result.resolution is discovery.IdentityResolution.DIFFERENT_REPOSITORY
    pipelines = [r for r in result.records.records if r.artifact.value == "pipeline"]
    assert len(pipelines) == 2


def test_a_workspace_with_no_git_integration_is_undetermined(tmp_path):
    result = live_run(tmp_path, transport=FakeArmTransport(repository_url=None))

    assert result.resolution is discovery.IdentityResolution.UNDETERMINED
    assert any("not identified with each other" in i.message for i in result.issues)


def test_an_unreadable_workspace_does_not_become_a_mismatch(tmp_path):
    """ARM refusing is not an answer about which repository the workspace has."""
    result = live_run(tmp_path, transport=FakeArmTransport(status_code=403))

    assert result.resolution is discovery.IdentityResolution.UNDETERMINED
    assert result.resolution is not discovery.IdentityResolution.DIFFERENT_REPOSITORY


def test_identical_definitions_in_both_sources_are_in_sync(tmp_path):
    from discovery_agent.discovery_models import DriftStatus

    result = live_run(tmp_path, transport=FakeArmTransport(REPOSITORY_URL))

    pipeline = next(
        r for r in result.records.records if r.artifact.value == "pipeline"
    )
    assert pipeline.runtime.drift is DriftStatus.IN_SYNC


def test_a_workspace_definition_edited_in_place_drifts(tmp_path):
    from discovery_agent.discovery_models import DriftStatus

    edited = dict(PIPELINE_JSON["properties"])
    edited["description"] = "published by hand"
    result = live_run(
        tmp_path,
        resources=workspace_resources(pipelines=[live_pipeline(properties=edited)]),
        transport=FakeArmTransport(REPOSITORY_URL),
    )

    pipeline = next(
        r for r in result.records.records if r.artifact.value == "pipeline"
    )
    assert pipeline.runtime.drift is DriftStatus.DRIFTED
    assert pipeline.runtime.drift_paths == ("description",)


# --- 9: P0 coverage and degradation ------------------------------------------


def test_every_p0_artifact_appears_in_coverage_found_or_not(tmp_path):
    from discovery_agent.source_strategy import P0Artifact

    result = live_run(tmp_path)

    found = result.p0_coverage()
    assert set(found) == {a.value for a in P0Artifact}
    assert found["pipeline"]["records"] >= 1
    assert found["dedicated_sql_table"]["records"] == 1
    # This repository fixture has neither, and zero is the honest answer.
    assert found["sqlscript"]["records"] == 0
    assert found["sparkJobDefinition"]["records"] == 0


def test_the_sql_leg_failing_costs_sql_and_nothing_else(tmp_path):
    """A refused pool login must not end a run that has two other sources."""

    class RefusingConnector(FakeConnector):
        def connect(self):
            from discovery_agent.errors import SqlConnectionError

            raise SqlConnectionError("login failed for user 'discovery'")

    result = live_run(tmp_path, connector=RefusingConnector())

    assert result.catalog is None
    assert result.synapse.counts_by_artifact()["pipeline"] == 1
    assert result.extraction.results
    assert any(
        "no claim is made about how many exist" in i.message for i in result.issues
    )


def test_the_git_leg_failing_costs_git_and_nothing_else(tmp_path):
    class FailingGit:
        def __call__(self, args, **kwargs):
            from discovery_agent.errors import GitCommandError

            if args[:1] == ("--version",):
                return "git version 2.45.1"
            raise GitCommandError(args, 128, "repository not found")

    result, _ = connected_run(
        tmp_path,
        git=FailingGit(),
        settings=full_settings(),
        connector=FakeConnector(),
        artifacts_client=FakeArtifactsClient(workspace_resources()),
    )

    assert result.snapshot is None
    assert result.walk.file_count == 0
    assert result.synapse.counts_by_artifact()["pipeline"] == 1
    assert result.catalog is not None
    assert any("could not be acquired" in i.message for i in result.issues)


def test_git_failing_is_still_fatal_when_git_is_the_only_source(tmp_path):
    """Nothing left to report is a failure, not an empty success."""

    class FailingGit:
        def __call__(self, args, **kwargs):
            from discovery_agent.errors import GitCommandError

            if args[:1] == ("--version",):
                return "git version 2.45.1"
            raise GitCommandError(args, 128, "repository not found")

    with pytest.raises(GitCommandError):
        connected_run(tmp_path, git=FailingGit())


def test_git_unavailable_still_allows_the_live_workspace_to_be_read(tmp_path):
    """Requirement: one source failing is not the whole discovery failing."""
    connections = ConnectionManager(
        ConnectionSettings(
            azure=AzureConnectionConfig(subscription_id=SUBSCRIPTION),
            synapse=SynapseConnectionConfig(
                resource_group="rg", workspace_name="ws"
            ),
        ),
        credential=credential(),
        artifacts_client=FakeArtifactsClient(workspace_resources()),
        transport=FakeArmTransport(),
    )

    result = discovery.run(
        config_for(tmp_path, tmp_path / "out"),
        connections=connections,
        scan_repository=False,
    )

    assert result.snapshot is None
    assert result.walk.file_count == 0
    assert result.synapse.counts_by_artifact()["pipeline"] == 1
    assert any("no repository was scanned" in i.message for i in result.issues)
    assert len(result.records.records) == 1


def test_a_run_over_the_same_sources_twice_is_deterministic(tmp_path):
    first = live_run(tmp_path / "a", transport=FakeArmTransport(REPOSITORY_URL))
    second = live_run(tmp_path / "b", transport=FakeArmTransport(REPOSITORY_URL))

    assert first.records.summary() == second.records.summary()
    assert first.p0_coverage() == second.p0_coverage()


# --- 10: the CLI's live flags -------------------------------------------------


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--workspace", "ws"], "--workspace needs --resource-group"),
        (
            ["--source", "repo", "--resource-group", "rg"],
            "--resource-group applies to --workspace",
        ),
        (["--sql-pool", "p", "--source", "repo"], "--sql-pool needs --workspace"),
    ],
)
def test_incomplete_live_arguments_are_refused(argv, expected, capsys):
    from discovery_agent import cli

    assert cli.main(argv) == 2
    assert expected in capsys.readouterr().err


def test_a_workspace_run_needs_no_repository(capsys):
    from discovery_agent import cli

    args = cli.build_parser().parse_args(["--workspace", "ws", "--resource-group", "rg"])
    connections = cli.connections_for(args)

    assert connections.settings.synapse.workspace_name == "ws"
    assert connections.settings.git is None
    assert connections.settings.azure is None


def test_the_subscription_is_optional_and_only_enables_the_management_plane():
    from discovery_agent import cli

    without = cli.connections_for(
        cli.build_parser().parse_args(["--workspace", "ws", "--resource-group", "rg"])
    )
    with_it = cli.connections_for(
        cli.build_parser().parse_args(
            ["--workspace", "ws", "--resource-group", "rg", "--subscription", SUBSCRIPTION]
        )
    )

    assert without.settings.azure is None
    assert with_it.settings.azure.subscription_id == SUBSCRIPTION


def test_the_sql_pool_flag_reaches_the_synapse_configuration():
    from discovery_agent import cli

    connections = cli.connections_for(
        cli.build_parser().parse_args(
            ["--workspace", "ws", "--resource-group", "rg", "--sql-pool", POOL]
        )
    )

    assert connections.settings.synapse.sql_pool_name == POOL
    assert connections.settings.synapse.resolved_database == POOL


def test_the_cli_accepts_no_credential_flag_of_any_kind():
    """Not "we ignore one" -- there is no option to supply one."""
    from discovery_agent import cli

    options = {
        option
        for action in cli.build_parser()._actions
        for option in action.option_strings
    }
    for forbidden in (
        "--token",
        "--password",
        "--pat",
        "--secret",
        "--client-secret",
        "--access-token",
        "--connection-string",
    ):
        assert forbidden not in options


def test_the_cli_never_asks_which_artifacts_exist():
    """The whole point of discovery is that it enumerates them."""
    from discovery_agent import cli

    options = {
        option
        for action in cli.build_parser()._actions
        for option in action.option_strings
    }
    for forbidden in ("--pipeline", "--notebook", "--table", "--dataset", "--artifacts"):
        assert forbidden not in options


def test_the_local_report_lists_every_p0_artifact(tmp_path, capsys):
    from discovery_agent import cli
    from discovery_agent.source_strategy import P0Artifact

    root = make_repository(tmp_path / "repo")
    cli.main(["--source", str(root), "--out", str(tmp_path / "out")])
    printed = capsys.readouterr().out

    assert "P0 coverage" in printed
    for artifact in P0Artifact:
        assert artifact.value in printed
