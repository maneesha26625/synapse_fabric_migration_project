// The DEMO consultation: the server's catalog (consultCatalog.json is a copy of it, and a backend test keeps the two the
// same) and a TypeScript copy of the engine, the brief and the policy, so Demo data walks the whole consultation without
// a backend. It mirrors src/discovery_agent/consult/ and api/consult.py; where the two could differ, the server is right.
//
// Demo differences, on purpose: the notes are always read with the built-in rules (nothing is sent anywhere, so there is
// no model to ask), and the questions' facts come from the demo estate.

import raw from "./consultCatalog.json";
import {
  ApiRequestError,
  type Actor,
  type AnswerPreview,
  type AnswerSource,
  type BriefCheck,
  type BriefContent,
  type BriefDecision,
  type BriefSummary,
  type BriefVersion,
  type BriefView,
  type ConsultCatalog,
  type ConsultConflict,
  type ConsultExplanation,
  type ConsultPolicy,
  type ConsultProposal,
  type ConsultQuestion,
  type ConsultQuestionType,
  type ConsultState,
  type DataFilterInput,
  type FactChange,
  type FilterableTable,
  type LlmGate,
  type PlanDifference,
  type RunOptions,
  type RunnableKind,
  type SettingStatus,
} from "../types";

type Facts = Record<string, unknown>;
type Values = Record<string, unknown>;

// ---- the catalog, the settings and the facts, as the server names them ------------------------------------------

interface RawOption { value: string; label?: string; description?: string; sets?: Record<string, unknown> }
interface RawQuestion {
  id: string; section: string; type: ConsultQuestionType; text: string; help?: string; options?: RawOption[]; optionsFrom?: string;
  rowsFrom?: string; appliesWhen?: unknown; defaults?: { value: unknown; reason: string; when?: unknown }[]; drives: string[];
  required?: boolean; optional?: boolean; evidence?: string[]; min?: number; max?: number; maxLength?: number;
  separateApprover?: { when: unknown; reason: string };
}
interface RawConflict {
  id: string; when: unknown; message: string; questions?: string[]; evidence?: string[];
  resolutions: { kind: "take" | "keep" | "change"; label: string; question?: string; value?: unknown; add?: string }[];
}
const CAT = raw as unknown as { version: string; sections: { id: string; title: string }[]; questions: RawQuestion[]; conflicts: RawConflict[] };
const QUESTION = new Map(CAT.questions.map((q) => [q.id, q]));

/** How far the accelerator acts on each setting, and who: mirrors consult/settings.py. */
const ENFORCED = "enforced", RECORDED = "recorded", NOTE = "note";
const SETTING_STATUS: Record<string, SettingStatus> = Object.fromEntries([
  "run.parts", "plan.scopeMode", "plan.scopeInclude", "plan.scopeExclude", "plan.serverless", "run.stopOnFailure", "run.waveGate",
  "run.existing", "warehouse.collation", "names.warehouse", "names.lakehouse", "llm.standards", "data.historyMode", "data.historyFrom",
  "data.sync", "data.syncOverlap", "data.mode", "data.run", "data.poolSignIn", "llm.consent", "llm.budgetUsd", "code.multiStatement",
  "code.rebuild", "security.migrate", "security.sqlUserMap", "connections.defaultSignIn", "storage.routes", "release.gitCommit",
  "release.commitScope", "release.signoff", "validate.depth",
].map((n) => [n, ENFORCED as SettingStatus]).concat([["assistant.autonomy", RECORDED]],
  ["owners.rebuild", "owners.setup", "release.path", "release.cutover", "context.timeline", "context.approver", "context.consumers",
    "context.painPoints"].map((n) => [n, NOTE as SettingStatus])));
const SETTING_APPLIES: Record<string, string> = {
  "run.parts": "Migrate: which stages are on", "plan.scopeMode": "Plan: which objects Add all puts in the plan",
  "plan.scopeInclude": "Plan: the schemas and folders in scope", "plan.scopeExclude": "Plan: name patterns left out of the plan",
  "plan.serverless": "Plan: scripts and pipelines written for the serverless pool",
  "run.stopOnFailure": "Migrate: stop at the end of a wave that has failures",
  "run.waveGate": "Migrate: a person signs off each wave before the next one starts", "run.existing": "Migrate: objects already in Fabric",
  "warehouse.collation": "Migrate: the Warehouse collation, set when it is created", "names.warehouse": "Migrate and Validate: the Warehouse's name",
  "names.lakehouse": "Migrate: the Lakehouse's name (shortcuts, storage routes, Spark jobs)",
  "llm.standards": "LLM drafts: rules every draft follows, added to the project requirements",
  "data.historyMode": "Plan: how much of each table's history loads", "data.historyFrom": "Plan: the first date loaded, on each table's date column",
  "data.sync": "Plan: tables with a change column are kept in sync after the first load", "data.syncOverlap": "Migrate: the re-read window of each sync",
  "data.mode": "Migrate: tables that already have rows", "data.run": "Migrate: run the load pipelines, or only create them",
  "data.poolSignIn": "Plan: how the load connection signs in to the pool (the secret is never kept)",
  "llm.consent": "LLM drafts, consultation help and the assistant: whether anything may be sent to Claude",
  "llm.budgetUsd": "LLM calls: the most this project may spend", "code.multiStatement": "Plan: multi-statement table-valued functions",
  "code.rebuild": "Plan: pipelines with activities Fabric lacks, and .NET notebooks", "owners.rebuild": "People: who rebuilds what has no Fabric equivalent",
  "security.migrate": "Migrate: whether the Roles & permissions stage is on",
  "security.sqlUserMap": "Migrate: the Microsoft Entra principal that replaces each SQL user",
  "connections.defaultSignIn": "Plan: how each linked service's connection signs in, preselected",
  "storage.routes": "Plan: storage the code reads directly, routed through OneLake shortcuts",
  "release.gitCommit": "Migrate: commit each run to the target workspace's Fabric Git branch", "release.commitScope": "Migrate: what that commit takes",
  "release.signoff": "Migrate: which runs a person signs off before they write",
  "owners.setup": "People: who sets up schedules, integration runtimes and event triggers", "release.path": "People: the path to Test and Prod",
  "validate.depth": "Validate: how deep it checks by default", "release.cutover": "People: the cutover window and what a rollback means",
  "assistant.autonomy": "Assistant: what it may do alone (the assistant is not built yet)", "context.timeline": "People: the timeline",
  "context.approver": "People: who approves the migration", "context.consumers": "People: what reads the data downstream (Power BI, applications)",
  "context.painPoints": "People: what hurts today",
};
/** The estate facts: what a brief rests on, and what stales it when it changes (environment facts never do). */
export const FACT_DESCRIPTIONS: Record<string, string> = {
  "objects.total": "Objects discovered", "objects.byType": "Objects by type",
  "objects.byClass": "Objects by how they move (Direct, Reconfigure, Transform, Manual, Review)", "waves.count": "Dependency waves",
  "pool.present": "A dedicated SQL pool was discovered", "pool.name": "The dedicated SQL pool",
  "pool.warehouseName": "The Warehouse name the pool's name gives", "pool.collation": "The Synapse pool's collation",
  "pool.caseInsensitive": "The pool compares text case-insensitively", "tables.count": "Tables (external tables aside)",
  "tables.external": "External tables", "tables.withDateColumn": "Tables with a date or time column",
  "tables.withChangeColumn": "Tables with a column like ModifiedAt, which moves when a row changes",
  "tables.withKeys": "Tables with a primary key or a unique constraint", "tables.withoutKeys": "Tables with no key",
  "tables.changeWithoutKeys": "Tables with a change column but no key: a sync can only append to them",
  "storage.locations": "Storage locations the code reads directly", "security.principals": "Database roles and users",
  "security.roles": "Database roles", "security.sqlUsers": "SQL-authenticated users (Fabric has no SQL authentication)",
  "security.sqlUserNames": "The SQL-authenticated users", "security.masks": "Columns with a dynamic data mask",
  "code.multiStatementFunctions": "Multi-statement table-valued functions (Fabric has none)",
  "code.dotnetNotebooks": ".NET for Spark notebooks (Fabric has none)",
  "code.unsupportedPipelines": "Pipelines with an activity Fabric has no equivalent for",
  "code.needsRewrite": "Objects that must be rebuilt by hand (unsupported pipelines and .NET notebooks)",
  "code.serverlessObjects": "Scripts and pipelines written for the serverless SQL pool", "linkedServices.count": "Linked services",
  "linkedServices.byType": "Linked services by type", "linkedServices.keyVault": "Linked services that read a secret from Azure Key Vault",
  "triggers.count": "Triggers", "triggers.schedule": "Schedule triggers (recreated switched off)",
  "triggers.event": "Event, tumbling-window and custom triggers (set up by hand)",
  "integrationRuntimes.count": "Integration runtimes (set up by hand)",
  "manual.setupCount": "Triggers and integration runtimes someone sets up or switches on", "scope.groups": "The schemas and folders discovered",
  "source.kind": "Where the definitions were read from (the live workspace, Git or a ZIP)", "source.workspace": "The Synapse workspace",
  "source.ref": "The Git branch or tag read", "source.commit": "The Git commit read", "source.environment": "The environment the definitions are for",
  "discovery.complete": "Discovery read every part of the source", "discovery.failedCategories": "The parts of the source discovery could not read",
};
const ENVIRONMENT_FACTS: Record<string, string> = {
  "target.connected": "The Fabric target is connected", "target.workspace": "The Fabric target workspace",
  "target.capacity": "The Fabric workspace is on a capacity", "target.gitConnected": "The Fabric workspace is connected to Git",
  "llm.available": "Claude Code is installed on this machine",
};
const STAGE_KEYS = ["warehouse", "data", "security", "spark", "notebooks", "connections", "pipelines", "spark_jobs", "sql_scripts", "schedules", "shortcuts"];

// ---- conditions: as consult/conditions.py, "unknown" never holds ---------------------------------------------------

