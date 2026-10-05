// The DEMO implementation of MigrationApi. It simulates latency, progress and
// a handful of failure modes, keyed off the workspace name so each state in the
// UI can be exercised on purpose:
//
//   *denied*   -> insufficient permissions      *notfound* -> workspace not found
//   *expired*  -> authentication expired        *offline*  -> network failure
//   *empty*    -> discovery finds nothing       *partial*  -> discovery with warnings
//   *fail*     -> discovery fails               *timeout*  -> discovery times out
//
// Anything else connects and discovers successfully. All output is demo data.

import {
  ApiRequestError,
  type Capabilities,
  type ConnectionConfig,
  type ConnectionCredentials,
  type ConnectionState,
  type DependencyGraph,
  type DiscoveryStatus,
  type ExecItem,
  type ExecutionRun,
  type FabricConfig,
  type FabricTarget,
  type PlanAnalysis,
  type PlanItem,
  type PlanRisk,
  type PlannerRun,
  type RiskSeverity,
  type RunOptions,
  type Strategy,
  type TypeStrategy,
  type ValidationRow,
  type DiscoverySummary,
  type MigrationApi,
  type ObjectDetail,
  type ObjectRow,
  type ProgressStep,
  type ResultsPage,
  type ResultsQuery,
} from "../types";
import { generateObjects } from "./mockData";
import { mockComponents } from "./mockMapping";

const STEP_MS = 700;
/** What a migration run moves in this build; mirrors the backend's list. */
const MIGRATABLE_TYPES = ["Dedicated SQL Pool", "Schema", "Table", "View", "Stored Procedure", "Notebook", "Spark Pool", "Linked Service", "Pipeline", "Dataset", "Spark Job Definition", "SQL Script", "Trigger", "External Table"];
const STEPS = [
  "Discovering SQL objects",
  "Discovering pipelines",
  "Discovering datasets and linked services",
  "Discovering Spark objects",
  "Assembling records",
];

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

interface Session {
  connection: ConnectionState;
  scenario: string;
  startedAt: number | null;
  startedIso: string | null;
  objects: ObjectDetail[] | null;
  summary: DiscoverySummary | null;
  error: string | null;
  signedIn: { subscriptionId: string; tenantId: string } | null;
}

// Demo-only simulation state for the later phases (no backend exists for them).
let fabric: FabricTarget = { status: "disconnected" };
interface ExecSim { runId: string; items: PlanItem[]; base: number; resumedAt: number | null; retried: boolean }
let execSim: ExecSim | null = null;
const hash = (text: string) => [...text].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7);

const fresh = (): Session => ({
  connection: { status: "disconnected", ok: true, checks: [] },
  scenario: "",
  startedAt: null,
  startedIso: null,
  objects: null,
  summary: null,
  error: null,
  signedIn: null,
});
let s = fresh();

function connectionFor(c: ConnectionConfig, connected: boolean, error?: ConnectionState["error"]): ConnectionState {
  const ws = c.workspace.trim();
  return {
    status: connected ? "connected" : "not_connected",
    ok: !error,
    method: c.method,
    sourcePlatform: "Azure Synapse",
    workspace: ws,
    resourceGroup: c.resourceGroup.trim(),
    subscriptionId: c.subscriptionId.trim(),
    subscriptionName: "Demo subscription",
    tenantId: c.tenantId.trim() || "00000000-0000-0000-0000-000000000000",
    sqlPool: c.sqlPool.trim() || null,
    testedAt: connected ? new Date().toISOString() : null,
    checks: error
      ? [{ name: "Synapse workspace", status: "failed", message: error.message, category: error.code }]
      : [
          { name: "Azure authentication", status: "ok", message: "Authenticated (demo).", category: null },
          { name: "Synapse workspace", status: "ok", message: "Workspace readable (demo).", category: null },
          { name: "Workspace artifacts", status: "ok", message: "Artifacts readable (demo).", category: null },
        ],
    error,
  };
}

function scenarioError(ws: string): ConnectionState["error"] | null {
  const w = ws.toLowerCase();
  if (w.includes("offline")) throw new ApiRequestError("network", "Azure could not be reached (demo).");
  if (w.includes("expired"))
    return { code: "authentication", title: "Authentication expired", hint: "The sign-in has expired. Authenticate again.", message: "The demo session token has expired." };
  if (w.includes("denied"))
    return { code: "authorization", title: "Insufficient permissions", hint: "The authenticated identity does not have access to the selected workspace.", message: "Demo: 403 from the workspace." };
  if (w.includes("notfound"))
    return { code: "not_found", title: "Workspace not found", hint: "Check the resource group and workspace names.", message: "Demo: workspace does not exist." };
  return null;
}

