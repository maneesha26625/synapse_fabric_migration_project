# Unified Discovery Record

One logical artifact, assembled from whichever sources were reachable, with
every fact traceable to the source that supplied it.

Implemented in `src/discovery_agent/discovery_models.py`. Read
`docs/p0_source_strategy.md` first — this model exists to express what that
strategy establishes.

## 1. Why it is needed

The source strategy established that our nine P0 artifacts draw on four
sources with very different reach: six are repository-primary, three are
SQL-primary, and Synapse and Azure supply runtime and infrastructure facts
that no definition contains.

Without a unifying record, every downstream stage inherits that complexity:

- **The dependency graph** would have to know that a pipeline's references
  come from Git but a view's come from `sys.sql_expression_dependencies`.
- **Assessment** would have to know that a notebook's libraries might be in
  the code *or* installed on a Spark pool it has to look up separately.
- **Migration workers** would have to re-derive which source was authoritative
  for each fact they act on.

There is also a correctness problem. A repository-only run is a legitimate,
complete run — it needs no credentials and works offline — but the records it
produces are missing runtime and infrastructure information. If that absence
is a silent `null`, a later stage cannot tell "we looked and there was
nothing" from "we never looked". The record makes that distinction explicit.

## 2. Model and facet structure

```
UnifiedDiscoveryRecord[T]
├── identity : ArtifactIdentity          (always present)
├── definition     : DefinitionFacet[T]  | None
├── runtime        : RuntimeFacet        | None
├── infrastructure : InfrastructureFacet | None
├── dependencies   : DependencyFacet     | None
├── security       : SecurityFacet       | None
└── issues         : Tuple[RecordIssue]
```

Every facet inherits from `Facet`, which carries three things:

```python
source     : SourceType              # who supplied this facet
provenance : ExtractionProvenance    # exactly where it came from
issues     : Tuple[ExtractionIssue]  # what went wrong collecting it
```

**Provenance and issues are not standalone facets.** The brief sketched a
`ProvenanceFacet` and an `IssueFacet`, but a single provenance per record
could not answer "which source supplied which part" — and that is the whole
point. So provenance is a property *of* each facet, and issues are collected
per facet and aggregated by the record with their facet and source attached.

### The facets

