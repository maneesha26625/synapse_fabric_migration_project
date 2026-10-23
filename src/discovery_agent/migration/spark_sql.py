"""T-SQL from a dedicated SQL pool -> Spark SQL in a notebook, for runs whose SQL target is a Lakehouse.

A Lakehouse's SQL analytics endpoint is read-only: a stored procedure that changes data cannot live
there. So in a Lakehouse run every stored procedure (and every SQL script that changes data) becomes
a **PySpark notebook** attached to the Lakehouse, which a pipeline's notebook activity runs where the
Synapse pipeline ran the procedure.

The conversion is rule-based and mechanical, and every place it is not sure of is listed in the
notebook and in the run's notes, never silently guessed:

* **Shape.** The procedure's parameters become the notebook's parameters cell (a pipeline overrides
  them). Its body becomes a Python function, ``run()``: each statement runs with ``spark.sql``, with
  T-SQL variables passed as named parameters (``@x`` -> ``:x``). Control flow becomes Python:
  ``IF``/``WHILE`` -> ``if``/``while`` (the condition is evaluated by Spark), ``BEGIN TRY``/``CATCH`` ->
  ``try``/``except``, ``RETURN`` -> ``return``, ``RAISERROR``/``THROW`` -> ``raise``, cursors -> a loop over
  collected rows. The notebook's exit value is the return code and the OUTPUT parameters, as JSON.
* **Statements.** ``SELECT ... INTO`` and CTAS -> ``CREATE TABLE ... AS SELECT``; ``TRUNCATE`` -> ``DELETE``;
  ``UPDATE ... FROM`` / ``DELETE ... FROM`` with one joined source -> Delta ``MERGE``; ``TOP n`` -> ``LIMIT n``;
  ``#temp`` tables and table variables -> Delta tables in the ``tmp_work`` schema, dropped at the end;
  ``BEGIN TRAN``/``COMMIT``/``ROLLBACK`` are removed (Delta commits each statement on its own).
* **Expressions.** ``[name]`` -> backticks, ``N'x'`` -> ``'x'``, T-SQL functions -> their Spark
  equivalents (``GETDATE``, ``ISNULL``, ``LEN``, ``CHARINDEX``, ``DATEADD``, ``DATEDIFF`` with T-SQL's
  boundary semantics for days, months and years, ``CONVERT``, ``IIF``, ``NEWID`` ...), T-SQL types -> Spark
  types, ``+`` beside a string literal -> ``||``.

What it cannot convert (dynamic SQL built as T-SQL text, ``GOTO``, ``SCOPE_IDENTITY``, an ``UPDATE`` with
several joins ...) is kept in a ``REVIEW`` comment and listed: the notebook is created, flagged
for a person to finish before it runs.
"""

from __future__ import annotations

import json
import keyword
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from discovery_agent.migration.lakehouse_ddl import WORK_SCHEMA, lakehouse_document

# ============================================================================== tokens


@dataclass
class Tok:
    kind: str  # word, ident, string, number, var, sysvar, temp, op
    text: str
    ws: str = ""  # whitespace and comments before the token

    @property
    def up(self) -> str:
        return self.text.upper() if self.kind == "word" else ""

    def is_(self, *words: str) -> bool:
        return self.kind == "word" and self.text.upper() in words


_OPS = ("<>", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=", "||", "::")


def tokenize(sql: str) -> List[Tok]:
    out: List[Tok] = []
    i, n, ws = 0, len(sql), ""
    while i < n:
        ch = sql[i]
        if ch.isspace():
            j = i
            while j < n and sql[j].isspace():
                j += 1
            ws += sql[i:j]
            i = j
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            ws += "/*" + sql[i + 2:j].replace("*/", "* /") + " */"  # kept as a block comment: lines may be joined later
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            ws += sql[i:j]
            i = j
            continue
        start = i
        if ch in "Nn" and i + 1 < n and sql[i + 1] == "'":
            i += 1
            ch = "'"
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(Tok("string", sql[start:j + 1], ws))
            i, ws = j + 1, ""
            continue
        if ch in "[\"":
            close = "]" if ch == "[" else '"'
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            out.append(Tok("ident", sql[i:j + 1], ws))
            i, ws = j + 1, ""
            continue
        m = re.compile(r"@@\w+|@[\w#$@]+|##?[\w#$@]+|0x[0-9A-Fa-f]*|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+|[A-Za-z_][\w$#@]*").match(sql, i)
        if m:
            text = m.group(0)
            kind = ("sysvar" if text.startswith("@@") else "var" if text.startswith("@") else "temp" if text.startswith("#")
                    else "number" if text[0].isdigit() or text[0] == "." else "word")
            out.append(Tok(kind, text, ws))
            i, ws = m.end(), ""
            continue
        op = next((o for o in _OPS if sql.startswith(o, i)), ch)
        out.append(Tok("op", op, ws))
        i, ws = i + len(op), ""
    if ws and out:
        out.append(Tok("op", "", ws))
    return out


def string_value(tok: Tok) -> str:
    text = tok.text[1:] if tok.text[:1] in "Nn" else tok.text
    return text[1:-1].replace("''", "'")


def ident_value(tok: Tok) -> str:
    if tok.kind == "ident":
        return tok.text[1:-1].replace("]]", "]").replace('""', '"')
    return tok.text


def text_of(toks: Sequence[Tok], first_ws: bool = False) -> str:
    """Original text of tokens."""
    return "".join((t.ws if (k or first_ws) else "") + t.text for k, t in enumerate(toks)).strip()


def _match(toks: Sequence[Tok], open_at: int) -> int:
    """Index of the parenthesis that closes the one at ``open_at`` (or the last index)."""
    depth = 0
    for k in range(open_at, len(toks)):
        if toks[k].text == "(" and toks[k].kind == "op":
            depth += 1
        elif toks[k].text == ")" and toks[k].kind == "op":
            depth -= 1
            if depth == 0:
                return k
    return len(toks) - 1


def split_top(toks: Sequence[Tok], sep: str = ",") -> List[List[Tok]]:
    parts: List[List[Tok]] = [[]]
    depth = 0
    for t in toks:
        if t.kind == "op" and t.text == "(":
            depth += 1
        elif t.kind == "op" and t.text == ")":
            depth -= 1
        if depth == 0 and t.kind == "op" and t.text == sep:
            parts.append([])
            continue
        parts[-1].append(t)
    return [p for p in parts if p]


def _top_index(toks: Sequence[Tok], pred: Callable[[Tok], bool], start: int = 0) -> int:
    """First index at parenthesis depth 0 (outside CASE) where ``pred`` holds, or -1."""
    depth = case = 0
    for k in range(start, len(toks)):
        t = toks[k]
        if t.kind == "op" and t.text == "(":
            depth += 1
        elif t.kind == "op" and t.text == ")":
            depth -= 1
        elif depth == 0 and t.is_("CASE"):
            case += 1
        elif depth == 0 and case and t.is_("END"):
            case -= 1
            continue
        if depth == 0 and case == 0 and pred(t):
            return k
    return -1


# ============================================================================== statements


STARTERS = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "CREATE", "DROP", "ALTER", "DECLARE", "SET", "IF", "WHILE",
    "BEGIN", "EXEC", "EXECUTE", "RETURN", "PRINT", "RAISERROR", "THROW", "WITH", "COMMIT", "ROLLBACK", "BREAK", "CONTINUE",
    "OPEN", "FETCH", "CLOSE", "DEALLOCATE", "RENAME", "GOTO", "WAITFOR", "USE", "GO", "SAVE",
}


@dataclass
class Stmt:
    toks: List[Tok]

    @property
    def head(self) -> str:
        return self.toks[0].up if self.toks else ""


@dataclass
class Block:
    body: List["Node"] = field(default_factory=list)


@dataclass
class IfNode:
    cond: List[Tok]
    then: "Node"
    other: Optional["Node"] = None


@dataclass
class WhileNode:
    cond: List[Tok]
    body: "Node"


@dataclass
class TryNode:
    body: Block
    catch: Block


Node = Union[Stmt, Block, IfNode, WhileNode, TryNode]


