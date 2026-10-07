"""Deterministic structural scanning of T-SQL text.

Not a SQL parser, and not trying to be. It finds the constructs that matter
for migration by matching statement keywords, and is explicit about what it
cannot know:

* **A `FROM` target's kind is genuinely unknowable.** ``FROM dbo.Sales`` could
  be a table, a view, or a synonym. Only a DDL keyword (``CREATE VIEW``,
  ``DROP TABLE``) settles it, so everything else is reported as UNKNOWN rather
  than guessed.
* **Dynamic SQL is recorded, never guessed.** ``EXEC(@sql)`` generally has no
  static answer; claiming one would be worse than admitting the gap.
  ``resolve_dynamic_sql`` reads only the provable case: SQL assembled from
  literals whose runtime parts are values, never names.
* **An interpolated name is reported, never completed.** A Synapse expression
  standing in for an identifier — ``TRUNCATE TABLE @{...}.TripsData`` — yields
  an INTERPOLATED reference carrying the name exactly as written. The
  dependency is real and must not vanish; which object it resolves to is not
  this stage's to invent. Only a whole name part is recognised this way; an
  expression spliced into the middle of an identifier is not.

Before anything is matched, comments and string literals are blanked out —
replaced by spaces so line numbers survive. Without that, a commented-out
``-- FROM secret_table`` becomes a phantom dependency.

Shared rather than embedded in the SQL script extractor: the pipeline
extractor already captures embedded SQL it does not yet analyse, and the
future SQL-source extractors will need exactly this.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

from discovery_agent.extractors.common_models import SecretKind, SecretReference
from discovery_agent.extractors.sql_models import (
    DynamicSqlSite,
    SqlFeature,
    SqlFeatureKind,
    SqlObjectKind,
    SqlObjectReference,
    SqlOperation,
)

EVIDENCE_MAX_CHARS = 200
REDACTED = "***"

# A Synapse expression interpolated into the SQL text. It stands where an
# identifier would, and what it names is only decided at runtime.
_INTERPOLATION = r"@\{[^{}]*\}"
_INTERPOLATION_AT = re.compile(_INTERPOLATION)

# A bracketed, quoted, interpolated or bare identifier, optionally qualified
# up to four parts. The interpolation alternative is listed before the bare
# one so that ``@{pipeline().parameters.Schema}`` is captured whole, instead
# of the bare ``@`` that the trailing alternative would stop at.
_IDENT = r'(?:\[[^\]]+\]|"[^"]+"|{0}|[A-Za-z_#@][\w$#@]*)'.format(_INTERPOLATION)
_QUALIFIED = r"{0}(?:\s*\.\s*{0}){{0,3}}".format(_IDENT)

# Function-like constructs that follow FROM but are not objects.
_NOT_OBJECTS = frozenset(
    {
        "openrowset", "openquery", "opendatasource", "openxml", "openjson",
        "string_split", "generate_series", "values", "select", "lateral",
        # COPY INTO t FROM 'path' WITH (...): with the path blanked, the
        # options clause is what follows FROM.
        "with",
    }
)

_QUOTED_LITERAL = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)

_CTE = re.compile(r"\bWITH\s+({0})\s+AS\s*\(".format(_IDENT), re.I)
_CTE_CHAIN = re.compile(r",\s*({0})\s+AS\s*\(".format(_IDENT), re.I)

# Ordered most specific first. The first pattern to claim an identifier wins,
# so DELETE FROM is not also reported as a bare FROM.
_OBJECT_PATTERNS: Tuple[Tuple[str, SqlOperation, SqlObjectKind, str], ...] = (
    (r"\bCREATE\s+EXTERNAL\s+TABLE\s+({0})", SqlOperation.CREATE, SqlObjectKind.EXTERNAL_TABLE, "CREATE EXTERNAL TABLE"),
    (r"\bCREATE\s+(?:OR\s+ALTER\s+)?VIEW\s+({0})", SqlOperation.CREATE, SqlObjectKind.VIEW, "CREATE VIEW"),
    (r"\bCREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\s+({0})", SqlOperation.CREATE, SqlObjectKind.PROCEDURE, "CREATE PROCEDURE"),
    (r"\bCREATE\s+(?:OR\s+ALTER\s+)?FUNCTION\s+({0})", SqlOperation.CREATE, SqlObjectKind.FUNCTION, "CREATE FUNCTION"),
    (r"\bCREATE\s+TABLE\s+({0})", SqlOperation.CREATE, SqlObjectKind.TABLE, "CREATE TABLE"),
    (r"\bALTER\s+VIEW\s+({0})", SqlOperation.ALTER, SqlObjectKind.VIEW, "ALTER VIEW"),
    (r"\bALTER\s+PROC(?:EDURE)?\s+({0})", SqlOperation.ALTER, SqlObjectKind.PROCEDURE, "ALTER PROCEDURE"),
    (r"\bALTER\s+TABLE\s+({0})", SqlOperation.ALTER, SqlObjectKind.TABLE, "ALTER TABLE"),
    (r"\bDROP\s+EXTERNAL\s+TABLE\s+({0})", SqlOperation.DROP, SqlObjectKind.EXTERNAL_TABLE, "DROP EXTERNAL TABLE"),
    (r"\bDROP\s+VIEW\s+(?:IF\s+EXISTS\s+)?({0})", SqlOperation.DROP, SqlObjectKind.VIEW, "DROP VIEW"),
    (r"\bDROP\s+PROC(?:EDURE)?\s+(?:IF\s+EXISTS\s+)?({0})", SqlOperation.DROP, SqlObjectKind.PROCEDURE, "DROP PROCEDURE"),
    (r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?({0})", SqlOperation.DROP, SqlObjectKind.TABLE, "DROP TABLE"),
    (r"\bTRUNCATE\s+TABLE\s+({0})", SqlOperation.TRUNCATE, SqlObjectKind.TABLE, "TRUNCATE TABLE"),
    (r"\bCOPY\s+INTO\s+({0})", SqlOperation.WRITE, SqlObjectKind.TABLE, "COPY INTO"),
    (r"\bINSERT\s+INTO\s+({0})", SqlOperation.WRITE, SqlObjectKind.UNKNOWN, "INSERT INTO"),
    (r"\bDELETE\s+FROM\s+({0})", SqlOperation.WRITE, SqlObjectKind.UNKNOWN, "DELETE FROM"),
    (r"\bMERGE\s+(?:INTO\s+)?({0})", SqlOperation.MERGE, SqlObjectKind.UNKNOWN, "MERGE"),
    (r"\bUPDATE\s+({0})", SqlOperation.WRITE, SqlObjectKind.UNKNOWN, "UPDATE"),
    (r"\bEXEC(?:UTE)?\s+({0})\b(?!\s*\()", SqlOperation.EXECUTE, SqlObjectKind.PROCEDURE, "EXECUTE"),
    (r"\b(?:INNER\s+|LEFT\s+|RIGHT\s+|FULL\s+|CROSS\s+)?(?:OUTER\s+)?JOIN\s+({0})", SqlOperation.READ, SqlObjectKind.UNKNOWN, "JOIN"),
    (r"\bFROM\s+({0})", SqlOperation.READ, SqlObjectKind.UNKNOWN, "FROM"),
)

_COMPILED_OBJECTS = tuple(
    (re.compile(pattern.format(_QUALIFIED), re.I), operation, kind, statement)
    for pattern, operation, kind, statement in _OBJECT_PATTERNS
)

_FEATURE_PATTERNS: Tuple[Tuple[str, SqlFeatureKind, str], ...] = (
    (r"\bDISTRIBUTION\s*=\s*HASH\s*\(", SqlFeatureKind.DISTRIBUTION, "DISTRIBUTION = HASH"),
    (r"\bDISTRIBUTION\s*=\s*ROUND_ROBIN\b", SqlFeatureKind.DISTRIBUTION, "DISTRIBUTION = ROUND_ROBIN"),
    (r"\bDISTRIBUTION\s*=\s*REPLICATE\b", SqlFeatureKind.DISTRIBUTION, "DISTRIBUTION = REPLICATE"),
    (r"\bCLUSTERED\s+COLUMNSTORE\s+INDEX\b", SqlFeatureKind.INDEX, "CLUSTERED COLUMNSTORE INDEX"),
    (r"\bCLUSTERED\s+INDEX\b", SqlFeatureKind.INDEX, "CLUSTERED INDEX"),
    (r"\bHEAP\b", SqlFeatureKind.INDEX, "HEAP"),
    (r"\bPARTITION\s*\(", SqlFeatureKind.PARTITIONING, "PARTITION"),
    (r"\bCREATE\s+EXTERNAL\s+DATA\s+SOURCE\b", SqlFeatureKind.EXTERNAL_DATA_SOURCE, "CREATE EXTERNAL DATA SOURCE"),
    (r"\bCREATE\s+EXTERNAL\s+FILE\s+FORMAT\b", SqlFeatureKind.EXTERNAL_FILE_FORMAT, "CREATE EXTERNAL FILE FORMAT"),
    (r"\bCREATE\s+EXTERNAL\s+TABLE\b", SqlFeatureKind.EXTERNAL_TABLE, "CREATE EXTERNAL TABLE"),
    (r"\bCOPY\s+INTO\b", SqlFeatureKind.COPY_INTO, "COPY INTO"),
    (r"\bOPTION\s*\(\s*LABEL\s*=", SqlFeatureKind.LABEL, "OPTION (LABEL)"),
    (r"\bCREATE\s+STATISTICS\b", SqlFeatureKind.STATISTICS, "CREATE STATISTICS"),
    (r"\bBEGIN\s+TRAN(?:SACTION)?\b", SqlFeatureKind.TRANSACTION, "BEGIN TRANSACTION"),
    (r"\b(?:GRANT|DENY|REVOKE)\b", SqlFeatureKind.PERMISSION, "GRANT/DENY/REVOKE"),
    (r"\bsp_addrolemember\b", SqlFeatureKind.PERMISSION, "sp_addrolemember"),
    (r"\bCREATE\s+(?:DATABASE\s+SCOPED\s+)?CREDENTIAL\b", SqlFeatureKind.CREDENTIAL, "CREATE CREDENTIAL"),
    (r"\bCREATE\s+MASTER\s+KEY\b", SqlFeatureKind.CREDENTIAL, "CREATE MASTER KEY"),
    (r"\bCREATE\s+LOGIN\b", SqlFeatureKind.CREDENTIAL, "CREATE LOGIN"),
)

_COMPILED_FEATURES = tuple(
    (re.compile(pattern, re.I), kind, construct)
    for pattern, kind, construct in _FEATURE_PATTERNS
)

_OPENROWSET = re.compile(
    r"\bOPENROWSET\s*\(\s*(?:BULK\s+)?'([^']*)'", re.I
)
_AS_SELECT = re.compile(r"\bAS\s+SELECT\b", re.I)
_CREATE_TABLE_NEAR = re.compile(r"\bCREATE\s+TABLE\b", re.I)

_DYNAMIC_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\bsp_executesql\b", "sp_executesql"),
    (r"\bEXEC(?:UTE)?\s*\(", "EXEC(...)"),
    (r"\bEXEC(?:UTE)?\s+@\w+", "EXEC @variable"),
)
_COMPILED_DYNAMIC = tuple(
    (re.compile(pattern, re.I), construct) for pattern, construct in _DYNAMIC_PATTERNS
)

# Credential literals written directly into SQL. Matched on the keyword; the
# value is never captured.
_SQL_SECRETS: Tuple[Tuple[str, str], ...] = (
    (r"\bSECRET\s*=\s*'", "SECRET"),
    (r"\bPASSWORD\s*=\s*'", "PASSWORD"),
    (r"\bSHARED\s+ACCESS\s+SIGNATURE\b", "SHARED ACCESS SIGNATURE"),
)
_COMPILED_SECRETS = tuple(
    (re.compile(pattern, re.I), keyword) for pattern, keyword in _SQL_SECRETS
)


def _blank(match: "re.Match") -> str:
    """Same length, same newlines, so offsets and line numbers stay valid."""
    return "".join("\n" if c == "\n" else " " for c in match.group(0))


def blank_comments(sql: str) -> str:
    """Blank out comments only, leaving string literals in place.

    Used where the *presence* of a literal is the thing being detected: a
    credential written as ``SECRET = '...'`` is invisible if the quotes have
    already been removed. Comments are still blanked, so a commented-out
    credential is not reported.
    """
    return _LINE_COMMENT.sub(_blank, _BLOCK_COMMENT.sub(_blank, sql))


def blank_comments_and_literals(sql: str) -> str:
    """Blank out comments and string literals, preserving positions.

    The default for structural scanning: without it a commented-out
    ``-- FROM secret_table`` becomes a phantom dependency, and a table name
    inside a string literal becomes a false reference.
    """
    return _QUOTED_LITERAL.sub(_blank, blank_comments(sql))


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _at(location: str, line: int) -> str:
    return f"{location}:L{line}"


def _evidence(sql: str, offset: int, redact: bool = False) -> str:
    """The source line containing an offset, trimmed and optionally redacted."""
    start = sql.rfind("\n", 0, offset) + 1
    end = sql.find("\n", offset)
    line = sql[start : end if end != -1 else len(sql)].strip()
    if redact:
        line = _QUOTED_LITERAL.sub(f"'{REDACTED}'", line)
    if len(line) > EVIDENCE_MAX_CHARS:
        line = line[:EVIDENCE_MAX_CHARS] + "..."
    return line


def _unquote(part: str) -> str:
    part = part.strip()
    if part.startswith("[") and part.endswith("]"):
        return part[1:-1]
    if part.startswith('"') and part.endswith('"'):
        return part[1:-1]
    return part


def _split_on_dots(raw: str) -> List[str]:
    """Split a name on its dots, treating an ``@{...}`` interpolation as one unit.

    A dot inside a Synapse expression — ``@{pipeline().parameters.Schema}`` —
    belongs to the expression, not to the name, and splitting there would
    shred one interpolated qualifier into three meaningless fragments.
    """
    parts: List[str] = []
    current: List[str] = []
    index = 0
    while index < len(raw):
        interpolation = _INTERPOLATION_AT.match(raw, index)
        if interpolation:
            current.append(interpolation.group(0))
            index = interpolation.end()
            continue
        if raw[index] == ".":
            parts.append("".join(current))
            current = []
        else:
            current.append(raw[index])
        index += 1
    parts.append("".join(current))
    return parts


def split_qualified_name(raw: str) -> Tuple[Optional[str], Optional[str], str]:
    """Split a qualified name into (database, schema, object).

    Handles one to four parts. A four-part name's first element is a linked
    server, which is dropped: it is not something this stage can resolve.
    """
    parts = [_unquote(p) for p in _split_on_dots(raw.strip()) if p.strip()]
    if not parts:
        return None, None, raw.strip()
    if len(parts) == 1:
        return None, None, parts[0]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return parts[1], parts[2], parts[3]


def find_cte_names(scrubbed: str) -> frozenset:
    """Common table expression names, which are not real database objects."""
    names = {_unquote(m.group(1)).lower() for m in _CTE.finditer(scrubbed)}
    names |= {_unquote(m.group(1)).lower() for m in _CTE_CHAIN.finditer(scrubbed)}
    return frozenset(names)


def scan_objects(sql: str, location: str) -> Tuple[SqlObjectReference, ...]:
    """Database objects the SQL text references, by statement keyword.

    A ``FROM`` or ``JOIN`` target's kind is left UNKNOWN because it genuinely
    is: only a DDL keyword can distinguish a table from a view.
    """
    scrubbed = blank_comments_and_literals(sql)
    cte_names = find_cte_names(scrubbed)

    claimed: List[Tuple[int, int]] = []
    found: List[SqlObjectReference] = []

    for pattern, operation, kind, statement in _COMPILED_OBJECTS:
        for match in pattern.finditer(scrubbed):
            span = match.span(1)
            if any(span[0] < end and start < span[1] for start, end in claimed):
                continue  # a more specific statement already claimed it
            raw = match.group(1).strip()
            database, schema, name = split_qualified_name(raw)
            if name.lower() in _NOT_OBJECTS:
                continue
            if schema is None and name.lower() in cte_names:
                continue  # a CTE is not a database object
            claimed.append(span)
            resolved_kind = kind
            if _INTERPOLATION_AT.search(raw):
                # Part of the name is substituted at runtime, so which object
                # this is cannot be read here. The dependency is still
                # recorded, as written: dropping it would hide it entirely.
                resolved_kind = SqlObjectKind.INTERPOLATED
            elif name.startswith("#") or name.startswith("@"):
                resolved_kind = SqlObjectKind.TEMPORARY
            line = _line_of(scrubbed, span[0])
            found.append(
                SqlObjectReference(
                    name=raw,
                    object_name=name,
                    schema=schema,
                    database=database,
                    kind=resolved_kind,
                    operation=operation,
                    statement=statement,
                    location=_at(location, line),
                )
            )
    return tuple(sorted(found, key=lambda o: (o.location, o.qualified_name)))


def scan_features(sql: str, location: str) -> Tuple[SqlFeature, ...]:
    """Migration-relevant SQL constructs, detected by keyword.

    Presence only. Whether ``DISTRIBUTION = HASH`` is easy or hard to migrate
    is Assessment's question.
    """
    scrubbed = blank_comments_and_literals(sql)
    features: List[SqlFeature] = []

    for pattern, kind, construct in _COMPILED_FEATURES:
        for match in pattern.finditer(scrubbed):
            redact = kind is SqlFeatureKind.CREDENTIAL
            features.append(
                SqlFeature(
                    kind=kind,
                    construct=construct,
                    location=_at(location, _line_of(scrubbed, match.start())),
                    evidence=_evidence(sql, match.start(), redact=redact),
                )
            )

    # CTAS: an AS SELECT preceded closely by CREATE TABLE. Checked by proximity
    # rather than one big regex, which keeps it fast and predictable.
    for match in _AS_SELECT.finditer(scrubbed):
        window = scrubbed[max(0, match.start() - 500) : match.start()]
        if _CREATE_TABLE_NEAR.search(window):
            features.append(
                SqlFeature(
                    kind=SqlFeatureKind.CTAS,
                    construct="CREATE TABLE AS SELECT",
                    location=_at(location, _line_of(scrubbed, match.start())),
                    evidence=_evidence(sql, match.start()),
                )
            )

    # OPENROWSET carries a path; the path is the migration-relevant part.
    for match in _OPENROWSET.finditer(sql):
        features.append(
            SqlFeature(
                kind=SqlFeatureKind.OPENROWSET,
                construct="OPENROWSET",
                location=_at(location, _line_of(sql, match.start())),
                evidence=match.group(1)[:EVIDENCE_MAX_CHARS],
            )
        )

    return tuple(sorted(features, key=lambda f: (f.location, f.kind.value, f.construct)))


def scan_dynamic_sql(sql: str, location: str) -> Tuple[DynamicSqlSite, ...]:
    """Places where SQL is assembled at runtime.

    Recorded, never resolved. ``EXEC(@sql)`` has no static answer, and
    inventing one would be worse than reporting the gap.
    """
    scrubbed = blank_comments_and_literals(sql)
    sites = [
        _site(sql, scrubbed, start, construct, location)
        for start, construct in _dynamic_matches(scrubbed)
    ]
    return tuple(sorted(sites, key=lambda s: (s.location, s.construct)))


def _dynamic_matches(scrubbed: str) -> List[Tuple[int, str]]:
    """(offset, construct) for every dynamic SQL site, each claimed once."""
    found: List[Tuple[int, str]] = []
    claimed: List[int] = []
    for pattern, construct in _COMPILED_DYNAMIC:
        for match in pattern.finditer(scrubbed):
            if match.start() in claimed:
                continue
            claimed.append(match.start())
            found.append((match.start(), construct))
    return found


def _site(sql: str, scrubbed: str, start: int, construct: str, location: str) -> DynamicSqlSite:
    return DynamicSqlSite(
        construct=construct,
        location=_at(location, _line_of(scrubbed, start)),
        evidence=_evidence(sql, start),
    )


# --- resolving dynamic SQL whose structure is static ------------------------
#
# ``SET @sql = 'COPY INTO stage.Trips FROM ''' + @root + 'trips/*.parquet'''``
# followed by ``EXEC(@sql)`` builds its statement at runtime, but only a path
# is decided then: the table it writes is written out in full. Such a site is
# resolvable without guessing, under one strict condition: every runtime part
# of the assembled text lands inside a string literal of the statement it
# builds, so it can change a value and never the SQL's structure. Anything
# else (a runtime table name, a variable assigned more than once or in a
# form not read here, a nested EXEC) stays unresolved.

_RUNTIME_VALUE = "__runtime_value__"
_STRING_LITERAL_AT = re.compile(r"N?'((?:[^']|'')*)'", re.I)
_VARIABLE_AT = re.compile(r"@\w+")
_CALL_AT = re.compile(r"[A-Za-z_]\w*\s*\(")
_EXEC_PAREN = re.compile(r"\bEXEC(?:UTE)?\s*\(", re.I)
_SP_EXECUTESQL = re.compile(r"\bsp_executesql\s+", re.I)


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _closing_paren(text: str, open_index: int) -> Optional[int]:
    """Offset just after the parenthesis that closes ``text[open_index]``."""
    depth = 0
    index = open_index
    while index < len(text):
        literal = _STRING_LITERAL_AT.match(text, index)
        if literal and text[index] in "Nn'":
            index = literal.end()
            continue
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _concatenation(text: str, index: int) -> Optional[Tuple[List[Optional[str]], int]]:
    """Parse ``'literal' + @var + FUNC(...) + ...`` starting at ``index``.

    Returns the pieces (a literal's text, ``"\\0@name"`` for a variable, None
    for any other runtime value) and where the expression ended. None when the
    text is not a plain concatenation.
    """
    pieces: List[Optional[str]] = []
    while True:
        index = _skip_space(text, index)
        literal = _STRING_LITERAL_AT.match(text, index)
        if literal:
            pieces.append(literal.group(1).replace("''", "'"))
            index = literal.end()
        elif _VARIABLE_AT.match(text, index):
            variable = _VARIABLE_AT.match(text, index)
            pieces.append("\0" + variable.group(0).lower())
            index = variable.end()
        elif _CALL_AT.match(text, index):
            call = _CALL_AT.match(text, index)
            end = _closing_paren(text, call.end() - 1)
            if end is None:
                return None
            pieces.append(None)
            index = end
        else:
            return None
        after = _skip_space(text, index)
        if after < len(text) and text[after] == "+" and not text.startswith("+=", after):
            index = after + 1
            continue
        return pieces, index


def _assignments(scrubbed: str, variable: str, before: int) -> List["re.Match"]:
    """Every statement before ``before`` that sets ``variable``, in any form."""
    name = re.escape(variable)
    pattern = re.compile(
        r"\bSET\s+{0}\s*[-+*/%&|^]?=|\bSELECT\s+(?:[^;]*?,\s*)?{0}\s*[-+*/%&|^]?="
        r"|\bDECLARE\s+{0}\s+\w+(?:\s*\([^)]*\))?\s*=|\b{0}\s+OUT(?:PUT)?\b".format(name),
        re.I,
    )
    return [m for m in pattern.finditer(scrubbed, 0, before)]


def _variable_text(sql: str, scrubbed: str, variable: str, site: int, floor: int) -> Optional[List[Optional[str]]]:
    """The pieces of the one plain ``SET``/``DECLARE`` that last gave ``variable`` its value.

    None unless exactly one assignment sits between ``floor`` (the previous
    dynamic site) and ``site``, it is a plain ``=`` of a concatenation, and
    nothing touches the variable between that assignment and the site.
    """
    found = [m for m in _assignments(scrubbed, variable, site) if m.start() >= floor]
    if len(found) != 1:
        return None
    assignment = found[0]
    written = assignment.group(0)
    if not written.upper().startswith(("SET", "DECLARE")) or re.search(r"[-+*/%&|^]=$", written):
        return None  # SELECT @v =, OUTPUT and += are not read here
    parsed = _concatenation(sql, assignment.end())
    if parsed is None:
        return None
    pieces, end = parsed
    if re.search(r"(?<![\w@]){0}\b".format(re.escape(variable)), scrubbed[end:site], re.I):
        return None
    return pieces


def _executed_pieces(sql: str, scrubbed: str, start: int, floor: int) -> Optional[List[Optional[str]]]:
    """The pieces of the SQL text a site executes, or None when not readable."""
    paren = _EXEC_PAREN.match(scrubbed, start)
    if paren:
        parsed = _concatenation(sql, paren.end())
        if parsed is None or not sql[_skip_space(sql, parsed[1]):].startswith(")"):
            return None
        pieces = parsed[0]
    else:
        call = _SP_EXECUTESQL.match(scrubbed, start)
        if not call:
            return None  # EXEC @variable runs a procedure named at runtime
        parsed = _concatenation(sql, call.end())
        if parsed is None:
            return None
        pieces = parsed[0]

    expanded: List[Optional[str]] = []
    for piece in pieces:
        if piece is not None and piece.startswith("\0"):
            if len(pieces) == 1:
                value = _variable_text(sql, scrubbed, piece[1:], start, floor)
                if value is None:
                    return None
                expanded.extend(None if v is not None and v.startswith("\0") else v for v in value)
            else:
                expanded.append(None)
        else:
            expanded.append(piece)
    return expanded


def _resolve_site(pieces: List[Optional[str]], location: str) -> Optional[Tuple[SqlObjectReference, ...]]:
    """The objects a statement names, when its runtime parts are only values."""
    if not any(p for p in pieces if p is not None):
        return None
    text = "".join(_RUNTIME_VALUE if p is None else p for p in pieces)
    literal_spans = [m.span() for m in _QUOTED_LITERAL.finditer(blank_comments(text))]
    offset = text.find(_RUNTIME_VALUE)
    while offset != -1:
        if not any(start < offset and offset < end for start, end in literal_spans):
            return None  # a runtime part shapes the SQL itself
        offset = text.find(_RUNTIME_VALUE, offset + 1)
    if _dynamic_matches(blank_comments_and_literals(text)):
        return None  # it builds SQL of its own
    return tuple(replace(r, location=location) for r in scan_objects(text, location))


def resolve_dynamic_sql(
    sql: str, location: str
) -> Tuple[Tuple[SqlObjectReference, ...], Tuple[DynamicSqlSite, ...]]:
    """The objects resolvable dynamic SQL names, and the sites that stay unresolved.

    A site resolves only when the SQL it runs is assembled from literals, and
    every runtime value lands inside a string literal of that SQL (a path, a
    date), never where a name or keyword goes. See the note above.
    """
    scrubbed = blank_comments_and_literals(sql)
    references: List[SqlObjectReference] = []
    unresolved: List[DynamicSqlSite] = []
    floor = 0
    for start, construct in sorted(_dynamic_matches(scrubbed)):
        site = _site(sql, scrubbed, start, construct, location)
        pieces = _executed_pieces(sql, scrubbed, start, floor)
        resolved = None if pieces is None else _resolve_site(pieces, site.location)
        if resolved is None:
            unresolved.append(site)
        else:
            references.extend(resolved)
        floor = start + 1
    return (
        tuple(sorted(references, key=lambda o: (o.location, o.qualified_name))),
        tuple(sorted(unresolved, key=lambda s: (s.location, s.construct))),
    )


def scan_sql_secrets(sql: str, location: str) -> Tuple[SecretReference, ...]:
    """Credential literals written into SQL. Records the keyword, never the value.

    Scans with comments removed but literals intact: the pattern keys on the
    opening quote of the value, so blanking literals first would hide exactly
    what this is looking for.
    """
    scrubbed = blank_comments(sql)
    found: List[SecretReference] = []
    for pattern, keyword in _COMPILED_SECRETS:
        for match in pattern.finditer(scrubbed):
            found.append(
                SecretReference(
                    kind=SecretKind.INLINE_LITERAL,
                    location=_at(location, _line_of(scrubbed, match.start())),
                    property_name=keyword,
                )
            )
    return tuple(sorted(found, key=lambda s: (s.location, s.property_name or "")))
