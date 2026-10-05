# Discovery Agent

Read-only discovery for an Azure Synapse estate. It enumerates the nine P0
artifacts from whichever sources are reachable — the Git-integrated
repository, the live workspace, and the dedicated SQL pool — and reports what
each source said, together with what could not be established.

**Scope**

* Read-only. Every Azure verb is a GET; every SQL statement is one of a fixed
  set of named SELECT queries.
* Deterministic. The same commit and the same workspace state produce the same
  output.
* Source-aware. Git holds definitions; it holds no physical schema, no
  published state and no runtime configuration, and nothing here manufactures
  those from a committed file.
* No LLM. Structural facts are extracted by parsers, not by a model.
* No secrets. Not redacted — structurally unable to be carried.

Assessment (complexity scoring, effort, migration planning) and migration
itself are separate components and deliberately not part of this agent.

## Pipeline

```
                    ┌─ Git acquisition ──→ walk ──→ detect ──┐
ConnectionManager ──┼─ Synapse Artifacts API ────────────────┼─→ extractors
                    └─ Dedicated SQL pool catalog ───────────┘       │
                                                                     ▼
                                                        identity resolution
                                                                     │
                                                                     ▼
                                                    Unified Discovery Records
```

All three legs are implemented. `writers/` is not: records are returned and
summarised, not persisted. See **[docs/discovery.md](docs/discovery.md)** for
the source-of-truth strategy, degradation behaviour, provenance and the full
list of limitations.

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

For the live connection chain (Azure, Synapse, dedicated SQL pool) add the
`live` extra, and sign in with the Azure CLI:

```
pip install -e ".[dev,live]"
az login
```

## Web UI

`frontend/` holds the Connections and Discovery UI, and `python -m
discovery_agent.api` is the small local API it talks to. The API adds no
authentication path and no discovery logic: it calls `ConnectionManager` and
`discovery.run`. See **[frontend/README.md](frontend/README.md)**.

## Repository acquisition

Clones the source repository into `input/repository/<repository-name>/` and
returns the snapshot's identity, including the exact checked-out commit SHA:

```
python -m discovery_agent.acquisition --repository-url https://github.com/<owner>/<repo> --ref main
```

```json
{
  "commit_sha": "b7c1f0e2a9d4...",
  "local_path": "C:\\...\\input\\repository\\<repo>",
  "provider": "github",
  "ref": "main",
  "repository_url": "https://github.com/<owner>/<repo>",
  "reused": false
}
```

`--ref` accepts a branch, tag, or commit SHA, and defaults to the repository's
default branch. `--input-root` overrides the clone location.

From Python:

```python
from discovery_agent.acquisition import acquire_repository

source = acquire_repository(
    repository_url="https://github.com/<owner>/<repo>",
    ref="main",
)
```

**Snapshot stability.** If `input/repository/<repository-name>/` already
exists, it is reused as-is and nothing is fetched, pulled, or checked out — the
snapshot cannot shift under a run in progress. The existing clone is verified
first: its `origin` must be the same repository, and the requested ref must be
the commit that is checked out. If either check fails, the run stops and tells
you to delete the directory to re-clone. Cloned repositories are gitignored via
`input/.gitignore`.

