import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useConsult } from "../agent/ConsultState";
import { useAgent } from "../agent/OrchestratorState";
import { stepMeta } from "../components/journey/steps";
import { useAppState } from "./AppState";
import { useMigration, type StepKey } from "./MigrationState";

/**
 * One activity log for the whole application: connections, discovery, the plan, runs, validation,
 * the consultation and the agent, in the order they happened. It watches the state the pages
 * already share, so every action is recorded however it was started (by a person on a step, or by
 * the agent orchestrator).
 */

export type ActivitySource = "you" | "agent" | "run" | "system";
export type ActivityLevel = "info" | "warn" | "error";
export interface ActivityEntry { at: string; source: ActivitySource; level: ActivityLevel; text: string }

interface ActivityValue {
  entries: ActivityEntry[];
  record: (text: string, source?: ActivitySource, level?: ActivityLevel) => void;
  clear: () => void;
}

const KEY = "ma.activity";
const MAX = 500;
const Ctx = createContext<ActivityValue | null>(null);

export function useActivity(): ActivityValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useActivity must be used inside <ActivityProvider>");
  return v;
}

function load(key: string): ActivityEntry[] {
  try { return JSON.parse(localStorage.getItem(key) ?? "[]") as ActivityEntry[]; } catch { return []; }
}

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

