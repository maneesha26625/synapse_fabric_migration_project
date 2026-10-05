"""The stage handlers: one method per kind of object the runner can migrate beyond
the first four. Mixed into ``Migrator``, which supplies the Fabric client, the
Warehouse session and the run state they use.

Each handler returns ``(status, step, target, notes)`` or raises
``MigrationError`` / ``FabricApiError``, exactly like the handlers in the runner.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from discovery_agent.migration import datacopy, warehouse_ddl
from discovery_agent.migration.common import (
    COMPLETED, DEFERRED_STATUS, SKIPPED, MigrationError, Source,
)

Result = tuple  # (status, step, target, notes)


class StageMixin:
    """Handlers for data, connections, pipelines, jobs, scripts, schedules and shortcuts."""

    # Provided by Migrator ------------------------------------------------
    run: Any
    _source_factory: Any
    _source: Any
    _item: Any

    # -- shared helpers ------------------------------------------------------

    def _source_connection(self) -> Any:
        """A read-only session on the Synapse pool the data comes from."""
        if self._source is None:
            if self._source_factory is None:
                raise MigrationError("The Synapse source is not connected, so there is nothing to read the rows from. Reconnect it on the Synapse Source page.")
            try:
                self._source = self._source_factory()
            except Exception as exc:  # noqa: BLE001 - driver, token or network failure
                raise MigrationError(f"Could not connect to the Synapse pool to read data: {type(exc).__name__}: {str(exc)[:300]}") from exc
        return self._source

    def _progress(self, text: str) -> None:
        item = getattr(self, "_item", None)
        if item is not None:
            with self.run.lock:
                item.step = text

    @staticmethod
    def _scalar(cursor: Any, sql: str, *params: Any) -> int:
        cursor.execute(sql, *params)
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    # -- table data ------------------------------------------------------------

    def _data(self, source: Source) -> Result:
        table = source.payload
        schema, name = source.schema or "dbo", source.object_name or source.name
        target = f"{self.run.warehouse}.{schema}.{name}"
        problem = datacopy.preflight(table)
        if problem:
            if problem[0] == "EXTERNAL":
                return SKIPPED, "External table: no rows to copy", target, [problem[1]]
            raise MigrationError(problem[1])

        settings = self.run.settings
        mode = settings.get("dataMode", "if_empty")
        limit = int(settings.get("maxRows") or datacopy.DEFAULT_MAX_ROWS)
        cursor = self._warehouse().cursor()
        if not self._exists(cursor, schema, name):
            raise MigrationError("The table is not in the warehouse yet. Run the Warehouse stage first (it creates the table, this stage fills it).")
        existing = self._scalar(cursor, datacopy.count_sql(schema, name))
        if existing and mode == "if_empty":
            return SKIPPED, f"Already has {datacopy.describe_count(existing)}; left unchanged", target, []

        reader = self._source_connection().cursor()
        total = self._scalar(reader, datacopy.count_sql(table.key.schema, table.key.name))
        if total > limit:
            return DEFERRED_STATUS, "Too large for a direct copy", target, [
                f"{datacopy.describe_count(total)} is over the {limit:,}-row limit. Use a pipeline Copy activity for this table."]
        if total == 0:
            if existing:
                self._run_sql(cursor, f"TRUNCATE TABLE {warehouse_ddl.qualified(schema, name)}", "clear the table")
            return COMPLETED, "No rows to copy (the source table is empty)", target, []

        notes: List[str] = []
        if existing:
            self._run_sql(cursor, f"TRUNCATE TABLE {warehouse_ddl.qualified(schema, name)}", "clear the table")
            notes.append(f"Replaced {datacopy.describe_count(existing)} that were already there.")
        try:
            written = datacopy.copy_rows(
                reader, cursor, table,
                on_progress=lambda n: self._progress(f"Loading {n:,} of {total:,} rows"))
            landed = self._scalar(cursor, datacopy.count_sql(schema, name))
        except Exception as exc:  # noqa: BLE001 - the driver's message is the useful part
            self._best_effort(cursor, f"TRUNCATE TABLE {warehouse_ddl.qualified(schema, name)}")
            raise MigrationError(f"The copy stopped and the partial rows were cleared: {type(exc).__name__}: {str(exc)[:300]}") from exc
        if landed != total or written != total:
            self._best_effort(cursor, f"TRUNCATE TABLE {warehouse_ddl.qualified(schema, name)}")
            raise MigrationError(f"Row counts do not match: {datacopy.describe_count(total)} in Synapse, {datacopy.describe_count(landed)} in the warehouse. The partial rows were cleared.")
        return COMPLETED, f"Loaded {datacopy.describe_count(total)}; counts match", target, notes

    @staticmethod
    def _best_effort(cursor: Any, statement: str) -> None:
        try:
            cursor.execute(statement)
        except Exception:  # noqa: BLE001 - cleanup must not mask the real failure
            pass
