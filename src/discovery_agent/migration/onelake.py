"""Files in a Lakehouse, through OneLake's ADLS Gen2-compatible API.

Used to give a migrated Spark job definition its own copy of its main file, with the storage paths inside it
rewritten (``storage_remap``): the original stays where it is, untouched, for Synapse. Reading goes through the
Lakehouse's shortcut, so the storage is reached with the Fabric connection's credential, not the user's.

The token is for OneLake (audience ``https://storage.azure.com/``) in the Fabric tenant; it is sent only to
``onelake.dfs.fabric.microsoft.com``.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, Mapping, Optional, Tuple

from discovery_agent.migration.storage_remap import ONELAKE_HOST

API_VERSION = "2023-11-03"
TIMEOUT = 120


class OneLakeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def _send(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


class OneLakeFiles:
    def __init__(self, token: Callable[[], str], send: Callable[..., Tuple[int, Dict[str, str], bytes]] = _send) -> None:
        self._token, self._send = token, send

    def _url(self, workspace_id: str, lakehouse_id: str, path: str, query: str = "") -> str:
        quoted = urllib.parse.quote(path.lstrip("/"), safe="/")
        return f"https://{ONELAKE_HOST}/{workspace_id}/{lakehouse_id}/{quoted}" + (f"?{query}" if query else "")

    def _call(self, method: str, url: str, body: Optional[bytes] = None, what: str = "") -> bytes:
        headers = {"Authorization": f"Bearer {self._token()}", "x-ms-version": API_VERSION}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        status, _, data = self._send(method, url, headers, body)
        if status >= 300:
            raise OneLakeError(status, f"OneLake refused to {what} ({status}): {data[:200].decode('utf-8', 'replace')}")
        return data

    def probe(self, workspace_id: str, lakehouse_id: str, path: str) -> None:
        """List at most one entry under a folder (a shortcut, for one). Through a shortcut OneLake reads the storage
        with the shortcut's connection, so this proves that connection's identity may read it. Raises ``OneLakeError``."""
        directory = urllib.parse.quote(f"{lakehouse_id}/{path.strip('/')}", safe="/")
        url = f"https://{ONELAKE_HOST}/{workspace_id}?resource=filesystem&recursive=false&maxResults=1&directory={directory}"
        self._call("GET", url, what=f"list {path}")

    def read(self, workspace_id: str, lakehouse_id: str, path: str) -> bytes:
        """A file, by its path in the Lakehouse (``Files/...``)."""
        return self._call("GET", self._url(workspace_id, lakehouse_id, path), what=f"read {path}")

    def write(self, workspace_id: str, lakehouse_id: str, path: str, data: bytes) -> None:
        """Create or replace a file: create, append the bytes, flush."""
        self._call("PUT", self._url(workspace_id, lakehouse_id, path, "resource=file"), b"", what=f"create {path}")
        if data:
            self._call("PATCH", self._url(workspace_id, lakehouse_id, path, "action=append&position=0"), data, what=f"write {path}")
        self._call("PATCH", self._url(workspace_id, lakehouse_id, path, f"action=flush&position={len(data)}"), b"", what=f"save {path}")


def lakehouse_path(abfss: str) -> Optional[Tuple[str, str, str]]:
    """(workspace id, lakehouse id, path inside it) of a OneLake abfss URL; None for anything else."""
    parsed = urllib.parse.urlsplit(abfss)
    if parsed.scheme not in ("abfss", "abfs") or not parsed.netloc.lower().endswith("@" + ONELAKE_HOST):
        return None
    workspace = parsed.netloc.split("@", 1)[0]
    parts = parsed.path.lstrip("/").split("/", 1)
    if len(parts) != 2:
        return None
    return workspace, parts[0], parts[1]
