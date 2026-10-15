"""What kind of failure an object's error is, and so what may be done about it.

Every object error is classified once, where it happens, into a closed set. The rules run in order, and the
order matters: sign-in comes before transient, so a sign-in that timed out is AUTH (try again later would not
help), and a capacity problem comes before everything, because nothing in that workspace will work until it
is fixed. Only TRANSIENT is retried without a person, and never counted as one of their retries.
"""

from __future__ import annotations

from typing import Optional

TRANSIENT, AUTH, CAPACITY, CONFIG, UNSUPPORTED, DEPENDENCY, CONFLICT, INTERRUPTED, UNKNOWN = (
    "TRANSIENT", "AUTH", "CAPACITY", "CONFIG", "UNSUPPORTED", "DEPENDENCY", "CONFLICT", "INTERRUPTED", "UNKNOWN")
CATEGORIES = (TRANSIENT, AUTH, CAPACITY, CONFIG, UNSUPPORTED, DEPENDENCY, CONFLICT, INTERRUPTED, UNKNOWN)
#: Retried within the pass, with backoff, without a person.
RETRYABLE = frozenset({TRANSIENT})

#: What each category means for the operator, for the page and the report.
MEANING = {
    TRANSIENT: "Fabric or the network did not answer this time. It is retried by itself; if it keeps failing, Retry failed later.",
    AUTH: "The sign-in was refused or has expired. Sign in to Fabric again, then Retry failed.",
    CAPACITY: "The workspace cannot hold items right now (its capacity is paused, missing or full). Fix the capacity, then Retry failed.",
    CONFIG: "Something the run needs was not given, such as a connection's credentials. Enter it in Plan, then run again.",
    UNSUPPORTED: "Fabric does not accept this object as it is. It needs a fix: a rule, a reviewed LLM draft, or a rewrite by hand.",
    DEPENDENCY: "It needs another object that is not in Fabric yet. It runs again once that one is there.",
    CONFLICT: "Fabric already has an item with this name. Leave it, or update it in place (Update objects that are already in Fabric).",
    INTERRUPTED: "The accelerator stopped while it was being created. It is checked again when the run resumes.",
    UNKNOWN: "The cause is not recognised. Read the error, then Retry failed or fix it by hand.",
}

_WORKSPACE_CODES = {"FeatureNotAvailable", "WorkspaceNotFound", "CapacityNotActive", "CapacityLimitExceeded"}
_AUTH_CODES = {"Unauthorized", "TokenExpired", "InvalidToken", "InsufficientPrivileges", "PrincipalTypeNotSupported"}
_TRANSIENT_CODES = {"operation_timeout", "throttled", "RequestTimeout", "ServiceUnavailable", "InternalServerError",
                    "TooManyRequests", "BadGateway", "GatewayTimeout", "fabric_unreachable"}


def classify(message: str, code: Optional[str] = None, status: Optional[int] = None) -> str:
    """The category of one error, from Fabric's status and code when there are any, else from its message."""
    text = (message or "").lower()
    code = code or ""
    if code in _WORKSPACE_CODES or "capacity" in text and ("paused" in text or "not active" in text or "assign" in text):
        return CAPACITY
    if status in (401, 403) or code in _AUTH_CODES or any(w in text for w in (
            "sign in", "signed in", "log in again", "token expired", "access token", "authentication failed")):
        return AUTH
    if any(w in text for w in ("enter credentials", "needs credentials", "there is no fabric connection",
                               "is not known. reconnect", "the synapse source is not connected")):
        return CONFIG
    if code == "ItemDisplayNameAlreadyInUse" or status == 409 or "already in use" in text:
        return CONFLICT
    if any(w in text for w in ("is not in the warehouse yet", "needs these in fabric first", "is not in fabric yet",
                               "stage first", "which failed in this run")):
        return DEPENDENCY
    if any(w in text for w in ("rejected the", "has no fabric equivalent", "masking function", "not supported",
                               "unsupported", "needs a rewrite", "cannot be recreated", "could not be read from the pool")):
        return UNSUPPORTED
    if (status is not None and (status == 429 or status >= 500)) or code in _TRANSIENT_CODES or any(w in text for w in (
            "timed out", "timeout", "temporarily", "try again in a few minutes", "has no sql endpoint yet",
            "connection reset", "could not connect to warehouse")):
        return TRANSIENT
    if "stopped while" in text or "interrupted" in text:
        return INTERRUPTED
    return UNKNOWN
