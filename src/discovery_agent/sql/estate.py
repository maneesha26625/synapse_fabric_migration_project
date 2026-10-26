"""What the dedicated pool says about itself beyond its objects: data volume, row-level security,
external sources and formats, and the workload it runs.

Read with registered catalog queries (``sql.queries``), like everything else in discovery, and kept
apart from the object catalog because each part needs a permission the object catalog does not
(``VIEW DATABASE STATE`` for sizes and query history). Every part degrades on its own: one that cannot
be read is recorded as unknown, with an issue, never as "none".

* **Table sizes**: rows and reserved space per table, from the distribution-level partition stats.
  A replicated table keeps a full copy per compute node, so its figures are one node's, not the sum.
* **Row-level security**: each security policy with its predicates (``FILTER`` / ``BLOCK``).
* **External data sources and file formats**: their names, locations, types and the name of the
  database-scoped credential a source uses. Never a secret: the catalog does not hold one.
* **Workload**: the most demanding recent SELECT statements (by runs x average time) from the
  request history, and the peak number of user requests running at once. The pool keeps only its
  last 10,000 requests, so this is a recent sample, not a history, and says over which window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.extractors.models import ExtractionIssue, IssueCode

#: A statement in the history that writes, or depends on session state, cannot be replayed safely.
_NOT_REPLAYABLE = ("into", "#", "@@", "@", "insert", "update", "delete", "merge", "exec", "create", "alter", "drop", "truncate")
_PAGE_KB = 8


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    return None


def _when(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class SqlTableSize:
    schema: str
    table: str
    rows: int
    reserved_kb: int

    def to_dict(self) -> dict:
        return {"schema": self.schema, "table": self.table, "rows": self.rows, "reservedKb": self.reserved_kb}


@dataclass(frozen=True)
class SqlPredicate:
    predicate_type: str  # FILTER | BLOCK
    definition: str
    target_schema: str
    target_table: str
    operation: Optional[str] = None  # BLOCK predicates: AFTER INSERT, BEFORE UPDATE, ...

    def to_dict(self) -> dict:
        return {"type": self.predicate_type, "operation": self.operation, "definition": self.definition,
                "target": f"{self.target_schema}.{self.target_table}"}


@dataclass(frozen=True)
class SqlSecurityPolicy:
    """One row-level security policy and its predicates."""

    schema: str
    name: str
    enabled: bool
    predicates: Tuple[SqlPredicate, ...] = ()

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def filters(self) -> Tuple[SqlPredicate, ...]:
        return tuple(p for p in self.predicates if p.predicate_type == "FILTER")

    @property
    def blocks(self) -> Tuple[SqlPredicate, ...]:
        return tuple(p for p in self.predicates if p.predicate_type != "FILTER")

    def to_dict(self) -> dict:
        return {"schema": self.schema, "name": self.name, "enabled": self.enabled, "predicates": [p.to_dict() for p in self.predicates]}


@dataclass(frozen=True)
class SqlExternalDataSource:
    name: str
    location: Optional[str]
    source_type: Optional[str]
    credential: Optional[str] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "location": self.location, "type": self.source_type, "credential": self.credential}


@dataclass(frozen=True)
class SqlExternalFileFormat:
    name: str
    format_type: Optional[str]
    field_terminator: Optional[str] = None
    compression: Optional[str] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "formatType": self.format_type, "fieldTerminator": self.field_terminator, "compression": self.compression}


@dataclass(frozen=True)
class SqlQuerySample:
    """One distinct SELECT from the request history, with how often it ran and how long it took on Synapse."""

    text: str
    runs: int
    avg_ms: int
    max_ms: int
    min_ms: int
    label: Optional[str] = None
    last_run: Optional[str] = None

    @property
    def replayable(self) -> bool:
        """A plain read: no SELECT INTO, temp tables, variables or writes, so running it on Fabric changes nothing."""
        words = self.text.lower().replace("(", " ").replace(")", " ").replace(",", " ").split()
        lowered = self.text.lower()
        return self.text.lstrip().lower().startswith(("select", "with")) and not any(
            w in words for w in _NOT_REPLAYABLE if w.isalpha()) and not any(c in lowered for c in ("#", "@"))

    def to_dict(self) -> dict:
        return {"text": self.text, "runs": self.runs, "avgMs": self.avg_ms, "maxMs": self.max_ms, "minMs": self.min_ms,
                "label": self.label, "lastRun": self.last_run, "replayable": self.replayable}


@dataclass(frozen=True)
class SqlWorkload:
    samples: Tuple[SqlQuerySample, ...] = ()
    #: User requests running at the same moment, at most, within the window.
    peak_concurrency: int = 0
    requests: int = 0
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    #: Total time spent running user requests in the window (a busy-ness measure, not wall-clock).
    busy_ms: int = 0

    def to_dict(self) -> dict:
        return {"samples": [s.to_dict() for s in self.samples], "peakConcurrency": self.peak_concurrency, "requests": self.requests,
                "windowStart": self.window_start, "windowEnd": self.window_end, "busyMs": self.busy_ms}


@dataclass(frozen=True)
class SqlEstate:
    sizes: Tuple[SqlTableSize, ...] = ()
    policies: Tuple[SqlSecurityPolicy, ...] = ()
    data_sources: Tuple[SqlExternalDataSource, ...] = ()
    file_formats: Tuple[SqlExternalFileFormat, ...] = ()
    workload: Optional[SqlWorkload] = None
    #: Which parts could be read. False means "unknown", never "none".
    sizes_readable: bool = True
    policies_readable: bool = True
    external_readable: bool = True
    workload_readable: bool = True
    issues: Tuple[ExtractionIssue, ...] = field(default=())

    def size_of(self, schema: str, table: str) -> Optional[SqlTableSize]:
        s, t = schema.lower(), table.lower()
        return next((x for x in self.sizes if x.schema.lower() == s and x.table.lower() == t), None)

    def policy(self, schema: str, name: str) -> Optional[SqlSecurityPolicy]:
        s, n = schema.lower(), name.lower()
        return next((p for p in self.policies if p.schema.lower() == s and p.name.lower() == n), None)

    @property
    def total_rows(self) -> int:
        return sum(x.rows for x in self.sizes)

    @property
    def total_kb(self) -> int:
        return sum(x.reserved_kb for x in self.sizes)

    def summary(self) -> dict:
        return {
            "tablesSized": len(self.sizes) if self.sizes_readable else None,
            "totalRows": self.total_rows if self.sizes_readable else None,
            "totalGb": round(self.total_kb / 1024 / 1024, 2) if self.sizes_readable else None,
            "securityPolicies": len(self.policies) if self.policies_readable else None,
            "externalDataSources": len(self.data_sources) if self.external_readable else None,
            "externalFileFormats": len(self.file_formats) if self.external_readable else None,
            "peakConcurrency": self.workload.peak_concurrency if self.workload is not None else None,
            "querySamples": len(self.workload.samples) if self.workload is not None else None,
        }

    def to_dict(self) -> dict:
        return {"sizes": [s.to_dict() for s in self.sizes], "policies": [p.to_dict() for p in self.policies],
                "dataSources": [d.to_dict() for d in self.data_sources], "fileFormats": [f.to_dict() for f in self.file_formats],
                "workload": self.workload.to_dict() if self.workload is not None else None, "summary": self.summary()}


# ---- building it from rows -------------------------------------------------------------------


def build_sizes(rows: Sequence[Mapping[str, Any]], replicated: Sequence[Tuple[str, str]] = ()) -> Tuple[SqlTableSize, ...]:
    """Rows are per (table, compute node). A distributed table's parts add up; a replicated one is one node's copy."""
    copies = {(s.lower(), t.lower()) for s, t in replicated}
    per: Dict[Tuple[str, str], List[Tuple[int, int]]] = {}
    names: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for row in rows:
        schema, table = _text(row.get("schema_name")), _text(row.get("table_name"))
        if not schema or not table:
            continue
        key = (schema.lower(), table.lower())
        names[key] = (schema, table)
        per.setdefault(key, []).append((_int(row.get("row_count")) or 0, (_int(row.get("reserved_pages")) or 0) * _PAGE_KB))
    out = []
    for key, parts in per.items():
        if key in copies:
            rows_, kb = max(parts)
        else:
            rows_, kb = sum(p[0] for p in parts), sum(p[1] for p in parts)
        out.append(SqlTableSize(names[key][0], names[key][1], rows_, kb))
    return tuple(sorted(out, key=lambda x: (x.schema.lower(), x.table.lower())))


def build_policies(rows: Sequence[Mapping[str, Any]]) -> Tuple[SqlSecurityPolicy, ...]:
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        schema, name = _text(row.get("policy_schema")), _text(row.get("policy_name"))
        if not schema or not name:
            continue
        entry = grouped.setdefault((schema, name), {"enabled": _bool(row.get("is_enabled")) is not False, "predicates": []})
        definition = _text(row.get("predicate_definition"))
        ts, tt = _text(row.get("target_schema")), _text(row.get("target_name"))
        if definition and ts and tt:
            entry["predicates"].append(SqlPredicate((_text(row.get("predicate_type")) or "FILTER").upper(), definition, ts, tt,
                                                    _text(row.get("operation"))))
    return tuple(SqlSecurityPolicy(s, n, e["enabled"], tuple(e["predicates"])) for (s, n), e in sorted(grouped.items()))


def build_data_sources(rows: Sequence[Mapping[str, Any]]) -> Tuple[SqlExternalDataSource, ...]:
    return tuple(SqlExternalDataSource(n, _text(r.get("location")), _text(r.get("source_type")), _text(r.get("credential_name")))
                 for r in rows for n in [_text(r.get("source_name"))] if n)


def build_file_formats(rows: Sequence[Mapping[str, Any]]) -> Tuple[SqlExternalFileFormat, ...]:
    return tuple(SqlExternalFileFormat(n, _text(r.get("format_type")), _text(r.get("field_terminator")), _text(r.get("data_compression")))
                 for r in rows for n in [_text(r.get("format_name"))] if n)


def build_workload(sample_rows: Sequence[Mapping[str, Any]], request_rows: Sequence[Mapping[str, Any]], limit: int = 25) -> SqlWorkload:
    samples = []
    for r in sample_rows:
        text = _text(r.get("command_text"))
        if not text:
            continue
        last = r.get("last_run")
        samples.append(SqlQuerySample(text, _int(r.get("run_count")) or 0, _int(r.get("avg_ms")) or 0, _int(r.get("max_ms")) or 0,
                                      _int(r.get("min_ms")) or 0, _text(r.get("query_label")),
                                      last.isoformat() if isinstance(last, datetime) else _text(last)))
    events: List[Tuple[datetime, int]] = []
    busy = 0
    starts: List[datetime] = []
    ends: List[datetime] = []
    for r in request_rows:
        start, end = _when(r.get("start_time")), _when(r.get("end_time"))
        if start is None or end is None or end < start:
            continue
        events += [(start, 1), (end, -1)]
        starts.append(start)
        ends.append(end)
        busy += _int(r.get("elapsed_ms")) or int((end - start).total_seconds() * 1000)
    peak = running = 0
    for _, step in sorted(events, key=lambda e: (e[0], e[1])):  # an end before a start at the same instant
        running += step
        peak = max(peak, running)
    return SqlWorkload(
        samples=tuple(samples[:limit]), peak_concurrency=peak, requests=len(starts),
        window_start=min(starts).isoformat() if starts else None, window_end=max(ends).isoformat() if ends else None, busy_ms=busy,
    )


def unreadable(part: str, database: str, exc: Exception) -> ExtractionIssue:
    return ExtractionIssue(IssueCode.MISSING_INFORMATION, f"{part} could not be read: {exc}", f"{database}.{part.replace(' ', '_')}")
