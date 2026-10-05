import { AlertTriangle, Gauge, Layers, ListChecks, Play, ShieldAlert, Timer } from "lucide-react";
import { useState } from "react";
import { Banner, Button, Card, EmptyState, StatusBadge } from "../shared/Shared";
import { MetricCard } from "../shared/Metrics";
import type { PlanAnalysis, RiskSeverity } from "../../types";

const SEVERITIES: RiskSeverity[] = ["BLOCKING", "HIGH", "MEDIUM", "LOW"];
const SEV_CLASS: Record<RiskSeverity, string> = { BLOCKING: "sev-blocking", HIGH: "sev-high", MEDIUM: "sev-medium", LOW: "sev-low" };

function tone(readiness: number): "success" | "warning" | "error" {
  return readiness >= 80 ? "success" : readiness >= 60 ? "warning" : "error";
}

/** The deterministic planner: what it reads, what it produced, and a way to run it again. */
export function PlannerPanel({ analysis, busy, error, hasPlan, onRerun }: {
  analysis: PlanAnalysis | null; busy: boolean; error: string | null; hasPlan: boolean; onRerun: () => void;
}) {
  const [filter, setFilter] = useState<RiskSeverity | "ALL">("ALL");
  const last = analysis?.history[0];
  const risks = (analysis?.risks ?? []).filter((r) => filter === "ALL" || r.severity === filter);
  const counts = analysis?.strategyCounts;

  return (
    <>
      <Card
        title="Synapse → Fabric Migration Planner"
        actions={<><StatusBadge tone="info">v{analysis?.plannerVersion ?? "1.0.0"}</StatusBadge><StatusBadge tone="success">DETERMINISTIC</StatusBadge></>}
      >
        <p className="muted" style={{ marginTop: 0 }}>
          Turns the discovered objects, their dependency order and the Fabric target into a wave-based plan: a migration strategy per object,
          risks with a severity, an effort estimate and a readiness score. The same plan over the same estate always gives the same result.
        </p>
        <div className="planner-io">
          <div><span className="eyebrow">Inputs</span><code>dependency_graph</code><code>discovery_inventory</code><code>object_definitions</code><code>fabric_target</code></div>
          <div><span className="eyebrow">Outputs</span><code className="out">migration_waves</code><code className="out">object_strategies</code><code className="out">risks</code><code className="out">readiness_score</code><code className="out">effort_estimate</code></div>
        </div>
        <p className="faint" style={{ margin: "12px 0 0" }}>
          {last
            ? `Last recorded run: ${last.status} · readiness ${last.readiness}/100 · ${last.objects} objects · ${new Date(last.createdAt).toLocaleString()}`
            : "No recorded planner run yet."}
          {analysis && <> · plan fingerprint <code>{analysis.fingerprint}</code></>}
        </p>
        <div className="row" style={{ marginTop: 14 }}>
          <Button variant="primary" onClick={onRerun} loading={busy} disabled={!hasPlan}><Play size={14} aria-hidden="true" />Re-run planner</Button>
          {!hasPlan && <span className="muted">Build a plan first.</span>}
        </div>
      </Card>

      {error && <Banner tone="error" title="Planner">{error}</Banner>}

      {!analysis ? (
        !error && <Card><EmptyState icon={<ListChecks size={22} />} title="No analysis yet.">Build a plan below and the planner scores it automatically.</EmptyState></Card>
      ) : (
        <>
          <div className="grid cols-4">
            <MetricCard icon={Gauge} label="Readiness" value={<span className={`tone-${tone(analysis.readiness)}`}>{analysis.readiness}/100</span>} hint="Falls with every risk, by severity" />
            <MetricCard icon={Layers} label="Objects" value={analysis.objects.toLocaleString()} hint={`${counts?.automated ?? 0} automated · ${counts?.manual ?? 0} manual · ${counts?.assess ?? 0} assess · ${counts?.later ?? 0} later`} />
            <MetricCard icon={Timer} label="Effort" value={`${analysis.effortDays}d`} hint="Estimated working days, review included" />
            <MetricCard icon={Layers} label="Waves" value={analysis.waves.length} hint="Dependency-ordered groups" />
          </div>
          <div className="grid cols-4">
            <MetricCard icon={ShieldAlert} label="Blocking" value={<span className={analysis.blocking ? "tone-error" : undefined}>{analysis.blocking}</span>} hint="Must be resolved first" />
            <MetricCard icon={AlertTriangle} label="Needs review" value={analysis.needsReview.count} hint={`${analysis.needsReview.effortDays}d of the estimate`} />
            <MetricCard label="Automated now" value={counts?.automated ?? 0} hint="Created in Fabric by this tool" />
            <MetricCard label="Later session" value={counts?.later ?? 0} hint="Mapped, not yet created by this build" />
          </div>

          <Card title={`Risks (${analysis.risks.length})`}
            actions={
              <div className="tabs tabs-pill" role="tablist" aria-label="Filter risks by severity">
                <button type="button" role="tab" className="tab" aria-selected={filter === "ALL"} onClick={() => setFilter("ALL")}>ALL</button>
                {SEVERITIES.map((s) => <button key={s} type="button" role="tab" className="tab" aria-selected={filter === s} onClick={() => setFilter(s)}>{s} ({analysis.riskCounts[s]})</button>)}
              </div>
            }>
            {risks.length === 0 ? (
              <p className="muted" style={{ margin: 0 }}>{analysis.risks.length ? "No risks at this severity." : "No risks found for this plan."}</p>
            ) : (
              <ul className="risk-list" aria-label="Migration risks">
                {risks.map((r) => (
                  <li key={r.id} className={`risk ${SEV_CLASS[r.severity]}`}>
                    <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                      <strong className="sev">{r.severity}</strong>
                      <span className="code">{r.code}</span>
                      <strong>{r.title}</strong>
                    </div>
                    <p>{r.message}</p>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </>
      )}
    </>
  );
}

/** The recorded planner runs, newest first. */
export function PlannerRuns({ analysis }: { analysis: PlanAnalysis | null }) {
  const runs = analysis?.history ?? [];
  if (!runs.length) return null;
  return (
    <Card title={`Planner runs (${runs.length})`}>
      <div className="table-wrap">
        <table className="data" style={{ minWidth: 720 }}>
          <caption className="sr-only">Recorded planner runs</caption>
          <thead><tr>{["Run", "Version", "Status", "Objects", "Effort", "Waves", "Readiness", "When"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.id}</td><td>v{r.plannerVersion}</td>
                <td><StatusBadge tone={r.blocking ? "warning" : "success"}>{r.status}</StatusBadge></td>
                <td className="num">{r.objects}</td><td className="num">{r.effortDays}d</td><td className="num">{r.waves}</td>
                <td className="num"><strong>{r.readiness}</strong></td>
                <td className="muted">{new Date(r.createdAt).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
