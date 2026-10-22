import type { RunOptions, ValidationRuntimeOptions } from "../types";
import { QUESTIONS, TOPICS, answerLabel, type AnswerValue, type Answers, type Question, type Recommendation } from "./catalog";
import type { Facts } from "./facts";

/** Where an answer stands: given by a person, the recommendation taken as given, or still open. */
export type AnswerSource = "person" | "recommendation" | "open";

export interface Resolved {
  q: Question;
  /** The question applies to this estate (its `when` holds). */
  applies: boolean;
  value: AnswerValue | undefined;
  source: AnswerSource;
  rec: Recommendation | null;
}

export interface BriefCounts { answered: number; assumed: number; needPerson: number; conflicts: number; applicable: number }

/**
 * Every question with the answer the brief uses: the person's, else the recommendation.
 * Questions are resolved in order, so a later `when` sees the earlier answers it depends on.
 */
export function resolve(facts: Facts, given: Answers): Resolved[] {
  const effective: Answers = {};
  return QUESTIONS.map((q) => {
    const applies = q.when ? q.when(facts, effective) : true;
    const rec = applies && q.recommend ? q.recommend(facts, effective) : null;
    const mine = given[q.id];
    const has = mine !== undefined && !(typeof mine === "string" && !mine.trim());
    const value = has ? mine : rec?.value;
    if (applies && value !== undefined) effective[q.id] = value;
    return { q, applies, value: applies ? value : undefined, source: !applies ? "open" : has ? "person" : rec ? "recommendation" : "open", rec };
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
  options: Partial<RunOptions>;
  validation: ValidationRuntimeOptions;
  failures: "retry" | "stop" | "report";
  approval: boolean;
  /** The run needs linked-service credentials a person types on the Plan step. */
  needsCredentials: boolean;
}

export function settingsFrom(facts: Facts, rs: Resolved[]): AgentSettings {
  const a = effectiveAnswers(rs);
  const known = new Set(facts.stages.map((s) => s.key));
  const stages = new Set<string>((Array.isArray(a["scope.stages"]) ? a["scope.stages"] : []).filter((k) => known.has(k)));
  const dataRun = pick(a, "data.move", "none");
  if (dataRun !== "none" && known.has("data") && facts.tables > 0) stages.add("data");
  if (a["ops.schedules"] === "skip") stages.delete("schedules");
  if (a["sec.roles"] === "no") stages.delete("security");
  if (a["sec.credentials"] === "skip") stages.delete("connections");
  const ordered = facts.stages.map((s) => s.key).filter((k) => stages.has(k));
  const planTypes = [...new Set(facts.stages.filter((s) => stages.has(s.key)).flatMap((s) => s.types))];
  const scope = pick(a, "conv.unsupported", "automated") === "all" ? "all" : "automated";
  const failures = pick(a, "val.retry", "retry") as AgentSettings["failures"];
  const depth = pick(a, "val.depth", "probe");
  const credentialStages = facts.stages.filter((s) => stages.has(s.key) && s.needsInput === "credentials").length > 0;
  return {
    stages: ordered,
    planTypes,
    includeUnstaged: scope === "all",
    options: {
      stages: ordered,
      scope,
      stopOnFailure: failures === "stop",
      collation: pick(a, "target.collation", "match_synapse") as RunOptions["collation"],
      existing: pick(a, "target.existing", "skip") as RunOptions["existing"],
      dataRun: dataRun === "create" ? "create" : "run",
      dataMode: pick(a, "data.existingRows", "if_empty") as RunOptions["dataMode"],
      syncOverlap: pick(a, "data.sync", "1h") as RunOptions["syncOverlap"],
      gitCommit: pick(a, "ops.gitCommit", "off") as RunOptions["gitCommit"],
    },
    validation: { probe: depth !== "compare", run: depth === "run" ? ["pipeline", "notebook"] : [], timeoutMinutes: 30 },
    failures,
    approval: pick(a, "ops.approval", "ask") !== "auto",
    needsCredentials: credentialStages,
  };
}

/** Answers that cannot all be carried out: a stage that needs another the brief leaves out. */
export function conflicts(facts: Facts, rs: Resolved[]): string[] {
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
  return out;
}

export function counts(facts: Facts, rs: Resolved[]): BriefCounts {
  const live = rs.filter((r) => r.applies);
  return {
    applicable: live.length,
    answered: live.filter((r) => r.source === "person").length,
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
    `Azure Synapse${facts.workspace ? ` (${facts.workspace})` : ""} to Microsoft Fabric. ${meta.version ? `Version ${meta.version}, ${meta.status}.` : "Draft, not yet submitted."}`,
    `Discovery: ${facts.total.toLocaleString()} objects${facts.waves ? ` in ${facts.waves} waves` : ""}.`,
    "",
  ];
  for (const t of TOPICS) {
    const items = rs.filter((r) => r.applies && r.q.topic === t.id);
    if (!items.length) continue;
    lines.push(`## ${t.title}`, "");
    for (const r of items) {
      const how = r.source === "person" ? "" : r.source === "recommendation" ? " _(recommendation)_" : " _(open)_";
      lines.push(`- **${r.q.text}**  `, `  ${answerLabel(r.q, facts, r.value)}${how}`);
    }
    lines.push("");
  }
  const c = conflicts(facts, rs);
  if (c.length) lines.push("## Conflicts", "", ...c.map((x) => `- ${x}`), "");
  return lines.join("\n");
}