function requireFields(c: ConnectionConfig) {
  const missing = [
    !c.subscriptionId.trim() && "Subscription ID",
    !c.resourceGroup.trim() && "Resource group",
    !c.workspace.trim() && "Synapse workspace",
  ].filter(Boolean);
  if (missing.length) throw new ApiRequestError("invalid_configuration", `${missing.join(", ")} required.`);
}

function statusNow(): DiscoveryStatus {
  if (s.startedAt === null) {
    return { state: "idle", startedAt: null, finishedAt: null, error: null, workspace: null, progress: null, summary: null };
  }
  const elapsed = Date.now() - s.startedAt;
  const done = Math.min(STEPS.length, Math.floor(elapsed / STEP_MS));
  const workspace = s.connection.workspace ?? null;
  const running = done < STEPS.length;
  const progress: ProgressStep[] = STEPS.map((label, i) => ({
    label,
    state: i < done ? "done" : i === done && running ? "active" : "pending",
  }));
  if (running) {
    return { state: "running", startedAt: s.startedIso, finishedAt: null, error: null, workspace, progress, summary: null };
  }
  if (s.objects === null) buildResult();
  const finishedAt = new Date(s.startedAt + STEPS.length * STEP_MS).toISOString();
  if (s.error) {
    return { state: "failed", startedAt: s.startedIso, finishedAt, error: s.error, workspace, progress, summary: null };
  }
  const warn = !!s.summary && (s.summary.failedCategories.length > 0 || s.summary.warningCount > 0);
  return { state: warn ? "completed_with_warnings" : "completed", startedAt: s.startedIso, finishedAt, error: null, workspace, progress, summary: s.summary };
}

function buildResult() {
  const w = s.scenario.toLowerCase();
  if (w.includes("fail")) {
    s.objects = [];
    s.error = "The workspace returned an unexpected response while listing artifacts (demo).";
    return;
  }
  if (w.includes("timeout")) {
    s.objects = [];
    s.error = "Discovery timed out before the workspace finished responding (demo).";
    return;
  }
  const partial = w.includes("partial");
  const objects = w.includes("empty")
    ? []
    : generateObjects({ workspace: s.connection.workspace ?? "demo", omitCategories: partial ? ["Spark"] : [] });
  s.objects = objects;
  const tally = (pick: (o: ObjectDetail) => string) => {
    const out: Record<string, number> = {};
    for (const o of objects) out[pick(o)] = (out[pick(o)] ?? 0) + 1;
    return out;
  };
  s.summary = {
    total: objects.length,
    byCategory: tally((o) => o.category),
    byType: tally((o) => o.type),
    byWorkstream: tally((o) => o.workstream),
    byClassification: tally((o) => o.classification),
    byPath: tally((o) => o.migrationPath),
    byFabricTarget: tally((o) => o.fabricTarget),
    withFabricMapping: objects.filter((o) => !["Requires Assessment", "Manual / Special Handling"].includes(o.migrationPath)).length,
    requiringAssessment: objects.filter((o) => o.assessmentRequired).length,
    manualOrAssessment: objects.filter((o) => ["Requires Assessment", "Manual / Special Handling"].includes(o.migrationPath)).length,
    failedCategories: partial
      ? [{ name: "Spark (Notebooks, Spark Job Definitions)", reason: "The workspace endpoint could not be read (demo: 403). This is not a count of zero." }]
      : [],
    warnings: partial ? ["Some notebooks could not be listed (demo)."] : [],
    warningCount: partial ? 1 : 0,
    coverage: {
      discoveredTypes: Object.keys(tally((o) => o.type)).sort(),
      notDiscovered: [
        { type: "Function", reason: "the SQL catalog queries cover tables, views and stored procedures only" },
        { type: "Lake Database", reason: "lake databases are not yet listed" },
        { type: "Security Object", reason: "users, roles and permissions are not yet read" },
      ],
    },
  };
}

function toRow(o: ObjectDetail): ObjectRow {
  const { id, name, type, category, workspace, status, dependencyCount, sources, fabricTarget, targetType, migrationPath, automationPotential, assessmentRequired, mappingStatus, workstream, classification, action, schema, size, component, wave } = o;
  return { id, name, type, category, workspace, status, dependencyCount, sources, fabricTarget, targetType, migrationPath, automationPotential, assessmentRequired, mappingStatus, workstream, classification, action, schema, size, component, wave };
}

