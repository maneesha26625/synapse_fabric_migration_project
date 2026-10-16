import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiRequestError, type AssistantTask, type ExecutionPlanView } from "../types";
import { useAppState } from "./AppState";
import { useMigration } from "./MigrationState";

// ---- the orchestrator: the execution plan, the agent's requests, and the assistant's questions ----------------------
//
// The backend owns all of it: the plan's versions, every request and what became of it, every question and its
// conversation. This page reads them (the plan board every few seconds while the page is visible, so a request the
// agent makes shows up without a reload) and sends what a person decides.

const POLL_MS = 6000;

interface OrchestratorValue {
  plan: ExecutionPlanView | null;
  /** Why the plan could not be read (no discovery yet, or a backend without an orchestrator). */
  error: string | null;
  /** The last action's refusal, in the backend's words. */
  actionError: string | null;
  clearActionError: () => void;
  busy: string | null;
  refresh: () => Promise<void>;
  mark: (step: string, done: boolean, note?: string) => Promise<boolean>;
  decide: (id: string, decision: "confirm" | "reject", text?: string) => Promise<boolean>;
  control: (action: "pause" | "resume" | "wake") => Promise<boolean>;
  /** Requests waiting for a person: the badge on the assistant's button. */
  waiting: number;
  // The assistant.
  task: AssistantTask | null;
  tasks: AssistantTask[];
  ask: (text: string) => Promise<boolean>;
  openTask: (id: string | null) => Promise<void>;
  cancelTask: () => Promise<boolean>;
  continueTask: () => Promise<boolean>;
}

const Ctx = createContext<OrchestratorValue | null>(null);

export function useOrchestrator(): OrchestratorValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useOrchestrator must be used inside <OrchestratorProvider>");
  return v;
}

const messageOf = (e: unknown) => (e instanceof ApiRequestError || e instanceof Error ? e.message : "Something went wrong.");
/** A question still being worked on: read again until it is answered or waits for a person. */
const working = (t: AssistantTask | null) => !!t && t.status === "running";

export function OrchestratorProvider({ children }: { children: ReactNode }) {
  const { api, discovery } = useAppState();
  const { projectEpoch, execution } = useMigration();
  const [plan, setPlan] = useState<ExecutionPlanView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [task, setTask] = useState<AssistantTask | null>(null);
  const [tasks, setTasks] = useState<AssistantTask[]>([]);
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";

  const refresh = useCallback(async () => {
    try {
      setPlan(await api.getExecutionPlan());
      setError(null);
    } catch (e) {
      setPlan(null);
      setError(messageOf(e));
    }
  }, [api]);

  // Read on the project, the discovery and the run moving; then every few seconds while the page is visible.
  useEffect(() => {
    if (!discovered) { setPlan(null); setError(null); return; }
    void refresh();
  }, [refresh, discovered, projectEpoch, execution.status, execution.version]);
  useEffect(() => {
    if (!discovered) return;
    const id = setInterval(() => { if (typeof document === "undefined" || document.visibilityState !== "hidden") void refresh(); }, POLL_MS);
    return () => clearInterval(id);
  }, [refresh, discovered]);
  useEffect(() => { setTask(null); setTasks([]); }, [projectEpoch]);

  const act = useCallback(async <T,>(name: string, call: () => Promise<T>, after?: (result: T) => void): Promise<boolean> => {
    setBusy(name);
    setActionError(null);
    try {
      const out = await call();
      after?.(out);
      return true;
    } catch (e) {
      setActionError(messageOf(e));
      void refresh();
      return false;
    } finally {
      setBusy(null);
    }
  }, [refresh]);

  const mark = useCallback((step: string, done: boolean, note?: string) => act(`mark:${step}`, () => api.markStep(step, done, note), setPlan), [act, api]);
  const decide = useCallback((id: string, decision: "confirm" | "reject", text?: string) =>
    act(`decide:${id}`, () => api.decideAction(id, decision, text), (out) => {
      setPlan(out.plan);
      if (task && out.action.task === task.id) void api.getAssistantTask(task.id).then(setTask, () => undefined);
    }), [act, api, task]);
  const control = useCallback((action: "pause" | "resume" | "wake") => act(`agent:${action}`, () => api.controlAgent(action), setPlan), [act, api]);

  const listTasks = useCallback(async () => {
    try { setTasks((await api.getAssistantTasks()).tasks); } catch { /* the list is a convenience */ }
  }, [api]);
  const ask = useCallback((text: string) => act("ask", () => api.askAssistant(text, task?.status === "waiting_for_reply" ? task.id : undefined), (t) => {
    setTask(t);
    void listTasks();
    void refresh();
  }), [act, api, task, listTasks, refresh]);
  const openTask = useCallback(async (id: string | null) => {
    if (!id) { setTask(null); return; }
    try { setTask(await api.getAssistantTask(id)); } catch (e) { setActionError(messageOf(e)); }
  }, [api]);
  const cancelTask = useCallback(() => (task ? act("cancel", () => api.cancelAssistantTask(task.id), setTask) : Promise.resolve(false)), [act, api, task]);
  const continueTask = useCallback(() => (task ? act("continue", () => api.continueAssistantTask(task.id), setTask) : Promise.resolve(false)), [act, api, task]);

  // A question the model is working on is read again until it answers or waits for a person.
  const taskRef = useRef(task);
  taskRef.current = task;
  useEffect(() => {
    if (!working(task)) return;
    const id = setInterval(() => {
      const t = taskRef.current;
      if (t) void api.getAssistantTask(t.id).then((next) => { setTask(next); if (!working(next)) void refresh(); }, () => undefined);
    }, 1500);
    return () => clearInterval(id);
  }, [api, task?.id, task?.status, refresh]); // eslint-disable-line react-hooks/exhaustive-deps

  const value: OrchestratorValue = {
    plan, error, actionError, clearActionError: () => setActionError(null), busy, refresh, mark, decide, control,
    waiting: plan?.actions.length ?? 0, task, tasks, ask, openTask, cancelTask, continueTask,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
