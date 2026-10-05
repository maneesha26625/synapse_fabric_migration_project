import { ArrowDown, ArrowUp, ListPlus, Route, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { ClassificationBadge } from "../shared/Metrics";
import { Button, Card, Collapsible, StatusBadge, type Tone } from "../shared/Shared";
import { useMigration } from "../../state/MigrationState";
import type { Strategy } from "../../types";

export const STRATEGY_TONE: Record<Strategy, Tone> = { automated: "success", manual: "warning", assess: "info", later: "neutral", deselected: "neutral" };
const STRATEGY_TEXT: Record<Strategy, string> = {
  automated: "This tool creates it in Fabric. Anything already there is skipped, never overwritten.",
  manual: "No automatic equivalent: set it up in Fabric by hand.",
  assess: "Review how it is used before a Fabric target is chosen.",
  later: "Mapped to a Fabric component, but this build cannot create it yet.",
  deselected: "Its migration stage is switched off for this run.",
};

/** Strategy by object type, how the run is carried out, and the plan itself. */
export function StrategyPanel() {
  const { graph, plan, analysis, addAllToPlan, addToPlan, removeFromPlan, setPlanWave, movePlanItem, clearPlan } = useMigration();
  const [pick, setPick] = useState("");
  const [pickWave, setPickWave] = useState(0);

  const nodes = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const byName = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.name.toLowerCase(), n])), [graph]);
  const visible = plan.filter((p) => nodes.has(p.id));
  const waves = [...new Set(visible.map((p) => p.wave))].sort((a, b) => a - b);
  const waveChoices = Array.from({ length: Math.max(5, ...(graph?.waves.map((w) => w.wave) ?? [0]), ...visible.map((p) => p.wave)) }, (_, i) => i + 1);

  const addPicked = () => {
    const found = byName.get(pick.trim().toLowerCase());
    if (!found) return;
    addToPlan([found.id]);
    if (pickWave) setPlanWave(found.id, pickWave);
    setPick("");
  };

  return (
    <>
      <Card eyebrow="Migration strategy" title="How each kind of object is migrated"
        subtitle="Chosen from what this build can create and each object's classification, never guessed from a name.">
        {!analysis ? (
          <p className="muted" style={{ margin: 0 }}>Build a plan to see the strategy for each object type.</p>
        ) : (
          <div className="table-wrap">
            <table className="data" style={{ minWidth: 860 }}>
              <caption className="sr-only">Migration strategy by object type</caption>
              <thead><tr>{["Object type", "Objects", "Strategy", "Fabric target", "Waves", "Effort"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
              <tbody>
                {analysis.typeStrategies.map((t) => (
                  <tr key={t.type}>
                    <td className="name">{t.type}</td>
                    <td className="num">{t.count}</td>
                    <td title={STRATEGY_TEXT[t.strategy]}><StatusBadge tone={STRATEGY_TONE[t.strategy]}>{t.strategyLabel}</StatusBadge></td>
                    <td className="ws" title={t.target}>{t.target || "—"}</td>
                    <td>{t.firstWave === t.lastWave ? `Wave ${t.firstWave}` : `Waves ${t.firstWave}–${t.lastWave}`}</td>
                    <td className="num">{t.effortDays}d</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card eyebrow="Migration objects" title="The plan"
        subtitle="Waves are suggested from the dependency graph. Add, remove, reorder or move objects; the planner re-scores as you edit.">
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
        </div>

        {visible.length === 0 ? (
          <p className="muted" style={{ marginBottom: 0 }}><Route size={14} aria-hidden="true" /> The plan is empty. Add every discovered object in its suggested wave, then adjust it.</p>
        ) : (
          <div className="stack" style={{ marginTop: 14 }}>
            {waves.map((w) => {
              const items = visible.filter((p) => p.wave === w);
              const auto = items.filter((p) => analysis?.objectStrategies[p.id]?.strategy === "automated").length;
              return (
                <Collapsible key={w} defaultOpen={false} summary={<span><strong>Wave {w}</strong> <span className="muted">· {items.length} object{items.length === 1 ? "" : "s"}{analysis ? ` · ${auto} automated` : ""}</span></span>}>
                  <div className="table-wrap">
                    <table className="data" style={{ minWidth: 940 }}>
                      <caption className="sr-only">Wave {w} objects</caption>
                      <thead><tr>{["Object", "Source", "Fabric target", "Classification", "Strategy", "Needs", ""].map((h) => <th key={h || "actions"} scope="col" className="static">{h || <span className="sr-only">Actions</span>}</th>)}</tr></thead>
                      <tbody>
                        {items.map((p) => {
                          const n = nodes.get(p.id)!;
                          const st = analysis?.objectStrategies[p.id];
                          return (
                            <tr key={p.id}>
                              <td className="name" title={n.name}>{n.name}</td>
                              <td>{n.type}</td>
                              <td className="ws" title={n.fabricTarget}>{n.fabricTarget}</td>
                              <td><ClassificationBadge value={n.classification} /></td>
                              <td>{st ? <StatusBadge tone={STRATEGY_TONE[st.strategy]}>{st.label}</StatusBadge> : <span className="faint">—</span>}</td>
                              <td className="num">{n.dependsOn}</td>
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
                </Collapsible>
              );
            })}
          </div>
        )}
      </Card>
    </>
  );
}