const UNKNOWN = Symbol("unknown");
type V = unknown;

function truthy(v: V): boolean {
  if (v === null || v === undefined || v === false || v === 0 || v === "") return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v as object).length > 0;
  return true;
}
function same(a: V, b: V): boolean {
  if (a === b) return true;
  if (typeof a === "number" && typeof b === "boolean") return a === Number(b);
  if (typeof b === "number" && typeof a === "boolean") return b === Number(a);
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => same(x, b[i]));
  if (a && b && typeof a === "object" && typeof b === "object" && !Array.isArray(a) && !Array.isArray(b)) {
    const ka = Object.keys(a as object), kb = Object.keys(b as object);
    return ka.length === kb.length && ka.every((k) => same((a as Record<string, V>)[k], (b as Record<string, V>)[k]));
  }
  return false;
}
function order(op: string, a: V, b: V): V {
  const num = (x: V) => typeof x === "number" || typeof x === "boolean";
  if (!((num(a) && num(b)) || (typeof a === "string" && typeof b === "string"))) return UNKNOWN; // Python refuses to order these
  const x = typeof a === "boolean" ? Number(a) : a, y = typeof b === "boolean" ? Number(b) : b;
  if (op === ">") return (x as number) > (y as number);
  if (op === ">=") return (x as number) >= (y as number);
  if (op === "<") return (x as number) < (y as number);
  return (x as number) <= (y as number);
}
export function evaluate(c: V, facts: Facts, values: Values): V {
  if (Array.isArray(c)) {
    const items = c.map((x) => evaluate(x, facts, values));
    return items.some((i) => i === UNKNOWN) ? UNKNOWN : items;
  }
  if (c === null || typeof c !== "object") return c;
  const [op, arg] = Object.entries(c as Record<string, V>)[0];
  if (op === "fact") return String(arg) in facts ? facts[String(arg)] : UNKNOWN;
  if (op === "answer") return String(arg) in values ? values[String(arg)] : UNKNOWN;
  const args = Array.isArray(arg) ? arg : [];
  if (["==", "!=", ">", ">=", "<", "<="].includes(op)) {
    const a = evaluate(args[0], facts, values), b = evaluate(args[1], facts, values);
    if (a === UNKNOWN || b === UNKNOWN) return UNKNOWN;
    if (op === "==") return same(a, b);
    if (op === "!=") return !same(a, b);
    return order(op, a, b);
  }
  if (op === "in") {
    const needle = evaluate(args[0], facts, values), hay = evaluate(args[1], facts, values);
    if (needle === UNKNOWN || hay === UNKNOWN) return UNKNOWN;
    if (Array.isArray(hay)) return hay.some((h) => same(h, needle));
    if (typeof hay === "string") return typeof needle === "string" && hay.includes(needle);
    if (hay && typeof hay === "object") return String(needle) in (hay as object);
    return UNKNOWN;
  }
  if (op === "and" || op === "or") {
    const results = args.map((x) => evaluate(x, facts, values));
    if (op === "and") {
      if (results.some((r) => r === false)) return false;
      return results.some((r) => r === UNKNOWN) ? UNKNOWN : results.every(truthy);
    }
    if (results.some((r) => r === true)) return true;
    return results.some((r) => r === UNKNOWN) ? UNKNOWN : results.some(truthy);
  }
  if (op === "not") {
    const inner = evaluate(arg, facts, values);
    return inner === UNKNOWN ? UNKNOWN : !truthy(inner);
  }
  if (op === "+") {
    const parts = args.map((x) => evaluate(x, facts, values));
    return parts.some((p) => typeof p !== "number") ? UNKNOWN : (parts as number[]).reduce((s, p) => s + p, 0);
  }
  if (op === "len") {
    const inner = evaluate(arg, facts, values);
    if (Array.isArray(inner) || typeof inner === "string") return inner.length;
    if (inner && typeof inner === "object") return Object.keys(inner as object).length;
    return UNKNOWN;
  }
  return UNKNOWN;
}
export const holds = (c: V, facts: Facts, values: Values) => c === undefined || c === null || evaluate(c, facts, values) === true;

function show(v: V): string {
  if (v === UNKNOWN || v === null || v === undefined) return "unknown";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.map(show).join(", ") || "none";
  if (typeof v === "object") return Object.entries(v as object).map(([k, x]) => `${k} ${x}`).join(", ") || "none";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("en-US") : String(v);
  return String(v);
}
export function render(template: string, facts: Facts, values: Values): string {
  return template.replace(/\{(fact|answer):([A-Za-z0-9_.]+)\}/g, (_m, kind: string, name: string) =>
    show(kind === "fact" ? (name in facts ? facts[name] : UNKNOWN) : name in values ? values[name] : UNKNOWN));
}
const known = (template: string, facts: Facts, values: Values) =>
  [...template.matchAll(/\{(fact|answer):([A-Za-z0-9_.]+)\}/g)].every(([, kind, name]) =>
    (kind === "fact" ? facts[name] : values[name]) !== undefined && (kind === "fact" ? facts[name] : values[name]) !== null);

// ---- checking an answer: as Question.check ------------------------------------------------------------------------

const NAME = /^[A-Za-z][A-Za-z0-9_]{0,122}$/;
export function choicesOf(q: RawQuestion, facts: Facts): string[] {
  if (q.optionsFrom) return Array.isArray(facts[q.optionsFrom]) ? (facts[q.optionsFrom] as unknown[]).map(String) : [];
  return (q.options ?? []).map((o) => o.value);
}
function bad(message: string): never { throw new ApiRequestError("invalid_answer", message, 400); }
export function check(q: RawQuestion, value: unknown, facts: Facts): unknown {
  switch (q.type) {
    case "choice": if (!choicesOf(q, facts).includes(String(value))) bad(`${q.id}: Choose one of: ${choicesOf(q, facts).join(", ")}.`); return value;
    case "multi": {
      if (!Array.isArray(value) || !value.every((v) => typeof v === "string")) bad(`${q.id}: Choose from the list.`);
      const allowed = choicesOf(q, facts);
      const unknownOnes = (value as string[]).filter((v) => !allowed.includes(v));
      if (unknownOnes.length) bad(`${q.id}: Not one of the choices: ${unknownOnes.slice(0, 5).join(", ")}.`);
      return allowed.filter((v) => (value as string[]).includes(v));
    }
    case "boolean": if (typeof value !== "boolean") bad(`${q.id}: Answer yes or no.`); return value;
    case "number": {
      if (typeof value !== "number" || Number.isNaN(value)) bad(`${q.id}: Enter a number.`);
      if ((q.min !== undefined && (value as number) < q.min) || (q.max !== undefined && (value as number) > q.max)) bad(`${q.id}: Enter a number from ${q.min} to ${q.max}.`);
      return value;
    }
    case "date": {
      const text = String(value ?? "");
      if (!/^\d{4}-\d{2}-\d{2}$/.test(text) || Number.isNaN(Date.parse(`${text}T00:00:00Z`)) || new Date(`${text}T00:00:00Z`).toISOString().slice(0, 10) !== text) bad(`${q.id}: Enter a date as YYYY-MM-DD.`);
      return text;
    }
    case "text": {
      if (typeof value !== "string") bad(`${q.id}: Enter text.`);
      const text = (value as string).trim();
      if (text.length > (q.maxLength ?? 2000)) bad(`${q.id}: At most ${(q.maxLength ?? 2000).toLocaleString()} characters.`);
      return text;
    }
    case "list": {
      const items = typeof value === "string" ? value.split(/[,\n]/) : value;
      if (!Array.isArray(items) || !items.every((v) => typeof v === "string")) bad(`${q.id}: Enter a list of names or patterns.`);
      const clean = [...new Set((items as string[]).map((v) => v.trim()).filter(Boolean))];
      if (clean.length > 50 || clean.some((v) => v.length > 120)) bad(`${q.id}: At most 50 entries of 120 characters.`);
      return clean;
    }
    case "name": if (typeof value !== "string" || !NAME.test(value.trim())) bad(`${q.id}: Letters, digits and underscores, starting with a letter.`); return (value as string).trim();
    case "principalMap": {
      if (!value || typeof value !== "object" || Array.isArray(value)) bad(`${q.id}: Enter a Microsoft Entra user or group for each SQL user, or leave it blank.`);
      const rows = new Map(((facts[q.rowsFrom ?? ""] as string[]) ?? []).map((r) => [r.toLowerCase(), r]));
      const out: Record<string, string> = {};
      for (const [key, target] of Object.entries(value as Record<string, unknown>)) {
        const name = rows.get(key.toLowerCase());
        if (!name) bad(`${q.id}: ${key} is not one of the SQL users discovered.`);
        if (typeof target !== "string") bad(`${q.id}: Enter a Microsoft Entra user or group for ${name}.`);
        const principal = (target as string).split(/\s+/).filter(Boolean).join(" ");
        if (!principal) continue;
        if (principal.length > 256 || /[[\]]/.test(principal)) bad(`${q.id}: ${principal.slice(0, 40)} is not a Microsoft Entra user or group name.`);
        out[name as string] = principal;
      }
      return Object.fromEntries(Object.entries(out).sort(([a], [b]) => a.localeCompare(b)));
    }
    default: return bad(`Not a question type: ${q.type}`);
  }
}

export function display(q: RawQuestion | undefined, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (!q) return String(value);
  if (q.type === "choice") return q.options?.find((o) => o.value === value)?.label ?? String(value);
  if (q.type === "multi") {
    const labels = new Map((q.options ?? []).map((o) => [o.value, o.label ?? o.value]));
    return (value as string[]).map((v) => labels.get(v) ?? (v.includes(":") ? v.split(":").slice(1).join(":") : v)).join(", ") || "None";
  }
  if (q.type === "boolean") return value ? "Yes" : "No";
  if (q.type === "principalMap") return Object.entries(value as Record<string, string>).map(([u, p]) => `${u} → ${p}`).join("; ") || "None mapped";
  if (q.type === "list") return (value as string[]).join(", ") || "None";
  if (q.type === "number") return (value as number).toLocaleString("en-US");
  return String(value) || "None";
}

