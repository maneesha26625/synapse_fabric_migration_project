"""The consultation engine: from the catalog, the facts and the answers, where every question stands.

Pure and deterministic: no I/O, no clock. Reading the state never changes anything.

* **Answers are append-only records**: ``answer``, ``skip``, ``withdraw`` and ``resolve``, each with who, when,
  its source (a person, a recommendation they accepted, a finding they took, a proposal they confirmed, an
  import), the catalog version and the evidence it was made against. Withdrawing supersedes; nothing is deleted.
* **Values** are the answers plus the recommendations no person has answered yet (assumed defaults).
  Applicability, recommendations and contradictions are all worked out over these values, in catalog order, so
  an assumed default opens its follow-up questions like an answer would.
* **An answer whose evidence changed is stale**: it needs a person to confirm it again.
* **An answer on a branch that stops applying is parked**, not lost, and revived if the branch applies again.
* **A contradiction** holds when its condition does. It stays open until a person takes the finding (which is an
  answer), or keeps their answers with a reason; a kept one lapses when its evidence or answers change.
* **Next question**: open contradictions first, then required questions, then the rest in catalog order;
  assumed ones, then skipped ones, come last.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from discovery_agent.consult import conditions as cond
from discovery_agent.consult.catalog import Catalog, Conflict, Question
from discovery_agent.consult.facts import FACTS, ESTATE
from discovery_agent.consult.settings import SETTINGS

ANSWERED, ASSUMED, OPEN, SKIPPED, STALE, PARKED, NOT_APPLICABLE = (
    "answered", "assumed", "open", "skipped", "stale", "parked", "not_applicable")
#: Sources of an answer: a person's own, a recommendation they accepted, a contradiction's finding they took, a
#: proposal they confirmed (read from their notes by the model, or by the rules), or an imported questionnaire's.
#: Each is a person's action; only an assumed default is nobody's.
PERSON, RECOMMENDATION, FINDING, AI, RULES, IMPORT = "person", "recommendation", "finding", "ai", "rules", "import"
SOURCES = (PERSON, RECOMMENDATION, FINDING, AI, RULES, IMPORT)


@dataclass
class QuestionState:
    id: str
    applies: bool
    status: str
    value: Any = None
    source: Optional[str] = None
    by: Optional[Dict[str, str]] = None
    at: Optional[str] = None
    note: Optional[str] = None
    recommendation: Optional[Dict[str, Any]] = None
    #: The estate facts the answer was made against, and those that differ now.
    changed: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)

    @property
    def has_value(self) -> bool:
        return self.value is not None


@dataclass
class ConflictState:
    id: str
    status: str  # open | kept
    message: str
    questions: List[str]
    evidence: List[Dict[str, Any]]
    reason: Optional[str] = None
    by: Optional[Dict[str, str]] = None
    at: Optional[str] = None


@dataclass
class State:
    questions: Dict[str, QuestionState]
    order: List[str]
    values: Dict[str, Any]
    conflicts: List[ConflictState]
    next: Optional[str]
    blockers: List[str]

    @property
    def submittable(self) -> bool:
        return not self.blockers

    def question(self, question_id: str) -> QuestionState:
        return self.questions[question_id]

    def applicable(self) -> List[QuestionState]:
        return [self.questions[q] for q in self.order if self.questions[q].applies]

    def progress(self) -> Dict[str, int]:
        live = self.applicable()
        return {
            "applicable": len(live),
            "answered": sum(1 for q in live if q.status == ANSWERED),
            "assumed": sum(1 for q in live if q.status in (ASSUMED, SKIPPED) and q.has_value),
            "needsPerson": len(self.blockers),
            "open": sum(1 for q in live if q.status in (OPEN, STALE)),
            "contradictions": sum(1 for c in self.conflicts if c.status == "open"),
        }


# -- records --------------------------------------------------------------------------------------------

def fold(records: Iterable[Mapping[str, Any]]) -> Tuple[Dict[str, Mapping[str, Any]], Dict[str, Mapping[str, Any]], Dict[str, Mapping[str, Any]]]:
    """The answers, skips and resolutions in force: each the latest of its kind."""
    answers: Dict[str, Mapping[str, Any]] = {}
    skips: Dict[str, Mapping[str, Any]] = {}
    resolutions: Dict[str, Mapping[str, Any]] = {}
    for r in records:
        kind, qid = r.get("type"), str(r.get("question") or "")
        if kind == "answer":
            answers[qid] = r
            skips.pop(qid, None)
        elif kind == "withdraw":
            answers.pop(qid, None)
            skips.pop(qid, None)
        elif kind == "skip":
            skips[qid] = r
        elif kind == "resolve":
            resolutions[str(r.get("conflict") or "")] = r
    return answers, skips, resolutions


def evidence_of(question: Question, facts: Mapping[str, Any]) -> Dict[str, Any]:
    """The estate facts an answer to this question rests on (its ``evidence``), as they are now: kept with the answer,
    so a change to one of them reopens it. Only facts that could change the answer are listed, never mere counts."""
    return {n: facts.get(n) for n in question.evidence if n in FACTS and FACTS[n].scope == ESTATE and not FACTS[n].informational}


def conflict_evidence(conflict: Conflict, facts: Mapping[str, Any]) -> Dict[str, Any]:
    names = set(conflict.evidence) | cond.references(conflict.when)[0]
    return {n: facts.get(n) for n in sorted(names) if n in FACTS}


# -- evaluation -----------------------------------------------------------------------------------------

def _known(template: str, facts: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    named_facts, named_answers = cond.template_references(template)
    return all(facts.get(f) is not None for f in named_facts) and all(values.get(a) is not None for a in named_answers)


def recommendation(question: Question, facts: Mapping[str, Any], values: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The first recommendation whose condition holds, with its reason quoting the facts; None when there is none."""
    for rule in question.defaults:
        if not cond.holds(rule.when, facts, values):
            continue
        if isinstance(rule.value, str) and "{" in rule.value and not _known(rule.value, facts, values):
            return None  # it would quote a fact nobody knows
        value = cond.template_value(rule.value, facts, values)
        try:
            value = question.check(value, facts)
        except ValueError:
            return None  # a rendered recommendation that is not a valid answer is no recommendation
        return {"value": value, "reason": cond.render(rule.reason, facts, values)}
    return None


