import { ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { MetricCard } from "../components/shared/Metrics";
import { Banner, Button, Card, EmptyState, PageHead, StatusBadge, type Tone } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";
import type { ValidationStatus } from "../types";

const TONE: Record<ValidationStatus, Tone> = { MATCH: "success", REVIEW: "warning", MISMATCH: "error" };
const MARK: Record<ValidationStatus, string> = { MATCH: "✓", REVIEW: "⚠", MISMATCH: "✕" };
const CATEGORIES = ["Schema", "Tables", "Columns", "Data Count", "Data Types", "Views", "Stored Procedures", "Pipelines", "Notebooks", "Security", "Dependencies"];

export function Validate() {
  const { mode } = useAppState();
  const { validation, validationError, validationBusy, runValidation, execution } = useMigration();
  const [category, setCategory] = useState("");
  const rows = useMemo(() => (validation ?? []).filter((r) => !category || r.category === category), [validation, category]);
  const count = (s: ValidationStatus) => (validation ?? []).filter((r) => r.status === s).length;
  const present = new Set((validation ?? []).map((r) => r.category));

  return (
    <div className="page wide">
      <PageHead icon={ShieldCheck} title="Migration Validation" badge={mode === "mock" ? <StatusBadge tone="warning">DEMO DATA</StatusBadge> : undefined}
        actions={<Button variant="primary" onClick={() => void runValidation()} loading={validationBusy}>Run Validation</Button>}>
        Compare the source Synapse environment with the migrated Fabric environment, object by object.
      </PageHead>

      {mode === "real" && (
        <Banner tone="info" title="Migration validation is not implemented in the backend yet">
          Run Validation will report "not implemented". Nothing is compared or counted without a backend. Switch to Demo data to preview the page.
        </Banner>
      )}
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
            <MetricCard label="Checks" value={validation.length} />
            <MetricCard label="Match" value={count("MATCH")} />
            <MetricCard label="Review" value={count("REVIEW")} />
            <MetricCard label="Mismatch" value={count("MISMATCH")} />
          </div>
          <Card title="Source Synapse vs target Fabric">
            <div className="tabs tabs-pill" role="tablist" aria-label="Validation categories" style={{ marginBottom: 12 }}>
              <button role="tab" type="button" className="tab" aria-selected={category === ""} onClick={() => setCategory("")}>All</button>
              {CATEGORIES.filter((c) => present.has(c)).map((c) => (
                <button key={c} role="tab" type="button" className="tab" aria-selected={category === c} onClick={() => setCategory(c)}>{c}</button>
              ))}
            </div>
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 760 }}>
                <caption className="sr-only">Validation results</caption>
                <thead><tr>{["Object", "Category", "Synapse", "Fabric", "Status"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
                <tbody>
                  {rows.slice(0, 500).map((r, i) => (
                    <tr key={`${r.object}-${r.category}-${i}`}>
                      <td className="name" title={r.object}>{r.object}</td>
                      <td>{r.category}</td>
                      <td>{r.source}</td>
                      <td>{r.target}</td>
                      <td><StatusBadge tone={TONE[r.status]}>{MARK[r.status]} {r.status}</StatusBadge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
