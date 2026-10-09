import {
  ChevronDown, ChevronRight, Clock, Cpu, Database, Eye, File, FileCode2, FileJson, Folder, FolderOpen, FolderTree, HardDrive,
  Layers, Link2, NotebookPen, Package, Sparkles, SquareFunction, Table2, TableProperties, Workflow, Zap, type LucideIcon,
} from "lucide-react";
import { useMemo, useState } from "react";
import { ObjectDetails } from "../discovery/ObjectDetails";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import type { GraphNode } from "../../types";

/** One row of the tree: a folder (has children) or an object (has an id). */
interface TreeNode {
  key: string;
  label: string;
  icon: LucideIcon;
  /** Objects inside, at any depth, for the count beside a folder. */
  count: number;
  children?: TreeNode[];
  objectId?: string;
  type?: string;
}

/** Workspace artifacts, in the folders a Synapse Git repository keeps them in. */
const REPO_FOLDERS: { folder: string; types: string[]; icon: LucideIcon }[] = [
  { folder: "linkedService", types: ["Linked Service"], icon: Link2 },
  { folder: "integrationRuntime", types: ["Integration Runtime"], icon: Cpu },
  { folder: "dataset", types: ["Dataset"], icon: FileJson },
  { folder: "pipeline", types: ["Pipeline"], icon: Workflow },
  { folder: "notebook", types: ["Notebook"], icon: NotebookPen },
  { folder: "sqlscript", types: ["SQL Script"], icon: FileCode2 },
  { folder: "sparkJobDefinition", types: ["Spark Job Definition"], icon: Zap },
  { folder: "trigger", types: ["Trigger"], icon: Clock },
  { folder: "bigDataPool", types: ["Spark Pool"], icon: Sparkles },
  { folder: "library", types: ["Spark Library"], icon: Package },
  { folder: "storage", types: ["Storage Reference"], icon: HardDrive },
];

/** What a dedicated SQL pool holds, per schema, in the order Synapse Studio lists it. */
const SQL_GROUPS: { label: string; type: string; icon: LucideIcon }[] = [
  { label: "Tables", type: "Table", icon: Table2 },
  { label: "External tables", type: "External Table", icon: TableProperties },
  { label: "Views", type: "View", icon: Eye },
  { label: "Stored procedures", type: "Stored Procedure", icon: SquareFunction },
  { label: "Functions", type: "Function", icon: SquareFunction },
];
const SQL_TYPES = new Set(SQL_GROUPS.map((g) => g.type));
const POOL = "Dedicated SQL Pool";
const SCHEMA = "Schema";
/** How many objects a folder lists before "Show more". */
const PAGE = 200;

const byName = (a: { label: string }, b: { label: string }) => a.label.localeCompare(b.label, undefined, { numeric: true, sensitivity: "base" });
const leaf = (n: GraphNode, icon: LucideIcon, label = n.name): TreeNode => ({ key: n.id, label, icon, count: 1, objectId: n.id, type: n.type });
/** A folder. ``self`` is the object a folder also stands for (a pool, a schema), counted once with what it holds. */
const folder = (key: string, label: string, children: TreeNode[], icon: LucideIcon = Folder, self?: GraphNode): TreeNode =>
  ({ key, label, icon, children, count: children.reduce((s, c) => s + c.count, 0) + (self ? 1 : 0), objectId: self?.id, type: self?.type });

/** "dbo.FactTrip" -> ["dbo", "FactTrip"]; a name without a schema sits under dbo. */
function splitSchema(name: string): [string, string] {
  const dot = name.indexOf(".");
  return dot > 0 ? [name.slice(0, dot), name.slice(dot + 1)] : ["dbo", name];
}

