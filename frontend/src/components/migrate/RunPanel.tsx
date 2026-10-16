import { CheckCircle2, CircleAlert, GitPullRequest, Pause, Play, RotateCcw, ScrollText, ShieldCheck, XCircle } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { DeviationDialog } from "../consult/DeviationDialog";
import { MetricCard } from "../shared/Metrics";
import { Banner, Button, Card, EmptyState, Modal, StatusBadge, TextField, type Tone } from "../shared/Shared";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import type { ExecItem, ExecutionRun } from "../../types";
import { STRATEGY_TONE } from "./StrategyPanel";
import { LlmBadge, useAcceptedTransforms } from "../discovery/LlmBadge";

export const TONE: Record<ExecItem["status"], Tone> = { PENDING: "neutral", "IN PROGRESS": "info", COMPLETED: "success", FAILED: "error", SKIPPED: "neutral", DEFERRED: "warning", BLOCKED: "warning" };
const time = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString() : "—");

type WaveState = "done" | "active" | "failed" | "pending";

// What the object table shows: one of its tabs, or the objects a summary card counts.
type View = "attention" | "done" | "all" | "migrated" | "skipped" | "deferred" | "failed" | "pending";
const VIEW_STATUSES: Record<View, ExecItem["status"][] | null> = {
  all: null,
  attention: ["FAILED", "BLOCKED", "DEFERRED"],
  done: ["COMPLETED", "SKIPPED"],
  migrated: ["COMPLETED"],
  skipped: ["SKIPPED"],
  deferred: ["DEFERRED"],
  failed: ["FAILED", "BLOCKED"],
  pending: ["PENDING", "IN PROGRESS"],
};
const CARD_LABEL: Partial<Record<View, string>> = {
  migrated: "Migrated", skipped: "Skipped", deferred: "Left for a person", failed: "Failed", pending: "Pending",
};
const finished = (s: ExecItem["status"]) => s === "COMPLETED" || s === "SKIPPED" || s === "DEFERRED";

/** Checks before anything is touched: the target, the capacity, the SQL driver. */
export function Preflight() {
  const { analysis } = useMigration();
  if (!analysis) return null;
  const icon = (s: string) => s === "ok" ? <CheckCircle2 size={15} aria-label="passed" /> : s === "warn" ? <CircleAlert size={15} aria-label="warning" /> : <XCircle size={15} aria-label="failed" />;
  const failed = analysis.checks.filter((c) => c.status === "fail").length;
  return (
    <Card eyebrow="Pre-run validation" title="Is everything ready?" actions={<StatusBadge tone={failed ? "error" : "success"}>{failed ? `${failed} FAILED` : "READY"}</StatusBadge>}>
      <ul className="checks" style={{ listStyle: "none", padding: 0, margin: 0 }} aria-label="Pre-run checks">
        {analysis.checks.map((c) => (
          <li key={c.label} className={`check ${c.status === "ok" ? "ok" : c.status === "warn" ? "skipped" : "failed"}`}>
            {icon(c.status)}
            <span><strong>{c.label}</strong>{c.detail ? <span className="muted"> — {c.detail}</span> : null}</span>
          </li>
        ))}
        <li className={`check ${analysis.blocking ? "failed" : "ok"}`}>
          {analysis.blocking ? <XCircle size={15} aria-label="failed" /> : <CheckCircle2 size={15} aria-label="passed" />}
          <span><strong>Plan has no blocking risks</strong><span className="muted"> — {analysis.blocking ? `${analysis.blocking} to resolve in Plan, under Readiness & risks` : "dependency order and object content checked"}</span></span>
        </li>
      </ul>
    </Card>
  );
}

/**
 * The whole run in one bar: green for done (or already there), amber for left
 * for a person, red for failed, grey for still to come. A run with a handful
 * of failures reads as mostly green, not as a red bar.
 */
