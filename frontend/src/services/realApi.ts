import {
  ApiRequestError,
  type ComponentRow,
  type DependencyGraph,
  type MetadataExport,
  type ConnectionConfig,
  type ConnectionState,
  type DiscoveryStatus,
  type Capabilities,
  type ExecutionRun,
  type PlanAnalysis,
  type FabricTarget,
  type Health,
  type MigrationApi,
  type ObjectDetail,
  type ResultsPage,
  type ResultsQuery,
  type ValidationRow,
} from "../types";

/**
 * The backend restarts itself after an update and is back within seconds. A read that fails
 * soon after the backend last answered is tried again for a little while, instead of failing
 * the page; a write is never repeated. Exported for tests.
 */
export const RESTART_GRACE = { recentMs: 60_000, retryForMs: 15_000, stepMs: 500 };
let lastAnswer = 0;

async function request<T>(path: string, init?: RequestInit, retry = true): Promise<T> {
  const read = !init?.method || init.method === "GET";
  const began = Date.now();
  for (;;) {
    try {
      return await once<T>(path, init);
    } catch (e) {
      const restarting = retry && read && e instanceof ApiRequestError && e.code === "backend_unavailable"
        && began - lastAnswer < RESTART_GRACE.recentMs && Date.now() - began < RESTART_GRACE.retryForMs;
      if (!restarting) throw e;
      await new Promise((r) => setTimeout(r, RESTART_GRACE.stepMs));
    }
  }
}

async function once<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      credentials: "same-origin",
    });
  } catch {
    throw new ApiRequestError(
      "backend_unavailable",
      "The accelerator backend is not reachable. Start it with `python -m discovery_agent.api`.",
    );
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // A non-JSON reply (for example a proxy error page) is handled below.
  }
  if (!response.ok) {
    const err = (body as { error?: { code?: string; message?: string } } | null)?.error;
    if (!err && response.status >= 500) {
      throw new ApiRequestError(
        "backend_unavailable",
        "The accelerator backend is not reachable. Start it with `python -m discovery_agent.api`.",
        response.status,
      );
    }
    lastAnswer = Date.now();
    throw new ApiRequestError(
      err?.code ?? "request_failed",
      err?.message ?? `The request failed (HTTP ${response.status}).`,
      response.status,
    );
  }
  lastAnswer = Date.now();
  return body as T;
}

/** Non-secret fields shared by every request. */
function connectionBody(c: ConnectionConfig) {
  return {
    method: c.method,
    tenantId: c.tenantId.trim(),
    subscriptionId: c.subscriptionId.trim(),
    resourceGroup: c.resourceGroup.trim(),
    workspace: c.workspace.trim(),
    workspaceUrl: c.workspaceUrl.trim(),
    sqlPool: c.sqlPool.trim(),
  };
}

function toQueryString(q: ResultsQuery): string {
  const p = new URLSearchParams();
  if (q.search) p.set("search", q.search);
  if (q.categories.length) p.set("category", q.categories.join(","));
  if (q.types.length) p.set("type", q.types.join(","));
  if (q.statuses.length) p.set("status", q.statuses.join(","));
  if (q.fabricTargets.length) p.set("fabricTarget", q.fabricTargets.join(","));
  if (q.paths.length) p.set("path", q.paths.join(","));
  if (q.workstreams.length) p.set("workstream", q.workstreams.join(","));
  if (q.mappingStatuses.length) p.set("mappingStatus", q.mappingStatuses.join(","));
  if (q.classifications.length) p.set("classification", q.classifications.join(","));
  if (q.assessment) p.set("assessment", q.assessment);
  p.set("sort", q.sort);
  p.set("dir", q.dir);
  p.set("page", String(q.page));
  p.set("pageSize", String(q.pageSize));
  return p.toString();
}

export const realApi: MigrationApi = {
  mode: "real",
  // Fails at once: the page's own watch decides when the backend is away.
  health: () => request<Health>("/api/health", undefined, false),
  getConnection: () => request<ConnectionState>("/api/connections"),
  authenticate: (c) =>
    request<ConnectionState>("/api/connections/authenticate", {
      method: "POST",
      body: JSON.stringify(connectionBody(c)),
    }),
  testConnection: (c) =>
    request<ConnectionState>("/api/connections/test", {
      method: "POST",
      body: JSON.stringify(connectionBody(c)),
    }),
  disconnect: () => request<ConnectionState>("/api/connections", { method: "DELETE" }),
  startDiscovery: () => request<DiscoveryStatus>("/api/discovery/start", { method: "POST" }),
  getDiscoveryStatus: () => request<DiscoveryStatus>("/api/discovery/status"),
  resetDiscovery: () => request<DiscoveryStatus>("/api/discovery", { method: "DELETE" }),
  getResults: (q) => request<ResultsPage>(`/api/discovery/results?${toQueryString(q)}`),
  getObject: (id) =>
    request<ObjectDetail>(`/api/discovery/results/${encodeURIComponent(id)}`),
  listResourceGroups: async () => (await request<{ items: string[] }>("/api/azure/resource-groups")).items,
  listWorkspaces: async (rg) =>
    (await request<{ items: string[] }>(`/api/azure/workspaces?resourceGroup=${encodeURIComponent(rg)}`)).items,
  getDependencies: () => request<DependencyGraph>("/api/dependencies"),
  getComponents: async () => (await request<{ components: ComponentRow[] }>("/api/mapping/components")).components,
  exportMetadata: () => request<MetadataExport>("/api/discovery/export"),
  getFabricTarget: () => request<FabricTarget>("/api/fabric/connection"),
  authenticateFabric: (c) =>
    request<FabricTarget>("/api/fabric/authenticate", { method: "POST", body: JSON.stringify({ method: c.method }) }),
  testFabric: (c) =>
    request<FabricTarget>("/api/fabric/test", {
      method: "POST",
      body: JSON.stringify({ method: c.method, workspaceId: c.workspaceId.trim() }),
    }),
  disconnectFabric: () => request<FabricTarget>("/api/fabric/connection", { method: "DELETE" }),
  getCapabilities: () => request<Capabilities>("/api/migration/capabilities"),
  analyzePlan: (items, record, options, credentials) =>
    request<PlanAnalysis>("/api/migration/plan", {
      method: "POST",
      body: JSON.stringify({ items, record: !!record, ...(options ? { options } : {}), ...(credentials ? { credentials } : {}) }),
    }),
  startExecution: (items, options, credentials) =>
    request<ExecutionRun>("/api/migration/start", {
      method: "POST",
      // Credentials travel once, over localhost, into the backend's memory. Never stored by the UI.
      body: JSON.stringify({ items, ...(options ? { options } : {}), ...(credentials ? { credentials } : {}) }),
    }),
  getExecution: () => request<ExecutionRun>("/api/migration/run"),
  controlExecution: (action, credentials) =>
    request<ExecutionRun>("/api/migration/control", {
      method: "POST",
      // Resume and Retry send the credentials again (memory only): a backend that restarted has none.
      body: JSON.stringify({ action, ...(credentials && Object.keys(credentials).length && (action === "resume" || action === "retry") ? { credentials } : {}) }),
    }),
  runValidation: async (items) =>
    (await request<{ rows: ValidationRow[] }>("/api/migration/validate", { method: "POST", body: JSON.stringify(items?.length ? { items } : {}) })).rows,
  listSqlPools: async (rg, ws) =>
    (await request<{ items: string[] }>(
      `/api/azure/sql-pools?resourceGroup=${encodeURIComponent(rg)}&workspace=${encodeURIComponent(ws)}`,
    )).items,
};