export function ActivityProvider({ children }: { children: ReactNode }) {
  const { mode, connection, discovery, ready } = useAppState();
  const mig = useMigration();
  const consult = useConsult();
  const agent = useAgent();
  // Per data mode and project, like everything else this browser keeps.
  const key = `${KEY}.${mode}.${mig.project.id}`;
  const [entries, setEntries] = useState<ActivityEntry[]>(() => load(key));
  useEffect(() => { setEntries(load(key)); }, [key]);
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(entries)); } catch { /* storage unavailable */ } }, [key, entries]);

  const add = useCallback((batch: ActivityEntry[]) => {
    if (batch.length) setEntries((e) => [...e, ...batch].slice(-MAX));
  }, []);
  const record = useCallback((text: string, source: ActivitySource = "you", level: ActivityLevel = "info") =>
    add([{ at: new Date().toISOString(), source, level, text }]), [add]);
  const clear = useCallback(() => setEntries([]), []);

  // What was seen last, so only changes are recorded. Reset when the project or mode changes:
  // a page loading (or a project opening) is not something that happened.
  const seen = useRef<Record<string, unknown> | null>(null);
  useEffect(() => { seen.current = null; }, [key]);

  const run = mig.execution;
  const validationSummary = mig.validation
    ? `${mig.validation.filter((r) => r.status === "MATCH").length}/${mig.validation.filter((r) => r.status === "REVIEW").length}/${mig.validation.filter((r) => r.status === "MISMATCH").length}`
    : null;
  useEffect(() => {
    // Until every part has been read once, a change is the page loading, not something that happened.
    if (!ready || !mig.fabricReady || !mig.executionReady) { seen.current = null; return; }
    const now = {
      connection: connection.status, workspace: connection.workspace ?? null, fabric: mig.fabric.status, fabricName: mig.fabric.workspaceName ?? null, fabricError: mig.fabricError,
      discovery: discovery.state, graph: mig.graph ? `${mig.graph.nodes.length}/${mig.graph.waves.length}` : null,
      plan: mig.plan.length, blocking: mig.analysis?.blocking ?? null,
      runId: run.runId, runState: run.state, failedIds: run.items.filter((i) => i.status === "FAILED").map((i) => i.id), logs: run.logs.length,
      validation: validationSummary, validationError: mig.validationError, executionError: mig.executionError,
      confirmed: mig.confirmed, notes: consult.notes.length, agentLog: agent.data.log.length,
    };
    const was = seen.current as typeof now | null;
    seen.current = now;
    if (!was) return;
    const at = new Date().toISOString();
    const out: ActivityEntry[] = [];
    const push = (text: string, source: ActivitySource = "system", level: ActivityLevel = "info") => out.push({ at, source, level, text });

    if (now.connection !== was.connection) {
      if (now.connection === "connected") push(`Source connected: ${now.workspace ?? "Azure Synapse"}.`, "you");
      else if (was.connection === "connected") push("Source disconnected.", "you");
    }
    if (now.fabric !== was.fabric) {
      if (now.fabric === "connected") push(`Fabric target connected: ${now.fabricName ?? "workspace"}.`, "you");
      else if (was.fabric === "connected") push("Fabric target disconnected.", "you");
    }
    if (now.fabricError && now.fabricError !== was.fabricError) push(`The Fabric connection failed: ${now.fabricError}`, "system", "error");
    if (now.discovery !== was.discovery) {
      if (now.discovery === "running") push(`Discovery started${now.workspace ? ` on ${now.workspace}` : ""}.`, "run");
      else if (now.discovery === "completed") push(`Discovery finished: ${plural(discovery.summary?.total ?? 0, "object")}.`, "run");
      else if (now.discovery === "completed_with_warnings") push(`Discovery finished with ${plural(discovery.summary?.warningCount ?? 0, "warning")}: ${plural(discovery.summary?.total ?? 0, "object")}.`, "run", "warn");
      else if (now.discovery === "failed") push(`Discovery failed${discovery.error ? `: ${discovery.error}` : "."}`, "run", "error");
      else if (now.discovery === "idle" && was.discovery !== "idle") push("Discovery was reset.", "you");
    }
    if (now.graph && now.graph !== was.graph && mig.graph) push(`Dependency graph built: ${plural(mig.graph.nodes.length, "object")} in ${plural(mig.graph.waves.length, "wave")}.`, "system");
    if (now.plan !== was.plan) push(now.plan ? `Plan changed: ${plural(now.plan, "object")}.` : "The plan was cleared.", "you");
    if (now.blocking !== was.blocking && now.blocking) push(`The plan has ${plural(now.blocking, "blocking risk")}.`, "system", "warn");
    if (now.runId && now.runId !== was.runId) push(`Migration run ${now.runId} started: ${plural(run.total, "object")}.`, "run");
    if (now.runState !== was.runState) {
      if (now.runState === "completed") push(`Migration run ${now.runId} finished: ${run.completed} created, ${run.skipped} already there, ${run.deferred} left for a person, ${run.failed} failed.`, "run", run.failed ? "warn" : "info");
      else if (now.runState === "paused") push(`Migration run ${now.runId} paused${run.haltedReason ? `: ${run.haltedReason}` : "."}`, "run", "warn");
      else if (now.runState === "running" && was.runState !== "idle" && now.runId === was.runId) push(`Migration run ${now.runId} resumed.`, "run");
      else if (now.runState === "idle" && was.runState !== "idle") push("The migration run's record was reset.", "you");
    }
    const newlyFailed = now.failedIds.filter((id) => !was.failedIds.includes(id));
    for (const item of run.items.filter((i) => newlyFailed.includes(i.id)).slice(0, 20)) push(`${item.name} failed: ${item.error ?? "no reason given"}`, "run", "error");
    if (now.logs > was.logs && now.runId === was.runId) for (const line of run.logs.slice(was.logs).slice(-30)) push(line, "run");
    if (now.validation && now.validation !== was.validation && mig.validation) {
      const [match, review, mismatch] = now.validation.split("/").map(Number);
      push(`Validation: ${plural(match, "match", "matches")}, ${review} to review, ${plural(mismatch, "mismatch", "mismatches")}.`, "run", mismatch ? "warn" : "info");
    }
    if (now.validationError && now.validationError !== was.validationError) push(`Validation failed: ${now.validationError}`, "run", "error");
    if (now.executionError && now.executionError !== was.executionError) push(`The run could not start: ${now.executionError}`, "run", "error");
    for (const k of now.confirmed.filter((c) => !was.confirmed.includes(c))) push(`Confirmed ${stepMeta(k as StepKey).title}.`, "you");
    for (const n of consult.notes.slice(was.notes)) push(`Consultation: ${n.text}`, n.kind === "system" ? "system" : "you");
    for (const l of agent.data.log.slice(was.agentLog)) push(l.text, l.kind === "you" ? "you" : "agent", l.kind === "warn" ? "warn" : l.kind === "error" ? "error" : "info");
    add(out);
  });

  return <Ctx.Provider value={{ entries, record, clear }}>{children}</Ctx.Provider>;
}