function completedObjects(): ObjectDetail[] {
  const status = statusNow();
  if (!s.objects || (status.state !== "completed" && status.state !== "completed_with_warnings"))
    throw new ApiRequestError("no_results", "There are no discovery results yet.", 409);
  return s.objects;
}

function runNow(): ExecutionRun {
  if (!execSim) return { runId: "", state: "idle", total: 0, completed: 0, inProgress: 0, failed: 0, pending: 0, skipped: 0, deferred: 0, items: [], logs: [] };
  const sim = execSim;
  const objects = new Map((s.objects ?? []).map((o) => [o.id, o]));
  const elapsed = sim.base + (sim.resumedAt === null ? 0 : Date.now() - sim.resumedAt);
  const PER = 350, DUR = 600;
  const started = Date.now() - elapsed;
  const logs: string[] = [];
  let slot = 0;
  const items: ExecItem[] = sim.items.map((pi) => {
    const o = objects.get(pi.id);
    // Like the real backend: only these types move in this session.
    if (!MIGRATABLE_TYPES.includes(o?.type ?? "")) {
      return { id: pi.id, name: o?.name ?? pi.id, type: o?.type ?? "", wave: pi.wave, step: "Not migrated in this session", status: "DEFERRED", startedAt: null, completedAt: null, error: `${o?.type ?? "This type"} objects move in a later session (demo).` };
    }
    const begin = slot * PER, end = begin + DUR;
    slot += 1;
    const failing = !sim.retried && hash(pi.id) % 11 === 0;
    const base: ExecItem = { id: pi.id, name: o?.name ?? pi.id, type: o?.type ?? "", wave: pi.wave, step: "Waiting", status: "PENDING", startedAt: null, completedAt: null, error: null };
    if (elapsed < begin) return base;
    const at = (ms: number) => new Date(started + ms).toISOString();
    if (elapsed < end) {
      logs.push(`${at(begin)}  START   ${base.name}`);
      return { ...base, step: "Creating in Fabric", status: "IN PROGRESS", startedAt: at(begin) };
    }
    if (failing) {
      logs.push(`${at(begin)}  START   ${base.name}`, `${at(end)}  FAILED  ${base.name}: demo error (simulated)`);
      return { ...base, step: "Create in Fabric", status: "FAILED", startedAt: at(begin), completedAt: at(end), error: "Simulated failure (demo)" };
    }
    logs.push(`${at(begin)}  START   ${base.name}`, `${at(end)}  DONE    ${base.name}`);
    return { ...base, step: "Validated", status: "COMPLETED", startedAt: at(begin), completedAt: at(end) };
  });
  const n = (st: ExecItem["status"]) => items.filter((i) => i.status === st).length;
  const allDone = items.every((i) => i.status === "COMPLETED" || i.status === "FAILED" || i.status === "DEFERRED");
  return {
    runId: sim.runId,
    state: allDone ? "completed" : sim.resumedAt === null ? "paused" : "running",
    total: items.length, completed: n("COMPLETED"), inProgress: n("IN PROGRESS"), failed: n("FAILED"), pending: n("PENDING"),
    skipped: n("SKIPPED"), deferred: n("DEFERRED"), workspace: fabric.workspaceName ?? null, warehouse: "demo_pool",
    items, logs,
  };
}

let plannerHistory: PlannerRun[] = [];

