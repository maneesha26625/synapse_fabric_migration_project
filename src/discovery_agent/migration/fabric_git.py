"""Fabric Git integration for the workspace the migration creates its items in.

Fabric keeps a workspace in step with one branch of a Git repository (Azure DevOps
or GitHub). That connection is made once, in Fabric (Workspace settings, Git
integration). After it, this module reads where the workspace is connected and
what changed, and commits the migrated items to that branch, so they can be
reviewed in a pull request before they go any further.

Everything goes through the Fabric REST API. Nothing here runs git or holds a Git
credential: Fabric signs in to the Git provider as the signed-in user, with their
own Git sign-in (single sign-on for Azure DevOps).

    GET   /v1/workspaces/{id}/git/connection         where the workspace is connected
    GET   /v1/workspaces/{id}/git/myGitCredentials   whether the caller's Git sign-in is set up
    PATCH /v1/workspaces/{id}/git/myGitCredentials   {"source": "Automatic"}: Azure DevOps single sign-on
    GET   /v1/workspaces/{id}/git/status             what changed on each side (may answer 202)
    POST  /v1/workspaces/{id}/git/commitToGit        commit (a long-running operation)

The commit is selective: it takes the items a run created or updated and leaves out
the tool's own one-off plumbing, the per-wave load pipelines and the sync pipeline,
which belong to each environment's data load rather than to the reviewed solution.
Anything else waiting in the workspace (an edit made by hand, an item from outside
the migration) is left for a person and listed, never swept into the migration's
commit. It never commits a deletion, and it never commits over changes the branch
has that the workspace does not: those need a person.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.migration.fabric_rest import FabricApiError

#: Pipelines the data stage creates for loading and syncing rows. They are environment
#: plumbing, recreated by each environment's data load, so they are never committed.
_PLUMBING = (re.compile(r"^load_.+_wave_\d+$"), re.compile(r"^sync_.+$"))
#: Fabric's limit on a commit comment.
MAX_COMMENT = 300
#: The branch a pull request from the connected branch targets by default.
DEFAULT_TARGET_BRANCH = "main"


KNOWN_NOT_CONNECTED = ("The Fabric workspace is not connected to Git. In Fabric, open Workspace settings, Git integration, "
                       "and connect it to your repository's dev branch; then commit again.")


class GitNotReady(Exception):
    """The workspace cannot be committed right now, with the reason a person can act on."""


@dataclass(frozen=True)
class GitConnection:
    """Where a workspace is connected. Holds no credential: Fabric reports none."""

    state: str  # NotConnected | Connected | ConnectedAndInitialized
    provider: Optional[str] = None  # AzureDevOps | GitHub
    organization: Optional[str] = None  # Azure DevOps organization, or GitHub owner
    project: Optional[str] = None  # Azure DevOps only
    repository: Optional[str] = None
    branch: Optional[str] = None
    folder: Optional[str] = None
    head: Optional[str] = None
    last_sync: Optional[str] = None
    custom_domain: Optional[str] = None  # GitHub Enterprise

    @property
    def connected(self) -> bool:
        return self.state in ("Connected", "ConnectedAndInitialized") and bool(self.repository and self.branch)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "organization": self.organization, "project": self.project,
            "repository": self.repository, "branch": self.branch, "folder": self.folder,
            "head": self.head, "lastSync": self.last_sync,
        }


@dataclass
class GitStatus:
    """The changes on each side since the workspace was last in step with the branch."""

    workspace_head: Optional[str]
    remote_head: Optional[str]
    changes: List[dict] = field(default_factory=list)

    @property
    def outgoing(self) -> List[dict]:
        """Items added or changed in the workspace and not yet in the branch."""
        return [c for c in self.changes if c.get("workspaceChange") in ("Added", "Modified") and c.get("conflictType") != "Conflict"]

    @property
    def incoming(self) -> List[dict]:
        """Items the branch has that the workspace does not have yet."""
        return [c for c in self.changes if c.get("remoteChange")]

    @property
    def conflicts(self) -> List[dict]:
        return [c for c in self.changes if c.get("conflictType") == "Conflict"]

    def counts(self) -> dict:
        return {"uncommitted": len([c for c in self.changes if c.get("workspaceChange")]),
                "incoming": len(self.incoming), "conflicts": len(self.conflicts)}


def _text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def parse_connection(payload: Mapping[str, Any]) -> GitConnection:
    """A ``git/connection`` answer as a GitConnection. A workspace never connected reads as NotConnected."""
    payload = payload if isinstance(payload, Mapping) else {}
    details = payload.get("gitProviderDetails") or {}
    sync = payload.get("gitSyncDetails") or {}
    provider = _text(details.get("gitProviderType"))
    return GitConnection(
        state=_text(payload.get("gitConnectionState")) or ("Connected" if details else "NotConnected"),
        provider=provider,
        organization=_text(details.get("organizationName") or details.get("ownerName")),
        project=_text(details.get("projectName")),
        repository=_text(details.get("repositoryName")),
        branch=_text(details.get("branchName")),
        folder=(_text(details.get("directoryName")) or "").strip("/") or None,
        head=_text(sync.get("head")),
        last_sync=_text(sync.get("lastSyncTime")),
        custom_domain=_text(details.get("customDomainName")),
    )


def parse_status(payload: Mapping[str, Any]) -> GitStatus:
    payload = payload if isinstance(payload, Mapping) else {}
    changes = [c for c in (payload.get("changes") or []) if isinstance(c, Mapping)]
    return GitStatus(_text(payload.get("workspaceHead")), _text(payload.get("remoteCommitHash")), [dict(c) for c in changes])


def _name(change: Mapping[str, Any]) -> str:
    return str(((change.get("itemMetadata") or {}).get("displayName")) or "")


def _type(change: Mapping[str, Any]) -> str:
    return str(((change.get("itemMetadata") or {}).get("itemType")) or "")


def is_plumbing(change: Mapping[str, Any]) -> bool:
    """A load or sync pipeline the data stage made: environment plumbing, never committed."""
    return _type(change) == "DataPipeline" and any(p.match(_name(change)) for p in _PLUMBING)


def _identifier(change: Mapping[str, Any]) -> dict:
    ident = (change.get("itemMetadata") or {}).get("itemIdentifier") or {}
    return {k: ident[k] for k in ("objectId", "logicalId") if ident.get(k)}


def _quote(part: Optional[str]) -> str:
    return urllib.parse.quote(part or "", safe="")


def repository_url(conn: GitConnection) -> Optional[str]:
    """The repository's web address, built from what Fabric reports. None when it cannot be built."""
    if not (conn.organization and conn.repository):
        return None
    if conn.provider == "AzureDevOps":
        if not conn.project:
            return None
        return f"https://dev.azure.com/{_quote(conn.organization)}/{_quote(conn.project)}/_git/{_quote(conn.repository)}"
    if conn.provider == "GitHub":
        host = f"https://{conn.custom_domain}" if conn.custom_domain else "https://github.com"
        return f"{host}/{_quote(conn.organization)}/{_quote(conn.repository)}"
    return None


