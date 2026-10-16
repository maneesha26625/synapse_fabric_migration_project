"""The question catalog: declarative, versioned, and checked when it loads.

``catalog.json`` holds every question, its options, when it applies, what it recommends (ordered rules, each
with a reason that quotes facts), the settings it drives, and the contradictions between answers and the
estate. Loading checks that:

* every ``drives`` setting, and every setting an option ``sets``, exists in ``settings`` and fits the question;
* every fact a condition, a reason or an evidence list names exists in ``facts``;
* conditions use only known operators, and read only questions that come **earlier** in the catalog, so the
  engine evaluates them in one pass, never in a loop;
* every contradiction has a resolution a person can always take.

A catalog that fails any of these is refused, whole: the accelerator never runs on half a catalog.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from discovery_agent.consult import conditions as cond
from discovery_agent.consult.facts import FACTS
from discovery_agent.consult.settings import BOOL, CHOICE, DATE, LIST, MAP, NUMBER, SETTINGS, TEXT

PATH = Path(__file__).with_name("catalog.json")
TYPES = ("choice", "multi", "boolean", "number", "date", "text", "list", "name", "principalMap")
#: The setting kind each question type feeds when an option sets nothing itself.
_FEEDS = {"choice": CHOICE, "multi": LIST, "boolean": BOOL, "number": NUMBER, "date": DATE, "text": TEXT, "list": LIST,
          "name": TEXT, "principalMap": MAP}
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,122}$")
MAX_TEXT, MAX_LIST, MAX_ITEM = 2000, 50, 120


class CatalogError(ValueError):
    """The catalog is not valid; the message lists every problem."""


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    description: str = ""
    sets: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DefaultRule:
    value: Any
    reason: str
    when: Any = None


@dataclass(frozen=True)
class Approver:
    """An answer that asks for an approver other than the person who submitted the brief, and why."""

    when: Any
    reason: str


@dataclass(frozen=True)
class Question:
    id: str
    section: str
    type: str
    text: str
    help: str = ""
    options: Tuple[Option, ...] = ()
    options_from: Optional[str] = None
    rows_from: Optional[str] = None
    applies_when: Any = None
    defaults: Tuple[DefaultRule, ...] = ()
    drives: Tuple[str, ...] = ()
    required: bool = False
    optional: bool = False
    evidence: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    max_length: int = MAX_TEXT
    #: When this answer asks for a separate approver (a decision that loosens a safeguard), or None.
    separate_approver: Optional[Approver] = None

    def option(self, value: Any) -> Optional[Option]:
        return next((o for o in self.options if o.value == value), None)

    def choices(self, facts: Mapping[str, Any]) -> List[str]:
        """The values a choice or a multi offers: its own options, or a fact's list."""
        if self.options_from:
            listed = facts.get(self.options_from)
            return [str(v) for v in listed] if isinstance(listed, list) else []
        return [o.value for o in self.options]

    def check(self, value: Any, facts: Mapping[str, Any]) -> Any:
        """The answer, cleaned, or ``ValueError`` with what is wrong, in words a person reads."""
        kind = self.type
        if kind == "choice":
            if value not in self.choices(facts):
                raise ValueError(f"Choose one of: {', '.join(self.choices(facts))}.")
            return value
        if kind == "multi":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError("Choose from the list.")
            allowed = self.choices(facts)
            unknown = [v for v in value if v not in allowed]
            if unknown:
                raise ValueError(f"Not one of the choices: {', '.join(unknown[:5])}.")
            return [v for v in allowed if v in set(value)]  # catalog order, no repeats
        if kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError("Answer yes or no.")
            return value
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Enter a number.")
            if self.minimum is not None and value < self.minimum or self.maximum is not None and value > self.maximum:
                raise ValueError(f"Enter a number from {self.minimum:g} to {self.maximum:g}.")
            return int(value) if float(value).is_integer() else float(value)
        if kind == "date":
            try:
                return _dt.date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise ValueError("Enter a date as YYYY-MM-DD.") from None
        if kind == "text":
            if not isinstance(value, str):
                raise ValueError("Enter text.")
            text = value.strip()
            if len(text) > self.max_length:
                raise ValueError(f"At most {self.max_length:,} characters.")
            return text
        if kind == "list":
            items = [v for v in re.split(r"[,\n]", value)] if isinstance(value, str) else value
            if not isinstance(items, list) or not all(isinstance(v, str) for v in items):
                raise ValueError("Enter a list of names or patterns.")
            clean = list(dict.fromkeys(v.strip() for v in items if v.strip()))
            if len(clean) > MAX_LIST or any(len(v) > MAX_ITEM for v in clean):
                raise ValueError(f"At most {MAX_LIST} entries of {MAX_ITEM} characters.")
            return clean
        if kind == "name":
            if not isinstance(value, str) or not _NAME.match(value.strip()):
                raise ValueError("Letters, digits and underscores, starting with a letter.")
            return value.strip()
        if kind == "principalMap":
            if not isinstance(value, dict):
                raise ValueError("Enter a Microsoft Entra user or group for each SQL user, or leave it blank.")
            rows = {str(r).lower(): str(r) for r in (facts.get(self.rows_from) or [])} if self.rows_from else {}
            out: Dict[str, str] = {}
            for key, target in value.items():
                name = rows.get(str(key).lower())
                if name is None:
                    raise ValueError(f"{key} is not one of the SQL users discovered.")
                if not isinstance(target, str):
                    raise ValueError(f"Enter a Microsoft Entra user or group for {name}.")
                principal = " ".join(target.split())
                if not principal:
                    continue  # blank: left for a person
                if len(principal) > 256 or any(c in principal for c in "[]"):
                    raise ValueError(f"{principal[:40]} is not a Microsoft Entra user or group name.")
                out[name] = principal
            return dict(sorted(out.items()))
        raise ValueError(f"Not a question type: {kind}")