function RunProgress({ done, deferred, failed, total }: { done: number; deferred: number; failed: number; total: number }) {
  const rest = Math.max(0, total - done - deferred - failed);
  const settled = total ? Math.round(((done + deferred + failed) / total) * 100) : 0;
  return (
    <Card>
      <div className="run-progress-head"><strong>Overall progress</strong><span className="muted">{settled}% · {(total - rest).toLocaleString()} of {total.toLocaleString()} objects handled</span></div>
      <div className="seg-bar" role="progressbar" aria-label="Overall progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={settled}>
        {done > 0 && <span className="seg ok" style={{ flexGrow: done }} />}
        {deferred > 0 && <span className="seg warn" style={{ flexGrow: deferred }} />}
        {failed > 0 && <span className="seg bad" style={{ flexGrow: failed }} />}
        {rest > 0 && <span className="seg rest" style={{ flexGrow: rest }} />}
      </div>
    </Card>
  );
}

const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

/**
 * What the commit to Fabric Git did once the run completed: the commit and the pull request
 * to open, or what stopped it and a way to commit again. A commit problem never fails the run.
 */
function GitRunBanner() {
  const { execution: run, options, fabricGit, fabricGitError, commitFabricGit } = useMigration();
  if (run.state !== "completed") return null;
  const git = run.git ?? null;
  const off = (run.options?.gitCommit ?? options.gitCommit) === "off";
  if (!git && !off) return null; // a backend without Fabric Git says nothing about it
  const committing = git?.state === "committing" || !!fabricGit?.committing;
  const branch = git?.branch ?? fabricGit?.branch ?? "the branch";
  const target = fabricGit?.targetBranch ?? "main";
  const pr = git?.pullRequestUrl ?? fabricGit?.pullRequestUrl ?? null;
  const prLink = pr && (
    <a className="btn small" href={pr} target="_blank" rel="noopener noreferrer"><GitPullRequest size={14} aria-hidden="true" />Open pull request {branch} → {target}</a>
  );
  const commitNow = <Button size="small" onClick={() => void commitFabricGit()} disabled={committing}>Commit now</Button>;
  const error = fabricGitError && <div className="faint" style={{ marginTop: 4 }}>{fabricGitError}</div>;

  if (committing) {
    return <Banner tone="info" title="Committing to Fabric Git">Committing what the run created to {branch}…</Banner>;
  }
  if (git?.state === "committed") {
    const left = git.skipped?.length ?? 0;
    return (
      <Banner tone="success" title={`Committed ${plural(git.items ?? 0, "item")} to ${branch}${git.commit ? ` (${git.commit.slice(0, 7)})` : ""}`}
        actions={<>{prLink}{git.commitUrl && <a className="btn small ghost" href={git.commitUrl} target="_blank" rel="noopener noreferrer">View commit</a>}</>}>
        {left > 0 && `Left out ${plural(left, "data-load pipeline")}: they are rebuilt per environment. `}
        Review and merge them with a pull request from {branch} to {target}.
        {error}
      </Banner>
    );
  }
  if (git?.state === "nothing") {
    return (
      <Banner tone="info" title="Nothing new to commit to Fabric Git" actions={prLink || undefined}>
        {git.message ?? `${branch} already has everything this run created.`}{error}
      </Banner>
    );
  }
  if (git) {
    const title = git.state === "skipped" ? "Not committed: the workspace is not connected to Git"
      : git.state === "failed" ? "The commit to Fabric Git failed" : "Not committed to Fabric Git yet";
    return (
      <Banner tone="warning" title={title} actions={<>{commitNow}{prLink}</>}>
        {git.message}{git.state === "failed" ? " The migration itself is complete." : ""}{error}
      </Banner>
    );
  }
  // Committing was switched off for this run: say what waits, and commit on request.
  const waiting = fabricGit?.state === "connected" && fabricGit.uncommitted ? ` ${plural(fabricGit.uncommitted, "item")} wait to be committed to ${branch}.` : "";
  return (
    <Banner tone="info" title="Not committed to Fabric Git" actions={fabricGit?.state === "connected" ? <>{commitNow}{prLink}</> : undefined}>
      Commit to Fabric Git was off for this run.{waiting}
      {fabricGit?.state === "not_connected" && " The target workspace is not connected to Git."}{error}
    </Banner>
  );
}

