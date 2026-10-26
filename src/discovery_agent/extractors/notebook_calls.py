"""Notebooks that a notebook runs: ``%run`` and ``mssparkutils.notebook.run``.

Shared by discovery, which turns each call into a reference (so the called notebook lands in the same
or an earlier wave), and by the migration, which rewrites the call for Fabric. Only a call whose target
is written out literally counts: a name built at run time (an f-string, a variable, a concatenation)
cannot be proven to point anywhere, so it is reported as dynamic and never guessed.

Synapse names a notebook by its path in the workspace (``%run /Shared/helpers``) or by its name;
Fabric runs a notebook in the same workspace by its name, and the migration places every notebook at
the workspace root, so the name is the last segment of the path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

#: ``%run <target> [params]``: a line magic, on its own line. The target may be quoted.
_RUN_MAGIC = re.compile(
    r"^(?P<lead>[ \t]*%run[ \t]+(?:-[A-Za-z-]+[ \t]+)*)(?P<target>\"[^\"\n]*\"|'[^'\n]*'|[^\s{]+)",
    re.MULTILINE,
)
#: ``mssparkutils.notebook.run(<target>, ...)`` and its Fabric spelling ``notebookutils.notebook.run``.
_RUN_CALL = re.compile(
    r"\b(?:mssparkutils|notebookutils)\s*\.\s*notebook\s*\.\s*run\s*\(\s*"
    # A literal counts only when it is the whole argument: ``"/jobs/" + name`` is built at run time.
    r"(?P<arg>(?P<prefix>[A-Za-z]{0,2})(?P<quote>\"|')(?P<target>[^\"'\n]*)(?P=quote)(?=\s*[,)])|[^,)\s]+)"
)
_DYNAMIC = re.compile(r"[{}$+]")


@dataclass(frozen=True)
class NotebookCall:
    """One call to run another notebook. ``target`` is the notebook's name, or None when it is built at run time."""

    target: Optional[str]
    written: str  # the target exactly as the code spells it
    line: int  # 1-based, within the text scanned
    magic: bool  # %run, rather than a utilities call


def notebook_name(path: str) -> str:
    """``/Shared/Helpers/load.ipynb`` -> ``load``: the name Fabric runs a notebook by."""
    text = path.strip().strip("\"'").rstrip("/")
    name = text.rsplit("/", 1)[-1]
    return name[:-6] if name.lower().endswith(".ipynb") else name


def _literal_target(written: str) -> Optional[str]:
    text = written.strip()
    if text[:1] in "\"'":
        text = text[1:-1]
    if not text or _DYNAMIC.search(text) or text.startswith("@"):
        return None
    name = notebook_name(text)
    return name or None


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def find_calls(text: str) -> List[NotebookCall]:
    """Every call to run a notebook in one cell's code, in order."""
    calls: List[NotebookCall] = []
    for m in _RUN_MAGIC.finditer(text or ""):
        written = m.group("target")
        calls.append(NotebookCall(_literal_target(written), written, _line_of(text, m.start("target")), True))
    for m in _RUN_CALL.finditer(text or ""):
        if m.group("quote") and not (m.group("prefix") or "").lower().replace("r", ""):
            written = m.group("quote") + m.group("target") + m.group("quote")
            target = _literal_target(written)
        else:  # a variable, an f-string or an expression: only known at run time
            written, target = m.group("arg"), None
        calls.append(NotebookCall(target, written, _line_of(text, m.start("arg")), False))
    return sorted(calls, key=lambda c: c.line)


def rewrite_calls(text: str) -> Tuple[str, int]:
    """Point every literal call at the notebook's name, as Fabric runs it; how many calls changed.

    A call that already names a notebook without a folder is left exactly as written."""
    changed = 0

    def magic(m: "re.Match[str]") -> str:
        nonlocal changed
        written = m.group("target")
        name = _literal_target(written)
        if name is None or "/" not in written and not written.lower().rstrip("\"'").endswith(".ipynb"):
            return m.group(0)
        changed += 1
        return m.group("lead") + (name if " " not in name else f'"{name}"')

    def call(m: "re.Match[str]") -> str:
        nonlocal changed
        quote = m.group("quote")
        if not quote or (m.group("prefix") or "").lower().replace("r", ""):
            return m.group(0)
        written = m.group("target")
        name = _literal_target(quote + written + quote)
        if name is None or "/" not in written and not written.lower().endswith(".ipynb"):
            return m.group(0)
        changed += 1
        start = m.start("arg") - m.start(0)
        return m.group(0)[:start] + m.group("prefix") + quote + name + quote

    text = _RUN_MAGIC.sub(magic, text or "")
    text = _RUN_CALL.sub(call, text)
    return text, changed
