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
  removed: Fabric has no such concept;
* ``COPY INTO ... CREDENTIAL = (IDENTITY = 'Managed Identity')`` loses the credential: a Warehouse does
  not take Synapse's managed identity, and without a credential COPY reads the storage as the
  Microsoft Entra identity that runs it;
* ``SET IDENTITY_INSERT <table> ON | OFF`` is kept: tables keep IDENTITY in Fabric, which supports
  the statement. It is removed only for tables the caller names as having no IDENTITY in the
  Warehouse (``without_identity``: tables created before IDENTITY was kept), where Fabric rejects it.

The last two also apply inside string literals, because they are usually built as dynamic SQL
(``SET @sql = 'COPY INTO ...'; EXEC (@sql)``).

A removed statement is replaced by a block comment saying what was removed, so
the definition still reads correctly and the change is visible in Fabric.
Strings, quoted identifiers and comments are never rewritten: the rules run on
a masked copy of the text in which they are blanked out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set, Tuple

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


_IDENTITY_INSERT = re.compile(rf"\bSET\s+IDENTITY_INSERT\s+(?:{_NAME_PART}\s*\.\s*){{0,2}}{_NAME_PART}\s+(?:ON|OFF)\b[ \t]*;?", re.IGNORECASE)
#: The credential option, with the comma that joins it to the other options. Quotes may be doubled inside dynamic SQL.
_MI_CREDENTIAL = re.compile(r"(?:,\s*CREDENTIAL\s*=\s*\(\s*IDENTITY\s*=\s*N?'{1,2}\s*Managed\s+Identity\s*'{1,2}\s*\)"
                            r"|CREDENTIAL\s*=\s*\(\s*IDENTITY\s*=\s*N?'{1,2}\s*Managed\s+Identity\s*'{1,2}\s*\)\s*,?)", re.IGNORECASE)
_EMPTY_WITH = re.compile(r"\bWITH\s*\(\s*\)", re.IGNORECASE)


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


def _mask_comments(sql: str) -> str:
    """The text with only its comments blanked: rules that must also see dynamic SQL inside strings use it."""
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end < 0 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end < 0 else end + 2
        elif sql[i] == "'":  # skip a string whole, so a "--" inside it is not taken for a comment
            j = i + 1
            while j < n and not (sql[j] == "'" and not sql.startswith("''", j)):
                j += 2 if sql.startswith("''", j) else 1
            i = j + 1
            continue
        else:
            i += 1
            continue
        for k in range(i, end):
            if out[k] not in "\r\n":
                out[k] = " "
        i = end
    return "".join(out)


def _copy_credential(sql: str, notes: List[str]) -> str:
    masked = _mask_comments(sql)
    edits = [(m.start(), m.end(), "") for m in _MI_CREDENTIAL.finditer(masked)]
    if not edits:
        return sql
    text = _apply(sql, edits)
    text = _EMPTY_WITH.sub("", text)
    notes.append(f"Removed CREDENTIAL = (IDENTITY = 'Managed Identity') from {len(edits)} COPY statement{'s' if len(edits) > 1 else ''}: "
                 "a Fabric Warehouse reads the storage as the Microsoft Entra identity that runs the statement, so that identity "
                 "(the user, or the identity running the pipeline) needs Storage Blob Data Reader on the storage account.")
    return text


def _identity_target(statement: str) -> str:
    """The table a SET IDENTITY_INSERT statement names, as ``schema.table`` (or ``table``), unbracketed and lowercased."""
    body = re.sub(r"^\s*SET\s+IDENTITY_INSERT\s+", "", statement, flags=re.IGNORECASE)
    body = re.sub(r"\s+(?:ON|OFF)\b.*$", "", body, flags=re.IGNORECASE | re.S)
    parts = [_unbracket(p) for p in re.split(r"\s*\.\s*", body.strip()) if p.strip()]
    return ".".join(parts[-2:]).lower()


def _identity_insert(sql: str, notes: List[str], without_identity: Optional[Set[str]]) -> str:
    if not without_identity:
        return sql
    masked = _mask_comments(sql)
    edits = []
    for m in _IDENTITY_INSERT.finditer(masked):
        name = _identity_target(sql[m.start():m.end()])
        short = name.split(".")[-1]
        if name in without_identity or (("." not in name) and f"dbo.{short}" in without_identity):
            edits.append((m.start(), m.end(), "/* removed for Fabric: SET IDENTITY_INSERT (the table has no IDENTITY in Fabric) */"))
    if edits:
        notes.append("Removed SET IDENTITY_INSERT for table(s) that have no IDENTITY in the Warehouse, so the explicit key values insert as they are.")
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


_CREATE_MODULE = re.compile(r"\bCREATE\s+(?!OR\s+ALTER\b)(PROC(?:EDURE)?|VIEW|FUNCTION)\b", re.IGNORECASE)
_CREATE_OR_ALTER = re.compile(r"\bCREATE\s+OR\s+ALTER\b", re.IGNORECASE)


def create_or_alter(sql: str) -> Optional[str]:
    """The definition as ``CREATE OR ALTER``, to update a view or procedure in place. None if it has no CREATE.

    Updating in place keeps the old object if the new text is rejected, unlike dropping it first."""
    masked = _mask(sql)
    if _CREATE_OR_ALTER.search(masked):
        return sql
    m = _CREATE_MODULE.search(masked)
    if not m:
        return None
    return sql[:m.start()] + "CREATE OR ALTER " + sql[m.start(1):]


def rewrite(sql: Optional[str], without_identity: Optional[Iterable[str]] = None) -> Rewrite:
    """Apply every rule. The notes say what changed; an unchanged text has no notes.

    ``without_identity``: ``schema.table`` names of Warehouse tables that have no IDENTITY column; a
    ``SET IDENTITY_INSERT`` on one of them is removed, because Fabric rejects it there."""
    if not sql:
        return Rewrite(sql or "")
    lacking = {str(n).lower() for n in without_identity} if without_identity else None
    notes: List[str] = []
    text = _storage_options(sql, notes)
    text = _renames(text, notes)
    text = _isolation(text, notes)
    text = _workload(text, notes)
    text = _copy_credential(text, notes)
    text = _identity_insert(text, notes, lacking)
    return Rewrite(text, notes)
