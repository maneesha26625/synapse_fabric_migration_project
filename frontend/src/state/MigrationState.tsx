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
  type FabricGitState,
  type FabricTarget,
  type Capabilities,
  type ConnectionCredentials,
  type MigrationApi,
  type Project,
  type ProjectListing,
  type PlanAnalysis,
  type PlanItem,
  type PlanStatus,
  type RunOptions,
  type ValidationRow,
  type ValidationRuntimeOptions,
} from "../types";
import { useAppState } from "./AppState";

// ---- projects ---------------------------------------------------------------------
//
// A project is one migration engagement. The backend owns them: each keeps its own
// discovery, Fabric target, runs, plan versions and audit trail, and one is active at
// a time. This browser keeps, per project id, only what it always kept: the plan being
// edited, the stage options and the steps confirmed. Against an older backend, which
// has no projects, they are labels kept in this browser, as before.

export type { Project };
const PROJECTS_KEY = "ma.projects";
/** Set once this browser's own projects have been handed to the backend. */
const ADOPTED_KEY = "ma.projects.adopted";
const PLAN_KEY = "ma.plan";
const OPTIONS_KEY = "ma.options";
const JOURNEY_KEY = "ma.journey";
/** Everything kept per project in this browser, by mode. Credentials are never among them. */
const PROJECT_KEYS = [PLAN_KEY, OPTIONS_KEY, JOURNEY_KEY];

/** The migration journey, in order. Each step unlocks once the one before it is confirmed. Consult sits after Assess:
 *  the brief its answers make is what Plan starts from and what a run is checked against. */
export const STEP_KEYS = ["discover", "assess", "consult", "waves", "plan", "migrate", "validate"] as const;
export type StepKey = (typeof STEP_KEYS)[number];

/** The confirmed steps of a journey saved before Consult was a step: one that confirmed a later step had nothing to
 *  confirm there, so Consult counts as confirmed (its work is the brief, which Migrate checks for itself). */
function loadJourney(key: string): StepKey[] {
  const saved = load<StepKey[]>(key, []).filter((k): k is StepKey => (STEP_KEYS as readonly string[]).includes(k));
  const after = STEP_KEYS.slice(STEP_KEYS.indexOf("consult") + 1);
  return saved.includes("consult") || !saved.some((k) => (after as readonly string[]).includes(k)) ? saved : [...saved, "consult"];
}

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

const DEFAULT_OPTIONS: RunOptions = { scope: "automated", stopOnFailure: false, stages: [], dataMode: "if_empty", dataRun: "run", collation: "match_synapse", syncOverlap: "1h", existing: "skip", dataFilters: {}, gitCommit: "auto" };

const EMPTY_RUN: ExecutionRun = { runId: "", state: "idle", total: 0, completed: 0, inProgress: 0, failed: 0, pending: 0, skipped: 0, deferred: 0, items: [], logs: [] };

interface MigrationStateValue {
  projects: Project[];
  project: Project;
  /** Whether the backend owns the projects (else they are this browser's labels, against an older backend). */
  backendProjects: boolean;
  /** Changes whenever the backend switches project, so what is read from it is read again. */
  projectEpoch: number;
  /** Each refuses (throws with the backend's message) when it cannot be done now, such as a switch while a run works. */
  addProject: (name: string) => Promise<void>;
  selectProject: (id: string) => Promise<void>;
  renameProject: (id: string, name: string) => Promise<void>;
  /** Removes the project (the backend archives its folder) and what this browser keeps for it. Never the last one. */
  deleteProject: (id: string) => Promise<void>;

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

  /** The target workspace's Fabric Git branch and what waits to be committed. Null until read. */
  fabricGit: FabricGitState | null;
  fabricGitError: string | null;
  refreshFabricGit: () => Promise<void>;
  /** Commit the migrated items to the workspace's Fabric Git branch now. */
  commitFabricGit: () => Promise<void>;

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
  /** The backend's code for that error (``brief_required``, ``deviation_unexplained``...), so the page can offer the way on. */
  executionErrorCode: string | null;
  /** Starts a run. `only` runs a single stage, leaving the others off for that run. `deviationReason`: why it differs
   *  from the approved brief, when it does and no reason was given in Plan. */
  startExecution: (only?: string, deviationReason?: string) => Promise<boolean>;
  /** `comment`: what a person says signing off the next wave (Resume on a run waiting for a wave's sign-off). */
  controlExecution: (action: "pause" | "resume" | "retry", comment?: string) => Promise<void>;
  /** A person signs the waiting run off, and it starts; or declines it. */
  approveExecution: (decision: "approve" | "reject", comment?: string) => Promise<boolean>;

