import type { PerformanceOptions, ReconcileOptions, RunOptions, ValidationRuntimeOptions } from "../types";
import { EFFECT_LABEL, QUESTIONS, TOPICS, answerLabel, type AnswerValue, type Answers, type Question, type Recommendation } from "./catalog";
import type { Facts } from "./facts";

/** Where an answer stands: read from the configuration, given by a person, the recommendation taken as given, or still open. */
export type AnswerSource = "config" | "person" | "recommendation" | "open";

export interface Resolved {
  q: Question;
  /** The question applies to this estate (its `when` holds). */
  applies: boolean;
  value: AnswerValue | undefined;
  source: AnswerSource;
  rec: Recommendation | null;
}

export interface BriefCounts { answered: number; fromConfig: number; assumed: number; needPerson: number; conflicts: number; applicable: number }

/**
 * Every question with the answer the brief uses: the configuration's, else the person's, else the
 * recommendation. Questions are resolved in order, so a later `when` sees the earlier answers.
 */
export function resolve(facts: Facts, given: Answers): Resolved[] {
  const effective: Answers = {};
  return QUESTIONS.map((q) => {
    const applies = q.when ? q.when(facts, effective) : true;
    const config = applies && q.resolve ? q.resolve(facts) : null;
    const rec = config ?? (applies && q.recommend ? q.recommend(facts, effective) : null);
    const mine = given[q.id];
    const has = !config && mine !== undefined && !(typeof mine === "string" && !mine.trim());
    const value = config ? config.value : has ? mine : rec?.value;
    if (applies && value !== undefined) effective[q.id] = value;
    const source: AnswerSource = !applies ? "open" : config ? "config" : has ? "person" : rec ? "recommendation" : "open";
    return { q, applies, value: applies ? value : undefined, source, rec };
  });
}

export const effectiveAnswers = (rs: Resolved[]): Answers =>
  Object.fromEntries(rs.filter((r) => r.applies && r.value !== undefined).map((r) => [r.q.id, r.value]));

const pick = (a: Answers, id: string, fallback: string) => (typeof a[id] === "string" ? (a[id] as string) : fallback);

/** What the approved brief makes the agent do. */
export interface AgentSettings {
  stages: string[];
  /** Object types put in the plan. */
  planTypes: string[];
  /** Also put objects of types no stage moves (recorded as deferred). */
  includeUnstaged: boolean;
  /** Only these dependency waves (a pilot moves the first); null for every wave. */
  waves: number[] | null;
  options: Partial<RunOptions>;
  validation: ValidationRuntimeOptions;
  failures: "retry" | "stop" | "report";
  approval: boolean;
  /** Objects with no automatic route make the agent stop for approval before the run. */
  approveUnsupported: boolean;
  /** The run needs linked-service credentials a person types on the Plan tab. */
  needsCredentials: boolean;
  /** Things an administrator sets up that the agent asks a person to confirm before the run. */
  prerequisites: string[];
  /** When validation finds mismatches: stop for a person (all, exceptions) or report (critical). */
  validationGate: "all" | "exceptions" | "critical";
  /** What a person confirms before cutover: the agent's last stage waits until they sign off. */
  signoffs: string[];
}

/** The value profile Validate reads, from the reconciliation answers. */
export function reconcileFrom(a: Answers): ReconcileOptions {
  const v = pick(a, "validation.tolerance", "exact");
  const results = pick(a, "validation.results", "procedures");
  const compare = { views: results === "both", procedures: results !== "none" };
  if (v === "counts") return { profile: false, ...compare, tolerancePct: 0, differences: "mismatch" };
  if (v === "business") return { profile: true, ...compare, tolerancePct: 0, differences: "review" };
  const pct = Number(v);
  return { profile: true, ...compare, tolerancePct: Number.isFinite(pct) ? pct : 0, differences: "mismatch" };
}

/** Validate's benchmark against Synapse, from the SLA answer. */
export function performanceFrom(a: Answers): PerformanceOptions {
  const sla = pick(a, "sql.sla", "none");
  if (sla === "thresholds") return { enabled: true, thresholdPct: 20, limit: 10, strict: true };
  if (sla === "benchmark") return { enabled: true, thresholdPct: 50, limit: 10, strict: false };
  return { enabled: false, thresholdPct: 50, limit: 10, strict: false };
}