/** The discovered objects as a repository-style tree: artifact folders, then the SQL pool by schema, then anything else by type. */
export function buildTree(nodes: GraphNode[]): TreeNode[] {
  const out: TreeNode[] = [];
  for (const f of REPO_FOLDERS) {
    const items = nodes.filter((n) => f.types.includes(n.type)).map((n) => leaf(n, f.icon)).sort(byName);
    if (items.length) out.push(folder(`repo:${f.folder}`, f.folder, items));
  }

  // The SQL pool: pool > schema > Tables / Views / ... > objects.
  const pools = nodes.filter((n) => n.type === POOL);
  const sql = nodes.filter((n) => SQL_TYPES.has(n.type));
  const schemaOf = (n: GraphNode) => n.name.split(".").pop() ?? n.name;
  const schemaNodes = new Map(nodes.filter((n) => n.type === SCHEMA).map((n) => [schemaOf(n), n]));
  const schemaNames = new Set(schemaNodes.keys());
  for (const n of sql) schemaNames.add(splitSchema(n.name)[0]);
  if (pools.length || sql.length) {
    const schemas = [...schemaNames].map((schema) => {
      const groups = SQL_GROUPS.map((g) => {
        const items = sql.filter((n) => n.type === g.type && splitSchema(n.name)[0] === schema)
          .map((n) => leaf(n, g.icon, splitSchema(n.name)[1])).sort(byName);
        return items.length ? folder(`sql:${schema}:${g.type}`, g.label, items) : null;
      }).filter((g): g is TreeNode => g !== null);
      return folder(`sql:${schema}`, schema, groups, Layers, schemaNodes.get(schema));
    }).sort(byName);
    // One pool holds every schema; with several, the objects cannot be told apart by pool, so they sit beside them.
    const poolNodes = pools.length === 1
      ? [folder(`pool:${pools[0].id}`, pools[0].name, schemas, Database, pools[0])]
      : [...pools.map((p) => folder(`pool:${p.id}`, p.name, [], Database, p)), ...(schemas.length ? [folder("sql:schemas", "schemas", schemas)] : [])];
    out.push(folder("repo:sqlPool", "sqlPool", poolNodes.sort(byName)));
  }

  // Anything else, by its own type, so nothing discovered is left out.
  const placed = new Set<string>([...REPO_FOLDERS.flatMap((f) => f.types), ...SQL_TYPES, POOL, SCHEMA]);
  const rest = new Map<string, GraphNode[]>();
  for (const n of nodes) if (!placed.has(n.type)) rest.set(n.type, [...(rest.get(n.type) ?? []), n]);
  if (rest.size) {
    out.push(folder("repo:other", "other", [...rest.entries()].map(([type, items]) =>
      folder(`other:${type}`, type, items.map((n) => leaf(n, File)).sort(byName))).sort(byName)));
  }
  return out;
}

/** The tree with only what matches the search, keeping the folders on the way to each match. */
function filterTree(nodes: TreeNode[], q: string): TreeNode[] {
  const out: TreeNode[] = [];
  for (const n of nodes) {
    if (n.children) {
      const kids = filterTree(n.children, q);
      const self = n.objectId && n.label.toLowerCase().includes(q) ? 1 : 0;
      if (kids.length || self) out.push({ ...n, children: kids, count: kids.reduce((s, c) => s + c.count, 0) + self });
    } else if (n.label.toLowerCase().includes(q)) {
      out.push(n);
    }
  }
  return out;
}

