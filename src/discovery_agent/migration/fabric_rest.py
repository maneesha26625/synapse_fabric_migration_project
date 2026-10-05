"""A small client for the Fabric REST API, for creating items.

Discovery never writes; this is the one place that does, and it writes only to
``api.fabric.microsoft.com``. The token comes from a provider callable (the
Fabric Target page's Azure CLI sign-in) and is put into a header at the call,
never kept on this object.

Fabric answers a create in one of two ways: ``201`` with the item, or ``202``
with a long-running operation to poll. Both are handled here, as are paging
(``continuationToken``) and throttling (``429`` with ``Retry-After``).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

FABRIC_HOST = "https://api.fabric.microsoft.com"
FABRIC_API = FABRIC_HOST + "/v1"
CALL_TIMEOUT_SECONDS = 60
#: How long one create may take before the run gives up on it.
OPERATION_TIMEOUT_SECONDS = 600
MAX_THROTTLE_RETRIES = 5

#: Fabric error codes with a cause worth stating plainly, and the fix.
NO_CAPACITY = (
    "This workspace is not on a Fabric capacity, so notebooks and warehouses cannot be created in it. "
    "In Fabric, open the workspace settings, choose License info, and assign a Fabric or Trial capacity; then retry"
)
KNOWN_ERRORS = {
    "FeatureNotAvailable": NO_CAPACITY,
    "CapacityNotActive": "The workspace's Fabric capacity is paused. Resume it in the Azure portal, then retry",
    "InsufficientPrivileges": "The signed-in account needs Contributor or higher on the Fabric workspace",
}

#: (method, url, headers, body) -> (status, lower-cased headers, body bytes)
Send = Callable[[str, str, Mapping[str, str], Optional[bytes]], Tuple[int, Dict[str, str], bytes]]


class FabricApiError(Exception):
    """A Fabric call that did not succeed. ``code`` is Fabric's errorCode when it gave one."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _urllib_send(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed https host
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read() or b""
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b""
    except (urllib.error.URLError, OSError) as exc:
        raise FabricApiError(0, "fabric_unreachable", "The Fabric API could not be reached. Check the network connection.") from exc


def _json(body: bytes) -> Any:
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {}


class FabricRestClient:
    def __init__(
        self,
        token_provider: Callable[[], str],
        send: Optional[Send] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token_provider
        self._send = send or _urllib_send
        self._sleep = sleep
        self._clock = clock

    # -- plumbing ------------------------------------------------------

    def _url(self, path_or_url: str) -> str:
        url = path_or_url if path_or_url.startswith("https://") else FABRIC_API + path_or_url
        # The bearer token only ever goes to Fabric: a Location or paging link
        # pointing anywhere else is refused, not followed.
        if not url.startswith(FABRIC_HOST + "/"):
            raise FabricApiError(0, "unexpected_host", "Fabric returned a link outside api.fabric.microsoft.com; it was not followed.")
        return url

    def request(self, method: str, path_or_url: str, body: Optional[dict] = None) -> Tuple[int, Dict[str, str], Any]:
        url = self._url(path_or_url)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(MAX_THROTTLE_RETRIES + 1):
            headers = {"Authorization": "Bearer " + self._token(), "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            status, response_headers, raw = self._send(method, url, headers, data)
            if status == 429 and attempt < MAX_THROTTLE_RETRIES:
                self._sleep(self._retry_after(response_headers, default=5.0 * (attempt + 1)))
                continue
            payload = _json(raw)
            if status >= 400:
                raise self._error(status, payload)
            return status, response_headers, payload
        raise FabricApiError(429, "throttled", "Fabric kept throttling the requests. Try again in a few minutes.")

    @staticmethod
    def _retry_after(headers: Mapping[str, str], default: float) -> float:
        try:
            return max(1.0, min(60.0, float(headers.get("retry-after", default))))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _error(status: int, payload: Any) -> FabricApiError:
        payload = payload if isinstance(payload, dict) else {}
        code = str(payload.get("errorCode") or f"http_{status}")
        message = str(payload.get("message") or f"The Fabric API returned HTTP {status}.")
        known = KNOWN_ERRORS.get(code)
        if known:
            message = f"{known} (Fabric: {code})"
        elif status in (401, 403):
            message = f"Fabric refused the request ({code}). The signed-in account needs Contributor or higher on the workspace. {message}"
        return FabricApiError(status, code, message[:500])

    # -- operations ------------------------------------------------------

    def list(self, path: str) -> List[dict]:
        """Every item of a collection, following ``continuationToken``."""
        items: List[dict] = []
        next_path: Optional[str] = path
        seen = set()
        while next_path:
            if next_path in seen:
                break
            seen.add(next_path)
            _, _, page = self.request("GET", next_path)
            items.extend(page.get("value") or [])
            token = page.get("continuationToken")
            if not token:
                break
            joiner = "&" if "?" in path else "?"
            next_path = f"{path}{joiner}continuationToken={urllib.parse.quote(str(token), safe='')}"
        return items

    def get(self, path: str) -> dict:
        _, _, payload = self.request("GET", path)
        return payload if isinstance(payload, dict) else {}

    def create(self, path: str, body: dict) -> dict:
        """POST a new item and wait for it. Returns the created item when Fabric reports it."""
        status, headers, payload = self.request("POST", path, body)
        if status != 202:
            return payload if isinstance(payload, dict) else {}
        location = headers.get("location")
        if not location:
            operation = headers.get("x-ms-operation-id")
            if not operation:
                return {}
            location = f"{FABRIC_API}/operations/{operation}"
        return self._wait(location, self._retry_after(headers, default=2.0))

    def _wait(self, location: str, delay: float) -> dict:
        deadline = self._clock() + OPERATION_TIMEOUT_SECONDS
        while True:
            self._sleep(delay)
            _, headers, state = self.request("GET", location)
            status = str((state or {}).get("status") or "")
            if status == "Succeeded":
                try:
                    _, _, result = self.request("GET", location.rstrip("/") + "/result")
                    return result if isinstance(result, dict) else {}
                except FabricApiError:
                    return {}  # some operations have no result; the create still succeeded
            if status == "Failed":
                error = (state or {}).get("error") or {}
                raise FabricApiError(400, str(error.get("errorCode") or "operation_failed"), str(error.get("message") or "Fabric reported the operation as failed.")[:500])
            if self._clock() > deadline:
                raise FabricApiError(504, "operation_timeout", "Fabric did not finish creating the item in time. Check the workspace, then retry.")
            delay = self._retry_after(headers, default=min(delay * 1.5, 15.0))