// ---- the engine: as consult/engine.py -----------------------------------------------------------------------------

export interface AnswerRecord {
  type: "answer" | "skip" | "withdraw" | "resolve";
  seq: number;
  question?: string;
  conflict?: string;
  value?: unknown;
  source?: AnswerSource;
  note?: string | null;
  facts?: Facts;
  answers?: Values;
  resolution?: "take" | "keep";
  reason?: string;
  by: Actor;
  at: string;
  catalogVersion: string;
}
interface QState extends ConsultQuestion { required: boolean; optional: boolean }
interface EngineState { questions: Map<string, QState>; values: Values; conflicts: ConsultConflict[]; next: string | null; blockers: string[] }

const evidenceOf = (q: RawQuestion, facts: Facts): Facts =>
  Object.fromEntries((q.evidence ?? []).filter((n) => n in FACT_DESCRIPTIONS).map((n) => [n, facts[n] ?? null]));
function conflictEvidence(c: RawConflict, facts: Facts): Facts {
  const names = new Set([...(c.evidence ?? []), ...[...JSON.stringify(c.when).matchAll(/"fact":"([^"]+)"/g)].map((m) => m[1])]);
  return Object.fromEntries([...names].sort().filter((n) => n in FACT_DESCRIPTIONS || n in ENVIRONMENT_FACTS).map((n) => [n, facts[n] ?? null]));
}
function recommendation(q: RawQuestion, facts: Facts, values: Values): { value: unknown; reason: string } | null {
  for (const rule of q.defaults ?? []) {
    if (!holds(rule.when, facts, values)) continue;
    if (typeof rule.value === "string" && rule.value.includes("{") && !known(rule.value, facts, values)) return null;
    const value = typeof rule.value === "string" && rule.value.includes("{") ? render(rule.value, facts, values) : rule.value;
    try {
      return { value: check(q, value, facts), reason: render(rule.reason, facts, values) };
    } catch {
      return null;
    }
  }
  return null;
}
function fold(records: AnswerRecord[]) {
  const answers = new Map<string, AnswerRecord>(), skips = new Map<string, AnswerRecord>(), resolutions = new Map<string, AnswerRecord>();
  for (const r of records) {
    const q = r.question ?? "";
    if (r.type === "answer") { answers.set(q, r); skips.delete(q); }
    else if (r.type === "withdraw") { answers.delete(q); skips.delete(q); }
    else if (r.type === "skip") skips.set(q, r);
    else if (r.type === "resolve") resolutions.set(r.conflict ?? "", r);
  }
  return { answers, skips, resolutions };
}
const describeFact = (n: string) => FACT_DESCRIPTIONS[n] ?? ENVIRONMENT_FACTS[n] ?? n;

export function evaluateAll(facts: Facts, records: AnswerRecord[]): EngineState {
  const { answers, skips, resolutions } = fold(records);
  const values: Values = {};
  const questions = new Map<string, QState>();
  for (const q of CAT.questions) {
    const applies = holds(q.appliesWhen, facts, values);
    const answer = answers.get(q.id);
    const base = { id: q.id, applies, value: null as unknown, display: null as string | null, source: null as AnswerSource | null, by: null as Actor | null,
      at: null as string | null, note: null as string | null, recommendation: null as { value: unknown; reason: string } | null, changed: [] as FactChange[],
      conflicts: [] as string[], choices: q.optionsFrom ? choicesOf(q, facts) : null, rows: q.rowsFrom ? ((facts[q.rowsFrom] as string[]) ?? []) : null,
      evidence: (q.evidence ?? []).map((n) => ({ fact: n, description: describeFact(n), value: facts[n] ?? null })),
      required: !!q.required, optional: !!q.optional };
    if (!applies) {
      questions.set(q.id, { ...base, status: answer ? "parked" : "not_applicable", value: answer?.value ?? null, source: answer?.source ?? null });
      continue;
    }
    const rec = recommendation(q, facts, values);
    let st: QState;
    if (answer) {
      const kept = answer.facts ?? {};
      const changed = Object.keys(kept).sort().filter((n) => !same(kept[n], facts[n] ?? null))
        .map((n) => ({ fact: n, description: describeFact(n), before: kept[n], after: facts[n] ?? null }));
      st = { ...base, status: changed.length ? "stale" : "answered", value: answer.value, source: answer.source ?? "person", by: answer.by, at: answer.at,
        note: answer.note ?? null, recommendation: rec, changed };
    } else if (skips.has(q.id)) {
      st = { ...base, status: "skipped", value: rec?.value ?? null, recommendation: rec, by: skips.get(q.id)!.by, at: skips.get(q.id)!.at };
    } else if (rec) {
      st = { ...base, status: "assumed", value: rec.value, recommendation: rec };
    } else {
      st = { ...base, status: "open" };
    }
    if (st.value !== null && st.value !== undefined) values[q.id] = st.value;
    st.display = st.value !== null && st.value !== undefined ? display(q, st.value) : null;
    questions.set(q.id, st);
  }
  const conflicts: ConsultConflict[] = [];
  for (const c of CAT.conflicts) {
    if (!holds(c.when, facts, values)) continue;
    const evidence = conflictEvidence(c, facts);
    const involved = Object.fromEntries((c.questions ?? []).map((q) => [q, values[q] ?? null]));
    const kept = resolutions.get(c.id);
    const isKept = !!kept && kept.resolution === "keep" && same(kept.facts ?? {}, evidence) && same(kept.answers ?? {}, involved);
    conflicts.push({ id: c.id, status: isKept ? "kept" : "open", message: render(c.message, facts, values), questions: [...(c.questions ?? [])],
      evidence: Object.entries(evidence).map(([fact, value]) => ({ fact, description: describeFact(fact), value })),
      reason: isKept ? kept!.reason ?? null : null, by: isKept ? kept!.by : null, at: isKept ? kept!.at : null,
      resolutions: c.resolutions.map((r) => ({ kind: r.kind, label: r.label, question: r.question ?? null })) });
    for (const q of c.questions ?? []) questions.get(q)?.conflicts.push(c.id);
  }
  const person = (s: QState) => s.status === "answered" && !!s.source;
  const blockers = conflicts.filter((c) => c.status === "open").map((c) => `Contradiction: ${c.message}`);
  for (const q of CAT.questions) {
    const s = questions.get(q.id)!;
    if (!s.applies) continue;
    if (s.status === "stale") blockers.push(`${q.id}: what it rests on changed since it was answered; confirm it again.`);
    else if (q.required && !person(s)) blockers.push(`${q.id}: ${q.text} A person answers this one.`);
    else if ((s.value === null || s.value === undefined) && !q.optional) blockers.push(`${q.id}: ${q.text} It needs an answer.`);
  }
  const live = CAT.questions.filter((q) => questions.get(q.id)!.applies);
  let next: string | null = null;
  for (const c of conflicts.filter((x) => x.status === "open")) {
    next = live.find((q) => c.questions.includes(q.id))?.id ?? null;
    if (next) break;
  }
  const tiers: ((q: RawQuestion, s: QState) => boolean)[] = [
    (q, s) => !!q.required && !person(s), (q, s) => s.status === "open" && !q.optional, (_q, s) => s.status === "stale",
    (_q, s) => s.status === "assumed", (_q, s) => s.status === "skipped",
  ];
  for (const tier of tiers) {
    if (next) break;
    next = live.find((q) => tier(q, questions.get(q.id)!))?.id ?? null;
  }
  return { questions, values, conflicts, next, blockers };
}

export function settingsOf(st: EngineState): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const q of CAT.questions) {
    const s = st.questions.get(q.id)!;
    if (!s.applies || s.value === null || s.value === undefined) continue;
    const option = q.type === "choice" ? q.options?.find((o) => o.value === s.value) : undefined;
    for (const name of q.drives) out[name] = option?.sets && name in option.sets ? option.sets[name] : s.value;
  }
  return Object.fromEntries(Object.keys(out).sort().map((k) => [k, out[k]]));
}

function preview(facts: Facts, records: AnswerRecord[], record: AnswerRecord): AnswerPreview["preview"] {
  const before = evaluateAll(facts, records), after = evaluateAll(facts, [...records, record]);
  const opens: string[] = [], parks: string[] = [], revives: string[] = [];
  for (const q of CAT.questions) {
    const b = before.questions.get(q.id)!, a = after.questions.get(q.id)!;
    if (!b.applies && a.applies) (b.status === "parked" ? revives : opens).push(q.id);
    else if (b.applies && !a.applies && a.status === "parked") parks.push(q.id);
  }
  const was = new Set(before.conflicts.filter((c) => c.status === "open").map((c) => c.id));
  const now = new Set(after.conflicts.filter((c) => c.status === "open").map((c) => c.id));
  const old = settingsOf(before), neu = settingsOf(after);
  const settings = [...new Set([...Object.keys(old), ...Object.keys(neu)])].sort().filter((k) => !same(old[k], neu[k]))
    .map((k) => ({ setting: k, before: old[k] ?? null, after: neu[k] ?? null }));
  return { opens, parks, revives, conflictsOpened: [...now].filter((c) => !was.has(c)).sort(), conflictsClosed: [...was].filter((c) => !now.has(c)).sort(), settings };
}

// ---- the brief: as consult/brief.py ---------------------------------------------------------------------------------

/** A stable digest, for a version's content hash and the facts fingerprint (Demo: FNV-1a, not SHA-256). */
function digest(text: string, length = 64): string {
  let out = "";
  for (let round = 0; out.length < length; round++) {
    let h = 0x811c9dc5 ^ round;
    for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    out += h.toString(16).padStart(8, "0");
  }
  return out.slice(0, length);
}
const canonical = (v: unknown): string => JSON.stringify(v, (_k, x) => (x && typeof x === "object" && !Array.isArray(x)
  ? Object.fromEntries(Object.keys(x).sort().map((k) => [k, (x as Record<string, unknown>)[k]])) : x));
