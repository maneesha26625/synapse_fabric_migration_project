import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { TransformPanel } from "../components/discovery/TransformPanel";
import { TransformQueue } from "../components/discovery/TransformQueue";
import { AppStateProvider } from "../state/AppState";
import { buildReport } from "../components/journey/report";
import { buildTree } from "../components/journey/WorkspaceExplorer";
import { mockApi, resetDemo, setDemoConsultation } from "../mock/mockApi";
import type { ConnectionConfig, ExecutionRun, FabricConfig, ResultsQuery, RunOptions } from "../types";

const CONFIG: ConnectionConfig = {
  method: "azure_cli", tenantId: "", subscriptionId: "10eb96c3-ba3c-492e-b95b-e9f1d6d85d70",
  resourceGroup: "rg-demo-migration", workspace: "demo-synapse-ws", workspaceUrl: "", sqlPool: "", resource: "",
};
const FABRIC: FabricConfig = { method: "azure_cli", workspaceId: "", workspaceName: "Fabric_demo" };
const Q: ResultsQuery = {
  search: "", categories: [], types: [], statuses: [], fabricTargets: [], paths: [], workstreams: [], mappingStatuses: [],
  classifications: [], assessment: "", sort: "name", dir: "asc", page: 1, pageSize: 200,
};
const STEP_ORDER = ["discover", "assess", "consult", "waves", "plan", "migrate", "validate"];
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
  // Runs start without an approved brief unless a test asks for one: the consultation's own tests switch it back.
  setDemoConsultation("advisory");
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

  it("loads a filtered table within its filter and registers a synced one with the sync pipeline", async () => {
    resetDemo();
    const graph = await mockApi.getDependencies();
    const [tbl] = graph.nodes.filter((n) => n.type === "Table");
    const filter = { column: "ModifiedAt", from: "2024-10-08", to: "", sync: true, changeColumn: "ModifiedAt", keys: [] };
    await mockApi.startExecution([{ id: tbl.id, wave: tbl.wave }], {
      scope: "automated", stopOnFailure: false, stages: ["data"], dataMode: "if_empty", dataRun: "run", collation: "match_synapse",
      dataFilters: { [tbl.name.toLowerCase()]: filter },
    });
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(Date.now() + 10 * 60_000);
      const done = (await mockApi.getExecution()).items.find((i) => i.type === "Table data")!;
      expect(done.step).toMatch(/counts match within the filter/);
      expect(done.notes?.join(" ")).toMatch(/Loads rows with ModifiedAt on or after 2024-10-08; kept in sync by ModifiedAt/);
      expect(done.notes?.join(" ")).toMatch(/Sync pipeline 'sync_.*' created/);
    } finally {
      vi.useRealTimers();
    }
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

  it("offers the workspace (read from its Git repository or live) and a ZIP export, each asking only what it needs", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const source = await screen.findByRole("radiogroup", { name: "How to connect to Azure Synapse" });
    // Two ways in: the workspace (its Git repository or the live workspace is a choice inside it), or a ZIP.
    expect(within(source).getAllByRole("radio").map((r) => r.textContent?.match(/^(Synapse workspace|Workspace export \(ZIP\))/)?.[0]))
      .toEqual(["Synapse workspace", "Workspace export (ZIP)"]);
    for (const r of within(source).getAllByRole("radio")) expect(r).not.toHaveAttribute("aria-disabled");
    for (const gone of [/Interactive browser/, /^Azure CLI/, /^Git repository/]) expect(within(source).queryByRole("radio", { name: gone })).toBeNull();
    expect(within(source).getByRole("radio", { name: /^Synapse workspace/ })).toHaveAttribute("aria-checked", "true");
    // Nothing to type: sign in and choose the workspace, or enter a repository URL instead.
    expect(screen.queryByLabelText(/Subscription ID|Tenant ID|Repository URL/)).toBeNull();
    expect(screen.getByRole("button", { name: "Sign in with Azure" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Enter a repository URL instead" }));
    expect(screen.getByLabelText("Repository URL")).toBeInTheDocument();
    await user.click(within(source).getByRole("radio", { name: /Workspace export \(ZIP\)/ }));
    expect(screen.getByLabelText("Workspace export (ZIP)")).toBeInTheDocument();
    await user.click(within(source).getByRole("radio", { name: /^Synapse workspace/ }));
    expect(within(source).getByRole("radio", { name: /^Synapse workspace/ })).toHaveAttribute("aria-checked", "true");
    for (const gone of [/Service Principal/, /Managed Identity/]) expect(screen.queryByRole("radio", { name: gone })).toBeNull();

    const target = await screen.findByRole("radiogroup", { name: "How to connect to Microsoft Fabric" });
    expect(within(target).getAllByRole("radio")).toHaveLength(2);
    await user.click(within(target).getByRole("radio", { name: /Fabric CLI/ }));
    expect(screen.getByRole("button", { name: "Sign in with Fabric CLI" })).toBeInTheDocument();
    // No secret is ever typed on this page.
    expect(screen.queryByLabelText(/token|secret|password|Client ID/i)).toBeNull();
  });

  it("connects one environment's Git branch, refusing a URL with credentials, and discovers without SQL pool objects", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const card = await screen.findByRole("region", { name: /^Source connection/ });
    await user.click(await within(card).findByRole("button", { name: "Enter a repository URL instead" }));
    await user.click(within(card).getByRole("button", { name: "Connect" }));
    expect(within(card).getByText("Repository URL is required.")).toBeInTheDocument();
    await user.type(within(card).getByLabelText("Repository URL"), "https://user:ghp_x@github.com/contoso/synapse-ws.git");
    await user.click(within(card).getByRole("button", { name: "Connect" }));
    expect(within(card).getByText(/Leave credentials out of the URL/)).toBeInTheDocument();
    await user.clear(within(card).getByLabelText("Repository URL"));
    await user.type(within(card).getByLabelText("Repository URL"), "https://github.com/contoso/synapse-ws.git");
    // The branch and environment are folded away with their defaults: the default branch, Dev.
    const more = within(card).getByRole("button", { name: /^More options · Dev environment$/ });
    expect(within(card).queryByLabelText(/^Branch/)).toBeNull();
    await user.click(more);
    await user.type(within(card).getByLabelText(/^Branch/), "release/prod");
    await user.selectOptions(within(card).getByLabelText("Environment"), "Prod");
    expect(more).toHaveAccessibleName("More options · Prod environment · branch release/prod");
    await user.click(within(card).getByRole("button", { name: "Connect" }));
    expect(await within(card).findByText("release/prod · 3f9a1c2", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(within(card).getByText("Prod")).toBeInTheDocument();
    expect(within(card).getByText(/Not added: tables, views, procedures and data are skipped/)).toBeInTheDocument();
    // Discovery reads the repository; with no pool there are no tables.
    await mockApi.startDiscovery();
    await new Promise((r) => setTimeout(r, 3700));
    const graph = await mockApi.getDependencies();
    expect(graph.nodes.some((n) => n.type === "Pipeline")).toBe(true);
    expect(graph.nodes.some((n) => n.type === "Table" || n.type === "Dedicated SQL Pool")).toBe(false);
  }, 30000);

  it("uploads a ZIP, applies an environment's parameters file, and asks for an Azure sign-in to add the SQL pool", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const card = await screen.findByRole("region", { name: /^Source connection/ });
    await user.click(await within(card).findByRole("radio", { name: /Workspace export \(ZIP\)/ }));
    fireEvent.change(within(card).getByLabelText("Workspace export (ZIP)"), { target: { files: [new File(["PK"], "contoso-synapse.zip", { type: "application/zip" })] } });
    expect(await within(card).findByText(/232 definitions found in synapse\//)).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: /^More options/ }));
    const params = JSON.stringify({ parameters: { ls_sql_connectionString: { value: "Server=prod" }, workspaceName: { value: "prod-ws" } } });
    fireEvent.change(within(card).getByLabelText("Environment parameters (optional)"), { target: { files: [new File([params], "parameters.prod.json", { type: "application/json" })] } });
    expect(await within(card).findByText("parameters.prod.json")).toBeInTheDocument();
    await user.selectOptions(within(card).getByLabelText("Environment"), "Other");
    await user.type(within(card).getByLabelText("Environment name"), "UAT");
    // The SQL pool needs an Azure sign-in first.
    await user.click(within(card).getByRole("checkbox", { name: /Also read the dedicated SQL pool/ }));
    await user.click(within(card).getByRole("button", { name: "Connect export" }));
    expect(within(card).getByText("Sign in to Azure to add the SQL pool, or switch it off.")).toBeInTheDocument();
    await user.click(within(card).getByRole("checkbox", { name: /Also read the dedicated SQL pool/ }));
    await user.click(within(card).getByRole("button", { name: "Connect export" }));
    expect(await within(card).findByText("UAT", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(within(card).getByText("parameters.prod.json · 1 value applied, 1 not matched")).toBeInTheDocument();
    expect(within(card).getByText("contoso-synapse.zip")).toBeInTheDocument();
  }, 30000);

  it("signs in with nothing typed, then lists the account's subscriptions, and each one its own resource groups", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const card = await screen.findByRole("region", { name: /^Source connection/ });
    await user.click(await within(card).findByRole("button", { name: "Sign in with Azure" }));
    expect(await within(card).findByText(/Enter your email, password and any MFA prompt there/)).toBeInTheDocument();
    expect(await within(card).findByText("demo.user@contoso.com", {}, { timeout: 4000 })).toBeInTheDocument();
    const subscription = await within(card).findByLabelText("Subscription");
    await waitFor(() => expect(within(subscription).getAllByRole("option").map((o) => o.textContent))
      .toEqual(["Select a subscription", "Contoso HR (demo)", "Demo subscription"]));
    // Until a subscription is chosen there is nothing to list below it.
    expect(within(card).getByLabelText("Resource group")).toBeDisabled();
    await user.selectOptions(subscription, "Contoso HR (demo)");
    const group = within(card).getByLabelText("Resource group");
    await waitFor(() => expect(group).toHaveValue("rg-hr-analytics")); // its only group, chosen without asking
    await user.selectOptions(subscription, "Demo subscription");
    await waitFor(() => expect(within(group).getAllByRole("option").map((o) => o.textContent)).toContain("rg-demo-migration"));
  }, 20000);

  it("lists subscriptions from every directory, and signs in to one that asks for its own sign-in on request", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const card = await screen.findByRole("region", { name: /^Source connection/ });
    await user.click(await within(card).findByRole("button", { name: "Sign in with Azure" }));
    const others = await within(card).findByRole("group", { name: "Other directories" }, { timeout: 4000 });
    expect(within(others).getByText(/asks for its own sign-in before showing subscriptions/)).toBeInTheDocument();
    await user.click(within(others).getByRole("button", { name: "Sign in to Partner directory" }));
    const subscription = within(card).getByLabelText("Subscription");
    // Once a second directory answers, every subscription is named with its directory.
    await waitFor(() => expect(within(subscription).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Select a subscription", "Contoso HR (demo) · Demo directory", "Demo subscription · Demo directory", "Partner analytics (demo) · Partner directory",
    ]), { timeout: 4000 });
    expect(within(card).queryByRole("group", { name: "Other directories" })).toBeNull();
  }, 20000);

  it("keeps Start migration disabled until both sides are signed in, chosen and tested", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const start = () => screen.getByRole("button", { name: /Start migration/ });
    const source = await screen.findByRole("region", { name: /^Source connection/ });
    expect(start()).toBeDisabled();

    // Source: sign in, choose the workspace, test.
    // Before signing in there is nothing to connect yet.
    await within(source).findByRole("button", { name: "Sign in with Azure" });
    expect(within(source).queryByRole("button", { name: "Connect" })).toBeNull();
    await user.click(within(source).getByRole("radio", { name: /^Synapse workspace/ })); // the form remembers the last method
    await user.click(within(source).getByRole("button", { name: "Sign in with Azure" }));
    const subscription = await within(source).findByLabelText("Subscription", {}, { timeout: 4000 });
    await waitFor(() => expect(within(subscription).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(subscription, "Demo subscription");
    const group = within(source).getByLabelText("Resource group");
    await waitFor(() => expect(within(group).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(group, "rg-demo-migration");
    const ws = within(source).getByLabelText("Synapse workspace");
    await waitFor(() => expect(within(ws).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(ws, "demo-synapse-ws");
    // The workspace has a single dedicated SQL pool: it is chosen without asking.
    await waitFor(() => expect(within(source).getByLabelText(/Dedicated SQL pool/)).toHaveValue("TransportDW"));
    // The workspace is connected to Git, so its repository is the choice made for you; read it live instead.
    const viaGit = await within(source).findByRole("radio", { name: /^Git repository/ }, { timeout: 4000 });
    expect(viaGit).toBeChecked();
    await user.click(within(source).getByRole("radio", { name: /^Live workspace/ }));
    await user.click(within(source).getByRole("button", { name: "Connect" }));
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

  it("reads a workspace's Git repository when it has one, chosen for you, with nothing typed", async () => {
    const user = userEvent.setup();
    localStorage.setItem("ma.route", JSON.stringify({ source: "synapse", destination: "fabric" }));
    go("/");
    const card = await screen.findByRole("region", { name: /^Source connection/ });
    // Nothing to type: the only thing asked first is the sign-in.
    expect(within(card).queryByRole("textbox")).toBeNull();
    await user.click(await within(card).findByRole("button", { name: "Sign in with Azure" }));
    const subscription = await within(card).findByLabelText("Subscription", {}, { timeout: 4000 });
    await waitFor(() => expect(within(subscription).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(subscription, "Demo subscription");
    const group = within(card).getByLabelText("Resource group");
    await waitFor(() => expect(within(group).getAllByRole("option").length).toBeGreaterThan(1));

    // A workspace without Git integration is simply read live: no choice, nothing to type.
    await user.selectOptions(group, "rg-demo-analytics");
    expect(await within(card).findByText("analytics-synapse-prod isn't connected to Git, so its definitions are read from the live workspace.", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(within(card).queryByRole("group", { name: "Read definitions from" })).toBeNull();
    expect(within(card).queryByRole("textbox")).toBeNull();

    // One that is: its repository is found and chosen for you; the live workspace is the alternative.
    await user.selectOptions(group, "rg-demo-migration");
    const ws = within(card).getByLabelText("Synapse workspace");
    await waitFor(() => expect(within(ws).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(ws, "demo-synapse-ws");
    const readFrom = await within(card).findByRole("group", { name: "Read definitions from" }, { timeout: 4000 });
    expect(within(readFrom).getByRole("radio", { name: /^Git repository/ })).toBeChecked();
    expect(within(readFrom).getByRole("radio", { name: /^Live workspace/ })).not.toBeChecked();
    expect(readFrom).toHaveTextContent("https://dev.azure.com/contoso/Synapse/_git/demo-synapse-ws");
    expect(readFrom).toHaveTextContent("Azure DevOps · branch main · folder synapse/");
    expect(within(card).queryByRole("textbox")).toBeNull();
    expect(within(card).queryByRole("checkbox", { name: /dedicated SQL pool/ })).toBeNull();
    expect(within(card).getByRole("button", { name: /^More options · Dev environment$/ })).toHaveAttribute("aria-expanded", "false");
    // The workspace's only SQL pool is chosen with it, so tables come along.
    await waitFor(() => expect(within(card).getByLabelText(/Dedicated SQL pool/)).toHaveValue("TransportDW"));

    await user.click(within(card).getByRole("button", { name: "Connect" }));
    expect(await within(card).findByText("main · 3f9a1c2", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(within(card).getByText("Dev")).toBeInTheDocument();
    expect(within(card).getByText("synapse/")).toBeInTheDocument();
    expect(within(card).getByText("TransportDW (demo-synapse-ws)")).toBeInTheDocument();
  }, 40000);

  it("offers a connected live workspace's Git repository in its place, already found", async () => {
    resetDemo();
    const user = userEvent.setup();
    go("/");
    const card = await screen.findByRole("region", { name: /^Source connection/ });
    expect(await within(card).findByText("https://dev.azure.com/contoso/Synapse/_git/demo-synapse-ws · main", {}, { timeout: 5000 })).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Use its Git repository instead" }));
    // The repository is already known and chosen: nothing to type.
    expect(within(card).getByRole("radio", { name: /^Git repository/ })).toBeChecked();
    expect(within(card).queryByRole("textbox")).toBeNull();
    // The same workspace and SQL pool stay chosen, so tables still come along.
    await waitFor(() => expect(within(card).getByLabelText(/Dedicated SQL pool/)).toHaveValue("TransportDW"), { timeout: 4000 });
    await user.click(within(card).getByRole("button", { name: "Connect" }));
    expect(await within(card).findByText("main · 3f9a1c2", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(within(card).getByText("TransportDW (demo-synapse-ws)")).toBeInTheDocument();
  }, 30000);
});

describe("migration journey", () => {
  it("asks to connect first when nothing is connected", async () => {
    go("/migration");
    expect(await screen.findByText("Connect both sides first")).toBeInTheDocument();
  });

  it("shows the seven steps left to right, locks the ones after the open step, and Next unlocks the following one", async () => {
    resetDemo();
    const user = userEvent.setup();
    go("/migration");
    expect(await stepHeading("Discover")).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Migration steps" });
    const boxes = within(nav).getAllByRole("button");
    expect(boxes.map((b) => b.getAttribute("aria-label")?.match(/^Step \d: (\w+)/)?.[1])).toEqual(["Discover", "Assess", "Consult", "Waves", "Plan", "Migrate", "Validate"]);
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
    expect(stepBox(5)).toBeDisabled();
  }, 15000);

  it("marks a confirmed step whose results are gone as Run again, and keeps the steps after it locked", async () => {
    resetDemo();
    await seedPlan();
    unlockTo("validate"); // Migrate is confirmed, but there is no run (as after a reload of Demo data)
    go("/migration");
    expect(await stepHeading("Migrate")).toBeInTheDocument();
    await waitFor(() => expect(stepBox(6)).toHaveAccessibleName(/Run again/));
    expect(stepBox(7)).toBeDisabled();
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
    expect(stored(JOURNEY)).toEqual(["discover", "assess", "consult"]);
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
    expect(stored(JOURNEY)).toEqual(["discover", "assess", "consult", "waves"]);
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

  it("resets Migrate by closing the run, which stays in the run history, so a fresh run can start", async () => {
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
    expect(within(dialog).getByText(/Migration run #\d+ .* it is closed, and its record stays in the run history/)).toBeInTheDocument();
    expect(within(dialog).getByText(/anything already created in Fabric/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Reset Migrate" }));
    expect(await screen.findByText("Nothing is running.", {}, { timeout: 5000 })).toBeInTheDocument();
    expect((await mockApi.getExecution()).state).toBe("idle");
    expect(await screen.findByRole("table", { name: "Earlier runs" })).toBeInTheDocument();
    expect(stored(JOURNEY)).toEqual(["discover", "assess", "consult", "waves", "plan"]);
  }, 20000);
});

describe("assistant", () => {
  it("opens from the + button (or Ctrl+I) as a panel on the right, answering by its rules in Demo data, and closes with Escape", async () => {
    const user = userEvent.setup();
    go("/");
    await user.click(await screen.findByRole("button", { name: /^Open the migration assistant \(Ctrl\+I\)/ }));
    const panel = screen.getByRole("complementary", { name: "Migration assistant" });
    expect(within(panel).getByText("Rules")).toBeInTheDocument();
    expect(within(panel).getByLabelText("Message the assistant")).toBeEnabled();
    expect(within(panel).getByRole("button", { name: "Send" })).toBeDisabled();
    // The launcher hides while the panel is open.
    expect(screen.queryByRole("button", { name: /^Open the migration assistant/ })).toBeNull();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("complementary", { name: "Migration assistant" })).toBeNull();
    await user.keyboard("{Control>}i{/Control}");
    expect(screen.getByRole("complementary", { name: "Migration assistant" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close the assistant" }));
    expect(screen.getByRole("button", { name: /^Open the migration assistant \(Ctrl\+I\)/ })).toBeInTheDocument();
  });

  it("answers what is left to do from the execution plan, and why a run failed from its diagnosis", async () => {
    setDemoConsultation("advisory");
    await discovered();
    await seedPlan(5);
    await mockApi.analyzePlan(JSON.parse(localStorage.getItem(PLAN) ?? "[]"), true);
    const user = userEvent.setup();
    go("/");
    await user.click(await screen.findByRole("button", { name: /^Open the migration assistant/ }));
    const panel = screen.getByRole("complementary", { name: "Migration assistant" });
    await user.click(within(panel).getByRole("button", { name: "What is left to do?" }));
    const open = await within(panel).findByText(/What is open now, in order:/);
    expect(open.textContent).toMatch(/Run the migration|A plan is recorded/);
    await user.click(within(panel).getByRole("button", { name: "All questions" }));
    await user.type(within(panel).getByLabelText("Message the assistant"), "Why did it fail?");
    await user.click(within(panel).getByRole("button", { name: "Send" }));
    expect(await within(panel).findByText("Nothing in the current run failed or was blocked.")).toBeInTheDocument();
    const trail = await mockApi.getAudit({ limit: 50 });
    expect(trail.records.filter((r) => r.action === "assistant.asked")).toHaveLength(2);
  }, 30000);
});

describe("the execution plan (demo)", () => {
  beforeEach(() => { resetDemo(); setDemoConsultation("advisory"); });

  it("builds the plan from the brief and the run, proposes a retry for what failed, and runs it once a person confirms", async () => {
    await discovered();
    await approveBrief([["G3", "West Europe"]]);
    const items = await seedPlan(25);
    await mockApi.analyzePlan(items, true);
    let plan = await mockApi.getExecutionPlan();
    const keys = plan.steps.map((s) => s.key);
    expect(keys.slice(0, 5)).toEqual(["ready.source", "ready.target", "ready.discovery", "ready.brief", "ready.plan"]);
    expect(plan.steps.find((s) => s.key === "ready.residency")).toMatchObject({ owner: "person", answers: ["G3"] });
    await mockApi.startExecution(items, (await mockApi.getConsultPolicy()).runOptions as RunOptions);
    await waitFor(async () => expect((await mockApi.getExecution()).state).toBe("completed"), { timeout: 90000 });
    plan = await mockApi.getExecutionPlan();
    const retry = plan.actions.find((a) => a.tool === "run.retry");
    expect(retry?.status).toBe("proposed");
    expect(plan.steps.find((s) => s.key === "deliver.fix")?.status).toBe("ready");
    localStorage.setItem(PLAN, JSON.stringify(items));
    unlockTo("migrate");
    const user = userEvent.setup();
    go("/migration?step=migrate&view=plan");
    const card = await screen.findByRole("article", { name: /Proposed: Retry the/ }, { timeout: 8000 });
    await user.click(within(card).getByRole("button", { name: "Confirm" }));
    await waitFor(async () => expect((await mockApi.getAudit({ limit: 100 })).records.some((r) => r.action === "orchestrator.action_confirmed")).toBe(true));
    const step = await screen.findByRole("listitem", { name: /The target capacity is in West Europe/ });
    await user.click(within(step).getByRole("button", { name: "Mark done" }));
    await waitFor(() => expect(screen.getByRole("listitem", { name: /The target capacity is in West Europe: Done/ })).toBeInTheDocument());
  }, 120000);

  it("lets a person confirm by hand only a check the accelerator cannot tell, and answers the brief and validation by its rules", async () => {
    await discovered();
    const plan = await mockApi.getExecutionPlan();
    expect(plan.steps.find((s) => s.key === "ready.plan")?.status).toBe("failed");
    await expect(mockApi.markStep("ready.plan", true)).rejects.toMatchObject({ code: "check_fails" });  // fixed on Plan, never confirmed
    await expect(mockApi.markStep("ready.discovery", true)).rejects.toMatchObject({ code: "nothing_to_confirm" });
    expect(plan.steps.find((s) => s.key === "ready.brief")?.status).toBe("warn");  // advisory: no brief only warns
    const confirmed = await mockApi.markStep("ready.brief", true, "Advisory for this pilot");
    expect(confirmed.steps.find((s) => s.key === "ready.brief")).toMatchObject({ status: "done", message: "Confirmed by a person." });
    expect((await mockApi.askAssistant("Which brief is in force?")).answer).toMatch(/^No migration brief is approved yet/);
    expect((await mockApi.askAssistant("Any validation mismatches?")).answer).toBe("Validation is not run.");
  }, 30000);
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
    // Nothing is chosen until the user picks; the options that pass no secret come first.
    const method = screen.getByLabelText("How to sign in to synapse-demo-synapse-ws-TransportDW");
    expect(method).toHaveValue("");
    expect([...(method as HTMLSelectElement).options].slice(1, 4).map((o) => o.value)).toEqual(["workspaceIdentity", "basicKeyVault", "servicePrincipalKeyVault"]);
    // Workspace identity asks for nothing and says what must be set up instead.
    await user.selectOptions(method, "workspaceIdentity");
    expect(screen.queryByLabelText("Password for synapse-demo-synapse-ws-TransportDW")).not.toBeInTheDocument();
    expect(screen.getByText(/FROM EXTERNAL PROVIDER/)).toBeInTheDocument();
    // Key Vault asks for a reference and a secret name: plain text, because neither is a secret.
    await user.selectOptions(method, "basicKeyVault");
    expect(screen.getByLabelText("Secret name for synapse-demo-synapse-ws-TransportDW")).toHaveAttribute("type", "text");
    expect(screen.queryByLabelText("Password for synapse-demo-synapse-ws-TransportDW")).not.toBeInTheDocument();
    await user.selectOptions(method, "basic");
    expect(await screen.findByLabelText("Password for synapse-demo-synapse-ws-TransportDW")).toHaveAttribute("type", "password");
    // Credentials: typed into a password field, kept out of localStorage and sessionStorage.
    const secret = (await screen.findAllByLabelText(/^Password for /))[0];
    expect(secret).toHaveAttribute("type", "password");
    await user.type(secret, "S3CRET-NEVER-STORED");
    const kept = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    expect(kept).not.toContain("S3CRET-NEVER-STORED");
    // Switching to another method blanks what was typed for the old one, so it is not sent along.
    await user.selectOptions(method, "workspaceIdentity");
    await user.selectOptions(method, "basic");
    expect(screen.getByLabelText("Password for synapse-demo-synapse-ws-TransportDW")).toHaveValue("");
  }, 30000);

  it("tells the planner which linked services have credentials, never the credentials, and does not re-score on every key", async () => {
    await discovered();
    unlockTo("plan");
    const user = userEvent.setup();
    const analyze = vi.spyOn(mockApi, "analyzePlan");
    try {
      go("/migration?step=plan");
      await waitFor(() => expect(screen.getByRole("button", { name: /^Add all .* objects/ })).toBeEnabled(), { timeout: 8000 });
      await user.click(screen.getByRole("button", { name: /^Add all .* objects/ }));
      await user.click(await screen.findByRole("tab", { name: "Stages & credentials" }));
      for (const section of screen.getAllByText(/Credentials/, { selector: "summary span" })) await user.click(section);
      const name = "synapse-demo-synapse-ws-TransportDW";
      // Choosing how it signs in is what gives it credentials: the planner is told its name, and only its name.
      await user.selectOptions(screen.getByLabelText(`How to sign in to ${name}`), "basic");
      await waitFor(() => expect(analyze.mock.calls.some((call) => call[3]?.includes(name))).toBe(true));
      await user.type(await screen.findByLabelText(`Password for ${name}`), "S3CRET-NEVER-SENT");
      await new Promise((resolve) => setTimeout(resolve, 700)); // past the re-score's 350 ms pause
      const scored = analyze.mock.calls.length;
      await user.type(screen.getByLabelText(`Password for ${name}`), "MORE");
      await new Promise((resolve) => setTimeout(resolve, 700));
      // More keys typed, the same linked services with credentials: nothing to score again, and nothing secret sent.
      expect(analyze.mock.calls.length).toBe(scored);
      expect(JSON.stringify(analyze.mock.calls)).not.toContain("S3CRET-NEVER-SENT");
    } finally {
      analyze.mockRestore();
    }
  }, 30000);

  it("sets a date filter per table, keeps a table in sync with a guessed change column, and keeps filters across a reload", async () => {
    resetDemo();
    await discovered();
    const graph = await mockApi.getDependencies();
    const tables = graph.nodes.filter((n) => n.type === "Table").slice(0, 2);
    localStorage.setItem(PLAN, JSON.stringify(tables.map((n) => ({ id: n.id, wave: n.wave }))));
    unlockTo("plan");
    const user = userEvent.setup();
    const first = go("/migration?step=plan");
    await user.click(await screen.findByRole("tab", { name: "Stages & credentials" }, { timeout: 5000 }));
    await user.click(await screen.findByText(/Date filters for table data/));
    const name = tables[0].name;
    const column = screen.getByLabelText(`Date column for ${name}`);
    // Until a column is chosen the table loads every row and its dates are off.
    expect(column).toHaveValue("");
    expect(screen.getByLabelText(`From date for ${name}`)).toBeDisabled();
    await user.selectOptions(column, "ModifiedAt");
    // A column without dates is flagged before the run.
    expect(screen.getByText("Give a start date, an end date or both.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(`From date for ${name}`), { target: { value: "2024-10-08" } });
    expect(screen.queryByText("Give a start date, an end date or both.")).not.toBeInTheDocument();
    // Keeping it in sync turns the end date off and guesses the change column.
    await user.click(screen.getByLabelText(`Keep ${name} in sync`));
    expect(screen.getByLabelText(`Until date for ${name}`)).toBeDisabled();
    expect(screen.getByLabelText(`Change column for ${name}`)).toHaveValue("ModifiedAt");
    expect(screen.getByText(/1 of 2 tables filtered/)).toBeInTheDocument();
    first.unmount();

    go("/migration?step=plan");
    await user.click(await screen.findByRole("tab", { name: "Stages & credentials" }, { timeout: 5000 }));
    await user.click(await screen.findByText(/Date filters for table data/));
    expect(screen.getByLabelText(`From date for ${name}`)).toHaveValue("2024-10-08");
    expect(screen.getByLabelText(`Keep ${name} in sync`)).toBeChecked();
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

describe("workspace explorer", () => {
  it("files discovered objects like a Synapse repository, with the SQL pool by schema", () => {
    const node = (id: string, name: string, type: string) =>
      ({ id, name, type, category: "", classification: "DIRECT" as const, fabricTarget: "", wave: 1, dependsOn: 0, dependedOnBy: 0 });
    const tree = buildTree([
      node("p1", "PL_Load", "Pipeline"), node("p2", "PL_Archive", "Pipeline"), node("ls", "ls_blob", "Linked Service"),
      node("pool", "TransportDW", "Dedicated SQL Pool"), node("s", "TransportDW.sales", "Schema"),
      node("t1", "sales.Orders", "Table"), node("v1", "sales.vOrders", "View"), node("t2", "dbo.Trips", "Table"),
      node("x", "fw-rule", "Networking Configuration"),
    ]);
    expect(tree.map((n) => n.label)).toEqual(["linkedService", "pipeline", "sqlPool", "other"]);
    expect(tree[1].children!.map((n) => n.label)).toEqual(["PL_Archive", "PL_Load"]);
    const pool = tree[2].children![0];
    expect(pool.label).toBe("TransportDW");
    expect(pool.children!.map((n) => n.label)).toEqual(["dbo", "sales"]);
    const sales = pool.children![1];
    expect(sales.children!.map((n) => `${n.label}:${n.children!.map((c) => c.label).join(",")}`)).toEqual(["Tables:Orders", "Views:vOrders"]);
    expect(tree[3].children![0].label).toBe("Networking Configuration");
    // every object is counted exactly once: the pool and the schema as the folders they are
    expect(tree.reduce((sum, n) => sum + n.count, 0)).toBe(9);
    expect([pool.objectId, sales.objectId]).toEqual(["pool", "s"]);
  });

  it("shows the discovered workspace beside the steps, filters it, opens an object, and can be hidden", async () => {
    await discovered();
    unlockTo("assess");
    const user = userEvent.setup();
    const first = go("/migration?step=discover");
    const explorer = await screen.findByRole("complementary", { name: "Workspace explorer" }, { timeout: 5000 });
    const pipelines = await within(explorer).findByRole("button", { name: /^pipeline/ }, { timeout: 5000 });
    expect(pipelines).toHaveAttribute("aria-expanded", "false");
    await user.click(pipelines);
    expect(pipelines).toHaveAttribute("aria-expanded", "true");
    expect(within(explorer).getByRole("button", { name: "PL_Master (Pipeline)" })).toBeInTheDocument();
    // filtering opens the folders on the way to each match and hides the rest
    await user.type(within(explorer).getByLabelText("Filter the workspace explorer"), "PL_Master");
    expect(within(explorer).queryByRole("button", { name: /^linkedService/ })).not.toBeInTheDocument();
    await user.click(within(explorer).getByRole("button", { name: "PL_Master (Pipeline)" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    // hidden stays hidden after a reload
    await user.click(screen.getByRole("button", { name: "Hide explorer" }));
    expect(screen.queryByRole("complementary", { name: "Workspace explorer" })).not.toBeInTheDocument();
    first.unmount();
    go("/migration?step=discover");
    expect(await screen.findByRole("button", { name: "Show explorer" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByRole("complementary", { name: "Workspace explorer" })).not.toBeInTheDocument();
  }, 30000);
});

describe("release path", () => {
  it("shows where the migration sits on the way to Prod, below the seven steps, with a checklist kept per project", async () => {
    await discovered();
    unlockTo("assess");
    const user = userEvent.setup();
    const first = go("/migration?step=discover");
    const path = await screen.findByRole("region", { name: "Release path" }, { timeout: 5000 });
    const stages = within(path).getByRole("list", { name: "From the source to production" });
    expect(within(stages).getAllByRole("listitem").map((li) => li.querySelector(".release-label")?.textContent))
      .toEqual(["Synapse workspace", "This migration", "Fabric Dev", "Fabric Git", "Fabric Test", "Fabric Prod"]);
    // The seven steps are unchanged.
    expect(within(screen.getByRole("navigation", { name: "Migration steps" })).getAllByRole("listitem")).toHaveLength(7);
    await user.click(within(path).getByRole("button", { name: /What comes next · 0\/6/ }));
    expect(within(path).getByText(/Getting them to Test and Prod is not a second migration/)).toBeInTheDocument();
    await user.click(within(path).getByRole("checkbox", { name: /Connect the Fabric workspace to Git/ }));
    expect(within(stages).getByText(/Fabric Git/).closest("li")).toHaveClass("done");
    first.unmount();
    go("/migration?step=discover");
    const again = await screen.findByRole("region", { name: "Release path" }, { timeout: 5000 });
    expect(within(again).getByRole("button", { name: /What comes next · 1\/6/ })).toBeInTheDocument();
  }, 30000);

  it("follows a repository source's environment: Prod definitions have nothing left to promote", async () => {
    await mockApi.connectRepository({ kind: "git", repositoryUrl: "https://github.com/contoso/synapse-ws.git", ref: "release/prod", environment: "Prod" });
    await mockApi.startDiscovery();
    await new Promise((r) => setTimeout(r, 3700));
    unlockTo("assess");
    const user = userEvent.setup();
    go("/migration?step=discover");
    const path = await screen.findByRole("region", { name: "Release path" }, { timeout: 5000 });
    const labels = [...path.querySelectorAll(".release-label")].map((e) => e.textContent);
    expect(labels).toEqual(["Synapse Prod", "This migration", "Fabric Prod", "Fabric Git"]);
    expect(within(path).getByText("Git · release/prod")).toBeInTheDocument();
    await user.click(within(path).getByRole("button", { name: /What comes next/ }));
    expect(within(path).getByText(/nothing to promote after it/)).toBeInTheDocument();
  }, 30000);

  it("reads Fabric Git from the target workspace: its branch, what waits to be committed, and the pull request", async () => {
    resetDemo();
    unlockTo("assess");
    const user = userEvent.setup();
    go("/migration?step=discover");
    const path = await screen.findByRole("region", { name: "Release path" }, { timeout: 5000 });
    const detail = await within(path).findByText(/^dev @ [0-9a-f]{7} · 0 uncommitted$/, {}, { timeout: 5000 });
    expect(detail.closest("li")).toHaveClass("done");
    expect(within(path).getByRole("link", { name: "Pull request dev → main" }))
      .toHaveAttribute("href", "https://dev.azure.com/contoso/Migration/_git/fabric-migration/pullrequestcreate?sourceRef=dev&targetRef=main");
    // The checklist's Git row follows Fabric: ticked, and not a box to tick by hand.
    await user.click(within(path).getByRole("button", { name: /What comes next · 1\/6/ }));
    const row = within(path).getByRole("checkbox", { name: /Connect the Fabric workspace to Git/ });
    expect(row).toBeChecked();
    expect(row).toBeDisabled();
    expect(within(path).getByText(/Connected: Fabric_demo commits to dev in fabric-migration, folder fabric\./)).toBeInTheDocument();
  }, 20000);

  it("says what to do when the target workspace is not connected to Git", async () => {
    resetDemo();
    await mockApi.testFabric({ method: "azure_cli", workspaceId: "9b1d5e22-0c3a-4f7e-8d11-6a4c2e9f0b33", workspaceName: "My workspace" });
    unlockTo("assess");
    go("/migration?step=discover");
    const path = await screen.findByRole("region", { name: "Release path" }, { timeout: 5000 });
    const detail = await within(path).findByText("Not connected: connect the Dev workspace to Git in Fabric", {}, { timeout: 5000 });
    expect(detail.closest("li")).toHaveClass("todo");
    expect(within(path).queryByRole("link", { name: /Pull request/ })).toBeNull();
  }, 20000);
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

  it("commits a completed run to Fabric Git, with the pull request from dev to main to open", async () => {
    resetDemo();
    const items = await seedPlan(3);
    await mockApi.startExecution(items);
    unlockTo("validate");
    // Jump past the end of the run and its commit instead of waiting for them.
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(Date.now() + 10 * 60_000);
      go("/migration?step=migrate");
      const title = await screen.findByText(/^Committed 3 items to dev \([0-9a-f]{7}\)$/, {}, { timeout: 5000 });
      const banner = title.closest(".banner") as HTMLElement;
      expect(within(banner).getByRole("link", { name: "Open pull request dev → main" }))
        .toHaveAttribute("href", "https://dev.azure.com/contoso/Migration/_git/fabric-migration/pullrequestcreate?sourceRef=dev&targetRef=main");
      expect(within(banner).getByRole("link", { name: "View commit" })).toHaveAttribute("href", expect.stringMatching(/\/commit\/[0-9a-f]{40}$/));
      expect(within(banner).queryByRole("button", { name: "Commit now" })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  }, 20000);

  it("leaves the commit for later when it is switched off, and Commit now makes it", async () => {
    resetDemo();
    const items = await seedPlan(3);
    await mockApi.startExecution(items, {
      scope: "all", stopOnFailure: false, stages: [], dataMode: "if_empty", dataRun: "run", collation: "match_synapse", gitCommit: "off",
    });
    unlockTo("validate");
    const user = userEvent.setup();
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(Date.now() + 10 * 60_000);
      expect((await mockApi.getExecution()).git ?? null).toBeNull();
      go("/migration?step=migrate");
      const title = await screen.findByText("Not committed to Fabric Git", {}, { timeout: 5000 });
      const banner = title.closest(".banner") as HTMLElement;
      await waitFor(() => expect(banner).toHaveTextContent("Commit to Fabric Git was off for this run. 3 items wait to be committed to dev."));
      await user.click(within(banner).getByRole("button", { name: "Commit now" }));
      expect(await screen.findByText("Committing to Fabric Git")).toBeInTheDocument();
      vi.setSystemTime(Date.now() + 5000); // the demo commit takes a moment
      expect(await screen.findByText(/^Committed 3 items to dev \([0-9a-f]{7}\)$/, {}, { timeout: 5000 })).toBeInTheDocument();
      expect((await mockApi.getFabricGit()).uncommitted).toBe(0);
    } finally {
      vi.useRealTimers();
    }
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

describe("LLM-assisted transformation (demo)", () => {
  it("generates a draft, rejects types it cannot convert, and uses only what is accepted", async () => {
    await discovered();
    const procs = await mockApi.getResults({ ...Q, types: ["Stored Procedure"] });
    const id = procs.items[0].id;
    expect((await mockApi.getTransform(id)).state).toBe("none");
    expect((await mockApi.getTransformPrompt(id)).prompt).toContain("Project requirements");
    const links = await mockApi.getResults({ ...Q, types: ["Linked Service"] });
    const queued = await mockApi.generateTransforms([id, links.items[0].id]);
    expect(queued.map((q) => q.state)).toEqual(["queued", "rejected"]);
    await waitFor(async () => expect((await mockApi.getTransform(id)).state).toBe("draft"), { timeout: 4000 });
    const draft = (await mockApi.getTransform(id)).draft!;
    expect(draft.code).toContain("UPDATE");
    const accepted = await mockApi.acceptTransform(id, `${draft.code}\n-- reviewed`);
    expect(accepted.state).toBe("accepted");
    expect(accepted.accepted?.edited).toBe(true);
    expect((await mockApi.discardTransform(id, "all")).state).toBe("none");
  }, 20000);

  it("shows source, rules and draft side by side and accepts from the panel", async () => {
    await discovered();
    const views = await mockApi.getResults({ ...Q, types: ["View"] });
    const object = await mockApi.getObject(views.items[0].id);
    const user = userEvent.setup();
    render(<AppStateProvider><TransformPanel object={object} /></AppStateProvider>);
    const generate = await screen.findByRole("button", { name: /Generate with LLM/ });
    await user.click(screen.getByRole("tab", { name: "Synapse source" }));
    expect(screen.getByLabelText("Synapse source").textContent).toContain("NOLOCK");
    await user.click(screen.getByRole("tab", { name: "LLM draft" }));
    await user.click(generate);
    const editor = await screen.findByLabelText("LLM draft (editable)", {}, { timeout: 6000 });
    expect((editor as HTMLTextAreaElement).value).not.toContain("NOLOCK)");
    expect(screen.getByText(/Model confidence/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Accept draft" }));
    expect(await screen.findByText("Accepted: used by the next run")).toBeInTheDocument();
  }, 20000);
});

describe("LLM transformation queue (demo)", () => {
  it("lists what the rules cannot convert first and generates drafts for a selection", async () => {
    await discovered();
    const before = await mockApi.listTransformCandidates();
    expect(before.summary.needsWork).toBeGreaterThan(0);
    const firstClean = before.items.findIndex((i) => !i.problems.length);
    expect(before.items.slice(0, firstClean).every((i) => i.problems.length > 0)).toBe(true);

    const user = userEvent.setup();
    render(<AppStateProvider><TransformQueue /></AppStateProvider>);
    const table = await screen.findByRole("table", { name: "Objects a draft can be made for" }, { timeout: 5000 });
    const boxes = within(table).getAllByRole("checkbox").slice(1, 3);
    for (const b of boxes) await user.click(b);
    await user.click(screen.getByRole("button", { name: "Generate drafts (2)" }));
    await user.click(await screen.findByRole("button", { name: "Generate drafts" }));
    expect(await screen.findByText("2 drafts queued.")).toBeInTheDocument();
    await waitFor(async () => expect((await mockApi.listTransformCandidates()).summary.drafts).toBe(2), { timeout: 5000 });
    await user.click(screen.getByRole("tab", { name: /Drafts to review/ }));
    await waitFor(() => expect(within(screen.getByRole("table", { name: "Objects a draft can be made for" })).getAllByText("Draft to review")).toHaveLength(2), { timeout: 6000 });
  }, 30000);

  it("edits and saves the project requirements every prompt carries", async () => {
    await discovered();
    const user = userEvent.setup();
    render(<AppStateProvider><TransformQueue /></AppStateProvider>);
    await user.click(await screen.findByText("Project requirements (sent with every prompt)", {}, { timeout: 5000 }));
    const editor = await screen.findByLabelText("Project requirements");
    await user.clear(editor);
    fireEvent.change(editor, { target: { value: "- Use the fin_ schema prefix." } });
    await user.click(screen.getByRole("button", { name: "Save requirements" }));
    expect(await screen.findByText("Saved. The next draft uses these requirements.")).toBeInTheDocument();
    expect((await mockApi.getProjectRequirements()).text).toBe("- Use the fin_ schema prefix.");
  }, 20000);
});

describe("LLM transforms in a run (demo)", () => {
  it("marks the objects created from an accepted transform", async () => {
    await discovered();
    const procs = await mockApi.getResults({ ...Q, types: ["Stored Procedure"] });
    const [a, b] = procs.items;
    await mockApi.generateTransforms([a.id]);
    await waitFor(async () => expect((await mockApi.getTransform(a.id)).state).toBe("draft"), { timeout: 4000 });
    await mockApi.acceptTransform(a.id);
    await mockApi.authenticateFabric(FABRIC);
    await mockApi.testFabric(FABRIC);
    await mockApi.startExecution([{ id: a.id, wave: 1 }, { id: b.id, wave: 1 }]);
    const items = (await mockApi.getExecution()).items;
    expect(items.find((i) => i.id === a.id)?.llm?.edited).toBe(false);
    expect(items.find((i) => i.id === b.id)?.llm).toBeNull();
  }, 20000);
});

describe("validate step: runs in Fabric (demo)", () => {
  it("checks that objects run, lets the operator ask for real runs, and lets a failed run decide the verdict", async () => {
    await discovered();
    await mockApi.authenticateFabric(FABRIC);
    await mockApi.testFabric(FABRIC);
    const items = await seedPlan(6);
    await mockApi.startExecution(items);
    await new Promise((r) => setTimeout(r, 4000));
    unlockTo("validate");
    const user = userEvent.setup();
    go("/migration?step=validate");
    expect(await stepHeading("Validate")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Check that SQL objects work/ })).toBeChecked();
    expect(screen.queryByText("These are real runs")).toBeNull();
    await user.click(screen.getByRole("checkbox", { name: "Run notebooks now" }));
    expect(screen.getByText("These are real runs")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Run validation \(with runs\)/ }));
    const table = await screen.findByRole("table", { name: "Validation results" }, { timeout: 8000 });
    expect(within(table).getByRole("columnheader", { name: "Runs in Fabric" })).toBeInTheDocument();
    expect(within(table).getAllByText("Ran now in Fabric").length).toBeGreaterThan(0);
  }, 30000);

  it("turns a failed run into a mismatch and a never-run object into a review", async () => {
    await discovered();
    await mockApi.authenticateFabric(FABRIC);
    await mockApi.testFabric(FABRIC);
    const graph = await mockApi.getDependencies();
    const nbs = graph.nodes.filter((n) => n.type === "Notebook").slice(0, 15).map((n) => ({ id: n.id, wave: n.wave }));
    await mockApi.startExecution(nbs);
    await waitFor(async () => expect((await mockApi.getExecution()).pending).toBe(0), { timeout: 40000 });
    const rows = await mockApi.runValidation(nbs, undefined, { probe: true, run: [], timeoutMinutes: 30 });
    const withRuns = rows.filter((r) => r.runtime);
    expect(withRuns.length).toBeGreaterThan(0);
    for (const r of withRuns) {
      if (r.runtime!.status === "FAIL") expect(r.status).toBe("MISMATCH");
      if (r.runtime!.status === "NOT RUN") expect(r.status).not.toBe("MATCH");
    }
    expect(withRuns.some((r) => r.runtime!.status === "NOT RUN")).toBe(true);
  }, 60000);
});

describe("updating objects already in Fabric", () => {
  it("is off by default, warns when switched on, and is kept with the project's options", async () => {
    resetDemo();
    await seedPlan();
    unlockTo("plan");
    const user = userEvent.setup();
    const openStages = async () => user.click(await screen.findByRole("tab", { name: "Stages & credentials" }, { timeout: 5000 }));
    const first = go("/migration?step=plan");
    await openStages();
    const box = await screen.findByRole("checkbox", { name: /Update objects that are already in Fabric/ });
    expect(box).not.toBeChecked();
    expect(screen.queryByText("Changes made in Fabric will be overwritten")).toBeNull();
    await user.click(box);
    expect(screen.getByText("Changes made in Fabric will be overwritten")).toBeInTheDocument();
    first.unmount();
    go("/migration?step=plan");
    await openStages();
    expect(await screen.findByRole("checkbox", { name: /Update objects that are already in Fabric/ })).toBeChecked();
  }, 25000);
});

describe("external storage routed through OneLake", () => {
  it("proposes the storage the code reads, routes it by default, and warns when it is not routed", async () => {
    resetDemo();
    await discovered();
    await seedPlan();
    unlockTo("plan");
    const user = userEvent.setup();
    go("/migration?step=plan");
    await user.click(await screen.findByRole("tab", { name: "Stages & credentials" }, { timeout: 5000 }));
    const box = await screen.findByRole("checkbox", { name: /Route raw on demorawstorage/ }, { timeout: 5000 });
    await waitFor(() => expect(box).toBeChecked());
    expect(screen.getByText("LS_ADLS_demorawstorage")).toBeInTheDocument();
    await user.click(box);
    expect(await screen.findByText("Not routed")).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("ma.options.mock.default") ?? "null")?.storageRoutes ?? []).toEqual([]);
  }, 30000);
});

describe("functions, roles and permissions (demo)", () => {
  it("discovers functions and principals, and offers the Roles & permissions stage", async () => {
    await discovered();
    const functions = await mockApi.getResults({ ...Q, types: ["Function"] });
    expect(functions.total).toBeGreaterThan(0);
    const multi = functions.items.find((f) => f.name.includes("Multi"))!;
    expect(multi.status).toBe("Warning");
    const principals = await mockApi.getResults({ ...Q, types: ["Security Object"] });
    expect(principals.items.map((p) => p.name)).toEqual(expect.arrayContaining(["reporting_reader", "legacy_etl_login", "analyst@contoso.com"]));
    const sqlUser = await mockApi.getObject(principals.items.find((p) => p.name === "legacy_etl_login")!.id);
    expect(String((sqlUser.configuration as Record<string, unknown>).fabricSupport)).toContain("no SQL authentication");
    const caps = await mockApi.getCapabilities();
    expect(caps.stages.find((s) => s.key === "security")?.types).toEqual(["Security Object"]);
    expect(caps.stages.find((s) => s.key === "warehouse")?.types).toContain("Function");
  });
});

describe("run records, sign-off, projects and activity", () => {
  const OPTIONS: RunOptions = { scope: "automated", stopOnFailure: false, stages: [], dataMode: "if_empty", dataRun: "run", collation: "match_synapse",
    syncOverlap: "1h", existing: "skip", dataFilters: {}, gitCommit: "auto" };

  async function readyToRun(count = 3) {
    await discovered();
    await mockApi.authenticateFabric(FABRIC);
    await mockApi.testFabric(FABRIC);
    return seedPlan(count);
  }

  it("holds a run that updates what Fabric has until a person signs it off, and records who did", async () => {
    const items = await readyToRun();
    const waiting = await mockApi.startExecution(items, { ...OPTIONS, existing: "update" });
    expect(waiting.status).toBe("awaiting_approval");
    expect(waiting.items.every((i) => i.status === "PENDING" || i.status === "DEFERRED")).toBe(true);
    unlockTo("migrate");
    const user = userEvent.setup();
    go("/migration?step=migrate");
    expect(await screen.findByText("This run waits for a person to sign it off", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByText("WAITING FOR SIGN-OFF")).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Comment/), "Checked with the data owner");
    await user.click(screen.getByRole("button", { name: /Sign off and run/ }));
    await waitFor(async () => expect((await mockApi.getExecution()).status).not.toBe("awaiting_approval"));
    const approved = (await mockApi.getAudit({ action: "run.approved" })).records[0];
    expect(approved.actor).toEqual({ kind: "person", id: "Demo operator" });
    expect(approved.reason).toBe("Checked with the data owner");
    expect((await mockApi.getExecution()).approval?.automatic).toBe(false);
  }, 20000);

  it("keeps a closed run in the run history, where its record can still be read", async () => {
    const items = await readyToRun(2);
    await mockApi.startExecution(items, OPTIONS);
    await mockApi.controlExecution("pause");
    await mockApi.controlExecution("reset");
    unlockTo("migrate");
    const user = userEvent.setup();
    go("/migration?step=migrate");
    const history = await screen.findByRole("table", { name: "Earlier runs" }, { timeout: 5000 });
    expect(within(history).getByText("CLOSED")).toBeInTheDocument();
    await user.click(within(history).getByRole("button", { name: /View/ }));
    expect(await screen.findByRole("table", { name: /^Objects of run/ })).toBeInTheDocument();
    expect((await mockApi.getAudit({ action: "run.closed" })).records).toHaveLength(1);
  }, 20000);

  it("switches to a new project the backend keeps, and the trail says who made it", async () => {
    const user = userEvent.setup();
    go("/");
    await user.click(await screen.findByRole("button", { name: /^Project: / }));
    await user.click(screen.getByRole("menuitem", { name: "New project…" }));
    await user.keyboard("Finance{Enter}");
    expect(await screen.findByRole("button", { name: "Project: Finance" })).toBeInTheDocument();
    const listing = await mockApi.listProjects();
    expect(listing.projects.map((p) => p.name)).toEqual(["Synapse to Fabric migration", "Finance"]);
    expect(listing.active).toBe(listing.projects[1].id);
    const records = (await mockApi.getAudit({ action: "project" })).records;
    expect(records.map((r) => r.action)).toEqual(["project.activated", "project.created"]);
    expect(records.every((r) => r.actor.kind === "person")).toBe(true);
  }, 15000);

  it("shows the activity of the project and checks that the trail is intact", async () => {
    const items = await readyToRun(2);
    await mockApi.startExecution(items, OPTIONS);
    unlockTo("migrate");
    const user = userEvent.setup();
    go("/migration?step=migrate");
    await user.click(await screen.findByRole("button", { name: "Show activity" }, { timeout: 5000 }));
    const table = await screen.findByRole("table", { name: "Audit trail, newest first" });
    expect(await within(table).findByText("A migration run was asked for")).toBeInTheDocument();
    expect(within(table).getAllByText("Accelerator").length).toBeGreaterThan(0); // its own approval, named as its own
    await user.click(screen.getByRole("button", { name: /Check the trail/ }));
    expect(await screen.findByText("The trail is intact")).toBeInTheDocument();
  }, 20000);

  it("hands this browser's projects to a backend that owns projects, once, carrying the discovery on", async () => {
    localStorage.setItem("ma.apiMode", "real");
    localStorage.setItem("ma.projects", JSON.stringify([{ id: "default", name: "Synapse to Fabric migration", createdAt: "2026-01-01T00:00:00Z" },
      { id: "pabc", name: "Finance", createdAt: "2026-02-01T00:00:00Z" }]));
    localStorage.setItem("ma.projects.current", JSON.stringify("pabc"));
    let listing = { active: "default", projects: [{ id: "default", name: "Synapse to Fabric migration", createdAt: "2026-01-01T00:00:00Z" }], version: 1, persistent: true };
    const posted: unknown[] = [];
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/projects" && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posted.push(body);
        listing = { ...listing, version: listing.version + 1, projects: [...listing.projects, { id: body.id, name: body.name, createdAt: "2026-10-15T00:00:00Z" }] };
        return json({ ...listing, created: body.id });
      }
      if (url === "/api/projects") return json(listing);
      if (url === "/api/projects/pabc/activate") {
        listing = { ...listing, active: "pabc", version: listing.version + 1 };
        return json(listing);
      }
      return json({ error: { code: "not_found", message: "No such endpoint." } }, 404);
    }));
    go("/");
    expect(await screen.findByRole("button", { name: "Project: Finance" }, { timeout: 5000 })).toBeInTheDocument();
    expect(posted).toEqual([{ name: "Finance", id: "pabc", copyFrom: "default" }]);
    expect(stored("ma.projects.adopted")).toBe(true);
  }, 15000);
});

// ---- the consultation (demo): required, as the backend's default ------------------------------------------------

/** Answer, accept the remaining recommendations, submit and approve: a brief in force (Demo data). */
async function approveBrief(answers: [string, unknown][] = []) {
  for (const [q, v] of answers) await mockApi.answerQuestion(q, v);
  await mockApi.acceptRecommendations();
  const view = await mockApi.submitBrief();
  const version = view.versions[view.versions.length - 1].version;
  await mockApi.decideBrief(version, "approve");
  return (await mockApi.getConsultPolicy()).runOptions as RunOptions;
}

describe("consultation (demo)", () => {
  beforeEach(() => { resetDemo(); setDemoConsultation("required"); });

  it("refuses a run until a brief is approved, then starts it under that brief, and only as the brief says", async () => {
    const graph = await mockApi.getDependencies();
    const items = graph.nodes.filter((n) => n.type === "Notebook").slice(0, 1).map((n) => ({ id: n.id, wave: n.wave }));
    await expect(mockApi.startExecution(items)).rejects.toMatchObject({ code: "brief_required" });
    const before = await mockApi.getConsult();
    expect(before.next).toBe("D1");
    expect(before.submittable).toBe(false);
    await expect(mockApi.submitBrief()).rejects.toMatchObject({ code: "brief_not_ready" });
    const options = await approveBrief();
    await expect(mockApi.startExecution(items, { ...options, stopOnFailure: !options.stopOnFailure })).rejects.toMatchObject({ code: "deviation_unexplained" });
    const run = await mockApi.startExecution(items, options);
    expect(run.brief?.version).toBe(1);
    const trail = await mockApi.getAudit({ limit: 100 });
    expect(trail.records.map((r) => r.action)).toEqual(expect.arrayContaining(["consult.accepted", "brief.submitted", "brief.approved", "run.requested"]));
  });

  it("asks what needs a person, takes every recommendation in one go, has the brief approved, and never locks the steps after it", async () => {
    unlockTo("consult");
    const user = userEvent.setup();
    go("/migration?step=consult");
    expect(await stepHeading("Consult")).toBeInTheDocument();
    const summary = await screen.findByLabelText("Consultation summary", {}, { timeout: 5000 });
    expect(within(summary).getByText("Not approved")).toBeInTheDocument();
    // Waves and the rest stay open without a brief: what a required consultation holds back is the run itself.
    expect(screen.getByRole("button", { name: /Continue to Waves anyway/ })).toBeEnabled();
    const d1 = screen.getByRole("region", { name: /^D1\./ });
    expect(within(d1).getByText("A person answers")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Accept all recommendations" }));
    await waitFor(() => expect(within(screen.getByRole("region", { name: /^D1\./ })).getByText("Answered")).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: /^Brief/ }));
    await user.click(await screen.findByRole("button", { name: "Submit the brief" }));
    await user.click(await screen.findByRole("button", { name: "Approve version 1" }));
    await waitFor(() => expect(within(screen.getByLabelText("Consultation summary")).getByText("v1")).toBeInTheDocument());
    expect(await screen.findByRole("list", { name: "Brief versions" })).toHaveTextContent(/Version 1.*APPROVED/);
    await user.click(await screen.findByRole("button", { name: /continue to Waves/ }));
    expect(await stepHeading("Waves")).toBeInTheDocument();
  }, 30000);

  it("holds a contradiction until it is resolved: an answer kept with a reason, and what an answer would change said first", async () => {
    unlockTo("consult");
    const user = userEvent.setup();
    go("/migration?step=consult");
    const c6 = await screen.findByRole("region", { name: /^C6\./ }, { timeout: 5000 });
    await user.click(within(c6).getByRole("radio", { name: /Replace their rows/ }));
    expect(await within(c6).findByText(/This answer .*data\.mode: if_empty → replace/)).toBeInTheDocument();
    await user.click(within(c6).getByRole("button", { name: "Save answer" }));
    const e4 = screen.getByRole("region", { name: /^E4\./ });
    await user.click(within(e4).getByRole("radio", { name: /^Never/ }));
    await user.click(within(e4).getByRole("button", { name: "Save answer" }));
    const conflict = await screen.findByRole("listitem", { name: "Contradiction K2" });
    expect(conflict).toHaveTextContent(/Replacing rows empties tables/);
    await user.click(within(conflict).getByRole("button", { name: "Keep my answers" }));
    await user.type(within(conflict).getByRole("textbox", { name: /Why K2/ }), "Only the test copy is replaced");
    await user.click(within(conflict).getByRole("button", { name: "Keep with this reason" }));
    expect(await screen.findByRole("listitem", { name: "Contradiction K2, kept" })).toHaveTextContent("Only the test copy is replaced");
  }, 30000);

  it("pre-sets Plan from the brief, leaves out what the brief does, and asks why when the plan differs from it", async () => {
    await approveBrief([["A0", ["structure", "orchestration"]]]);
    unlockTo("plan");
    const user = userEvent.setup();
    go("/migration?step=plan");
    expect(await screen.findByText(/Add all skips the .* objects? the approved brief leaves out\. Written for the serverless SQL pool/, {}, { timeout: 8000 })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Add all .* objects/ }));
    expect(await screen.findByText("Pre-set from migration brief version 1. Nothing here differs from it.", {}, { timeout: 8000 })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Stages & credentials" }));
    const data = await screen.findByRole("checkbox", { name: "Include Table data" });
    expect(data).not.toBeChecked();
    await user.click(data);
    expect(await screen.findByText(/This plan differs from brief version 1/, {}, { timeout: 8000 })).toBeInTheDocument();
    const next = await screen.findByRole("button", { name: /Continue to Migrate anyway/ }, { timeout: 8000 });
    await waitFor(() => expect(next).toBeEnabled());
    await user.click(next);
    const dialog = await screen.findByRole("dialog", { name: /The plan differs from the approved brief/ });
    await user.type(within(dialog).getByRole("textbox", { name: "Why the plan differs from the brief" }), "The client wants the data in this pilot");
    await user.click(within(dialog).getByRole("button", { name: "Confirm the plan with this reason" }));
    expect(await stepHeading("Migrate")).toBeInTheDocument();
    const trail = await mockApi.getAudit({ limit: 50 });
    expect(trail.records.find((r) => r.action === "plan.deviation")?.details.reason).toBe("The client wants the data in this pilot");
  }, 40000);

  it("pauses a run before each later wave until a person signs that wave off", async () => {
    const options = await approveBrief([["A6", "signoff_each_wave"]]);
    expect(options.waveGate).toBe(true);
    const graph = await mockApi.getDependencies();
    const notebooks = graph.nodes.filter((n) => n.type === "Notebook").slice(0, 2);
    const items = notebooks.map((n, i) => ({ id: n.id, wave: i + 1 }));
    localStorage.setItem(PLAN, JSON.stringify(items));
    await mockApi.startExecution(items, options);
    await waitFor(async () => expect((await mockApi.getExecution()).pausedReason).toBe("awaiting_wave_signoff"), { timeout: 5000 });
    unlockTo("migrate");
    const user = userEvent.setup();
    go("/migration?step=migrate");
    expect(await screen.findByText("Wave 2 waits for a person to sign it off", {}, { timeout: 5000 })).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: /Comment \(kept with the sign-off\)/ }), "Wave 1 checked");
    await user.click(screen.getByRole("button", { name: "Sign off wave 2 and continue" }));
    await waitFor(async () => expect((await mockApi.getExecution()).releasedWaves).toEqual([2]), { timeout: 5000 });
    const signed = (await mockApi.getAudit({ limit: 50 })).records.find((r) => r.action === "run.wave_signed_off");
    expect(signed?.details).toMatchObject({ wave: 2, comment: "Wave 1 checked" });
  }, 30000);

  it("reads answers from notes for a person to confirm, and a withdrawn consent stops LLM drafts at once", async () => {
    unlockTo("consult");
    const user = userEvent.setup();
    go("/migration?step=consult");
    await user.click(await screen.findByRole("tab", { name: "Help" }, { timeout: 5000 }));
    await user.type(screen.getByRole("textbox", { name: "Meeting notes or an email" }), "Every run must be signed off. No AI please.");
    await user.click(screen.getByRole("button", { name: "Read answers from the notes" }));
    expect(await screen.findByText(/2 answers proposed\. Demo data reads notes with the built-in rules/)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /^Questions/ }));
    await user.click(await screen.findByRole("button", { name: "Confirm D1" }));
    await waitFor(() => expect(within(screen.getByRole("region", { name: /^D1\./ })).getByText(/Read from notes, confirmed by/)).toBeInTheDocument());
    expect(screen.getByRole("list", { name: "Proposed answers" })).toHaveTextContent(/E4/);
    await expect(mockApi.generateTransforms(["anything"])).rejects.toMatchObject({ code: "llm_no_consent" });
  }, 30000);

  it("writes the approved brief into MIGRATION_CONTEXT.md, sends it with every draft, and flags a draft made under an earlier one", async () => {
    await discovered();
    expect((await mockApi.getMigrationContext()).hash).toBeNull();
    await approveBrief([["D1", true], ["B3", "Prefix every view with v_."], ["I4", "T-SQL first"]]);
    const made = await mockApi.getMigrationContext();
    expect(made.text).toMatch(/^# Migration context/);
    expect(made.text).toContain("Prefix every view with v_.");
    expect(made.text).toContain("T-SQL first");
    const procs = await mockApi.getResults({ ...Q, types: ["Stored Procedure"] });
    const [a] = procs.items;
    await mockApi.generateTransforms([a.id]);
    await waitFor(async () => expect((await mockApi.getTransform(a.id)).state).toBe("draft"), { timeout: 4000 });
    const draft = (await mockApi.getTransform(a.id)).draft!;
    expect(draft.context?.hash).toBe(made.hash);
    expect(draft.contextStale).toBe(false);
    expect((await mockApi.getTransformPrompt(a.id)).prompt).toContain("MIGRATION_CONTEXT.md");
    expect((await mockApi.exportMigrationContext(made.hash!)).text).toBe(made.text);
    await approveBrief([["B3", "Keywords in lower case."]]);
    expect((await mockApi.getTransform(a.id)).draft?.contextStale).toBe(true);
  }, 30000);

  it("keeps a draft current when the brief is approved again with the same decisions, and runs nothing H4 says may write to production", async () => {
    await discovered();
    await approveBrief([["D1", true], ["H4", "unsure"]]);
    const first = await mockApi.getMigrationContext();
    const [a] = (await mockApi.getResults({ ...Q, types: ["Stored Procedure"] })).items;
    await mockApi.generateTransforms([a.id]);
    await waitFor(async () => expect((await mockApi.getTransform(a.id)).state).toBe("draft"), { timeout: 4000 });
    await approveBrief([["E4", "always"]]);  // a decision that never reaches the model
    const again = await mockApi.getMigrationContext();
    expect(again.hash).not.toBe(first.hash);
    expect(again.decisionsHash).toBe(first.decisionsHash);
    expect((await mockApi.getTransform(a.id)).draft?.contextStale).toBe(false);
    await expect(mockApi.runValidation([], {}, { probe: true, run: ["pipeline"], timeoutMinutes: 30 })).rejects.toMatchObject({ code: "external_writes" });
  }, 30000);

  it("says on a draft's review which MIGRATION_CONTEXT.md it was sent and what it applied, and flags it once the brief moves on", async () => {
    await discovered();
    await approveBrief([["D1", true], ["B3", "Prefix every view with v_."]]);
    const made = await mockApi.getMigrationContext();
    const views = await mockApi.getResults({ ...Q, types: ["View"] });
    const object = await mockApi.getObject(views.items[0].id);
    const user = userEvent.setup();
    const first = render(<AppStateProvider><TransformPanel object={object} /></AppStateProvider>);
    await user.click(await screen.findByRole("button", { name: /Generate with LLM/ }));
    await screen.findByLabelText("LLM draft (editable)", {}, { timeout: 6000 });
    const sent = document.querySelector(".context-ref");
    expect(sent?.textContent).toContain(`Sent MIGRATION_CONTEXT.md from brief version ${made.briefVersion}`);
    expect(sent?.textContent).toContain(made.hash!.slice(0, 12));
    expect(screen.getByText("What it applied from the migration context")).toBeInTheDocument();
    expect(screen.queryByText("Made with an earlier migration context")).toBeNull();
    first.unmount();
    await approveBrief([["B3", "Keywords in lower case."]]);
    render(<AppStateProvider><TransformPanel object={object} /></AppStateProvider>);
    expect(await screen.findByText("Made with an earlier migration context", {}, { timeout: 6000 })).toBeInTheDocument();
  }, 30000);

  it("shows the migration context on the Consult step, and a journey saved before Consult existed keeps its later steps open", async () => {
    await approveBrief([["D1", true]]);
    localStorage.setItem(JOURNEY, JSON.stringify(["discover", "assess", "waves"]));
    const user = userEvent.setup();
    go("/migration?step=plan");
    expect(await stepHeading("Plan")).toBeInTheDocument();
    go("/migration?step=consult");
    await user.click(await screen.findByRole("tab", { name: "Migration context" }, { timeout: 5000 }));
    expect(await screen.findByLabelText("The migration context")).toHaveTextContent(/Made from the approved migration brief, version 1/);
  }, 30000);
});
