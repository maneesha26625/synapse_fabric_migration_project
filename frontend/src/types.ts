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
  /** The Azure sign-in method held, when `method` names a repository source instead. */
  signInMethod?: AuthMethod | null;
  /** Who signed in (their sign-in name), for display. */
  account?: string | null;
  /** Every directory (tenant) the signed-in account belongs to. */
  directories?: AzureDirectory[];
  /** How many subscriptions the sign-in can see across them; changes when a directory is signed in to. */
  subscriptionCount?: number;
  signingIn?: boolean;
  /** How the source was connected: an Azure sign-in method, or a Git / ZIP repository source. */
  method?: AuthMethod | RepositoryKind;
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
  /** Where the definitions come from: the live workspace, or one environment's repository. */
  sourceKind?: SourceKind;
  repository?: RepositoryInfo | null;
}

export type SourceKind = "workspace" | RepositoryKind;

/** A subscription the signed-in account can see. */
export interface AzureSubscription {
  id: string;
  name: string;
  tenantId: string;
  /** The directory it belongs to, by name. */
  tenantName?: string;
  state: string;
}

/** A directory (tenant) the signed-in account belongs to. */
export interface AzureDirectory {
  id: string;
  name: string;
  domain: string;
  subscriptions: number;
  /** Its policy wants its own sign-in before it shows anything. */
  needsSignIn: boolean;
  error?: string;
}
export type RepositoryKind = "git" | "zip";

/** A Git or ZIP source, as connected: never a credential. */
export interface RepositoryInfo {
  kind: RepositoryKind;
  /** The repository URL, or the ZIP's file name. */
  label: string;
  environment: string;
  /** The folder the Synapse definitions are in, relative to the repository or ZIP root. */
  rootFolder: string;
  ref?: string | null;
  commit?: string | null;
  parametersName?: string | null;
  /** Parameters written into linked services, and those that matched no property. */
  applied: string[];
  unmatched: string[];
  /** Parameters whose secret values were kept off the disk: masked in what was written, or left out. */
  secrets?: string[];
  /** Definition files per Synapse folder. */
  counts: Record<string, number>;
  artifacts: number;
}

/** What the backend found in an uploaded ZIP. */
export interface ZipUpload {
  uploadId: string;
  fileName: string;
  rootFolder: string | null;
  counts: Record<string, number>;
  note?: string | null;
}

/** A repository connection request. `parameters` is the environment's parameters file text. */
export interface RepositoryConfig {
  kind: RepositoryKind;
  environment: string;
  repositoryUrl?: string;
  ref?: string;
  rootFolder?: string;
  uploadId?: string;
  fileName?: string;
  parameters?: string;
  parametersName?: string;
  subscriptionId?: string;
  resourceGroup?: string;
  workspace?: string;
  sqlPool?: string;
}

