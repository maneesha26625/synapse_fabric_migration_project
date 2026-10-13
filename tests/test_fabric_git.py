"""Fabric Git for the workspace the migration creates items in: status, commit, pull-request link.

Offline. The workspace is a fake REST client that answers the Fabric Git endpoints
the way the Fabric REST API documents them; what a live Fabric workspace and Azure
DevOps do with the commit is checked by hand (see the README).
"""

from __future__ import annotations

import json
import threading

import pytest

from discovery_agent.api.service import ApiError
from discovery_agent.migration import fabric_git
from discovery_agent.migration.fabric_rest import FabricApiError, FabricRestClient

from test_migration import WS, FakeRest, FakeSender, client, fake_job, service, wait_until  # noqa: E402 - shared fakes


def change(name, item_type, workspace="Added", remote=None, conflict="None", oid=None):
    """One entry of a git/status answer."""
    entry = {"itemMetadata": {"itemIdentifier": {"objectId": oid or f"id-{name}"}, "itemType": item_type, "displayName": name},
             "conflictType": conflict}
    if workspace:
        entry["workspaceChange"] = workspace
    if remote:
        entry["remoteChange"] = remote
    return entry


def migrated_changes():
    return [
        change("pool01", "Warehouse"),
        change("LoadSales", "Notebook"),
        change("pl_load", "DataPipeline", workspace="Modified"),
        change("load_pool01_wave_2", "DataPipeline"),  # data-load plumbing
        change("sync_pool01", "DataPipeline"),  # sync plumbing
        change("Retired", "Notebook", workspace="Deleted"),  # a deletion is never committed by the tool
    ]


class GitRest(FakeRest):
    """A Fabric workspace connected to a branch, with its Git status, sign-in and commits."""

    def __init__(self, connected=True, provider="AzureDevOps", branch="dev", credentials="Automatic",
                 changes=None, fail_commit=(), hold=None, **kw):
        super().__init__(warehouses=[{"id": "wh-1", "displayName": "pool01"}], **kw)
        self.connected, self.provider, self.branch, self.credentials = connected, provider, branch, credentials
        self.changes = list(migrated_changes() if changes is None else changes)
        self.head = "0" * 40
        self.fail_commit = list(fail_commit)
        self.hold = hold  # a threading.Event a commit waits on, to look at the service mid-commit
        self.commits, self.patched = [], []

    def get(self, path):
        if path.endswith("/git/connection"):
            self.calls.append(("get", path))
            if not self.connected:
                return {"gitConnectionState": "NotConnected"}
            if self.provider == "GitHub":
                details = {"gitProviderType": "GitHub", "ownerName": "contoso", "repositoryName": "fabric",
                           "branchName": self.branch, "directoryName": "/fabric"}
            else:
                details = {"gitProviderType": "AzureDevOps", "organizationName": "Contoso Data", "projectName": "Migration",
                           "repositoryName": "fabric-migration", "branchName": self.branch, "directoryName": "/fabric"}
            return {"gitConnectionState": "ConnectedAndInitialized", "gitProviderDetails": details,
                    "gitSyncDetails": {"head": self.head, "lastSyncTime": "2026-10-13T09:00:00Z"}}
        if path.endswith("/git/myGitCredentials"):
            return {"source": self.credentials}
        return super().get(path)

    def get_lro(self, path, what="the request"):
        assert path == f"/workspaces/{WS}/git/status"
        remote = next((c for c in self.changes if c.get("remoteChange")), None)
        return {"workspaceHead": self.head, "remoteCommitHash": "f" * 40 if remote else self.head,
                "changes": [json.loads(json.dumps(c)) for c in self.changes]}

    def patch(self, path, body):
        self.patched.append((path, body))
        if path.endswith("/git/myGitCredentials"):
            self.credentials = body["source"]
        return dict(body)

    def create(self, path, body, what="creating the item"):
        if not path.endswith("/git/commitToGit"):
            return super().create(path, body)
        if self.hold is not None:
            self.hold.wait(5)
        if self.fail_commit:
            raise self.fail_commit.pop(0)
        assert body["mode"] == "Selective" and body["workspaceHead"] == self.head and what == "the Git commit"
        self.commits.append(body)
        done = {i["objectId"] for i in body["items"]}
        self.changes = [c for c in self.changes if c["itemMetadata"]["itemIdentifier"]["objectId"] not in done]
        self.head = str(len(self.commits)) * 40
        return {}


