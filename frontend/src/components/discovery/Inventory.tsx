import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, Search, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useDebounce } from "../../hooks/useDebounce";
import { useAppState } from "../../state/AppState";
import type { DiscoverySummary, ResultsPage, ResultsQuery, SortKey } from "../../types";
import { ClassificationBadge } from "../shared/Metrics";
import { Banner, Button, EmptyState, Skeleton } from "../shared/Shared";
import { ObjectDetails } from "./ObjectDetails";
import { ObjectStatusBadge } from "./ObjectStatusBadge";

const PAGE_SIZES = [25, 50, 100];

interface Tab { id: string; label: string; categories?: string[]; types?: string[] }
const TABS: Tab[] = [
  { id: "all", label: "All Objects" },
  { id: "sql", label: "SQL Objects", categories: ["SQL"] },
  { id: "pipelines", label: "Pipelines", types: ["Pipeline", "Trigger"] },
  { id: "spark", label: "Spark", categories: ["Spark"] },
  { id: "storage", label: "Storage", categories: ["Storage"] },
  { id: "security", label: "Security", categories: ["Security"] },
  { id: "networking", label: "Networking", categories: ["Networking"] },
  { id: "integration", label: "Integration", types: ["Dataset", "Linked Service"] },
];

const COLUMNS: { key: SortKey; label: string; cls?: string }[] = [
  { key: "name", label: "Object name" },
  { key: "type", label: "Object type" },
  { key: "category", label: "Synapse component" },
  { key: "workspace", label: "Schema" },
  { key: "dependencies", label: "Dependencies", cls: "num" },
  { key: "type", label: "Count / size" },
  { key: "status", label: "Status" },
  { key: "target", label: "Fabric equivalent" },
  { key: "classification", label: "Migration classification" },
];
// "Schema" and "Count / size" are not separately sortable server-side; they
// sort by the nearest meaningful column instead.
const SORTABLE = new Set(["Object name", "Object type", "Synapse component", "Dependencies", "Status", "Fabric equivalent", "Migration classification"]);

const INITIAL = {
  search: "", tab: "all", type: "", status: "", target: "", path: "", classification: "", assessment: "" as "" | "yes" | "no",
  sort: "name" as SortKey, dir: "asc" as "asc" | "desc", page: 1, pageSize: 25,
};

interface Props {
  summary: DiscoverySummary;
  /** Set by workstream cards on the page; "" means no workstream filter. */
  workstream?: string;
  onWorkstream?: (workstream: string) => void;
  /** A classification chosen elsewhere on the page (for example an Assessment card). */
  classification?: string;
  /** An object type chosen elsewhere on the page (for example a Discovery card). */
  type?: string;
  showTabs?: boolean;
}

const tabCount = (tab: Tab, s: DiscoverySummary) =>
  tab.categories ? tab.categories.reduce((n, c) => n + (s.byCategory[c] ?? 0), 0)
  : tab.types ? tab.types.reduce((n, t) => n + (s.byType[t] ?? 0), 0)
  : s.total;

