import { ListPlus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { DependencyGraph } from "../../discovery/DependencyGraph";
import { ObjectDetails } from "../../discovery/ObjectDetails";
import { ClassificationBadge, StatStrip } from "../../shared/Metrics";
import { Banner, Button, EmptyState, ErrorState, LoadingState, Tabs } from "../../shared/Shared";
import { useMigration } from "../../../state/MigrationState";

/** Largest number of nodes drawn at once; beyond it the graph is unreadable and slow. */
const MAX_NODES = 250;

export function WavesPanel() {
  const { graph, graphError, graphLoading, reloadGraph, addToPlan, inPlan } = useMigration();
  const [tab, setTab] = useState<"waves" | "graph">("waves");
  const [search, setSearch] = useState("");
  const [type, setType] = useState("");
  const [cls, setCls] = useState("");
  const [wave, setWave] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const filtered = useMemo(() => {
    if (!graph) return { nodes: [], total: 0 };
    const term = search.trim().toLowerCase();
    const all = graph.nodes.filter((n) =>
      (!term || n.name.toLowerCase().includes(term)) && (!type || n.type === type) && (!cls || n.classification === cls) && (!wave || n.wave === wave));
    // Objects with relationships first: an isolated node says nothing about dependencies.
    const ranked = [...all].sort((a, b) => (b.dependsOn + b.dependedOnBy) - (a.dependsOn + a.dependedOnBy));
    return { nodes: ranked.slice(0, MAX_NODES), total: all.length };
  }, [graph, search, type, cls, wave]);

  const byId = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const node = selected ? byId.get(selected) : undefined;
  const needs = useMemo(() => (graph?.edges ?? []).filter((e) => e.source === selected).map((e) => byId.get(e.target)).filter(Boolean), [graph, selected, byId]);
  const neededBy = useMemo(() => (graph?.edges ?? []).filter((e) => e.target === selected).map((e) => byId.get(e.source)).filter(Boolean), [graph, selected, byId]);
  const types = useMemo(() => [...new Set((graph?.nodes ?? []).map((n) => n.type))].sort(), [graph]);

  if (graphLoading && !graph) return <LoadingState label="Building the dependency graph…" />;
  if (graphError) return <ErrorState title="Could not build the dependency graph" message={graphError} actions={<Button onClick={reloadGraph}>Retry</Button>} />;
  if (!graph) return <p className="muted">Waves are built from the discovered objects. Run discovery first.</p>;

  const largest = Math.max(1, ...graph.waves.map((w) => w.count));
  return (
    <div className="stack panel-body">
      <StatStrip label="Dependency summary" items={[
        { label: "Objects", value: graph.nodes.length.toLocaleString() },
        { label: "Dependencies", value: graph.edges.length.toLocaleString(), hint: "Links between discovered objects" },
        { label: "Waves", value: graph.waves.length, tone: "accent", hint: "Nothing is planned before what it needs" },
        { label: "With dependencies", value: graph.nodes.filter((n) => n.dependsOn + n.dependedOnBy > 0).length.toLocaleString() },
      ]} />

      <Tabs label="Waves sections" value={tab} onChange={setTab} tabs={[{ id: "waves", label: "Waves", count: graph.waves.length }, { id: "graph", label: "Dependency graph" }]} />

      {tab === "waves" ? (
        <ol className="wave-list" aria-label="Migration waves">
          {graph.waves.map((w) => (
            <li key={w.wave}>
              <button type="button" className="wave-row" onClick={() => { setWave(w.wave); setTab("graph"); }} title={`Show wave ${w.wave} in the graph`}>
                <span className="wave-badge">{w.wave}</span>
                <span className="wave-main">
                  <strong>Wave {w.wave}</strong>
                  <span className="faint">{Object.entries(w.types).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([t, c]) => `${t} ${c}`).join(" · ")}{Object.keys(w.types).length > 4 ? " · …" : ""}</span>
                </span>
                <span className="wave-bar" aria-hidden="true"><span style={{ width: `${(w.count / largest) * 100}%` }} /></span>
                <span className="wave-count">{w.count.toLocaleString()}</span>
              </button>
            </li>
          ))}
        </ol>
      ) : (
        <>
          <div className="toolbar">
            <div className="search">
              <Search size={15} aria-hidden="true" />
              <input className="input" type="search" placeholder="Search objects" aria-label="Search the graph" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
            <select className="select" aria-label="Filter by object type" value={type} onChange={(e) => setType(e.target.value)}>
              <option value="">All object types</option>{types.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <select className="select" aria-label="Filter by classification" value={cls} onChange={(e) => setCls(e.target.value)}>
              <option value="">All classifications</option>
              {["DIRECT", "RECONFIGURE", "TRANSFORM", "MANUAL", "REVIEW"].map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            <select className="select" aria-label="Filter by wave" value={wave} onChange={(e) => setWave(Number(e.target.value))}>
              <option value={0}>All waves</option>{graph.waves.map((w) => <option key={w.wave} value={w.wave}>Wave {w.wave}</option>)}
            </select>
          </div>
          {filtered.total > MAX_NODES && <Banner tone="info">Showing the {MAX_NODES} most connected of {filtered.total.toLocaleString()} matching objects. Narrow the filters to see others.</Banner>}
          {filtered.nodes.length === 0 ? <EmptyState title="No objects match these filters" /> : (
            <div className="graph-layout">
              <DependencyGraph nodes={filtered.nodes} edges={graph.edges} selectedId={selected} onSelect={setSelected} />
              <aside className="graph-side" aria-label="Selected object">
                {node ? (
                  <div className="stack">
                    <div><div className="eyebrow">Selected</div><h3 style={{ overflowWrap: "anywhere" }}>{node.name}</h3><span className="muted">{node.type} · Wave {node.wave}</span></div>
                    <ClassificationBadge value={node.classification} />
                    <div>
                      <div className="eyebrow">Needs ({needs.length})</div>
                      <ul className="plain">{needs.length ? needs.map((n) => n && <li key={n.id}><button type="button" className="link" onClick={() => setSelected(n.id)}>{n.name}</button> <span className="faint">{n.type}</span></li>) : <li className="faint">Nothing</li>}</ul>
                    </div>
                    <div>
                      <div className="eyebrow">Needed by ({neededBy.length})</div>
                      <ul className="plain">{neededBy.length ? neededBy.slice(0, 30).map((n) => n && <li key={n.id}><button type="button" className="link" onClick={() => setSelected(n.id)}>{n.name}</button> <span className="faint">{n.type}</span></li>) : <li className="faint">Nothing</li>}</ul>
                    </div>
                    <div className="row">
                      <Button size="small" onClick={() => setOpen(node.id)}>Open details</Button>
                      <Button size="small" variant="primary" disabled={inPlan(node.id)} onClick={() => addToPlan([node.id])}><ListPlus size={14} aria-hidden="true" />{inPlan(node.id) ? "In plan" : "Add to plan"}</Button>
                    </div>
                  </div>
                ) : <p className="muted">Select an object to see what it needs and what needs it.</p>}
              </aside>
            </div>
          )}
        </>
      )}
      {open && <ObjectDetails id={open} onClose={() => setOpen(null)} onNavigate={setOpen} />}
    </div>
  );
}
