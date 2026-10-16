// The DEMO orchestrator: the execution plan and the agent's requests, from the Demo data's own state, so the plan board
// and the assistant can be walked through without a backend. It mirrors src/discovery_agent/orchestrator/ and
// api/orchestrator.py; where the two could differ, the server is right.
//
// Demo differences, on purpose: the agent only ever proposes (it never acts alone, whatever the brief's autonomy),
// the assistant answers with its fixed intents (nothing is sent to a model), and the plan's hash is the Demo's own.

import {
  ApiRequestError,
  type Actor,
  type AgentAction,
  type AssistantEntry,
  type AssistantTask,
  type ConsultPolicy,
  type ExecutionPlanView,
  type ExecutionRun,
  type PlanStep,
  type PlanStepStatus,
  type ValidationRow,
} from "../types";

/** What the Demo orchestrator reads, and the two things it may ask the Demo run to do. */
export interface DemoWorld {
  person: Actor;
  run(): ExecutionRun;
  validation(): { state: string; rows?: ValidationRow[] };
  plan(): { id: string; objects: number; blocking: number; readiness: number } | null;
  policy(): ConsultPolicy;
  flags(): Record<string, string[]>;
  names(): Record<string, string>;
  ready(): { source: boolean; target: boolean; capacity: boolean; discovered: boolean; complete: boolean };
  /** The LLM drafts: what the rules leave, drafts waiting for review, accepted ones. */
  drafts(): { needsWork: number; drafts: number; accepted: number };
  retry(): Promise<ExecutionRun>;
  record(action: string, details: Record<string, unknown>, target: { type: string; id: string | null }, actor?: Actor): void;
}

const PHASES = [["readiness", "Readiness"], ["person", "Before the run"], ["prepare", "Prepare"], ["deliver", "Deliver"],
  ["validate", "Validate"], ["release", "Release"], ["cutover", "Cutover"], ["report", "Sign-off"]] as const;
const AGENT: Actor = { kind: "agent", id: "orchestrator" };
/** A failure a retry fixes (a timeout, throttling), as the server's categories have it; else one Fabric rejected. */
const PASSING = /busy|timed out|timeout|throttl|try again/i;

function digest(text: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
  return h.toString(16).padStart(8, "0").repeat(8);
}
const count = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

type Def = Omit<PlanStep, "status" | "message" | "markedBy" | "actions" | "digest">;

export class DemoOrchestrator {
  private actions: AgentAction[] = [];
  private marks = new Map<string, { digest: string; done: boolean; by: Actor; note: string | null }>();
  private tasks: (AssistantTask & { entries: AssistantEntry[] })[] = [];
  private paused = false;
  private versions: string[] = [];

  constructor(private readonly world: DemoWorld) {}