# ---- reading where the workspace is connected ------------------------------------------------


def test_a_connection_is_read_for_azure_devops_and_github_and_a_never_connected_workspace():
    ado = fabric_git.connection(GitRest(), WS)
    assert ado.connected and (ado.provider, ado.organization, ado.project, ado.repository, ado.branch, ado.folder) == (
        "AzureDevOps", "Contoso Data", "Migration", "fabric-migration", "dev", "fabric")
    github = fabric_git.connection(GitRest(provider="GitHub"), WS)
    assert github.connected and (github.organization, github.repository, github.project) == ("contoso", "fabric", None)
    never = fabric_git.connection(GitRest(connected=False), WS)
    assert not never.connected and never.state == "NotConnected"
    assert not fabric_git.parse_connection({}).connected


def test_pull_request_and_commit_links_are_built_from_what_fabric_reports():
    ado = fabric_git.connection(GitRest(), WS)
    assert fabric_git.pull_request_url(ado) == (
        "https://dev.azure.com/Contoso%20Data/Migration/_git/fabric-migration/pullrequestcreate?sourceRef=dev&targetRef=main")
    assert fabric_git.commit_url(ado, "abc") == "https://dev.azure.com/Contoso%20Data/Migration/_git/fabric-migration/commit/abc"
    github = fabric_git.connection(GitRest(provider="GitHub"), WS)
    assert fabric_git.pull_request_url(github) == "https://github.com/contoso/fabric/compare/main...dev?expand=1"
    # Connected straight to main: the commit already landed there, nothing to merge.
    assert fabric_git.pull_request_url(fabric_git.connection(GitRest(branch="main"), WS)) is None
    assert fabric_git.commit_url(ado, None) is None


def test_the_description_counts_changes_and_shows_the_links():
    state = fabric_git.describe(GitRest(), WS)
    assert state["state"] == "connected" and state["branch"] == "dev" and state["folder"] == "fabric"
    assert (state["uncommitted"], state["incoming"], state["conflicts"], state["plumbing"]) == (6, 0, 0, 2)
    assert state["pullRequestUrl"].endswith("sourceRef=dev&targetRef=main") and state["targetBranch"] == "main"
    off = fabric_git.describe(GitRest(connected=False), WS)
    assert off["state"] == "not_connected" and "Git integration" in off["message"]


# ---- committing ------------------------------------------------------------------------------


def test_a_commit_takes_the_migrated_items_and_leaves_out_plumbing_and_deletions():
    rest = GitRest()
    result = fabric_git.commit(rest, WS, "Synapse to Fabric migration run 001")
    assert result.state == "committed" and result.items == 3 and result.batches == 1
    assert result.skipped == ["load_pool01_wave_2", "sync_pool01"]
    (body,) = rest.commits
    assert {i["objectId"] for i in body["items"]} == {"id-pool01", "id-LoadSales", "id-pl_load"}
    assert body["comment"] == "Synapse to Fabric migration run 001"
    assert result.commit == rest.head == "1" * 40
    # What stays uncommitted: the plumbing, and the deletion a person decides on.
    assert sorted(c["itemMetadata"]["displayName"] for c in rest.changes) == ["Retired", "load_pool01_wave_2", "sync_pool01"]


def test_only_plumbing_changed_means_nothing_to_commit():
    rest = GitRest(changes=[change("load_pool01_wave_1", "DataPipeline"), change("sync_pool01", "DataPipeline")])
    result = fabric_git.commit(rest, WS, "run")
    assert result.state == "nothing" and result.items == 0 and rest.commits == []
    assert result.skipped == ["load_pool01_wave_1", "sync_pool01"]


