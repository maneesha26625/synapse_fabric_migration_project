import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { buildReport } from "../components/journey/report";
import { mockApi, resetDemo } from "../mock/mockApi";
import { RESTART_GRACE } from "../services/realApi";
import { BACKEND_WATCH } from "../state/AppState";
import { REQUIRED_API_VERSION } from "../types";
import type { ConnectionConfig, ConnectionState, ExecutionRun, FabricConfig, FabricTarget, RestoredState, ResultsQuery } from "../types";

const CONFIG: ConnectionConfig = {
  method: "azure_cli", tenantId: "", subscriptionId: "10eb96c3-ba3c-492e-b95b-e9f1d6d85d70",
  resourceGroup: "rg-demo-migration", workspace: "demo-synapse-ws", workspaceUrl: "", sqlPool: "", resource: "",
};
const FABRIC: FabricConfig = { method: "azure_cli", workspaceId: "", workspaceName: "Fabric_demo" };
const Q: ResultsQuery = {
  search: "", categories: [], types: [], statuses: [], fabricTargets: [], paths: [], workstreams: [], mappingStatuses: [],
  classifications: [], assessment: "", sort: "name", dir: "asc", page: 1, pageSize: 200,
};
const STEP_ORDER = ["discover", "assess", "waves", "plan", "migrate", "validate"];
const JOURNEY = "ma.journey.mock.default";
const PLAN = "ma.plan.mock.default";
const stored = (key: string) => JSON.parse(localStorage.getItem(key) ?? "null");

/** Connect and finish discovery in the demo layer, so a step can be rendered against results. */
async function discovered() {
  await mockApi.testConnection(CONFIG);
  await mockApi.startDiscovery();
  await new Promise((r) => setTimeout(r, 3700));
}

/** Confirm every step before `step`, as if the operator had pressed Next on each. */
function unlockTo(step: string) {
  localStorage.setItem(JOURNEY, JSON.stringify(STEP_ORDER.slice(0, STEP_ORDER.indexOf(step))));
}

/** A small plan of real demo objects, as if built on the Plan step. Needs a finished discovery. */
async function seedPlan(count = 3) {
  const graph = await mockApi.getDependencies();
  const items = graph.nodes.filter((n) => n.type === "Notebook").slice(0, count).map((n) => ({ id: n.id, wave: n.wave }));
  localStorage.setItem(PLAN, JSON.stringify(items));
  return items;
}

function go(path: string) {
  window.history.pushState({}, "", path);
  return render(<App />);
}

const stepHeading = (name: string) => screen.findByRole("heading", { level: 2, name }, { timeout: 5000 });
const stepBox = (n: number) => within(screen.getByRole("navigation", { name: "Migration steps" })).getByRole("button", { name: new RegExp(`^Step ${n}:`) });

