import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  ApiRequestError,
  type DependencyGraph,
  type ExecutionRun,
  type FabricConfig,
  type FabricTarget,
  type PlanItem,
  type PlanStatus,
  type ValidationRow,
} from "../types";
import { useAppState } from "./AppState";

// ---- projects (local labels) ----------------------------------------------------
//
// A project is a label the operator organises work under. The backend holds one
// connection and one discovery at a time, so projects do not separate data;
// they name it. Only names and ids are stored, in this browser.

export interface Project { id: string; name: string; createdAt: string }
const PROJECTS_KEY = "ma.projects";
const PLAN_KEY = "ma.plan";

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}
function save(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
}

const DEFAULT_PROJECT: Project = { id: "default", name: "Synapse to Fabric migration", createdAt: new Date(0).toISOString() };

const EMPTY_RUN: ExecutionRun = { runId: "", state: "idle", total: 0, completed: 0, inProgress: 0, failed: 0, pending: 0, items: [], logs: [] };

interface MigrationStateValue {
  projects: Project[];
  project: Project;
  addProject: (name: string) => void;
  selectProject: (id: string) => void;

  /** The dependency graph and suggested waves, for the current discovery. */
  graph: DependencyGraph | null;
  graphError: string | null;
  graphLoading: boolean;
  reloadGraph: () => void;

  plan: PlanItem[];
  addToPlan: (ids: string[]) => void;
  addAllToPlan: () => void;
  removeFromPlan: (id: string) => void;
  setPlanWave: (id: string, wave: number) => void;
  movePlanItem: (id: string, direction: -1 | 1) => void;
  clearPlan: () => void;
  inPlan: (id: string) => boolean;
  /** READY / BLOCKED from the plan and the graph; the run's status once it has one. */
  planStatus: (id: string) => PlanStatus;

  fabric: FabricTarget;
  fabricBusy: "authenticate" | "test" | null;
  fabricError: string | null;
  authenticateFabric: (c: FabricConfig) => Promise<void>;
  testFabric: (c: FabricConfig) => Promise<void>;
  disconnectFabric: () => Promise<void>;

  execution: ExecutionRun;
  executionError: string | null;
  startExecution: () => Promise<boolean>;
  controlExecution: (action: "pause" | "resume" | "retry") => Promise<void>;

  validation: ValidationRow[] | null;
  validationError: string | null;
  validationBusy: boolean;
  runValidation: () => Promise<void>;
}

const Ctx = createContext<MigrationStateValue | null>(null);

export function useMigration(): MigrationStateValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useMigration must be used inside <MigrationStateProvider>");
  return v;
}

const messageOf = (e: unknown) => (e instanceof ApiRequestError || e instanceof Error ? e.message : "Something went wrong.");