def evaluate(catalog: Catalog, facts: Mapping[str, Any], records: Iterable[Mapping[str, Any]]) -> State:
    answers, skips, resolutions = fold(records)
    values: Dict[str, Any] = {}
    states: Dict[str, QuestionState] = {}
    order = [q.id for q in catalog.questions]
    for q in catalog.questions:
        applies = cond.holds(q.applies_when, facts, values)
        answer = answers.get(q.id)
        if not applies:
            states[q.id] = QuestionState(q.id, False, PARKED if answer else NOT_APPLICABLE,
                                         value=answer.get("value") if answer else None,
                                         source=answer.get("source") if answer else None)
            continue
        rec = recommendation(q, facts, values)
        if answer is not None:
            kept = answer.get("facts") or {}
            changed = [{"fact": n, "description": FACTS[n].description, "before": kept[n], "after": facts.get(n)}
                       for n in sorted(kept) if n in FACTS and kept[n] != facts.get(n)]
            state = QuestionState(q.id, True, STALE if changed else ANSWERED, value=answer.get("value"),
                                  source=answer.get("source"), by=answer.get("by"), at=answer.get("at"),
                                  note=answer.get("note"), recommendation=rec, changed=changed)
        elif q.id in skips:
            state = QuestionState(q.id, True, SKIPPED, value=rec["value"] if rec else None, recommendation=rec,
                                  by=skips[q.id].get("by"), at=skips[q.id].get("at"))
        elif rec is not None:
            state = QuestionState(q.id, True, ASSUMED, value=rec["value"], recommendation=rec)
        else:
            state = QuestionState(q.id, True, OPEN, recommendation=None)
        if state.value is not None:
            values[q.id] = state.value
        states[q.id] = state

    conflicts: List[ConflictState] = []
    for c in catalog.conflicts:
        if not cond.holds(c.when, facts, values):
            continue
        evidence = conflict_evidence(c, facts)
        involved = {q: values.get(q) for q in c.questions}
        kept = resolutions.get(c.id)
        is_kept = (kept is not None and kept.get("resolution") == "keep" and (kept.get("facts") or {}) == evidence
                   and (kept.get("answers") or {}) == involved)
        conflicts.append(ConflictState(
            c.id, "kept" if is_kept else "open", cond.render(c.message, facts, values), list(c.questions),
            [{"fact": n, "description": FACTS[n].description, "value": v} for n, v in evidence.items()],
            reason=kept.get("reason") if is_kept else None, by=kept.get("by") if is_kept else None,
            at=kept.get("at") if is_kept else None))
        for qid in c.questions:
            if qid in states:
                states[qid].conflicts.append(c.id)

    blockers = _blockers(catalog, states, conflicts)
    return State(states, order, values, conflicts, _next(catalog, states, conflicts), blockers)


def _person(state: QuestionState) -> bool:
    return state.status == ANSWERED and state.source in SOURCES


