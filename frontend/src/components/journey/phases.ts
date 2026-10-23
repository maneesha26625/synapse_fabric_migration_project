import { Bot, MessageSquare, Radar, type LucideIcon } from "lucide-react";
import { useConsult } from "../../agent/ConsultState";
import { useAgent } from "../../agent/OrchestratorState";
import type { StepKey } from "../../state/MigrationState";
import { useJourney, type StepStatus } from "./useJourney";

/**
 * The migration as three phases, the way it is done with a client: discover the source (with its
 * assessment and dependency waves), agree the approach in a consultation, then let the agent
 * orchestrator plan, migrate and validate. Underneath, the six steps keep their own state and
 * confirmations; each phase is a view over some of them.
 */

export type PhaseKey = "discover" | "consult" | "orchestrate";
export type DiscoverTab = "objects" | "assessment" | "waves";
export type OrchestrateTab = "agent" | "plan" | "migrate" | "validate";

export interface PhaseMeta { key: PhaseKey; title: string; purpose: string; icon: LucideIcon; steps: StepKey[] }

export const PHASES: PhaseMeta[] = [
  { key: "discover", title: "Discover", purpose: "Read the source workspace, see how each object moves, and the waves it moves in", icon: Radar, steps: ["discover", "assess", "waves"] },
  { key: "consult", title: "Consultation", purpose: "Agree with the client how the migration is done", icon: MessageSquare, steps: [] },
  { key: "orchestrate", title: "Agent orchestrator", purpose: "Plan, migrate and validate, done in order by the agent with a person approving", icon: Bot, steps: ["plan", "migrate", "validate"] },
];

export const PHASE_KEYS = PHASES.map((p) => p.key);

/** Where a step key (or an old link) opens: its phase, and the tab inside it. */
export function locate(key: string | null): { phase: PhaseKey; tab: string | null } | null {
  switch (key) {
    case "discover": return { phase: "discover", tab: null };
    case "assess": return { phase: "discover", tab: "assessment" };
    case "waves": return { phase: "discover", tab: "waves" };
    case "consult": return { phase: "consult", tab: null };
    case "orchestrate": return { phase: "orchestrate", tab: null };
    case "plan": case "migrate": case "validate": return { phase: "orchestrate", tab: key };
    default: return null;
  }
}

export interface PhaseStatus extends PhaseMeta {
  index: number;
  locked: boolean;
  confirmed: boolean;
  complete: boolean;
  running: boolean;
  paused: boolean;
  attention: string | null;
  metric: string;
  outcome: string;
  waiting: string;
  stale: boolean;
  lostReason: string;
}

export function usePhases(): { phases: PhaseStatus[]; current: PhaseKey; steps: StepStatus[]; finished: boolean } {
  const { steps, finished } = useJourney();
  const c = useConsult();
  const agent = useAgent();
  const by = (k: StepKey) => steps.find((s) => s.key === k)!;
  const group = (keys: StepKey[]) => keys.map(by);

  const d = group(["discover", "assess", "waves"]);
  const discoverConfirmed = d.every((s) => s.confirmed && !s.stale);
  const dStale = d.find((s) => s.stale);
  const dOpen = d.find((s) => !s.complete);
  const discover: Omit<PhaseStatus, keyof PhaseMeta | "index"> = {
    locked: false,
    confirmed: discoverConfirmed,
    complete: d.every((s) => s.complete),
    running: d.some((s) => s.running),
    paused: false,
    attention: d.find((s) => s.attention)?.attention ?? null,
    metric: by("waves").complete ? `${by("discover").metric} · ${by("waves").metric}` : by("discover").metric,
    outcome: [by("discover").outcome, by("waves").outcome].filter(Boolean).join(" "),
    waiting: dOpen?.waiting ?? "",
    stale: !!dStale,
    lostReason: dStale?.lostReason ?? "",
  };

  const approved = c.state === "approved";
  const consult: typeof discover = {
    locked: !discoverConfirmed,
    confirmed: approved,
    complete: approved,
    running: false,
    paused: false,
    attention: c.counts.conflicts ? `${c.counts.conflicts} conflict${c.counts.conflicts === 1 ? "" : "s"}` : null,
    metric: approved ? `Brief v${c.current?.version} approved` : c.state === "submitted" ? "Waiting for approval" : `${c.counts.answered + c.counts.assumed} of ${c.counts.applicable} decided`,
    outcome: approved ? `Brief v${c.current?.version} approved by ${c.current?.approvedBy}.` : "",
    waiting: c.state === "submitted" ? "The brief waits for approval." : "Answer the questions, then submit and approve the brief.",
    stale: c.changedSinceApproval,
    lostReason: c.changedSinceApproval ? "The answers changed since the brief was approved." : "",
  };

  const o = group(["plan", "migrate", "validate"]);
  const oStale = o.find((s) => s.stale);
  const agentBusy = agent.data.status === "running";
  const agentNeeds = agent.data.status === "waiting" || agent.data.status === "failed";
  const orchestrate: typeof discover = {
    // The agent needs an approved brief; the plan, run and validation can also be worked by hand.
    locked: !discoverConfirmed,
    confirmed: o.every((s) => s.confirmed && !s.stale),
    complete: by("validate").complete,
    running: agentBusy || o.some((s) => s.running),
    paused: by("migrate").paused,
    attention: agentNeeds ? (agent.data.status === "waiting" ? "The agent needs you" : "The agent stopped at a failure") : o.find((s) => s.attention)?.attention ?? null,
    metric: agentBusy ? "Agent running" : agent.data.status === "done" ? "Agent finished" : o.find((s) => s.running || s.paused)?.metric ?? (by("validate").complete ? by("validate").metric : by("migrate").complete ? by("migrate").metric : "Not started"),
    outcome: [by("migrate").outcome, by("validate").outcome].filter(Boolean).join(" "),
    waiting: agentBusy ? "The agent is working." : approved ? "Start the agent, or work through the plan, migration and validation." : "Approve the brief so the agent can run, or work through the plan, migration and validation.",
    stale: !!oStale,
    lostReason: oStale?.lostReason ?? "",
  };

  const facts = { discover, consult, orchestrate };
  const phases = PHASES.map((meta, index) => ({ ...meta, index, ...facts[meta.key] }));
  const current: PhaseKey = !discover.confirmed ? "discover" : !consult.confirmed && !orchestrate.confirmed && agent.data.status === "idle" && !o.some((s) => s.confirmed) ? "consult" : "orchestrate";
  return { phases, current, steps, finished: finished && !orchestrate.stale };
}