class Parser:
    def __init__(self, toks: List[Tok]) -> None:
        self.toks = [t for t in toks if t.text != ""]
        self.pos = 0

    def cur(self, k: int = 0) -> Optional[Tok]:
        p = self.pos + k
        return self.toks[p] if p < len(self.toks) else None

    def statements(self, stop_at_end: bool = False) -> List[Node]:
        out: List[Node] = []
        while self.pos < len(self.toks):
            t = self.toks[self.pos]
            if t.kind == "op" and t.text == ";":
                self.pos += 1
                continue
            if t.is_("END") and stop_at_end:
                break
            if t.is_("GO"):
                self.pos += 1
                continue
            if t.is_("ELSE", "END"):  # stray: keep going rather than loop
                self.pos += 1
                continue
            out.append(self.statement())
        return out

    def statement(self) -> Node:
        t = self.toks[self.pos]
        nxt = self.cur(1)
        if t.is_("BEGIN") and nxt is not None and nxt.is_("TRY"):
            self.pos += 2
            body = Block(self.statements(stop_at_end=True))
            self._expect_end("TRY")
            catch = Block()
            if self.cur() is not None and self.cur().is_("BEGIN") and self.cur(1) is not None and self.cur(1).is_("CATCH"):
                self.pos += 2
                catch = Block(self.statements(stop_at_end=True))
                self._expect_end("CATCH")
            return TryNode(body, catch)
        if t.is_("BEGIN") and nxt is not None and nxt.is_("TRAN", "TRANSACTION", "DISTRIBUTED"):
            return self.simple()
        if t.is_("BEGIN"):
            self.pos += 1
            body = self.statements(stop_at_end=True)
            self._expect_end()
            return Block(body)
        if t.is_("IF"):
            self.pos += 1
            cond = self.condition()
            then = self.statement() if self.pos < len(self.toks) else Block()
            while self.cur() is not None and self.cur().kind == "op" and self.cur().text == ";":
                self.pos += 1
            other = None
            if self.cur() is not None and self.cur().is_("ELSE"):
                self.pos += 1
                other = self.statement() if self.pos < len(self.toks) else Block()
            return IfNode(cond, then, other)
        if t.is_("WHILE"):
            self.pos += 1
            cond = self.condition()
            return WhileNode(cond, self.statement() if self.pos < len(self.toks) else Block())
        return self.simple()

    def _expect_end(self, word: str = "") -> None:
        if self.cur() is not None and self.cur().is_("END"):
            self.pos += 1
            if word and self.cur() is not None and self.cur().is_(word):
                self.pos += 1

    def condition(self) -> List[Tok]:
        out: List[Tok] = []
        depth = 0
        while self.pos < len(self.toks):
            t = self.toks[self.pos]
            if t.kind == "op" and t.text == "(":
                depth += 1
            elif t.kind == "op" and t.text == ")":
                depth -= 1
            elif depth == 0 and out and t.kind == "word" and t.up in STARTERS and not (t.up == "SELECT" and out[-1].text == "("):
                break
            out.append(t)
            self.pos += 1
        return out

    #: After a statement's head, the keywords that continue it rather than start the next statement,
    #: and what each of those allows next (one-shot: ``INSERT ... SELECT`` takes one SELECT).
    _NEXT = {
        ("INSERT", "SELECT"): set(), ("INSERT", "WITH"): {"SELECT"}, ("INSERT", "EXEC"): set(), ("INSERT", "EXECUTE"): set(),
        ("WITH", "SELECT"): set(), ("WITH", "INSERT"): {"SELECT", "WITH"}, ("WITH", "UPDATE"): {"SET"}, ("WITH", "DELETE"): set(),
        ("UPDATE", "SET"): set(),
    }
    _FIRST = {"INSERT": {"SELECT", "WITH", "EXEC", "EXECUTE"}, "WITH": {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"},
              "UPDATE": {"SET"}}

    def simple(self) -> Stmt:
        first = self.toks[self.pos]
        mode = first.up
        out = [first]
        self.pos += 1
        depth = case = 0
        allow: Set[str] = set(self._FIRST.get(mode, set()))
        while self.pos < len(self.toks):
            t = self.toks[self.pos]
            prev = out[-1]
            if t.kind == "op" and t.text == "(":
                depth += 1
            elif t.kind == "op" and t.text == ")":
                depth -= 1
            elif depth <= 0 and t.is_("CASE"):
                case += 1
            elif depth <= 0 and case and t.is_("END"):
                case -= 1
                out.append(t)
                self.pos += 1
                continue
            if depth <= 0 and case == 0:
                if t.kind == "op" and t.text == ";":
                    self.pos += 1
                    break
                if t.is_("VALUES"):
                    allow = set()  # INSERT ... VALUES takes no SELECT after it
                if t.is_("END", "ELSE") and mode != "MERGE":
                    break
                if t.kind == "word" and t.up in STARTERS:
                    w = t.up
                    if mode == "MERGE" and w in ("UPDATE", "INSERT", "DELETE", "SET", "SELECT"):
                        pass
                    elif w in allow:
                        if mode == "WITH" and w == "MERGE":
                            mode, allow = "MERGE", set()
                        else:
                            allow = set(self._NEXT.get((mode, w), set()))
                            if mode == "WITH" and w in ("INSERT", "UPDATE"):
                                mode = w  # the CTE's statement decides what follows
                    elif w == "SELECT" and (prev.is_("UNION", "ALL", "EXCEPT", "INTERSECT", "AS", "FOR") or prev.text == "("):
                        pass
                    elif w == "WITH" and self.cur(1) is not None and self.cur(1).text == "(":
                        pass  # a table hint or storage options, not a common table expression
                    else:
                        break
            out.append(t)
            self.pos += 1
        return Stmt(out)


# ============================================================================== header


@dataclass
class Param:
    name: str  # without @, lower-cased
    sql_type: str
    default: Optional[str] = None  # T-SQL text
    output: bool = False


def split_header(sql: str) -> Tuple[str, List[Param], List[Tok]]:
    """(procedure name, parameters, body tokens) of a ``CREATE PROCEDURE`` text."""
    toks = [t for t in tokenize(sql) if t.text != ""]
    k = 0
    while k < len(toks) and not toks[k].is_("PROC", "PROCEDURE"):
        k += 1
    if k >= len(toks):
        return "", [], toks
    k += 1
    name_parts: List[str] = []
    while k < len(toks) and (toks[k].kind in ("word", "ident") or toks[k].text == "."):
        if toks[k].text != ".":
            name_parts.append(ident_value(toks[k]))
        k += 1
        if k < len(toks) and toks[k].text != "." and (toks[k - 1].text != "."):
            break
    if k < len(toks) and toks[k].kind == "op" and toks[k].text == ";":  # numbered procedures, ";1"
        k += 2
    end = k
    depth = 0
    while end < len(toks):
        t = toks[end]
        if t.text == "(" and t.kind == "op":
            depth += 1
        elif t.text == ")" and t.kind == "op":
            depth -= 1
        elif depth == 0 and t.is_("AS") and not (end > 0 and toks[end - 1].kind == "var"):
            break
        end += 1
    header = toks[k:end]
    with_at = _top_index(header, lambda t: t.is_("WITH"))
    if with_at >= 0:
        header = header[:with_at]
    if header and header[0].text == "(" and header[-1].text == ")":
        header = header[1:-1]
    params: List[Param] = []
    for part in split_top(header):
        if not part or part[0].kind != "var":
            continue
        rest = part[1:]
        if rest and rest[0].is_("AS"):
            rest = rest[1:]
        output = any(t.is_("OUT", "OUTPUT") for t in rest)
        rest = [t for t in rest if not t.is_("OUT", "OUTPUT", "READONLY", "VARYING")]
        eq = next((i for i, t in enumerate(rest) if t.kind == "op" and t.text == "="), -1)
        default = text_of(rest[eq + 1:]) if eq >= 0 else None
        type_toks = rest[:eq] if eq >= 0 else rest
        params.append(Param(part[0].text[1:].lower(), text_of(type_toks), default, output))
    body = toks[end + 1:] if end < len(toks) else []
    return ".".join(name_parts), params, body


# ============================================================================== types and functions


def spark_type(tsql: str) -> str:
    """A T-SQL type spelling (``nvarchar(50)``, ``decimal(18, 2)``) as a Spark SQL type."""
    m = re.match(r"\s*\[?([A-Za-z_][\w]*)\]?\s*(?:\(\s*([^)]*)\))?", tsql or "")
    if not m:
        return "STRING"
    base, args = m.group(1).lower(), [a.strip() for a in (m.group(2) or "").split(",") if a.strip()]
    if base in ("decimal", "numeric"):
        p = args[0] if args and args[0].isdigit() else "18"
        s = args[1] if len(args) > 1 and args[1].isdigit() else "0"
        return f"DECIMAL({p},{s})"
    return {
        "bit": "BOOLEAN", "tinyint": "SMALLINT", "smallint": "SMALLINT", "int": "INT", "integer": "INT", "bigint": "BIGINT",
        "money": "DECIMAL(19,4)", "smallmoney": "DECIMAL(10,4)", "float": "DOUBLE", "real": "FLOAT",
        "date": "DATE", "datetime": "TIMESTAMP", "datetime2": "TIMESTAMP", "smalldatetime": "TIMESTAMP",
        "datetimeoffset": "TIMESTAMP", "time": "STRING", "binary": "BINARY", "varbinary": "BINARY", "image": "BINARY",
        "rowversion": "BINARY", "timestamp": "BINARY",
    }.get(base, "STRING")


_UNITS = {
    "year": "YEAR", "yy": "YEAR", "yyyy": "YEAR", "quarter": "QUARTER", "qq": "QUARTER", "q": "QUARTER",
    "month": "MONTH", "mm": "MONTH", "m": "MONTH", "dayofyear": "DAY", "dy": "DAY", "y": "DAY",
    "day": "DAY", "dd": "DAY", "d": "DAY", "week": "WEEK", "wk": "WEEK", "ww": "WEEK", "weekday": "DAY", "dw": "DAY",
    "hour": "HOUR", "hh": "HOUR", "minute": "MINUTE", "mi": "MINUTE", "n": "MINUTE", "second": "SECOND", "ss": "SECOND",
    "s": "SECOND", "millisecond": "MILLISECOND", "ms": "MILLISECOND", "microsecond": "MICROSECOND", "mcs": "MICROSECOND",
}
_PARTS = {
    "year": "YEAR", "yy": "YEAR", "yyyy": "YEAR", "quarter": "QUARTER", "qq": "QUARTER", "q": "QUARTER",
    "month": "MONTH", "mm": "MONTH", "m": "MONTH", "dayofyear": "DOY", "dy": "DOY", "y": "DOY", "day": "DAY", "dd": "DAY",
    "d": "DAY", "week": "WEEK", "wk": "WEEK", "ww": "WEEK", "weekday": "DOW", "dw": "DOW", "hour": "HOUR", "hh": "HOUR",
    "minute": "MINUTE", "mi": "MINUTE", "n": "MINUTE", "second": "SECOND", "ss": "SECOND", "s": "SECOND",
}
#: Same function, different name.
_RENAMED = {"ISNULL": "coalesce", "CEILING": "ceil", "POWER": "pow", "NEWID": "uuid", "IIF": "if", "REPLICATE": "repeat",
            "CHOOSE": "elt", "EOMONTH": "last_day", "DATALENGTH": "octet_length", "CHAR": "chr", "TRY_PARSE": "try_cast",
            "SYSTEM_USER": "current_user", "SUSER_SNAME": "current_user", "USER_NAME": "current_user", "SUSER_NAME": "current_user"}
_NOW = {"GETDATE", "SYSDATETIME", "GETUTCDATE", "SYSUTCDATETIME", "CURRENT_TIMESTAMP", "SYSDATETIMEOFFSET"}
#: Functions with no Spark equivalent this converter knows: kept, and flagged.
_FLAGGED = {"OBJECT_ID", "OBJECT_NAME", "SCOPE_IDENTITY", "IDENT_CURRENT", "PATINDEX", "STUFF", "QUOTENAME", "FORMAT",
            "ISDATE", "STR", "DB_NAME", "DB_ID", "SCHEMA_NAME", "SCHEMA_ID", "COLUMNPROPERTY", "OBJECTPROPERTY", "HOST_NAME",
            "APP_NAME", "SESSION_CONTEXT", "CONTEXT_INFO", "PARSE", "SOUNDEX", "DIFFERENCE", "CHECKSUM", "BINARY_CHECKSUM"}
_ERRORS = {"ERROR_MESSAGE": "error_message", "ERROR_NUMBER": "error_number", "ERROR_SEVERITY": "error_severity",
           "ERROR_STATE": "error_state", "ERROR_LINE": "error_line", "ERROR_PROCEDURE": "error_procedure"}


# ============================================================================== the converter


def spark_string(value: str) -> str:
    """A Spark SQL string literal (backslash escapes; Spark reads '' as two adjacent literals)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", text.lower()).strip("_") or "x"


def py_name(text: str) -> str:
    """A T-SQL variable or parameter name (without @) as the notebook's name for it: a valid Python identifier,
    also used as the Spark SQL parameter marker (``:name``) and by a pipeline's notebook activity."""
    name = _slug(text.lstrip("@"))
    if name[0].isdigit() or keyword.iskeyword(name) or name in ("v", "q", "row", "result", "spark", "json", "re", "time"):
        name = "p_" + name
    return name


@dataclass
class Context:
    """What one conversion needs and what it found."""

    work_prefix: str                      # temporary tables: tmp_work.<prefix>_<name>
    pool: str = ""                        # the Synapse pool's name, dropped from three-part names
    notebooks: Mapping[str, str] = field(default_factory=dict)  # procedure name (lower, schema.name and name) -> notebook name
    callee_params: Mapping[str, Sequence[str]] = field(default_factory=dict)  # same keys -> parameter names, in order
    review: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    variables: Set[str] = field(default_factory=set)
    work_tables: Set[str] = field(default_factory=set)
    uses_rowcount: bool = False
    uses_fetch: bool = False

    def flag(self, text: str) -> None:
        if text not in self.review:
            self.review.append(text)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def work_table(self, name: str) -> str:
        bare = name.lstrip("#@")
        self.work_tables.add(bare.lower())
        return f"{WORK_SCHEMA}.{self.work_prefix}_{_slug(bare)}"


class Converter:
    """T-SQL tokens -> Spark SQL text."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # -- expressions -------------------------------------------------------------------

    def sql(self, toks: Sequence[Tok], table_vars: Optional[Set[str]] = None) -> str:
        """Spark SQL text for a run of T-SQL tokens."""
        toks = self._names(list(toks))
        out: List[str] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            ws = t.ws if out else ""
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if t.kind == "word" and nxt is not None and nxt.kind == "op" and nxt.text == "(":
                close = _match(toks, i + 1)
                rendered = self._call(t, toks[i + 2:close])
                if rendered is not None:
                    out.append(ws + rendered)
                    i = close + 1
                    continue
            out.append(ws + self._token(t, toks, i, table_vars or set()))
            i += 1
        return "".join(out).strip()

    def _names(self, toks: List[Tok]) -> List[Tok]:
        """Three- and four-part names lose the database (the pool, or tempdb); hints and COLLATE are dropped."""
        out: List[Tok] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.kind in ("word", "ident") and i + 4 < len(toks) and toks[i + 1].text == "." and toks[i + 3].text == "." \
                    and toks[i + 2].kind in ("word", "ident") and toks[i + 4].kind in ("word", "ident", "temp") \
                    and not (out and out[-1].text == "."):
                # database.schema.table, or schema.table.column: only the pool's own name (or tempdb) is dropped.
                if ident_value(t).lower() in {self.ctx.pool.lower(), "tempdb"} - {""}:
                    out.append(Tok(toks[i + 2].kind, toks[i + 2].text, t.ws))
                    i += 3
                    continue
            if t.kind in ("word", "ident") and i + 2 < len(toks) and toks[i + 1].text == "." and toks[i + 2].text == "." \
                    and ident_value(t).lower() == "tempdb":
                i += 3  # tempdb..#t -> #t
                if i < len(toks):
                    toks[i] = Tok(toks[i].kind, toks[i].text, t.ws)
                continue
            if t.is_("WITH") and i + 1 < len(toks) and toks[i + 1].text == "(" and out and (out[-1].kind in ("word", "ident", "temp")):
                close = _match(toks, i + 1)
                inner = text_of(toks[i + 2:close]).upper()
                if re.fullmatch(r"[\sA-Z_,()=0-9]*", inner) and any(h in inner for h in ("NOLOCK", "READUNCOMMITTED", "TABLOCK", "HOLDLOCK", "ROWLOCK", "UPDLOCK", "INDEX", "FORCESEEK", "READPAST")):
                    i = close + 1  # a table hint: Delta reads a snapshot and takes no locks
                    continue
            if t.is_("COLLATE") and i + 1 < len(toks):
                self.ctx.note("COLLATE clauses removed: Spark compares strings by their bytes (case-sensitive).")
                i += 2
                continue
            out.append(t)
            i += 1
        return out

    def _token(self, t: Tok, toks: Sequence[Tok], i: int, table_vars: Set[str]) -> str:
        if t.kind == "ident":
            return "`" + ident_value(t).replace("`", "``") + "`"
        if t.kind == "string":
            return spark_string(string_value(t))
        if t.kind == "var":
            name = t.text[1:].lower()
            if name in table_vars:
                return self.ctx.work_table(t.text)
            self.ctx.variables.add(name)
            return ":" + py_name(name)
        if t.kind == "sysvar":
            up = t.text.upper()
            if up == "@@ROWCOUNT":
                self.ctx.uses_rowcount = True
                return ":rowcount"
            if up == "@@FETCH_STATUS":
                self.ctx.uses_fetch = True
                return ":fetch_status"
            if up == "@@TRANCOUNT":
                return "0"
            if up == "@@ERROR":
                self.ctx.flag("@@ERROR has no Spark equivalent (errors raise exceptions); it reads as 0.")
                return "0"
            self.ctx.flag(f"{t.text} has no Spark equivalent.")
            return "NULL"
        if t.kind == "temp":
            return self.ctx.work_table(t.text)
        if t.kind == "word":
            up = t.up
            if up in ("CURRENT_TIMESTAMP",):
                return "current_timestamp()"
            if up in ("SYSTEM_USER", "SESSION_USER", "CURRENT_USER"):
                return "current_user()"
            return t.text
        if t.kind == "op" and t.text == "+":
            before = toks[i - 1] if i > 0 else None
            after = toks[i + 1] if i + 1 < len(toks) else None
            if (before is not None and before.kind == "string") or (after is not None and after.kind == "string"):
                return "||"
            return "+"
        if t.kind == "op" and t.text in ("+=", "-=", "*=", "/=", "%="):
            return t.text
        return t.text

    def _args(self, toks: Sequence[Tok]) -> List[List[Tok]]:
        return split_top(toks)

    def _call(self, name: Tok, inner: Sequence[Tok]) -> Optional[str]:
        """A T-SQL function call in Spark SQL, or None to leave the name and convert the arguments in place."""
        up = name.up
        if up not in _KNOWN:
            if up in _FLAGGED:
                self.ctx.flag(f"{up}() has no Spark equivalent this converter knows; rewrite it.")
            return None
        args = self._args(inner)
        conv = [self.sql(a) for a in args]
        if up in _NOW:
            if up == "SYSDATETIMEOFFSET":
                self.ctx.note("SYSDATETIMEOFFSET() became current_timestamp(): the time-zone offset is not kept.")
            return "current_timestamp()"
        if up in _ERRORS:
            return ":" + _ERRORS[up]
        if up in _RENAMED:
            return f"{_RENAMED[up]}({', '.join(conv)})"
        if up == "LEN" and len(conv) == 1:
            return f"length(rtrim({conv[0]}))"
        if up == "CHARINDEX" and len(conv) in (2, 3):
            return f"locate({', '.join(conv)})"
        if up == "LOG" and len(conv) == 2:
            return f"log({conv[1]}, {conv[0]})"
        if up == "LOG" and len(conv) == 1:
            return f"ln({conv[0]})"
        if up == "SQUARE" and len(conv) == 1:
            return f"pow({conv[0]}, 2)"
        if up == "SPACE" and len(conv) == 1:
            return f"repeat(' ', {conv[0]})"
        if up == "ISNUMERIC" and len(conv) == 1:
            return f"CASE WHEN try_cast({conv[0]} AS DOUBLE) IS NULL THEN 0 ELSE 1 END"
        if up in ("CAST", "TRY_CAST") and len(args) == 1:
            at = _top_index(args[0], lambda t: t.is_("AS"))
            if at > 0:
                fn = "CAST" if up == "CAST" else "try_cast"
                return f"{fn}({self.sql(args[0][:at])} AS {spark_type(text_of(args[0][at + 1:]))})"
        if up in ("CONVERT", "TRY_CONVERT") and len(args) >= 2:
            if len(args) == 3:
                self.ctx.note(f"{up}(..., style) lost its style argument: check the format of the converted value.")
            fn = "CAST" if up == "CONVERT" else "try_cast"
            return f"{fn}({conv[1]} AS {spark_type(text_of(args[0]))})"
        if up == "DATEADD" and len(args) == 3:
            unit = _UNITS.get(text_of(args[0]).lower().strip("'"))
            if unit:
                if unit == "QUARTER":
                    return f"dateadd(MONTH, 3 * ({conv[1]}), {conv[2]})"
                return f"dateadd({unit}, {conv[1]}, {conv[2]})"
        if up in ("DATEDIFF", "DATEDIFF_BIG") and len(args) == 3:
            unit = _UNITS.get(text_of(args[0]).lower().strip("'"))
            a, b = conv[1], conv[2]
            if unit == "DAY":
                return f"datediff(CAST({b} AS DATE), CAST({a} AS DATE))"
            if unit == "MONTH":
                return f"((year({b}) - year({a})) * 12 + month({b}) - month({a}))"
            if unit == "YEAR":
                return f"(year({b}) - year({a}))"
            if unit:
                self.ctx.note(f"DATEDIFF({unit.lower()}, ...) counts whole {unit.lower()}s elapsed in Spark; T-SQL counts "
                              "boundaries crossed, so results can differ by one.")
                return f"timestampdiff({unit}, {a}, {b})"
        if up == "DATEPART" and len(args) == 2:
            part = _PARTS.get(text_of(args[0]).lower().strip("'"))
            if part:
                return f"date_part('{part}', {conv[1]})" if part != "DOW" else f"dayofweek({conv[1]})"
        if up == "DATENAME" and len(args) == 2:
            part = _PARTS.get(text_of(args[0]).lower().strip("'"))
            if part == "MONTH":
                return f"date_format({conv[1]}, 'MMMM')"
            if part == "DOW":
                return f"date_format({conv[1]}, 'EEEE')"
            if part:
                return f"CAST(date_part('{part}', {conv[1]}) AS STRING)"
        if up == "DATEFROMPARTS" and len(conv) == 3:
            return f"make_date({', '.join(conv)})"
        if up == "STRING_AGG" and len(conv) == 2:
            return f"array_join(collect_list({conv[0]}), {conv[1]})"
        if up == "HASHBYTES" and len(args) == 2:
            algorithm = string_value(args[0][0]).upper() if args[0] and args[0][0].kind == "string" else ""
            self.ctx.note("HASHBYTES became a Spark hash function, which returns hexadecimal text rather than binary.")
            if algorithm == "MD5":
                return f"md5({conv[1]})"
            if algorithm == "SHA1":
                return f"sha1({conv[1]})"
            if algorithm in ("SHA2_256", "SHA2_512"):
                return f"sha2({conv[1]}, {algorithm[-3:]})"
        return None


#: Every function ``Converter._call`` rewrites; any other call is left as it is (its arguments still converted).
_KNOWN = (set(_RENAMED) | _NOW | set(_ERRORS) | {
    "LEN", "CHARINDEX", "LOG", "SQUARE", "SPACE", "ISNUMERIC", "CAST", "TRY_CAST", "CONVERT", "TRY_CONVERT", "DATEADD",
    "DATEDIFF", "DATEDIFF_BIG", "DATEPART", "DATENAME", "DATEFROMPARTS", "STRING_AGG", "HASHBYTES"})


# ============================================================================== notebook code


@dataclass
class Emitter:
    lines: List[str] = field(default_factory=list)
    depth: int = 1  # inside def run():

    def emit(self, line: str) -> None:
        self.lines.append("    " * self.depth + line)

    def block(self, before: int) -> None:
        if len(self.lines) == before:
            self.emit("pass")


def _py_sql(text: str) -> str:
    """Spark SQL text as a Python string literal, kept readable."""
    if '"""' not in text and not text.endswith("\\") and not text.endswith('"'):
        if "\n" in text:
            return 'r"""' + text + '"""'
        if '"' not in text:
            return 'r"' + text + '"'
    return repr(text)


class ProcedureWriter:
    """Nodes -> the body of ``run()``."""

    def __init__(self, ctx: Context, table_vars: Set[str]) -> None:
        self.ctx = ctx
        self.conv = Converter(ctx)
        self.table_vars = table_vars
        self.out = Emitter()
        self.cursors: Set[str] = set()
        self.in_loop = 0

    def sql(self, toks: Sequence[Tok]) -> str:
        return self.conv.sql(toks, self.table_vars)

    def node(self, node: Node) -> None:
        if isinstance(node, Block):
            for n in node.body:
                self.node(n)
        elif isinstance(node, IfNode):
            self.out.emit(f"if {self.condition(node.cond)}:")
            self._indented(node.then)
            if node.other is not None:
                if isinstance(node.other, IfNode):
                    self.out.emit("else:")
                    self._indented(node.other)
                else:
                    self.out.emit("else:")
                    self._indented(node.other)
        elif isinstance(node, WhileNode):
            self.out.emit(f"while {self.condition(node.cond)}:")
            self.in_loop += 1
            self._indented(node.body)
            self.in_loop -= 1
        elif isinstance(node, TryNode):
            self.out.emit("try:")
            self._indented(node.body)
            self.out.emit("except Exception as exc:  # BEGIN CATCH")
            self.out.depth += 1
            before = len(self.out.lines)
            self.out.emit('v["error_message"], v["error_number"], v["error_severity"], v["error_state"] = str(exc)[:4000], 50000, 16, 1')
            self.ctx.variables.update({"error_message", "error_number", "error_severity", "error_state"})
            for n in node.catch.body:
                self.node(n)
            self.out.block(before)
            self.out.depth -= 1
        else:
            self.statement(node)

    def _indented(self, node: Node) -> None:
        self.out.depth += 1
        before = len(self.out.lines)
        self.node(node)
        self.out.block(before)
        self.out.depth -= 1

    def condition(self, toks: Sequence[Tok]) -> str:
        toks = list(toks)
        while len(toks) >= 2 and toks[0].text == "(" and _match(toks, 0) == len(toks) - 1:
            toks = toks[1:-1]
        text = text_of(toks)
        m = re.fullmatch(r"(NOT\s+)?OBJECT_ID\s*\(\s*N?'([^']+)'\s*(?:,\s*N?'\w+'\s*)?\)\s+IS\s+(NOT\s+)?NULL", text, re.I | re.S)
        if m:
            exists = bool(m.group(3)) != bool(m.group(1))
            return ("" if exists else "not ") + f"spark.catalog.tableExists({self._table_literal(m.group(2))!r})"
        return f"truth({_py_sql(self.sql(toks))})"

    def _table_literal(self, name: str) -> str:
        parts = [p.strip("[]") for p in name.split(".") if p]
        if parts and parts[-1].startswith("#"):
            return self.ctx.work_table(parts[-1])
        if len(parts) >= 3:
            parts = parts[-2:]
        return ".".join(parts)

    def _run(self, sql_text: str, dml: bool = True) -> None:
        call = f"q({_py_sql(sql_text)})"
        if dml and self.ctx.uses_rowcount:
            self.out.emit(f'v["rowcount"] = affected({call})')
        else:
            self.out.emit(call)

    def _review(self, why: str, toks: Sequence[Tok]) -> None:
        self.ctx.flag(why)
        self.out.emit(f"# REVIEW: {why}")
        for line in text_of(toks).splitlines() or [""]:
            self.out.emit("#   " + line)

    def _comment(self, text: str) -> None:
        self.out.emit("# " + text)

    # -- one statement ------------------------------------------------------------------

    def statement(self, stmt: Stmt) -> None:
        toks = stmt.toks
        head = stmt.head
        handler = {
            "SET": self._set, "DECLARE": self._declare, "SELECT": self._select, "INSERT": self._insert, "UPDATE": self._update,
            "DELETE": self._delete, "MERGE": self._merge, "TRUNCATE": self._truncate, "CREATE": self._create, "DROP": self._drop,
            "WITH": self._with, "EXEC": self._exec, "EXECUTE": self._exec, "RETURN": self._return, "PRINT": self._print,
            "RAISERROR": self._raiserror, "THROW": self._throw, "BEGIN": self._transaction, "COMMIT": self._transaction,
            "ROLLBACK": self._transaction, "SAVE": self._transaction, "BREAK": self._loop_word, "CONTINUE": self._loop_word,
            "OPEN": self._cursor_noop, "CLOSE": self._cursor_noop, "DEALLOCATE": self._cursor_noop, "FETCH": self._fetch,
            "RENAME": self._rename, "ALTER": self._alter, "WAITFOR": self._waitfor,
        }.get(head)
        if handler is None:
            if head == "USE":
                self._comment(f"removed: {text_of(toks)} (a notebook works in its default Lakehouse)")
                return
            self._review(f"{head or 'This'} statement has no Spark conversion", toks)
            return
        handler(toks)

    def _set(self, toks: List[Tok]) -> None:
        if len(toks) >= 3 and toks[1].kind == "var":
            name = py_name(toks[1].text[1:])
            op = toks[2].text
            rest = toks[3:]
            if rest and rest[0].is_("CURSOR"):
                self._review("Cursor variables are not converted", toks)
                return
            self.ctx.variables.add(name)
            expr = self.sql(rest)
            if op in ("+=", "-=", "*=", "/=", "%="):
                expr = f":{name} {op[0]} ({expr})"
            elif op != "=":
                self._review("Unrecognised SET", toks)
                return
            self.out.emit(f'v["{name}"] = scalar({_py_sql("SELECT " + expr)})')
            return
        option = " ".join(t.text.upper() for t in toks[1:3])
        if toks[1:2] and toks[1].is_("ROWCOUNT"):
            self._review("SET ROWCOUNT has no Spark equivalent (use LIMIT in the statements it limits)", toks)
            return
        if toks[1:2] and toks[1].is_("IDENTITY_INSERT"):
            self._comment("removed: SET IDENTITY_INSERT (a Lakehouse table has no IDENTITY; key values insert as they are)")
            return
        self._comment(f"removed: SET {option} ... (a session option with no meaning in Spark)")

    def _declare(self, toks: List[Tok]) -> None:
        body = toks[1:]
        if len(body) >= 2 and body[0].kind in ("word", "ident") and body[1].is_("CURSOR"):
            for_at = _top_index(body, lambda t: t.is_("FOR"))
            if for_at < 0:
                self._review("Cursor without a SELECT", toks)
                return
            select = body[for_at + 1:]
            upd = _top_index(select, lambda t: t.is_("FOR"))
            if upd >= 0:
                select = select[:upd]
            name = _slug(ident_value(body[0]))
            self.cursors.add(name)
            self.ctx.note("Cursor loops became loops over collected rows: they run row by row on the driver; "
                          "rewrite them as one set-based statement where you can.")
            self.out.emit(f"cursor_{name} = iter(q({_py_sql(self.sql(select))}).collect())")
            return
        for part in split_top(body):
            if not part or part[0].kind != "var":
                continue
            name = py_name(part[0].text[1:])
            rest = part[1:]
            if rest and rest[0].is_("AS"):
                rest = rest[1:]
            if rest and rest[0].is_("TABLE"):
                columns = rest[1:]
                self.table_vars.add(part[0].text[1:].lower())
                target = self.ctx.work_table(part[0].text)
                self.out.emit(f"q({_py_sql(f'CREATE OR REPLACE TABLE {target} {self._columns(columns)} USING DELTA')})")
                continue
            self.ctx.variables.add(name)
            eq = next((i for i, t in enumerate(rest) if t.kind == "op" and t.text == "="), -1)
            type_text = text_of(rest[:eq] if eq >= 0 else rest)
            if eq >= 0:
                expr = self.sql(rest[eq + 1:])
                self.out.emit(f'v["{name}"] = scalar({_py_sql(f"SELECT CAST({expr} AS {spark_type(type_text)})")})')
            else:
                self.out.emit(f'v["{name}"] = None  # {type_text}')

    def _columns(self, toks: Sequence[Tok]) -> str:
        """A T-SQL column list ``( col type [NULL|NOT NULL] [IDENTITY..] , ... )`` in Spark SQL."""
        toks = list(toks)
        if toks and toks[0].text == "(":
            toks = toks[1:_match(toks, 0)]
        cols = []
        for part in split_top(toks):
            if not part or part[0].is_("PRIMARY", "UNIQUE", "CONSTRAINT", "INDEX", "CHECK", "FOREIGN"):
                continue
            name = ident_value(part[0])
            type_end = 1
            while type_end < len(part) and not part[type_end].is_("NULL", "NOT", "IDENTITY", "PRIMARY", "UNIQUE",
                                                                   "DEFAULT", "CONSTRAINT", "COLLATE", "CHECK"):
                if part[type_end].text == "(":
                    type_end = _match(part, type_end)
                type_end += 1
            not_null = any(part[k].is_("NOT") and k + 1 < len(part) and part[k + 1].is_("NULL") for k in range(type_end, len(part)))
            cols.append(f"`{name}` {spark_type(text_of(part[1:type_end]))}{' NOT NULL' if not_null else ''}")
        return "(" + ", ".join(cols) + ")"

    def _select(self, toks: List[Tok]) -> None:
        body = list(toks)
        # SELECT @a = x, @b = y FROM ...: variables from the first row.
        from_at = _top_index(body, lambda t: t.is_("FROM", "WHERE", "GROUP", "ORDER", "UNION", "HAVING", "INTO"), 1)
        items = split_top(body[1:from_at if from_at >= 0 else len(body)])
        if items and all(len(it) >= 2 and it[0].kind == "var" and it[1].kind == "op" and it[1].text in ("=", "+=") for it in items):
            names = [py_name(it[0].text[1:]) for it in items]
            self.ctx.variables.update(names)
            exprs = []
            for name, it in zip(names, items):
                expr = self.sql(it[2:])
                exprs.append((f":{name} + ({expr})" if it[1].text == "+=" else expr) + f" AS `{name}`")
            rest = self.sql(body[from_at:]) if from_at >= 0 else ""
            select = "SELECT " + ", ".join(exprs) + (" " + rest if rest else "")
            self.out.emit(f"row = q({_py_sql(self._limit(select))}).first()")
            self.out.emit("if row is not None:")
            self.out.emit("    " + ", ".join(f'v["{n}"]' for n in names) + " = " + ", ".join(f"row[{k}]" for k in range(len(names))))
            return
        into_at = _top_index(body, lambda t: t.is_("INTO"))
        if into_at > 0:
            target_tok = body[into_at + 1]
            rest = body[into_at + 2:]
            k = 0
            name_toks = [target_tok]
            while k + 1 < len(rest) and rest[k].text == ".":
                name_toks += rest[k:k + 2]
                k += 2
            rest = rest[k:]
            select = self._limit(self.sql(body[:into_at] + rest))
            target = self.sql(name_toks)
            replace = "OR REPLACE " if target_tok.kind == "temp" else ""
            self._run(f"CREATE {replace}TABLE {target} USING DELTA AS {select}")
            return
        self.out.emit(f"display(q({_py_sql(self._limit(self.sql(body)))}))")

    def _limit(self, text: str) -> str:
        """``SELECT [DISTINCT] TOP (n) ...`` -> ``SELECT ... LIMIT n`` for the statement's outer SELECT."""
        m = re.match(r"(\s*SELECT\s+(?:DISTINCT\s+)?)TOP\s*\(?\s*(\d+|:\w+)\s*\)?(\s+PERCENT)?(\s+WITH\s+TIES)?\s+", text, re.I)
        if not m:
            if re.search(r"\bTOP\s*\(?\s*\d", text, re.I):
                self.ctx.flag("TOP inside a subquery: rewrite it with LIMIT or a window function.")
            return text
        if m.group(3) or m.group(4):
            self.ctx.flag("TOP ... PERCENT / WITH TIES has no Spark form: rewrite it with a window function.")
            return text
        body = text[m.end():]
        order = re.search(r"\bOPTION\s*\(.*\)\s*$", body, re.I | re.S)
        if order:
            body = body[:order.start()]
        return m.group(1) + body.rstrip().rstrip(";") + f" LIMIT {m.group(2)}"

    def _strip_option(self, toks: List[Tok]) -> List[Tok]:
        at = _top_index(toks, lambda t: t.is_("OPTION"))
        if at > 0 and at + 1 < len(toks) and toks[at + 1].text == "(":
            return toks[:at] + toks[_match(toks, at + 1) + 1:]
        return toks

    def _insert(self, toks: List[Tok]) -> None:
        toks = self._strip_option(toks)
        if len(toks) > 1 and not toks[1].is_("INTO"):
            toks = [toks[0], Tok("word", "INTO", " ")] + [Tok(toks[1].kind, toks[1].text, " ")] + toks[2:]
        exec_at = _top_index(toks, lambda t: t.is_("EXEC", "EXECUTE"))
        if exec_at > 0:
            self._review("INSERT ... EXEC: run the procedure's notebook and insert what it returns", toks)
            return
        sel = _top_index(toks, lambda t: t.is_("SELECT"))
        text = self.sql(toks)
        if sel > 0:
            prefix, select = self.sql(toks[:sel]), self.sql(toks[sel:])
            text = prefix + " " + self._limit(select)
        self._run(text)

    def _sources(self, toks: Sequence[Tok]) -> Optional[List[Tuple[List[Tok], str, List[Tok]]]]:
        """``FROM`` clause items: (table tokens, alias, ON condition tokens). None if it is not a plain join list."""
        out: List[Tuple[List[Tok], str, List[Tok]]] = []
        parts: List[List[Tok]] = [[]]
        conds: List[List[Tok]] = [[]]
        depth = 0
        k = 0
        toks = list(toks)
        while k < len(toks):
            t = toks[k]
            if t.text == "(" and t.kind == "op":
                depth += 1
            elif t.text == ")" and t.kind == "op":
                depth -= 1
            if depth == 0 and (t.is_("JOIN") or (t.kind == "op" and t.text == ",")):
                while parts[-1] and parts[-1][-1].is_("INNER"):
                    parts[-1].pop()
                if parts[-1] and parts[-1][-1].is_("LEFT", "RIGHT", "FULL", "OUTER", "CROSS"):
                    return None
                parts.append([])
                conds.append([])
                k += 1
                continue
            if depth == 0 and t.is_("ON"):
                k += 1
                start = k
                while k < len(toks) and not (toks[k].is_("JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS") and depth == 0) \
                        and not (toks[k].kind == "op" and toks[k].text == "," and depth == 0):
                    if toks[k].text == "(":
                        depth += 1
                    elif toks[k].text == ")":
                        depth -= 1
                    k += 1
                conds[-1] = toks[start:k]
                continue
            parts[-1].append(t)
            k += 1
        for part, cond in zip(parts, conds):
            if not part:
                return None
            if part[0].text == "(":
                close = _match(part, 0)
                table, rest = part[:close + 1], part[close + 1:]
            else:
                end = 1
                while end + 1 < len(part) and part[end].text == ".":
                    end += 2
                table, rest = part[:end], part[end:]
            if rest and rest[0].is_("AS"):
                rest = rest[1:]
            alias = ident_value(rest[0]) if rest else ""
            out.append((table, alias, cond))
        return out

    def _merge_from(self, toks: List[Tok], kind: str) -> bool:
        """UPDATE ... SET ... FROM ... / DELETE x FROM ... as a Delta MERGE. False when the shape is not one it can convert."""
        from_at = _top_index(toks, lambda t: t.is_("FROM"), 1)
        where_at = _top_index(toks, lambda t: t.is_("WHERE"), max(from_at, 1))
        set_at = _top_index(toks, lambda t: t.is_("SET"), 1) if kind == "UPDATE" else -1
        target_name = text_of(toks[1:set_at if kind == "UPDATE" else from_at])
        sources = self._sources(toks[from_at + 1:where_at if where_at > 0 else len(toks)])
        if not sources:
            return False
        key = target_name.replace("[", "").replace("]", "").lower()

        def matches(entry: Tuple[List[Tok], str, List[Tok]]) -> bool:
            table, alias, _ = entry
            names = {alias.lower(), text_of(table).replace("[", "").replace("]", "").lower(),
                     ident_value(table[-1]).lower() if table else ""}
            return key in names or key.split(".")[-1] in names

        target = next((s for s in sources if matches(s)), None)
        others = [s for s in sources if s is not target]
        if len(others) != 1:
            return False
        if target is None:
            target_table = toks[1:set_at if kind == "UPDATE" else from_at]
            target_alias = ""
        else:
            target_table, target_alias, _ = target
        other_table, other_alias, on = others[0]
        conditions = [c for c in (on, target[2] if target else []) if c]
        where = toks[where_at + 1:] if where_at > 0 else []
        if not conditions and where:
            conditions, where = [where], []
        if not conditions:
            return False
        on_text = " AND ".join(f"({self.sql(c)})" for c in conditions)
        alias_t = f" AS `{target_alias}`" if target_alias else ""
        alias_o = f" AS `{other_alias}`" if other_alias else ""
        source_sql = self.sql(other_table)
        lines = [f"MERGE INTO {self.sql(target_table)}{alias_t}", f"USING {source_sql}{alias_o}", f"ON {on_text}"]
        when = "WHEN MATCHED" + (f" AND ({self.sql(where)})" if where else "")
        if kind == "UPDATE":
            sets = []
            for item in split_top(toks[set_at + 1:from_at]):
                eq = next((i for i, t in enumerate(item) if t.kind == "op" and t.text == "="), -1)
                column = item[eq - 1] if eq > 0 else item[0]
                sets.append(f"`{ident_value(column)}` = {self.sql(item[eq + 1:])}")
            lines.append(f"{when} THEN UPDATE SET " + ", ".join(sets))
        else:
            lines.append(f"{when} THEN DELETE")
        self.ctx.note("UPDATE / DELETE with a join became a Delta MERGE: it fails if one target row matches several source "
                      "rows, where T-SQL picked one of them.")
        self._run("\n".join(lines))
        return True

    def _update(self, toks: List[Tok]) -> None:
        toks = self._strip_option(toks)
        if _top_index(toks, lambda t: t.is_("FROM"), 1) > 0:
            if not self._merge_from(toks, "UPDATE"):
                self._review("UPDATE with several joined tables (or an outer join): rewrite it as a MERGE", toks)
            return
        self._run(self.sql(toks))

    def _delete(self, toks: List[Tok]) -> None:
        toks = self._strip_option(toks)
        froms = [k for k in range(1, len(toks)) if toks[k].is_("FROM")]
        if len(toks) > 1 and toks[1].is_("FROM") and _top_index(toks, lambda t: t.is_("FROM"), 2) < 0:
            self._run(self.sql(toks))
            return
        if len(toks) > 1 and not toks[1].is_("FROM") and not froms:
            self._run(self.sql([toks[0], Tok("word", "FROM", " ")] + [Tok(toks[1].kind, toks[1].text, " ")] + toks[2:]))
            return
        if toks[1].is_("FROM"):
            toks = [toks[0]] + toks[2:]
        if not self._merge_from(toks, "DELETE"):
            self._review("DELETE with several joined tables: rewrite it as a MERGE", toks)

    def _merge(self, toks: List[Tok]) -> None:
        text = self.sql(self._strip_option(toks))
        if re.search(r"\bOUTPUT\b", text, re.I):
            self._review("MERGE ... OUTPUT has no Spark form", toks)
            return
        text = re.sub(r"\bWHEN\s+NOT\s+MATCHED\s+BY\s+TARGET\b", "WHEN NOT MATCHED", text, flags=re.I)
        text = re.sub(r"\bMERGE\s+(?!INTO\b)", "MERGE INTO ", text, count=1, flags=re.I)
        self._run(text)

    def _truncate(self, toks: List[Tok]) -> None:
        target = self.sql(toks[2:] if len(toks) > 2 and toks[1].is_("TABLE") else toks[1:])
        self._run(f"DELETE FROM {target}")

    def _create(self, toks: List[Tok]) -> None:
        if len(toks) > 1 and toks[1].is_("INDEX", "CLUSTERED", "NONCLUSTERED", "UNIQUE", "STATISTICS", "COLUMNSTORE"):
            self._comment(f"removed: {text_of(toks[:4])} ... (Delta has no indexes or statistics to create)")
            return
        if len(toks) > 2 and toks[1].is_("TABLE"):
            k = 2
            name = [toks[k]]
            while k + 2 < len(toks) and toks[k + 1].text == ".":
                name += toks[k + 1:k + 3]
                k += 2
            rest = toks[k + 1:]
            as_at = _top_index(rest, lambda t: t.is_("AS"))
            target = self.sql(name)
            replace = "OR REPLACE " if toks[2].kind == "temp" else ""
            if as_at >= 0:  # CTAS: storage options before AS are dropped
                self._run(f"CREATE {replace}TABLE {target} USING DELTA AS {self._limit(self.sql(rest[as_at + 1:]))}")
                return
            if rest and rest[0].text == "(":
                create = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE IF NOT EXISTS"
                self._run(f"{create} {target} {self._columns(rest)} USING DELTA", dml=False)
                return
        self._review("CREATE statement inside a procedure (views, procedures and functions are not created by notebooks)", toks)

    def _drop(self, toks: List[Tok]) -> None:
        if len(toks) > 2 and toks[1].is_("TABLE"):
            rest = toks[2:]
            if len(rest) > 2 and rest[0].is_("IF") and rest[1].is_("EXISTS"):
                rest = rest[2:]
            self._run(f"DROP TABLE IF EXISTS {self.sql(rest)}", dml=False)
            return
        if len(toks) > 1 and toks[1].is_("INDEX", "STATISTICS"):
            self._comment(f"removed: {text_of(toks)} (Delta has no indexes or statistics)")
            return
        self._review("DROP of something other than a table", toks)

    def _with(self, toks: List[Tok]) -> None:
        main = -1  # the statement after the last CTE's closing parenthesis
        depth, k, last_close = 0, 1, -1
        while k < len(toks):
            if toks[k].text == "(":
                depth += 1
            elif toks[k].text == ")":
                depth -= 1
                if depth == 0:
                    last_close = k
            elif depth == 0 and last_close > 0 and toks[k].is_("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"):
                main = k
                break
            k += 1
        if main < 0:
            self._review("Common table expression without a statement", toks)
            return
        ctes, rest = toks[:main], toks[main:]
        if rest[0].is_("SELECT"):
            self.out.emit(f"display(q({_py_sql(self.sql(ctes) + ' ' + self._limit(self.sql(rest)))}))")
            return
        if rest[0].is_("INSERT"):
            sel = _top_index(rest, lambda t: t.is_("SELECT"))
            if sel > 0:
                head = list(rest[:sel])
                if len(head) > 1 and not head[1].is_("INTO"):
                    head = [head[0], Tok("word", "INTO", " "), Tok(head[1].kind, head[1].text, " ")] + head[2:]
                self._run(self.sql(head) + " " + self.sql(ctes) + " " + self._limit(self.sql(rest[sel:])))
                return
        self._review("A common table expression feeding UPDATE / DELETE / MERGE: move it into the statement's source", toks)

    def _exec(self, toks: List[Tok]) -> None:
        body = toks[1:]
        result_var = None
        if len(body) >= 2 and body[0].kind == "var" and body[1].text == "=":
            result_var = py_name(body[0].text[1:])
            body = body[2:]
        if not body:
            return
        if body[0].kind == "var" or body[0].text == "(" or (body[0].kind == "word" and body[0].text.lower() in ("sp_executesql", "sys.sp_executesql")) \
                or (len(body) > 2 and ident_value(body[0]).lower() == "sys" and ident_value(body[2]).lower() == "sp_executesql"):
            self._review("Dynamic SQL is built as T-SQL text at run time: rewrite the text it builds as Spark SQL", toks)
            return
        k = 1
        while k + 1 < len(body) and body[k].text == ".":
            k += 2
        name = ".".join(ident_value(t) for t in body[:k] if t.text != ".")
        parts = name.split(".")
        short = ".".join(parts[-2:]).lower()
        notebook = self.ctx.notebooks.get(short) or self.ctx.notebooks.get(parts[-1].lower())
        if not notebook:
            self._review(f"EXEC {name}: not a procedure migrated in this run (system or external procedure)", toks)
            return
        args = split_top(body[k:])
        params = list(self.ctx.callee_params.get(short) or self.ctx.callee_params.get(parts[-1].lower()) or [])
        passed: List[str] = []
        outputs: List[Tuple[str, str]] = []
        for n, arg in enumerate(args):
            arg = [t for t in arg]
            is_out = bool(arg) and arg[-1].is_("OUT", "OUTPUT")
            if is_out:
                arg = arg[:-1]
            if len(arg) >= 3 and arg[0].kind == "var" and arg[1].text == "=":
                pname, value = arg[0].text[1:].lower(), arg[2:]
            elif n < len(params):
                pname, value = params[n], arg
            else:
                self._review(f"EXEC {name}: positional argument {n + 1} could not be matched to a parameter", toks)
                return
            if is_out and value and value[0].kind == "var":
                outputs.append((py_name(pname), py_name(value[0].text[1:])))
            passed.append(f'"{py_name(pname)}": {self._value(value)}')
        call = f"notebookutils.notebook.run({notebook!r}, 3600, {{{', '.join(passed)}}})"
        if outputs or result_var:
            self.out.emit(f"result = json.loads({call} or '{{}}')")
            for pname, var in outputs:
                self.ctx.variables.add(var)
                self.out.emit(f'v["{var}"] = result.get("{pname}")')
            if result_var:
                self.ctx.variables.add(result_var)
                self.out.emit(f'v["{result_var}"] = result.get("return_value", 0)')
        else:
            self.out.emit(call)

    def _value(self, toks: Sequence[Tok]) -> str:
        """A Python expression for a T-SQL argument value."""
        if len(toks) == 1:
            t = toks[0]
            if t.kind == "var":
                name = py_name(t.text[1:])
                self.ctx.variables.add(name)
                return f'v["{name}"]'
            if t.kind == "string":
                return repr(string_value(t))
            if t.kind == "number":
                return t.text
            if t.is_("NULL"):
                return "None"
        return f"scalar({_py_sql('SELECT ' + self.sql(toks))})"

    def _return(self, toks: List[Tok]) -> None:
        if len(toks) > 1:
            self.out.emit(f"return {self._value(toks[1:])}")
        else:
            self.out.emit("return 0")

    def _print(self, toks: List[Tok]) -> None:
        self.out.emit(f"print({self._value(toks[1:])})")

    def _raiserror(self, toks: List[Tok]) -> None:
        inner = toks[1:]
        if inner and inner[0].text == "(":
            inner = inner[1:_match(inner, 0)]
        args = split_top(inner)
        if len(args) < 3:
            self._review("RAISERROR without a severity", toks)
            return
        severity = text_of(args[1])
        message = self._value(args[0])
        if len(args) > 3:
            values = ", ".join(self.sql(a) for a in args[3:])
            message = f"scalar({_py_sql('SELECT format_string(' + self.sql(args[0]) + ', ' + values + ')')})"
        if severity.isdigit() and int(severity) <= 10:
            self.out.emit(f"print({message})  # RAISERROR with severity {severity} is informational")
        else:
            self.out.emit(f"raise Exception({message})")

    def _throw(self, toks: List[Tok]) -> None:
        args = split_top(toks[1:])
        if not args:
            self.out.emit("raise")
            return
        message = self._value(args[1]) if len(args) > 1 else repr("THROW")
        self.out.emit(f"raise Exception({message})")

    def _transaction(self, toks: List[Tok]) -> None:
        self.ctx.note("Transactions removed: each Spark statement commits on its own (Delta has no multi-statement transaction). "
                      "A failure part-way leaves the statements before it committed.")
        self._comment(f"removed: {text_of(toks)} (each Delta statement commits on its own)")

    def _loop_word(self, toks: List[Tok]) -> None:
        self.out.emit(toks[0].text.lower() if self.in_loop else "pass  # " + toks[0].text)

    def _cursor_noop(self, toks: List[Tok]) -> None:
        self._comment(f"{text_of(toks)}: not needed, the rows are already collected")

    def _fetch(self, toks: List[Tok]) -> None:
        from_at = _top_index(toks, lambda t: t.is_("FROM"))
        into_at = _top_index(toks, lambda t: t.is_("INTO"))
        if from_at < 0 or into_at < 0 or not toks[from_at + 1:into_at]:
            self._review("FETCH without FROM ... INTO", toks)
            return
        cursor = _slug(ident_value(toks[from_at + 1]))
        variables = [py_name(t.text[1:]) for t in toks[into_at + 1:] if t.kind == "var"]
        self.ctx.uses_fetch = True
        self.ctx.variables.update(variables)
        self.out.emit(f"row = next(cursor_{cursor}, None)")
        self.out.emit('v["fetch_status"] = 0 if row is not None else -1')
        self.out.emit("if row is not None:")
        self.out.emit("    " + ", ".join(f'v["{n}"]' for n in variables) + " = " + ", ".join(f"row[{k}]" for k in range(len(variables))))

    def _rename(self, toks: List[Tok]) -> None:
        to_at = _top_index(toks, lambda t: t.is_("TO"))
        start = 2 if len(toks) > 2 and toks[1].is_("OBJECT") else 1
        if to_at < 0:
            self._review("RENAME without TO", toks)
            return
        old = toks[start:to_at]
        if old and old[0].text.upper() == "OBJECT" and len(old) > 2:
            old = old[2:]
        schema = ".".join(ident_value(t) for t in old if t.text != ".").split(".")[:-1]
        new = ".".join(schema + [ident_value(toks[to_at + 1])])
        self._run(f"ALTER TABLE {self.sql(old)} RENAME TO {new}", dml=False)

    def _alter(self, toks: List[Tok]) -> None:
        if len(toks) > 1 and toks[1].is_("INDEX"):
            self._comment(f"removed: {text_of(toks[:4])} ... (Delta has no indexes)")
            return
        self._review("ALTER statement: check it against Delta's ALTER TABLE", toks)

    def _waitfor(self, toks: List[Tok]) -> None:
        if len(toks) > 2 and toks[1].is_("DELAY") and toks[2].kind == "string":
            hh, mm, ss = (string_value(toks[2]).split(":") + ["0", "0"])[:3]
            try:
                seconds = int(hh) * 3600 + int(mm) * 60 + float(ss)
            except ValueError:
                seconds = 0
            self.out.emit(f"time.sleep({seconds:g})")
            return
        self._review("WAITFOR TIME has no direct equivalent", toks)


# ============================================================================== the notebook


_HELPERS = '''import json
import re
import time

v = {}  # the procedure's variables, by name; Spark SQL reads them as :name


def q(sql):
    """Run one Spark SQL statement with the variables it uses as named parameters."""
    names = {n for n in v if re.search(r":" + n + r"\\b", sql)}
    return spark.sql(sql, args={n: v[n] for n in names}) if names else spark.sql(sql)


def scalar(sql):
    row = q(sql).first()
    return None if row is None else row[0]


def truth(condition):
    """A T-SQL condition, evaluated by Spark."""
    return not q("SELECT 1 WHERE " + condition).isEmpty()


def affected(df):
    """Rows a Delta INSERT, UPDATE, DELETE or MERGE changed (@@ROWCOUNT)."""
    try:
        row = df.first()
        return int(row["num_affected_rows"]) if row is not None and "num_affected_rows" in row.asDict() else 0
    except Exception:
        return 0
'''


@dataclass
class Converted:
    document: Dict[str, object]
    notes: List[str]
    review: List[str]


def _py_default(param: Param, ctx: Context) -> str:
    if param.default is None:
        return "None"
    toks = [t for t in tokenize(param.default) if t.text != ""]
    if len(toks) == 1:
        t = toks[0]
        if t.kind == "number":
            return t.text
        if t.kind == "string":
            return repr(string_value(t))
        if t.is_("NULL"):
            return "None"
    ctx.flag(f"Default of @{param.name} ({param.default}) is an expression: set it in the parameters cell.")
    return "None"


def procedure_notebook(text: str, *, name: str, lakehouse_id: str, lakehouse: str, workspace_id: str, pool: str = "",
                       notebooks: Optional[Mapping[str, str]] = None,
                       callee_params: Optional[Mapping[str, Sequence[str]]] = None) -> Converted:
    """A stored procedure's ``CREATE PROCEDURE`` text as a PySpark notebook attached to the Lakehouse."""
    proc_name, params, body = split_header(text)
    return _notebook(body, params, title=f"Stored procedure {proc_name or name}", work_prefix=_slug(name), lakehouse_id=lakehouse_id,
                     lakehouse=lakehouse, workspace_id=workspace_id, pool=pool, notebooks=notebooks, callee_params=callee_params)


def script_notebook(text: str, *, name: str, lakehouse_id: str, lakehouse: str, workspace_id: str, pool: str = "",
                    notebooks: Optional[Mapping[str, str]] = None,
                    callee_params: Optional[Mapping[str, Sequence[str]]] = None) -> Converted:
    """A SQL script as a PySpark notebook attached to the Lakehouse."""
    body = [t for t in tokenize(text) if t.text != ""]
    return _notebook(body, [], title=f"SQL script {name}", work_prefix=_slug(name), lakehouse_id=lakehouse_id, lakehouse=lakehouse,
                     workspace_id=workspace_id, pool=pool, notebooks=notebooks, callee_params=callee_params)


def _notebook(body: List[Tok], params: List[Param], *, title: str, work_prefix: str, lakehouse_id: str, lakehouse: str,
              workspace_id: str, pool: str, notebooks: Optional[Mapping[str, str]],
              callee_params: Optional[Mapping[str, Sequence[str]]]) -> Converted:
    ctx = Context(work_prefix=work_prefix, pool=pool, notebooks=notebooks or {}, callee_params=callee_params or {})
    ctx.uses_rowcount = any(t.kind == "sysvar" and t.text.upper() == "@@ROWCOUNT" for t in body)
    nodes = Parser(body).statements()
    writer = ProcedureWriter(ctx, set())
    for node in nodes:
        writer.node(node)
    writer.out.block(0)
    run_body = "\n".join(writer.out.lines)

    parameter_lines = [f"{py_name(p.name)} = {_py_default(p, ctx)}  # @{p.name} {p.sql_type}{' OUTPUT' if p.output else ''}" for p in params]
    names = [py_name(p.name) for p in params]
    outputs = [py_name(p.name) for p in params if p.output]
    setup = [_HELPERS]
    setup.append("v.update({" + ", ".join(f'"{n}": {n}' for n in names) + "})\n" if names else "")
    for var in sorted(ctx.variables - set(names)):
        setup.append(f'v.setdefault("{var}", None)\n')
    if ctx.uses_rowcount:
        setup.append('v.setdefault("rowcount", 0)\n')
    if ctx.uses_fetch:
        setup.append('v.setdefault("fetch_status", -1)\n')
    if ctx.work_tables:
        setup.append(f'spark.sql("CREATE SCHEMA IF NOT EXISTS {WORK_SCHEMA}")  # temporary tables of the procedure\n')

    work = sorted(ctx.work_tables)
    finally_lines = "".join(f'    spark.sql("DROP TABLE IF EXISTS {WORK_SCHEMA}.{work_prefix}_{_slug(w)}")\n' for w in work)
    exit_value = "{" + ", ".join([f'"{n}": v.get("{n}")' for n in outputs] + ['"return_value": result']) + "}"
    main = ("def run():\n" + run_body + "\n\n\n"
            + ("try:\n    result = run()\nfinally:  # the procedure's temporary tables are dropped, even when it fails\n"
               + finally_lines if work else "result = run()\n")
            + f"notebookutils.notebook.exit(json.dumps({exit_value}, default=str))\n")

    review_md = ""
    if ctx.review:
        review_md = "\n\n**Review before running** (marked `# REVIEW` in the code):\n" + "".join(f"\n* {r}" for r in ctx.review)
    notes_md = "".join(f"\n* {n}" for n in ctx.notes)
    intro = (f"# {title}\n\nConverted from Synapse T-SQL to Spark SQL by the migration run, for the Lakehouse "
             f"`{lakehouse}`. Parameters are in the next cell; a pipeline's notebook activity overrides them. The exit value "
             "is JSON with the return value and any OUTPUT parameters."
             + (f"\n\nWhat changed:{notes_md}" if notes_md else "") + review_md)
    document = lakehouse_document([("markdown", intro), ("code", "".join(setup)), ("code", main)], lakehouse_id, lakehouse, workspace_id,
                                  parameters="\n".join(parameter_lines) + "\n" if parameter_lines else "# This procedure has no parameters.\n")
    return Converted(document, list(ctx.notes), list(ctx.review))


# ============================================================================== classification


_WRITES = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|DROP|ALTER|EXEC|EXECUTE|RENAME|SELECT\b[^;]*?\bINTO)\b", re.I | re.S)


