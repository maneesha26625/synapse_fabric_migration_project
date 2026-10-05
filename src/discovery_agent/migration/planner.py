"""The migration planner: from a plan and what discovery knows, to a strategy.

Deterministic on purpose. The same plan over the same estate always produces
the same strategies, risks, effort, readiness and fingerprint: there is no
clock, randomness or network in here. Anything that needs the outside world
(is Fabric connected, can the SQL driver load, can a table's DDL be built) is
computed by the caller and passed in as ``findings`` and ``checks``.

Four ideas, in the order the page shows them:

* **Strategy** per object: Automated (this tool creates it), Manual,
  Assess first, or Later session. Chosen from what the runner can migrate and
  the object's classification, never guessed from a name.
* **Risks** with a severity. A BLOCKING risk means the run would fail or do the
  wrong thing; the readiness score falls with every risk.
* **Effort** in working days: a review allowance for automated objects, a
  build allowance for the rest.
* **Waves** straight from the dependency order, each with how much of it the
  tool can do now.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

PLANNER_VERSION = "1.0.0"

AUTOMATED, MANUAL, ASSESS, LATER, DESELECTED = "automated", "manual", "assess", "later", "deselected"
STRATEGY_LABELS = {
    AUTOMATED: "Automated",
    MANUAL: "Manual setup",
    ASSESS: "Assess first",
    LATER: "Later session",
    DESELECTED: "Not selected",
}
STRATEGY_HELP = {
    AUTOMATED: "This tool creates it in Fabric. Existing objects are skipped, never overwritten.",
    MANUAL: "Set it up in Fabric by hand: it has no automatic equivalent.",
    ASSESS: "Needs a review of how it is used before a target is chosen.",
    LATER: "Mapped to a Fabric component, but this build cannot create it yet.",
    DESELECTED: "Its migration stage is switched off for this run.",
}

BLOCKING, HIGH, MEDIUM, LOW = "BLOCKING", "HIGH", "MEDIUM", "LOW"
SEVERITIES = (BLOCKING, HIGH, MEDIUM, LOW)
_PENALTY = {BLOCKING: 25.0, HIGH: 6.0, MEDIUM: 2.0, LOW: 0.5}
_SEVERITY_ORDER = {s: i for i, s in enumerate(SEVERITIES)}

#: Working hours to migrate one object of a type by hand, end to end.
EFFORT_HOURS = {
    "Pipeline": 4.0, "Dataset": 1.0, "Linked Service": 1.0, "Trigger": 0.5, "SQL Script": 1.0,
    "External Table": 1.0, "Spark Job Definition": 3.0, "Integration Runtime": 2.0,
    "Notebook": 2.0, "Table": 1.0, "View": 1.0, "Stored Procedure": 2.0, "Schema": 0.25,
    "Dedicated SQL Pool": 1.0, "Spark Pool": 1.0,
}
DEFAULT_EFFORT_HOURS = 2.0
#: Share of that effort left for an automated object: reading the result.
AUTOMATED_REVIEW_SHARE = 0.25
MANUAL_FACTOR = 1.5
HOURS_PER_DAY = 8.0
#: An object this many others depend on is a hub: migrate it early, test it hard.
HUB_THRESHOLD = 5


@dataclass(frozen=True)
class PlanObject:
    id: str
    name: str
    type: str
    wave: int
    classification: str = ""
    fabric_target: str = ""
    #: The runner's kind for it, or None when this build cannot migrate the type.
    kind: Optional[str] = None
    depends_on: Sequence[str] = ()
    depended_on_by: int = 0
    #: False when the stage that would migrate it is switched off for this run.
    selected: bool = True


@dataclass(frozen=True)
class Finding:
    """A risk raised from the object's own content (DDL, definition, notebook)."""

    object_id: str
    code: str
    severity: str
    message: str


def strategy_for(obj: PlanObject) -> str:
    if obj.kind and not obj.selected:
        return DESELECTED
    if obj.kind:
        return AUTOMATED
    cls = (obj.classification or "").upper()
    if cls == "MANUAL":
        return MANUAL
    if cls in ("REVIEW", "ASSESS", "ASSESSMENT"):
        return ASSESS
    return LATER


def effort_hours(obj: PlanObject, strategy: str) -> float:
    if strategy == DESELECTED:
        return 0.0  # not part of this run's work
    base = EFFORT_HOURS.get(obj.type, DEFAULT_EFFORT_HOURS)
    if strategy == AUTOMATED:
        return base * AUTOMATED_REVIEW_SHARE
    if strategy == MANUAL:
        return base * MANUAL_FACTOR
    return base


def _risk(code: str, severity: str, title: str, message: str, objects: Iterable[str] = ()) -> Dict[str, Any]:
    return {"code": code, "severity": severity, "title": title, "message": message, "objects": sorted(set(objects))}


