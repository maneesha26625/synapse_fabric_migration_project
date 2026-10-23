The target is a **Delta table** in the Fabric Lakehouse `{{target.lakehouse}}` (schemas enabled). It is created
by Spark, so return one **Spark SQL** statement:
`CREATE TABLE IF NOT EXISTS `{{object.schema}}`.`{{object.name}}` (...) USING DELTA`. The rows are loaded
separately by a Fabric data pipeline that appends to (or overwrites) this table.

- Keep the name `{{object.schema}}.{{object.name}}`, every column and the column order. Keep `NOT NULL` where the
  source has it.
- Spark SQL syntax: backtick identifiers, no brackets, no `WITH (...)` storage options, no index, distribution or
  partition clauses from the pool.
- **Types: use the same mapping as the rule output.** The pipeline, the SQL analytics endpoint and validation expect
  it: `bit` → `BOOLEAN`, `tinyint` → `SMALLINT`, `int`/`bigint`/`smallint` as they are, `decimal(p,s)` →
  `DECIMAL(p,s)`, `money` → `DECIMAL(19,4)`, `float` → `DOUBLE`, `real` → `FLOAT`, `date` → `DATE`,
  `datetime`/`datetime2`/`smalldatetime`/`datetimeoffset` → `TIMESTAMP`, `time` → `STRING`, every text type →
  `STRING`, `uniqueidentifier` → `STRING`, binary types → `BINARY`. Only change a type where the rules failed or
  the project requirements say to, and say why in `notes`.
- **No `IDENTITY`, no keys.** A Delta table in a Lakehouse does not generate values and has no primary key, unique
  or foreign-key constraints. Leave them out and list in `unconverted` any table whose new rows relied on IDENTITY.
- Column names with spaces or `,;{}()=` need Delta column mapping:
  `TBLPROPERTIES ('delta.columnMapping.mode' = 'name', 'delta.minReaderVersion' = '2', 'delta.minWriterVersion' = '5')`.
- Partitioning (`PARTITIONED BY`) only where the project requirements ask for it; the SQL pool's partitions do not carry over.