def commit_url(conn: GitConnection, sha: Optional[str]) -> Optional[str]:
    base = repository_url(conn)
    if not (base and sha):
        return None
    return f"{base}/commit/{sha}"


def pull_request_url(conn: GitConnection, target: str = DEFAULT_TARGET_BRANCH) -> Optional[str]:
    """The page that opens a pull request from the connected branch into ``target``.

    None when the workspace is connected straight to the target branch: there is
    nothing to merge, the commit already landed there."""
    base = repository_url(conn)
    if not (base and conn.branch) or conn.branch == target:
        return None
    if conn.provider == "AzureDevOps":
        query = urllib.parse.urlencode({"sourceRef": conn.branch, "targetRef": target})
        return f"{base}/pullrequestcreate?{query}"
    return f"{base}/compare/{_quote(target)}...{_quote(conn.branch)}?expand=1"


def comment_for(text: str) -> str:
    """A commit comment Fabric accepts: one line, at most 300 characters."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= MAX_COMMENT else line[: MAX_COMMENT - 1] + "…"


# -- calls -------------------------------------------------------------------------------


def connection(client: Any, workspace_id: str) -> GitConnection:
    try:
        return parse_connection(client.get(f"/workspaces/{workspace_id}/git/connection"))
    except FabricApiError as exc:
        if exc.code == "WorkspaceNotConnectedToGit":
            return GitConnection(state="NotConnected")
        raise


def ensure_credentials(client: Any, workspace_id: str, conn: GitConnection) -> None:
    """Make sure the caller's Git sign-in is set up for this workspace.

    Fabric commits as the caller, so the caller needs Git credentials configured.
    For Azure DevOps that is single sign-on with the same Microsoft account
    ("Automatic"), set here when it is missing. GitHub needs a token the person
    saves in Fabric themselves; this tool never handles one, so it says how."""
    current = client.get(f"/workspaces/{workspace_id}/git/myGitCredentials")
    source = str((current or {}).get("source") or "None")
    if source != "None":
        return
    if conn.provider == "AzureDevOps":
        client.patch(f"/workspaces/{workspace_id}/git/myGitCredentials", {"source": "Automatic"})
        return
    raise GitNotReady(
        "Your Git sign-in for this workspace is not set up. In Fabric, open the workspace's Source control panel, "
        "choose the Accounts tab and connect your GitHub account, then commit again.")


def status(client: Any, workspace_id: str) -> GitStatus:
    return parse_status(client.get_lro(f"/workspaces/{workspace_id}/git/status", "reading the Git status"))


@dataclass
class CommitResult:
    state: str  # committed | nothing
    items: int = 0
    skipped: List[str] = field(default_factory=list)  # plumbing left out, by name
    commit: Optional[str] = None
    batches: int = 0
    #: Changes waiting in the workspace that the run did not make (``commit(only=...)``), left out, by name.
    others: List[str] = field(default_factory=list)


def _commit_batch(client: Any, workspace_id: str, items: Sequence[dict], comment: str) -> None:
    current = status(client, workspace_id)
    client.create(f"/workspaces/{workspace_id}/git/commitToGit",
                  {"mode": "Selective", "workspaceHead": current.workspace_head, "comment": comment, "items": list(items)},
                  what="the Git commit")


def _too_large(exc: FabricApiError) -> bool:
    text = f"{exc.code} {exc.message}".lower()
    return exc.status == 413 or "size" in text or "too large" in text or "toolarge" in text


def commit(client: Any, workspace_id: str, comment: str, *, exclude: Sequence[str] = (),
           only: Optional[Collection[Tuple[str, str]]] = None) -> CommitResult:
    """Commit what the workspace added or changed, except plumbing, to the connected branch.

    ``only`` limits the commit to those (item type, display name) pairs, the items a run created
    or updated; every other change waiting in the workspace is left out and listed in ``others``.
    Without it, everything the workspace added or changed is committed.

    Refuses, with the reason, when the branch has changes the workspace does not
    (an update in Fabric must come first) or when an item changed on both sides
    (a conflict a person resolves). A head that moved underneath is retried once,
    and a commit Fabric finds too large is split in two until it fits."""
    conn = connection(client, workspace_id)
    if not conn.connected:
        raise GitNotReady(KNOWN_NOT_CONNECTED)
    ensure_credentials(client, workspace_id, conn)
    current = status(client, workspace_id)
    if current.conflicts:
        names = ", ".join(sorted(_name(c) for c in current.conflicts)[:5])
        raise GitNotReady(f"{len(current.conflicts)} item(s) changed both in the workspace and in the branch ({names}). "
                          "Resolve them in Fabric's Source control panel, then commit again.")
    if current.incoming:
        raise GitNotReady(f"The {conn.branch} branch has {len(current.incoming)} change(s) the workspace doesn't have yet. "
                          "In Fabric, open Source control and choose Update all, then commit again.")
    wanted = {str(n).lower() for n in exclude}
    allowed = None if only is None else {(str(t).lower(), str(n).lower()) for t, n in only}

    def ours(change: Mapping[str, Any]) -> bool:
        return allowed is None or (_type(change).lower(), _name(change).lower()) in allowed

    candidates = [c for c in current.outgoing if not is_plumbing(c) and _name(c).lower() not in wanted]
    skipped = sorted(_name(c) for c in current.outgoing if is_plumbing(c) or _name(c).lower() in wanted)
    others = sorted(_name(c) for c in candidates if not ours(c))
    items = [_identifier(c) for c in candidates if ours(c) and _identifier(c)]
    if not items:
        return CommitResult("nothing", skipped=skipped, others=others)
    text = comment_for(comment)
    batches = 0
    pending: List[List[dict]] = [items]
    while pending:
        batch = pending.pop(0)
        try:
            try:
                _commit_batch(client, workspace_id, batch, text)
            except FabricApiError as exc:
                if exc.code != "WorkspaceHeadMismatch":
                    raise
                _commit_batch(client, workspace_id, batch, text)  # the head moved: read it again, once
        except FabricApiError as exc:
            if _too_large(exc) and len(batch) > 1:
                half = len(batch) // 2
                pending[:0] = [batch[:half], batch[half:]]
                continue
            raise
        batches += 1
    head = connection(client, workspace_id).head
    return CommitResult("committed", items=len(items), skipped=skipped, commit=head, batches=batches, others=others)


def describe(client: Any, workspace_id: str, target: str = DEFAULT_TARGET_BRANCH) -> Dict[str, Any]:
    """Where the workspace is connected, what is waiting to be committed, and the pull-request link."""
    conn = connection(client, workspace_id)
    if not conn.connected:
        return {"state": "not_connected", "message": KNOWN_NOT_CONNECTED}
    out: Dict[str, Any] = {"state": "connected", **conn.to_dict(),
                           "repositoryUrl": repository_url(conn), "commitUrl": commit_url(conn, conn.head),
                           "pullRequestUrl": pull_request_url(conn, target), "targetBranch": target}
    current = status(client, workspace_id)
    out.update(current.counts())
    out["plumbing"] = len([c for c in current.outgoing if is_plumbing(c)])
    return out