beforeEach(async () => {
  localStorage.clear();
  localStorage.setItem("ma.apiMode", "mock");
  window.history.pushState({}, "", "/");
  resetDemo();
  await mockApi.disconnect();
  await mockApi.disconnectFabric();
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("ready demo", () => {
  it("starts connected and discovered, with a Fabric target on a capacity: no sign-in anywhere", async () => {
    resetDemo();
    expect((await mockApi.getConnection()).status).toBe("connected");
    expect((await mockApi.getDiscoveryStatus()).state).toBe("completed");
    const target = await mockApi.getFabricTarget();
    expect(target.status).toBe("connected");
    expect(target.capacityAssigned).toBe(true);
    // A run starts straight away.
    const graph = await mockApi.getDependencies();
    const items = graph.nodes.filter((n) => n.type === "Notebook").slice(0, 1).map((n) => ({ id: n.id, wave: n.wave }));
    expect((await mockApi.startExecution(items)).total).toBe(1);
  });

  it("opens the start page ready to go: route chosen, both sides connected, Start migration enabled", async () => {
    resetDemo();
    go("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Start a migration" })).toBeInTheDocument();
    expect(screen.getByText("Demo data.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /Start migration/ })).toBeEnabled(), { timeout: 5000 });
    expect(screen.getByRole("button", { name: "From · Source" })).toHaveTextContent("Azure Synapse Analytics");
    expect(screen.getByRole("button", { name: "To · Destination" })).toHaveTextContent("Microsoft Fabric");
    expect(document.title).toBe("Connections · Migration Accelerator");
  }, 15000);

  it("loads every table with that wave's pipeline and lists linked services until credentials are given", async () => {
    resetDemo();
    const graph = await mockApi.getDependencies();
    const tables = graph.nodes.filter((n) => n.type === "Table").slice(0, 2);
    const links = graph.nodes.filter((n) => n.type === "Linked Service").slice(0, 1);
    const run = await mockApi.startExecution([...tables, ...links].map((n) => ({ id: n.id, wave: n.wave })), {
      scope: "automated", stopOnFailure: false, stages: [], dataMode: "if_empty", dataRun: "run", collation: "match_synapse",
    });
    expect(run.items.filter((i) => i.type === "Table data")).toHaveLength(2);
    const link = run.items.find((i) => i.type === "Linked Service")!;
    expect(link).toMatchObject({ status: "DEFERRED", step: "Needs credentials" });
    // The way forward names where credentials are entered: Plan, not the Connections page.
    expect(link.notes?.join(" ")).toMatch(/in Plan, Stages & credentials/);
  });

  it("fails only a few objects on purpose, however large the plan, and Retry Failed clears them", async () => {
    resetDemo();
    const graph = await mockApi.getDependencies();
    await mockApi.startExecution(graph.nodes.map((n) => ({ id: n.id, wave: n.wave })));
    // Jump the clock past the end of the run instead of waiting for it.
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(Date.now() + 10 * 60_000);
      const run = await mockApi.getExecution();
      expect(run.total).toBeGreaterThan(1000);
      expect(run.state).toBe("completed");
      expect(run.failed).toBeGreaterThan(0);
      expect(run.failed).toBeLessThanOrEqual(3);
      await mockApi.controlExecution("retry");
      vi.setSystemTime(Date.now() + 10 * 60_000);
      expect((await mockApi.getExecution()).failed).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("demo api", () => {
  it("refuses to start discovery before a connection has passed its test", async () => {
    await expect(mockApi.startDiscovery()).rejects.toMatchObject({ code: "not_connected" });
  });

  it("surfaces an authorization failure as a connection error, not a connection", async () => {
    const state = await mockApi.testConnection({ ...CONFIG, workspace: "denied-ws" });
    expect(state.status).toBe("not_connected");
    expect(state.error?.code).toBe("authorization");
  });

  it("filters, sorts and pages results the way the real API does", async () => {
    await discovered();
    const page = await mockApi.getResults({ ...Q, categories: ["Integration"], types: ["Pipeline"], pageSize: 25 });
    expect(page.total).toBe(60);
    expect(page.items).toHaveLength(25);
    expect(page.items.every((r) => r.type === "Pipeline")).toBe(true);
  }, 15000);

  it("gives every object a Fabric target, classification and assessment flag, and never overclaims", async () => {
    await discovered();
    const all = await mockApi.getResults(Q);
    expect(all.total).toBeGreaterThan(1000);
    for (const o of all.items) {
      expect(o.fabricTarget).toBeTruthy();
      expect(o.classification).toBeTruthy();
      expect(o.assessmentRequired).toBe(true);
    }
    const detail = await mockApi.getObject(all.items[0].id);
    expect(detail.notes.join(" ").toLowerCase()).not.toMatch(/automatically migrated|100%|no changes required/);
    const graph = await mockApi.getDependencies();
    // Waves never schedule an object before what it depends on.
    const wave = new Map(graph.nodes.map((n) => [n.id, n.wave]));
    for (const e of graph.edges) expect(wave.get(e.source)!).toBeGreaterThanOrEqual(wave.get(e.target)!);
  }, 20000);

  it("simulates a run only once the Fabric target is connected, deferring what this session does not move", async () => {
    await discovered();
    const graph = await mockApi.getDependencies();
    const movable = ["Notebook", "View", "Stored Procedure"];
    const now = graph.nodes.filter((n) => movable.includes(n.type)).slice(0, 3);
    // An integration runtime is always set up by hand, so it is the one thing a run never creates.
    const later = graph.nodes.filter((n) => n.type === "Integration Runtime").slice(0, 2);
    const items = [...now, ...later].map((n) => ({ id: n.id, wave: n.wave }));
    await expect(mockApi.startExecution(items)).rejects.toMatchObject({ code: "target_not_connected" });
    await mockApi.authenticateFabric(FABRIC);
    await mockApi.testFabric(FABRIC);
    const run = await mockApi.startExecution(items);
    expect(run.total).toBe(now.length + later.length);
    await new Promise((r) => setTimeout(r, 3000));
    const done = await mockApi.getExecution();
    expect(done.state).toBe("completed");
    expect(done.completed + done.failed).toBe(now.length);
    expect(done.deferred).toBe(later.length);
    expect(done.items.filter((i) => i.status === "DEFERRED").every((i) => i.type === "Integration Runtime" && !!i.error)).toBe(true);
  }, 20000);

  it("resets a finished run and a finished discovery, and refuses while they are working", async () => {
    resetDemo();
    const graph = await mockApi.getDependencies();
    await mockApi.startExecution(graph.nodes.map((n) => ({ id: n.id, wave: n.wave })));
    await expect(mockApi.controlExecution("reset")).rejects.toMatchObject({ code: "run_in_progress" });
    await mockApi.controlExecution("pause");
    expect((await mockApi.controlExecution("reset")).state).toBe("idle");
    expect((await mockApi.resetDiscovery()).state).toBe("idle");
    await mockApi.startDiscovery();
    await expect(mockApi.resetDiscovery()).rejects.toMatchObject({ code: "discovery_running" });
  });
});

describe("start page", () => {
  it("has no sidebar: a top bar with Connections, and Migration locked until a source connects", async () => {
    go("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Start a migration" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(within(nav).getByRole("link", { name: "Connections" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Migration" })).toBeNull();
    expect(within(nav).getByText("Migration")).toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByRole("list", { name: "Migration workflow" })).toBeNull();
  });

  it("chooses the route from two dropdowns where only Azure Synapse and Microsoft Fabric can be picked", async () => {
    const user = userEvent.setup();
    go("/");
    await screen.findByRole("heading", { level: 1, name: "Start a migration" });
    // Nothing to connect until the route is chosen.
    expect(screen.getByText(/Choose a source and a destination above/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start migration/ })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "From · Source" }));
    let list = screen.getByRole("listbox", { name: "From · Source" });
    const sources = within(list).getAllByRole("option");
    expect(sources.length).toBeGreaterThan(4);
    for (const o of sources) {
      if (o.textContent?.includes("Azure Synapse Analytics")) expect(o).not.toHaveAttribute("aria-disabled");
      else expect(o).toHaveAttribute("aria-disabled", "true");
    }
    // A platform that is coming soon cannot be chosen.
    await user.click(within(list).getByRole("option", { name: /Snowflake/ }));
    expect(screen.getByRole("listbox", { name: "From · Source" })).toBeInTheDocument();
    await user.click(within(list).getByRole("option", { name: /Azure Synapse Analytics/ }));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("button", { name: "From · Source" })).toHaveTextContent("Azure Synapse Analytics");

    await user.click(screen.getByRole("button", { name: "To · Destination" }));
    list = screen.getByRole("listbox", { name: "To · Destination" });
    for (const o of within(list).getAllByRole("option")) {
      if (o.textContent?.includes("Microsoft Fabric")) expect(o).not.toHaveAttribute("aria-disabled");
      else expect(o).toHaveAttribute("aria-disabled", "true");
    }
    await user.click(within(list).getByRole("option", { name: /Microsoft Fabric/ }));

    // Both connection cards appear on the same page.
    expect(screen.getByRole("region", { name: "Source connection: Azure Synapse Analytics" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Destination connection: Microsoft Fabric" })).toBeInTheDocument();
    expect(stored("ma.route")).toEqual({ source: "synapse", destination: "fabric" });
  });

  it("lists every sign-in method, with ZIP and Git shown for later but not selectable", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const source = await screen.findByRole("radiogroup", { name: "How to connect to Azure Synapse" });
    expect(within(source).getAllByRole("radio")).toHaveLength(4);
    for (const m of [/^Azure CLI/, /^Interactive browser/]) expect(within(source).getByRole("radio", { name: m })).not.toHaveAttribute("aria-disabled");
    for (const m of [/Workspace export \(ZIP\)/, /Git repository/]) {
      const tile = within(source).getByRole("radio", { name: m });
      expect(tile).toHaveAttribute("aria-disabled", "true");
      await user.click(tile);
      expect(tile).toHaveAttribute("aria-checked", "false");
    }
    expect(within(source).getByRole("radio", { name: /^Azure CLI/ })).toHaveAttribute("aria-checked", "true");
    for (const gone of [/Service Principal/, /Managed Identity/]) expect(screen.queryByRole("radio", { name: gone })).toBeNull();

    const target = await screen.findByRole("radiogroup", { name: "How to connect to Microsoft Fabric" });
    expect(within(target).getAllByRole("radio")).toHaveLength(2);
    await user.click(within(target).getByRole("radio", { name: /Fabric CLI/ }));
    expect(screen.getByRole("button", { name: "Sign in with Fabric CLI" })).toBeInTheDocument();
    // No secret is ever typed on this page.
    expect(screen.queryByLabelText(/token|secret|password|Client ID/i)).toBeNull();
  });

  it("requires the tenant for the browser sign-in and says where the window opens", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const card = await screen.findByRole("region", { name: /^Source connection/ });
    await user.click(await within(card).findByRole("radio", { name: /Interactive browser/ }));
    await user.type(within(card).getByLabelText("Subscription ID"), CONFIG.subscriptionId);
    await user.click(within(card).getByRole("button", { name: "Authenticate" }));
    expect(await within(card).findByText("Tenant ID is required.")).toBeInTheDocument();
    await user.type(within(card).getByLabelText("Tenant ID"), "8a24d8ed-7a4b-45b3-b56b-d781dd225aa1");
    await user.click(within(card).getByRole("button", { name: "Authenticate" }));
    expect(await within(card).findByText(/A sign-in window should open on the machine running the accelerator/)).toBeInTheDocument();
    await within(card).findByLabelText("Resource group", {}, { timeout: 4000 });
  }, 15000);

  it("keeps Start migration disabled until both sides are signed in, chosen and tested", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const start = () => screen.getByRole("button", { name: /Start migration/ });
    const source = await screen.findByRole("region", { name: /^Source connection/ });
    expect(start()).toBeDisabled();

    // Source: sign in, choose the workspace, test.
    expect(await within(source).findByRole("button", { name: "Test connection" })).toBeDisabled();
    await user.click(within(source).getByRole("radio", { name: /^Azure CLI/ })); // the form remembers the last method
    await user.clear(within(source).getByLabelText("Subscription ID"));
    await user.type(within(source).getByLabelText("Subscription ID"), CONFIG.subscriptionId);
    await user.click(within(source).getByRole("button", { name: "Sign in with Azure" }));
    const group = await within(source).findByLabelText("Resource group", {}, { timeout: 4000 });
    await waitFor(() => expect(within(group).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(group, "rg-demo-migration");
    const ws = within(source).getByLabelText("Synapse workspace");
    await waitFor(() => expect(within(ws).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(ws, "demo-synapse-ws");
    // The workspace has a single dedicated SQL pool: it is chosen without asking.
    await waitFor(() => expect(within(source).getByLabelText(/Dedicated SQL pool/)).toHaveValue("TransportDW"));
    await user.click(within(source).getByRole("button", { name: "Test connection" }));
    await within(source).findByText("demo-synapse-ws", { selector: "strong" }, { timeout: 8000 });
    expect(start()).toBeDisabled();

    // Destination: sign in, choose the workspace, test.
    const target = screen.getByRole("region", { name: /^Destination connection/ });
    await user.click(await within(target).findByRole("button", { name: "Sign in with Azure CLI" }));
    const fabricWs = within(target).getByLabelText("Fabric workspace");
    await waitFor(() => expect(fabricWs).toBeEnabled(), { timeout: 4000 });
    await user.selectOptions(fabricWs, "Fabric_demo");
    await user.click(within(target).getByRole("button", { name: "Test connection" }));
    await within(target).findByText("Fabric_demo", { selector: "strong" }, { timeout: 4000 });

    await waitFor(() => expect(start()).toBeEnabled());
    await user.click(start());
    expect(await stepHeading("Discover")).toBeInTheDocument();
  }, 40000);

  it("asks before disconnecting a source whose discovery would be lost", async () => {
    resetDemo();
    const user = userEvent.setup();
    go("/");
    const source = await screen.findByRole("region", { name: /^Source connection/ });
    await user.click(await within(source).findByRole("button", { name: "Disconnect" }, { timeout: 5000 }));
    let dialog = screen.getByRole("dialog", { name: "Disconnect Azure Synapse?" });
    expect(within(dialog).getByText(/discovered inventory \([\d,]+ objects\) is cleared/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect((await mockApi.getConnection()).status).toBe("connected");
    await user.click(within(source).getByRole("button", { name: "Disconnect" }));
    dialog = screen.getByRole("dialog", { name: "Disconnect Azure Synapse?" });
    await user.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await waitFor(async () => expect((await mockApi.getConnection()).status).not.toBe("connected"));
  }, 15000);
});

describe("starting over", () => {
  it("disconnecting Fabric starts Migrate and Validate over, and disconnecting the source starts everything over", async () => {
    resetDemo();
    const items = await seedPlan(2);
    await mockApi.startExecution(items);
    await new Promise((r) => setTimeout(r, 2500));
    unlockTo("validate");
    const user = userEvent.setup();
    go("/");
    // Fabric: the run's record and the later confirmations go; the discovery and the plan stay.
    const target = await screen.findByRole("region", { name: /^Destination connection/ });
    await user.click(await within(target).findByRole("button", { name: "Disconnect" }, { timeout: 5000 }));
    let dialog = screen.getByRole("dialog", { name: "Disconnect Microsoft Fabric?" });
    expect(within(dialog).getByText(/the record of migration run #\d+ is cleared/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(stored(JOURNEY)).toEqual(["discover", "assess", "waves", "plan"]));
    expect((await mockApi.getExecution()).state).toBe("idle");
    expect(stored(PLAN)).toHaveLength(2);
    // The source: everything that was built on it goes.
    const source = screen.getByRole("region", { name: /^Source connection/ });
    await user.click(within(source).getByRole("button", { name: "Disconnect" }));
    dialog = screen.getByRole("dialog", { name: "Disconnect Azure Synapse?" });
    expect(within(dialog).getByText(/This starts the migration over/)).toBeInTheDocument();
    expect(within(dialog).getByText(/the plan \(2 objects\)/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(stored(JOURNEY)).toEqual([]));
    expect(stored(PLAN)).toEqual([]);
    expect((await mockApi.getDiscoveryStatus()).state).toBe("idle");
  }, 25000);

  it("asks once for an older Live backend that does not update itself to be replaced, instead of failing on calls it does not know", async () => {
    localStorage.setItem("ma.apiMode", "real");
    liveBackend({ up: true, boot: "", apiVersion: 0, supervised: false, connection: SIGNED_OUT, fabric: { status: "disconnected" } });
    go("/");
    expect(await screen.findByText("The accelerator's backend is an older version that does not update itself.")).toBeInTheDocument();
    expect(screen.getByText(/replaces the old backend with one that restarts by itself/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  }, 15000);
});

/* ---- a Live backend, played by the test ---------------------------------------------------- */

interface LiveBackend {
  up: boolean;
  boot: string;
  apiVersion?: number;
  supervised?: boolean;
  busy?: string[];
  restored?: RestoredState | null;
  connection: ConnectionState;
  fabric: Partial<FabricTarget>;
}

const SIGNED_OUT: ConnectionState = { status: "disconnected", ok: true, signedIn: false, checks: [] };
const CONNECTED: ConnectionState = {
  status: "connected", ok: true, signedIn: true, method: "azure_cli", workspace: "ws-live", sqlPool: "pool01",
  subscriptionId: CONFIG.subscriptionId, resourceGroup: "rg", checks: [],
};
const FABRIC_CONNECTED: Partial<FabricTarget> = {
  status: "connected", method: "azure_cli", workspaceId: "11111111-2222-3333-4444-555555555555", workspaceName: "Fabric WS",
  workspaces: [{ id: "11111111-2222-3333-4444-555555555555", name: "Fabric WS" }], capacityAssigned: true, checks: [],
};

/** Answers the page's calls from `state`, or not at all while `state.up` is false (a restart). */
function liveBackend(state: LiveBackend): LiveBackend {
  const idleRun = { runId: "", state: "idle", total: 0, completed: 0, inProgress: 0, failed: 0, pending: 0, skipped: 0, deferred: 0, items: [], logs: [] };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (!state.up) throw new TypeError("Failed to fetch");
    const bodies: Record<string, unknown> = {
      "/api/health": {
        status: "ok", apiVersion: state.apiVersion ?? REQUIRED_API_VERSION, bootId: state.boot, supervised: state.supervised ?? true,
        busy: state.busy ?? [], restored: state.restored ?? null,
        capabilities: { authMethods: ["azure_cli", "interactive_browser"], discoveryScope: [] },
      },
      "/api/connections": state.connection,
      "/api/discovery/status": { state: "idle", startedAt: null, finishedAt: null, error: null, workspace: null, progress: null, summary: null },
      "/api/fabric/connection": state.fabric,
      "/api/migration/run": idleRun,
      "/api/migration/capabilities": { stages: [], defaults: {}, linkedServices: [] },
    };
    return { ok: true, status: 200, json: async () => bodies[url.split("?")[0]] ?? {} };
  }));
  return state;
}

describe("a backend that restarts itself", () => {
  const watch = { ...BACKEND_WATCH };
  const grace = { ...RESTART_GRACE };
  const REAL_PLAN = "ma.plan.real.default";
  const REAL_JOURNEY = "ma.journey.real.default";
  beforeEach(() => {
    Object.assign(BACKEND_WATCH, { everyMs: 60, awayEveryMs: 40, giveUpMs: 20000 });
    Object.assign(RESTART_GRACE, { stepMs: 20 });
    localStorage.setItem("ma.apiMode", "real");
  });
  afterEach(() => {
    Object.assign(BACKEND_WATCH, watch);
    Object.assign(RESTART_GRACE, grace);
  });

  it("waits quietly while it restarts, reads again what it kept, says so, and starts nothing over", async () => {
    localStorage.setItem(REAL_PLAN, JSON.stringify([{ id: "sql://ws-live/pool01/dbo/Orders", wave: 1 }]));
    localStorage.setItem(REAL_JOURNEY, JSON.stringify(["discover", "assess", "waves", "plan"]));
    const backend = liveBackend({ up: true, boot: "A", connection: CONNECTED, fabric: FABRIC_CONNECTED });
    go("/");
    expect((await screen.findAllByText("ws-live", {}, { timeout: 5000 })).length).toBeGreaterThan(0);

    backend.up = false; // the code changed: the backend restarts
    expect(await screen.findByText("Reconnecting to the backend…")).toBeInTheDocument();
    expect(screen.queryByText("The accelerator's backend is not answering.")).toBeNull();

    // Back, as a new process. Say this one could not take the source connection back.
    Object.assign(backend, {
      up: true, boot: "B", connection: SIGNED_OUT,
      restored: { at: "2026-10-07T10:00:00+00:00", kept: ["the discovery (12 objects)", "migration run #003"], notes: ["Sign in to Synapse again: the last sign-in could not be restored."] },
    });
    expect(await screen.findByText(/^The backend restarted and kept the discovery \(12 objects\) and migration run #003\. Sign in to Synapse again/, {}, { timeout: 5000 })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Reconnecting to the backend…")).toBeNull());
    await waitFor(() => expect(screen.queryAllByText("ws-live")).toHaveLength(0)); // read again: signed out now
    // A restart is not the operator signing out: the plan and the confirmed steps stay.
    expect(stored(REAL_PLAN)).toEqual([{ id: "sql://ws-live/pool01/dbo/Orders", wave: 1 }]);
    expect(stored(REAL_JOURNEY)).toEqual(["discover", "assess", "waves", "plan"]);
  }, 20000);

  it("says an outdated backend that updates itself is updating, and waits for the work it names", async () => {
    liveBackend({ up: true, boot: "A", apiVersion: REQUIRED_API_VERSION - 1, supervised: true, busy: ["the migration run"], connection: SIGNED_OUT, fabric: { status: "disconnected" } });
    go("/");
    expect(await screen.findByText("The backend is updating.")).toBeInTheDocument();
    expect(screen.getByText(/restarts by itself on the latest code once the migration run finishes, and keeps your work/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull(); // nothing to do
  }, 15000);

  it("reports a backend gone for good as not answering, and reconnects by itself when it is back", async () => {
    Object.assign(BACKEND_WATCH, { giveUpMs: 300 });
    const backend = liveBackend({ up: true, boot: "A", connection: CONNECTED, fabric: { status: "disconnected" } });
    go("/");
    expect((await screen.findAllByText("ws-live", {}, { timeout: 5000 })).length).toBeGreaterThan(0);
    backend.up = false;
    expect(await screen.findByText("The accelerator's backend is not answering.", {}, { timeout: 5000 })).toBeInTheDocument();
    backend.up = true; // started again by hand, the same process kept nothing to say
    await waitFor(() => expect(screen.queryByText("The accelerator's backend is not answering.")).toBeNull(), { timeout: 5000 });
    expect(screen.queryByText(/The backend restarted/)).toBeNull(); // the same process: no restart to report
  }, 20000);
});

describe("migration journey", () => {
  it("asks to connect first when nothing is connected", async () => {
    go("/migration");
    expect(await screen.findByText("Connect both sides first")).toBeInTheDocument();
  });

  it("shows the six steps left to right, locks the ones after the open step, and Next unlocks the following one", async () => {
    resetDemo();
    const user = userEvent.setup();
    go("/migration");
    expect(await stepHeading("Discover")).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Migration steps" });
    const boxes = within(nav).getAllByRole("button");
    expect(boxes.map((b) => b.getAttribute("aria-label")?.match(/^Step \d: (\w+)/)?.[1])).toEqual(["Discover", "Assess", "Waves", "Plan", "Migrate", "Validate"]);
    expect(boxes[0]).toHaveAttribute("aria-current", "step");
    for (const b of boxes.slice(1)) expect(b).toBeDisabled();
    expect(document.title).toBe("Discover · Migration · Migration Accelerator");

    // Discovery is already done in the demo, so the step can be confirmed.
    await user.click(await screen.findByRole("button", { name: "Looks good, continue to Assess" }, { timeout: 5000 }));
    expect(await stepHeading("Assess")).toBeInTheDocument();
    expect(stepBox(2)).toBeEnabled();
    expect(stepBox(2)).toHaveAttribute("aria-current", "step");
    expect(stepBox(3)).toBeDisabled();
    expect(window.location.search).toBe("?step=assess");
    expect(stored(JOURNEY)).toEqual(["discover"]);

    // Back returns to a confirmed step, which then offers the way forward again.
    await user.click(screen.getByRole("button", { name: "Discover" }));
    expect(await stepHeading("Discover")).toBeInTheDocument();
    expect(screen.getByText("Discover confirmed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to Assess" })).toBeInTheDocument();
  }, 20000);

  it("opens any unlocked step by clicking its box, and falls back to the open step for a locked one", async () => {
    resetDemo();
    unlockTo("waves");
    const user = userEvent.setup();
    go("/migration?step=validate");
    // Validate is locked, so the open step (Waves) is shown instead.
    expect(await stepHeading("Waves")).toBeInTheDocument();
    await user.click(stepBox(1));
    expect(await stepHeading("Discover")).toBeInTheDocument();
    await user.click(stepBox(2));
    expect(await stepHeading("Assess")).toBeInTheDocument();
    expect(stepBox(4)).toBeDisabled();
  }, 15000);

  it("marks a confirmed step whose results are gone as Run again, and keeps the steps after it locked", async () => {
    resetDemo();
    await seedPlan();
    unlockTo("validate"); // Migrate is confirmed, but there is no run (as after a reload of Demo data)
    go("/migration");
    expect(await stepHeading("Migrate")).toBeInTheDocument();
    await waitFor(() => expect(stepBox(5)).toHaveAccessibleName(/Run again/));
    expect(stepBox(6)).toBeDisabled();
    expect(screen.getByText("Migrate needs to run again")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Migration complete" })).toBeNull();
    // The confirmation itself is kept: once the run exists again, the journey comes back.
    expect(stored(JOURNEY)).toContain("migrate");
  }, 15000);

  it("asks before running discovery again when later steps are confirmed", async () => {
    resetDemo();
    unlockTo("waves");
    const user = userEvent.setup();
    go("/migration?step=discover");
    await user.click(await screen.findByRole("button", { name: "Run again" }, { timeout: 5000 }));
    const dialog = screen.getByRole("dialog", { name: "Run discovery again?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(stored(JOURNEY)).toEqual(["discover", "assess"]);
  }, 15000);

  it.each([
    ["/discovery", "Discover"],
    ["/assessment", "Assess"],
    ["/dependencies", "Waves"],
    ["/execute", "Migrate"],
  ])("sends the old %s URL to its step", async (path, title) => {
    resetDemo();
    await seedPlan();
    unlockTo("validate");
    go(path);
    expect(await stepHeading(title)).toBeInTheDocument();
  });

  it.each(["/synapse", "/fabric", "/connections", "/nowhere/at/all"])("sends %s to the start page", async (path) => {
    go(path);
    expect(await screen.findByRole("heading", { level: 1, name: "Start a migration" })).toBeInTheDocument();
  });
});

describe("resetting a step", () => {
  it("asks first, then clears the step and the ones after it, and nothing before it", async () => {
    resetDemo();
    await seedPlan();
    unlockTo("migrate"); // Discover to Plan confirmed
    const user = userEvent.setup();
    go("/migration?step=plan");
    expect(await stepHeading("Plan")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Reset step" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Reset step" }));
    let dialog = screen.getByRole("dialog", { name: "Reset Plan?" });
    expect(within(dialog).getByText(/The plan: 3 objects/)).toBeInTheDocument();
    expect(within(dialog).getByText("Your confirmation of Plan.")).toBeInTheDocument();
    // Focus starts on Cancel, so Enter never resets by accident; Cancel changes nothing.
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(stored(PLAN)).toHaveLength(3);

    await user.click(screen.getByRole("button", { name: "Reset step" }));
    dialog = screen.getByRole("dialog", { name: "Reset Plan?" });
    await user.click(within(dialog).getByRole("button", { name: "Reset Plan" }));
    expect(await screen.findByRole("button", { name: /^Add all .* objects/ }, { timeout: 5000 })).toBeInTheDocument();
    expect(stored(PLAN)).toEqual([]);
    expect(stored(JOURNEY)).toEqual(["discover", "assess", "waves"]);
    expect(screen.getByText(/Plan was reset, with the steps after it/)).toBeInTheDocument();
  }, 20000);

  it("resets Discover to an empty workspace, ready to run again", async () => {
    resetDemo();
    unlockTo("assess");
    const user = userEvent.setup();
    go("/migration?step=discover");
    expect(await stepHeading("Discover")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Reset step" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Reset step" }));
    const dialog = screen.getByRole("dialog", { name: "Reset Discover?" });
    expect(within(dialog).getByText(/The discovered inventory: [\d,]+ objects/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Reset Discover" }));
    expect(await screen.findByRole("button", { name: "Run discovery" }, { timeout: 5000 })).toBeEnabled();
    expect((await mockApi.getDiscoveryStatus()).state).toBe("idle");
    expect(stored(JOURNEY)).toEqual([]);
  }, 20000);

  it("resets Migrate by forgetting the run, so a fresh run can start", async () => {
    resetDemo();
    const items = await seedPlan(2);
    await mockApi.startExecution(items);
    await new Promise((r) => setTimeout(r, 2500));
    unlockTo("validate");
    const user = userEvent.setup();
    go("/migration?step=migrate");
    expect(await stepHeading("Migrate")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Reset step" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Reset step" }));
    const dialog = screen.getByRole("dialog", { name: "Reset Migrate?" });
    expect(within(dialog).getByText(/The record of migration run #\d+/)).toBeInTheDocument();
    expect(within(dialog).getByText(/anything already created in Fabric/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Reset Migrate" }));
    expect(await screen.findByText("Nothing is running.", {}, { timeout: 5000 })).toBeInTheDocument();
    expect((await mockApi.getExecution()).state).toBe("idle");
    expect(stored(JOURNEY)).toEqual(["discover", "assess", "waves", "plan"]);
  }, 20000);
});

describe("assistant", () => {
  it("opens from the + button (or Ctrl+I) as a panel on the right, marked as a preview, and closes with Escape", async () => {
    const user = userEvent.setup();
    go("/");
    await user.click(await screen.findByRole("button", { name: "Open the migration assistant (Ctrl+I)" }));
    const panel = screen.getByRole("complementary", { name: "Migration assistant" });
    expect(within(panel).getByText("Preview")).toBeInTheDocument();
    expect(within(panel).getByLabelText("Message the assistant")).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Send" })).toBeDisabled();
    // The launcher hides while the panel is open.
    expect(screen.queryByRole("button", { name: "Open the migration assistant (Ctrl+I)" })).toBeNull();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("complementary", { name: "Migration assistant" })).toBeNull();
    await user.keyboard("{Control>}i{/Control}");
    expect(screen.getByRole("complementary", { name: "Migration assistant" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close the assistant" }));
    expect(screen.getByRole("button", { name: "Open the migration assistant (Ctrl+I)" })).toBeInTheDocument();
  });
});

describe("projects and the frame", () => {
  it("creates (with Enter), renames and deletes projects from the project menu", async () => {
    const user = userEvent.setup();
    go("/");
    await user.click(await screen.findByRole("button", { name: /^Project: / }));
    await user.click(screen.getByRole("menuitem", { name: "New project…" }));
    // The name field has focus: typing goes straight into it, and Enter creates the project.
    await user.keyboard("Wave 2 rollout{Enter}");
    expect(await screen.findByRole("button", { name: "Project: Wave 2 rollout" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Project: Wave 2 rollout" }));
    await user.click(screen.getByRole("menuitem", { name: "Rename this project…" }));
    const field = screen.getByLabelText("Project name");
    await user.clear(field);
    await user.type(field, "Finance estate{Enter}");
    expect(await screen.findByRole("button", { name: "Project: Finance estate" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Project: Finance estate" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete this project…" }));
    await user.click(within(screen.getByRole("dialog", { name: /Delete/ })).getByRole("button", { name: "Delete project" }));
    expect(await screen.findByRole("button", { name: "Project: Synapse to Fabric migration" })).toBeInTheDocument();
    // The only project left cannot be deleted.
    await user.click(screen.getByRole("button", { name: "Project: Synapse to Fabric migration" }));
    expect(screen.getByRole("menuitem", { name: "Delete this project…" })).toBeDisabled();
  }, 15000);

  it("says plainly when the backend is not answering in Live mode, with a retry and a way to Demo data", async () => {
    localStorage.setItem("ma.apiMode", "real");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const user = userEvent.setup();
    go("/");
    expect(await screen.findByText("The accelerator's backend is not answering.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Use Demo data" }));
    await waitFor(() => expect(screen.queryByText("The accelerator's backend is not answering.")).toBeNull());
    expect(screen.getByRole("button", { name: "Demo" })).toHaveAttribute("aria-pressed", "true");
  }, 15000);
});

describe("discover step", () => {
  it("summarises the discovery, then shows the inventory with Fabric equivalents and opens an object", async () => {
    await discovered();
    const user = userEvent.setup();
    go("/migration?step=discover");
    expect(await stepHeading("Discover")).toBeInTheDocument();
    const summary = await screen.findByLabelText("Discovery summary", {}, { timeout: 5000 });
    for (const label of ["Objects", "Object types", "With a Fabric mapping", "Warnings"]) expect(within(summary).getByText(label)).toBeInTheDocument();
    // The type tiles are buttons in a list, so screen readers announce both.
    const types = screen.getByRole("list", { name: "Discovered object types" });
    expect(within(types).getAllByRole("button").length).toBeGreaterThan(5);
    await user.click(screen.getByRole("tab", { name: /^Inventory/ }));
    const table = await screen.findByRole("table", {}, { timeout: 5000 });
    for (const h of ["Fabric equivalent", "Migration classification", "Object name", "Synapse component"]) {
      expect(within(table).getByText(h)).toBeInTheDocument();
    }
    const rows = await within(table).findAllByRole("row", { name: /Open details/ }, { timeout: 8000 });
    await user.click(rows[0]);
    const dialog = await screen.findByRole("dialog", {}, { timeout: 5000 });
    await within(dialog).findByText("Fabric target", { selector: "h3" }, { timeout: 5000 });
    expect(within(dialog).getByText(/Recommended migration steps/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /Add to Migration Plan/ })).toBeInTheDocument();
  }, 25000);

  it("opens what each summary card counts: the inventory narrowed to its objects, or the types", async () => {
    await discovered();
    const user = userEvent.setup();
    go("/migration?step=discover");
    const summary = await screen.findByLabelText("Discovery summary", {}, { timeout: 5000 });
    const card = (label: string) => within(summary).getByRole("button", { name: new RegExp(`^${label}`) });
    const count = (label: string) => within(card(label)).getByRole("definition").textContent!;

    for (const label of ["With a Fabric mapping", "Manual or review", "Objects"]) {
      await user.click(card(label));
      expect(card(label)).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByRole("tab", { name: /^Inventory/ })).toHaveAttribute("aria-selected", "true");
      // The inventory shows exactly the objects the card counted.
      expect(await screen.findByText(new RegExp(`of ${count(label)}$`), {}, { timeout: 8000 })).toBeInTheDocument();
    }

    await user.click(card("Object types"));
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("list", { name: "Discovered object types" })).toBeInTheDocument();
  }, 40000);
});

describe("assess step", () => {
  it("counts how objects move, filters by a class, and offers the component mapping", async () => {
    await discovered();
    unlockTo("assess");
    const user = userEvent.setup();
    go("/migration?step=assess");
    const strip = await screen.findByRole("list", { name: "How objects move" }, { timeout: 5000 });
    for (const label of ["Direct", "Reconfigure", "Transform", "Manual", "Review"]) expect(within(strip).getByText(label)).toBeInTheDocument();
    const manual = within(strip).getByRole("button", { name: /Manual/ });
    await user.click(manual);
    expect(manual).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("tab", { name: /^Objects/ })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("tab", { name: "Component mapping" }));
    expect(await screen.findByText("Serverless SQL", {}, { timeout: 4000 })).toBeInTheDocument();
  }, 20000);
});

describe("plan step", () => {
  it("builds a plan, scores it, configures every stage without running any, and will not move on while risks block it", async () => {
    await discovered();
    unlockTo("plan");
    const user = userEvent.setup();
    go("/migration?step=plan");
    // Re-query: the panel re-renders when the graph arrives, replacing the element.
    await waitFor(() => expect(screen.getByRole("button", { name: /^Add all .* objects/ })).toBeEnabled(), { timeout: 8000 });
    await user.click(screen.getByRole("button", { name: /^Add all .* objects/ }));
    const summary = await screen.findByLabelText("Plan summary", {}, { timeout: 5000 });
    expect(within(summary).getByText("Readiness")).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: "Migration risks" })).toBeInTheDocument();
    // The Fabric target is not connected in this test, so the planner blocks and Next stays shut.
    expect(screen.getByRole("button", { name: /Continue to Migrate/ })).toBeDisabled();

    await user.click(screen.getByRole("tab", { name: "Strategy by type" }));
    expect(screen.getByRole("table", { name: "Migration strategy by object type" })).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Stages & credentials" }));
    // Every stage is offered and on by default. Plan only configures: runs start in the Migrate step.
    for (const label of ["Warehouse & schema", "Table data", "Spark pool & environment", "Notebooks", "Connections", "Pipelines & datasets", "Spark job definitions", "SQL scripts", "Schedules", "External tables"]) {
      expect(await screen.findByRole("checkbox", { name: `Include ${label}` })).toBeChecked();
    }
    expect(screen.queryAllByRole("button", { name: /Run this stage/ })).toHaveLength(0);
    // The run strategy is a choice, and stop-on-failure is off by default.
    expect(screen.getByRole("radio", { name: /Automated objects only/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Stop at the end of a wave/ })).not.toBeChecked();
  }, 30000);

  it("switching a stage off marks its objects Not selected, and credentials never reach browser storage", async () => {
    await discovered();
    unlockTo("plan");
    const user = userEvent.setup();
    go("/migration?step=plan");
    await waitFor(() => expect(screen.getByRole("button", { name: /^Add all .* objects/ })).toBeEnabled(), { timeout: 8000 });
    await user.click(screen.getByRole("button", { name: /^Add all .* objects/ }));
    await user.click(await screen.findByRole("tab", { name: "Stages & credentials" }));
    const pipelines = await screen.findByRole("checkbox", { name: "Include Pipelines & datasets" });
    await user.click(pipelines);
    expect(pipelines).not.toBeChecked();
    // Data load options are a strategy the user can change: pipelines created and run, or created only.
    const mode = screen.getByLabelText("If a table already has rows");
    await user.selectOptions(mode, "replace");
    expect(mode).toHaveValue("replace");
    const runMode = screen.getByLabelText("What the stage does");
    await user.selectOptions(runMode, "create");
    expect(runMode).toHaveValue("create");
    // Two stages ask for credentials: Table data (the Synapse pool connection) and Connections (linked services).
    const sections = screen.getAllByText(/Credentials/, { selector: "summary span" });
    expect(sections).toHaveLength(2);
    for (const section of sections) await user.click(section);
    expect(await screen.findByLabelText("Password for synapse-demo-synapse-ws-TransportDW")).toHaveAttribute("type", "password");
    // Credentials: typed into a password field, kept out of localStorage and sessionStorage.
    const secret = (await screen.findAllByLabelText(/^Password for /))[0];
    expect(secret).toHaveAttribute("type", "password");
    await user.type(secret, "S3CRET-NEVER-STORED");
    const kept = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    expect(kept).not.toContain("S3CRET-NEVER-STORED");
  }, 30000);

  it("keeps a project's stage options after a reload, including every stage off", async () => {
    resetDemo();
    await seedPlan();
    unlockTo("plan");
    const user = userEvent.setup();
    const openStages = async () => user.click(await screen.findByRole("tab", { name: "Stages & credentials" }, { timeout: 5000 }));
    const first = go("/migration?step=plan");
    await openStages();
    await user.click(await screen.findByRole("checkbox", { name: "Include Pipelines & datasets" }));
    first.unmount();

    const second = go("/migration?step=plan");
    await openStages();
    expect(await screen.findByRole("checkbox", { name: "Include Pipelines & datasets" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Include Notebooks" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "All off" }));
    second.unmount();

    go("/migration?step=plan");
    await openStages();
    expect(await screen.findByRole("checkbox", { name: "Include Notebooks" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Include Warehouse & schema" })).not.toBeChecked();
  }, 25000);
});

describe("plan tab", () => {
  it("lists the objects behind a risk, finds one in the plan, and a confirmed plan that gains a blocking risk is not done", async () => {
    resetDemo();
    const graph = await mockApi.getDependencies();
    const byId = new Map(graph.nodes.map((n) => [n.id, n]));
    const edge = graph.edges.find((e) => byId.get(e.source)?.type === "View" && byId.get(e.target)?.type === "Table")!;
    const view = byId.get(edge.source)!, table = byId.get(edge.target)!;
    // The view runs in wave 1, before the table it reads (wave 2): a blocking risk.
    localStorage.setItem(PLAN, JSON.stringify([{ id: view.id, wave: 1 }, { id: table.id, wave: 2 }]));
    unlockTo("migrate"); // Plan was confirmed before the change
    const user = userEvent.setup();
    go("/migration?step=plan");
    expect(await stepHeading("Plan")).toBeInTheDocument();
    await waitFor(() => expect(stepBox(4)).toHaveAccessibleName(/Fix risks/), { timeout: 5000 });
    expect(stepBox(5)).toBeDisabled();
    expect(screen.getByText("1 blocking risk")).toBeInTheDocument();
    await user.click(screen.getAllByText("Show the object")[0]);
    await user.click(screen.getAllByRole("button", { name: /Find in plan/ })[0]);
    expect(screen.getByRole("tab", { name: /^Objects in plan/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("Find in the plan")).toHaveValue(view.name);
    expect(await screen.findByText(view.name, { selector: "td" })).toBeInTheDocument();
    // Moving the view after the table it reads resolves it, and the plan is done again.
    await user.selectOptions(screen.getByLabelText(`Wave for ${view.name}`), "2");
    await waitFor(() => expect(stepBox(4)).toHaveAccessibleName(/Done/), { timeout: 5000 });
    expect(stepBox(5)).toBeEnabled();
  }, 25000);

  it("adds a whole type, says when everything is in, finds objects, and asks before clearing the plan", async () => {
    resetDemo();
    await seedPlan(2);
    unlockTo("plan");
    const user = userEvent.setup();
    go("/migration?step=plan");
    await user.click(await screen.findByRole("tab", { name: /^Objects in plan/ }, { timeout: 5000 }));
    await user.selectOptions(screen.getByLabelText("Add every object of a type"), "Table");
    await waitFor(() => expect(stored(PLAN).length).toBeGreaterThan(700));
    expect(screen.getByText(/Table objects added to the plan/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Add the .* not in the plan/ }));
    expect(screen.getByRole("button", { name: "Every object is in the plan" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Move .* up/ })).toBeNull(); // order inside a wave is by type, not by hand
    await user.type(screen.getByLabelText("Find in the plan"), "FactSales");
    expect(screen.getByText(/objects match/)).toBeInTheDocument();
    // Clearing asks first, and Cancel keeps everything.
    await user.click(screen.getByRole("button", { name: "Clear plan" }));
    await user.click(within(screen.getByRole("dialog", { name: "Clear the plan?" })).getByRole("button", { name: "Cancel" }));
    expect(stored(PLAN).length).toBeGreaterThan(1000);
    await user.click(screen.getByRole("button", { name: "Clear plan" }));
    await user.click(within(screen.getByRole("dialog", { name: "Clear the plan?" })).getByRole("button", { name: "Clear plan" }));
    expect(stored(PLAN)).toEqual([]);
    expect(await screen.findByRole("button", { name: /^Add all .* objects/ })).toBeInTheDocument();
  }, 25000);
});

describe("migrate step", () => {
  it("offers to run every stage or a single one, here and not in Plan", async () => {
    resetDemo();
    await seedPlan();
    unlockTo("migrate");
    go("/migration?step=migrate");
    await screen.findByRole("combobox", { name: "What to run" }, { timeout: 5000 });
    // Re-query: the panel re-renders as the plan is scored.
    await waitFor(() => {
      const choice = screen.getByRole("combobox", { name: "What to run" });
      expect(within(choice).getByRole("option", { name: /^Every stage that is on/ })).toBeInTheDocument();
      expect(within(choice).getByRole("option", { name: "Only Notebooks" })).toBeInTheDocument();
    });
  }, 15000);

  it("narrows the object table to what each run card counts", async () => {
    await discovered();
    await mockApi.authenticateFabric(FABRIC);
    await mockApi.testFabric(FABRIC);
    const items = await seedPlan(4);
    await mockApi.startExecution(items);
    await new Promise((r) => setTimeout(r, 2500));
    unlockTo("migrate");
    const user = userEvent.setup();
    go("/migration?step=migrate");
    const card = (label: string) => screen.getByRole("button", { name: new RegExp(`^${label}`) });
    await waitFor(() => expect(card("Migrated")).toBeInTheDocument(), { timeout: 5000 });
    const table = screen.getByRole("table", { name: "Migration state by object" });
    const shown = () => within(table).queryAllByRole("row").length - 1; // less the header row

    await user.click(card("Migrated"));
    expect(card("Migrated")).toHaveAttribute("aria-pressed", "true");
    const migrated = Number(within(card("Migrated")).getByText(/^\d+$/).textContent);
    expect(shown()).toBe(migrated || 1); // an empty view still has its "Nothing in this view" row
    within(table).queryAllByRole("row").slice(1).forEach((r) => migrated && expect(r).toHaveTextContent("COMPLETED"));

    await user.click(screen.getByRole("button", { name: "Show every object" }));
    expect(card("Total")).toHaveAttribute("aria-pressed", "true");
    expect(shown()).toBe(4);
  }, 25000);
});

describe("validate step", () => {
  it("compares both sides, filters by result, and Finish completes the migration with a report to download", async () => {
    await discovered();
    await mockApi.authenticateFabric(FABRIC);
    await mockApi.testFabric(FABRIC);
    const items = await seedPlan(2);
    await mockApi.startExecution(items);
    await new Promise((r) => setTimeout(r, 2500));
    unlockTo("validate");
    const user = userEvent.setup();
    go("/migration?step=validate");
    expect(await stepHeading("Validate")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Finish migration/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Run validation" }));
    const table = await screen.findByRole("table", { name: "Validation results" }, { timeout: 5000 });
    expect(within(table).getByRole("columnheader", { name: "Details" })).toBeInTheDocument();
    const match = screen.getByRole("button", { name: /^Match/ });
    await user.click(match);
    expect(match).toHaveAttribute("aria-pressed", "true");
    expect(within(table).getAllByText(/MATCH/).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: /Finish migration/ }));
    expect(await screen.findByRole("heading", { name: "Migration complete" })).toBeInTheDocument();
    expect(screen.getByLabelText("Migration result")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download report (CSV)" })).toBeInTheDocument();
  }, 25000);
});

describe("report", () => {
  it("lists every object and check as CSV, quoting what needs it and never anything secret", () => {
    const run: ExecutionRun = {
      runId: "007", state: "completed", total: 1, completed: 1, inProgress: 0, failed: 0, pending: 0, skipped: 0, deferred: 0, logs: [],
      items: [{ id: "a", name: 'dbo.Fact, "Sales"', type: "Table", wave: 2, step: "Table created", status: "COMPLETED", startedAt: null, completedAt: "2026-10-06T12:00:00Z", error: null, target: "WH / dbo.Fact" }],
    };
    const csv = buildReport({ run, validation: [{ category: "Tables", object: "dbo.Fact", source: "8 columns", target: "8 columns", status: "MATCH", detail: "Same columns and types." }], project: "Finance", source: "ws", target: "Fabric_demo" });
    expect(csv).toContain('"Migration","dbo.Fact, ""Sales""","Table","2","COMPLETED","Table created","WH / dbo.Fact","","2026-10-06T12:00:00Z"');
    expect(csv).toContain('"Validation","dbo.Fact","Tables","8 columns","8 columns","MATCH","Same columns and types."');
    expect(csv.split("\r\n")[0]).toBe('"Migration report","Finance"');
  });
});