/** How a run's status reads on its badge. */
export function runBadge(run: Pick<ExecutionRun, "state" | "status" | "failed" | "blocked">): { tone: Tone; label: string } {
  switch (run.status) {
    case "awaiting_approval": return { tone: "warning", label: "WAITING FOR SIGN-OFF" };
    case "queued": return { tone: "info", label: "STARTING" };
    case "completed_with_failures": return { tone: "warning", label: "COMPLETED WITH FAILURES" };
    case "failed": return { tone: "error", label: "FAILED" };
    case "cancelled": return { tone: "neutral", label: "CLOSED" };
    case "rejected": return { tone: "neutral", label: "DECLINED" };
    default: break;
  }
  const tone: Tone = run.state === "completed" ? (run.failed || run.blocked ? "warning" : "success") : run.state === "paused" ? "warning" : "info";
  return { tone, label: run.state.toUpperCase() };
}

/** A run that changes what Fabric already has waits for a person to sign it off before it writes anything. */
function SignOffCard() {
  const { execution: run, approveExecution } = useMigration();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  if (run.status !== "awaiting_approval" || !run.approval) return null;
  const decide = async (decision: "approve" | "reject") => {
    setBusy(decision);
    await approveExecution(decision, comment.trim() || undefined);
    setBusy(null);
  };
  const needs = run.needsInput ?? [];
  return (
    <Banner tone="warning" title="This run waits for a person to sign it off"
      actions={<>
        <Button variant="primary" onClick={() => void decide("approve")} disabled={busy !== null}><ShieldCheck size={14} aria-hidden="true" />{busy === "approve" ? "Signing off…" : "Sign off and run"}</Button>
        <Button onClick={() => void decide("reject")} disabled={busy !== null}>{busy === "reject" ? "Declining…" : "Decline"}</Button>
      </>}>
      <p style={{ margin: "0 0 6px" }}>{run.approval.reason} Nothing has been written to Fabric yet.</p>
      {needs.length > 0 && <p className="muted" style={{ margin: "0 0 6px" }}>The accelerator was restarted since this run was asked for, and credentials are never kept: enter them again for {needs.join(", ")} in Plan, Stages & credentials, then sign off.</p>}
      <TextField label="Comment (kept with the sign-off)" optional value={comment} onChange={(e) => setComment(e.target.value)} maxLength={500} />
      <p className="faint" style={{ margin: "4px 0 0" }}>Asked for by {run.approval.requestedBy?.id ?? "the operator"} under the “{run.approval.policy}” sign-off rule. Your name, the time and the comment are recorded in the audit trail.</p>
    </Banner>
  );
}

/** The brief has a person sign off each wave: the run waits before the next one, and Resume is that sign-off. */
function WaveSignOffCard() {
  const { execution: run, controlExecution } = useMigration();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  if (run.state !== "paused" || run.pausedReason !== "awaiting_wave_signoff") return null;
  const wave = run.waitingWave ?? "next";
  const go = async () => {
    setBusy(true);
    await controlExecution("resume", comment.trim() || undefined);
    setBusy(false);
  };
  return (
    <Banner tone="warning" title={`Wave ${wave} waits for a person to sign it off`}
      actions={<Button variant="primary" onClick={() => void go()} loading={busy}><ShieldCheck size={14} aria-hidden="true" />Sign off wave {wave} and continue</Button>}>
      <p style={{ margin: "0 0 6px" }}>The migration brief has each wave signed off before the next one starts. Check what the waves before it made in Fabric, then sign wave {wave} off.</p>
      <TextField label="Comment (kept with the sign-off)" optional value={comment} onChange={(e) => setComment(e.target.value)} maxLength={500} />
    </Banner>
  );
}

/** A start the approved brief refuses: no brief, a stale one, or a plan that differs from it with no reason. */
function BriefRefusal({ only }: { only: string }) {
  const navigate = useNavigate();
  const { executionError, executionErrorCode, startExecution, analysis } = useMigration();
  const [asking, setAsking] = useState(false);
  if (!executionErrorCode || !["brief_required", "brief_stale", "deviation_unexplained"].includes(executionErrorCode)) return null;
  const openConsult = <Button size="small" onClick={() => navigate("/migration?step=consult")}>Open Consult</Button>;
  if (executionErrorCode !== "deviation_unexplained") {
    return <Banner tone="warning" title={executionErrorCode === "brief_stale" ? "The brief no longer fits the estate" : "No approved migration brief"} actions={openConsult}>{executionError}</Banner>;
  }
  return (
    <>
      <Banner tone="warning" title="The run differs from the approved brief" actions={<Button size="small" variant="primary" onClick={() => setAsking(true)}>Give the reason and start</Button>}>{executionError}</Banner>
      {asking && <DeviationDialog differences={analysis?.brief?.differences ?? []} version={analysis?.brief?.version ?? null} confirmLabel="Start with this reason"
        onConfirm={async (reason) => { const ok = await startExecution(only || undefined, reason); if (ok) setAsking(false); return ok; }} onCancel={() => setAsking(false)} />}
    </>
  );
}

