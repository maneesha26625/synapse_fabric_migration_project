import { ListChecks, ListPlus, Play, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { PlannerRuns } from "../../migrate/PlannerPanel";
import { StagesPanel } from "../../migrate/StagesPanel";
import { PlanEditor, StrategyTable, days } from "../../migrate/StrategyPanel";
import { StatStrip } from "../../shared/Metrics";
import { notify } from "../../shared/notify";
import { Banner, Button, Collapsible, Tabs } from "../../shared/Shared";
import { useMigration } from "../../../state/MigrationState";
import type { PlanRisk, RiskSeverity } from "../../../types";

const SEVERITIES: RiskSeverity[] = ["BLOCKING", "HIGH", "MEDIUM", "LOW"];
const SEV_CLASS: Record<RiskSeverity, string> = { BLOCKING: "sev-blocking", HIGH: "sev-high", MEDIUM: "sev-medium", LOW: "sev-low" };
const SEV_LABEL: Record<RiskSeverity, string> = { BLOCKING: "Blocking", HIGH: "High", MEDIUM: "Medium", LOW: "Low" };
const readinessTone = (r: number) => (r >= 80 ? "success" : r >= 60 ? "warning" : "error") as "success" | "warning" | "error";
/** Objects listed under one risk before "and N more". */
const OBJECTS_SHOWN = 50;

/** The objects a risk is about, by name, each a shortcut to it in the plan. */
function RiskObjects({ risk, onFind }: { risk: PlanRisk; onFind: (name: string) => void }) {
  const { graph, plan } = useMigration();
  const nodes = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const waveOf = useMemo(() => new Map(plan.map((p) => [p.id, p.wave])), [plan]);
  if (!risk.objects.length) return null;
  const listed = risk.objects.slice(0, OBJECTS_SHOWN);
  return (
    <details className="risk-objects">
      <summary>Show the {risk.objects.length === 1 ? "object" : `${risk.objects.length.toLocaleString()} objects`}</summary>
      <ul>
        {listed.map((id) => {
          const n = nodes.get(id);
          const name = n?.name ?? id.split("/").pop() ?? id;
          return (
            <li key={id}>
              <span className="risk-object-name">{name}</span>
              <span className="faint">{n ? `${n.type}${waveOf.has(id) ? ` · wave ${waveOf.get(id)}` : ""}` : "not in the discovery"}</span>
              {n && waveOf.has(id) && (
                <button type="button" className="link" onClick={() => onFind(name)}><Search size={12} aria-hidden="true" />Find in plan</button>
              )}
            </li>
          );
        })}
      </ul>
      {risk.objects.length > OBJECTS_SHOWN && <p className="faint">and {(risk.objects.length - OBJECTS_SHOWN).toLocaleString()} more</p>}
    </details>
  );
}

/** Risks first, worst first: the reason the plan is or is not ready. */
function Risks({ onFind }: { onFind: (name: string) => void }) {
  const { analysis, analysisBusy, analyze } = useMigration();
  const [filter, setFilter] = useState<RiskSeverity | "ALL">("ALL");
  if (!analysis) return null;
  const risks = analysis.risks.filter((r) => filter === "ALL" || r.severity === filter);
  const scoreAgain = async () => {
    const result = await analyze(true);
    if (result) notify(`Scored again: readiness ${result.readiness}/100${result.blocking ? `, ${result.blocking} blocking` : ""}. The run is listed under Planner runs.`);
  };
  return (
    <div className="stack">
      <div className="panel-toolbar">
        <div className="tabs tabs-pill" role="tablist" aria-label="Filter risks by severity">
          <button type="button" role="tab" className="tab" aria-selected={filter === "ALL"} onClick={() => setFilter("ALL")}>All {analysis.risks.length}</button>
          {SEVERITIES.filter((s) => analysis.riskCounts[s]).map((s) => <button key={s} type="button" role="tab" className="tab" aria-selected={filter === s} onClick={() => setFilter(s)}>{SEV_LABEL[s]} {analysis.riskCounts[s]}</button>)}
        </div>
        <span className="spacer" />
        <Button size="small" onClick={() => void scoreAgain()} loading={analysisBusy} title="Score the plan again and keep the result under Planner runs"><Play size={13} aria-hidden="true" />Score again</Button>
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
                {r.objects.length > 0 && <span className="faint">· {r.objects.length.toLocaleString()} object{r.objects.length === 1 ? "" : "s"}</span>}
              </div>
              <p>{r.message}</p>
              <RiskObjects risk={r} onFind={onFind} />
            </li>
          ))}
        </ul>
      )}
      <Collapsible summary="How the plan is scored">
        <p className="muted" style={{ margin: 0 }}>The planner is deterministic: the same plan over the same estate always gives the same strategy, risks, effort and readiness. Readiness starts at 100 and falls with every risk, by severity; a blocking risk must be resolved before the migration can start. Plan fingerprint <code>{analysis.fingerprint}</code>, planner v{analysis.plannerVersion}.</p>
      </Collapsible>
      <PlannerRuns analysis={analysis} />
    </div>
  );
}

export function PlanPanel() {
  const { graph, plan, analysis, analysisError, addAllToPlan } = useMigration();
  const [tab, setTab] = useState<"risks" | "objects" | "strategy" | "stages">("risks");
  const [picking, setPicking] = useState(false);
  const [find, setFind] = useState("");
  const showInPlan = (name: string) => { setFind(name); setTab("objects"); };

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
      ) : !analysisError && plan.length > 0 && <p className="muted"><span className="spinner" aria-hidden="true" /> Scoring the plan…</p>}
      {analysisError && <Banner tone="error" title="The plan could not be scored">{analysisError}</Banner>}
      {analysis && analysis.blocking > 0 && (
        <Banner tone="error" title={`${analysis.blocking} blocking risk${analysis.blocking === 1 ? "" : "s"}`}
          actions={tab !== "risks" ? <Button size="small" onClick={() => setTab("risks")}>Show them</Button> : undefined}>
          The migration cannot start until {analysis.blocking === 1 ? "it is" : "they are"} resolved.
        </Banner>
      )}

      <Tabs label="Plan sections" value={tab} onChange={setTab} tabs={[
        { id: "risks", label: "Readiness & risks", count: analysis?.risks.length },
        { id: "objects", label: "Objects in plan", count: plan.length },
        { id: "strategy", label: "Strategy by type" },
        { id: "stages", label: "Stages & credentials" },
      ]} />

      {tab === "risks" && (analysis ? <Risks onFind={showInPlan} /> : <p className="muted">The risks appear once the plan is scored.</p>)}
      {tab === "objects" && <PlanEditor find={find} onFind={setFind} />}
      {tab === "strategy" && <StrategyTable />}
      {tab === "stages" && <StagesPanel />}
    </div>
  );
}
