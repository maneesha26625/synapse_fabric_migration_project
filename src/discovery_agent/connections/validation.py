"""What a connection check found, as a value rather than as console output.

Validation returns; it never prints. The dev command in ``__main__`` is the
only thing in this package that writes to a terminal, and a future UI will
render the same objects without a parallel code path having to be written.

Two rules hold for everything in this module:

* **A result is a fact, not a verdict about what to do next.** It says which
  connection was checked, whether it worked, and -- when it did not -- which
  *category* of problem it was, because "not signed in" and "signed in but
  not authorized" need different actions from an operator.
* **A result cannot carry a credential.** ``redact`` runs over every message
  and every detail value on construction. That is a backstop, not the design:
  nothing in this package puts a token in a message. It exists because the
  text of a driver's or an HTTP library's error is not ours to vouch for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Tuple

from discovery_agent.extractors.models import SourceType

#: The mask a redacted value is replaced with. Distinctive on purpose: seeing
#: it in output means something tried to put a credential in a result.
REDACTED = "[redacted]"

_SECRET_PATTERNS: Tuple[re.Pattern, ...] = (
    # A JWT, which is what every Entra access token is.
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    # Connection-string and query-string spellings of the same thing.
    re.compile(
        r"\b(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|password|pwd|sig)"
        r"\s*[=:]\s*\S+",
        re.IGNORECASE,
    ),
    # Credentials embedded in a URL: https://user:secret@host/...
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
)


def redact(text: str) -> str:
    """Replace anything credential-shaped in ``text``.

    Deliberately blunt. A false positive costs an operator one detail in a
    message; a false negative writes a live token into a log file.
    """
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


class ValidationStatus(str, Enum):
    """The three outcomes of a connection check."""

    OK = "ok"
    FAILED = "failed"
    #: Not attempted, because something it depends on failed first. Distinct
    #: from FAILED: reporting a SQL failure caused by a missing Azure sign-in
    #: would send an operator to fix the wrong thing.
    SKIPPED = "skipped"


class ErrorCategory(str, Enum):
    """Why a check failed, at the granularity that changes what you do about it."""

    CONFIGURATION = "configuration"  # we were never told enough to try
    AUTHENTICATION = "authentication"  # no usable identity: `az login`
    AUTHORIZATION = "authorization"  # an identity, but not one with rights
    NOT_FOUND = "not_found"  # the resource named does not exist
    #: The resource exists and we may read it, but it cannot serve a
    #: connection right now -- a paused SQL pool being the case that matters
    #: here. Distinct from CONFIGURATION, which would send an operator to
    #: check a name that is perfectly correct, and from NOT_FOUND, which
    #: would send them to create a pool they already have.
    UNAVAILABLE = "unavailable"
    NETWORK = "network"  # could not reach the endpoint at all
    DEPENDENCY = "dependency"  # a driver, SDK or tool is not installed
    UNSUPPORTED = "unsupported"  # a mechanism this build does not implement
    UNKNOWN = "unknown"


#: HTTP status to category, for the ARM calls. 403 is kept apart from 401
#: because the remedy differs: sign in again, versus be granted a role.
_HTTP_CATEGORIES = {
    400: ErrorCategory.CONFIGURATION,
    401: ErrorCategory.AUTHENTICATION,
    403: ErrorCategory.AUTHORIZATION,
    404: ErrorCategory.NOT_FOUND,
    409: ErrorCategory.CONFIGURATION,
}


def category_for_status(status_code: int) -> ErrorCategory:
    """The category an ARM response's status code implies."""
    if status_code in _HTTP_CATEGORIES:
        return _HTTP_CATEGORIES[status_code]
    if 500 <= status_code < 600:
        return ErrorCategory.NETWORK
    return ErrorCategory.UNKNOWN


@dataclass(frozen=True)
class ConnectionValidation:
    """The result of checking one connection.

    ``details`` is non-secret metadata an operator needs in order to act:
    which subscription, which workspace, which endpoint, which identity. It is
    a mapping of strings so it serializes without a schema and renders without
    a template.
    """

    connection: SourceType
    status: ValidationStatus
    message: str
    details: Mapping[str, str] = field(default_factory=dict)
    category: Optional[ErrorCategory] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", redact(self.message))
        object.__setattr__(
            self,
            "details",
            MappingProxyType(
                {str(key): redact(str(value)) for key, value in self.details.items()}
            ),
        )
        if self.status is ValidationStatus.OK and self.category is not None:
            raise ValueError("a successful validation cannot carry an error category")
        if self.status is ValidationStatus.FAILED and self.category is None:
            raise ValueError(
                f"a failed validation must say why: {self.connection.value} "
                f"gave no error category"
            )

    @property
    def ok(self) -> bool:
        return self.status is ValidationStatus.OK

    def to_dict(self) -> dict:
        return {
            "connection": self.connection.value,
            "status": self.status.value,
            "message": self.message,
            "details": dict(self.details),
            "category": self.category.value if self.category else None,
        }


def ok(connection: SourceType, message: str, **details: str) -> ConnectionValidation:
    """A successful check.

    Details arrive as keyword arguments because they differ per connection and
    a positional dict reads worse at every call site.
    """
    return ConnectionValidation(
        connection=connection,
        status=ValidationStatus.OK,
        message=message,
        details=details,
    )


def failed(
    connection: SourceType,
    message: str,
    category: ErrorCategory = ErrorCategory.UNKNOWN,
    **details: str,
) -> ConnectionValidation:
    """A failed check, with the category that tells an operator what to fix."""
    return ConnectionValidation(
        connection=connection,
        status=ValidationStatus.FAILED,
        message=message,
        details=details,
        category=category,
    )


def skipped(
    connection: SourceType, message: str, **details: str
) -> ConnectionValidation:
    """A check that was never run because a prerequisite failed."""
    return ConnectionValidation(
        connection=connection,
        status=ValidationStatus.SKIPPED,
        message=message,
        details=details,
    )


@dataclass(frozen=True)
class ValidationReport:
    """Every check from one validation run, in the order they were attempted."""

    results: Tuple[ConnectionValidation, ...]

    def __init__(self, results: Iterable[ConnectionValidation]) -> None:
        object.__setattr__(self, "results", tuple(results))

    @property
    def ok(self) -> bool:
        """True only if every check ran and every check passed.

        A skip is not a pass. A report that called itself fine while SQL was
        never attempted would be the single most misleading thing this package
        could produce.
        """
        return bool(self.results) and all(result.ok for result in self.results)

    def result_for(self, connection: SourceType) -> Optional[ConnectionValidation]:
        return next((r for r in self.results if r.connection is connection), None)

    @property
    def failures(self) -> Tuple[ConnectionValidation, ...]:
        return tuple(r for r in self.results if r.status is ValidationStatus.FAILED)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "results": [result.to_dict() for result in self.results],
        }
