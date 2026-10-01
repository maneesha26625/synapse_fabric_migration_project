"""Dependency-ordered migration waves.

A wave is a group of objects that can be migrated together because everything
they reference is in the same or an earlier wave. The ordering is derived from
two things only: the object's type (infrastructure before data, data before
logic, logic before orchestration) and the dependencies discovery actually
observed. No names are inspected and nothing is guessed.

This is a *suggested* grouping for planning. It does not decide that an object
can migrate, only that, if it does, it should not be attempted before what it
references.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Tuple

#: The earliest wave an object type belongs in, before its dependencies are
#: considered: foundations, then data, then logic, then orchestration.
BASE_WAVE: Mapping[str, int] = {
    "Linked Service": 1,
    "Integration Runtime": 1,
    "Storage Reference": 1,
    "Dedicated SQL Pool": 1,
    "Serverless SQL": 1,
    "Spark Pool": 1,
    "Spark Library": 1,
    "Schema": 1,
    "Security Object": 1,
    "Networking Configuration": 1,
    "Table": 2,
    "External Table": 2,
    "Dataset": 2,
    "Lake Database": 2,
    "View": 3,
    "Stored Procedure": 3,
    "Function": 3,
    "SQL Script": 3,
    "Notebook": 4,
    "Spark Job Definition": 4,
    "Pipeline": 4,
    "Trigger": 5,
}
_DEFAULT_WAVE = 3


def assign_waves(
    types: Mapping[str, str], edges: Iterable[Tuple[str, str]]
) -> Dict[str, int]:
    """Wave number per object id.

    ``edges`` are ``(dependent, dependency)`` pairs: the first needs the
    second. An object's wave is its type's base wave, pushed later when a
    dependency sits at or after it -- so it is never scheduled before what it
    references. A cycle is broken by ignoring the edge that closes it.
    """
    deps: Dict[str, List[str]] = {}
    for dependent, dependency in edges:
        if dependent in types and dependency in types and dependent != dependency:
            deps.setdefault(dependent, []).append(dependency)

    waves: Dict[str, int] = {}
    visiting = set()

    def wave(node: str) -> int:
        if node in waves:
            return waves[node]
        base = BASE_WAVE.get(types[node], _DEFAULT_WAVE)
        visiting.add(node)
        best = base
        for dep in deps.get(node, ()):
            if dep in visiting:
                continue  # closes a cycle: ignore this edge
            best = max(best, wave(dep) + 1 if wave(dep) >= base else base)
        visiting.discard(node)
        waves[node] = best
        return best

    # Iterative driver: recursion depth equals dependency chain length, which
    # is small, but a deliberately deep chain must not crash the API.
    import sys

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 5000))
    try:
        for node in types:
            wave(node)
    finally:
        sys.setrecursionlimit(limit)
    return waves


def summarise_waves(
    waves: Mapping[str, int], types: Mapping[str, str]
) -> List[dict]:
    """Per wave: how many objects, and of which types."""
    out: Dict[int, Dict[str, int]] = {}
    for node, number in waves.items():
        bucket = out.setdefault(number, {})
        bucket[types[node]] = bucket.get(types[node], 0) + 1
    return [
        {"wave": number, "count": sum(counts.values()), "types": dict(sorted(counts.items()))}
        for number, counts in sorted(out.items())
    ]