/** A small stand-in for the backend planner so the demo page is fully populated. */
function demoAnalysis(items: PlanItem[], record: boolean, options?: RunOptions): PlanAnalysis {
  const byId = new Map(completedObjects().map((o) => [o.id, o]));
  const rows = items.flatMap((pi) => { const o = byId.get(pi.id); return o ? [{ o, wave: pi.wave }] : []; });
  const on = (type: string) => !options?.stages?.length || options.stages.includes(stageOfType(type) ?? "");
  const strategyOf = (type: string, cls: string): Strategy =>
    MIGRATABLE_TYPES.includes(type) ? (on(type) ? "automated" : "deselected") : cls === "MANUAL" ? "manual" : cls === "REVIEW" ? "assess" : "later";
  const objectStrategies: PlanAnalysis["objectStrategies"] = {};
  const label: Record<Strategy, string> = { automated: "Automated", manual: "Manual setup", assess: "Assess first", later: "Later session", deselected: "Not selected" };
  const counts: Record<Strategy, number> = { automated: 0, manual: 0, assess: 0, later: 0, deselected: 0 };
  let hours = 0, reviewHours = 0;
  for (const { o, wave } of rows) {
    const st = strategyOf(o.type, o.classification);
    const h = st === "deselected" ? 0 : st === "automated" ? 0.5 : st === "manual" ? 3 : 2;
    objectStrategies[o.id] = { strategy: st, label: label[st], effortHours: h };
    counts[st] += 1; hours += h;
    if (st === "manual" || st === "assess") reviewHours += h;
    void wave;
  }
  const risks: PlanRisk[] = [];
  if (fabric.status !== "connected") risks.push({ id: "", code: "TARGET_NOT_READY", severity: "BLOCKING", title: "Fabric target connected", message: "Connect the Fabric target and pass its connection test.", objects: [] });
  const later = rows.filter(({ o }) => !["automated", "deselected"].includes(objectStrategies[o.id].strategy));
  if (later.length) risks.push({ id: "", code: "NOT_AUTOMATED", severity: "LOW", title: "Not migrated by this build", message: `${later.length} object(s) are in the plan but not created by this run (demo).`, objects: later.map(({ o }) => o.id) });
  risks.forEach((r, i) => { r.id = `R${String(i + 1).padStart(3, "0")}`; });
  const riskCounts = { BLOCKING: 0, HIGH: 0, MEDIUM: 0, LOW: 0 } as Record<RiskSeverity, number>;
  for (const r of risks) riskCounts[r.severity] += 1;
  const readiness = Math.max(0, Math.round(100 - riskCounts.BLOCKING * 25 - riskCounts.HIGH * 6 - riskCounts.MEDIUM * 2 - riskCounts.LOW * 0.5));
  const waveNumbers = [...new Set(rows.map((r) => r.wave))].sort((a, b) => a - b);
  const waves = waveNumbers.map((w) => {
    const m = rows.filter((r) => r.wave === w);
    const types: Record<string, number> = {};
    for (const { o } of m) types[o.type] = (types[o.type] ?? 0) + 1;
    return { wave: w, count: m.length, automated: m.filter(({ o }) => objectStrategies[o.id].strategy === "automated").length, types, effortDays: +(m.reduce((a, { o }) => a + objectStrategies[o.id].effortHours, 0) / 8).toFixed(2) };
  });
  const types = [...new Set(rows.map((r) => r.o.type))];
  const typeStrategies: TypeStrategy[] = types.map((t) => {
    const m = rows.filter((r) => r.o.type === t);
    const st = objectStrategies[m[0].o.id].strategy;
    return { type: t, count: m.length, strategy: st, strategyLabel: label[st], target: m[0].o.fabricTarget, firstWave: Math.min(...m.map((r) => r.wave)), lastWave: Math.max(...m.map((r) => r.wave)), effortDays: +(m.reduce((a, { o }) => a + objectStrategies[o.id].effortHours, 0) / 8).toFixed(2) };
  }).sort((a, b) => a.firstWave - b.firstWave || a.type.localeCompare(b.type));
  const fingerprint = (hash(items.map((i) => `${i.id}@${i.wave}`).sort().join("|")) >>> 0).toString(16).padStart(8, "0").padEnd(12, "0");
  const effortDays = +(hours / 8).toFixed(2);
  if (record) {
    plannerHistory = [{ id: `P${String(plannerHistory.length + 1).padStart(3, "0")}`, plannerVersion: "1.0.0", fingerprint, objects: rows.length, effortDays, waves: waves.length, readiness, blocking: riskCounts.BLOCKING, createdAt: new Date().toISOString(), status: "COMPLETED" }, ...plannerHistory].slice(0, 20);
  }
  return {
    plannerVersion: "1.0.0", fingerprint, readiness, objects: rows.length, strategyCounts: counts, effortDays,
    needsReview: { count: counts.manual + counts.assess, effortDays: +(reviewHours / 8).toFixed(2) }, blocking: riskCounts.BLOCKING,
    waves, typeStrategies, risks, riskCounts,
    checks: [
      { label: "Fabric target connected", status: fabric.status === "connected" ? "ok" : "fail", detail: fabric.status === "connected" ? "Demo workspace" : "Connect the Fabric target first." },
      { label: "SQL driver for the Warehouse", status: "ok", detail: "Demo" },
    ],
    objectStrategies, history: plannerHistory,
  };
}