/** What a person confirms before cutover: one line per answer that asks for it, then what the run leaves switched off. */
export function signoffsFrom(facts: Facts, rs: Resolved[], stages: string[]): string[] {
  const out: string[] = [];
  for (const r of rs) {
    if (!r.applies || r.value === undefined || !r.q.signoff) continue;
    const line = r.q.signoff(r.value, facts, answerLabel(r.q, facts, r.value));
    if (line && !out.includes(line)) out.push(line);
  }
  if (facts.tables > 0) {
    out.push("The Cutover checks in Validate are ready (validation clean, synced tables current, final sync run), and the approval is recorded there.");
  }
  if (stages.includes("schedules") && (facts.byType["Trigger"] ?? 0) > 0) {
    out.push("The Fabric pipeline schedules (created switched off) are switched on, and the matching Synapse triggers stopped.");
  }
  return out;
}

const REMOVE: Record<string, (a: Answers) => boolean> = {
  data: (a) => !a["data.load"] || a["data.load"] === "none" || a["security.sensitive"] === "blocked",
  security: (a) => a["security.roles"] === "no",
  spark: (a) => a["spark.compute"] === "starter",
  spark_jobs: (a) => a["spark.jobs"] === "skip",
  pipelines: (a) => a["pipelines.migrate"] === "redesign",
  schedules: (a) => a["pipelines.triggers"] === "skip" || a["pipelines.migrate"] === "redesign",
  connections: (a) => a["security.credentials"] === "skip",
  shortcuts: (a) => a["features.external"] === "copy",
};

export function settingsFrom(facts: Facts, rs: Resolved[]): AgentSettings {
  const a = effectiveAnswers(rs);
  const known = new Set(facts.stages.map((s) => s.key));
  const mode = pick(a, "scope.mode", "all");
  const chosen = mode === "selected" && Array.isArray(a["scope.stages"]) ? a["scope.stages"] : facts.stagesWithObjects.map((s) => s.key).filter((k) => k !== "data");
  const stages = new Set<string>(chosen.filter((k) => known.has(k)));
  if (known.has("data") && facts.tables > 0) stages.add("data");
  for (const [k, off] of Object.entries(REMOVE)) if (off(a)) stages.delete(k);
  const ordered = facts.stages.map((s) => s.key).filter((k) => stages.has(k));
  const planTypes = [...new Set(facts.stages.filter((s) => stages.has(s.key)).flatMap((s) => s.types))];
  const unsupported = pick(a, "sql.unsupported", "automated");
  const scope = unsupported === "all" ? "all" : "automated";
  const failures = pick(a, "validation.failures", "retry") as AgentSettings["failures"];
  const depth = pick(a, "validation.depth", "probe");
  const load = pick(a, "data.load", "none");
  const credentialStages = facts.stages.filter((s) => stages.has(s.key) && s.needsInput === "credentials").length > 0;
  const prerequisites = [
    ...(a["target.workspace"] === "new" ? ["A Fabric administrator creates the target workspace, and it is connected on the Connections page."] : []),
    ...(facts.capacityAssigned !== true && a["target.capacity"] && a["target.capacity"] !== "assigned" ? ["The target workspace is assigned a Fabric capacity."] : []),
    ...(a["pipelines.runtime"] === "gateway" ? ["An administrator installs and registers an on-premises data gateway for the systems behind the self-hosted integration runtime."] : []),
    ...(a["security.network"] === "yes" || a["security.network"] === "unknown" ? ["Fabric can reach the source systems through the network (gateway or managed private endpoints), checked by an administrator."] : []),
  ];
  const options: Partial<RunOptions> = {
    stages: ordered,
    scope,
    stopOnFailure: failures === "stop",
    collation: pick(a, "target.collation", "match_synapse") as RunOptions["collation"],
    // A Lakehouse is a run option of the Warehouse & schema stage: Delta tables, procedures as notebooks.
    target: a["target.sql"] === "lakehouse" ? "lakehouse" : "warehouse",
    existing: pick(a, "target.existing", "skip") as RunOptions["existing"],
    dataRun: load === "create" ? "create" : "run",
    dataMode: pick(a, "data.existingRows", "if_empty") as RunOptions["dataMode"],
    syncOverlap: pick(a, "data.sync", "1h") as RunOptions["syncOverlap"],
    gitCommit: pick(a, "target.gitCommit", "off") as RunOptions["gitCommit"],
    // Keeping direct storage paths means routing nothing; otherwise the run's default routes apply.
    ...(a["spark.storage"] === "direct" ? { storageRoutes: [] } : {}),
  };
  return {
    stages: ordered,
    planTypes,
    includeUnstaged: scope === "all",
    waves: mode === "pilot" ? [1] : null,
    options,
    validation: {
      probe: depth !== "compare", run: depth === "run" ? ["pipeline", "notebook"] : [], timeoutMinutes: 30, reconcile: reconcileFrom(a),
      // Storage access is always proven: a shortcut its connection cannot read fails everything that reads through it.
      access: true, performance: performanceFrom(a),
    },
    failures,
    approval: pick(a, "validation.approval", "ask") !== "auto" || unsupported === "block",
    approveUnsupported: unsupported === "block",
    needsCredentials: credentialStages,
    prerequisites,
    validationGate: pick(a, "validation.gate", "exceptions") as AgentSettings["validationGate"],
    signoffs: signoffsFrom(facts, rs, ordered),
  };
}