def changes_data(text: str) -> bool:
    """Whether T-SQL (a script) writes, creates or calls anything: if not, it can run as it is on the read-only SQL endpoint."""
    masked = " ".join(t.text if t.kind != "string" else "''" for t in tokenize(text or ""))
    return bool(_WRITES.search(masked))


def notebook_name(schema: str, name: str) -> str:
    """The Fabric notebook a stored procedure becomes."""
    return f"sp_{schema}_{name}"[:120]


def procedure_keys(schema: str, name: str) -> Iterable[str]:
    """The names a call may use for a procedure: ``schema.name`` and ``name``."""
    return (f"{schema}.{name}".lower(), name.lower())


def parameter_names(procedure: object) -> List[str]:
    """A discovered ``SqlProcedure``'s parameter names in order, without ``@``."""
    out = []
    for p in sorted(getattr(procedure, "parameters", ()) or (), key=lambda p: p.parameter_id):
        if not p.is_return_value and p.name:
            out.append(str(p.name).lstrip("@").lower())
    return out


def notebook_parameters(stored_procedure_parameters: Mapping[str, object]) -> Dict[str, Dict[str, object]]:
    """A Stored procedure activity's ``storedProcedureParameters`` as a Notebook activity's ``parameters``."""
    kinds = {"int16": "int", "int32": "int", "int64": "int", "decimal": "float", "double": "float", "single": "float",
             "boolean": "bool"}
    out: Dict[str, Dict[str, object]] = {}
    for name, spec in (stored_procedure_parameters or {}).items():
        spec = spec if isinstance(spec, Mapping) else {"value": spec}
        out[py_name(str(name))] = {"value": spec.get("value"), "type": kinds.get(str(spec.get("type") or "").lower(), "string")}
    return out


def dumps(document: Mapping[str, object]) -> str:
    return json.dumps(document)
