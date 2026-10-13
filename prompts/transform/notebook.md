The target is a **Fabric notebook** running on the workspace's Fabric Spark runtime, attached to a Lakehouse, with the Warehouse `{{target.warehouse}}` reachable. The source is the notebook's cells as
JSON. **Return the same shape**: `{"language": "<notebook language>", "cells": [{"cell_type": "code" | "markdown",
"language": "<only when the cell overrides the notebook language>", "source": "<cell text>"}]}`.

- Keep the cells **in order and one for one**: same count, same markdown, same purpose. Convert a cell in place;
  don't merge or split cells unless that is the only way to keep the behaviour, and say so in `notes`.
- `mssparkutils` → `notebookutils` (same API). `from notebookutils import mssparkutils` is no longer needed.
- `TokenLibrary`, `mssparkutils.credentials.getSecretWithLS / getConnectionStringOrCreds / getPropertiesAll`: there are
  no linked services in Fabric. Use `notebookutils.credentials.getSecret("<key vault url>", "<secret name>")`.
  If the vault or secret name isn't known, keep a clear placeholder and add it to `unconverted`.
- `mssparkutils.env.*` (workspace, pool, job ids): use `notebookutils.runtime.context` where there is an equivalent;
  otherwise list it in `unconverted`.
- Synapse SQL connector: `spark.read.synapsesql("pool.schema.table")` → `spark.read.synapsesql("{{target.warehouse}}.schema.table")`
  with `import com.microsoft.spark.fabric` (Scala) / `from com.microsoft.spark.fabric.Constants import Constants` (Python).
  For writes (`.write.synapsesql(...)`), use the Fabric connector's `synapsesql` write mode, or write Delta to the Lakehouse
  and list the choice in `notes`.
- `abfss://...dfs.core.windows.net/...` paths still work if the Fabric workspace identity has access. Keep them, and in
  `notes` suggest a OneLake shortcut. If the project requirements give a Lakehouse path, use it instead.
- Spark pool configuration magics (`%%configure`) stay only with settings Fabric accepts (`driverMemory`, `executorMemory`,
  `numExecutors`, `conf`). Remove `%%configure` keys for Synapse pools.
{{#if notebook.language}}
- The notebook language is `{{notebook.language}}`. **If it is .NET (C#/`csharp`/`dotnetspark`)**, Fabric can't run it:
  rewrite every code cell in PySpark, set `"language": "python"`, keep the same logic, and say in `notes` which
  .NET APIs became which PySpark calls.
{{/if}}
- Don't add display/print calls, retries or logging that aren't in the source.