const estateOf = (facts: Facts) => Object.fromEntries(Object.keys(facts).filter((k) => k in FACT_DESCRIPTIONS).sort().map((k) => [k, facts[k]]));
export const fingerprint = (facts: Facts) => digest(canonical(estateOf(facts)), 16);
function changes(before: Facts, after: Facts): FactChange[] {
  const a = estateOf(before), b = estateOf(after);
  return [...new Set([...Object.keys(a), ...Object.keys(b)])].sort().filter((k) => !same(a[k] ?? null, b[k] ?? null))
    .map((k) => ({ fact: k, description: describeFact(k), before: a[k] ?? null, after: b[k] ?? null }));
}

function draft(facts: Facts, st: EngineState): BriefContent {
  const titles = new Map(CAT.sections.map((s) => [s.id, s.title]));
  const decisions: BriefDecision[] = [];
  for (const q of CAT.questions) {
    const s = st.questions.get(q.id)!;
    if (!s.applies || s.value === null || s.value === undefined) continue;
    const assumed = s.status === "assumed" || s.status === "skipped";
    decisions.push({ question: q.id, section: q.section, sectionTitle: titles.get(q.section) ?? q.section, text: q.text, value: s.value,
      display: display(q, s.value), assumed, source: assumed ? null : s.source, by: assumed ? null : s.by, at: assumed ? null : s.at,
      reason: !assumed && s.note ? s.note : s.recommendation?.reason ?? null,
      drives: q.drives.map((n) => ({ setting: n, status: SETTING_STATUS[n], applies: SETTING_APPLIES[n] })) });
  }
  const settings = settingsOf(st);
  return {
    catalogVersion: CAT.version, factsFingerprint: fingerprint(facts), facts: estateOf(facts), snapshot: (facts["discovery.snapshot"] as string) ?? null,
    decisions, settings, enforced: Object.fromEntries(Object.entries(settings).filter(([k]) => SETTING_STATUS[k] === ENFORCED)),
    assumptions: decisions.filter((d) => d.assumed).map((d) => d.question),
    recordedOnly: Object.entries(settings).filter(([k]) => SETTING_STATUS[k] !== ENFORCED)
      .map(([k, value]) => ({ setting: k, value, status: SETTING_STATUS[k], applies: SETTING_APPLIES[k] })),
    keptContradictions: st.conflicts.filter((c) => c.status === "kept").map((c) => ({ conflict: c.id, message: c.message, reason: c.reason ?? "", by: c.by, at: c.at })),
    separateApprover: CAT.questions.filter((q) => q.separateApprover && st.questions.get(q.id)!.applies && holds(q.separateApprover.when, facts, st.values))
      .map((q) => ({ question: q.id, reason: q.separateApprover!.reason })),
  };
}
const contentHash = (c: BriefContent) => digest(canonical({
  catalogVersion: c.catalogVersion, decisions: c.decisions.map((d) => [d.question, d.value, d.source, d.assumed]), settings: c.settings,
  kept: c.keptContradictions.map((k) => [k.conflict, k.reason]),
}));

const valueText = (v: unknown): string => typeof v === "boolean" ? (v ? "yes" : "no") : Array.isArray(v) ? v.join(", ") || "none"
  : v && typeof v === "object" ? Object.entries(v).map(([k, x]) => `${k} → ${x}`).join("; ") || "none" : v === null || v === undefined || v === "" ? "—" : String(v);
const cell = (t: unknown) => String(t).replace(/\|/g, "\\|").replace(/\n/g, " ");
const WHO: Record<string, string> = { person: "answered", recommendation: "recommendation accepted", finding: "finding taken",
  ai: "AI proposal confirmed", rules: "read from notes, confirmed", import: "imported answer confirmed" };

interface StoredVersion { version: number; hash: string; content: BriefContent; submittedAt: string; submittedBy: Actor; note: string | null }
interface BriefEvent { event: "submitted" | "approved" | "rejected" | "superseded"; version: number; by: Actor; at: string; comment?: string | null;
  selfApproved?: boolean; separateApprover?: string[]; hash?: string }

function toMarkdown(v: StoredVersion, events: BriefEvent[]): string {
  const c = v.content;
  const lines = [`# Migration brief, version ${v.version}`, ""];
  const decided = events.filter((e) => e.version === v.version && (e.event === "approved" || e.event === "rejected"));
  const status = [...events].reverse().find((e) => e.version === v.version)?.event ?? "submitted";
  if (decided.length) {
    const last = decided[decided.length - 1];
    lines.push(`**${last.event.charAt(0).toUpperCase()}${last.event.slice(1)}** by ${last.by.id} on ${last.at}${last.selfApproved ? " (the person who submitted it)" : ""}${last.comment ? `: ${last.comment}` : ""}.`);
  } else {
    lines.push(`**${status.charAt(0).toUpperCase()}${status.slice(1)}** on ${v.submittedAt} by ${v.submittedBy.id}.`);
  }
  const where = [c.facts["source.kind"], c.facts["source.ref"], c.facts["source.commit"]].filter(Boolean).join(", ");
  lines.push("", `Made for ${c.facts["source.workspace"] ?? "the source"}${where ? ` (${where})` : ""}, discovery snapshot ${c.snapshot ?? "—"}, facts fingerprint \`${c.factsFingerprint}\`. Catalog ${c.catalogVersion}, content hash \`${v.hash.slice(0, 16)}\`.`, "", "## Decisions", "");
  let section: string | null = null;
  for (const d of c.decisions) {
    if (d.sectionTitle !== section) { if (section !== null) lines.push(""); lines.push(`### ${d.sectionTitle}`, ""); section = d.sectionTitle; }
    const who = d.assumed ? "assumed: nobody confirmed it" : WHO[d.source ?? ""] ?? String(d.source);
    lines.push(`- **${d.text}** ${d.display} (${who}${d.by ? ` by ${d.by.id}` : ""}).${d.reason ? ` ${d.reason}` : ""}`);
  }
  lines.push("", "## What the accelerator acts on", "", "| Setting | Value | Acted on by |", "|---|---|---|");
  for (const [k, value] of Object.entries(c.enforced)) lines.push(`| \`${k}\` | ${cell(valueText(value))} | ${cell(SETTING_APPLIES[k])} |`);
  if (c.assumptions.length) lines.push("", "## Assumptions", "", `Recommendations nobody confirmed: ${c.assumptions.join(", ")}.`);
  if (c.recordedOnly.length) lines.push("", "## Recorded, not acted on", "", ...c.recordedOnly.map((r) => `- \`${r.setting}\` (${r.status}): ${valueText(r.value)}. ${r.applies}.`));
  if (c.keptContradictions.length) lines.push("", "## Contradictions kept, with their reasons", "", ...c.keptContradictions.map((k) => `- ${k.message} Kept by ${k.by?.id ?? "—"}: ${k.reason}`));
  if (c.separateApprover.length) lines.push("", "## Decisions that ask for a separate approver", "", ...c.separateApprover.map((x) => `- ${x.question}: ${x.reason}`));
  lines.push("", "## The estate it rests on", "", "| Fact | Value |", "|---|---|");
  for (const [k, value] of Object.entries(c.facts)) if (!Array.isArray(value) && !(value && typeof value === "object")) lines.push(`| ${cell(describeFact(k))} | ${cell(valueText(value))} |`);
  return `${lines.join("\n")}\n`;
}

// ---- the policy: as consult/policy.py --------------------------------------------------------------------------------

const PARTS: Record<string, string[]> = {
  structure: ["warehouse", "security", "shortcuts"], data: ["data"],
  orchestration: ["spark", "notebooks", "connections", "pipelines", "spark_jobs", "sql_scripts", "schedules"],
};
const RUN_OPTIONS: [string, keyof RunOptions][] = [["run.stopOnFailure", "stopOnFailure"], ["run.waveGate", "waveGate"], ["run.existing", "existing"],
  ["warehouse.collation", "collation"], ["data.mode", "dataMode"], ["data.run", "dataRun"], ["data.syncOverlap", "syncOverlap"], ["release.commitScope", "commitScope"]];
const SETTING_OF: Record<string, string> = { ...Object.fromEntries(RUN_OPTIONS.map(([s, k]) => [k, s])), stages: "run.parts", gitCommit: "release.gitCommit" };
const LABELS: Record<string, string> = { stages: "Stages", stopOnFailure: "Stop at a wave with failures", waveGate: "Sign off each wave",
  existing: "Objects already in Fabric", collation: "Warehouse collation", dataMode: "Tables that already have rows", dataRun: "Load pipelines",
  syncOverlap: "Sync re-read window", commitScope: "What the Fabric Git commit takes", gitCommit: "Commit to Fabric Git" };
const CHANGE = /(modif|updat|chang|last_?mod|_ts$|^ts$|timestamp)/i;
const EVENT = /(creat|insert|load|order|transaction|event|posted|booked|_date$|^date)/i;
const DEPTHS: Record<string, { probe: boolean; run: RunnableKind[] }> = {
  compare: { probe: false, run: [] }, probe: { probe: true, run: [] }, run: { probe: true, run: ["pipeline", "notebook", "script", "sparkjob"] } };
const said = (v: unknown) => typeof v === "boolean" ? (v ? "yes" : "no") : Array.isArray(v) ? v.join(", ") || "none" : v === null || v === undefined || v === "" ? "nothing" : String(v);

/** What Plan's Add all and the differences are judged on: each object, its scope groups and what the estate says of it. */
export interface DemoEstate { items: { id: string; name: string }[]; groupOf: Record<string, string[]>; flags: Record<string, string[]>; filterable: FilterableTable[] }