/** Why the run is not working right now, and what carries it on. */
function PausedBanner() {
  const { execution: run } = useMigration();
  if (run.state !== "paused" || run.status === "awaiting_approval" || run.pausedReason === "awaiting_wave_signoff") return null;
  const needs = run.needsInput ?? [];
  if (run.resumable === false) {
    return <Banner tone="warning" title="This run cannot carry on">{run.unresumableReason} Use Start over (top right) to close it; its record stays in the run history.</Banner>;
  }
  if (run.pausedReason === "interrupted") {
    return (
      <Banner tone="warning" title="The accelerator stopped while this run was working">
        Resume carries it on from where it stopped. Anything it was in the middle of making is checked in Fabric first, so nothing is made twice.
        {needs.length > 0 && ` Enter the credentials for ${needs.join(", ")} again in Plan, Stages & credentials first: they are never kept.`}
      </Banner>
    );
  }
  if (needs.length > 0) {
    return <Banner tone="warning" title="Credentials needed again">The accelerator was restarted, and credentials are never kept: enter them for {needs.join(", ")} in Plan, Stages & credentials, then Resume.</Banner>;
  }
  return null;
}

/** The jobs the run started in Fabric (its data pipelines' runs), so each can be found in Fabric's Monitoring hub. */
function ExecutionsList() {
  const { execution: run } = useMigration();
  const jobs = run.executions ?? [];
  if (!jobs.length) return null;
  return (
    <Card title="Started in Fabric" subtitle="Data pipeline runs this migration started. After a restart, a run still going is followed to its end, never started twice.">
      <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 8 }} aria-label="Jobs started in Fabric">
        {jobs.map((j) => (
          <li key={j.id}>
            <StatusBadge tone={j.status === "Completed" ? "success" : j.status === "running" ? "info" : "error"} running={j.status === "running"}>{j.status.toUpperCase()}</StatusBadge>{" "}
            <strong>{j.pipeline ?? j.kind}</strong>
            <span className="muted"> · wave {j.wave ?? "—"} · {(j.tables ?? []).length} table{(j.tables ?? []).length === 1 ? "" : "s"} · started {time(j.startedAt)}{j.finishedAt ? `, finished ${time(j.finishedAt)}` : ""}</span>
            {j.note && <div className="faint">{j.note}</div>}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** Run the migration: what to run, the controls, one card per wave, and the state of every object. */
export function RunPanel() {
  const { mode } = useAppState();
  const navigate = useNavigate();
  const { execution: run, executionError, executionErrorCode, startExecution, controlExecution, plan, fabric, analysis, analysisError, options, capabilities } = useMigration();
  const [logs, setLogs] = useState(false);
  const [view, setView] = useState<View>("all");
  const accepted = useAcceptedTransforms();
  const table = useRef<HTMLDivElement>(null);
  // A card shows the objects it counts, and brings the table into view.
  const showCard = (v: View) => { setView(v); table.current?.scrollIntoView?.({ behavior: "smooth", block: "start" }); };
  const card = (v: View) => ({ onClick: () => showCard(v), active: view === v });
  // Every stage that is on, or just one of them: a single stage can be run (or re-run) on its own.
  const [only, setOnly] = useState("");
  const stagesOn = (capabilities?.stages ?? []).filter((st) => options.stages.includes(st.key));
  const onlyLabel = stagesOn.find((st) => st.key === only)?.label;

  const running = run.state === "running";
  const started = run.state !== "idle";
  const waiting = run.status === "awaiting_approval";
  const needsAttention = run.failed + (run.blocked ?? 0);
  const badge = runBadge(run);
  const blocked = (analysis?.blocking ?? 0) > 0;
  const ready = plan.length > 0 && fabric.status === "connected" && !blocked && analysis !== null && options.stages.length > 0;
  // Only waiting for the plan's score (after a reload, or an edit): not a problem to report.
  const scoring = plan.length > 0 && fabric.status === "connected" && analysis === null && !analysisError;
  const runIds = new Set(run.items.map((i) => i.id));
  const planChanged = started && plan.some((p) => !runIds.has(p.id)) && options.scope === "all";

  const steps = useMemo(() => {
    const byWave = new Map<number, ExecItem[]>();
    for (const i of run.items) byWave.set(i.wave ?? 1, [...(byWave.get(i.wave ?? 1) ?? []), i]);
    return [...byWave.entries()].sort((a, b) => a[0] - b[0]).map(([wave, items]) => {
      const done = items.filter((i) => finished(i.status)).length;
      const state: WaveState = items.some((i) => i.status === "FAILED" || i.status === "BLOCKED") ? "failed" : done === items.length ? "done" : items.some((i) => i.status === "IN PROGRESS") || done > 0 ? "active" : "pending";
      return { wave, items, done, state, types: [...new Set(items.map((i) => i.type))] };
    });
  }, [run.items]);

  const statuses = VIEW_STATUSES[view];
  const rows = statuses ? run.items.filter((i) => statuses.includes(i.status)) : run.items;
  const attention = run.items.filter((i) => i.status === "FAILED" || i.status === "BLOCKED").length;

  return (
    <>
      <Card eyebrow="Run the migration" title={started ? `Migration Run #${run.runId}` : "Migration run"}
        subtitle={started && (run.workspace || run.warehouse) ? `Fabric workspace ${run.workspace ?? "—"} · Warehouse ${run.warehouse ?? "—"}` : "Each wave is a step; each step's objects are created in dependency order."}
        actions={started ? <StatusBadge tone={badge.tone} running={running}>{badge.label}</StatusBadge> : undefined}>
        {mode === "mock" && <Banner tone="info" title="Demo run">Results are simulated the way a real run reports them. A run takes about a minute, and a few objects fail on purpose so you can try Retry failed.</Banner>}
        {mode === "real" && <Banner tone="info" title="What a run does">Each stage you switched on is carried out in dependency order: Warehouse and schema, table data, Spark, notebooks, connections, pipelines, jobs, scripts, schedules and shortcuts. Nothing already in Fabric is overwritten, so a run can be repeated safely.{fabric.method === "fabric_cli" && " With the Fabric CLI the first SQL object opens one Microsoft sign-in window on the machine running the server."}</Banner>}
        {executionError && !["brief_required", "brief_stale", "deviation_unexplained"].includes(executionErrorCode ?? "") && <Banner tone="error" title="Execution">{executionError}</Banner>}
        <BriefRefusal only={only} />
        <SignOffCard />
        <WaveSignOffCard />
        <PausedBanner />
        {run.status === "failed" && <Banner tone="error" title="The run stopped">The workspace refused it, or the accelerator hit a problem of its own: the log says which. Fix the cause, then Retry failed carries it on.</Banner>}
        {run.haltedReason && run.state === "paused" && <Banner tone="warning" title="Run stopped at a wave boundary">{run.haltedReason}</Banner>}
        <GitRunBanner />
        {planChanged && run.state === "completed" && <Banner tone="info" title="The plan has changed since this run">Press Start new run to migrate the current plan.</Banner>}
        {!ready && !started && (scoring ? (
          <p className="muted" role="status"><span className="spinner" aria-hidden="true" /> Checking the plan…</p>
        ) : (
          <Banner tone="warning" title="Not ready to start">
            {plan.length === 0 ? "The migration plan is empty. " : ""}
            {fabric.status !== "connected" ? "The Fabric target is not connected. " : ""}
            {blocked ? "The planner found blocking risks. " : ""}
            {options.stages.length === 0 ? "Every stage is switched off: switch some on in Plan, under Stages & credentials. " : ""}
            {analysisError ? `The plan could not be scored: ${analysisError} ` : ""}
            {fabric.status !== "connected" && <button type="button" className="link" onClick={() => navigate("/")}>Open Connections</button>}
          </Banner>
        ))}
        <div className="row">
          {stagesOn.length > 1 && (
            <select className="select run-target" aria-label="What to run" value={only} onChange={(e) => setOnly(e.target.value)} disabled={running}>
              <option value="">Every stage that is on ({stagesOn.length})</option>
              {stagesOn.map((st) => <option key={st.key} value={st.key}>Only {st.label}</option>)}
            </select>
          )}
          <Button variant="primary" onClick={() => void startExecution(only || undefined)} disabled={!ready || running || (started && run.state !== "completed")}>
            <Play size={14} aria-hidden="true" />{onlyLabel ? `Run ${onlyLabel} only` : run.state === "completed" ? "Start new run" : "Start migration"}
          </Button>
          {run.state === "paused" && !waiting ? (
            <Button onClick={() => void controlExecution("resume")} disabled={run.resumable === false}><Play size={14} aria-hidden="true" />
              {run.pausedReason === "awaiting_wave_signoff" ? `Sign off wave ${run.waitingWave ?? ""}` : "Resume"}</Button>
          ) : (
            <Button onClick={() => void controlExecution("pause")} disabled={!running}><Pause size={14} aria-hidden="true" />Pause</Button>
          )}
          <Button onClick={() => void controlExecution("retry")} disabled={!(needsAttention || run.status === "failed") || running || waiting || run.resumable === false}><RotateCcw size={14} aria-hidden="true" />Retry failed</Button>
          <Button onClick={() => setLogs(true)} disabled={!started}><ScrollText size={14} aria-hidden="true" />View logs</Button>
          <span className="spacer" />
          <span className="muted">{options.stages.length} stage{options.stages.length === 1 ? "" : "s"} on · {options.scope === "automated" ? "automated objects only" : "everything in the plan"}{options.stopOnFailure ? " · stop on failure" : ""}{options.waveGate ? " · each wave signed off" : ""}{options.gitCommit === "off" ? " · no Git commit" : ""}{run.brief?.version ? ` · under brief v${run.brief.version}` : ""} · change in Plan</span>
        </div>
      </Card>

      {(() => {
        const fromLlm = started ? run.items.filter((i) => i.llm).length : plan.filter((p) => accepted.has(p.id)).length;
        return fromLlm > 0 ? (
          <Banner tone="info" title={`${fromLlm} object${fromLlm === 1 ? "" : "s"} ${started ? "were" : "will be"} created from accepted LLM transforms`}>
            Marked <LlmBadge /> below. Everything else uses the rule-based conversion. Review or withdraw them in Assess → LLM transforms.
          </Banner>
        ) : null;
      })()}

      {!started ? (
        <Card><EmptyState icon={<Play size={22} />} title="Nothing is running.">Start the migration to see each wave's progress and every object's result.</EmptyState></Card>
      ) : (
        <>
          <RunProgress done={run.completed + (run.skipped ?? 0)} deferred={run.deferred ?? 0} failed={needsAttention} total={run.total} />
          <div className="grid cols-6">
            <MetricCard label="Total" value={run.total.toLocaleString()} {...card("all")} />
            <MetricCard label="Migrated" value={<span className="tone-success">{run.completed.toLocaleString()}</span>} {...card("migrated")} />
            <MetricCard label="Skipped" value={(run.skipped ?? 0).toLocaleString()} hint="Already in Fabric" {...card("skipped")} />
            <MetricCard label="Left for a person" value={<span className={run.deferred ? "tone-warning" : undefined}>{(run.deferred ?? 0).toLocaleString()}</span>} hint="Set up by hand, or needs input" {...card("deferred")} />
            <MetricCard label="Failed" value={<span className={needsAttention ? "tone-error" : undefined}>{needsAttention.toLocaleString()}</span>}
              hint={run.blocked ? `${run.blocked.toLocaleString()} blocked by what failed` : undefined} {...card("failed")} />
            <MetricCard label="Pending" value={(run.pending + run.inProgress).toLocaleString()} {...card("pending")} />
          </div>
          <div ref={table} />

          <div className="waves">
            {steps.map((s, i) => (
              <section key={s.wave} className={`wave-card wave-${s.state}`} aria-label={`Wave ${s.wave}`}>
                <header>
                  <span className="wave-num">{i + 1}</span>
                  <h3>Wave {s.wave}</h3>
                  <span className="spacer" />
                  <StatusBadge tone={s.state === "done" ? "success" : s.state === "failed" ? "error" : s.state === "active" ? "info" : "neutral"} running={s.state === "active" && running}>
                    {s.state === "done" ? "DONE" : s.state === "failed" ? "NEEDS ATTENTION" : s.state === "active" ? "RUNNING" : "WAITING"}
                  </StatusBadge>
                </header>
                <p className="muted wave-types">{s.types.join(" · ")}</p>
                <div className="bar" role="progressbar" aria-valuenow={Math.round((s.done / s.items.length) * 100)} aria-valuemin={0} aria-valuemax={100} aria-label={`Wave ${s.wave} progress`}>
                  <span className={`fill ${s.state === "failed" ? "error" : s.state === "done" ? "success" : "info"}`} style={{ width: `${(s.done / s.items.length) * 100}%` }} />
                </div>
                <p className="faint">{s.done.toLocaleString()} of {s.items.length.toLocaleString()} finished{s.items.some((x) => x.status === "FAILED") ? ` · ${s.items.filter((x) => x.status === "FAILED").length} failed` : ""}{s.items.some((x) => x.status === "BLOCKED") ? ` · ${s.items.filter((x) => x.status === "BLOCKED").length} blocked` : ""}</p>
              </section>
            ))}
          </div>

          <Card title="Migration state" subtitle={`${run.items.length.toLocaleString()} object${run.items.length === 1 ? "" : "s"}. What each one did, and why.`}
            actions={
              <div className="tabs tabs-pill" role="tablist" aria-label="Filter objects">
                {([["attention", `Needs attention (${(attention + (run.deferred ?? 0)).toLocaleString()})`], ["done", `Done (${(run.completed + (run.skipped ?? 0)).toLocaleString()})`], ["all", `All (${run.items.length.toLocaleString()})`]] as const).map(([id, label]) => (
                  <button key={id} type="button" role="tab" className="tab" aria-selected={view === id} onClick={() => setView(id)}>{label}</button>
                ))}
              </div>
            }>
            {!["all", "attention", "done"].includes(view) && (
              <div className="row"><span className="chip" role="status">{CARD_LABEL[view]}: {rows.length.toLocaleString()}<button type="button" className="chip-x" aria-label="Show every object" onClick={() => setView("all")}>×</button></span></div>
            )}
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 1000 }}>
                <caption className="sr-only">Migration state by object</caption>
                <thead><tr>{["Object", "Type", "Wave", "Strategy", "Status", "Result", "Details", "Completed"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
                <tbody>
                  {rows.slice(0, 500).map((i) => {
                    const st = analysis?.objectStrategies[i.id];
                    return (
                      <tr key={i.id}>
                        <td className="name" title={i.name}>{i.name}{i.llm && <> <LlmBadge edited={i.llm.edited} acceptedAt={i.llm.acceptedAt} /></>}</td>
                        <td>{i.type}</td>
                        <td className="num">{i.wave ?? "—"}</td>
                        <td>{st ? <StatusBadge tone={STRATEGY_TONE[st.strategy]}>{st.label}</StatusBadge> : "—"}</td>
                        <td><StatusBadge tone={TONE[i.status]} running={i.status === "IN PROGRESS"}>{i.status}</StatusBadge></td>
                        <td style={{ whiteSpace: "normal", minWidth: 170, maxWidth: 260 }}>{i.step}{i.target ? <div className="faint">{i.target}</div> : null}</td>
                        <td className="muted" style={{ whiteSpace: "normal", maxWidth: 380 }}>{i.error ?? (i.notes && i.notes.length ? i.notes.join(" ") : "—")}</td>
                        <td className="muted">{time(i.completedAt)}</td>
                      </tr>
                    );
                  })}
                  {rows.length === 0 && <tr><td colSpan={8} className="muted">Nothing in this view.</td></tr>}
                </tbody>
              </table>
            </div>
            {rows.length > 500 && <p className="faint">Showing the first 500 of {rows.length.toLocaleString()} objects.</p>}
          </Card>
          <ExecutionsList />
          {(run.passes?.length ?? 0) > 1 && (
            <p className="faint">{run.passes!.length} passes: {run.passes!.map((x) => `${x.trigger} (${x.status})`).join(", ")}.</p>
          )}
        </>
      )}

      {logs && (
        <Modal title={`Run #${run.runId} log`} onClose={() => setLogs(false)} footer={<Button onClick={() => setLogs(false)}>Close</Button>}>
          <pre className="json" style={{ maxHeight: 360 }} tabIndex={0}>{run.logs.length ? run.logs.join("\n") : "No log lines yet."}</pre>
        </Modal>
      )}
    </>
  );
}
