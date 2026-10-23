import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useAppState } from "../state/AppState";
import { useMigration, type StepKey } from "../state/MigrationState";
import { useConsult } from "./ConsultState";

/**
 * The agent orchestrator: everything after the consultation, done in order. It carries out the
 * approved brief with the same actions a person uses in the six steps (build the plan, set the run
 * options, check readiness, run, retry, validate), so nothing it does is out of reach of the steps,
 * and it stops for a person wherever the brief, a risk or a failure asks for one.
 *
 * It runs in this browser tab: closing the tab stops the agent (never a run already started in
 * Fabric), and Resume picks up from the first stage that is not done.
 */

export type AgentStageKey = "brief" | "snapshot" | "dependencies" | "plan" | "readiness" | "approval" | "run" | "validate";
export type AgentStageStatus = "pending" | "running" | "waiting" | "done" | "failed" | "skipped";

export interface GateOption {
  id: string;
  label: string;
  primary?: boolean;
  /** Opens a place in the app instead of acting: the consultation, a journey step, or Connections. */
  navigate?: { view?: "consult"; step?: StepKey; connections?: boolean };
}
export interface Gate { title: string; why: string; items?: string[]; options: GateOption[] }

export interface AgentStage {
  status: AgentStageStatus;
  message: string;
  details?: string[];
  attention?: boolean;
  startedAt?: string | null;
  finishedAt?: string | null;
  gate?: Gate | null;
}

export interface AgentLogLine { at: string; stage: AgentStageKey | null; kind: "agent" | "you" | "warn" | "error"; text: string }

export type AgentStatus = "idle" | "running" | "waiting" | "stopped" | "done" | "failed";

interface AgentData {
  status: AgentStatus;
  stages: Record<AgentStageKey, AgentStage>;
  log: AgentLogLine[];
  briefVersion: number | null;
  /** The run the agent started, so it can tell its own run from one already on record. */
  runId: string | null;
  retried: boolean;
  /** A person confirmed the brief's prerequisites (gateway, capacity, network) are in place. */
  prereqConfirmed?: boolean;
  startedAt: string | null;
  finishedAt: string | null;
}

export const AGENT_STAGES: { key: AgentStageKey; title: string; blurb: string; group: string }[] = [
  { key: "brief", title: "Approved brief", blurb: "The consultation's approved answers: what moves, how, and where a person decides.", group: "Before the run" },
  { key: "snapshot", title: "Discovery snapshot", blurb: "The discovered estate the run works from. The agent never rediscovers on its own.", group: "Understand the estate" },
  { key: "dependencies", title: "Dependency analysis", blurb: "What depends on what, and the waves objects move in.", group: "Understand the estate" },
  { key: "plan", title: "Migration plan", blurb: "The objects in the brief's scope, in wave order, with the run options the brief sets.", group: "Build" },
  { key: "readiness", title: "Readiness check", blurb: "The planner's risks and checks, the Fabric connection and the credentials the run needs.", group: "Build" },
  { key: "approval", title: "Approval", blurb: "A person approves before anything is written to Fabric, when the brief asks for it.", group: "Build" },
  { key: "run", title: "Migration run", blurb: "Creates the objects in Fabric and loads the data, wave by wave; failed objects are retried as the brief says.", group: "Deploy and load" },
  { key: "validate", title: "Validation", blurb: "Compares every migrated object with Synapse, with the checks the brief asks for.", group: "Deploy and load" },
];

/** The journey steps a finished stage confirms. */
const CONFIRMS: Partial<Record<AgentStageKey, StepKey[]>> = {
  dependencies: ["discover", "assess", "waves"],
  readiness: ["plan"],
  run: ["migrate"],
  validate: ["validate"],
};

const fresh = (): Record<AgentStageKey, AgentStage> =>
  Object.fromEntries(AGENT_STAGES.map((s) => [s.key, { status: "pending", message: "Not started" }])) as Record<AgentStageKey, AgentStage>;
const EMPTY: AgentData = { status: "idle", stages: fresh(), log: [], briefVersion: null, runId: null, retried: false, startedAt: null, finishedAt: null };
const KEY = "ma.agent";
const now = () => new Date().toISOString();
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function load(key: string): AgentData {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return EMPTY;
    const d = { ...EMPTY, ...(JSON.parse(raw) as AgentData) };
    d.stages = { ...fresh(), ...d.stages };
    // A tab closed (or reloaded) mid-stage stopped the agent there; say so rather than claim it still runs.
    if (d.status === "running") {
      d.status = "stopped";
      for (const s of AGENT_STAGES) if (d.stages[s.key].status === "running") d.stages[s.key] = { ...d.stages[s.key], status: "pending", message: "Interrupted: the page was closed or reloaded" };
    }
    return d;
  } catch { return EMPTY; }
}