class Policy {
  constructor(readonly version: number, readonly hash: string, readonly settings: Record<string, unknown>, readonly made: Facts,
    readonly approvedBy: Actor | null, readonly approvedAt: string | null, now: Facts) {
    this.stale = Object.keys(now).length > 0 && fingerprint(now) !== fingerprint(made);
    this.changes = this.stale ? changes(made, now) : [];
  }
  readonly stale: boolean;
  readonly changes: FactChange[];
  get = (name: string) => this.settings[name];
  stages(): string[] | null {
    const parts = this.get("run.parts") as string[] | undefined;
    if (!parts) return null;
    const on = new Set(parts.flatMap((p) => PARTS[p] ?? []));
    if (this.get("security.migrate") === false) on.delete("security");
    return STAGE_KEYS.filter((k) => on.has(k));
  }
  runOptions(): Partial<RunOptions> {
    const out: Record<string, unknown> = {};
    const stages = this.stages();
    if (stages) out.stages = stages;
    for (const [setting, key] of RUN_OPTIONS) if (setting in this.settings) out[key] = this.settings[setting];
    if ("release.gitCommit" in this.settings) out.gitCommit = this.settings["release.gitCommit"] ? "auto" : "off";
    return out as Partial<RunOptions>;
  }
  dataFilters(tables: FilterableTable[]): Record<string, DataFilterInput> {
    const since = this.get("data.historyMode") === "since" ? this.get("data.historyFrom") as string : null;
    const sync = !!this.get("data.sync");
    const out: Record<string, DataFilterInput> = {};
    for (const t of tables) {
      const dates = t.dateColumns.map((c) => c.name);
      let entry: DataFilterInput | null = null;
      if (since && dates.length) entry = { column: dates.find((n) => EVENT.test(n)) ?? dates.find((n) => CHANGE.test(n)) ?? dates[0], from: since, to: "", sync: false, changeColumn: "", keys: [] };
      const change = sync ? dates.find((n) => CHANGE.test(n)) : undefined;
      if (change) entry = { ...(entry ?? { column: "", from: "", to: "", sync: false, changeColumn: "", keys: [] }), sync: true, changeColumn: change, keys: [...t.keyColumns] };
      if (entry) out[t.key] = entry;
    }
    return out;
  }
  excluded(item: { id: string; name: string }, estate: DemoEstate): string | null {
    const marks = estate.flags[item.id] ?? [];
    if (marks.includes("serverless") && this.get("plan.serverless") === "leave_out") return "Written for the serverless SQL pool: the brief leaves it out (A3).";
    if (marks.includes("multiStatement") && this.get("code.multiStatement") === "leave_out") return "A multi-statement table-valued function: the brief leaves these out (D3).";
    if (marks.includes("rebuild") && this.get("code.rebuild") === "leave_out") return "No Fabric equivalent as it is: the brief leaves these out (D4).";
    const mode = this.get("plan.scopeMode");
    if (mode === "include") {
      const chosen = (this.get("plan.scopeInclude") as string[]) ?? [];
      const groups = estate.groupOf[item.id] ?? [];
      if (groups.length && !groups.some((g) => chosen.some((c) => g === c || (c.startsWith("folder:") && g.startsWith(`${c}/`))))) return "Outside the schemas and folders the brief puts in scope (A2).";
    }
    if (mode === "exclude") {
      const short = item.name.split(".").pop() ?? item.name;
      for (const pattern of (this.get("plan.scopeExclude") as string[]) ?? []) {
        const re = new RegExp(`^${pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`, "i");
        if (re.test(short) || re.test(item.name)) return `Its name matches '${pattern}', which the brief leaves out (A2).`;
      }
    }
    return null;
  }
  differences(options: Partial<RunOptions>, leftOut: Record<string, string>, tables: FilterableTable[] | null): PlanDifference[] {
    const out: PlanDifference[] = [];
    for (const [key, value] of Object.entries(this.runOptions())) {
      const have = (options as Record<string, unknown>)[key];
      const alike = key === "stages" ? same([...((have as string[]) ?? [])].sort(), [...(value as string[])].sort()) : same(have ?? (key === "waveGate" || key === "stopOnFailure" ? false : have), value);
      if (!alike) out.push({ option: key, setting: SETTING_OF[key] ?? null, brief: value, plan: have ?? null, message: `${LABELS[key] ?? key}: the brief says ${said(value)}, the plan has ${said(have)}.` });
    }
    const ids = Object.keys(leftOut).sort();
    if (ids.length) out.push({ option: "items", setting: "plan.scope", brief: [], plan: ids.slice(0, 50),
      message: `${ids.length} object${ids.length === 1 ? "" : "s"} the brief leaves out ${ids.length === 1 ? "is" : "are"} in the plan: ${leftOut[ids[0]]}${ids.length > 1 ? " (and others)" : ""}` });
    if (this.get("storage.routes") === "none" && options.storageRoutes?.length) out.push({ option: "storageRoutes", setting: "storage.routes", brief: [], plan: options.storageRoutes.length,
      message: "Storage routes: the brief leaves storage where it is, the plan routes it through OneLake." });
    if (tables && ("data.historyMode" in this.settings || "data.sync" in this.settings)) {
      const wanted = this.dataFilters(tables);
      const shape = (f?: DataFilterInput) => f ? [f.column.toLowerCase(), f.from, f.to, f.sync, f.sync ? f.changeColumn.toLowerCase() : "", f.sync ? f.keys.map((k) => k.toLowerCase()) : []] : ["", "", "", false, "", []];
      const moved = tables.map((t) => t.key).sort().filter((k) => !same(shape(wanted[k]), shape(options.dataFilters?.[k])));
      if (moved.length) out.push({ option: "dataFilters", setting: "data.historyMode", brief: Object.keys(wanted).length, plan: moved.slice(0, 50),
        message: `Date filters: ${moved.length} table${moved.length === 1 ? "" : "s"} load${moved.length === 1 ? "s" : ""} differently from the brief (such as ${moved[0]}).` });
    }
    return out;
  }
}

// ---- the rules that read answers from notes ---------------------------------------------------------------------------

