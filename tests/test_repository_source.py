"""Synapse definitions from a Git repository or a ZIP of one, for one environment.

Offline: the repository is a folder written by the test, and Git is a fake that
hands that folder back. What this cannot establish is that a real ``git`` clones
a real remote; the acquisition layer has its own tests for that.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from discovery_agent.acquisition.models import RepositorySource
from discovery_agent.api import migration as migration_module
from discovery_agent.api import repository_source as rs
from discovery_agent.api import service as service_module
from discovery_agent.api.service import ApiError, Session
from discovery_agent.source_strategy import P0Artifact

DEV_CS = "Server=tcp:dev-ws.sql.azuresynapse.net,1433;Database=devpool;"
PROD_CS = "Server=tcp:prod-ws.sql.azuresynapse.net,1433;Database=prodpool;"

FILES = {
    "linkedService/ls_sql.json": {"name": "ls_sql", "properties": {"type": "AzureSqlDW", "typeProperties": {"connectionString": DEV_CS}}},
    "linkedService/ls_adls.json": {"name": "ls_adls", "properties": {"type": "AzureBlobFS", "typeProperties": {"url": "https://devlake.dfs.core.windows.net/"}}},
    "pipeline/PL_Load.json": {"name": "PL_Load", "properties": {"activities": [{"name": "Wait1", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}}]}},
    "trigger/TR_Daily.json": {"name": "TR_Daily", "properties": {
        "type": "ScheduleTrigger", "typeProperties": {"recurrence": {"frequency": "Day", "interval": 1}},
        "pipelines": [{"pipelineReference": {"referenceName": "PL_Load", "type": "PipelineReference"}}]}},
    "integrationRuntime/AutoResolveIntegrationRuntime.json": {"name": "AutoResolveIntegrationRuntime", "properties": {
        "type": "Managed", "typeProperties": {"computeProperties": {"location": "AutoResolve"}}}},
}
PROD_PARAMETERS = json.dumps({"$schema": "x", "contentVersion": "1.0.0.0", "parameters": {
    "workspaceName": {"value": "prod-ws"},
    "ls_sql_connectionString": {"value": PROD_CS},
    "ls_adls_properties_typeProperties_url": {"value": "https://prodlake.dfs.core.windows.net/"},
    "ls_kv_secret": {"reference": {"keyVault": {"id": "/x"}, "secretName": "s"}},
}})


def write_repo(root: Path, prefix: str = "") -> Path:
    for rel, doc in FILES.items():
        target = root / prefix / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc), encoding="utf-8")
    return root / prefix


def zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, doc in files.items():
            z.writestr(name, json.dumps(doc) if isinstance(doc, dict) else doc)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(rs, "WORK_ROOT", tmp_path / "environments")


# ---- the environment's parameters --------------------------------------------------------------


def test_parameters_are_read_from_an_arm_file_or_a_flat_object_and_references_are_skipped():
    values = rs.parse_parameters(PROD_PARAMETERS)
    assert values == {"workspaceName": "prod-ws", "ls_sql_connectionString": PROD_CS,
                      "ls_adls_properties_typeProperties_url": "https://prodlake.dfs.core.windows.net/"}
    assert rs.parse_parameters('{"ls_sql_connectionString": "x"}') == {"ls_sql_connectionString": "x"}
    with pytest.raises(rs.RepositoryError, match="not valid JSON"):
        rs.parse_parameters("{nope")
    with pytest.raises(rs.RepositoryError, match="no parameter values"):
        rs.parse_parameters('{"parameters": {"kv": {"reference": {}}}}')


def test_parameters_go_into_a_working_copy_and_the_source_is_untouched(tmp_path):
    repo = write_repo(tmp_path / "repo", "synapse")
    copy, applied, unmatched = rs.prepare(repo, rs.parse_parameters(PROD_PARAMETERS))
    assert applied == ["ls_adls_properties_typeProperties_url", "ls_sql_connectionString"]
    assert unmatched == ["workspaceName"]
    ls = json.loads((copy / "linkedService/ls_sql.json").read_text())
    assert ls["properties"]["typeProperties"]["connectionString"] == PROD_CS
    assert json.loads((repo / "linkedService/ls_sql.json").read_text())["properties"]["typeProperties"]["connectionString"] == DEV_CS
    rs.discard(copy)
    assert not copy.exists() and repo.exists()


def test_a_parameter_for_a_property_that_does_not_exist_is_reported_not_invented(tmp_path):
    repo = write_repo(tmp_path / "repo")
    _, applied, unmatched = rs.prepare(repo, {"ls_adls_properties_typeProperties_accountKey": "k", "ls_sql_x": "y"})
    assert applied == [] and unmatched == ["ls_adls_properties_typeProperties_accountKey", "ls_sql_x"]


def test_discard_never_removes_anything_outside_its_own_folder(tmp_path):
    outside = write_repo(tmp_path / "keep")
    rs.discard(outside)
    assert outside.exists()


# ---- the ZIP and the root folder ---------------------------------------------------------------


def test_a_zip_is_extracted_and_its_synapse_folder_found_under_a_wrapping_folder():
    data = zip_bytes({f"repo-main/synapse/{k}": v for k, v in FILES.items()} | {"repo-main/README.md": "hi"})
    upload_id, folder = rs.extract_zip(data)
    root, relative = rs.find_root(folder)
    assert relative == "repo-main/synapse" and rs.counts(root)["linkedService"] == 2
    assert rs.upload_folder(upload_id) == folder


def test_a_zip_with_a_path_outside_its_folder_is_refused():
    with pytest.raises(rs.RepositoryError, match="outside its own folder"):
        rs.extract_zip(zip_bytes({"../evil.json": {"name": "x"}}))
    with pytest.raises(rs.RepositoryError, match="not a valid ZIP"):
        rs.extract_zip(b"not a zip")


def test_the_root_folder_must_hold_synapse_folders(tmp_path):
    repo = write_repo(tmp_path / "repo", "synapse")
    assert rs.find_root(tmp_path / "repo", "synapse")[1] == "synapse"
    with pytest.raises(rs.RepositoryError, match="There is no folder"):
        rs.find_root(tmp_path / "repo", "workspace")
    with pytest.raises(rs.RepositoryError, match="inside the repository"):
        rs.find_root(tmp_path / "repo", "../elsewhere")
    (tmp_path / "empty").mkdir()
    with pytest.raises(rs.RepositoryError, match="No Synapse definitions"):
        rs.find_root(tmp_path / "empty")
    assert repo.exists()


def test_an_unknown_upload_id_is_refused():
    with pytest.raises(rs.RepositoryError, match="Upload the ZIP again"):
        rs.upload_folder("../../etc")


# ---- the session: ZIP and Git, end to end ------------------------------------------------------


def wait_for_discovery(session: Session) -> dict:
    for _ in range(200):
        status = session.discovery_status()
        if status["state"] not in ("running", "idle"):
            return status
        time.sleep(0.05)
    raise AssertionError("discovery did not finish")


def test_a_zip_for_the_prod_environment_is_discovered_and_migrates_with_prod_values():
    session = Session()
    upload = session.upload_zip(zip_bytes({f"repo-main/synapse/{k}": v for k, v in FILES.items()}), "contoso-synapse.zip")
    assert upload["rootFolder"] == "repo-main/synapse" and upload["counts"]["pipeline"] == 1
    state = session.connect_repository({"kind": "zip", "uploadId": upload["uploadId"], "fileName": upload["fileName"],
                                        "environment": "Prod", "parameters": PROD_PARAMETERS, "parametersName": "prod.json"})
    assert state["ok"] and state["status"] == "connected" and state["sourceKind"] == "zip"
    assert state["workspace"] == "contoso-synapse" and state["sqlPool"] is None
    repo = state["repository"]
    assert (repo["environment"], repo["rootFolder"], repo["parametersName"]) == ("Prod", "repo-main/synapse", "prod.json")
    assert repo["applied"] == ["ls_adls_properties_typeProperties_url", "ls_sql_connectionString"]
    assert [c["name"] for c in state["checks"]] == ["ZIP export", "Synapse definitions", "Prod parameters"]

    session.start_discovery()
    assert wait_for_discovery(session)["state"].startswith("completed")
    job, pool = session.migration_snapshot()
    assert pool is None
    types = {i["type"] for i in job.items}
    assert {"Pipeline", "Linked Service", "Trigger", "Integration Runtime"} <= types
    # migration reads the environment's values, not the dev ones committed
    ls = job.repository_artifacts[P0Artifact.LINKED_SERVICE]["ls_sql"]
    assert ls["properties"]["typeProperties"]["connectionString"] == PROD_CS
    assert migration_module._artifacts(job)[P0Artifact.PIPELINE]["PL_Load"]["name"] == "PL_Load"
    trigger = next(i for i in job.items if i["type"] == "Trigger")
    assert any(e["source"] == trigger["id"] for e in job.graph["edges"])
    # no pool: the data stage has nothing to read
    assert session.source_endpoint() is None and session.source_sql_factory() is None

    # disconnecting drops the working copy
    copy = Path(session._connection.repository.path)
    assert copy.exists()
    session.disconnect()
    assert not copy.exists()


class FakeGit:
    def __init__(self, folder: Path, ok: bool = True):
        self.folder, self.ok = folder, ok

    def __call__(self, config):
        self.config = config
        return self

    def validate(self):
        return SimpleNamespace(ok=self.ok, status=SimpleNamespace(value="ok" if self.ok else "failed"),
                               message="reachable" if self.ok else "Repository not found", category=None)

    def acquire(self):
        return RepositorySource(provider="github", repository_url=self.config.repository_url, ref=self.config.ref or "main",
                                local_path=self.folder, commit_sha="abc123")


def test_a_git_branch_for_an_environment_is_cloned_and_its_root_folder_used(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    write_repo(clone, "synapse")
    fake = FakeGit(clone)
    monkeypatch.setattr(service_module, "GitConnection", fake)
    session = Session()
    state = session.connect_repository({"kind": "git", "repositoryUrl": "https://github.com/contoso/synapse-ws.git",
                                        "ref": "release/test", "rootFolder": "synapse", "environment": "Test"})
    assert state["ok"] and state["workspace"] == "synapse-ws"
    repo = state["repository"]
    assert (repo["kind"], repo["ref"], repo["commit"], repo["rootFolder"]) == ("git", "release/test", "abc123", "synapse")
    assert fake.config.ref == "release/test"
    assert state["checks"][-1] == {"name": "Test parameters", "status": "skipped", "category": None,
                                   "message": "No parameters file: linked services keep the values committed in the repository"}


def test_an_unreachable_repository_fails_with_the_reason_and_leaves_nothing_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "GitConnection", FakeGit(tmp_path, ok=False))
    session = Session()
    state = session.connect_repository({"kind": "git", "repositoryUrl": "https://github.com/contoso/missing.git", "environment": "Dev"})
    assert not state["ok"] and state["status"] == "disconnected" and state["error"]["title"] == "Repository not reachable"
    with pytest.raises(ApiError, match="Connect to a Synapse workspace"):
        session.start_discovery()


def test_requests_are_checked_before_anything_runs():
    session = Session()
    with pytest.raises(ApiError, match="Unexpected field"):
        session.connect_repository({"kind": "zip", "token": "ghp_secret"})
    with pytest.raises(ApiError, match="Choose a Git repository or a ZIP"):
        session.connect_repository({"kind": "ftp", "environment": "Dev"})
    with pytest.raises(ApiError, match="Choose the environment"):
        session.connect_repository({"kind": "zip", "uploadId": "x"})
    with pytest.raises(ApiError, match="choose its resource group, workspace and pool"):
        session.connect_repository({"kind": "zip", "environment": "Dev", "sqlPool": "pool01"})
    # adding the pool needs an Azure sign-in first
    with pytest.raises(ApiError, match="Sign in to Azure first"):
        session.connect_repository({"kind": "zip", "environment": "Dev", "resourceGroup": "rg", "workspace": "ws", "sqlPool": "pool01"})
    with pytest.raises(ApiError, match="Upload the ZIP again"):
        session.connect_repository({"kind": "zip", "environment": "Dev", "uploadId": "0" * 32})


def test_health_offers_the_three_sources():
    assert Session.health()["capabilities"]["sourceKinds"] == ["workspace", "git", "zip"]


# ---- over HTTP -----------------------------------------------------------------------------------


def test_the_upload_route_takes_only_a_zip_and_the_repository_route_only_json():
    import threading
    from http.client import HTTPConnection
    from discovery_agent.api.server import create_server

    httpd = create_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def post(path, body, content_type):
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", path, body=body, headers={"Content-Type": content_type})
        response = conn.getresponse()
        data = json.loads(response.read() or b"null")
        conn.close()
        return response.status, data

    try:
        status, body = post("/api/connections/upload?name=ws.zip", b'{"a": 1}', "application/json")
        assert status == 415 and body["error"]["code"] == "unsupported_media_type"
        status, body = post("/api/connections/upload?name=ws.zip", zip_bytes(FILES), "application/zip")
        assert status == 200 and body["fileName"] == "ws.zip" and body["rootFolder"] == "" and len(body["uploadId"]) == 32
        status, body = post("/api/connections/repository", zip_bytes(FILES), "application/zip")
        assert status == 415
        status, body = post("/api/connections/repository", json.dumps({"kind": "zip", "password": "x"}).encode(), "application/json")
        assert status == 400 and "Unexpected field(s): password" in body["error"]["message"]
    finally:
        httpd.shutdown()
        httpd.server_close()
