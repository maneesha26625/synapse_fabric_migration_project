"""Deterministic scanners for notebook source code.

Six small scanners rather than one enormous regex, each owning one kind of
observation and all working line by line so every finding carries a line
number:

    NotebookCodeScanner
      +- magic commands      %%sql, %pip, %%configure, %run
      +- Synapse APIs        mssparkutils, synapsesql, TokenLibrary, ...
      +- SQL blocks          %%sql cells, spark.sql("...") literals
      +- packages            pip/conda installs, %%configure, imports
      +- resource paths      abfss://, wasbs://, https://, synfs:/, ...
      +- credentials         secret APIs and literal assignments (redacted)

No LLM, no AST, no semantic inference. A construct is reported because a
deterministic rule matched a line, and the matched line travels with it as
evidence.

Credential evidence is redacted here, at the point of capture, so a secret
value cannot reach a finding, a summary, or a log. The cell's ``source`` is
left untouched — it is the artifact, and the migration worker needs it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from discovery_agent.extractors.notebook_models import (
    CodeFinding,
    DetectionMethod,
    FindingCategory,
    MagicCommand,
    MagicScope,
    NotebookLanguage,
    NotebookSqlBlock,
    PackageFinding,
    PackageKind,
    ResourceCategory,
    ResourceReference,
    SqlDetection,
)

EVIDENCE_MAX_CHARS = 200
REDACTED = "***"


@dataclass(frozen=True)
class CodeRule:
    """One construct worth reporting, and the pattern that finds it."""

    category: FindingCategory
    construct: str
    pattern: "re.Pattern"
    detection: DetectionMethod = DetectionMethod.CODE_PATTERN


def _rule(category, construct, pattern, detection=DetectionMethod.CODE_PATTERN):
    return CodeRule(category, construct, re.compile(pattern), detection)


# Ordered so the most specific construct wins when several could match a line.
CODE_RULES: Tuple[CodeRule, ...] = (
    # -- Synapse filesystem / notebook / credential utilities --------------
    _rule(FindingCategory.SYNAPSE_UTILS, "mssparkutils.fs", r"\bmssparkutils\.fs\b"),
    _rule(
        FindingCategory.NOTEBOOK_EXECUTION,
        "mssparkutils.notebook",
        r"\bmssparkutils\.notebook\.\w+",
    ),
    _rule(
        FindingCategory.CREDENTIAL,
        "mssparkutils.credentials",
        r"\bmssparkutils\.credentials\.\w+",
    ),
    _rule(FindingCategory.SYNAPSE_UTILS, "mssparkutils.env", r"\bmssparkutils\.env\b"),
    _rule(FindingCategory.SYNAPSE_UTILS, "mssparkutils", r"\bmssparkutils\b"),
    _rule(
        FindingCategory.NOTEBOOK_EXECUTION,
        "notebookutils.notebook",
        r"\bnotebookutils\.notebook\.\w+",
    ),
    _rule(
        FindingCategory.CREDENTIAL,
        "notebookutils.credentials",
        r"\bnotebookutils\.credentials\.\w+",
    ),
    _rule(FindingCategory.SYNAPSE_UTILS, "notebookutils", r"\bnotebookutils\b"),
    # -- Synapse SQL connector --------------------------------------------
    _rule(
        FindingCategory.SYNAPSE_SQL_CONNECTOR,
        "spark.read.synapsesql",
        r"\.read\s*\.\s*synapsesql\s*\(",
    ),
    _rule(
        FindingCategory.SYNAPSE_SQL_CONNECTOR,
        "spark.write.synapsesql",
        r"\.write\s*\.\s*synapsesql\s*\(",
    ),
    _rule(FindingCategory.SYNAPSE_SQL_CONNECTOR, "synapsesql", r"\bsynapsesql\s*\("),
    _rule(
        FindingCategory.SYNAPSE_SQL_CONNECTOR,
        "com.microsoft.spark.sqlanalytics",
        r"com\.microsoft\.spark\.sqlanalytics",
    ),
    # -- Linked service / token APIs ---------------------------------------
    _rule(
        FindingCategory.LINKED_SERVICE_API,
        "TokenLibrary.getConnectionString",
        r"\bTokenLibrary\.getConnectionString\w*\s*\(",
    ),
    _rule(
        FindingCategory.CREDENTIAL, "TokenLibrary.getSecret", r"\bTokenLibrary\.getSecret\s*\("
    ),
    _rule(FindingCategory.LINKED_SERVICE_API, "TokenLibrary", r"\bTokenLibrary\b"),
    _rule(
        FindingCategory.LINKED_SERVICE_API,
        "spark.storage.synapse.linkedServiceName",
        r"spark\.storage\.synapse\.[\w.]*linkedServiceName",
    ),
    _rule(
        FindingCategory.LINKED_SERVICE_API,
        "linkedServiceName",
        r"\blinkedServiceName\b",
    ),
    # -- Spark / workspace configuration -----------------------------------
    _rule(
        FindingCategory.WORKSPACE_CONFIG,
        "hadoopConfiguration.set",
        r"hadoopConfiguration\(\)\s*\.\s*set",
    ),
    _rule(FindingCategory.SESSION_CONFIG, "spark.conf.set", r"\bspark\.conf\.set\s*\("),
    _rule(FindingCategory.SPARK_API, "spark.sql", r"\bspark\.sql\s*\("),
    _rule(
        FindingCategory.SPARK_API,
        "saveAsTable",
        r"\.saveAsTable\s*\(",
    ),
    _rule(FindingCategory.SPARK_API, "spark.read.load", r"\bspark\.read\b"),
    _rule(
        FindingCategory.WORKSPACE_CONFIG,
        "synapse.workspace",
        r"\bdev\.azuresynapse\.net\b",
    ),
)

# Literal assignments that name a secret. Matched on the key, never on the
# value -- a string is only interesting because of what it was assigned to.
CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(password|passwd|pwd|secret|secrets|token|api_?key|access_?key|"
    r"account_?key|sas_?token|connection_?string|conn_?str|client_?secret)\s*"
    r"=\s*[\"'][^\"']+[\"']",
    re.IGNORECASE,
)

_QUOTED_LITERAL = re.compile(r"([\"'])(?:(?!\1).)*\1")

# Magics. A cell magic only counts on the first non-blank line, which is the
# real Synapse rule; a line magic may appear anywhere. Requiring a letter
# after "%" keeps Python's modulo and %-formatting out of the results.
CELL_MAGIC = re.compile(r"^\s*%%(?P<name>[A-Za-z][\w.-]*)(?P<args>.*)$")
LINE_MAGIC = re.compile(r"^\s*%(?P<name>[A-Za-z][\w.-]*)(?P<args>.*)$")

KNOWN_MAGICS = frozenset(
    {
        "sql", "pyspark", "spark", "scala", "csharp", "python", "sparkr", "r",
        "markdown", "md", "html", "configure", "pip", "conda", "run", "sh",
        "bash", "time", "timeit", "matplotlib", "load_ext", "env", "writefile",
        "capture", "synapse",
    }
)

# Magics that set the language of the cell they head.
MAGIC_LANGUAGES = {
    "sql": NotebookLanguage.SQL,
    "pyspark": NotebookLanguage.PYTHON,
    "python": NotebookLanguage.PYTHON,
    "spark": NotebookLanguage.SCALA,
    "scala": NotebookLanguage.SCALA,
    "csharp": NotebookLanguage.CSHARP,
    "sparkr": NotebookLanguage.R,
    "r": NotebookLanguage.R,
    "markdown": NotebookLanguage.MARKDOWN,
    "md": NotebookLanguage.MARKDOWN,
}

PACKAGE_INSTALL = re.compile(
    r"^\s*[%!]\s*(?P<tool>pip|conda)\s+install\s+(?P<args>.+)$", re.IGNORECASE
)
IMPORT_STATEMENT = re.compile(r"^\s*import\s+(?P<modules>[\w.,\s]+?)(?:\s+as\s+\w+)?\s*$")
FROM_IMPORT = re.compile(r"^\s*from\s+(?P<module>[\w.]+)\s+import\s+")

SPARK_SQL_LITERAL = re.compile(
    r"""\bspark\s*\.\s*sql\s*\(\s*(?P<quote>\"\"\"|'''|\"|')(?P<sql>.*?)(?P=quote)""",
    re.DOTALL,
)

URI_PATTERN = re.compile(
    r"\b(?P<scheme>abfss|abfs|wasbs|wasb|adl|synfs|dbfs|hdfs|s3a|s3n|s3|gs|https|http|file)"
    r"://[^\s'\"`,\)\]\}]+"
)
SYNFS_PATTERN = re.compile(r"\bsynfs:/[^\s'\"`,\)\]\}]+")
SYNAPSE_MOUNT = re.compile(r"/synapse/workspaces/[^\s'\"`,\)\]\}]+")

SCHEME_CATEGORIES = {
    "abfss": ResourceCategory.STORAGE,
    "abfs": ResourceCategory.STORAGE,
    "wasbs": ResourceCategory.STORAGE,
    "wasb": ResourceCategory.STORAGE,
    "adl": ResourceCategory.STORAGE,
    "hdfs": ResourceCategory.STORAGE,
    "s3": ResourceCategory.STORAGE,
    "s3a": ResourceCategory.STORAGE,
    "s3n": ResourceCategory.STORAGE,
    "gs": ResourceCategory.STORAGE,
    "http": ResourceCategory.ENDPOINT,
    "https": ResourceCategory.ENDPOINT,
    "synfs": ResourceCategory.MOUNT,
    "dbfs": ResourceCategory.MOUNT,
    "file": ResourceCategory.LOCAL_PATH,
}

# A fixed list, deliberately not ``sys.stdlib_module_names``: that varies by
# interpreter version, and the same snapshot must extract identically
# everywhere. Incomplete by nature, which is why the field is Optional.
PYTHON_STANDARD_LIBRARY = frozenset(
    {
        "abc", "argparse", "ast", "asyncio", "base64", "collections", "concurrent",
        "contextlib", "copy", "csv", "dataclasses", "datetime", "decimal", "enum",
        "functools", "glob", "gzip", "hashlib", "http", "importlib", "io",
        "itertools", "json", "logging", "math", "multiprocessing", "operator",
        "os", "pathlib", "pickle", "platform", "queue", "random", "re",
        "shutil", "socket", "sqlite3", "statistics", "string", "struct",
        "subprocess", "sys", "tempfile", "textwrap", "threading", "time",
        "typing", "unittest", "urllib", "uuid", "warnings", "xml", "zipfile",
    }
)


def _evidence(line: str, redact: bool = False) -> str:
    """The matched line as evidence, trimmed and redacted where needed."""
    text = line.strip()
    if redact:
        text = _QUOTED_LITERAL.sub(REDACTED, text)
    if len(text) > EVIDENCE_MAX_CHARS:
        text = text[:EVIDENCE_MAX_CHARS] + "..."
    return text


def _at(location: str, line_number: int) -> str:
    return f"{location}:L{line_number}"


@dataclass(frozen=True)
class CellScan:
    """Everything the scanners observed in one cell."""

    magics: Tuple[MagicCommand, ...] = ()
    findings: Tuple[CodeFinding, ...] = ()
    sql_blocks: Tuple[NotebookSqlBlock, ...] = ()
    resources: Tuple[ResourceReference, ...] = ()
    packages: Tuple[PackageFinding, ...] = ()
    magic_language: Optional[NotebookLanguage] = None
    unknown_magics: Tuple[str, ...] = ()


class NotebookCodeScanner:
    """Runs every scanner over one cell's source."""

    def __init__(self, rules: Sequence[CodeRule] = CODE_RULES) -> None:
        self.rules = tuple(rules)

    def scan(self, source: str, cell_index: int, location: str) -> CellScan:
        lines = source.splitlines()
        magics, magic_language, unknown = self._scan_magics(lines, cell_index, location)

        findings: List[CodeFinding] = list(
            self._scan_rules(lines, cell_index, location)
        ) + list(self._scan_credentials(lines, cell_index, location))
        findings.extend(
            CodeFinding(
                category=FindingCategory.MAGIC_COMMAND,
                construct=f"%{'%' if magic.scope is MagicScope.CELL else ''}{magic.name}",
                cell_index=cell_index,
                location=magic.location,
                evidence=_evidence(f"%{'%' if magic.scope is MagicScope.CELL else ''}"
                                   f"{magic.name}{magic.arguments or ''}"),
                detection=DetectionMethod.MAGIC,
            )
            for magic in magics
        )

        packages = self._scan_packages(lines, cell_index, location, magics, source)
        findings.extend(
            CodeFinding(
                category=FindingCategory.PACKAGE_INSTALL,
                construct=f"{package.kind.value}:{package.name}",
                cell_index=cell_index,
                location=package.location,
                evidence=_evidence(package.name + (package.specifier or "")),
                detection=DetectionMethod.CONFIGURE_MAGIC
                if package.kind is PackageKind.SPARK_PACKAGE
                else DetectionMethod.CODE_PATTERN,
            )
            for package in packages
            if package.kind is not PackageKind.IMPORT
        )

        resources = self._scan_resources(lines, cell_index, location)
        findings.extend(
            CodeFinding(
                category=FindingCategory.RESOURCE_PATH,
                construct=f"{resource.scheme}://",
                cell_index=cell_index,
                location=resource.location,
                evidence=_evidence(resource.uri),
                detection=DetectionMethod.URI_SCHEME,
            )
            for resource in resources
        )

        return CellScan(
            magics=magics,
            findings=tuple(findings),
            sql_blocks=self._scan_sql(source, lines, cell_index, location, magics),
            resources=resources,
            packages=packages,
            magic_language=magic_language,
            unknown_magics=unknown,
        )

    # -- individual scanners ------------------------------------------------

    def _scan_magics(
        self, lines: Sequence[str], cell_index: int, location: str
    ) -> Tuple[Tuple[MagicCommand, ...], Optional[NotebookLanguage], Tuple[str, ...]]:
        magics: List[MagicCommand] = []
        unknown: List[str] = []
        language: Optional[NotebookLanguage] = None
        seen_content = False

        for number, line in enumerate(lines, start=1):
            cell_match = CELL_MAGIC.match(line)
            if cell_match and not seen_content:
                name = cell_match.group("name")
                recognized = name.lower() in KNOWN_MAGICS
                magics.append(
                    MagicCommand(
                        name=name,
                        scope=MagicScope.CELL,
                        arguments=cell_match.group("args") or None,
                        cell_index=cell_index,
                        location=_at(location, number),
                        recognized=recognized,
                    )
                )
                if not recognized:
                    unknown.append(f"%%{name}")
                language = language or MAGIC_LANGUAGES.get(name.lower())
                seen_content = True
                continue
            if line.strip():
                seen_content = True
            if cell_match:
                continue
            line_match = LINE_MAGIC.match(line)
            if line_match:
                name = line_match.group("name")
                recognized = name.lower() in KNOWN_MAGICS
                magics.append(
                    MagicCommand(
                        name=name,
                        scope=MagicScope.LINE,
                        arguments=line_match.group("args") or None,
                        cell_index=cell_index,
                        location=_at(location, number),
                        recognized=recognized,
                    )
                )
                if not recognized:
                    unknown.append(f"%{name}")
        return tuple(magics), language, tuple(unknown)

    def _scan_rules(self, lines, cell_index, location):
        """First matching rule per line wins, so a line yields one construct."""
        for number, line in enumerate(lines, start=1):
            for rule in self.rules:
                if rule.pattern.search(line):
                    yield CodeFinding(
                        category=rule.category,
                        construct=rule.construct,
                        cell_index=cell_index,
                        location=_at(location, number),
                        evidence=_evidence(
                            line, redact=rule.category is FindingCategory.CREDENTIAL
                        ),
                        detection=rule.detection,
                    )
                    break

    def _scan_credentials(self, lines, cell_index, location):
        """Literal secret assignments. The value never leaves this function."""
        for number, line in enumerate(lines, start=1):
            match = CREDENTIAL_ASSIGNMENT.search(line)
            if match:
                yield CodeFinding(
                    category=FindingCategory.CREDENTIAL,
                    construct=f"literal_assignment:{match.group(1).lower()}",
                    cell_index=cell_index,
                    location=_at(location, number),
                    evidence=_evidence(line, redact=True),
                    detection=DetectionMethod.CODE_PATTERN,
                )

    def _scan_sql(
        self, source, lines, cell_index, location, magics
    ) -> Tuple[NotebookSqlBlock, ...]:
        blocks: List[NotebookSqlBlock] = []

        cell_magic = next(
            (m for m in magics if m.scope is MagicScope.CELL and m.name.lower() == "sql"),
            None,
        )
        if cell_magic is not None:
            # Everything after the magic line is the statement, kept verbatim.
            magic_line = int(cell_magic.location.rsplit(":L", 1)[1])
            body = "\n".join(lines[magic_line:])
            if body.strip():
                blocks.append(
                    NotebookSqlBlock(
                        cell_index=cell_index,
                        sql=body,
                        detection=SqlDetection.MAGIC_CELL,
                        location=_at(location, magic_line + 1),
                    )
                )

        for match in SPARK_SQL_LITERAL.finditer(source):
            sql = match.group("sql")
            if not sql.strip():
                continue
            line_number = source[: match.start()].count("\n") + 1
            blocks.append(
                NotebookSqlBlock(
                    cell_index=cell_index,
                    sql=sql,
                    detection=SqlDetection.SPARK_SQL_CALL,
                    location=_at(location, line_number),
                )
            )
        return tuple(blocks)

    def _scan_packages(
        self, lines, cell_index, location, magics, source
    ) -> Tuple[PackageFinding, ...]:
        packages: List[PackageFinding] = []

        for number, line in enumerate(lines, start=1):
            install = PACKAGE_INSTALL.match(line)
            if install:
                kind = (
                    PackageKind.PIP_INSTALL
                    if install.group("tool").lower() == "pip"
                    else PackageKind.CONDA_INSTALL
                )
                for token in install.group("args").split():
                    if token.startswith("-"):
                        continue
                    name, specifier = _split_requirement(token)
                    packages.append(
                        PackageFinding(
                            name=name,
                            kind=kind,
                            specifier=specifier,
                            location=_at(location, number),
                            cell_index=cell_index,
                        )
                    )
                continue

            module = _imported_module(line)
            if module:
                for name in module:
                    root = name.split(".")[0]
                    packages.append(
                        PackageFinding(
                            name=name,
                            kind=PackageKind.IMPORT,
                            location=_at(location, number),
                            cell_index=cell_index,
                            is_standard_library=root in PYTHON_STANDARD_LIBRARY,
                        )
                    )

        packages.extend(self._scan_configure(magics, lines, cell_index, location))
        return tuple(packages)

    @staticmethod
    def _scan_configure(magics, lines, cell_index, location):
        """Spark packages declared in a %%configure cell's JSON body."""
        configure = next(
            (m for m in magics if m.name.lower() == "configure"), None
        )
        if configure is None:
            return []
        magic_line = int(configure.location.rsplit(":L", 1)[1])
        body = "\n".join(lines[magic_line:])
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            return []
        if not isinstance(parsed, dict):
            return []
        found = []
        for key in ("jars", "packages", "pyFiles", "files"):
            values = parsed.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str) and value:
                    name, specifier = _split_requirement(value)
                    found.append(
                        PackageFinding(
                            name=name,
                            kind=PackageKind.SPARK_PACKAGE,
                            specifier=specifier,
                            location=_at(location, magic_line + 1),
                            cell_index=cell_index,
                        )
                    )
        return found

    @staticmethod
    def _scan_resources(lines, cell_index, location) -> Tuple[ResourceReference, ...]:
        resources: List[ResourceReference] = []
        seen = set()
        for number, line in enumerate(lines, start=1):
            for match in URI_PATTERN.finditer(line):
                uri = match.group(0).rstrip(".,;")
                scheme = match.group("scheme").lower()
                key = (uri, number)
                if key in seen:
                    continue
                seen.add(key)
                resources.append(
                    ResourceReference(
                        uri=uri,
                        scheme=scheme,
                        category=SCHEME_CATEGORIES.get(scheme, ResourceCategory.STORAGE),
                        location=_at(location, number),
                        detection=DetectionMethod.URI_SCHEME,
                        cell_index=cell_index,
                    )
                )
            for pattern, scheme in ((SYNFS_PATTERN, "synfs"), (SYNAPSE_MOUNT, "mount")):
                for match in pattern.finditer(line):
                    uri = match.group(0).rstrip(".,;")
                    key = (uri, number)
                    if key in seen:
                        continue
                    seen.add(key)
                    resources.append(
                        ResourceReference(
                            uri=uri,
                            scheme=scheme,
                            category=ResourceCategory.MOUNT,
                            location=_at(location, number),
                            detection=DetectionMethod.URI_SCHEME,
                            cell_index=cell_index,
                        )
                    )
        return tuple(resources)


def _split_requirement(token: str) -> Tuple[str, Optional[str]]:
    """Split ``pandas==1.5`` into its name and its specifier, verbatim."""
    for separator in ("==", ">=", "<=", "~=", ">", "<", "="):
        if separator in token:
            name, _, rest = token.partition(separator)
            return name.strip(), separator + rest.strip()
    return token.strip(), None


def _imported_module(line: str) -> Tuple[str, ...]:
    """The module names an import statement names, or an empty tuple."""
    from_match = FROM_IMPORT.match(line)
    if from_match:
        return (from_match.group("module"),)
    import_match = IMPORT_STATEMENT.match(line)
    if import_match:
        modules = [m.strip() for m in import_match.group("modules").split(",")]
        return tuple(m for m in modules if m)
    return ()
