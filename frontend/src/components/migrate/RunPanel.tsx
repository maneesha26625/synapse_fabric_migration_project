import { CheckCircle2, CircleAlert, Pause, Play, RotateCcw, ScrollText, ShieldCheck, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { MetricCard, ProgressCard } from "../shared/Metrics";
import { Banner, Button, Card, EmptyState, Modal, StatusBadge, type Tone } from "../shared/Shared";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import type { ExecItem } from "../../types";
import { STRATEGY_TONE } from "./StrategyPanel";

const TONE: Record<ExecItem["status"], Tone> = { PENDING: "neutral", "IN PROGRESS": "info", COMPLETED: "success", FAILED: "error", SKIPPED: "neutral", DEFERRED: "warning" };
const time = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString() : "—");

type WaveState = "done" | "active" | "failed" | "pending";
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
          <span><strong>Plan has no blocking risks</strong><span className="muted"> — {analysis.blocking ? `${analysis.blocking} to resolve in the risks above` : "dependency order and object content checked"}</span></span>
        </li>
      </ul>
    </Card>
  );
}

/** Run the migration: controls, one step per wave, and the state of every object. */
export function RunPanel() {
  const { mode } = useAppState();
  const navigate = useNavigate();
  const { execution: run, executionError, startExecution, controlExecution, plan, fabric, analysis, options } = useMigration();
  const [logs, setLogs] = useState(false);
  const [view, setView] = useState<"attention" | "done" | "all">("all");

  const running = run.state === "running";
  const started = run.state !== "idle";
  const settled = run.completed + run.failed + (run.skipped ?? 0) + (run.deferred ?? 0);
  const percent = run.total ? (settled / run.total) * 100 : 0;
  const blocked = (analysis?.blocking ?? 0) > 0;
  const ready = plan.length > 0 && fabric.status === "connected" && !blocked && analysis !== null;
  const runIds = new Set(run.items.map((i) => i.id));
  const planChanged = started && plan.some((p) => !runIds.has(p.id)) && options.scope === "all";

  const steps = useMemo(() => {
    const byWave = new Map<number, ExecItem[]>();
    for (const i of run.items) byWave.set(i.wave ?? 1, [...(byWave.get(i.wave ?? 1) ?? []), i]);
    return [...byWave.entries()].sort((a, b) => a[0] - b[0]).map(([wave, items]) => {
      const done = items.filter((i) => finished(i.status)).length;
      const state: WaveState = items.some((i) => i.status === "FAILED") ? "failed" : done === items.length ? "done" : items.some((i) => i.status === "IN PROGRESS") || done > 0 ? "active" : "pending";
      return { wave, items, done, state, types: [...new Set(items.map((i) => i.type))] };
    });
  }, [run.items]);

  const rows = run.items.filter((i) => view === "all" || (view === "attention" ? i.status === "FAILED" || i.status === "DEFERRED" : i.status === "COMPLETED" || i.status === "SKIPPED"));
  const attention = run.items.filter((i) => i.status === "FAILED").length;

  return (
    <>
      <Card eyebrow="Run the migration" title={started ? `Migration Run #${run.runId}` : "Migration run"}
        subtitle={started && (run.workspace || run.warehouse) ? `Fabric workspace ${run.workspace ?? "—"} · Warehouse ${run.warehouse ?? "—"}` : "Each wave is a step; each step's objects are created in dependency order."}
        actions={started ? <StatusBadge tone={run.state === "completed" ? (run.failed ? "warning" : "success") : run.state === "paused" ? "warning" : "info"} running={running}>{run.state.toUpperCase()}</StatusBadge> : undefined}>
        {mode === "real" && <Banner tone="info" title="What a run does">Each stage you switched on is carried out in dependency order: Warehouse and schema, table data, Spark, notebooks, connections, pipelines, jobs, scripts, schedules and shortcuts. Nothing already in Fabric is overwritten, so a run can be repeated safely.{fabric.method === "fabric_cli" && " With the Fabric CLI the first SQL object opens one Microsoft sign-in window on the machine running the server."}</Banner>}
        {executionError && <Banner tone="error" title="Execution">{executionError}</Banner>}
        {run.haltedReason && run.state === "paused" && <Banner tone="warning" title="Run stopped at a wave boundary">{run.haltedReason}</Banner>}
        {planChanged && run.state === "completed" && <Banner tone="info" title="The plan has changed since this run">Press Start new run to migrate the current plan.</Banner>}
        {!ready && !started && (
          <Banner tone="warning" title="Not ready to start">
            {plan.length === 0 ? "The migration plan is empty. " : ""}
            {fabric.status !== "connected" ? "The Fabric target is not connected. " : ""}
            {blocked ? "The planner found blocking risks. " : ""}
            {fabric.status !== "connected" && <button type="button" className="link" onClick={() => navigate("/fabric")}>Open Fabric Target</button>}
          </Banner>
        )}
        <div className="row">
          <Button variant="primary" onClick={() => void startExecution()} disabled={!ready || running || (started && run.state !== "completed")}><Play size={14} aria-hidden="true" />{run.state === "completed" ? "Start new run" : "Start migration"}</Button>
          {run.state === "paused" ? (
            <Button onClick={() => void controlExecution("resume")}><Play size={14} aria-hidden="true" />Resume</Button>
          ) : (
            <Button onClick={() => void controlExecution("pause")} disabled={!running}><Pause size={14} aria-hidden="true" />Pause</Button>
          )}
          <Button onClick={() => void controlExecution("retry")} disabled={!run.failed || running}><RotateCcw size={14} aria-hidden="true" />Retry failed</Button>
          <Button onClick={() => setLogs(true)} disabled={!started}><ScrollText size={14} aria-hidden="true" />View logs</Button>
          <span className="spacer" />
          <span className="muted">{options.stages.length} stage{options.stages.length === 1 ? "" : "s"} on · {options.scope === "automated" ? "automated objects only" : "everything in the plan"}{options.stopOnFailure ? " · stop on failure" : ""}</span>
        </div>
      </Card>

      {!started ? (
        <Card><EmptyState icon={<Play size={22} />} title="Nothing is running.">Start the migration to see each wave's progress and every object's result.</EmptyState></Card>
      ) : (
        <>
          <ProgressCard title="Overall progress" percent={percent} tone={run.state === "completed" ? (run.failed ? "error" : "success") : "info"} />
          <div className="grid cols-6">
            <MetricCard label="Total" value={run.total} />
            <MetricCard label="Migrated" value={run.completed} />
            <MetricCard label="Skipped" value={run.skipped ?? 0} hint="Already in Fabric" />
            <MetricCard label="Deferred" value={run.deferred ?? 0} hint="Later session" />
            <MetricCard label="Failed" value={<span className={run.failed ? "tone-error" : undefined}>{run.failed}</span>} />
            <MetricCard label="Pending" value={run.pending + run.inProgress} />
          </div>

          <div className="steps">
            {steps.map((s, i) => (
              <section key={s.wave} className={`step step-${s.state}`} aria-label={`Wave ${s.wave}`}>
                <header>
                  <span className="step-num">{i + 1}</span>
                  <div>
                    <h3>Wave {s.wave}</h3>
                    <p className="muted">{s.types.join(" · ")}</p>
                  </div>
                  <span className="spacer" />
                  <StatusBadge tone={s.state === "done" ? "success" : s.state === "failed" ? "error" : s.state === "active" ? "info" : "neutral"} running={s.state === "active" && running}>
                    {s.state === "done" ? "DONE" : s.state === "failed" ? "NEEDS ATTENTION" : s.state === "active" ? "RUNNING" : "WAITING"}
                  </StatusBadge>
                </header>
                <div className="bar" role="progressbar" aria-valuenow={Math.round((s.done / s.items.length) * 100)} aria-valuemin={0} aria-valuemax={100} aria-label={`Wave ${s.wave} progress`}>
                  <span className={`fill ${s.state === "failed" ? "error" : s.state === "done" ? "success" : "info"}`} style={{ width: `${(s.done / s.items.length) * 100}%` }} />
                </div>
                <p className="faint" style={{ margin: "6px 0 0" }}>{s.done} of {s.items.length} finished</p>
              </section>
            ))}
          </div>

          <Card title="Migration state" subtitle={`${run.items.length} object${run.items.length === 1 ? "" : "s"}. What each one did, and why.`}
            actions={
              <div className="tabs tabs-pill" role="tablist" aria-label="Filter objects">
                {([["attention", `Needs attention (${attention + (run.deferred ?? 0)})`], ["done", `Done (${run.completed + (run.skipped ?? 0)})`], ["all", `All (${run.items.length})`]] as const).map(([id, label]) => (
                  <button key={id} type="button" role="tab" className="tab" aria-selected={view === id} onClick={() => setView(id)}>{label}</button>
                ))}
              </div>
            }>
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 1000 }}>
                <caption className="sr-only">Migration state by object</caption>
                <thead><tr>{["Object", "Type", "Wave", "Strategy", "Status", "Result", "Details", "Completed"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
                <tbody>
                  {rows.slice(0, 500).map((i) => {
                    const st = analysis?.objectStrategies[i.id];
                    return (
                      <tr key={i.id}>
                        <td className="name" title={i.name}>{i.name}</td>
                        <td>{i.type}</td>
                        <td className="num">{i.wave ?? "—"}</td>
                        <td>{st ? <StatusBadge tone={STRATEGY_TONE[st.strategy]}>{st.label}</StatusBadge> : "—"}</td>
                        <td><StatusBadge tone={TONE[i.status]} running={i.status === "IN PROGRESS"}>{i.status}</StatusBadge></td>
                        <td>{i.step}{i.target ? <div className="faint">{i.target}</div> : null}</td>
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
          {run.state === "completed" && !run.failed && (
            <Banner tone="success" title="Run complete">Next: validate the result against the source on the Validation page. <button type="button" className="link" onClick={() => navigate("/validate")}><ShieldCheck size={13} aria-hidden="true" /> Open Validation</button></Banner>
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
