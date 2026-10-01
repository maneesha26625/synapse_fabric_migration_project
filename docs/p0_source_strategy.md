# P0 Source Strategy

Which source is authoritative for each of the nine frozen P0 Synapse
data-engineering artifacts, and what each source can and cannot tell us.

The machine-readable form of this document is
`src/discovery_agent/source_strategy.py`. If the two disagree, the module is
correct — it is validated at import time and covered by the test suite.

## Why this matters

A migration tool that reads the wrong source produces confident, wrong
answers. Two failure modes concern us:

- **Missing an artifact entirely.** Dedicated SQL tables, views and stored
  procedures have no representation in Git. A repository-only discovery run
  reports zero of them and looks successful.
- **Reporting a definition that is not the one running.** A Git-integrated
  workspace can have unpublished changes; the committed pipeline and the live
  pipeline are not guaranteed to match.

## Three kinds of information

The same artifact draws on different sources for different things. Forcing
each artifact into a single source loses information.

| Class | Meaning | Example |
|---|---|---|
| **A. Definition** | The artifact itself | pipeline JSON, view DDL, notebook code |
| **B. Runtime** | Published state, live config, execution history | published vs. committed drift, trigger state, run history |
| **C. Infrastructure** | Azure resources, identity, networking | Spark pool SKU, managed identity, role assignments, private endpoints |

## Source roles

| Role | Meaning |
|---|---|
| **primary** | The authoritative definition comes from here |
| **secondary** | Authoritative for its own facet; genuine enrichment |
| **incidental** | May appear here, but never authoritative |
| **unavailable** | This source cannot supply this artifact at all |

## The matrix

| Artifact | Git | Synapse API | SQL | Azure | Primary Source | Secondary/Enrichment |
|----------|-----|-------------|-----|-------|----------------|----------------------|
| Dedicated SQL Tables | incidental | unavailable | **primary** | secondary | `sql` | `azure` |
| SQL Views | incidental | unavailable | **primary** | secondary | `sql` | `azure` |
| Stored Procedures | incidental | unavailable | **primary** | secondary | `sql` | `azure` |
| SQL Scripts | **primary** | secondary | secondary | incidental | `repository` | `synapse`, `sql` |
| Pipelines | **primary** | secondary | unavailable | secondary | `repository` | `synapse`, `azure` |
| Datasets | **primary** | secondary | secondary | secondary | `repository` | `synapse`, `sql`, `azure` |
| Linked Services | **primary** | secondary | unavailable | secondary | `repository` | `synapse`, `azure` |
| Notebooks | **primary** | secondary | unavailable | secondary | `repository` | `synapse`, `azure` |
| Spark Job Definitions | **primary** | secondary | unavailable | secondary | `repository` | `synapse`, `azure` |

**6 of 9 are repository-primary. 3 of 9 are SQL-primary. None is Synapse-API-primary or Azure-primary** — the Synapse REST API duplicates what Git already holds, and Azure supplies infrastructure rather than definitions.

## Reasoning, artifact by artifact

### 1. Dedicated SQL Tables — primary `sql`

The physical schema exists only inside the database. This is not a judgement
call; it is verifiable in the sample repository.

`TripsData`, `FaresData` and `AggregateTaxiData` are all *named* in the
snapshot — in `dataset/*.json` under `typeProperties.table`, and in the
pipeline's `preCopyScript`. But the Copy activity sinks use
`"tableOption": "autoCreate"`, which means the table is created at run time
from the source's inferred shape. **The column list, data types, distribution
method and index type are declared nowhere in Git.** A repository-only
discovery run would report three table names and nothing about their shape.

`sql` supplies: `sys.tables`/`sys.columns`/`sys.types`,
`sys.pdw_table_distribution_properties` (HASH / ROUND_ROBIN / REPLICATE),
`sys.pdw_column_distribution_properties` (the distribution column),
`sys.indexes` (clustered columnstore vs. heap), partitioning, statistics, and
row counts via `sys.dm_pdw_nodes_db_partition_stats`. Distribution and index
type are precisely the facts that determine migration effort into Fabric.

`azure` is secondary and infrastructure-only: pool SKU/DWU tier, pause state,
collation, geo-backup, firewall. It says nothing about the table.

`synapse` is **unavailable**. The Artifacts REST API serves workspace
artifacts; database objects are not workspace artifacts.

### 2. SQL Views — primary `sql`

