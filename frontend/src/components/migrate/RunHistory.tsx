import { History } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import { ApiRequestError, type ExecutionRun, type RunHistory as RunHistoryList, type RunSummary } from "../../types";
import { Banner, Button, Card, Modal, StatusBadge } from "../shared/Shared";
import { TONE, runBadge } from "./RunPanel";

const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
const count = (s: RunSummary, key: string) => (s.counts?.[key] ?? 0).toLocaleString();

/** One closed run, as its record has it: what each object did, and its log. Read-only. */
function RunRecord({ run, onClose }: { run: ExecutionRun; onClose: () => void }) {
  const badge = runBadge(run);
  return (
    <Modal title={`Run #${run.runId}`} onClose={onClose} footer={<Button onClick={onClose}>Close</Button>}>
      <p className="muted" style={{ marginTop: 0 }}>
        <StatusBadge tone={badge.tone}>{badge.label}</StatusBadge>{" "}
        Asked for by {run.requestedBy?.id ?? "—"} · {when(run.createdAt)} · closed {when(run.closedAt)}
        {run.approval && !run.approval.automatic && run.approval.by ? ` · signed off by ${run.approval.by.id}` : ""}
      </p>
      <div className="table-wrap" style={{ maxHeight: 280 }}>
        <table className="data">
          <caption className="sr-only">Objects of run {run.runId}</caption>
          <thead><tr>{["Object", "Type", "Status", "Result"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
          <tbody>
            {run.items.slice(0, 300).map((i) => (
              <tr key={i.id}>
                <td className="name" title={i.name}>{i.name}</td>
                <td>{i.type}</td>
                <td><StatusBadge tone={TONE[i.status] ?? "neutral"}>{i.status}</StatusBadge></td>
                <td className="muted" style={{ whiteSpace: "normal" }}>{i.error ?? i.step}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {run.items.length > 300 && <p className="faint">Showing the first 300 of {run.items.length.toLocaleString()} objects.</p>}
      <pre className="json" style={{ maxHeight: 200 }} tabIndex={0} aria-label={`Run ${run.runId} log`}>{run.logs.length ? run.logs.join("\n") : "No log lines."}</pre>
    </Modal>
  );
}

/** Every run of the project, newest first. A run closed by Start over, or replaced by a new run, is kept here as it was left. */
export function RunHistory() {
  const { api } = useAppState();
  const { execution, projectEpoch } = useMigration();
  const [history, setHistory] = useState<RunHistoryList | null>(null);
  const [open, setOpen] = useState<ExecutionRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api.listRuns().then((h) => { if (live) setHistory(h); }, () => { if (live) setHistory(null); });
    return () => { live = false; };
  }, [api, projectEpoch, execution.runId, execution.status]);
  const past = (history?.runs ?? []).filter((r) => r.runId !== history?.current);
  if (!past.length) return null;
  const view = async (runId: string) => {
    setError(null);
    try { setOpen(await api.getRun(runId)); } catch (e) { setError(e instanceof ApiRequestError || e instanceof Error ? e.message : "The run could not be read."); }
  };
  return (
    <Card eyebrow="Kept for the record" title="Run history" subtitle="Earlier runs of this project. Each record stays as the run left it: what every object did, who asked for it and who signed it off.">
      {error && <Banner tone="error" title="Run history">{error}</Banner>}
      <div className="table-wrap">
        <table className="data">
          <caption className="sr-only">Earlier runs</caption>
          <thead><tr>{["Run", "Status", "Asked for by", "Created", "Closed", "Completed", "Failed", "Passes", ""].map((h) => <th key={h || "view"} scope="col" className="static">{h}</th>)}</tr></thead>
          <tbody>
            {past.map((r) => {
              const badge = runBadge({ state: "completed", status: r.status, failed: r.counts?.FAILED ?? 0, blocked: r.counts?.BLOCKED ?? 0 });
              return (
                <tr key={r.runId}>
                  <td className="num">#{r.runId}</td>
                  <td><StatusBadge tone={badge.tone}>{badge.label}</StatusBadge></td>
                  <td>{r.requestedBy?.id ?? "—"}</td>
                  <td className="muted">{when(r.createdAt)}</td>
                  <td className="muted">{when(r.closedAt)}</td>
                  <td className="num">{count(r, "COMPLETED")}</td>
                  <td className="num">{count(r, "FAILED")}</td>
                  <td className="num">{r.passes}</td>
                  <td><Button size="small" onClick={() => void view(r.runId)}><History size={14} aria-hidden="true" />View</Button></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {open && <RunRecord run={open} onClose={() => setOpen(null)} />}
    </Card>
  );
}