def analyze(
    objects: Sequence[PlanObject],
    findings: Sequence[Finding] = (),
    checks: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """The full analysis of a plan. Pure: nothing here reads the world."""
    by_id = {o.id: o for o in objects}
    strategies = {o.id: strategy_for(o) for o in objects}
    risks: List[Dict[str, Any]] = []

    # -- ordering and completeness ---------------------------------------
    for o in objects:
        later, missing = [], []
        for dep_id in o.depends_on:
            dep = by_id.get(dep_id)
            if dep is None:
                missing.append(dep_id)
            elif dep.wave > o.wave:
                later.append(dep.name)
        if later:
            risks.append(_risk(
                "DEPENDENCY_LATER", BLOCKING, o.name,
                f"{o.name} is in wave {o.wave} but needs {', '.join(sorted(later)[:3])}"
                f"{' and more' if len(later) > 3 else ''}, planned in a later wave. Move it later or its dependency earlier.",
                [o.id]))
        if missing and strategies[o.id] == AUTOMATED:
            names = sorted(m.rsplit("/", 1)[-1] for m in missing)
            risks.append(_risk(
                "DEPENDENCY_NOT_PLANNED", HIGH, o.name,
                f"{o.name} needs {', '.join(names[:3])}{' and more' if len(names) > 3 else ''}, which is not in the plan. "
                "Add it, or confirm it already exists in Fabric.", [o.id]))

    hubs = [o for o in objects if o.depended_on_by >= HUB_THRESHOLD]
    for o in hubs:
        risks.append(_risk(
            "HIGH_BLAST_RADIUS", MEDIUM, o.name,
            f"{o.name} is a hub ({o.depended_on_by} objects depend on it): migrate it early and test it hard.", [o.id]))

    for f in findings:
        if f.object_id in by_id:
            risks.append(_risk(f.code, f.severity, by_id[f.object_id].name, f.message, [f.object_id]))

    deferred = [o for o in objects if strategies[o.id] not in (AUTOMATED, DESELECTED)]
    if deferred:
        counts = Counter(o.type for o in deferred)
        detail = ", ".join(f"{n} {t}" for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:6])
        risks.append(_risk(
            "NOT_AUTOMATED", LOW, "Not migrated by this build",
            f"{len(deferred)} object(s) are in the plan but not created by this run: {detail}.", [o.id for o in deferred]))

    for c in checks:
        if c.get("status") == "fail":
            risks.append(_risk("TARGET_NOT_READY", BLOCKING, str(c.get("label")), str(c.get("detail") or c.get("label")), []))
        elif c.get("status") == "warn":
            risks.append(_risk("TARGET_WARNING", MEDIUM, str(c.get("label")), str(c.get("detail") or c.get("label")), []))

    risks.sort(key=lambda r: (_SEVERITY_ORDER[r["severity"]], r["code"], r["title"]))
    for index, r in enumerate(risks, 1):
        r["id"] = f"R{index:03d}"
    by_severity = Counter(r["severity"] for r in risks)
    penalty = sum(_PENALTY[s] * n for s, n in by_severity.items())
    readiness = max(0, min(100, round(100 - penalty)))

    # -- effort ------------------------------------------------------------
    hours = {o.id: effort_hours(o, strategies[o.id]) for o in objects}
    review_ids = [o.id for o in objects if strategies[o.id] in (MANUAL, ASSESS)]
    effort_days = round(sum(hours.values()) / HOURS_PER_DAY, 2)

    # -- waves -------------------------------------------------------------
    waves = []
    for number in sorted({o.wave for o in objects}):
        members = [o for o in objects if o.wave == number]
        waves.append({
            "wave": number,
            "count": len(members),
            "automated": sum(1 for o in members if strategies[o.id] == AUTOMATED),
            "types": dict(sorted(Counter(o.type for o in members).items())),
            "effortDays": round(sum(hours[o.id] for o in members) / HOURS_PER_DAY, 2),
        })

    # -- strategy by object type ------------------------------------------
    grouped: Dict[str, List[PlanObject]] = defaultdict(list)
    for o in objects:
        grouped[o.type].append(o)
    type_rows = []
    for type_name, members in sorted(grouped.items(), key=lambda kv: (min(m.wave for m in kv[1]), kv[0])):
        strategy = Counter(strategies[m.id] for m in members).most_common(1)[0][0]
        type_rows.append({
            "type": type_name,
            "count": len(members),
            "strategy": strategy,
            "strategyLabel": STRATEGY_LABELS[strategy],
            "target": next((m.fabric_target for m in members if m.fabric_target), ""),
            "firstWave": min(m.wave for m in members),
            "lastWave": max(m.wave for m in members),
            "effortDays": round(sum(hours[m.id] for m in members) / HOURS_PER_DAY, 2),
        })

    counts = Counter(strategies.values())
    fingerprint = hashlib.sha256(
        "|".join(
            [f"{o.id}@{o.wave}:{strategies[o.id]}" for o in sorted(objects, key=lambda x: x.id)]
            + [f"{r['code']}:{r['severity']}:{','.join(r['objects'])}" for r in risks]
        ).encode("utf-8")
    ).hexdigest()[:12]

    return {
        "plannerVersion": PLANNER_VERSION,
        "fingerprint": fingerprint,
        "readiness": readiness,
        "objects": len(objects),
        "strategyCounts": {k: counts.get(k, 0) for k in (AUTOMATED, MANUAL, ASSESS, LATER, DESELECTED)},
        "effortDays": effort_days,
        "needsReview": {"count": len(review_ids), "effortDays": round(sum(hours[i] for i in review_ids) / HOURS_PER_DAY, 2)},
        "blocking": by_severity.get(BLOCKING, 0),
        "waves": waves,
        "typeStrategies": type_rows,
        "risks": risks,
        "riskCounts": {s: by_severity.get(s, 0) for s in SEVERITIES},
        "checks": [dict(c) for c in checks],
        "objectStrategies": {
            o.id: {"strategy": strategies[o.id], "label": STRATEGY_LABELS[strategies[o.id]], "effortHours": round(hours[o.id], 2)}
            for o in objects
        },
    }
