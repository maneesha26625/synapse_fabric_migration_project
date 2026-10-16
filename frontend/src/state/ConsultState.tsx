import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import {
  ApiRequestError,
  type BriefView,
  type ConsultCatalog,
  type ConsultExplanation,
  type ConsultPolicy,
  type ConsultState,
  type MigrationContextView,
} from "../types";
import { useAppState } from "./AppState";
import { useMigration } from "./MigrationState";

// ---- the consultation ------------------------------------------------------------------------------
//
// The questions a consultant asks before a migration, and the migration brief their answers make. The backend owns
// all of it: every answer, every brief version and who approved it. This page holds what it last read, and one thing
// of its own per project: the decisions (the approved version's content hash) it last pre-set Plan and Validate from,
// so the pre-set happens once for each set of decisions, and a person's later changes are never overwritten by it, not
// even by a new version approved only because the estate moved. The policy read is tagged with the project it was
// read for, so a switch of project never pre-sets the new project from the old one's brief.

const PRESET_KEY = "ma.briefPreset";

interface ConsultValue {
  catalog: ConsultCatalog | null;
  /** Where every question stands. Null until read, and while there is no discovery to consult on. */
  consult: ConsultState | null;
  brief: BriefView | null;
  policy: ConsultPolicy | null;
  /** MIGRATION_CONTEXT.md as every LLM draft is sent it now (null until read). */
  migrationContext: MigrationContextView | null;
  /** Why the consultation could not be read (no discovery yet, or a backend without one). */
  loadError: string | null;
  /** The last action's refusal, in the backend's words. */
  actionError: string | null;
  clearActionError: () => void;
  /** The action in flight, by name, so its button can say so. */
  busy: string | null;
  refresh: () => Promise<void>;
  answer: (question: string, value: unknown, note?: string) => Promise<boolean>;
  skip: (question: string) => Promise<boolean>;
  withdraw: (question: string) => Promise<boolean>;
  accept: (section?: string, questions?: string[]) => Promise<boolean>;
  resolve: (conflict: string, resolution: "take" | "keep", reason?: string) => Promise<boolean>;
  submit: (note?: string) => Promise<boolean>;
  decide: (version: number, decision: "approve" | "reject", comment?: string) => Promise<boolean>;
  propose: (text: string) => Promise<ConsultState | null>;
  decideProposals: (ids: string[], decision: "confirm" | "reject") => Promise<boolean>;
  explain: (question: string) => Promise<ConsultExplanation | null>;
  importQuestionnaire: (answers: Record<string, unknown>) => Promise<ConsultState | null>;
  /** The approved brief in force (its version), whether it still fits the estate, and the consultation mode. */
  approved: number | null;
  stale: boolean;
  required: boolean;
  /** The objects the brief leaves out of the plan, with why. */
  excluded: Record<string, string>;
}

const Ctx = createContext<ConsultValue | null>(null);

export function useConsult(): ConsultValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useConsult must be used inside <ConsultProvider>");
  return v;
}

const messageOf = (e: unknown) => (e instanceof ApiRequestError || e instanceof Error ? e.message : "Something went wrong.");

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}
function save(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable: pre-set again next time */ }
}

