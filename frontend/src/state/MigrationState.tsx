import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ApiRequestError,
  type DependencyGraph,
  type ExecutionRun,
  type FabricConfig,
  type FabricTarget,
  type Capabilities,
  type ConnectionCredentials,
  type PlanAnalysis,
  type PlanItem,
  type PlanStatus,
  type RunOptions,
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
const OPTIONS_KEY = "ma.options";
const JOURNEY_KEY = "ma.journey";
/** Everything kept per project in this browser, by mode. Credentials are never among them. */
const PROJECT_KEYS = [PLAN_KEY, OPTIONS_KEY, JOURNEY_KEY];

/** The migration journey, in order. Each step unlocks once the one before it is confirmed. */
export const STEP_KEYS = ["discover", "assess", "waves", "plan", "migrate", "validate"] as const;
export type StepKey = (typeof STEP_KEYS)[number];

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

const DEFAULT_OPTIONS: RunOptions = { scope: "automated", stopOnFailure: false, stages: [], dataMode: "if_empty", dataRun: "run", collation: "match_synapse", syncOverlap: "1h", dataFilters: {} };

const EMPTY_RUN: ExecutionRun = { runId: "", state: "idle", total: 0, completed: 0, inProgress: 0, failed: 0, pending: 0, skipped: 0, deferred: 0, items: [], logs: [] };

interface MigrationStateValue {
  projects: Project[];
  project: Project;
  addProject: (name: string) => void;
  selectProject: (id: string) => void;
  renameProject: (id: string, name: string) => void;
  /** Removes the project and what this browser keeps for it (plan, options, confirmations). Never the last one. */
  deleteProject: (id: string) => void;

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
  /** False until the Fabric target's state has been read once. */
  fabricReady: boolean;
  fabricBusy: "authenticate" | "test" | null;
  fabricError: string | null;
  authenticateFabric: (c: FabricConfig) => Promise<void>;
  testFabric: (c: FabricConfig) => Promise<void>;
  disconnectFabric: () => Promise<void>;

  /** The planner's strategy, risks and checks for the current plan. Recomputed when the plan or target changes. */
  analysis: PlanAnalysis | null;
  analysisBusy: boolean;
  analysisError: string | null;
  /** `record` also adds the result to the planner-run history. */
  analyze: (record?: boolean) => Promise<void>;
  options: RunOptions;
  setOptions: (patch: Partial<RunOptions>) => void;
  /** The migration stages, their strategies and the linked services that need credentials. Null until loaded. */
  capabilities: Capabilities | null;
  /** Connection credentials typed on this page. Memory only. */
  credentials: ConnectionCredentials;
  setCredential: (name: string, patch: Record<string, string>) => void;
  clearCredentials: () => void;

  execution: ExecutionRun;
  /** False until the run's state has been read once, so an empty run is never mistaken for a lost one. */
  executionReady: boolean;
  executionError: string | null;
  /** Starts a run. `only` runs a single stage, leaving the others off for that run. */
  startExecution: (only?: string) => Promise<boolean>;
  controlExecution: (action: "pause" | "resume" | "retry") => Promise<void>;

  validation: ValidationRow[] | null;
  validationError: string | null;
  validationBusy: boolean;
  runValidation: () => Promise<void>;

  /** Steps the operator has reviewed and confirmed with Next, per project and data mode. */
  confirmed: StepKey[];
  confirmStep: (step: StepKey) => void;
  /** Forget the confirmations from ``step`` onwards (a step was redone). Without a step: all of them. */
  resetJourney: (from?: StepKey) => void;
  /**
   * Start ``step`` over: clear its results and those of every step after it,
   * and their confirmations. The backend is asked first, so a refusal (something
   * still running) changes nothing here. Throws the backend's message.
   */
  resetStep: (step: StepKey) => Promise<void>;
  /** Changes on every reset, so a step's panel can start over from a clean state. */
  resetNonce: number;
}

const Ctx = createContext<MigrationStateValue | null>(null);

export function useMigration(): MigrationStateValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useMigration must be used inside <MigrationStateProvider>");
  return v;
}

const messageOf = (e: unknown) => (e instanceof ApiRequestError || e instanceof Error ? e.message : "Something went wrong.");

