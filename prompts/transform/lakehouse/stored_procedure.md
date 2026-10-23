The target is a **Fabric Lakehouse** `{{target.lakehouse}}`: Delta tables written by Spark, with a
**read-only** SQL analytics endpoint. A stored procedure that changes data cannot live there, so it becomes a
**PySpark notebook** attached to the Lakehouse. Pipelines that called the procedure run this notebook with a
Notebook activity, passing the same parameter values.

Return the whole notebook as JSON in the same shape as the rule output:
`{"language": "python", "cells": [{"cell_type": "markdown" | "code", "source": "...", "tags": ["parameters"]?}]}`.

- **Start from the rule output.** It already has the right shape: a title cell, a parameters cell (tagged
  `parameters`), a helpers cell (`q()` runs Spark SQL with the procedure's variables as named parameters `:name`,
  `scalar()`, `truth()`, `affected()`), and `def run():` with the body. Keep that shape and the helper names; fix
  what it marked `# REVIEW` and anything it got wrong.
- **Parameters.** One variable per procedure parameter in the parameters cell, named as in the rule output
  (lower-case, without `@`), with the same defaults. A pipeline overrides them by those names.
- **Exit value.** End with `notebookutils.notebook.exit(json.dumps({...}))` carrying the return value
  (`"return_value"`) and every OUTPUT parameter by name, as the rule output does. Callers read it.
- **Spark SQL, not T-SQL.** Tables are `schema.table` in the default Lakehouse (no database name, no brackets: use
  backticks). Use Delta statements: `MERGE INTO`, `UPDATE`, `DELETE FROM`, `INSERT INTO ... SELECT`,
  `CREATE OR REPLACE TABLE ... AS SELECT`. `TRUNCATE` becomes `DELETE FROM`. `TOP n` becomes `LIMIT n`.
  T-SQL functions become Spark ones (`GETDATE()` → `current_timestamp()`, `ISNULL` → `coalesce`, `LEN` →
  `length(rtrim(...))`, `CHARINDEX(a, b)` → `locate(a, b)`, `DATEADD`/`DATEDIFF` with the same boundary semantics).
- **`UPDATE ... FROM` and `DELETE ... FROM` with joins** become `MERGE INTO`. A MERGE fails when one target row
  matches several source rows; make the source unique (or say in `unconverted` why it cannot be).
- **Temporary tables** (`#t`) and table variables become Delta tables in the `tmp_work` schema, created in the
  notebook and dropped at the end in a `finally:`.
- **Transactions do not exist across statements** in Delta: each statement commits on its own. Remove
  `BEGIN TRAN`/`COMMIT`/`ROLLBACK`. If the procedure relied on rolling back several statements together, make the
  steps safe to re-run (overwrite, or MERGE) and say so in `notes`.
- **Control flow is Python**: `IF`/`WHILE` → `if`/`while` with the condition evaluated by Spark, `TRY`/`CATCH` →
  `try`/`except`, `RAISERROR`/`THROW` → `raise`, `PRINT` → `print`, `RETURN` → `return`. Cursors: prefer one
  set-based statement; otherwise loop over `q(...).collect()`.
- **Calls to other procedures** (`EXEC schema.proc`) run their notebooks:
  `notebookutils.notebook.run("sp_<schema>_<proc>", 3600, {params})`, reading OUTPUT values from its exit JSON.
- **Dynamic SQL** must become Spark SQL built in Python (f-strings for identifiers, named parameters for values).
- **Text comparisons are case-sensitive** in Spark. Where the procedure relied on Synapse's case-insensitive
  collation (joins or filters on text), use `lower()` on both sides and say so in `notes`.
- Code that reads storage (`COPY INTO`, `OPENROWSET`) becomes `spark.read` of the files (through a OneLake
  shortcut if the requirements name one) written with `saveAsTable` / `INSERT INTO`.
- Do **not** create or drop tables the procedure does not already create or drop.
