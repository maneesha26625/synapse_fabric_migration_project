import { feature, plural, type Facts } from "./facts";

/**
 * The consultation: the questions agreed with the client before anything moves, after the
 * consultant reference (scope, target, data, then questions per discovered workload, security,
 * validation and special features).
 *
 * Three rules keep it honest:
 *  - A question about a workload or feature is asked only when discovery found it (`when`).
 *  - What the configuration already says (the Fabric workspace, its capacity, who is signed in) is
 *    read, not asked (`resolve`).
 *  - Options are limited to what Fabric and this tool can actually do, and every question says
 *    whether its answer drives the run, makes the agent stop for a person, or is recorded in the
 *    brief for the team (`effect`).
 */

export type AnswerValue = string | string[];
export type Answers = Record<string, AnswerValue | undefined>;

export interface Option { value: string; label: string; description?: string }

export interface Recommendation { value: AnswerValue; reason: string }

/** run: the agent's run follows it. gate: the agent stops for a person on it. record: kept in the brief only. */
export type Effect = "run" | "gate" | "record";

export type TopicId = "scope" | "target" | "data" | "sql" | "spark" | "pipelines" | "security" | "validation" | "features" | "context";

export interface Topic { id: TopicId; title: string; summary: string }

export interface Question {
  id: string;
  topic: TopicId;
  text: string;
  help: string;
  kind: "single" | "multi" | "text";
  options?: (f: Facts) => Option[];
  required: boolean;
  effect: Effect;
  /** Asked only when this holds; otherwise the question does not apply and is not counted. */
  when?: (f: Facts, a: Answers) => boolean;
  /** An answer the configuration already gives: shown, never asked. */
  resolve?: (f: Facts) => Recommendation | null;
  recommend?: (f: Facts, a: Answers) => Recommendation | null;
  /** What the answer changes, in words. */
  applies: string;
  placeholder?: string;
}

export const TOPICS: Topic[] = [
  { id: "scope", title: "Scope and approach", summary: "What moves, in what order, and how much risk the client accepts." },
  { id: "target", title: "Target architecture", summary: "Where it lands in Fabric. Known settings are read from the configuration." },
  { id: "data", title: "Data migration and loading", summary: "How table rows reach Fabric and stay current." },
  { id: "sql", title: "SQL pool and SQL objects", summary: "Asked because discovery found a dedicated SQL pool or SQL code." },
  { id: "spark", title: "Spark pools and notebooks", summary: "Asked because discovery found Spark pools, notebooks or Spark jobs." },
  { id: "pipelines", title: "Pipelines, triggers and connections", summary: "Asked because discovery found pipelines, triggers or linked services." },
  { id: "security", title: "Security, networking and identity", summary: "Access, secrets, networks and sensitive data. Nothing here weakens a control." },
  { id: "validation", title: "Validation and cutover", summary: "When the migration counts as successful, and who says so." },
  { id: "features", title: "Special features", summary: "Asked only for features discovery found (or cannot see)." },
  { id: "context", title: "Context (notes only)", summary: "Who signs off, and anything the team should know." },
];

const stage = (f: Facts, key: string) => f.stages.find((s) => s.key === key);
const has = (f: Facts, ...types: string[]) => types.some((t) => (f.byType[t] ?? 0) > 0);
const count = (f: Facts, ...types: string[]) => types.reduce((n, t) => n + (f.byType[t] ?? 0), 0);
/** Stages the scope question chooses among. Table data has its own question under Data. */
const scopeStages = (f: Facts) => f.stagesWithObjects.filter((s) => s.key !== "data");
const SQL_TYPES = ["Table", "View", "Stored Procedure", "Function", "Dedicated SQL Pool"];
const hasSql = (f: Facts) => has(f, ...SQL_TYPES);
const distributed = (f: Facts) => Object.values(f.features?.distribution ?? {}).reduce((a, b) => a + b, 0);
const triggers = (f: Facts) => f.features?.triggers;

