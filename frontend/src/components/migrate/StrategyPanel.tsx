import { Info, ListPlus, Route, Search, Trash2 } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { ClassificationBadge } from "../shared/Metrics";
import { notify } from "../shared/notify";
import { Banner, Button, Collapsible, ConfirmDialog, StatusBadge, type Tone } from "../shared/Shared";
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

/** Working days, to one decimal: 48.63 reads as 48.6, and anything under 0.1 as "<0.1". */
export const days = (value: number) => (value > 0 && value < 0.05 ? "<0.1" : (Math.round(value * 10) / 10).toLocaleString());

/** How each kind of object is migrated: one row per object type, and the totals. */
export function StrategyTable() {
  const { analysis } = useMigration();
  if (!analysis) return <p className="muted" style={{ margin: 0 }}>Build a plan to see the strategy for each object type.</p>;
  const total = analysis.typeStrategies.reduce((a, t) => a + t.count, 0);
  const effort = analysis.typeStrategies.reduce((a, t) => a + t.effortDays, 0);
  return (
    <div className="table-wrap">
      <table className="data" style={{ minWidth: 860 }}>
        <caption className="sr-only">Migration strategy by object type</caption>
        <thead><tr>{["Object type", "Objects", "Strategy", "Fabric target", "Waves", "Effort"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
        <tbody>
          {analysis.typeStrategies.map((t) => (
            <tr key={t.type}>
              <td className="name">{t.type}</td>
              <td className="num">{t.count.toLocaleString()}</td>
              <td title={STRATEGY_TEXT[t.strategy]}><StatusBadge tone={STRATEGY_TONE[t.strategy]}>{t.strategyLabel}</StatusBadge></td>
              <td className="ws" title={t.target}>{t.target || "—"}</td>
              <td>{t.firstWave === t.lastWave ? `Wave ${t.firstWave}` : `Waves ${t.firstWave}–${t.lastWave}`}</td>
              <td className="num">{days(t.effortDays)}d</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="total-row">
            <th scope="row">All types</th>
            <td className="num">{total.toLocaleString()}</td>
            <td colSpan={3} className="faint">{analysis.typeStrategies.length} object types</td>
            <td className="num">{days(effort)}d</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

/** Rows shown per wave before "Show all": a wave of a thousand tables stays quick to open. */
const ROWS_PER_WAVE = 100;

/**
 * The plan itself: add objects (all, by type, or by name), find them, move them between
 * waves and take them out. The order inside a wave is set by object type, so it is explained
 * rather than offered as something to rearrange.
 */
export function PlanEditor({ find, onFind }: { find: string; onFind: (value: string) => void }) {
  const { graph, plan, analysis, addAllToPlan, addToPlan, removeFromPlan, removeManyFromPlan, setPlanWave, clearPlan } = useMigration();
  const [pick, setPick] = useState("");
  const [pickWave, setPickWave] = useState(0);
  const [askClear, setAskClear] = useState(false);
  const [showAll, setShowAll] = useState<Set<number>>(new Set());

  const nodes = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const byName = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.name.toLowerCase(), n])), [graph]);
  const planned = useMemo(() => new Set(plan.map((p) => p.id)), [plan]);
  const notPlanned = useMemo(() => (graph?.nodes ?? []).filter((n) => !planned.has(n.id)), [graph, planned]);
  const typesNotPlanned = useMemo(() => {
    const counts = new Map<string, number>();
    for (const n of notPlanned) counts.set(n.type, (counts.get(n.type) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [notPlanned]);
  const visible = plan.filter((p) => nodes.has(p.id));
  const orphans = plan.filter((p) => !nodes.has(p.id)).map((p) => p.id);
  const q = find.trim().toLowerCase();
  const shown = q ? visible.filter((p) => { const n = nodes.get(p.id)!; return n.name.toLowerCase().includes(q) || n.type.toLowerCase().includes(q); }) : visible;
  const waves = [...new Set(shown.map((p) => p.wave))].sort((a, b) => a - b);
  const waveChoices = Array.from({ length: Math.max(5, ...(graph?.waves.map((w) => w.wave) ?? [0]), ...visible.map((p) => p.wave)) }, (_, i) => i + 1);
  const picked = byName.get(pick.trim().toLowerCase());

  const addPicked = () => {
    if (!picked) return;
    if (planned.has(picked.id)) { notify(`${picked.name} is already in the plan, in wave ${plan.find((p) => p.id === picked.id)?.wave}.`); return; }
    addToPlan([picked.id]);
    if (pickWave) setPlanWave(picked.id, pickWave);
    notify(`${picked.name} added to the plan${pickWave ? `, in wave ${pickWave}` : ""}.`);
    setPick("");
  };
  const addType = (type: string) => {
    const ids = notPlanned.filter((n) => n.type === type).map((n) => n.id);
    if (!ids.length) return;
    addToPlan(ids);
    notify(`${ids.length.toLocaleString()} ${type} object${ids.length === 1 ? "" : "s"} added to the plan in their suggested waves.`);
  };
  const addRest = () => {
    const before = notPlanned.length;
    addAllToPlan();
    notify(`${before.toLocaleString()} object${before === 1 ? "" : "s"} added to the plan in their suggested waves.`);
  };
  const close = useCallback(() => setAskClear(false), []);

  return (
    <div className="stack">
      <div className="plan-tools" role="group" aria-label="Add objects to the plan">
        <Button onClick={addRest} disabled={!notPlanned.length} title={notPlanned.length ? undefined : "Every discovered object is in the plan"}>
          <ListPlus size={14} aria-hidden="true" />
          {!notPlanned.length ? "Every object is in the plan" : plan.length ? `Add the ${notPlanned.length.toLocaleString()} not in the plan` : `Add all ${notPlanned.length.toLocaleString()} objects`}
        </Button>
        <select className="select" aria-label="Add every object of a type" value="" disabled={!typesNotPlanned.length}
          onChange={(e) => { if (e.target.value) addType(e.target.value); }}>
          <option value="">Add every object of a type…</option>
          {typesNotPlanned.map(([t, n]) => <option key={t} value={t}>{t} ({n.toLocaleString()})</option>)}
        </select>
        <input className="input" style={{ maxWidth: 260 }} list="plan-objects" placeholder="Add one object by name" aria-label="Object to add" value={pick}
          onChange={(e) => setPick(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addPicked(); } }} />
        <datalist id="plan-objects">{notPlanned.slice(0, 400).map((n) => <option key={n.id} value={n.name}>{n.type}</option>)}</datalist>
        <select className="select" aria-label="Wave for the added object" value={pickWave} onChange={(e) => setPickWave(Number(e.target.value))}>
          <option value={0}>Its suggested wave</option>{waveChoices.map((w) => <option key={w} value={w}>Wave {w}</option>)}
        </select>
        <Button onClick={addPicked} disabled={!picked}>Add to plan</Button>
      </div>

      <div className="plan-tools">
        <div className="input-wrap plan-find">
          <Search size={15} aria-hidden="true" />
          <input className="input" placeholder="Find in the plan by name or type" aria-label="Find in the plan" value={find} onChange={(e) => onFind(e.target.value)} />
        </div>
        {q && <span className="muted" role="status">{shown.length.toLocaleString()} of {visible.length.toLocaleString()} objects match</span>}
        {q && <Button size="small" variant="ghost" onClick={() => onFind("")}>Clear</Button>}
        <span className="spacer" />
        <Button variant="ghost" onClick={() => setAskClear(true)} disabled={!plan.length}><Trash2 size={14} aria-hidden="true" />Clear plan</Button>
      </div>

      {orphans.length > 0 && (
        <Banner tone="warning" title={`${orphans.length.toLocaleString()} object${orphans.length === 1 ? " in the plan is" : "s in the plan are"} not in the latest discovery`}
          actions={<Button size="small" onClick={() => { removeManyFromPlan(orphans); notify(`${orphans.length.toLocaleString()} object${orphans.length === 1 ? "" : "s"} taken out of the plan.`); }}>Take them out</Button>}>
          They were discovered before and are not there any more, so they cannot be migrated.
        </Banner>
      )}

      {visible.length === 0 ? (
        <p className="muted" style={{ marginBottom: 0 }}><Route size={14} aria-hidden="true" /> The plan is empty. Add every discovered object in its suggested wave, a whole type, or one object by name.</p>
      ) : shown.length === 0 ? (
        <p className="muted" style={{ marginBottom: 0 }}>No object in the plan matches “{find.trim()}”.</p>
      ) : (
        <>
          <p className="faint note-line"><Info size={13} aria-hidden="true" /> Each wave starts after the one before it. Inside a wave, objects run by type (Warehouse and schemas, connections, tables, views and procedures, data, notebooks, jobs, pipelines, schedules), so nothing starts before what it needs. Move an object to another wave to change when it runs.</p>
          <div className="stack">
            {waves.map((w) => {
              const items = shown.filter((p) => p.wave === w);
              const auto = items.filter((p) => analysis?.objectStrategies[p.id]?.strategy === "automated").length;
              const limit = showAll.has(w) ? items.length : ROWS_PER_WAVE;
              return (
                <Collapsible key={`${w}-${q ? "find" : "all"}`} defaultOpen={!!q}
                  summary={<span><strong>Wave {w}</strong> <span className="muted">· {items.length.toLocaleString()} {q ? "matching" : `object${items.length === 1 ? "" : "s"}`}{analysis ? ` · ${auto.toLocaleString()} automated` : ""}</span></span>}>
                  <div className="table-wrap">
                    <table className="data" style={{ minWidth: 900 }}>
                      <caption className="sr-only">Wave {w} objects</caption>
                      <thead><tr>{["Object", "Source", "Fabric target", "Classification", "Strategy", "Needs", ""].map((h) => <th key={h || "actions"} scope="col" className="static">{h || <span className="sr-only">Actions</span>}</th>)}</tr></thead>
                      <tbody>
                        {items.slice(0, limit).map((p) => {
                          const n = nodes.get(p.id)!;
                          const st = analysis?.objectStrategies[p.id];
                          return (
                            <tr key={p.id}>
                              <td className="name" title={n.name}>{n.name}</td>
                              <td>{n.type}</td>
                              <td className="ws" title={n.fabricTarget}>{n.fabricTarget}</td>
                              <td><ClassificationBadge value={n.classification} /></td>
                              <td>{st ? <StatusBadge tone={STRATEGY_TONE[st.strategy]}>{st.label}</StatusBadge> : <span className="faint">—</span>}</td>
                              <td className="num" title={`${n.dependsOn} object${n.dependsOn === 1 ? "" : "s"} it needs`}>{n.dependsOn}</td>
                              <td>
                                <div className="row" style={{ flexWrap: "nowrap", gap: 4 }}>
                                  <select className="select" style={{ height: 30 }} aria-label={`Wave for ${n.name}`} value={p.wave} onChange={(e) => setPlanWave(p.id, Number(e.target.value))}>
                                    {waveChoices.map((x) => <option key={x} value={x}>Wave {x}</option>)}
                                  </select>
                                  <Button size="small" icon variant="ghost" aria-label={`Take ${n.name} out of the plan`} title="Take it out of the plan" onClick={() => removeFromPlan(p.id)}><Trash2 size={14} /></Button>
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  {items.length > limit && (
                    <Button size="small" variant="ghost" onClick={() => setShowAll((s) => new Set(s).add(w))}>
                      Show all {items.length.toLocaleString()} objects in wave {w}
                    </Button>
                  )}
                </Collapsible>
              );
            })}
          </div>
        </>
      )}

      {askClear && (
        <ConfirmDialog title="Clear the plan?" confirmLabel="Clear plan" onCancel={close}
          onConfirm={() => { const n = plan.length; clearPlan(); setAskClear(false); notify(`The plan was cleared (${n.toLocaleString()} objects).`); }}>
          <p>All {plan.length.toLocaleString()} objects are taken out of the plan. The stage options and any credentials you typed are kept.</p>
          <p className="muted">Nothing in Synapse or Fabric changes. You can add everything back in its suggested wave with one click.</p>
        </ConfirmDialog>
      )}
    </div>
  );
}
