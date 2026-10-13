The script becomes **one T-SQL cell in a Fabric notebook** that runs against the Warehouse
`{{target.warehouse}}`. Return only the converted T-SQL text of the script, not a notebook.

- Apply the same rules as for stored procedures: no dedicated-pool storage options, no workload
  management, no `sys.pdw_*`, `RENAME OBJECT` → `sp_rename`, snapshot isolation only.
- `USE <database>` does not work in a Fabric notebook cell. Remove it; the cell is already bound to the Warehouse.
- Keep `GO` batch separators only where a statement must be first in its batch
  (`CREATE VIEW`, `CREATE PROCEDURE`, `CREATE FUNCTION`).
- Ad-hoc exploration (a `SELECT` with no side effects) is converted like everything else, without comment.