| Facet | What it holds | Notes |
|---|---|---|
| `DefinitionFacet[T]` | `content` (the extractor's typed model), `status`, `content_type`, `extractor` + version | Generic over the definition type, mirroring `ExtractionResult[T]` |
| `RuntimeFacet` | `published`, `state`, `drift`, `drift_paths`, `observations` | Drift is `DriftStatus`: unknown / in_sync / drifted / not_published / not_in_source_control |
| `InfrastructureFacet` | `resources: Tuple[AzureResource]`, `observations` | Each resource has an ARM id, type, and an `InfrastructureRole` (host / compute / storage / network / identity / secret_store) |
| `DependencyFacet` | `references: Tuple[ArtifactReference]` | Rejects any reference with `resolved=True` |
| `SecurityFacet` | `authentication`, `secret_references` | Both member types are structurally incapable of holding a value |

### Identity

Identity is deliberately separate from content, because a future
reconciliation component has to match the same logical artifact across
sources before any facet can be merged.

```python
ArtifactIdentity(artifact, name, workspace, database, schema, source_keys)
```

`logical_id` is **compatible with `models.asset_id()`** for the six
repository artifacts:

```
synapse://pipeline/TripFaresDataPipeline
```

That is not cosmetic. `ArtifactReference.target_id`, which the extractors
already emit, uses exactly this scheme — so a reference joins straight onto a
record with no translation table. Verified against the real repository: 11 of
12 reference target ids join onto a record, and the one that does not is
`synapse://dataflow/tripFaresDataTransformations`, correctly absent because
data flows are not in P0.

SQL objects have no `AssetType`, so they get their own scheme:

```
sql://poolone/dbo/TripsData
```

`scoped_id` adds the workspace (`synapse://poc-ws/pipeline/...`) and is the
migration path for multi-workspace runs. It is not used for joining today,
because extractors emit unscoped ids.

`source_keys` is the raw material for reconciliation — how each source names
this artifact:

| Source | `SourceKeyKind` | Example |
|---|---|---|
| repository | `repository_path` | `synapsepoc/pipeline/TripFaresDataPipeline.json` |
| synapse | `synapse_artifact_name` | `TripFaresDataPipeline` |
| sql | `sql_object_name` | `poolone.dbo.TripsData` |
| azure | `azure_resource_id` | `/subscriptions/.../bigDataPools/ws1sparkpool1` |

**No matching is implemented.** The model stores the keys; a future
identity-resolution component compares them.

## 3. Which source supplies each facet

| Artifact | Definition | Runtime | Infrastructure | Dependencies | Security |
|---|---|---|---|---|---|
| Pipeline | repository | synapse | azure | repository | — |
| Dataset | repository | synapse | azure | repository | — |
| Linked Service | repository | synapse | **azure** | repository | repository + azure |
| Notebook | repository | synapse | azure | repository | — |
| Spark Job Definition | repository | synapse | azure | repository | — |
| SQL Script | repository | synapse | azure | repository + sql | — |
| Dedicated SQL Table | **sql** | **sql** | azure | sql | — |
| SQL View | **sql** | sql | azure | sql | — |
| Stored Procedure | **sql** | sql | azure | sql | — |

Two things worth noting. For a SQL table the *runtime* facet also comes from
`sql`, not Synapse — row counts and statistics are runtime facts about a
table, and only the database has them. And for a linked service the
infrastructure facet carries unusual weight: a linked service that parses
perfectly can still be non-functional because a role assignment is missing,
and only Azure can say so.

## 4. Examples

### Pipeline — repository + Synapse + Azure

```python
UnifiedDiscoveryRecord(
    identity = ArtifactIdentity(PIPELINE, "TripFaresDataPipeline",
                                source_keys=(repo_path, synapse_name)),
    definition = DefinitionFacet(source=REPOSITORY,
                                 provenance=<commit b7c1f0e, synapsepoc/pipeline/...>,
                                 content=PipelineDefinition(6 activities)),
    runtime = RuntimeFacet(source=SYNAPSE, published=True,
                           drift=DRIFTED,
                           drift_paths=("properties.activities[0].policy.timeout",)),
    infrastructure = InfrastructureFacet(source=AZURE,
                           resources=(AzureResource(AutoResolveIR, role=COMPUTE),)),
    dependencies = DependencyFacet(source=REPOSITORY, references=(12 unresolved,)),
)
```

### Notebook

Definition from Git (cells, kernel, session config). Runtime from Synapse.
Infrastructure from Azure — and this is where the Spark pool's
**installed libraries** appear, which are not in the repository at all. A
notebook can import a package nothing in Git declares.

### Dataset

Definition from Git. The `sql` facet matters here for a reason already built
into `DatasetExtractor`: `properties.schema` is the author's *declared*
column list — seven of the nine datasets in the sample declare `schema: []` —
while the physical table schema can only come from SQL.

### Linked Service

Definition from Git: type, endpoint, authentication method, Key Vault
reference. Security facet from both Git (declared mechanism) and Azure
(managed identity principal id, role assignments, vault existence). No secret
value from anywhere.

### Dedicated SQL Table

```python
UnifiedDiscoveryRecord(
    identity = ArtifactIdentity(DEDICATED_SQL_TABLE, "TripsData",
                                database="poolone", schema="dbo"),
    definition = DefinitionFacet(source=SQL, content=TableDefinition(...)),
    runtime    = RuntimeFacet(source=SQL,
                              observations=(ConfigEntry("row_count", "1048576"),)),
    infrastructure = InfrastructureFacet(source=AZURE,
                              resources=(AzureResource(sqlPool, role=COMPUTE),)),
)
```

No repository facet at all — and that is correct. In the sample snapshot
`TripsData` is *named* by a dataset and by a pipeline's `preCopyScript`, but
the Copy sink uses `tableOption: autoCreate`, so its columns, types and
distribution exist nowhere in Git.

## 5. How provenance is preserved

Each facet holds the existing `ExtractionProvenance`, unchanged. Nothing is
re-derived:

| Source | What provenance carries |
|---|---|
| repository | `repository_url`, `ref`, `commit_sha`, `source_path`, `sha256`, `source_format` |
| synapse | `resource_id` (the workspace artifact path) |
| sql | `resource_id` (the three-part object name) |
| azure | `resource_id` (the ARM id) |

`record.provenance_by_facet()` answers "where did each part come from" in one
call:

```python
{"definition":     {"source_type": "repository", "commit_sha": "b7c1...", ...},
 "runtime":        {"source_type": "synapse",    "resource_id": "/workspaces/..."},
 "infrastructure": {"source_type": "azure",      "resource_id": "/subscriptions/..."}}
```

## 6. Missing and partial information

Three distinct states, kept distinct:

| State | How it is represented |
|---|---|
| Never looked | facet is `None` + `missing_facet(...)` issue with code `MISSING_INFORMATION` |
| Looked, source unreachable | facet is `None` + `source_unavailable(...)` issue with code `SOURCE_UNAVAILABLE` |
| Looked, found, unreadable | facet **present**, status `PARTIAL`, issue with code `OPAQUE_DEFINITION` |

The third is the encrypted stored procedure case: the procedure exists, we
know its name and signature, and `sys.sql_modules.definition` is NULL. It
must not be reported as absent.

Two invariants are enforced at construction rather than by convention:

- A `DefinitionFacet` with status `FAILED` cannot carry content. The same
  rule `ExtractionResult` already enforces — an empty model must never be
  mistakable for an artifact that really is empty.
- A `DependencyFacet` cannot contain a reference with `resolved=True`.

A record with only a definition facet is complete for what it claims to be.
`is_definition_only` reports it; it is not an error.

## 7. How dependency resolution will consume this

The `DependencyFacet` holds `ArtifactReference` values exactly as the
extractors emit them, with `resolved=False`. The graph stage will:

1. Build an index of `logical_id` → record from the `DiscoveryRecordSet`.
2. For each reference, take `target_id` (already in the same id scheme).
3. Join. A hit is an edge; a miss is a dangling reference worth reporting.
4. References whose `target_type is None` — a storage path, a REST endpoint,
   a SQL object name from a `SqlPoolStoredProcedure` activity — have no
   `target_id` and never join. They are real references to things that are
   not repository artifacts.

`DiscoveryRecordSet.all_references` hands the graph stage every reference in
one call, and `by_id` does the lookup. Nothing in this module performs step 3.

Against the real repository today: 22 references across 17 records, 12 with a
`target_id`, 11 of which join.

## 8. How assessment and migration will consume this

**Assessment** reads the record and asks questions the record can answer
without knowing about sources:

- `record.content` — the typed definition, whatever source produced it
- `record.sources` — what evidence this verdict rests on
- `record.all_issues` — what discovery could not establish, so an assessment
  based on partial information says so
- `record.has_usable_definition` — whether there is anything to assess at all

Crucially, a record from a repository-only run and one from a fully enriched
run have the same shape. Assessment does not branch on which sources were
available; it reads `issues` and qualifies its confidence.

**Migration workers** get `provenance_by_facet()` to answer "where did this
come from" for any fact they act on, and `identity.source_keys` to locate the
artifact in its original source when they need to write back.

## 9. Security

The `SecurityFacet` holds two member types, and the guarantee is **structural,
not procedural**:

- `SecretReference(kind, location, secret_name, store_name, property_name)`
- `AuthenticationMetadata(authentication_type, location, identity_name, target)`

Neither has a field a secret value could occupy. There is no redaction step
to forget, because there is nowhere to put a secret in the first place. Tests
assert the absence of any `value` field.

Safe to record: authentication mechanism, Key Vault linked service name and
secret *name*, identity type and principal id, endpoint hostnames, role
assignment scopes. Never recorded: secret values, passwords, access keys, SAS
tokens, client secrets. See `source_strategy.NEVER_RETRIEVE`.

## 10. Design decisions and open questions

**Decisions**

1. **One facet per kind, not a list of facets.** A pipeline could in principle
   have a definition facet from Git *and* one from Synapse. Instead, drift is
   represented inside `RuntimeFacet` (`drift`, `drift_paths`). Simpler, and it
   keeps "which definition is authoritative" a settled question rather than a
   per-record one. If comparing two full definitions becomes necessary, the
   `RuntimeFacet` gains a field rather than the record gaining a shape.
2. **`logical_id` deliberately omits the workspace** so it matches the
   extractor-emitted ids that references already carry. `scoped_id` exists for
   when multi-workspace runs arrive.
3. **Non-P0 artifacts produce no record.** `from_extraction_result` returns
   `None` for a trigger or data flow. Inventing records for them would
   misrepresent the frozen scope.
4. **A skipped extraction still produces a record.** The detector found the
   artifact, so it exists; only the definition is missing, and the skip reason
   is in the facet's issues. This is what "absence is stated" means in
   practice — the six linked services in the sample produce records saying
   "found, no extractor yet".

**Open questions**

1. **Identity resolution across sources is unimplemented and non-trivial.**
   Name matching is the obvious approach and will mostly work, but Synapse
   artifact names are case-sensitive in some APIs and not others, and SQL
   object names are subject to collation. Deferred deliberately.
2. **Drift detection semantics are undefined.** `DriftStatus` and
   `drift_paths` exist, but what counts as a meaningful difference — does a
   reordered JSON key count? — needs the Synapse source before it can be
   settled.
3. **Multi-database SQL scope.** A workspace can have several dedicated pools
   plus serverless. `database` is part of the identity, so records will not
   collide, but enumerating which databases to visit is a configuration
   question the model does not answer.
4. **Facet-level freshness.** Facets from different sources are collected at
   different moments. There is no timestamp today, on purpose — timestamps
   break determinism. If freshness becomes important it belongs in the run
   manifest, not the record.
