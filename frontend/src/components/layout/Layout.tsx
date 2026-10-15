import { Check, ChevronDown, Download, FolderOpen, FolderPlus, Pencil, PlugZap, RefreshCw, ServerCrash, Trash2, Workflow, Zap } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import { notify, TOAST_EVENT } from "../shared/notify";
import { Button, ConfirmDialog, Modal, TextField } from "../shared/Shared";
import { AssistantLauncher, AssistantPanel } from "./Assistant";

/** A side's live state, in the bar: green when connected. */
function Presence({ label, name, ok, busy }: { label: string; name: string | null | undefined; ok: boolean; busy?: boolean }) {
  return (
    <NavLink to="/" className={`presence${ok ? " ok" : ""}${busy ? " busy" : ""}`} title={ok ? `${label}: ${name} (connected)` : `${label}: not connected`}>
      <span className="presence-dot" aria-hidden="true" />
      <span className="presence-label">{label}</span>
      <span className="presence-name">{ok ? name : "Not connected"}</span>
    </NavLink>
  );
}

/**
 * The project switcher: pick a project, or create, rename or delete one. A
 * project names a migration and keeps its own plan, options and progress.
 */
function ProjectMenu() {
  const { projects, project, selectProject, addProject, renameProject, deleteProject, backendProjects } = useMigration();
  const [busy, setBusy] = useState(false);
  // Each refuses when it cannot be done now (a switch while a run works, a list changed meanwhile): say why.
  const attempt = async (work: () => Promise<void>, done?: string) => {
    setBusy(true);
    try {
      await work();
      if (done) notify(done);
      return true;
    } catch (e) {
      notify(e instanceof Error ? e.message : "That could not be done.");
      return false;
    } finally {
      setBusy(false);
    }
  };
  const [open, setOpen] = useState(false);
  const [dialog, setDialog] = useState<"new" | "rename" | "delete" | null>(null);
  const [name, setName] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", away);
    (menu.current?.querySelector<HTMLElement>("[aria-checked=true]") ?? menu.current?.querySelector<HTMLElement>("button"))?.focus();
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const items = () => [...(menu.current?.querySelectorAll<HTMLButtonElement>("[role^=menuitem]:not(:disabled)") ?? [])];
  const onMenuKey = (e: KeyboardEvent) => {
    const list = items();
    const i = list.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length]?.focus(); }
    else if (e.key === "Home") { e.preventDefault(); list[0]?.focus(); }
    else if (e.key === "End") { e.preventDefault(); list[list.length - 1]?.focus(); }
    else if (e.key === "Escape") { setOpen(false); trigger.current?.focus(); }
    else if (e.key === "Tab") setOpen(false);
  };
  const show = (d: "new" | "rename" | "delete") => { setOpen(false); setName(d === "rename" ? project.name : ""); setDialog(d); };
  const close = useCallback(() => setDialog(null), []);
  const submit = async () => {
    const value = name.trim();
    if (!value) return;
    const ok = dialog === "new" ? await attempt(() => addProject(value), `Project “${value}” created.`)
      : await attempt(() => renameProject(project.id, value), `Project renamed to “${value}”.`);
    if (ok) setDialog(null);
  };

  return (
    <div className="project-menu" ref={root}>
      <button ref={trigger} type="button" className="project-trigger" aria-haspopup="menu" aria-expanded={open}
        aria-label={`Project: ${project.name}`} title={project.name} onClick={() => setOpen((o) => !o)}>
        <FolderOpen size={15} aria-hidden="true" />
        <span className="project-name">{project.name}</span>
        <ChevronDown size={15} className="project-chevron" aria-hidden="true" />
      </button>
      {open && (
        <div className="menu" role="menu" aria-label="Projects" ref={menu} onKeyDown={onMenuKey}>
          <div className="menu-label" aria-hidden="true">Projects</div>
          {projects.map((p) => (
            <button key={p.id} type="button" role="menuitemradio" aria-checked={p.id === project.id} className="menu-item"
              disabled={busy} onClick={() => { setOpen(false); trigger.current?.focus(); if (p.id !== project.id) void attempt(() => selectProject(p.id), `Switched to “${p.name}”.`); }}>
              <span className="menu-check" aria-hidden="true">{p.id === project.id && <Check size={14} />}</span>
              <span className="menu-text">{p.name}</span>
            </button>
          ))}
          <div className="menu-sep" role="separator" />
          <button type="button" role="menuitem" className="menu-item" onClick={() => show("new")}><FolderPlus size={14} aria-hidden="true" /><span className="menu-text">New project…</span></button>
          <button type="button" role="menuitem" className="menu-item" onClick={() => show("rename")}><Pencil size={14} aria-hidden="true" /><span className="menu-text">Rename this project…</span></button>
          <button type="button" role="menuitem" className="menu-item danger" disabled={projects.length < 2} title={projects.length < 2 ? "The only project cannot be deleted" : undefined}
            onClick={() => show("delete")}><Trash2 size={14} aria-hidden="true" /><span className="menu-text">Delete this project…</span></button>
        </div>
      )}

      {(dialog === "new" || dialog === "rename") && (
        <Modal title={dialog === "new" ? "New project" : "Rename project"} onClose={close} onSubmit={() => void submit()}
          footer={<><Button onClick={close}>Cancel</Button><Button type="submit" variant="primary" disabled={!name.trim() || busy}>{dialog === "new" ? "Create" : "Rename"}</Button></>}>
          <TextField label="Project name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} />
          {dialog === "new" && <p className="faint">{backendProjects
            ? "A project is one migration: the backend keeps its own discovery, Fabric target, runs and audit trail, and switches to it now. The Synapse sign-in stays as it is."
            : "A project names a migration and keeps its own plan, options and progress in this browser. This backend holds one connection at a time."}</p>}
        </Modal>
      )}
      {dialog === "delete" && (
        <ConfirmDialog title={`Delete “${project.name}”?`} confirmLabel="Delete project" onCancel={close} busy={busy}
          onConfirm={() => { const gone = project.name; void attempt(() => deleteProject(project.id), `Project “${gone}” deleted.`).then((ok) => ok && setDialog(null)); }}>
          {backendProjects
            ? <p>This takes the project off the list and switches to another one. The backend archives its folder (its discovery, runs and audit trail) rather than deleting it.</p>
            : <p>This removes the project from this browser: its plan, stage options and the steps you confirmed.</p>}
          <p className="muted">Nothing in Synapse or Fabric changes.</p>
        </ConfirmDialog>
      )}
    </div>
  );
}