const DEMO_STAGES: Capabilities["stages"] = [
  { key: "warehouse", label: "Warehouse & schema", summary: "The SQL pool becomes a Fabric Warehouse; its schemas, tables (empty), views and stored procedures are created in it.", types: ["Dedicated SQL Pool", "Schema", "Table", "View", "Stored Procedure"], needs: [], needsInput: null, creates: "Warehouse, schemas, tables, views, procedures", options: [] },
  { key: "data", label: "Table data", summary: "Copies the rows of each migrated table from the Synapse pool into the Warehouse, then checks the row counts match.", types: ["Table"], needs: ["warehouse"], needsInput: null, creates: "Rows in the Warehouse tables", options: [{ key: "dataMode", label: "If a table already has rows", default: "if_empty", choices: [{ value: "if_empty", description: "Skip it. Never touches data that is already there (safe re-runs)." }, { value: "replace", description: "Replace it. Clears the table, then loads it again." }] }] },
  { key: "spark", label: "Spark pool & environment", summary: "A Spark pool becomes a custom Fabric Spark pool plus a published Environment.", types: ["Spark Pool"], needs: [], needsInput: null, creates: "Spark pool, Environment", options: [] },
  { key: "notebooks", label: "Notebooks", summary: "Synapse notebooks become Fabric notebooks.", types: ["Notebook"], needs: [], needsInput: null, creates: "Notebooks", options: [] },
  { key: "connections", label: "Connections", summary: "Linked services become Fabric connections. Fabric needs their credentials, which Synapse does not give up.", types: ["Linked Service"], needs: [], needsInput: "credentials", creates: "Fabric connections", options: [] },
  { key: "pipelines", label: "Pipelines & datasets", summary: "Pipelines become Fabric data pipelines. Datasets are folded into the pipelines that use them.", types: ["Pipeline", "Dataset"], needs: ["connections", "notebooks", "warehouse"], needsInput: null, creates: "Data pipelines", options: [] },
  { key: "spark_jobs", label: "Spark job definitions", summary: "Spark job definitions are recreated as Fabric Spark job definitions.", types: ["Spark Job Definition"], needs: [], needsInput: null, creates: "Spark job definitions", options: [] },
  { key: "sql_scripts", label: "SQL scripts", summary: "Each SQL script becomes a notebook with a T-SQL cell, bound to the Warehouse. Review them before running.", types: ["SQL Script"], needs: ["warehouse"], needsInput: null, creates: "Notebooks (T-SQL)", options: [] },
  { key: "schedules", label: "Schedules", summary: "Triggers become pipeline schedules, created switched off so nothing runs before you are ready.", types: ["Trigger"], needs: ["pipelines"], needsInput: null, creates: "Pipeline schedules", options: [] },
  { key: "shortcuts", label: "External tables", summary: "External tables become OneLake shortcuts in a Lakehouse, pointing at the same storage.", types: ["External Table"], needs: ["connections"], needsInput: null, creates: "Lakehouse, shortcuts", options: [] },
];
const stageOfType = (type: string) => DEMO_STAGES.find((st) => st.types.includes(type))?.key;

