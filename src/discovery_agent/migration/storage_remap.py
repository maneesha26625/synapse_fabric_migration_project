"""Route code that reads Azure storage directly through a OneLake shortcut instead.

In Synapse, notebooks, Spark jobs and SQL read ``abfss://container@account.dfs.core.windows.net/...`` as the
workspace's managed identity. In Fabric they run as the user, and that often cannot work at all: when the storage
account is in another Microsoft Entra tenant than Fabric, the storage refuses every token Fabric can present (401),
whatever roles are granted. A Fabric *connection* can still reach it (an account key or SAS works across tenants), and
a OneLake shortcut uses the connection. So each mapped ``(account, container)`` gets a shortcut named after the
container under ``Files`` in a Lakehouse, and the code's paths are rewritten to go through it:

    abfss://migrationcontainer@acct.dfs.core.windows.net/x  ->  abfss://<ws>@onelake.dfs.fabric.microsoft.com/<lh>/Files/migrationcontainer/x
    https://acct.dfs.core.windows.net/migrationcontainer/x  ->  https://onelake.dfs.fabric.microsoft.com/<ws>/<lh>/Files/migrationcontainer/x

Paths built from variables are rewritten too, as long as the account is unambiguous (one mapped account):

    f"abfss://{container}@{account}.dfs.core.windows.net/x"  ->  f"abfss://<ws>@onelake.dfs.fabric.microsoft.com/<lh>/Files/{container}/x"
    'https://' + @Account + '.dfs.core.windows.net/' + @C    ->  'https://onelake.dfs.fabric.microsoft.com/<ws>/<lh>/Files/' + @C

The container keeps coming from the variable, which works because each shortcut is named after its container.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

ONELAKE_HOST = "onelake.dfs.fabric.microsoft.com"
_ACCOUNT = r"[a-z0-9]{3,24}"
_CONTAINER = r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?"
_PLACEHOLDER = r"\{[^{}\s]+\}"

#: abfss://container@account.dfs.core.windows.net  (either part may be an f-string / format placeholder)
_ABFSS = re.compile(rf"\b(?:abfss?|wasbs?)://({_CONTAINER}|{_PLACEHOLDER})@({_ACCOUNT}|{_PLACEHOLDER})\.(?:dfs|blob)\.core\.windows\.net",
                    re.IGNORECASE)
#: https://account.dfs.core.windows.net/container
_HTTPS = re.compile(rf"\bhttps://({_ACCOUNT})\.(?:dfs|blob)\.core\.windows\.net/({_CONTAINER})\b", re.IGNORECASE)
#: T-SQL building the host from a variable: 'https://' + @Account + '.dfs.core.windows.net/'
_SQL_BUILT = re.compile(r"N?'https://'\s*\+\s*(?:@\w+|N?'[^']*')\s*\+\s*N?'\.(?:dfs|blob)\.core\.windows\.net/'", re.IGNORECASE)
_STORAGE_HOST = re.compile(r"\.(?:dfs|blob)\.core\.windows\.net", re.IGNORECASE)


@dataclass(frozen=True)
class Route:
    """One storage location routed through a OneLake shortcut, resolved for a run."""

    account: str
    container: str
    workspace_id: str
    lakehouse_id: str
    shortcut: str  # the shortcut's name under Files: the container's name

    @property
    def abfss(self) -> str:
        return f"abfss://{self.workspace_id}@{ONELAKE_HOST}/{self.lakehouse_id}/Files/{self.shortcut}"

    @property
    def https(self) -> str:
        return f"https://{ONELAKE_HOST}/{self.workspace_id}/{self.lakehouse_id}/Files/{self.shortcut}"


class Routes:
    """The routes of a run, looked up by account and container."""

    def __init__(self, routes: Iterable[Route]) -> None:
        self.items = list(routes)
        self._by_pair = {(r.account.lower(), r.container.lower()): r for r in self.items}
        self.accounts = sorted({r.account.lower() for r in self.items})

    def __bool__(self) -> bool:
        return bool(self.items)

    def get(self, account: str, container: str) -> Optional[Route]:
        return self._by_pair.get((account.lower(), container.lower()))

    def single(self) -> Optional[Route]:
        """Any route of the only mapped account, for paths whose account comes from a variable. None if ambiguous."""
        if len(self.accounts) != 1:
            return None
        return self.items[0]

    def base(self, account: str) -> Optional[Route]:
        """Any route on this account: its workspace and Lakehouse serve every container of the account."""
        return next((r for r in self.items if r.account.lower() == account.lower()), None)


def _is_placeholder(text: str) -> bool:
    return text.startswith("{") and text.endswith("}")


def rewrite(code: str, routes: Routes) -> Tuple[str, List[str]]:
    """Rewrite every storage path in Python / Scala / SQL text that a route covers. Returns (text, notes)."""
    if not routes or not code or not _STORAGE_HOST.search(code):
        return code, []
    changed: List[str] = []
    left: Set[str] = set()

    def abfss(m: "re.Match[str]") -> str:
        container, account = m.group(1), m.group(2)
        if not _is_placeholder(account):
            if _is_placeholder(container):
                route = routes.base(account)
                if route is None:
                    left.add(account)
                    return m.group(0)
                changed.append(f"{account} (container from {container})")
                return f"abfss://{route.workspace_id}@{ONELAKE_HOST}/{route.lakehouse_id}/Files/{container}"
            route = routes.get(account, container)
            if route is None:
                left.add(f"{container}@{account}")
                return m.group(0)
            changed.append(f"{container}@{account}")
            return route.abfss
        route = routes.single()
        if route is None:
            left.add(f"{container}@{account}")
            return m.group(0)
        if _is_placeholder(container):
            changed.append(f"{account} (account and container from variables)")
            return f"abfss://{route.workspace_id}@{ONELAKE_HOST}/{route.lakehouse_id}/Files/{container}"
        changed.append(f"{container}@{account}")
        return f"abfss://{route.workspace_id}@{ONELAKE_HOST}/{route.lakehouse_id}/Files/{container}"

    def https(m: "re.Match[str]") -> str:
        route = routes.get(m.group(1), m.group(2))
        if route is None:
            left.add(f"https://{m.group(1)}/{m.group(2)}")
            return m.group(0)
        changed.append(f"{m.group(2)} on {m.group(1)}")
        return route.https

    def built(m: "re.Match[str]") -> str:
        route = routes.single()
        if route is None:
            left.add("an account built from a variable")
            return m.group(0)
        changed.append("an account built from a variable")
        return f"'https://{ONELAKE_HOST}/{route.workspace_id}/{route.lakehouse_id}/Files/'"

    text = _ABFSS.sub(abfss, code)
    text = _HTTPS.sub(https, text)
    text = _SQL_BUILT.sub(built, text)
    notes: List[str] = []
    if changed:
        where = ", ".join(sorted(set(changed))[:4])
        notes.append(f"Storage paths now go through the OneLake shortcut ({len(changed)} rewritten: {where}).")
    if left:
        notes.append("Not rewritten (no route for it): " + ", ".join(sorted(left)[:4]) + ". It still reads the storage account directly.")
    return text, notes


def still_direct(code: str) -> bool:
    """Whether the text still names an Azure storage endpoint after rewriting."""
    return bool(_STORAGE_HOST.search(code or ""))


# -- what a plan uses ---------------------------------------------------------------------------------


def locations(texts: Iterable[str]) -> Dict[Tuple[str, str], int]:
    """Every literal (account, container) the code names, with how many times."""
    found: Dict[Tuple[str, str], int] = {}
    for text in texts:
        for m in _ABFSS.finditer(text or ""):
            container, account = m.group(1), m.group(2)
            if not _is_placeholder(container) and not _is_placeholder(account):
                key = (account.lower(), container.lower())
                found[key] = found.get(key, 0) + 1
        for m in _HTTPS.finditer(text or ""):
            key = (m.group(1).lower(), m.group(2).lower())
            found[key] = found.get(key, 0) + 1
    return found


def uses_storage(text: str) -> bool:
    return bool(_STORAGE_HOST.search(text or ""))


def connection_location(path: str) -> Optional[Tuple[str, Optional[str]]]:
    """(account, container or None) a Fabric ADLS connection points at, from its path."""
    m = re.match(rf"^https://({_ACCOUNT})\.(?:dfs|blob)\.core\.windows\.net/?([^/?#]*)", (path or "").strip(), re.IGNORECASE)
    if not m:
        return None
    return m.group(1).lower(), (m.group(2).lower() or None)


def suggest(found: Dict[Tuple[str, str], int], connections: Sequence[Dict[str, str]]) -> List[Dict[str, object]]:
    """Locations to route, each with the Fabric connection that can reach it (None when there is none yet).

    ``connections``: {"id", "name", "path"} for each Fabric ADLS / Blob connection the user can see. A connection's own
    container is offered too when the code builds that account's paths from variables (it never names one)."""
    pairs = set(found)
    accounts = {a for a, _ in found}
    for c in connections:
        loc = connection_location(c.get("path", ""))
        if loc and loc[1] and loc[0] in accounts:
            pairs.add((loc[0], loc[1]))
    out: List[Dict[str, object]] = []
    for account, container in sorted(pairs):
        best = None
        for c in connections:
            loc = connection_location(c.get("path", ""))
            if not loc or loc[0] != account:
                continue
            if loc[1] in (None, container):  # the account, or exactly this container
                best = c if best is None or loc[1] == container else best
        out.append({"account": account, "container": container, "uses": found.get((account, container), 0),
                    "connectionId": best.get("id") if best else None, "connectionName": best.get("name") if best else None,
                    "linkedService": best.get("linkedService") if best else None})
    return out
