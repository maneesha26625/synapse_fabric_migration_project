The target is a user-defined function on the **SQL analytics endpoint** of the Fabric Lakehouse
`{{target.lakehouse}}`. Return the full T-SQL `CREATE FUNCTION` statement; the run creates it on the endpoint.

- Keep the name `{{object.schema}}.{{object.name}}`, its parameters (names, order and types) and what it returns.
- The endpoint supports **scalar** and **inline table-valued** functions only. Rewrite a multi-statement
  table-valued function as one inline `SELECT`, or keep the original and list it in `unconverted`.
- A function on the endpoint reads only; it cannot change a table.
- Tables it reads are the Lakehouse's Delta tables by `schema.table`. Text compares **case-sensitively** there;
  where the function relied on case-insensitive comparison, use `LOWER()` / `UPPER()` and say so in `notes`.
- `sys.pdw_*` views do not exist in Fabric. Use the Fabric equivalent, or list it in `unconverted`.