export const mockApi: MigrationApi = {
  mode: "mock",

  async health() {
    await sleep(80);
    return {
      status: "ok",
      capabilities: {
        authMethods: ["azure_cli", "interactive_browser"],
        authMethodDetails: [
          { id: "azure_cli", label: "Azure CLI", detail: "Opens a sign-in window each time; nothing is kept between sign-ins." },
          {
            id: "interactive_browser", label: "Interactive browser",
            detail: "Opens a sign-in window against the tenant you name, and leaves your Azure CLI session untouched.",
            bestFor: "A tenant your `az login` cannot reach.",
            caveat: "The window opens on the machine running this server.",
          },
        ],
        discoveryScope: ["Tables", "Views", "Stored Procedures", "SQL Scripts", "Pipelines", "Datasets", "Linked Services", "Notebooks", "Spark Job Definitions"],
        migratableTypes: [...MIGRATABLE_TYPES],
      },
    };
  },

  async getConnection() {
    return s.connection;
  },

  async authenticate(c) {
    // Stands in for the browser sign-in window.
    await sleep(c.method === "azure_cli" ? 1500 : 800);
    if (!c.subscriptionId.trim()) throw new ApiRequestError("invalid_configuration", "Subscription ID is required.");
    if (c.method === "interactive_browser" && !c.tenantId.trim()) throw new ApiRequestError("invalid_configuration", "Tenant ID is required.");
    s.signedIn = { subscriptionId: c.subscriptionId.trim(), tenantId: c.tenantId.trim() || "00000000-0000-0000-0000-000000000000" };
    s.connection = { status: "disconnected", ok: true, signedIn: true, method: c.method, subscriptionId: s.signedIn.subscriptionId, subscriptionName: "Demo subscription", tenantId: s.signedIn.tenantId, checks: [] };
    return s.connection;
  },

  async listResourceGroups() {
    await sleep(300);
    if (!s.signedIn) throw new ApiRequestError("sign_in_required", "Sign in to Azure first.", 409);
    return ["rg-demo-analytics", "rg-demo-migration", "TransportationMigrationRG"];
  },

  async listWorkspaces(rg) {
    await sleep(300);
    const byGroup: Record<string, string[]> = {
      "rg-demo-analytics": ["analytics-synapse-prod"],
      // Names double as demo scenarios: see the header of this file.
      "rg-demo-migration": ["demo-synapse-ws", "partial-ws", "empty-ws", "fail-ws", "denied-ws"],
      TransportationMigrationRG: ["transportationsynapsemigration"],
    };
    return byGroup[rg] ?? [];
  },

  async listSqlPools(_rg, ws) {
    await sleep(250);
    return ws.includes("empty") ? [] : ["poolone"];
  },
  async testConnection(c) {
    await sleep(700);
    requireFields(c);
    const error = scenarioError(c.workspace);
    s.connection = { ...connectionFor(c, !error, error ?? undefined), signedIn: !!s.signedIn };
    s.scenario = c.workspace;
    if (error) {
      s.startedAt = null;
      s.objects = null;
    }
    return s.connection;
  },

  async disconnect() {
    await sleep(150);
    execSim = null;
    s = fresh();
    return s.connection;
  },

  async startDiscovery() {
    if (s.connection.status !== "connected")
      throw new ApiRequestError("not_connected", "Connect to a Synapse workspace and pass the connection test first.", 409);
    s.startedAt = Date.now();
    s.startedIso = new Date().toISOString();
    s.objects = null;
    s.summary = null;
    s.error = null;
    return statusNow();
  },

  async getDiscoveryStatus() {
    return statusNow();
  },

  async getResults(q: ResultsQuery): Promise<ResultsPage> {
    await sleep(120);
    const status = statusNow();
    if (!s.objects || (status.state !== "completed" && status.state !== "completed_with_warnings"))
      throw new ApiRequestError("no_results", "There are no discovery results yet.", 409);
    const all = s.objects;
    const term = q.search.trim().toLowerCase();
    const has = (list: string[], v: string) => !list.length || list.includes(v);
    const rows = all
      .filter(
        (o) =>
          has(q.categories, o.category) && has(q.types, o.type) && has(q.statuses, o.status) &&
          has(q.fabricTargets, o.fabricTarget) && has(q.paths, o.migrationPath) &&
          has(q.workstreams, o.workstream) && has(q.mappingStatuses, o.mappingStatus) && has(q.classifications, o.classification) &&
          (q.assessment === "" || (q.assessment === "yes") === o.assessmentRequired) &&
          (!term || [o.name, o.type, o.fabricTarget, o.migrationPath].some((v) => v.toLowerCase().includes(term))),
      )
      .map(toRow);
    const pick: Record<string, (r: ObjectRow) => string | number> = {
      name: (r) => r.name.toLowerCase(), type: (r) => r.type, category: (r) => r.category, status: (r) => r.status,
      dependencies: (r) => r.dependencyCount, workspace: (r) => r.workspace, target: (r) => r.fabricTarget,
      targetType: (r) => r.targetType, path: (r) => r.migrationPath, automation: (r) => r.automationPotential,
      assessment: (r) => String(r.assessmentRequired), mappingStatus: (r) => r.mappingStatus,
      classification: (r) => r.classification, wave: (r) => r.wave,
    };
    const key = pick[q.sort] ?? pick.name;
    rows.sort((a, b) => {
      const x = key(a), y = key(b);
      const c = x < y ? -1 : x > y ? 1 : a.name.localeCompare(b.name);
      return q.dir === "desc" ? -c : c;
    });
    const facet = (f: (o: ObjectDetail) => string) => {
      const out: Record<string, number> = {};
      for (const o of all) out[f(o)] = (out[f(o)] ?? 0) + 1;
      return out;
    };
    const start = (q.page - 1) * q.pageSize;
    return {
      items: rows.slice(start, start + q.pageSize),
      total: rows.length,
      page: q.page,
      pageSize: q.pageSize,
      facets: {
        categories: facet((o) => o.category), types: facet((o) => o.type), statuses: facet((o) => o.status),
        fabricTargets: facet((o) => o.fabricTarget), paths: facet((o) => o.migrationPath),
        workstreams: facet((o) => o.workstream), mappingStatuses: facet((o) => o.mappingStatus),
        classifications: facet((o) => o.classification),
        assessment: { Yes: all.filter((o) => o.assessmentRequired).length, No: all.filter((o) => !o.assessmentRequired).length },
      },
      discoveredAt: status.finishedAt,
    };
  },

  async getDependencies(): Promise<DependencyGraph> {
    await sleep(150);
    const objects = completedObjects();
    const ids = new Set(objects.map((o) => o.id));
    const edges: { source: string; target: string }[] = [];
    const out: Record<string, number> = {}, inn: Record<string, number> = {};
    for (const o of objects) for (const d of o.dependencies) {
      if (d.objectId && ids.has(d.objectId) && d.location !== "contains") {
        edges.push({ source: o.id, target: d.objectId });
        out[o.id] = (out[o.id] ?? 0) + 1;
        inn[d.objectId] = (inn[d.objectId] ?? 0) + 1;
      }
    }
    const waves = new Map<number, { count: number; types: Record<string, number> }>();
    for (const o of objects) {
      const w = waves.get(o.wave) ?? { count: 0, types: {} };
      w.count++;
      w.types[o.type] = (w.types[o.type] ?? 0) + 1;
      waves.set(o.wave, w);
    }
    return {
      nodes: objects.map((o) => ({ id: o.id, name: o.name, type: o.type, category: o.category, classification: o.classification, fabricTarget: o.fabricTarget, wave: o.wave, dependsOn: out[o.id] ?? 0, dependedOnBy: inn[o.id] ?? 0 })),
      edges,
      waves: [...waves.entries()].sort((a, b) => a[0] - b[0]).map(([wave, v]) => ({ wave, ...v })),
      discoveredAt: statusNow().finishedAt,
    };
  },

  async getComponents() {
    await sleep(80);
    return mockComponents();
  },

  async exportMetadata() {
    await sleep(150);
    const objects = completedObjects();
    const names = new Map(objects.map((o) => [o.id, o.name]));
    return {
      platform: "Azure Synapse",
      workspace: s.connection.workspace ?? null,
      discoveredAt: statusNow().finishedAt,
      summary: s.summary,
      objects: objects.map((o) => ({ ...toRow(o), dependsOn: o.dependencies.filter((d) => d.objectId).map((d) => names.get(d.objectId as string) ?? d.name) })),
    };
  },

  async getFabricTarget() {
    return fabric;
  },

  async authenticateFabric(c: FabricConfig) {
    await sleep(900);
    fabric = {
      status: "authenticated", method: c.method, account: "demo.user@contoso.com", tenantId: "00000000-0000-0000-0000-000000000000",
      workspaces: [{ id: "3f2a9c1e-7b64-4d0a-9a55-1c2e8b7d4f10", name: "Fabric_practice" }, { id: "9b1d5e22-0c3a-4f7e-8d11-6a4c2e9f0b33", name: "My workspace" }],
      message: "Authenticated (demo).",
    };
    return fabric;
  },

  async testFabric(c: FabricConfig) {
    await sleep(800);
    if (!c.workspaceId.trim() && !c.workspaceName.trim()) throw new ApiRequestError("invalid_configuration", "Select a Fabric workspace first.");
    if ((c.workspaceName + c.workspaceId).toLowerCase().includes("denied")) {
      fabric = { ...fabric, status: "failed", message: "The identity has no access to this Fabric workspace (demo)." };
      return fabric;
    }
    fabric = {
      ...fabric, status: "connected", method: c.method,
      workspaceName: c.workspaceName.trim() || "Fabric_practice",
      workspaceId: c.workspaceId.trim() || "3f2a9c1e-7b64-4d0a-9a55-1c2e8b7d4f10", message: null,
      checks: [
        { label: c.method === "azure_cli" ? "Azure CLI authenticated" : "Fabric CLI authenticated", ok: true },
        { label: c.method === "azure_cli" ? "Fabric API accessible" : "Fabric workspace discovered", ok: true },
        { label: "Workspace accessible", ok: true },
      ],
    };
    return fabric;
  },

  async disconnectFabric() {
    fabric = { status: "disconnected" };
    return fabric;
  },

  async getCapabilities() {
    await sleep(60);
    const linked = (s.objects ?? []).filter((o) => o.type === "Linked Service").slice(0, 3).map((o) => ({
      name: o.name, type: "AzureSqlDW", fabricType: "SQL", needsPath: false, unsupported: null,
      authTypes: [
        { value: "basic", label: "SQL login", fields: ["username", "password"] },
        { value: "servicePrincipal", label: "Service principal", fields: ["tenantId", "clientId", "clientSecret"] },
      ],
    }));
    return { stages: DEMO_STAGES, defaults: { dataMode: "if_empty" }, linkedServices: linked, maxRowsDefault: 1_000_000, maxRowsCeiling: 10_000_000 };
  },

  async analyzePlan(items: PlanItem[], record?: boolean, options?: RunOptions, credentials?: ConnectionCredentials) {
    await sleep(120);
    void credentials;
    return demoAnalysis(items, !!record, options);
  },

  async startExecution(items: PlanItem[], options?: RunOptions, credentials?: ConnectionCredentials) {
    void credentials;
    await sleep(200);
    if (fabric.status !== "connected") throw new ApiRequestError("target_not_connected", "Connect the Fabric target first.", 409);
    if (!items.length) throw new ApiRequestError("empty_plan", "The migration plan is empty.", 400);
    const typeOf = (id: string) => (s.objects ?? []).find((o) => o.id === id)?.type ?? "";
    const staged = (i: PlanItem) => !options?.stages?.length || options.stages.includes(stageOfType(typeOf(i.id)) ?? "");
    const kept = options?.scope === "automated" ? items.filter((i) => MIGRATABLE_TYPES.includes(typeOf(i.id)) && staged(i)) : items.filter(staged);
    if (!kept.length) throw new ApiRequestError("nothing_to_migrate", "No object in the plan can be created by this build.", 400);
    execSim = { runId: "001", items: kept, base: 0, resumedAt: Date.now(), retried: false };
    return runNow();
  },

  async getExecution() {
    return runNow();
  },

  async controlExecution(action: "pause" | "resume" | "retry") {
    if (!execSim) throw new ApiRequestError("no_run", "There is no migration run.", 409);
    const now = Date.now();
    if (action === "pause" && execSim.resumedAt !== null) {
      execSim.base += now - execSim.resumedAt;
      execSim.resumedAt = null;
    } else if (action === "resume" && execSim.resumedAt === null) {
      execSim.resumedAt = now;
    } else if (action === "retry") {
      execSim.retried = true;
    }
    return runNow();
  },

  async runValidation(): Promise<ValidationRow[]> {
    await sleep(500);
    const run = runNow();
    const done = run.items.filter((i) => i.status === "COMPLETED");
    if (!done.length) throw new ApiRequestError("nothing_to_validate", "No object has been migrated yet.", 409);
    const rows: ValidationRow[] = [];
    for (const i of done) {
      const h = hash(i.id);
      if (i.type === "Table" || i.type === "External Table") {
        const n = 1000 + (h % 900000);
        const bad = h % 13 === 0;
        rows.push({ category: "Data Count", object: i.name, source: `${n.toLocaleString()} rows`, target: `${(bad ? n - 12 : n).toLocaleString()} rows`, status: bad ? "MISMATCH" : "MATCH" });
        rows.push({ category: "Schema", object: i.name, source: "columns", target: "columns", status: "MATCH" });
      } else if (i.type === "Pipeline") {
        rows.push({ category: "Pipelines", object: i.name, source: "1 pipeline", target: "1 pipeline", status: h % 3 === 0 ? "REVIEW" : "MATCH" });
      } else if (i.type === "Notebook") {
        rows.push({ category: "Notebooks", object: i.name, source: "1 notebook", target: "1 notebook", status: "MATCH" });
      } else if (i.type === "View" || i.type === "Stored Procedure") {
        rows.push({ category: i.type === "View" ? "Views" : "Stored Procedures", object: i.name, source: "1 definition", target: "1 definition", status: h % 5 === 0 ? "REVIEW" : "MATCH" });
      } else {
        rows.push({ category: "Dependencies", object: i.name, source: "present", target: "present", status: "MATCH" });
      }
    }
    return rows;
  },

  async getObject(id) {
    await sleep(120);
    const found = s.objects?.find((o) => o.id === id);
    if (!found) throw new ApiRequestError("object_not_found", "That object is not in the current discovery results.", 404);
    return found;
  },
};

