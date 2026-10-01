# Discovery

How the Discovery Agent finds the nine P0 artifacts, which source it believes
for each one, and what it does when a source is not there.

This document describes what is implemented. Where something is not
implemented, or cannot be known, it says so.

---

## 1. The nine P0 artifacts

| # | Artifact | Primary source | Also observed in | Why |
|---|----------|----------------|------------------|-----|
| 1 | Dedicated SQL table | SQL catalog | — | A physical table exists only inside the pool. Git may *name* one; it never defines its shape. |
| 2 | SQL view | SQL catalog | — | The body lives in `sys.sql_modules`. There is no Git file and no Artifacts route. |
| 3 | Stored procedure | SQL catalog | — | As above. |
| 4 | SQL script | Git | Synapse | A `sqlscript/*.json` artifact. Distinct from a view: a script is authored, a view is a database object. |
| 5 | Pipeline | Git | Synapse | Committed JSON is the definition; the workspace holds what is published. |
| 6 | Dataset | Git | Synapse | As above. |
| 7 | Linked service | Git | Synapse | As above. The workspace also holds ones Git never had. |
| 8 | Notebook | Git | Synapse | As above. |
| 9 | Spark job definition | Git | Synapse | As above. Not a notebook: it carries no code, only a pointer to an application file. |

The full per-facet strategy — what each source can and cannot supply for each
artifact — is data, in `discovery_agent.source_strategy`, and is documented in
[p0_source_strategy.md](p0_source_strategy.md).

**Git is not the complete source of truth.** It holds definitions. It holds no
physical schema, no distribution policy, no published state and no runtime
configuration, and nothing in this agent manufactures those from a committed
file.

---

## 2. The three sources

```
ConnectionManager
├── AzureConnection              subscription + the run's one identity
├── SynapseWorkspaceConnection   the workspace, over ARM (management plane)
├── SynapseArtifactsConnection   the workspace's artifacts (data plane)
├── SqlConnection                the dedicated pool, over TDS
└── GitConnection                the repository, over the user's own git
```

### Git

`GitConnection` shells out to the machine's own `git`. Authentication is
whatever the credential helper, Git Credential Manager or SSH key already
does. The tool never accepts, stores or transmits a PAT, password or token,
and a repository URL containing credentials is refused at construction.

### Synapse Artifacts (data plane)

`GET {workspace}.dev.azuresynapse.net/<route>?api-version=2020-12-01`, with an
Entra token for the `https://dev.azuresynapse.net/.default` audience. Six
routes are registered and no others can be requested:

| Artifact | Route |
|---|---|
| Pipeline | `pipelines` |
| Dataset | `datasets` |
| Linked service | `linkedservices` *(lowercase, as the service spells it)* |
| Notebook | `notebooks` |
| SQL script | `sqlScripts` |
| Spark job definition | `sparkJobDefinitions` |

Listings return complete definitions and are paged by following the service's
own `nextLink`. The response shape is `{id, name, type, properties, etag}` —
the same `name` + `properties` document a Git-integrated repository stores,
which is why the **same extractors read both sources unchanged**.

Data-plane authorization is separate from management-plane authorization.
Reader on the resource group lets an identity see the workspace in ARM and
grants nothing on the data plane; reading artifacts needs a role such as
**Synapse Artifact User**. The two are validated separately so an operator is
told which one is missing.

Dedicated SQL tables, views and stored procedures have **no** Artifacts route.
That is recorded explicitly in `synapse.api.NO_ARTIFACTS_API` rather than left
as an absence, and asking for one names the SQL catalog as the source instead.

### SQL catalog

`DedicatedPoolSource` over ODBC Driver 18 with an Entra token. Discovery runs
only the fixed, named, SELECT-only queries in `discovery_agent.sql.queries` —
there is no parameter through which a caller could pass SQL text.

Implemented: database identity, connected principal, tables, columns,
distribution policy and columns, external-table status, index and base storage
structure, views, stored procedures, module bodies and procedure parameters.

Not implemented, deliberately: row counts, storage size, partitions,
constraints and statistics.

---

## 3. The flow

```
                    ┌─ GitConnection.acquire() ─→ walk ─→ detect ─┐
ConnectionManager ──┼─ SynapseArtifactsConnection.discover() ─────┼─→ ExtractorOrchestrator
                    └─ SqlConnection.source().discover() ─────────┘        │
                                                                          ▼
                                                              identity resolution
                                                                          │
                                                                          ▼
                                                            UnifiedDiscoveryRecord set
```

