import { Bot, ChevronRight, MessageSquare } from "lucide-react";
import type { ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAppState } from "../state/AppState";
import type { StepKey } from "../state/MigrationState";
import { ConsultationView } from "./ConsultationView";
import { ConsultProvider, useConsult } from "./ConsultState";
import { OrchestratorProvider, useAgent } from "./OrchestratorState";
import { OrchestratorView } from "./OrchestratorView";
import "./agent.css";

/**
 * Agent mode: the consultation and the agent orchestrator, beside the six steps rather than in them.
 * Everything it does goes through the same state the steps use, so either way of working sees the
 * other's results.
 */

export type AgentViewKey = "consult" | "agent";
export const AGENT_VIEW_PARAM = "view";
export const isAgentView = (v: string | null): v is AgentViewKey => v === "consult" || v === "agent";

export function AgentProvider({ children }: { children: ReactNode }) {
  return (
    <ConsultProvider>
      <OrchestratorProvider>{children}</OrchestratorProvider>
    </ConsultProvider>
  );
}

/** The two ways into agent mode, at the foot of the workspace explorer. */
export function AgentLaunch() {
  const { discovery } = useAppState();
  const c = useConsult();
  const { data } = useAgent();
  const [params, setParams] = useSearchParams();
  const view = params.get(AGENT_VIEW_PARAM);
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const open = (v: AgentViewKey) => {
    const next = new URLSearchParams(params);
    next.set(AGENT_VIEW_PARAM, v);
    setParams(next);
  };
  const consultMeta = c.state === "approved" ? `Brief v${c.current?.version} approved`
    : c.state === "submitted" ? "Waiting for approval"
    : `${c.counts.answered + c.counts.assumed} of ${c.counts.applicable} decided`;
  const agentMeta = { idle: c.approved ? "Ready to start" : "Needs an approved brief", running: "Running", waiting: "Needs you", stopped: "Stopped", done: "Finished", failed: "Stopped at a failure" }[data.status];
  const entries: { key: AgentViewKey; title: string; meta: string; icon: typeof Bot; attention: boolean }[] = [
    { key: "consult", title: "Consultation", meta: consultMeta, icon: MessageSquare, attention: c.counts.conflicts > 0 },
    { key: "agent", title: "Agent orchestrator", meta: agentMeta, icon: Bot, attention: data.status === "waiting" || data.status === "failed" },
  ];
  return (
    <div className="ag-launch" role="group" aria-label="Agent mode">
      <div className="explorer-head"><span className="eyebrow">Agent mode</span><span className="faint">preview</span></div>
      {entries.map((e) => (
        <button key={e.key} type="button" className={`ag-launch-item${view === e.key ? " active" : ""}${e.attention ? " attention" : ""}`}
          disabled={!discovered} aria-current={view === e.key ? "page" : undefined}
          title={discovered ? undefined : "Available once discovery has finished"} onClick={() => open(e.key)}>
          <span className="ag-launch-icon" aria-hidden="true"><e.icon size={15} /></span>
          <span className="ag-launch-text"><strong>{e.title}</strong><span className="faint">{discovered ? e.meta : "After discovery"}</span></span>
          {data.status === "running" && e.key === "agent" ? <span className="spinner" aria-hidden="true" /> : <ChevronRight size={14} className="faint" aria-hidden="true" />}
        </button>
      ))}
    </div>
  );
}

/** The consultation or the orchestrator, shown in place of the step panel. */
export function AgentView({ view, onBack }: { view: AgentViewKey; onBack: () => void }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const go = (v: AgentViewKey) => { const next = new URLSearchParams(params); next.set(AGENT_VIEW_PARAM, v); setParams(next); };
  const step = (k: StepKey) => setParams({ step: k });
  return view === "consult"
    ? <ConsultationView onAgent={() => go("agent")} onClose={onBack} />
    : <OrchestratorView onConsult={() => go("consult")} onStep={step} onConnections={() => navigate("/")} onClose={onBack} />;
}
