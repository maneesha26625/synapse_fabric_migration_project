# Migration Accelerator UI

The web application for the Synapse → Fabric accelerator. It has two pages and
no sidebar:

* **Connections** (`/`): choose the route (source and destination dropdowns),
  connect both sides, then **Start migration**. The Synapse source is the
  **Synapse workspace** (sign in and choose it; when it is connected to Git its
  definitions are read from its **Git repository**, recommended and chosen for
  you, or from the **live workspace** if you choose) or a **ZIP export**.
* **Migration** (`/migration?step=…`): the seven steps (Discover, Assess,
  Consult, Waves, Plan, Migrate, Validate) side by side. Click a step to see its
  details below; each step is confirmed with Next before the following one opens.
  **Consult** asks the questions before a migration (sections A to I) and has the
  migration brief approved; Plan is pre-set from it, confirming a plan that differs
  from it asks why, and its **Migration context** tab shows MIGRATION_CONTEXT.md,
  the brief as every LLM draft is sent it. Consult never locks the steps after it:
  a required consultation holds back the run itself, and Migrate says why.
  **Migrate** has two tabs: the **Run**, and the **Execution plan** (every step
  from readiness to the sign-off report, who owns it, what the agent proposes, and
  the agent's pause switch). Beside the
  steps, the **workspace explorer** shows the discovered workspace as a
  repository tree; under them, the **release path** shows the way from Dev to
  Prod, with a live **Fabric Git** chip (branch, last commit, what waits to be
  committed, and the pull request to open).

When a run completes, the Migrate step shows its **commit to Fabric Git**:
*Committed 12 items to dev (1a2b3c4)* with **Open pull request dev → main**, or
what stopped it with **Commit now**. *Commit to Fabric Git after the run* is a
run option in Plan → Stages & credentials (on by default).

Every step has **Reset step** in its header: after a dialog that lists what
will go, it clears that step and every step after it so they can be done again
from the start. When the migration is finished, **Download report (CSV)** saves
every object's result and every validation check.

A **+** button at the bottom right (or Ctrl+I) opens the migration assistant
panel on the right; the number on it is how many of the agent's requests wait for
you. **Ask** is the conversation (answered from the accelerator's record by its
rules, or with an approved brief's consent by Claude, one checked step at a time,
every change an action card you confirm or reject), **Plan** the execution plan as
a checklist, and **Requests** what waits for you. Each LLM draft's review says
which MIGRATION_CONTEXT.md it was sent and what it applied, and flags one made
under an earlier brief.

What each step does, and what a migration run creates in Fabric, is described
in the [root README](../README.md).

Stack: React 18 + TypeScript + Vite, `react-router-dom`, `lucide-react`. Plain
CSS with design tokens (`src/styles/global.css`). No state library and no UI kit.

## Run it

Backend (from the project root, with `az login` done):

```
pip install -e ".[live]"
python -m discovery_agent.api            # http://127.0.0.1:8001
```

Frontend, development:

```
cd frontend
npm install
npm run dev                              # http://localhost:5173, proxies /api -> :8001
```

Frontend, single process (the API serves the built UI):

```
cd frontend && npm run build
python -m discovery_agent.api            # open http://127.0.0.1:8001
```

On Windows, `..\start-ui.ps1` starts both. Checks: `npm run typecheck`,
`npm test` (95 tests, on Demo data), and `pytest tests/test_api.py
tests/test_fabric_git.py` for the API. After pulling backend changes, restart
the API: the UI dev server reloads by itself, the API does not.

## Data layer: Live vs Demo

The **Live / Demo** switch in the top bar chooses the implementation of one
interface, `MigrationApi` (`src/types.ts`):

| Mode | Implementation | Notes |
|---|---|---|
| **Live** | `services/realApi.ts` → `/api/*` | Real Synapse and Fabric. Default. |
| **Demo** | `mock/mockApi.ts` | Generated sample data, about 1,500 objects. No backend, Azure or Fabric needed. A banner says so on every page. |

Demo data starts **ready**: the route is Azure Synapse → Microsoft Fabric, the
demo Synapse workspace `demo-synapse-ws` (pool `TransportDW`) is connected and
already discovered, and the demo Fabric workspace `Fabric_demo` is connected
and on a capacity. Start migration is enabled straight away. The Migrate step
runs a simulated migration (about a minute) that reports objects the way the
real run does: table data loaded by each wave's pipeline, linked services
waiting for credentials, integration runtimes set up by hand, and a few
failures on purpose so Retry failed can be tried. Validate then compares the
migrated objects, with three deliberate row-count mismatches and a few items to
review, so the filters have something to show.

To walk through the connection steps, press **Disconnect** (it asks first,
since the discovery is cleared too) or **Change** on a side's card on the
Connections page and connect again; no sign-in window opens. Scenarios are chosen by workspace name: containing `denied`,
`notfound`, `expired` or `offline` fails the connection test; `empty`,
`partial`, `fail` or `timeout` change what discovery returns. Anything else
succeeds. Reloading the page restores the ready demo; steps you confirmed stay
confirmed, and Migrate and Validate say *Run again* until they are. A run fails
three objects on purpose and validation finds three short tables, so Retry
failed and the filters have something to show.

Git in Demo data: choosing a demo workspace finds
`https://dev.azure.com/contoso/Synapse/_git/<workspace>` (branch `main`, folder
`synapse`) for most of them, with *Git repository* chosen; one whose name
contains `analytics` has no Git integration (it is read live), and `denied`
cannot be read. `Fabric_demo` is connected to the
`dev` branch of `contoso/Migration/fabric-migration` (folder `fabric`), so a
completed run is committed a moment later with a pull-request link; choosing
*My workspace* as the target shows what happens when the workspace is not
connected to Git. With *Commit to Fabric Git after the run* off, the run's
items wait and **Commit now** commits them.

## Design

* **One flow, one place for detail.** The Migration page shows the whole
  journey as six boxes, each with one number and its state (Done, Ready to
  confirm, In progress, Running, Locked). The selected step's details open
  below, starting with a summary row; the detail sits in tabs, so nothing is
  shown that was not asked for.
* **Confirm to move on.** The bar at the foot of each step says where it
  stands and offers *Looks good, continue to …*, *Continue anyway* when there
  is something worth a look, or a disabled button with the reason. Plan cannot
  be confirmed while a risk is blocking.
* **Roadmap visible, not selectable.** Platforms that are not built yet (other
  sources and destinations) are listed, dimmed and marked *Coming soon*. They
  are announced as unavailable to screen readers and ignore clicks and keys.
* **Nothing typed that can be read.** Lists are filled from the signed-in
  account, and a Git source is found from the workspace's own Git integration;
  a list with one entry is chosen for you. Choices with good defaults (branch,
  root folder, environment, parameters file) sit folded under *More options*,
  which says what will be used; a field appears only when it is needed.
* **Accessible.** The dropdowns are listboxes with full keyboard support,
  method tiles are radio groups, step boxes say their state in their label,
  and focus, contrast and reduced motion are respected.
* **Start a step over.** *Reset step* clears the step and the ones after it.
  The dialog lists what goes (with the real numbers) and what stays; the backend
  is asked first, so a refusal (something still running) changes nothing.
* **Ask before losing work.** Reset, running discovery again over confirmed
  steps, disconnecting a discovered source and deleting a project all ask
  first. Focus starts on Cancel, so Enter never confirms by accident.
* **Honest state.** A confirmed step whose results were lost (source signed
  out, server restarted, Demo data reloaded, validation results gone with the
  page) shows *Run again* with the reason, the steps after it wait, and the
  completion card waits for it. A page that is still loading says so instead
  of flashing "Not connected"; Live mode without a backend says that, with
  Retry and Use Demo data.
* **Plan configures, Migrate runs.** Stage switches, options and credentials
  live in Plan; every run, including a single stage on its own, starts from
  Migrate.
* **Responsive.** Below 1180px the assistant overlays the page instead of
  docking; below 900px the route and the connection cards stack; below 640px
  the top bar takes two rows, the step strip swipes sideways and keeps the
  selected step in view, and the action bars sit at the end of the page. The
  floating **+** button never covers an action button: where they would meet,
  the bars keep their right end clear.

## Environment

| Variable | Where | Purpose |
|---|---|---|
| `VITE_API_MODE` | frontend | `real` (default) or `mock`; the top-bar switch overrides it per browser |
| `VITE_BACKEND_URL` | frontend dev | where Vite proxies `/api` (default `http://127.0.0.1:8001`) |

No secret is read from, or written to, any environment variable or storage.

## Security notes

- Azure Synapse offers the **Synapse workspace** (a Microsoft sign-in window,
  then the subscription, resource group, workspace and pool chosen from lists;
  its definitions read from its Git repository or the live workspace) and
  **Workspace export (ZIP)**; Microsoft Fabric offers **Azure CLI** and
  **Fabric CLI**. There is no password or token field for signing
  in, and the backend refuses any sign-in field it does not expect.
- Git is reached with the machine's own credential helper or SSH key: a
  repository URL with credentials in it is refused, the workspace's Git
  integration is read from Azure without any credential, and Fabric commits to
  its branch with the signed-in account (Azure DevOps single sign-on). No Git
  token passes through the UI.
- The UI reads `/api/health` (`authMethods`, `authMethodDetails`) and disables a
  method the running backend does not support.
- Tokens never reach the browser: the backend holds them, and its responses are
  redacted.
- Credentials for linked services and the data-load connection are typed into
  password fields in Plan → *Stages & credentials*, kept in memory for the
  session, sent with the run, and never written to browser storage (a test
  checks this).
- `localStorage` holds only preferences and progress: the Live/Demo choice
  (`ma.apiMode`), the route (`ma.route`), the projects and each project's plan
  and stage options (`ma.projects`, `ma.plan.*`, `ma.options.*`), and which
  steps are confirmed (`ma.journey.<mode>.<project>`). Deleting a project
  removes its keys. Credentials are never among them.

## Layout

```
src/
  pages/        Setup (Connections), Workspace (Migration)
  components/
    setup/        platforms list, PlatformPicker (route dropdowns)
    connections/  SourceConnection (Synapse), TargetConnection (Fabric), MethodTiles
    journey/      steps, useJourney (each step's state), ResetStep, report (CSV), WorkspaceExplorer, ReleasePath, panels/ (one per step)
    discovery/    Inventory, object details, summaries, mapping, dependency graph
    consult/      ConsultPanel (questions, brief, changes since approval, the migration context, help), QuestionCard, DeviationDialog, format
    orchestrator/ PlanBoard (the execution plan, the agent), ActionCard (a request: confirm or reject)
    migrate/      planner runs, stages and credentials (and the Fabric Git option), strategy, run panel (and its Git banner)
    layout/       Layout (top bar, project menu, backend bar), Assistant (launcher with its badge, and the conversation)
    shared/       buttons, fields, banners, tabs, metrics, dialogs, notify (toasts)
  state/        AppState (connection, discovery), MigrationState (projects, plan, run, journey), ConsultState (the consultation, and Plan pre-set from the approved brief), OrchestratorState (the execution plan, requests, the assistant's questions)
  services/     realApi, mode selection            (the only code that calls fetch)
  mock/         mockApi, mockData, mockConsult with consultCatalog.json and consultVocabulary.json (the server's catalog, settings, facts and engine, in TypeScript), mockOrchestrator (the plan and a proposing agent from Demo data)   (demo only)
  styles/       global.css (tokens and every style)
  test/         app.test.tsx, consult.test.ts (the Demo engine against the server's, on consultParity.json)
```

The earlier one-page-per-step addresses (`/discovery`, `/assessment`,
`/dependencies`, `/migrate`, `/execute`, `/validate`) open the matching step;
`/synapse`, `/fabric` and `/connections` open the Connections page.
