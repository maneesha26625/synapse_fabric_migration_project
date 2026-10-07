# Migration Accelerator UI

The web application for the Synapse → Fabric accelerator. It has two pages and
no sidebar:

* **Connections** (`/`): choose the route (source and destination dropdowns),
  connect both sides, then **Start migration**.
* **Migration** (`/migration?step=…`): the six steps (Discover, Assess, Waves,
  Plan, Migrate, Validate) side by side. Click a step to see its details below;
  each step is confirmed with Next before the following one opens.

Every step has **Reset step** in its header: after a dialog that lists what
will go, it clears that step and every step after it so they can be done again
from the start. When the migration is finished, **Download report (CSV)** saves
every object's result and every validation check.

A **+** button at the bottom right (or Ctrl+I) opens the migration assistant
panel on the right. It is a preview: the panel and its context are there, the
conversation is planned for a later release.

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
`npm test` (50 tests, mostly on Demo data), and `pytest tests/test_api.py` for the API.

The API restarts itself when its code changes and keeps its state, and Vite
reloads the page's code: nothing is restarted by hand after an update.

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

To walk through the connection steps, press **Disconnect** or **Change** on a
side's card on the Connections page and connect again; no sign-in window opens.
As in Live mode, disconnecting (or connecting another workspace) starts over
what was built on that side, and asks first. Scenarios are chosen by workspace name: containing `denied`,
`notfound`, `expired` or `offline` fails the connection test; `empty`,
`partial`, `fail` or `timeout` change what discovery returns. Anything else
succeeds. Reloading the page restores the ready demo; steps you confirmed stay
confirmed, and Migrate and Validate say *Run again* until they are. A run fails
three objects on purpose and validation finds three short tables, so Retry
failed and the filters have something to show.

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
* **Roadmap visible, not selectable.** Platforms and sign-in methods that are
  not built yet (other sources and destinations, Workspace export (ZIP), Git
  repository) are listed, dimmed and marked *Coming soon*. They are announced
  as unavailable to screen readers and ignore clicks and keys.
* **Accessible.** The dropdowns are listboxes with full keyboard support,
  method tiles are radio groups, step boxes say their state in their label,
  and focus, contrast and reduced motion are respected.
* **Start a step over.** *Reset step* clears the step and the ones after it.
  The dialog lists what goes (with the real numbers) and what stays; the backend
  is asked first, so a refusal (something still running) changes nothing.
* **Ask before losing work.** Reset, running discovery again over confirmed
  steps, disconnecting either side, clearing the plan and deleting a project
  all ask first and say exactly what goes. Focus starts on Cancel, so Enter
  never confirms by accident.
* **A new connection starts fresh.** Disconnecting the source, or connecting
  another workspace, starts the whole migration over; doing so on the Fabric
  side starts Migrate and Validate over. Old results never reappear under a new
  connection. Testing the same workspace again, or a failed test, changes
  nothing.
* **Honest state.** A confirmed step whose results were lost (source signed
  out, server restarted, Demo data reloaded, validation results gone with the
  page) shows *Run again* with the reason, the steps after it wait, and the
  completion card waits for it. A confirmed plan edited into a blocking risk
  says *Fix risks* and holds Migrate. A page that is still loading says so
  instead of flashing "Not connected".
* **Rides through backend restarts.** In Live mode the page checks the backend
  every few seconds. While it restarts after an update, a blue *Reconnecting*
  bar shows and reads are retried; when it is back (a new `bootId`), the page
  reads the connections, discovery, Fabric target and run again, and a note
  says what the backend kept. A restart is never taken for a sign-out, so the
  plan and confirmed steps stay. Resume and Retry send the run's credentials
  again, since a restarted backend has none. A backend older than the page
  (its `apiVersion`) that updates itself says it is updating; one that does
  not is asked, once, to be replaced with `start-ui.ps1`; one that stays away
  is *not answering*, with Retry and Use Demo data.
* **A plan you can work with.** Risks open to show their objects, with *Find in
  plan*; objects are added all at once, by type or by name, found by name or
  type, moved between waves and taken out; large waves show 100 rows at a time;
  objects no longer in the discovery are flagged; numbers read the same
  everywhere (1,542; 98.4d).
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

- Azure Synapse offers **Azure CLI** and **Interactive browser**; Microsoft
  Fabric offers **Azure CLI** and **Fabric CLI**. There is no password or token
  field for signing in. Interactive browser asks only for Tenant ID and
  Subscription ID, and the backend refuses any sign-in field it does not
  expect.
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
    journey/      steps, useJourney (each step's state), ResetStep, report (CSV), panels/ (one per step)
    discovery/    Inventory, object details, summaries, mapping, dependency graph
    migrate/      planner runs, stages and credentials, strategy, run panel
    layout/       Layout (top bar, project menu, backend bar), Assistant (launcher and panel)
    shared/       buttons, fields, banners, tabs, metrics, dialogs, notify (toasts)
  state/        AppState (connection, discovery), MigrationState (projects, plan, run, journey)
  services/     realApi, mode selection            (the only code that calls fetch)
  mock/         mockApi, mockData                  (demo only)
  styles/       global.css (tokens and every style)
  test/         app.test.tsx
```

The earlier one-page-per-step addresses (`/discovery`, `/assessment`,
`/dependencies`, `/migrate`, `/execute`, `/validate`) open the matching step;
`/synapse`, `/fabric` and `/connections` open the Connections page.
