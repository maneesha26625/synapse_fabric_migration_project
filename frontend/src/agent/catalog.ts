import { plural, type Facts } from "./facts";

/**
 * The consultation: the questions agreed with the client before anything moves. Each comes with a
 * recommendation worked out from what discovery found, and says what its answer changes. The agent
 * orchestrator carries out the approved answers; nothing here runs anything.
 */

export type AnswerValue = string | string[];
export type Answers = Record<string, AnswerValue | undefined>;

export interface Option { value: string; label: string; description?: string }

export interface Recommendation { value: AnswerValue; reason: string }

export type TopicId = "scope" | "target" | "data" | "conversion" | "operations" | "security" | "validation" | "context";

export interface Topic { id: TopicId; title: string; summary: string }

export interface Question {
  id: string;
  topic: TopicId;
  text: string;
  help: string;
  kind: "single" | "multi" | "text";
  options?: (f: Facts) => Option[];
  required: boolean;
  /** Asked only when this holds; otherwise the question does not apply and is not counted. */
  when?: (f: Facts, a: Answers) => boolean;
  recommend?: (f: Facts, a: Answers) => Recommendation | null;
  /** What the answer changes, in words. */
  applies: string;
  placeholder?: string;
}

export const TOPICS: Topic[] = [
  { id: "scope", title: "Scope: what moves", summary: "Which of the discovered objects this migration moves." },
  { id: "target", title: "Target layout: where it lands", summary: "How the Fabric Warehouse is created and what happens to objects already there." },
  { id: "data", title: "Data loading", summary: "Whether table rows are copied, and how a table that already has rows is treated." },
  { id: "conversion", title: "Conversion", summary: "What happens to objects the rules cannot convert on their own." },
  { id: "operations", title: "Environments and operations", summary: "Schedules, Fabric Git and who approves before anything is written." },
  { id: "security", title: "Security and governance", summary: "Roles, permissions and the credentials Fabric connections need." },
  { id: "validation", title: "Validation and cutover", summary: "How the result is checked, and what the agent does with failures." },
  { id: "context", title: "Context (notes only)", summary: "Who signs off and anything the agent and the team should know." },
];

/** Stages the scope question chooses among. Table data has its own question under Data loading. */
const scopeStages = (f: Facts) => f.stagesWithObjects.filter((s) => s.key !== "data");

const stage = (f: Facts, key: string) => f.stages.find((s) => s.key === key);

