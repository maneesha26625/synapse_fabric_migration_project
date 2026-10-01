import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App } from "../App";
import { mockApi } from "../mock/mockApi";
import type { ConnectionConfig, FabricConfig, ResultsQuery } from "../types";

const CONFIG: ConnectionConfig = {
  method: "azure_cli", tenantId: "", subscriptionId: "10eb96c3-ba3c-492e-b95b-e9f1d6d85d70",
  resourceGroup: "rg-demo-migration", workspace: "demo-synapse-ws", workspaceUrl: "", sqlPool: "", clientId: "", clientSecret: "", resource: "",
};
const FABRIC: FabricConfig = { method: "azure_cli", tenantId: "", workspaceId: "", workspaceName: "Fabric_practice", clientId: "", clientSecret: "" };
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

  it("simulates a run only once the Fabric target is connected", async () => {
    await discovered();
    const graph = await mockApi.getDependencies();
    const items = graph.nodes.slice(0, 5).map((n) => ({ id: n.id, wave: n.wave }));
    await expect(mockApi.startExecution(items)).rejects.toMatchObject({ code: "target_not_connected" });
    await mockApi.authenticateFabric(FABRIC);
    await mockApi.testFabric(FABRIC);
    const run = await mockApi.startExecution(items);
    expect(run.total).toBe(5);
    await new Promise((r) => setTimeout(r, 3000));
    const done = await mockApi.getExecution();
    expect(done.state).toBe("completed");
    expect(done.completed + done.failed).toBe(5);
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
    ["/plan", "Migration Plan"],
    ["/execute", "Migration Execution"],
    ["/validate", "Migration Validation"],
  ];
  it.each(routes)("renders %s with its empty state when nothing is connected", async (path, title) => {
    go(path);
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
  });

  it("keeps the earlier URLs working", async () => {
    go("/connections");
    expect(await screen.findByRole("heading", { level: 1, name: "Synapse Source" })).toBeInTheDocument();
  });

  it("shows the sidebar sections and the workflow stepper", async () => {
    go("/");
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    for (const label of ["Projects", "Synapse Source", "Fabric Target", "Discovery", "Assessment", "Dependencies & Waves", "Migration Plan", "Execute Migration", "Validation"]) {
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

  it("masks the client secret and swaps the form with the selected method", async () => {
    const user = userEvent.setup();
    go("/synapse");
    await user.click((await screen.findAllByRole("button", { name: /Add Synapse Workspace/ }))[0]);
    await user.click(await screen.findByRole("radio", { name: /Service Principal/ }));
    expect(screen.getByLabelText("Client Secret")).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("radio", { name: /Managed Identity/ }));
    expect(screen.queryByLabelText("Client Secret")).toBeNull();
    expect(screen.getByLabelText(/Identity client ID/)).toBeInTheDocument();
  });

  it("keeps Discover Workspace disabled until the connection test passes", async () => {
    const user = userEvent.setup();
    go("/synapse");
    await user.click((await screen.findAllByRole("button", { name: /Add Synapse Workspace/ }))[0]);
    expect(screen.getByRole("button", { name: "Test Connection" })).toBeDisabled();
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
  it("is a separate page with its own three methods", async () => {
    const user = userEvent.setup();
    go("/fabric");
    await screen.findByRole("heading", { level: 1, name: "Fabric Target" });
    for (const m of ["Azure CLI", "Fabric CLI", "Service Principal"]) expect(screen.getByRole("radio", { name: new RegExp(m) })).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /Service Principal/ }));
    expect(screen.getByLabelText("Client Secret")).toHaveAttribute("type", "password");
  });

  it("authenticates and tests in separate steps", async () => {
    const user = userEvent.setup();
    go("/fabric");
    await screen.findByRole("heading", { level: 1, name: "Fabric Target" });
    expect(screen.getByRole("button", { name: "Test Connection" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Login with Azure CLI" }));
    await screen.findAllByText("AUTHENTICATED", {}, { timeout: 3000 });
    await user.type(screen.getByLabelText("Workspace name"), "Fabric_practice");
    await user.click(screen.getByRole("button", { name: "Test Connection" }));
    await screen.findAllByText("CONNECTED", {}, { timeout: 3000 });
    expect(screen.getByText("Fabric_practice", { selector: "h2" })).toBeInTheDocument();
  }, 15000);
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

  it("builds a plan from suggested waves", async () => {
    await discovered();
    const user = userEvent.setup();
    go("/plan");
    // Re-query: the card re-renders when the graph arrives, replacing the element.
    await waitFor(() => expect(screen.getByRole("button", { name: /Add all objects/ })).toBeEnabled(), { timeout: 8000 });
    await user.click(screen.getByRole("button", { name: /Add all objects/ }));
    await screen.findByRole("heading", { name: "Wave 1" }, { timeout: 4000 });
    expect(screen.getAllByText(/^(READY|BLOCKED|NOT STARTED)$/).length).toBeGreaterThan(0);
  }, 20000);
});