function TopBar() {
  const { api, mode, setMode, isConnected, connection, connectionBusy, discovery, refreshDiscovery, refreshHealth } = useAppState();
  const { reloadGraph, fabric, fabricBusy } = useMigration();
  const [exporting, setExporting] = useState(false);
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const canMigrate = isConnected || discovered;

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
      notify(`Exported ${data.objects.length.toLocaleString()} objects.`);
    } catch (e) {
      notify(e instanceof Error ? e.message : "The export failed.");
    } finally {
      setExporting(false);
    }
  };

  const refresh = async () => {
    await Promise.all([refreshHealth(), refreshDiscovery()]);
    reloadGraph();
    notify("Status and results refreshed.");
  };

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true"><Zap size={16} /></span>
        <span className="brand-name">Migration Accelerator</span>
      </div>

      <nav className="topnav" aria-label="Primary">
        <NavLink to="/" end className={({ isActive }) => `topnav-item${isActive ? " active" : ""}`}><PlugZap size={15} aria-hidden="true" />Connections</NavLink>
        {canMigrate ? (
          <NavLink to="/migration" className={({ isActive }) => `topnav-item${isActive ? " active" : ""}`}><Workflow size={15} aria-hidden="true" />Migration</NavLink>
        ) : (
          <span className="topnav-item" aria-disabled="true" title="Connect the source first"><Workflow size={15} aria-hidden="true" />Migration</span>
        )}
      </nav>

      <div className="presence-group" aria-label="Connections">
        <Presence label="Source" name={connection.workspace} ok={isConnected} busy={!!connectionBusy} />
        <Presence label="Destination" name={fabric.workspaceName} ok={fabric.status === "connected"} busy={!!fabricBusy || fabric.status === "signing_in"} />
      </div>

      <span className="spacer" />

      <ProjectMenu />
      <Button variant="ghost" icon onClick={() => void exportMetadata()} loading={exporting} disabled={!discovered} aria-label="Export metadata" title={discovered ? "Download the discovered inventory as JSON" : "Run discovery first"}><Download size={16} /></Button>
      <Button variant="ghost" icon onClick={() => void refresh()} aria-label="Refresh" title="Refresh status and results"><RefreshCw size={16} /></Button>
      <div className="mode-switch" role="group" aria-label="Data source">
        <button type="button" aria-pressed={mode === "real"} onClick={() => setMode("real")} title="Use your real Azure through the accelerator's backend">Live</button>
        <button type="button" className="demo" aria-pressed={mode === "mock"} onClick={() => setMode("mock")} title="Use generated sample data; nothing real is touched">Demo</button>
      </div>
    </header>
  );
}

/** Live mode without a backend: say so once, plainly, with the two ways forward. */
function BackendBar() {
  const { mode, backendError, reload, setMode } = useAppState();
  if (mode !== "real" || !backendError) return null;
  return (
    <div className="backend-bar" role="alert">
      <ServerCrash size={17} aria-hidden="true" />
      <span>
        <strong>The accelerator's backend is not answering.</strong>{" "}
        Start it with <code>python -m discovery_agent.api</code> (or <code>start-ui.ps1</code>), then retry. Demo data works without it.
      </span>
      <span className="spacer" />
      <Button size="small" onClick={reload}>Retry</Button>
      <Button size="small" variant="ghost" onClick={() => setMode("mock")}>Use Demo data</Button>
    </div>
  );
}

export function Layout() {
  const [toast, setToast] = useState<string | null>(null);
  const [assistant, setAssistant] = useState(false);
  const { mode } = useAppState();
  const { pathname } = useLocation();

  useEffect(() => {
    const show = (e: Event) => setToast((e as CustomEvent<string>).detail);
    window.addEventListener(TOAST_EVENT, show);
    return () => window.removeEventListener(TOAST_EVENT, show);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 4500);
    return () => clearTimeout(id);
  }, [toast]);

  // Ctrl+I (Cmd+I) opens and closes the assistant, like an editor's chat.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "i") { e.preventDefault(); setAssistant((a) => !a); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  useEffect(() => { window.scrollTo?.(0, 0); }, [pathname]);

  return (
    <div className={`shell${assistant ? " with-assistant" : ""}`}>
      <TopBar />
      {mode === "mock" && <div className="demo-bar" role="note">DEMO DATA · generated sample content, not a real Synapse workspace</div>}
      <BackendBar />
      <div className="shell-body">
        <main className="main" id="main"><Outlet /></main>
        {assistant && <AssistantPanel onClose={() => setAssistant(false)} />}
      </div>
      {!assistant && <AssistantLauncher onOpen={() => setAssistant(true)} />}
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
