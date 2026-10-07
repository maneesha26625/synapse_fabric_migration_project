"""Migration validation: compare what Synapse has with what Fabric now has, object by object.

Read-only on both sides. Every check states what it saw on each side; a check
that could not be made is reported as REVIEW with the reason, never as a match.

* MATCH     the two sides agree on what was checked.
* REVIEW    something differs in a way the migration explains (a converted type,
            a definition reformatted, rows not loaded yet), or the check could not run.
* MISMATCH  the object is missing from Fabric, or the two sides disagree.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.migration import datacopy, environments, jobs, notebooks, pipelines, warehouse_ddl
from discovery_agent.migration.common import (
    CONNECTION, DATA, DATASET, DEFERRED, ENVIRONMENT, MISSING, NOTEBOOK, PIPELINE, POOL, PROCEDURE, SCHEDULE, SCHEMA, SCRIPT,
    SHORTCUT, SPARKJOB, TABLE, VIEW, Source,
)
from discovery_agent.migration.fabric_rest import FabricApiError, FabricRestClient

MATCH, REVIEW, MISMATCH = "MATCH", "REVIEW", "MISMATCH"
CATEGORY = {
    POOL: "Warehouse", SCHEMA: "Schema", TABLE: "Tables", DATA: "Data Count", VIEW: "Views", PROCEDURE: "Stored Procedures",
    NOTEBOOK: "Notebooks", ENVIRONMENT: "Spark", CONNECTION: "Connections", PIPELINE: "Pipelines", DATASET: "Pipelines",
    SPARKJOB: "Spark Jobs", SCRIPT: "SQL Scripts", SCHEDULE: "Schedules", SHORTCUT: "Shortcuts", DEFERRED: "Manual", MISSING: "Manual",
}
_SIZED = {"varchar", "char", "varbinary", "binary"}

SCHEMAS_SQL = "SELECT name FROM sys.schemas"
OBJECTS_SQL = ("SELECT s.name, o.name, o.type FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
               "WHERE o.type IN ('U', 'V', 'P')")
COLUMNS_SQL = ("SELECT s.name, o.name, c.column_id, c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable "
               "FROM sys.columns c JOIN sys.objects o ON o.object_id = c.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id "
               "JOIN sys.types t ON t.user_type_id = c.user_type_id WHERE o.type = 'U'")
MODULES_SQL = ("SELECT s.name, o.name, m.definition FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id "
               "JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE o.type IN ('V', 'P')")


def row(source: Source, status: str, synapse: str, fabric: str, detail: str = "", category: Optional[str] = None) -> Dict[str, str]:
    return {"category": category or CATEGORY.get(source.kind, source.type), "object": source.name,
            "source": synapse, "target": fabric, "status": status, "detail": detail}


def _norm(text: str) -> str:
    """A definition with comments and whitespace removed, so a reformat is not a difference."""
    text = re.sub(r"--[^\n]*", "", text or "")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip().lower().rstrip(";").strip()


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
        pool_name: str = "",
        pool_server: str = "",
    ) -> None:
        self.rest, self.wid, self.workspace_name = rest, workspace_id, workspace_name
        self.warehouse_name = warehouse_name
        self.pool_name, self.pool_server = pool_name, pool_server
        self._sql_factory, self._source_factory = sql_factory, source_factory
        self.artifacts = artifacts or {}
        self._ids: Dict[str, Dict[str, str]] = {}
        self._sql: Any = None
        self._source: Any = None
        self._wh: Dict[str, Any] = {}

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
        found = self.find("warehouses", self.warehouse_name)
        if not found:
            self._wh = {"error": f"The Warehouse '{self.warehouse_name}' does not exist in {self.workspace_name}."}
            return self._wh
        try:
            detail = self.rest.get(f"/workspaces/{self.wid}/warehouses/{found}")
            host = str((detail.get("properties") or {}).get("connectionString") or "").replace("tcp:", "").split(",")[0].strip()
            self._sql = self._sql_factory(host, self.warehouse_name)
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
        except Exception as exc:  # noqa: BLE001 - driver, token or network failure
            self._wh = {"error": f"Could not read the Warehouse: {type(exc).__name__}: {str(exc)[:200]}"}
        return self._wh

    def target_rows(self, schema: str, name: str) -> Optional[int]:
        cur = self._sql.cursor()
        cur.execute(datacopy.count_sql(schema, name))
        r = cur.fetchone()
        return int(r[0]) if r and r[0] is not None else 0

    def source_rows(self, schema: str, name: str) -> Optional[int]:
        if self._source is None:
            if self._source_factory is None:
                return None
            self._source = self._source_factory()
        cur = self._source.cursor()
        cur.execute(datacopy.count_sql(schema, name))
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
            for s in sources:
                handler = getattr(self, f"_check_{s.kind}", None)
                try:
                    out.extend(handler(s) if handler else [row(s, REVIEW, s.type, "—", "Not checked by this build.")])
                except Exception as exc:  # noqa: BLE001 - one object never stops the report
                    out.append(row(s, REVIEW, s.type, "—", f"The check could not run: {type(exc).__name__}: {str(exc)[:200]}"))
        finally:
            self.close()
        return out

    def _sql_gate(self, s: Source) -> Optional[List[Dict[str, str]]]:
        wh = self.warehouse()
        if "error" in wh:
            return [row(s, MISMATCH, s.type, "Warehouse not readable", wh["error"])]
        return None

    def _check_pool(self, s: Source) -> List[Dict[str, str]]:
        wh = self.warehouse()
        if "error" in wh:
            return [row(s, MISMATCH, "SQL pool", "not found", wh["error"])]
        return [row(s, MATCH, "SQL pool", f"Warehouse {self.warehouse_name}", "The Warehouse exists and is readable.")]

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
            return [row(s, MISMATCH, f"{len(s.payload.columns)} columns", "missing", "The table is not in the Warehouse.")]
        shown = {c.name.lower(): c.name for c in s.payload.columns}  # report columns as the source spells them
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
        converted = [c for c in s.payload.columns if warehouse_ddl.map_type(c)[1]]
        if converted:
            return [row(s, REVIEW, label_s, label_t, f"{len(converted)} column type(s) converted by design (e.g. {warehouse_ddl.map_type(converted[0])[1]}).")]
        return [row(s, MATCH, label_s, label_t, "Column names and types agree.")]

    def _check_data(self, s: Source) -> List[Dict[str, str]]:
        if (gate := self._sql_gate(s)):
            return gate
        schema, name = (s.schema or "dbo"), (s.object_name or s.name)
        if (schema.lower(), name.lower()) not in self.warehouse()["objects"]:
            return [row(s, MISMATCH, "rows", "no table", "The table is not in the Warehouse.", "Data Count")]
        label = s.name.replace(" (data)", "")
        src = self.source_rows(s.payload.key.schema, s.payload.key.name)
        tgt = self.target_rows(schema, name)
        if src is None:
            return [{**row(s, REVIEW, "not read", f"{tgt:,} rows", "The Synapse source is not connected, so its row count could not be read.", "Data Count"), "object": label}]
        status = MATCH if src == tgt else REVIEW if tgt == 0 else MISMATCH
        detail = "Counts agree." if src == tgt else "No rows loaded yet: run the Table data stage." if tgt == 0 else f"{abs(src - tgt):,} row(s) {'missing from' if tgt < src else 'extra in'} Fabric."
        return [{**row(s, status, f"{src:,} rows", f"{tgt:,} rows", detail, "Data Count"), "object": label}]

    def _module(self, s: Source, kind: str) -> List[Dict[str, str]]:
        if (gate := self._sql_gate(s)):
            return gate
        key = ((s.schema or "dbo").lower(), (s.object_name or s.name).lower())
        wh = self.warehouse()
        if key not in wh["objects"]:
            return [row(s, MISMATCH, f"1 {kind}", "missing", f"The {kind} is not in the Warehouse.")]
        text = getattr(getattr(s.payload, "definition", None), "text", "") or ""
        if wh["modules"] is None:
            return [row(s, REVIEW, f"1 {kind}", f"1 {kind}", "Exists; the definition could not be read to compare.")]
        same = _norm(text) == _norm(wh["modules"].get(key, ""))
        return [row(s, MATCH if same else REVIEW, "1 definition", "1 definition", "Definitions agree." if same else "Exists, but the definition text differs (Fabric may have reformatted it).")]

    def _check_view(self, s: Source) -> List[Dict[str, str]]:
        return self._module(s, "view")

    def _check_procedure(self, s: Source) -> List[Dict[str, str]]:
        return self._module(s, "procedure")

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
        return [row(s, MATCH if self.find("notebooks", s.name) else MISMATCH, "1 script", "1 notebook" if self.find("notebooks", s.name) else "missing",
                    "Created as a T-SQL notebook." if self.find("notebooks", s.name) else "The notebook is not in the Fabric workspace.")]

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
        if pipelines.is_pool_service(s.payload if isinstance(s.payload, dict) else {}, self.pool_name, pool_server=self.pool_server, any_database=True):
            return [row(s, MATCH, "1 linked service", f"Warehouse {self.warehouse_name}",
                        "Replaced by the migrated Warehouse: the pipelines that used it point at the Warehouse, so no connection is needed.")]
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
        lake = self.find("lakehouses", f"{self.warehouse_name}_lakehouse")
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


def summarize(rows: Iterable[Mapping[str, str]]) -> Dict[str, int]:
    out = {MATCH: 0, REVIEW: 0, MISMATCH: 0}
    for r in rows:
        out[r["status"]] += 1
    return out
