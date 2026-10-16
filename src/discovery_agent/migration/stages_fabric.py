"""Stage handlers that create Fabric items: connections, pipelines, Spark jobs,
SQL-script notebooks, schedules and shortcuts. Mixed into ``Migrator``.

Each returns ``(status, step, target, notes)`` or raises ``MigrationError`` /
``FabricApiError``, like every handler in the runner.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Tuple

from discovery_agent.migration import fabric_connections, jobs, onelake, pipelines, storage_remap
from discovery_agent.migration.common import (
    COMPLETED, DEFERRED_STATUS, SKIPPED, MigrationError, Source, lakehouse_of, override_note,
)
from discovery_agent.migration.fabric_rest import FabricApiError

Result = tuple  # (status, step, target, notes)


class FabricStageMixin:
    # Provided by Migrator ------------------------------------------------
    run: Any
    _credentials: Dict[str, Dict[str, str]]

    # -- lookups shared by the handlers -----------------------------------------

    def _ids(self, collection: str) -> Dict[str, str]:
        """displayName -> id for a workspace collection (notebooks, dataPipelines, ...), cached for the pass."""
        cache = self.__dict__.setdefault("_id_cache", {})
        if collection not in cache:
            items = self._client().list(f"/workspaces/{self.run.workspace_id}/{collection}")  # type: ignore[attr-defined]
            cache[collection] = {str(i.get("displayName", "")): str(i.get("id", "")) for i in items}
        return cache[collection]

    # -- the Lakehouse, and storage routed through its shortcuts ------------------------------

    def _lakehouse(self) -> Tuple[str, str]:
        """(name, id) of the run's Lakehouse, created on first use. It holds the OneLake shortcuts."""
        wid, client = self.run.workspace_id, self._client()  # type: ignore[attr-defined]
        name = lakehouse_of(self.run.warehouse, self.run.settings)
        lakehouse = self._find(self._ids("lakehouses"), name)
        if not lakehouse:
            created = client.create(f"/workspaces/{wid}/lakehouses",
                                    {"displayName": name, "description": "OneLake shortcuts for migrated storage paths and external tables"})
            self._forget("lakehouses")
            lakehouse = str(created.get("id") or "") or self._find(self._ids("lakehouses"), name) or ""
        if not lakehouse:
            raise MigrationError("The Lakehouse was created but Fabric did not return its id. Retry in a minute.")
        return name, lakehouse

    def _routes(self) -> storage_remap.Routes:
        """The storage locations this run routes through OneLake shortcuts: each one's shortcut, created on first use.

        From the run option ``storageRoutes`` ([{account, container, connectionId}]). Empty when there are none."""
        cache = self.__dict__.get("_routes_cache")
        if cache is not None:
            return cache
        wanted = []
        for r in (getattr(self.run, "settings", None) or {}).get("storageRoutes") or []:
            # A Fabric connection chosen in Plan, or the one the Connections stage made from a Synapse linked service.
            cid = r.get("connectionId") or (self._connections().get(str(r.get("linkedService") or "")) or {}).get("id")
            if cid:
                wanted.append({**r, "connectionId": cid})
            else:
                self.run.log("NOTE", "storage", f"No Fabric connection for {r.get('container')} on {r.get('account')}: "  # type: ignore[attr-defined]
                             "its paths are left as they are. Run the Connections stage for its linked service first.")
        if not wanted:
            self.__dict__["_routes_cache"] = storage_remap.Routes([])
            return self.__dict__["_routes_cache"]
        wid, client = self.run.workspace_id, self._client()  # type: ignore[attr-defined]
        lakehouse_name, lakehouse = self._lakehouse()
        routes = []
        for r in wanted:
            account, container = str(r["account"]).lower(), str(r["container"]).lower()
            body = {"path": "Files", "name": container,
                    "target": {"adlsGen2": {"location": f"https://{account}.dfs.core.windows.net", "subpath": f"/{container}",
                                            "connectionId": str(r["connectionId"])}}}
            try:
                client.create(f"/workspaces/{wid}/items/{lakehouse}/shortcuts", body)
                self.run.log("CREATE", "storage", f"OneLake shortcut {lakehouse_name}/Files/{container} -> {container} on {account}")  # type: ignore[attr-defined]
            except FabricApiError as exc:
                if not (exc.status == 409 or "AlreadyExists" in exc.code or "AlreadyInUse" in exc.code):
                    raise MigrationError(f"The OneLake shortcut for {container} on {account} could not be created: {exc.message}") from exc
            routes.append(storage_remap.Route(account, container, wid, lakehouse, container))
        self.__dict__["_routes_cache"] = storage_remap.Routes(routes)
        return self.__dict__["_routes_cache"]

    #: Main files whose code is rewritten when copied: text the Spark job runs directly.
    _SCRIPT_SUFFIXES = (".py", ".r", ".scala", ".sql")

    def _remap_job(self, source: Source, content: Dict[str, Any]) -> List[str]:
        """Route a Spark job's main file and libraries through the shortcuts.

        The main file's own code builds storage paths too, so it is copied into the Lakehouse with those paths
        rewritten, and the job runs the copy. The original stays where it is, unchanged, for Synapse."""
        notes: List[str] = []
        exe = str(content.get("executableFile") or "")
        routed, exe_notes = self._remap(exe)
        libs = [self._remap(str(u))[0] for u in content.get("additionalLibraryUris") or []]
        if libs != list(content.get("additionalLibraryUris") or []):
            content["additionalLibraryUris"] = libs
            notes.append("Library paths go through the OneLake shortcut.")
        if routed == exe:
            return notes + exe_notes
        content["executableFile"] = routed
        where = onelake.lakehouse_path(routed)
        files = getattr(self, "_onelake", None)
        if not routed.lower().endswith(self._SCRIPT_SUFFIXES) or where is None or files is None:
            return notes + ["Main file read through the OneLake shortcut; the code inside it was not rewritten, so check the paths it uses."]
        ws, lakehouse, path = where
        try:
            code = files.read(ws, lakehouse, path).decode("utf-8")
        except (onelake.OneLakeError, UnicodeDecodeError) as exc:
            raise MigrationError(f"The job's main file could not be read through the OneLake shortcut ({path}): {exc}") from exc
        rewritten, code_notes = self._remap(code)
        if rewritten == code:
            return notes + ["Main file read through the OneLake shortcut; its code names no storage paths to rewrite."]
        copy_path = f"Files/_migrated_jobs/{source.name}/{path.rsplit('/', 1)[-1]}"
        try:
            files.write(ws, lakehouse, copy_path, rewritten.encode("utf-8"))
        except onelake.OneLakeError as exc:
            raise MigrationError(f"The rewritten main file could not be saved in the Lakehouse ({copy_path}): {exc.message}") from exc
        content["executableFile"] = f"abfss://{ws}@{storage_remap.ONELAKE_HOST}/{lakehouse}/{copy_path}"
        return notes + [f"Main file copied to the Lakehouse ({copy_path}) with its storage paths rewritten; the original is unchanged.",
                        *code_notes]

    #: Pipeline fields that hold SQL text: a Script activity's scripts, a Lookup or Copy source's query.
    _SQL_FIELDS = ("text", "sqlReaderQuery", "query", "preCopyScript")

    def _remap_sql_text(self, node: Any) -> List[str]:
        """Route storage paths in a pipeline's SQL text (OPENROWSET, COPY) through the shortcuts. Edits ``node`` in place."""
        notes: List[str] = []

        def walk(n: Any) -> None:
            if isinstance(n, dict):
                for k, v in n.items():
                    if k in self._SQL_FIELDS and isinstance(v, str) and storage_remap.uses_storage(v):
                        n[k], found = self._remap(v)
                        notes.extend(x for x in found if x not in notes)
                    elif k in self._SQL_FIELDS and isinstance(v, dict) and isinstance(v.get("value"), str) \
                            and storage_remap.uses_storage(v["value"]):
                        v["value"], found = self._remap(v["value"])
                        notes.extend(x for x in found if x not in notes)
                    else:
                        walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)

        walk(node)
        return notes

    def _remap(self, text: str) -> Tuple[str, List[str]]:
        """Storage paths in code rewritten to go through the run's OneLake shortcuts (unchanged when there are none)."""
        if not storage_remap.uses_storage(text):
            return text, []
        routes = self._routes()
        return storage_remap.rewrite(text, routes) if routes else (text, [])

    def _updating(self) -> bool:
        """Whether this run updates objects already in Fabric (run option ``existing: update``) instead of skipping them."""
        return (getattr(self.run, "settings", None) or {}).get("existing") == "update"

    def _update_definition(self, item_id: str, body: Dict[str, Any]) -> None:
        """Replace an existing item's definition in place. Its id, and so everything that calls it, stays the same."""
        self._client().create(  # type: ignore[attr-defined]
            f"/workspaces/{self.run.workspace_id}/items/{item_id}/updateDefinition", {"definition": body["definition"]})

    def _forget(self, collection: str) -> None:
        self.__dict__.setdefault("_id_cache", {}).pop(collection, None)

    @staticmethod
    def _find(ids: Dict[str, str], name: str) -> Optional[str]:
        if name in ids:
            return ids[name]
        return {k.lower(): v for k, v in ids.items()}.get(name.lower())

    def _connections(self) -> Dict[str, Dict[str, Any]]:
        """Fabric connections visible to the signed-in user, by display name."""
        cache = self.__dict__.setdefault("_conn_cache", {})
        if not cache:
            for c in self._client().list("/connections"):  # type: ignore[attr-defined]
                cache[str(c.get("displayName", ""))] = c
        return cache

    def _warehouse_info(self) -> Optional[pipelines.Warehouse]:
        """The migrated Warehouse's id and SQL endpoint, or None if it does not exist yet."""
        found = self._find_warehouse()  # type: ignore[attr-defined]
        if found is None:
            return None
        detail = self._client().get(f"/workspaces/{self.run.workspace_id}/warehouses/{found['id']}")  # type: ignore[attr-defined]
        host = str((detail.get("properties") or {}).get("connectionString") or "").replace("tcp:", "").split(",")[0].strip()
        return pipelines.Warehouse(str(found["id"]), host, self.run.warehouse)

    # -- connections (linked services) ---------------------------------------------

    @staticmethod
    def _need_definition(source: Source, what: str) -> None:
        if not source.payload:
            raise MigrationError(f"Discovery did not keep this {what}'s definition. Run discovery again.")

    def _connection(self, source: Source) -> Result:
        self._need_definition(source, "linked service")
        plan = fabric_connections.parse(source.payload or {})
        if plan.unsupported:
            pool = getattr(self.run, "pool_name", "") or ""
            if source.uses and all(pipelines.points_at_pool(source.payload or {}, pool, use) for use in source.uses):
                # Every pipeline points it at the pool being migrated, and those now use the Warehouse instead.
                return COMPLETED, "Replaced by the migrated Warehouse", self.run.warehouse, [
                    f"Every pipeline uses '{plan.name}' to reach the SQL pool '{pool}', which is now the Fabric Warehouse "
                    f"'{self.run.warehouse}'. The pipelines were pointed at the Warehouse, so no Fabric connection is needed."]
            return DEFERRED_STATUS, "Create it in Fabric by hand", None, [plan.unsupported]
        if self._connections().get(plan.name):
            return SKIPPED, "A connection with this name already exists; left unchanged", plan.name, plan.notes
        supplied = self._credentials.get(plan.name)
        if not supplied:
            return DEFERRED_STATUS, "Needs credentials", plan.name, [
                f"Enter credentials for '{plan.name}' in Plan, Stages & credentials (the Connections stage), then run the Connections stage again from Migrate. "
                "Synapse does not give up the secret, so Fabric cannot be given it automatically."] + plan.notes
        try:
            body = fabric_connections.create_body(plan, supplied, fabric_connections.key_vault_ids(self._connections()))
        except fabric_connections.CredentialError as exc:
            raise MigrationError(str(exc)) from exc
        created = self._client().create("/connections", body)  # type: ignore[attr-defined]
        self.__dict__.setdefault("_conn_cache", {})[plan.name] = {"id": created.get("id"), "displayName": plan.name}
        return COMPLETED, "Created as a Fabric connection", plan.name, plan.notes

    # -- pipelines, and the datasets folded into them ---------------------------------

    def _dataset(self, source: Source) -> Result:
        return COMPLETED, "Embedded in the pipelines that use it", None, [
            "Fabric pipelines carry their dataset settings inside each activity, so there is no separate dataset to create."]

    def _pipeline(self, source: Source) -> Result:
        self._need_definition(source, "pipeline")
        client, wid = self._client(), self.run.workspace_id  # type: ignore[attr-defined]
        existing = self._ids("dataPipelines")
        target = f"{self.run.workspace_name} / {source.name}"
        existing_id = self._find(existing, source.name)
        if existing_id and not self._updating():
            return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
        bundle = source.payload or {}
        context = pipelines.Context(
            datasets=bundle.get("datasets", {}), linked_services=bundle.get("linkedServices", {}),
            connections={n: str(c.get("id")) for n, c in self._connections().items() if c.get("id")},
            notebooks=self._ids("notebooks"), pipelines=existing, spark_jobs=self._ids("sparkJobDefinitions"),
            workspace_id=wid, warehouse=self._warehouse_info(), pool_name=getattr(self.run, "pool_name", "") or "")
        resource = bundle.get("resource") or {}
        if source.override:
            # The accepted transform is a Synapse-shaped pipeline: the converter still embeds datasets and resolves ids.
            try:
                resource = json.loads(source.override["code"])
            except ValueError as exc:
                raise MigrationError("The accepted LLM-assisted transform is not valid JSON. Discard it or accept a corrected one.") from exc
        converted = pipelines.convert(resource, context)
        if source.override:
            converted.notes.insert(0, override_note(source))
        converted.notes.extend(self._remap_sql_text(converted.definition))
        if converted.unsupported:
            return DEFERRED_STATUS, "Needs a rewrite", None, [
                "These activities have no Fabric equivalent, so the pipeline was not created: " + "; ".join(converted.unsupported) + "."]
        if converted.missing:
            raise MigrationError("It needs these in Fabric first: " + ", ".join(converted.missing)
                                 + ". Run the stages that create them (Connections, Notebooks, Warehouse) and retry.")
        description = str((resource.get("properties") or {}).get("description") or "Migrated from Azure Synapse")
        if existing_id:
            # Its id stays the same, so pipelines that call it and its schedules keep pointing at it.
            self._update_definition(existing_id, pipelines.create_body(source.name, converted.definition, description))
            return COMPLETED, "Updated in Fabric (definition replaced)", target, converted.notes
        client.create(f"/workspaces/{wid}/dataPipelines", pipelines.create_body(source.name, converted.definition, description))
        self._forget("dataPipelines")
        return COMPLETED, "Created as a Fabric data pipeline", target, converted.notes

    # -- Spark job definitions ----------------------------------------------------------

    def _sparkjob(self, source: Source) -> Result:
        self._need_definition(source, "Spark job definition")
        wid = self.run.workspace_id
        target = f"{self.run.workspace_name} / {source.name}"
        existing_id = self._find(self._ids("sparkJobDefinitions"), source.name)
        if existing_id and not self._updating():
            return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
        resource = source.payload or {}
        pool = str(((resource.get("properties") or {}).get("targetBigDataPool") or {}).get("referenceName") or "")
        env_id = self._find(self._ids("environments"), pool) if pool else None
        try:
            content, notes = jobs.spark_job(resource, env_id)
        except jobs.NotConvertible as exc:
            return DEFERRED_STATUS, "Needs a rewrite", None, [str(exc)]
        notes.extend(self._remap_job(source, content))
        if not content.get("defaultLakehouseArtifactId"):
            # Fabric refuses to run a Spark job definition without one ("'DefaultLakehouseArtifactId' property is
            # required for job execution"). The run's Lakehouse also holds its shortcuts and rewritten main files.
            lakehouse_name, lakehouse = self._lakehouse()
            content["defaultLakehouseArtifactId"] = lakehouse
            notes.append(f"Default Lakehouse: {lakehouse_name} (Fabric needs one to run a Spark job definition).")
        body = jobs.spark_job_body(source.name, content, "Migrated from Azure Synapse")
        if existing_id:
            self._update_definition(existing_id, body)
            return COMPLETED, "Updated in Fabric (definition replaced)", target, notes
        self._client().create(f"/workspaces/{wid}/sparkJobDefinitions", body)  # type: ignore[attr-defined]
        self._forget("sparkJobDefinitions")
        return COMPLETED, "Created as a Fabric Spark job definition", target, notes

    # -- SQL scripts -> T-SQL notebooks ----------------------------------------------------

    def _script(self, source: Source) -> Result:
        self._need_definition(source, "SQL script")
        wid = self.run.workspace_id
        target = f"{self.run.workspace_name} / {source.name}"
        existing_id = self._find(self._ids("notebooks"), source.name)
        if existing_id and not self._updating():
            return SKIPPED, "A notebook with this name is already in the workspace; left unchanged", target, []
        warehouse = self._warehouse_info()
        payload = copy.deepcopy(source.payload or {})
        content = payload.setdefault("properties", {}).setdefault("content", {})
        if source.override:
            content["query"] = source.override["code"]
        content["query"], remap_notes = self._remap(str(content.get("query") or ""))
        try:
            document, notes = jobs.sql_script_notebook(payload, warehouse.artifact_id if warehouse else None, self.run.warehouse)
            if source.override:
                notes.insert(0, override_note(source))
            notes.extend(remap_notes)
        except jobs.NotConvertible as exc:
            return DEFERRED_STATUS, "Needs a rewrite", None, [str(exc)]
        if existing_id:
            self._update_definition(existing_id, jobs.sql_script_body(source.name, document))
            return COMPLETED, "Updated in Fabric (definition replaced)", target, notes
        self._client().create(f"/workspaces/{wid}/notebooks", jobs.sql_script_body(source.name, document))  # type: ignore[attr-defined]
        self._forget("notebooks")
        return COMPLETED, "Created as a T-SQL notebook", target, notes

    # -- triggers -> pipeline schedules ------------------------------------------------------

    def _schedule(self, source: Source) -> Result:
        self._need_definition(source, "trigger")
        wid, client = self.run.workspace_id, self._client()  # type: ignore[attr-defined]
        try:
            bodies, notes = jobs.schedule_bodies(source.payload or {}, as_in_synapse=self.run.settings.get("schedules") == "match")
        except jobs.NotConvertible as exc:
            return DEFERRED_STATUS, "Recreate it by hand", None, [str(exc)]
        pipeline_ids = self._ids("dataPipelines")
        created: List[str] = []
        for name, body in bodies:
            pid = self._find(pipeline_ids, name)
            if not pid:
                raise MigrationError(f"Pipeline '{name}' is not in Fabric yet. Run the Pipelines stage first, then retry.")
            path = f"/workspaces/{wid}/items/{pid}/jobs/Pipeline/schedules"
            if client.list(path):
                notes.append(f"Pipeline '{name}' already has a schedule; left unchanged.")
                continue
            client.create(path, body)
            created.append(name)
        if not created:
            return SKIPPED, "The pipeline(s) already have a schedule; left unchanged", None, notes
        state = "switched on" if bodies and bodies[0][1].get("enabled") else "switched off"
        return COMPLETED, f"Schedule created ({state}) on {', '.join(created)}", None, notes

    # -- external tables -> OneLake shortcuts ----------------------------------------------------

    def _shortcut(self, source: Source) -> Result:
        schema, name = source.schema or "dbo", source.object_name or source.name
        reader = self._source_connection().cursor()  # type: ignore[attr-defined]
        reader.execute(jobs.EXTERNAL_LOCATION_SQL, schema, name)
        row = reader.fetchone()
        if not row:
            return DEFERRED_STATUS, "Location not found", None, ["The pool did not report where this external table's data lives."]
        try:
            endpoint, subpath = jobs.shortcut_target(str(row[0] or ""), str(row[1] or ""))
        except jobs.NotConvertible as exc:
            return DEFERRED_STATUS, "Recreate it by hand", None, [str(exc)]
        connection = next((c for c in self._connections().values()
                           if str((c.get("connectionDetails") or {}).get("path", "")).lower().startswith(endpoint.lower()) and c.get("id")), None)
        if connection is None:
            raise MigrationError(f"There is no Fabric connection to {endpoint}. Run the Connections stage for the storage linked service first.")
        wid, client = self.run.workspace_id, self._client()  # type: ignore[attr-defined]
        lakehouse_name, lakehouse = self._lakehouse()
        body = {"path": f"Files/{schema}", "name": name,
                "target": {"adlsGen2": {"location": endpoint, "subpath": subpath, "connectionId": str(connection["id"])}}}
        target = f"{lakehouse_name}/Files/{schema}/{name}"
        try:
            client.create(f"/workspaces/{wid}/items/{lakehouse}/shortcuts", body)
        except FabricApiError as exc:
            if exc.status == 409 or "AlreadyExists" in exc.code or "AlreadyInUse" in exc.code:
                return SKIPPED, "Shortcut already exists; left unchanged", target, []
            raise
        return COMPLETED, "Created as a OneLake shortcut", target, ["It is under Files; promote it to a table in the Lakehouse to query it with SQL."]
