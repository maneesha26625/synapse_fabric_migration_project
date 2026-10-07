import { Card, StatusBadge } from "../shared/Shared";
import type { PlanAnalysis } from "../../types";
import { days } from "./StrategyPanel";

/** The recorded planner runs (each "Score again"), newest first. */
export function PlannerRuns({ analysis }: { analysis: PlanAnalysis | null }) {
  const runs = analysis?.history ?? [];
  if (!runs.length) return null;
  return (
    <Card title={`Planner runs (${runs.length})`} subtitle="Each Score again is kept here, so readiness can be compared as the plan changes.">
      <div className="table-wrap">
        <table className="data" style={{ minWidth: 720 }}>
          <caption className="sr-only">Recorded planner runs</caption>
          <thead><tr>{["Run", "Version", "Status", "Objects", "Effort", "Waves", "Readiness", "When"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.id}</td><td>v{r.plannerVersion}</td>
                <td><StatusBadge tone={r.blocking ? "warning" : "success"}>{r.blocking ? `${r.blocking} BLOCKING` : r.status}</StatusBadge></td>
                <td className="num">{r.objects.toLocaleString()}</td><td className="num">{days(r.effortDays)}d</td><td className="num">{r.waves}</td>
                <td className="num"><strong>{r.readiness}</strong>/100</td>
                <td className="muted">{new Date(r.createdAt).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