export function ConsultProvider({ children }: { children: ReactNode }) {
  const { api, mode, discovery } = useAppState();
  const { project, projectEpoch, fabric, setOptions, setValidationRuntime, validationRuntime, capabilities } = useMigration();
  const [catalog, setCatalog] = useState<ConsultCatalog | null>(null);
  const [consult, setConsult] = useState<ConsultState | null>(null);
  const [brief, setBrief] = useState<BriefView | null>(null);
  const [policy, setPolicy] = useState<ConsultPolicy | null>(null);
  /** The project the policy was read for: the pre-set below uses a policy only for the project it belongs to. */
  const [policyProject, setPolicyProject] = useState<string | null>(null);
  const [migrationContext, setMigrationContext] = useState<MigrationContextView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";

  useEffect(() => {
    let live = true;
    api.getConsultCatalog().then((c) => { if (live) setCatalog(c); }, () => { if (live) setCatalog(null); });
    return () => { live = false; };
  }, [api]);

  const projectId = project.id;
  const readPolicy = useCallback(async () => {
    const asked = projectId;
    const [inForce, context] = await Promise.all([api.getConsultPolicy(), api.getMigrationContext().catch(() => null)]);
    setPolicy(inForce);
    setPolicyProject(asked);
    setMigrationContext(context);
  }, [api, projectId]);
  const refresh = useCallback(async () => {
    try {
      const [state, view] = await Promise.all([api.getConsult(), api.getBrief()]);
      await readPolicy();
      setConsult(state);
      setBrief(view);
      setLoadError(null);
    } catch (e) {
      setConsult(null);
      setBrief(null);
      setPolicy(null);
      setPolicyProject(null);
      setMigrationContext(null);
      setLoadError(messageOf(e));
    }
  }, [api, readPolicy]);
  // Read again when the project, the discovery or the target changes: each can change what applies.
  useEffect(() => {
    if (!discovered) { setConsult(null); setBrief(null); setPolicy(null); setPolicyProject(null); setMigrationContext(null); setLoadError(null); return; }
    void refresh();
  }, [refresh, discovered, discovery.finishedAt, projectEpoch, fabric.status, fabric.workspaceId]);

  const refreshRef = useRef<() => Promise<void>>();
  refreshRef.current = refresh;
  /** Run an action: its result replaces what the page shows, and its refusal is said. */
  const act = useCallback(async <T,>(name: string, call: () => Promise<T>, after?: (result: T) => void | Promise<void>): Promise<T | null> => {
    setBusy(name);
    setActionError(null);
    try {
      const result = await call();
      await after?.(result);
      return result;
    } catch (e) {
      setActionError(messageOf(e));
      // A refusal can mean the page read the consultation before it moved (another page answered, the estate
      // changed): read it again, so what is shown is what the refusal was about.
      void refreshRef.current?.();
      return null;
    } finally {
      setBusy(null);
    }
  }, []);
  const withState = useCallback(async (state: ConsultState) => {
    setConsult(state);
    // The brief's draft follows every answer; the policy only moves with an approval.
    try { setBrief(await api.getBrief()); } catch { /* the questions are what matter here */ }
  }, [api]);
  const withBrief = useCallback(async (view: BriefView) => {
    setBrief(view);
    try {
      const state = await api.getConsult();
      await readPolicy();
      setConsult(state);
    } catch { /* shown on the next read */ }
  }, [api, readPolicy]);

  const answer = useCallback(async (question: string, value: unknown, note?: string) =>
    (await act(`answer:${question}`, () => api.answerQuestion(question, value, { note, seq: consult?.seq }), withState)) !== null, [act, api, consult?.seq, withState]);
  const skip = useCallback(async (question: string) => (await act(`skip:${question}`, () => api.skipQuestion(question), withState)) !== null, [act, api, withState]);
  const withdraw = useCallback(async (question: string) => (await act(`withdraw:${question}`, () => api.withdrawAnswer(question), withState)) !== null, [act, api, withState]);
  const accept = useCallback(async (section?: string, questions?: string[]) =>
    (await act(questions?.length === 1 ? `accept:${questions[0]}` : `accept:${section ?? ""}`, () => api.acceptRecommendations(section, questions), withState)) !== null,
  [act, api, withState]);
  const resolve = useCallback(async (conflict: string, resolution: "take" | "keep", reason?: string) =>
    (await act(`resolve:${conflict}`, () => api.resolveConflict(conflict, resolution, reason), withState)) !== null, [act, api, withState]);
  const submit = useCallback(async (note?: string) => (await act("submit", () => api.submitBrief(note), withBrief)) !== null, [act, api, withBrief]);
  const decide = useCallback(async (version: number, decision: "approve" | "reject", comment?: string) =>
    (await act(`decide:${decision}`, () => api.decideBrief(version, decision, comment, brief?.versions.find((v) => v.version === version)?.hash), withBrief)) !== null,
  [act, api, brief?.versions, withBrief]);
  const propose = useCallback((text: string) => act("propose", () => api.proposeAnswers(text), withState), [act, api, withState]);
  const decideProposals = useCallback(async (ids: string[], decision: "confirm" | "reject") =>
    (await act(`proposals:${decision}`, () => api.decideProposals(ids, decision), withState)) !== null, [act, api, withState]);
  const explain = useCallback((question: string) => act(`explain:${question}`, () => api.explainQuestion(question)), [act, api]);
  const importQuestionnaire = useCallback((answers: Record<string, unknown>) => act("import", () => api.importQuestionnaire(answers), withState), [act, api, withState]);

  // ---- Plan and Validate start from the approved brief: once for each set of decisions, never over a person's later changes
  const presetKey = `${PRESET_KEY}.${mode}.${project.id}`;
  const runtime = useRef(validationRuntime);
  runtime.current = validationRuntime;
  const inForce = policy?.inForce?.hash ?? null;
  useEffect(() => {
    if (!policy?.inForce || !capabilities || policyProject !== project.id) return;
    if (load<string | null>(presetKey, null) === policy.inForce.hash) return;
    const known = new Set(capabilities.stages.map((s) => s.key));
    const run = policy.runOptions ?? {};
    const settings = policy.inForce.settings ?? {};
    // A brief that decides the history or the sync decides every table's filter: none means none, so the page's
    // own (from before this brief) are cleared, never kept beside the brief's.
    const decidesData = "data.historyMode" in settings || "data.sync" in settings;
    setOptions({
      ...run,
      ...(run.stages ? { stages: run.stages.filter((k) => known.has(k)) } : {}),
      ...(decidesData ? { dataFilters: policy.dataFilters ?? {} } : {}),
      // "all": every location a connection can reach is routed when the plan first meets storage (the panel's own default).
      ...(policy.storageRoutes === "none" ? { storageRoutes: [] } : policy.storageRoutes === "all" ? { storageRoutes: undefined } : {}),
    });
    if (policy.validate) setValidationRuntime({ ...runtime.current, probe: policy.validate.probe, run: [...policy.validate.run] });
    save(presetKey, policy.inForce.hash);
  }, [inForce, presetKey, capabilities, policyProject]); // eslint-disable-line react-hooks/exhaustive-deps

  const value: ConsultValue = {
    catalog, consult, brief, policy: policyProject === project.id ? policy : null, migrationContext, loadError, actionError,
    clearActionError: () => setActionError(null), busy, refresh,
    answer, skip, withdraw, accept, resolve, submit, decide, propose, decideProposals, explain, importQuestionnaire,
    approved: consult?.brief.approved ?? policy?.inForce?.version ?? null,
    stale: !!(consult?.brief.stale ?? policy?.inForce?.stale),
    required: (consult?.mode ?? policy?.mode ?? "required") === "required",
    excluded: policy?.excluded ?? {},
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