**Credentials.** The tool shells out to your installed `git`, so
authentication is whatever your git configuration already does — a credential
helper, an SSH key, or Git Credential Manager. It never accepts, reads, or
stores a token, and a repository URL containing embedded credentials is
rejected. This command goes through `GitConnection`; see
[Connection Architecture](#connection-architecture).

**Providers.** GitHub, Azure DevOps, GitLab, Bitbucket and any self-hosted
git host, over HTTPS or SSH. See
[Git: providers, transports and credentials](#git-providers-transports-and-credentials).

## Connection Architecture

> **Developer rule.** Do not create a new Azure, Synapse, SQL, or Git
> authentication/connection path. Use `ConnectionManager` and the existing
> connection abstractions. This applies to discovery code, extractors, agents,
> scripts and tests alike. If you find yourself importing `azure.identity`,
> calling `pyodbc.connect`, or shelling out to `git`, you are in the wrong
> layer — ask the connection layer for a connection instead.

All external connectivity and authentication lives in **one** package:
`src/discovery_agent/connections/`. Three tests enforce that rather than
trusting it: no module outside `connections/` may touch `azure.identity`, only
`sql/connection.py` may call the ODBC driver, and only `acquisition/git.py` may
run a git subprocess.

### The layers

```
                        CONNECTIONS
                            |
        +-------------------+-------------------+
        v                   v                   v
      Azure                Git               Synapse
        |                   |                   |
        |                   |          +--------+--------+
        |                   |          v                 v
        |                   |    Artifacts API          SQL
        |                   |     (data plane)     (dedicated pool)
        v                   v          v                 v
              Discovery / Sources / Extractors
                            v
                   Unified Discovery Records
```

Connection code may depend on low-level libraries. Discovery code depends on
connection abstractions. Extractors consume already-connected sources — they
never authenticate, never create a credential, never open a SQL connection and
never clone a repository.

### Where each thing lives

| Concern | Module | Notes |
| --- | --- | --- |
| Azure authentication | `connections/azure.py` — `AzureCredentialProvider`, `AzureCliCredentialProvider`, `InteractiveBrowserCredentialProvider` | The **only** use of `azure.identity` in the repository. Signed-in browser identities are held per (method, tenant, client id) by `credential_provider()`; `reset_credentials()` drops them |
| Token acquisition + caching | `connections/azure.py` — `credential_provider()`, `AccessToken` | Per-audience cache, 5-minute expiry margin |
| Azure ARM | `connections/azure.py` — `AzureConnection`, `ArmTransport` | The only HTTP client; GET only, so this layer cannot mutate anything |
| Synapse workspace | `connections/synapse.py` — `SynapseWorkspaceConnection` | Workspace metadata, workspace id, SQL pool status, and which repository the workspace is Git-integrated with. The only Synapse ARM client |
| Synapse artifacts | `connections/synapse_artifacts.py` — `SynapseArtifactsConnection` | The data plane, on the `dev.azuresynapse.net` audience. Separate from the above because its authorization is separate |
| SQL connection | `connections/sql.py` — `SqlConnection` | Composes the SQL package; takes a *credential provider*, never a subscription |
| Git connection | `connections/git.py` — `GitConnection` | Five providers, HTTPS and SSH; wraps acquisition; probes with `git ls-remote` |
| Configuration | `connections/models.py` | Non-secret only. No password, token, or client-secret field exists |
| Validation results | `connections/validation.py` | Structured, redacted on construction |
| **SQL protocol/catalog** | `sql/` — `PyodbcConnector`, `SqlConnectionConfig`, `CatalogSource`, `DedicatedPoolSource`, `queries.py` | SQL-specific, stays here. Cannot acquire a credential |
| **Git acquisition** | `acquisition/` — `run_git`, `acquire_repository`, `providers.py` | Git-specific, stays here. The only clone implementation |
| **Synapse artifact protocol** | `synapse/` — `ArtifactsClient`, `SynapseArtifactSource`, `api.py` | Data-plane-specific, stays here. The fixed GET-only route registry, mirroring `sql/queries.py` |
| **Cross-source assembly** | `records.py` | Identity resolution, drift, and building `UnifiedDiscoveryRecord`s. Connects to nothing |

The split is: `connections/` owns **authentication and external connection
lifecycle**; `sql/` and `acquisition/` own **protocol and operation**. A class
stays where it is if it speaks a protocol rather than establishing an identity.

### One login, one session

```
az login
   |
   v
ConnectionManager.credential()      <- built at most once per run
   |
   +-- management.azure.com/.default  -> AzureConnection, SynapseWorkspaceConnection
   +-- database.windows.net/.default  -> SqlConnection -> ODBC Driver 18
```

One `AzureCredentialProvider` per `ConnectionManager`, created lazily on first
use and injected into every Azure-backed connection. Audiences get their own
tokens — that is how OAuth works — but they come from the same identity.

Token handling is automatic and invisible:

* acquired on demand, never pasted in, never read from configuration;
* cached in memory per audience, for the process only, never written to disk;
* re-acquired silently when within 5 minutes of expiry, so a long run cannot
  present an expired token;
* the SQL layer holds a *callable*, not a token, so it picks up a refreshed
  token between connections;
* never present in a connection string, a `repr()`, a log line, an exception
  or a validation result. `AccessToken.__repr__` describes the token, and
  `ConnectionValidation` redacts every message and detail on construction.

`ConnectionSettings` cannot carry a secret. There is no field for a password,
access token, refresh token or client secret, and a repository URL with
credentials embedded in it is rejected at construction.

### Sign-in methods: Azure CLI and Interactive browser

The UI offers two ways to sign in to Azure.

| Method | What happens | Use it when |
| --- | --- | --- |
| **Azure CLI** (`azure_cli`) | A sign-in window opens each time you authenticate. Nothing is kept between sign-ins. | The default. |
| **Interactive browser** (`interactive_browser`) | A Microsoft sign-in window opens against the **tenant you name**. You pick or type your own account there (MFA included); no account is pre-selected. One sign-in serves every audience: ARM, Synapse, SQL and Fabric. | A tenant your `az login` cannot reach, or when you want to leave your Azure CLI session alone. |

Interactive browser never runs `az` and never reads or writes the Azure CLI's
token cache: `az account show` reports the same account before and after.

* **The window opens on the machine running the server**, not necessarily the
  one your browser is on. If the API runs elsewhere (a VM, a container,
  headless), use Azure CLI instead. You have 120 seconds to finish the sign-in.
* **The form asks for two things only: Tenant ID and Subscription ID**, both
  required. There are no optional fields. If the tenant answers
  `access_denied`, it has not consented to Microsoft's default developer
  sign-in app; its administrator needs to allow that app. (The command line
  still accepts `--client-id` for a tenant that requires its own app.)
* **It survives a server restart without a new window.** Tokens go into the
  SDK's encrypted token cache (`synapse-discovery-agent`; DPAPI on Windows,
  Keychain on macOS, libsecret on Linux), never in plaintext. If no keyring is
  available, the cache stays in memory only. An *authentication record*
  (username, home account id, authority, tenant, client id; **no token**) is
  kept at `~/.synapse-discovery/authentication-record.json`, or under
  `$SYNAPSE_DISCOVERY_HOME`. azure-identity needs that record to sign in
  silently from its cache.
* **Signing out** (Disconnect in the UI, `DELETE /api/connections`) drops the
  identities the server holds and deletes the record, so the next sign-in
  opens the window again. The Azure CLI session is untouched.

From the command line: `--credential-method interactive_browser --tenant <id>
[--client-id <id>]`.

### Git: providers, transports and credentials

Five providers, detected from the **hostname** and never from the repository
name:

| Provider | Hosts | Transports |
| --- | --- | --- |
| `github` | `github.com` | HTTPS, SSH |
| `azure_devops` | `dev.azure.com`, `ssh.dev.azure.com`, `*.visualstudio.com` | HTTPS, SSH |
| `gitlab` | `gitlab.com` | HTTPS, SSH |
| `bitbucket` | `bitbucket.org` | HTTPS, SSH |
| `generic` | any host no named provider claims | HTTPS, SSH |

`generic` is a real answer, not a failure: a self-hosted GitLab, Gitea or
Bitbucket Server is reached exactly the same way as github.com. A *known* host
with a malformed path (`github.com/contoso` — a user page, not a repository)
is rejected rather than adopted as generic, so the error arrives at
configuration time instead of at clone time. `git://` and `file://` are
refused: neither can carry authenticated access to a private repository.

Private repositories work because **git authenticates, not this application**:

| Transport | Mechanism reported | What actually holds the secret |
| --- | --- | --- |
| HTTPS | `git_credential_manager` | your configured `credential.helper` (Git Credential Manager on Windows) |
| SSH | `ssh` | your SSH key, ssh-agent and `~/.ssh/config` |

There is no PAT field, no token prompt, no provider OAuth client and no
provider-specific API anywhere in this layer. The application reports *which*
mechanism applies; git does the rest. This is completely independent of Azure
authentication — no Azure identity is consulted to reach a repository, and
`GitConnection` has no way to obtain one.

Validation reports what a user needs to see, and nothing else:

```
[OK] Git
     provider: github
     transport: https
     authentication: git_credential_manager
     repository: public
     ref: main
     reachable: true
```

`repository` is determined by asking, not assuming: a second `ls-remote` with
every credential helper switched off. If that succeeds the repository is
public; if it is refused while the authenticated probe succeeded, it is
private. Over SSH there is no credential-free form to request, so the answer is
`unknown` rather than a guess.

**Probes never prompt.** Every validation call carries
`-c credential.interactive=never` and a 60-second timeout, so a host with no
cached credential fails in seconds instead of blocking behind a Git Credential
Manager sign-in window — which matters on a server, where there is no display
to open one on. Cloning through `acquire()` is left interactive, so a user
running a clone themselves can still sign in normally.

**Credential safety.** A URL with a username, password or token embedded in it
is refused at construction, and the rejection never quotes what it found. Over
SSH, `git@host` is recognised as a login name rather than a secret, so SSH
remotes are not caught by that rule. Git failures are redacted twice — once
where the message is produced, once when the `ConnectionValidation` is built —
because a misconfigured credential helper prints its protocol output on
failure and git's stderr is not ours to vouch for. Nothing reads or logs a
credential environment variable.

**One execution boundary.** `acquisition/git.py` holds the only
`subprocess.run` in the codebase, and `acquire_repository` is the only clone
implementation. `GitConnection` reaches both through the injected runner and
adds neither.

### Validating the whole chain

```
python -m discovery_agent.connections \
    --subscription-id <guid> \
    --resource-group <resource group> \
    --workspace <workspace> \
    --sql-pool <pool> \
    --repository-url https://github.com/<owner>/<repo> --ref main
```

This exercises the real path — an ARM call, an ARM workspace and pool read, an
Entra-authenticated `SELECT DB_NAME()` on the pool, and a `git ls-remote` — not
just the configuration fields.

```
[OK] Azure
     subscription_id: ...
     tenant_id: ...
     credential: Azure CLI sign-in

[OK] Synapse
     workspace_id: ...
     sql_pool_status: Online

[OK] SQL
     connected_database: ...
     principal: ...
     authentication: access_token
     driver: ODBC Driver 18 for SQL Server

[OK] Git
     provider: github
     ref: main

Overall: READY
```

A failure names the category rather than collapsing into "connection failed":

```
[FAIL] Synapse
     category: NOT_FOUND
     message: workspace 'x' in resource group 'y' could not be read: ...
```

Categories are `AUTHENTICATION`, `AUTHORIZATION`, `NETWORK`, `CONFIGURATION`,
`DEPENDENCY`, `NOT_FOUND`, `UNAVAILABLE`, `UNSUPPORTED` and `UNKNOWN` — so
"the workspace does not exist", "you may not read this workspace", "the pool is
paused" and "the network is down" stay four different answers.

Exit code 0 when every configured connection passed, 1 when one failed or was
skipped, 2 when nothing was configured. `--json` emits the structured report;
`--skip-remote-probe` leaves the git remote alone. Every argument also has an
environment variable, for developer convenience — the UI will build the same
typed configuration objects directly.

### How discovery code obtains a connection

This is the entry point. Nothing else:

```python
from discovery_agent.connections import (
    AzureConnectionConfig, ConnectionManager, ConnectionSettings,
    GitRepositoryConfig, SynapseConnectionConfig,
)

connections = ConnectionManager(ConnectionSettings(
    azure=AzureConnectionConfig(subscription_id="<guid>"),
    synapse=SynapseConnectionConfig(
        resource_group="<rg>", workspace_name="<ws>", sql_pool_name="<pool>",
    ),
    git=GitRepositoryConfig(repository_url="https://github.com/<owner>/<repo>"),
))

report   = connections.validate_all()   # structured results; never prints
source   = connections.sql().source()   # a CatalogSource, ready to query
snapshot = connections.git().acquire()  # a RepositorySource, ready to walk
workspace = connections.synapse().metadata()
```

A future Synapse Artifacts source (pipelines, notebooks, datasets, linked
services, triggers, Spark jobs) receives its token the same way —
`connections.credential().token_provider_for(SYNAPSE_SCOPE)` — and must not
implement authentication of its own.

`python -m discovery_agent.sql` and `python -m discovery_agent.acquisition`
follow this rule themselves: both are consumers of the connection layer, not
second connection paths. They are the worked examples to copy.


## Discovery

You say *where* to look. The tool works out *what is there* — there is no flag
with which to name a pipeline, a notebook or a table, because enumerating them
is the point. There is no `--token`, `--password` or `--pat` flag either.

```bash
# A clone you already have. No credentials, no network, no Azure sign-in.
python -m discovery_agent --source input/repository/<repo>

# Acquire and scan. Git authenticates with your own credential helper or SSH key.
python -m discovery_agent --repository-url https://github.com/<owner>/<repo> --ref main

# The whole estate: repository, live workspace and dedicated pool.
python -m discovery_agent \
  --repository-url https://github.com/<owner>/<repo> \
  --subscription <id> --resource-group <rg> \
  --workspace <workspace> --sql-pool <pool>

# The live workspace alone, with no repository at all.
python -m discovery_agent --workspace <workspace> --resource-group <rg>
```

`--json` prints the run summary as JSON instead of text.

### What it reports

Per P0 artifact: how many records, which sources contributed, and — where the
workspace is Git-integrated with the repository that was scanned — whether the
committed and published definitions agree.

```
P0 coverage (20 records):
  dedicated_sql_table       1  primary=sql        sources=sql
  sql_view                  0  primary=sql        sources=none
  stored_procedure          0  primary=sql        sources=none
  sqlscript                 0  primary=repository sources=none
  pipeline                  1  primary=repository sources=repository, synapse
  dataset                   9  primary=repository sources=repository, synapse
  linkedService             8  primary=repository sources=repository, synapse
  notebook                  1  primary=repository sources=repository, synapse
  sparkJobDefinition        0  primary=repository sources=none
```

A zero means none were found. A source that could not be read is reported as
an issue and never as a count — an artifact type whose endpoint returned 403
is absent from the counts entirely, so it cannot be mistaken for empty.

### Permissions

| To read | You need |
|---|---|
| The repository | Whatever your own git credential helper or SSH key already grants |
| Workspace and pool metadata (ARM) | Reader on the resource group |
| Workspace artifacts (data plane) | A Synapse role such as **Synapse Artifact User** |
| The SQL catalog | A database user with read access to the catalog views |

The two Synapse permissions are independent, and the tool validates them
separately: Reader on the resource group satisfies ARM and grants nothing on
the data plane.

## Migration

The **Plan & Migrate** page scores the plan, lets you choose which **stages**
run and how, checks the target, then runs the migration wave by wave. It is the
one part of the application that writes, and it writes only to Fabric: nothing
in Synapse is changed.

### Stages

Each stage is a capability you switch on or off, tune, and run on its own
("Run this stage") or together ("Start migration", in dependency order). The
list lives in one place, `migration/capabilities.py`, and the UI reads it from
`GET /api/migration/capabilities`.

| Stage | Synapse object | Becomes in Fabric | Strategy / notes |
|---|---|---|---|
| Warehouse & schema | Dedicated SQL pool, schema, table, view, stored procedure | A Warehouse of the same name, its schemas, **empty** tables, views and procedures | Tables are rebuilt from discovered columns; storage clauses are dropped and unsupported types mapped, each change noted. Views and procedures are created from their own text. |
| Table data | The rows of each migrated table | Rows in the Warehouse table | Read in chunks, written as multi-row `INSERT`s with typed parameters, **counts verified**; a failed load is cleared so a re-run is not fooled. Choose *skip* (default) or *replace* for a table that already has rows. Tables over the row limit (default 1,000,000) are left for a pipeline Copy activity. |
| Spark pool & environment | Spark pool | A custom Spark pool and a published Environment | Node size, autoscale, runtime mapped; libraries are not copied. |
| Notebooks | Notebook | Fabric Notebook | Cells kept; Spark pool binding and outputs dropped; Synapse-only calls flagged. .NET notebooks are deferred. |
| Connections | Linked service | Fabric connection | Synapse does not give up the secret, so you enter credentials (sent once, held in memory, never stored or returned). SQL, ADLS Gen2 and Blob are converted; others are created by hand. |
| Pipelines & datasets | Pipeline, dataset | Fabric data pipeline | Datasets are embedded in each activity; notebook, pipeline and Spark-job references become Fabric ids; anything that read the Synapse pool is pointed at the migrated Warehouse. A pipeline with an activity that has no Fabric equivalent is **not created**, with the activity names. |
| Spark job definitions | Spark job definition | Fabric Spark job definition | Main file, class, arguments and libraries kept; attached to the Environment of the same name. |
| SQL scripts | SQL script | A notebook with a T-SQL cell bound to the Warehouse | Flagged for review: Synapse-only T-SQL may be rejected. |
| Schedules | Schedule trigger | Pipeline schedule, **created switched off** | Minute, hour, daily and weekly recurrences. Event and tumbling-window triggers, and every-N-days or monthly, are recreated by hand. |
| External tables | External table | A OneLake shortcut in a Lakehouse | The storage location is read from the pool's external data source; needs a Fabric connection to that storage. |

Integration runtimes are always set up by hand.

* **Planner.** `POST /api/migration/plan` is read-only and deterministic: a
  strategy per object (Automated, Manual, Assess first, Later, Not selected),
  risks with a severity, effort, a readiness score, pre-run checks and a
  history of recorded runs. It reads the real definitions, so an unsupported
  column, an unreadable view, a pipeline activity with no Fabric equivalent or a
  linked service missing credentials is a finding *before* the run.
* **Safe to re-run.** An object that already exists in Fabric is **Skipped**,
  never overwritten. Retry Failed runs only the failed objects again.
* **Order.** Waves come from the dependency graph; within a wave: warehouse,
  schemas, connections, tables, views, procedures, data, notebooks, scripts,
  jobs, pipelines, shortcuts, schedules. *Stop at the end of a wave with
  failures* is an option, and Pause stops after the current object.
* **One Warehouse per SQL pool,** named after the pool.
* **A Fabric capacity is required.** Test Connection on the Fabric Target page
  reports it, and a run is refused up front without one.
* **Sign-in: either CLI.** With **Azure CLI** the run gets tokens from `az` for
  the Fabric API and the Warehouse SQL endpoint. With **Fabric CLI**, Fabric
  calls go through `fab api`; the first SQL object opens one Microsoft sign-in
  window (the Fabric CLI cannot issue a SQL token).
* **Needs** ODBC Driver 18 on the machine running the server. Table data and
  shortcuts also read from the connected Synapse pool, so keep that connection.
* **What is verified.** The data load has been run against a real Warehouse.
  The other stages are tested offline against Synapse's documented formats and
  Fabric's documented request bodies; Fabric validates each create, and its own
  error message is shown if it disagrees.

### Validation

The **Validation** page (`POST /api/migration/validate`) compares the discovered
Synapse objects with what is now in Fabric, object by object, and reads both
sides without writing to either. Each row says what it saw on each side:

| Category | What is compared |
|---|---|
| Warehouse, Schema | The Warehouse and each schema exist. |
| Tables | Column names and types (a type changed by design is a REVIEW that names it). |
| Data Count | Rows in Synapse against rows in the Warehouse; zero loaded is a REVIEW, a difference a MISMATCH. |
| Views, Stored Procedures | Present, and the definition text agrees (ignoring comments and whitespace). |
| Spark | The custom Spark pool and Environment exist with the same node size and scale. |
| Notebooks, Pipelines | Present, with the same number of cells / activities (nested activities included). |
| Connections, Spark Jobs, SQL Scripts, Schedules, Shortcuts | Present in Fabric. |
| Manual | Integration runtimes and anything else set up by hand, listed for review. |

A check that could not run is a REVIEW with the reason, never a match. Row
counts need the Synapse connection; without it they are reported as unread.

API: `GET /api/migration/capabilities`, `POST /api/migration/plan`,
`POST /api/migration/validate`, `POST /api/migration/start` (`{items, options: {scope, stages, dataMode,
maxRows, stopOnFailure}, credentials}`), `GET /api/migration/run` and
`POST /api/migration/control` (`pause|resume|retry`). Code:
`src/discovery_agent/migration/` (`runner.py`, `stages.py`, `stages_fabric.py`,
`capabilities.py`, `planner.py`, `preflight.py`, `datacopy.py`,
`fabric_connections.py`, `pipelines.py`, `jobs.py`, `notebooks.py`,
`environments.py`, `warehouse_ddl.py`, `fabric_rest.py`) and `api/migration.py`.

## Tests

```
pytest
```

Tests run fully offline — no network access, no Azure subscription, no ODBC
driver and no dependency on any real repository. Every credential, ARM call,
SQL session and git invocation in the unit suite is a fake.

Two opt-in integration suites talk to real resources and are skipped unless
their flag and configuration are both set:

```
# the catalog queries, against a real dedicated pool
SYNAPSE_SQL_INTEGRATION=1 SYNAPSE_SQL_SERVER=... SYNAPSE_SQL_DATABASE=... pytest

# the whole connection chain, against a real subscription
CONNECTIONS_INTEGRATION=1 AZURE_SUBSCRIPTION_ID=... SYNAPSE_RESOURCE_GROUP=...   SYNAPSE_WORKSPACE_NAME=... SYNAPSE_SQL_POOL=... pytest
```
