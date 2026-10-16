"""Conditions: when a question applies, when a recommendation holds, when answers contradict the estate.

A condition is plain data, never code, so a catalog can be read, checked and moved anywhere:

* a literal: a number, a string, true / false / null, or a list of conditions;
* ``{"fact": "tables.withDateColumn"}``: a fact's value; ``{"answer": "C2"}``: a question's value, answered or
  assumed;
* comparisons ``==  !=  >  >=  <  <=`` and ``{"in": [needle, haystack]}`` over two operands;
* ``{"and": [...]}``, ``{"or": [...]}``, ``{"not": c}``; ``{"+": [...]}`` adds numbers; ``{"len": c}`` counts a list.

**An unknown fact (missing, or None) or a question with no value means "does not hold".** Every operation over one is unknown in
turn (``not`` of it too), and a condition holds only when it is plainly true. So a fact the discovery could not
compute never switches a question on, nor a recommendation, nor a contradiction.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Set, Tuple


class _Unknown:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()
_COMPARE = {"==", "!=", ">", ">=", "<", "<="}
_OPS = _COMPARE | {"in", "and", "or", "not", "+", "len", "fact", "answer"}


class ConditionError(ValueError):
    """A condition that is not in the format."""


def _compare(op: str, a: Any, b: Any) -> Any:
    if a is UNKNOWN or b is UNKNOWN:
        return UNKNOWN
    try:
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "<":
            return a < b
        return a <= b
    except TypeError:
        return UNKNOWN  # a number against a string: neither true nor false


def evaluate(condition: Any, facts: Mapping[str, Any], values: Mapping[str, Any]) -> Any:
    """The condition's value: a value, True / False, or ``UNKNOWN``."""
    if isinstance(condition, list):
        items = [evaluate(c, facts, values) for c in condition]
        return UNKNOWN if any(i is UNKNOWN for i in items) else items
    if not isinstance(condition, dict):
        return condition
    if len(condition) != 1:
        raise ConditionError(f"A condition has exactly one operator: {condition!r}")
    op, arg = next(iter(condition.items()))
    if op == "fact":
        found = facts.get(str(arg), UNKNOWN)
        return UNKNOWN if found is None else found  # a fact discovery could not work out is unknown, never a value
    if op == "answer":
        found = values.get(str(arg), UNKNOWN)
        return UNKNOWN if found is None else found
    if op in _COMPARE:
        if not isinstance(arg, list) or len(arg) != 2:
            raise ConditionError(f"'{op}' takes two operands")
        return _compare(op, evaluate(arg[0], facts, values), evaluate(arg[1], facts, values))
    if op == "in":
        if not isinstance(arg, list) or len(arg) != 2:
            raise ConditionError("'in' takes a value and a list")
        needle, haystack = evaluate(arg[0], facts, values), evaluate(arg[1], facts, values)
        if needle is UNKNOWN or haystack is UNKNOWN or not isinstance(haystack, (list, tuple, dict, str)):
            return UNKNOWN
        try:
            return needle in haystack
        except TypeError:
            return UNKNOWN  # a list looked for in a text, a list as a map's key: neither true nor false
    if op in ("and", "or"):
        if not isinstance(arg, list) or not arg:
            raise ConditionError(f"'{op}' takes a list of conditions")
        results = [evaluate(c, facts, values) for c in arg]
        if op == "and":
            if any(r is False for r in results):
                return False
            return UNKNOWN if any(r is UNKNOWN for r in results) else all(bool(r) for r in results)
        if any(r is True for r in results):
            return True
        return UNKNOWN if any(r is UNKNOWN for r in results) else any(bool(r) for r in results)
    if op == "not":
        inner = evaluate(arg, facts, values)
        return UNKNOWN if inner is UNKNOWN else not inner
    if op == "+":
        if not isinstance(arg, list) or not arg:
            raise ConditionError("'+' takes a list of numbers")
        parts = [evaluate(c, facts, values) for c in arg]
        if any(p is UNKNOWN or isinstance(p, bool) or not isinstance(p, (int, float)) for p in parts):
            return UNKNOWN
        return sum(parts)
    if op == "len":
        inner = evaluate(arg, facts, values)
        return len(inner) if isinstance(inner, (list, tuple, dict, str)) else UNKNOWN
    raise ConditionError(f"Not an operator: '{op}'")


def holds(condition: Any, facts: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    """Whether a condition is plainly true. None (no condition) always holds."""
    if condition is None:
        return True
    return evaluate(condition, facts, values) is True


def references(condition: Any) -> Tuple[Set[str], Set[str]]:
    """The facts and the questions a condition reads, checking its shape on the way. Raises ``ConditionError``."""
    facts: Set[str] = set()
    answers: Set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            if node is not None and not isinstance(node, (bool, int, float, str)):
                raise ConditionError(f"Not a literal: {node!r}")
            return
        if len(node) != 1:
            raise ConditionError(f"A condition has exactly one operator: {node!r}")
        op, arg = next(iter(node.items()))
        if op not in _OPS:
            raise ConditionError(f"Not an operator: '{op}'")
        if op == "fact":
            facts.add(str(arg))
        elif op == "answer":
            answers.add(str(arg))
        elif op == "not" or op == "len":
            walk(arg)
        else:
            if not isinstance(arg, list):
                raise ConditionError(f"'{op}' takes a list")
            walk(arg)

    walk(condition)
    return facts, answers


def facts_in(conditions: Iterable[Any]) -> Set[str]:
    out: Set[str] = set()
    for c in conditions:
        out |= references(c)[0]
    return out


FACT_TOKEN = re.compile(r"\{fact:([A-Za-z0-9_.]+)\}")
ANSWER_TOKEN = re.compile(r"\{answer:([A-Za-z0-9_]+)\}")


def template_references(template: Any) -> Tuple[Set[str], Set[str]]:
    """The facts and questions a reason or a default's template quotes."""
    if not isinstance(template, str):
        return set(), set()
    return set(FACT_TOKEN.findall(template)), set(ANSWER_TOKEN.findall(template))


def render(template: str, facts: Mapping[str, Any], values: Mapping[str, Any]) -> str:
    """A reason or a default with ``{fact:name}`` and ``{answer:id}`` filled in: what a person reads as evidence."""
    out, i = [], 0
    while i < len(template):
        start = template.find("{", i)
        if start < 0:
            out.append(template[i:])
            break
        end = template.find("}", start)
        if end < 0:
            out.append(template[i:])
            break
        out.append(template[i:start])
        token = template[start + 1:end]
        kind, _, name = token.partition(":")
        if kind == "fact":
            value = facts.get(name, UNKNOWN)
        elif kind == "answer":
            value = values.get(name, UNKNOWN)
        else:
            out.append("{" + token + "}")
            i = end + 1
            continue
        out.append(_show(value))
        i = end + 1
    return "".join(out)


def _show(value: Any) -> str:
    if value is UNKNOWN or value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_show(v) for v in value) or "none"
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in value.items()) or "none"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:,}" if isinstance(value, int) else str(value)


def template_value(value: Any, facts: Mapping[str, Any], values: Mapping[str, Any]) -> Any:
    """A default's value: a string with ``{fact:...}`` / ``{answer:...}`` in it is rendered; anything else is as given."""
    if isinstance(value, str) and "{" in value:
        return render(value, facts, values)
    return value