const RULES: [string, unknown, RegExp][] = ([
  ["A1", ["structure"], /\b(structure|schema|tables?) only\b|\bno data\b|\bwithout (the )?data\b/i],
  ["A1", ["structure", "data"], /\b(no|without|skip) (the )?(pipelines|orchestration|notebooks)\b/i],
  ["A5", "update", /\b(update|overwrite) (what is |objects |items )?(already )?(there|in fabric|existing)\b|\bupdate in place\b/i],
  ["A5", "skip", /\b(never|don'?t|do not) (overwrite|update)\b/i],
  ["B1", "case_sensitive", /\bcase[- ]sensitive\b(?! *(?:is|was) not)/i],
  ["B1", "case_insensitive", /\bcase[- ]insensitive\b/i],
  ["C2", true, /\b(keep (it |them |the tables )?in sync|incremental(ly)?|sync (it|them|the tables))\b/i],
  ["C2", false, /\b(one[- ]off|one[- ]time|single) (load|copy|migration)\b/i],
  ["C4", "replace", /\b(replace|truncate|reload) (the )?(rows|data|tables)\b/i],
  ["C5", "create", /\b(only create|create only|don'?t run|do not run) (the )?(load )?pipelines\b/i],
  ["D1", false, /\b(no (ai|llm|claude|gen ?ai)|don'?t send|do not send|never send|not (allowed|permitted) to send)\b/i],
  ["D1", true, /\b(ai|llm|claude) (is )?(fine|ok|okay|allowed|welcome)\b|\buse (ai|an llm|claude)\b/i],
  ["E1", false, /\b(skip|no|without) (the )?(security|permissions|roles)\b/i],
  ["G1", false, /\b(no|without|skip) (fabric )?git\b|\bdon'?t commit\b/i],
  ["G1", true, /\bcommit (it |each run |every run )?to (fabric )?git\b/i],
  ["G3", "always", /\b(sign[- ]off|approv\w*) (for |on |before )?(every|each|all) runs?\b|\balways (sign[- ]off|approve)\b|\b(every|each) run (must|should|has to|needs to) be (signed[- ]off|approved)\b/i],
  ["G3", "never", /\bno (sign[- ]off|approvals?)\b/i],
  ["G5", "dev_test_prod", /\b(test and prod|dev,? test,? (and )?prod)\b/i],
  ["G5", "dev_test", /\bdev and test\b/i],
  ["H1", "run", /\b(run (the )?(pipelines|notebooks) (in|during|to) valid\w*|full validation)\b/i],
  ["H1", "compare", /\b(compare only|just compare)\b/i],
] as [string, unknown, RegExp][]);

export function ruleProposals(text: string, today = new Date()): { question: string; value: unknown; reason: string }[] {
  const out = new Map<string, { question: string; value: unknown; reason: string }>();
  const add = (question: string, value: unknown, words: string) => { if (!out.has(question)) out.set(question, { question, value, reason: `You wrote: "${words.trim()}".` }); };
  for (const [q, v, re] of RULES) { const m = re.exec(text); if (m) add(q, v, m[0]); }
  const years = /\b(?:last|past|previous)\s+(\d{1,2})\s+years?\b/i.exec(text);
  const since = /\b(?:since|from|after)\s+(\d{4}-\d{2}-\d{2}|\d{4})\b/i.exec(text);
  if (years) {
    const d = new Date(Date.UTC(today.getUTCFullYear() - Number(years[1]), today.getUTCMonth(), Math.min(today.getUTCDate(), 28)));
    add("C1", "since", years[0]);
    add("C1D", d.toISOString().slice(0, 10), years[0]);
  } else if (since) {
    add("C1", "since", since[0]);
    add("C1D", since[1].length === 10 ? since[1] : `${since[1]}-01-01`, since[0]);
  }
  const budget = /(?:budget|spend|up to|at most|max(?:imum)?)\D{0,20}\$\s?(\d{1,5})|\$\s?(\d{1,5})\s*(?:budget|for ai|for the ai|on ai)/i.exec(text);
  if (budget) add("D2", Number(budget[1] ?? budget[2]), budget[0]);
  return [...out.values()];
}

// ---- the demo consultation itself -------------------------------------------------------------------------------------

/** One Demo project's consultation: its answers, proposals, brief versions and deviations, in memory like the rest of Demo data. */
export class DemoConsult {
  private records: AnswerRecord[] = [];
  private proposals: (ConsultProposal & { decided?: "confirmed" | "rejected" })[] = [];
  private versions: StoredVersion[] = [];
  private events: BriefEvent[] = [];
  private deviations: { briefHash: string; keys: Set<string>; reason: string; by: Actor; at: string }[] = [];
  private batch = 0;

  constructor(private readonly person: Actor, private readonly audit: (action: string, details: Record<string, unknown>, target?: { type: string; id: string | null }) => void) {}

  catalog(): ConsultCatalog {
    return {
      version: CAT.version, sections: CAT.sections,
      questions: CAT.questions.map((q) => ({ id: q.id, section: q.section, type: q.type, text: q.text, help: q.help ?? "",
        options: (q.options ?? []).map((o) => ({ value: o.value, label: o.label ?? o.value, description: o.description ?? "" })),
        optionsFrom: q.optionsFrom ?? null, rowsFrom: q.rowsFrom ?? null, required: !!q.required, optional: !!q.optional,
        drives: q.drives.map((n) => ({ setting: n, status: SETTING_STATUS[n], applies: SETTING_APPLIES[n] })), min: q.min ?? null, max: q.max ?? null,
        maxLength: q.maxLength ?? 2000, separateApprover: q.separateApprover?.reason ?? null })),
      conflicts: CAT.conflicts.map((c) => ({ id: c.id, questions: c.questions ?? [], resolutions: c.resolutions.map((r) => ({ kind: r.kind, label: r.label, question: r.question ?? null })) })),
    };
  }

  private now = () => new Date().toISOString();
  private write(r: Omit<AnswerRecord, "seq" | "by" | "at" | "catalogVersion">): void {
    this.records.push({ ...r, seq: this.records.length + 1, by: this.person, at: this.now(), catalogVersion: CAT.version } as AnswerRecord);
  }
  private question(id: string): RawQuestion {
    const q = QUESTION.get(id);
    if (!q) throw new ApiRequestError("not_found", "There is no such question.", 404);
    return q;
  }
  private applicable(st: EngineState, q: RawQuestion) {
    if (!st.questions.get(q.id)!.applies) throw new ApiRequestError("not_applicable", `${q.id} does not apply to this estate as it is answered now.`, 409);
  }

  private status(n: number): BriefVersion["status"] { return ([...this.events].reverse().find((e) => e.version === n)?.event ?? "submitted") as BriefVersion["status"]; }
  private approvedNumber(): number | null {
    const approved = this.versions.map((v) => v.version).filter((n) => this.status(n) === "approved");
    return approved.length ? Math.max(...approved) : null;
  }
  inForce(facts: Facts): Policy | null {
    const n = this.approvedNumber();
    const v = n === null ? undefined : this.versions.find((x) => x.version === n);
    if (!v) return null;
    const approval = [...this.events].reverse().find((e) => e.version === n && e.event === "approved");
    return new Policy(v.version, v.hash, v.content.settings, v.content.facts, approval?.by ?? null, approval?.at ?? null, facts);
  }

  private versionsView(): BriefVersion[] {
    return this.versions.map((v) => {
      const decided = [...this.events].reverse().find((e) => e.version === v.version && (e.event === "approved" || e.event === "rejected"));
      return { version: v.version, hash: v.hash, status: this.status(v.version), submittedAt: v.submittedAt, submittedBy: v.submittedBy, note: v.note,
        decidedBy: decided?.by ?? null, decidedAt: decided?.at ?? null, comment: decided?.comment ?? null, selfApproved: decided?.selfApproved ?? null,
        catalogVersion: v.content.catalogVersion, factsFingerprint: v.content.factsFingerprint, snapshot: v.content.snapshot,
        decisions: v.content.decisions.length, assumptions: v.content.assumptions.length, separateApprover: v.content.separateApprover.map((x) => x.question) };
    });
  }

  private summary(facts: Facts, st: EngineState): BriefSummary {
    const content = draft(facts, st);
    const hash = contentHash(content);
    const policy = this.inForce(facts);
    const approved = policy ? this.versions.find((v) => v.version === policy.version) : undefined;
    const pending = [...this.versionsView()].reverse().find((v) => v.status === "submitted")?.version ?? null;
    const before = new Map((approved?.content.decisions ?? []).map((d) => [d.question, d]));
    const after = new Map(content.decisions.map((d) => [d.question, d]));
    const changed = approved ? [...new Set([...before.keys(), ...after.keys()])].flatMap((q) => {
      const a = before.get(q), b = after.get(q);
      if (a && b && same(a.value, b.value) && a.assumed === b.assumed) return [];
      return [{ question: q, text: (b ?? a)!.text, before: a?.display ?? null, after: b?.display ?? null, kind: (!a ? "added" : !b ? "removed" : "changed") as "added" | "removed" | "changed" }];
    }) : [];
    return { draftHash: hash, versions: this.versions.length, pending, approved: policy?.version ?? null, approvedHash: policy?.hash ?? null,
      approvedBy: policy?.approvedBy ?? null, approvedAt: policy?.approvedAt ?? null, stale: !!policy?.stale, factChanges: policy?.changes ?? [],
      sameAsApproved: !!policy && policy.hash === hash, changes: changed };
  }

  private pending(st: EngineState): ConsultProposal[] {
    const latest = new Map<string, ConsultProposal & { decided?: string }>();
    for (const p of this.proposals) latest.set(p.question, p);
    return [...latest.values()].filter((p) => {
      const s = st.questions.get(p.question);
      return !p.decided && s?.applies && !(s.status === "answered" && same(s.value, p.value));
    }).map((p) => ({ ...p, current: st.questions.get(p.question)?.display ?? null }))
      .sort((a, b) => CAT.questions.findIndex((q) => q.id === a.question) - CAT.questions.findIndex((q) => q.id === b.question));
  }

  llmGate(purpose: "transform" | "consult", mode: "required" | "advisory"): LlmGate {
    const { answers } = fold(this.records);
    const live = answers.has("D1") ? answers.get("D1")!.value as boolean : null;
    const liveBudget = answers.has("D2") ? answers.get("D2")!.value as number : null;
    const policy = this.inForce({});
    let consent = policy ? (policy.get("llm.consent") as boolean | undefined) ?? null : null;
    let budget = policy && consent ? (policy.get("llm.budgetUsd") as number | undefined) ?? null : null;
    if (purpose === "consult" && live !== null) consent = live;
    if (liveBudget !== null && (budget === null || liveBudget < budget)) budget = liveBudget;
    if (budget === null && consent) budget = 25;
    const out: LlmGate = { purpose, mode, consent, liveConsent: live, budgetUsd: budget, spentUsd: 0, allowed: true, reason: null, code: null };
    if (live === false) return { ...out, allowed: false, code: "llm_no_consent", reason: "D1 says object definitions may not be sent to Claude. Change the answer in Consult to use AI." };
    if (consent === false) return { ...out, allowed: false, code: "llm_no_consent", reason: `The approved brief (version ${policy?.version}) does not allow sending definitions to Claude (D1).` };
    // Demo data sends nothing anywhere, so with nobody asked yet, drafts are simulated as before there were briefs.
    return out;
  }

  state(facts: Facts, mode: "required" | "advisory"): ConsultState {
    const st = evaluateAll(facts, this.records);
    const live = [...st.questions.values()].filter((q) => q.applies);
    return {
      mode, catalogVersion: CAT.version, snapshot: (facts["discovery.snapshot"] as string) ?? null, factsFingerprint: fingerprint(facts),
      facts: [...Object.entries(FACT_DESCRIPTIONS).map(([name, description]) => ({ name, description, scope: "estate" as const, value: facts[name] ?? null })),
        ...Object.entries(ENVIRONMENT_FACTS).map(([name, description]) => ({ name, description, scope: "environment" as const, value: facts[name] ?? null }))],
      questions: CAT.questions.map((q) => { const { required, optional, ...rest } = st.questions.get(q.id)!; void required; void optional; return rest; }),
      conflicts: st.conflicts, next: st.next, blockers: st.blockers, submittable: st.blockers.length === 0,
      progress: { applicable: live.length, answered: live.filter((q) => q.status === "answered").length,
        assumed: live.filter((q) => (q.status === "assumed" || q.status === "skipped") && q.value !== null).length, needsPerson: st.blockers.length,
        open: live.filter((q) => q.status === "open" || q.status === "stale").length, contradictions: st.conflicts.filter((c) => c.status === "open").length },
      seq: this.records.length, brief: this.summary(facts, st), proposals: this.pending(st), llm: this.llmGate("consult", mode),
    };
  }

  answer(facts: Facts, mode: "required" | "advisory", id: string, value: unknown, note?: string, previewOnly = false): ConsultState | AnswerPreview {
    const q = this.question(id);
    const st = evaluateAll(facts, this.records);
    this.applicable(st, q);
    const clean = check(q, value, facts);
    const record = { type: "answer" as const, question: q.id, value: clean, source: "person" as AnswerSource, note: note?.trim() || null, facts: evidenceOf(q, facts) };
    if (previewOnly) return { question: q.id, value: clean, preview: preview(facts, this.records, { ...record, seq: this.records.length + 1, by: this.person, at: this.now(), catalogVersion: CAT.version }) };
    this.write(record);
    this.audit("consult.answered", { question: q.id, value: display(q, clean), source: "person" }, { type: "question", id: q.id });
    return this.state(facts, mode);
  }
  skip(facts: Facts, mode: "required" | "advisory", id: string): ConsultState {
    const q = this.question(id);
    this.applicable(evaluateAll(facts, this.records), q);
    this.write({ type: "skip", question: q.id });
    this.audit("consult.skipped", { question: q.id }, { type: "question", id: q.id });
    return this.state(facts, mode);
  }
  withdraw(facts: Facts, mode: "required" | "advisory", id: string): ConsultState {
    const q = this.question(id);
    const { answers, skips } = fold(this.records);
    if (!answers.has(q.id) && !skips.has(q.id)) throw new ApiRequestError("nothing_to_withdraw", `${q.id} has no answer to withdraw.`, 409);
    this.write({ type: "withdraw", question: q.id });
    this.audit("consult.withdrawn", { question: q.id }, { type: "question", id: q.id });
    return this.state(facts, mode);
  }
  accept(facts: Facts, mode: "required" | "advisory", section?: string): ConsultState {
    const accepted: string[] = [];
    for (let i = 0; i < CAT.questions.length; i++) {
      const st = evaluateAll(facts, this.records);
      const found = CAT.questions.filter((q) => {
        const s = st.questions.get(q.id)!;
        return s.applies && (s.status === "assumed" || s.status === "skipped") && s.recommendation && (!section || q.section === section) && !accepted.includes(q.id);
      });
      if (!found.length) break;
      for (const q of found) {
        this.write({ type: "answer", question: q.id, value: st.questions.get(q.id)!.recommendation!.value, source: "recommendation", note: null, facts: evidenceOf(q, facts) });
        accepted.push(q.id);
      }
    }
    if (accepted.length) this.audit("consult.accepted", { questions: accepted, section: section ?? null, count: accepted.length }, { type: "section", id: section ?? null });
    return { ...this.state(facts, mode), accepted };
  }
  resolve(facts: Facts, mode: "required" | "advisory", id: string, resolution: "take" | "keep", reason?: string): ConsultState {
    const c = CAT.conflicts.find((x) => x.id === id);
    if (!c) throw new ApiRequestError("not_found", "There is no such contradiction.", 404);
    const st = evaluateAll(facts, this.records);
    const current = st.conflicts.find((x) => x.id === id);
    if (!current) throw new ApiRequestError("not_open", "That contradiction does not hold any more.", 409);
    if (resolution === "take") {
      const take = c.resolutions.find((r) => r.kind === "take");
      if (!take?.question) throw new ApiRequestError("invalid_request", "This contradiction has no finding to take; keep it with a reason.", 400);
      const q = this.question(take.question);
      this.applicable(st, q);
      const value = check(q, take.add !== undefined ? [...((st.values[q.id] as string[]) ?? []), take.add] : take.value, facts);
      this.write({ type: "answer", question: q.id, value, source: "finding", note: `Taken to resolve: ${current.message}`.slice(0, 500), facts: evidenceOf(q, facts) });
      this.write({ type: "resolve", conflict: id, resolution: "take" });
      this.audit("consult.resolved", { conflict: id, resolution: "take", question: q.id, value: display(q, value) }, { type: "conflict", id });
      return this.state(facts, mode);
    }
    if (!c.resolutions.some((r) => r.kind === "keep")) throw new ApiRequestError("invalid_request", "This contradiction cannot be kept: take the finding, or change the answer.", 400);
    const why = (reason ?? "").split(/\s+/).filter(Boolean).join(" ").slice(0, 500);
    if (!why) throw new ApiRequestError("reason_required", "Say why the answers stand as they are.", 400);
    this.write({ type: "resolve", conflict: id, resolution: "keep", reason: why, facts: conflictEvidence(c, facts),
      answers: Object.fromEntries((c.questions ?? []).map((q) => [q, st.values[q] ?? null])) });
    this.audit("consult.resolved", { conflict: id, resolution: "keep", reason: why }, { type: "conflict", id });
    return this.state(facts, mode);
  }

  brief(facts: Facts): BriefView {
    const st = evaluateAll(facts, this.records);
    const content = draft(facts, st);
    return { draft: content, draftHash: contentHash(content), submittable: st.blockers.length === 0, blockers: st.blockers, versions: this.versionsView(), summary: this.summary(facts, st) };
  }
  submit(facts: Facts, note?: string): BriefView {
    const st = evaluateAll(facts, this.records);
    if (st.blockers.length) {
      throw new ApiRequestError("brief_not_ready", `The brief cannot be submitted yet: ${st.blockers.slice(0, 3).join(" ")}${st.blockers.length > 3 ? ` (${st.blockers.length - 3} more in Consult.)` : ""}`, 409);
    }
    const content = draft(facts, st);
    const version = this.versions.length + 1;
    const hash = contentHash(content);
    for (const v of this.versionsView()) if (v.status === "submitted") this.events.push({ event: "superseded", version: v.version, by: this.person, at: this.now() });
    this.versions.push({ version, hash, content, submittedAt: this.now(), submittedBy: this.person, note: note?.trim() || null });
    this.events.push({ event: "submitted", version, by: this.person, at: this.now(), hash });
    this.audit("brief.submitted", { version, hash, decisions: content.decisions.length, assumptions: content.assumptions.length }, { type: "brief", id: String(version) });
    return this.brief(facts);
  }
  decide(facts: Facts, version: number, decision: "approve" | "reject", comment?: string): BriefView {
    const v = this.versions.find((x) => x.version === version);
    if (!v) throw new ApiRequestError("not_found", "There is no such brief version.", 404);
    const status = this.status(version);
    if (status !== "submitted") throw new ApiRequestError("not_waiting", `Version ${version} is ${status}, not waiting for a decision.`, 409);
    const said = comment?.trim() || null;
    if (decision === "reject") {
      this.events.push({ event: "rejected", version, by: this.person, at: this.now(), comment: said });
      this.audit("brief.rejected", { version, hash: v.hash, comment: said }, { type: "brief", id: String(version) });
      return this.brief(facts);
    }
    if (v.content.factsFingerprint !== fingerprint(facts)) {
      throw new ApiRequestError("brief_stale", `The estate changed since version ${version} was submitted. Submit the brief again, from the estate as it is now, then approve that version.`, 409);
    }
    const previous = this.approvedNumber();
    if (previous !== null) this.events.push({ event: "superseded", version: previous, by: this.person, at: this.now() });
    const selfApproved = v.submittedBy.id === this.person.id;
    this.events.push({ event: "approved", version, by: this.person, at: this.now(), comment: said, selfApproved, separateApprover: v.content.separateApprover.map((x) => x.question) });
    this.audit("brief.approved", { version, hash: v.hash, comment: said, selfApproved, superseded: previous }, { type: "brief", id: String(version) });
    return this.brief(facts);
  }
  exportBrief(version: number | undefined, format: "md" | "json"): { text: string; fileName: string } {
    const n = version ?? this.approvedNumber() ?? this.versions[this.versions.length - 1]?.version;
    const v = this.versions.find((x) => x.version === n);
    if (!v) throw new ApiRequestError("not_found", "There is no such brief version. Submit the brief first.", 404);
    this.audit("brief.exported", { version: v.version, format }, { type: "brief", id: String(v.version) });
    if (format === "json") {
      return { text: JSON.stringify({ ...v, events: this.events.filter((e) => e.version === v.version) }, null, 2), fileName: `migration-brief-v${v.version}.json` };
    }
    return { text: toMarkdown(v, this.events), fileName: `migration-brief-v${v.version}.md` };
  }

  policy(facts: Facts, estate: DemoEstate, mode: "required" | "advisory"): ConsultPolicy {
    const p = this.inForce(facts);
    const llm = this.llmGate("transform", mode);
    if (!p) return { mode, inForce: null, llm };
    return {
      mode, llm, inForce: { version: p.version, hash: p.hash, approvedBy: p.approvedBy, approvedAt: p.approvedAt, stale: p.stale, changes: p.changes, settings: p.settings },
      runOptions: p.runOptions(), dataFilters: p.dataFilters(estate.filterable), storageRoutes: (p.get("storage.routes") as "all" | "none" | undefined) ?? null,
      excluded: Object.fromEntries(estate.items.flatMap((i) => { const why = p.excluded(i, estate); return why ? [[i.id, why]] : []; })),
      signoff: (p.get("release.signoff") as ConsultPolicy["signoff"]) ?? null,
      names: { warehouse: (p.get("names.warehouse") as string) ?? null, lakehouse: (p.get("names.lakehouse") as string) ?? null },
      validate: DEPTHS[p.get("validate.depth") as string] ?? null, sqlUserMap: (p.get("security.sqlUserMap") as Record<string, string>) ?? {},
    };
  }
  private differences(p: Policy, items: { id: string }[], options: Partial<RunOptions>, estate: DemoEstate): PlanDifference[] {
    const byId = new Map(estate.items.map((i) => [i.id, i]));
    const leftOut: Record<string, string> = {};
    for (const e of items) { const i = byId.get(e.id); const why = i ? p.excluded(i, estate) : null; if (why) leftOut[e.id] = why; }
    const ids = new Set(items.map((i) => i.id));
    const tables = options.stages?.includes("data") ? estate.filterable.filter((t) => ids.has(t.id)) : null;
    return p.differences(options, leftOut, tables);
  }
  private keyOf = (d: PlanDifference) => JSON.stringify([d.option, d.plan]);
  checkPlan(facts: Facts, estate: DemoEstate, items: { id: string }[], options: Partial<RunOptions>, mode: "required" | "advisory"): BriefCheck {
    const p = this.inForce(facts);
    if (!p) return { mode, version: null, stale: false, differences: [], explained: false };
    const differences = this.differences(p, items, options, estate);
    const keys = differences.map(this.keyOf);
    return { mode, version: p.version, hash: p.hash, stale: p.stale, changes: p.changes, differences,
      explained: differences.length > 0 && this.deviations.some((d) => d.briefHash === p.hash && keys.every((k) => d.keys.has(k))) };
  }
  recordDeviation(facts: Facts, estate: DemoEstate, items: { id: string }[], options: Partial<RunOptions>, reason: string): { differences: PlanDifference[]; recorded: boolean } {
    const p = this.inForce(facts);
    if (!p) throw new ApiRequestError("brief_required", "No migration brief is approved yet, so there is nothing to differ from.", 409);
    const differences = this.differences(p, items, options, estate);
    if (!differences.length) return { differences, recorded: false };
    const why = reason.split(/\s+/).filter(Boolean).join(" ").slice(0, 500);
    if (!why) throw new ApiRequestError("reason_required", "Say why the plan differs from the approved brief.", 400);
    this.deviations.push({ briefHash: p.hash, keys: new Set(differences.map(this.keyOf)), reason: why, by: this.person, at: this.now() });
    this.audit("plan.deviation", { briefVersion: p.version, differences: differences.map((d) => d.message), reason: why }, { type: "brief", id: String(p.version) });
    return { differences, recorded: true };
  }
  /** What a run starts under; throws as the server does under a required consultation. */
  runGate(facts: Facts, estate: DemoEstate, items: { id: string }[], options: Partial<RunOptions>, mode: "required" | "advisory", reason?: string):
    { brief: { version: number | null; hash?: string; differences: PlanDifference[]; deviation: string | null }; signoff: string | null; names: { warehouse: string | null; lakehouse: string | null } } {
    const p = this.inForce(facts);
    if (!p) {
      if (mode === "required") throw new ApiRequestError("brief_required", "No migration brief is approved yet. Answer the questions in Consult, submit the brief and have it approved, then start the run.", 409);
      return { brief: { version: null, differences: [], deviation: null }, signoff: null, names: { warehouse: null, lakehouse: null } };
    }
    if (p.stale && mode === "required") {
      const shown = p.changes.slice(0, 3).map((c) => `${c.description}: ${said(c.before)} → ${said(c.after)}`).join("; ");
      throw new ApiRequestError("brief_stale", `The estate changed since brief version ${p.version} was approved (${shown}). Reconfirm it in Consult: submit the brief again and have it approved.`, 409);
    }
    const differences = this.differences(p, items, options, estate);
    let deviation: string | null = null;
    if (differences.length) {
      const keys = differences.map(this.keyOf);
      deviation = [...this.deviations].reverse().find((d) => d.briefHash === p.hash && keys.every((k) => d.keys.has(k)))?.reason ?? null;
      if (!deviation && reason?.trim()) deviation = (this.recordDeviation(facts, estate, items, options, reason), reason.trim());
      if (!deviation && mode === "required") {
        throw new ApiRequestError("deviation_unexplained", `The run differs from the approved brief (version ${p.version}): ${differences.slice(0, 3).map((d) => d.message).join(" ")} Confirm the plan with a reason, or change it to match the brief.`, 409);
      }
    }
    return { brief: { version: p.version, hash: p.hash, differences, deviation }, signoff: (p.get("release.signoff") as string) ?? null,
      names: { warehouse: (p.get("names.warehouse") as string) ?? null, lakehouse: (p.get("names.lakehouse") as string) ?? null } };
  }

  propose(facts: Facts, mode: "required" | "advisory", text: string, source: "rules" | "import" = "rules", given?: { question: string; value: unknown; reason: string }[]): ConsultState {
    if (!text.trim() && !given) throw new ApiRequestError("invalid_request", "Paste the notes to read answers from.", 400);
    const proposed = given ?? ruleProposals(text);
    const kept: { question: string; value: unknown; reason: string }[] = [], rejected: { question: string; value: unknown; reason: string; message: string }[] = [];
    const seen = new Set<string>();
    for (const p of proposed) {
      if (seen.has(p.question)) continue;
      seen.add(p.question);
      const q = QUESTION.get(p.question);
      if (!q) { rejected.push({ ...p, message: "There is no such question." }); continue; }
      try { kept.push({ ...p, value: check(q, p.value, facts) }); } catch (e) { rejected.push({ ...p, message: (e as Error).message }); }
    }
    const hypothetical = evaluateAll(facts, [...this.records, ...kept.map((p, i) => ({ type: "answer" as const, question: p.question, value: p.value, source: "person" as AnswerSource,
      seq: this.records.length + i + 1, by: this.person, at: this.now(), catalogVersion: CAT.version }))]);
    const applies = kept.filter((p) => hypothetical.questions.get(p.question)?.applies);
    for (const p of kept) if (!applies.includes(p)) rejected.push({ ...p, message: "It does not apply to this estate." });
    this.batch += 1;
    const ids = applies.map((p, i) => {
      const id = `demo${this.batch}-${i + 1}`;
      this.proposals.push({ id, question: p.question, value: p.value, reason: p.reason, source, model: null, at: this.now(), display: display(QUESTION.get(p.question), p.value), current: null });
      return id;
    });
    this.audit("consult.proposed", { source, model: null, count: ids.length, questions: applies.map((p) => p.question) }, { type: "proposals", id: ids.length ? `demo${this.batch}` : null });
    return { ...this.state(facts, mode), proposed: ids, rejected, source,
      message: source === "rules" ? "Demo data reads notes with the built-in rules: nothing is sent anywhere." : null };
  }
  decideProposals(facts: Facts, mode: "required" | "advisory", ids: string[], decision: "confirm" | "reject"): ConsultState {
    const st = evaluateAll(facts, this.records);
    const pending = new Map(this.pending(st).map((p) => [p.id, p]));
    const missing = ids.find((i) => !pending.has(i));
    if (missing) throw new ApiRequestError("not_pending", `Proposal ${missing} is not waiting for a decision any more. Read the page again.`, 409);
    const answered: string[] = [];
    for (const id of ids) {
      const p = pending.get(id)!;
      const q = this.question(p.question);
      const stored = this.proposals.find((x) => x.id === id)!;
      if (decision === "confirm") {
        this.applicable(evaluateAll(facts, this.records), q);
        this.write({ type: "answer", question: q.id, value: check(q, p.value, facts), source: p.source === "import" ? "import" : p.source === "ai" ? "ai" : "rules", note: p.reason, facts: evidenceOf(q, facts) });
        answered.push(q.id);
        stored.decided = "confirmed";
        this.audit("consult.proposal_confirmed", { proposal: id, question: q.id, value: p.display, source: p.source }, { type: "question", id: q.id });
      } else {
        stored.decided = "rejected";
        this.audit("consult.proposal_rejected", { proposal: id, question: q.id, value: p.display, source: p.source }, { type: "question", id: q.id });
      }
    }
    return { ...this.state(facts, mode), answered };
  }
  explain(facts: Facts, id: string): ConsultExplanation {
    const q = this.question(id);
    const s = evaluateAll(facts, this.records).questions.get(q.id)!;
    const parts = [q.help ?? ""];
    if (q.options?.length) parts.push(q.options.map((o) => (o.description ? `${o.label ?? o.value}: ${o.description.replace(/\.$/, "")}.` : `${o.label ?? o.value}.`)).join(" "));
    if (s.recommendation) parts.push(`Recommended: ${s.recommendation.reason}`);
    const evidence = (q.evidence ?? []).map((n) => `${describeFact(n)}: ${show(facts[n] ?? null)}`);
    if (evidence.length) parts.push(`What it rests on: ${evidence.join("; ")}.`);
    parts.push(`It decides: ${q.drives.map((n) => `${SETTING_APPLIES[n]} (${SETTING_STATUS[n]})`).join("; ")}.`);
    this.audit("consult.explained", { question: q.id, source: "rules" }, { type: "question", id: q.id });
    return { question: q.id, text: parts.filter((x) => x.trim()).join(" "), source: "rules", model: null };
  }
  questionnaire(facts: Facts, format: "md" | "json"): { text: string; fileName: string } {
    const st = evaluateAll(facts, this.records);
    const live = CAT.questions.filter((q) => st.questions.get(q.id)!.applies);
    this.audit("consult.exported", { format, questions: live.length }, { type: "questionnaire", id: null });
    const fileName = `migration-questionnaire-${facts["discovery.snapshot"] ?? "draft"}.${format}`;
    if (format === "json") {
      return { fileName, text: JSON.stringify({ catalogVersion: CAT.version, snapshot: facts["discovery.snapshot"] ?? null,
        instructions: "Fill in answers (each a JSON value, as in 'choices'), then import this file in Consult.",
        questions: live.map((q) => { const s = st.questions.get(q.id)!; return { id: q.id, section: q.section, type: q.type, text: q.text, help: q.help ?? "",
          choices: q.type === "choice" || q.type === "multi" ? choicesOf(q, facts) : null, rows: s.rows, recommended: s.recommendation?.value ?? null, current: s.value, status: s.status }; }),
        answers: Object.fromEntries(live.flatMap((q) => { const v = st.questions.get(q.id)!.value; return v === null || v === undefined ? [] : [[q.id, v]]; })) }, null, 2) };
    }
    const titles = new Map(CAT.sections.map((x) => [x.id, x.title]));
    const lines = ["# Migration questionnaire", "", `Catalog ${CAT.version}, discovery snapshot ${facts["discovery.snapshot"] ?? "—"}. Answer each question; the recommendation is what stands if you do not. Return it, or import the JSON version in Consult.`, ""];
    let section: string | null = null;
    for (const q of live) {
      const s = st.questions.get(q.id)!;
      if (q.section !== section) { section = q.section; lines.push(`## ${titles.get(section) ?? section}`, ""); }
      lines.push(`### ${q.id}. ${q.text}${q.required ? " (required)" : ""}`);
      if (q.help) lines.push("", q.help);
      if (q.options?.length) lines.push("", ...q.options.map((o) => `- [${same(s.value, o.value) ? "x" : " "}] ${o.label ?? o.value}${o.description ? `: ${o.description}` : ""}`));
      if (s.recommendation) lines.push("", `Recommended: ${display(q, s.recommendation.value)}. ${s.recommendation.reason}`);
      lines.push("", `Your answer: ${s.status === "answered" ? display(q, s.value) : "______"}`, "");
    }
    return { fileName, text: lines.join("\n") };
  }
  importAnswers(facts: Facts, mode: "required" | "advisory", answers: Record<string, unknown>): ConsultState {
    const st = evaluateAll(facts, this.records);
    const given = Object.entries(answers).filter(([q, v]) => { const s = st.questions.get(q); return !(s && s.status === "answered" && same(s.value, v)); })
      .map(([question, value]) => ({ question, value, reason: "From the imported questionnaire." }));
    return this.propose(facts, mode, "", "import", given);
  }
}