`sys.views` joined to `sys.sql_modules.definition` returns the full
`CREATE VIEW` text; `sys.columns` gives the projected columns;
`sys.sql_expression_dependencies` gives the objects it reads.

Two caveats worth planning for:

- **Serverless views are a different connection.** Views over `OPENROWSET`
  and external tables live in the serverless endpoint's databases at
  `<workspace>-ondemand.sql.azuresynapse.net`, not the dedicated pool. This
  is the common Synapse pattern. Enumerating only the dedicated pool silently
  misses them.
- **`WITH ENCRYPTION` yields a NULL definition.** There is no way to recover
  the text; discovery must report the view as present-but-opaque rather than
  as absent.

Git is incidental: a `CREATE VIEW` may sit in a committed SQL script, but
nothing guarantees it matches what is deployed. The sample snapshot contains
no view DDL at all.

### 3. Stored Procedures — primary `sql`

Same mechanism: `sys.procedures` + `sys.sql_modules.definition` for the body,
`sys.parameters` for the signature, `sys.sql_expression_dependencies` for
static dependencies.

Git contributes procedure *names* — our `PipelineExtractor` already emits a
`SQL_OBJECT` reference for `typeProperties.storedProcedureName` on
`SqlPoolStoredProcedure` activities. That tells us a procedure is called; it
says nothing about what it does.

Limits: encrypted procedures, and anything reached through dynamic SQL
assembled at runtime, cannot be resolved statically from either source.

### 4. SQL Scripts — primary `repository`

Distinct from the three above: a SQL script is a *workspace artifact*
(`sqlscript/*.json`), serialized to Git like a pipeline. `properties.content.query`
holds the T-SQL verbatim and `currentConnection` names the target pool.

`synapse` is secondary for the published version and drift detection.
`sql` is secondary for a different reason: resolving whether the objects the
script references actually exist and in what shape. The script text and the
objects it touches are two different discoveries.

**Gap:** the sample snapshot has no `sqlscript/` folder, so this path has
never run against real data.

### 5. Pipelines — primary `repository`

Fully defined in Git for a Git-integrated workspace. Already implemented;
`PipelineExtractor` produces activities, nesting, `dependsOn`, parameters,
references, embedded SQL and expressions from the JSON alone.

`synapse` is secondary and genuinely useful: the published definition (for
drift against the commit), trigger state (started/stopped — the trigger
*artifact* is in Git, but whether it is currently enabled is not), and run
history.

`azure` is secondary for infrastructure: integration runtime type — Azure vs.
self-hosted is a major migration factor and the Git JSON only names the IR.

`sql` is **unavailable** for the pipeline itself. It is still needed to
resolve the objects the pipeline's `preCopyScript` and `sqlReaderQuery`
touch, but that is discovery of *those objects*, not of the pipeline.

### 6. Datasets — primary `repository`

Fully defined in Git. `sql` is a real secondary source here, and the
distinction is already built into `DatasetExtractor`: `properties.schema` is
the author's *declared* column list, which may be empty, stale, or simply
wrong. Seven of the nine datasets in the sample declare `schema: []`. The
physical schema of the table behind them can only come from `sql`.

`azure` is secondary for the storage account behind a file dataset — whether
it exists, and its firewall and private endpoint posture.

### 7. Linked Services — primary `repository`, with `azure` mattering most

The configuration is in Git: type, target endpoint, authentication method,
and Key Vault references (linked service name plus secret *name*). Synapse
never writes plaintext secrets to Git.

This is the artifact where infrastructure metadata carries the most weight. A
linked service can parse perfectly and still be non-functional because a role
assignment is missing. Only `azure` can tell us:

- the workspace managed identity's type and principal id
- role assignments granting that identity access to the target resource
- whether the referenced Key Vault exists and how access is granted
- whether the target resource still exists
- private endpoint and firewall posture

None of that is knowable from the artifact definition.

### 8. Notebooks — primary `repository`

Code, declared language, attached pool reference and session sizing are all
in Git, and `NotebookExtractor` already extracts them.

`azure` is secondary for a reason that will bite during migration: **libraries
installed on the Spark pool are not in the repository.** A notebook can
`import` a package that nothing in Git declares, because it was installed at
pool scope via `requirements.txt` or workspace packages. The Spark version,
node size, auto-scale and auto-pause settings are likewise ARM properties.

The sample notebook's `bigDataPool` reference points at `ws1sparkpool1` — an
ARM resource, not a repository artifact, which is why the extractor already
records it with `target_type=None, kind=COMPUTE`.

