**Important: return a Synapse pipeline, not a Fabric one.** Your output goes back through the
accelerator's pipeline converter, which puts the datasets inline, points activities at Fabric item **ids**,
and repoints the SQL pool to the Fabric **Lakehouse** `{{target.lakehouse}}`. Return the whole pipeline
**resource JSON** in the same shape as the source (`{"name": ..., "properties": {"activities": [...], ...}}`).

What the converter does with the SQL pool when the target is a Lakehouse, and so what you must shape the pipeline for:
- `SqlPoolStoredProcedure` / `SqlServerStoredProcedure` on the pool: **kept as they are**. The converter turns each
  into a Notebook activity that runs the notebook the procedure became, with the same parameter values.
- `Copy` from or to a pool **table** (a dataset with a schema and table): kept. It reads or writes the Lakehouse's
  Delta table. A sink's pre-copy script may only empty the table (`TRUNCATE TABLE` / `DELETE FROM`): it becomes an overwrite.
- A `Copy` or `Lookup` that reads the pool **with a query** (`sqlReaderQuery`) or a procedure: a Lakehouse table is
  read whole, so the converter refuses it. Replace it with a `SynapseNotebook` activity calling a notebook named
  `q_<activity name>` that runs the query with Spark SQL, and add "write notebook q_<activity name>" to `unconverted`
  with the query in `notes`. If a `Lookup`'s output is used by later activities, keep its name on the replacement
  and say in `notes` that the notebook must return the rows with `notebookutils.notebook.exit`.
- `Script` activities on the pool (or on the serverless SQL pool): the Lakehouse endpoint is read-only and pipelines
  cannot reach it. Replace each with a `SynapseNotebook` activity calling a notebook named `s_<activity name>` and add
  "write notebook s_<activity name>" to `unconverted`, with the script in `notes`.

Activities the converter supports otherwise: `Copy`, `Lookup`, `GetMetadata`, `Delete`, `Validation`, `SynapseNotebook`,
`SparkJob`, `ExecutePipeline`, `ForEach`, `Until`, `IfCondition`, `Switch`, `Wait`, `SetVariable`, `AppendVariable`,
`Fail`, `Filter`, `WebActivity`, `WebHook`, `AzureFunctionActivity`, the Databricks / HDInsight activities, `Custom`,
`AzureMLExecutePipeline`. `ExecuteDataFlow` becomes a `SynapseNotebook` calling `df_<data flow name>` (add it to
`unconverted`); activities with no Fabric equivalent become a `Fail` activity whose `message` says what it did.

Never change activity names, `dependsOn` conditions, parameters, variables, expressions, or the reference names of
datasets, notebooks, Spark jobs and pipelines: the converter resolves them.

{{#if rules.problems}}
Fix the activities listed under "Problems the rules could not solve".
{{/if}}
