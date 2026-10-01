import { Pause, Play, RotateCcw, ScrollText } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { MetricCard, ProgressCard } from "../components/shared/Metrics";
import { Banner, Button, Card, EmptyState, Modal, PageHead, StatusBadge, type Tone } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";
import type { ExecItem } from "../types";

const TONE: Record<ExecItem["status"], Tone> = { PENDING: "neutral", "IN PROGRESS": "info", COMPLETED: "success", FAILED: "error" };
const time = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString() : "—");

export function Execute() {
  const { mode } = useAppState();
  const { execution: run, executionError, startExecution, controlExecution, plan, fabric } = useMigration();
  const navigate = useNavigate();
  const [logs, setLogs] = useState(false);

  const running = run.state === "running";
  const started = run.state !== "idle";
  const finished = run.completed + run.failed;
  const percent = run.total ? (finished / run.total) * 100 : 0;
  const ready = plan.length > 0 && fabric.status === "connected";

  return (
    <div className="page wide">
      <PageHead icon={Play} title="Migration Execution" badge={mode === "mock" ? <StatusBadge tone="warning">DEMO DATA</StatusBadge> : undefined}>
        Run the migration plan wave by wave and follow each object's progress.
      </PageHead>

      {mode === "real" && (
        <Banner tone="info" title="Migration execution is not implemented in the backend yet">
          Start will report "not implemented"; no progress is ever shown unless the backend produces it. Switch to Demo data to preview the page with a simulated run.
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
          <h2>{started ? `Migration Run #${run.runId}` : "No migration run yet"}</h2>
          {started && <StatusBadge tone={run.state === "completed" ? (run.failed ? "warning" : "success") : run.state === "paused" ? "warning" : "info"} running={running}>{run.state.toUpperCase()}</StatusBadge>}
          <span className="spacer" />
          <Button variant="primary" onClick={() => void startExecution()} disabled={running || (started && run.state !== "completed")}><Play size={14} aria-hidden="true" />Start</Button>
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
          <div className="grid cols-5">
            <MetricCard label="Total" value={run.total} />
            <MetricCard label="Completed" value={run.completed} />
            <MetricCard label="In progress" value={run.inProgress} />
            <MetricCard label="Failed" value={run.failed} />
            <MetricCard label="Pending" value={run.pending} />
          </div>
          <Card title="Objects">
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 820 }}>
                <caption className="sr-only">Migration progress by object</caption>
                <thead><tr>{["Object", "Migration step", "Status", "Started", "Completed", "Error"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
                <tbody>
                  {run.items.slice(0, 500).map((i) => (
                    <tr key={i.id}>
                      <td className="name" title={i.name}>{i.name}</td>
                      <td>{i.step}</td>
                      <td><StatusBadge tone={TONE[i.status]} running={i.status === "IN PROGRESS"}>{i.status}</StatusBadge></td>
                      <td className="muted">{time(i.startedAt)}</td>
                      <td className="muted">{time(i.completedAt)}</td>
                      <td className="muted">{i.error ?? "—"}</td>
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
