import {
  ChevronsLeft,
  ChevronsRight,
  Cloud,
  Database,
  Download,
  FolderKanban,
  Network,
  Rocket,
  Plus,
  Radar,
  RefreshCw,
  ScanSearch,
  Server,
  ShieldCheck,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import { Button, Modal, StatusBadge, TextField } from "../shared/Shared";

/* ---- Sidebar ---------------------------------------------------------------- */

interface NavEntry { to: string; label: string; icon: LucideIcon }
const NAV: { section: string; items: NavEntry[] }[] = [
  { section: "Overview", items: [{ to: "/", label: "Projects", icon: FolderKanban }] },
  {
    section: "Connect",
    items: [
      { to: "/synapse", label: "Synapse Source", icon: Database },
      { to: "/fabric", label: "Fabric Target", icon: Cloud },
    ],
  },
  {
    section: "Analyze",
    items: [
      { to: "/discovery", label: "Discovery", icon: Radar },
      { to: "/assessment", label: "Assessment", icon: ScanSearch },
      { to: "/dependencies", label: "Dependencies & Waves", icon: Network },
    ],
  },
  {
    section: "Migrate",
    items: [
      { to: "/migrate", label: "Plan & Migrate", icon: Rocket },
      { to: "/validate", label: "Validation", icon: ShieldCheck },
    ],
  },
];

function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  return (
    <nav className="sidebar" aria-label="Primary">
      {NAV.map(({ section, items }) => (
        <div key={section} className="nav-group">
          <div className="nav-section">{section}</div>
          {items.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => `nav-item${isActive ? " active" : ""}`} title={label}>
              <span className="nav-ico"><Icon size={18} aria-hidden="true" /></span>
              <span className="nav-label">{label}</span>
            </NavLink>
          ))}
        </div>
      ))}
      <div className="spacer" />
      <Button variant="ghost" size="small" onClick={onToggle} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} style={{ alignSelf: "flex-start" }}>
        {collapsed ? <ChevronsRight size={18} /> : <ChevronsLeft size={18} />}
      </Button>
    </nav>
  );
}

/* ---- Workflow stepper ------------------------------------------------------------ */

type StepState = "done" | "active" | "pending";

/** Where the operator is in the migration workflow, and what is already done. */
export function useWorkflow(): { to: string; label: string; state: StepState }[] {
  const { pathname } = useLocation();
  const { isConnected, discovery } = useAppState();
  const { fabric, execution, validation, project } = useMigration();
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const steps: { to: string; label: string; done: boolean }[] = [
    { to: "/", label: "Project", done: !!project },
    { to: "/synapse", label: "Synapse Source", done: isConnected },
    { to: "/discovery", label: "Discovery", done: discovered },
    { to: "/assessment", label: "Assessment", done: false },
    { to: "/dependencies", label: "Dependencies & Waves", done: false },
    { to: "/fabric", label: "Fabric Target", done: fabric.status === "connected" },
    { to: "/migrate", label: "Plan & Migrate", done: execution.state === "completed" },
    { to: "/validate", label: "Validate", done: !!validation },
  ];
  return steps.map((s) => ({
    to: s.to,
    label: s.label,
    state: (s.to === "/" ? pathname === "/" : pathname.startsWith(s.to)) ? "active" : s.done ? "done" : "pending",
  }));
}

function Stepper() {
  const steps = useWorkflow();
  return (
    <ol className="stepper" aria-label="Migration workflow">
      {steps.map((s, i) => (
        <li key={s.to} style={{ display: "contents" }}>
          {i > 0 && <span className="step-sep" aria-hidden="true" />}
          <Link to={s.to} className={`step ${s.state}`} aria-current={s.state === "active" ? "step" : undefined}>
            <span className="step-n" aria-hidden="true">{s.state === "done" ? "✓" : i + 1}</span>
            <span className="step-label">{s.label}</span>
            <span className="sr-only">{s.state === "done" ? "completed" : s.state === "active" ? "current" : "not started"}</span>
          </Link>
        </li>
      ))}
    </ol>
  );
}

/* ---- Header ------------------------------------------------------------------------ */

