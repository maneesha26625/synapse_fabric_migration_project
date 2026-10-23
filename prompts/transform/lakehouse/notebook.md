The target is a **Fabric notebook** running on the workspace's Fabric Spark runtime. The SQL pool became the
Fabric **Lakehouse** `{{target.lakehouse}}`: its tables are Delta tables the notebook reads and writes by name, with
that Lakehouse attached as its default. The source is the notebook's cells as JSON. **Return the same shape**:
`{"language": "<notebook language>", "cells": [{"cell_type": "code" | "markdown", "language": "<only when the cell
overrides the notebook language>", "source": "<cell text>"}]}`.

- Keep the cells **in order and one for one**: same count, same markdown, same purpose. Convert a cell in place.
- `mssparkutils` → `notebookutils` (same API). `from notebookutils import mssparkutils` is no longer needed.
- `TokenLibrary`, `mssparkutils.credentials.getSecretWithLS / getConnectionStringOrCreds / getPropertiesAll`: there are
  no linked services in Fabric. Use `notebookutils.credentials.getSecret("<key vault url>", "<secret name>")`.
  If the vault or secret name isn't known, keep a clear placeholder and add it to `unconverted`.
- **Reads of the SQL pool** (`spark.read.synapsesql("pool.schema.table")`, with or without `.option(...)`) become
  `spark.read.table("schema.table")`. **Writes** (`df.write.synapsesql(...)`) become
  `df.write.mode("append" | "overwrite").saveAsTable("schema.table")`, keeping the original mode; say which in `notes`.
- SQL cells that queried the pool (`%%sql`) run as Spark SQL on the Lakehouse tables: Spark syntax, `schema.table`.
- `abfss://...dfs.core.windows.net/...` paths still work if the Fabric workspace identity has access. Keep them, and in
  `notes` suggest a OneLake shortcut. If the project requirements give a Lakehouse path, use it instead.
- Spark pool configuration magics (`%%configure`) stay only with settings Fabric accepts. Remove keys for Synapse pools.
{{#if notebook.language}}
- The notebook language is `{{notebook.language}}`. **If it is .NET (C#/`csharp`/`dotnetspark`)**, Fabric can't run it:
  rewrite every code cell in PySpark, set `"language": "python"`, and say in `notes` which .NET APIs became which calls.
{{/if}}
- Don't add display/print calls, retries or logging that aren't in the source.
