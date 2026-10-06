import { ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { StatStrip } from "../../shared/Metrics";
import { Banner, Button, StatusBadge, type Tone } from "../../shared/Shared";
import { useAppState } from "../../../state/AppState";
import { useMigration } from "../../../state/MigrationState";
import type { ValidationStatus } from "../../../types";

const TONE: Record<ValidationStatus, Tone> = { MATCH: "success", REVIEW: "warning", MISMATCH: "error" };
const MARK: Record<ValidationStatus, string> = { MATCH: "✓", REVIEW: "⚠", MISMATCH: "✕" };
const CATEGORIES = ["Warehouse", "Schema", "Tables", "Columns", "Data Count", "Data Types", "Views", "Stored Procedures", "Spark", "Notebooks", "Connections", "Pipelines", "Spark Jobs", "SQL Scripts", "Schedules", "Shortcuts", "Security", "Dependencies", "Manual"];

export function ValidatePanel() {
  const { mode, discovery } = useAppState();
  const { validation, validationError, validationBusy, runValidation, execution, fabric } = useMigration();
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState<ValidationStatus | "">("");
  const rows = useMemo(() => (validation ?? []).filter((r) => (!category || r.category === category) && (!status || r.status === status)), [validation, category, status]);
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const ready = discovered && fabric.status === "connected";
  const why = !discovered ? "Run discovery first." : fabric.status !== "connected" ? "Connect the Fabric destination first." : "";
  const count = (s: ValidationStatus) => (validation ?? []).filter((r) => r.status === s).length;
  const present = CATEGORIES.filter((c) => (validation ?? []).some((r) => r.category === c));
  const runButton = (
    <Button variant="primary" onClick={() => void runValidation()} loading={validationBusy} disabled={mode === "real" && !ready} title={why || undefined}>
      <ShieldCheck size={14} aria-hidden="true" />{validation ? "Validate again" : "Run validation"}
    </Button>
  );

  if (!validation) {
    return (
      <div className="panel-center">
        <span className="intro-icon" aria-hidden="true"><ShieldCheck size={26} /></span>
        <h3>Compare the source with the destination</h3>
        <p className="muted">Validation reads both sides, object by object: columns and types, row counts, definitions, notebook cells and pipeline activities. It changes nothing.</p>
        {mode === "real" && !ready && <Banner tone="warning" title="Not ready to validate">{why}</Banner>}
        {validationError && <Banner tone="error" title="Validation">{validationError}</Banner>}
        {runButton}
        {!execution.completed && <p className="faint">Nothing has been migrated yet, so most checks would report objects as missing.</p>}
      </div>
    );
  }

  return (
    <div className="stack panel-body">
      <div className="panel-toolbar">
        <span className="muted">Synapse compared with Fabric workspace {fabric.workspaceName ?? "—"}</span>
        <span className="spacer" />
        {runButton}
      </div>
      {validationError && <Banner tone="error" title="Validation">{validationError}</Banner>}

      <div className="filter-stats" role="group" aria-label="Filter by result">
        {([["", "Checks", validation.length, undefined], ["MATCH", "Match", count("MATCH"), "success"], ["REVIEW", "Review", count("REVIEW"), "warning"], ["MISMATCH", "Mismatch", count("MISMATCH"), "error"]] as const).map(([s, label, value, tone]) => (
          <button key={label} type="button" className={`filter-stat${tone ? ` tone-${tone}` : ""}`} aria-pressed={status === s} onClick={() => setStatus(status === s ? "" : s)}>
            <span className="filter-label">{label}</span><span className="filter-value">{value.toLocaleString()}</span>
          </button>
        ))}
      </div>

      <div className="tabs tabs-pill" role="tablist" aria-label="Validation categories">
        <button role="tab" type="button" className="tab" aria-selected={category === ""} onClick={() => setCategory("")}>All</button>
        {present.map((c) => <button key={c} role="tab" type="button" className="tab" aria-selected={category === c} onClick={() => setCategory(c)}>{c}</button>)}
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
      {rows.length > 500 && <p className="faint">Showing the first 500 of {rows.length.toLocaleString()} checks. Filter to narrow them.</p>}
    </div>
  );
}

/** The final summary once every step is confirmed. */
export function CompletionSummary() {
  const { execution, validation } = useMigration();
  return (
    <StatStrip label="Migration result" items={[
      { label: "Migrated", value: execution.completed.toLocaleString(), tone: "success" },
      { label: "Already there", value: (execution.skipped ?? 0).toLocaleString() },
      { label: "Left for a person", value: (execution.deferred ?? 0).toLocaleString() },
      { label: "Failed", value: execution.failed, tone: execution.failed ? "error" : undefined },
      { label: "Validation matches", value: (validation ?? []).filter((r) => r.status === "MATCH").length.toLocaleString() },
    ]} />
  );
}