/** Answers that cannot all be carried out together. */
export function conflicts(facts: Facts, rs: Resolved[]): string[] {
  const a = effectiveAnswers(rs);
  const s = settingsFrom(facts, rs);
  const on = new Set(s.stages);
  const withObjects = new Set(facts.stagesWithObjects.map((x) => x.key));
  const label = (k: string) => facts.stages.find((x) => x.key === k)?.label ?? k;
  const out: string[] = [];
  for (const st of facts.stages) {
    if (!on.has(st.key)) continue;
    for (const need of st.needs) {
      // A needed stage with nothing to move is not a conflict: there is nothing for it to create.
      if (!on.has(need) && withObjects.has(need)) out.push(`${st.label} needs ${label(need)}, which the brief leaves out.`);
    }
  }
  if (a["security.sensitive"] === "blocked" && a["data.load"] && a["data.load"] !== "none") {
    out.push("Data movement is blocked pending approval, but the data load is set to copy rows: choose Schema only, or lift the restriction.");
  }
  if (a["data.load"] === "sync" && a["validation.parallel"] === "no" && a["scope.approach"] === "parallel") {
    out.push("The approach is to run Synapse and Fabric in parallel, but the brief says they will not run in parallel before cutover.");
  }
  return out;
}

export function counts(facts: Facts, rs: Resolved[]): BriefCounts {
  const live = rs.filter((r) => r.applies);
  const fromConfig = live.filter((r) => r.source === "config").length;
  return {
    applicable: live.length,
    fromConfig,
    answered: live.filter((r) => r.source === "person").length + fromConfig,
    assumed: live.filter((r) => r.source === "recommendation").length,
    needPerson: live.filter((r) => r.q.required && r.source === "open").length,
    conflicts: conflicts(facts, rs).length,
  };
}

/** Same answers, same fingerprint: tells whether an approved version still says what the answers say. */
export function fingerprint(rs: Resolved[]): string {
  const text = JSON.stringify(rs.filter((r) => r.applies).map((r) => [r.q.id, r.value ?? null]));
  let h = 5381;
  for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) | 0;
  return (h >>> 0).toString(16).padStart(8, "0");
}

/** The brief as Markdown, to download and share with the client. */
export function briefMarkdown(facts: Facts, rs: Resolved[], meta: { project: string; version: number | null; status: string }): string {
  const lines = [
    `# Migration brief: ${meta.project}`,
    "",
    `Azure Synapse${facts.workspace ? ` (${facts.workspace})` : ""} to Microsoft Fabric${facts.fabricWorkspace ? ` (${facts.fabricWorkspace})` : ""}. ${meta.version ? `Version ${meta.version}, ${meta.status}.` : "Draft, not yet submitted."}`,
    `Discovery: ${facts.total.toLocaleString()} objects${facts.waves ? ` in ${facts.waves} waves` : ""}.`,
    "",
  ];
  for (const t of TOPICS) {
    const items = rs.filter((r) => r.applies && r.q.topic === t.id);
    if (!items.length) continue;
    lines.push(`## ${t.title}`, "");
    for (const r of items) {
      const how = r.source === "config" ? " _(from the configuration)_" : r.source === "recommendation" ? " _(recommendation)_" : r.source === "open" ? " _(open)_" : "";
      lines.push(`- **${r.q.text}**  `, `  ${answerLabel(r.q, facts, r.value)}${how} · ${EFFECT_LABEL[r.q.effect]}`);
    }
    lines.push("");
  }
  const s = settingsFrom(facts, rs);
  if (s.prerequisites.length) lines.push("## Prerequisites before the run", "", ...s.prerequisites.map((x) => `- ${x}`), "");
  if (s.signoffs.length) lines.push("## Sign-off before cutover", "", ...s.signoffs.map((x) => `- [ ] ${x}`), "");
  const c = conflicts(facts, rs);
  if (c.length) lines.push("## Conflicts", "", ...c.map((x) => `- ${x}`), "");
  if (facts.notRecorded.length) lines.push("## Not visible to discovery", "", `${facts.notRecorded.join(", ")}: questions about these were asked, never answered from discovery.`, "");
  return lines.join("\n");
}
