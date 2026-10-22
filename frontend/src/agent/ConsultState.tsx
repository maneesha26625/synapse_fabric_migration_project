import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";
import { counts as countBrief, conflicts as briefConflicts, effectiveAnswers, fingerprint, resolve, settingsFrom, type AgentSettings, type BriefCounts, type Resolved } from "./brief";
import { QUESTION_BY_ID, type AnswerValue, type Answers } from "./catalog";
import { readFacts, type Facts } from "./facts";

/** One line of the consultation's conversation: what was decided, said or asked. */
export interface ConsultNote { at: string; kind: "note" | "you" | "system"; text: string }

/** A submitted brief. Approving it is what lets the agent orchestrator act on it. */
export interface BriefVersion {
  version: number;
  fingerprint: string;
  answers: Answers;
  submittedAt: string;
  approvedAt: string | null;
  approvedBy: string | null;
}

interface ConsultData { answers: Answers; notes: ConsultNote[]; versions: BriefVersion[] }

export type BriefState = "draft" | "submitted" | "approved";

interface ConsultValue {
  facts: Facts;
  resolved: Resolved[];
  counts: BriefCounts;
  conflicts: string[];
  notes: ConsultNote[];
  versions: BriefVersion[];
  /** Where the current answers stand: not yet submitted (or changed since), submitted, or approved. */
  state: BriefState;
  /** The latest version, when the answers still say what it says. */
  current: BriefVersion | null;
  /** An approved version exists but the answers (or what discovery found) changed since. */
  changedSinceApproval: boolean;
  /** What the agent does, once the current answers are approved; null until then. */
  approved: { version: number; settings: AgentSettings } | null;
  setAnswer: (id: string, value: AnswerValue) => void;
  clearAnswer: (id: string) => void;
  acceptRecommendations: () => number;
  addNote: (text: string, kind?: ConsultNote["kind"]) => void;
  submit: () => void;
  approve: (by: string) => void;
  importAnswers: (answers: Answers) => number;
  resetConsultation: () => void;
}

const KEY = "ma.consult";
const EMPTY: ConsultData = { answers: {}, notes: [], versions: [] };
const Ctx = createContext<ConsultValue | null>(null);

export function useConsult(): ConsultValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useConsult must be used inside <ConsultProvider>");
  return v;
}

function load(key: string): ConsultData {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...EMPTY, ...(JSON.parse(raw) as ConsultData) } : EMPTY;
  } catch { return EMPTY; }
}

const now = () => new Date().toISOString();

export function ConsultProvider({ children }: { children: ReactNode }) {
  const { mode, discovery, connection } = useAppState();
  const { project, graph, capabilities, fabric, fabricGit } = useMigration();
  // Per data mode and project, like the plan: demo answers never leak into a live project.
  const key = `${KEY}.${mode}.${project.id}`;
  const [data, setData] = useState<ConsultData>(() => load(key));
  useEffect(() => { setData(load(key)); }, [key]);
  useEffect(() => {
    try { localStorage.setItem(key, JSON.stringify(data)); } catch { /* storage unavailable: kept for this visit */ }
  }, [key, data]);

  const facts = useMemo(() => readFacts({ discovery, connection, graph, capabilities, fabricConnected: fabric.status === "connected", fabricGit }),
    [discovery, connection, graph, capabilities, fabric.status, fabricGit]);
  const resolved = useMemo(() => resolve(facts, data.answers), [facts, data.answers]);
  const fp = useMemo(() => fingerprint(resolved), [resolved]);
  const conflicts = useMemo(() => briefConflicts(facts, resolved), [facts, resolved]);
  const counts = useMemo(() => countBrief(facts, resolved), [facts, resolved]);

  const latest = data.versions[data.versions.length - 1] ?? null;
  const current = latest && latest.fingerprint === fp ? latest : null;
  const state: BriefState = !current ? "draft" : current.approvedAt ? "approved" : "submitted";
  const changedSinceApproval = !current && data.versions.some((v) => v.approvedAt);
  const approved = useMemo(() => (current?.approvedAt ? { version: current.version, settings: settingsFrom(facts, resolved) } : null),
    [current, facts, resolved]);

  const note = (d: ConsultData, text: string, kind: ConsultNote["kind"] = "system"): ConsultData => ({ ...d, notes: [...d.notes, { at: now(), kind, text }].slice(-200) });

  const setAnswer = useCallback((id: string, value: AnswerValue) => setData((d) => ({ ...d, answers: { ...d.answers, [id]: value } })), []);
  const clearAnswer = useCallback((id: string) => setData((d) => {
    const answers = { ...d.answers };
    delete answers[id];
    return { ...d, answers };
  }), []);
  const acceptRecommendations = useCallback(() => {
    const taken = resolved.filter((r) => r.applies && r.source === "recommendation" && r.rec);
    if (!taken.length) return 0;
    setData((d) => note({ ...d, answers: { ...d.answers, ...Object.fromEntries(taken.map((r) => [r.q.id, r.rec!.value])) } },
      `Accepted ${taken.length} recommended answer${taken.length === 1 ? "" : "s"}.`));
    return taken.length;
  }, [resolved]);
  const addNote = useCallback((text: string, kind: ConsultNote["kind"] = "note") => setData((d) => note(d, text, kind)), []);
  const submit = useCallback(() => setData((d) => {
    const version = (d.versions[d.versions.length - 1]?.version ?? 0) + 1;
    const v: BriefVersion = { version, fingerprint: fp, answers: effectiveAnswers(resolved), submittedAt: now(), approvedAt: null, approvedBy: null };
    return note({ ...d, versions: [...d.versions, v] }, `Brief v${version} submitted for approval.`);
  }), [fp, resolved]);
  const approve = useCallback((by: string) => setData((d) => {
    const last = d.versions[d.versions.length - 1];
    if (!last || last.fingerprint !== fp) return d;
    const who = by.trim() || "this browser's user";
    const versions = [...d.versions.slice(0, -1), { ...last, approvedAt: now(), approvedBy: who }];
    return note({ ...d, versions }, `Brief v${last.version} approved by ${who}.`);
  }), [fp]);
  const importAnswers = useCallback((answers: Answers) => {
    const known = Object.entries(answers).filter(([id, v]) => QUESTION_BY_ID.has(id) && (typeof v === "string" || Array.isArray(v)));
    if (known.length) setData((d) => note({ ...d, answers: { ...d.answers, ...Object.fromEntries(known) } }, `Imported ${known.length} answer${known.length === 1 ? "" : "s"} from a questionnaire.`));
    return known.length;
  }, []);
  const resetConsultation = useCallback(() => setData(EMPTY), []);

  const value: ConsultValue = {
    facts, resolved, counts, conflicts, notes: data.notes, versions: data.versions, state, current, changedSinceApproval, approved,
    setAnswer, clearAnswer, acceptRecommendations, addNote, submit, approve, importAnswers, resetConsultation,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
