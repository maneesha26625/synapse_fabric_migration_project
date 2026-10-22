import { AlertTriangle, Bot, Check, CircleDashed, Hand, Play, RotateCcw, Send, SkipForward, Square, X, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useMigration, type StepKey } from "../state/MigrationState";
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
export function OrchestratorView({ onConsult, onStep, onConnections, onClose }: {
  onConsult: () => void; onStep: (k: StepKey) => void; onConnections: () => void; onClose: () => void;
}) {
  const { data, activeStage, start, stop, reset, choose, ask } = useAgent();
  const c = useConsult();
  const { execution } = useMigration();
  const [follow, setFollow] = useState(true);
  const [picked, setPicked] = useState<AgentStageKey>(activeStage ?? "brief");
  const [text, setText] = useState("");
  const [confirmStop, setConfirmStop] = useState(false);
  const [logFilter, setLogFilter] = useState<"all" | "problems" | "agent" | "you">("all");
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
  const lines = data.log.filter((l) => logFilter === "all" || (logFilter === "problems" ? l.kind === "warn" || l.kind === "error" : logFilter === "you" ? l.kind === "you" : l.kind !== "you")).slice().reverse();
  const problems = data.log.filter((l) => l.kind === "error").length;

  const mainLabel = data.status === "idle" ? "Start the agent" : data.status === "done" ? "Run the agent again" : "Resume";

  return (
    <section className="ag-view" aria-labelledby="ag-orch-title">
      <header className="ag-head">
        <span className="step-head-icon" aria-hidden="true"><Bot size={20} /></span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <span className="eyebrow">Agent mode · preview</span>
          <h2 id="ag-orch-title">Agent orchestrator</h2>
          <p className="muted">Everything after the consultation, done in order by the agent: from the discovery snapshot to the migration run and validation. Pick any stage to see what it is doing or did. When the agent needs you, it asks at the stage where it stopped.</p>
        </div>
        <Button size="small" variant="ghost" onClick={onClose}><X size={14} aria-hidden="true" />Back to the steps</Button>
      </header>

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
            <p className="muted" style={{ margin: 0 }}>{now ? data.stages[now.key].message : data.status === "done" ? data.stages.handover.message : "The agent has not started."}</p>
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
        </section>
      </div>

      <section className="card ag-ask" aria-labelledby="ag-orch-ask">
        <div className="row" style={{ gap: 8 }}>
          <Bot size={16} aria-hidden="true" />
          <h3 id="ag-orch-ask" style={{ margin: 0, flex: 1 }}>Ask the orchestrator to do something</h3>
          <StatusBadge tone="neutral">AI off</StatusBadge>
        </div>
        <p className="muted" style={{ margin: 0 }}>For example: "status", "why did it fail", "retry", "mismatches", "stop". Type ? for everything it understands.</p>
        <div className="ag-compose">
          <input className="input" aria-label="Ask the orchestrator" placeholder="Ask or tell the agent…" value={text}
            onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") send(); }} />
          <Button variant="primary" onClick={send} disabled={!text.trim()}><Send size={14} aria-hidden="true" />Do it</Button>
        </div>
      </section>

      <section className="card ag-activity" aria-labelledby="ag-activity-title">
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <h3 id="ag-activity-title" style={{ margin: 0 }}>Activity</h3>
          {problems > 0 && <StatusBadge tone="error">{problems} error{problems === 1 ? "" : "s"}</StatusBadge>}
          <span className="spacer" />
          <div className="tabs panel-tabs" role="tablist" aria-label="Filter the activity">
            {(["all", "problems", "agent", "you"] as const).map((f) => (
              <button key={f} type="button" role="tab" className="tab" aria-selected={logFilter === f} onClick={() => setLogFilter(f)}>
                {f === "all" ? "All" : f === "problems" ? "Problems" : f === "agent" ? "Agent" : "You"}
              </button>
            ))}
          </div>
        </div>
        <ul className="ag-log">
          {lines.slice(0, 80).map((l, i) => (
            <li key={`${l.at}-${i}`} className={l.kind}>
              <span className="ag-log-time">{time(l.at)}</span>
              <span className="ag-log-who">{l.kind === "you" ? "You" : "Agent"}</span>
              <span>{l.text}</span>
            </li>
          ))}
          {!lines.length && <li className="faint">Nothing yet.</li>}
        </ul>
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
