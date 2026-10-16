import { Bot, CheckCircle2, Circle, CircleAlert, CircleDashed, Download, Loader2, Pause, Play, RefreshCw, XCircle } from "lucide-react";
import { useState } from "react";
import { useAppState } from "../../state/AppState";
import { useOrchestrator } from "../../state/OrchestratorState";
import type { PlanStep, PlanStepStatus } from "../../types";
import { Banner, Button, Card, StatusBadge, type Tone } from "../shared/Shared";
import { ActionCard } from "./ActionCard";

const STATUS: Record<PlanStepStatus, { label: string; tone: Tone; icon: typeof Circle }> = {
  done: { label: "Done", tone: "success", icon: CheckCircle2 }, ready: { label: "To do", tone: "info", icon: Circle },
  waiting: { label: "Waiting", tone: "plain", icon: CircleDashed }, in_progress: { label: "In progress", tone: "info", icon: Loader2 },
  failed: { label: "Needs attention", tone: "error", icon: XCircle }, warn: { label: "Check", tone: "warning", icon: CircleAlert },
  skipped: { label: "Not needed", tone: "plain", icon: CircleDashed },
};
const OWNER: Record<PlanStep["owner"], string> = { person: "A person", agent: "The agent", system: "The accelerator" };
const AUTONOMY: Record<string, string> = { observe: "Observe: it reports, and proposes nothing", suggest: "Suggest: it proposes, a person decides",
  act_dev: "Act on Dev: it resumes and retries passing failures on its own, on a development workspace" };

function download(out: { url?: string; text?: string; fileName: string }, type: string) {
  const a = document.createElement("a");
  if (out.url) a.href = out.url;
  else if (out.text !== undefined) a.href = URL.createObjectURL(new Blob([out.text], { type }));
  a.download = out.fileName;
  a.click();
}

/** One step: where it stands, who owns it, the brief answers it came from, and what a person can do about it. */
function StepRow({ step }: { step: PlanStep }) {
  const { mark, busy } = useOrchestrator();
  const [note, setNote] = useState("");
  const s = STATUS[step.status];
  const Icon = s.icon;
  const personTask = step.owner === "person" && step.kind === "task";
  // A check the accelerator cannot tell (it only warns) is confirmed by hand; one it sees failing is fixed instead.
  const confirmable = step.kind === "check" && step.status === "warn";
  return (
    <li className={`plan-step status-${step.status}`} aria-label={`${step.title}: ${s.label}`}>
      <div className="row" style={{ gap: 8, alignItems: "flex-start" }}>
        <Icon size={16} aria-hidden="true" className="plan-step-icon" />
        <div style={{ flex: 1, minWidth: 0 }}>
          <strong>{step.title}</strong>
          <div className="faint">{OWNER[step.owner]}{step.answers.length ? ` · from the brief: ${step.answers.join(", ")}` : ""}</div>
          {step.message && <div className="muted">{step.message}</div>}
          {step.detail && <div className="faint">{step.detail}</div>}
          {Array.isArray(step.params.objects) && (step.params.objects as string[]).length > 0 && (
            <div className="faint">Objects: {(step.params.objects as string[]).slice(0, 8).join(", ")}{(step.params.objects as string[]).length > 8 ? "…" : ""}</div>
          )}
          {step.markedBy && <div className="faint">Marked done by {step.markedBy.id}.</div>}
        </div>
        <StatusBadge tone={s.tone}>{s.label}</StatusBadge>
      </div>
      {(personTask || confirmable) && step.status !== "done" && step.status !== "waiting" && (
        <div className="row" style={{ marginTop: 6, gap: 6 }}>
          <input className="input" style={{ flex: 1, minWidth: 180 }} aria-label={`Note for ${step.title}`} placeholder="Note (optional)" value={note}
            maxLength={500} onChange={(e) => setNote(e.target.value)} />
          <Button size="small" loading={busy === `mark:${step.key}`} onClick={() => void mark(step.key, true, note.trim() || undefined)}>
            {confirmable ? "Confirm by hand" : "Mark done"}
          </Button>
        </div>
      )}
      {(personTask || step.kind === "check") && step.status === "done" && step.markedBy && (
        <Button size="small" variant="ghost" onClick={() => void mark(step.key, false)}>{personTask ? "Mark not done" : "Undo the confirmation"}</Button>
      )}
      {step.actions.map((a) => <ActionCard key={a.id} action={a} compact />)}
    </li>
  );
}

