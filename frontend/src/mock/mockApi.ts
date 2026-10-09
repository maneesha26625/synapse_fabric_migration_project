// The DEMO implementation of MigrationApi: the whole application without Azure.
//
// It starts ready to use: a demo Synapse workspace (demo-synapse-ws, pool
// TransportDW) is connected and already discovered, and a demo Fabric
// workspace is connected, so every page works without signing in anywhere.
// Disconnect either side to walk through the connection screens; reloading the
// page restores the ready demo. Latency, progress and failures are simulated,
// and failure modes are keyed off the workspace name you pick when connecting:
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
  type DataFilterInput,
  type RepositoryConfig,
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
  type RunControl,
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

const DEMO_TENANT = "00000000-0000-0000-0000-000000000000";
const DEMO_SOURCE: ConnectionConfig = {
  method: "azure_cli", tenantId: DEMO_TENANT, subscriptionId: "11111111-2222-3333-4444-555555555555",
  resourceGroup: "rg-demo-migration", workspace: "demo-synapse-ws", workspaceUrl: "", sqlPool: "TransportDW", resource: "",
};
const DEMO_FABRIC_WORKSPACES = [{ id: "3f2a9c1e-7b64-4d0a-9a55-1c2e8b7d4f10", name: "Fabric_demo" }, { id: "9b1d5e22-0c3a-4f7e-8d11-6a4c2e9f0b33", name: "My workspace" }];
const fabricChecks = (method: "azure_cli" | "fabric_cli") => [
  { label: method === "azure_cli" ? "Azure CLI authenticated" : "Fabric CLI authenticated", ok: true },
  { label: method === "azure_cli" ? "Fabric API accessible" : "Fabric workspace discovered", ok: true },
  { label: "Workspace accessible", ok: true },
  { label: "Fabric capacity assigned", ok: true, detail: "Trial capacity (demo)" },
];
/** The demo Fabric target, connected and on a capacity, so a migration can run straight away. */
const readyFabric = (): FabricTarget => ({
  status: "connected", method: "azure_cli", account: "demo.user@contoso.com", tenantId: DEMO_TENANT,
  workspaces: DEMO_FABRIC_WORKSPACES, workspaceId: DEMO_FABRIC_WORKSPACES[0].id, workspaceName: DEMO_FABRIC_WORKSPACES[0].name,
  checks: fabricChecks("azure_cli"), capacityAssigned: true, message: null,
});

// Demo-only simulation state for the later phases.
let fabric: FabricTarget = readyFabric();
interface SimItem { id: string; name: string; type: string; wave: number; table?: string; filter?: DataFilterInput }
interface ExecSim { runId: string; items: SimItem[]; base: number; resumedAt: number | null; retried: boolean; credentials: string[]; options?: RunOptions; per: number; dur: number; failing: Set<string> }
let execSim: ExecSim | null = null;
let runCounter = 0;
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
/** The ready demo: the demo workspace connected, and its discovery finished a moment ago. */
const ready = (): Session => {
  const finished = Date.now() - STEPS.length * STEP_MS - 1000;
  return {
    connection: { ...connectionFor(DEMO_SOURCE, true), signedIn: true },
    scenario: DEMO_SOURCE.workspace,
    startedAt: finished,
    startedIso: new Date(finished).toISOString(),
    objects: null,
    summary: null,
    error: null,
    signedIn: { subscriptionId: DEMO_SOURCE.subscriptionId, tenantId: DEMO_TENANT },
  };
};
let s = ready();

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
    tenantId: c.tenantId.trim() || DEMO_TENANT,
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

/** What lives in the dedicated SQL pool: absent when a repository source has no pool added. */
const POOL_TYPES = new Set(["Table", "External Table", "View", "Stored Procedure", "Schema", "Dedicated SQL Pool"]);

function withoutPool(objects: ObjectDetail[]): ObjectDetail[] {
  const kept = objects.filter((o) => !POOL_TYPES.has(o.type));
  const ids = new Set(kept.map((o) => o.id));
  return kept.map((o) => ({
    ...o,
    dependencies: o.dependencies.map((d) => (d.objectId && !ids.has(d.objectId) ? { ...d, objectId: null } : d)),
    referencedBy: o.referencedBy.filter((r) => !r.objectId || ids.has(r.objectId)),
  }));
}