function Branch({ nodes, depth, open, toggle, limits, more, selected, onPick, searching }: {
  nodes: TreeNode[]; depth: number; open: Set<string>; toggle: (k: string) => void;
  limits: Record<string, number>; more: (k: string) => void; selected: string | null; onPick: (id: string) => void; searching: boolean;
}) {
  return (
    <ul className="tree-list">
      {nodes.map((n) => {
        const pad = { paddingLeft: 8 + depth * 14 };
        if (!n.children) {
          const Icon = n.icon;
          return (
            <li key={n.key}>
              <button type="button" className={`tree-row tree-leaf${selected === n.objectId ? " selected" : ""}`} style={pad}
                title={`${n.label} · ${n.type}`} aria-label={`${n.label} (${n.type})`} onClick={() => onPick(n.objectId!)}>
                <span className="tree-caret" aria-hidden="true" />
                <Icon size={14} className="tree-icon" aria-hidden="true" />
                <span className="tree-label">{n.label}</span>
              </button>
            </li>
          );
        }
        // While searching, every folder on the way to a match is open.
        const expanded = searching || open.has(n.key);
        const Icon = n.icon === Folder ? (expanded ? FolderOpen : Folder) : n.icon;
        const shown = limits[n.key] ?? PAGE;
        const kids = n.children;
        return (
          <li key={n.key}>
            <button type="button" className="tree-row tree-folder" style={pad} aria-expanded={expanded} onClick={() => toggle(n.key)}>
              <span className="tree-caret" aria-hidden="true">{expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
              <Icon size={15} className="tree-icon folder" aria-hidden="true" />
              <span className="tree-label">{n.label}</span>
              <span className="tree-count">{n.count.toLocaleString()}</span>
            </button>
            {expanded && (
              <>
                <Branch nodes={kids.slice(0, shown)} depth={depth + 1} open={open} toggle={toggle} limits={limits} more={more}
                  selected={selected} onPick={onPick} searching={searching} />
                {kids.length > shown && (
                  <button type="button" className="tree-row tree-more" style={{ paddingLeft: 8 + (depth + 1) * 14 + 18 }} onClick={() => more(n.key)}>
                    Show {Math.min(PAGE, kids.length - shown).toLocaleString()} more of {(kids.length - shown).toLocaleString()}
                  </button>
                )}
              </>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The discovered workspace as a repository explorer: its artifacts in the folders a Synapse Git
 * repository uses, the SQL pool by schema, and anything else by type. Read-only; an object opens
 * its details.
 */
export function WorkspaceExplorer() {
  const { connection, discovery } = useAppState();
  const { graph, graphLoading, graphError } = useMigration();
  const [open, setOpen] = useState<Set<string>>(() => new Set(["root"]));
  const [limits, setLimits] = useState<Record<string, number>>({});
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const tree = useMemo(() => buildTree(graph?.nodes ?? []), [graph]);
  const q = query.trim().toLowerCase();
  const shown = useMemo(() => (q ? filterTree(tree, q) : tree), [tree, q]);
  const total = tree.reduce((s, n) => s + n.count, 0);
  const toggle = (k: string) => setOpen((o) => { const n = new Set(o); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  const more = (k: string) => setLimits((l) => ({ ...l, [k]: (l[k] ?? PAGE) + PAGE }));
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const rootOpen = !!q || open.has("root");

  let body;
  if (graphLoading) body = <p className="explorer-note" role="status"><span className="spinner" aria-hidden="true" /> Reading the workspace…</p>;
  else if (graphError) body = <p className="explorer-note err">{graphError}</p>;
  else if (!discovered || !graph) body = <p className="explorer-note">Run Discover to list the workspace's folders and objects here.</p>;
  else if (!total) body = <p className="explorer-note">Discovery found no objects.</p>;
  else body = (
    <ul className="tree-list tree-root" aria-label="Workspace folders">
      <li>
        <button type="button" className="tree-row tree-folder" style={{ paddingLeft: 8 }} aria-expanded={rootOpen} onClick={() => toggle("root")}>
          <span className="tree-caret" aria-hidden="true">{rootOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
          <FolderTree size={15} className="tree-icon folder" aria-hidden="true" />
          <span className="tree-label strong">{connection.workspace ?? "Synapse workspace"}</span>
          <span className="tree-count">{total.toLocaleString()}</span>
        </button>
        {rootOpen && (shown.length
          ? <Branch nodes={shown} depth={1} open={open} toggle={toggle} limits={limits} more={more} selected={selected} onPick={setSelected} searching={!!q} />
          : <p className="explorer-note">Nothing matches “{query.trim()}”.</p>)}
      </li>
    </ul>
  );

  return (
    <aside className="explorer" aria-label="Workspace explorer">
      <div className="explorer-head">
        <span className="eyebrow">Explorer</span>
        {total > 0 && <span className="faint">{total.toLocaleString()} objects</span>}
      </div>
      {total > 0 && (
        <input className="input explorer-search" type="search" placeholder="Filter by name" aria-label="Filter the workspace explorer"
          value={query} onChange={(e) => setQuery(e.target.value)} />
      )}
      <div className="explorer-tree">{body}</div>
      {selected && <ObjectDetails id={selected} onClose={() => setSelected(null)} onNavigate={setSelected} />}
    </aside>
  );
}