  private defs(): Def[] {
    const p = this.world.policy();
    const s = p.inForce?.settings ?? {};
    const get = (k: string) => s[k];
    const plan = this.world.plan();
    const flags = this.world.flags();
    const names = this.world.names();
    const listed = (ids: string[]) => ids.map((i) => names[i] ?? i).sort().slice(0, 25);
    const out: Def[] = [];
    const add = (d: Omit<Def, "depends" | "answers" | "params" | "detail"> & Partial<Def>) =>
      out.push({ detail: "", depends: [], answers: [], params: {}, ...d });
    add({ key: "ready.source", phase: "readiness", title: "The Synapse source is connected", owner: "system", kind: "check" });
    add({ key: "ready.target", phase: "readiness", title: "The Fabric target is connected, on a capacity", owner: "system", kind: "check" });
    add({ key: "ready.discovery", phase: "readiness", title: "Discovery read every part of the source", owner: "system", kind: "check" });
    add({ key: "ready.brief", phase: "readiness", title: "The migration brief is approved, and still fits the estate", owner: "system", kind: "check" });
    add({ key: "ready.plan", phase: "readiness", title: "A plan is recorded, with no blocking risk", owner: "system", kind: "check", params: { planVersion: plan?.id ?? null } });
    const residency = String(get("governance.residency") ?? "").trim();
    if (residency) add({ key: "ready.residency", phase: "readiness", title: `The target capacity is in ${residency}`, owner: "person", kind: "task", answers: ["G3"], params: { region: residency } });
    const gate: string[] = [];
    const lossy = flags.lossy ?? [];
    if (lossy.length && get("types.lossy") === "review") {
      add({ key: "person.lossy", phase: "person", title: `Review ${count(lossy.length, "table")} whose types change, before their data loads`, owner: "person", kind: "task", answers: ["D4"], params: { objects: listed(lossy) } });
      gate.push("person.lossy");
    }
    const multi = flags.multiStatement ?? [];
    if (multi.length && get("code.multiStatement") === "manual") add({ key: "person.multiStatement", phase: "person", title: `Rewrite ${count(multi.length, "multi-statement function")} by hand`, owner: "person", kind: "task", answers: ["D3"], params: { objects: listed(multi) } });
    const rejected = flags.unconvertible ?? [];
    if (rejected.length && get("code.unconvertible") === "by_hand") add({ key: "person.unconvertible", phase: "person", title: `Rewrite ${count(rejected.length, "object")} Fabric rejects, by hand`, owner: "person", kind: "task", answers: ["D5"], params: { objects: listed(rejected) } });
    const toDraft = [...new Set([...(get("llm.consent") === true && get("code.multiStatement") === "llm" ? multi : []),
      ...(get("llm.consent") === true && get("code.unconvertible") === "ai" ? rejected : [])])].sort();
    if (toDraft.length) {
      add({ key: "prepare.drafts", phase: "prepare", title: `Draft ${count(toDraft.length, "object")} with AI`, owner: "agent", kind: "action", answers: ["D1"], params: { objects: toDraft } });
      add({ key: "prepare.review", phase: "prepare", title: "Review and accept each draft", owner: "person", kind: "task", depends: ["prepare.drafts"], answers: ["D1"], params: { objects: toDraft } });
      gate.push("prepare.review");
    }
    const delivery = get("run.waveGate") ? ", wave by wave, a person signing off each wave" : get("run.stopOnFailure") ? ", wave by wave, stopping at a wave with failures" : "";
    add({ key: "deliver.run", phase: "deliver", title: `Run the migration${delivery}`, owner: "agent", kind: "action", depends: ["ready.source", "ready.target", "ready.brief", "ready.plan", ...gate], answers: ["A0", "A6", "E4"] });
    add({ key: "deliver.fix", phase: "deliver", title: "Fix what failed, and retry it", owner: "agent", kind: "action", depends: ["deliver.run"] });
    const depth = String(get("validate.depth") ?? "probe");
    add({ key: "validate.run", phase: "validate", title: `Validate, ${({ compare: "comparing definitions, columns and row counts", probe: "also querying and compiling", run: "also running pipelines, notebooks and Spark jobs" } as Record<string, string>)[depth] ?? depth}`, owner: "agent", kind: "action", depends: ["deliver.run"], answers: ["H1"], params: { depth } });
    add({ key: "validate.mismatches", phase: "validate", title: "Resolve each mismatch, or accept it with a reason", owner: "person", kind: "task", depends: ["validate.run"] });
    if (get("release.gitCommit") === true) {
      add({ key: "release.commit", phase: "release", title: `Commit to the Fabric Git branch: ${get("release.commitScope") === "all" ? "everything waiting in the workspace" : "only what the run made"}`, owner: "agent", kind: "action", depends: ["deliver.run"], answers: ["E0", "E0S"] });
      add({ key: "release.review", phase: "release", title: "Review and merge the pull request", owner: "person", kind: "task", depends: ["release.commit"], answers: ["E7"] });
    }
    if (get("data.sync") === true && get("data.deletes") === "reload") add({ key: "cutover.reload", phase: "cutover", title: "Reload the synced tables in full, removing rows deleted in Synapse", owner: "person", kind: "task", depends: ["validate.run"], answers: ["C4"] });
    if (get("cutover.parallelRun") === true) add({ key: "cutover.parallel", phase: "cutover", title: `Run Synapse and Fabric side by side for ${count(Number(get("cutover.parallelCycles") ?? 3), "cycle")}, comparing their results`, owner: "person", kind: "task", depends: ["validate.run"], answers: ["H2", "H2A"] });
    add({ key: "report.signoff", phase: "report", title: "The sign-off report: brief, plan, runs, validation, what was left for a person, the trail's hash", owner: "system", kind: "report", depends: ["deliver.run"] });
    return out;
  }

