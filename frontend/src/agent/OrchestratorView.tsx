import { AlertTriangle, ArrowRight, Bot, Check, CheckCircle2, CircleDashed, Hand, Play, RotateCcw, Send, SkipForward, Square, X, XCircle } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useMigration, type StepKey } from "../state/MigrationState";
import type { OrchestrateTab } from "../components/journey/phases";
import { MigratePanel } from "../components/journey/panels/MigratePanel";
import { PlanPanel } from "../components/journey/panels/PlanPanel";
import { ValidatePanel } from "../components/journey/panels/ValidatePanel";
import type { StepStatus } from "../components/journey/useJourney";
import { Banner, Button, ConfirmDialog, StatusBadge } from "../components/shared/Shared";
import { useConsult } from "./ConsultState";
import { AGENT_STAGES, useAgent, type AgentStageKey, type AgentStageStatus, type GateOption } from "./OrchestratorState";

const STATUS: Record<AgentStageStatus, { label: string; tone: "success" | "warning" | "error" | "info" | "neutral" }> = {
  pending: { label: "Not started", tone: "neutral" },
  running: { label: "Running", tone: "info" },
  waiting: { label: "Needs you", tone: "warning" },
  done: { label: "Done", tone: "success" },
  failed: { label: "Failed", tone: "error" },
  skipped: { label: "Skipped", tone: "neutral" },
};

function StageIcon({ status }: { status: AgentStageStatus }) {
  if (status === "running") return <span className="spinner" aria-hidden="true" />;
  if (status === "done") return <Check size={14} aria-hidden="true" />;
  if (status === "waiting") return <Hand size={14} aria-hidden="true" />;
  if (status === "failed") return <XCircle size={14} aria-hidden="true" />;
  if (status === "skipped") return <SkipForward size={14} aria-hidden="true" />;
  return <CircleDashed size={14} aria-hidden="true" />;
}

