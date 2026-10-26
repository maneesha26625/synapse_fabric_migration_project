"""Migration validation: compare what Synapse has with what Fabric now has, object by object.

Read-only on both sides. Every check states what it saw on each side; a check
that could not be made is reported as REVIEW with the reason, never as a match.

* MATCH     the two sides agree on what was checked.
* REVIEW    something differs in a way the migration explains (a converted type,
            a definition reformatted, rows not loaded yet), or the check could not run.
* MISMATCH  the object is missing from Fabric, or the two sides disagree.

With ``runtime`` options, each object Fabric can run is also checked for whether it *runs*
(``runtime_checks``), and the verdict takes that into account: a failed run or query makes the row a
MISMATCH; an object that matches but has never run in Fabric is REVIEW, because nothing proves it works yet.

With ``reconcile`` options that ask for a profile, a table whose row counts agree is also compared value by
value (``reconcile``): NULLs, exact sums, earliest and latest dates, text lengths and duplicate keys, read with
one aggregate query per side and shown as a "Data Profile" row.

With ``target="lakehouse"`` the SQL side is the Lakehouse's SQL analytics endpoint: tables are compared with the
types it shows for Delta columns, and each stored procedure is looked for as the notebook it became.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.migration import (
    datacopy, datafilter, environments, equivalence, jobs, lakehouse_ddl, notebooks, pipelines, reconcile, runtime_checks, spark_sql,
    warehouse_ddl,
)
from discovery_agent.migration import security as security_sql
from discovery_agent.migration.common import (
    CONNECTION, DATA, DATASET, DEFERRED, ENVIRONMENT, FUNCTION, MISSING, NOTEBOOK, PIPELINE, POOL, PROCEDURE, SCHEDULE, SCHEMA,
    SCRIPT, SECURITY, SHORTCUT, SPARKJOB, TABLE, VIEW, Source,
)
from discovery_agent.migration.fabric_rest import FabricApiError, FabricRestClient
from discovery_agent.sql.estate import SqlSecurityPolicy

MATCH, REVIEW, MISMATCH = "MATCH", "REVIEW", "MISMATCH"
CATEGORY = {
    POOL: "Warehouse", SCHEMA: "Schema", TABLE: "Tables", DATA: "Data Count", VIEW: "Views", PROCEDURE: "Stored Procedures",
    NOTEBOOK: "Notebooks", ENVIRONMENT: "Spark", CONNECTION: "Connections", PIPELINE: "Pipelines", DATASET: "Pipelines",
    SPARKJOB: "Spark Jobs", SCRIPT: "SQL Scripts", SCHEDULE: "Schedules", SHORTCUT: "Shortcuts", DEFERRED: "Manual", MISSING: "Manual",
    FUNCTION: "Functions", SECURITY: "Security",
}
_SIZED = {"varchar", "char", "varbinary", "binary"}

SCHEMAS_SQL = "SELECT name FROM sys.schemas"
OBJECTS_SQL = ("SELECT s.name, o.name, o.type FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
               "WHERE o.type IN ('U', 'V', 'P', 'FN', 'IF', 'TF')")
COLUMNS_SQL = ("SELECT s.name, o.name, c.column_id, c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable "
               "FROM sys.columns c JOIN sys.objects o ON o.object_id = c.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id "
               "JOIN sys.types t ON t.user_type_id = c.user_type_id WHERE o.type IN ('U', 'V')")
MODULES_SQL = ("SELECT s.name, o.name, m.definition FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id "
               "JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE o.type IN ('V', 'P', 'FN', 'IF', 'TF')")


def row(source: Source, status: str, synapse: str, fabric: str, detail: str = "", category: Optional[str] = None) -> Dict[str, str]:
    return {"category": category or CATEGORY.get(source.kind, source.type), "object": source.name,
            "source": synapse, "target": fabric, "status": status, "detail": detail}


def _norm(text: str) -> str:
    """A definition with comments and whitespace removed, so a reformat is not a difference."""
    text = re.sub(r"--[^\n]*", "", text or "")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip().lower().rstrip(";").strip()


def _base(type_name: str) -> str:
    """``varchar(8000)`` -> ``varchar``; other types as they are."""
    return re.sub(r"^(varchar|varbinary|char|binary)\(.*\)$", r"\1", type_name)


def actual_type(name: str, max_length: int, precision: int, scale: int) -> str:
    """A Warehouse column's type in the same spelling ``warehouse_ddl.map_type`` produces."""
    base = (name or "").lower()
    if base in _SIZED:
        return f"{base}({'MAX' if max_length is None or max_length < 0 else max_length})"
    if base in ("decimal", "numeric"):
        return f"decimal({precision},{scale})"
    if base in ("datetime2", "time"):
        return f"{base}({scale})"
    return base


def count_activities(node: Any) -> int:
    """Every activity in a pipeline, including those nested in ForEach, If, Until and Switch.

    An activity is an object with a name, a type and typeProperties. Dataset settings and linked
    services inside one have no ``name`` beside a ``type``, so they are not counted."""
    if isinstance(node, list):
        return sum(count_activities(n) for n in node)
    if not isinstance(node, dict):
        return 0
    own = 1 if isinstance(node.get("name"), str) and isinstance(node.get("type"), str) and "typeProperties" in node else 0
    return own + sum(count_activities(v) for v in node.values() if isinstance(v, (list, dict)))


def _part(definition: Mapping[str, Any], path: str) -> Optional[Dict[str, Any]]:
    for p in (definition.get("definition") or definition).get("parts", []):
        if p.get("path") == path:
            try:
                return json.loads(base64.b64decode(p.get("payload") or ""))
            except ValueError:
                return None
    return None