export interface Health {
  status: "ok";
  capabilities: { authMethods: AuthMethod[]; authMethodDetails?: AuthMethodDetail[]; discoveryScope: string[]; migratableTypes?: string[]; sourceKinds?: SourceKind[] };
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
  /** Loaded when the API started, from the last session: the source it came from is not signed in yet. */
  restored?: boolean;
  restoredFrom?: { workspace: string | null; pool: string | null } | null;
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
/** A storage location the plan's code reads directly, and the connection a OneLake shortcut to it can use. */
export interface StorageSuggestion {
  account: string;
  container: string;
  /** How many times the code names it literally (0: only built from variables). */
  uses: number;
  /** A Fabric connection already there that reaches it, or null. */
  connectionId: string | null;
  connectionName: string | null;
  /** Otherwise the Synapse linked service whose connection the Connections stage creates in the same run. */
  linkedService: string | null;
}

/** One storage location the run sends through a OneLake shortcut (run option). */
export interface StorageRoute {
  account: string;
  container: string;
  connectionId?: string;
  linkedService?: string;
}

export interface PlanAnalysis {
  /** Storage the plan's code reads directly; route each through a OneLake shortcut in Stages & credentials. */
  storage?: StorageSuggestion[];
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
  /** How the plan stands against the approved migration brief; null when consultation is not served. */
  brief?: BriefCheck | null;
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
  /** An object already in Fabric: left as it is, or updated in place (views/procedures by CREATE OR ALTER,
   *  notebooks, pipelines, scripts and Spark jobs by replacing their definition). Tables are never replaced. */
  existing?: "skip" | "update";
  /** Storage read directly by code, routed through OneLake shortcuts (paths rewritten). Undefined: not chosen yet. */
  storageRoutes?: StorageRoute[];
  /** Table data: how far back each sync of a table with key columns re-reads. */
  syncOverlap?: "1h" | "1d" | "none";
  /** Table data: which rows each table loads, by "schema.table" (lowercase). A table not listed loads every row. */
  dataFilters?: Record<string, DataFilterInput>;
  /** After the run completes, commit what it created to the workspace's Fabric Git branch (auto) or not (off). */
  gitCommit?: "auto" | "off";
  /** What that commit takes: only what the run created or updated (run), or everything waiting in the workspace (all). */
  commitScope?: "run" | "all";
  /** A person signs off each wave before the next one starts (the brief's A4). */
  waveGate?: boolean;
}

// ---- consultation: the questions, the migration brief, the policy in force -------------------

export type ConsultQuestionType = "choice" | "multi" | "boolean" | "number" | "date" | "text" | "list" | "name" | "principalMap";
/** answered: by a person; assumed: its recommendation stands, nobody confirmed it; stale: what it rests on changed;
 *  parked: answered on a branch that no longer applies (kept, and back if it applies again). */
export type ConsultStatus = "answered" | "assumed" | "open" | "skipped" | "stale" | "parked" | "not_applicable";
/** Every source but an assumption is a person's action: their own answer, an accepted recommendation, a finding they
 *  took, or a proposal they confirmed (read from notes by the model or the rules, or from an imported questionnaire). */
export type AnswerSource = "person" | "recommendation" | "finding" | "ai" | "rules" | "import";
export type SettingStatus = "enforced" | "recorded" | "note";
export interface SettingDrive { setting: string; status: SettingStatus; applies: string }
export interface ConsultQuestionDef {
  id: string;
  section: string;
  type: ConsultQuestionType;
  text: string;
  help: string;
  options: { value: string; label: string; description: string }[];
  /** A multi question whose choices are a fact's list (the schemas and folders discovered). */
  optionsFrom: string | null;
  /** A principal map's rows: a fact's list (the SQL users discovered). */
  rowsFrom: string | null;
  required: boolean;
  optional: boolean;
  drives: SettingDrive[];
  min: number | null;
  max: number | null;
  maxLength: number;
  /** Why an answer to it can ask for an approver other than the submitter, or null. */
  separateApprover: string | null;
}
export interface ConsultCatalog {
  version: string;
  sections: { id: string; title: string }[];
  questions: ConsultQuestionDef[];
  conflicts: { id: string; questions: string[]; resolutions: { kind: "take" | "keep" | "change"; label: string; question: string | null }[] }[];
}
export interface FactChange { fact: string; description: string; before: unknown; after: unknown }
export interface ConsultQuestion {
  id: string;
  applies: boolean;
  status: ConsultStatus;
  value: unknown;
  display: string | null;
  source: AnswerSource | null;
  by: Actor | null;
  at: string | null;
  note: string | null;
  recommendation: { value: unknown; reason: string } | null;
  /** For a stale answer: the facts it rests on that changed since. */
  changed: FactChange[];
  conflicts: string[];
  choices: string[] | null;
  rows: string[] | null;
  evidence: { fact: string; description: string; value: unknown }[];
}
export interface ConsultConflict {
  id: string;
  status: "open" | "kept";
  message: string;
  questions: string[];
  evidence: { fact: string; description: string; value: unknown }[];
  reason: string | null;
  by: Actor | null;
  at: string | null;
  resolutions: { kind: "take" | "keep" | "change"; label: string; question: string | null }[];
}
export interface ConsultProposal {
  id: string;
  question: string;
  value: unknown;
  reason: string | null;
  source: "ai" | "rules" | "import";
  model: string | null;
  at: string;
  display: string;
  /** What the question holds now, for comparison. */
  current: string | null;
}
/** Whether an LLM call may be made now: consent (D1) and the budget (D2), against what has been spent. */
export interface LlmGate {
  purpose: "transform" | "consult";
  mode: "required" | "advisory";
  consent: boolean | null;
  liveConsent: boolean | null;
  budgetUsd: number | null;
  spentUsd: number;
  allowed: boolean;
  reason: string | null;
  code: string | null;
}
export interface BriefChange { question: string; text: string; before: string | null; after: string | null; kind: "added" | "removed" | "changed" }
export interface BriefSummary {
  draftHash: string;
  versions: number;
  /** The version submitted and waiting for a decision, if any. */
  pending: number | null;
  approved: number | null;
  approvedHash: string | null;
  approvedBy: Actor | null;
  approvedAt: string | null;
  /** The estate changed since the approved version was made: a run under a required consultation is refused. */
  stale: boolean;
  factChanges: FactChange[];
  /** The answers as they stand make the same brief as the approved one. */
  sameAsApproved: boolean;
  /** How the answers now differ from the approved version. */
  changes: BriefChange[];
}
export interface ConsultState {
  mode: "required" | "advisory";
  catalogVersion: string;
  snapshot: string | null;
  factsFingerprint: string;
  facts: { name: string; description: string; scope: "estate" | "environment"; value: unknown }[];
  questions: ConsultQuestion[];
  conflicts: ConsultConflict[];
  next: string | null;
  blockers: string[];
  submittable: boolean;
  progress: { applicable: number; answered: number; assumed: number; needsPerson: number; open: number; contradictions: number };
  seq: number;
  brief: BriefSummary;
  proposals: ConsultProposal[];
  llm: LlmGate;
  /** After an action: the questions it answered, the proposals it made, what could not be used, and a note. */
  accepted?: string[];
  answered?: string[];
  proposed?: string[];
  rejected?: { question: string; value: unknown; reason?: string | null; message: string }[];
  source?: string;
  message?: string | null;
}
export interface BriefDecision {
  question: string;
  section: string;
  sectionTitle: string;
  text: string;
  value: unknown;
  display: string;
  /** Nobody confirmed it: its recommendation stands. */
  assumed: boolean;
  source: AnswerSource | null;
  by: Actor | null;
  at: string | null;
  reason: string | null;
  drives: SettingDrive[];
}
export interface BriefContent {
  catalogVersion: string;
  factsFingerprint: string;
  facts: Record<string, unknown>;
  snapshot: string | null;
  decisions: BriefDecision[];
  settings: Record<string, unknown>;
  enforced: Record<string, unknown>;
  assumptions: string[];
  recordedOnly: { setting: string; value: unknown; status: SettingStatus; applies: string }[];
  keptContradictions: { conflict: string; message: string; reason: string; by: Actor | null; at: string | null }[];
  separateApprover: { question: string; reason: string }[];
}
export type BriefStatus = "submitted" | "approved" | "rejected" | "superseded";
export interface BriefVersion {
  version: number;
  hash: string;
  status: BriefStatus;
  submittedAt: string;
  submittedBy: Actor;
  note: string | null;
  decidedBy: Actor | null;
  decidedAt: string | null;
  comment: string | null;
  selfApproved: boolean | null;
  catalogVersion: string;
  factsFingerprint: string;
  snapshot: string | null;
  decisions: number;
  assumptions: number;
  separateApprover: string[];
}
export interface BriefView { draft: BriefContent; draftHash: string; submittable: boolean; blockers: string[]; versions: BriefVersion[]; summary: BriefSummary }
export interface AnswerPreview {
  question: string;
  value: unknown;
  preview: { opens: string[]; parks: string[]; revives: string[]; conflictsOpened: string[]; conflictsClosed: string[];
    settings: { setting: string; before: unknown; after: unknown }[] };
}
/** Where a plan or a run differs from the approved brief. */
export interface PlanDifference { option: string; setting: string | null; brief: unknown; plan: unknown; message: string }
/** How a plan stands against the approved brief (from the planner). */
export interface BriefCheck {
  mode: "required" | "advisory";
  version: number | null;
  hash?: string;
  stale: boolean;
  changes?: FactChange[];
  differences: PlanDifference[];
  /** A reason recorded when Plan was confirmed covers every difference. */
  explained: boolean;
}
/** The policy in force, as Plan pre-sets and Migrate enforces it. Null fields: the brief does not decide them. */
export interface ConsultPolicy {
  mode: "required" | "advisory";
  inForce: { version: number; hash: string; approvedBy: Actor | null; approvedAt: string | null; stale: boolean; changes: FactChange[];
    settings: Record<string, unknown> } | null;
  llm: LlmGate;
  runOptions?: Partial<RunOptions>;
  dataFilters?: Record<string, DataFilterInput>;
  storageRoutes?: "all" | "none" | null;
  /** The objects the brief leaves out of Add all, with why. */
  excluded?: Record<string, string>;
  signoff?: "never" | "overwrite" | "always" | null;
  names?: { warehouse: string | null; lakehouse: string | null };
  validate?: { probe: boolean; run: RunnableKind[] } | null;
  sqlUserMap?: Record<string, string>;
}
export interface ConsultExplanation { question: string; text: string; source: "ai" | "rules"; model: string | null; message?: string }

// ---- Git: the Synapse workspace's repository, and the Fabric workspace's branch ---------------

/** The Git repository a Synapse workspace is integrated with, as Azure reports it. Never a credential. */
export interface WorkspaceGit {
  workspace: string;
  configured: boolean;
  provider?: "AzureDevOps" | "GitHub" | string;
  repositoryUrl?: string;
  /** The collaboration branch: where the workspace's definitions are committed. */
  branch?: string | null;
  /** Without leading or trailing slashes; empty when the definitions are at the top of the repository. */
  rootFolder?: string;
  lastCommit?: string | null;
}

/** What a commit to Fabric Git did (after a run, or by hand). */
export interface GitCommitResult {
  state: "committing" | "committed" | "nothing" | "skipped" | "blocked" | "failed";
  message?: string;
  items?: number;
  /** Data-load pipelines left out: they are rebuilt per environment. */
  skipped?: string[];
  /** Other changes waiting in the workspace that the run did not make: left for a person, never committed by the run. */
  others?: string[];
  commit?: string | null;
  branch?: string | null;
  commitUrl?: string | null;
  pullRequestUrl?: string | null;
  at?: string;
  code?: string;
}

/** The target workspace's Fabric Git branch and what waits to be committed. */
export interface FabricGitState {
  state: "connected" | "not_connected" | "no_target" | "error";
  message?: string;
  workspace?: string;
  provider?: string | null;
  organization?: string | null;
  project?: string | null;
  repository?: string | null;
  branch?: string | null;
  folder?: string | null;
  head?: string | null;
  lastSync?: string | null;
  repositoryUrl?: string | null;
  commitUrl?: string | null;
  pullRequestUrl?: string | null;
  targetBranch?: string;
  uncommitted?: number;
  incoming?: number;
  conflicts?: number;
  /** Uncommitted data-load pipelines, which are never committed. */
  plumbing?: number;
  committing?: boolean;
  lastCommit?: GitCommitResult | null;
}

/** One table's date filter. Dates are fixed (YYYY-MM-DD), so every run of a plan loads the same rows. */
export interface DataFilterInput {
  /** The date column the range applies to; empty to load every row (a synced table may still sync). */
  column: string;
  /** Inclusive start date, or "". */
  from: string;
  /** Exclusive end date, or "". Not allowed for a table kept in sync. */
  to: string;
  /** After the first load, keep loading new and changed rows with the sync pipeline. */
  sync: boolean;
  /** The column that moves when a row is added or changed (e.g. modified_at). */
  changeColumn: string;
  /** Columns that identify a row, so a changed row replaces the old one. Empty: the table's own key. */
  keys: string[];
}

/** A discovered table a date filter can apply to. */
export interface FilterableTable {
  id: string;
  schema: string;
  name: string;
  /** "schema.table", lowercase: how its filter is addressed. */
  key: string;
  dateColumns: { name: string; type: string }[];
  /** The table's primary key (or first unique constraint) columns, if it has one. */
  keyColumns: string[];
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
export interface CredentialAuth {
  value: string;
  label: string;
  fields: string[];
  /** What must already be set up for an option that takes no secret (workspace identity, Key Vault). */
  hint?: string;
}
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
  /** Tables with a date or time column, which a date filter can apply to. */
  dataTables?: FilterableTable[];
}
/** Credentials by linked-service name. Held in memory only: never stored in the browser, never shown again. */
export type ConnectionCredentials = Record<string, Record<string, string>>;

export type ExecState = "idle" | "running" | "paused" | "completed";
/** A run's own status, finer than `state`: how it was left, and so what may be done with it. */
export type RunStatus = "queued" | "awaiting_approval" | "running" | "paused" | "completed" | "completed_with_failures"
  | "failed" | "cancelled" | "rejected";
/** Why a run is paused: each has its own way on. awaiting_wave_signoff: Resume, by a person, signs the next wave off. */
export type PausedReason = "by_person" | "halted_on_failures" | "needs_input" | "interrupted" | "awaiting_wave_signoff";
/** What kind of failure an object's error is (the backend classifies every one). */
export type FailureCategory = "TRANSIENT" | "AUTH" | "CAPACITY" | "CONFIG" | "UNSUPPORTED" | "DEPENDENCY" | "CONFLICT"
  | "INTERRUPTED" | "UNKNOWN";
/** Who did something: a person (the operator at the machine), the agent (an LLM draft) or the accelerator itself. */
export interface Actor { kind: "person" | "agent" | "system"; id: string }
/** The sign-off before a run writes to Fabric: asked of a person, or given by the accelerator itself. */
export interface RunApproval {
  policy: "never" | "overwrite" | "always";
  required: boolean;
  reason: string | null;
  subjectHash: string;
  requestedAt: string;
  requestedBy: Actor;
  decision: "approved" | "rejected" | null;
  by: Actor | null;
  at: string | null;
  comment: string | null;
  automatic: boolean;
}
/** One pass over a run's pending objects: Start, then each Resume and Retry. */
export interface RunPass { n: number; trigger: "start" | "resume" | "retry"; status: string; startedAt: string; finishedAt: string | null; by: Actor }
/** A job the run started in Fabric (a data pipeline's run), followed again after a restart rather than started twice. */
export interface RunExecution {
  id: string;
  kind: string;
  wave?: number;
  pipeline?: string;
  instance?: string;
  tables?: string[];
  status: string;
  startedAt: string;
  finishedAt: string | null;
  note?: string;
}
/** What the run sent to Fabric for an object, kept by its hash: a definition, or a SQL statement. */
export interface ArtifactPart { kind: "fabric" | "sql"; what: string; hash: string; bytes: number }
export interface ExecItem {
  id: string;
  name: string;
  type: string;
  /** The wave it was planned in. */
  wave?: number;
  step: string;
  /** SKIPPED: already in Fabric, left unchanged. DEFERRED: not migrated in this session; error says why.
   *  BLOCKED: not started, because something in the run it needs failed. */
  status: "PENDING" | "IN PROGRESS" | "COMPLETED" | "FAILED" | "SKIPPED" | "DEFERRED" | "BLOCKED";
  startedAt: string | null;
  completedAt: string | null;
  error: string | null;
  /** What changed on the way (types mapped, settings dropped). */
  notes?: string[];
  /** Where it landed in Fabric. */
  target?: string | null;
  /** Created from an accepted LLM-assisted transform (reviewed in Assess → LLM transforms). */
  llm?: { acceptedAt: string | null; edited: boolean } | null;
  /** What kind of failure `error` is, and so what may be done about it. */
  category?: FailureCategory | null;
  /** How many passes have started on it. */
  attempts?: number;
  /** An earlier attempt may have made it in Fabric (the accelerator stopped under it): checked there first. */
  maybeCreated?: boolean;
  /** What was sent to Fabric for it. */
  artifact?: { parts: ArtifactPart[] } | null;
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
  /** The commit to Fabric Git once the run completed; null until it is tried. */
  git?: GitCommitResult | null;
  items: ExecItem[];
  logs: string[];
  // What a backend that keeps a record of each run adds. Older backends (and an idle run) leave them out.
  status?: RunStatus;
  pausedReason?: PausedReason | null;
  /** Connections whose credentials it needs again (lost with a restart: they are never kept), by name. */
  needsInput?: string[];
  blocked?: number;
  passes?: RunPass[];
  /** Changes with every change to the run: sent back with a control, so a stale page is refused. */
  version?: number;
  createdAt?: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  closedAt?: string | null;
  requestedBy?: Actor;
  approval?: RunApproval | null;
  resumable?: boolean;
  unresumableReason?: string | null;
  executions?: RunExecution[];
  /** The waves a person has signed off, when each wave is signed off (the brief's A4). */
  releasedWaves?: number[];
  /** The wave a paused run waits to start until a person signs it off. */
  waitingWave?: number | null;
  /** The migration brief it started under (version and hash), and how it differs from it. */
  brief?: { version: number | null; hash?: string; differences?: PlanDifference[] } | null;
}

/** One run in the project's run history. */
export interface RunSummary {
  runId: string;
  status: RunStatus;
  pausedReason?: PausedReason | null;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  closedAt?: string | null;
  workspace?: string | null;
  warehouse?: string | null;
  requestedBy?: Actor;
  counts?: Record<string, number>;
  approval?: RunApproval | null;
  passes: number;
}
export interface RunHistory { runs: RunSummary[]; current: string | null }

/** A project: one migration engagement. The backend keeps its discovery, runs, plan versions and audit trail. */
export interface Project { id: string; name: string; createdAt: string; updatedAt?: string }
export interface ProjectListing {
  active: string;
  projects: Project[];
  /** Sent back with a change, so one made from an older list is refused. */
  version: number;
  /** False with MIGRATION_STATE=off: nothing outlives the backend process. */
  persistent?: boolean;
  created?: string;
}

/** One record of the audit trail: who did what, to what, when, and how it ended. */
export interface AuditRecord {
  seq: number;
  at: string;
  action: string;
  actor: Actor;
  target: { type: string; id: string | null } | null;
  outcome: "done" | "failed" | "refused";
  code: string | null;
  reason: string | null;
  details: Record<string, unknown>;
  correlationId: string | null;
  project: string | null;
  run: string | null;
  prev: string;
  hash: string;
}
export interface AuditQuery { before?: number; limit?: number; action?: string; actor?: string; outcome?: string; run?: string }
export interface AuditPage {
  records: AuditRecord[];
  /** Pass as `before` to read further back; null at the start of the trail. */
  next: number | null;
  matched: number;
  /** What each action means, for the page. */
  catalog: Record<string, string>;
}
export interface AuditVerification { ok: boolean; records: number; brokenAt: number | null; problem: string | null; head: string }

export type ValidationStatus = "MATCH" | "REVIEW" | "MISMATCH";
export interface ValidationRow {
  category: string;
  object: string;
  source: string;
  target: string;
  status: ValidationStatus;
  /** Why it matched, differs or could not be checked. */
  detail?: string;
  /** Whether it runs in Fabric: present when a run check applies to the object. A FAIL makes the row a MISMATCH. */
  runtime?: RuntimeCheck;
}

export type RuntimeStatus = "PASS" | "FAIL" | "NOT RUN" | "NOT CHECKED";
export interface RuntimeCheck {
  status: RuntimeStatus;
  /** What was done: "Queried the view", "Compiled", "Last run in Fabric", "Ran now in Fabric"… */
  check: string;
  detail: string;
  runAt?: string | null;
}

/** Object kinds Validate may run for real in Fabric, when the operator asks. */
export type RunnableKind = "pipeline" | "notebook" | "script" | "sparkjob" | "procedure";
export interface ValidationRuntimeOptions {
  /** Read-only checks in the Warehouse: query tables and views, compile procedures. */
  probe: boolean;
  /** Run these now in Fabric (real runs) instead of reading their last run. */
  run: RunnableKind[];
  timeoutMinutes: number;
}

/** The single seam between the UI and whatever supplies data. */
export type RunControl = "pause" | "resume" | "retry" | "reset";
/** What a control may carry: the run's version (a stale page is refused), credentials a restart lost, and the comment
 *  a person signs the next wave off with. */
export interface RunControlExtra { version?: number; credentials?: ConnectionCredentials; comment?: string }

/* ---- LLM-assisted transformation ------------------------------------------- */

/** The Synapse object kinds a draft can be generated for. */
export type TransformKind = "procedure" | "view" | "table" | "script" | "pipeline" | "notebook" | "function";
export type TransformState = "none" | "queued" | "running" | "failed" | "draft" | "accepted";

export interface TransformSettings {
  /** Generation is allowed on this server (MIGRATION_LLM is not off). */
  enabled: boolean;
  /** The Claude Code CLI was found on the machine running the API. */
  available: boolean;
  model: string;
  timeoutSeconds: number;
  message: string | null;
  disabledReason: string | null;
  kinds: TransformKind[];
  promptsDir: string;
  storeDir: string;
  /** What the migration brief allows: consent to send definitions (D1), the budget (D2) and what is spent. */
  consent?: Pick<LlmGate, "allowed" | "reason" | "code" | "consent" | "budgetUsd" | "spentUsd">;
}

export interface TransformCheck {
  level: "ok" | "warning" | "error";
  message: string;
}

export interface TransformDraft {
  code: string;
  notes: string[];
  unconverted: string[];
  confidence: "high" | "medium" | "low";
  checks: TransformCheck[];
  sourceHash: string;
  generatedAt: string;
  model: string;
  costUsd: number | null;
  durationMs: number;
  /** The source changed since this was generated; null when unknown. */
  stale: boolean | null;
}

export interface TransformAccepted extends Omit<TransformDraft, "costUsd" | "durationMs"> {
  acceptedAt: string;
  /** The reviewer changed the draft before accepting it. */
  edited: boolean;
}

export interface TransformObject {
  objectId: string;
  name: string | null;
  type: string | null;
  kind: TransformKind | null;
  state: TransformState;
  error: string | null;
  startedAt: string | null;
  draft: TransformDraft | null;
  accepted: TransformAccepted | null;
  /** Present on a single object, not in the list. */
  source?: {
    code: string;
    language: "sql" | "json";
    hash: string;
    ruleOutput: string;
    ruleNotes: string[];
    problems: string[];
  };
}

export interface TransformPrompt {
  objectId: string;
  prompt: string;
  characters: number;
  promptsDir: string;
}

export interface TransformQueued {
  objectId: string;
  state: "queued" | "rejected";
  error: string | null;
}

/** One row of the transformation work queue. */
export interface TransformCandidate {
  objectId: string;
  name: string;
  type: string;
  kind: TransformKind;
  /** What the rules cannot convert. Empty when the rules handle it alone. */
  problems: string[];
  /** The rules change the object on the way (they can convert it, with changes). */
  rulesChange: boolean;
  state: TransformState;
  error: string | null;
  confidence: "high" | "medium" | "low" | null;
  stale: boolean | null;
  /** Why no draft can be made (for example the definition was not kept), or null. */
  unavailable: string | null;
}

export interface TransformCandidates {
  items: TransformCandidate[];
  summary: { total: number; needsWork: number; drafts: number; accepted: number; working: number; failed: number };
}

export interface ProjectRequirements {
  text: string;
  path: string;
  placeholders: string[];
}

export interface MigrationApi {
  readonly mode: ApiMode;
  health(): Promise<Health>;
  getConnection(): Promise<ConnectionState>;
  authenticate(config: ConnectionConfig): Promise<ConnectionState>;
  testConnection(config: ConnectionConfig): Promise<ConnectionState>;
  /** Sign in to one more directory the account belongs to, chosen from `directories`. */
  authenticateDirectory(tenantId: string): Promise<ConnectionState>;
  /** Extract an uploaded ZIP of a Synapse repository on the backend. */
  uploadZip(file: File): Promise<ZipUpload>;
  /** Use one environment's definitions from Git or an uploaded ZIP, optionally with a live SQL pool. */
  connectRepository(config: RepositoryConfig): Promise<ConnectionState>;
  disconnect(): Promise<ConnectionState>;
  startDiscovery(): Promise<DiscoveryStatus>;
  getDiscoveryStatus(): Promise<DiscoveryStatus>;
  /** Forget the last discovery's results (the connection is kept). Refused while discovery runs. */
  resetDiscovery(): Promise<DiscoveryStatus>;
  getResults(query: ResultsQuery): Promise<ResultsPage>;
  getObject(id: string): Promise<ObjectDetail>;
  /** Dropdown contents. Each needs a completed sign-in. */
  /** The subscriptions the signed-in account can see, by name. */
  listSubscriptions(): Promise<AzureSubscription[]>;
  listResourceGroups(subscriptionId: string): Promise<string[]>;
  listWorkspaces(subscriptionId: string, resourceGroup: string): Promise<string[]>;
  listSqlPools(subscriptionId: string, resourceGroup: string, workspace: string): Promise<string[]>;
  /** The Git repository a Synapse workspace is integrated with, to fill in the Git source. */
  getWorkspaceGit(subscriptionId: string, resourceGroup: string, workspace: string): Promise<WorkspaceGit>;
  getDependencies(): Promise<DependencyGraph>;
  getComponents(): Promise<ComponentRow[]>;
  exportMetadata(): Promise<MetadataExport>;
  // The Fabric target, the planner, the run and validation.
  getFabricTarget(): Promise<FabricTarget>;
  authenticateFabric(config: FabricConfig): Promise<FabricTarget>;
  testFabric(config: FabricConfig): Promise<FabricTarget>;
  disconnectFabric(): Promise<FabricTarget>;
  /** The target workspace's Fabric Git branch, its uncommitted changes and the pull-request link. */
  getFabricGit(): Promise<FabricGitState>;
  /** Commit the migrated items to that branch (in the background; poll `getFabricGit`). */
  commitFabricGit(comment?: string): Promise<FabricGitState>;
  getCapabilities(): Promise<Capabilities>;
  /** Planning is told which linked services have credentials (`credentialNames`), never what they are: those travel once, with the run. */
  analyzePlan(items: PlanItem[], record?: boolean, options?: RunOptions, credentialNames?: string[]): Promise<PlanAnalysis>;
  /** `deviationReason`: why the run differs from the approved brief, when it does and no reason was recorded in Plan. */
  startExecution(items: PlanItem[], options?: RunOptions, credentials?: ConnectionCredentials, deviationReason?: string): Promise<ExecutionRun>;
  getExecution(): Promise<ExecutionRun>;
  /** `reset` closes the run (its record stays in the history) so the next run starts fresh; refused while it works. */
  controlExecution(action: RunControl, extra?: RunControlExtra): Promise<ExecutionRun>;
  /** A person signs a waiting run off, and it starts; or declines it. Credentials a restart lost travel with it. */
  approveExecution(decision: "approve" | "reject", comment?: string, extra?: { subjectHash?: string; credentials?: ConnectionCredentials }): Promise<ExecutionRun>;
  /** Every run of the project, newest first. */
  listRuns(): Promise<RunHistory>;
  /** One run as its record has it. */
  getRun(runId: string): Promise<ExecutionRun>;
  /** The last validation's results, if one finished (or failed) in this project. */
  getValidation(): Promise<{ state: string; rows?: ValidationRow[]; finishedAt?: string; error?: { code?: string; message?: string } }>;
  // Projects, which the backend owns, and each one's audit trail.
  listProjects(): Promise<ProjectListing>;
  createProject(name: string, options?: { id?: string; version?: number; activate?: boolean; copyFrom?: string }): Promise<ProjectListing>;
  renameProject(id: string, name: string, version?: number): Promise<ProjectListing>;
  activateProject(id: string, version?: number): Promise<ProjectListing>;
  deleteProject(id: string, version?: number): Promise<ProjectListing>;
  getAudit(query?: AuditQuery): Promise<AuditPage>;
  verifyAudit(): Promise<AuditVerification>;
  /** Where the trail downloads from (real), or its text (demo). */
  exportAudit(format: "csv" | "jsonl"): Promise<{ url?: string; text?: string; fileName: string }>;
  /** A person confirmed a step of the journey: recorded in the trail. */
  confirmStep(step: string, note?: string): Promise<void>;
  /** Compares Synapse with Fabric. With no items, every discovered object is checked. */
  runValidation(items?: PlanItem[], options?: Pick<RunOptions, "dataFilters">, runtime?: ValidationRuntimeOptions,
    onProgress?: (text: string) => void): Promise<ValidationRow[]>;
  // LLM-assisted transformation, through Claude Code on the machine running the API.
  getTransformSettings(): Promise<TransformSettings>;
  listTransforms(): Promise<TransformObject[]>;
  getTransform(id: string): Promise<TransformObject>;
  getTransformPrompt(id: string): Promise<TransformPrompt>;
  generateTransforms(ids: string[]): Promise<TransformQueued[]>;
  /** Accept the draft; with `code`, an edited version of it. */
  acceptTransform(id: string, code?: string): Promise<TransformObject>;
  /** `draft` drops the draft only; `all` goes back to the rule-based conversion. */
  discardTransform(id: string, scope: "draft" | "all"): Promise<TransformObject>;
  /** Every object a draft can be made for, those the rules cannot convert first. */
  listTransformCandidates(): Promise<TransformCandidates>;
  /** The project requirements every prompt carries (prompts/transform/project.md on the server). */
  getProjectRequirements(): Promise<ProjectRequirements>;
  saveProjectRequirements(text: string): Promise<ProjectRequirements>;
  // The consultation: questions, answers, the migration brief and the policy in force.
  getConsult(): Promise<ConsultState>;
  getConsultCatalog(): Promise<ConsultCatalog>;
  answerQuestion(question: string, value: unknown, extra?: { note?: string; seq?: number }): Promise<ConsultState>;
  /** What an answer would open, park, revive and move, without recording it. */
  previewAnswer(question: string, value: unknown): Promise<AnswerPreview>;
  skipQuestion(question: string): Promise<ConsultState>;
  withdrawAnswer(question: string): Promise<ConsultState>;
  /** Accept the recommendations as answers: every section's, or one's. */
  acceptRecommendations(section?: string): Promise<ConsultState>;
  resolveConflict(conflict: string, resolution: "take" | "keep", reason?: string): Promise<ConsultState>;
  getBrief(): Promise<BriefView>;
  submitBrief(note?: string): Promise<BriefView>;
  /** A person approves or rejects the version waiting for a decision. */
  decideBrief(version: number, decision: "approve" | "reject", comment?: string, hash?: string): Promise<BriefView>;
  /** Where a version downloads from (real), or its text (demo). Without a version: the one in force. */
  exportBrief(version?: number, format?: "md" | "json"): Promise<{ url?: string; text?: string; fileName: string }>;
  getConsultPolicy(): Promise<ConsultPolicy>;
  /** Confirm a plan that differs from the approved brief, with the reason (kept in the audit trail). */
  recordDeviation(items: PlanItem[], options: RunOptions, reason: string): Promise<{ differences: PlanDifference[]; recorded: boolean }>;
  /** Answers read from notes (by the model with consent, else by the rules), for a person to confirm. */
  proposeAnswers(text: string, useAi?: boolean): Promise<ConsultState>;
  decideProposals(ids: string[], decision: "confirm" | "reject"): Promise<ConsultState>;
  explainQuestion(question: string, useAi?: boolean): Promise<ConsultExplanation>;
  exportQuestionnaire(format: "md" | "json"): Promise<{ url?: string; text?: string; fileName: string }>;
  /** A questionnaire's answers, by question id: each proposed for a person to confirm. */
  importQuestionnaire(answers: Record<string, unknown>): Promise<ConsultState>;
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
