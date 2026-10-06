// Shapes shared by the UI, the real API client and the mock layer.
// They mirror what the Python API returns (src/discovery_agent/api).

export type AuthMethod = "azure_cli" | "interactive_browser";
export type ApiMode = "real" | "mock";

/** What the operator types. Identifiers only; no field can hold a secret. */
export interface ConnectionConfig {
  method: AuthMethod;
  tenantId: string;
  subscriptionId: string;
  resourceGroup: string;
  workspace: string;
  workspaceUrl: string;
  sqlPool: string;
  resource: string;
}

export type ConnectionStatus = "disconnected" | "not_connected" | "connected";

export interface ConnectionCheck {
  name: string;
  status: "ok" | "failed" | "skipped";
  message: string;
  category: string | null;
}

export interface ConnectionError {
  code: string;
  title: string;
  hint: string;
  message: string;
}

/** Safe connection facts. Contains no token, secret or header. */
export interface ConnectionState {
  status: ConnectionStatus;
  ok: boolean;
  /** An Azure identity has been proven on the backend for `method`. */
  signedIn?: boolean;
  signingIn?: boolean;
  method?: AuthMethod;
  sourcePlatform?: string;
  workspace?: string;
  resourceGroup?: string;
  subscriptionId?: string;
  subscriptionName?: string | null;
  tenantId?: string | null;
  sqlPool?: string | null;
  testedAt?: string | null;
  checks: ConnectionCheck[];
  error?: ConnectionError;
}

export interface Health {
  status: "ok";
  capabilities: { authMethods: AuthMethod[]; authMethodDetails?: AuthMethodDetail[]; discoveryScope: string[]; migratableTypes?: string[] };
}

/** What the backend says about one sign-in method. */
export interface AuthMethodDetail {
  id: AuthMethod;
  label: string;
  detail: string;
  bestFor?: string;
  caveat?: string;
}

export type DiscoveryState =
  | "idle"
  | "running"
  | "completed"
  | "completed_with_warnings"
  | "failed";

export interface ProgressStep {
  label: string;
  state: "done" | "active" | "pending";
}

/** Counts come from the discovery result, never from the UI. */
export interface DiscoverySummary {
  total: number;
  byCategory: Record<string, number>;
  byType: Record<string, number>;
  byWorkstream: Record<string, number>;
  byClassification: Record<string, number>;
  byPath: Record<string, number>;
  byFabricTarget: Record<string, number>;
  /** A named Fabric component exists for the path. Says nothing about whether it will migrate. */
  withFabricMapping: number;
  requiringAssessment: number;
  manualOrAssessment: number;
  failedCategories: { name: string; reason: string }[];
  warnings: string[];
  warningCount: number;
  coverage: {
    discoveredTypes: string[];
    notDiscovered: { type: string; reason: string }[];
  };
}

export interface DiscoveryStatus {
  state: DiscoveryState;
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
  workspace: string | null;
  progress: ProgressStep[] | null;
  summary: DiscoverySummary | null;
}

/** Discovery status: factual, never evaluative. */
export type ObjectStatus = "Discovered" | "Warning" | "Partial" | "Failed";

/** How far the *mapping* got. It is not a migration verdict. */
export type MappingStatus =
  | "Mapped"
  | "Mapped with Transformation"
  | "Requires Assessment"
  | "No Automatic Mapping";

/** Display codes for the preliminary classification. Owned by the backend. */
export type Classification = "DIRECT" | "RECONFIGURE" | "TRANSFORM" | "MANUAL" | "REVIEW" | "NOT SUPPORTED";

export type MigrationPath =
  | "Direct Target"
  | "Target With Transformation"
  | "Target With Refactoring"
  | "Requires Reconfiguration"
  | "Requires Assessment"
  | "Manual / Special Handling";

export interface ObjectRow {
  id: string;
  name: string;
  /** The Synapse object type. */
  type: string;
  category: string;
  workspace: string;
  status: ObjectStatus;
  dependencyCount: number;
  sources: string[];
  fabricTarget: string;
  targetType: string;
  migrationPath: MigrationPath;
  automationPotential: string;
  assessmentRequired: boolean;
  mappingStatus: MappingStatus;
  workstream: string;
  classification: Classification;
  action: string;
  /** Schema name where the object has one, else "". */
  schema: string;
  /** Columns, activities, cells... or "". */
  size: string;
  /** The Synapse component the object belongs to, e.g. "Dedicated SQL Pool". */
  component: string;
  /** Suggested migration wave (dependency ordered). */
  wave: number;
}

