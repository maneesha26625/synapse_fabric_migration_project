"""Stage handlers that create Fabric items: connections, pipelines, Spark jobs,
SQL-script notebooks, schedules and shortcuts. Mixed into ``Migrator``.

Each returns ``(status, step, target, notes)`` or raises ``MigrationError`` /
``FabricApiError``, like every handler in the runner.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from discovery_agent.migration import fabric_connections, jobs, pipelines
from discovery_agent.migration.common import COMPLETED, DEFERRED_STATUS, SKIPPED, MigrationError, Source
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
            body = fabric_connections.create_body(plan, supplied)
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
        if self._find(existing, source.name):
            return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
        bundle = source.payload or {}
        context = pipelines.Context(
            datasets=bundle.get("datasets", {}), linked_services=bundle.get("linkedServices", {}),
            connections={n: str(c.get("id")) for n, c in self._connections().items() if c.get("id")},
            notebooks=self._ids("notebooks"), pipelines=existing, spark_jobs=self._ids("sparkJobDefinitions"),
            workspace_id=wid, warehouse=self._warehouse_info(), pool_name=getattr(self.run, "pool_name", "") or "")
        resource = bundle.get("resource") or {}
        converted = pipelines.convert(resource, context)
        if converted.unsupported:
            return DEFERRED_STATUS, "Needs a rewrite", None, [
                "These activities have no Fabric equivalent, so the pipeline was not created: " + "; ".join(converted.unsupported) + "."]
        if converted.missing:
            raise MigrationError("It needs these in Fabric first: " + ", ".join(converted.missing)
                                 + ". Run the stages that create them (Connections, Notebooks, Warehouse) and retry.")
        description = str((resource.get("properties") or {}).get("description") or "Migrated from Azure Synapse")
        client.create(f"/workspaces/{wid}/dataPipelines", pipelines.create_body(source.name, converted.definition, description))
        self._forget("dataPipelines")
        return COMPLETED, "Created as a Fabric data pipeline", target, converted.notes

    # -- Spark job definitions ----------------------------------------------------------

    def _sparkjob(self, source: Source) -> Result:
        self._need_definition(source, "Spark job definition")
        wid = self.run.workspace_id
        target = f"{self.run.workspace_name} / {source.name}"
        if self._find(self._ids("sparkJobDefinitions"), source.name):
            return SKIPPED, "Already in the Fabric workspace; left unchanged", target, []
        resource = source.payload or {}
        pool = str(((resource.get("properties") or {}).get("targetBigDataPool") or {}).get("referenceName") or "")
        env_id = self._find(self._ids("environments"), pool) if pool else None
        try:
            content, notes = jobs.spark_job(resource, env_id)
        except jobs.NotConvertible as exc:
            return DEFERRED_STATUS, "Needs a rewrite", None, [str(exc)]
        self._client().create(f"/workspaces/{wid}/sparkJobDefinitions", jobs.spark_job_body(source.name, content, "Migrated from Azure Synapse"))  # type: ignore[attr-defined]
        self._forget("sparkJobDefinitions")
        return COMPLETED, "Created as a Fabric Spark job definition", target, notes

    # -- SQL scripts -> T-SQL notebooks ----------------------------------------------------

    def _script(self, source: Source) -> Result:
        self._need_definition(source, "SQL script")
        wid = self.run.workspace_id
        target = f"{self.run.workspace_name} / {source.name}"
        if self._find(self._ids("notebooks"), source.name):
            return SKIPPED, "A notebook with this name is already in the workspace; left unchanged", target, []
        warehouse = self._warehouse_info()
        try:
            document, notes = jobs.sql_script_notebook(source.payload or {}, warehouse.artifact_id if warehouse else None, self.run.warehouse)
        except jobs.NotConvertible as exc:
            return DEFERRED_STATUS, "Needs a rewrite", None, [str(exc)]
        self._client().create(f"/workspaces/{wid}/notebooks", jobs.sql_script_body(source.name, document))  # type: ignore[attr-defined]
        self._forget("notebooks")
        return COMPLETED, "Created as a T-SQL notebook", target, notes

    # -- triggers -> pipeline schedules ------------------------------------------------------

    def _schedule(self, source: Source) -> Result:
        self._need_definition(source, "trigger")
        wid, client = self.run.workspace_id, self._client()  # type: ignore[attr-defined]
        try:
            bodies, notes = jobs.schedule_bodies(source.payload or {})
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
        return COMPLETED, f"Schedule created (switched off) on {', '.join(created)}", None, notes

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
        lakehouse_name = f"{self.run.warehouse}_lakehouse"
        lakehouse = self._find(self._ids("lakehouses"), lakehouse_name)
        if not lakehouse:
            created = client.create(f"/workspaces/{wid}/lakehouses", {"displayName": lakehouse_name, "description": "Shortcuts for migrated external tables"})
            self._forget("lakehouses")
            lakehouse = str(created.get("id") or "") or self._find(self._ids("lakehouses"), lakehouse_name) or ""
        if not lakehouse:
            raise MigrationError("The Lakehouse was created but Fabric did not return its id. Retry in a minute.")
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
