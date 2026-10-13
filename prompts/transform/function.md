The target is a **Fabric Warehouse** user-defined function. Return the full `CREATE FUNCTION` statement.

- Keep the name `{{object.schema}}.{{object.name}}`, its parameters (names, order and types) and what it
  returns. Views, procedures and reports call it by that signature.
- A Warehouse supports **scalar** functions and **inline table-valued** functions
  (`RETURNS TABLE AS RETURN SELECT ...`). It does **not** support multi-statement table-valued
  functions (`RETURNS @t TABLE (...) AS BEGIN ... END`). Rewrite one as a single inline `SELECT`
  (common table expressions, `UNION ALL` and `CASE` usually cover what the statements did) and say
  in `notes` how it was rewritten. If the logic cannot be one `SELECT`, keep the original and list it
  in `unconverted` (it may need to become a view or a stored procedure instead).
- Scalar functions must stay deterministic and free of side effects; remove nothing they compute.
- `sys.pdw_*` views do not exist in Fabric. Use the Fabric equivalent, or list it in `unconverted`.