def test_a_long_comment_is_cut_to_what_fabric_accepts():
    assert len(fabric_git.comment_for("x" * 500)) == fabric_git.MAX_COMMENT
    assert fabric_git.comment_for("two\nlines  here") == "two lines here"


def test_azure_devops_sign_in_is_set_up_automatically_and_github_says_how():
    rest = GitRest(credentials="None")
    fabric_git.commit(rest, WS, "run")
    assert rest.patched == [(f"/workspaces/{WS}/git/myGitCredentials", {"source": "Automatic"})]
    github = GitRest(provider="GitHub", credentials="None")
    with pytest.raises(fabric_git.GitNotReady, match="Accounts tab"):
        fabric_git.commit(github, WS, "run")
    assert github.patched == [] and github.commits == []
    # Already set up: left exactly as it is.
    configured = GitRest(credentials="ConfiguredConnection")
    fabric_git.commit(configured, WS, "run")
    assert configured.patched == []


def test_a_branch_ahead_of_the_workspace_or_a_conflict_is_refused_with_the_reason():
    ahead = GitRest(changes=migrated_changes() + [change("NewInGit", "Notebook", workspace=None, remote="Added")])
    with pytest.raises(fabric_git.GitNotReady, match="Update all"):
        fabric_git.commit(ahead, WS, "run")
    clash = GitRest(changes=[change("LoadSales", "Notebook", workspace="Modified", remote="Modified", conflict="Conflict")])
    with pytest.raises(fabric_git.GitNotReady, match="Resolve them"):
        fabric_git.commit(clash, WS, "run")
    assert ahead.commits == [] and clash.commits == []


def test_a_moved_head_is_retried_once_and_a_commit_too_large_is_split():
    moved = GitRest(fail_commit=[FabricApiError(400, "WorkspaceHeadMismatch", "Head mismatch")])
    assert fabric_git.commit(moved, WS, "run").state == "committed" and len(moved.commits) == 1
    large = GitRest(fail_commit=[FabricApiError(400, "CommitSizeExceeded", "The total size of the commit is too large")])
    result = fabric_git.commit(large, WS, "run")
    assert result.state == "committed" and result.batches == 2 and len(large.commits) == 2
    assert sum(len(b["items"]) for b in large.commits) == 3
    other = GitRest(fail_commit=[FabricApiError(403, "InsufficientPrivileges", "No")])
    with pytest.raises(FabricApiError):
        fabric_git.commit(other, WS, "run")


def test_a_workspace_not_connected_to_git_is_never_committed():
    rest = GitRest(connected=False)
    with pytest.raises(fabric_git.GitNotReady, match="not connected to Git"):
        fabric_git.commit(rest, WS, "run")
    assert rest.commits == []


# ---- the REST client underneath ---------------------------------------------------------------


def test_a_status_answered_with_202_is_followed_to_its_result():
    op = "https://api.fabric.microsoft.com/v1/operations/op-9"
    sender = FakeSender([(202, {"location": op, "retry-after": "1"}, {}), (200, {}, {"status": "Succeeded"}),
                         (200, {}, {"workspaceHead": "h", "remoteCommitHash": "h", "changes": []})])
    status = fabric_git.status(client(sender), WS)
    assert status.workspace_head == "h" and status.changes == []
    assert sender.requests[0][1].endswith(f"/workspaces/{WS}/git/status") and sender.requests[-1][1] == op + "/result"


def test_git_error_codes_are_explained_plainly():
    sender = FakeSender([(400, {}, {"errorCode": "WorkspaceNotConnectedToGit", "message": "raw"})])
    with pytest.raises(FabricApiError) as raised:
        client(sender).get(f"/workspaces/{WS}/git/status")
    assert "not connected to Git" in raised.value.message and "(Fabric: WorkspaceNotConnectedToGit)" in raised.value.message


def test_a_commit_that_outlasts_its_wait_says_what_it_was_waiting_for():
    op = "https://api.fabric.microsoft.com/v1/operations/op-1"
    ticks = iter([0, 10_000])
    slow = FabricRestClient(lambda: "tok", send=FakeSender([(202, {"location": op}, {}), (200, {}, {"status": "Running"})]),
                            sleep=lambda s: None, clock=lambda: next(ticks))
    with pytest.raises(FabricApiError, match="did not finish the Git commit in time"):
        slow.create(f"/workspaces/{WS}/git/commitToGit", {"mode": "All"}, what="the Git commit")


