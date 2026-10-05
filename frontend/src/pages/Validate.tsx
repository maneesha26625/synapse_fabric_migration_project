import { ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { MetricCard } from "../components/shared/Metrics";
import { Banner, Button, Card, EmptyState, PageHead, StatusBadge, type Tone } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";
import type { ValidationStatus } from "../types";

const TONE: Record<ValidationStatus, Tone> = { MATCH: "success", REVIEW: "warning", MISMATCH: "error" };
const MARK: Record<ValidationStatus, string> = { MATCH: "✓", REVIEW: "⚠", MISMATCH: "✕" };
const CATEGORIES = ["Warehouse", "Schema", "Tables", "Columns", "Data Count", "Data Types", "Views", "Stored Procedures", "Spark", "Notebooks", "Connections", "Pipelines", "Spark Jobs", "SQL Scripts", "Schedules", "Shortcuts", "Security", "Dependencies", "Manual"];

export function Validate() {
  const { mode, discovery } = useAppState();
  const { validation, validationError, validationBusy, runValidation, execution, fabric } = useMigration();
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState<ValidationStatus | "">("");
  const rows = useMemo(() => (validation ?? []).filter((r) => (!category || r.category === category) && (!status || r.status === status)), [validation, category, status]);
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const ready = discovered && fabric.status === "connected";
  const why = !discovered ? "Run Discovery first." : fabric.status !== "connected" ? "Connect the Fabric target first." : "";
  const count = (s: ValidationStatus) => (validation ?? []).filter((r) => r.status === s).length;
  const present = new Set((validation ?? []).map((r) => r.category));

  return (
    <div className="page wide">
      <PageHead icon={ShieldCheck} title="Migration Validation" badge={mode === "mock" ? <StatusBadge tone="warning">DEMO DATA</StatusBadge> : undefined}
        actions={<Button variant="primary" onClick={() => void runValidation()} loading={validationBusy} disabled={mode === "real" && !ready} title={why || undefined}>Run Validation</Button>}>
        Compare the source Synapse environment with the migrated Fabric environment, object by object.
      </PageHead>

      {mode === "real" && !ready && <Banner tone="warning" title="Not ready to validate">{why} Validation reads both sides: the discovered Synapse objects and the Fabric workspace.</Banner>}
      {validationError && <Banner tone="error" title="Validation">{validationError}</Banner>}

      {!validation ? (
        <Card>
          <EmptyState icon={<ShieldCheck size={22} />} title="Validation has not been run.">
            {execution.completed ? "Run validation to compare the migrated objects with the source." : "Validation compares objects after they have been migrated. Run a migration first."}
          </EmptyState>
        </Card>
      ) : (
        <>
          <div className="grid cols-4">
            <MetricCard label="Checks" value={validation.length} onClick={() => setStatus("")} active={status === ""} hint="Show every check" />
            <MetricCard label="Match" value={count("MATCH")} onClick={() => setStatus(status === "MATCH" ? "" : "MATCH")} active={status === "MATCH"} hint="Both sides agree" />
            <MetricCard label="Review" value={count("REVIEW")} onClick={() => setStatus(status === "REVIEW" ? "" : "REVIEW")} active={status === "REVIEW"} hint="Differs by design, or could not be checked" />
            <MetricCard label="Mismatch" value={<span className={count("MISMATCH") ? "tone-error" : undefined}>{count("MISMATCH")}</span>} onClick={() => setStatus(status === "MISMATCH" ? "" : "MISMATCH")} active={status === "MISMATCH"} hint="Missing from Fabric, or the two sides disagree" />
          </div>
          <Card title="Source Synapse vs target Fabric">
            <div className="tabs tabs-pill" role="tablist" aria-label="Validation categories" style={{ marginBottom: 12 }}>
              <button role="tab" type="button" className="tab" aria-selected={category === ""} onClick={() => setCategory("")}>All</button>
              {CATEGORIES.filter((c) => present.has(c)).map((c) => (
                <button key={c} role="tab" type="button" className="tab" aria-selected={category === c} onClick={() => setCategory(c)}>{c}</button>
              ))}
            </div>
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 980 }}>
                <caption className="sr-only">Validation results</caption>
                <thead><tr>{["Object", "Category", "Synapse", "Fabric", "Status", "Details"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
                <tbody>
                  {rows.slice(0, 500).map((r, i) => (
                    <tr key={`${r.object}-${r.category}-${i}`}>
                      <td className="name" title={r.object}>{r.object}</td>
                      <td>{r.category}</td>
                      <td>{r.source}</td>
                      <td>{r.target}</td>
                      <td><StatusBadge tone={TONE[r.status]}>{MARK[r.status]} {r.status}</StatusBadge></td>
                      <td className="muted" style={{ whiteSpace: "normal", maxWidth: 420 }}>{r.detail || "—"}</td>
                    </tr>
                  ))}
                  {rows.length === 0 && <tr><td colSpan={6} className="muted">Nothing in this view.</td></tr>}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