class Validator:
    def __init__(
        self,
        rest: FabricRestClient,
        workspace_id: str,
        workspace_name: str,
        warehouse_name: str,
        sql_factory: Callable[[str, str], Any],
        source_factory: Optional[Callable[[], Any]] = None,
        artifacts: Optional[Mapping[str, Mapping[str, Any]]] = None,
        runtime: Optional[runtime_checks.RuntimeOptions] = None,
        progress: Callable[[str], None] = lambda text: None,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
        target: str = "warehouse",
        reconcile_options: Optional[reconcile.ReconcileOptions] = None,
        performance: Optional[equivalence.PerformanceOptions] = None,
        workload: Sequence[Any] = (),
        pool_name: str = "",
        onelake: Any = None,
        access: bool = False,
    ) -> None:
        self.rest, self.wid, self.workspace_name = rest, workspace_id, workspace_name
        self.warehouse_name = warehouse_name
        #: Where the SQL pool went: "warehouse", or "lakehouse" (its SQL analytics endpoint is read instead).
        self.lakehouse = target == "lakehouse"
        self.label = "Lakehouse" if self.lakehouse else "Warehouse"
        self._sql_factory, self._source_factory = sql_factory, source_factory
        self.artifacts = artifacts or {}
        self._ids: Dict[str, Dict[str, str]] = {}
        self._sql: Any = None
        self._source: Any = None
        self._wh: Dict[str, Any] = {}
        self.runtime = runtime
        self.reconcile = reconcile_options or reconcile.ReconcileOptions()
        #: Replaying Synapse's most demanding SELECTs on Fabric (``equivalence.replay``), from the discovered workload.
        self.performance = performance or equivalence.PerformanceOptions()
        self.workload = tuple(workload)
        self.pool_name = pool_name
        #: Prove every OneLake shortcut is readable through its connection (``_access``), with this OneLake client.
        self.onelake, self.access = onelake, access
        self.progress = progress
        self._runner = runtime_checks.JobRunner(rest, workspace_id, runtime.timeout_minutes if runtime else runtime_checks.DEFAULT_TIMEOUT_MINUTES,
                                                progress=progress, **{k: f for k, f in (("sleep", sleep), ("clock", clock)) if f is not None})
        self._waiting: Dict[str, Dict[str, str]] = {}  # job key -> the row its run result goes on

    # -- lookups -----------------------------------------------------------------------

    def ids(self, collection: str) -> Dict[str, str]:
        if collection not in self._ids:
            try:
                items = self.rest.list(f"/workspaces/{self.wid}/{collection}")
            except FabricApiError:
                items = []
            self._ids[collection] = {str(i.get("displayName", "")).lower(): str(i.get("id", "")) for i in items}
        return self._ids[collection]

    def find(self, collection: str, name: str) -> Optional[str]:
        return self.ids(collection).get(name.lower())

    def definition(self, collection: str, item_id: str, fmt: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """An item's definition. Notebooks come back in Git format unless ``ipynb`` is asked for."""
        try:
            query = f"?format={fmt}" if fmt else ""
            return self.rest.create(f"/workspaces/{self.wid}/{collection}/{item_id}/getDefinition{query}", {})
        except FabricApiError:
            return None

    # -- the Warehouse -----------------------------------------------------------------------

    def warehouse(self) -> Dict[str, Any]:
        """Schemas, objects, columns and module text from the Warehouse, or {'error': why}."""
        if self._wh:
            return self._wh
        collection = "lakehouses" if self.lakehouse else "warehouses"
        found = self.find(collection, self.warehouse_name)
        if not found:
            self._wh = {"error": f"The {self.label} '{self.warehouse_name}' does not exist in {self.workspace_name}."}
            return self._wh
        try:
            detail = self.rest.get(f"/workspaces/{self.wid}/{collection}/{found}")
            props = detail.get("properties") or {}
            if self.lakehouse:
                props = props.get("sqlEndpointProperties") or {}
            host = str(props.get("connectionString") or "").replace("tcp:", "").split(",")[0].strip()
            self._sql = self._sql_factory(host, self.warehouse_name)
            try:
                self._sql.timeout = runtime_checks.SQL_TIMEOUT_SECONDS  # pyodbc: per-statement timeout for the run checks
            except Exception:  # noqa: BLE001 - a driver without it simply waits
                pass
            cur = self._sql.cursor()

            def fetch(sql: str) -> List[Tuple]:
                cur.execute(sql)
                return [tuple(r) for r in cur.fetchall()]

            self._wh = {
                "schemas": {str(r[0]).lower() for r in fetch(SCHEMAS_SQL)},
                "objects": {(str(r[0]).lower(), str(r[1]).lower()): str(r[2]).strip() for r in fetch(OBJECTS_SQL)},
                "columns": {},
                "modules": {},
            }
            for r in fetch(COLUMNS_SQL):
                self._wh["columns"].setdefault((str(r[0]).lower(), str(r[1]).lower()), []).append(r)
            try:
                for r in fetch(MODULES_SQL):
                    self._wh["modules"][(str(r[0]).lower(), str(r[1]).lower())] = str(r[2] or "")
            except Exception:  # noqa: BLE001 - definitions are an extra; their absence is reported per object
                self._wh["modules"] = None
            try:
                self._wh["masks"] = {(str(r[0]).lower(), str(r[1]).lower(), str(r[2]).lower()): str(r[3] or "")
                                     for r in fetch(security_sql.MASKS_SQL)}
            except Exception:  # noqa: BLE001 - reported on the tables that have masks
                self._wh["masks"] = None
        except Exception as exc:  # noqa: BLE001 - driver, token or network failure
            self._wh = {"error": f"Could not read the {self.label}: {type(exc).__name__}: {str(exc)[:200]}"}
        return self._wh

    def target_rows(self, schema: str, name: str) -> Optional[int]:
        cur = self._sql.cursor()
        cur.execute(datacopy.count_sql(schema, name))
        r = cur.fetchone()
        return int(r[0]) if r and r[0] is not None else 0

    def source_rows(self, schema: str, name: str, predicates: Sequence[str] = ()) -> Optional[int]:
        if self._source is None:
            if self._source_factory is None:
                return None
            self._source = self._source_factory()
        cur = self._source.cursor()
        cur.execute(datafilter.count_sql(schema, name, list(predicates)))
        r = cur.fetchone()
        return int(r[0]) if r and r[0] is not None else 0

    def close(self) -> None:
        for conn in (self._sql, self._source):
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass

    # -- the checks --------------------------------------------------------------------------

    def run(self, sources: Sequence[Source]) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        try:
            for n, s in enumerate(sources, 1):
                self.progress(f"Checking {n} of {len(sources)}: {s.name}")
                handler = getattr(self, f"_check_{s.kind}", None)
                try:
                    rows = handler(s) if handler else [row(s, REVIEW, s.type, "—", "Not checked by this build.")]
                except Exception as exc:  # noqa: BLE001 - one object never stops the report
                    rows = [row(s, REVIEW, s.type, "—", f"The check could not run: {type(exc).__name__}: {str(exc)[:200]}")]
                if self.runtime is not None and rows:
                    try:
                        self._runtime(s, rows[0])
                    except Exception as exc:  # noqa: BLE001
                        apply_runtime(rows[0], runtime_checks.result(runtime_checks.NOT_CHECKED, "Runs in Fabric", f"{type(exc).__name__}: {str(exc)[:200]}"))
                out.extend(rows)
            if self._waiting:
                for key, outcome in self._runner.run().items():
                    apply_runtime(self._waiting[key], outcome)
            if self.access:
                out.extend(self._access())
            if self.performance.enabled:
                out.extend(self._performance())
        finally:
            self.close()
        return out

    # -- does it run? -------------------------------------------------------------------------

    def _runtime(self, s: Source, r: Dict[str, str]) -> None:
        """Attach the run check for one object to its first row (see ``runtime_checks``)."""
        opts = self.runtime
        assert opts is not None
        if self.lakehouse and s.kind == PROCEDURE:
            item_id = self.find("notebooks", spark_sql.notebook_name(s.schema or "dbo", s.object_name or s.name))
            if item_id:
                apply_runtime(r, runtime_checks.last_run(self.rest, self.wid, item_id))
            return
        if s.kind in (TABLE, VIEW, PROCEDURE):
            if not opts.probe and s.kind not in opts.run:
                return
            wh = self.warehouse()
            if "error" in wh or ((s.schema or "dbo").lower(), (s.object_name or s.name).lower()) not in wh["objects"]:
                return  # not there: the structural check already says so
            cursor = self._sql.cursor()
            outcome = (runtime_checks.query_check(cursor, s) if s.kind != PROCEDURE
                       else runtime_checks.procedure_check(cursor, s, execute=PROCEDURE in opts.run))
            apply_runtime(r, outcome)
        elif s.kind in runtime_checks.JOBS:
            collection, job_type = runtime_checks.JOBS[s.kind]
            item_id = self.find(collection, s.name)
            if not item_id:
                return
            if s.kind in opts.run:
                key = f"{s.id}#{len(self._waiting)}"
                self._waiting[key] = r
                self._runner.add(key, item_id, job_type, s.name)
            else:
                apply_runtime(r, runtime_checks.last_run(self.rest, self.wid, item_id))

    def _sql_gate(self, s: Source) -> Optional[List[Dict[str, str]]]:
        wh = self.warehouse()
        if "error" in wh:
            return [row(s, MISMATCH, s.type, f"{self.label} not readable", wh["error"])]
        return None

    def _check_pool(self, s: Source) -> List[Dict[str, str]]:
        wh = self.warehouse()
        if "error" in wh:
            return [row(s, MISMATCH, "SQL pool", "not found", wh["error"])]
        detail = ("The Lakehouse exists and its SQL analytics endpoint is readable." if self.lakehouse
                  else "The Warehouse exists and is readable.")
        return [row(s, MATCH, "SQL pool", f"{self.label} {self.warehouse_name}", detail)]

    def _check_schema(self, s: Source) -> List[Dict[str, str]]:
        if (gate := self._sql_gate(s)):
            return gate
        ok = (s.schema or s.name).lower() in self.warehouse()["schemas"]
        return [row(s, MATCH if ok else MISMATCH, "schema", "schema" if ok else "missing", "" if ok else "Run the Warehouse stage.")]

    def _check_table(self, s: Source) -> List[Dict[str, str]]:
        if (gate := self._sql_gate(s)):
            return gate
        schema, name = (s.schema or "dbo"), (s.object_name or s.name)
        key = (schema.lower(), name.lower())
        wh = self.warehouse()
        if key not in wh["objects"]:
            return [row(s, MISMATCH, f"{len(s.payload.columns)} columns", "missing", f"The table is not in the {self.label}.")]
        shown = {c.name.lower(): c.name for c in s.payload.columns}  # report columns as the source spells them
        if self.lakehouse:
            # Text and binary lengths are the endpoint's to choose; the base type is what is compared.
            expected = {c.name.lower(): _base(lakehouse_ddl.endpoint_type(c)) for c in s.payload.columns}
            actual = {str(r[3]).lower(): _base(actual_type(r[4], r[5], r[6], r[7])) for r in wh["columns"].get(key, [])}
        else:
            expected = {c.name.lower(): warehouse_ddl.map_type(c)[0].lower() for c in s.payload.columns}
            actual = {str(r[3]).lower(): actual_type(r[4], r[5], r[6], r[7]) for r in wh["columns"].get(key, [])}
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        shown.update({k: k for k in extra})
        wrong = sorted(c for c in expected if c in actual and expected[c] != actual[c])
        label_s, label_t = f"{len(expected)} columns", f"{len(actual)} columns"
        if missing or extra or wrong:
            parts = ([f"missing: {', '.join(shown[c] for c in missing[:5])}"] if missing else []) \
                + ([f"unexpected: {', '.join(shown[c] for c in extra[:5])}"] if extra else []) \
                + ([f"type differs: {', '.join(f'{shown[c]} ({expected[c]} expected, {actual[c]} found)' for c in wrong[:3])}"] if wrong else [])
            return [row(s, MISMATCH, label_s, label_t, "; ".join(parts))]
        if s.masks:
            masks = wh.get("masks")
            if masks is None:
                return [row(s, REVIEW, label_s, label_t, "Columns agree; the dynamic data masks could not be read to compare.")]
            unmasked = [m.column for m in s.masks if (schema.lower(), name.lower(), m.column.lower()) not in masks]
            if unmasked:
                return [row(s, MISMATCH, label_s, label_t, f"Dynamic data mask missing in Fabric on: {', '.join(unmasked[:5])}. "
                            "Those columns are visible unmasked to users without UNMASK.")]
        mapper = lakehouse_ddl.map_type if self.lakehouse else warehouse_ddl.map_type
        converted = [c for c in s.payload.columns if mapper(c)[1]]
        if converted:
            return [row(s, REVIEW, label_s, label_t, f"{len(converted)} column type(s) converted by design (e.g. {mapper(converted[0])[1]}).")]
        return [row(s, MATCH, label_s, label_t, "Column names and types agree.")]

    def _check_data(self, s: Source) -> List[Dict[str, str]]:
        if (gate := self._sql_gate(s)):
            return gate
        schema, name = (s.schema or "dbo"), (s.object_name or s.name)
        if (schema.lower(), name.lower()) not in self.warehouse()["objects"]:
            return [row(s, MISMATCH, "rows", "no table", f"The table is not in the {self.label}.", "Data Count")]
        label = s.name.replace(" (data)", "")
        if s.data_filter is not None:
            counted = {**self._check_filtered(s, schema, name), "object": label}
            if counted["status"] != MATCH:
                return [counted]
            scope = datafilter.scope_predicates(datafilter.resolve(s.payload, s.data_filter))
            return [counted] + self._profile(s, label, schema, name, scope)
        src = self.source_rows(s.payload.key.schema, s.payload.key.name)
        tgt = self.target_rows(schema, name)
        if src is None:
            return [{**row(s, REVIEW, "not read", f"{tgt:,} rows", "The Synapse source is not connected, so its row count could not be read.", "Data Count"), "object": label}]
        status = MATCH if src == tgt else REVIEW if tgt == 0 else MISMATCH
        detail = "Counts agree." if src == tgt else "No rows loaded yet: run the Table data stage." if tgt == 0 else f"{abs(src - tgt):,} row(s) {'missing from' if tgt < src else 'extra in'} Fabric."
        counted = {**row(s, status, f"{src:,} rows", f"{tgt:,} rows", detail, "Data Count"), "object": label}
        return [counted] + (self._profile(s, label, schema, name) if status == MATCH else [])

    def _profile(self, s: Source, label: str, schema: str, name: str, scope: Sequence[str] = ()) -> List[Dict[str, str]]:
        """The value profile of a table whose row counts agree (``reconcile``), when the options ask for one."""
        if not self.reconcile.profile or self._source is None:
            return []
        try:
            result = reconcile.run_profile(self._source.cursor(), self._sql.cursor(), s.payload, (schema, name), self.reconcile, scope)
        except Exception as exc:  # noqa: BLE001 - an unreadable profile is reported, never taken as a match
            return [{**row(s, REVIEW, "profile", "not read", f"The value profile could not be read: {type(exc).__name__}: "
                                                              f"{runtime_checks._message(exc)[:200]}", "Data Profile"), "object": label}]
        how = self.reconcile.describe()
        return [{**row(s, result.status, result.summary, result.summary, f"{result.detail} ({how})", "Data Profile"), "object": label}]

    def _watermark(self, schema: str, name: str) -> Optional[str]:
        """A synced table's watermark from the Warehouse control table, or None when it is not registered."""
        try:
            cur = self._sql.cursor()
            cur.execute(warehouse_ddl.EXISTS_QUERY, datafilter.CONTROL)
            found = cur.fetchone()
            if not found or found[0] is None:
                return None
            cur.execute(datafilter.REGISTERED_SQL, datafilter.table_key(schema, name))
            row = cur.fetchone()
            return datafilter.clean_watermark(row[0]) if row and row[0] is not None else None
        except Exception:  # noqa: BLE001 - an unreadable control table reads as not registered
            return None

    def _check_filtered(self, s: Source, schema: str, name: str) -> Dict[str, str]:
        """A filtered table: Synapse is counted with the same filter.

        A synced table is also compared up to its watermark. Synapse keeps changing between syncs, so a
        Warehouse count between "up to the last sync" and "everything in the filter now" is in step, not wrong.
        """
        tgt = self.target_rows(schema, name) or 0
        try:
            resolved = datafilter.resolve(s.payload, s.data_filter)
        except datafilter.FilterError as exc:
            return row(s, MISMATCH, "filter", f"{tgt:,} rows", f"The date filter does not fit the table: {exc}", "Data Count")
        how = s.data_filter.describe()
        scope = datafilter.scope_predicates(resolved)
        src = self.source_rows(s.payload.key.schema, s.payload.key.name, scope)
        if src is None:
            return row(s, REVIEW, "not read", f"{tgt:,} rows", "The Synapse source is not connected, so its row count could not be read.", "Data Count")
        if src == tgt:
            return row(s, MATCH, f"{src:,} rows", f"{tgt:,} rows", f"Counts agree ({how}).", "Data Count")
        watermark = self._watermark(schema, name) if resolved.sync else None
        if watermark is not None:
            through = self.source_rows(s.payload.key.schema, s.payload.key.name, datafilter.first_load_predicates(resolved, watermark)) or 0
            if min(through, src) <= tgt <= max(through, src):
                return row(s, REVIEW, f"{src:,} rows", f"{tgt:,} rows",
                           f"In step up to the last sync ({watermark}); rows added or changed in Synapse since then arrive "
                           "with the next sync run.", "Data Count")
        if tgt == 0:
            return row(s, REVIEW, f"{src:,} rows", "0 rows", f"No rows loaded yet: run the Table data stage ({how}).", "Data Count")
        detail = f"{abs(src - tgt):,} row(s) {'missing from' if tgt < src else 'extra in'} Fabric ({how})."
        if resolved.sync and tgt > src:
            detail += " Rows deleted in Synapse stay in the Warehouse: syncs do not carry hard deletes."
        return row(s, MISMATCH, f"{src:,} rows", f"{tgt:,} rows", detail, "Data Count")

    def _module(self, s: Source, kind: str) -> List[Dict[str, str]]:
        if (gate := self._sql_gate(s)):
            return gate
        key = ((s.schema or "dbo").lower(), (s.object_name or s.name).lower())
        wh = self.warehouse()
        if key not in wh["objects"]:
            return [row(s, MISMATCH, f"1 {kind}", "missing", f"The {kind} is not in the {self.label}.")]
        text = getattr(getattr(s.payload, "definition", None), "text", "") or ""
        if wh["modules"] is None:
            return [row(s, REVIEW, f"1 {kind}", f"1 {kind}", "Exists; the definition could not be read to compare.")]
        same = _norm(text) == _norm(wh["modules"].get(key, ""))
        return [row(s, MATCH if same else REVIEW, "1 definition", "1 definition", "Definitions agree." if same else "Exists, but the definition text differs (Fabric may have reformatted it).")]

    def _source_cursor(self) -> Optional[Any]:
        if self._source is None and self._source_factory is not None:
            self._source = self._source_factory()
        return self._source.cursor() if self._source is not None else None

    def _check_view(self, s: Source) -> List[Dict[str, str]]:
        rows = self._module(s, "view")
        if not self.reconcile.views or rows[0]["status"] == MISMATCH:
            return rows
        schema, name = s.schema or "dbo", s.object_name or s.name
        label = f"{schema}.{name}"
        source = self._source_cursor()
        if source is None:
            return rows + [{**row(s, REVIEW, "not read", "results", "The Synapse source is not connected, so the view's results could not be compared.",
                                  "View Results"), "object": label}]
        columns = equivalence.columns_from_catalog(self.warehouse()["columns"].get((schema.lower(), name.lower()), []))
        try:
            result = equivalence.view_profile(source, self._sql.cursor(), schema, name, columns, self.reconcile)
        except Exception as exc:  # noqa: BLE001 - an unreadable comparison is reported, never taken as a match
            return rows + [{**row(s, REVIEW, "results", "not read", f"The view's results could not be compared: {type(exc).__name__}: "
                                                                   f"{runtime_checks._message(exc)[:200]}", "View Results"), "object": label}]
        return rows + [{**row(s, result.status, result.summary, result.summary, f"{result.detail} ({self.reconcile.describe()})",
                              "View Results"), "object": label}]

    def _access(self) -> List[Dict[str, str]]:
        """Every shortcut the run made (external tables, routed storage), read through its connection.

        A shortcut that exists but cannot be read fails every notebook, view and pipeline that goes through it,
        with an authorization error at run time. Reading one entry here finds that before cutover."""
        base = {"category": "Access"}
        lakes = [n for n in {self.warehouse_name if self.lakehouse else f"{self.warehouse_name}_lakehouse"} if self.find("lakehouses", n)]
        if not lakes:
            return []
        if self.onelake is None:
            return [{**base, "object": "OneLake shortcuts", "source": "storage", "target": "not checked", "status": REVIEW,
                     "detail": "No OneLake sign-in for this check: shortcuts were not read. Sign in to Fabric with the Azure CLI."}]
        out: List[Dict[str, str]] = []
        for name in lakes:
            lake = self.find("lakehouses", name)
            try:
                shortcuts = self.rest.list(f"/workspaces/{self.wid}/items/{lake}/shortcuts")
            except FabricApiError as exc:
                out.append({**base, "object": name, "source": "shortcuts", "target": "not listed", "status": REVIEW,
                            "detail": f"Shortcuts could not be listed: {exc.message[:160]}"})
                continue
            for sc in shortcuts:
                label = f"{name}/{str(sc.get('path') or '').strip('/')}/{sc.get('name')}".replace("//", "/")
                target = sc.get("target") or {}
                where = next((str(v.get("location") or "") + str(v.get("subpath") or "") for v in target.values() if isinstance(v, dict)), "")
                try:
                    self.onelake.probe(self.wid, lake, f"{str(sc.get('path') or 'Files').strip('/')}/{sc.get('name')}")
                except Exception as exc:  # noqa: BLE001 - OneLakeError or a network failure: the reason is the result
                    status = getattr(exc, "status", 0)
                    why = ("The shortcut's connection is not allowed to read it: give that identity Storage Blob Data Reader "
                           "(or ACLs) on the location, then validate again." if status in (401, 403)
                           else "The location it points at does not exist (or was renamed)." if status == 404
                           else f"It could not be read: {str(exc)[:200]}")
                    out.append({**base, "object": label, "source": where or "storage", "target": "not readable", "status": MISMATCH,
                                "detail": why + " Everything reading through it fails the same way."})
                    continue
                out.append({**base, "object": label, "source": where or "storage", "target": "readable", "status": MATCH,
                            "detail": "Read through the shortcut's connection: the identity it uses may read the location."})
        return out

    def _performance(self) -> List[Dict[str, str]]:
        """Synapse's most demanding recent SELECTs, run on Fabric and compared with their Synapse times."""
        samples = sorted((q for q in self.workload if q.replayable), key=lambda q: -(q.runs * q.avg_ms))[: self.performance.limit]
        skipped = sum(1 for q in self.workload if not q.replayable)
        out: List[Dict[str, str]] = []
        base = {"category": "Performance"}
        if not self.workload:
            return [{**base, "object": "Query history", "source": "not read", "target": "—", "status": REVIEW,
                     "detail": "Discovery has no Synapse request history (it needs VIEW DATABASE STATE), so there is nothing to benchmark."}]
        wh = self.warehouse()
        if "error" in wh:
            return [{**base, "object": "Query history", "source": f"{len(samples)} queries", "target": "not run", "status": REVIEW,
                     "detail": wh["error"]}]
        for n, q in enumerate(samples, 1):
            self.progress(f"Benchmarking query {n} of {len(samples)} in Fabric")
            out.append({**base, **equivalence.replay(self._sql.cursor(), q, self.performance, self.pool_name, self.warehouse_name)})
        if skipped:
            out.append({**base, "object": "Not replayed", "source": f"{skipped} queries", "target": "—", "status": REVIEW,
                        "detail": f"{skipped} history quer{'y' if skipped == 1 else 'ies'} write data or use temporary tables or variables: "
                                  "benchmark them in their pipelines."})
        return out

    def _check_procedure(self, s: Source) -> List[Dict[str, str]]:
        if self.lakehouse:
            notebook = spark_sql.notebook_name(s.schema or "dbo", s.object_name or s.name)
            ok = bool(self.find("notebooks", notebook))
            return [row(s, REVIEW if ok else MISMATCH, "1 procedure", f"notebook {notebook}" if ok else "missing",
                        "Converted to a Spark notebook: compare what it does with the procedure before relying on it."
                        if ok else f"The notebook '{notebook}' the procedure becomes is not in the Fabric workspace.")]
        rows = self._module(s, "procedure")
        if not self.reconcile.procedures or rows[0]["status"] == MISMATCH:
            return rows
        source = self._source_cursor()
        label = f"{s.schema or 'dbo'}.{s.object_name or s.name}"
        if source is None:
            return rows + [{**row(s, REVIEW, "not read", "result set", "The Synapse source is not connected, so the result sets could not be compared.",
                                  "Procedure Results"), "object": label}]
        try:
            status, a, b, detail = equivalence.procedure_shape(source, self._sql.cursor(), runtime_checks._exec_text(s))
        except Exception as exc:  # noqa: BLE001
            return rows + [{**row(s, REVIEW, "result set", "not read", f"The result sets could not be described: {runtime_checks._message(exc)[:200]}",
                                  "Procedure Results"), "object": label}]
        return rows + [{**row(s, status, a, b, detail, "Procedure Results"), "object": label}]

    def _check_function(self, s: Source) -> List[Dict[str, str]]:
        return self._module(s, "function")

    def _security_state(self) -> Dict[str, Any]:
        """Fabric's roles, members and explicit permissions, read once, or {'error': why}."""
        wh = self.warehouse()
        if "error" in wh:
            return wh
        if "security" not in wh:
            try:
                cur = self._sql.cursor()
                cur.execute(security_sql.PRINCIPALS_SQL)
                principals = {str(r[0]).lower() for r in cur.fetchall()}
                cur.execute(security_sql.MEMBERS_SQL)
                members = {(str(r[0]).lower(), str(r[1]).lower()) for r in cur.fetchall()}
                cur.execute(security_sql.PERMISSIONS_SQL)
                permissions = {security_sql.permission_key(r[0], int(r[1]), r[2], str(r[3]), r[4], r[5], r[6]) for r in cur.fetchall()}
                wh["security"] = {"principals": principals, "members": members, "permissions": permissions}
            except Exception as exc:  # noqa: BLE001 - reported on each security row
                wh["security"] = {"error": f"Roles and permissions could not be read: {type(exc).__name__}: {str(exc)[:200]}"}
        return wh["security"]

    def _check_policy(self, s: Source) -> List[Dict[str, str]]:
        """A row-level security policy: present, with its filter predicates and its state."""
        policy = s.payload
        wanted = len(policy.filters)
        label = f"{wanted} filter predicate(s), {'on' if policy.enabled else 'off'}"
        try:
            cur = self._sql.cursor()
            cur.execute(security_sql.POLICIES_SQL)
            found = {(str(r[0]).lower(), str(r[1]).lower()): (bool(r[2]), int(r[3] or 0)) for r in cur.fetchall()}
        except Exception as exc:  # noqa: BLE001
            return [row(s, REVIEW, label, "not read", f"Security policies could not be read: {type(exc).__name__}: {str(exc)[:200]}")]
        got = found.get((policy.schema.lower(), policy.name.lower()))
        if got is None:
            return [row(s, MISMATCH, label, "missing", "The security policy is not in Fabric: every user sees every row.")]
        enabled, count = got
        shown = f"{count} filter predicate(s), {'on' if enabled else 'off'}"
        if count != wanted or enabled != policy.enabled:
            return [row(s, MISMATCH, label, shown, "The policy differs from Synapse: rows may be visible that were filtered before.")]
        note = f" {len(policy.blocks)} BLOCK predicate(s) were left for a person." if policy.blocks else ""
        return [row(s, MATCH if not policy.blocks else REVIEW, label, shown, "Filter predicates and state agree." + note)]

    def _check_security(self, s: Source) -> List[Dict[str, str]]:
        if (gate := self._sql_gate(s)):
            return gate
        if isinstance(s.payload, SqlSecurityPolicy):
            return self._check_policy(s)
        plan = security_sql.plan_principal(s.payload, s.name)
        state = self._security_state()
        wanted_p = security_sql.expected_keys(plan, s.payload)
        label_s = f"{len(plan.members)} members, {len(wanted_p)} permissions" if plan.principal.is_role else f"{len(wanted_p)} permissions"
        if "error" in state:
            return [row(s, REVIEW, label_s, "not read", state["error"])]
        problems = []
        if plan.create_role and plan.principal.name.lower() not in state["principals"]:
            return [row(s, MISMATCH, label_s, "missing", "The role is not in the Warehouse.")]
        if not plan.principal.is_role and wanted_p and plan.principal.name.lower() not in state["principals"]:
            return [row(s, MISMATCH, label_s, "missing", "The user is not in the Warehouse: none of its permissions were applied.")]
        missing_m = [m for m, _ in plan.members if (plan.principal.name.lower(), m.lower()) not in state["members"]]
        missing_p = [k for k in wanted_p if k not in state["permissions"]]
        if missing_m:
            problems.append(f"member(s) missing: {', '.join(missing_m[:5])}")
        if missing_p:
            shown = [f"{k[3] == 'd' and 'DENY' or 'GRANT'} {k[2].upper()} on {'.'.join(x for x in k[4:6] if x) or 'the database'}" for k in missing_p[:3]]
            problems.append(f"{len(missing_p)} permission(s) missing, e.g. {'; '.join(shown)}")
        got = f"{len(plan.members) - len(missing_m)} members, {len(wanted_p) - len(missing_p)} permissions" if plan.principal.is_role \
            else f"{len(wanted_p) - len(missing_p)} permissions"
        if problems:
            return [row(s, MISMATCH, label_s, got, "; ".join(problems) + ".")]
        return [row(s, MATCH, label_s, got, "Role, members and permissions agree." if plan.principal.is_role else "Permissions agree.")]

    def _check_notebook(self, s: Source) -> List[Dict[str, str]]:
        found = self.find("notebooks", s.name)
        cells = len(((s.payload or {}).get("properties") or {}).get("cells") or [])
        if not found:
            return [row(s, MISMATCH, f"{cells} cells", "missing", "The notebook is not in the Fabric workspace.")]
        doc = self.definition("notebooks", found, "ipynb")
        body = _part(doc, "notebook-content.ipynb") if doc else None
        if body is None:
            return [row(s, REVIEW, f"{cells} cells", "exists", "Exists; its content could not be read to compare.")]
        got = len(body.get("cells") or [])
        return [row(s, MATCH if got == cells else MISMATCH, f"{cells} cells", f"{got} cells", "Cell counts agree." if got == cells else "The cell counts differ.")]

    def _check_script(self, s: Source) -> List[Dict[str, str]]:
        ok = bool(self.find("notebooks", s.name))
        made = "Created as a notebook." if self.lakehouse else "Created as a T-SQL notebook."
        return [row(s, MATCH if ok else MISMATCH, "1 script", "1 notebook" if ok else "missing",
                    made if ok else "The notebook is not in the Fabric workspace.")]

    def _check_environment(self, s: Source) -> List[Dict[str, str]]:
        meta = s.payload if isinstance(s.payload, dict) else {}
        wanted = environments.plan(meta, s.name)
        env = self.find("environments", s.name)
        try:
            pools = {str(p.get("name", "")).lower(): p for p in self.rest.list(f"/workspaces/{self.wid}/spark/pools")}
        except FabricApiError:
            pools = {}
        pool = pools.get(s.name.lower())
        src = f"{wanted.pool['nodeSize']}, {wanted.pool['autoScale']['minNodeCount']}–{wanted.pool['autoScale']['maxNodeCount']} nodes"
        if not env and not pool:
            return [row(s, MISMATCH, src, "missing", "Neither the Fabric Spark pool nor the Environment exists.")]
        if not env or not pool:
            return [row(s, MISMATCH, src, "partly there", f"The {'Environment' if not env else 'Spark pool'} is missing.")]
        scale = pool.get("autoScale") or {}
        got = f"{pool.get('nodeSize')}, {scale.get('minNodeCount')}–{scale.get('maxNodeCount')} nodes"
        same = pool.get("nodeSize") == wanted.pool["nodeSize"] and scale.get("minNodeCount") == wanted.pool["autoScale"]["minNodeCount"] \
            and scale.get("maxNodeCount") == wanted.pool["autoScale"]["maxNodeCount"]
        return [row(s, MATCH if same else REVIEW, src, got, "Pool and Environment exist with the same size." if same else "Exists with different settings.")]

    def _check_connection(self, s: Source) -> List[Dict[str, str]]:
        try:
            names = {str(c.get("displayName", "")).lower() for c in self.rest.list("/connections")}
        except FabricApiError as exc:
            return [row(s, REVIEW, "1 linked service", "—", f"Fabric connections could not be listed: {exc.message[:120]}")]
        ok = s.name.lower() in names
        return [row(s, MATCH if ok else MISMATCH, "1 linked service", "1 connection" if ok else "missing", "" if ok else "Run the Connections stage with credentials.")]

    def _check_pipeline(self, s: Source) -> List[Dict[str, str]]:
        resource = (s.payload or {}).get("resource") or {}
        want = count_activities((resource.get("properties") or {}).get("activities") or [])
        found = self.find("dataPipelines", s.name)
        if not found:
            return [row(s, MISMATCH, f"{want} activities", "missing", "The pipeline is not in the Fabric workspace.")]
        doc = self.definition("dataPipelines", found)
        body = _part(doc, "pipeline-content.json") if doc else None
        if body is None:
            return [row(s, REVIEW, f"{want} activities", "exists", "Exists; its content could not be read to compare.")]
        got = count_activities((body.get("properties") or {}).get("activities") or [])
        return [row(s, MATCH if got == want else MISMATCH, f"{want} activities", f"{got} activities", "Activity counts agree." if got == want else "The activity counts differ.")]

    def _check_dataset(self, s: Source) -> List[Dict[str, str]]:
        users = [n for n, p in (self.artifacts.get("pipelines") or {}).items() if s.name in pipelines.referenced_names(p)["datasets"]]
        if not users:
            return [row(s, MATCH, "1 dataset", "unused", "No pipeline uses it, so there is nothing to embed.", "Pipelines")]
        missing = [u for u in users if not self.find("dataPipelines", u)]
        return [row(s, MISMATCH if missing else MATCH, "1 dataset", f"in {len(users) - len(missing)} of {len(users)} pipelines",
                    f"Not embedded: {', '.join(missing)} not in Fabric." if missing else "Embedded in every pipeline that uses it.", "Pipelines")]

    def _check_sparkjob(self, s: Source) -> List[Dict[str, str]]:
        ok = bool(self.find("sparkJobDefinitions", s.name))
        return [row(s, MATCH if ok else MISMATCH, "1 job definition", "1 job definition" if ok else "missing", "" if ok else "The job definition is not in the Fabric workspace.")]

    def _check_schedule(self, s: Source) -> List[Dict[str, str]]:
        try:
            bodies, _ = jobs.schedule_bodies(s.payload or {})
        except jobs.NotConvertible as exc:
            return [row(s, REVIEW, "1 trigger", "by hand", f"Not migrated automatically: {exc}")]
        results = []
        for pipeline_name, _body in bodies:
            pid = self.find("dataPipelines", pipeline_name)
            if not pid:
                results.append(f"{pipeline_name}: pipeline missing")
                continue
            try:
                has = bool(self.rest.list(f"/workspaces/{self.wid}/items/{pid}/jobs/Pipeline/schedules"))
            except FabricApiError:
                results.append(f"{pipeline_name}: schedules could not be read")
                continue
            if not has:
                results.append(f"{pipeline_name}: no schedule")
        ok = not results
        return [row(s, MATCH if ok else MISMATCH, f"{len(bodies)} schedule(s)", f"{len(bodies)} schedule(s)" if ok else "missing", "Schedules exist (created switched off)." if ok else "; ".join(results))]

    def _check_shortcut(self, s: Source) -> List[Dict[str, str]]:
        lake = self.find("lakehouses", self.warehouse_name if self.lakehouse else f"{self.warehouse_name}_lakehouse")
        if not lake:
            return [row(s, MISMATCH, "1 external table", "no Lakehouse", "The Lakehouse for shortcuts does not exist.")]
        name = (s.object_name or s.name).lower()
        try:
            names = {str(x.get("name", "")).lower() for x in self.rest.list(f"/workspaces/{self.wid}/items/{lake}/shortcuts")}
        except FabricApiError as exc:
            return [row(s, REVIEW, "1 external table", "—", f"Shortcuts could not be listed: {exc.message[:120]}")]
        ok = name in names
        return [row(s, MATCH if ok else MISMATCH, "1 external table", "1 shortcut" if ok else "missing", "" if ok else "The shortcut is not in the Lakehouse.")]

    def _check_deferred(self, s: Source) -> List[Dict[str, str]]:
        return [row(s, REVIEW, s.type, "set up by hand", s.reason or "This kind of object is not migrated by the tool; check it in Fabric yourself.")]

    _check_missing = _check_deferred


def apply_runtime(r: Dict[str, Any], outcome: Mapping[str, Any]) -> None:
    """Put a run check on a row, and let it decide the verdict: a failure is a MISMATCH, never-run is not a MATCH."""
    r["runtime"] = dict(outcome)
    if outcome["status"] == runtime_checks.FAIL:
        r["status"] = MISMATCH
    elif outcome["status"] == runtime_checks.NOT_RUN and r["status"] == MATCH:
        r["status"] = REVIEW


def summarize(rows: Iterable[Mapping[str, str]]) -> Dict[str, int]:
    out = {MATCH: 0, REVIEW: 0, MISMATCH: 0}
    for r in rows:
        out[r["status"]] += 1
    return out