  private statusOf(d: Def, done: Map<string, PlanStepStatus>): { status: PlanStepStatus; message: string; markedBy: Actor | null } {
    const r = this.world.ready();
    const p = this.world.policy();
    const run = this.world.run();
    const plan = this.world.plan();
    const v = this.world.validation();
    const digestOf = digest(JSON.stringify(d));
    const mark = this.marks.get(d.key);
    const marked = !!mark?.done && mark.digest === digestOf;
    const waiting = d.depends.filter((k) => done.has(k) && !["done", "skipped", "warn"].includes(done.get(k)!));
    const wait = (why = `Waits for: ${waiting.join(", ")}.`) => ({ status: "waiting" as const, message: why, markedBy: null });
    if (d.kind === "check") {
      const pass = (message: string) => ({ status: "done" as const, message, markedBy: null });
      // Only what the accelerator cannot tell is confirmed by hand: a check it sees failing is fixed, never confirmed.
      const fail = (message: string, warn = false) => marked && warn ? { status: "done" as const, message: "Confirmed by a person.", markedBy: mark!.by }
        : { status: (warn ? "warn" : "failed") as PlanStepStatus, message, markedBy: null };
      if (d.key === "ready.source") return r.source ? pass("Connected.") : fail("Not connected: sign in on Connections.", r.discovered);
      if (d.key === "ready.target") return !r.target ? fail("The Fabric target is not connected: connect it on Connections.") : !r.capacity ? fail("The workspace is on no capacity.") : pass("Connected.");
      if (d.key === "ready.discovery") return !r.discovered ? fail("No discovery: run it on Discover.") : r.complete ? pass("Every part was read.") : fail("Some parts could not be read.", true);
      if (d.key === "ready.brief") {
        if (!p.inForce) return fail("No brief is approved: answer the questions in Consult, submit the brief and have it approved.", p.mode !== "required");
        return p.inForce.stale ? fail(`Version ${p.inForce.version} no longer fits: reconfirm it in Consult.`, p.mode !== "required") : pass(`Version ${p.inForce.version} is in force.`);
      }
      if (d.key === "ready.plan") return !plan ? fail("No plan is recorded: build it on Plan.") : plan.blocking ? fail(`Plan ${plan.id} has ${count(plan.blocking, "blocking risk")}.`) : pass(`Plan ${plan.id}, no blocking risk.`);
    }
    if (d.kind === "task") {
      if (d.key === "validate.mismatches" && v.state === "completed" && !(v.rows ?? []).some((x) => x.status === "MISMATCH")) return { status: "done", message: "No mismatch.", markedBy: null };
      if (marked) return { status: "done", message: mark!.note ?? "Marked done.", markedBy: mark!.by };
      return waiting.length ? wait() : { status: "ready", message: "For a person to do, then mark done.", markedBy: null };
    }
    if (d.kind === "report") return waiting.length ? wait("") : { status: "ready", message: "Download it from the plan board.", markedBy: null };
    const status = run.status ?? (run.state === "idle" ? "idle" : run.state);
    if (d.key === "deliver.run") {
      if (run.state === "idle") return waiting.length ? wait() : { status: "ready", message: "Ready to start.", markedBy: null };
      if (run.state === "running" || status === "awaiting_approval" || run.state === "paused") return { status: "in_progress", message: `Run ${run.runId} is ${String(status).replace(/_/g, " ")}.`, markedBy: null };
      return { status: "done", message: run.failed ? `Run ${run.runId} finished with failures: see Fix what failed.` : `Run ${run.runId} completed.`, markedBy: null };
    }
    if (d.key === "deliver.fix") {
      if (run.state !== "completed") return wait("Waits for the run.");
      const bad = run.failed + (run.blocked ?? 0);
      return bad ? { status: "ready", message: `${count(bad, "object")} failed or blocked.`, markedBy: null } : { status: "done", message: "Nothing failed.", markedBy: null };
    }
    if (d.key === "validate.run") {
      if (waiting.length) return wait("Waits for the run.");
      return v.state === "completed" ? { status: "done", message: "Validated.", markedBy: null } : { status: "ready", message: "Ready to validate.", markedBy: null };
    }
    if (d.key === "release.commit") {
      if (waiting.length) return wait("Waits for the run.");
      const git = run.git?.state;
      return git === "committed" || git === "nothing" ? { status: "done", message: run.git?.message ?? "Committed.", markedBy: null }
        : git === "skipped" || git === "failed" || git === "blocked" ? { status: "failed", message: run.git?.message ?? "The commit was not made.", markedBy: null }
        : { status: "ready", message: "Commit the run's items.", markedBy: null };
    }
    return waiting.length ? wait() : { status: "ready", message: "", markedBy: null };
  }

