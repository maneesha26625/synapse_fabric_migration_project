"""Rule-based T-SQL rewrites: dedicated-pool code -> code a Fabric Warehouse accepts.

Applied to view and procedure definitions and to SQL scripts before they are
created in Fabric. Each rule is mechanical and safe to repeat:

* storage options on ``CREATE TABLE`` / CTAS (``WITH (DISTRIBUTION = ...,
  CLUSTERED COLUMNSTORE INDEX | HEAP | CLUSTERED INDEX (...), PARTITION (...))``)
  are removed: Fabric manages storage itself and rejects them;
* ``RENAME OBJECT [schema.]old TO new`` becomes ``EXEC sp_rename``;
* ``SET TRANSACTION ISOLATION LEVEL`` (other than SNAPSHOT) is removed: a
  Warehouse runs every transaction under snapshot isolation;
* workload management (``CREATE / ALTER / DROP WORKLOAD GROUP | CLASSIFIER``,
  resource-class role changes such as ``sp_addrolemember 'largerc', ...``) is
  removed: Fabric has no such concept.

A removed statement is replaced by a block comment saying what was removed, so
the definition still reads correctly and the change is visible in Fabric.
Strings, quoted identifiers and comments are never rewritten: the rules run on
a masked copy of the text in which they are blanked out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

_STORAGE_OPTION = re.compile(
    r"^\s*(DISTRIBUTION\s*=.*|CLUSTERED\s+COLUMNSTORE\s+INDEX(\s+ORDER\s*\(.*\))?|HEAP|CLUSTERED\s+INDEX\s*\(.*\)|PARTITION\s*\(.*\))\s*$",
    re.IGNORECASE | re.DOTALL)
_WITH_PAREN = re.compile(r"\bWITH\s*\(", re.IGNORECASE)
_NAME_PART = r"(?:\[[^\]]+\]|[A-Za-z_#@][\w$#@]*)"
_RENAME = re.compile(
    rf"\bRENAME\s+OBJECT\s+(?:OBJECT\s*::\s*)?((?:{_NAME_PART}\s*\.\s*){{0,2}}{_NAME_PART})\s+TO\s+({_NAME_PART})",
    re.IGNORECASE)
_ISOLATION = re.compile(r"\bSET\s+TRANSACTION\s+ISOLATION\s+LEVEL\s+(READ\s+UNCOMMITTED|READ\s+COMMITTED|REPEATABLE\s+READ|SERIALIZABLE|SNAPSHOT)\s*;?",
                        re.IGNORECASE)
_WORKLOAD = re.compile(rf"\b(CREATE|ALTER|DROP)\s+WORKLOAD\s+(GROUP|CLASSIFIER)\s+{_NAME_PART}", re.IGNORECASE)
_RESOURCE_CLASS = re.compile(
    r"\bEXEC(?:UTE)?\s+(?:sys\.)?sp_(?:add|drop)rolemember\s+N?'(?:small|medium|large|xlarge)rc'\s*,\s*N?'[^']*'\s*;?"
    r"|\bEXEC(?:UTE)?\s+(?:sys\.)?sp_(?:add|drop)rolemember\s+N?'staticrc\d+'\s*,\s*N?'[^']*'\s*;?",
    re.IGNORECASE)


@dataclass
class Rewrite:
    text: str
    notes: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.notes)


def _mask(sql: str) -> str:
    """The text with strings, [identifiers], "identifiers" and comments blanked to spaces (same length)."""
    out = list(sql)
    i, n = 0, len(sql)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] not in "\r\n":
                out[k] = " "

    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif ch in "'[\"":
            close = {"'": "'", "[": "]", '"': '"'}[ch]
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:  # doubled quote inside a string
                        j += 2
                        continue
                    if close == "]" and j + 1 < n and sql[j + 1] == "]":
                        j += 2
                        continue
                    break
                j += 1
            blank(i + 1, j)  # keep the delimiters so identifiers stay visible as tokens
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _balanced(masked: str, open_at: int) -> Optional[int]:
    """Index just past the parenthesis that closes the one at ``open_at``."""
    depth = 0
    for k in range(open_at, len(masked)):
        if masked[k] == "(":
            depth += 1
        elif masked[k] == ")":
            depth -= 1
            if depth == 0:
                return k + 1
    return None


def _split_top(text: str) -> List[str]:
    parts, depth, start = [], 0, 0
    for k, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:k])
            start = k + 1
    parts.append(text[start:])
    return parts


def _comment(text: str) -> str:
    return "/* removed for Fabric: " + " ".join(text.split()).replace("*/", "* /") + " */"


def _storage_options(sql: str, notes: List[str]) -> str:
    edits: List[Tuple[int, int, str]] = []
    masked = _mask(sql)
    for m in _WITH_PAREN.finditer(masked):
        open_at = m.end() - 1
        end = _balanced(masked, open_at)
        if end is None:
            continue
        inner = masked[open_at + 1:end - 1]
        options = [p for p in _split_top(inner) if p.strip()]
        if options and all(_STORAGE_OPTION.match(p) for p in options):
            edits.append((m.start(), end, _comment(sql[m.start():end])))
    if edits:
        notes.append(f"Removed {len(edits)} table storage option clause{'s' if len(edits) > 1 else ''} "
                     "(DISTRIBUTION, columnstore, heap, partitions): Fabric manages storage itself.")
    return _apply(sql, edits)


def _apply(sql: str, edits: List[Tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(edits, reverse=True):
        sql = sql[:start] + replacement + sql[end:]
    return sql


def _unbracket(name: str) -> str:
    name = name.strip()
    return name[1:-1].replace("]]", "]") if name.startswith("[") and name.endswith("]") else name


def _renames(sql: str, notes: List[str]) -> str:
    masked = _mask(sql)
    edits = []
    for m in _RENAME.finditer(masked):
        original = sql[m.start(1):m.end(1)]
        parts = [p.strip() for p in re.split(r"\s*\.\s*", original)]
        two_part = ".".join(parts[-2:])  # sp_rename takes [schema.]object; a database prefix is dropped
        new_name = _unbracket(sql[m.start(2):m.end(2)])
        literal = lambda s: "N'" + s.replace("'", "''") + "'"  # noqa: E731
        edits.append((m.start(), m.end(), f"EXEC sp_rename {literal(two_part)}, {literal(new_name)}"))
    if edits:
        notes.append(f"Rewrote {len(edits)} RENAME OBJECT statement{'s' if len(edits) > 1 else ''} as sp_rename.")
    return _apply(sql, edits)


def _isolation(sql: str, notes: List[str]) -> str:
    masked = _mask(sql)
    edits = [(m.start(), m.end(), _comment(sql[m.start():m.end()]))
             for m in _ISOLATION.finditer(masked) if not m.group(1).upper().startswith("SNAPSHOT")]
    if edits:
        notes.append("Removed SET TRANSACTION ISOLATION LEVEL: a Fabric Warehouse runs every transaction under snapshot isolation.")
    return _apply(sql, edits)


def _workload(sql: str, notes: List[str]) -> str:
    masked = _mask(sql)
    edits: List[Tuple[int, int, str]] = []
    for m in _WORKLOAD.finditer(masked):
        end = k = m.end()
        while k < len(masked) and masked[k].isspace():
            k += 1
        rest = _WITH_PAREN.match(masked, k)
        if rest:
            closed = _balanced(masked, rest.end() - 1)
            end = closed if closed else end
        if end < len(masked) and masked[end:end + 1] == ";":
            end += 1
        edits.append((m.start(), end, _comment(sql[m.start():end])))
    for m in _RESOURCE_CLASS.finditer(sql):  # the role names are string literals, so match the original text
        if masked[m.start():m.start() + 4].upper() == "EXEC":
            edits.append((m.start(), m.end(), _comment(sql[m.start():m.end()])))
    if edits:
        notes.append("Removed workload management (workload groups, classifiers or resource classes): Fabric manages compute itself.")
    return _apply(sql, edits)


def rewrite(sql: Optional[str]) -> Rewrite:
    """Apply every rule. The notes say what changed; an unchanged text has no notes."""
    if not sql:
        return Rewrite(sql or "")
    notes: List[str] = []
    text = _storage_options(sql, notes)
    text = _renames(text, notes)
    text = _isolation(text, notes)
    text = _workload(text, notes)
    return Rewrite(text, notes)
