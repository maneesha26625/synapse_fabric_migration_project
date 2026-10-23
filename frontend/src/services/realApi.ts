import {
  ApiRequestError,
  type AzureSubscription,
  type ComponentRow,
  type DependencyGraph,
  type MetadataExport,
  type ConnectionConfig,
  type ConnectionState,
  type DiscoveryStatus,
  type Capabilities,
  type ExecutionRun,
  type FabricGitState,
  type PlanAnalysis,
  type FabricTarget,
  type WorkspaceGit,
  type Health,
  type MigrationApi,
  type ObjectDetail,
  type ResultsPage,
  type ResultsQuery,
  type ProjectRequirements,
  type TransformCandidates,
  type ConsultFacts,
  type TransformObject,
  type TransformPrompt,
  type TransformQueued,
  type TransformSettings,
  type ValidationRow,
  type ZipUpload,
} from "../types";

/** The server's background validation, as GET /api/migration/validation reports it. */
interface ValidationState {
  state: "idle" | "running" | "completed" | "failed";
  progress?: string;
  rows?: ValidationRow[];
  error?: { code?: string; message?: string };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
    throw new ApiRequestError(
      err?.code ?? "request_failed",
      err?.message ?? `The request failed (HTTP ${response.status}).`,
      response.status,
    );
  }
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
  health: () => request<Health>("/api/health"),
  getConnection: () => request<ConnectionState>("/api/connections"),
  authenticate: (c) =>
    request<ConnectionState>("/api/connections/authenticate", {
      method: "POST",
      // The sign-in takes nothing but the method: the subscription is chosen after it, from a list.
      body: JSON.stringify({ method: c.method }),
    }),
  authenticateDirectory: (tenantId) =>
    request<ConnectionState>("/api/connections/authenticate-directory", { method: "POST", body: JSON.stringify({ tenantId }) }),
  testConnection: (c) =>
    request<ConnectionState>("/api/connections/test", {
      method: "POST",
      body: JSON.stringify(connectionBody(c)),
    }),
  uploadZip: (file) =>
    request<ZipUpload>(`/api/connections/upload?name=${encodeURIComponent(file.name)}`, {
      method: "POST",
      body: file,
      headers: { "Content-Type": "application/zip" },
    }),
  connectRepository: (c) =>
    request<ConnectionState>("/api/connections/repository", { method: "POST", body: JSON.stringify(c) }),
  disconnect: () => request<ConnectionState>("/api/connections", { method: "DELETE" }),
  startDiscovery: () => request<DiscoveryStatus>("/api/discovery/start", { method: "POST" }),
  getDiscoveryStatus: () => request<DiscoveryStatus>("/api/discovery/status"),
  resetDiscovery: () => request<DiscoveryStatus>("/api/discovery", { method: "DELETE" }),
  getResults: (q) => request<ResultsPage>(`/api/discovery/results?${toQueryString(q)}`),
  getObject: (id) =>
    request<ObjectDetail>(`/api/discovery/results/${encodeURIComponent(id)}`),
  listSubscriptions: async () => (await request<{ items: AzureSubscription[] }>("/api/azure/subscriptions")).items,
  listResourceGroups: async (sub) =>
    (await request<{ items: string[] }>(`/api/azure/resource-groups?subscriptionId=${encodeURIComponent(sub)}`)).items,
  listWorkspaces: async (sub, rg) =>
    (await request<{ items: string[] }>(`/api/azure/workspaces?subscriptionId=${encodeURIComponent(sub)}&resourceGroup=${encodeURIComponent(rg)}`)).items,
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
  getFabricGit: () => request<FabricGitState>("/api/fabric/git"),
  commitFabricGit: (comment) =>
    request<FabricGitState>("/api/fabric/git/commit", { method: "POST", body: JSON.stringify(comment ? { comment } : {}) }),
  getCapabilities: () => request<Capabilities>("/api/migration/capabilities"),
  analyzePlan: (items, record, options, credentialNames) =>
    request<PlanAnalysis>("/api/migration/plan", {
      method: "POST",
      body: JSON.stringify({ items, record: !!record, ...(options ? { options } : {}), ...(credentialNames?.length ? { credentialNames } : {}) }),
    }),
  startExecution: (items, options, credentials) =>
    request<ExecutionRun>("/api/migration/start", {
      method: "POST",
      // Credentials travel once, over localhost, into the backend's memory. Never stored by the UI.
      body: JSON.stringify({ items, ...(options ? { options } : {}), ...(credentials ? { credentials } : {}) }),
    }),
  getExecution: () => request<ExecutionRun>("/api/migration/run"),
  controlExecution: (action) =>
    request<ExecutionRun>("/api/migration/control", { method: "POST", body: JSON.stringify({ action }) }),
  runValidation: async (items, options, runtime, onProgress) => {
    // Validation runs in the background on the server (runs in Fabric can take minutes): start it, then follow it.
    await request<ValidationState>("/api/migration/validate", { method: "POST", body: JSON.stringify({
      ...(items?.length ? { items } : {}),
      // Date filters decide which Synapse rows a table should hold, so the counts compare like with like.
      ...(options?.dataFilters ? { options: { dataFilters: options.dataFilters } } : {}),
      ...(runtime ? { runtime } : {}),
    }) });
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const state = await request<ValidationState>("/api/migration/validation");
      if (state.state === "completed") return state.rows ?? [];
      if (state.state === "failed") throw new ApiRequestError(state.error?.code ?? "validation_failed", state.error?.message ?? "Validation stopped.");
      if (state.state !== "running") throw new ApiRequestError("validation_lost", "The server has no validation running (was it restarted?). Run validation again.");
      onProgress?.(state.progress ?? "Validating");
    }
  },
  getTransformSettings: () => request<TransformSettings>("/api/transform/settings"),
  listTransforms: async () => (await request<{ items: TransformObject[] }>("/api/transform")).items,
  getTransform: (id) => request<TransformObject>(`/api/transform/objects/${encodeURIComponent(id)}`),
  getTransformPrompt: (id) => request<TransformPrompt>(`/api/transform/prompt/${encodeURIComponent(id)}`),
  generateTransforms: async (ids) =>
    (await request<{ items: TransformQueued[] }>("/api/transform/generate", { method: "POST", body: JSON.stringify({ ids }) })).items,
  acceptTransform: (id, code) =>
    request<TransformObject>("/api/transform/accept", { method: "POST", body: JSON.stringify(code === undefined ? { id } : { id, code }) }),
  discardTransform: (id, scope) =>
    request<TransformObject>("/api/transform/discard", { method: "POST", body: JSON.stringify({ id, scope }) }),
  listTransformCandidates: () => request<TransformCandidates>("/api/transform/candidates"),
  getConsultFacts: () => request<ConsultFacts>("/api/consult/facts"),
  getProjectRequirements: () => request<ProjectRequirements>("/api/transform/project"),
  saveProjectRequirements: (text) =>
    request<ProjectRequirements>("/api/transform/project", { method: "POST", body: JSON.stringify({ text }) }),
  listSqlPools: async (sub, rg, ws) =>
    (await request<{ items: string[] }>(
      `/api/azure/sql-pools?subscriptionId=${encodeURIComponent(sub)}&resourceGroup=${encodeURIComponent(rg)}&workspace=${encodeURIComponent(ws)}`,
    )).items,
  getWorkspaceGit: (sub, rg, ws) =>
    request<WorkspaceGit>(
      `/api/azure/workspace-git?subscriptionId=${encodeURIComponent(sub)}&resourceGroup=${encodeURIComponent(rg)}&workspace=${encodeURIComponent(ws)}`,
    ),
};

