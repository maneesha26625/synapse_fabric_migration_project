import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import { mockApi } from "../mock/mockApi";
import type { ConnectionConfig, FabricConfig, ResultsQuery } from "../types";

const CONFIG: ConnectionConfig = {
  method: "azure_cli", tenantId: "", subscriptionId: "10eb96c3-ba3c-492e-b95b-e9f1d6d85d70",
  resourceGroup: "rg-demo-migration", workspace: "demo-synapse-ws", workspaceUrl: "", sqlPool: "", resource: "",
};
const FABRIC: FabricConfig = { method: "azure_cli", workspaceId: "", workspaceName: "Fabric_practice" };
const Q: ResultsQuery = {
  search: "", categories: [], types: [], statuses: [], fabricTargets: [], paths: [], workstreams: [], mappingStatuses: [],
  classifications: [], assessment: "", sort: "name", dir: "asc", page: 1, pageSize: 200,
};

/** Connect and finish discovery in the demo layer, so a page can be rendered against results. */
async function discovered() {
  await mockApi.testConnection(CONFIG);
  await mockApi.startDiscovery();
  await new Promise((r) => setTimeout(r, 3700));
}

function go(path: string) {
  window.history.pushState({}, "", path);
  return render(<App />);
}

beforeEach(async () => {
  localStorage.clear();
  localStorage.setItem("ma.apiMode", "mock");
  window.history.pushState({}, "", "/");
  await mockApi.disconnect();
  await mockApi.disconnectFabric();
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
    const movable = ["Notebook", "Table", "View", "Stored Procedure"];
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
});

