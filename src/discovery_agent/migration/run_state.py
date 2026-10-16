"""A migration run's vocabulary: its statuses, the moves between them, and those of each object in it.

Pure: no clock, no I/O, no imports from the rest of the accelerator. It is the one place a reviewer reads to see
the whole policy, and ``GET /api/migration/policy`` hands it to the page as it is, so the page never keeps a
second copy that could drift.

A run::

    queued ─► awaiting_approval ─► queued (approved) │ rejected ■
    queued ─► running ─► completed ■
                 ├──► completed_with_failures ─► queued   (Retry failed: a new pass)
                 ├──► paused{reason} ──────────► queued   (Resume: a new pass)
                 └──► failed ──────────────────► queued   (Retry)
    paused · completed_with_failures · failed · awaiting_approval · queued ─► cancelled ■   (Reset closes it)

``■`` is terminal: nothing leaves it. A run is done once, and its record stays.

An object in it::

    pending ─► in_progress ─► completed │ skipped │ failed │ deferred
    pending ─► blocked (something it needs failed in this pass)
    failed │ blocked ─► pending (the next pass)      in_progress ─► pending (the process stopped under it)
"""

from __future__ import annotations

from typing import Dict, FrozenSet

QUEUED, AWAITING_APPROVAL, RUNNING, PAUSED = "queued", "awaiting_approval", "running", "paused"
COMPLETED, COMPLETED_WITH_FAILURES, FAILED, CANCELLED, REJECTED = (
    "completed", "completed_with_failures", "failed", "cancelled", "rejected")
STATUSES = (QUEUED, AWAITING_APPROVAL, RUNNING, PAUSED, COMPLETED, COMPLETED_WITH_FAILURES, FAILED, CANCELLED, REJECTED)

TRANSITIONS: Dict[str, FrozenSet[str]] = {
    # paused: the process stopped between the request and the pass's first object
    QUEUED: frozenset({AWAITING_APPROVAL, RUNNING, PAUSED, CANCELLED}),
    AWAITING_APPROVAL: frozenset({QUEUED, REJECTED, CANCELLED}),
    RUNNING: frozenset({PAUSED, COMPLETED, COMPLETED_WITH_FAILURES, FAILED}),
    PAUSED: frozenset({QUEUED, CANCELLED}),
    COMPLETED_WITH_FAILURES: frozenset({QUEUED, CANCELLED}),
    FAILED: frozenset({QUEUED, CANCELLED}),
    COMPLETED: frozenset(),
    CANCELLED: frozenset(),
    REJECTED: frozenset(),
}
TERMINAL = frozenset(s for s, moves in TRANSITIONS.items() if not moves)
#: A pass is working on objects (or about to): the run cannot be changed under it.
WORKING = frozenset({QUEUED, RUNNING})

#: Why a run is paused. Each has its own way on. ``awaiting_wave_signoff``: the brief has a person sign off each wave
#: before the next one starts (Resume, by a person, is that sign-off).
BY_PERSON, HALTED, NEEDS_INPUT, INTERRUPTED, WAVE_SIGNOFF = (
    "by_person", "halted_on_failures", "needs_input", "interrupted", "awaiting_wave_signoff")
PAUSE_REASONS = (BY_PERSON, HALTED, NEEDS_INPUT, INTERRUPTED, WAVE_SIGNOFF)

#: How each run status reads in the page's coarser vocabulary (idle | running | paused | completed).
_LEGACY = {QUEUED: "running", RUNNING: "running", AWAITING_APPROVAL: "paused", PAUSED: "paused",
           COMPLETED: "completed", COMPLETED_WITH_FAILURES: "completed", FAILED: "completed",
           CANCELLED: "completed", REJECTED: "completed"}

# -- objects ------------------------------------------------------------------------------------

PENDING, IN_PROGRESS, DONE, SKIPPED, ITEM_FAILED, DEFERRED, BLOCKED = (
    "PENDING", "IN PROGRESS", "COMPLETED", "SKIPPED", "FAILED", "DEFERRED", "BLOCKED")
ITEM_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    PENDING: frozenset({IN_PROGRESS, BLOCKED}),
    IN_PROGRESS: frozenset({DONE, SKIPPED, ITEM_FAILED, DEFERRED, PENDING}),
    ITEM_FAILED: frozenset({PENDING}),
    BLOCKED: frozenset({PENDING}),
    DONE: frozenset(),
    SKIPPED: frozenset(),
    DEFERRED: frozenset(),
}
#: Objects that need a person's attention after a pass.
NEEDS_ATTENTION = frozenset({ITEM_FAILED, BLOCKED})


class IllegalTransition(Exception):
    """A move the policy does not allow. Nothing was changed."""

    def __init__(self, what: str, current: str, target: str, allowed: FrozenSet[str]) -> None:
        moves = ", ".join(sorted(allowed)) or "none: it is final"
        super().__init__(f"A {what} that is {current} cannot become {target} (allowed: {moves}).")
        self.current, self.target = current, target


def check_run(current: str, target: str) -> None:
    allowed = TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise IllegalTransition("run", current, target, allowed)


def check_item(current: str, target: str) -> None:
    if current == target:
        return
    allowed = ITEM_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise IllegalTransition("migration object", current, target, allowed)


def legacy_state(status: str) -> str:
    return _LEGACY.get(status, "completed")


def describe() -> dict:
    """The whole policy, as the page reads it."""
    return {
        "statuses": list(STATUSES), "terminal": sorted(TERMINAL), "working": sorted(WORKING),
        "transitions": {s: sorted(moves) for s, moves in TRANSITIONS.items()},
        "pauseReasons": list(PAUSE_REASONS),
        "itemTransitions": {s: sorted(moves) for s, moves in ITEM_TRANSITIONS.items()},
        "needsAttention": sorted(NEEDS_ATTENTION),
    }