  private reconcile(): void {
    if (this.paused) return;
    const p = this.world.policy();
    if ((p.assistant?.autonomy ?? "suggest") === "observe") return;
    const run = this.world.run();
    // Requests that no longer apply lapse.
    for (const a of this.actions.filter((x) => x.status === "proposed")) {
      if (a.tool === "run.retry" && !(run.state === "completed" && run.failed > 0)) {
        a.status = "stale"; a.reason = "Nothing in the run waits to be retried."; a.decidedAt = new Date().toISOString();
        this.world.record("orchestrator.action_expired", { action: a.id, tool: a.tool, reason: a.reason }, { type: "action", id: a.id }, AGENT);
      }
    }
    // A retry only when every failure would pass on one, as the server's diagnosis has it: one Fabric rejected needs a
    // draft or a rewrite first, and the assistant says so.
    const bad = run.items.filter((i) => i.status === "FAILED" || i.status === "BLOCKED");
    const retryable = bad.length > 0 && bad.every((i) => i.status === "BLOCKED" || PASSING.test(i.error ?? ""));
    if (run.state === "completed" && run.failed > 0 && retryable && !this.actions.some((a) => a.status === "proposed" && a.tool === "run.retry")) {
      const action: AgentAction = { id: `A${digest(`${run.runId}-${Date.now()}-${this.actions.length}`).slice(0, 10)}`, tool: "run.retry", tier: "change", args: {},
        preview: { summary: `Retry the ${count(run.failed + (run.blocked ?? 0), "failed or blocked object")} of run ${run.runId}.` },
        rationale: "The run finished with failures: a retry carries the failed objects on (Demo: the agent only proposes).",
        step: "deliver.fix", task: null, status: "proposed", proposedBy: AGENT, createdAt: new Date().toISOString() };
      this.actions.unshift(action);
      this.world.record("orchestrator.action_proposed", { action: action.id, tool: action.tool, tier: action.tier, summary: action.preview.summary, step: action.step }, { type: "action", id: action.id }, AGENT);
    }
  }

  plan(): ExecutionPlanView {
    this.reconcile();
    const defs = this.defs();
    const done = new Map<string, PlanStepStatus>();
    const steps: PlanStep[] = [];
    for (const d of defs) {
      const st = this.statusOf(d, done);
      done.set(d.key, st.status);
      steps.push({ ...d, digest: digest(JSON.stringify(d)), ...st, actions: this.actions.filter((a) => a.status === "proposed" && a.step === d.key) });
    }
    const hash = digest(JSON.stringify(defs.map((d) => d.key)) + JSON.stringify(this.world.policy().inForce?.hash ?? null) + (this.world.plan()?.id ?? ""));
    if (this.versions[this.versions.length - 1] !== hash) {
      this.versions.push(hash);
      this.world.record("orchestrator.plan_built", { version: this.versions.length, hash, steps: steps.length }, { type: "plan", id: String(this.versions.length) }, { kind: "system", id: "accelerator" });
    }
    const p = this.world.policy();
    return {
      version: this.versions.length, hash, inputs: { briefVersion: p.inForce?.version ?? null, briefHash: p.inForce?.hash ?? null, planVersion: this.world.plan()?.id ?? null, snapshot: "S001" },
      phases: PHASES.map(([id, title]) => ({ id, title })), steps, actions: this.actions.filter((a) => a.status === "proposed"),
      agent: { on: true, paused: this.paused, pausedBy: this.paused ? this.world.person : null, autonomy: p.assistant?.autonomy ?? "suggest",
        targetRole: p.run?.targetRole ?? null, actsAlone: false, lastWake: null },
      assistant: { ai: false, reason: "Demo data sends nothing to a model: the assistant answers with its fixed intents." },
    };
  }

  mark(step: string, isDone: boolean, note?: string): ExecutionPlanView {
    const d = this.defs().find((x) => x.key === step);
    if (!d) throw new ApiRequestError("not_found", "There is no such step in the execution plan.", 404);
    if (d.owner !== "person" && d.kind !== "check") throw new ApiRequestError("not_a_task", "This step is done by the accelerator or the agent: it is not marked by hand.", 409);
    if (d.kind === "check" && isDone) {
      const now = this.plan().steps.find((s) => s.key === step);
      if (now?.status === "failed") throw new ApiRequestError("check_fails", `The accelerator sees this check failing, so it is fixed, not confirmed: ${now.message}`, 409);
      if (now?.status !== "warn") throw new ApiRequestError("nothing_to_confirm", "This check passes: there is nothing to confirm by hand.", 409);
    }
    this.marks.set(step, { digest: digest(JSON.stringify(d)), done: isDone, by: this.world.person, note: note?.trim() || null });
    this.world.record("orchestrator.step_marked", { step, done: isDone, note: note?.trim() || null }, { type: "step", id: step });
    return this.plan();
  }

