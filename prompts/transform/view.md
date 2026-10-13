The target is a **Fabric Warehouse** view. Return the full `CREATE VIEW` statement.

- Keep the name `{{object.schema}}.{{object.name}}`, the column names, their order and their types. Callers
  and reports depend on them.
- Materialized views do not exist in a Warehouse. Convert `CREATE MATERIALIZED VIEW` to a plain view
  and say in `notes` that it is no longer precomputed. If the requirements ask for a table instead,
  list that in `unconverted`.
- Remove table hints a Warehouse rejects (`NOLOCK` and other locking hints) and say so in `notes`.
- `sys.pdw_*` views do not exist in Fabric. Use the Fabric equivalent, or list it in `unconverted`.
- If it references a table in another database through `OPENROWSET` or an external table, keep the
  reference as it is and list it in `unconverted` (it becomes a OneLake shortcut, handled elsewhere).
- `WITH SCHEMABINDING`: keep it only if every referenced object is in the same Warehouse.
