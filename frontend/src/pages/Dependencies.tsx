import { ListPlus, Network, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { DependencyGraph } from "../components/discovery/DependencyGraph";
import { ObjectDetails } from "../components/discovery/ObjectDetails";
import { ClassificationBadge, MetricCard } from "../components/shared/Metrics";
import { Banner, Button, Card, EmptyState, ErrorState, LoadingState, PageHead } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";

/** Largest number of nodes drawn at once; beyond it the graph is unreadable and slow. */
const MAX_NODES = 250;

export function Dependencies() {
  const { discovery, isConnected } = useAppState();
  const { graph, graphError, graphLoading, reloadGraph, addToPlan, inPlan } = useMigration();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [type, setType] = useState("");
  const [cls, setCls] = useState("");
  const [wave, setWave] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const done = discovery.state === "completed" || discovery.state === "completed_with_warnings";

  const filtered = useMemo(() => {
    if (!graph) return { nodes: [], total: 0 };
    const term = search.trim().toLowerCase();
    const all = graph.nodes.filter((n) =>
      (!term || n.name.toLowerCase().includes(term)) && (!type || n.type === type) && (!cls || n.classification === cls) && (!wave || n.wave === wave));
    // With no narrowing, show the objects that actually have dependency
    // relationships first: an isolated node says nothing about dependencies.
    const ranked = [...all].sort((a, b) => (b.dependsOn + b.dependedOnBy) - (a.dependsOn + a.dependedOnBy));
    return { nodes: ranked.slice(0, MAX_NODES), total: all.length };
  }, [graph, search, type, cls, wave]);

  const byId = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const node = selected ? byId.get(selected) : undefined;
  const needs = useMemo(() => (graph?.edges ?? []).filter((e) => e.source === selected).map((e) => byId.get(e.target)).filter(Boolean), [graph, selected, byId]);
  const neededBy = useMemo(() => (graph?.edges ?? []).filter((e) => e.target === selected).map((e) => byId.get(e.source)).filter(Boolean), [graph, selected, byId]);
  const types = useMemo(() => [...new Set((graph?.nodes ?? []).map((n) => n.type))].sort(), [graph]);

  let body;
  if (!done) {
    body = (
      <Card>
        <EmptyState icon={<Network size={22} />} title={isConnected ? "Discovery has not been executed." : "No Synapse workspace connected."} actions={<Button variant="primary" onClick={() => navigate(isConnected ? "/discovery" : "/synapse")}>{isConnected ? "Run Discovery" : "Connect Synapse"}</Button>}>
          Dependencies and waves are built from the discovered objects.
        </EmptyState>
      </Card>
    );
  } else if (graphLoading && !graph) body = <Card><LoadingState label="Building the dependency graph…" /></Card>;
  else if (graphError) body = <Card><ErrorState title="Could not load the dependency graph" message={graphError} actions={<Button onClick={reloadGraph}>Retry</Button>} /></Card>;
  else if (graph) {
    body = (
      <>
        <div className="grid cols-4">
          <MetricCard label="Objects" value={graph.nodes.length.toLocaleString()} />
          <MetricCard label="Dependencies" value={graph.edges.length.toLocaleString()} hint="Links between discovered objects" />
          <MetricCard label="Migration waves" value={graph.waves.length} hint="Suggested grouping, ordered by dependency" />
          <MetricCard label="Objects with dependencies" value={graph.nodes.filter((n) => n.dependsOn + n.dependedOnBy > 0).length.toLocaleString()} />
        </div>

        <Card title="Migration waves" subtitle="A suggested order: nothing is scheduled before the objects it depends on. Select a wave to focus the graph.">
          <div className="ws-grid">
            {graph.waves.map((w) => (
              <button key={w.wave} type="button" className="ws-card" aria-pressed={wave === w.wave} onClick={() => setWave(wave === w.wave ? 0 : w.wave)}>
                <span className="ws-name">Wave {w.wave}</span>
                <span className="ws-count">{w.count.toLocaleString()}</span>
                <span className="faint">{Object.entries(w.types).map(([t, n]) => `${t} ${n}`).join(" · ")}</span>
              </button>
            ))}
          </div>
        </Card>

        <Card title="Dependency graph" subtitle="Columns are waves. An arrow points from an object to what it needs. Drag to pan, scroll to zoom.">
          <div className="toolbar" style={{ marginBottom: 12 }}>
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
          {filtered.total > MAX_NODES && (
            <Banner tone="info">Showing the {MAX_NODES} most connected of {filtered.total.toLocaleString()} matching objects. Narrow the filters to see others.</Banner>
          )}
          {filtered.nodes.length === 0 ? (
            <EmptyState title="No objects match these filters" />
          ) : (
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
                ) : (
                  <p className="muted">Select an object to see what it needs and what needs it.</p>
                )}
              </aside>
            </div>
          )}
        </Card>
      </>
    );
  }

  return (
    <div className="page wide">
      <PageHead icon={Network} title="Dependencies & Migration Waves">
        See what depends on what, and the order objects should be migrated in so nothing is attempted before what it needs.
      </PageHead>
      {body}
      {open && <ObjectDetails id={open} onClose={() => setOpen(null)} onNavigate={setOpen} />}
    </div>
  );
}
