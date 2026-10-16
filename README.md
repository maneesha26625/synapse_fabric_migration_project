# Synapse → Fabric Migration Accelerator

Moves an **Azure Synapse Analytics** workspace to **Microsoft Fabric**. It
reads everything in the Synapse workspace, works out how each object maps to
Fabric, agrees a **migration brief** with the client (what moves, how, and who
decides), orders the objects into dependency waves, builds and scores a
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
[Consultation and the migration brief](#consultation-and-the-migration-brief) ·
[Migration](#migration) ·
[LLM-assisted transformation](#llm-assisted-transformation) ·
[Fabric Git (Dev)](#fabric-git-dev) ·
[Projects, run records and the audit trail](#projects-run-records-and-the-audit-trail) ·
[Tests](#tests)

## What it does

A migration is seven steps, done in order. Each step is reviewed and confirmed
before the next one opens.

| # | Step | What happens | Writes to |
|---|---|---|---|
| 1 | **Discover** | Reads every object in the Synapse workspace (SQL pools, tables, views, procedures, scripts, pipelines, datasets, linked services, triggers, notebooks, Spark pools, Spark jobs, libraries, integration runtimes, storage) and maps each one to its Fabric component. | Nothing |
| 2 | **Assess** | Classifies every object by how it moves: **Direct** (a matching Fabric component exists), **Reconfigure** (same idea, configured differently), **Transform** (code or settings change), **Manual** (needs a person), **Review** (no reliable one-to-one mapping). Also shows the Synapse → Fabric component mapping. | Nothing |
| 3 | **Consult** | Asks the questions a consultant asks before a migration (what moves, history and sync, names and standards, SQL users, sign-off, Git, validation depth), each only when the estate makes it relevant and each with a recommendation that quotes the estate. The answers make the **migration brief**, which a person approves: Plan starts from it, and every run is checked against it. | Nothing (the brief is a record in the project) |
| 4 | **Waves** | Builds the dependency graph and groups objects into waves, so nothing is created before what it depends on. | Nothing |
| 5 | **Plan** | Chooses what moves, pre-set from the approved brief. A deterministic planner gives each object a strategy, lists risks by severity, estimates effort and scores readiness (0–100). Stages, their options and any credentials are set here; where the plan differs from the brief, confirming it asks why. | Nothing |
| 6 | **Migrate** | Pre-run checks, then the run, wave by wave: Warehouse and schemas, empty tables, views, procedures, data pipelines that load the rows, notebooks, connections, pipelines, Spark, jobs, scripts, schedules and shortcuts. Safe to re-run; Retry failed re-runs only the failures. A run that changes what Fabric already has waits for a person to sign it off first. Every run is recorded as it happens: a restart brings it back paused, Resume carries it on without making anything twice, and closed runs stay in the run history. When the Fabric workspace is connected to Git, the run is then committed to its branch (`dev`), ready for a pull request. | **Fabric only** (and, through Fabric, its Git branch) |
| 7 | **Validate** | Compares Synapse with Fabric, object by object: columns and types, row counts, definitions, notebook cells, pipeline activities. It also checks each object **runs in Fabric**: tables and views are queried, procedures compiled, and pipelines, notebooks and Spark jobs judged by their latest run (or run now, on request). | Nothing, unless you ask it to run items or procedures (procedures are rolled back) |

## Principles

* **Synapse is never changed.** Every Azure call against the source is a GET,
  and every SQL statement is one of a fixed set of named SELECT queries.
* **Only the Migrate step writes, and only to Fabric.** An object that already
  exists in Fabric is skipped, unless you tick **Update objects that are already
  in Fabric** (Plan → Stages & credentials) to redeploy fixes. Views and
  procedures are then updated with `CREATE OR ALTER`, so the old version stays
  if the new one is rejected. Notebooks, pipelines, SQL scripts and Spark jobs
  get their new definition and keep their id. Tables are never replaced. The one exception is
  Validate: when you tick it, it runs migrated pipelines, notebooks or Spark
  jobs in Fabric to prove they work. It runs procedures only inside a
  transaction that is rolled back. The one write beyond the workspace is the
  commit to its Fabric Git branch after a run, made by Fabric itself and only
  to the branch the workspace is connected to; it can be switched off.
* **Structure is migrated; data moves through Fabric data pipelines.** Rows go
  from the Synapse pool to the Fabric Warehouse inside Fabric, never through
  the accelerator.
* **No secrets are kept.** Sign-in is done by the Azure CLI, a Microsoft
  sign-in window, or the Fabric CLI, on the machine running the API. There is
  no password or token field for signing in. Credentials that Synapse will not
  give up (for linked services and the data-load connection) are typed into the
  Plan step, sent once when the run starts, held in memory, and never stored,
  logged or returned. Planning is told only which linked services have them, and
  refuses a request that carries them. A restart forgets them, so a run carried
  on after one asks for them again. A secret in an environment's parameters
  file never reaches the disk either.
* **Deterministic by default.** The same workspace and the same plan give the same
  result. Parsers and rules do the work. A language model is used only when a person
  asks for a draft of one object ([LLM-assisted transformation](#llm-assisted-transformation)),
  and a run uses the result only after that person has reviewed and accepted it.
* **Honest about limits.** Anything that cannot be converted is reported, with
  the reason, as a risk before the run and as *left for a person* after it.
* **Agreed before it moves.** By default no run starts until a person has
  approved a migration brief that still fits the estate, and a plan that departs
  from the brief says why. The brief says who decided each thing, and which
  decisions nobody confirmed. See [Consultation and the migration brief](#consultation-and-the-migration-brief).
* **Everything is on record.** Every run is written down as it happens: each
  change of status and each object's outcome, and exactly what was sent to
  Fabric. Every action goes into the project's hash-chained audit trail with
  who did it, what, when and how it ended, whether it was a person, the LLM or
  the accelerator itself. What the accelerator does on its own (approving a run
  that needs no sign-off, recovering a run after a restart) is recorded as its
  own, never as a person's. See [Projects, run records and the audit trail](#projects-run-records-and-the-audit-trail).

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
and opens the browser. **Run it once; you don't restart anything by hand after that:**

* The API runs with `--reload` and restarts itself within a second when code under `src/` changes.
  The UI dev server reloads the page itself.
* Everything a project keeps comes back after every restart: its discovery, Fabric target, runs,
  plan versions, last validation and audit trail. A run the restart cut short comes back paused, and
  Resume carries it on. They're saved in `.accelerator/state/projects/<project>/`, which is
  git-ignored; set `MIGRATION_STATE=off` to keep nothing. Tokens, passwords and credentials are never
  saved. The Synapse sign-in isn't kept either, so after a restart sign in to Synapse again only to run
  discovery again, load table data, or count rows in Validate.
* Running `start-ui.ps1` again is safe: what is already running is reused, and the API refuses to
  start a second copy on the same port. It also refuses to start on a state folder another running API
  is using (give it its own with `MIGRATION_STATE_DIR`): two writers would corrupt each other's records.
* The API logs to its console and, as JSON lines, to `.accelerator/logs/api.jsonl` (10 MB, five kept).
  Each line carries the request's **correlation id** (also sent back as `X-Correlation-Id`, and carried
  by everything the request started: a discovery, a run, a validation, an LLM draft, a commit), who asked,
  the project and the run. Secrets are masked before anything is written.
* An unexpected error never shows a traceback in the browser. It is written, redacted, to the API's
  log, and the message on screen quotes its reference, such as *reference 7f3a2c1b*, so you can find
  it there. This covers a request, discovery, a run's object, validation, an LLM draft and the Fabric
  Git commit.

**Keep the API running in the background (recommended).** `.\install-api-service.ps1` creates a
Windows scheduled task, `MigrationAcceleratorAPI`, that runs the API hidden with auto-reload. It
starts it now and every time you sign in to Windows, and no window has to stay open. It needs no
administrator rights. The log is in `.accelerator\api.log`, and `.\install-api-service.ps1 -Remove`
stops and removes it. With the task installed, `start-ui.ps1` only opens the UI.

To start them yourself:

```
python -m discovery_agent.api --reload     # API on http://127.0.0.1:8001, restarts when the code changes
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
   connecting is three steps: sign in, choose the workspace, connect.

   | Side | How it connects |
   |---|---|
   | Azure Synapse (source) | **Synapse workspace**: *Sign in with Azure*, then choose the subscription, resource group, workspace and SQL pool from lists (nothing typed). If the workspace is connected to Git, its definitions are read from its **Git repository** (recommended, chosen for you) or, if you choose, the **live workspace**. **Workspace export (ZIP)**: one environment's export, uploaded (see below) |
   | Microsoft Fabric (destination) | **Azure CLI**; **Fabric CLI** |

   After sign-in, Synapse asks for the resource group, workspace and
   (optional) dedicated SQL pool; Fabric asks for the workspace. A list with a
   single entry is chosen for you, so a workspace's only SQL pool is never
   skipped by accident. Connecting runs real checks: ARM and data-plane access
   (live) or the clone (Git), the SQL pool, and for Fabric the workspace and its
   **capacity**. While the page
   first reads both sides it says *Checking…* rather than *Not connected*.
   Disconnecting the source asks first when discovery results would be lost.
3. **Start migration.** The button at the bottom is enabled once the route is
   chosen and both sides are connected and tested, with a capacity on the
   Fabric workspace. A checklist beside it says what is still missing. If the
   migration is already under way, the button reads *Continue migration*.

### Git repository, live workspace or ZIP export

The **Synapse workspace** option asks for three things: *Sign in with Azure*,
the workspace (subscription, resource group and workspace lists; a list with
one entry is chosen for you) and **Connect**. The definitions (pipelines,
notebooks, scripts and so on) then come from one of two places, shown as one
choice, **Read definitions from**:

| | Git repository (recommended) | Live workspace |
|---|---|---|
| Read from | The repository's collaboration branch, cloned by `git` on this machine (`api/repository_source.py`) | What is published in the workspace, through Synapse's APIs |
| Includes work merged but not yet published | Yes | No |
| Records the commit migrated | Yes (`main · 3f9a1c2`) | No |
| Other environments | Any branch, plus a parameters file | Only the workspace chosen |
| Access needed | Reader on the resource group, and Git access to the repository | A Synapse role such as Synapse Artifact User |

Tables, views, procedures and data come from the dedicated SQL pool either way.

* **Git is chosen for you, nothing typed.** Choosing the workspace reads its
  Git integration from Azure (`GET /api/azure/workspace-git`, an ARM read of
  `workspaceRepositoryConfiguration`) and, when it has one, shows the
  repository, the **collaboration branch** and the **root folder** with *Git
  repository* selected. A workspace without Git integration says so and is
  read live, with nothing to choose. The workspace's dedicated SQL pool is
  chosen with it, so tables come along (*None* in that list leaves them out).
  The environment is **Dev**. Azure reports no credential, and none is asked
  for.
* **More options** (shown with Git, folded, with what will be used beside it):
  another branch (for example a release branch for Prod), the root folder, the
  environment, and an environment's parameters file. Choices made for one
  workspace are cleared when another workspace is chosen.
* **A repository URL** is typed only on request: **Use another repository**
  (under the Git choice), or **Enter a repository URL instead**, which needs
  no Azure sign-in (and then reads no SQL pool). It is cloned by `git` on the
  machine running the API, with your own credential helper or SSH key; no
  token is entered, and a URL with credentials in it is refused. Clone the
  repository once in a terminal (`git clone <url>`) so Git Credential Manager
  remembers your sign-in; the API's own clones then need nothing typed.
* **A workspace connected live** that is in Git shows its *Git integration*
  row and **Use its Git repository instead**, which reopens the form with the
  Git choice made and the same workspace and SQL pool chosen.
* **Workspace export (ZIP):** a ZIP of the repository folder (for example a
  branch downloaded from GitHub or Azure DevOps), up to 200 MB. It is
  extracted safely (no paths outside its folder, no symbolic links, size and
  file-count limits), and the folder holding `pipeline/`, `linkedService/` and
  the others is found automatically, or named.
* **Environment:** Dev unless another is chosen under *More options* (Test,
  Prod or a named one); shown with the connection. An optional **parameters
  file** (`TemplateParametersForWorkspace.json`, or an
  environment's copy) replaces the linked-service values committed in the
  repository, which are usually Dev's, with that environment's: parameters
  named `<linked service>_connectionString` or
  `<linked service>_properties_typeProperties_<property>`. They are applied to
  a working copy under `input/environments/`; the repository and the upload
  are never changed. A parameter that matches no existing property is listed,
  not guessed at. A secret in the file never reaches that copy: the credential
  part of a connection string or URL (`Password=`, `AccountKey=`, `sig=` …) is
  masked, and a value that is only a secret (a password or key) is left out.
  Nothing needs them, since Fabric connections get their credentials in Plan.
  The connection check names each parameter this was done to.
* **Uploads and working copies** hold a client's definitions, so they are kept
  only while the API process that made them runs. Each is marked with that
  process (`<folder>.owner`). The next start removes the ones whose process has
  ended (a restart, or `--reload`), and leaves those of an API still running on
  another port. A new ZIP replaces the earlier upload.
* **Optional SQL pool:** the repository holds definitions only. To also
  migrate tables and data, the dedicated SQL pool is read from Azure (for Git,
  the one chosen in the pool list; for a ZIP, tick *Also read the dedicated SQL
  pool* and sign in); the definitions still come from the repository. Without
  it, tables, data loads, date filters and row counts are skipped.

Triggers and integration runtimes are read from the repository's own
folders. Spark pools and libraries are not in a repository, so they appear
only when the SQL pool's workspace is signed in to.

### Migration: the seven steps

The seven steps sit side by side at the top, each a box with its key number
(objects found, % direct, the brief's version, waves, readiness, objects migrated, matches) and its
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
| Consult | The brief (its version, or *not approved*), answered, assumed, needing a person, contradictions | Questions (by section, with **Accept all recommendations**, proposed answers to confirm, and the contradictions first), Brief (the draft, **Submit the brief**, approve or reject the version waiting, each version as Markdown or JSON), Changes since approval, Help (read answers from notes, the questionnaire) |
| Waves | Objects, dependencies, waves | Waves (one row per wave with its object types), Dependency graph |
| Plan | Readiness, objects, automated, needs a person, effort, blocking; how the plan stands against the approved brief | Readiness & risks, Objects in plan, Strategy by type, Stages & credentials (configuration only: nothing runs from Plan; an option that differs from the brief is marked) |
| Migrate | Pre-run checks, then the run: every stage that is on or a single stage, progress by wave, every object's result, pause, resume, Retry failed; the **sign-off** card when the run waits for one; why a run is paused (by you, at a wave with failures, or cut short by a restart) and the credentials to enter again; objects **blocked** by a failure they depend on; the data-pipeline runs it started in Fabric; once it completes, the commit to Fabric Git with **Open pull request dev → main**, or **Commit now**; and the **run history** | — |
| Validate | Checks, match, review, mismatch (each one filters) | One tab per category |

The bar at the foot of every step says where it stands and how to move on:
*Looks good, continue to …* once the step is finished, *Continue to … anyway*
when it finished with something worth a look (warnings, failures, blocking
risks), and a disabled button with the reason while it is not finished. Plan
cannot be confirmed while a risk is blocking, and confirming a plan that differs
from the approved brief asks why. Under a required consultation, Consult is
finished only once a brief that fits the estate is approved. Confirming the last step shows
*Migration complete*: the final numbers, said plainly when objects failed or
checks did not match, and **Download report (CSV)** with every object's result
and every validation check.

**Starting a step over.** Every step has **Reset step** in its header. It
clears that step and every step after it (the discovered inventory, the plan
with its options and typed credentials, the current run, which is closed and
kept in the run history, the validation results, and their confirmations),
then lets you do them again from the start. A dialog lists exactly what will be cleared first, and says what is not
touched: the connections, everything in Synapse, and anything already created
in Fabric (a new run skips what exists; nothing is deleted). Reset is refused,
with the reason, while discovery, a run or a validation is working. Running
discovery again from Discover asks first when later steps are confirmed.

**Results that disappear.** Confirmations are kept per project in the
browser; the discovery, the run and the last validation are kept per project by
the backend, and come back after a restart. When a confirmed step's results are
gone (the source was signed out, a step was reset, or Demo data was reloaded),
its box says *Run again* (*Rebuild* for an empty plan), the steps after it wait,
and *Migration complete* is hidden until it has been redone; the later steps
then come back by themselves.

**Workspace explorer.** Beside the steps, the discovered workspace is shown as
a repository tree: artifacts in the folders a Synapse Git repository uses
(`linkedService`, `dataset`, `pipeline`, `notebook`, `sqlscript`,
`sparkJobDefinition`, `trigger`, `integrationRuntime`, …), the dedicated SQL
pool nested by schema (tables, external tables, views, procedures, functions),
and anything else under *other*, so every object appears once and the total
matches Discover. Folders show counts; a name filter keeps only matches;
clicking an object opens its details. *Hide explorer* is remembered per
browser; below 1100 px wide it sits above the steps.

**Release path.** Under the steps, one strip shows where this migration sits on
the way from Dev to Prod: the Synapse source (its environment, and branch or
ZIP), this migration, the Fabric workspace, **Fabric Git**, and the
environments still ahead (none after Prod). The Fabric Git chip is read live
from the target workspace: *dev @ 1a2b3c4 · 0 uncommitted* with a **Pull
request dev → main** link beside it, or *Not connected: connect the Dev
workspace to Git in Fabric*. *What comes next* opens the one-time migration and
the ongoing CI/CD flow, a six-item checklist kept per project in this browser
(its Git row follows Fabric once Fabric can be read) and a short glossary.
Test and Prod stay *to do*: whether they were deployed is not something the
app can see yet.

**Activity.** Under the step, **Show activity** opens the project's audit
trail, newest first: who did what (a person, the LLM, or the accelerator),
to what, when and how it ended, filtered by kind of action and by who did it.
**Check the trail** walks its hash chain and says whether any record was
changed, removed or moved; **CSV** and **JSON lines** download it (the JSON
lines keep the hashes, so the trail can be checked elsewhere).

### Migration assistant and the execution plan

The round **+** button at the bottom right (or **Ctrl+I**) opens the
assistant, docked on the right like an editor's chat; the number on the button
is how many of the agent's requests wait for you. Ask it why something failed,
what is left to do, or where the migration stands: it answers from the
accelerator's own record (the run, its diagnosis, the execution plan). With
consent in an approved brief (D1) it asks Claude, one checked step at a time, and
anything it would change is an **action card** you confirm or reject; without it,
it answers the questions it knows by its rules. Its **Plan** tab is the execution
plan as a checklist, and **Requests** lists what waits for you.

**Migrate → Execution plan** is the plan board: every step from the readiness
checks to the sign-off report, who owns it (a person, the agent, the
accelerator), the brief answers it came from, and what the agent proposes. A
person marks their own tasks done, pauses or resumes the agent (the kill switch),
and downloads the **sign-off report**. See
[The orchestrator](#the-orchestrator-execution-plan-agent-and-assistant).

The layout works from wide screens down to phones: on a phone the top bar
takes two rows, the step strip swipes sideways with the selected step kept in
view, and the action bars sit at the end of the page.

### Top bar

Connections and Migration (Migration opens once a source is connected), a
status chip for each side, the **project menu** (switch projects, or create,
rename and delete one; each project is the backend's, with its own discovery,
Fabric target, runs and audit trail, while its plan, stage options and
confirmations are kept in this browser; a switch is refused while something of
the current project is still working), **Export** (the discovered inventory as JSON),
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
    ├─ migration/            planner, stages, T-SQL rules, Fabric REST, data pipelines, validation, Fabric Git
    ├─ consult/              the questions, the migration brief, the policy, MIGRATION_CONTEXT.md
    ├─ orchestrator/         the execution plan, diagnosis, the tool registry, the assistant's task loop
    └─ state/                projects, run records, sign-off, audit trail, logs and correlation ids
             │               (.accelerator/state/projects/<project>/, .accelerator/logs/)
             │
             ▼
 Microsoft Fabric: REST API, Warehouse SQL endpoint, data pipelines, Git integration
             │  (commits made by Fabric, to the branch the workspace is connected to)
             ▼
 Azure DevOps / GitHub: the Fabric repository (dev → pull request → main)
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
| `src/discovery_agent/migration/` | The planner, the eleven stages, the T-SQL rules, Fabric REST calls, data pipelines, validation, `fabric_git.py` (the target workspace's Fabric Git: status, commit, pull-request link), `run_state.py` (the statuses of a run and its objects, and the moves between them) and `failures.py` (what kind of failure an object's error is). See [Migration](#migration) and [Fabric Git (Dev)](#fabric-git-dev). |
| `src/discovery_agent/consult/` | The consultation: `catalog.json` (the questions, as data, and their history) and `catalog.py` (checks it at load, reads older answers under new ids), `conditions.py` (when a question applies), `facts.py` (what the estate facts are), `settings.py` (the settings a brief can set, and who acts on each), `engine.py` (where every question stands), `brief.py` (versions, hashes, Markdown), `policy.py` (what every consumer reads) and `context_file.py` (MIGRATION_CONTEXT.md). See [Consultation and the migration brief](#consultation-and-the-migration-brief). |
| `src/discovery_agent/orchestrator/` | The orchestrator: `plan.py` (the execution plan, and where each step stands), `diagnosis.py` (what each failure allows), `tools.py` (the tool registry and its tiers) and `assistant.py` (the task loop and its fixed intents). See [The orchestrator](#the-orchestrator-execution-plan-agent-and-assistant). |
| `src/discovery_agent/state/` | What the backend keeps and records: `store.py` (a project's files: versioned documents, append-only streams, blobs, leases), `projects.py`, `runs.py` (run records), `approvals.py` (sign-off), `audit.py` (the hash-chained trail), `context.py` (correlation id and actor), `logs.py` (JSON logs), `processes.py`. See [Projects, run records and the audit trail](#projects-run-records-and-the-audit-trail). |
| `src/discovery_agent/api/` | The local HTTP API: `service.py` (connections, discovery, results), `fabric.py` (Fabric sign-in), `consult.py` (the consultation, the brief, the LLM gate, the migration context), `orchestrator.py` (the plan board, requests, the supervisor, the report, the assistant), `migration.py` (plan, run, sign-off, run history, validate), `projects.py` (projects, the audit trail), `mapping.py`, `server.py`. |
| `prompts/transform/` | The prompt templates for LLM-assisted transformation: `base.md`, one file per object type, and `project.md` for your project's own rules. |
| `transforms/` | Drafts and accepted LLM-assisted transforms, one JSON file per object (created on first use; git-ignored, because it holds the client's code). |
| `tests/` | The backend test suite; runs offline. |
| `docs/` | [discovery.md](docs/discovery.md), [p0_source_strategy.md](docs/p0_source_strategy.md), [unified_discovery_record.md](docs/unified_discovery_record.md), and the *Synapse to Fabric Transformation Guide* (Word): what changes between Synapse and Fabric, what is automated and what needs a person. |
| `input/` | Where Git repositories are cloned for discovery (gitignored). |
| `.accelerator/` | Created by the API (gitignored): `state/` (the projects and what each keeps) and `logs/api.jsonl`. |
| `start-ui.ps1` | Starts the API and the UI together on Windows. |

## API

All under `/api`, JSON in and out. Nothing returns a token or a secret.

| Area | Endpoints |
|---|---|
| Health | `GET /health`: the sign-in methods this backend supports, what discovery reads, and the types a run migrates |
| Synapse connection | `GET /connections` · `POST /connections/authenticate` · `POST /connections/test` · `POST /connections/upload` (a ZIP, `application/zip`) · `POST /connections/repository` (Git or the uploaded ZIP, for one environment) · `DELETE /connections` · `GET /azure/<subscriptions, resource-groups, workspaces, sql-pools>` for the dropdowns · `GET /azure/workspace-git?subscriptionId=&resourceGroup=&workspace=` (the workspace's Git integration: `configured`, `provider`, `repositoryUrl`, `branch`, `rootFolder`, `lastCommit`; never a credential) |
| Discovery | `POST /discovery/start` · `GET /discovery/status` · `GET /discovery/results` (filter, sort, page) · `GET /discovery/results/<id>` · `GET /discovery/export` · `DELETE /discovery` (forget the results; refused while discovery runs) |
| Mapping and waves | `GET /mapping/components` · `GET /dependencies` |
| Fabric connection | `GET /fabric/connection` · `POST /fabric/authenticate` · `POST /fabric/workspaces` · `POST /fabric/test` · `DELETE /fabric/connection` |
| LLM transformation | `GET /transform/settings` · `GET /transform` · `GET /transform/objects/<id>` · `GET /transform/prompt/<id>` · `POST /transform/generate` (`ids`) · `POST /transform/accept` (`id`, optional edited `code`) · `POST /transform/discard` (`id`, `scope`: `draft` or `all`) · `GET /transform/candidates` · `GET`/`POST /transform/project` |
| Consultation | `GET /consult` (every question as it stands, the contradictions, what is next, the brief, proposals, the LLM gate) · `GET /consult/catalog` · `POST /consult/answer` (`question`, `value`, `note?`; `preview: true` says what it would change without recording it) · `POST /consult/skip` · `POST /consult/withdraw` · `POST /consult/accept` (`section?`, `questions?`) · `POST /consult/resolve` (`conflict`, `resolution`: `take` or `keep`, `reason`) · `GET /consult/brief` · `POST /consult/brief/submit` (`note?`) · `POST /consult/brief/approve` (`version`, `decision`: `approve` or `reject`, `comment?`, `hash?`; a person only) · `GET /consult/brief/export?version=&format=md|json` · `GET /consult/policy` (what Plan pre-sets: run options, date filters, objects left out, names, sign-off, Validate's depth) · `POST /consult/deviation` (`items`, `options`, `reason`) · `POST /consult/propose` (`text`) · `POST /consult/proposals` (`ids`, `decision`: `confirm` or `reject`) · `POST /consult/explain` (`question`) · `GET /consult/questionnaire?format=md|json` · `POST /consult/questionnaire` (`answers`) · `GET /consult/context` (MIGRATION_CONTEXT.md as every draft is sent it now) · `GET /consult/context/<sha256>` (one a draft was made with) |
| Orchestrator and assistant | `GET /orchestrator/plan` (the execution plan: every step with its status, the open requests, the agent) · `POST /orchestrator/steps/mark` (`step`, `done`, `note?`; a person only) · `GET /orchestrator/actions?status=open|all` · `POST /orchestrator/actions/decide` (`id`, `decision`: `confirm` or `reject`, `note?`, `reason`; a person only) · `POST /orchestrator/supervisor` (`action`: `pause`, `resume`, `wake`) · `GET /orchestrator/tools` · `GET /orchestrator/diagnosis/<id>` · `GET /orchestrator/report?format=md|json` · `GET /assistant/tasks` · `GET /assistant/tasks/<id>` · `POST /assistant/ask` (`text`, `taskId?` to reply) · `POST /assistant/tasks/<id>/cancel` · `POST /assistant/tasks/<id>/continue` |
| Migration | `GET /migration/capabilities` · `POST /migration/plan` (`credentialNames`: which linked services have credentials; a request carrying the credentials themselves is refused; `record` keeps it as a plan version) · `POST /migration/start` (refused with `plan_blocked` while the planner finds a blocking risk, and under a required consultation with `brief_required`, `brief_stale` or `deviation_unexplained`; `deviationReason` explains a difference from the brief; a run that changes what Fabric has answers `awaiting_approval`) · `POST /migration/approve` (`decision`: `approve` or `reject`, `comment`, `subjectHash`; a person only) · `GET /migration/run` (with `status`, `pausedReason`, `needsInput`, `passes`, `approval`, `executions`, and `git`: the commit after the run) · `POST /migration/control` (`pause`, `resume`, `retry`, `reset`: close the run, keeping its record; `version` refuses a page that read an older run; `credentials` for the connections a restart lost) · `GET /migration/runs` · `GET /migration/runs/<id>` · `GET /migration/runs/<id>/events?after=&limit=` · `GET /migration/runs/<id>/artifacts/<sha256>` (what the run sent to Fabric for an object) · `GET /migration/plans/<P001>` · `GET /migration/policy` · `POST /migration/validate` (starts it in the background) · `GET /migration/validation` (also the last one, after a restart) |
| Projects | `GET /projects` · `POST /projects` (`name`, `id?`, `version?`, `activate?`, `copyFrom?`) · `POST /projects/<id>/rename` · `POST /projects/<id>/activate` (refused with `project_busy` while something of the current project works) · `DELETE /projects/<id>?version=` (archives the folder) |
| Audit trail | `GET /audit` (newest first; `action` (a family such as `run`), `actor` (`person`, `system`, `agent` or `kind:id`), `outcome`, `run`, `since`, `until`, `correlationId`, `limit`, `before` to page back, `project` for another project's trail) · `GET /audit/verify` · `GET /audit/export?format=jsonl|csv` · `POST /journey/confirm` (`step`, `note?`) |
| Fabric Git | `GET /fabric/git` (the target workspace's branch, head, uncommitted / incoming / conflicting items, data-load pipelines kept out, the pull-request link, the last commit) · `POST /fabric/git/commit` (`{comment?}`; commits what the current run created or updated, in the background; refused with no run, or while a run or another commit is working) |
| Errors | Every error is `{"error": {"code", "message", "correlationId"}}`. An unexpected one is a 500 with `code: internal_error` and a `reference`; the traceback is in the API's log under that reference, redacted, and never in the response. A change refused because what it was based on moved on is a 409 with `code: stale`: read again and retry |

## What is automated, manual, or coming

**Automated by a migration run:** Warehouse (one per SQL pool) and schemas;
tables as empty structures with types mapped, keys recreated `NOT
ENFORCED`, **IDENTITY kept** (as `bigint IDENTITY`, existing key values loaded
with `IDENTITY_INSERT`, then reseeded) and **dynamic data masks** in the
`CREATE TABLE`; scalar and inline table-valued **functions**, views and stored
procedures after the T-SQL rules; database **roles, role members and GRANT /
DENY permissions** (object, column, schema and database level) for roles and
Microsoft Entra users; table data
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
views and `sys.pdw_*` system views; multi-statement table-valued functions
(Fabric does not support them); **SQL-authenticated users** (Fabric has no SQL
authentication: Plan lists the Microsoft Entra principal to choose and the
grants to give it); resource-class roles, networking and workload
management; lake databases (not discovered yet); SQL scripts and pipelines written for Synapse's **serverless** pool,
which Plan reports as `SERVERLESS_SQL` (the Lakehouse shortcut and its SQL
endpoint replace them; leave them out of the plan).

**Storage read directly by code is routed through OneLake.** Code that reads
or writes `abfss://…@<account>.dfs.core.windows.net` or
`https://<account>.dfs/.blob.core.windows.net` ran in Synapse as the
workspace's managed identity. In Fabric it runs as the signed-in user. A storage
account in another Microsoft Entra tenant than Fabric rejects that user whatever
roles it has (401 / "Cannot find the CREDENTIAL"), but a Fabric connection with
an account key or SAS still reaches it. So Plan → Stages & credentials →
**External storage** lists each storage location the code uses, with the
connection that can reach it, ticked by default. A routed location gets a
OneLake shortcut named after its container under `Files` in the run's Lakehouse,
and every path to it is rewritten to go through that shortcut:

* notebooks, SQL scripts, views and procedures: literal paths, f-string paths
  (`f"abfss://{container}@{account}.dfs.core.windows.net/…"`), and T-SQL that
  builds the host (`'https://' + @Account + '.dfs.core.windows.net/'`);
* Spark job definitions: the job runs a copy of its main file, saved in the
  Lakehouse under `Files/_migrated_jobs/` with the paths in its code rewritten.
  The original stays where it is, for Synapse.

Plan reports each object as `STORAGE_ROUTED`, or `EXTERNAL_STORAGE` when it still
reads storage directly. Pipelines that run such an object are reported as
`DEPENDS_ON_STORAGE`.

**Converted by the T-SQL rules**, also inside dynamic SQL: `COPY INTO ...
CREDENTIAL = (IDENTITY = 'Managed Identity')` loses the credential, so it reads
as the identity that runs it. `SET IDENTITY_INSERT` is kept, because tables keep
IDENTITY; it is removed only for a Warehouse table that has none (one created
before IDENTITY was kept), where Fabric would reject it.

**Automated around the run:** filling in the Git source from the Synapse
workspace's own Git integration; committing what the run created to the Fabric
workspace's Git branch, with the pull-request link (see
[Fabric Git (Dev)](#fabric-git-dev)).

**Set up by hand, once:** the Fabric repository and its branches, the Fabric
tenant setting for Git, and connecting the Dev workspace to Git (see
[Fabric Git (Dev)](#fabric-git-dev)).

**Coming next:** deploying from Git to Fabric Test and Prod (with
`fabric-cicd`): each environment's workspace, connections, Warehouse schema
and data loads, and schedules switched on only where they should run; and the
orchestrator's **learning loop**: a fix a person accepts for a statement Fabric
rejected becomes a deterministic T-SQL rule (admitted only once its example
statements pass), so the next estate converts it without a model call.

**Shown in the UI, not built yet:** sources and destinations other than
Synapse and Fabric.

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

## Consultation and the migration brief

Before anything is planned, the **Consult** step asks the questions a consultant
asks a client before a migration, and turns the answers into a **migration
brief**: every decision, who made it and why, the settings it implies, and the
estate it rests on. A person approves the brief. Plan then starts from it, and
every run is checked against it.

### The questions

The questions are data, not code: `src/discovery_agent/consult/catalog.json`
(version 2.0.0: 61 questions and 10 contradictions, in sections lettered as a
SQL Server → Databricks consultation letters them: **A** scope and approach, **B**
target and standards, **C** data loading, **D** code conversion and AI, **E**
environments and operations, **G** security and governance, **H** validation and
cutover, **I** context for the team and the AI). Each says when it
applies (a condition over what discovery found and the answers before it), what
it recommends and why (ordered rules whose reasons quote the estate, such as
*4 objects use the serverless pool*), which settings it drives, and whether a
person must answer it. The catalog is checked when the API starts and refused
whole if anything is wrong: every setting and fact it names exists, a condition
reads only earlier questions, every contradiction can be resolved, and accepting
every recommendation gives a brief that can be submitted.

A question appears only when the estate makes it relevant: the data questions
when there are tables, the SQL-user question when SQL users were discovered, the
Git questions when the Fabric workspace is connected to Git, the type question when
a table has a column Fabric stores with less. What a question
rests on is a **fact**, worked out once per discovery from metadata only (counts,
names and short lists, never a definition, a row or a credential):
`consult/facts.py` names them, `api/consult.py` (`estate_facts`) works them out.

| Status | Meaning |
|---|---|
| Answered | A person answered it, accepted its recommendation, took a contradiction's finding, or confirmed a proposed answer. The brief says which. |
| Assumed | Nobody answered: its recommendation stands, and the brief lists it as an assumption. |
| Open | It has no recommendation and needs an answer. |
| Skipped | A person moved past it: the recommendation stands, as an assumption. |
| Confirm again | What the answer rests on changed since it was given (a new discovery): a person confirms it, or changes it. |
| Parked | Answered on a branch that no longer applies. The answer is kept, and comes back if the branch applies again. |

**From the SQL Server → Databricks question list.** Most of its questions are
common to any migration and are asked here as they are; some are about Databricks
and are asked the way Synapse → Fabric needs them; a few have nothing to act on:

| That list | Here | How |
|---|---|---|
| A0 what moves, A2 schemas and tables, A5/A5b history from a date, A6 one cutover or waves | A0, A2 (A2I, A2X), A5/A5D, A6 | As they are; A6's waves are the run's dependency waves, signed off one by one if chosen |
| A1 each source: migrate, ingest or federate | A1 | Federating is a OneLake shortcut: storage the code reads is reached in place, or left as it is |
| A3 procedures, views and functions converted | A3, plus A3S | Bring them across, or move the tables first; A3S is Synapse's serverless SQL pool, which Fabric does not have |
| A4b SSIS | A4 (A4O) | Pipelines with an activity Fabric lacks (an SSIS package, a mapping data flow) and .NET notebooks: rebuilt by hand, or left out |
| B2/B4 schema per layer, B3 flatten schemas | not asked | A lift to a Fabric Warehouse keeps every Synapse schema as it is; there are no medallion layers to map |
| B6 naming convention | B6, plus B1, B2, B2L, B3 | Naming for anything new a draft creates; the Warehouse's collation (set once), the Warehouse and Lakehouse names, coding standards |
| C1 full reload or incremental, C2 watermark column, C3 tables without a key, C4 deletes, C6 parallel reads | C1, C2 (C2N), C3, C4, C8, plus C5, C6, C7, C9 | Sync after the first load; the change column detected or named; a keyless table loaded once or appended to; deletes kept, or the synced tables reloaded at cutover; tables copied at once (the load pipeline's batch). Also the sync's re-read window, replacing rows, running the loads, and how the pool connection signs in |
| D1 notebook language | D7 | Only .NET notebooks need a language (Fabric Spark has no .NET): PySpark or Scala |
| D2 AI may write code, G4 metadata to the AI, G5 AI budget | D1, G4, D2 | Consent to send definitions; what goes with each draft (the whole migration context, or only its decisions); the budget |
| D4 DECIMAL precision | D4 | Fabric's own precision limits: time keeps 6 fractional digits, datetimeoffset its offset no more, rowversion stops changing; accepted, or reviewed before the data loads |
| D5 procedural T-SQL by AI, D6 which objects AI translates | D5, D6, plus D3 | Code Fabric rejects after the rules: drafted with AI or rewritten by hand; drafts only for what the rules cannot convert, or any object; multi-statement functions (Fabric has none) |
| E0 delivery, E1 target roles, E2 user or service principal, E4 sign-off, E5 jobs paused, E6 autonomy, E7 promotion | E0 (E0S), E1, E2, E4, E5 (E5O), E6, E7 | Delivery is the commit to the Fabric workspace's Git branch; a production target signs off every run; schedules created switched off, or as they were |
| E8 parallel loads | C8 | One load pipeline per wave copies its tables in parallel; runs themselves go wave by wave |
| F1 source structure intake, F2 live extract | not asked | Chosen on Connections: the live workspace, its Git repository, or a ZIP export |
| G1 PII columns, G2 grants, G3 residency | G1 (G1M), G2 (G2U), G3 | Sensitive columns kept out of logs and copies by every draft, and masked if chosen; roles and permissions, with each SQL user mapped to a Microsoft Entra principal; the capacity's region checked |
| H1 output comparison, H2/H2a parallel run, H3 cutover window, H4 test copy | H1, H2 (H2A), H3, H4 | Validate's depth; side-by-side cycles before cutover; the window; whether pipelines write to production systems, which keeps Validate from running them (K10) |
| H5, H5a, H5b comparison tolerances | not asked | Validate compares definitions, columns, types and row counts, not values: there is nothing a tolerance would apply to yet |
| I1–I4 timeline, pain points, consumers, team skills | I1–I4, plus I5 | Sent to the AI with each draft, in the migration context; I5 (who approves) is kept for people only |

**Answers given before the questions changed.** Every answer records the catalog
version it was made against. The catalog's `history` says which question each
old id is now (version 1.0.0's A1, what moves, is A0; its G3, the sign-off, is
E4), so answers given before are read under the questions they answer now, and
nothing is lost. A brief approved against an earlier major version of the
questions never answered those added since: it is stale (*The consultation's
questions (catalog version): 1.0.0 → 2.0.0*) until it is submitted and approved
again.

**Accept all recommendations** answers every assumed question with its
recommendation in one go, the two a person must answer included (D1, whether
definitions may be sent to Claude, and E4, which runs need a sign-off); each
section has its own button, and **Use the recommendation** answers one question. A choice says what it would change before it is saved
(*This answer opens A2I; plan.scopeMode: all → include*), and **Explain** says
what a question decides for this estate.

**Contradictions.** Some answers contradict the estate or each other: replacing
rows while no run needs a sign-off, a case-sensitive Warehouse for a
case-insensitive pool, a production target with runs nobody signs off, running
pipelines in Validate when they may write to production systems, AI drafts without
consent. A
contradiction comes first and holds the brief back until a person **takes the
finding** (the safe answer, recorded as theirs), **keeps the answers with a
reason** (kept in the brief and the audit trail), or changes an answer. One kept
opens again if its answers or what it rests on change.

Every answer is an append-only record (`consult/answers`) with who, when, its
source, the catalog version and the evidence it was made against. Withdrawing an
answer is a new record; nothing is deleted.

### The brief

**Submit the brief** makes a new numbered version that never changes, with a
content hash over its decisions. A version is *submitted*, then *approved* or
*rejected*; approving one supersedes the one in force, and submitting supersedes
one still waiting. Only a person approves, and a version whose estate changed
since it was submitted cannot be: submit again. A decision that loosens a
safeguard (sending definitions to Claude, updating objects in place, replacing
rows, no sign-off) asks for an approver other than the submitter. Whether the
approver was the submitter is recorded either way, and
`MIGRATION_BRIEF_APPROVAL=separate` refuses such a version's self-approval. Each
version downloads as Markdown (for the client, and the final report) or JSON.

A version lists each decision with who made it and why (a recommendation's
reason only where the decision is that recommendation), the settings the
accelerator acts on and who acts on each, what only the AI acts on (sent with
every draft), the assumptions, what is kept for people (owners, the cutover
window), the contradictions kept with their reasons, and the facts it rests on
with their fingerprint.

**Freshness.** A new discovery whose facts differ makes the approved brief
**stale**: *Changes since approval* lists exactly what moved (*SQL Script 12 →
11*), answers whose own evidence changed are to be confirmed again, and the brief
is submitted and approved again. Only facts about the estate count: a target
that is briefly disconnected never stales a brief. One environment fact does: the
approval keeps which Fabric workspace was the target, and once another is connected
the brief is stale (*The Fabric target workspace: Sales WS → Sales PROD*), because
what it says about the target (E1, what it is for) was about the one it was
approved for.

### What the brief changes

One policy object (`consult/policy.py`), built from the approved brief, is what
every consumer reads; no code outside the consultation reads a question or an
answer.

| The brief decides | Where it takes effect |
|---|---|
| What moves (structure, data, orchestration), and whether roles and permissions do | Plan's stages are pre-set |
| What is in scope (schemas and folders, or name patterns left out), and leaving out serverless-pool code, multi-statement functions and what must be rebuilt | Plan's **Add all** leaves those objects out, each with why |
| How a run goes through the waves | Stop at a wave with failures, or a person **signs off each wave** before the next one starts (the run pauses, `awaiting_wave_signoff`; Resume, by a person, releases that wave and is recorded as `run.wave_signed_off`) |
| Objects already in Fabric, the Warehouse collation, tables that already have rows, whether load pipelines run, the sync re-read window | Plan's stage options are pre-set |
| How much history loads, and whether tables stay in sync | Each table's date filter is pre-set (a brief that decides them decides every table: none means none) |
| Storage the code reads directly | Routed through OneLake shortcuts, or left |
| The Warehouse's and the Lakehouse's names | The run creates them under those names; Validate compares against them |
| The Microsoft Entra user or group that replaces each SQL user | The SQL user is migrated as that principal: its role memberships and its grants go to it |
| Commit each run to Fabric Git, and what the commit takes | Only what the run made (the default), or everything waiting in the workspace |
| Which runs a person signs off | The sign-off rule, which never loosens one this machine is set to (`MIGRATION_SIGNOFF`): the stricter holds |
| Whether definitions may be sent to Claude, and the budget | Every LLM call is refused without consent, and once the budget is spent |
| Views, procedures and functions now, or the tables first (A3) | Plan's **Add all** leaves them out for a later run |
| Which column marks a changed row, and tables with no key (C2, C2N, C3) | Each table's sync follows the named column (first found); a keyless table is loaded once, or appended to |
| How many tables load at once (C8) | The load pipeline's batch (1 to 50) |
| Schedules switched off, or as they were in Synapse (E5) | Schedules are created that way; on only where the Synapse trigger had been started |
| Sensitive columns, and masking them (G1, G1M) | Each matching column without a mask gets the default dynamic data mask; drafts keep them out of logs and copies |
| What the target workspace is for (E1) | Production signs off every run, and the agent never acts alone there |
| Which objects may be drafted with AI, and what goes with each draft (D6, G4) | A draft for an object the rules convert is refused (`llm_scope`); the migration context in full, or only its decisions |
| Deletes, reviews, rewrites by hand, residency, identity, parallel runs (C4, D4, D3, D5, A4, G3, E2, H2) | Steps of the execution plan: a person's task, or a check (see [The orchestrator](#the-orchestrator-execution-plan-agent-and-assistant)) |
| What the assistant may do alone (E6) | Observe, suggest (the default), or act on a development workspace |
| Naming and coding standards, the notebook language, the context answers (B3, B6, D7, I1–I4) | Sent to the AI with every draft, in `MIGRATION_CONTEXT.md` |
| How deep Validate checks | Validate's checks are pre-set |

Plan is pre-set **once for each set of decisions**, so a person's later changes
are never overwritten, not even by a brief approved again only because the
estate moved. Where the plan differs from the brief, Plan says how (*Stop at a
wave with failures: the brief says yes, the plan has no*) and marks each option
that differs, and confirming Plan asks **why**. The reason is kept in the audit
trail (`plan.deviation`) and with the run, and covers the same differences until
the decisions change.

**Consultation mode** (`MIGRATION_CONSULTATION`). `required`, the default,
refuses a run without an approved brief (`brief_required`), under one that no
longer fits the estate (`brief_stale`), and one that differs from it with no
reason given (`deviation_unexplained`; the reason can also be given at Start).
`advisory` refuses none of these; an approved brief is still pre-set and recorded
with each run. Every run keeps the brief it started under (`pins.brief`: its
version and hash, the differences, and the reason).

### The migration context (MIGRATION_CONTEXT.md)

Every LLM draft is sent the approved brief written for the model: the
**migration context**, made by `consult/context_file.py` from the policy in force.
It holds the decisions that shape code, each as an instruction (*The Warehouse
compares names and text case-insensitively, as the Synapse pool did*; *Storage the
code reads directly is reached through OneLake shortcuts*; *These columns hold
personal or sensitive data …*), then the coding standards and the naming
convention as they were written, then the context answers. It goes in the prompt
fenced off, after the project requirements, and wins where the two disagree.

* **Referred to by each draft.** A draft records the file it was sent: its content
  hash, the hash of its decisions alone, the brief version and whether it was
  minimal (G4). The model lists, in its answer, each instruction of the context the
  draft applies; the review shows them. A draft or an accepted transform made with
  other decisions than the ones in force is flagged (*Made with an earlier migration
  context*): draft it again to follow the brief as it is. A brief approved again with
  the same decisions, or with others that never reach the model (who signs runs off,
  say), leaves every draft current. `transform.drafted` in the audit trail carries
  the context's hash.
* **Read and kept.** **Consult → Migration context** shows the file every draft is
  sent now (`GET /consult/context`); each one a draft was made with is kept in the
  project's `blobs/` by its hash and downloads with `GET /consult/context/<sha256>`.
  The same brief makes the same file, byte for byte.
* **Minimal (G4).** Only the decisions: no context answers, and no names of
  sensitive columns or Microsoft Entra principals; the objects a draft reads are
  named without their columns.

### Help: notes, explanations, a questionnaire

* **Read answers from notes.** Paste meeting notes or an email (*the last 3
  years, and every run must be signed off*): the answers the text gives plainly
  are proposed, each with the words it was read from, and nothing is answered
  until a person confirms each one. With consent (D1) Claude reads the notes;
  what is sent is the text pasted, the questions, and counts and yes/no facts
  about the estate, never a name from the estate or a definition (a
  recommendation goes only as a value of the catalog's own, never its reason).
  Otherwise built-in rules read them.
* **Explain** says what a question decides for this estate: Claude, with
  consent, or the catalog's own words.
* **Questionnaire.** Download the questions (Markdown to read, JSON to fill in)
  for someone without access; importing the filled JSON proposes each answer for
  a person to confirm.

**The LLM gate.** Consent (D1) and the budget (D2) are what the approved brief
says, with two exceptions that only make the gate stricter and so take effect at
once: a person withdrawing consent, or lowering the budget. What every call cost
is kept in the project's `llm/spend` ledger; once the spend reaches the budget the
next call is refused (the call that crosses it can go over by its own cost).
Before any brief is approved, the consultation's own help follows a person's
latest answer to D1; once one is, the brief decides there too, and a later "yes"
loosens nothing until a brief with it is approved. The assistant's task loop uses
the model only with an approved brief's consent.

**Demo data** runs the same consultation in the browser:
`frontend/src/mock/consultCatalog.json` is a copy of the catalog and
`consultVocabulary.json` of the settings and facts (backend tests keep them the
same), and `frontend/src/mock/mockConsult.ts` a TypeScript copy of the engine,
the brief, the policy and the migration context, checked against the server's on
the cases `tests/test_consult.py` writes to `frontend/src/test/consultParity.json`.
Demo data reads notes with the rules only: nothing is sent anywhere.

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
| Warehouse & schema | Dedicated SQL pool, schema, table, function, view, stored procedure | A Warehouse of the same name, its schemas, **empty** tables, functions, views and procedures | Structure only. Tables are rebuilt from discovered columns; storage clauses are dropped and unsupported types mapped, each change noted; primary keys and unique constraints are recreated `NOT ENFORCED`; an IDENTITY column stays IDENTITY as `bigint IDENTITY` (Fabric supports no other type, seed or increment); a column's **dynamic data mask** is part of its `CREATE TABLE`, so no row ever lands unmasked (a masking function Fabric lacks stops the table, before any data). A table that already exists gets the masks it is missing. Functions (scalar and inline table-valued; multi-statement ones are left for a person), views and procedures are created from their own text after the T-SQL rules (below). The Warehouse **collation** is chosen once, at creation: *same as Synapse* (default), case-insensitive or case-sensitive. |
| Table data | The rows of each migrated table | **Fabric data pipelines**, one per wave, and the rows they load | Data never passes through the accelerator. Each wave gets a metadata-driven pipeline (`load_<warehouse>_wave_<n>`): a ForEach over a `tables` parameter whose Copy activity reads the pool with a typed SELECT through a Fabric SQL connection and writes the Warehouse with the COPY command. A table with an IDENTITY column is copied into a staging table without IDENTITY (`migration_load.<schema>__<table>`), then an *After copy* Script activity moves the rows in with `SET IDENTITY_INSERT ... ON` and reseeds with `DBCC CHECKIDENT ... RESEED`, so the Synapse key values are kept (Microsoft's documented path); the staging table carries the same masks and is emptied afterwards. *Create and run* (default) runs it, waits, and compares row counts table by table; *create only* leaves running it to you. *Skip* (default) leaves tables that already have rows; *replace* truncates them inside the pipeline first. The connection to the pool is reused if one exists, or created from what you choose on this stage (workspace identity, Key Vault, SQL login or service principal). Per-table **date filters** load part of a table and can keep it in sync afterwards: see below. |
| Roles & permissions | Database role, user (Security Object) | Warehouse roles, their members, and GRANT / DENY permissions | Custom roles get `CREATE ROLE`; built-in roles (`db_datareader` …) and `public` exist already, so only their members and permissions move. Members are added with `ALTER ROLE … ADD MEMBER`; permissions on the database, schemas, objects and columns are granted or denied as they were (`WITH GRANT OPTION` kept). Fabric has no `CREATE USER`: a Microsoft Entra user is created by its first GRANT. Not carried: the database `CONNECT` every user holds (the workspace gives access in Fabric), resource-class roles (no workload management in Fabric) and **SQL users**, which are listed for a person with the grants to give their Microsoft Entra replacement. A rejected statement fails the role with every reason; Retry Failed re-applies safely. Workspace roles and item permissions still decide who can connect. |
| Spark pool & environment | Spark pool | A custom Spark pool and a published Environment | Node size, autoscale, runtime mapped; libraries are not copied. |
| Notebooks | Notebook | Fabric Notebook | Cells kept; Spark pool binding and outputs dropped. Python cells: `mssparkutils` becomes `notebookutils`, and the Synapse SQL connector import and reads of the migrated pool are pointed at Fabric's connector and the Warehouse. Other Synapse-only calls (linked-service credentials, `mssparkutils.env`, connector writes, ADLS paths) are flagged. .NET notebooks are deferred. |
| Connections | Linked service | Fabric connection | Synapse does not give up the secret, so you choose how Fabric signs in. **Workspace identity** and **Key Vault reference** (an Azure Key Vault reference's alias or ID and the secret's name) pass no secret through the accelerator and are offered first; a SQL login, key, SAS token or service principal secret can still be typed (sent once, held in memory, never stored or returned). SQL, ADLS Gen2 and Blob are converted; a parameterised linked service uses its parameters' default values. Others are created by hand, with the linked service's name so pipelines find them. |
| Pipelines & datasets | Pipeline, dataset | Fabric data pipeline | Datasets are embedded in each activity; notebook, pipeline and Spark-job references become Fabric ids; anything that read the Synapse pool is pointed at the migrated Warehouse. Azure Function, Databricks, Batch (Custom), HDInsight, Machine Learning and Webhook activities keep their type and point at the connection of the same name. A pipeline with an activity that has no Fabric equivalent (for example a mapping data flow) is **not created**, with the activity names. |
| Spark job definitions | Spark job definition | Fabric Spark job definition | Main file, class, arguments and libraries kept; attached to the Environment of the same name. |
| SQL scripts | SQL script | A notebook with a T-SQL cell bound to the Warehouse | The T-SQL rules are applied; what they cannot convert is flagged for review. |
| Schedules | Schedule trigger | Pipeline schedule, **created switched off** | Minute, hour, daily and weekly recurrences. Event and tumbling-window triggers, and every-N-days or monthly, are recreated by hand. |
| External tables | External table | A OneLake shortcut in a Lakehouse | The storage location is read from the pool's external data source; needs a Fabric connection to that storage. |

Integration runtimes are always set up by hand.

A SQL user that the approved brief replaces with a Microsoft Entra user or group
(question E2) is migrated as that principal: the roles it was a member of get the
principal as a member, and its grants are made to it.

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
with Synapse's managed identity, and what Fabric Warehouse's T-SQL surface area
does not support: `SET ROWCOUNT`, `FOR XML`, synonyms, `PREDICT`, `CREATE USER`,
triggers).

* **Planner.** `POST /api/migration/plan` is read-only and deterministic: a
  strategy per object (Automated, Manual, Assess first, Later, Not selected),
  risks with a severity, effort, a readiness score, pre-run checks and a
  history of recorded runs. It reads the real definitions, so an unsupported
  column, an unreadable view, a pipeline activity with no Fabric equivalent or a
  linked service missing credentials is a finding *before* the run. It is told
  which linked services have credentials by name only (`credentialNames`).
* **A blocking risk stops the run before it starts.** Plan holds Start back
  while a risk is blocking, and the backend enforces the same rule: when a run
  starts, the planner runs again on exactly what that run was given, and a
  blocking risk refuses it (409 `plan_blocked`, naming the risks) before
  anything in Fabric is touched.
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
  keep that connection. Plan checks for it only when the plan has table data to
  load or external tables: a source connected without a dedicated SQL pool (a
  workspace or repository with no pool chosen) needs none, even with the Table
  data and External tables stages on.
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
| Tables | Column names and types (a type changed by design is a REVIEW that names it), and that every dynamic data mask is in place. |
| Data Count | Rows in Synapse against rows in the Warehouse; zero loaded is a REVIEW, a difference a MISMATCH. |
| Functions, Views, Stored Procedures | Present, and the definition text agrees (ignoring comments and whitespace). |
| Security | Each role exists with its members, and every permission given to a role or user is in the Warehouse with the same state (GRANT or DENY). |
| Spark | The custom Spark pool and Environment exist with the same node size and scale. |
| Notebooks, Pipelines | Present, with the same number of cells / activities (nested activities included). |
| Connections, Spark Jobs, SQL Scripts, Schedules, Shortcuts | Present in Fabric. |
| Manual | Integration runtimes and anything else set up by hand, listed for review. |

A check that could not run is a REVIEW with the reason, never a match. Row
counts need the Synapse connection; without it they are reported as unread. A
table loaded from a date is counted from that date: the date filters the request
names, or else the ones the run itself was given (what the agent's validations
use). Really running pipelines, notebooks, scripts and Spark jobs is refused while
the approved brief says they may write to production systems, or nobody knows (H4),
unless a person kept running them anyway when the brief was approved (K10).

**Does it run in Fabric?** Arriving is not working, so each object Fabric can run
is also checked there (`migration/runtime_checks.py`). The result is shown in the
**Runs in Fabric** column, and it decides the verdict: a failure is a **MISMATCH**,
and an object that matches but has never run is a **REVIEW**, because nothing
proves it works yet.

| Object | Checked by default (read-only) | Only when ticked in Validate |
|---|---|---|
| Tables, views | Queried in the Warehouse (`SELECT TOP (1) *`). | — |
| Stored procedures | Compiled without running (`sp_describe_first_result_set`). One using temporary tables can't be compiled ahead and is reported as not checked. | Executed inside a transaction that is rolled back, if it has no required parameters. |
| Pipelines, notebooks, SQL-script notebooks, Spark job definitions | Their latest run in Fabric's run history: succeeded, failed (with Fabric's reason), or never run. | **Run now in Fabric** and followed to the end, three at a time, up to the time limit you set. These are real runs: they read their sources and write their targets. |

Validation runs in the background: `POST /api/migration/validate` (with
`runtime: {probe, run: ["pipeline", "notebook", "script", "sparkjob", "procedure"], timeoutMinutes}`)
starts it, and `GET /api/migration/validation` reports its progress and then its rows.

API: `GET /api/migration/capabilities`, `POST /api/migration/plan`,
`POST /api/migration/validate`, `POST /api/migration/start` (`{items, options: {scope, stages, dataMode,
dataRun, collation, stopOnFailure, gitCommit}, credentials}`), `GET /api/migration/run` and
`POST /api/migration/control` (`pause|resume|retry|reset`). Code:
`src/discovery_agent/migration/` (`runner.py`, `stages.py`, `stages_fabric.py`,
`capabilities.py`, `planner.py`, `preflight.py`, `datacopy.py`, `datapipeline.py`, `tsql_rules.py`,
`fabric_connections.py`, `pipelines.py`, `jobs.py`, `notebooks.py`,
`environments.py`, `warehouse_ddl.py`, `fabric_rest.py`, `fabric_git.py`) and `api/migration.py`.

## Fabric Git (Dev)

The migration lands in a Fabric **Dev** workspace connected to Fabric's own Git
repository (not the Synapse one). After each run, what the run created is
committed to that workspace's branch, so every migration is reviewed in a pull
request before it reaches `main`. Deploying from Git to Test and Prod (with
`fabric-cicd`) is the next round and is not built yet.

```
Synapse Git (collaboration branch) ──discover──▶ this app ──migrate──▶ Fabric Dev workspace
                                                                         │ commit after each run
                                                                         ▼
                                     Fabric repository:  dev ──pull request + review──▶ main
```

**Set up once, by hand:**

1. **Azure DevOps** (or GitHub): a repository for Fabric, for example
   `fabric-migration`, with branches `dev`, `main` and `prod`. Protect `main`
   and `prod` with a pull request and a reviewer; leave `dev` without a policy,
   because Fabric commits to it directly.
2. **Fabric admin portal** (a tenant admin): enable *Users can synchronize
   workspace items with their Git repositories*.
3. **Fabric:** create the Dev workspace on a capacity, with you as Admin. Then
   *Workspace settings → Git integration*: Azure DevOps, the repository,
   branch `dev`, folder `fabric`, *Connect and sync*. Do this before the run.
4. **This machine:** `az login` as the same Microsoft account. For Azure
   DevOps, Fabric commits with that account (single sign-on; the accelerator
   sets the workspace's Git credentials to *Automatic* when they are missing),
   so no token is entered anywhere. A GitHub repository needs your GitHub
   account connected once in Fabric (*Source control → Accounts*); the
   accelerator never handles a GitHub token.

**After each run** (`api/migration.py`, `migration/fabric_git.py`, through
Fabric's Git REST APIs):

* When the run completes and **Commit to Fabric Git after the run** is on
  (Plan → *Stages & credentials*; on by default; run option
  `gitCommit: "auto" | "off"`), the accelerator reads the workspace's Git
  connection and status and makes a **selective commit** of the items **this
  run created or updated**, with the message *Synapse to Fabric migration
  run 00N: X object(s) created in &lt;workspace&gt;*. Those are its notebooks
  (SQL-script notebooks included), pipelines, Spark job definitions and
  Environments; the Warehouse when any of its schemas, tables, views,
  procedures, functions or roles changed; and the run's Lakehouse when it made
  shortcuts, routed storage, or gave a Spark job its default Lakehouse.
* **Left out on purpose:** the per-wave data-load pipelines
  (`load_<warehouse>_wave_<n>`) and the sync pipeline (`sync_<warehouse>`).
  They are rebuilt per environment, so they never go through Git. Deletions
  are not committed either, and neither is **anything else waiting in the
  workspace that this run did not make**: an edit made by hand in Fabric, or an
  item from outside the migration. Those are named in the result, *Left out 2
  other change(s) … (pl_load, pool01)*, to be committed from Fabric's Source
  control panel if they belong in Git.
* **Stops and says so** rather than forcing anything: when the branch has
  changes the workspace has not taken yet (*Update all* in Fabric first) or
  there are conflicts (resolve them in Fabric's Source control panel). A head
  that moved while committing is retried once; a commit that is too large is
  split into batches.
* A commit problem **never fails the run**. The Migrate step says what
  happened: *Committed 12 items to dev (1a2b3c4)* with **Open pull request dev
  → main** and **View commit**; *Nothing new to commit*; *Not committed: the
  workspace is not connected to Git*; or the error. Anything not committed has
  **Commit now** to try again. The commit runs on its own thread, so Retry
  failed and Reset are free the moment the run completes.
* **Commit now** (`POST /api/fabric/git/commit`) commits by hand what the
  current run created or updated: after a run with committing switched off, or
  to retry. With no run for the workspace it is refused (`no_run`).
* The pull-request link opens Azure DevOps' (or GitHub's) *create pull
  request* page from the workspace's branch into `main`; the review and the
  merge happen there.

The Release path's **Fabric Git** chip shows the branch, the last commit and
what waits to be committed, read live from Fabric (`GET /api/fabric/git`).

**Permissions:** Admin in the Fabric workspace to connect it to Git (once);
Admin, Member or Contributor to commit; and write access to the `dev` branch.

**Not in Git, by design:** connections (each environment has its own) and
data. Some items' Git support in Fabric is still in preview (for example the
Warehouse); that is Fabric's limit, listed on Microsoft Learn.

## LLM-assisted transformation

Some objects can't be converted by the rules alone: a procedure using `sys.pdw_*` or `MERGE`, a table
with a `sql_variant` column, a pipeline with a mapping data flow, a notebook using `TokenLibrary`, or a
.NET notebook. For these, you can ask Claude for a draft and review it before anything is deployed.

**How it is called.** The API runs the **Claude Code CLI** on its own machine in headless mode
(`claude -p --output-format json --json-schema ...`). No API key is involved: it uses the account the
CLI is already signed in with (run `claude` once in a terminal to sign in). The CLI runs with every
tool switched off, no MCP servers and no saved session, from an empty temporary folder, so it can only
answer. It can't read files, run commands or reach Synapse or Fabric.

**Finding them.** **Assess → LLM transforms** runs the rules over every object a draft can be made for
(Stored Procedures, Views, Tables, SQL Scripts, Pipelines, Notebooks). It lists the ones the rules can't
convert first, each with the reason, and shows how many drafts are waiting for review or have been
accepted. Tick several and choose **Generate drafts**. They run a few at a time in the background, and
the list updates as they finish. Click a name to review that object. The **Project requirements** editor
on the same tab changes `prompts/transform/project.md` for everyone.

**Reviewing one object.** Open an object and choose the **LLM transform** tab (or **Convert with LLM**
on its overview, or click it in the list above). Then:

1. **Preview prompt** shows exactly what will be sent.
2. **Generate with LLM** queues a draft. It usually takes 20–90 seconds, and you can close the panel meanwhile.
3. Compare the **LLM draft** with the **Synapse source** and the **Rule-based output**. Read the automatic
   checks, the model's notes, its confidence, and anything it left for a person. You can edit the draft in place.
4. **Accept** it. Edited code is checked again first, and a draft with a failing check can't be accepted.
   **Discard draft** throws the draft away; **Go back to the rules** deletes the accepted version too.

**In a run.** The Plan and Migrate steps use an accepted transform instead of the rule-based conversion,
but only while the object's source is unchanged since the draft was made. Each transform is pinned to a
hash of the source; if the source changes, the run falls back to the rules and says so in the log.
Accepted SQL still goes through the T-SQL rules, which are safe to repeat. An accepted pipeline is still a
*Synapse-shaped* pipeline, and the usual converter embeds its datasets and resolves the Fabric ids. The
run log marks every object created this way.

**Consent, budget and the migration context.** Drafts follow the migration brief:
none is generated without consent to send definitions to Claude (D1), and none once
the project has spent its budget (D2; every draft's cost is kept in the project's
`llm/spend` ledger). Under a required consultation, consent comes from the approved
brief; a person withdrawing it in Consult stops drafts at once. D6 can keep drafts to
the objects the rules cannot convert. Every draft is sent `MIGRATION_CONTEXT.md`, the
approved brief written for the model, and records which one by its hash (see
[The migration context](#the-migration-context-migration_contextmd)).

**Changing the prompt for a project.** The prompt is put together on the server from Markdown files in
`prompts/transform/`:

| File | What it is for |
|---|---|
| `base.md` | The frame of every prompt: the object, its source, the rule output, what discovery found, its dependencies (with the column types of the tables it reads), and the answer format. |
| `stored_procedure.md`, `view.md`, `table.md`, `sql_script.md`, `pipeline.md`, `notebook.md` | How to convert that type of object. A notebook is sent and returned as its cells (`{"language", "cells"}`), and the usual notebook conversion still runs on the result. |
| `project.md` | **Your project's requirements**: naming, target names, coding standards, things to avoid. They take priority over the general guidance. |

The files support `{{placeholders}}` (`object.name`, `object.schema`, `object.type`, `target.warehouse`,
`target.workspace`, `target.component`, `notebook.language`), `{{#if name}} ... {{else}} ... {{/if}}` blocks, and
`<!-- comments -->`, which are removed before sending. Edits take effect on the next draft; no restart is needed.

**Settings** (environment variables for the API process):

| Variable | Default | Effect |
|---|---|---|
| `MIGRATION_LLM` | `on` | `off` stops new drafts from being generated. Already-accepted transforms are still used. |
| `MIGRATION_LLM_MODEL` | Claude Code's default | Passed to `claude --model` (for example `sonnet` or `opus`). |
| `MIGRATION_LLM_TIMEOUT` | `180` | Seconds allowed for one draft. |
| `MIGRATION_LLM_WORKERS` | `2` | Drafts generated at the same time. |
| `CLAUDE_CLI` | `claude` on `PATH` | Path to the CLI. |
| `MIGRATION_PROMPTS_DIR` | `prompts/transform` | Where the templates are read from. |
| `MIGRATION_TRANSFORMS_DIR` | `transforms` | Where drafts and accepted transforms are kept. |

**What leaves the machine.** The object's definition, its rule output, the names and column types of
what it depends on (names only, with G4 at minimal), your `project.md` and the migration context are sent to
Anthropic through Claude Code. No credential, connection string or table data is ever sent. If this client's
code must not leave the network, set `MIGRATION_LLM=off`.

**Demo mode** shows the whole flow with canned drafts, and no model is called.

## The orchestrator: execution plan, agent and assistant

**The agent proposes and sequences. The engine executes. A person approves.** The
agent's tools are the same service calls the page's buttons make, so the run's
state machine, the sign-off, the leases and the audit trail apply to it exactly as
they do to a person (`src/discovery_agent/orchestrator/`, `api/orchestrator.py`).

**The execution plan** is built from the approved brief, the latest recorded plan
version and the discovery snapshot; the same inputs always give the same steps and
the same hash, and a new version is kept only when one of them moves. A person's
task marked done under one version stays done in the next while the task itself is
unchanged. A check the accelerator cannot tell (which kind of identity Fabric is
signed in as, a source signed out after discovery) a person may confirm by hand; one
it sees failing is fixed, never confirmed, and a confirmation stops counting once
the accelerator sees the check fail.

| Phase | Steps |
|---|---|
| Readiness | The source and the target connected (with a capacity), discovery complete, the brief approved and current, a plan with no blocking risk; the Fabric sign-in's kind (E2) and the capacity's region (G3, a person's check the run waits for) |
| Before the run | Credentials to enter (the agent only names them), SQL users to map, tables whose types change to review (D4), code to rewrite or rebuild by hand (D3, D5, A4) |
| Prepare | Drafts the brief sends to AI (D3, D5), then a person reviews and accepts each |
| Deliver | The run, as A6 says, and the fix loop; integration runtimes and event triggers set up by hand |
| Validate | At H1's depth; each mismatch resolved, or accepted with a reason |
| Release | The commit to Fabric Git (E0, E0S), then the pull request reviewed and merged |
| Cutover | The synced tables reloaded to remove deletes (C4), side-by-side cycles (H2, H2A), the window (H3) |
| Sign-off | The report: the brief, the plans, the runs, validation, what was left for a person, and the audit trail's head hash (`GET /orchestrator/report?format=md|json`) |

**Tools and tiers.** One registry (`GET /orchestrator/tools`), each tool with typed
arguments, a preview, a re-check and an idempotency key:

| Tier | Tools | Who may trigger them |
|---|---|---|
| read | the journey, discovery, objects, the plan's analysis, a run and its failures and log, validation, Fabric Git, drafts, the brief, a diagnosis, the plan's steps | always, at once |
| prepare | LLM drafts, a compare-only validation | always a request: a draft sends a definition to Claude and spends the budget (D2) |
| change | start, pause, resume, retry, a probing validation, a Fabric Git commit | a request; at *act on Dev* the agent resumes a pass a restart cut short, and retries failures that were each a timeout or throttling, each at most twice a run |
| high | update in place, replace rows, run pipelines in Validate, close a run, discover again; and any start whose brief options overwrite (A7, C6), which is never a plain start | always a request, each confirmed on its own |
| person only | approve a brief or a run, accept or discard a draft, enter credentials, answer the brief, confirm a step | never the agent: they are not tools at all, and the services refuse anything but a person |

**Requests.** What the agent proposes waits as an action card with exactly what it
would do (the plan and options of a run, what it overwrites, whether a person signs
it off or the rule in force approves it). It is bound to what it acts on: the run and
its version, the plan and what a start would send. When a person confirms it, it is
re-checked and runs, as that person, only while that is still so (*It was for run
004, and the current run is 005 now*); a retry or resume sends the run's version it
was proposed for, so the run itself refuses one that moved in between. A rejection
needs a reason, which goes back to the assistant, and the agent never asks for the
same thing again until what it acts on moves (nor for a run a person declined or
stopped, until a new plan is recorded). One request at a time for the same thing; a
question to the assistant can wait on another's card, and hears its decision. One
that no longer applies lapses on the next wake-up, even with the agent paused, and
one nobody decided lapses after a day and can no longer be confirmed.

**Autonomy (E6).** *Observe* proposes nothing; *suggest* (the default) makes
everything a request; *act on Dev* does the two things above on its own, recorded as
the agent's (`orchestrator.action_taken`), and only on the very workspace the brief
was approved for and calls development (E1): a brief's approval keeps which Fabric
workspace was the target, another one connected later makes the brief stale (every
run is then signed off there until it is reconfirmed), and a brief approved with no
workspace connected names none, so the agent asks for everything. The plan board
says why when it does. At most three new actions a wake-up, in every mode.

**The supervisor** wakes on what the audit trail records (a pass finished, halted
or cut short, a sign-off, a brief approved, a validation finished, a draft
accepted), at once when the accelerator starts, and on a five-minute timer as a
fallback, and rebuilds what it knows from the services and the store each time: a
restart loses nothing. The trail only signals it: its work happens in its own
thread, never in the one that wrote the record (which may hold a service's lock),
one wake-up at a time. A project switch holds it: the wake-up in flight finishes
first (or the switch is refused), and none starts until the switch is done. Each
wake-up that did something is kept (`orchestrator/wakeups`). **Pause the agent**
stops it, and the assistant's proposals, until resumed; `MIGRATION_AGENT=off` keeps
the plan board and switches the agent off.

**Diagnosis.** A failure's category (see [Run records](#run-records)) allows a closed
set of actions, the rule's choice first: a passing failure is retried; one Fabric
rejects gets a draft (with consent) or a rewrite; a sign-in or capacity problem is
escalated to a person with what to do; a name already in use is left, or updated
in place (high tier). `GET /orchestrator/diagnosis/<id>` says it for one object. A
model's explanation can never widen what a category allows.

**The assistant** (`POST /assistant/ask`). With an approved brief's consent, each
turn is one structured answer naming one step (read, change, ask or finish); the
server checks the tool exists, its tier fits the step and the arguments fit the
tool. A read runs at once and its result, redacted and cut to size, is added as
data, fenced and escaped (nothing in it can close the fence), so text from Fabric or
a log can never instruct the model; with G4 at *minimal* the brief it reads holds
only the decisions, and errors come without the values they quote. Reads never
include a definition. A change becomes an action card and the loop waits for the
person. Each task has limits (12 turns, 3 changes, 10 minutes of work across its
continuations, $2) and every turn's cost goes to the `llm/spend` ledger (estimated
when the CLI does not say). **Cancel** stops it at once, even mid-turn (that turn's
answer is dropped and nothing more is proposed). A task a restart cut short, or
that hit an unexpected error, is stopped and says so; **Continue** carries it on. Without consent it answers fixed
questions from the read tools: why something failed (with each failure's
diagnosis), what is left to do, where the migration stands, the brief, validation,
the drafts. Demo data answers by the rules only, and its agent only proposes.

## Projects, run records and the audit trail

What the backend keeps, and how a run is recorded, carried on and signed off
(`src/discovery_agent/state/`, `migration/run_state.py`, `migration/failures.py`).

### Projects

A project is one migration engagement, and the backend owns them; one is
active at a time. Each keeps its own folder, `.accelerator/state/projects/<id>/`:

| File | What it holds |
|---|---|
| `discovery.pickle`, `fabric_target.pickle` | The finished discovery and the Fabric target's selection, restored at start |
| `snapshots.jsonl` | One record per discovery: what it saw, a fingerprint over every object and definition, whether all of the source could be read |
| `plans.jsonl` | Plan versions (`P001`, …): the objects and waves, the options, the planner's score, the discovery it was made from, who recorded it |
| `migration.json`, `runs/<id>/` | Which run is current, and each run's record (below) |
| `approvals.jsonl` | Every sign-off decision, a wave's included |
| `consult/answers.jsonl`, `consult/proposals.jsonl` | Every answer, skip, withdrawal and resolution, and the answers proposed from notes or a questionnaire with what a person decided |
| `consult/briefs/<n>.json`, `consult/brief_events.jsonl`, `consult/deviations.jsonl` | Each brief version, never changed; each version's lifecycle; the plans confirmed although they differ from the brief, with the reason |
| `llm/spend.jsonl` | What each LLM call cost (drafts, consultation help, the assistant), against the brief's budget |
| `orchestrator/plans/<n>.json`, `orchestrator/marks.jsonl`, `orchestrator/actions.jsonl`, `orchestrator/wakeups.jsonl`, `orchestrator/control.json` | Each version of the execution plan; a person's marks on its tasks; every request and what became of it; the supervisor's wake-ups; whether the agent is paused |
| `assistant/tasks/<id>.json`, `assistant/<id>.jsonl` | Each question to the assistant, and its conversation |
| `validation.json`, `git.json` | The last validation, and the last Fabric Git commit |
| `audit.jsonl` | The audit trail (below) |
| `blobs/` | What the runs sent to Fabric, and each migration context a draft was sent, by hash |

**Switching** to another project (the project menu, `POST /projects/<id>/activate`)
is like restarting into it: its discovery and Fabric target come back the way they
do after a restart, and the Synapse sign-in stays where it is. It is refused while
anything of the current project is still working (a discovery, a sign-in window,
a run, a validation, a commit or an LLM draft). Like a restart, it runs alone: it
waits for the requests already being answered (up to 15 seconds, then it is refused)
and holds new ones until it is done, so no request reads one project and writes the
next. A switch that fails half way is undone for each service that had switched.
Project ids are lower case, since Windows folder names ignore case. **Deleting** a project archives its folder under `archive/`;
nothing is deleted, and neither the active project nor the last one can go.
Accepted LLM transforms are shared by every project (they apply only where an
object's source is exactly the one they were made from).

**From the single-project version:** on its first start, the backend creates
the `default` project and copies the discovery and Fabric target saved before
into it; the originals stay where they were. The browser then hands its own
projects to the backend once, keeping their ids, so each one's plan and options
carry on; the project the browser was on carries on the saved discovery. Against
an older backend, projects stay labels kept in the browser, as before.

### Run records

Every change to a run goes through one place that checks the move and writes it
down in the same step, so a run's record is complete by construction:
`runs/<id>/run.json` (its status, options, passes, sign-off, and what it was made
from: the plan entries, the discovery snapshot, the planner's fingerprint and the
*names* of the connections it was given credentials for), `runs/<id>/events.jsonl`
(every status change, every change to an object and every log line, in order,
each with its correlation id and actor) and `runs/<id>/items.json` (the objects
after the last pass). Run numbers carry on after the highest run recorded, so a
record is never written over; error texts and log lines are masked for secrets
before they are kept; and no object is left *in progress* when a pass ends,
whatever stopped it.

| Run status | Meaning | What moves it on |
|---|---|---|
| `awaiting_approval` | A person must sign it off before it writes anything | **Sign off and run**, or **Decline** (`rejected`, final) |
| `queued`, `running` | A pass is working | It ends by itself, or **Pause** |
| `paused` | Stopped between objects: by you, at a wave with failures (*stop on failure*), before a wave a person signs off (`awaiting_wave_signoff`, when the brief has each wave signed off), or cut short by a restart (`interrupted`) | **Resume** (a new pass); before a wave, **Sign off wave N**, which releases that wave only |
| `completed_with_failures`, `failed` | The pass ended with objects failed or blocked, or the workspace refused it | **Retry failed** (a new pass) |
| `completed` | Done | Final |
| `cancelled` | Closed by **Reset step** or replaced by a new run | Final; its record stays in the **run history** |

Start, each Resume and each Retry are *passes* of the same run. An object
**blocked** by a failure is not attempted: a view whose table failed, a pipeline
whose notebook or connection failed, a schedule whose pipeline failed, a table's
data whose table failed. It says what it needed, and Retry failed runs both
again. Kinds that only need something when they run (a notebook reading a
table) are never held back.

Every object error is classified once: `TRANSIENT`, `AUTH`, `CAPACITY`,
`CONFIG`, `UNSUPPORTED`, `DEPENDENCY`, `CONFLICT`, `INTERRUPTED` or `UNKNOWN`
(`GET /migration/policy` lists what each one means). Only a `TRANSIENT` failure
(a 5xx, a timeout, Fabric unreachable) is retried by the pass itself, twice with
back-off, and only for kinds that are made, or found and left, by name, so a
retry can never make anything twice.

**After a restart** a run that was working comes back `paused` (`interrupted`).
What it was in the middle of goes back in the queue, marked: the next pass looks
for it in Fabric first and, finding it, records that this run made it. A data
pipeline run the run had started is followed to its end rather than started
again beside it (a run Fabric no longer knows is left for a person; one it cannot
answer about right now is asked about again on the next pass). And no load starts
while Fabric's own job list shows the wave's pipeline running for anything else, a
run since closed or a person in Fabric: a second load beside it could copy rows
twice, or empty a table under it. Credentials
are never kept, so the run lists the connections it needs again
(`needsInput`) and Resume, Retry and sign-off accept them. A run made from a
discovery that has since been run again cannot carry on; its record stays.

**What was sent to Fabric** for each object (item definitions, and every SQL
statement the Warehouse accepted) is kept under its SHA-256 in the project's
`blobs/`, named on the object (`artifact.parts`) and readable with
`GET /migration/runs/<id>/artifacts/<sha256>`. A connection's request is never
kept: it carries credentials.

### Sign-off

The approved brief's E4 decides which runs a person signs off before they write
to Fabric, and a production target (E1) signs off every run. `MIGRATION_SIGNOFF` is
this machine's own rule: with no brief it decides, and with one the stricter of the
two holds. A run waiting for sign-off is started only while the brief it was checked
against is still the one in force and still fits (`brief_changed` otherwise):

| Value | A person signs off |
|---|---|
| `overwrite` (default) | Runs that change what Fabric already has: *Update objects that are already in Fabric*, or Table data with *Replace it* |
| `always` | Every run |
| `never` | No run |

A run that needs no sign-off is approved by the accelerator, and recorded as
such (`by: system`, `automatic: true`). Only a person can sign off. What is
signed off is the run's exact inputs, hashed (`subjectHash`): its plan, options
and stages, which a run never changes once made.

### Audit trail

`audit.jsonl` holds one record per action: what it was (a closed catalog:
`project.*`, `source.*`, `discovery.*`, `fabric.*`, `consult.*`, `brief.*`,
`plan.recorded`, `plan.deviation`, `run.*`, `validation.*`, `git.*`,
`transform.*`, `llm.refused`, `orchestrator.*`, `assistant.*`, `report.exported`,
`journey.step_confirmed`, `system.*`, `audit.exported`), who did it (`person`:
`MIGRATION_OPERATOR`, else the account the API runs as; `agent`: the LLM for a draft
it wrote, the orchestrator for what it proposed or did, the assistant for what it
proposed; `system`: the accelerator itself), the target, the outcome (`done`, `failed`, `refused`) with a code and a
reason, the details its action allows (nothing else is kept, and text is masked
for secrets), and the correlation id, project and run. Each record carries the
hash of the one before it and its own, so `GET /audit/verify` shows where the file
was changed, if it was; it shows *that* it was changed, not who changed it.
Writing a record never fails the action it records.

### Settings

| Variable | Default | What it does |
|---|---|---|
| `MIGRATION_STATE_DIR` | `.accelerator/state` | Where the projects are kept. One API per folder. |
| `MIGRATION_STATE` | `on` | `off` keeps everything for the life of the process only. |
| `MIGRATION_LOG_DIR` | `.accelerator/logs` | Where `api.jsonl` is written. |
| `MIGRATION_LOG` | `on` | `off` writes no log file: the console only. |
| `MIGRATION_LOG_LEVEL` | `INFO` | `DEBUG` adds every Fabric read and every Warehouse statement (by kind and hash, never its text). |
| `MIGRATION_SIGNOFF` | `overwrite` | Which runs a person signs off: `never`, `overwrite` or `always`. With an approved brief, the stricter of this and the brief's rule holds. |
| `MIGRATION_CONSULTATION` | `required` | `required`: no run without an approved brief that fits the estate, and differences from it explained. `advisory`: neither is refused. |
| `MIGRATION_BRIEF_APPROVAL` | (unset) | `separate`: a brief with a decision that loosens a safeguard cannot be approved by the person who submitted it. |
| `MIGRATION_OPERATOR` | the account the API runs as | The person the log and the audit trail name. |
| `MIGRATION_AGENT` | `on` | `off` switches the orchestrator's agent off: the execution plan and the plan board stay, nothing is proposed or done by it. |

## Tests

```
pytest                                  # backend: 1,765 tests pass, 104 skipped (below)
cd frontend && npm run typecheck && npm test   # UI: type check and 95 tests
```

**Safe to run beside a live session.** `tests/conftest.py` points everything a
real session keeps on the machine into each test's own temporary folder:
`MIGRATION_STATE_DIR` (the projects, their discoveries, runs and audit trails),
`MIGRATION_TRANSFORMS_DIR` (accepted LLM transforms), `MIGRATION_LOG_DIR`, and
the upload and working-copy folders under `input/`. It also fixes the operator's
name, sets `MIGRATION_SIGNOFF=never`, `MIGRATION_CONSULTATION=advisory` and
`MIGRATION_AGENT=off`, so a test asks for a sign-off, an approved brief or the agent
only when it means to (and no supervisor wakes in a test's background), and makes
sure no test finds a real `claude` on the machine. A test API used to delete a running
session's saved `discovery.pickle` and leave uploads behind; a test now checks
that nothing points at the real folders. (`MIGRATION_STATE=off` alone was never
enough: a reset or sign-out still deleted the saved file. Switched off, the
folder is now never read, written or emptied.)

**Changing the consultation.** The Demo page keeps its own copy of the catalog
and of the engine. After a change to `consult/catalog.json`, copy it to
`frontend/src/mock/consultCatalog.json` (a test fails until the two match); after
a change to the catalog or the engine, write the cases the Demo engine is checked
against again, then run the UI tests:

```
UPDATE_CONSULT_PARITY=1 pytest tests/test_consult.py -k "demo_engine or demo_pages_settings"
cd frontend && npx vitest run src/test/consult.test.ts
```

The orchestrator tests (`tests/test_orchestrator.py`): the same inputs give the same
plan; the brief's answers become steps naming them; each step's status from what the
services report; a person's mark carried over only while the task is unchanged; a
check confirmed by hand only while the accelerator cannot tell (one it sees failing is
fixed, never confirmed, and a confirmation stops counting once it fails); each
failure category's actions, a model never widening them; the tiers, with no tool for
what only a person does; a request re-checked on confirm and run as the person, and
never on another run or plan than its card showed (a retry sends the version it was
proposed for); the agent never deciding its own, and never asking again for what a
person rejected or a run they declined; a start that overwrites proposed as a
high-tier one whose card says so; at *suggest* it asks and never acts, at *act on Dev*
it retries timeouts alone and resumes alone, each at most twice, never drafts or
validates alone, never off the workspace the brief was approved for or while paused,
and at *observe* proposes nothing; a pass cut short resumed, a wave's sign-off never;
requests that lapse, even while paused, and one past its time never carried out; the
trail only signalling it (no deadlock with a service's lock); a project switch holding
it; drafts proposed only where one would be made; the sign-off report; and the
assistant: its rules without consent, the brief and errors as G4 lets them go to the
AI, a fence nothing can close, the task loop reading, proposing, waiting for a person
(or another's card) and finishing, a cancel mid-turn, a step it cannot take, a question
back, its limits across continuations, an unexpected error stopping it, and a task
carried on after a restart.

The skipped backend tests are the two opt-in integration suites (39 tests,
below), 63 that read a local snapshot of a sample repository
(`input/repository/Test-Drive-Azure-Synapse-with-a-1-click-POC`) when it has
been cloned there, and 2 that need permission to create symbolic links.

Tests run fully offline — no network access, no Azure subscription, no ODBC
driver and no dependency on any real repository. Every credential, ARM call,
SQL session and git invocation in the unit suite is a fake, and so is the ODBC
driver the planner checks for (`MigrationService(driver=...)`).

Two opt-in integration suites talk to real resources and are skipped unless
their flag and configuration are both set:

```
# the catalog queries, against a real dedicated pool
SYNAPSE_SQL_INTEGRATION=1 SYNAPSE_SQL_SERVER=... SYNAPSE_SQL_DATABASE=... pytest

# the whole connection chain, against a real subscription
CONNECTIONS_INTEGRATION=1 AZURE_SUBSCRIPTION_ID=... SYNAPSE_RESOURCE_GROUP=...   SYNAPSE_WORKSPACE_NAME=... SYNAPSE_SQL_POOL=... pytest
```

The state tests: `tests/test_state.py` (the store's documents, streams, blobs
and leases; process identity across reused ids; projects, their adoption and
archive; the single API per folder; the run and object state machines; every
failure class; sign-off; and the audit trail's allow-listed details, redaction,
hash chain, paging, exports and catalog), `tests/test_run_records.py` (a run
recorded as it happens; a restart that cut a run short, resumed without making
anything twice; credentials asked for again and never written; a run made from an
older discovery; sign-off approved, declined and kept across a restart; blocked
objects; a fault in the runner itself; pipeline runs followed after a restart;
what was sent kept by hash, never a connection's credentials),
`tests/test_projects_api.py` (projects, switching with its refusal, its undo and
running alone, no deadlock between a switch and a service recording an action, and
the trail over HTTP) and `tests/test_logging.py` (correlation ids on every line and
across threads, masking). They include the cases a code review found: a line cut
short inside a character, a refused Start that must leave the current run alone, a
Start prepared before a switch, run numbers after a lost pointer, a wave whose
tables must not stay *in progress*, a load never started beside a running one, and
a retry that finds what a lost answer made.

The Fabric Git tests (`tests/test_fabric_git.py`) use a fake Fabric REST
client: the connection and status (including a status that answers 202 and is
followed), a selective commit that leaves out data-load pipelines and
deletions, Azure DevOps single sign-on set when missing and GitHub refused with
the way forward, conflicts and incoming changes refused, a moved head retried
once, a commit too large split into batches, error messages without a token,
the pull-request links, and the run: committed after it completes (only what
it created or updated, with the other waiting changes named and left), free for
Retry while the commit finishes, switched off, a commit by hand refused without
a run, and a commit failure kept as a note that never fails the run.

The UI tests run against Demo data in a simulated browser: choosing the
route (only Synapse and Fabric selectable), the two source options (the
workspace and a ZIP), a workspace's Git repository found and chosen with
nothing typed (a workspace without one read live, and the live workspace
chosen instead), a typed repository URL, swapping a connected live workspace
for its Git repository, Start migration staying disabled until both sides are connected
and tested, the step lock and Next, resetting a step, results that disappear
(*Run again*), stage options kept per project, the project menu, the
backend-unavailable bar, the dialogs that ask first, every step's panel, the
workspace explorer, the release path (with its live Fabric Git chip), a run
committed to Fabric Git with its pull-request link, *Commit now* after a run
with committing off, the assistant, the old page addresses, that a typed
credential never reaches browser storage, that planning is told only which
linked services have credentials (never what they are, and not again on every
key typed), a run held for sign-off and signed off, a closed run in the run
history, a new project switched to and recorded, the activity and its check,
and a browser's projects handed once to a backend that owns them.