function Header({ onToast }: { onToast: (message: string) => void }) {
  const { api, mode, setMode, isConnected, connection, connectionBusy, discovery, refreshDiscovery, refreshHealth } = useAppState();
  const { projects, project, selectProject, addProject, reloadGraph } = useMigration();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [exporting, setExporting] = useState(false);
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";

  const exportMetadata = async () => {
    setExporting(true);
    try {
      const data = await api.exportMetadata();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `synapse-metadata-${data.workspace ?? "workspace"}.json`;
      a.click();
      URL.revokeObjectURL(url);
      onToast(`Exported ${data.objects.length.toLocaleString()} objects.`);
    } catch (e) {
      onToast(e instanceof Error ? e.message : "The export failed.");
    } finally {
      setExporting(false);
    }
  };

  const refresh = async () => {
    await Promise.all([refreshHealth(), refreshDiscovery()]);
    reloadGraph();
  };

  return (
    <header className="header">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true"><Zap size={14} /></span>
        <span className="brand-name">Migration Accelerator</span>
        <span className={`live-pill${mode === "mock" ? " demo" : ""}`}><Server size={13} aria-hidden="true" />{mode === "mock" ? "DEMO" : "LIVE"}</span>
        <span className="brand-sub">Synapse → Fabric</span>
      </div>
      {isConnected ? (
        <StatusBadge tone="success">CONNECTED</StatusBadge>
      ) : (
        <StatusBadge tone={connectionBusy ? "info" : "neutral"} running={!!connectionBusy}>{connectionBusy ? "CONNECTING" : "DISCONNECTED"}</StatusBadge>
      )}
      <div className="spacer" />
      <label className="sr-only" htmlFor="project-select">Project</label>
      <select id="project-select" className="select project-select" value={project.id} onChange={(e) => selectProject(e.target.value)} title={isConnected ? `Connected to ${connection.workspace}` : "Project"}>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select>
      <Button onClick={() => void exportMetadata()} loading={exporting} disabled={!discovered} title={discovered ? "Download the discovered inventory as JSON" : "Run discovery first"}>
        <Download size={14} aria-hidden="true" /><span className="hide-narrow">Export Metadata</span>
      </Button>
      <Button onClick={() => void refresh()} aria-label="Refresh" title="Refresh status and results"><RefreshCw size={14} aria-hidden="true" /><span className="hide-narrow">Refresh</span></Button>
      <Button variant="primary" onClick={() => setCreating(true)}><Plus size={14} aria-hidden="true" /><span className="hide-narrow">New Project</span></Button>
      <div className="mode-switch" role="group" aria-label="Data source">
        <button type="button" aria-pressed={mode === "real"} onClick={() => setMode("real")} title="Talk to the accelerator backend">Live API</button>
        <button type="button" className="demo" aria-pressed={mode === "mock"} onClick={() => setMode("mock")} title="Use generated sample data — not a real workspace">Demo data</button>
      </div>
      {creating && (
        <Modal
          title="New project"
          onClose={() => setCreating(false)}
          footer={<><Button onClick={() => setCreating(false)}>Cancel</Button><Button variant="primary" disabled={!name.trim()} onClick={() => { addProject(name); setName(""); setCreating(false); }}>Create</Button></>}
        >
          <TextField label="Project name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} autoFocus />
          <p className="faint">A project names this migration. The backend holds one connection at a time, so projects label work; they do not separate data.</p>
        </Modal>
      )}
    </header>
  );
}

/* ---- Shell -------------------------------------------------------------------------- */

export function Layout() {
  const [collapsed, setCollapsed] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const { mode } = useAppState();
  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 4500);
    return () => clearTimeout(id);
  }, [toast]);
  return (
    <div className={`app${collapsed ? " collapsed" : ""}`}>
      <Header onToast={setToast} />
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
      <main className="main" id="main">
        {mode === "mock" && (
          <div className="demo-bar" role="note" style={{ margin: "-28px -32px 16px" }}>
            DEMO DATA — generated sample content, not a real Synapse workspace
          </div>
        )}
        <Stepper />
        <Outlet />
      </main>
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
