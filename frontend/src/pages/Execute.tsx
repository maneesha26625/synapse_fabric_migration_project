import { Pause, Play, RotateCcw, ScrollText } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { MetricCard, ProgressCard } from "../components/shared/Metrics";
import { Banner, Button, Card, EmptyState, Modal, PageHead, StatusBadge, type Tone } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";
import type { ExecItem } from "../types";

const TONE: Record<ExecItem["status"], Tone> = { PENDING: "neutral", "IN PROGRESS": "info", COMPLETED: "success", FAILED: "error", SKIPPED: "neutral", DEFERRED: "warning" };
const time = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString() : "—");

export function Execute() {
  const { mode } = useAppState();
  const { execution: run, executionError, startExecution, controlExecution, plan, fabric } = useMigration();
  const navigate = useNavigate();
  const [logs, setLogs] = useState(false);

  const running = run.state === "running";
  const started = run.state !== "idle";
  const finished = run.completed + run.failed + (run.skipped ?? 0) + (run.deferred ?? 0);
  const percent = run.total ? (finished / run.total) * 100 : 0;
  const noCapacity = mode === "real" && fabric.status === "connected" && fabric.capacityAssigned === false;
  const ready = plan.length > 0 && fabric.status === "connected" && !noCapacity;
  // The run on screen may come from an earlier plan; say so instead of looking stale.
  const runIds = new Set(run.items.map((i) => i.id));
  const planChanged = started && (plan.length !== run.items.length || plan.some((p) => !runIds.has(p.id)));

  return (
    <div className="page wide">
      <PageHead icon={Play} title="Migration Execution" badge={mode === "mock" ? <StatusBadge tone="warning">DEMO DATA</StatusBadge> : undefined}>
        Run the migration plan wave by wave and follow each object's progress.
      </PageHead>

      <Banner tone="info" title="What this run migrates">
        Notebooks become Fabric notebooks. The SQL pool becomes a Fabric Warehouse of the same name, and its schemas, tables, views and stored procedures are created in it; tables are created empty, data moves in a later session. Everything else in the plan is marked Deferred, with the reason. Anything that already exists in Fabric is skipped, never overwritten.
        {fabric.method === "fabric_cli" && " Signed in with the Fabric CLI: the first SQL object opens one Microsoft sign-in window on the machine running the server, because the Fabric CLI cannot sign in to the Warehouse's SQL endpoint."}
      </Banner>
      {noCapacity && (
        <Banner tone="error" title="This Fabric workspace is not on a Fabric capacity">
          Fabric cannot create notebooks or warehouses in it. In Fabric, open the workspace settings, choose License info, and assign a Fabric or Trial capacity. Then run Test Connection again on Fabric Target.{" "}
          <button type="button" className="link" onClick={() => navigate("/fabric")}>Open Fabric Target</button>
        </Banner>
      )}
      {planChanged && run.state === "completed" && (
        <Banner tone="info" title="The plan has changed since this run">
          This run covered {run.items.length.toLocaleString()} objects; the plan now has {plan.length.toLocaleString()}. Press Start new run to migrate the current plan.
        </Banner>
      )}
      {executionError && <Banner tone="error" title="Execution">{executionError}</Banner>}
      {!ready && !started && (
        <Banner tone="warning" title="Not ready to start">
          {plan.length === 0 ? "The migration plan is empty. " : ""}{fabric.status !== "connected" ? "The Fabric target is not connected. " : ""}
          <button type="button" className="link" onClick={() => navigate(plan.length === 0 ? "/plan" : "/fabric")}>{plan.length === 0 ? "Open Migration Plan" : "Open Fabric Target"}</button>
        </Banner>
      )}

      <Card>
        <div className="row">
          <div>
            <h2>{started ? `Migration Run #${run.runId}` : "No migration run yet"}</h2>
            {started && (run.workspace || run.warehouse) && <p className="muted" style={{ margin: "2px 0 0" }}>Fabric workspace {run.workspace ?? "—"} · Warehouse {run.warehouse ?? "—"}</p>}
          </div>
          {started && <StatusBadge tone={run.state === "completed" ? (run.failed ? "warning" : "success") : run.state === "paused" ? "warning" : "info"} running={running}>{run.state.toUpperCase()}</StatusBadge>}
          <span className="spacer" />
          <Button variant="primary" onClick={() => void startExecution()} disabled={!ready || running || (started && run.state !== "completed")}><Play size={14} aria-hidden="true" />{run.state === "completed" ? "Start new run" : "Start"}</Button>
          {run.state === "paused" ? (
            <Button onClick={() => void controlExecution("resume")}><Play size={14} aria-hidden="true" />Resume</Button>
          ) : (
            <Button onClick={() => void controlExecution("pause")} disabled={!running}><Pause size={14} aria-hidden="true" />Pause</Button>
          )}
          <Button onClick={() => void controlExecution("retry")} disabled={!run.failed}><RotateCcw size={14} aria-hidden="true" />Retry Failed</Button>
          <Button onClick={() => setLogs(true)} disabled={!started}><ScrollText size={14} aria-hidden="true" />View Logs</Button>
        </div>
      </Card>

      {!started ? (
        <Card><EmptyState icon={<Play size={22} />} title="Nothing is running.">Start a run to see overall progress and each object's step.</EmptyState></Card>
      ) : (
        <>
          <ProgressCard title="Overall progress" percent={percent} tone={run.state === "completed" ? (run.failed ? "error" : "success") : "info"} />
          <div className="grid cols-6">
            <MetricCard label="Total" value={run.total} />
            <MetricCard label="Migrated" value={run.completed} />
            <MetricCard label="Skipped" value={run.skipped ?? 0} hint="Already in Fabric" />
            <MetricCard label="Deferred" value={run.deferred ?? 0} hint="Later session" />
            <MetricCard label="Failed" value={run.failed} />
            <MetricCard label="Pending" value={run.pending + run.inProgress} />
          </div>
          <Card title="Objects">
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 960 }}>
                <caption className="sr-only">Migration progress by object</caption>
                <thead><tr>{["Object", "Type", "Status", "Result", "Details", "Completed"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
                <tbody>
                  {run.items.slice(0, 500).map((i) => (
                    <tr key={i.id}>
                      <td className="name" title={i.name}>{i.name}</td>
                      <td>{i.type}</td>
                      <td><StatusBadge tone={TONE[i.status]} running={i.status === "IN PROGRESS"}>{i.status}</StatusBadge></td>
                      <td>{i.step}{i.target ? <div className="faint">{i.target}</div> : null}</td>
                      <td className="muted" style={{ whiteSpace: "normal", maxWidth: 380 }}>{i.error ?? (i.notes && i.notes.length ? i.notes.join(" ") : "—")}</td>
                      <td className="muted">{time(i.completedAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {run.items.length > 500 && <p className="faint">Showing the first 500 of {run.items.length.toLocaleString()} objects.</p>}
          </Card>
        </>
      )}

      {logs && (
        <Modal title={`Run #${run.runId} log`} onClose={() => setLogs(false)} footer={<Button onClick={() => setLogs(false)}>Close</Button>}>
          <pre className="json" style={{ maxHeight: 360 }} tabIndex={0}>{run.logs.length ? run.logs.join("\n") : "No log lines yet."}</pre>
        </Modal>
      )}
    </div>
  );
}