  async decide(id: string, decision: "confirm" | "reject", text?: string): Promise<{ action: AgentAction; plan: ExecutionPlanView }> {
    const a = this.actions.find((x) => x.id === id);
    if (!a) throw new ApiRequestError("not_found", "There is no such action.", 404);
    if (a.status !== "proposed") throw new ApiRequestError("not_open", `This action is ${a.status}: there is nothing to decide.`, 409);
    a.decidedBy = this.world.person;
    a.decidedAt = new Date().toISOString();
    if (decision === "reject") {
      const reason = (text ?? "").trim();
      if (!reason) throw new ApiRequestError("reason_required", "Say why, so the agent knows (it is kept with the action).", 400);
      a.status = "rejected"; a.reason = reason;
      this.world.record("orchestrator.action_rejected", { action: a.id, tool: a.tool, reason }, { type: "action", id: a.id });
    } else {
      const run = this.world.run();
      if (a.tool === "run.retry" && !(run.state === "completed" && run.failed > 0)) {
        a.status = "stale"; a.reason = "Nothing in the run waits to be retried.";
        throw new ApiRequestError("action_stale", `It no longer applies: ${a.reason}`, 409);
      }
      await this.world.retry();
      a.status = "done"; a.note = text?.trim() || null;
      this.world.record("orchestrator.action_confirmed", { action: a.id, tool: a.tool, tier: a.tier, note: a.note, outcome: "done" }, { type: "action", id: a.id });
    }
    return { action: { ...a }, plan: this.plan() };
  }

  control(action: "pause" | "resume" | "wake"): ExecutionPlanView {
    if (action === "pause" || action === "resume") {
      this.paused = action === "pause";
      this.world.record(this.paused ? "orchestrator.paused" : "orchestrator.resumed", {}, { type: "agent", id: "orchestrator" });
    }
    return this.plan();
  }

  report(format: "md" | "json"): { text: string; fileName: string } {
    const view = this.plan();
    const p = this.world.policy();
    const run = this.world.run();
    const data = { madeAt: new Date().toISOString(), brief: p.inForce, executionPlan: { version: view.version, steps: view.steps.map((s) => ({ title: s.title, owner: s.owner, status: s.status })) },
      run: run.runId ? { runId: run.runId, status: run.status, completed: run.completed, failed: run.failed } : null, validation: this.world.validation().state };
    this.world.record("report.exported", { format }, { type: "report", id: String(view.version) });
    if (format === "json") return { text: JSON.stringify(data, null, 2), fileName: "migration-signoff-report.json" };
    const lines = ["# Migration sign-off report", "", `Made ${data.madeAt} from the Demo data's record.`, "", "## The migration brief", "",
      p.inForce ? `Version ${p.inForce.version} (content hash \`${p.inForce.hash.slice(0, 16)}\`).` : "No brief is approved.", "", "## The execution plan", "",
      `Version ${view.version}.`, "", "| Step | Owner | Status |", "|---|---|---|", ...view.steps.map((s) => `| ${s.title} | ${s.owner} | ${s.status.replace(/_/g, " ")} |`), "",
      "## Runs", "", run.runId ? `- Run ${run.runId}: ${String(run.status).replace(/_/g, " ")}, ${run.completed} completed, ${run.failed} failed.` : "No run yet.", "",
      "## Validation", "", data.validation];
    return { text: `${lines.join("\n")}\n`, fileName: "migration-signoff-report.md" };
  }

  // -- the assistant: fixed intents only, from the Demo state ------------------------------------------------------

  ask(text: string, taskId?: string): AssistantTask {
    if (taskId) throw new ApiRequestError("not_waiting", "This question is not waiting for a reply.", 409);
    const clean = text.split(/\s+/).filter(Boolean).join(" ").slice(0, 2000);
    if (!clean) throw new ApiRequestError("invalid_request", "Write the question.", 400);
    const now = new Date().toISOString();
    const answer = this.answer(clean);
    const task = { id: digest(`${now}-${this.tasks.length}`).slice(0, 12), question: clean, status: "done" as const, source: "rules" as const, createdAt: now,
      updatedAt: now, turns: 0, changes: 0, costUsd: 0, answer, entries: [{ type: "person" as const, at: now, text: clean }, { type: "answer" as const, at: now, text: answer }], actions: [] };
    this.tasks.unshift(task);
    this.world.record("assistant.asked", { task: task.id, chars: clean.length, source: "rules" }, { type: "task", id: task.id });
    this.world.record("assistant.finished", { task: task.id, turns: 0, actions: 0, outcome: "answered by the rules", costUsd: 0 }, { type: "task", id: task.id });
    return { ...task };
  }

