# Synapse → Fabric Migration Accelerator

Moves an **Azure Synapse Analytics** workspace to **Microsoft Fabric**. It
reads everything in the Synapse workspace, works out how each object maps to
Fabric, orders the objects into dependency waves, builds and scores a
migration plan, creates the objects in Fabric, and then compares the two sides
object by object.

The accelerator *migrates* objects rather than copying them: table structures,
views, procedures, notebooks, pipelines, connections, Spark settings and
schedules are rebuilt in their Fabric form, with Synapse-only syntax rewritten
on the way. Table **data** is never copied through the accelerator. It is
loaded by **Fabric data pipelines** that the accelerator creates, one per wave.

It is a web application (React, `frontend/`) on a small local Python API
(`src/discovery_agent/`). **Demo data** mode runs the whole application on
generated sample data, with no Azure, Synapse or Fabric needed.

**Contents:**
[What it does](#what-it-does) ·
[Principles](#principles) ·
[Quick start](#quick-start) ·
[Using the application](#using-the-application) ·
[Architecture](#architecture) ·
[Repository layout](#repository-layout) ·
[API](#api) ·
[What is automated, manual, or coming](#what-is-automated-manual-or-coming) ·
[Discovery engine](#discovery-engine) ·
[Connection architecture](#connection-architecture) ·
[Migration](#migration) ·
[Tests](#tests)

## What it does

A migration is six steps, done in order. Each step is reviewed and confirmed
before the next one opens.

| # | Step | What happens | Writes to |
|---|---|---|---|
| 1 | **Discover** | Reads every object in the Synapse workspace (SQL pools, tables, views, procedures, scripts, pipelines, datasets, linked services, triggers, notebooks, Spark pools, Spark jobs, libraries, integration runtimes, storage) and maps each one to its Fabric component. | Nothing |
| 2 | **Assess** | Classifies every object by how it moves: **Direct** (a matching Fabric component exists), **Reconfigure** (same idea, configured differently), **Transform** (code or settings change), **Manual** (needs a person), **Review** (no reliable one-to-one mapping). Also shows the Synapse → Fabric component mapping. | Nothing |
| 3 | **Waves** | Builds the dependency graph and groups objects into waves, so nothing is created before what it depends on. | Nothing |
| 4 | **Plan** | Chooses what moves. A deterministic planner gives each object a strategy, lists risks by severity, estimates effort and scores readiness (0–100). Stages, their options and any credentials are set here. | Nothing |
| 5 | **Migrate** | Pre-run checks, then the run, wave by wave: Warehouse and schemas, empty tables, views, procedures, data pipelines that load the rows, notebooks, connections, pipelines, Spark, jobs, scripts, schedules and shortcuts. Safe to re-run; Retry failed re-runs only the failures. | **Fabric only** |
| 6 | **Validate** | Compares Synapse with Fabric, object by object: columns and types, row counts, definitions, notebook cells, pipeline activities. | Nothing |

## Principles

* **Synapse is never changed.** Every Azure call against the source is a GET,
  and every SQL statement is one of a fixed set of named SELECT queries.
* **Only the Migrate step writes, and only to Fabric.** An object that already
  exists in Fabric is skipped, never overwritten.
* **Structure is migrated; data moves through Fabric data pipelines.** Rows go
  from the Synapse pool to the Fabric Warehouse inside Fabric, never through
  the accelerator.
* **No secrets are kept.** Sign-in is done by the Azure CLI, a Microsoft
  sign-in window, or the Fabric CLI, on the machine running the API. There is
  no password or token field for signing in. Credentials that Synapse will not
  give up (for linked services and the data-load connection) are typed into the
  Plan step, sent once, held in memory, and never stored, logged or returned.
* **Deterministic.** The same workspace and the same plan give the same
  result. Parsers and rules do the work; no language model is in the path.
* **Honest about limits.** Anything that cannot be converted is reported, with
  the reason, as a risk before the run and as *left for a person* after it.

## Quick start

**You need:** Python 3.10+, Node.js 18+, the Azure CLI (and optionally the
Fabric CLI), ODBC Driver 18 for SQL Server, and a Fabric workspace on a Fabric
or Trial capacity.

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,live]"
az login
.\start-ui.ps1
```

`start-ui.ps1` starts the API on port 8001 and the UI dev server on port 5173,
and opens the browser. To start them yourself:

```
python -m discovery_agent.api              # API on http://127.0.0.1:8001
cd frontend && npm install && npm run dev  # UI on http://localhost:5173, proxies /api to :8001
```

Or as one process: `cd frontend && npm run build`, then
`python -m discovery_agent.api` serves the built UI on port 8001.

**No Azure?** Choose **Demo** in the top bar. A sample Synapse workspace
(`demo-synapse-ws`, about 1,500 objects) and a sample Fabric workspace
(`Fabric_demo`) are already connected, and every step works on simulated
results. See [frontend/README.md](frontend/README.md).

## Using the application

The application has two pages and no sidebar: **Connections**, where a
migration starts, and **Migration**, where it runs.

### Connections: choose the route and connect

1. **Choose the route.** Two dropdowns: *From* (source) and *To*
   (destination). **Azure Synapse Analytics** and **Microsoft Fabric** can be
   chosen today. Other platforms are listed, dimmed and marked *Coming soon*
   so the roadmap is visible: Azure Data Factory, SQL Server, Azure
   Databricks, Snowflake, Teradata, Oracle, Amazon Redshift and Google
   BigQuery as sources; Azure Databricks, Snowflake, Azure SQL Database and
   Google BigQuery as destinations.
2. **Connect both sides.** Each side is a card that shows its status, and
   connecting is three steps: sign in, choose the workspace, test.

   | Side | Sign-in methods |
   |---|---|
   | Azure Synapse (source) | **Azure CLI**: *Sign in with Azure*, then choose the subscription, resource group, workspace and SQL pool from lists (nothing typed); **Git repository** and **Workspace export (ZIP)**, for one environment (see below) |
   | Microsoft Fabric (destination) | **Azure CLI**; **Fabric CLI** |

   After sign-in, Synapse asks for the resource group, workspace and
   (optional) dedicated SQL pool; Fabric asks for the workspace. A list with a
   single entry is chosen for you, so a workspace's only SQL pool is never
   skipped by accident. Test runs real checks: ARM and data-plane access, the
   SQL pool, and for Fabric the workspace and its **capacity**. While the page
   first reads both sides it says *Checking…* rather than *Not connected*.
   Disconnecting the source asks first when discovery results would be lost.
3. **Start migration.** The button at the bottom is enabled once the route is
   chosen and both sides are connected and tested, with a capacity on the
   Fabric workspace. A checklist beside it says what is still missing. If the
   migration is already under way, the button reads *Continue migration*.

### Git repository or ZIP export, per environment

Instead of the live workspace, the definitions can come from the workspace's
Git repository or a ZIP of it (`api/repository_source.py`):

* **Git repository:** the URL, the **branch** for the environment (for
  example `main` for Dev, a release branch for Prod) and, optionally, the
  workspace's Git **root folder**. It is cloned by `git` on the machine running
  the API, with your own credential helper or SSH key; no token is entered,
  and a URL with credentials in it is refused.
* **Workspace export (ZIP):** a ZIP of the repository folder (for example a
  branch downloaded from GitHub or Azure DevOps), up to 200 MB. It is
  extracted safely (no paths outside its folder, no symbolic links, size and
  file-count limits), and the folder holding `pipeline/`, `linkedService/` and
  the others is found automatically, or named.
* **Environment:** Dev, Test, Prod or a named one, shown with the connection.
  An optional **parameters file** (`TemplateParametersForWorkspace.json`, or an
  environment's copy) replaces the linked-service values committed in the
  repository, which are usually Dev's, with that environment's: parameters
  named `<linked service>_connectionString` or
  `<linked service>_properties_typeProperties_<property>`. They are applied to
  a working copy under `input/environments/`; the repository and the upload
  are never changed. A parameter that matches no existing property is listed,
  not guessed at.
* **Optional SQL pool:** the repository holds definitions only. To also
  migrate tables and data, sign in to Azure in the same card and choose the
  dedicated SQL pool; only the pool is read from Azure, the definitions still
  come from the repository. Without it, tables, data loads, date filters and
  row counts are skipped.

Triggers and integration runtimes are read from the repository's own
folders. Spark pools and libraries are not in a repository, so they appear
only when the SQL pool's workspace is signed in to.

### Migration: the six steps

The six steps sit side by side at the top, each a box with its key number
(objects found, % direct, waves, readiness, objects migrated, matches) and its
state: *To do*, *Running*, *Paused*, *Needs attention*, *Ready to confirm*,
*Done*, *Run again* or *Locked*. Clicking a box opens that step's details
below the flow, and the address and the browser tab follow the step you are
on. Steps after the current one stay locked until it is confirmed.

Each step's details start with a short summary row and put the detail in tabs,
so the page stays readable:

| Step | Summary | Tabs |
|---|---|---|
| Discover | Objects, object types, with a Fabric mapping, manual or review, warnings | Overview (a tile per object type; click one to filter), Inventory (searchable table; click a row for the object's details, Fabric target and steps) |
| Assess | A tile and percentage per class, and a bar across all of them | By workstream, Objects, Component mapping |
| Waves | Objects, dependencies, waves | Waves (one row per wave with its object types), Dependency graph |
| Plan | Readiness, objects, automated, needs a person, effort, blocking | Readiness & risks, Objects in plan, Strategy by type, Stages & credentials (configuration only: nothing runs from Plan) |
| Migrate | Pre-run checks, then the run: every stage that is on or a single stage, progress by wave, every object's result, pause, resume, Retry failed | — |
| Validate | Checks, match, review, mismatch (each one filters) | One tab per category |

The bar at the foot of every step says where it stands and how to move on:
*Looks good, continue to …* once the step is finished, *Continue to … anyway*
when it finished with something worth a look (warnings, failures, blocking
risks), and a disabled button with the reason while it is not finished. Plan
cannot be confirmed while a risk is blocking. Confirming the last step shows
*Migration complete*: the final numbers, said plainly when objects failed or
checks did not match, and **Download report (CSV)** with every object's result
and every validation check.

**Starting a step over.** Every step has **Reset step** in its header. It
clears that step and every step after it (the discovered inventory, the plan
with its options and typed credentials, the record of the run, the validation
results, and their confirmations), then lets you do them again from the
start. A dialog lists exactly what will be cleared first, and says what is not
touched: the connections, everything in Synapse, and anything already created
in Fabric (a new run skips what exists; nothing is deleted). Reset is refused,
with the reason, while discovery, a run or a validation is working. Running
discovery again from Discover asks first when later steps are confirmed.

**Results that disappear.** Confirmations are kept per project in the
browser, but the discovery and the run live in the backend's memory and the
validation results in the page's. When a confirmed step's results are gone
(the source was signed out, the server restarted, or Demo data was reloaded),
its box says *Run again* (*Rebuild* for an empty plan), the steps after it wait,
and *Migration complete* is hidden until it has been redone; the later steps
then come back by themselves.

### Migration assistant (preview)

The round **+** button at the bottom right (or **Ctrl+I**) opens the
assistant, docked on the right like an editor's chat. It will help resolve
migration errors without leaving the screen: explain why an object failed from
its error and definition, suggest a fix (for example T-SQL a Fabric Warehouse
accepts) for you to review, and apply it and retry just that object. This
release shows the panel, the context it will use (step, source, target,
failed objects) and suggested questions; the conversation itself is not built
yet, and nothing typed there is sent anywhere.

The layout works from wide screens down to phones: on a phone the top bar
takes two rows, the step strip swipes sideways with the selected step kept in
view, and the action bars sit at the end of the page.

### Top bar

Connections and Migration (Migration opens once a source is connected), a
status chip for each side, the **project menu** (switch projects, or create,
rename and delete one; each project keeps its own plan, stage options and
progress in this browser), **Export** (the discovered inventory as JSON),
**Refresh**, and the **Live / Demo** switch. In Live mode, if the backend does
not answer, a bar under the top bar says so, with **Retry** and **Use Demo
data**.

## Architecture

```
 Browser: React UI (frontend/)
    │  /api/*   (proxied by Vite in development; same origin when the API serves the build)
    ▼
 Local API: python -m discovery_agent.api (port 8001)
    ├─ connections/          Azure sign-in and every source connection: Azure, Synapse, SQL, Git
    ├─ discovery, extractors/, records, sql/, synapse/   read the source into Unified Discovery Records
    ├─ mapping/              Synapse → Fabric mapping, classification, dependency waves
    ├─ api/fabric.py         Fabric sign-in through the Azure CLI or the Fabric CLI
    └─ migration/            planner, stages, T-SQL rules, Fabric REST, data pipelines, validation
             │
             ▼
 Microsoft Fabric: REST API, Warehouse SQL endpoint, data pipelines
```

The UI talks to one interface, `MigrationApi` (`frontend/src/types.ts`). In
Live mode that is `realApi.ts` and the local API; in Demo mode it is
`mockApi.ts`, which simulates the same responses in the browser.

## Repository layout

| Path | What is there |
|---|---|
| `frontend/` | The web application. `src/pages/` (Setup = Connections, Workspace = Migration), `src/components/` (`setup/`, `connections/`, `journey/` with one panel per step, `discovery/`, `migrate/`, `layout/` with the top bar and assistant, `shared/`), `src/state/`, `src/services/realApi.ts`, `src/mock/`. See [frontend/README.md](frontend/README.md). |
| `src/discovery_agent/connections/` | Authentication and every external connection. The only place that signs in. |
| `src/discovery_agent/discovery.py`, `records.py`, `extractors/`, `sql/`, `synapse/`, `acquisition/`, `artifacts/` | The discovery engine: what the workspace, the SQL pool and the Git repository say, merged into one record per object. |
| `src/discovery_agent/mapping/` | `synapse_fabric_mapping.py` (Fabric target, classification and workstream for each object type and pipeline activity) and `waves.py` (dependency waves). |
| `src/discovery_agent/migration/` | The planner, the ten stages, the T-SQL rules, Fabric REST calls, data pipelines and validation. See [Migration](#migration). |
| `src/discovery_agent/api/` | The local HTTP API: `service.py` (connections, discovery, results), `fabric.py` (Fabric sign-in), `migration.py` (plan, run, validate), `mapping.py`, `server.py`. |
| `tests/` | The backend test suite; runs offline. |
| `docs/` | [discovery.md](docs/discovery.md), [p0_source_strategy.md](docs/p0_source_strategy.md), [unified_discovery_record.md](docs/unified_discovery_record.md), and the *Synapse to Fabric Transformation Guide* (Word): what changes between Synapse and Fabric, what is automated and what needs a person. |
| `input/` | Where Git repositories are cloned for discovery (gitignored). |
| `start-ui.ps1` | Starts the API and the UI together on Windows. |

## API

All under `/api`, JSON in and out. Nothing returns a token or a secret.

| Area | Endpoints |
|---|---|
| Health | `GET /health`: the sign-in methods this backend supports, what discovery reads, and the types a run migrates |
| Synapse connection | `GET /connections` · `POST /connections/authenticate` · `POST /connections/test` · `POST /connections/upload` (a ZIP, `application/zip`) · `POST /connections/repository` (Git or the uploaded ZIP, for one environment) · `DELETE /connections` · `GET /azure/<subscriptions, resource groups, workspaces, pools>` for the dropdowns |
| Discovery | `POST /discovery/start` · `GET /discovery/status` · `GET /discovery/results` (filter, sort, page) · `GET /discovery/results/<id>` · `GET /discovery/export` · `DELETE /discovery` (forget the results; refused while discovery runs) |
| Mapping and waves | `GET /mapping/components` · `GET /dependencies` |
| Fabric connection | `GET /fabric/connection` · `POST /fabric/authenticate` · `POST /fabric/workspaces` · `POST /fabric/test` · `DELETE /fabric/connection` |
| Migration | `GET /migration/capabilities` · `POST /migration/plan` · `POST /migration/start` · `GET /migration/run` · `POST /migration/control` (`pause`, `resume`, `retry`, `reset`: forget the run's record, refused while it is working) · `POST /migration/validate` |

## What is automated, manual, or coming

**Automated by a migration run:** Warehouse (one per SQL pool) and schemas;
tables as empty structures with types mapped and keys recreated `NOT
ENFORCED`; views and stored procedures after the T-SQL rules; table data
through per-wave Fabric data pipelines with row counts compared; custom Spark
pools and Environments; notebooks with `mssparkutils` and Synapse connector
calls rewritten; linked services as Fabric connections (SQL, ADLS Gen2, Blob)
from the credentials you enter; pipelines with datasets embedded and
references re-pointed; Spark job definitions; SQL scripts as Warehouse
notebooks; schedule triggers (created switched off); external tables as
OneLake shortcuts.

**Left for a person, and reported as such:** integration runtimes; linked
services of other types; event and tumbling-window triggers and unusual
recurrences; pipelines containing an activity with no Fabric equivalent (for
example mapping data flows); .NET notebooks; Spark libraries; materialized
views, `sys.pdw_*` system views and `COPY INTO` with Synapse's managed
identity; security, networking and workload management.

**Shown in the UI, not built yet:** sources and destinations other than
Synapse and Fabric; the migration assistant's conversation.

## Discovery engine

The engine under the Discover step, also usable on its own from the command
line (`python -m discovery_agent`). Read-only discovery for an Azure Synapse estate. It enumerates the nine P0
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

Assessment, planning and migration are separate packages (`mapping/`,
`migration/`) that build on its records; the engine itself never writes.

### Pipeline

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

### Repository acquisition

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

### Signing in to the Synapse source

The source card signs in first and asks for no ids:

1. **Sign in with Azure** opens a Microsoft sign-in window. The email,
   password and MFA are entered there, in Microsoft's page, never in the
   accelerator. The sign-in goes to the account's own directory, so no tenant
   is asked for.
2. The account's **subscriptions** are listed by name (`GET /api/azure/subscriptions`);
   disabled ones are left out.
3. Choosing one lists its **resource groups**, then **workspaces**, then
   **dedicated SQL pools**; a list with one entry is chosen automatically.
4. **Test connection** checks the chain.

* **Nothing is kept between sign-ins**: each one opens the window again, and
  the existing `az login` session is neither used nor changed.
* **The window opens on the machine running the server**, not necessarily the
  one your browser is on. You have 120 seconds to finish the sign-in.
* **Every directory the account belongs to is searched.** The window signs in
  to the account's own directory; the subscriptions of every other directory it
  is a member or guest of are then read from the same sign-in, silently, and
  each is shown with its directory's name. A directory whose policy (MFA,
  conditional access) wants its own sign-in is offered as a **Sign in to
  \<directory\>** button; no window opens unless that button is pressed.
  Each subscription is then read with tokens for its own directory.
* **Signing out** (Disconnect in the UI, `DELETE /api/connections`) drops the
  identity the server holds.

The command line still accepts `--credential-method interactive_browser
--tenant <id> [--client-id <id>]` for a named tenant.

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


## Discovery from the command line

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

The **Plan** step scores the plan and is where you choose which **stages** run
and how (*Stages & credentials*). The **Migrate** step checks the target, then
runs the migration wave by wave. Migrate is the one part of the application
that writes, and it writes only to Fabric: nothing in Synapse is changed.

### Stages

Each stage is a capability you switch on or off and tune in Plan, under
*Stages & credentials*. The Migrate step runs every stage that is on, in
dependency order, or a single stage on its own (*What to run*). The list lives
in one place, `migration/capabilities.py`, and the UI reads it from
`GET /api/migration/capabilities`.

| Stage | Synapse object | Becomes in Fabric | Strategy / notes |
|---|---|---|---|
| Warehouse & schema | Dedicated SQL pool, schema, table, view, stored procedure | A Warehouse of the same name, its schemas, **empty** tables, views and procedures | Structure only. Tables are rebuilt from discovered columns; storage clauses are dropped and unsupported types mapped, each change noted; primary keys and unique constraints are recreated `NOT ENFORCED`. Views and procedures are created from their own text after the T-SQL rules (below). The Warehouse **collation** is chosen once, at creation: *same as Synapse* (default), case-insensitive or case-sensitive. |
| Table data | The rows of each migrated table | **Fabric data pipelines**, one per wave, and the rows they load | Data never passes through the accelerator. Each wave gets a metadata-driven pipeline (`load_<warehouse>_wave_<n>`): a ForEach over a `tables` parameter whose Copy activity reads the pool with a typed SELECT through a Fabric SQL connection and writes the Warehouse with the COPY command. *Create and run* (default) runs it, waits, and compares row counts table by table; *create only* leaves running it to you. *Skip* (default) leaves tables that already have rows; *replace* truncates them inside the pipeline first. The connection to the pool is reused if one exists, or created from what you choose on this stage (workspace identity, Key Vault, SQL login or service principal). Per-table **date filters** load part of a table and can keep it in sync afterwards: see below. |
| Spark pool & environment | Spark pool | A custom Spark pool and a published Environment | Node size, autoscale, runtime mapped; libraries are not copied. |
| Notebooks | Notebook | Fabric Notebook | Cells kept; Spark pool binding and outputs dropped. Python cells: `mssparkutils` becomes `notebookutils`, and the Synapse SQL connector import and reads of the migrated pool are pointed at Fabric's connector and the Warehouse. Other Synapse-only calls (linked-service credentials, `mssparkutils.env`, connector writes, ADLS paths) are flagged. .NET notebooks are deferred. |
| Connections | Linked service | Fabric connection | Synapse does not give up the secret, so you choose how Fabric signs in. **Workspace identity** and **Key Vault reference** (an Azure Key Vault reference's alias or ID and the secret's name) pass no secret through the accelerator and are offered first; a SQL login, key, SAS token or service principal secret can still be typed (sent once, held in memory, never stored or returned). SQL, ADLS Gen2 and Blob are converted; a parameterised linked service uses its parameters' default values. Others are created by hand, with the linked service's name so pipelines find them. |
| Pipelines & datasets | Pipeline, dataset | Fabric data pipeline | Datasets are embedded in each activity; notebook, pipeline and Spark-job references become Fabric ids; anything that read the Synapse pool is pointed at the migrated Warehouse. Azure Function, Databricks, Batch (Custom), HDInsight, Machine Learning and Webhook activities keep their type and point at the connection of the same name. A pipeline with an activity that has no Fabric equivalent (for example a mapping data flow) is **not created**, with the activity names. |
| Spark job definitions | Spark job definition | Fabric Spark job definition | Main file, class, arguments and libraries kept; attached to the Environment of the same name. |
| SQL scripts | SQL script | A notebook with a T-SQL cell bound to the Warehouse | The T-SQL rules are applied; what they cannot convert is flagged for review. |
| Schedules | Schedule trigger | Pipeline schedule, **created switched off** | Minute, hour, daily and weekly recurrences. Event and tumbling-window triggers, and every-N-days or monthly, are recreated by hand. |
| External tables | External table | A OneLake shortcut in a Lakehouse | The storage location is read from the pool's external data source; needs a Fabric connection to that storage. |

Integration runtimes are always set up by hand.

### Date filters and keeping tables in sync

By default the Table data stage copies every row. Under *Stages & credentials →
Date filters* a table can instead load part of its rows, and optionally keep
loading new and changed rows afterwards (`migration/datafilter.py`):

| Setting | What it does |
|---|---|
| **Date column, from, until** | The first load copies `WHERE <column> >= from AND <column> < until` (either bound may be empty). The dates are fixed in the plan, never "two years ago", so every run of a plan copies the same rows. Row counts after the load, and in Validate, count Synapse with the same filter. |
| **Keep in sync** | After the first load, a sync pipeline copies rows whose **change column** (such as `ModifiedAt`) moved since the table's **watermark**, the newest change already loaded. A synced table has no end date. |
| **Key columns** | Default: the table's primary key or first unique constraint. A changed row replaces the Warehouse row with the same key; without keys it is appended (the planner warns). |
| **Re-read window** (stage option) | Each sync of a keyed table re-reads one hour (default), one day or nothing before the watermark, to catch rows committed late. Re-read rows replace themselves. |

*For every table with column* sets the same filter on every plan table that has
that column. Which column to use is a choice: a business date (`OrderDate`)
scopes what belongs; `CreatedAt` suits append-only tables; `ModifiedAt` is the
only one that sees updates, so it is the usual change column; `DeletedAt` is not
a filter: a soft delete that moves the change column is carried over.

How a synced table is set up, by the Table data stage:

1. The newest change in the filter is read from Synapse. That is the watermark.
2. The first load copies the filter's rows up to the watermark (rows with a NULL
   change column are included), so the first sync starts exactly where it ends.
3. The table is registered in the Warehouse control table
   `migration_sync.sync_tables`: its watermark and the exact SQL each sync runs.
   An empty staging table `migration_sync.<schema>__<table>` is created.
4. One sync pipeline per Warehouse, `sync_<warehouse>`, is created with a
   **daily schedule (02:00 UTC) switched off**. It reads its table list from the
   control table, so registering a table is adding its row; the pipeline never
   changes. Tables are synced one after another (each one's transaction writes
   the control table, and the Warehouse detects write conflicts per table). For
   each table it looks up the newest change in Synapse, copies the
   rows changed since the watermark into the staging table, and runs one
   Warehouse transaction that deletes the matching rows, inserts the staged ones
   and moves the watermark. A failed step leaves the watermark where it was, so
   the next run copies the same window again.

Limits: rows deleted in Synapse stay in the Warehouse (hard deletes leave nothing
to copy); a change column the source does not always update misses those
changes; a table that already has rows is skipped and is not put in sync until
it is reloaded with *Replace it*; a table kept in sync is not loaded while the
sync pipeline's schedule is on (a sync during the load would lose or duplicate
rows), so switch it off before a reload. Validate reports a synced table as in step
when its count lies between "up to the last sync" and "everything in the filter
now".

**T-SQL rules** (`migration/tsql_rules.py`), applied to views, procedures and SQL
scripts before Fabric sees them, each change noted on the object:

* storage options on `CREATE TABLE` / CTAS (`WITH (DISTRIBUTION = ...,
  CLUSTERED COLUMNSTORE INDEX | HEAP | CLUSTERED INDEX (...), PARTITION (...))`)
  are removed;
* `RENAME OBJECT [schema.]a TO b` becomes `EXEC sp_rename`;
* `SET TRANSACTION ISOLATION LEVEL` (other than SNAPSHOT) is removed;
* workload management (`CREATE / ALTER / DROP WORKLOAD GROUP | CLASSIFIER`,
  resource-class `sp_addrolemember`) is removed.

Removed statements stay in the text as a comment. Strings, comments and
quoted identifiers are never touched, and running the rules twice changes
nothing more. The planner reports what the rules will fix (LOW) and what is
left for a person (MEDIUM: `sys.pdw_*` views, materialized views, `COPY INTO`
with Synapse's managed identity).

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
* **A Fabric capacity is required.** Test connection on the Connections page
  reports it, and a run is refused up front without one.
* **Sign-in: either CLI.** With **Azure CLI** the run gets tokens from `az` for
  the Fabric API and the Warehouse SQL endpoint. With **Fabric CLI**, Fabric
  calls go through `fab api`; the first SQL object opens one Microsoft sign-in
  window (the Fabric CLI cannot issue a SQL token).
* **Needs** ODBC Driver 18 on the machine running the server. Row counts after
  a data pipeline run, and shortcuts, read from the connected Synapse pool, so
  keep that connection.
* **What is verified.** Every stage is tested offline against Synapse's
  documented formats and Fabric's documented request bodies; Fabric validates
  each create and each pipeline run, and its own error message is shown if it
  disagrees. The data pipelines' Copy settings (Synapse source, Warehouse sink
  with staging), and the sync pipeline's Lookup, Copy and Script activities
  against the Warehouse, should be confirmed on a first real run.

### Validation

The **Validate** step (`POST /api/migration/validate`) compares the discovered
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
dataRun, collation, stopOnFailure}, credentials}`), `GET /api/migration/run` and
`POST /api/migration/control` (`pause|resume|retry`). Code:
`src/discovery_agent/migration/` (`runner.py`, `stages.py`, `stages_fabric.py`,
`capabilities.py`, `planner.py`, `preflight.py`, `datacopy.py`, `datapipeline.py`, `tsql_rules.py`,
`fabric_connections.py`, `pipelines.py`, `jobs.py`, `notebooks.py`,
`environments.py`, `warehouse_ddl.py`, `fabric_rest.py`) and `api/migration.py`.

## Tests

```
pytest                                  # backend: 1,304 tests
cd frontend && npm run typecheck && npm test   # UI: type check and 43 tests
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

The UI tests run against Demo data in a simulated browser: choosing the
route (only Synapse and Fabric selectable), the sign-in methods (ZIP and Git
shown but not selectable), Start migration staying disabled until both sides
are connected and tested, the step lock and Next, resetting a step, results
that disappear (*Run again*), stage options kept per project, the project menu,
the backend-unavailable bar, the dialogs that ask first, every step's panel, the
assistant, the old page addresses, and that a typed credential never reaches
browser storage.
