import { AtSign, Bot, CheckCircle2, Circle, CircleAlert, CircleDashed, Lightbulb, ListChecks, Plus, SendHorizontal, Sparkles, Wrench, X, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import { useOrchestrator } from "../../state/OrchestratorState";
import type { AssistantEntry, AssistantTask, PlanStepStatus } from "../../types";
import { ActionCard } from "../orchestrator/ActionCard";
import { Button, Collapsible } from "../shared/Shared";
import { STEPS } from "../journey/steps";

const SUGGESTIONS = [
  { icon: Wrench, text: "Why did the run fail, and what can be done?" },
  { icon: ListChecks, text: "What is left to do?" },
  { icon: Lightbulb, text: "Where does the migration stand?" },
];
const STEP_ICON: Record<PlanStepStatus, typeof Circle> = {
  done: CheckCircle2, ready: Circle, waiting: CircleDashed, in_progress: CircleDashed, failed: XCircle, warn: CircleAlert, skipped: CircleDashed,
};
const STATUS_WORDS: Record<AssistantTask["status"], string> = {
  running: "Working on it…", waiting_for_person: "Waiting for you to confirm or reject the action below.",
  waiting_for_reply: "Waiting for your reply.", done: "", cancelled: "Cancelled.", paused: "Paused when the accelerator stopped: Continue carries it on.",
  blocked: "Stopped: see why above.",
};

/** The round "+" that opens the assistant, with the number of requests waiting for a person. */
export function AssistantLauncher({ onOpen }: { onOpen: () => void }) {
  const { waiting } = useOrchestrator();
  return (
    <button type="button" className="ai-fab" onClick={onOpen} aria-label={`Open the migration assistant (Ctrl+I)${waiting ? `: ${waiting} request${waiting === 1 ? "" : "s"} waiting` : ""}`}
      title="Migration assistant · Ctrl+I">
      <span className="ai-fab-glow" aria-hidden="true" />
      <Sparkles size={20} aria-hidden="true" />
      <span className="ai-fab-plus" aria-hidden="true"><Plus size={11} strokeWidth={3} /></span>
      <span className="ai-fab-label">Ask AI</span>
      {waiting > 0 && <span className="ai-fab-badge" aria-hidden="true">{waiting}</span>}
    </button>
  );
}

function Entry({ entry, task }: { entry: AssistantEntry; task: AssistantTask }) {
  if (entry.type === "person") return <div className="ai-msg mine"><div className="ai-bubble">{entry.text}</div></div>;
  if (entry.type === "answer" || entry.type === "ask") {
    return (
      <div className="ai-msg">
        <span className="ai-avatar small" aria-hidden="true"><Sparkles size={13} /></span>
        <div className="ai-bubble" style={{ whiteSpace: "pre-wrap" }}>{entry.text}</div>
      </div>
    );
  }
  if (entry.type === "step") {
    if (entry.kind === "finish" || entry.kind === "ask") return null;
    return <p className="faint ai-step"><Bot size={12} aria-hidden="true" /> {entry.kind === "read" ? "Reading" : "Proposing"} <code>{entry.tool}</code>{entry.message ? `: ${entry.message}` : ""}</p>;
  }
  if (entry.type === "result") {
    return (
      <div className="ai-result">
        <Collapsible summary={<span className="faint">{entry.ok === false ? "Could not read" : "Read"} <code>{entry.tool}</code></span>}>
          <pre className="ai-data">{entry.data ?? entry.error}</pre>
        </Collapsible>
      </div>
    );
  }
  if (entry.type === "action") {
    const action = (task.actions ?? []).find((a) => a.id === entry.actionId);
    return action ? <ActionCard action={action} /> : null;
  }
  if (entry.type === "decision") {
    if (entry.decision === "lapsed") {
      return <p className="faint ai-step">The request for <code>{entry.tool}</code> lapsed{entry.reason ? `: ${entry.reason}` : ""}.</p>;
    }
    return <p className="faint ai-step">You {entry.decision === "confirmed" ? "confirmed" : "rejected"} <code>{entry.tool}</code>{entry.reason ? `: ${entry.reason}` : ""}.</p>;
  }
  return <p className={`faint ai-step${entry.type === "error" ? " ai-error" : ""}`}>{entry.text}</p>;
}

/**
 * The migration assistant, docked on the right like an editor's chat. It answers from the accelerator's own record
 * (read tools), and with an approved brief's consent asks Claude, one checked step at a time: every change it would
 * make is an action card a person confirms or rejects. It never approves, accepts a draft or enters a credential.
 */
export function AssistantPanel({ onClose }: { onClose: () => void }) {
  const { pathname } = useLocation();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { connection } = useAppState();
  const { execution, fabric } = useMigration();
  const { plan, task, tasks, ask, openTask, cancelTask, continueTask, busy, actionError, clearActionError } = useOrchestrator();
  const [tab, setTab] = useState<"ask" | "plan" | "requests">("ask");
  const [text, setText] = useState("");
  const ref = useRef<HTMLElement>(null);
  const thread = useRef<HTMLDivElement>(null);
  useEffect(() => { ref.current?.focus(); }, []);
  useEffect(() => { thread.current?.scrollTo?.({ top: thread.current.scrollHeight }); }, [task?.entries?.length]);

  const step = pathname.startsWith("/migration") ? STEPS.find((s) => s.key === params.get("step"))?.title ?? "Migration" : "Connections";
  const failed = execution.items.filter((i) => i.status === "FAILED").length;
  const context = [
    `Step: ${step}`,
    connection.workspace && `Source: ${connection.workspace}`,
    fabric.workspaceName && `Target: ${fabric.workspaceName}`,
    failed > 0 && `${failed} failed object${failed === 1 ? "" : "s"}`,
  ].filter(Boolean) as string[];
  const ai = plan?.assistant?.ai ?? false;
  const requests = plan?.actions ?? [];
  const replying = task?.status === "waiting_for_reply";
  const thinking = task?.status === "running" || busy === "ask";
  const send = async (words: string) => {
    if (!words.trim()) return;
    if (await ask(words.trim())) setText("");
  };

  return (
    <aside className="ai-panel" aria-label="Migration assistant" ref={ref} tabIndex={-1} onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <header className="ai-head">
        <span className="ai-avatar" aria-hidden="true"><Sparkles size={16} /></span>
        <div className="ai-title"><strong>Migration Assistant</strong><span className="ai-badge" title={ai ? "Questions go to Claude, one checked step at a time" : plan?.assistant?.reason ?? ""}>{ai ? "AI" : "Rules"}</span></div>
        <span className="spacer" />
        <button type="button" className="btn ghost icon small" onClick={onClose} aria-label="Close the assistant"><X size={16} /></button>
      </header>

      <div className="ai-context" aria-label="What the assistant will use">
        {context.map((c) => <span key={c} className="ai-chip"><AtSign size={11} aria-hidden="true" />{c}</span>)}
      </div>

      <div className="tabs tabs-pill ai-tabs" role="tablist" aria-label="Assistant">
        {([["ask", "Ask"], ["plan", "Plan"], ["requests", `Requests${requests.length ? ` (${requests.length})` : ""}`]] as const).map(([id, label]) => (
          <button key={id} type="button" role="tab" className="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>
        ))}
      </div>

      <div className="ai-thread" ref={thread}>
        {actionError && <p className="ai-step ai-error" role="alert">{actionError} <button type="button" className="link" onClick={clearActionError}>Dismiss</button></p>}
        {tab === "ask" && (task ? (
          <>
            <div className="row" style={{ gap: 6 }}>
              <button type="button" className="link" onClick={() => void openTask(null)}>All questions</button>
              <span className="spacer" />
              {task.source === "ai" && <span className="faint">{task.turns} turn{task.turns === 1 ? "" : "s"} · ${task.costUsd.toFixed(2)}</span>}
            </div>
            {(task.entries ?? []).map((e, i) => <Entry key={`${e.at}-${i}`} entry={e} task={task} />)}
            {STATUS_WORDS[task.status] && <p className="faint ai-step" role="status">{thinking && <span className="spinner" aria-hidden="true" />} {STATUS_WORDS[task.status]}</p>}
            <div className="row" style={{ gap: 6 }}>
              {(task.status === "running" || task.status === "waiting_for_person" || task.status === "waiting_for_reply") &&
                <Button size="small" variant="ghost" onClick={() => void cancelTask()}>Cancel</Button>}
              {(task.status === "paused" || task.status === "blocked") && task.source === "ai" &&
                <Button size="small" onClick={() => void continueTask()}>Continue</Button>}
            </div>
          </>
        ) : (
          <>
            <div className="ai-msg">
              <span className="ai-avatar small" aria-hidden="true"><Sparkles size={13} /></span>
              <div className="ai-bubble">
                <p><strong>Ask about the migration, and fix it without leaving this screen.</strong></p>
                <p>{ai ? "Questions go to Claude, which reads the accelerator's record one step at a time. Anything it would change is shown to you first, to confirm or reject."
                  : `I answer from the accelerator's own record: why something failed, what is left, where it stands. ${plan?.assistant?.reason ? `The AI is off: ${plan.assistant.reason}` : ""}`}</p>
                <p className="faint">I never approve anything, accept a draft or enter a credential: you do, and I say where.</p>
              </div>
            </div>
            <div className="ai-suggestions" aria-label="Suggested questions">
              {SUGGESTIONS.map(({ icon: Icon, text: words }) => (
                <button key={words} type="button" className="ai-suggestion" onClick={() => void send(words)} disabled={busy === "ask"}>
                  <Icon size={14} aria-hidden="true" />{words}
                </button>
              ))}
            </div>
            {tasks.length > 0 && (
              <div className="ai-history" aria-label="Earlier questions">
                <span className="eyebrow">Earlier</span>
                {tasks.slice(0, 6).map((t) => <button key={t.id} type="button" className="link" onClick={() => void openTask(t.id)}>{t.question}</button>)}
              </div>
            )}
          </>
        ))}
        {tab === "plan" && (plan ? (
          <div className="stack" style={{ gap: 6 }}>
            <p className="faint" style={{ margin: 0 }}>Execution plan v{plan.version}: {plan.steps.filter((s) => s.status === "done").length} of {plan.steps.length} done.</p>
            <ul className="ai-checklist" aria-label="Execution plan">
              {plan.steps.map((s) => {
                const Icon = STEP_ICON[s.status];
                return <li key={s.key} className={`status-${s.status}`}><Icon size={13} aria-hidden="true" /> {s.title}</li>;
              })}
            </ul>
            <Button size="small" onClick={() => navigate("/migration?step=migrate&view=plan")}>Open the plan board</Button>
          </div>
        ) : <p className="faint">The execution plan appears once discovery has run.</p>)}
        {tab === "requests" && (requests.length
          ? requests.map((a) => <ActionCard key={a.id} action={a} />)
          : <p className="faint">Nothing waits for you. What the agent proposes appears here, and on the plan board.</p>)}
      </div>

      {tab === "ask" && (
        <form className="ai-composer" onSubmit={(e) => { e.preventDefault(); void send(text); }}>
          <label className="sr-only" htmlFor="ai-input">Message the assistant</label>
          <textarea id="ai-input" rows={3} value={text} maxLength={2000} disabled={thinking || task?.status === "waiting_for_person"}
            placeholder={replying ? "Your reply…" : "Ask about an error, an object or a step…"} onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(text); } }} />
          <div className="ai-composer-row">
            <span className="faint">{replying ? "Replying to the question above" : ai ? "Claude, with every tool off; changes wait for you" : "Answered from the accelerator's record"}</span>
            <span className="spacer" />
            <button type="submit" className="btn primary icon small" disabled={!text.trim() || thinking} aria-label="Send"><SendHorizontal size={15} /></button>
          </div>
        </form>
      )}
    </aside>
  );
}