const time = (iso?: string | null) => (iso ? new Date(iso).toLocaleTimeString() : "");
function duration(a?: string | null, b?: string | null) {
  if (!a) return "";
  const ms = (b ? new Date(b).getTime() : Date.now()) - new Date(a).getTime();
  if (ms < 1000) return "<1s";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

/**
 * Everything after the consultation, done in order by the agent: from the discovery snapshot to the
 * run and validation. Pick a stage to see what it is doing or did; when the agent needs a person it
 * asks at the stage where it stopped.
 */
export function OrchestratorView({ onConsult, onStep, onConnections, onClose, embedded, tab = "agent", onTab, steps = [] }: {
  onConsult: () => void; onStep: (k: StepKey) => void; onConnections: () => void; onClose?: () => void;
  /** Shown as a step of the journey: the step's header stands in for this view's own. */
  embedded?: boolean;
  /** The agent's run, or one of the phases it works through (also open to a person). */
  tab?: OrchestrateTab;
  onTab?: (t: OrchestrateTab) => void;
  steps?: StepStatus[];
}) {
  const setTab = (t: OrchestrateTab) => onTab?.(t);
  const phaseTabs: { id: OrchestrateTab; label: string; step?: StepStatus }[] = [
    { id: "agent", label: "Agent run" },
    { id: "plan", label: "Plan", step: steps.find((s) => s.key === "plan") },
    { id: "migrate", label: "Migration", step: steps.find((s) => s.key === "migrate") },
    { id: "validate", label: "Validation", step: steps.find((s) => s.key === "validate") },
  ];
  const tabs = onTab && (
    <div className="tabs panel-tabs ag-phase-tabs" role="tablist" aria-label="Agent orchestrator phases">
      {phaseTabs.map((t) => (
        <button key={t.id} type="button" role="tab" className="tab" aria-selected={tab === t.id} disabled={t.step?.locked}
          title={t.step?.locked ? "Opens once the phase before it is confirmed (the agent confirms them as it goes)" : t.step?.purpose}
          onClick={() => setTab(t.id)}>
          {t.step?.confirmed && !t.step.stale ? <Check size={13} aria-hidden="true" /> : t.step?.running ? <span className="spinner" aria-hidden="true" /> : null}
          {t.label}
        </button>
      ))}
    </div>
  );
  if (tab !== "agent") {
    const step = phaseTabs.find((t) => t.id === tab)!.step;
    return (
      <section className="ag-view" aria-label={`Agent orchestrator: ${tab}`}>
        {tabs}
        {tab === "plan" ? <PlanPanel /> : tab === "migrate" ? <MigratePanel /> : <ValidatePanel />}
        {step && <PhaseFooter step={step} next={phaseTabs[phaseTabs.findIndex((t) => t.id === tab) + 1]?.id} onTab={setTab} />}
      </section>
    );
  }
  return <AgentRun {...{ onConsult, onStep, onConnections, onClose, embedded, tabs, onTab: setTab }} />;
}

/** Under a phase worked by hand: where it stands, and confirming it so the next one opens. */
function PhaseFooter({ step, next, onTab }: { step: StepStatus; next?: OrchestrateTab; onTab: (t: OrchestrateTab) => void }) {
  const { confirmStep } = useMigration();
  const label = step.title;
  if (step.confirmed && !step.stale) {
    return (
      <div className="ag-phase-foot done" role="status">
        <CheckCircle2 size={18} className="tone-success" aria-hidden="true" />
        <span><strong>{label} confirmed.</strong> <span className="muted">{step.outcome}</span></span>
        {next && <Button size="small" onClick={() => onTab(next)}>Go to {next === "migrate" ? "Migration" : "Validation"}<ArrowRight size={14} aria-hidden="true" /></Button>}
      </div>
    );
  }
  return (
    <div className="ag-phase-foot" role="status">
      {step.complete ? <CheckCircle2 size={18} className={step.attention ? "tone-warning" : "tone-success"} aria-hidden="true" /> : <CircleDashed size={18} className="faint" aria-hidden="true" />}
      <span><strong>{step.complete ? `${label} finished${step.attention ? ` · ${step.attention}` : ""}` : `${label} is not finished`}</strong> <span className="muted">{step.complete ? step.outcome : step.waiting}</span></span>
      {step.complete && <Button size="small" variant="primary" onClick={() => { confirmStep(step.key); if (next) onTab(next); }}>Confirm {label.toLowerCase()}{next ? ", continue" : ""}</Button>}
    </div>
  );
}

const STAGE_TAB: Partial<Record<AgentStageKey, OrchestrateTab>> = { plan: "plan", readiness: "plan", run: "migrate", validate: "validate" };

function AgentRun({ onConsult, onStep, onConnections, onClose, embedded, tabs, onTab }: {
  onConsult: () => void; onStep: (k: StepKey) => void; onConnections: () => void; onClose?: () => void; embedded?: boolean; tabs: ReactNode; onTab: (t: OrchestrateTab) => void;
}) {
  const { data, activeStage, start, stop, reset, choose, ask } = useAgent();
  const c = useConsult();
  const { execution } = useMigration();
  const [follow, setFollow] = useState(true);
  const [picked, setPicked] = useState<AgentStageKey>(activeStage ?? "brief");
  const [text, setText] = useState("");
  const [confirmStop, setConfirmStop] = useState(false);
  useEffect(() => { if (follow && activeStage) setPicked(activeStage); }, [follow, activeStage]);

  const stage = AGENT_STAGES.find((s) => s.key === picked)!;
  const st = data.stages[picked];
  const finished = AGENT_STAGES.filter((s) => ["done", "skipped"].includes(data.stages[s.key].status)).length;
  const pct = Math.round((finished / AGENT_STAGES.length) * 100);
  const running = data.status === "running";
  const now = activeStage ? AGENT_STAGES.find((s) => s.key === activeStage)! : null;
  const groups = [...new Set(AGENT_STAGES.map((s) => s.group))];

  const act = (o: GateOption) => {
    if (o.navigate?.view === "consult") return onConsult();
    if (o.navigate?.step) return onStep(o.navigate.step);
    if (o.navigate?.connections) return onConnections();
    choose(picked, o.id);
  };
  const send = () => { if (!text.trim()) return; ask(text); setText(""); };
  // The latest exchanges with the agent, beside the box they came from; everything is in the Activity panel.
  const recent = data.log.slice(-4);

  const mainLabel = data.status === "idle" ? "Start the agent" : data.status === "done" ? "Run the agent again" : "Resume";

  return (
    <section className="ag-view" aria-label="Agent orchestrator">
      {!embedded && (
        <header className="ag-head">
          <span className="step-head-icon" aria-hidden="true"><Bot size={20} /></span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2>Agent orchestrator</h2>
            <p className="muted">Everything after the consultation, done in order by the agent.</p>
          </div>
          {onClose && <Button size="small" variant="ghost" onClick={onClose}><X size={14} aria-hidden="true" />Close</Button>}
        </header>
      )}
      {tabs}
      <p className="muted" style={{ margin: 0 }}>Everything after the consultation, done in order by the agent: the plan, the migration run and validation. Pick any stage to see what it is doing or did; when the agent needs you, it asks at the stage where it stopped. The Plan, Migration and Validation tabs show the same work in full, and can be worked by hand.</p>

      <div className="row ag-controls">
        <label className="check-row" style={{ margin: 0 }}>
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          <span>Follow the agent</span>
        </label>
        <span className="spacer" />
        {running
          ? <Button onClick={() => setConfirmStop(true)}><Square size={14} aria-hidden="true" />Stop</Button>
          : <Button variant="primary" onClick={start} disabled={!c.facts.discovered}>{data.status === "idle" || data.status === "done" ? <Play size={15} aria-hidden="true" /> : <RotateCcw size={15} aria-hidden="true" />}{mainLabel}</Button>}
        {!running && data.status !== "idle" && <Button variant="ghost" onClick={reset}>Clear the agent's record</Button>}
      </div>
      {!c.approved && data.status === "idle" && (
        <Banner tone="info" title="The agent works from an approved brief" actions={<Button size="small" onClick={onConsult}>Open the consultation</Button>}>
          {c.changedSinceApproval ? "The answers changed since the last approval: submit and approve them again." : "Answer the consultation's questions, submit the brief and approve it. Starting now, the agent waits at the first stage for that."}
        </Banner>
      )}

      <section className="card ag-mission" aria-label="Where the agent stands">
        <div className="row" style={{ gap: 12 }}>
          <StatusBadge tone={data.status === "done" ? "success" : data.status === "failed" ? "error" : data.status === "waiting" ? "warning" : running ? "info" : "neutral"} running={running}>
            {data.status === "idle" ? "Not started" : data.status === "waiting" ? "Needs you" : data.status === "stopped" ? "Stopped" : data.status === "done" ? "Done" : data.status === "failed" ? "Failed" : "Running"}
          </StatusBadge>
          <div style={{ flex: 1, minWidth: 0 }}>
            <span className="eyebrow">Now</span>{" "}
            <strong>{now ? now.title : data.status === "done" ? "Finished" : "—"}</strong>
            <p className="muted" style={{ margin: 0 }}>{now ? data.stages[now.key].message : data.status === "done" ? data.stages.validate.message : "The agent has not started."}</p>
          </div>
          <strong className="ag-pct">{pct}%</strong>
        </div>
        <span className="ag-meter" aria-hidden="true"><span style={{ width: `${pct}%` }} /></span>
        <p className="faint" style={{ margin: 0 }}>
          {finished} of {AGENT_STAGES.length} stages done
          {data.startedAt ? ` · started ${new Date(data.startedAt).toLocaleString()}` : ""}
          {data.briefVersion ? ` · brief v${data.briefVersion}` : ""}
          {execution.runId ? ` · run ${execution.runId}` : ""}
        </p>
      </section>

      <div className="ag-orch">
        <nav className="card ag-stages" aria-label="Agent stages">
          {groups.map((g) => (
            <div key={g}>
              <span className="eyebrow">{g}</span>
              <ul>
                {AGENT_STAGES.filter((s) => s.group === g).map((s) => {
                  const x = data.stages[s.key];
                  return (
                    <li key={s.key}>
                      <button type="button" className={`ag-stage ${x.status}${picked === s.key ? " selected" : ""}`} aria-current={picked === s.key ? "true" : undefined}
                        onClick={() => { setPicked(s.key); if (activeStage && s.key !== activeStage) setFollow(false); }}>
                        <span className="ag-stage-icon"><StageIcon status={x.status} /></span>
                        <span className="ag-stage-text">
                          <strong>{s.title}</strong>
                          <span className="faint">{x.status === "pending" ? "Not started" : x.message}</span>
                        </span>
                        {x.attention && <AlertTriangle size={14} className="tone-warning" aria-label="needs attention" />}
                        {x.startedAt && <span className="faint ag-dur">{duration(x.startedAt, x.finishedAt ?? (x.status === "running" ? null : x.startedAt))}</span>}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>

        <section className="card ag-detail" aria-labelledby="ag-stage-title">
          <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
            <span className={`ag-stage-icon big ${st.status}`}><StageIcon status={st.status} /></span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <span className="eyebrow">Stage {AGENT_STAGES.indexOf(stage) + 1} of {AGENT_STAGES.length} · {stage.group}</span>
              <h3 id="ag-stage-title" style={{ margin: "2px 0" }}>{stage.title}</h3>
              <p className="muted" style={{ margin: 0 }}>{stage.blurb}</p>
            </div>
            <StatusBadge tone={STATUS[st.status].tone} running={st.status === "running"}>{STATUS[st.status].label}</StatusBadge>
          </div>
          <dl className="ag-facts">
            <div><dt>Started</dt><dd>{time(st.startedAt) || "—"}</dd></div>
            <div><dt>{st.status === "running" ? "Running for" : "Took"}</dt><dd>{st.startedAt ? duration(st.startedAt, st.finishedAt) : "—"}</dd></div>
            <div><dt>Status</dt><dd>{STATUS[st.status].label}</dd></div>
          </dl>
          <div className={`ag-now ${st.status}`}>
            <span className="eyebrow">{st.status === "running" ? "Now" : "Result"}</span>
            <p style={{ margin: 0 }}>{st.status === "pending" ? "Not started yet." : st.message}</p>
            {!!st.details?.length && <ul className="ag-details">{st.details.map((d) => <li key={d}>{d}</li>)}</ul>}
          </div>
          {st.gate && st.status === "waiting" && (
            <div className="ag-gate" role="group" aria-label={st.gate.title}>
              <strong><Hand size={15} aria-hidden="true" /> {st.gate.title}</strong>
              <p className="muted" style={{ margin: 0 }}>{st.gate.why}</p>
              {!!st.gate.items?.length && <ul className="ag-details">{st.gate.items.map((i) => <li key={i}>{i}</li>)}</ul>}
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                {st.gate.options.map((o) => <Button key={o.id} size="small" variant={o.primary ? "primary" : "default"} onClick={() => act(o)}>{o.label}</Button>)}
              </div>
            </div>
          )}
          {st.status === "failed" && (
            <div className="row" style={{ gap: 8 }}>
              <Button variant="primary" size="small" onClick={start}><RotateCcw size={14} aria-hidden="true" />Try this stage again</Button>
            </div>
          )}
          {STAGE_TAB[picked] && (
            <button type="button" className="link" style={{ alignSelf: "flex-start" }} onClick={() => onTab(STAGE_TAB[picked]!)}>
              Open the {STAGE_TAB[picked] === "plan" ? "Plan" : STAGE_TAB[picked] === "migrate" ? "Migration" : "Validation"} tab<ArrowRight size={13} aria-hidden="true" style={{ verticalAlign: "-2px", marginLeft: 4 }} />
            </button>
          )}
        </section>
      </div>

      <section className="card ag-ask" aria-labelledby="ag-orch-ask">
        <div className="row" style={{ gap: 8 }}>
          <Bot size={16} aria-hidden="true" />
          <h3 id="ag-orch-ask" style={{ margin: 0, flex: 1 }}>Ask the orchestrator to do something</h3>
          <StatusBadge tone="neutral">AI off</StatusBadge>
        </div>
        <p className="muted" style={{ margin: 0 }}>For example: "status", "why did it fail", "retry", "mismatches", "stop". Type ? for everything it understands.</p>
        {recent.length > 0 && (
          <ul className="ag-notes" aria-label="Latest from the agent">
            {recent.map((l, i) => <li key={`${l.at}-${i}`} className={l.kind === "you" ? "you" : ""}><span className="ag-note-kind">{l.kind === "you" ? "You" : "Agent"}</span>{l.text}</li>)}
          </ul>
        )}
        <div className="ag-compose">
          <input className="input" aria-label="Ask the orchestrator" placeholder="Ask or tell the agent…" value={text}
            onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") send(); }} />
          <Button variant="primary" onClick={send} disabled={!text.trim()}><Send size={14} aria-hidden="true" />Do it</Button>
        </div>
      </section>

      {confirmStop && (
        <ConfirmDialog title="Stop the agent?" confirmLabel="Stop" onCancel={() => setConfirmStop(false)} onConfirm={() => { setConfirmStop(false); stop(); }}>
          <p>The agent stops at its current stage. A migration run it started is paused with it{execution.state === "running" ? " (one is running now)" : ""}; nothing already created in Fabric is removed.</p>
          <p>Resume carries on from the stage where it stopped.</p>
        </ConfirmDialog>
      )}
      {data.status === "stopped" && <Banner tone="warning" title="The agent is stopped" actions={<Button size="small" variant="primary" onClick={start}><Play size={14} aria-hidden="true" />Resume</Button>}>It carries on from the first stage that is not done.</Banner>}
    </section>
  );
}