/** The execution plan: every step from readiness to the sign-off report, the agent's requests, and the kill switch. */
export function PlanBoard() {
  const { api } = useAppState();
  const { plan, error, actionError, clearActionError, control, busy } = useOrchestrator();
  if (error) return <Banner tone="warning" title="The execution plan cannot be read yet">{error}</Banner>;
  if (!plan) return <p className="muted" role="status"><span className="spinner" aria-hidden="true" /> Reading the execution plan…</p>;
  const agent = plan.agent;
  const done = plan.steps.filter((s) => s.status === "done").length;
  const report = async (format: "md" | "json") => download(await api.exportSignoffReport(format), format === "md" ? "text/markdown" : "application/json");
  const loose = plan.actions.filter((a) => !a.step || !plan.steps.some((s) => s.key === a.step));
  return (
    <div className="stack">
      <Card eyebrow="Execution plan" title={`Version ${plan.version}: ${done} of ${plan.steps.length} steps done`}
        subtitle={`Built from brief ${plan.inputs.briefVersion ? `v${plan.inputs.briefVersion}` : "(none approved)"}, plan ${plan.inputs.planVersion ?? "(none recorded)"} and discovery ${plan.inputs.snapshot ?? "—"}. The same inputs always give the same plan.`}
        actions={<div className="row" style={{ gap: 6 }}>
          <Button size="small" variant="ghost" onClick={() => void report("md")}><Download size={13} aria-hidden="true" />Sign-off report</Button>
          <Button size="small" variant="ghost" onClick={() => void report("json")}>JSON</Button>
        </div>}>
        <div className="agent-strip row" style={{ gap: 8, flexWrap: "wrap" }} aria-label="The agent">
          <Bot size={15} aria-hidden="true" />
          <span><strong>The agent</strong>: {!agent.on ? "switched off on this server (MIGRATION_AGENT=off)" : agent.paused ? `paused by ${agent.pausedBy?.id ?? "a person"}` : AUTONOMY[agent.autonomy] ?? agent.autonomy}
            {agent.on && !agent.paused && agent.autonomy === "act_dev" && !agent.actsAlone
              ? ` (here it asks for everything: ${agent.aloneWhyNot ?? "this is not the development workspace the brief was approved for"})` : ""}.</span>
          <span className="spacer" />
          {agent.on && (agent.paused
            ? <Button size="small" onClick={() => void control("resume")} loading={busy === "agent:resume"}><Play size={13} aria-hidden="true" />Resume the agent</Button>
            : <Button size="small" onClick={() => void control("pause")} loading={busy === "agent:pause"}><Pause size={13} aria-hidden="true" />Pause the agent</Button>)}
          <Button size="small" variant="ghost" onClick={() => void control("wake")} loading={busy === "agent:wake"}><RefreshCw size={13} aria-hidden="true" />Check now</Button>
        </div>
        <p className="faint" style={{ margin: 0 }}>It never approves anything, accepts a draft or enters a credential: those are a person's. Everything it does is in the audit trail, as its own.
          {agent.lastWake ? ` It last looked at ${new Date(agent.lastWake.at).toLocaleTimeString()} (${agent.lastWake.trigger}).` : ""}</p>
        {actionError && <Banner tone="error" title="That did not work" actions={<Button size="small" variant="ghost" onClick={clearActionError}>Dismiss</Button>}>{actionError}</Banner>}
      </Card>
      {loose.length > 0 && (
        <Card eyebrow="Waiting for you" title={`${loose.length} request${loose.length === 1 ? "" : "s"}`}>
          {loose.map((a) => <ActionCard key={a.id} action={a} />)}
        </Card>
      )}
      {plan.phases.map((phase) => {
        const steps = plan.steps.filter((s) => s.phase === phase.id);
        if (!steps.length) return null;
        return (
          <section key={phase.id} className="stack plan-phase" aria-label={phase.title}>
            <h3 className="eyebrow" style={{ margin: 0 }}>{phase.title}</h3>
            <ul className="plan-steps">{steps.map((s) => <StepRow key={s.key} step={s} />)}</ul>
          </section>
        );
      })}
    </div>
  );
}
