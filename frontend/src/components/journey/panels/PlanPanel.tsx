import { ListChecks, ListPlus, Play } from "lucide-react";
import { useState } from "react";
import { PlannerRuns } from "../../migrate/PlannerPanel";
import { StagesPanel } from "../../migrate/StagesPanel";
import { PlanEditor, StrategyTable } from "../../migrate/StrategyPanel";
import { StatStrip } from "../../shared/Metrics";
import { Banner, Button, Collapsible, Tabs } from "../../shared/Shared";
import { useMigration } from "../../../state/MigrationState";
import type { RiskSeverity } from "../../../types";

const SEVERITIES: RiskSeverity[] = ["BLOCKING", "HIGH", "MEDIUM", "LOW"];
const SEV_CLASS: Record<RiskSeverity, string> = { BLOCKING: "sev-blocking", HIGH: "sev-high", MEDIUM: "sev-medium", LOW: "sev-low" };
/** Working days, to one decimal: 98.38 reads as 98.4. */
const days = (d: number) => (Math.round(d * 10) / 10).toLocaleString();
const readinessTone = (r: number) => (r >= 80 ? "success" : r >= 60 ? "warning" : "error") as "success" | "warning" | "error";

/** Risks first, worst first: the reason the plan is or is not ready. */
function Risks() {
  const { analysis, analysisBusy, analyze } = useMigration();
  const [filter, setFilter] = useState<RiskSeverity | "ALL">("ALL");
  if (!analysis) return null;
  const risks = analysis.risks.filter((r) => filter === "ALL" || r.severity === filter);
  return (
    <div className="stack">
      <div className="panel-toolbar">
        <div className="tabs tabs-pill" role="tablist" aria-label="Filter risks by severity">
          <button type="button" role="tab" className="tab" aria-selected={filter === "ALL"} onClick={() => setFilter("ALL")}>All {analysis.risks.length}</button>
          {SEVERITIES.filter((s) => analysis.riskCounts[s]).map((s) => <button key={s} type="button" role="tab" className="tab" aria-selected={filter === s} onClick={() => setFilter(s)}>{s.charAt(0) + s.slice(1).toLowerCase()} {analysis.riskCounts[s]}</button>)}
        </div>
        <span className="spacer" />
        <Button size="small" onClick={() => void analyze(true)} loading={analysisBusy}><Play size={13} aria-hidden="true" />Score again</Button>
      </div>
      {risks.length === 0 ? (
        <p className="muted">{analysis.risks.length ? "No risks at this severity." : "No risks found for this plan."}</p>
      ) : (
        <ul className="risk-list" aria-label="Migration risks">
          {risks.map((r) => (
            <li key={r.id} className={`risk ${SEV_CLASS[r.severity]}`}>
              <div className="row" style={{ gap: 8 }}>
                <strong className="sev">{r.severity}</strong>
                <strong>{r.title}</strong>
                {r.objects.length > 0 && <span className="faint">· {r.objects.length} object{r.objects.length === 1 ? "" : "s"}</span>}
              </div>
              <p>{r.message}</p>
            </li>
          ))}
        </ul>
      )}
      <Collapsible summary="How the plan is scored">
        <p className="muted" style={{ margin: 0 }}>The planner is deterministic: the same plan over the same estate always gives the same strategy, risks, effort and readiness. Readiness starts at 100 and falls with every risk, by severity. Plan fingerprint <code>{analysis.fingerprint}</code>, planner v{analysis.plannerVersion}.</p>
      </Collapsible>
      <PlannerRuns analysis={analysis} />
    </div>
  );
}

export function PlanPanel() {
  const { graph, plan, analysis, analysisError, addAllToPlan } = useMigration();
  const [tab, setTab] = useState<"risks" | "objects" | "strategy" | "stages">("risks");
  const [picking, setPicking] = useState(false);

  if (!graph) return <p className="muted">The plan is built from the discovered objects and their waves. Finish the earlier steps first.</p>;

  if (!plan.length && !picking) {
    return (
      <div className="panel-center">
        <span className="intro-icon" aria-hidden="true"><ListChecks size={26} /></span>
        <h3>Build the migration plan</h3>
        <p className="muted">Start with every discovered object in its suggested wave, then remove or move what you want. The plan is scored as you edit: strategy, risks, effort and readiness.</p>
        <div className="row" style={{ justifyContent: "center" }}>
          <Button variant="primary" onClick={addAllToPlan}><ListPlus size={14} aria-hidden="true" />Add all {graph.nodes.length.toLocaleString()} objects</Button>
          <Button onClick={() => { setPicking(true); setTab("objects"); }}>Choose objects myself</Button>
        </div>
      </div>
    );
  }

  const counts = analysis?.strategyCounts;
  return (
    <div className="stack panel-body">
      {analysis ? (
        <StatStrip label="Plan summary" items={[
          { label: "Readiness", value: `${analysis.readiness}/100`, tone: readinessTone(analysis.readiness), hint: "Falls with every risk, by severity" },
          { label: "Objects", value: analysis.objects.toLocaleString() },
          { label: "Automated", value: (counts?.automated ?? 0).toLocaleString(), hint: "Created in Fabric by this tool", tone: "success" },
          { label: "Needs a person", value: ((counts?.manual ?? 0) + (counts?.assess ?? 0)).toLocaleString(), hint: "Manual setup or assess first" },
          { label: "Effort", value: `${days(analysis.effortDays)}d`, hint: "Working days, review included" },
          { label: "Blocking", value: analysis.blocking, tone: analysis.blocking ? "error" : undefined },
        ]} />
      ) : !analysisError && <p className="muted"><span className="spinner" aria-hidden="true" /> Scoring the plan…</p>}
      {analysisError && <Banner tone="error" title="The plan could not be scored">{analysisError}</Banner>}

      <Tabs label="Plan sections" value={tab} onChange={setTab} tabs={[
        { id: "risks", label: "Readiness & risks", count: analysis?.risks.length },
        { id: "objects", label: "Objects in plan", count: plan.length },
        { id: "strategy", label: "Strategy by type" },
        { id: "stages", label: "Stages & credentials" },
      ]} />

      {tab === "risks" && (analysis ? <Risks /> : <p className="muted">The risks appear once the plan is scored.</p>)}
      {tab === "objects" && <PlanEditor />}
      {tab === "strategy" && <StrategyTable />}
      {tab === "stages" && <StagesPanel />}
    </div>
  );
}