/** The object inventory: tabs, filters, sortable columns, pagination. One page of rows is ever in the DOM. */
export function Inventory({ summary, workstream = "", onWorkstream, classification = "", type = "", showTabs = true }: Props) {
  const { getResults } = useAppState();
  const [f, setF] = useState(INITIAL);
  const [searchText, setSearchText] = useState("");
  const debounced = useDebounce(searchText, 300);
  const [data, setData] = useState<ResultsPage | null>(null);
  const [facets, setFacets] = useState<ResultsPage["facets"] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const ticket = useRef(0);

  useEffect(() => { setF((s) => (s.search === debounced ? s : { ...s, search: debounced, page: 1 })); }, [debounced]);
  useEffect(() => { setF((s) => ({ ...s, page: 1 })); }, [workstream]);
  useEffect(() => { setF((s) => ({ ...s, classification, page: 1 })); }, [classification]);
  useEffect(() => { setF((s) => ({ ...s, type, tab: type ? "all" : s.tab, page: 1 })); }, [type]);

  useEffect(() => {
    const mine = ++ticket.current;
    const one = (v: string) => (v ? [v] : []);
    const tab = TABS.find((t) => t.id === f.tab) ?? TABS[0];
    const query: ResultsQuery = {
      search: f.search, categories: tab.categories ?? [], types: f.type ? [f.type] : tab.types ?? [], statuses: one(f.status),
      fabricTargets: one(f.target), paths: one(f.path), workstreams: one(workstream), mappingStatuses: [],
      classifications: one(f.classification), assessment: f.assessment, sort: f.sort, dir: f.dir, page: f.page, pageSize: f.pageSize,
    };
    setLoading(true);
    setError(null);
    getResults(query).then(
      (page) => {
        if (mine !== ticket.current) return; // a newer request superseded this one
        setData(page);
        setFacets(page.facets);
        setLoading(false);
      },
      (e) => {
        if (mine !== ticket.current) return;
        setError(e instanceof Error ? e.message : "Results could not be loaded.");
        setLoading(false);
      },
    );
  }, [f, workstream, getResults, attempt]);

  const patch = (p: Partial<typeof INITIAL>) => setF((s) => ({ ...s, ...p, page: "page" in p ? p.page! : 1 }));
  const clear = () => { setSearchText(""); onWorkstream?.(""); setF({ ...INITIAL, classification, type, sort: f.sort, dir: f.dir, pageSize: f.pageSize }); };
  const filtered = !!(f.search || f.status || f.target || f.path || f.assessment || workstream || (!classification && f.classification) || (!type && f.type));
  const sortBy = (key: SortKey) => patch({ sort: key, dir: f.sort === key && f.dir === "asc" ? "desc" : "asc", page: 1 });

  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / f.pageSize));
  const from = total ? (f.page - 1) * f.pageSize + 1 : 0;
  const to = Math.min(total, f.page * f.pageSize);
  const options = (counts?: Record<string, number>) => Object.entries(counts ?? {}).map(([k, n]) => <option key={k} value={k}>{k} ({n})</option>);

  return (
    <div className="stack" style={{ minWidth: 0 }}>
      {showTabs && (
        <div className="tabs tabs-pill" role="tablist" aria-label="Object groups">
          {TABS.map((t) => {
            const n = tabCount(t, summary);
            return (
              <button key={t.id} role="tab" type="button" className="tab" aria-selected={f.tab === t.id} onClick={() => patch({ tab: t.id, type: "" })}>
                {t.label} <span className="tab-count">{n.toLocaleString()}</span>
              </button>
            );
          })}
        </div>
      )}

      <div className="toolbar">
        <div className="search">
          <Search size={15} aria-hidden="true" />
          <input className="input" type="search" placeholder="Search by object, type, Fabric target or path" aria-label="Search objects" value={searchText} onChange={(e) => setSearchText(e.target.value)} />
        </div>
        <select className="select" aria-label="Filter by Synapse object type" value={f.type} onChange={(e) => patch({ type: e.target.value })}>
          <option value="">All object types</option>{options(facets?.types)}
        </select>
        <select className="select" aria-label="Filter by Fabric target" value={f.target} onChange={(e) => patch({ target: e.target.value })}>
          <option value="">All Fabric targets</option>{options(facets?.fabricTargets)}
        </select>
        <select className="select" aria-label="Filter by classification" value={f.classification} onChange={(e) => patch({ classification: e.target.value })}>
          <option value="">All classifications</option>{options(facets?.classifications)}
        </select>
        <select className="select" aria-label="Filter by migration path" value={f.path} onChange={(e) => patch({ path: e.target.value })}>
          <option value="">All migration paths</option>{options(facets?.paths)}
        </select>
        <select className="select" aria-label="Filter by discovery status" value={f.status} onChange={(e) => patch({ status: e.target.value })}>
          <option value="">All statuses</option>{options(facets?.statuses)}
        </select>
        {workstream && onWorkstream && (
          <button type="button" className="chip" onClick={() => onWorkstream("")} aria-label={`Remove workstream filter ${workstream}`}>
            Workstream: {workstream} <X size={13} aria-hidden="true" />
          </button>
        )}
        <Button variant="ghost" onClick={clear} disabled={!filtered}><X size={14} aria-hidden="true" />Clear filters</Button>
      </div>

      {error && <Banner tone="error" title="Could not load results" actions={<Button size="small" onClick={() => setAttempt((n) => n + 1)}>Retry</Button>}>{error}</Banner>}

      <div className="table-wrap" aria-busy={loading}>
        <table className="data">
          <caption className="sr-only">Discovered Synapse objects and their Fabric equivalents</caption>
          <thead>
            <tr>
              {COLUMNS.map((c) => (
                <th key={c.label} scope="col" aria-sort={SORTABLE.has(c.label) && f.sort === c.key ? (f.dir === "asc" ? "ascending" : "descending") : "none"}>
                  {SORTABLE.has(c.label) ? (
                    <button type="button" onClick={() => sortBy(c.key)}>
                      {c.label}
                      {f.sort === c.key ? (f.dir === "asc" ? <ArrowUp size={13} aria-hidden="true" /> : <ArrowDown size={13} aria-hidden="true" />) : <ArrowUpDown size={13} aria-hidden="true" style={{ opacity: 0.4 }} />}
                    </button>
                  ) : <span style={{ display: "block", padding: "10px 14px" }}>{c.label}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {!data && loading && Array.from({ length: 8 }, (_, i) => (
              <tr key={i}>{COLUMNS.map((c) => <td key={c.label}><Skeleton width={c.key === "name" ? 180 : 80} /></td>)}</tr>
            ))}
            {data?.items.map((o) => (
              <tr key={o.id} className={`clickable${selected === o.id ? " selected" : ""}`} tabIndex={0} onClick={() => setSelected(o.id)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelected(o.id); } }} aria-label={`${o.name}, ${o.type}, Fabric equivalent ${o.fabricTarget}. Open details`} style={{ opacity: loading ? 0.6 : 1 }}>
                <td className="name" title={o.name}>{o.name}</td>
                <td>{o.type}</td>
                <td className="muted">{o.component}</td>
                <td className="muted">{o.schema || "—"}</td>
                <td className="num">{o.dependencyCount}</td>
                <td className="muted">{o.size || "—"}</td>
                <td><ObjectStatusBadge status={o.status} /></td>
                <td className="ws" title={o.fabricTarget}>{o.fabricTarget}</td>
                <td><ClassificationBadge value={o.classification} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && total === 0 && !loading && (
          <EmptyState title={filtered ? "No objects match these filters" : "No objects"} actions={filtered ? <Button onClick={clear}>Clear filters</Button> : undefined}>
            {filtered ? "Try a different search term or remove a filter." : "Nothing was discovered in this group."}
          </EmptyState>
        )}
      </div>

      <div className="pager">
        <span className="muted" aria-live="polite">{total ? `Showing ${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}` : "0 results"}</span>
        <div className="row">
          <label className="muted" htmlFor="page-size">Rows</label>
          <select id="page-size" className="select" value={f.pageSize} onChange={(e) => patch({ pageSize: Number(e.target.value) })}>
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <Button size="small" icon aria-label="Previous page" disabled={f.page <= 1} onClick={() => patch({ page: f.page - 1 })}><ChevronLeft size={15} /></Button>
          <span className="muted" style={{ fontVariantNumeric: "tabular-nums" }}>Page {f.page} of {pages}</span>
          <Button size="small" icon aria-label="Next page" disabled={f.page >= pages} onClick={() => patch({ page: f.page + 1 })}><ChevronRight size={15} /></Button>
        </div>
      </div>

      {selected && <ObjectDetails id={selected} onClose={() => setSelected(null)} onNavigate={setSelected} />}
    </div>
  );
}