# ---- after a run, and by hand ----------------------------------------------------------------


NOTEBOOK_PLAN = {"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}], "options": {"stages": ["notebooks"]}}


def test_a_completed_run_commits_to_fabric_git_and_offers_the_pull_request():
    rest = GitRest()
    svc = service(fake_job(), rest=rest)
    svc.start(NOTEBOOK_PLAN)
    assert wait_until(lambda: svc.state()["state"] == "completed" and (svc.state()["git"] or {}).get("state") == "committed")
    git = svc.state()["git"]
    assert git["items"] == 3 and git["branch"] == "dev" and git["commit"] == "1" * 40
    assert git["pullRequestUrl"].endswith("pullrequestcreate?sourceRef=dev&targetRef=main")
    assert git["commitUrl"].endswith("/commit/" + "1" * 40)
    assert "Left out 2 data-load pipeline(s)" in git["message"]
    assert svc.state()["options"]["gitCommit"] == "auto"
    assert any("GIT" in line for line in svc.state()["logs"])


def test_the_run_is_free_while_its_commit_finishes():
    hold = threading.Event()
    rest = GitRest(hold=hold)
    svc = service(fake_job(), rest=rest)
    svc.start(NOTEBOOK_PLAN)
    assert wait_until(lambda: (svc.state()["git"] or {}).get("state") == "committing")
    # Completed, and not busy: Retry or Reset are not held up by the commit.
    assert wait_until(lambda: not svc._busy())  # noqa: SLF001
    assert svc.state()["state"] == "completed" and svc.git_state()["committing"]
    hold.set()
    assert wait_until(lambda: svc.state()["git"]["state"] == "committed")


def test_switching_the_commit_off_leaves_the_branch_alone():
    rest = GitRest()
    svc = service(fake_job(), rest=rest)
    svc.start({**NOTEBOOK_PLAN, "options": {"stages": ["notebooks"], "gitCommit": "off"}})
    assert wait_until(lambda: svc.state()["state"] == "completed" and not svc._busy())  # noqa: SLF001
    assert svc.state()["git"] is None and rest.commits == []


def test_a_commit_problem_is_a_note_and_never_fails_the_run():
    rest = GitRest(fail_commit=[FabricApiError(403, "InsufficientPrivileges", "The signed-in account needs Contributor")])
    svc = service(fake_job(), rest=rest)
    svc.start(NOTEBOOK_PLAN)
    assert wait_until(lambda: (svc.state()["git"] or {}).get("state") == "failed")
    state = svc.state()
    assert state["state"] == "completed" and state["failed"] == 0 and state["completed"] == 1
    assert "Contributor" in state["git"]["message"]
    blocked = GitRest(connected=False)
    svc = service(fake_job(), rest=blocked)
    svc.start(NOTEBOOK_PLAN)
    assert wait_until(lambda: (svc.state()["git"] or {}).get("state") == "skipped")


def test_the_git_state_and_a_commit_by_hand():
    rest = GitRest()
    svc = service(fake_job(), rest=rest)
    state = svc.git_state()
    assert state["state"] == "connected" and state["workspace"] == "Sales WS" and state["uncommitted"] == 6
    assert not state["committing"] and state["lastCommit"] is None
    svc.git_commit({"comment": "Reviewed fixes"})
    assert wait_until(lambda: (svc.git_state()["lastCommit"] or {}).get("state") == "committed")
    assert rest.commits[0]["comment"] == "Reviewed fixes"
    with pytest.raises(ApiError) as refused:
        svc.git_commit({"token": "s3cr3t-value"})
    assert refused.value.code == "invalid_request" and "s3cr3t-value" not in refused.value.message


def test_without_a_fabric_target_the_git_state_says_so():
    assert service(fake_job(), ready=False).git_state()["state"] == "no_target"