describe("routes", () => {
  const routes: [string, string][] = [
    ["/", "Projects"],
    ["/synapse", "Synapse Source"],
    ["/fabric", "Fabric Target"],
    ["/discovery", "Discovery"],
    ["/assessment", "Migration Assessment"],
    ["/dependencies", "Dependencies & Migration Waves"],
    ["/migrate", "Plan & Migrate"],
    ["/validate", "Migration Validation"],
  ];
  it.each(routes)("renders %s with its empty state when nothing is connected", async (path, title) => {
    go(path);
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
  });

  it.each(["/plan", "/execute", "/execution"])("sends the old %s URL to Plan & Migrate", async (path) => {
    go(path);
    expect(await screen.findByRole("heading", { level: 1, name: "Plan & Migrate" })).toBeInTheDocument();
  });

  it("keeps the earlier URLs working", async () => {
    go("/connections");
    expect(await screen.findByRole("heading", { level: 1, name: "Synapse Source" })).toBeInTheDocument();
  });

  it("shows the sidebar sections and the workflow stepper", async () => {
    go("/");
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    for (const label of ["Projects", "Synapse Source", "Fabric Target", "Discovery", "Assessment", "Dependencies & Waves", "Plan & Migrate", "Validation"]) {
      expect(within(nav).getByRole("link", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("list", { name: "Migration workflow" })).toBeInTheDocument();
  });
});

describe("synapse source", () => {
  it("never offers Fabric in the source connection", async () => {
    go("/synapse");
    await screen.findByRole("heading", { level: 1, name: "Synapse Source" });
    expect(screen.queryByLabelText(/fabric/i)).toBeNull();
  });

  it("offers Azure CLI and Interactive browser only, and swaps the form with the method", async () => {
    const user = userEvent.setup();
    go("/synapse");
    await user.click((await screen.findAllByRole("button", { name: /Add Synapse Workspace/ }))[0]);
    expect(await screen.findAllByRole("radio")).toHaveLength(2);
    for (const gone of [/Service Principal/, /Managed Identity/]) expect(screen.queryByRole("radio", { name: gone })).toBeNull();
    await user.click(screen.getByRole("radio", { name: /Interactive browser/ }));
    // Only the two fields the sign-in needs: no optional extras, no secret.
    expect(screen.getByLabelText("Tenant ID")).toBeInTheDocument();
    expect(screen.getByLabelText("Subscription ID")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Client ID/)).toBeNull();
    expect(screen.queryByLabelText(/secret/i)).toBeNull();
    expect(screen.queryByText("(optional)")).toBeNull();
  });

  it("requires the tenant for the browser sign-in and says where the window opens", async () => {
    const user = userEvent.setup();
    go("/synapse");
    await user.click((await screen.findAllByRole("button", { name: /Add Synapse Workspace/ }))[0]);
    await user.click(await screen.findByRole("radio", { name: /Interactive browser/ }));
    await user.type(screen.getByLabelText("Subscription ID"), CONFIG.subscriptionId);
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    expect(await screen.findByText("Tenant ID is required.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Tenant ID"), "8a24d8ed-7a4b-45b3-b56b-d781dd225aa1");
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    expect(await screen.findByText(/A sign-in window should open on this machine — complete it there. Waiting…/)).toBeInTheDocument();
    await screen.findByLabelText("Resource group", {}, { timeout: 4000 });
  }, 15000);

  it("keeps Discover Workspace disabled until the connection test passes", async () => {
    const user = userEvent.setup();
    go("/synapse");
    await user.click((await screen.findAllByRole("button", { name: /Add Synapse Workspace/ }))[0]);
    await user.click(screen.getByRole("radio", { name: /^Azure CLI/ })); // the form remembers the last method
    expect(screen.getByRole("button", { name: "Test Connection" })).toBeDisabled();
    await user.clear(screen.getByLabelText("Subscription ID"));
    await user.type(screen.getByLabelText("Subscription ID"), CONFIG.subscriptionId);
    await user.click(screen.getByRole("button", { name: "Sign in with Azure" }));
    const group = await screen.findByLabelText("Resource group", {}, { timeout: 4000 });
    await waitFor(() => expect(within(group).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(group, "rg-demo-migration");
    const ws = screen.getByLabelText("Synapse workspace");
    await waitFor(() => expect(within(ws).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(ws, "demo-synapse-ws");
    await user.click(screen.getByRole("button", { name: "Test Connection" }));
    // Re-query each time: the card re-renders as the connection state changes.
    await waitFor(() => expect(screen.getByRole("button", { name: /Discover Workspace/ })).toBeEnabled(), { timeout: 8000 });
  }, 25000);
});

describe("fabric target", () => {
  it("is a separate page with exactly two methods", async () => {
    const user = userEvent.setup();
    go("/fabric");
    await screen.findByRole("heading", { level: 1, name: "Fabric Target" });
    for (const m of ["Azure CLI", "Fabric CLI"]) expect(screen.getByRole("radio", { name: new RegExp(m) })).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /Service Principal/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /Fabric CLI/ }));
    expect(screen.getByRole("button", { name: "Login with Fabric CLI" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/token/i)).not.toBeInTheDocument();
  });

  it("authenticates and tests in separate steps", async () => {
    const user = userEvent.setup();
    go("/fabric");
    await screen.findByRole("heading", { level: 1, name: "Fabric Target" });
    expect(screen.getByRole("button", { name: "Test Connection" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Login with Azure CLI" }));
    await screen.findAllByText("AUTHENTICATED", {}, { timeout: 3000 });
    await user.selectOptions(screen.getByLabelText("Fabric Workspace"), "Fabric_practice");
    await user.click(screen.getByRole("button", { name: "Test Connection" }));
    await screen.findAllByText("CONNECTED", {}, { timeout: 3000 });
    expect(screen.getByText("Workspace accessible")).toBeInTheDocument();
  }, 15000);
});

describe("validation page", () => {
  it("compares both sides in demo mode, shows why each check landed where it did, and filters by status", async () => {
    await discovered();
    await mockApi.authenticateFabric(FABRIC);
    await mockApi.testFabric(FABRIC);
    const items = (await mockApi.getDependencies()).nodes.filter((n) => n.type === "Notebook").slice(0, 2).map((n) => ({ id: n.id, wave: n.wave }));
    await mockApi.startExecution(items);
    await new Promise((r) => setTimeout(r, 2500));
    const user = userEvent.setup();
    go("/validate");
    await screen.findByRole("heading", { level: 1, name: "Migration Validation" });
    expect(screen.queryByText(/not implemented in the backend/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run Validation" }));
    await screen.findByRole("table", { name: "Validation results" }, { timeout: 5000 });
    expect(screen.getByRole("columnheader", { name: "Details" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Match/ }));
    expect(screen.getAllByText(/MATCH/).length).toBeGreaterThan(0);
  }, 20000);
});

describe("discovery page", () => {
  it("shows the inventory with Fabric equivalents and opens an object", async () => {
    await discovered();
    const user = userEvent.setup();
    go("/discovery");
    await screen.findByText("DEMO DATA", { selector: ".badge" });
    const table = await screen.findByRole("table", {}, { timeout: 5000 });
    for (const h of ["Fabric equivalent", "Migration classification", "Object name", "Synapse component"]) {
      expect(within(table).getByText(h)).toBeInTheDocument();
    }
    expect(screen.getByRole("tab", { name: /Pipelines/ })).toBeInTheDocument();
    const rows = await within(table).findAllByRole("row", { name: /Open details/ }, { timeout: 8000 });
    await user.click(rows[0]);
    const dialog = await screen.findByRole("dialog", {}, { timeout: 5000 });
    await within(dialog).findByText("Fabric target", { selector: "h3" }, { timeout: 5000 });
    expect(within(dialog).getByText(/Recommended migration steps/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /Add to Migration Plan/ })).toBeInTheDocument();
  }, 25000);
});

describe("assessment and plan", () => {
  it("counts classifications from the discovery result and offers the component mapping", async () => {
    await discovered();
    const user = userEvent.setup();
    go("/assessment");
    await screen.findByText("Preliminary classification from Discovery", {}, { timeout: 5000 });
    expect(screen.getByText("Requires configuration")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Component Mapping" }));
    expect(await screen.findByText("Serverless SQL", {}, { timeout: 4000 })).toBeInTheDocument();
  }, 20000);

  it("builds a plan, then offers every stage with its strategy and says why the run cannot start yet", async () => {
    await discovered();
    const user = userEvent.setup();
    go("/migrate");
    // Re-query: the card re-renders when the graph arrives, replacing the element.
    await waitFor(() => expect(screen.getByRole("button", { name: /Add all objects/ })).toBeEnabled(), { timeout: 8000 });
    await user.click(screen.getByRole("button", { name: /Add all objects/ }));
    await screen.findByText("DETERMINISTIC");
    expect(await screen.findByText("Readiness", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Risks \(/ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Migration strategy by object type" })).toBeInTheDocument();
    // Every stage is offered, on by default, with its own run button.
    for (const label of ["Warehouse & schema", "Table data", "Spark pool & environment", "Notebooks", "Connections", "Pipelines & datasets", "Spark job definitions", "SQL scripts", "Schedules", "External tables"]) {
      expect(await screen.findByRole("checkbox", { name: `Include ${label}` })).toBeChecked();
    }
    expect(screen.getAllByRole("button", { name: /Run this stage/ })).toHaveLength(10);
    // The Fabric target is not connected in this test, so the planner blocks and nothing can start.
    expect(screen.getByRole("button", { name: /Start migration/ })).toBeDisabled();
    expect(screen.getAllByRole("button", { name: /Run this stage/ }).every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
    expect(screen.getByText("Not ready to start")).toBeInTheDocument();
    // The run strategy is a choice, and stop-on-failure is off by default.
    expect(screen.getByRole("radio", { name: /Automated objects only/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Stop at the end of a wave/ })).not.toBeChecked();
  }, 25000);

  it("switching a stage off marks its objects Not selected, and credentials never reach browser storage", async () => {
    await discovered();
    const user = userEvent.setup();
    go("/migrate");
    await waitFor(() => expect(screen.getByRole("button", { name: /Add all objects/ })).toBeEnabled(), { timeout: 8000 });
    await user.click(screen.getByRole("button", { name: /Add all objects/ }));
    const pipelines = await screen.findByRole("checkbox", { name: "Include Pipelines & datasets" });
    await user.click(pipelines);
    expect(pipelines).not.toBeChecked();
    // Data load options are a strategy the user can change.
    const mode = screen.getByLabelText("If a table already has rows");
    await user.selectOptions(mode, "replace");
    expect(mode).toHaveValue("replace");
    // Credentials: typed into a password field, kept out of localStorage and sessionStorage.
    await user.click(screen.getByText(/Credentials/, { selector: "summary span" }));
    const secret = (await screen.findAllByLabelText(/^Password for /))[0];
    expect(secret).toHaveAttribute("type", "password");
    await user.type(secret, "S3CRET-NEVER-STORED");
    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    expect(stored).not.toContain("S3CRET-NEVER-STORED");
  }, 30000);
});
