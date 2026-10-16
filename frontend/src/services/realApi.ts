import {
  ApiRequestError,
  type AnswerPreview,
  type AuditPage,
  type AuditVerification,
  type BriefView,
  type ConsultCatalog,
  type ConsultExplanation,
  type ConsultPolicy,
  type ConsultState,
  type PlanDifference,
  type ProjectListing,
  type RunHistory,
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
  finishedAt?: string;
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
  startExecution: (items, options, credentials, deviationReason) =>
    request<ExecutionRun>("/api/migration/start", {
      method: "POST",
      // Credentials travel once, over localhost, into the backend's memory. Never stored by the UI.
      body: JSON.stringify({ items, ...(options ? { options } : {}), ...(credentials ? { credentials } : {}),
        ...(deviationReason ? { deviationReason } : {}) }),
    }),
  getExecution: () => request<ExecutionRun>("/api/migration/run"),
  controlExecution: (action, extra) =>
    request<ExecutionRun>("/api/migration/control", {
      method: "POST",
      // Credentials a restart lost travel once more, the same way: into the backend's memory, never stored here.
      body: JSON.stringify({ action, ...(extra?.version !== undefined ? { version: extra.version } : {}),
        ...(extra?.comment ? { comment: extra.comment } : {}),
        ...(extra?.credentials && Object.keys(extra.credentials).length ? { credentials: extra.credentials } : {}) }),
    }),
  approveExecution: (decision, comment, extra) =>
    request<ExecutionRun>("/api/migration/approve", {
      method: "POST",
      body: JSON.stringify({ decision, ...(comment ? { comment } : {}), ...(extra?.subjectHash ? { subjectHash: extra.subjectHash } : {}),
        ...(extra?.credentials && Object.keys(extra.credentials).length ? { credentials: extra.credentials } : {}) }),
    }),
  listRuns: () => request<RunHistory>("/api/migration/runs"),
  getRun: (runId) => request<ExecutionRun>(`/api/migration/runs/${encodeURIComponent(runId)}`),
  getValidation: () => request<ValidationState>("/api/migration/validation"),
  listProjects: () => request<ProjectListing>("/api/projects"),
  createProject: (name, options) =>
    request<ProjectListing>("/api/projects", { method: "POST", body: JSON.stringify({ name, ...(options ?? {}) }) }),
  renameProject: (id, name, version) =>
    request<ProjectListing>(`/api/projects/${encodeURIComponent(id)}/rename`, {
      method: "POST", body: JSON.stringify({ name, ...(version !== undefined ? { version } : {}) }) }),
  activateProject: (id, version) =>
    request<ProjectListing>(`/api/projects/${encodeURIComponent(id)}/activate`, {
      method: "POST", body: JSON.stringify(version !== undefined ? { version } : {}) }),
  deleteProject: (id, version) =>
    request<ProjectListing>(`/api/projects/${encodeURIComponent(id)}${version !== undefined ? `?version=${version}` : ""}`, { method: "DELETE" }),
  getAudit: (q) => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(q ?? {})) if (v !== undefined && v !== "") p.set(k, String(v));
    return request<AuditPage>(`/api/audit${p.toString() ? `?${p}` : ""}`);
  },
  verifyAudit: () => request<AuditVerification>("/api/audit/verify"),
  exportAudit: async (format) => ({ url: `/api/audit/export?format=${format}`, fileName: `audit.${format}` }),
  confirmStep: async (step, note) => {
    await request<unknown>("/api/journey/confirm", { method: "POST", body: JSON.stringify({ step, ...(note ? { note } : {}) }) });
  },
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
  getProjectRequirements: () => request<ProjectRequirements>("/api/transform/project"),
  saveProjectRequirements: (text) =>
    request<ProjectRequirements>("/api/transform/project", { method: "POST", body: JSON.stringify({ text }) }),
  listSqlPools: async (sub, rg, ws) =>
    (await request<{ items: string[] }>(
      `/api/azure/sql-pools?subscriptionId=${encodeURIComponent(sub)}&resourceGroup=${encodeURIComponent(rg)}&workspace=${encodeURIComponent(ws)}`,
    )).items,
  getConsult: () => request<ConsultState>("/api/consult"),
  getConsultCatalog: () => request<ConsultCatalog>("/api/consult/catalog"),
  answerQuestion: (question, value, extra) =>
    request<ConsultState>("/api/consult/answer", { method: "POST", body: JSON.stringify({ question, value, ...(extra?.note ? { note: extra.note } : {}),
      ...(extra?.seq !== undefined ? { seq: extra.seq } : {}) }) }),
  previewAnswer: (question, value) =>
    request<AnswerPreview>("/api/consult/answer", { method: "POST", body: JSON.stringify({ question, value, preview: true }) }),
  skipQuestion: (question) => request<ConsultState>("/api/consult/skip", { method: "POST", body: JSON.stringify({ question }) }),
  withdrawAnswer: (question) => request<ConsultState>("/api/consult/withdraw", { method: "POST", body: JSON.stringify({ question }) }),
  acceptRecommendations: (section) =>
    request<ConsultState>("/api/consult/accept", { method: "POST", body: JSON.stringify(section ? { section } : {}) }),
  resolveConflict: (conflict, resolution, reason) =>
    request<ConsultState>("/api/consult/resolve", { method: "POST", body: JSON.stringify({ conflict, resolution, ...(reason ? { reason } : {}) }) }),
  getBrief: () => request<BriefView>("/api/consult/brief"),
  submitBrief: (note) => request<BriefView>("/api/consult/brief/submit", { method: "POST", body: JSON.stringify(note ? { note } : {}) }),
  decideBrief: (version, decision, comment, hash) =>
    request<BriefView>("/api/consult/brief/approve", { method: "POST", body: JSON.stringify({ version, decision, ...(comment ? { comment } : {}),
      ...(hash ? { hash } : {}) }) }),
  exportBrief: async (version, format = "md") => ({
    url: `/api/consult/brief/export?format=${format}${version !== undefined ? `&version=${version}` : ""}`,
    fileName: `migration-brief${version !== undefined ? `-v${version}` : ""}.${format}`,
  }),
  getConsultPolicy: () => request<ConsultPolicy>("/api/consult/policy"),
  recordDeviation: (items, options, reason) =>
    request<{ differences: PlanDifference[]; recorded: boolean }>("/api/consult/deviation", { method: "POST", body: JSON.stringify({ items, options, reason }) }),
  proposeAnswers: (text, useAi = true) =>
    request<ConsultState>("/api/consult/propose", { method: "POST", body: JSON.stringify({ text, useAi }) }),
  decideProposals: (ids, decision) =>
    request<ConsultState>("/api/consult/proposals", { method: "POST", body: JSON.stringify({ ids, decision }) }),
  explainQuestion: (question, useAi = true) =>
    request<ConsultExplanation>("/api/consult/explain", { method: "POST", body: JSON.stringify({ question, useAi }) }),
  exportQuestionnaire: async (format) => ({ url: `/api/consult/questionnaire?format=${format}`, fileName: `migration-questionnaire.${format}` }),
  importQuestionnaire: (answers) =>
    request<ConsultState>("/api/consult/questionnaire", { method: "POST", body: JSON.stringify({ answers }) }),
  getWorkspaceGit: (sub, rg, ws) =>
    request<WorkspaceGit>(
      `/api/azure/workspace-git?subscriptionId=${encodeURIComponent(sub)}&resourceGroup=${encodeURIComponent(rg)}&workspace=${encodeURIComponent(ws)}`,
    ),
};