### 9. Spark Job Definitions — primary `repository`

The definition (`sparkJobDefinition/*.json`) gives target pool, main file
path, main class, arguments and sizing.

The important limit: **the code it runs is not in the definition.** The main
file is a JAR or `.py` in ADLS. Discovering what the job actually does means
reading that file from storage — an `azure` concern, and a separate decision,
because the file is code rather than metadata.

**Gap:** the sample snapshot has no `sparkJobDefinition/` folder.

## Discovery flow

```
Repository (local clone)
    -> Git artifact discovery      -> Artifact extraction      -> DEFINITION
Synapse API (dev.azuresynapse.net)
    -> Live/published metadata     -> Enrichment               -> RUNTIME
SQL endpoint (dedicated + serverless)
    -> SQL object/schema metadata  -> Enrichment/primary       -> DEFINITION
Azure ARM (management.azure.com)
    -> Infrastructure metadata     -> Enrichment               -> INFRASTRUCTURE
                                             |
                                             v
                                 Unified Discovery Record
                                             |
                                             v
                                    Dependency Graph
```

Repository discovery runs first and alone. It is the only stage that works
offline, it needs no credentials, and it already produces a complete
inventory for six of the nine artifacts. Everything else enriches a record
that already exists — except the SQL stage, which *creates* records for the
three database object types that Git never contained.

## Connections required

| Source | Endpoint | Protocol | Credential |
|---|---|---|---|
| `repository` | local clone from acquisition | filesystem | none |
| `synapse` | `https://<workspace>.dev.azuresynapse.net` | HTTPS (Artifacts data plane) | Entra ID token, audience `https://dev.azuresynapse.net` |
| `sql` | `<workspace>.sql.azuresynapse.net:1433` and `<workspace>-ondemand.sql.azuresynapse.net:1433` | TDS | Entra ID token or SQL login, read access to catalog views |
| `azure` | `https://management.azure.com` | HTTPS (ARM) | Entra ID token, Reader on the resource group |

Note that `sql` is two endpoints, not one. Discovery configuration must allow
enumerating both, or serverless views and external tables are silently
missed.

## Security requirement

Discovery **never retrieves**, from any source, under any configuration:

- secret values
- passwords
- storage account access keys
- SAS tokens
- access tokens and refresh tokens
- client secrets
- certificate private keys
- connection strings with embedded credentials

This is a *never request* rule, not a *redact afterwards* rule. In particular,
discovery reads Key Vault **metadata** — that a vault exists, how access is
granted — and never calls a secret-retrieval operation.

Discovery **may record** this safe metadata, which describes how
authentication is arranged rather than what it is:

- authentication type (managed identity, service principal, SQL login, key)
- Key Vault linked service name and secret *name*
- linked service name and type
- managed identity type and principal id
- endpoint hostnames and resource ids
- role assignment names and scopes
- network posture (private endpoint present, firewall enabled)

These rules are encoded as `NEVER_RETRIEVE` and `SAFE_TO_RECORD` in
`source_strategy.py`. The existing extractors already implement the principle:
`DatasetExtractor` records a Key Vault reference as
`SecretReference(kind=KEY_VAULT, secret_name=...)` and drops the value;
`NotebookExtractor` redacts credential literals from finding evidence.

## Known gaps and uncertainties

1. **Three P0 artifacts are unexercised.** SQL scripts, Spark job definitions
   and all three SQL object types have zero instances in the only repository
   we have. The detector and pipeline extractor support the first two, but no
   real data has ever passed through them.
2. **Git-integration is assumed.** If a workspace is *not* Git-connected, the
   repository source does not exist and all six repository-primary artifacts
   fall back to `synapse`. The strategy would invert. This is worth detecting
   explicitly rather than assuming.
3. **Published-vs-committed drift is unmeasured.** We assert Git is
   authoritative for definitions; that holds for a workspace whose changes are
   all committed. Quantifying drift needs the `synapse` source, which does not
   exist yet.
4. **Serverless vs. dedicated enumeration.** Getting this wrong loses a whole
   class of views. It needs explicit configuration, not inference.
5. **Encrypted modules and dynamic SQL** are irreducible limits, not
   implementation gaps. Discovery should report them as opaque rather than
   absent.
6. **Lake databases** (Spark-created tables in the workspace's lake database)
   are a fourth SQL-ish surface, out of P0 scope but adjacent to it. Worth
   confirming they are genuinely excluded rather than forgotten.