class Stopped extends Error {}

/** What a stage's work ends in. */
type Outcome =
  | { done: string; details?: string[]; attention?: boolean }
  | { skipped: string }
  | { wait: Gate; message: string }
  | { fail: string; details?: string[] };

interface AgentValue {
  data: AgentData;
  /** The stage being worked on or waiting, for "Follow the agent". */
  activeStage: AgentStageKey | null;
  start: () => void;
  stop: () => void;
  reset: () => void;
  choose: (stage: AgentStageKey, optionId: string) => void;
  ask: (text: string) => void;
}

const Ctx = createContext<AgentValue | null>(null);
export function useAgent(): AgentValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAgent must be used inside <OrchestratorProvider>");
  return v;
}

export function OrchestratorProvider({ children }: { children: ReactNode }) {
  const app = useAppState();
  const mig = useMigration();
  const consult = useConsult();
  const key = `${KEY}.${app.mode}.${mig.project.id}`;
  const [data, setDataState] = useState<AgentData>(() => load(key));
  const dataRef = useRef(data);
  // Every change goes through here and lands in the ref at once, so the loop reads what it just wrote.
  const setData = useCallback((fn: (d: AgentData) => AgentData) => {
    const next = fn(dataRef.current);
    dataRef.current = next;
    setDataState(next);
  }, []);
  useEffect(() => { const d = load(key); dataRef.current = d; setDataState(d); }, [key]);
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(data)); } catch { /* storage unavailable */ } }, [key, data]);

  // The agent reads the app as it is now, never as it was when a stage began.
  const live = useRef({ app, mig, consult });
  live.current = { app, mig, consult };
  const token = useRef({ stopped: true });

  const log = useCallback((stage: AgentStageKey | null, text: string, kind: AgentLogLine["kind"] = "agent") =>
    setData((d) => ({ ...d, log: [...d.log, { at: now(), stage, kind, text }].slice(-300) })), [setData]);
  const patchStage = useCallback((k: AgentStageKey, patch: Partial<AgentStage>) =>
    setData((d) => ({ ...d, stages: { ...d.stages, [k]: { ...d.stages[k], ...patch } } })), [setData]);

  const waitFor = async (cond: () => boolean, timeoutMs = 30000) => {
    const end = Date.now() + timeoutMs;
    while (!cond()) {
      if (token.current.stopped) throw new Stopped();
      if (Date.now() > end) return false;
      await sleep(150);
    }
    return true;
  };

  // ---- the stages ------------------------------------------------------------------------

  const work: Record<AgentStageKey, () => Promise<Outcome>> = {
    brief: async () => {
      const c = live.current.consult;
      if (!c.approved) {
        const why = c.changedSinceApproval
          ? "The answers changed since the brief was approved. Submit and approve the new version: the agent only carries out an approved brief."
          : c.state === "submitted" ? "The brief is submitted and waits for approval." : "The consultation's brief has not been approved yet.";
        return { message: "Waiting for an approved brief", wait: { title: "Approve the brief first", why, options: [{ id: "recheck", label: "Check again" }, { id: "open", label: "Open the consultation", primary: true, navigate: { view: "consult" } }] } };
      }
      setData((d) => ({ ...d, briefVersion: c.approved!.version }));
      const v = c.versions.find((x) => x.version === c.approved!.version);
      return { done: `Brief v${c.approved.version} approved${v?.approvedBy ? ` by ${v.approvedBy}` : ""}.`, details: [`Stages: ${c.approved.settings.stages.map((k) => c.facts.stages.find((s) => s.key === k)?.label ?? k).join(", ") || "none"}`] };
    },

    snapshot: async () => {
      const { discovery } = live.current.app;
      if (discovery.state === "running") await waitFor(() => live.current.app.discovery.state !== "running", 10 * 60_000);
      const d = live.current.app.discovery;
      if (d.state !== "completed" && d.state !== "completed_with_warnings") {
        return { message: "No discovery to work from", wait: { title: "Run discovery first", why: "The agent works from a discovery snapshot and never rediscovers on its own.", options: [{ id: "recheck", label: "Check again" }, { id: "open", label: "Open Discover", primary: true, navigate: { step: "discover" } }] } };
      }
      const s = d.summary;
      const warnings = s?.warningCount ?? 0;
      return { done: `${(s?.total ?? 0).toLocaleString()} objects discovered${d.workspace ? ` in ${d.workspace}` : ""}${d.finishedAt ? ` (${new Date(d.finishedAt).toLocaleString()})` : ""}.`, attention: warnings > 0, details: warnings ? [`${warnings} discovery warning${warnings === 1 ? "" : "s"}: see Discover.`] : undefined };
    },

    dependencies: async () => {
      await waitFor(() => !!live.current.mig.graph || !!live.current.mig.graphError, 60_000);
      const { graph, graphError } = live.current.mig;
      if (!graph) return { fail: graphError ?? "The dependency graph could not be read." };
      return { done: `${graph.nodes.length.toLocaleString()} objects in ${graph.waves.length} wave${graph.waves.length === 1 ? "" : "s"}, ${graph.edges.length.toLocaleString()} dependencies.` };
    },

    plan: async () => {
      const { mig: m, consult: c } = live.current;
      const settings = c.approved?.settings;
      if (!settings || !m.graph) return { fail: "The brief or the dependency graph is missing; resume from the start." };
      const staged = new Set(c.facts.stages.flatMap((s) => s.types));
      const types = new Set(settings.planTypes);
      // A pilot moves the first dependency wave only.
      const firstWave = Math.min(...m.graph.nodes.map((n) => n.wave));
      const inWave = (w: number) => !settings.waves || settings.waves.map((x) => x - 1 + firstWave).includes(w);
      const ids = m.graph.nodes.filter((n) => (types.has(n.type) || (settings.includeUnstaged && !staged.has(n.type))) && inWave(n.wave)).map((n) => n.id);
      if (!ids.length) return { fail: "The brief's scope holds none of the discovered objects. Widen the scope in the consultation." };
      m.clearPlan();
      m.addToPlan(ids);
      m.setOptions(settings.options);
      m.setValidationRuntime(settings.validation);
      const same = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);
      const ok = await waitFor(() => live.current.mig.plan.length === ids.length && same(live.current.mig.options.stages, settings.stages));
      if (!ok) return { fail: "The plan did not take the brief's scope. Open the Plan step to see why." };
      const waves = new Set(live.current.mig.plan.map((p) => p.wave)).size;
      return { done: `${ids.length.toLocaleString()} objects in ${waves} wave${waves === 1 ? "" : "s"}${settings.waves ? " (a pilot: the first wave)" : ""}, ${settings.stages.length} stages. It replaces the plan in the Plan tab.` };
    },

    readiness: async () => {
      const m0 = live.current.mig;
      // What an administrator sets up is confirmed by a person; the agent never does it itself.
      const prerequisites = live.current.consult.approved?.settings.prerequisites ?? [];
      if (prerequisites.length && !dataRef.current.prereqConfirmed) {
        return { message: "Waiting for prerequisites to be confirmed", wait: { title: "Confirm the prerequisites", why: "The brief names things an administrator sets up before the run. Confirm they are in place; the agent does not set them up or work around them.", items: prerequisites, options: [{ id: "prereqs-ok", label: "They are in place, continue", primary: true }, { id: "open", label: "Open Connections", navigate: { connections: true } }] } };
      }
      if (live.current.mig.fabric.status !== "connected") {
        return { message: "Microsoft Fabric is not connected", wait: { title: "Connect Microsoft Fabric", why: "The run writes to a Fabric workspace; connect one on the Connections page.", options: [{ id: "recheck", label: "Check again" }, { id: "open", label: "Open Connections", primary: true, navigate: { connections: true } }] } };
      }
      await m0.analyze(true);
      await waitFor(() => !live.current.mig.analysisBusy);
      const { analysis, analysisError, capabilities, credentials, options } = live.current.mig;
      if (!analysis) return { fail: analysisError ?? "The planner did not return an analysis." };
      const blocking = analysis.risks.filter((r) => r.severity === "BLOCKING");
      const settings = live.current.consult.approved?.settings;
      const on = new Set(options.stages);
      const missing = settings?.needsCredentials
        ? (capabilities?.linkedServices ?? []).filter((ls) => on.has(ls.stage ?? "connections") && !ls.unsupported
          && !Object.values(credentials[ls.name] ?? {}).some((v) => typeof v === "string" && v.trim()))
        : [];
      if (blocking.length || missing.length) {
        const items = [
          ...blocking.map((r) => `Blocking: ${r.title}. ${r.message}`),
          ...missing.map((ls) => `Credentials needed for ${ls.name} (${ls.type}).`),
        ];
        const options: GateOption[] = [
          { id: "recheck", label: "Check again", primary: true },
          { id: "open", label: "Open the Plan step", navigate: { step: "plan" } },
        ];
        if (missing.length) options.push({ id: "drop-credential-stages", label: "Leave out the stages that need credentials" });
        return { message: `${blocking.length + missing.length} thing${blocking.length + missing.length === 1 ? "" : "s"} to fix before the run`, wait: { title: "The run is not ready", why: "The agent will not start a run the planner blocks, or one that would fail for lack of credentials. Fix these in the Plan step (credentials are under Stages & credentials), then check again.", items, options } };
      }
      const high = analysis.riskCounts.HIGH ?? 0;
      return { done: `Readiness ${analysis.readiness}%. ${analysis.objects.toLocaleString()} objects, about ${analysis.effortDays} days of effort, no blocking risks.`, attention: high > 0, details: high ? [`${high} high risk${high === 1 ? "" : "s"}: listed in the Plan step.`] : undefined };
    },

    approval: async () => {
      const { consult: c, mig: m } = live.current;
      if (!c.approved?.settings.approval) return { skipped: "The brief lets the agent start the run without asking." };
      if (dataRef.current.stages.approval.message.startsWith("Approved")) return { done: dataRef.current.stages.approval.message };
      const labels = m.options.stages.map((k) => m.capabilities?.stages.find((s) => s.key === k)?.label ?? k);
      // With "stop for approval", the objects no rule can convert are named here, before anything is written.
      const planned = new Set(m.plan.map((p) => p.id));
      const unconvertible = c.approved.settings.approveUnsupported
        ? (m.graph?.nodes ?? []).filter((n) => planned.has(n.id) && (n.classification === "MANUAL" || n.classification === "NOT SUPPORTED"))
        : [];
      return {
        message: "Waiting for a person to approve",
        wait: {
          title: `Approve writing to ${m.fabric.workspaceName ?? "the Fabric workspace"}`,
          why: `The agent is about to migrate ${m.plan.length.toLocaleString()} objects with these stages. Nothing has been written to Fabric yet.${unconvertible.length ? ` ${unconvertible.length} of them have no automatic route; the run defers them for a person.` : ""}`,
          items: [...labels, ...unconvertible.slice(0, 10).map((n) => `No automatic route: ${n.name} (${n.type})`)],
          options: [{ id: "approve", label: `Approve and start the run`, primary: true }, { id: "open", label: "Review the plan", navigate: { step: "plan" } }],
        },
      };
    },

    run: async () => {
      const d0 = dataRef.current;
      let e = live.current.mig.execution;
      if (e.state !== "idle" && e.runId !== d0.runId) {
        return { message: "A run is already on record", wait: { title: "A run is already on record", why: `Run ${e.runId || ""} is ${e.state}. Start a new run (the record of that one is cleared, never what it created in Fabric), or carry on with its result.`, options: [{ id: "new-run", label: "Start a new run", primary: true }, { id: "use-run", label: "Use that run's result" }] } };
      }
      if (e.state === "idle") {
        const ok = await live.current.mig.startExecution();
        if (!ok) return { fail: live.current.mig.executionError ?? "The run could not start." };
        await waitFor(() => live.current.mig.execution.runId !== "" && live.current.mig.execution.state !== "idle");
        e = live.current.mig.execution;
        setData((d) => ({ ...d, runId: e.runId, retried: false }));
        log("run", `Run ${e.runId} started: ${e.total.toLocaleString()} objects.`);
      }
      for (;;) {
        // Report progress while the run works; a person can watch it in the Migrate step too.
        while (live.current.mig.execution.state === "running") {
          const x = live.current.mig.execution;
          const total = x.total || 1;
          patchStage("run", { message: `${(x.completed + x.skipped + x.failed + x.deferred).toLocaleString()} of ${x.total.toLocaleString()} done${x.failed ? `, ${x.failed} failed` : ""} (${Math.round(((x.completed + x.skipped + x.failed + x.deferred) / total) * 100)}%)` });
          if (token.current.stopped) throw new Stopped();
          await sleep(700);
        }
        const x = live.current.mig.execution;
        if (x.state === "paused") {
          return { message: "The run paused", wait: { title: "The run paused", why: x.haltedReason ?? "The run stopped at a failure, as the brief asks.", items: x.items.filter((i) => i.status === "FAILED").slice(0, 8).map((i) => `${i.name}: ${i.error ?? "failed"}`), options: [{ id: "retry-run", label: "Retry the failed objects", primary: true }, { id: "resume-run", label: "Resume the run" }, { id: "open", label: "Open Migrate", navigate: { step: "migrate" } }] } };
        }
        if (x.state === "completed" && x.failed > 0 && live.current.consult.approved?.settings.failures === "retry" && !dataRef.current.retried) {
          setData((d) => ({ ...d, retried: true }));
          log("run", `${x.failed} object${x.failed === 1 ? "" : "s"} failed; retrying them once, as the brief asks.`, "warn");
          await live.current.mig.controlExecution("retry");
          await waitFor(() => live.current.mig.execution.state === "running", 15000);
          continue;
        }
        const failed = x.items.filter((i) => i.status === "FAILED");
        return {
          done: `${x.completed.toLocaleString()} created, ${x.skipped.toLocaleString()} already in Fabric, ${x.deferred.toLocaleString()} left for a person, ${x.failed.toLocaleString()} failed.`,
          attention: x.failed > 0,
          details: failed.slice(0, 8).map((i) => `${i.name}: ${i.error ?? "failed"}`),
        };
      }
    },

    validate: async () => {
      const settings = live.current.consult.approved?.settings;
      if (settings) {
        live.current.mig.setValidationRuntime(settings.validation);
        await waitFor(() => JSON.stringify(live.current.mig.validationRuntime) === JSON.stringify(settings.validation), 5000);
      }
      await live.current.mig.runValidation();
      const { validation, validationError } = live.current.mig;
      if (!validation) {
        return { message: "Validation could not run", wait: { title: "Validation could not run", why: validationError ?? "The comparison did not return results.", options: [{ id: "recheck", label: "Try again", primary: true }, { id: "open", label: "Open Validate", navigate: { step: "validate" } }] } };
      }
      const n = (s: string) => validation.filter((r) => r.status === s).length;
      const mism = validation.filter((r) => r.status === "MISMATCH");
      // The brief says whether a mismatch stops the migration for a person, or is only reported.
      const gate = settings?.validationGate ?? "critical";
      if (mism.length && gate !== "critical") {
        const options: GateOption[] = [{ id: "recheck", label: "Run validation again", primary: true }, { id: "open", label: "Open the Validation tab", navigate: { step: "validate" } }];
        if (gate === "exceptions") options.push({ id: "accept-exceptions", label: `Approve ${mism.length === 1 ? "this exception" : `these ${mism.length} exceptions`}` });
        return {
          message: `${mism.length} mismatch${mism.length === 1 ? "" : "es"}: waiting for a person`,
          wait: {
            title: "Validation found mismatches",
            why: gate === "all" ? "The brief says every object must pass before cutover. Fix them (the Validation tab shows each), then run validation again." : "The brief allows approved exceptions. Review each one; approve them, or fix them and run validation again.",
            items: mism.slice(0, 10).map((r) => `${r.object} (${r.category}): ${r.detail ?? `${r.source} vs ${r.target}`}`),
            options,
          },
        };
      }
      return {
        done: `${validation.length.toLocaleString()} checks: ${n("MATCH").toLocaleString()} match, ${n("REVIEW").toLocaleString()} to review, ${mism.length.toLocaleString()} mismatched.`,
        attention: mism.length > 0,
        details: mism.slice(0, 8).map((r) => `${r.object} (${r.category}): ${r.detail ?? `${r.source} vs ${r.target}`}`),
      };
    },
  };

  // ---- the loop ---------------------------------------------------------------------------

  const drive = useCallback(async () => {
    const t = { stopped: false };
    token.current.stopped = true; // a loop already going stops at its next wait
    token.current = t;
    setData((d) => ({ ...d, status: "running", startedAt: d.startedAt ?? now(), finishedAt: null }));
    try {
      for (const s of AGENT_STAGES) {
        const st = dataRef.current.stages[s.key];
        if (st.status === "done" || st.status === "skipped") continue;
        if (t.stopped) return;
        patchStage(s.key, { status: "running", message: "Working…", gate: null, details: undefined, attention: false, startedAt: now(), finishedAt: null });
        const out = await work[s.key]();
        if (t.stopped) return;
        if ("done" in out) {
          patchStage(s.key, { status: "done", message: out.done, details: out.details, attention: out.attention, finishedAt: now() });
          // The journey's steps follow the agent, so their tabs open as it reaches them.
          for (const k of CONFIRMS[s.key] ?? []) live.current.mig.confirmStep(k);
          log(s.key, `${s.title}: ${out.done}`, out.attention ? "warn" : "agent");
        } else if ("skipped" in out) {
          patchStage(s.key, { status: "skipped", message: out.skipped, finishedAt: now() });
          log(s.key, `${s.title} skipped: ${out.skipped}`);
        } else if ("wait" in out) {
          patchStage(s.key, { status: "waiting", message: out.message, gate: out.wait });
          setData((d) => ({ ...d, status: "waiting" }));
          log(s.key, `${s.title}: waiting for you. ${out.wait.title}.`, "warn");
          return;
        } else {
          patchStage(s.key, { status: "failed", message: out.fail, details: out.details, finishedAt: now() });
          setData((d) => ({ ...d, status: "failed" }));
          log(s.key, `${s.title} failed: ${out.fail}`, "error");
          return;
        }
      }
      setData((d) => ({ ...d, status: "done", finishedAt: now() }));
      log(null, "The agent finished every stage.");
    } catch (e) {
      if (e instanceof Stopped) return;
      const msg = e instanceof Error ? e.message : "Something went wrong.";
      const k = AGENT_STAGES.find((s) => dataRef.current.stages[s.key].status === "running")?.key ?? null;
      if (k) patchStage(k, { status: "failed", message: msg, finishedAt: now() });
      setData((d) => ({ ...d, status: "failed" }));
      log(k, `Stopped by an error: ${msg}`, "error");
    } finally {
      if (token.current === t) t.stopped = true;
    }
  }, [log, patchStage, setData]); // eslint-disable-line react-hooks/exhaustive-deps

  // Never keep looping after the provider goes away.
  useEffect(() => () => { token.current.stopped = true; }, []);

  const start = useCallback(() => {
    const d = dataRef.current;
    // A failed stage is tried again; a finished agent starts over from the brief.
    if (d.status === "done") setData((x) => ({ ...x, stages: fresh(), runId: null, retried: false, prereqConfirmed: false, startedAt: null }));
    else setData((x) => ({ ...x, stages: Object.fromEntries(Object.entries(x.stages).map(([k, s]) => [k, s.status === "failed" || s.status === "waiting" ? { ...s, status: "pending", gate: null } : s])) as AgentData["stages"] }));
    log(null, d.status === "idle" || d.status === "done" ? "Agent started." : "Agent resumed.", "you");
    void drive();
  }, [drive, log, setData]);

  const stop = useCallback(() => {
    token.current.stopped = true;
    const m = live.current.mig;
    // A run the agent started is paused with it, so nothing keeps writing behind a stopped agent.
    if (m.execution.state === "running" && m.execution.runId === dataRef.current.runId) void m.controlExecution("pause");
    setData((d) => ({
      ...d, status: "stopped",
      stages: Object.fromEntries(Object.entries(d.stages).map(([k, s]) => [k, s.status === "running" ? { ...s, status: "pending", message: "Stopped" } : s])) as AgentData["stages"],
    }));
    log(null, "Agent stopped.", "you");
  }, [log, setData]);

  const reset = useCallback(() => {
    token.current.stopped = true;
    setData(() => EMPTY);
  }, [setData]);

  const choose = useCallback((stage: AgentStageKey, optionId: string) => {
    const m = live.current.mig;
    const resume = () => { patchStage(stage, { status: "pending", gate: null }); void drive(); };
    const label = dataRef.current.stages[stage].gate?.options.find((o) => o.id === optionId)?.label ?? optionId;
    log(stage, label, "you");
    switch (optionId) {
      case "recheck": return resume();
      case "prereqs-ok":
        setData((d) => ({ ...d, prereqConfirmed: true }));
        return resume();
      case "accept-exceptions": {
        const mism = (m.validation ?? []).filter((r) => r.status === "MISMATCH").length;
        patchStage(stage, { status: "done", gate: null, attention: true, message: `${mism} mismatch${mism === 1 ? "" : "es"} approved as exceptions by a person.`, finishedAt: now() });
        m.confirmStep("validate");
        void drive();
        return;
      }
      case "approve":
        patchStage(stage, { status: "done", gate: null, message: `Approved at ${new Date().toLocaleTimeString()}.`, finishedAt: now() });
        void drive();
        return;
      case "drop-credential-stages": {
        const needing = new Set((m.capabilities?.stages ?? []).filter((s) => s.needsInput === "credentials").map((s) => s.key));
        m.setOptions({ stages: m.options.stages.filter((k) => !needing.has(k)) });
        log(stage, `Left out: ${[...needing].join(", ")}. The brief still says otherwise; the migration report lists what was not moved.`, "warn");
        return resume();
      }
      case "new-run":
        void m.resetStep("migrate").then(resume, (e) => log(stage, e instanceof Error ? e.message : "The run could not be reset.", "error"));
        return;
      case "use-run":
        setData((d) => ({ ...d, runId: m.execution.runId }));
        return resume();
      case "retry-run": void m.controlExecution("retry").then(resume); return;
      case "resume-run": void m.controlExecution("resume").then(resume); return;
      default: return;
    }
  }, [drive, log, patchStage, setData]);

  /** The "ask the orchestrator" box. Without an AI model it understands a few plain commands. */
  const ask = useCallback((raw: string) => {
    const text = raw.trim();
    if (!text) return;
    log(null, text, "you");
    const t = text.toLowerCase();
    const d = dataRef.current;
    const x = live.current.mig.execution;
    const reply = (s: string, kind: AgentLogLine["kind"] = "agent") => log(null, s, kind);
    if (/^(\?|help)|what can you/.test(t)) {
      return reply("I understand: start, stop, resume, status, retry (the failed objects), why (what failed and why), and mismatches. The AI model is off here, so anything else is kept as a note.");
    }
    if (/\b(start|go|begin)\b/.test(t) && d.status !== "running") return start();
    if (/\b(resume|continue)\b/.test(t) && d.status !== "running") return start();
    if (/\b(stop|halt|pause)\b/.test(t)) return d.status === "running" ? stop() : reply("The agent is not running.");
    if (/\bretry\b/.test(t)) {
      if (x.state === "running") return reply("The run is still working; retry once it finishes.");
      if (!x.failed) return reply("Nothing has failed in the run, so there is nothing to retry.");
      void live.current.mig.controlExecution("retry");
      return reply(`Retrying ${x.failed} failed object${x.failed === 1 ? "" : "s"}. Follow it under Migration run.`);
    }
    if (/\b(why|fail|failed|error|errors)\b/.test(t)) {
      const failed = x.items.filter((i) => i.status === "FAILED");
      if (!failed.length) return reply("Nothing in the run has failed.");
      return reply(`${failed.length} failed: ${failed.slice(0, 5).map((i) => `${i.name} (${i.error ?? "no reason given"})`).join("; ")}${failed.length > 5 ? "; …" : ""}`);
    }
    if (/\bmismatch/.test(t)) {
      const mism = (live.current.mig.validation ?? []).filter((r) => r.status === "MISMATCH");
      if (!live.current.mig.validation) return reply("Validation has not run yet.");
      return reply(mism.length ? `${mism.length} mismatched: ${mism.slice(0, 5).map((r) => `${r.object} (${r.detail ?? r.category})`).join("; ")}` : "Validation found no mismatches.");
    }
    if (/\b(status|where|progress|doing)\b/.test(t)) {
      const at = AGENT_STAGES.find((s) => ["running", "waiting", "failed"].includes(d.stages[s.key].status));
      return reply(at ? `${at.title}: ${d.stages[at.key].message}` : d.status === "done" ? "Every stage is done." : "The agent has not started.");
    }
    reply("Kept as a note. The AI model is off, so I only understand a few commands: type ? for the list.");
  }, [log, start, stop]);

  const activeStage = AGENT_STAGES.find((s) => ["running", "waiting", "failed"].includes(data.stages[s.key].status))?.key ?? null;
  const value: AgentValue = { data, activeStage, start, stop, reset, choose, ask };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
