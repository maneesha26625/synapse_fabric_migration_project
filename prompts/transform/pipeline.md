**Important: return a Synapse pipeline, not a Fabric one.** Your output goes back through the
accelerator's pipeline converter. That converter puts the datasets inline, points activities at Fabric
connection and item **ids** (which only exist at run time), and repoints the SQL pool to the Warehouse.
Your job is to get the pipeline into a shape that converter accepts. Return the whole pipeline
**resource JSON** in the same shape as the source (`{"name": ..., "properties": {"activities": [...], ...}}`).

Activities the converter supports (keep these as they are, unless the requirements say otherwise):
`Copy`, `Lookup`, `GetMetadata`, `Delete`, `Validation`, `Script`, `SqlPoolStoredProcedure`,
`SqlServerStoredProcedure`, `SynapseNotebook`, `SparkJob`, `ExecutePipeline`, `ForEach`, `Until`,
`IfCondition`, `Switch`, `Wait`, `SetVariable`, `AppendVariable`, `Fail`, `Filter`, `WebActivity`, `WebHook`,
`AzureFunctionActivity`, the Databricks / HDInsight activities, `Custom`, `AzureMLExecutePipeline`.

What to do with each activity the converter rejects:
- `ExecuteDataFlow` (mapping data flow): replace it with a `SynapseNotebook` activity that calls a
  notebook named `df_<data flow name>`. In `notes`, describe what that notebook must do (sources,
  transformations, sink), and add "write notebook df_<name>" to `unconverted`.
- `ExecuteSSISPackage`, `AzureDataExplorerCommand`, `DataLakeAnalyticsU-SQL`, and other activities
  with no Fabric equivalent: replace each one with a `Fail` activity whose `message` says what the
  original did, and add it to `unconverted`.
- Integration-runtime references (`connectVia`) to a **self-hosted** runtime: keep them, and add an
  on-premises data gateway item to `unconverted`.

Never change:
- activity names (other activities and `dependsOn` refer to them),
- `dependsOn` conditions, parameters, variables or expressions (`@pipeline()...`, `@activity(...)`),
- dataset, notebook, Spark job or pipeline **reference names**. The converter resolves them.

{{#if rules.problems}}
Fix the activities listed under "Problems the rules could not solve".
{{/if}}