`discovery.run(config, connections)` is the single composition seam. It
authenticates nothing, opens nothing, and reaches no network except through a
connection it was handed.

The repository leg and the live leg run the **same** `ExtractorOrchestrator`
over the **same** `default_registry()`. A pipeline read from the API is parsed
by `PipelineExtractor`, not by a second implementation that could disagree
with it.

---

## 4. Identity across sources

A pipeline named `Load` in a clone and a pipeline named `Load` in a workspace
are the same pipeline **only if that workspace is Git-integrated with that
repository**. Matching names prove nothing on their own.

ARM answers this authoritatively: a workspace reports its
`workspaceRepositoryConfiguration`, and the agent compares the repository it
names with the one that was actually acquired, using the same canonicalisation
that decides whether an existing clone may be reused.

| Resolution | Meaning | Merging |
|---|---|---|
| `proven` | The workspace names the repository that was scanned. | Yes |
| `different_repository` | The workspace names a different repository. | No |
| `undetermined` | No Git configuration, or ARM could not be read. | No |
| `not_applicable` | Only one source ran. | N/A |

`different_repository` and `undetermined` are kept apart because they mean
different things: one is a positive answer that the two are unrelated, the
other is no answer at all.

When identity is **not** proven, both observations are kept as two records.
Each states that the other source holds a same-named artifact, that they may
be the same, and that this was not established. The live record is qualified
by workspace so the two keep distinct ids.

Identities carry a `SourceKey` per source — repository path, workspace route,
or `database.schema.object` — so a later reconciler has the raw material.

---

## 5. Drift

Computed only between definitions identity has **proven** are the same
artifact, and only from a canonical, normalised rendering.

| Status | Meaning |
|---|---|
| `in_sync` | The two sources declare the same artifact. |
| `drifted` | They declare different things. `drift_paths` names the top-level properties. |
| `not_published` | Committed, but the workspace has no such artifact. |
| `not_in_source_control` | Published, but the repository has no such file. |
| `unknown` | Not compared. Never means "they match". |

**Normalisation** (`synapse.models.normalize_definition`) exists because the
API and the repository do not write the same JSON for the same artifact. The
API materialises defaults that Git omits. Against the real POC workspace,
before normalisation, four untouched artifacts reported as drifted purely
because of:

```
lastPublishTime: "..."            service metadata
policy: {"elapsedTimeMetric": {}} service-added default
typeProperties: {}                empty object where Git has no key
parameters: {}                    inside an activity input
outputs: []                       inside a notebook cell
metadata: null                    explicit null where Git has no key
```

Three rules, applied symmetrically to both sides: drop service-managed keys;
drop a key whose value is null; drop a key whose value is an empty object or
array after normalising.

What normalisation does **not** do: reorder or drop list items. Array order is
meaning — reordering a pipeline's activities changes the pipeline — so an
emptied or reordered activity list still reports as drifted.

This is canonicalisation, not a semantic diff. It knows nothing about what an
activity or a cell means.

---

## 6. Degradation

Every source degrades on its own. A source failing never becomes a count of
zero, and never ends the run unless it was the only source asked for.

| Situation | Behaviour |
|---|---|
| Git unreachable, workspace configured | Repository leg skipped, issue recorded, workspace and SQL still discovered. |
| Git unreachable, Git the only source | Fails. There is nothing left to report. |
| Workspace not configured | `run.synapse is None`. Distinct from an empty workspace. |
| One Artifacts route returns 403 | That artifact type is marked **unreachable**, not empty. It is absent from `counts_by_artifact()` entirely, so it cannot contribute a zero. |
| One Artifacts route returns 404 | `UNSUPPORTED_CONSTRUCT`: the workspace does not serve it at this API version. |
| Pool paused or refusing login | `catalog is None` plus an issue. Repository and workspace legs unaffected. |
| `sys.sql_modules` not granted | Every view and procedure is still discovered, each carrying `MISSING_INFORMATION`. |
| A module body is NULL | `OPAQUE_DEFINITION`. The body exists and was withheld — encrypted, or `VIEW DEFINITION` not granted. **Never** reported as "no definition". |
| `sys.parameters` not granted | Procedures discovered; signature explicitly unknown, never "takes no parameters". |
| An enumeration query fails | Raises. An empty list would assert the database has none. |

