import { ArrowDown, ArrowUp, ListChecks, ListPlus, Play, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ClassificationBadge, MetricCard } from "../components/shared/Metrics";
import { Banner, Button, Card, EmptyState, ErrorState, LoadingState, PageHead, StatusBadge, type Tone } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";
import type { PlanStatus } from "../types";

const TONE: Record<PlanStatus, Tone> = {
  "NOT STARTED": "neutral", READY: "info", "IN PROGRESS": "info", COMPLETED: "success", FAILED: "error", BLOCKED: "warning",
};

export function Plan() {
  const { discovery, isConnected, health } = useAppState();
  // What a run moves now; everything else in the plan is deferred to a later session.
  const migratable = useMemo(() => new Set(health?.capabilities.migratableTypes ?? ["Dedicated SQL Pool", "Schema", "Table", "View", "Stored Procedure", "Notebook"]), [health]);
  const { graph, graphLoading, graphError, reloadGraph, plan, addToPlan, addAllToPlan, removeFromPlan, setPlanWave, movePlanItem, clearPlan, planStatus, fabric } = useMigration();
  const navigate = useNavigate();
  const [pick, setPick] = useState("");
  const [pickWave, setPickWave] = useState(0);
  const done = discovery.state === "completed" || discovery.state === "completed_with_warnings";

  const nodes = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const byName = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.name.toLowerCase(), n])), [graph]);
  const visible = plan.filter((p) => nodes.has(p.id)); // an id from an older discovery is not shown
  const waves = [...new Set(visible.map((p) => p.wave))].sort((a, b) => a - b);
  const waveChoices = Array.from({ length: Math.max(5, ...(graph?.waves.map((w) => w.wave) ?? [0]), ...visible.map((p) => p.wave)) }, (_, i) => i + 1);
  const statuses = visible.map((p) => planStatus(p.id));
  const count = (s: PlanStatus) => statuses.filter((x) => x === s).length;

  const addPicked = () => {
    const found = byName.get(pick.trim().toLowerCase());
    if (!found) return;
    addToPlan([found.id]);
    if (pickWave) setPlanWave(found.id, pickWave);
    setPick("");
  };

  let body;
  if (!done) {
    body = (
      <Card>
        <EmptyState icon={<ListChecks size={22} />} title={isConnected ? "Discovery has not been executed." : "No Synapse workspace connected."} actions={<Button variant="primary" onClick={() => navigate(isConnected ? "/discovery" : "/synapse")}>{isConnected ? "Run Discovery" : "Connect Synapse"}</Button>}>
          The plan is built from the discovered objects and their dependency order.
        </EmptyState>
      </Card>
    );
  } else if (graphLoading && !graph) body = <Card><LoadingState label="Loading objects…" /></Card>;
  else if (graphError) body = <Card><ErrorState title="Could not load the objects" message={graphError} actions={<Button onClick={reloadGraph}>Retry</Button>} /></Card>;
  else {
    body = (
      <>
        <div className="grid cols-4">
          <MetricCard label="Objects in plan" value={visible.length.toLocaleString()} />
          <MetricCard label="Ready" value={count("READY")} hint="Everything it needs is planned in the same or an earlier wave" />
          <MetricCard label="Blocked" value={count("BLOCKED")} hint="Needs something that is not in the plan, or is planned later" />
          <MetricCard label="Waves" value={waves.length} />
        </div>

        {fabric.status !== "connected" && (
          <Banner tone="warning" title="Fabric target is not connected">You can build the plan now. Connect the Fabric target before starting the migration.</Banner>
        )}

        <Card>
          <div className="toolbar">
            <Button onClick={addAllToPlan} disabled={!graph?.nodes.length}><ListPlus size={14} aria-hidden="true" />Add all objects (suggested waves)</Button>
            <input className="input" style={{ maxWidth: 260 }} list="plan-objects" placeholder="Add an object by name" aria-label="Object to add" value={pick} onChange={(e) => setPick(e.target.value)} />
            <datalist id="plan-objects">{(graph?.nodes ?? []).filter((n) => !plan.some((p) => p.id === n.id)).slice(0, 400).map((n) => <option key={n.id} value={n.name} />)}</datalist>
            <select className="select" aria-label="Wave for the added object" value={pickWave} onChange={(e) => setPickWave(Number(e.target.value))}>
              <option value={0}>Suggested wave</option>{waveChoices.map((w) => <option key={w} value={w}>Wave {w}</option>)}
            </select>
            <Button onClick={addPicked} disabled={!byName.has(pick.trim().toLowerCase())}>Add to Wave</Button>
            <span className="spacer" />
            <Button variant="ghost" onClick={clearPlan} disabled={!plan.length}>Clear plan</Button>
            <Button variant="primary" disabled={!visible.length} onClick={() => navigate("/execute")}><Play size={14} aria-hidden="true" />Start Migration</Button>
          </div>
        </Card>

        {visible.length === 0 ? (
          <Card><EmptyState icon={<ListChecks size={22} />} title="The migration plan is empty." actions={<Button variant="primary" onClick={addAllToPlan} disabled={!graph?.nodes.length}><ListPlus size={14} aria-hidden="true" />Build plan from suggested waves</Button>}>Build a plan with every discovered object in its suggested wave, then adjust it. Or add single objects above, or from any object in Discovery with Add to Migration Plan.</EmptyState></Card>
        ) : waves.map((w) => {
          const items = visible.filter((p) => p.wave === w);
          return (
            <Card key={w} title={`Wave ${w}`} subtitle={`${items.length} object${items.length === 1 ? "" : "s"}`}>
              <div className="table-wrap">
                <table className="data" style={{ minWidth: 1020 }}>
                  <caption className="sr-only">Wave {w} objects</caption>
                  <thead><tr>{["Object", "Source", "Target", "Classification", "Dependencies", "This session", "Status", ""].map((h) => <th key={h || "actions"} scope="col" className="static">{h || <span className="sr-only">Actions</span>}</th>)}</tr></thead>
                  <tbody>
                    {items.map((p) => {
                      const n = nodes.get(p.id)!;
                      const st = planStatus(p.id);
                      return (
                        <tr key={p.id}>
                          <td className="name" title={n.name}>{n.name}</td>
                          <td>{n.type}</td>
                          <td className="ws" title={n.fabricTarget}>{n.fabricTarget}</td>
                          <td><ClassificationBadge value={n.classification} /></td>
                          <td className="num">{n.dependsOn}</td>
                          <td>{migratable.has(n.type) ? <StatusBadge tone="success">Migrates now</StatusBadge> : <StatusBadge tone="neutral">Later session</StatusBadge>}</td>
                          <td><StatusBadge tone={TONE[st]}>{st}</StatusBadge></td>
                          <td>
                            <div className="row" style={{ flexWrap: "nowrap", gap: 4 }}>
                              <Button size="small" icon variant="ghost" aria-label={`Move ${n.name} up`} onClick={() => movePlanItem(p.id, -1)}><ArrowUp size={14} /></Button>
                              <Button size="small" icon variant="ghost" aria-label={`Move ${n.name} down`} onClick={() => movePlanItem(p.id, 1)}><ArrowDown size={14} /></Button>
                              <select className="select" style={{ height: 30 }} aria-label={`Wave for ${n.name}`} value={p.wave} onChange={(e) => setPlanWave(p.id, Number(e.target.value))}>
                                {waveChoices.map((x) => <option key={x} value={x}>Wave {x}</option>)}
                              </select>
                              <Button size="small" icon variant="ghost" aria-label={`Remove ${n.name}`} onClick={() => removeFromPlan(p.id)}><Trash2 size={14} /></Button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Card>
          );
        })}
      </>
    );
  }

  return (
    <div className="page wide">
      <PageHead icon={ListChecks} title="Migration Plan">
        Decide which objects migrate, and in which wave. Waves are suggested from dependencies; you can add, remove, reorder and move objects.
      </PageHead>
      {body}
    </div>
  );
}
