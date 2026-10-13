import { PlayCircle, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { StatStrip } from "../../shared/Metrics";
import { Banner, Button, Collapsible, StatusBadge, type Tone } from "../../shared/Shared";
import { useAppState } from "../../../state/AppState";
import { useMigration } from "../../../state/MigrationState";
import type { RunnableKind, RuntimeStatus, ValidationStatus } from "../../../types";

const TONE: Record<ValidationStatus, Tone> = { MATCH: "success", REVIEW: "warning", MISMATCH: "error" };
const MARK: Record<ValidationStatus, string> = { MATCH: "✓", REVIEW: "⚠", MISMATCH: "✕" };
const RUN_TONE: Record<RuntimeStatus, Tone> = { PASS: "success", FAIL: "error", "NOT RUN": "warning", "NOT CHECKED": "neutral" };
const RUN_LABEL: Record<RuntimeStatus, string> = { PASS: "Runs", FAIL: "Fails", "NOT RUN": "Never run", "NOT CHECKED": "Not checked" };
const CATEGORIES = ["Warehouse", "Schema", "Tables", "Columns", "Data Count", "Data Types", "Functions", "Views", "Stored Procedures", "Spark", "Notebooks", "Connections", "Pipelines", "Spark Jobs", "SQL Scripts", "Schedules", "Shortcuts", "Security", "Dependencies", "Manual"];
const RUNNABLE: { kind: RunnableKind; label: string }[] = [
  { kind: "pipeline", label: "Pipelines" },
  { kind: "notebook", label: "Notebooks" },
  { kind: "script", label: "SQL-script notebooks" },
  { kind: "sparkjob", label: "Spark job definitions" },
  { kind: "procedure", label: "Stored procedures with no required parameters (in a transaction that is rolled back)" },
];

/** What Validate checks in Fabric beyond comparing definitions, and what it may run for real. */
function RuntimeOptionsForm() {
  const { validationRuntime: rt, setValidationRuntime, validationBusy } = useMigration();
  const toggle = (kind: RunnableKind) =>
    setValidationRuntime({ ...rt, run: rt.run.includes(kind) ? rt.run.filter((k) => k !== kind) : [...rt.run, kind] });
  const real = rt.run.filter((k) => k !== "procedure");
  return (
    <fieldset className="stack" style={{ gap: 10, border: 0, padding: 0, margin: 0 }} disabled={validationBusy}>
      <legend className="sr-only">Checks in Fabric</legend>
      <label className="row" style={{ gap: 8 }}>
        <input type="checkbox" checked={rt.probe} onChange={(e) => setValidationRuntime({ ...rt, probe: e.target.checked })} />
        <span><strong>Check that SQL objects work</strong> <span className="muted">(read-only): query every table and view, and compile every stored procedure, in the Warehouse.</span></span>
      </label>
      <p className="muted" style={{ margin: 0 }}>
        Pipelines, notebooks and Spark jobs are judged by <strong>their latest run in Fabric</strong>: a failed run is a mismatch, and one that has
        never run is left for review. To prove them now, run them from here:
      </p>
      <div className="stack" style={{ gap: 6, paddingLeft: 4 }}>
        {RUNNABLE.map(({ kind, label }) => (
          <label key={kind} className="row" style={{ gap: 8 }}>
            <input type="checkbox" checked={rt.run.includes(kind)} onChange={() => toggle(kind)} />
            <span>Run {label.charAt(0).toLowerCase() + label.slice(1)} now</span>
          </label>
        ))}
        <label className="row" style={{ gap: 8 }}>
          <span className="muted">Stop waiting for a run after</span>
          <input className="input" type="number" min={1} max={240} style={{ width: 80 }} value={rt.timeoutMinutes}
            onChange={(e) => setValidationRuntime({ ...rt, timeoutMinutes: Math.max(1, Math.min(240, Number(e.target.value) || 30)) })} aria-label="Minutes to wait for each run" />
          <span className="muted">minutes</span>
        </label>
      </div>
      {real.length > 0 && (
        <Banner tone="warning" title="These are real runs">
          The selected items run in the Fabric workspace exactly as they would on a schedule: they read their sources and write their
          targets, use capacity, and may take a while. A few run at a time. Run them against a development or test workspace, or when
          their outputs may be overwritten.
        </Banner>
      )}
    </fieldset>
  );
}

export function ValidatePanel() {
  const { mode, discovery } = useAppState();
  const { validation, validationError, validationBusy, validationProgress, runValidation, execution, fabric, validationRuntime } = useMigration();
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState<ValidationStatus | "">("");
  const [runs, setRuns] = useState<RuntimeStatus | "">("");
  const rows = useMemo(() => (validation ?? []).filter((r) => (!category || r.category === category) && (!status || r.status === status)
    && (!runs || r.runtime?.status === runs)), [validation, category, status, runs]);
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const ready = discovered && fabric.status === "connected";
  const why = !discovered ? "Run discovery first." : fabric.status !== "connected" ? "Connect the Fabric destination first." : "";
  const count = (s: ValidationStatus) => (validation ?? []).filter((r) => r.status === s).length;
  const runCount = (s: RuntimeStatus) => (validation ?? []).filter((r) => r.runtime?.status === s).length;
  const present = CATEGORIES.filter((c) => (validation ?? []).some((r) => r.category === c));
  const willRun = validationRuntime.run.length;
  const runButton = (
    <Button variant="primary" onClick={() => void runValidation()} loading={validationBusy} disabled={mode === "real" && !ready} title={why || undefined}>
      {willRun ? <PlayCircle size={14} aria-hidden="true" /> : <ShieldCheck size={14} aria-hidden="true" />}
      {validation ? "Validate again" : "Run validation"}{willRun ? " (with runs)" : ""}
    </Button>
  );
  const progress = validationBusy && <p className="muted" role="status">{validationProgress ?? "Validating…"}</p>;

  if (!validation) {
    return (
      <div className="panel-center">
        <span className="intro-icon" aria-hidden="true"><ShieldCheck size={26} /></span>
        <h3>Compare the source with the destination, and check it runs</h3>
        <p className="muted">Validation reads both sides, object by object: columns and types, row counts, definitions, notebook cells and pipeline
          activities. It then asks Fabric whether each object works: tables and views are queried, procedures compiled, and pipelines,
          notebooks and Spark jobs judged by their runs.</p>
        <div style={{ textAlign: "left", width: "100%", maxWidth: 720 }}><RuntimeOptionsForm /></div>
        {mode === "real" && !ready && <Banner tone="warning" title="Not ready to validate">{why}</Banner>}
        {validationError && <Banner tone="error" title="Validation">{validationError}</Banner>}
        {runButton}
        {progress}
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
      {progress}
      <Collapsible summary={`Checks in Fabric: ${validationRuntime.probe ? "SQL objects queried and compiled" : "no SQL checks"}${willRun ? `; runs now: ${validationRuntime.run.join(", ")}` : "; last runs read"}`}>
        <RuntimeOptionsForm />
      </Collapsible>
      {validationError && <Banner tone="error" title="Validation">{validationError}</Banner>}

      <div className="filter-stats" role="group" aria-label="Filter by result">
        {([["", "Checks", validation.length, undefined], ["MATCH", "Match", count("MATCH"), "success"], ["REVIEW", "Review", count("REVIEW"), "warning"], ["MISMATCH", "Mismatch", count("MISMATCH"), "error"]] as const).map(([s, label, value, tone]) => (
          <button key={label} type="button" className={`filter-stat${tone ? ` tone-${tone}` : ""}`} aria-pressed={status === s} onClick={() => setStatus(status === s ? "" : s)}>
            <span className="filter-label">{label}</span><span className="filter-value">{value.toLocaleString()}</span>
          </button>
        ))}
      </div>
      <div className="row" role="group" aria-label="Filter by whether it runs in Fabric">
        <span className="muted">Runs in Fabric:</span>
        {(["PASS", "FAIL", "NOT RUN", "NOT CHECKED"] as const).map((s) => (
          <button key={s} type="button" className="chip" aria-pressed={runs === s} onClick={() => setRuns(runs === s ? "" : s)}
            style={{ cursor: "pointer", opacity: runs && runs !== s ? 0.6 : 1 }}>
            <StatusBadge tone={RUN_TONE[s]}>{RUN_LABEL[s]}</StatusBadge> {runCount(s).toLocaleString()}
          </button>
        ))}
      </div>

      <div className="tabs tabs-pill" role="tablist" aria-label="Validation categories">
        <button role="tab" type="button" className="tab" aria-selected={category === ""} onClick={() => setCategory("")}>All</button>
        {present.map((c) => <button key={c} role="tab" type="button" className="tab" aria-selected={category === c} onClick={() => setCategory(c)}>{c}</button>)}
      </div>

      <div className="table-wrap">
        <table className="data" style={{ minWidth: 1180 }}>
          <caption className="sr-only">Validation results</caption>
          <thead><tr>{["Object", "Category", "Synapse", "Fabric", "Runs in Fabric", "Status", "Details"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
          <tbody>
            {rows.slice(0, 500).map((r, i) => (
              <tr key={`${r.object}-${r.category}-${i}`}>
                <td className="name" title={r.object}>{r.object}</td>
                <td>{r.category}</td>
                <td>{r.source}</td>
                <td>{r.target}</td>
                <td style={{ whiteSpace: "normal", minWidth: 150 }}>
                  {r.runtime ? (
                    <span title={r.runtime.detail}>
                      <StatusBadge tone={RUN_TONE[r.runtime.status]}>{RUN_LABEL[r.runtime.status]}</StatusBadge>
                      <div className="faint" style={{ fontSize: 13 }}>{r.runtime.check}</div>
                    </span>
                  ) : <span className="faint">—</span>}
                </td>
                <td><StatusBadge tone={TONE[r.status]}>{MARK[r.status]} {r.status}</StatusBadge></td>
                <td className="muted" style={{ whiteSpace: "normal", maxWidth: 420 }}>
                  {r.detail || (r.runtime ? "" : "—")}
                  {r.runtime && <div style={{ marginTop: r.detail ? 4 : 0 }}><strong>{r.runtime.check}:</strong> {r.runtime.detail}</div>}
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={7} className="muted">Nothing in this view.</td></tr>}
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
      { label: "Run in Fabric", value: (validation ?? []).filter((r) => r.runtime?.status === "PASS").length.toLocaleString() },
    ]} />
  );
}