export function MigrationStateProvider({ children }: { children: ReactNode }) {
  const { api, mode, discovery, isConnected, connection, resetDiscovery, refreshDiscovery } = useAppState();

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
  const renameProject = useCallback((id: string, name: string) => {
    if (!name.trim()) return;
    setProjects((ps) => ps.map((p) => (p.id === id ? { ...p, name: name.trim() } : p)));
  }, []);
  const deleteProject = useCallback((id: string) => {
    const rest = projects.filter((p) => p.id !== id);
    if (!rest.length) return;
    for (const m of ["real", "mock"]) for (const k of PROJECT_KEYS) {
      try { localStorage.removeItem(`${k}.${m}.${id}`); } catch { /* storage unavailable */ }
    }
    setProjects(rest);
    if (project.id === id) setProjectId(rest[0].id);
  }, [projects, project.id]);

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

  // ---- journey (which steps were confirmed), per mode and project like the plan
  const journeyKey = `${JOURNEY_KEY}.${mode}.${project.id}`;
  const [confirmed, setConfirmed] = useState<StepKey[]>(() => load<StepKey[]>(journeyKey, []));
  useEffect(() => { setConfirmed(load<StepKey[]>(journeyKey, [])); }, [journeyKey]);
  useEffect(() => save(journeyKey, confirmed), [journeyKey, confirmed]);
  const confirmStep = useCallback((step: StepKey) => setConfirmed((c) => (c.includes(step) ? c : [...c, step])), []);
  const resetJourney = useCallback((from?: StepKey) => {
    setConfirmed((c) => (from ? c.filter((k) => STEP_KEYS.indexOf(k) < STEP_KEYS.indexOf(from)) : []));
  }, []);

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
  const [fabricReady, setFabricReady] = useState(false);
  const [fabricBusy, setFabricBusy] = useState<"authenticate" | "test" | null>(null);
  const [fabricError, setFabricError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setFabricError(null);
    setFabricReady(false);
    api.getFabricTarget().then(
      (f) => { if (live) setFabric(f); },
      () => { if (live) setFabric({ status: "disconnected" }); },
    ).finally(() => { if (live) setFabricReady(true); });
    return () => { live = false; };
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
  // A browser sign-in finishes on its own time: poll until the backend settles.
  useEffect(() => {
    if (fabric.status !== "signing_in") return;
    const timer = setInterval(() => {
      api.getFabricTarget().then((next) => {
        setFabric(next);
        if (next.status === "failed") setFabricError(next.message ?? "The sign-in failed.");
      }, () => undefined);
    }, 1500);
    return () => clearInterval(timer);
  }, [api, fabric.status]);
  const authenticateFabric = useCallback((c: FabricConfig) => fabricCall("authenticate", () => api.authenticateFabric(c)), [api, fabricCall]);
  const testFabric = useCallback((c: FabricConfig) => fabricCall("test", () => api.testFabric(c)), [api, fabricCall]);
  const disconnectFabric = useCallback(async () => { setFabric(await api.disconnectFabric()); setFabricError(null); }, [api]);

  // ---- planner (read-only analysis of the plan)
  const [analysis, setAnalysis] = useState<PlanAnalysis | null>(null);
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  // Run options are kept per project like the plan, so a stage switched off stays off after a reload.
  const optionsKey = `${OPTIONS_KEY}.${mode}.${project.id}`;
  const stored = (key: string) => load<Partial<RunOptions>>(key, {});
  const [options, setOptionsState] = useState<RunOptions>(() => ({ ...DEFAULT_OPTIONS, ...stored(optionsKey) }));
  // Whether the stage list was ever chosen: an empty list then means "all off", not "not chosen yet".
  const stagesChosen = useRef(Array.isArray(stored(optionsKey).stages));
  useEffect(() => {
    const kept = stored(optionsKey);
    stagesChosen.current = Array.isArray(kept.stages);
    setOptionsState({ ...DEFAULT_OPTIONS, ...kept });
  }, [optionsKey]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => save(optionsKey, stagesChosen.current ? options : { ...options, stages: undefined }), [optionsKey, options]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [credentials, setCredentials] = useState<ConnectionCredentials>({});
  const setCredential = useCallback((name: string, patch: Record<string, string>) => setCredentials((c) => ({ ...c, [name]: { ...(c[name] ?? {}), ...patch } })), []);
  const clearCredentials = useCallback(() => setCredentials({}), []);
  // The stage list comes from the backend, so the page can never offer one it cannot run.
  useEffect(() => {
    let live = true;
    // Keep the stages on screen while they are read again: no flicker when discovery finishes.
    api.getCapabilities().then((c) => {
      if (!live) return;
      setCapabilities(c);
      const known = new Set(c.stages.map((s) => s.key));
      setOptionsState((o) => ({ ...o, stages: stagesChosen.current ? o.stages.filter((k) => known.has(k)) : c.stages.map((s) => s.key) }));
    }, () => { if (live) setCapabilities(null); });
    return () => { live = false; };
  }, [api, discovery.state]);
  const setOptions = useCallback((patch: Partial<RunOptions>) => {
    if (patch.stages) stagesChosen.current = true;
    setOptionsState((o) => ({ ...o, ...patch }));
  }, []);
  const analyze = useCallback(async (record = false) => {
    if (!plan.length) { setAnalysis(null); setAnalysisError(null); return; }
    setAnalysisBusy(true);
    setAnalysisError(null);
    try {
      setAnalysis(await api.analyzePlan([...plan].sort((a, b) => a.wave - b.wave), record, options, credentials));
    } catch (e) {
      setAnalysisError(messageOf(e));
    } finally {
      setAnalysisBusy(false);
    }
  }, [api, plan, options, credentials]);
  // Re-analyse (debounced) whenever the plan or the Fabric target changes, once discovery has results.
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  useEffect(() => {
    if (!discovered || !plan.length) { setAnalysis(null); return; }
    const t = setTimeout(() => { void analyze(false); }, 350);
    return () => clearTimeout(t);
  }, [discovered, plan, fabric.status, options.stages, analyze]);

  // ---- execution (polls only while running)
  const [execution, setExecution] = useState<ExecutionRun>(EMPTY_RUN);
  const [executionReady, setExecutionReady] = useState(false);
  const [executionError, setExecutionError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setExecutionReady(false);
    api.getExecution().then((r) => { if (live) setExecution(r); }, () => { if (live) setExecution(EMPTY_RUN); })
      .finally(() => { if (live) setExecutionReady(true); });
    return () => { live = false; };
  }, [api]);
  // Signing out, reconnecting or a new discovery can change what the backend holds: read the run again.
  useEffect(() => {
    if (!executionReady) return;
    api.getExecution().then(setExecution, () => undefined);
  }, [connection.status, discovery.state]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (execution.state !== "running") return;
    const id = setInterval(() => { api.getExecution().then(setExecution, () => undefined); }, 900);
    return () => clearInterval(id);
  }, [api, execution.state]);

  const startExecution = useCallback(async (only?: string) => {
    setExecutionError(null);
    try {
      const ordered = [...plan].sort((a, b) => a.wave - b.wave);
      const run = only ? { ...options, stages: [only] } : options;
      setExecution(await api.startExecution(ordered, run, credentials));
      return true;
    } catch (e) {
      setExecutionError(messageOf(e));
      return false;
    }
  }, [api, plan, options, credentials]);
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
    try { setValidation(await api.runValidation([...plan].sort((a, b) => a.wave - b.wave), { dataFilters: options.dataFilters })); } catch (e) { setValidationError(messageOf(e)); } finally { setValidationBusy(false); }
  }, [api, plan, options.dataFilters]);

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
    if (run === "COMPLETED" || run === "SKIPPED") return "COMPLETED"; // skipped = already in Fabric
    if (run === "FAILED") return "FAILED";
    if (run === "IN PROGRESS") return "IN PROGRESS";
    // Blocked: it needs something that is not in the plan, or that is planned later.
    const mine = planWave.get(id) ?? 1;
    const blocked = (dependsOn.get(id) ?? []).some((d) => !planWave.has(d) || (planWave.get(d) as number) > mine);
    if (blocked) return "BLOCKED";
    return isConnected && plan.length ? "READY" : "NOT STARTED";
  }, [runStatus, planWave, dependsOn, isConnected, plan.length]);

  // ---- start a step over
  const [resetNonce, setResetNonce] = useState(0);
  const resetStep = useCallback(async (step: StepKey) => {
    const from = STEP_KEYS.indexOf(step);
    const covers = (k: StepKey) => from <= STEP_KEYS.indexOf(k);
    // The backend first: if it refuses (a run or a discovery still working), nothing here changes.
    if (covers("migrate") && execution.state !== "idle") {
      try { setExecution(await api.controlExecution("reset")); } catch (e) { throw new Error(messageOf(e)); }
    }
    if (step === "discover" && discovery.state !== "idle") {
      try { await resetDiscovery(); } catch (e) { throw new Error(messageOf(e)); }
    }
    if (step === "assess") await refreshDiscovery();
    if (step === "waves") setGraphEpoch((n) => n + 1);
    if (covers("plan")) {
      setPlan([]);
      stagesChosen.current = false;
      setOptionsState({ ...DEFAULT_OPTIONS, stages: capabilities?.stages.map((s) => s.key) ?? [] });
      setCredentials({});
      setAnalysis(null);
      setAnalysisError(null);
    }
    if (covers("migrate")) setExecutionError(null);
    setValidation(null);
    setValidationError(null);
    resetJourney(step);
    setResetNonce((n) => n + 1);
  }, [api, execution.state, discovery.state, resetDiscovery, refreshDiscovery, capabilities, resetJourney]);

  const value: MigrationStateValue = {
    projects, project, addProject, selectProject: setProjectId, renameProject, deleteProject,
    graph, graphError, graphLoading, reloadGraph: () => setGraphEpoch((n) => n + 1),
    plan, addToPlan, addAllToPlan, removeFromPlan, setPlanWave, movePlanItem, clearPlan, inPlan, planStatus,
    fabric, fabricReady, fabricBusy, fabricError, authenticateFabric, testFabric, disconnectFabric,
    analysis, analysisBusy, analysisError, analyze, options, setOptions, capabilities, credentials, setCredential, clearCredentials,
    execution, executionReady, executionError, startExecution, controlExecution,
    validation, validationError, validationBusy, runValidation,
    confirmed, confirmStep, resetJourney, resetStep, resetNonce,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