/** The demo repository's folders, as an upload or a clone would report them. */
const DEMO_REPO_COUNTS: Record<string, number> = { linkedService: 24, dataset: 60, pipeline: 40, notebook: 55, sqlscript: 30, sparkJobDefinition: 12, trigger: 8, integrationRuntime: 3 };

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
  s.objects = s.connection.repository && !s.connection.sqlPool ? withoutPool(objects) : objects;
  const kept = s.objects;
  const tally = (pick: (o: ObjectDetail) => string) => {
    const out: Record<string, number> = {};
    for (const o of kept) out[pick(o)] = (out[pick(o)] ?? 0) + 1;
    return out;
  };
  s.summary = {
    total: kept.length,
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

const WAREHOUSE = "TransportDW";
const deferredReason: Record<string, string> = {
  "Integration Runtime": "Set up by hand: Fabric runs cloud work on its own compute; a self-hosted runtime becomes an on-premises data gateway.",
};

/** What a completed object shows, by type, like the real run. */
/** The same wording the backend uses for a filter. */
function describeFilter(f: DataFilterInput): string {
  const parts: string[] = [];
  if (f.column) parts.push(`rows with ${f.column} ${[f.from && `on or after ${f.from}`, f.to && `before ${f.to}`].filter(Boolean).join(" and ")}`);
  if (f.sync) parts.push(`kept in sync by ${f.changeColumn || f.column}`);
  return parts.join("; ") || "every row";
}

/** Demo tables' date columns, the same for a table every time: every table records when rows change; facts also carry a business date. */
function demoDateColumns(name: string): { dateColumns: { name: string; type: string }[]; keyColumns: string[] } {
  const h = hash(name);
  const table = name.split(".").pop() ?? name;
  const dates = [{ name: "CreatedAt", type: "datetime2" }, { name: "ModifiedAt", type: "datetime2" }];
  if (/fact|trip|order|sale|ticket|event|log/i.test(table) || h % 3 === 0) dates.unshift({ name: h % 2 ? "TripDate" : "BusinessDate", type: "date" });
  if (h % 5 === 0) dates.push({ name: "DeletedAt", type: "datetime2" });
  return { dateColumns: dates, keyColumns: h % 4 === 0 ? [] : [`${table.replace(/^(Dim|Fact)/, "")}Id`] };
}

function outcome(i: SimItem, h: number): { step: string; target: string | null; notes: string[] } {
  const ws = fabric.workspaceName ?? "Fabric_demo";
  const wave = `load_${WAREHOUSE}_wave_${i.wave}`;
  switch (i.type) {
    case "Dedicated SQL Pool": return { step: "Fabric Warehouse created", target: `${ws} / ${WAREHOUSE}`, notes: ["Collation Latin1_General_100_CI_AS_KS_WS_SC_UTF8 (case-insensitive, like the Synapse pool)."] };
    case "Schema": return { step: "Schema created", target: `${WAREHOUSE}.${i.name.split(".").pop()}`, notes: [] };
    case "Table": return { step: "Table created (empty: the Table data stage loads it with a pipeline)", target: `${WAREHOUSE}.${i.name}`, notes: h % 3 === 0 ? ["Key kept as NOT ENFORCED: primary key (demo)."] : [] };
    case "Table data": {
      const f = i.filter;
      const rows = Math.round((1000 + (h % 900000)) * (f?.column ? 0.4 : 1)).toLocaleString();
      if (!f) return { step: `Loaded ${rows} rows by pipeline; counts match`, target: `${WAREHOUSE}.${i.table}`, notes: [`Pipeline '${wave}' created and run (demo).`] };
      const notes = [`Pipeline '${wave}' created and run (demo).`, `Loads ${describeFilter(f)}.`];
      if (f.sync) {
        const change = f.changeColumn || f.column;
        notes.push(`Registered for sync: changes to ${change} after 2026-10-08T09:30:00 are left to the sync pipeline (demo).`,
          `Sync pipeline 'sync_${WAREHOUSE}' created. Its daily schedule (02:00 UTC) was created switched off: switch it on in Fabric once the first load is done.`);
      }
      return { step: `Loaded ${rows} rows by pipeline; counts match${f.column ? " within the filter" : ""}`, target: `${WAREHOUSE}.${i.table}`, notes };
    }
    case "View": case "Stored Procedure": {
      const converted = h % 4 === 0;
      return { step: `${i.type === "View" ? "View" : "Stored procedure"} created${converted ? " (converted for Fabric)" : ""}`, target: `${WAREHOUSE}.${i.name}`,
        notes: converted ? ["Removed 1 table storage option clause (DISTRIBUTION, columnstore, heap, partitions): Fabric manages storage itself."] : [] };
    }
    case "Notebook": return { step: "Created as a Fabric notebook", target: `${ws} / ${i.name}`, notes: h % 2 === 0 ? ["Rewritten for Fabric: mssparkutils -> notebookutils (demo)."] : [] };
    case "Spark Pool": return { step: "Created as a Fabric Environment and published", target: `${ws} / ${i.name}`, notes: ["Spark 3.4 runs on Fabric Runtime 1.2 (demo)."] };
    case "Linked Service": return { step: "Created as a Fabric connection", target: i.name, notes: [] };
    case "Pipeline": return { step: "Created as a Fabric data pipeline", target: `${ws} / ${i.name}`, notes: [] };
    case "Dataset": return { step: "Embedded in the pipelines that use it", target: null, notes: ["Fabric pipelines carry their dataset settings inside each activity."] };
    case "Spark Job Definition": return { step: "Created as a Fabric Spark job definition", target: `${ws} / ${i.name}`, notes: [] };
    case "SQL Script": return { step: "Created as a T-SQL notebook", target: `${ws} / ${i.name}`, notes: ["Review before running: the script ran on the Synapse pool."] };
    case "Trigger": return { step: "Schedule created (switched off)", target: null, notes: [] };
    case "External Table": return { step: "Created as a OneLake shortcut", target: `${WAREHOUSE}_lakehouse/Files/${i.name}`, notes: [] };
    default: return { step: "Created in Fabric", target: null, notes: [] };
  }
}

function runNow(): ExecutionRun {
  if (!execSim) return { runId: "", state: "idle", total: 0, completed: 0, inProgress: 0, failed: 0, pending: 0, skipped: 0, deferred: 0, items: [], logs: [] };
  const sim = execSim;
  const elapsed = sim.base + (sim.resumedAt === null ? 0 : Date.now() - sim.resumedAt);
  const started = Date.now() - elapsed;
  const at = (ms: number) => new Date(started + ms).toISOString();
  const line = (ms: number, level: string, name: string, text: string) => `${at(ms)}  ${level.padEnd(11)}${name}: ${text}`;
  const logs: string[] = [line(0, "RUN", "run", `${sim.items.length} objects, workspace ${fabric.workspaceName ?? "Fabric_demo"}, warehouse ${WAREHOUSE} (demo)`)];
  let slot = 0;
  const items: ExecItem[] = sim.items.map((si) => {
    const base: ExecItem = { id: si.id, name: si.name, type: si.type, wave: si.wave, step: "Waiting", status: "PENDING", startedAt: null, completedAt: null, error: null };
    // Like the real backend: what this build does not create is listed, with the reason.
    if (!MIGRATABLE_TYPES.includes(si.type) && si.type !== "Table data") {
      return { ...base, step: "Not migrated in this session", status: "DEFERRED", error: deferredReason[si.type] ?? `${si.type} objects are migrated by hand (demo).` };
    }
    if (si.type === "Linked Service" && !sim.credentials.includes(si.name)) {
      return { ...base, step: "Needs credentials", status: "DEFERRED", target: si.name,
        notes: [`Enter credentials for '${si.name}' in Plan, Stages & credentials (the Connections stage), then run the Connections stage again from Migrate (demo).`] };
    }
    const begin = slot * sim.per, end = begin + sim.dur;
    slot += 1;
    if (elapsed < begin) return base;
    if (elapsed < end) {
      logs.push(line(begin, "START", si.name, si.type));
      return { ...base, step: si.type === "Table data" ? `Pipeline '${`load_${WAREHOUSE}_wave_${si.wave}`}' running` : "Creating in Fabric", status: "IN PROGRESS", startedAt: at(begin) };
    }
    const h = hash(si.id);
    if (!sim.retried && sim.failing.has(si.id)) {
      const error = si.type === "View" || si.type === "Stored Procedure"
        ? "Fabric Warehouse rejected the create: Incorrect syntax near 'OPTION' (demo). Review it by hand, then Retry Failed."
        : "Fabric was busy and did not answer in time (demo). Retry Failed runs it again.";
      logs.push(line(begin, "START", si.name, si.type), line(end, "FAILED", si.name, error));
      return { ...base, step: "Failed", status: "FAILED", startedAt: at(begin), completedAt: at(end), error };
    }
    const done = outcome(si, h);
    logs.push(line(begin, "START", si.name, si.type), line(end, "COMPLETED", si.name, done.step + (done.target ? ` -> ${done.target}` : "")));
    for (const n of done.notes) logs.push(line(end, "NOTE", si.name, n));
    return { ...base, step: done.step, status: "COMPLETED", startedAt: at(begin), completedAt: at(end), target: done.target, notes: done.notes };
  });
  const n = (st: ExecItem["status"]) => items.filter((i) => i.status === st).length;
  const allDone = items.every((i) => i.status !== "PENDING" && i.status !== "IN PROGRESS");
  if (allDone) logs.push(line(elapsed, "DONE", "run", "every pending object was processed"));
  return {
    runId: sim.runId,
    state: allDone ? "completed" : sim.resumedAt === null ? "paused" : "running",
    total: items.length, completed: n("COMPLETED"), inProgress: n("IN PROGRESS"), failed: n("FAILED"), pending: n("PENDING"),
    skipped: n("SKIPPED"), deferred: n("DEFERRED"), workspace: fabric.workspaceName ?? null, warehouse: WAREHOUSE,
    options: sim.options, items, logs,
  };
}

let plannerHistory: PlannerRun[] = [];

/** Back to the ready demo (connected, discovered, Fabric connected), as on a fresh page load. */
export function resetDemo(): void {
  s = ready();
  fabric = readyFabric();
  execSim = null;
  plannerHistory = [];
}

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
  if (fabric.status !== "connected") risks.push({ id: "", code: "TARGET_NOT_READY", severity: "BLOCKING", title: "Fabric target connected", message: "Connect the Fabric target and pass its connection test (in Demo data, reload the page to restore the demo target).", objects: [] });
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
      { label: "Fabric target connected", status: fabric.status === "connected" ? "ok" : "fail", detail: fabric.status === "connected" ? `Workspace ${fabric.workspaceName} (demo)` : "Connect the Fabric target first." },
      { label: "Workspace on a Fabric capacity", status: fabric.status === "connected" ? "ok" : "fail", detail: fabric.status === "connected" ? "Trial capacity (demo)" : "Connect the Fabric target first." },
      { label: "SQL driver for the Warehouse", status: "ok", detail: "Not needed in Demo data" },
      { label: "Synapse source connected (row counts and shortcuts)", status: s.connection.status === "connected" ? "ok" : "fail", detail: s.connection.status === "connected" ? `${s.connection.workspace} (demo)` : "Connect the Synapse source again." },
    ],
    objectStrategies, history: plannerHistory,
  };
}

