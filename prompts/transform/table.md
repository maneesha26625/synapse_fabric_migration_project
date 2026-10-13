The target is a **Fabric Warehouse** table (Delta storage, managed by Fabric). Return one
`CREATE TABLE` statement for the **empty** table. The rows are loaded separately by a Fabric data
pipeline, which copies each column into the type you choose here.

- Keep the name `{{object.schema}}.{{object.name}}`, every column, the column order and nullability.
- No storage options: no `DISTRIBUTION`, `CLUSTERED COLUMNSTORE INDEX`, `HEAP`, `PARTITION` or index clauses.
- **Types: use the same mapping as the rule output.** The data pipeline and validation expect it:
  `datetime` → `datetime2(3)`, `smalldatetime` → `datetime2(0)`, `nvarchar(n)` → `varchar(n×4)` (UTF-8),
  `money` → `decimal(19,4)`, `tinyint` → `smallint`, `text`/`ntext`/`xml` → `varchar(MAX)`,
  `image` → `varbinary(MAX)`, `rowversion` → `varbinary(8)`, `geography`/`geometry` → `varbinary(MAX)`.
  Only change a type where the rules failed (for example `sql_variant`, or a type they reported as
  unsupported) or where the project requirements say to. Say why in `notes`.
- `IDENTITY` columns: the column stays as plain data loaded as it is, so remove `IDENTITY` and add a note.
- Do **not** add primary-key, unique or foreign-key constraints. The accelerator adds the keys itself, as
  `NOT ENFORCED`, after the table is created.