  private answer(text: string): string {
    const run = this.world.run();
    if (/\b(why|what).{0,40}\bfail|\bfailures?\b|\berrors?\b/i.test(text)) {
      const bad = run.items.filter((i) => i.status === "FAILED" || i.status === "BLOCKED");
      if (!bad.length) return "Nothing in the current run failed or was blocked.";
      return [`${count(bad.length, "object")} failed or were blocked:`, ...bad.slice(0, 10).map((i) => {
        const passing = PASSING.test(i.error ?? "");
        return `- ${i.name} (${passing ? "TRANSIENT" : i.status === "BLOCKED" ? "DEPENDENCY" : "UNSUPPORTED"}): ${passing ? "Retry it: a new pass over what failed." : i.status === "BLOCKED" ? "Retry it once what it needs is there." : "Draft it with AI, review and accept the draft, then retry; or rewrite it by hand."}`;
      })].join("\n");
    }
    if (/\b(what('?s| is)? (next|left)|to do|todo|remaining|still need|next step)\b/i.test(text)) {
      const open = this.plan().steps.filter((s) => ["ready", "failed", "in_progress"].includes(s.status));
      if (!open.length) return "Nothing is waiting: every step that can be done is done, or waits for another.";
      return ["What is open now, in order:", ...open.slice(0, 8).map((s) => `- ${s.title} (${s.owner === "person" ? "a person" : s.owner === "agent" ? "the agent" : "the accelerator"}, ${s.status.replace(/_/g, " ")})${s.message ? `: ${s.message}` : ""}`)].join("\n");
    }
    if (/\bvalidat|\bmismatch/i.test(text)) {
      const v = this.world.validation();
      if (v.state !== "completed") return `Validation is ${v.state === "idle" ? "not run" : v.state}.`;
      const rows = v.rows ?? [];
      const tally: Record<string, number> = {};
      for (const r of rows) tally[r.status] = (tally[r.status] ?? 0) + 1;
      const summary = Object.keys(tally).sort().map((k) => `${tally[k]} ${k.toLowerCase()}`).join(", ");
      const wrong = rows.filter((r) => r.status === "MISMATCH").slice(0, 10);
      return `Validation: ${summary || "nothing compared"}.${wrong.map((r) => `\n- ${r.object}: ${r.detail ?? r.target}`).join("")}`;
    }
    if (/\bdrafts?\b|\btransform/i.test(text)) {
      const d = this.world.drafts();
      return `${d.needsWork} object(s) the rules cannot convert on their own; ${d.drafts} draft(s) to review, ${d.accepted} accepted, 0 being drafted.`;
    }
    if (/\bbrief\b|\bconsult|\bdecid/i.test(text)) {
      const p = this.world.policy().inForce;
      if (!p) return "No migration brief is approved yet: answer the questions in Consult, submit the brief, and have it approved.";
      return `Brief version ${p.version} is in force${p.stale ? ", but it no longer fits: reconfirm it in Consult." : "."} It decides ${Object.keys(p.settings ?? {}).length} settings.`;
    }
    if (/\bstatus\b|\bwhere .{0,20}stand|\bprogress\b|\bhow far\b|\bsummary\b/i.test(text)) {
      const p = this.world.policy();
      return `Where it stands: brief ${p.inForce ? `v${p.inForce.version}` : "not approved"}; plan ${this.world.plan()?.id ?? "not recorded"}; run ${run.runId ? `${run.runId} ${String(run.status).replace(/_/g, " ")}` : "not started"}; validation ${this.world.validation().state}.`;
    }
    return "Without the AI I can answer: why something failed, what is left to do, where the migration stands, the brief, validation, the drafts. Demo data sends nothing to a model.";
  }

  tasks_(): { tasks: AssistantTask[] } { return { tasks: this.tasks.map((t) => ({ ...t, entries: undefined })) }; }
  task(id: string): AssistantTask {
    const t = this.tasks.find((x) => x.id === id);
    if (!t) throw new ApiRequestError("not_found", "There is no such question.", 404);
    return { ...t };
  }
}
