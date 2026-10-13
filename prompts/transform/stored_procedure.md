The target is a **Fabric Warehouse** (T-SQL, Delta tables, snapshot isolation). Return the full
`CREATE PROCEDURE` statement.

- Keep the name `{{object.schema}}.{{object.name}}`, the parameters (names, types, defaults, `OUTPUT`) and
  the result sets the callers get.
- Remove anything that only matters to a dedicated pool: `DISTRIBUTION`, `CLUSTERED COLUMNSTORE INDEX`,
  `HEAP`, `PARTITION` options, `OPTION (LABEL = ...)` where it is only telemetry, and workload management.
- `RENAME OBJECT` becomes `EXEC sp_rename`.
- `sys.pdw_*` and `sys.dm_pdw_*` views do not exist in Fabric. Rewrite the query with the Fabric
  equivalents (`sys.dm_exec_requests`, `queryinsights.*`). If there is none, list it in `unconverted`.
- Fabric Warehouse does not support these, so rewrite them:
  - `MERGE`: where it isn't supported, use `UPDATE` + `INSERT` (or `DELETE` + `INSERT`) inside a transaction, keeping the matching logic.
  - `IDENTITY` columns in `CREATE TABLE`: use `ROW_NUMBER()` plus an offset, or a key passed in.
  - `TRUNCATE TABLE`: keep it. It is supported.
  - Temporary tables: `#temp` tables are session-scoped and supported. Global `##temp` tables are not; replace them with a regular table you clean up at the end.
  - `SET ANSI_NULLS / QUOTED_IDENTIFIER` and other `SET` options a Warehouse ignores: remove them and say so in `notes`.
  - Cursors: keep a simple read-only cursor, but prefer a set-based rewrite when it is straightforward.
  - Dynamic SQL (`EXEC(@sql)`, `sp_executesql`): convert the SQL text inside it too.
- `COPY INTO` with `CREDENTIAL = (IDENTITY = 'Managed Identity')`, including inside dynamic SQL: a Warehouse
  does not accept Synapse's managed identity. Remove the `CREDENTIAL` option, so `COPY` reads the storage as
  the Microsoft Entra identity running it, unless the requirements name another method (for example a SAS
  secret). Add to `unconverted` that this identity needs Storage Blob Data Reader on the storage account.
- `SET IDENTITY_INSERT ... ON/OFF`: tables are migrated **without** `IDENTITY`, and the statement fails on them.
  Remove it; the explicit key values then insert as they are.
- Code that reads or writes a storage account (`https://<account>.dfs|blob.core.windows.net/...`, `OPENROWSET(BULK ...)`):
  it now runs as the user or pipeline identity, not the Synapse managed identity. Keep it, and list the account in
  `unconverted` as needing access (or a OneLake shortcut, if the requirements give one).
- Transactions: Fabric runs every transaction under snapshot isolation. Remove `SET TRANSACTION
  ISOLATION LEVEL` other than `SNAPSHOT`, and check for logic that relied on locking.
- Do **not** create or alter tables the procedure does not already create.