export interface DependencyRef {
  name: string;
  kind: string;
  type: string | null;
  /** Set only when the target is another object in this discovery run. */
  objectId: string | null;
  location: string;
  /** The Fabric component this dependency conceptually lands on. A label, not an object. */
  fabricTarget: string | null;
}

export interface ReferencedBy {
  name: string;
  type: string;
  objectId: string;
}

export interface ObjectIssue {
  code: string;
  message: string;
  source: string | null;
  facet: string | null;
}

export interface ActivityMapping {
  name: string;
  type: string;
  parent: string | null;
  source: string | null;
  sink: string | null;
  dependsOn: string[];
  references: string[];
  expressionCount: number;
  fabricEquivalent: string;
  equivalence: "Known equivalent" | "Requires Assessment";
  requiresTransformation: boolean;
  requiresManualReview: boolean;
  note: string;
}

export interface ObjectDetail extends ObjectRow {
  discoveredAt: string | null;
  target: { platform: string; component: string; componentType: string };
  migration: {
    path: MigrationPath;
    route: string;
    assessmentRequired: boolean;
    automationPotential: string;
    mappingStatus: MappingStatus;
    workstream: string;
    classification: Classification;
    action: string;
  };
  notes: string[];
  steps: string[];
  actions: { label: string; state: "ok" | "warn" }[];
  overview: Record<string, unknown>;
  configuration: Record<string, unknown> | null;
  dependencies: DependencyRef[];
  referencedBy: ReferencedBy[];
  activities: ActivityMapping[];
  issues: ObjectIssue[];
  rawMetadata: unknown;
}

export type SortKey =
  | "name"
  | "type"
  | "category"
  | "status"
  | "dependencies"
  | "workspace"
  | "target"
  | "targetType"
  | "path"
  | "automation"
  | "assessment"
  | "mappingStatus"
  | "classification"
  | "wave";

export interface ResultsQuery {
  search: string;
  categories: string[];
  types: string[];
  statuses: string[];
  fabricTargets: string[];
  paths: string[];
  workstreams: string[];
  mappingStatuses: string[];
  classifications: string[];
  /** "" = either, "yes" or "no". */
  assessment: "" | "yes" | "no";
  sort: SortKey;
  dir: "asc" | "desc";
  page: number;
  pageSize: number;
}

export interface ResultsPage {
  items: ObjectRow[];
  total: number;
  page: number;
  pageSize: number;
  facets: {
    categories: Record<string, number>;
    types: Record<string, number>;
    statuses: Record<string, number>;
    fabricTargets: Record<string, number>;
    paths: Record<string, number>;
    workstreams: Record<string, number>;
    mappingStatuses: Record<string, number>;
    classifications: Record<string, number>;
    assessment: Record<string, number>;
  };
  discoveredAt: string | null;
}

// ---- dependency graph & waves ---------------------------------------------------

export interface GraphNode {
  id: string;
  name: string;
  type: string;
  category: string;
  classification: Classification;
  fabricTarget: string;
  wave: number;
  dependsOn: number;
  dependedOnBy: number;
}
export interface DependencyGraph {
  nodes: GraphNode[];
  /** `source` needs `target`. */
  edges: { source: string; target: string }[];
  waves: { wave: number; count: number; types: Record<string, number> }[];
  discoveredAt: string | null;
}

export interface ComponentRow {
  sourceType: string;
  fabricTarget: string;
  targetType: string;
  migrationPath: MigrationPath;
  classification: Classification;
  action: string;
  workstream: string;
  notes: string[];
}

export interface MetadataExport {
  platform: string;
  workspace: string | null;
  discoveredAt: string | null;
  summary: DiscoverySummary | null;
  objects: (ObjectRow & { dependsOn: string[] })[];
}

// ---- Fabric target ----------------------------------------------------------------