The vocabulary is the existing `IssueCode`: `SOURCE_UNAVAILABLE`,
`OPAQUE_DEFINITION`, `MISSING_INFORMATION`, `UNSUPPORTED_CONSTRUCT`,
`MALFORMED_ARTIFACT`, `DRIFT_DETECTED`.

---

## 7. Provenance

Every facet of every record names the source that supplied it and carries its
own `ExtractionProvenance`:

| Source | Carries |
|---|---|
| Repository | path, file sha256, repository URL, ref, **real commit SHA** |
| Synapse | route, definition sha256, the service's own resource id |
| SQL | the fully qualified `server/database/schema/object` |

A merged record therefore states "definition from Git at commit `abc`, runtime
from workspace `ws`" rather than a single provenance for the whole record.

Two hashes exist and are not the same thing. `content_hash` identifies the
bytes a source served and belongs in provenance. `comparison_hash` is the
normalised one two sources are compared on, and is recorded on the runtime
facet as `committed_comparison_sha256` / `published_comparison_sha256` so a
drift verdict can be rechecked.

---

## 8. Security

No secret value reaches a record, a summary, a log line, the CLI, the JSON
output, an exception or a test snapshot.

This is structural, not a redaction step:

* `ConnectionSettings` has no password, token or secret field.
* A repository URL with embedded credentials is refused at construction, and
  the refusal message never quotes it.
* `SecretReference` records that a secret is referenced, and which vault and
  secret *name*. It has no field a value could occupy.
* `AuthenticationMetadata` records the mechanism. Same.
* `AccessToken` overrides `__repr__`/`__str__`; reaching the value takes a
  deliberate attribute access.
* The SQL and Artifacts clients hold a token *provider*, never a token.

The one place this had to be enforced rather than inherited is config
rendering: a Spark job's `conf` may hold a `SecureString`, and a renderer that
JSON-encodes a non-scalar would copy the value out. Scalars only; a
secret-named or structured value keeps its key and loses its value.

---

## 9. What is not implemented

* **Records are not persisted.** `DiscoveryRun.records` is returned and
  summarised; `writers/` is still empty and `--out` is carried but unused.
* **References are not resolved.** Every `ArtifactReference` leaves with
  `resolved=False`, by design — the dependency-graph stage needs them that way.
* **No infrastructure facet.** Spark pools, integration runtimes and storage
  accounts are *observed* as references but no ARM resource is read for them.
* **Non-P0 artifact types** — triggers, data flows, KQL scripts, integration
  runtimes, credentials, managed private endpoints and managed virtual
  networks — are detected in the repository and are not extracted, not listed
  from the data plane, and not turned into records.
* **Serverless SQL** is out of scope; only the dedicated pool endpoint is read.
* **No Fabric.** It is a migration target; nothing here maps to one.

### Known characteristics of the drift comparison

An explicit `false` on one side and an absent key on the other is reported as
drift. It is a difference in what was declared, and deciding that a particular
boolean's default makes them equivalent would require a schema — which is the
semantic diff engine this stage deliberately does not build. Observed live:
`metadata.a365ComputeOptions.automaticScaleJobs` and
`sessionProperties.runAsWorkspaceSystemIdentity` on a notebook.

---

## 10. Running it

```bash
# A clone you already have. No credentials, no network, no Azure.
python -m discovery_agent --source input/repository/<repo>

# Acquire and scan. Git authenticates with your own credential helper.
python -m discovery_agent --repository-url https://github.com/<owner>/<repo> --ref main

# The whole estate.
python -m discovery_agent \
  --repository-url https://github.com/<owner>/<repo> \
  --subscription <id> \
  --resource-group <rg> \
  --workspace <workspace> \
  --sql-pool <pool>

# The live workspace alone, with no repository at all.
python -m discovery_agent --workspace <workspace> --resource-group <rg>
```

`--subscription` is optional. Without it the data plane and the pool are still
read; what is lost is the ARM workspace metadata, and with it the proof that a
repository belongs to a workspace — so identity resolution reports
`undetermined` and nothing is merged.

You never list artifacts. There is no `--pipeline`, `--notebook` or `--table`
flag, because enumerating them is what discovery is for. There is no
`--token`, `--password` or `--pat` flag either.