@dataclass(frozen=True)
class Resolution:
    kind: str  # take | keep | change
    label: str
    question: Optional[str] = None
    value: Any = None
    add: Optional[str] = None


@dataclass(frozen=True)
class Conflict:
    id: str
    when: Any
    message: str
    questions: Tuple[str, ...] = ()
    evidence: Tuple[str, ...] = ()
    resolutions: Tuple[Resolution, ...] = ()


@dataclass(frozen=True)
class Catalog:
    version: str
    sections: Tuple[Tuple[str, str], ...]
    questions: Tuple[Question, ...]
    conflicts: Tuple[Conflict, ...]

    def question(self, question_id: str) -> Optional[Question]:
        return next((q for q in self.questions if q.id == question_id), None)

    def conflict(self, conflict_id: str) -> Optional[Conflict]:
        return next((c for c in self.conflicts if c.id == conflict_id), None)

    def describe(self) -> Dict[str, Any]:
        """The catalog as the page reads it."""
        return {
            "version": self.version,
            "sections": [{"id": s, "title": t} for s, t in self.sections],
            "questions": [{
                "id": q.id, "section": q.section, "type": q.type, "text": q.text, "help": q.help,
                "options": [{"value": o.value, "label": o.label, "description": o.description} for o in q.options],
                "optionsFrom": q.options_from, "rowsFrom": q.rows_from, "required": q.required, "optional": q.optional,
                "drives": [{"setting": s, "status": SETTINGS[s].status, "applies": SETTINGS[s].applies} for s in q.drives],
                "min": q.minimum, "max": q.maximum, "maxLength": q.max_length,
                "separateApprover": q.separate_approver.reason if q.separate_approver else None,
            } for q in self.questions],
            "conflicts": [{"id": c.id, "questions": list(c.questions),
                           "resolutions": [{"kind": r.kind, "label": r.label, "question": r.question} for r in c.resolutions]}
                          for c in self.conflicts],
        }


# -- loading --------------------------------------------------------------------------------------------

def _tuple(raw: Any) -> Tuple[Any, ...]:
    return tuple(raw) if isinstance(raw, list) else ()