def _blockers(catalog: Catalog, states: Mapping[str, QuestionState], conflicts: List[ConflictState]) -> List[str]:
    out = [f"Contradiction: {c.message}" for c in conflicts if c.status == "open"]
    for q in catalog.questions:
        s = states[q.id]
        if not s.applies:
            continue
        if s.status == STALE:
            out.append(f"{q.id}: what it rests on changed since it was answered; confirm it again.")
        elif q.required and not _person(s):
            out.append(f"{q.id}: {q.text} A person answers this one.")
        elif s.value is None and not q.optional:
            out.append(f"{q.id}: {q.text} It needs an answer.")
    return out


def _next(catalog: Catalog, states: Mapping[str, QuestionState], conflicts: List[ConflictState]) -> Optional[str]:
    live = [q for q in catalog.questions if states[q.id].applies]
    for c in conflicts:
        if c.status == "open":
            for q in live:
                if q.id in c.questions:
                    return q.id
    tiers = (
        lambda q, s: q.required and not _person(s),
        lambda q, s: s.status == OPEN and not q.optional,
        lambda q, s: s.status == STALE,
        lambda q, s: s.status == ASSUMED,
        lambda q, s: s.status == SKIPPED,
    )
    for tier in tiers:
        for q in live:
            if tier(q, states[q.id]):
                return q.id
    return None


# -- accepting recommendations ------------------------------------------------------------------------------

def acceptable(catalog: Catalog, state: State, section: Optional[str] = None,
               questions: Optional[Iterable[str]] = None) -> List[Tuple[Question, Dict[str, Any]]]:
    """The questions whose recommendation a person can accept now (assumed or skipped, with one), in catalog order,
    each with that recommendation: what *Accept all recommendations* answers."""
    wanted = set(questions) if questions is not None else None
    out = []
    for q in catalog.questions:
        s = state.questions[q.id]
        if not s.applies or s.status not in (ASSUMED, SKIPPED) or s.recommendation is None:
            continue
        if (section is not None and q.section != section) or (wanted is not None and q.id not in wanted):
            continue
        out.append((q, s.recommendation))
    return out


def accept_all_problems(catalog: Catalog, facts_sets: Iterable[Mapping[str, Any]]) -> List[str]:
    """Whether accepting every recommendation gives a brief that can be submitted, on each set of facts: every
    blocker left over is a problem. (A contradiction with the estate, such as a discovery that could not read
    everything, is left for a person on purpose: give such facts only to see it named.)"""
    problems: List[str] = []
    for n, facts in enumerate(facts_sets, start=1):
        records: List[Dict[str, Any]] = []
        for _ in range(len(catalog.questions)):  # accepting one can open the next; each pass accepts what applies now
            state = evaluate(catalog, facts, records)
            found = acceptable(catalog, state)
            if not found:
                break
            records += [{"type": "answer", "question": q.id, "value": rec["value"], "source": RECOMMENDATION,
                         "facts": evidence_of(q, facts)} for q, rec in found]
        state = evaluate(catalog, facts, records)
        for blocker in state.blockers:
            problems.append(f"Accepting every recommendation on facts set {n} leaves: {blocker}")
    return problems


# -- what a change would do -------------------------------------------------------------------------------

def settings_of(catalog: Catalog, state: State) -> Dict[str, Any]:
    """The settings the values imply: what an approved brief makes every consumer do."""
    out: Dict[str, Any] = {}
    for q in catalog.questions:
        s = state.questions[q.id]
        if not s.applies or s.value is None:
            continue
        option = q.option(s.value) if q.type == "choice" else None
        for name in q.drives:
            out[name] = option.sets[name] if option is not None and name in option.sets else s.value
    return {k: out[k] for k in sorted(out) if k in SETTINGS}


def preview(catalog: Catalog, facts: Mapping[str, Any], records: List[Mapping[str, Any]], record: Mapping[str, Any]) -> Dict[str, Any]:
    """What recording ``record`` would change, without recording it: questions it opens, parks or revives,
    contradictions it opens or closes, and the settings that move."""
    before, after = evaluate(catalog, facts, records), evaluate(catalog, facts, list(records) + [record])
    opens, parks, revives = [], [], []
    for qid in before.order:
        b, a = before.questions[qid], after.questions[qid]
        if not b.applies and a.applies:
            (revives if b.status == PARKED else opens).append(qid)
        elif b.applies and not a.applies and a.status == PARKED:
            parks.append(qid)
    was = {c.id for c in before.conflicts if c.status == "open"}
    now = {c.id for c in after.conflicts if c.status == "open"}
    old, new = settings_of(catalog, before), settings_of(catalog, after)
    moved = [{"setting": k, "before": old.get(k), "after": new.get(k)} for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)]
    return {"opens": opens, "parks": parks, "revives": revives, "conflictsOpened": sorted(now - was),
            "conflictsClosed": sorted(was - now), "settings": moved}