export type FabricAuthMethod = "azure_cli" | "fabric_cli";
/** No token or secret ever travels through the UI: the backend holds the session. */
export interface FabricConfig {
  method: FabricAuthMethod;
  workspaceId: string;
  workspaceName: string;
}
export interface FabricWorkspace { id: string; name: string }
export interface FabricCheck { label: string; ok: boolean; detail?: string }
export type FabricStatus = "disconnected" | "signing_in" | "authenticated" | "connected" | "failed";
export interface FabricTarget {
  status: FabricStatus;
  method?: FabricAuthMethod;
  account?: string | null;
  tenantId?: string | null;
  workspaces?: FabricWorkspace[];
  workspaceId?: string | null;
  workspaceName?: string | null;
  checks?: FabricCheck[];
  message?: string | null;
  /** After a passed test: whether the workspace is on a Fabric capacity (migration needs one). */
  capacityAssigned?: boolean | null;
}

// ---- plan, execution, validation ----------------------------------------------------

export type PlanStatus = "NOT STARTED" | "READY" | "IN PROGRESS" | "COMPLETED" | "FAILED" | "BLOCKED";
export interface PlanItem { id: string; wave: number }

// ---- planner: strategy, risks, effort -------------------------------------------------

export type Strategy = "automated" | "manual" | "assess" | "later" | "deselected";
export type RiskSeverity = "BLOCKING" | "HIGH" | "MEDIUM" | "LOW";
export interface PlanRisk { id: string; code: string; severity: RiskSeverity; title: string; message: string; objects: string[] }
export interface PlanCheck { label: string; status: "ok" | "warn" | "fail"; detail?: string }
export interface PlanWaveSummary { wave: number; count: number; automated: number; types: Record<string, number>; effortDays: number }
export interface TypeStrategy {
  type: string; count: number; strategy: Strategy; strategyLabel: string; target: string;
  firstWave: number; lastWave: number; effortDays: number;
}
export interface PlannerRun {
  id: string; plannerVersion: string; fingerprint: string; objects: number; effortDays: number;
  waves: number; readiness: number; blocking: number; createdAt: string; status: string;
}
export interface PlanAnalysis {
  plannerVersion: string;
  /** Same plan over the same estate always gives the same fingerprint. */
  fingerprint: string;
  readiness: number;
  objects: number;
  strategyCounts: Record<Strategy, number>;
  effortDays: number;
  needsReview: { count: number; effortDays: number };
  blocking: number;
  waves: PlanWaveSummary[];
  typeStrategies: TypeStrategy[];
  risks: PlanRisk[];
  riskCounts: Record<RiskSeverity, number>;
  checks: PlanCheck[];
  objectStrategies: Record<string, { strategy: Strategy; label: string; effortHours: number }>;
  history: PlannerRun[];
}
/** How a run is carried out. `automated` runs only what this build creates; `all` also lists the deferred. */
export interface RunOptions {
  scope: "automated" | "all";
  stopOnFailure: boolean;
  /** The migration stages switched on for this run. */
  stages: string[];
  /** Table data: what to do with a table that already has rows. */
  dataMode: "if_empty" | "replace";
  /** Table data: create each wave's data pipeline and run it, or only create it. */
  dataRun: "run" | "create";
  /** Warehouse: the collation it is created with (it cannot change afterwards). */
  collation: "match_synapse" | "case_insensitive" | "case_sensitive";
}

// ---- migration stages ------------------------------------------------------------------

export interface StageOption { key: string; label: string; default: string; choices: { value: string; label?: string; description: string }[] }
export interface StageDef {
  key: string;
  label: string;
  summary: string;
  /** Plan object types this stage migrates. */
  types: string[];
  /** Stages that must have run first. */
  needs: string[];
  needsInput: string | null;
  creates: string;
  options: StageOption[];
}
export interface CredentialAuth { value: string; label: string; fields: string[] }
export interface LinkedServiceInput {
  name: string;
  type: string;
  fabricType: string | null;
  authTypes: CredentialAuth[];
  needsPath: boolean;
  /** Set when this linked service has no Fabric connection type this tool can create. */
  unsupported: string | null;
  /** The stage that asks for this credential: "connections" for linked services, "data" for the Synapse pool connection. */
  stage?: string;
}
export interface Capabilities {
  stages: StageDef[];
  defaults: Record<string, string>;
  linkedServices: LinkedServiceInput[];
}
/** Credentials by linked-service name. Held in memory only: never stored in the browser, never shown again. */
export type ConnectionCredentials = Record<string, Record<string, string>>;