def parse(data: Mapping[str, Any]) -> Catalog:
    """The catalog in ``data``, checked. Raises ``CatalogError`` listing every problem."""
    problems: List[str] = []
    sections = tuple((str(s.get("id")), str(s.get("title") or s.get("id"))) for s in data.get("sections") or [])
    questions: List[Question] = []
    for raw in data.get("questions") or []:
        try:
            questions.append(Question(
                id=str(raw["id"]), section=str(raw.get("section") or ""), type=str(raw.get("type") or ""),
                text=str(raw.get("text") or ""), help=str(raw.get("help") or ""),
                options=tuple(Option(str(o["value"]), str(o.get("label") or o["value"]), str(o.get("description") or ""),
                                     dict(o.get("sets") or {})) for o in raw.get("options") or []),
                options_from=raw.get("optionsFrom"), rows_from=raw.get("rowsFrom"), applies_when=raw.get("appliesWhen"),
                defaults=tuple(DefaultRule(d.get("value"), str(d.get("reason") or ""), d.get("when")) for d in raw.get("defaults") or []),
                drives=tuple(str(s) for s in raw.get("drives") or []), required=bool(raw.get("required")),
                optional=bool(raw.get("optional")), evidence=tuple(str(f) for f in raw.get("evidence") or []),
                minimum=raw.get("min"), maximum=raw.get("max"), max_length=int(raw.get("maxLength") or MAX_TEXT),
                separate_approver=(Approver(raw["separateApprover"].get("when"), str(raw["separateApprover"].get("reason") or ""))
                                   if isinstance(raw.get("separateApprover"), dict) else None),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"A question cannot be read ({exc}): {str(raw)[:80]}")
    conflicts: List[Conflict] = []
    for raw in data.get("conflicts") or []:
        try:
            conflicts.append(Conflict(
                id=str(raw["id"]), when=raw.get("when"), message=str(raw.get("message") or ""),
                questions=tuple(str(q) for q in raw.get("questions") or []), evidence=tuple(str(f) for f in raw.get("evidence") or []),
                resolutions=tuple(Resolution(str(r.get("kind")), str(r.get("label") or ""), r.get("question"), r.get("value"),
                                             r.get("add")) for r in raw.get("resolutions") or []),
            ))
        except (KeyError, TypeError) as exc:
            problems.append(f"A contradiction cannot be read ({exc}): {str(raw)[:80]}")
    catalog = Catalog(str(data.get("version") or ""), sections, tuple(questions), tuple(conflicts))
    problems += check(catalog)
    if problems:
        raise CatalogError("The consultation catalog is not valid:\n- " + "\n- ".join(problems))
    return catalog


def _facts_named(text: Any) -> List[str]:
    return sorted(cond.template_references(text)[0])


def check(catalog: Catalog) -> List[str]:
    """Every problem with a catalog, in words; empty when it is valid."""
    problems: List[str] = []
    if not catalog.version:
        problems.append("The catalog has no version.")
    section_ids = [s for s, _ in catalog.sections]
    if len(set(section_ids)) != len(section_ids):
        problems.append("Two sections share an id.")
    seen: Dict[str, int] = {}
    for index, q in enumerate(catalog.questions):
        where = f"Question {q.id}"
        if q.id in seen:
            problems.append(f"{where} appears twice.")
        seen[q.id] = index
        if q.section not in section_ids:
            problems.append(f"{where}: no section '{q.section}'.")
        if q.type not in TYPES:
            problems.append(f"{where}: no question type '{q.type}'.")
            continue
        if not q.text:
            problems.append(f"{where} has no text.")
        if q.required and q.optional:
            problems.append(f"{where} cannot be both required and optional.")
        if q.type in ("choice", "multi") and not q.options and not q.options_from:
            problems.append(f"{where} offers no options.")
        if q.type == "choice" and q.options_from:
            problems.append(f"{where}: only a multi question lists its options from a fact.")
        if len({o.value for o in q.options}) != len(q.options):
            problems.append(f"{where}: two options share a value.")
        for name in (q.options_from, q.rows_from):
            if name is not None and name not in FACTS:
                problems.append(f"{where}: no fact '{name}'.")
        if q.type == "principalMap" and not q.rows_from:
            problems.append(f"{where}: a principal map needs rowsFrom.")
        if q.type == "number" and (q.minimum is None or q.maximum is None):
            problems.append(f"{where}: a number needs min and max.")
        if not q.drives:
            problems.append(f"{where} drives no setting.")
        for name in q.drives:
            setting = SETTINGS.get(name)
            if setting is None:
                problems.append(f"{where}: no setting '{name}'.")
                continue
            sets_it = [o for o in q.options if name in o.sets]
            if sets_it:
                if len(sets_it) != len(q.options):
                    problems.append(f"{where}: some options set '{name}' and some do not.")
                continue
            if _FEEDS[q.type] != setting.kind:
                problems.append(f"{where}: a {q.type} answer cannot feed '{name}' ({setting.kind}).")
            elif setting.values and q.type in ("choice", "multi") and not q.options_from:
                outside = [o.value for o in q.options if o.value not in setting.values]
                if outside:
                    problems.append(f"{where}: options {outside} are not values of '{name}'.")
        for o in q.options:
            for name, value in o.sets.items():
                if name not in q.drives:
                    problems.append(f"{where}: option '{o.value}' sets '{name}', which the question does not drive.")
                elif name in SETTINGS and SETTINGS[name].values and value not in SETTINGS[name].values:
                    problems.append(f"{where}: option '{o.value}' sets '{name}' to a value it does not take.")
        conditions = [q.applies_when] + [d.when for d in q.defaults]
        for c in conditions:
            if c is None:
                continue
            try:
                facts, answers = cond.references(c)
            except cond.ConditionError as exc:
                problems.append(f"{where}: {exc}.")
                continue
            for f in facts - set(FACTS):
                problems.append(f"{where}: no fact '{f}'.")
            for a in answers:
                if seen.get(a, len(catalog.questions)) >= index:
                    problems.append(f"{where} reads {a}, which does not come before it.")
        for d in q.defaults:
            if not d.reason:
                problems.append(f"{where}: a recommendation without a reason.")
            for f in _facts_named(d.reason) + _facts_named(d.value):
                if f not in FACTS:
                    problems.append(f"{where}: no fact '{f}' in a recommendation.")
            for a in sorted(cond.template_references(d.value)[1] | cond.template_references(d.reason)[1]):
                if seen.get(a, len(catalog.questions)) >= index:
                    problems.append(f"{where}'s recommendation reads {a}, which does not come before it.")
            if not (isinstance(d.value, str) and "{" in d.value) and not q.options_from and q.type != "principalMap":
                try:
                    q.check(d.value, {})
                except ValueError as exc:
                    problems.append(f"{where}: a recommendation that is not a valid answer ({exc}).")
        for f in q.evidence:
            if f not in FACTS:
                problems.append(f"{where}: no fact '{f}' in its evidence.")
    ids = set(seen)
    for q in catalog.questions:
        if q.separate_approver is None:
            continue
        # Read once every answer is known, so it may read any question, its own included.
        if not q.separate_approver.reason:
            problems.append(f"Question {q.id}: a separate approver without a reason.")
        try:
            facts, answers = cond.references(q.separate_approver.when)
        except cond.ConditionError as exc:
            problems.append(f"Question {q.id}: {exc}.")
            continue
        for f in facts - set(FACTS):
            problems.append(f"Question {q.id}: no fact '{f}'.")
        for a in answers - ids:
            problems.append(f"Question {q.id}: its separate approver reads no question '{a}'.")
    conflict_ids = [c.id for c in catalog.conflicts]
    if len(set(conflict_ids)) != len(conflict_ids):
        problems.append("Two contradictions share an id.")
    for c in catalog.conflicts:
        where = f"Contradiction {c.id}"
        try:
            facts, answers = cond.references(c.when)
        except cond.ConditionError as exc:
            problems.append(f"{where}: {exc}.")
            facts, answers = set(), set()
        for f in (facts | set(c.evidence) | set(_facts_named(c.message))) - set(FACTS):
            problems.append(f"{where}: no fact '{f}'.")
        for a in (answers | set(c.questions)) - ids:
            problems.append(f"{where}: no question '{a}'.")
        if not c.message:
            problems.append(f"{where} has no message.")
        if not any(r.kind in ("take", "keep") for r in c.resolutions):
            problems.append(f"{where} has no resolution a person can take.")
        for r in c.resolutions:
            if r.kind not in ("take", "keep", "change"):
                problems.append(f"{where}: no resolution kind '{r.kind}'.")
            if r.kind in ("take", "change"):
                target = catalog.question(str(r.question))
                if target is None:
                    problems.append(f"{where}: a resolution for no question ('{r.question}').")
                    continue
                if r.kind == "take":
                    if r.add is not None:
                        if target.type != "multi" or r.add not in target.choices({}):
                            problems.append(f"{where}: cannot add '{r.add}' to {target.id}.")
                    else:
                        try:
                            target.check(r.value, {})
                        except ValueError as exc:
                            problems.append(f"{where}: its finding is not a valid answer to {target.id} ({exc}).")
    return problems


@lru_cache(maxsize=4)
def _load(path: str) -> Catalog:
    return parse(json.loads(Path(path).read_text(encoding="utf-8")))


def load(path: Optional[Path] = None) -> Catalog:
    """The catalog the accelerator runs on, read and checked once."""
    return _load(str(path or PATH))