export const QUESTIONS: Question[] = [
  // ---- Scope
  {
    id: "scope.stages", topic: "scope", kind: "multi", required: true,
    text: "Which kinds of objects should this migration move?",
    help: "Only kinds discovery found are offered. Each becomes one stage of the run; a kind left out is listed in the hand-over report and not touched in Fabric.",
    options: (f) => scopeStages(f).map((s) => ({ value: s.key, label: s.label, description: `${s.summary} (${plural(s.types.reduce((n, t) => n + (f.byType[t] ?? 0), 0), "object")})` })),
    recommend: (f) => {
      const keys = scopeStages(f).map((s) => s.key);
      return keys.length ? { value: keys, reason: `Discovery found objects for ${plural(keys.length, "stage")}; moving them all is the usual first migration.` } : null;
    },
    applies: "The run's stages and the objects added to the plan.",
  },
  {
    id: "conv.unsupported", topic: "conversion", kind: "single", required: true,
    text: "What should happen to objects with no automatic route to Fabric?",
    help: "Objects classed MANUAL or NOT SUPPORTED cannot be created by the rules. They can be left out of the run and listed for a person, or kept in the run so it records each one as deferred, with the reason.",
    options: () => [
      { value: "automated", label: "List them for a person", description: "The run moves only what it can create; the rest is in the hand-over report." },
      { value: "all", label: "Keep them in the run as deferred", description: "Every object appears in the run's record, the deferred ones with why." },
    ],
    recommend: (f) => ({
      value: "automated",
      reason: f.noAutomaticRoute ? `${plural(f.noAutomaticRoute, "object")} ha${f.noAutomaticRoute === 1 ? "s" : "ve"} no automatic route; a person is better placed to rebuild them.` : "Nothing discovered lacks an automatic route, so this changes nothing today.",
    }),
    applies: "Run option: scope (automated or all).",
  },

  // ---- Target
  {
    id: "target.collation", topic: "target", kind: "single", required: true,
    when: (f) => !!stage(f, "warehouse"),
    text: "Which collation should the Fabric Warehouse be created with?",
    help: "A Warehouse's collation is set once, when it is created, and cannot change afterwards. It decides whether 'ABC' equals 'abc' in comparisons and object names.",
    options: () => [
      { value: "match_synapse", label: "Same as Synapse", description: "Case-sensitive only if the Synapse pool was." },
      { value: "case_insensitive", label: "Case-insensitive" },
      { value: "case_sensitive", label: "Case-sensitive (Fabric's default)" },
    ],
    recommend: () => ({ value: "match_synapse", reason: "Keeping the pool's behaviour means queries and reports return what they did in Synapse." }),
    applies: "Run option: Warehouse collation.",
  },
  {
    id: "target.existing", topic: "target", kind: "single", required: true,
    text: "If an object already exists in the Fabric workspace, what should happen?",
    help: "Re-running a migration finds what an earlier run created. Leaving it is always safe; updating replaces views, procedures, notebooks, pipelines and scripts in place. Tables are never replaced.",
    options: () => [
      { value: "skip", label: "Leave it as it is", description: "Safe to re-run; changes made in Fabric are kept." },
      { value: "update", label: "Update it from Synapse", description: "Overwrites changes made in Fabric since." },
    ],
    recommend: () => ({ value: "skip", reason: "Nothing made by hand in Fabric is overwritten, and the run can be repeated safely." }),
    applies: "Run option: objects already in Fabric.",
  },

  // ---- Data
  {
    id: "data.move", topic: "data", kind: "single", required: true,
    when: (f) => f.tables > 0,
    text: "Should the tables' rows be copied to Fabric?",
    help: "Rows are loaded by Fabric data pipelines, one per wave, reading the Synapse SQL pool through a Fabric connection. That connection needs credentials, entered on the Plan step.",
    options: () => [
      { value: "run", label: "Copy the rows", description: "Create each wave's pipeline, run it, and compare row counts." },
      { value: "create", label: "Create the pipelines only", description: "You run them from Fabric, or a schedule, when you choose." },
      { value: "none", label: "Schema only", description: "Tables are created empty." },
    ],
    recommend: (f) => f.sqlPool
      ? { value: "run", reason: `The SQL pool ${f.sqlPool} is connected, so ${plural(f.tables, "table")} can be loaded and their row counts compared.` }
      : { value: "none", reason: "No SQL pool is connected, so there are no rows to read. Connect the workspace's pool to copy data." },
    applies: "The Table data stage and its 'What the stage does' option.",
  },
  {
    id: "data.existingRows", topic: "data", kind: "single", required: true,
    when: (f, a) => f.tables > 0 && a["data.move"] !== "none",
    text: "If a Fabric table already has rows, what should the load do?",
    help: "A second run meets tables the first one filled.",
    options: () => [
      { value: "if_empty", label: "Skip it", description: "Never touches data already there, so re-runs are safe." },
      { value: "replace", label: "Replace it", description: "Empties the table (TRUNCATE) before loading again." },
    ],
    recommend: () => ({ value: "if_empty", reason: "Re-running never loses or doubles rows." }),
    applies: "Run option: if a table already has rows.",
  },
  {
    id: "data.sync", topic: "data", kind: "single", required: false,
    when: (f, a) => f.tables > 0 && a["data.move"] === "run",
    text: "For tables kept in sync, how far back should each sync re-read?",
    help: "Tables with key columns can be loaded again later, re-reading a window of recent changes to catch late arrivals.",
    options: () => [
      { value: "1h", label: "One hour" },
      { value: "1d", label: "One day" },
      { value: "none", label: "No overlap" },
    ],
    recommend: () => ({ value: "1h", reason: "Catches rows committed late without re-reading much." }),
    applies: "Run option: re-read window for tables kept in sync.",
  },

  // ---- Operations
  {
    id: "ops.schedules", topic: "operations", kind: "single", required: true,
    when: (f) => (f.byType.Trigger ?? 0) > 0 && !!stage(f, "schedules"),
    text: "Should Synapse triggers become Fabric pipeline schedules?",
    help: "Schedules are created switched off, so nothing runs in Fabric before you are ready to cut over.",
    options: () => [
      { value: "create", label: "Create them, switched off" },
      { value: "skip", label: "Leave them out", description: "Listed in the hand-over report." },
    ],
    recommend: (f) => ({ value: "create", reason: `${plural(f.byType.Trigger ?? 0, "trigger")} found; creating them switched off keeps the timings without running anything.` }),
    applies: "The Schedules stage.",
  },
  {
    id: "ops.gitCommit", topic: "operations", kind: "single", required: true,
    text: "After the run, should what it created be committed to the Fabric workspace's Git branch?",
    help: "Committing records the migrated items in the workspace's repository, ready for a pull request to Test and Prod.",
    options: () => [
      { value: "auto", label: "Commit after the run" },
      { value: "off", label: "Don't commit", description: "Commit later from the Migrate step." },
    ],
    recommend: (f) => f.fabricGitConnected
      ? { value: "auto", reason: "The target workspace is connected to Git." }
      : { value: "off", reason: "The target workspace is not connected to Git (or not read yet), so there is nowhere to commit." },
    applies: "Run option: commit to Fabric Git.",
  },
  {
    id: "ops.approval", topic: "operations", kind: "single", required: true,
    text: "Should the agent stop for a person's approval before it writes to Fabric?",
    help: "The agent plans and checks on its own. With approval on, it shows what it is about to create and waits before the run starts.",
    options: () => [
      { value: "ask", label: "Yes, ask before writing", description: "Recommended for a first run and for production." },
      { value: "auto", label: "No, go ahead", description: "The agent starts the run as soon as the checks pass." },
    ],
    recommend: (f) => ({ value: "ask", reason: f.environment && /prod/i.test(f.environment) ? "The source is the Prod environment." : "A person should see the plan before the first write to Fabric." }),
    applies: "An approval gate in the agent orchestrator.",
  },

  // ---- Security
  {
    id: "sec.roles", topic: "security", kind: "single", required: true,
    when: (f) => (f.byType["Security Object"] ?? 0) > 0 && !!stage(f, "security"),
    text: "Should database roles and permissions be recreated in the Warehouse?",
    help: "Roles are recreated with their members, and GRANT / DENY applied to roles and Microsoft Entra users. SQL users are listed for a person: Fabric has no SQL authentication.",
    options: () => [
      { value: "yes", label: "Recreate them" },
      { value: "no", label: "Leave them for a person" },
    ],
    recommend: (f) => ({ value: "yes", reason: `${plural(f.byType["Security Object"] ?? 0, "security object")} found; without them, users lose access they have today.` }),
    applies: "The Roles & permissions stage.",
  },
  {
    id: "sec.credentials", topic: "security", kind: "single", required: true,
    when: (f) => f.linkedServices > 0,
    text: "Linked services need credentials to become Fabric connections. How are they provided?",
    help: "Synapse never gives up stored secrets, so Fabric connections need them again. They are typed on the Plan step, held in memory only and sent once with the run.",
    options: () => [
      { value: "person", label: "A person enters them before the run", description: "The agent waits for them." },
      { value: "skip", label: "Leave connections out", description: "Objects that need them are deferred, with the reason." },
    ],
    recommend: (f) => ({ value: "person", reason: `${plural(f.linkedServices, "linked service")} found; pipelines and the data load need their connections.` }),
    applies: "The Connections stage, and a wait in the agent orchestrator.",
  },

  // ---- Validation
  {
    id: "val.depth", topic: "validation", kind: "single", required: true,
    text: "How thoroughly should the agent validate the result?",
    help: "Every object is compared with Synapse. Read-only checks also query tables and views and compile procedures in the Warehouse. Running for real starts pipelines and notebooks in Fabric.",
    options: () => [
      { value: "probe", label: "Compare and run read-only checks", description: "Nothing is started in Fabric." },
      { value: "compare", label: "Compare definitions and counts only" },
      { value: "run", label: "Also run pipelines and notebooks in Fabric", description: "Real runs: they read and write data." },
    ],
    recommend: () => ({ value: "probe", reason: "Finds objects that would not work without starting anything that writes data." }),
    applies: "Validation: read-only checks, and which objects run in Fabric.",
  },
  {
    id: "val.retry", topic: "validation", kind: "single", required: true,
    text: "If objects fail during the run, what should the agent do?",
    help: "Failures are often transient (a throttled request, a capacity that was paused).",
    options: () => [
      { value: "retry", label: "Retry the failed ones once, then report" },
      { value: "stop", label: "Stop at the first failure and ask a person" },
      { value: "report", label: "Carry on and report them" },
    ],
    recommend: () => ({ value: "retry", reason: "One retry clears transient failures; anything left is reported with its error." }),
    applies: "Run option: stop on failure, and the agent's retry.",
  },

  // ---- Context
  {
    id: "context.owner", topic: "context", kind: "text", required: true,
    text: "Who signs off this migration for the client?",
    help: "Named on the brief and in the hand-over report. Only a person can answer this.",
    placeholder: "Name and role, e.g. Priya Shah, data platform lead",
    applies: "The brief and the hand-over report.",
  },
  {
    id: "context.notes", topic: "context", kind: "text", required: false,
    text: "Anything else the team should know? (cutover window, systems to avoid, contacts)",
    help: "Kept with the brief. The agent does not act on it.",
    placeholder: "e.g. Cut over on a Sunday; finance reports must keep working",
    applies: "The brief only.",
  },
];

export const QUESTION_BY_ID = new Map(QUESTIONS.map((q) => [q.id, q]));

/** The label an answer is shown with. */
export function answerLabel(q: Question, f: Facts, v: AnswerValue | undefined): string {
  if (v === undefined || (Array.isArray(v) && !v.length && q.kind !== "multi")) return "—";
  if (q.kind === "text") return String(v).trim() || "—";
  const opts = q.options?.(f) ?? [];
  const label = (x: string) => opts.find((o) => o.value === x)?.label ?? x;
  return Array.isArray(v) ? (v.length ? v.map(label).join(", ") : "None") : label(v);
}