export type ExecState = "idle" | "running" | "paused" | "completed";
export interface ExecItem {
  id: string;
  name: string;
  type: string;
  /** The wave it was planned in. */
  wave?: number;
  step: string;
  /** SKIPPED: already in Fabric, left unchanged. DEFERRED: not migrated in this session; error says why. */
  status: "PENDING" | "IN PROGRESS" | "COMPLETED" | "FAILED" | "SKIPPED" | "DEFERRED";
  startedAt: string | null;
  completedAt: string | null;
  error: string | null;
  /** What changed on the way (types mapped, settings dropped). */
  notes?: string[];
  /** Where it landed in Fabric. */
  target?: string | null;
}
export interface ExecutionRun {
  runId: string;
  state: ExecState;
  total: number;
  completed: number;
  inProgress: number;
  failed: number;
  pending: number;
  skipped: number;
  deferred: number;
  workspace?: string | null;
  warehouse?: string | null;
  options?: RunOptions;
  /** Set when a stop-on-failure run paused itself, with the reason. */
  haltedReason?: string | null;
  items: ExecItem[];
  logs: string[];
}

export type ValidationStatus = "MATCH" | "REVIEW" | "MISMATCH";
export interface ValidationRow {
  category: string;
  object: string;
  source: string;
  target: string;
  status: ValidationStatus;
  /** Why it matched, differs or could not be checked. */
  detail?: string;
}

/** The single seam between the UI and whatever supplies data. */
export interface MigrationApi {
  readonly mode: ApiMode;
  health(): Promise<Health>;
  getConnection(): Promise<ConnectionState>;
  authenticate(config: ConnectionConfig): Promise<ConnectionState>;
  testConnection(config: ConnectionConfig): Promise<ConnectionState>;
  disconnect(): Promise<ConnectionState>;
  startDiscovery(): Promise<DiscoveryStatus>;
  getDiscoveryStatus(): Promise<DiscoveryStatus>;
  getResults(query: ResultsQuery): Promise<ResultsPage>;
  getObject(id: string): Promise<ObjectDetail>;
  /** Dropdown contents. Each needs a completed sign-in. */
  listResourceGroups(): Promise<string[]>;
  listWorkspaces(resourceGroup: string): Promise<string[]>;
  listSqlPools(resourceGroup: string, workspace: string): Promise<string[]>;
  getDependencies(): Promise<DependencyGraph>;
  getComponents(): Promise<ComponentRow[]>;
  exportMetadata(): Promise<MetadataExport>;
  // The calls below have no backend yet. The real client rejects them with a
  // "not_implemented" error; only the demo client simulates them.
  getFabricTarget(): Promise<FabricTarget>;
  authenticateFabric(config: FabricConfig): Promise<FabricTarget>;
  testFabric(config: FabricConfig): Promise<FabricTarget>;
  disconnectFabric(): Promise<FabricTarget>;
  getCapabilities(): Promise<Capabilities>;
  analyzePlan(items: PlanItem[], record?: boolean, options?: RunOptions, credentials?: ConnectionCredentials): Promise<PlanAnalysis>;
  startExecution(items: PlanItem[], options?: RunOptions, credentials?: ConnectionCredentials): Promise<ExecutionRun>;
  getExecution(): Promise<ExecutionRun>;
  controlExecution(action: "pause" | "resume" | "retry"): Promise<ExecutionRun>;
  /** Compares Synapse with Fabric. With no items, every discovered object is checked. */
  runValidation(items?: PlanItem[]): Promise<ValidationRow[]>;
}

export class ApiRequestError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly status = 0,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

/** Categories in display order. */
export const CLASSIFICATIONS: Classification[] = ["DIRECT", "RECONFIGURE", "TRANSFORM", "MANUAL", "REVIEW", "NOT SUPPORTED"];

export const CATEGORIES = ["SQL", "Spark", "Integration", "Storage", "Security", "Networking", "Other"] as const;

export const MIGRATION_PATHS: MigrationPath[] = [
  "Direct Target",
  "Target With Transformation",
  "Target With Refactoring",
  "Requires Reconfiguration",
  "Requires Assessment",
  "Manual / Special Handling",
];

/** Workstreams in display order (the backend assigns each object to one). */
export const WORKSTREAMS = [
  "Data Warehouse",
  "Data Engineering",
  "Data Factory",
  "OneLake / Storage",
  "Connections",
  "Security & Governance",
  "Unassigned",
] as const;