const DEMO_STAGES: Capabilities["stages"] = [
  { key: "warehouse", label: "Warehouse & schema", summary: "The SQL pool becomes a Fabric Warehouse; its schemas, tables (empty, with their keys), views and stored procedures are created in it. Synapse-only T-SQL in views and procedures is converted first.", types: ["Dedicated SQL Pool", "Schema", "Table", "View", "Stored Procedure"], needs: [], needsInput: null, creates: "Warehouse, schemas, tables, views, procedures", options: [{ key: "collation", label: "Warehouse collation (set once, when it is created)", default: "match_synapse", choices: [{ value: "match_synapse", label: "Same as Synapse", description: "Case-sensitive only if the Synapse pool was; Synapse's default is case-insensitive." }, { value: "case_insensitive", label: "Case-insensitive", description: "'ABC' equals 'abc' in comparisons and object names, as in most Synapse pools." }, { value: "case_sensitive", label: "Case-sensitive", description: "Fabric's own default: 'ABC' and 'abc' are different." }] }] },
  { key: "data", label: "Table data", summary: "Loads the rows of each migrated table with Fabric data pipelines: one pipeline per wave, reading the Synapse pool through a Fabric connection and writing the Warehouse. The run then compares row counts.", types: ["Table"], needs: ["warehouse"], needsInput: "credentials", creates: "Data pipelines; rows in the Warehouse tables", options: [{ key: "dataRun", label: "What the stage does", default: "run", choices: [{ value: "run", label: "Create and run", description: "Creates each wave's pipeline, runs it, waits for it, and compares row counts." }, { value: "create", label: "Create only", description: "Creates the pipelines; you run them from Fabric (or a schedule) when you choose." }] }, { key: "dataMode", label: "If a table already has rows", default: "if_empty", choices: [{ value: "if_empty", label: "Skip it (safe)", description: "Never touches data that is already there, so re-runs are safe." }, { value: "replace", label: "Replace it", description: "The pipeline empties the table (TRUNCATE) before loading it again." }] }, { key: "syncOverlap", label: "Re-read window for tables kept in sync", default: "1h", choices: [{ value: "1h", label: "One hour", description: "Each sync also re-reads the hour before the last one, catching rows committed late. Re-read rows replace themselves, so nothing doubles." }, { value: "1d", label: "One day", description: "Re-reads the day before the last sync: for sources that write rows with older timestamps, or a date-only change column." }, { value: "none", label: "None", description: "Reads only from the last sync onwards. Tables without key columns always work this way." }] }] },
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
        sourceKinds: ["workspace", "git", "zip"],
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
    s.signedIn = { subscriptionId: c.subscriptionId.trim(), tenantId: c.tenantId.trim() || DEMO_TENANT };
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
    return ws.includes("empty") ? [] : ["TransportDW"];
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

  async uploadZip(file: File) {
    await sleep(500);
    if (!/\.zip$/i.test(file.name)) throw new ApiRequestError("invalid_zip", "The file is not a valid ZIP archive.", 400);
    return { uploadId: "d".repeat(32), fileName: file.name, rootFolder: "synapse", counts: { ...DEMO_REPO_COUNTS }, note: null };
  },

  async connectRepository(c: RepositoryConfig) {
    await sleep(900);
    if (!c.environment?.trim()) throw new ApiRequestError("invalid_configuration", "Choose the environment these definitions are for.", 400);
    const withPool = !!(c.resourceGroup || c.workspace || c.sqlPool);
    if (withPool && !(c.resourceGroup && c.workspace && c.sqlPool)) throw new ApiRequestError("invalid_configuration", "To add the SQL pool, choose its resource group, workspace and pool.", 400);
    if (withPool && !s.signedIn) throw new ApiRequestError("sign_in_required", "Sign in to Azure first.", 409);
    const url = (c.repositoryUrl ?? "").trim();
    if (c.kind === "git" && !/^(https:\/\/|git@|ssh:\/\/)/.test(url)) throw new ApiRequestError("invalid_configuration", "Enter the repository's HTTPS or SSH URL.", 400);
    if (c.kind === "zip" && !c.uploadId) throw new ApiRequestError("invalid_configuration", "The upload is not known. Upload the ZIP again.", 400);
    const label = c.kind === "git" ? url : (c.fileName || "workspace.zip");
    const firstCheck = c.kind === "git"
      ? { category: null, name: "Git repository", status: "ok" as const, message: `${url} reachable (demo)` }
      : { category: null, name: "ZIP export", status: "ok" as const, message: `${label} extracted (demo)` };
    if (c.kind === "git" && /missing|denied/i.test(url)) {
      s.connection = { status: "disconnected", ok: false, signedIn: !!s.signedIn, checks: [{ ...firstCheck, status: "failed", message: "Repository not found (demo)" }],
        error: { code: "configuration", title: "Repository not reachable", hint: "Check the URL and branch, and that git on this machine can reach it.", message: "Repository not found (demo)" } };
      return s.connection;
    }
    const params = c.parameters ? Object.keys(((): Record<string, unknown> => { try { const d = JSON.parse(c.parameters!); return (d.parameters ?? d) as Record<string, unknown>; } catch { return {}; } })()) : [];
    const applied = params.filter((n) => /^ls_/i.test(n));
    const unmatched = params.filter((n) => !/^ls_/i.test(n));
    const rootFolder = (c.rootFolder ?? "").trim() || "synapse";
    const counts = { ...DEMO_REPO_COUNTS };
    const artifacts = Object.values(counts).reduce((a, b) => a + b, 0);
    const display = c.kind === "git" ? (url.replace(/\/+$/, "").split("/").pop() ?? "repository").replace(/\.git$/, "") : label.replace(/\.zip$/i, "");
    s.connection = {
      status: "connected", ok: true, signedIn: !!s.signedIn, signInMethod: s.signedIn ? "azure_cli" : null, method: c.kind, sourceKind: c.kind, sourcePlatform: "Azure Synapse",
      workspace: c.workspace || display, resourceGroup: c.resourceGroup ?? "", subscriptionId: s.signedIn?.subscriptionId ?? "",
      subscriptionName: s.signedIn ? "Demo subscription" : null, tenantId: s.signedIn?.tenantId ?? null,
      sqlPool: withPool ? c.sqlPool! : null, testedAt: new Date().toISOString(),
      checks: [
        firstCheck,
        { category: null, name: "Synapse definitions", status: "ok", message: `${artifacts} definitions in ${Object.keys(counts).length} folders, in ${rootFolder}/ (demo)` },
        c.parameters
          ? { category: null, name: `${c.environment} parameters`, status: "ok", message: `${applied.length} of ${params.length} values applied to linked services (demo)` }
          : { category: null, name: `${c.environment} parameters`, status: "skipped", message: "No parameters file: linked services keep the values committed in the repository" },
        ...(withPool ? [{ category: null, name: "Dedicated SQL pool", status: "ok" as const, message: `${c.sqlPool} reachable (demo)` }] : []),
      ],
      repository: {
        kind: c.kind, label, environment: c.environment.trim(), rootFolder, ref: c.kind === "git" ? (c.ref?.trim() || "main") : null,
        commit: c.kind === "git" ? "3f9a1c2" : null, parametersName: c.parameters ? (c.parametersName ?? "parameters.json") : null,
        applied, unmatched, counts, artifacts,
      },
    };
    s.scenario = "repository";
    s.startedAt = null;
    s.objects = null;
    return s.connection;
  },

  async disconnect() {
    await sleep(150);
    // Like the backend: signing out forgets the discovery, not the record of a run.
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

  async resetDiscovery() {
    await sleep(150);
    if (statusNow().state === "running") throw new ApiRequestError("discovery_running", "Discovery is running; wait for it to finish.", 409);
    s.startedAt = null;
    s.startedIso = null;
    s.objects = null;
    s.summary = null;
    s.error = null;
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
      status: "authenticated", method: c.method, account: "demo.user@contoso.com", tenantId: DEMO_TENANT,
      workspaces: DEMO_FABRIC_WORKSPACES,
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
    const picked = DEMO_FABRIC_WORKSPACES.find((w) => w.id === c.workspaceId.trim());
    fabric = {
      ...fabric, status: "connected", method: c.method,
      workspaceName: c.workspaceName.trim() || picked?.name || DEMO_FABRIC_WORKSPACES[0].name,
      workspaceId: c.workspaceId.trim() || DEMO_FABRIC_WORKSPACES[0].id, message: null,
      checks: fabricChecks(c.method), capacityAssigned: true,
    };
    return fabric;
  },

  async disconnectFabric() {
    fabric = { status: "disconnected" };
    return fabric;
  },

  async getCapabilities() {
    await sleep(60);
    statusNow(); // builds the discovery results if they are due, so the linked services are listed on a first load
    const kvHint = "The secret stays in Azure Key Vault and never passes through this tool. Create an Azure Key Vault reference in Fabric (Manage connections and gateways, Azure Key Vault references) and enter its alias or ID with the secret's name. Fabric reads the secret's latest version each time it connects.";
    const sqlAuth = [
      { value: "workspaceIdentity", label: "Workspace identity (no secret)", fields: [], hint: "Nothing to enter: Fabric signs in as the workspace's own identity. It works once a workspace admin has created the workspace identity (Workspace settings, Workspace identity) and a database admin runs CREATE USER [<Fabric workspace name>] FROM EXTERNAL PROVIDER and grants it read access (db_datareader) in the database. Whoever runs the pipelines needs the Admin, Member or Contributor role in the workspace." },
      { value: "basicKeyVault", label: "SQL login, password in Key Vault", fields: ["username", "keyVault", "secretName"], hint: kvHint },
      { value: "servicePrincipalKeyVault", label: "Service principal, secret in Key Vault", fields: ["tenantId", "clientId", "keyVault", "secretName"], hint: kvHint },
      { value: "basic", label: "SQL login", fields: ["username", "password"] },
      { value: "servicePrincipal", label: "Service principal", fields: ["tenantId", "clientId", "clientSecret"] },
    ];
    const linked = (s.objects ?? []).filter((o) => o.type === "Linked Service").slice(0, 3).map((o) => ({
      name: o.name, type: "AzureSqlDW", fabricType: "SQL", needsPath: false, unsupported: null, authTypes: sqlAuth, stage: "connections",
    }));
    const pool = { name: `synapse-${s.connection.workspace ?? DEMO_SOURCE.workspace}-${s.connection.sqlPool ?? DEMO_SOURCE.sqlPool}`, type: "Synapse dedicated SQL pool", fabricType: "SQL", needsPath: false, unsupported: null, authTypes: sqlAuth, stage: "data" };
    const dataTables = (s.objects ?? []).filter((o) => o.type === "Table").map((o) => {
      const [schema, name] = o.name.includes(".") ? o.name.split(".", 2) : ["dbo", o.name];
      return { id: o.id, schema, name, key: o.name.toLowerCase(), ...demoDateColumns(o.name) };
    });
    return { stages: DEMO_STAGES, defaults: { dataMode: "if_empty", dataRun: "run", collation: "match_synapse", syncOverlap: "1h" }, linkedServices: [pool, ...linked], dataTables };
  },

  async analyzePlan(items: PlanItem[], record?: boolean, options?: RunOptions, credentials?: ConnectionCredentials) {
    await sleep(120);
    void credentials;
    return demoAnalysis(items, !!record, options);
  },

  async startExecution(items: PlanItem[], options?: RunOptions, credentials?: ConnectionCredentials) {
    await sleep(200);
    if (fabric.status !== "connected") throw new ApiRequestError("target_not_connected", "Connect the Fabric target first. In Demo data, reload the page to restore the demo target.", 409);
    if (!items.length) throw new ApiRequestError("empty_plan", "The migration plan is empty.", 400);
    const objects = new Map((s.objects ?? []).map((o) => [o.id, o]));
    const stages = options?.stages ?? [];
    const on = (key: string | undefined) => !stages.length || (!!key && stages.includes(key));
    const sim: SimItem[] = [];
    for (const pi of items) {
      const o = objects.get(pi.id);
      if (!o) continue;
      const key = stageOfType(o.type);
      if (on(key)) sim.push({ id: o.id, name: o.name, type: o.type, wave: pi.wave });
      else if (options?.scope === "all") sim.push({ id: o.id, name: o.name, type: o.type, wave: pi.wave });
      // Like the real run: every table gets a data load, done by that wave's pipeline.
      if (o.type === "Table" && on("data")) sim.push({ id: `${o.id}#data`, name: `${o.name} (data)`, type: "Table data", wave: pi.wave, table: o.name, filter: options?.dataFilters?.[o.name.toLowerCase()] });
    }
    const kept = options?.scope === "automated" ? sim.filter((i) => MIGRATABLE_TYPES.includes(i.type) || i.type === "Table data") : sim;
    if (!kept.length) throw new ApiRequestError("nothing_to_migrate", "No object in the plan can be created by this build. Switch on more stages or choose scope All.", 400);
    const order = (i: SimItem) => i.wave * 100 + (["Dedicated SQL Pool", "Schema", "Linked Service", "Table", "View", "Stored Procedure", "Table data"].indexOf(i.type) + 1 || 50);
    kept.sort((a, b) => order(a) - order(b));
    // A demo run takes about a minute, however large the plan.
    const per = Math.max(30, Math.min(350, Math.floor(60000 / kept.length)));
    // Two or three failures on purpose, so Retry Failed can be tried: one view or procedure Fabric "rejects",
    // and a busy service. Chosen the same way every time for the same plan.
    const candidates = kept.filter((i) => MIGRATABLE_TYPES.includes(i.type) && i.type !== "Dataset" && i.type !== "Linked Service")
      .sort((a, b) => hash(a.id) - hash(b.id));
    const rejected = candidates.find((i) => i.type === "View" || i.type === "Stored Procedure");
    const others = candidates.filter((i) => i !== rejected).slice(0, kept.length >= 20 ? 2 : kept.length >= 5 ? 1 : 0);
    const failing = new Set([...(rejected && kept.length >= 5 ? [rejected.id] : []), ...others.map((i) => i.id)]);
    runCounter += 1;
    execSim = { runId: String(runCounter).padStart(3, "0"), items: kept, base: 0, resumedAt: Date.now(), retried: false,
      credentials: Object.entries(credentials ?? {}).filter(([, f]) => Object.values(f).some((v) => v.trim())).map(([n]) => n), options, per, dur: Math.min(600, per * 2), failing };
    return runNow();
  },

  async getExecution() {
    return runNow();
  },

  async controlExecution(action: RunControl) {
    if (action === "reset") {
      if (runNow().state === "running") throw new ApiRequestError("run_in_progress", "The run is still working. Pause it, then reset.", 409);
      execSim = null;
      return runNow();
    }
    if (!execSim) throw new ApiRequestError("no_run", "There is no migration run.", 409);
    const now = Date.now();
    if (action === "pause" && execSim.resumedAt !== null) {
      execSim.base += now - execSim.resumedAt;
      execSim.resumedAt = null;
    } else if (action === "resume" && execSim.resumedAt === null) {
      execSim.resumedAt = now;
    } else if (action === "retry") {
      // Failed objects run again from now, and succeed this time.
      execSim.retried = true;
      if (execSim.resumedAt === null) execSim.resumedAt = now;
    }
    return runNow();
  },

  async runValidation(): Promise<ValidationRow[]> {
    await sleep(500);
    const run = runNow();
    const done = run.items.filter((i) => i.status === "COMPLETED");
    if (!done.length) throw new ApiRequestError("nothing_to_validate", "Nothing has been migrated yet. Run the migration in the Migrate step first.", 409);
    const rows: ValidationRow[] = [];
    let short = 0; // deliberate row-count mismatches, at most three
    const category: Record<string, string> = {
      "Dedicated SQL Pool": "Warehouse", Schema: "Schema", Table: "Tables", View: "Views", "Stored Procedure": "Stored Procedures",
      "Spark Pool": "Spark", Notebook: "Notebooks", "Linked Service": "Connections", Pipeline: "Pipelines", Dataset: "Pipelines",
      "Spark Job Definition": "Spark Jobs", "SQL Script": "SQL Scripts", Trigger: "Schedules", "External Table": "Shortcuts",
    };
    for (const i of done) {
      const h = hash(i.id);
      if (i.type === "Table data") {
        const n = 1000 + (h % 900000);
        const bad = h % 13 === 0 && short++ < 3;
        rows.push({ category: "Data Count", object: i.name.replace(/ \(data\)$/, ""), source: `${n.toLocaleString()} rows`, target: `${(bad ? n - 12 : n).toLocaleString()} rows`, status: bad ? "MISMATCH" : "MATCH",
          detail: bad ? "12 rows fewer in the Warehouse (demo): check the pipeline run, then reload the table." : "Same row count on both sides." });
      } else if (i.type === "Table") {
        const changed = h % 20 === 0;
        rows.push({ category: "Tables", object: i.name, source: "8 columns", target: "8 columns", status: changed ? "REVIEW" : "MATCH",
          detail: changed ? "A column type changed by design (datetime → datetime2(3), demo)." : "Same columns and types." });
      } else if (i.type === "Pipeline" || i.type === "Notebook") {
        const n = 2 + (h % 6);
        rows.push({ category: category[i.type], object: i.name, source: `${n} ${i.type === "Pipeline" ? "activities" : "cells"}`, target: `${n} ${i.type === "Pipeline" ? "activities" : "cells"}`, status: "MATCH", detail: "Present in Fabric with the same structure." });
      } else if (i.type === "View" || i.type === "Stored Procedure") {
        const review = h % 10 === 0;
        rows.push({ category: category[i.type], object: i.name, source: "definition", target: "definition", status: review ? "REVIEW" : "MATCH",
          detail: review ? "The definition was converted for Fabric (storage options removed); compare its results once." : "The definition text agrees." });
      } else if (category[i.type]) {
        rows.push({ category: category[i.type], object: i.name, source: "present", target: "present", status: "MATCH", detail: "Present in Fabric." });
      }
    }
    for (const i of run.items.filter((x) => x.status === "DEFERRED" && x.type === "Integration Runtime")) {
      rows.push({ category: "Manual", object: i.name, source: "present", target: "set up by hand", status: "REVIEW", detail: i.error ?? "Set up by hand." });
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