  validation: ValidationRow[] | null;
  validationError: string | null;
  validationBusy: boolean;
  /** What the server is doing while validation runs (runs in Fabric can take minutes). */
  validationProgress: string | null;
  /** Whether, and how, Validate also checks that objects run in Fabric. */
  validationRuntime: ValidationRuntimeOptions;
  setValidationRuntime: (next: ValidationRuntimeOptions) => void;
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

/**
 * Hand this browser's projects to the backend, once: each keeps its id, so the plan and options kept here under
 * it carry on. The project this browser was on carries on the discovery the backend saved before it had projects
 * (that one went to its default project), and becomes the active one.
 */
async function adoptBrowserProjects(api: MigrationApi, listing: ProjectListing): Promise<ProjectListing> {
  const local = load<Project[]>(PROJECTS_KEY, []);
  const current = load<string>(`${PROJECTS_KEY}.current`, DEFAULT_PROJECT.id);
  const known = new Set(listing.projects.map((p) => p.id));
  for (const p of local) {
    if (known.has(p.id) || !p.name?.trim()) continue;
    try {
      const carry = p.id === current && known.has(DEFAULT_PROJECT.id) ? { copyFrom: DEFAULT_PROJECT.id } : {};
      listing = await api.createProject(p.name, { id: p.id, ...carry });
      known.add(p.id);
    } catch { /* one that cannot be made (an id the backend refuses) stays this browser's label only */ }
  }
  if (known.has(current) && listing.active !== current) {
    try { listing = await api.activateProject(current); } catch { /* the backend's active project stands */ }
  }
  save(ADOPTED_KEY, true);
  return listing;
}

export function MigrationStateProvider({ children }: { children: ReactNode }) {
  const { api, mode, discovery, isConnected, connection, resetDiscovery, refreshDiscovery, reload } = useAppState();

  // ---- projects: the backend's; against an older backend, this browser's labels as before
  const [projects, setProjects] = useState<Project[]>(() => load<Project[]>(PROJECTS_KEY, [DEFAULT_PROJECT]));
  const [projectId, setProjectId] = useState<string>(() => load<string>(`${PROJECTS_KEY}.current`, DEFAULT_PROJECT.id));
  const [backendProjects, setBackendProjects] = useState(false);
  const [projectsVersion, setProjectsVersion] = useState<number | undefined>(undefined);
  const [projectEpoch, setProjectEpoch] = useState(0);
  const project = projects.find((p) => p.id === projectId) ?? projects[0] ?? DEFAULT_PROJECT;
  // This browser's own list is kept only while it is the list: never overwritten with the backend's, or the demo's.
  useEffect(() => { if (!backendProjects) save(PROJECTS_KEY, projects); }, [projects, backendProjects]);
  useEffect(() => { if (!backendProjects) save(`${PROJECTS_KEY}.current`, projectId); }, [projectId, backendProjects]);
  const applyListing = useCallback((listing: ProjectListing) => {
    setProjects(listing.projects.length ? listing.projects : [DEFAULT_PROJECT]);
    setProjectId(listing.active);
    setProjectsVersion(listing.version);
  }, []);
  // The latest reload, for the effect below without making it run again on every render.
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        let listing = await api.listProjects();
        const before = listing.active;
        if (api.mode === "real" && !load<boolean>(ADOPTED_KEY, false)) listing = await adoptBrowserProjects(api, listing);
        if (!live) return;
        setBackendProjects(true);
        applyListing(listing);
        if (listing.active !== before) {
          // Handing this browser's projects over switched the backend to the one it was on: read it all again.
          reloadRef.current();
          setProjectEpoch((n) => n + 1);
        }
      } catch {
        // An older backend (no /api/projects), or none answering yet: projects stay this browser's labels.
        if (live) {
          setBackendProjects(false);
          setProjects(load<Project[]>(PROJECTS_KEY, [DEFAULT_PROJECT]));
          setProjectId(load<string>(`${PROJECTS_KEY}.current`, DEFAULT_PROJECT.id));
        }
      }
    })();
    return () => { live = false; };
  }, [api, applyListing]);
  // The backend now holds another project: read everything it holds again.
  const switched = useCallback((listing: ProjectListing) => {
    applyListing(listing);
    reload();
    setProjectEpoch((n) => n + 1);
  }, [applyListing, reload]);
  // A change refused because the list moved on: read the list again, so the next try uses it.
  const refused = useCallback(async (e: unknown) => {
    if (e instanceof ApiRequestError && e.code === "stale") {
      try { applyListing(await api.listProjects()); } catch { /* the error below says enough */ }
    }
    throw e instanceof Error ? e : new Error(messageOf(e));
  }, [api, applyListing]);
  const addProject = useCallback(async (name: string) => {
    if (!backendProjects) {
      const created: Project = { id: `p${Date.now().toString(36)}`, name: name.trim(), createdAt: new Date().toISOString() };
      setProjects((ps) => [...ps, created]);
      setProjectId(created.id);
      return;
    }
    try { switched(await api.createProject(name.trim(), { version: projectsVersion, activate: true })); } catch (e) { await refused(e); }
  }, [api, backendProjects, projectsVersion, switched, refused]);
  const selectProject = useCallback(async (id: string) => {
    if (!backendProjects) { setProjectId(id); return; }
    if (id === projectId) return;
    try { switched(await api.activateProject(id, projectsVersion)); } catch (e) { await refused(e); }
  }, [api, backendProjects, projectId, projectsVersion, switched, refused]);
  const renameProject = useCallback(async (id: string, name: string) => {
    if (!name.trim()) return;
    if (!backendProjects) {
      setProjects((ps) => ps.map((p) => (p.id === id ? { ...p, name: name.trim() } : p)));
      return;
    }
    try { applyListing(await api.renameProject(id, name.trim(), projectsVersion)); } catch (e) { await refused(e); }
  }, [api, backendProjects, projectsVersion, applyListing, refused]);
  const deleteProject = useCallback(async (id: string) => {
    const rest = projects.filter((p) => p.id !== id);
    if (!rest.length) return;
    if (backendProjects) {
      // The backend refuses to delete the active project: switch away first, as the menu does.
      try {
        if (id === projectId) switched(await api.activateProject(rest[0].id, projectsVersion));
        applyListing(await api.deleteProject(id));
      } catch (e) { await refused(e); }
    }
    for (const m of ["real", "mock"]) for (const k of PROJECT_KEYS) {
      try { localStorage.removeItem(`${k}.${m}.${id}`); } catch { /* storage unavailable */ }
    }
    if (!backendProjects) {
      setProjects(rest);
      if (project.id === id) setProjectId(rest[0].id);
    }
  }, [api, projects, project.id, projectId, projectsVersion, backendProjects, switched, applyListing, refused]);

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
  const [confirmed, setConfirmed] = useState<StepKey[]>(() => loadJourney(journeyKey));
  useEffect(() => { setConfirmed(loadJourney(journeyKey)); }, [journeyKey]);
  useEffect(() => save(journeyKey, confirmed), [journeyKey, confirmed]);
  const confirmStep = useCallback((step: StepKey) => {
    setConfirmed((c) => (c.includes(step) ? c : [...c, step]));
    // The audit trail records who confirmed which step; a backend without one is simply not told.
    api.confirmStep(step).catch(() => undefined);
  }, [api]);
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
  }, [api, projectEpoch]);
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

  // ---- Fabric Git (read whenever the target workspace changes; polled while a commit runs)
  const [fabricGit, setFabricGit] = useState<FabricGitState | null>(null);
  const [fabricGitError, setFabricGitError] = useState<string | null>(null);
  const refreshFabricGit = useCallback(async () => {
    try {
      setFabricGit(await api.getFabricGit());
      setFabricGitError(null);
    } catch (e) {
      setFabricGitError(messageOf(e));
    }
  }, [api]);
  useEffect(() => {
    if (fabric.status !== "connected") { setFabricGit(null); return; }
    void refreshFabricGit();
  }, [fabric.status, fabric.workspaceId, refreshFabricGit]);
  const commitFabricGit = useCallback(async () => {
    setFabricGitError(null);
    try { setFabricGit(await api.commitFabricGit()); } catch (e) { setFabricGitError(messageOf(e)); }
  }, [api]);
  useEffect(() => {
    if (!fabricGit?.committing) return;
    const id = setInterval(() => { void refreshFabricGit(); }, 1000);
    return () => clearInterval(id);
  }, [fabricGit?.committing, refreshFabricGit]);

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
  }, [api, discovery.state, projectEpoch]);
  const setOptions = useCallback((patch: Partial<RunOptions>) => {
    if (patch.stages) stagesChosen.current = true;
    setOptionsState((o) => ({ ...o, ...patch }));
  }, []);
  // Planning only needs to know which linked services have credentials, never what they are. So only their names are
  // sent, and a re-score happens when that list changes, not on every key typed. The values travel once, with the run.
  const credentialKey = useMemo(() => JSON.stringify(Object.keys(credentials)
    .filter((name) => Object.values(credentials[name] ?? {}).some((v) => typeof v === "string" && v.trim() !== ""))
    .sort()), [credentials]);
  const analyze = useCallback(async (record = false) => {
    if (!plan.length) { setAnalysis(null); setAnalysisError(null); return; }
    setAnalysisBusy(true);
    setAnalysisError(null);
    try {
      setAnalysis(await api.analyzePlan([...plan].sort((a, b) => a.wave - b.wave), record, options, JSON.parse(credentialKey) as string[]));
    } catch (e) {
      setAnalysisError(messageOf(e));
    } finally {
      setAnalysisBusy(false);
    }
  }, [api, plan, options, credentialKey]);
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
  const [executionError, setExecutionErrorText] = useState<string | null>(null);
  const [executionErrorCode, setExecutionErrorCode] = useState<string | null>(null);
  const setExecutionError = useCallback((e: unknown) => {
    setExecutionErrorText(e === null ? null : messageOf(e));
    setExecutionErrorCode(e instanceof ApiRequestError ? e.code : null);
  }, []);
  useEffect(() => {
    let live = true;
    setExecutionReady(false);
    api.getExecution().then((r) => { if (live) setExecution(r); }, () => { if (live) setExecution(EMPTY_RUN); })
      .finally(() => { if (live) setExecutionReady(true); });
    return () => { live = false; };
  }, [api, projectEpoch]);
  // Signing out, reconnecting or a new discovery can change what the backend holds: read the run again.
  useEffect(() => {
    if (!executionReady) return;
    api.getExecution().then(setExecution, () => undefined);
  }, [connection.status, discovery.state]); // eslint-disable-line react-hooks/exhaustive-deps
  // Keep reading while the run works, and after it completes while its commit to Fabric Git finishes.
  const committingAfterRun = execution.git?.state === "committing";
  useEffect(() => {
    if (execution.state !== "running" && !committingAfterRun) return;
    const id = setInterval(() => { api.getExecution().then(setExecution, () => undefined); }, 900);
    return () => clearInterval(id);
  }, [api, execution.state, committingAfterRun]);
  // Once the run's commit settles, the branch has moved: read the Git state again.
  const runGitState = execution.git?.state;
  useEffect(() => {
    if (runGitState && runGitState !== "committing" && fabric.status === "connected") void refreshFabricGit();
  }, [runGitState]); // eslint-disable-line react-hooks/exhaustive-deps
  // A commit made by hand also lands on the run's record: read the run when it finishes.
  const gitCommitting = !!fabricGit?.committing;
  const wasCommitting = useRef(false);
  useEffect(() => {
    if (wasCommitting.current && !gitCommitting) api.getExecution().then(setExecution, () => undefined);
    wasCommitting.current = gitCommitting;
  }, [api, gitCommitting]);

  const startExecution = useCallback(async (only?: string, deviationReason?: string) => {
    setExecutionError(null);
    try {
      const ordered = [...plan].sort((a, b) => a.wave - b.wave);
      const run = only ? { ...options, stages: [only] } : options;
      setExecution(await api.startExecution(ordered, run, credentials, deviationReason));
      return true;
    } catch (e) {
      setExecutionError(e);
      return false;
    }
  }, [api, plan, options, credentials, setExecutionError]);
  // The credentials a run asks for again (a restart lost them: they are never kept), from what was typed on this page.
  const neededCredentials = useCallback((): ConnectionCredentials => Object.fromEntries(
    (execution.needsInput ?? []).filter((name) => credentials[name]).map((name) => [name, credentials[name]])), [execution.needsInput, credentials]);
  const controlExecution = useCallback(async (action: "pause" | "resume" | "retry", comment?: string) => {
    setExecutionError(null);
    try {
      setExecution(await api.controlExecution(action, { version: execution.version, credentials: neededCredentials(), ...(comment ? { comment } : {}) }));
    } catch (e) {
      setExecutionError(e);
      // Refused because the run moved on since it was read, or for input it needs: show it as it is now.
      api.getExecution().then(setExecution, () => undefined);
    }
  }, [api, execution.version, neededCredentials, setExecutionError]);
  const approveExecution = useCallback(async (decision: "approve" | "reject", comment?: string) => {
    setExecutionError(null);
    try {
      setExecution(await api.approveExecution(decision, comment, { subjectHash: execution.approval?.subjectHash, credentials: neededCredentials() }));
      return true;
    } catch (e) {
      setExecutionError(e);
      api.getExecution().then(setExecution, () => undefined);
      return false;
    }
  }, [api, execution.approval?.subjectHash, neededCredentials, setExecutionError]);

  // ---- validation
  const [validation, setValidation] = useState<ValidationRow[] | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [validationBusy, setValidationBusy] = useState(false);
  const [validationProgress, setValidationProgress] = useState<string | null>(null);
  // Read-only run checks are on by default; real runs in Fabric only when the operator ticks them.
  const [validationRuntime, setValidationRuntime] = useState<ValidationRuntimeOptions>({ probe: true, run: [], timeoutMinutes: 30 });
  // The project's last validation comes back after a reload; one still running on the server is followed to its end.
  useEffect(() => {
    let live = true;
    setValidation(null);
    setValidationError(null);
    (async () => {
      try {
        let state = await api.getValidation();
        if (state.state === "running") {
          setValidationBusy(true);
          while (live && state.state === "running") {
            await new Promise((r) => setTimeout(r, 1500));
            state = await api.getValidation();
          }
          if (live) setValidationBusy(false);
        }
        if (!live) return;
        if (state.state === "completed" && state.rows) setValidation(state.rows);
        else if (state.state === "failed" && state.error?.code !== "interrupted") setValidationError(state.error?.message ?? null);
      } catch { if (live) setValidationBusy(false); /* an older backend, or none: nothing to show */ }
    })();
    return () => { live = false; };
  }, [api, projectEpoch]);
  const runValidation = useCallback(async () => {
    setValidationBusy(true);
    setValidationError(null);
    setValidationProgress(null);
    try {
      setValidation(await api.runValidation([...plan].sort((a, b) => a.wave - b.wave), { dataFilters: options.dataFilters }, validationRuntime, setValidationProgress));
    } catch (e) { setValidationError(messageOf(e)); } finally { setValidationBusy(false); setValidationProgress(null); }
  }, [api, plan, options.dataFilters, validationRuntime]);

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
    if (run === "BLOCKED") return "BLOCKED"; // something it needs failed in the run
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
  }, [api, execution.state, discovery.state, resetDiscovery, refreshDiscovery, capabilities, resetJourney, setExecutionError]);

  const value: MigrationStateValue = {
    projects, project, backendProjects, projectEpoch, addProject, selectProject, renameProject, deleteProject,
    graph, graphError, graphLoading, reloadGraph: () => setGraphEpoch((n) => n + 1),
    plan, addToPlan, addAllToPlan, removeFromPlan, setPlanWave, movePlanItem, clearPlan, inPlan, planStatus,
    fabric, fabricReady, fabricBusy, fabricError, authenticateFabric, testFabric, disconnectFabric,
    fabricGit, fabricGitError, refreshFabricGit, commitFabricGit,
    analysis, analysisBusy, analysisError, analyze, options, setOptions, capabilities, credentials, setCredential, clearCredentials,
    execution, executionReady, executionError, executionErrorCode, startExecution, controlExecution, approveExecution,
    validation, validationError, validationBusy, validationProgress, validationRuntime, setValidationRuntime, runValidation,
    confirmed, confirmStep, resetJourney, resetStep, resetNonce,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