export const QUESTIONS: Question[] = [
  // ---- A. Scope and approach -------------------------------------------------------------
  {
    id: "scope.mode", topic: "scope", kind: "single", required: true, effect: "run",
    text: "What is the migration scope?",
    help: "A pilot moves the first dependency wave only (the objects nothing else needs first), so the approach can be proven before the rest. Business-critical objects are not known to discovery: pick them as a selection.",
    options: (f) => [
      { value: "all", label: "Migrate all discovered objects", description: `${plural(f.total, "object")} across ${plural(scopeStages(f).length, "stage")}.` },
      { value: "selected", label: "Migrate only selected kinds of objects", description: "Choose the kinds on the next question." },
      { value: "pilot", label: "Pilot: the first wave only", description: f.waves ? `Wave 1 of ${f.waves}.` : "The first dependency wave." },
    ],
    recommend: (f) => f.total > 2000 || f.waves > 6
      ? { value: "pilot", reason: `${plural(f.total, "object")} in ${plural(f.waves, "wave")}: prove the approach on the first wave before moving the rest.` }
      : { value: "all", reason: `${plural(f.total, "object")} in ${plural(f.waves || 1, "wave")} is a size one run handles, in dependency order.` },
    applies: "Which objects the agent puts in the plan.",
  },
  {
    id: "scope.stages", topic: "scope", kind: "multi", required: true, effect: "run",
    when: (_f, a) => a["scope.mode"] === "selected",
    text: "Which kinds of objects should this migration move?",
    help: "Only kinds discovery found are offered. Each becomes one stage of the run; a kind left out is listed in the migration report and not touched in Fabric.",
    options: (f) => scopeStages(f).map((s) => ({ value: s.key, label: s.label, description: `${s.summary} (${plural(s.types.reduce((n, t) => n + (f.byType[t] ?? 0), 0), "object")})` })),
    recommend: (f) => {
      const keys = scopeStages(f).map((s) => s.key);
      return keys.length ? { value: keys, reason: `Discovery found objects for ${plural(keys.length, "stage")}; untick what this migration leaves out.` } : null;
    },
    applies: "The run's stages and the objects added to the plan.",
  },
  {
    id: "scope.approach", topic: "scope", kind: "single", required: true, effect: "record",
    text: "What is the preferred migration approach?",
    help: "The run always creates objects in dependency-wave order. Cutting over wave by wave, or running Synapse and Fabric side by side, is organised by the team: the brief records it.",
    options: () => [
      { value: "bigbang", label: "Big bang", description: "Cut over all in-scope objects together." },
      { value: "phased", label: "Phased", description: "Cut over in dependency-aware waves." },
      { value: "parallel", label: "Parallel", description: "Run Synapse and Fabric together before cutover." },
    ],
    recommend: (f) => f.waves > 3 ? { value: "phased", reason: `${plural(f.waves, "wave")}: cutting over wave by wave limits what one problem can stop.` }
      : { value: "bigbang", reason: "Few waves: one cutover is simpler to run and to roll back." },
    applies: "The brief and the cutover plan.",
  },
  {
    id: "scope.downtime", topic: "scope", kind: "single", required: false, effect: "record",
    text: "What downtime is acceptable during cutover?",
    help: "Decides when the data load and the switch of consumers can happen.",
    options: () => [
      { value: "zero", label: "Near-zero downtime" }, { value: "1h", label: "Less than 1 hour" }, { value: "4h", label: "1–4 hours" },
      { value: "8h", label: "4–8 hours" }, { value: "window", label: "Flexible / maintenance window available" },
    ],
    applies: "The brief and the cutover plan.",
  },
  {
    id: "scope.priority", topic: "scope", kind: "single", required: false, effect: "record",
    text: "What is the primary migration priority?",
    help: "Guides trade-offs a person makes (for example capacity size, or how far to redesign).",
    options: () => [
      { value: "cost", label: "Minimize cost" }, { value: "performance", label: "Preserve or improve performance" },
      { value: "speed", label: "Complete the migration quickly" }, { value: "compatibility", label: "Maximize compatibility with the source" },
      { value: "balance", label: "Balance cost, performance and effort" },
    ],
    recommend: () => ({ value: "balance", reason: "Without a stated priority, a balanced approach avoids over-optimising one dimension." }),
    applies: "The brief.",
  },
  {
    id: "scope.environment", topic: "scope", kind: "single", required: true, effect: "record",
    text: "Which environment should be migrated first?",
    help: "The source connected now is the environment this migration reads.",
    options: () => [
      { value: "devtest", label: "Development / test first, then production" },
      { value: "pilot", label: "A controlled production pilot" },
      { value: "single", label: "Single environment only" },
    ],
    recommend: (f) => f.environment && /prod/i.test(f.environment)
      ? { value: "pilot", reason: `The connected source is the ${f.environment} environment.` }
      : { value: "devtest", reason: "Proving the migration on development first keeps production untouched until it works." },
    applies: "The brief.",
  },
  {
    id: "scope.rollback", topic: "scope", kind: "single", required: true, effect: "record",
    text: "What happens if migration or validation fails?",
    help: "This tool never changes or deletes anything in Synapse, and never deletes in Fabric, so Synapse stays a working fallback until cutover.",
    options: () => [
      { value: "fallback", label: "Keep Synapse as the fallback until validation passes" },
      { value: "repair", label: "Rerun or repair the migration" },
      { value: "formal", label: "Define formal rollback and recovery steps", description: "Recorded for the team to write." },
    ],
    recommend: () => ({ value: "fallback", reason: "Synapse is untouched by the migration, so it is the simplest rollback." }),
    applies: "The brief.",
  },

  // ---- B. Target architecture --------------------------------------------------------------
  {
    id: "target.workspace", topic: "target", kind: "single", required: true, effect: "gate",
    text: "Which Fabric workspace should receive the migrated objects?",
    help: "The run writes to the workspace connected on the Connections page. Creating a workspace needs a Fabric administrator.",
    resolve: (f) => f.fabricWorkspace ? { value: f.fabricWorkspace, reason: "The Fabric workspace connected on the Connections page." } : null,
    options: () => [
      { value: "existing", label: "An existing workspace", description: "Connect it on the Connections page before the run." },
      { value: "new", label: "A new workspace", description: "An administrator creates it first; the agent waits." },
    ],
    applies: "The agent waits until a Fabric workspace is connected.",
  },
  {
    id: "target.capacity", topic: "target", kind: "single", required: true, effect: "gate",
    text: "Which Fabric capacity should support the workload?",
    help: "A workspace needs a Fabric capacity before items such as a Warehouse can be created. Assigning one needs a capacity administrator.",
    resolve: (f) => f.capacityAssigned === true ? { value: "assigned", reason: "The connected workspace is on a Fabric capacity." } : null,
    options: () => [
      { value: "existing", label: "Assign an existing capacity" },
      { value: "provision", label: "Provision an approved capacity" },
      { value: "assess", label: "Assess the capacity needed first" },
    ],
    recommend: (f) => f.capacityAssigned === false ? { value: "existing", reason: "The connected workspace has no capacity: the run cannot create items until it has one." } : null,
    applies: "A prerequisite the agent asks a person to confirm before the run.",
  },
  {
    id: "target.sql", topic: "target", kind: "single", required: true, effect: "run",
    when: (f) => hasSql(f) && !!stage(f, "warehouse"),
    text: "Which Fabric destination should host the dedicated SQL pool workload?",
    help: "A Fabric Warehouse runs T-SQL tables, views, functions and procedures (this tool creates it). A Lakehouse suits lake-oriented, Spark-heavy work; its SQL analytics endpoint is read-only, so procedures that write must be redesigned. This tool does not convert SQL objects to a Lakehouse.",
    options: () => [
      { value: "warehouse", label: "Fabric Warehouse", description: "Created by the run." },
      { value: "split", label: "Split: Warehouse for SQL objects, Lakehouse redesign for lake-oriented data", description: "The Warehouse is created; the redesign is recorded for a person." },
      { value: "lakehouse", label: "Lakehouse redesign", description: "Not automated: the SQL objects are left out of the run." },
    ],
    recommend: (f) => {
      const code = count(f, "Stored Procedure", "View", "Function");
      return code > 0
        ? { value: "warehouse", reason: `${plural(code, "procedure, view or function", "procedures, views and functions")}: a SQL-centric workload the Warehouse runs as it is.` }
        : { value: "split", reason: "Tables without SQL code: the Warehouse holds them; lake-oriented use can move to a Lakehouse later." };
    },
    applies: "Whether the Warehouse & schema stage (and what depends on it) runs.",
  },
  {
    id: "target.collation", topic: "target", kind: "single", required: true, effect: "run",
    when: (f, a) => hasSql(f) && !!stage(f, "warehouse") && a["target.sql"] !== "lakehouse",
    text: "Which collation should the Fabric Warehouse be created with?",
    help: "A Warehouse's collation is set once, when it is created, and cannot change afterwards.",
    options: () => [
      { value: "match_synapse", label: "Same as Synapse", description: "Case-sensitive only if the Synapse pool was." },
      { value: "case_insensitive", label: "Case-insensitive" },
      { value: "case_sensitive", label: "Case-sensitive (Fabric's default)" },
    ],
    recommend: () => ({ value: "match_synapse", reason: "Queries and reports then return what they did in Synapse." }),
    applies: "Run option: Warehouse collation.",
  },
  {
    id: "target.names", topic: "target", kind: "single", required: true, effect: "record",
    text: "Should source schemas and object names be preserved?",
    help: "The run keeps every schema and object name. Renaming is done by a person afterwards (and every reference to the name must follow).",
    options: () => [
      { value: "preserve", label: "Preserve names", description: "What the run does." },
      { value: "standards", label: "Apply naming standards afterwards", description: "Recorded for the team." },
    ],
    recommend: () => ({ value: "preserve", reason: "Keeps every query, report and pipeline that names an object working." }),
    applies: "The brief.",
  },
  {
    id: "target.layers", topic: "target", kind: "single", required: false, effect: "record",
    text: "Should the target follow a Bronze/Silver/Gold (medallion) architecture?",
    help: "The run recreates the existing logical layers. Restructuring into medallion layers is a redesign the brief records.",
    options: () => [
      { value: "retain", label: "No, retain the existing logical layers" },
      { value: "medallion", label: "Yes, restructure afterwards", description: "Recorded for the team." },
    ],
    recommend: () => ({ value: "retain", reason: "Migrate first, restructure once the result is validated." }),
    applies: "The brief.",
  },
  {
    id: "target.environments", topic: "target", kind: "single", required: true, effect: "record",
    text: "Are separate development, test and production workspaces required?",
    help: "With Fabric Git integration, what the run creates in the development workspace is committed and promoted to Test and Prod through pull requests and deployment.",
    options: () => [
      { value: "separate", label: "Yes, separate workspaces" },
      { value: "single", label: "No, one workspace" },
      { value: "existing", label: "Follow the existing workspace strategy" },
    ],
    recommend: (f) => f.fabricGitConnected
      ? { value: "separate", reason: "The target workspace is connected to Git, ready for promotion to Test and Prod." }
      : { value: "existing", reason: "The target workspace is not connected to Git; follow the strategy already in place." },
    applies: "The brief.",
  },
  {
    id: "target.gitCommit", topic: "target", kind: "single", required: true, effect: "run",
    text: "After the run, should what it created be committed to the Fabric workspace's Git branch?",
    help: "Commits record the migrated items in the workspace's repository, ready for a pull request.",
    options: () => [
      { value: "auto", label: "Commit after the run" },
      { value: "off", label: "Don't commit", description: "Commit later from the Migration tab." },
    ],
    recommend: (f) => f.fabricGitConnected
      ? { value: "auto", reason: "The target workspace is connected to Git." }
      : { value: "off", reason: "The target workspace is not connected to Git (or not read yet)." },
    applies: "Run option: commit to Fabric Git.",
  },
  {
    id: "target.existing", topic: "target", kind: "single", required: true, effect: "run",
    text: "If an object already exists in the Fabric workspace, what should happen?",
    help: "Re-running meets what an earlier run created. Updating replaces views, procedures, notebooks, pipelines and scripts in place; tables are never replaced. (Separate copies, or a decision per conflict, are not something the run can do.)",
    options: () => [
      { value: "skip", label: "Reuse it as it is", description: "Safe to re-run; changes made in Fabric are kept." },
      { value: "update", label: "Update it from Synapse", description: "Overwrites changes made in Fabric since." },
    ],
    recommend: () => ({ value: "skip", reason: "Nothing made by hand in Fabric is overwritten, and the run can be repeated safely." }),
    applies: "Run option: objects already in Fabric.",
  },

  // ---- C. Data migration and loading ---------------------------------------------------------
  {
    id: "data.load", topic: "data", kind: "single", required: true, effect: "run",
    when: (f, a) => f.tables > 0 && a["target.sql"] !== "lakehouse",
    text: "How should the table data be loaded?",
    help: "Rows are copied by Fabric data pipelines reading the SQL pool. A dedicated SQL pool has no change data capture or change tracking, so keeping tables current works through a date/time change column (chosen per table on the Plan tab); tables without one are reloaded.",
    options: () => [
      { value: "full", label: "Full load once" },
      { value: "sync", label: "Full load, then keep tables in sync by a change column" },
      { value: "create", label: "Create the load pipelines only", description: "Run them from Fabric or a schedule when you choose." },
      { value: "none", label: "Schema only", description: "Tables are created empty." },
    ],
    recommend: (f) => f.sqlPool
      ? { value: "full", reason: `The SQL pool ${f.sqlPool} is connected, so ${plural(f.tables, "table")} can be loaded and their row counts compared.` }
      : { value: "none", reason: "No SQL pool is connected, so there are no rows to read." },
    applies: "The Table data stage, and what it does.",
  },
  {
    id: "data.frequency", topic: "data", kind: "single", required: true, effect: "record",
    when: (_f, a) => a["data.load"] === "sync",
    text: "How often must new data arrive in Fabric?",
    help: "Sync schedules are created switched off and turned on at cutover. Near-real-time needs a different design (for example Real-Time Intelligence).",
    options: () => [
      { value: "minutes", label: "Every few minutes" }, { value: "hourly", label: "Hourly" }, { value: "daily", label: "Daily" },
      { value: "weekly", label: "Weekly" }, { value: "demand", label: "On demand" },
    ],
    recommend: () => ({ value: "daily", reason: "Matches most Synapse batch loads." }),
    applies: "The brief and the schedules set up at cutover.",
  },
  {
    id: "data.sync", topic: "data", kind: "single", required: false, effect: "run",
    when: (_f, a) => a["data.load"] === "sync",
    text: "For tables kept in sync, how far back should each sync re-read?",
    help: "Re-reading a window of recent changes catches rows committed late.",
    options: () => [{ value: "1h", label: "One hour" }, { value: "1d", label: "One day" }, { value: "none", label: "No overlap" }],
    recommend: () => ({ value: "1h", reason: "Catches late rows without re-reading much." }),
    applies: "Run option: re-read window for tables kept in sync.",
  },
  {
    id: "data.existingRows", topic: "data", kind: "single", required: true, effect: "run",
    when: (f, a) => f.tables > 0 && !!a["data.load"] && a["data.load"] !== "none",
    text: "If a Fabric table already has rows, what should the load do?",
    help: "The migration copies one way, from Synapse to Fabric: it never merges or resolves record conflicts.",
    options: () => [
      { value: "if_empty", label: "Skip it", description: "Never touches data already there, so re-runs are safe." },
      { value: "replace", label: "Replace it", description: "Empties the table (TRUNCATE) before loading again." },
    ],
    recommend: () => ({ value: "if_empty", reason: "Re-running never loses or doubles rows." }),
    applies: "Run option: if a table already has rows.",
  },
  {
    id: "data.history", topic: "data", kind: "single", required: false, effect: "record",
    when: (f, a) => f.tables > 0 && !!a["data.load"] && a["data.load"] !== "none",
    text: "Must all historical records be migrated?",
    help: "A date filter per table loads only recent rows; it is set on the Plan tab (Table data).",
    options: () => [
      { value: "all", label: "Migrate all history" },
      { value: "limited", label: "Only a recent period", description: "Set a date filter per table on the Plan tab." },
      { value: "policy", label: "Follow the existing retention policy" },
    ],
    recommend: () => ({ value: "all", reason: "Without a retention rule, moving everything keeps reports complete." }),
    applies: "The brief; date filters are set on the Plan tab.",
  },
  {
    id: "data.schemaChanges", topic: "data", kind: "single", required: false, effect: "record",
    when: (f) => f.tables > 0,
    text: "Are schema changes expected in Synapse after migration starts?",
    help: "A change after discovery is not in the plan: discovery must run again and the plan be rebuilt.",
    options: () => [
      { value: "freeze", label: "No: the schema is frozen until cutover" },
      { value: "approve", label: "Yes, each change needs approval and a new discovery" },
    ],
    recommend: () => ({ value: "freeze", reason: "A frozen schema keeps the plan and the validation true." }),
    applies: "The brief.",
  },
  {
    id: "data.sourceAvailability", topic: "data", kind: "single", required: false, effect: "record",
    text: "Should the source remain available during the migration?",
    help: "The migration only reads from Synapse; it never stops or changes it.",
    options: () => [
      { value: "untilCutover", label: "Until final validation and cutover" },
      { value: "yes", label: "Yes, indefinitely" },
      { value: "no", label: "No" },
    ],
    recommend: () => ({ value: "untilCutover", reason: "Synapse stays the fallback until Fabric is validated." }),
    applies: "The brief.",
  },

  // ---- SQL pool and SQL objects (only when discovered) -------------------------------------
  {
    id: "sql.distribution", topic: "sql", kind: "single", required: false, effect: "record",
    when: (f, a) => distributed(f) > 0 && a["target.sql"] !== "lakehouse",
    text: "Distribution and index settings: how should they be treated?",
    help: "A Fabric Warehouse has no distribution or index options: it manages storage itself, so the run drops DISTRIBUTION, CLUSTERED COLUMNSTORE and partition clauses. The only choice is whether to review performance-sensitive tables.",
    options: (f) => [
      { value: "automatic", label: "Rely on Fabric's automatic optimisation", description: "What the run does." },
      { value: "review", label: `Also flag the ${plural(f.features?.distribution?.hash ?? 0, "hash-distributed table")} for a performance review` },
    ],
    recommend: (f) => ({ value: "automatic", reason: `${plural(distributed(f), "table")} with a distribution policy; Fabric needs none, and a review is only worth it for known slow queries.` }),
    applies: "The brief (the run drops the settings either way).",
  },
  {
    id: "sql.unsupported", topic: "sql", kind: "single", required: true, effect: "run",
    when: (f) => f.noAutomaticRoute > 0 || has(f, "Stored Procedure", "Function", "SQL Script", "View"),
    text: "What should happen to objects the rules cannot convert (classified Manual or Review)?",
    help: "Synapse-only T-SQL with a known Fabric form is converted by rules. The rest can be drafted with Claude on the Assessment tab (LLM transforms) and accepted by a person before the run; drafts are never used unaccepted.",
    options: () => [
      { value: "automated", label: "Exclude them from the run and list them for a person" },
      { value: "all", label: "Keep them in the run's record as deferred, with the reason" },
      { value: "block", label: "Stop for approval before the run", description: "The agent lists them and waits for a person." },
    ],
    recommend: (f) => ({
      value: "automated",
      reason: f.noAutomaticRoute ? `${plural(f.noAutomaticRoute, "object")} ha${f.noAutomaticRoute === 1 ? "s" : "ve"} no automatic route; a person (or an accepted draft) handles them.` : "Nothing discovered lacks an automatic route today.",
    }),
    applies: "Run option: scope; with 'Stop for approval', an approval gate.",
  },
  {
    id: "sql.dynamic", topic: "sql", kind: "single", required: true, effect: "record",
    when: (f) => feature(f, "dynamicSqlObjects") > 0,
    text: "How should SQL that builds statements at run time (dynamic SQL) be handled?",
    help: "Dynamic SQL (EXEC, sp_executesql) cannot be checked by reading it: what it runs is only known at run time.",
    options: () => [
      { value: "review", label: "Convert it, and have a person review each one" },
      { value: "exclude", label: "Exclude it until it has been assessed" },
    ],
    recommend: (f) => ({ value: "review", reason: `${plural(feature(f, "dynamicSqlObjects"), "object")} use dynamic SQL; converting with a review keeps them moving without trusting them blindly.` }),
    applies: "The brief.",
  },
  {
    id: "sql.crossDatabase", topic: "sql", kind: "single", required: true, effect: "record",
    when: (f) => feature(f, "crossDatabaseObjects") > 0,
    text: "How should references to other databases be handled?",
    help: "A Fabric Warehouse can query other warehouses and lakehouses in the same workspace by three-part names. References to databases that do not move must be redesigned.",
    options: () => [
      { value: "map", label: "Map them to Fabric items in the same workspace", description: "Reviewed by a person." },
      { value: "redesign", label: "Redesign them" },
    ],
    recommend: (f) => ({ value: "map", reason: `${plural(feature(f, "crossDatabaseObjects"), "object")} name another database.` }),
    applies: "The brief.",
  },
  {
    id: "sql.keys", topic: "sql", kind: "single", required: false, effect: "record",
    when: (f, a) => (feature(f, "identityTables") > 0 || feature(f, "primaryKeyTables") > 0) && a["target.sql"] !== "lakehouse",
    text: "IDENTITY columns and keys: is the way the run keeps them acceptable?",
    help: "The run keeps IDENTITY as bigint IDENTITY (Fabric chooses the seed and increment; existing values are loaded with IDENTITY_INSERT and reseeded), and keeps primary keys and unique constraints as NOT ENFORCED, as in Synapse. Sequences and foreign keys are not read by discovery.",
    options: (f) => [
      { value: "accept", label: "Yes, keep them as the run does", description: `${plural(feature(f, "identityTables"), "table")} with IDENTITY, ${plural(feature(f, "primaryKeyTables"), "table")} with a primary key.` },
      { value: "review", label: "Review the tables that depend on exact identity values" },
    ],
    recommend: () => ({ value: "accept", reason: "Existing key values are kept exactly; only new values follow Fabric's own sequence." }),
    applies: "The brief.",
  },
  {
    id: "sql.sla", topic: "sql", kind: "single", required: false, effect: "record",
    when: (f) => has(f, "Stored Procedure", "View"),
    text: "Are there performance-critical queries that must meet an SLA?",
    help: "Validation checks results, not speed: benchmarking is done by the team.",
    options: () => [
      { value: "thresholds", label: "Yes, with thresholds to record" }, { value: "benchmark", label: "Benchmark source and target" }, { value: "none", label: "No specific SLA" },
    ],
    applies: "The brief.",
  },

  // ---- Spark pools and notebooks -------------------------------------------------------------
  {
    id: "spark.compute", topic: "spark", kind: "single", required: true, effect: "run",
    when: (f) => has(f, "Spark Pool") && !!stage(f, "spark"),
    text: "Which Fabric compute should run the migrated Spark workload?",
    help: "The run can create a custom Fabric Spark pool and a published Environment from each Synapse pool, or the notebooks can run on the workspace's starter pool.",
    options: (f) => [
      { value: "custom", label: "Custom pools and Environments from the Synapse pools", description: `${plural(count(f, "Spark Pool"), "pool")}.` },
      { value: "starter", label: "The workspace's starter pool", description: "Spark pools are left out of the run." },
    ],
    recommend: (f) => ({ value: "custom", reason: `${plural(count(f, "Spark Pool"), "Spark pool")} with node sizes and settings the notebooks were written for.` }),
    applies: "The Spark pool & environment stage.",
  },
  {
    id: "spark.libraries", topic: "spark", kind: "single", required: true, effect: "record",
    when: (f) => feature(f, "customLibraries") > 0 || feature(f, "notebooksInstallingPackages") > 0,
    text: "Custom libraries were found. How should they be handled?",
    help: "Libraries on Synapse pools belong in the Fabric Environment; packages installed inside notebooks keep installing there. Compatibility with the Fabric runtime is checked by a person.",
    options: () => [
      { value: "environment", label: "Carry them into the Fabric Environment, then test" },
      { value: "review", label: "Review each for compatibility first" },
    ],
    recommend: (f) => ({ value: "environment", reason: `${plural(feature(f, "customLibraries"), "library or pool requirement", "libraries or pool requirements")}, ${plural(feature(f, "notebooksInstallingPackages"), "notebook")} installing packages.` }),
    applies: "The brief.",
  },
  {
    id: "spark.storage", topic: "spark", kind: "single", required: true, effect: "run",
    when: (f) => feature(f, "notebooksWithStoragePaths") > 0,
    text: "Notebooks read external storage directly. How should that access work in Fabric?",
    help: "Routing through OneLake shortcuts rewrites the paths in the code to the shortcut, using a Fabric connection. Direct paths need the Fabric identity to have access to the storage account.",
    options: () => [
      { value: "shortcuts", label: "Route through OneLake shortcuts", description: "Paths rewritten by the run." },
      { value: "direct", label: "Keep the direct storage paths", description: "Access is granted by an administrator." },
    ],
    recommend: (f) => ({ value: "shortcuts", reason: `${plural(feature(f, "notebooksWithStoragePaths"), "notebook")} read storage directly; shortcuts keep that access governed in OneLake.` }),
    applies: "Run option: storage routed through OneLake shortcuts.",
  },
  {
    id: "spark.secrets", topic: "spark", kind: "single", required: true, effect: "record",
    when: (f) => feature(f, "notebooksWithSecrets") > 0,
    text: "Notebooks read secrets (Key Vault, credentials utilities). How should they be updated?",
    help: "Fabric notebooks can read Key Vault secrets with notebookutils.credentials when the running identity has access. Nothing is copied or exposed by the migration.",
    options: () => [
      { value: "keyvault", label: "Keep reading the same Key Vault, with access granted to the Fabric identity" },
      { value: "review", label: "Review each notebook" },
    ],
    recommend: (f) => ({ value: "keyvault", reason: `${plural(feature(f, "notebooksWithSecrets"), "notebook")} read secrets; the vault stays the single place they live.` }),
    applies: "The brief.",
  },
  {
    id: "spark.utils", topic: "spark", kind: "single", required: false, effect: "record",
    when: (f) => feature(f, "mssparkutilsNotebooks") > 0,
    text: "Notebooks use mssparkutils. How should those calls be handled?",
    help: "Fabric notebooks still accept mssparkutils for compatibility; notebookutils is its current name there. Calls that depend on Synapse-only resources need a person.",
    options: () => [
      { value: "keep", label: "Keep them and test" },
      { value: "rename", label: "Move them to notebookutils during review" },
    ],
    recommend: (f) => ({ value: "keep", reason: `${plural(feature(f, "mssparkutilsNotebooks"), "notebook")} use it; it keeps working, so renaming can wait.` }),
    applies: "The brief.",
  },
  {
    id: "spark.jobs", topic: "spark", kind: "single", required: true, effect: "run",
    when: (f) => has(f, "Spark Job Definition") && !!stage(f, "spark_jobs"),
    text: "How should Spark job definitions be migrated?",
    help: "They can be recreated as Fabric Spark job definitions; their schedules come from the pipelines or triggers that start them.",
    options: () => [
      { value: "recreate", label: "Recreate them as Fabric Spark job definitions" },
      { value: "skip", label: "Leave them for a redesign" },
    ],
    recommend: () => ({ value: "recreate", reason: "A direct Fabric equivalent exists." }),
    applies: "The Spark job definitions stage.",
  },

  // ---- Pipelines, triggers and connections ---------------------------------------------------
  {
    id: "pipelines.migrate", topic: "pipelines", kind: "single", required: true, effect: "run",
    when: (f) => has(f, "Pipeline") && !!stage(f, "pipelines"),
    text: "Should each Synapse pipeline become a Fabric Data Factory pipeline?",
    help: "Pipelines are recreated one to one, with their parameters and dependencies; datasets are folded into the pipelines that use them.",
    options: () => [
      { value: "migrate", label: "One to one, where supported" },
      { value: "redesign", label: "Leave them for a redesign", description: "Pipelines and their schedules are left out of the run." },
    ],
    recommend: (f) => ({ value: "migrate", reason: `${plural(count(f, "Pipeline"), "pipeline")} with a direct Fabric equivalent.` }),
    applies: "The Pipelines & datasets stage.",
  },
  {
    id: "pipelines.unsupported", topic: "pipelines", kind: "single", required: true, effect: "record",
    when: (f, a) => (f.features?.unsupportedActivities?.activities ?? 0) > 0 && a["pipelines.migrate"] !== "redesign",
    text: "Some pipeline activities have no Fabric equivalent. How should they be handled?",
    help: "Discovery found activity types the mapping does not recognise; a person replaces each with a supported activity, a notebook, or custom logic.",
    options: (f) => [
      { value: "flag", label: "Migrate the pipelines and flag those activities", description: `${plural(f.features?.unsupportedActivities?.activities ?? 0, "activity", "activities")} in ${plural(f.features?.unsupportedActivities?.pipelines ?? 0, "pipeline")}.` },
      { value: "hold", label: "Hold those pipelines back until redesigned" },
    ],
    recommend: () => ({ value: "flag", reason: "The rest of each pipeline moves; only the flagged activities wait for a person." }),
    applies: "The brief.",
  },
  {
    id: "pipelines.triggers", topic: "pipelines", kind: "single", required: true, effect: "run",
    when: (f, a) => has(f, "Trigger") && !!stage(f, "schedules") && a["pipelines.migrate"] !== "redesign",
    text: "How should schedules and triggers be recreated?",
    help: "Schedule triggers become Fabric pipeline schedules, created switched off so nothing runs before cutover. Tumbling-window and event triggers have no direct schedule equivalent and are flagged for a person.",
    options: (f) => [
      { value: "disabled", label: "Recreate schedules, disabled until cutover approval", description: triggers(f) ? `${triggers(f)!.schedule} schedule, ${triggers(f)!.tumblingWindow} tumbling-window, ${triggers(f)!.event} event trigger(s).` : undefined },
      { value: "skip", label: "Leave them out", description: "Listed in the migration report." },
    ],
    recommend: (f) => ({ value: "disabled", reason: `${plural(count(f, "Trigger"), "trigger")}${triggers(f)?.started ? `, ${triggers(f)!.started} running in Synapse today` : ""}; creating them switched off keeps the timings without running anything early.` }),
    applies: "The Schedules stage.",
  },
  {
    id: "pipelines.runtime", topic: "pipelines", kind: "single", required: true, effect: "gate",
    when: (f) => feature(f, "selfHostedRuntimes") > 0 || feature(f, "linkedServicesOnSelfHostedRuntime") > 0,
    text: "Some connections go through a self-hosted integration runtime. How should Fabric reach those systems?",
    help: "Fabric reaches on-premises or private systems through an on-premises data gateway, installed and registered by an administrator. The agent never sets one up itself.",
    options: () => [
      { value: "gateway", label: "An administrator sets up a data gateway first", description: "A prerequisite the agent waits for." },
      { value: "exclude", label: "Leave what needs it until access is approved" },
    ],
    recommend: (f) => ({ value: "gateway", reason: `${plural(feature(f, "linkedServicesOnSelfHostedRuntime"), "linked service")} use${feature(f, "linkedServicesOnSelfHostedRuntime") === 1 ? "s" : ""} a self-hosted runtime.` }),
    applies: "A prerequisite the agent asks a person to confirm before the run.",
  },

  // ---- Security, networking and identity -----------------------------------------------------
  {
    id: "security.identity", topic: "security", kind: "single", required: true, effect: "record",
    text: "Which identity should the migration automation use?",
    help: "The run uses the identity signed in on the Connections page, with its own permissions: nothing more.",
    resolve: (f) => f.account ? { value: f.account, reason: `Signed in on the Connections page${f.signInMethod ? ` (${f.signInMethod === "azure_cli" ? "Azure CLI" : f.signInMethod === "interactive_browser" ? "browser sign-in" : f.signInMethod})` : ""}.` } : null,
    options: () => [{ value: "signin", label: "The identity signed in on the Connections page" }],
    recommend: () => ({ value: "signin", reason: "The run never uses another identity than the one signed in." }),
    applies: "The brief.",
  },
  {
    id: "security.credentials", topic: "security", kind: "single", required: true, effect: "gate",
    when: (f) => f.linkedServices > 0,
    text: "Linked services need credentials to become Fabric connections. How are they provided?",
    help: "Synapse never gives up stored secrets. Credentials are typed on the Plan tab, held in memory only and sent once with the run.",
    options: (f) => [
      { value: "person", label: "A person enters them before the run", description: `The agent waits for them.${feature(f, "keyVaultLinkedServices") ? ` ${feature(f, "keyVaultLinkedServices")} use Key Vault in Synapse.` : ""}` },
      { value: "skip", label: "Leave connections out", description: "Objects that need them are deferred, with the reason." },
    ],
    recommend: (f) => ({ value: "person", reason: `${plural(f.linkedServices, "linked service")}; pipelines and the data load need their connections.` }),
    applies: "The Connections stage, and a wait in the agent orchestrator.",
  },
  {
    id: "security.network", topic: "security", kind: "single", required: true, effect: "gate",
    text: "Does the source use private networking or firewall restrictions?",
    help: "Discovery cannot see private endpoints or firewall rules. Where Fabric must reach a private network, an administrator configures a gateway or managed private endpoints.",
    options: () => [
      { value: "yes", label: "Yes", description: "A connectivity check is a prerequisite before the run." },
      { value: "no", label: "No" },
      { value: "unknown", label: "Unknown: check connectivity first" },
    ],
    recommend: (f) => feature(f, "managedVnetRuntimes") > 0
      ? { value: "yes", reason: "The workspace uses a managed virtual network integration runtime." }
      : { value: "unknown", reason: "Discovery cannot see network rules, so a connectivity check is the safe default." },
    applies: "A prerequisite the agent asks a person to confirm before the run (Yes or Unknown).",
  },
  {
    id: "security.sensitive", topic: "security", kind: "single", required: true, effect: "run",
    when: (f) => f.tables > 0,
    text: "Are there restrictions on moving sensitive data to Fabric?",
    help: "With data blocked, the run creates the schema only and loads no rows until approval.",
    options: () => [
      { value: "none", label: "No additional restrictions" },
      { value: "rules", label: "Approved data classification rules apply", description: "Recorded; masks are recreated with the tables." },
      { value: "blocked", label: "Data movement is blocked pending approval", description: "Schema only." },
    ],
    recommend: (f) => feature(f, "maskedColumns") > 0
      ? { value: "rules", reason: `${plural(feature(f, "maskedColumns"), "masked column")}: data in this pool is already classified.` }
      : { value: "none", reason: "No masked columns were found." },
    applies: "The Table data stage (none when blocked).",
  },
  {
    id: "security.roles", topic: "security", kind: "single", required: true, effect: "run",
    when: (f) => (f.byType["Security Object"] ?? 0) > 0 && !!stage(f, "security"),
    text: "How should database roles, permissions and column security be migrated?",
    help: "Roles are recreated with their members, GRANT / DENY (including column grants) applied, and dynamic data masks travel with their tables. SQL users are listed for a person: Fabric has no SQL authentication. Row-level security policies are not read by discovery.",
    options: (f) => [
      { value: "yes", label: "Recreate the supported policies", description: `${plural(feature(f, "maskedColumns"), "masked column")}, ${plural(feature(f, "columnGrants"), "column grant")}.` },
      { value: "no", label: "Leave them for a security review" },
    ],
    recommend: (f) => ({ value: "yes", reason: `${plural(f.byType["Security Object"] ?? 0, "security object")} found; without them users lose access they have today.` }),
    applies: "The Roles & permissions stage.",
  },
  {
    id: "security.audit", topic: "security", kind: "multi", required: false, effect: "record",
    text: "What evidence must the migration produce?",
    help: "Produced either way: the activity log, the brief, the run's record and the migration report (CSV) with every object and check.",
    options: () => [
      { value: "logs", label: "Activity log" }, { value: "mapping", label: "Source-target mapping" },
      { value: "validation", label: "Validation report" }, { value: "exceptions", label: "Exception report" }, { value: "audit", label: "Complete audit trail" },
    ],
    recommend: () => ({ value: ["logs", "mapping", "validation", "exceptions"], reason: "All of these come from the run and its report." }),
    applies: "The brief.",
  },

  // ---- Validation and cutover -----------------------------------------------------------------
  {
    id: "validation.depth", topic: "validation", kind: "single", required: true, effect: "run",
    text: "What level of validation is required?",
    help: "Every object is compared with Synapse and table row counts are compared exactly. Read-only checks query tables and views and compile procedures in the Warehouse. Running pipelines and notebooks starts real work in Fabric. Performance is not measured.",
    options: () => [
      { value: "probe", label: "Structure, row counts and read-only checks", description: "Nothing is started in Fabric." },
      { value: "compare", label: "Structure and row counts only" },
      { value: "run", label: "All of these, and run pipelines and notebooks in Fabric", description: "Real runs: they read and write data." },
    ],
    recommend: () => ({ value: "probe", reason: "Finds objects that would not work without starting anything that writes data." }),
    applies: "Validation: read-only checks, and which objects run in Fabric.",
  },
  {
    id: "validation.tolerance", topic: "validation", kind: "single", required: false, effect: "record",
    text: "What data reconciliation tolerance is acceptable?",
    help: "Row counts are compared exactly by the run. Numeric or business tolerances are applied by a person.",
    options: () => [{ value: "exact", label: "Exact match" }, { value: "numeric", label: "An approved numeric tolerance" }, { value: "business", label: "A business-defined tolerance" }],
    recommend: () => ({ value: "exact", reason: "A one-way copy should match exactly." }),
    applies: "The brief.",
  },
  {
    id: "validation.gate", topic: "validation", kind: "single", required: true, effect: "gate",
    text: "Must every migrated object pass validation before cutover?",
    help: "With every object required to pass, the agent stops on a mismatch until a person decides.",
    options: () => [
      { value: "all", label: "Yes, every object must pass", description: "The agent stops on any mismatch." },
      { value: "exceptions", label: "Allow approved exceptions", description: "The agent stops so a person can approve them." },
      { value: "critical", label: "Only critical objects must pass", description: "Mismatches are reported; a person judges them." },
    ],
    recommend: () => ({ value: "exceptions", reason: "Nothing is cut over unseen, without blocking on a mismatch a person can explain." }),
    applies: "A stop in the agent's validation stage.",
  },
  {
    id: "validation.failures", topic: "validation", kind: "single", required: true, effect: "run",
    text: "How should migration and load failures be handled?",
    help: "Failures are often transient (a throttled request, a capacity that was paused).",
    options: () => [
      { value: "retry", label: "Retry the failed objects once, then report" },
      { value: "stop", label: "Stop at the first failure and ask a person" },
      { value: "report", label: "Carry on and report them" },
    ],
    recommend: () => ({ value: "retry", reason: "One retry clears transient failures; anything left is reported with its error." }),
    applies: "Run option: stop on failure, and the agent's retry.",
  },
  {
    id: "validation.approval", topic: "validation", kind: "single", required: true, effect: "gate",
    text: "Should the agent stop for a person's approval before it writes to Fabric?",
    help: "The agent plans and checks on its own. With approval on, it shows what it is about to create and waits.",
    options: () => [
      { value: "ask", label: "Yes, ask before writing", description: "Recommended for a first run and for production." },
      { value: "auto", label: "No, go ahead once the checks pass" },
    ],
    recommend: (f) => ({ value: "ask", reason: f.environment && /prod/i.test(f.environment) ? "The source is the Prod environment." : "A person should see the plan before the first write to Fabric." }),
    applies: "An approval gate in the agent orchestrator.",
  },
  {
    id: "validation.parallel", topic: "validation", kind: "single", required: false, effect: "record",
    text: "Should source and target run in parallel before cutover?",
    help: "Synapse is untouched by the migration, so both can run side by side; comparing their outputs over time is done by the team.",
    options: () => [{ value: "period", label: "Yes, for a defined period" }, { value: "critical", label: "Only for critical workloads" }, { value: "no", label: "No" }],
    applies: "The brief.",
  },
  {
    id: "validation.source", topic: "validation", kind: "single", required: true, effect: "record",
    text: "What should happen to the Synapse workspace after migration?",
    help: "The migration never changes or deletes Synapse; decommissioning is a separate, approved step.",
    options: () => [
      { value: "temporary", label: "Retain it temporarily" }, { value: "permanent", label: "Retain it permanently" },
      { value: "decommission", label: "Decommission after formal approval" },
    ],
    recommend: () => ({ value: "temporary", reason: "Keeps the fallback until Fabric has run in production for a while." }),
    applies: "The brief.",
  },

  // ---- Special features (only when found, or when discovery cannot see them) ----------------
  {
    id: "features.external", topic: "features", kind: "single", required: true, effect: "run",
    when: (f) => (feature(f, "externalTables") > 0 || has(f, "External Table")) && !!stage(f, "shortcuts"),
    text: "External tables were found. Should their data stay external?",
    help: "The run recreates external tables as OneLake shortcuts in a Lakehouse, pointing at the same storage. Copying the data into OneLake instead is not automated.",
    options: () => [
      { value: "shortcuts", label: "Keep it external, through OneLake shortcuts" },
      { value: "copy", label: "Copy it into OneLake", description: "Not automated: left out of the run." },
    ],
    recommend: (f) => ({ value: "shortcuts", reason: `${plural(Math.max(feature(f, "externalTables"), count(f, "External Table")), "external table")}; shortcuts read the same files without copying them.` }),
    applies: "The External tables stage.",
  },
  {
    id: "features.materializedViews", topic: "features", kind: "single", required: false, effect: "record",
    when: (f, a) => hasSql(f) && a["target.sql"] !== "lakehouse",
    text: "Does the SQL pool use materialized views?",
    help: "Discovery cannot see materialized views, and a Fabric Warehouse has none. If the pool has them, they become plain views or tables a pipeline refreshes.",
    options: () => [
      { value: "none", label: "No" },
      { value: "views", label: "Yes: recreate them as plain views" },
      { value: "tables", label: "Yes: replace them with tables refreshed by a pipeline" },
      { value: "unknown", label: "Not known: a person checks the pool" },
    ],
    applies: "The brief.",
  },
  {
    id: "features.streaming", topic: "features", kind: "single", required: true, effect: "record",
    when: (f) => (triggers(f)?.event ?? 0) > 0,
    text: "Event-driven triggers were found. What latency must the target meet?",
    help: "Fabric pipelines can run on storage events (through Activator); streaming needs Real-Time Intelligence. Neither is set up by this run.",
    options: () => [{ value: "minutes", label: "Minutes: event-triggered pipelines" }, { value: "seconds", label: "Seconds: Real-Time Intelligence" }, { value: "batch", label: "Batch is acceptable" }],
    recommend: () => ({ value: "minutes", reason: "Closest to how the Synapse event triggers behave today." }),
    applies: "The brief.",
  },

  // ---- Context ------------------------------------------------------------------------------
  {
    id: "context.owner", topic: "context", kind: "text", required: true, effect: "record",
    text: "Who approves production cutover for the client?",
    help: "Named on the brief and in the migration report. Only a person can answer this.",
    placeholder: "Name and role, e.g. Priya Shah, data platform lead",
    applies: "The brief and its approval.",
  },
  {
    id: "context.notes", topic: "context", kind: "text", required: false, effect: "record",
    text: "Anything else the team should know? (cutover window, systems to avoid, contacts)",
    help: "Kept with the brief. The agent does not act on it.",
    placeholder: "e.g. Cut over on a Sunday; finance reports must keep working",
    applies: "The brief only.",
  },
];

export const QUESTION_BY_ID = new Map(QUESTIONS.map((q) => [q.id, q]));

export const EFFECT_LABEL: Record<Effect, string> = { run: "Drives the run", gate: "Agent stops for a person", record: "Recorded in the brief" };

/** The label an answer is shown with. */
export function answerLabel(q: Question, f: Facts, v: AnswerValue | undefined): string {
  if (v === undefined || (Array.isArray(v) && !v.length && q.kind !== "multi")) return "—";
  if (q.kind === "text") return String(v).trim() || "—";
  const opts = q.options?.(f) ?? [];
  const label = (x: string) => opts.find((o) => o.value === x)?.label ?? x;
  return Array.isArray(v) ? (v.length ? v.map(label).join(", ") : "None") : label(v);
}