export function MigrationStateProvider({ children }: { children: ReactNode }) {
  const { api, mode, discovery, isConnected } = useAppState();

  // ---- projects
  const [projects, setProjects] = useState<Project[]>(() => load<Project[]>(PROJECTS_KEY, [DEFAULT_PROJECT]));
  const [projectId, setProjectId] = useState<string>(() => load<string>(`${PROJECTS_KEY}.current`, DEFAULT_PROJECT.id));
  const project = projects.find((p) => p.id === projectId) ?? projects[0] ?? DEFAULT_PROJECT;
  useEffect(() => save(PROJECTS_KEY, projects), [projects]);
  useEffect(() => save(`${PROJECTS_KEY}.current`, projectId), [projectId]);
  const addProject = useCallback((name: string) => {
    const created: Project = { id: `p${Date.now().toString(36)}`, name: name.trim(), createdAt: new Date().toISOString() };
    setProjects((ps) => [...ps, created]);
    setProjectId(created.id);
  }, []);

  // ---- graph (loaded once per completed discovery)
  const done = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const [graph, setGraph] = useState<DependencyGraph | null>(null);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphEpoch, setGraphEpoch] = useState(0);
  useEffect(() => {
    if (!done) { setGraph(null); setGraphError(null); return; }
    let cancelled = false;
    setGraphLoading(true);
    setGraphError(null);
    api.getDependencies().then(
      (g) => !cancelled && setGraph(g),
      (e) => !cancelled && setGraphError(messageOf(e)),
    ).finally(() => !cancelled && setGraphLoading(false));
    return () => { cancelled = true; };
  }, [api, done, discovery.finishedAt, graphEpoch]);

  // ---- plan (ids and waves only, per mode so demo ids never leak into live)
  const planKey = `${PLAN_KEY}.${mode}.${project.id}`;
  const [plan, setPlan] = useState<PlanItem[]>(() => load<PlanItem[]>(planKey, []));
  useEffect(() => { setPlan(load<PlanItem[]>(planKey, [])); }, [planKey]);
  useEffect(() => save(planKey, plan), [planKey, plan]);

  const waveOf = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n.wave])), [graph]);
  const addToPlan = useCallback((ids: string[]) => {
    setPlan((current) => {
      const have = new Set(current.map((p) => p.id));
      const added = ids.filter((id) => !have.has(id)).map((id) => ({ id, wave: waveOf.get(id) ?? 1 }));
      return [...current, ...added];
    });
  }, [waveOf]);
  const addAllToPlan = useCallback(() => addToPlan((graph?.nodes ?? []).map((n) => n.id)), [addToPlan, graph]);
  const removeFromPlan = useCallback((id: string) => setPlan((p) => p.filter((x) => x.id !== id)), []);
  const setPlanWave = useCallback((id: string, wave: number) => setPlan((p) => p.map((x) => (x.id === id ? { ...x, wave } : x))), []);
  const movePlanItem = useCallback((id: string, direction: -1 | 1) => {
    setPlan((p) => {
      const i = p.findIndex((x) => x.id === id);
      const wave = p[i]?.wave;
      // Swap with the neighbour in the same wave, so reordering never changes the wave.
      let j = i + direction;
      while (j >= 0 && j < p.length && p[j].wave !== wave) j += direction;
      if (i < 0 || j < 0 || j >= p.length) return p;
      const next = [...p];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  }, []);
  const clearPlan = useCallback(() => setPlan([]), []);
  const planIds = useMemo(() => new Set(plan.map((p) => p.id)), [plan]);
  const inPlan = useCallback((id: string) => planIds.has(id), [planIds]);

  // ---- fabric target
  const [fabric, setFabric] = useState<FabricTarget>({ status: "disconnected" });
  const [fabricBusy, setFabricBusy] = useState<"authenticate" | "test" | null>(null);
  const [fabricError, setFabricError] = useState<string | null>(null);
  useEffect(() => {
    setFabricError(null);
    api.getFabricTarget().then(setFabric, () => setFabric({ status: "disconnected" }));
  }, [api]);
  const fabricCall = useCallback(async (kind: "authenticate" | "test", call: () => Promise<FabricTarget>) => {
    setFabricBusy(kind);
    setFabricError(null);
    try {
      const next = await call();
      setFabric(next);
      if (next.status === "failed") setFabricError(next.message ?? "The connection failed.");
    } catch (e) {
      setFabricError(messageOf(e));
    } finally {
      setFabricBusy(null);
    }
  }, []);
  const authenticateFabric = useCallback((c: FabricConfig) => fabricCall("authenticate", () => api.authenticateFabric(c)), [api, fabricCall]);
  const testFabric = useCallback((c: FabricConfig) => fabricCall("test", () => api.testFabric(c)), [api, fabricCall]);
  const disconnectFabric = useCallback(async () => { setFabric(await api.disconnectFabric()); setFabricError(null); }, [api]);

  // ---- execution (polls only while running)
  const [execution, setExecution] = useState<ExecutionRun>(EMPTY_RUN);
  const [executionError, setExecutionError] = useState<string | null>(null);
  useEffect(() => { api.getExecution().then(setExecution, () => setExecution(EMPTY_RUN)); }, [api]);
  useEffect(() => {
    if (execution.state !== "running") return;
    const id = setInterval(() => { api.getExecution().then(setExecution, () => undefined); }, 900);
    return () => clearInterval(id);
  }, [api, execution.state]);

  const startExecution = useCallback(async () => {
    setExecutionError(null);
    try {
      const ordered = [...plan].sort((a, b) => a.wave - b.wave);
      setExecution(await api.startExecution(ordered));
      return true;
    } catch (e) {
      setExecutionError(messageOf(e));
      return false;
    }
  }, [api, plan]);
  const controlExecution = useCallback(async (action: "pause" | "resume" | "retry") => {
    setExecutionError(null);
    try { setExecution(await api.controlExecution(action)); } catch (e) { setExecutionError(messageOf(e)); }
  }, [api]);

  // ---- validation
  const [validation, setValidation] = useState<ValidationRow[] | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [validationBusy, setValidationBusy] = useState(false);
  useEffect(() => { setValidation(null); setValidationError(null); }, [api]);
  const runValidation = useCallback(async () => {
    setValidationBusy(true);
    setValidationError(null);
    try { setValidation(await api.runValidation()); } catch (e) { setValidationError(messageOf(e)); } finally { setValidationBusy(false); }
  }, [api]);

  // ---- plan status
  const dependsOn = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const e of graph?.edges ?? []) m.set(e.source, [...(m.get(e.source) ?? []), e.target]);
    return m;
  }, [graph]);
  const planWave = useMemo(() => new Map(plan.map((p) => [p.id, p.wave])), [plan]);
  const runStatus = useMemo(() => new Map(execution.items.map((i) => [i.id, i.status])), [execution.items]);
  const planStatus = useCallback((id: string): PlanStatus => {
    const run = runStatus.get(id);
    if (run === "COMPLETED") return "COMPLETED";
    if (run === "FAILED") return "FAILED";
    if (run === "IN PROGRESS") return "IN PROGRESS";
    // Blocked: it needs something that is not in the plan, or that is planned later.
    const mine = planWave.get(id) ?? 1;
    const blocked = (dependsOn.get(id) ?? []).some((d) => !planWave.has(d) || (planWave.get(d) as number) > mine);
    if (blocked) return "BLOCKED";
    return isConnected && plan.length ? "READY" : "NOT STARTED";
  }, [runStatus, planWave, dependsOn, isConnected, plan.length]);

  const value: MigrationStateValue = {
    projects, project, addProject, selectProject: setProjectId,
    graph, graphError, graphLoading, reloadGraph: () => setGraphEpoch((n) => n + 1),
    plan, addToPlan, addAllToPlan, removeFromPlan, setPlanWave, movePlanItem, clearPlan, inPlan, planStatus,
    fabric, fabricBusy, fabricError, authenticateFabric, testFabric, disconnectFabric,
    execution, executionError, startExecution, controlExecution,
    validation, validationError, validationBusy, runValidation,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
