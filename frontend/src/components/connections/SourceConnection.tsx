import { CheckCircle2, ChevronDown, ChevronRight, CircleSlash, FileArchive, FileJson, GitBranch, Layers, X, XCircle } from "lucide-react";
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { useAppState } from "../../state/AppState";
import type { AuthMethod, AzureSubscription, ConnectionConfig, ConnectionState, RepositoryConfig, RepositoryKind, ZipUpload } from "../../types";
import { Banner, Button, SelectField, TextField, ConfirmDialog } from "../shared/Shared";
import { MethodTiles, MiniSteps, type MethodOption } from "./MethodTiles";

/* ---- Methods ------------------------------------------------------------------------ */

type SourceMethod = AuthMethod | RepositoryKind;

// Three ways in: the live workspace, its Git repository by URL, or a ZIP export. The live workspace
// can be added to either repository source, for the dedicated SQL pool.
const METHODS: MethodOption<SourceMethod>[] = [
  { id: "azure_cli", title: "Synapse workspace", desc: "Sign in and choose the workspace: its definitions are read live", icon: Layers },
  { id: "git", title: "Git repository", desc: "Enter the URL of the workspace's Git repository", icon: GitBranch },
  { id: "zip", title: "Workspace export (ZIP)", desc: "Upload one environment's exported repository folder as a ZIP", icon: FileArchive },
];
const METHOD_LABEL: Record<AuthMethod, string> = { azure_cli: "Azure CLI", interactive_browser: "Interactive browser" };
const methodLabel = (m: ConnectionState["method"]) => (m === "azure_cli" || m === "interactive_browser" ? METHOD_LABEL[m] : "—");

/* ---- Form state ------------------------------------------------------------------ */

/** The Azure sign-in method behind a connection; a repository source signs in to Azure only for its SQL pool. */
const authOf = (m: ConnectionState["method"]): AuthMethod => (m === "interactive_browser" ? "interactive_browser" : "azure_cli");

const EMPTY: ConnectionConfig = {
  method: "azure_cli", tenantId: "", subscriptionId: "", resourceGroup: "", workspace: "",
  workspaceUrl: "", sqlPool: "", resource: "",
};

// Kept in memory so the form survives navigation. Identifiers only, and
// nothing here is written to browser storage.
let remembered: ConnectionConfig | null = null;

export function configFromConnection(c: ConnectionState): ConnectionConfig {
  return {
    ...EMPTY,
    method: authOf(c.method),
    tenantId: c.tenantId ?? "",
    subscriptionId: c.subscriptionId ?? "",
    resourceGroup: c.resourceGroup ?? "",
    workspace: c.workspace ?? "",
    sqlPool: c.sqlPool ?? "",
  };
}

type Errors = Partial<Record<keyof ConnectionConfig, string>>;

/** The sign-in needs nothing typed; testing needs the subscription, resource group and workspace chosen. */
function validate(c: ConnectionConfig, action: "authenticate" | "test"): Errors {
  const e: Errors = {};
  if (action === "authenticate") return e;
  const need = (k: keyof ConnectionConfig, label: string) => { if (!c[k].trim()) e[k] = `Choose the ${label}.`; };
  need("subscriptionId", "subscription");
  need("resourceGroup", "resource group");
  need("workspace", "Synapse workspace");
  return e;
}

/* ---- Scope: subscription -> resource group -> workspace -> SQL pool --------------- */

interface ScopeProps {
  config: ConnectionConfig;
  errors: Errors;
  set: (patch: Partial<ConnectionConfig>) => void;
  signedIn: boolean;
  /** Changes when signing in to another directory adds subscriptions, so the list is read again. */
  version?: number;
}

/**
 * Every dropdown is filled from the signed-in identity, so nothing is typed
 * that could be misspelt, and only what that identity can see is offered.
 */
function ScopeSelectors({ config, errors, set, signedIn, version = 0 }: ScopeProps) {
  const { azureLists } = useAppState();
  const [subscriptions, setSubscriptions] = useState<AzureSubscription[]>([]);
  const [groups, setGroups] = useState<string[]>([]);
  const [workspaces, setWorkspaces] = useState<string[]>([]);
  const [pools, setPools] = useState<string[]>([]);
  const [loading, setLoading] = useState<"subscriptions" | "groups" | "workspaces" | "pools" | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  const load = async <T,>(kind: "subscriptions" | "groups" | "workspaces" | "pools", fn: () => Promise<T[]>, apply: (v: T[]) => void) => {
    setLoading(kind);
    setListError(null);
    try { apply(await fn()); } catch (e) { setListError(e instanceof Error ? e.message : "The list could not be loaded."); apply([]); }
    finally { setLoading(null); }
  };

  // A list with exactly one entry is chosen for the operator: nobody should have to
  // open a dropdown to pick its only item, and the SQL pool is never skipped by accident.
  useEffect(() => {
    if (!signedIn) { setSubscriptions([]); return; }
    void load("subscriptions", () => azureLists.listSubscriptions(), (v) => {
      setSubscriptions(v);
      // Keep a subscription chosen earlier only if this account can still see it.
      if (config.subscriptionId && !v.some((x) => x.id === config.subscriptionId)) set({ subscriptionId: "", resourceGroup: "", workspace: "", sqlPool: "" });
      else if (v.length === 1 && !config.subscriptionId) set({ subscriptionId: v[0].id, resourceGroup: "", workspace: "", sqlPool: "" });
    });
  }, [signedIn, version]); // eslint-disable-line react-hooks/exhaustive-deps

  // With subscriptions from several directories, each is named with its directory.
  const directoryCount = new Set(subscriptions.map((x) => x.tenantId)).size;

  useEffect(() => {
    setGroups([]); setWorkspaces([]); setPools([]);
    if (!signedIn || !config.subscriptionId) return;
    void load("groups", () => azureLists.listResourceGroups(config.subscriptionId), (v) => {
      setGroups(v);
      if (v.length === 1 && !config.resourceGroup) set({ resourceGroup: v[0], workspace: "", sqlPool: "" });
    });
  }, [signedIn, config.subscriptionId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setWorkspaces([]); setPools([]);
    if (!signedIn || !config.subscriptionId || !config.resourceGroup) return;
    void load("workspaces", () => azureLists.listWorkspaces(config.subscriptionId, config.resourceGroup), (v) => {
      setWorkspaces(v);
      if (v.length === 1 && !config.workspace) set({ workspace: v[0], sqlPool: "" });
    });
  }, [signedIn, config.subscriptionId, config.resourceGroup]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setPools([]);
    if (!signedIn || !config.subscriptionId || !config.resourceGroup || !config.workspace) return;
    void load("pools", () => azureLists.listSqlPools(config.subscriptionId, config.resourceGroup, config.workspace), (v) => {
      setPools(v);
      if (v.length === 1 && !config.sqlPool) set({ sqlPool: v[0] });
    });
  }, [signedIn, config.subscriptionId, config.resourceGroup, config.workspace]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!signedIn) return null;
  return (
    <>
      <div className="form-grid">
        <SelectField label="Subscription" value={config.subscriptionId} onChange={(v) => set({ subscriptionId: v, resourceGroup: "", workspace: "", sqlPool: "" })}
          options={subscriptions.map((x) => ({ value: x.id, label: directoryCount > 1 && x.tenantName ? `${x.name} · ${x.tenantName}` : x.name }))} loading={loading === "subscriptions"}
          placeholder={subscriptions.length || loading ? "Select a subscription" : "No subscriptions for this account"} error={errors.subscriptionId} />
        <SelectField label="Resource group" value={config.resourceGroup} onChange={(v) => set({ resourceGroup: v, workspace: "", sqlPool: "" })} options={groups} loading={loading === "groups"}
          disabled={!config.subscriptionId} placeholder={config.subscriptionId ? "Select a resource group" : "Select a subscription first"} error={errors.resourceGroup} />
        <SelectField label="Synapse workspace" value={config.workspace} onChange={(v) => set({ workspace: v, sqlPool: "" })} options={workspaces} loading={loading === "workspaces"} disabled={!config.resourceGroup} placeholder={config.resourceGroup ? (workspaces.length ? "Select a workspace" : "No Synapse workspaces in this group") : "Select a resource group first"} error={errors.workspace} />
        <SelectField label="Dedicated SQL pool" optional value={config.sqlPool} onChange={(v) => set({ sqlPool: v })} options={pools} loading={loading === "pools"} disabled={!config.workspace} placeholder={config.workspace ? "None (skip tables, views, procedures)" : "Select a workspace first"} hint="Needed for tables, views and stored procedures." />
      </div>
      {listError && <Banner tone="error" title="Could not load the list">{listError}</Banner>}
    </>
  );
}

/* ---- Connected summary ---------------------------------------------------------------- */

export function CheckList({ checks, label = "Connection checks" }: { checks: { name: string; status: "ok" | "failed" | "skipped"; message?: string | null }[]; label?: string }) {
  if (!checks.length) return null;
  return (
    <ul className="checks" aria-label={label}>
      {checks.map((c) => (
        <li key={c.name} className={`check ${c.status}`}>
          {c.status === "ok" ? <CheckCircle2 size={15} aria-label="passed" /> : c.status === "failed" ? <XCircle size={15} aria-label="failed" /> : <CircleSlash size={15} aria-label="skipped" />}
          <span><strong>{c.name}</strong>{c.message ? <span className="muted"> — {c.message}</span> : null}</span>
        </li>
      ))}
    </ul>
  );
}

/** Checks folded into one line ("3 of 3 checks passed"), opened on demand. */
export function CheckFold({ checks, label }: { checks: { name: string; status: "ok" | "failed" | "skipped"; message?: string | null }[]; label?: string }) {
  if (!checks.length) return null;
  const passed = checks.filter((c) => c.status === "ok").length;
  const failed = checks.some((c) => c.status === "failed");
  return (
    <details className={`check-fold${failed ? " failed" : ""}`}>
      <summary>{failed ? <XCircle size={15} aria-hidden="true" /> : <CheckCircle2 size={15} aria-hidden="true" />}{passed} of {checks.length} checks passed</summary>
      <CheckList checks={checks} label={label} />
    </details>
  );
}

export function SummaryList({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="kv compact">
      {rows.map(([k, v]) => (<Fragment key={k}><dt>{k}</dt><dd>{v || "—"}</dd></Fragment>))}
    </dl>
  );
}

/* ---- Repository sources: Git, or a ZIP of the repository ---------------------------- */

const ENVIRONMENTS = ["Dev", "Test", "Prod", "Other"];

interface RepoForm {
  url: string;
  ref: string;
  rootFolder: string;
  environment: string;
  customEnvironment: string;
  /** The environment's parameters file: its text and name, held in memory only. */
  parameters: string;
  parametersName: string;
  upload: ZipUpload | null;
  withPool: boolean;
}
// The environment defaults to Dev: a workspace's Git integration holds its working (Dev) definitions.
const EMPTY_REPO: RepoForm = { url: "", ref: "", rootFolder: "", environment: "Dev", customEnvironment: "", parameters: "", parametersName: "", upload: null, withPool: false };
// Like the Azure form: kept in memory so it survives navigation, never in browser storage.
let rememberedRepo: RepoForm = EMPTY_REPO;
let lastRepositoryRequest: RepositoryConfig | null = null;

type RepoErrors = Partial<Record<"url" | "upload" | "environment" | "parameters" | "pool", string>>;

/** The environment's name; Dev unless another one is chosen. */
function environmentOf(f: RepoForm): string {
  return (f.environment === "Other" ? f.customEnvironment.trim() : f.environment) || "Dev";
}

function validateRepo(kind: RepositoryKind, f: RepoForm, config: ConnectionConfig, signedIn: boolean): RepoErrors {
  const e: RepoErrors = {};
  if (kind === "git") {
    const url = f.url.trim();
    if (!url) e.url = "Repository URL is required.";
    else if (!/^(https:\/\/|ssh:\/\/|git@)/.test(url)) e.url = "Use the repository's HTTPS or SSH URL.";
    else if (/^https:\/\/[^/@]+@/.test(url)) e.url = "Leave credentials out of the URL: git on this machine signs in with your own credential helper or SSH key.";
  } else if (!f.upload) {
    e.upload = "Choose the ZIP to upload.";
  }
  if (f.environment === "Other" && !f.customEnvironment.trim()) e.environment = "Name the environment.";
  if (f.withPool && !(signedIn && config.subscriptionId && config.resourceGroup && config.workspace && config.sqlPool)) {
    e.pool = kind === "git"
      ? (signedIn ? "Choose the subscription, resource group, workspace and SQL pool, or untick the live workspace." : "Sign in to Azure to connect the live workspace, or untick it.")
      : (signedIn ? "Choose the subscription, resource group, workspace and SQL pool, or switch the SQL pool off." : "Sign in to Azure to add the SQL pool, or switch it off.");
  }
  return e;
}

function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsText(file);
  });
}

/** A file chooser with the chosen file's name and a way to clear it. */
function FilePick({ label, accept, fileName, busy, onPick, onClear, hint, error, icon }: {
  label: string; accept: string; fileName: string; busy?: boolean; onPick: (f: File) => void; onClear?: () => void; hint?: ReactNode; error?: string; icon: ReactNode;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <div className="field full">
      <span className="field-label">{label}</span>
      <div className="row" style={{ gap: 8 }}>
        <Button size="small" onClick={() => input.current?.click()} loading={busy}>{icon}{fileName ? "Choose another file" : "Choose file"}</Button>
        {fileName && <span className="file-chip">{fileName}{onClear && <button type="button" className="file-clear" aria-label={`Remove ${fileName}`} onClick={onClear}><X size={13} /></button>}</span>}
        <input ref={input} type="file" accept={accept} hidden aria-label={label}
          onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) onPick(f); }} />
      </div>
      {hint && !error && <span className="hint">{hint}</span>}
      {error && <span className="err">{error}</span>}
    </div>
  );
}

/** The ZIP to read: one environment's exported repository folder. */
function ZipUploadField({ form, error, uploading, onUpload }: { form: RepoForm; error?: string | null; uploading: boolean; onUpload: (f: File) => void }) {
  const found = form.upload ? Object.values(form.upload.counts).reduce((a, b) => a + b, 0) : 0;
  return (
    <div className="form-grid">
      <FilePick label="Workspace export (ZIP)" accept=".zip,application/zip" fileName={form.upload?.fileName ?? ""} busy={uploading} onPick={onUpload}
        icon={<FileArchive size={14} aria-hidden="true" />} error={error ?? undefined}
        hint={form.upload
          ? (form.upload.rootFolder !== null ? `${found.toLocaleString()} definitions found${form.upload.rootFolder ? ` in ${form.upload.rootFolder}/` : ""}.` : form.upload.note ?? "No Synapse folders found yet: name the root folder under More options.")
          : "A ZIP of the workspace's Git repository (for example the branch downloaded from GitHub or Azure DevOps). Up to 200 MB."} />
    </div>
  );
}

/**
 * Choices with sensible defaults, folded away: the branch and root folder (found for you), the
 * environment (Dev) and an environment's parameters file. The fold says what will be used.
 */
function MoreOptions({ kind, form, setForm, errors, open, onToggle }: {
  kind: RepositoryKind; form: RepoForm; setForm: (p: Partial<RepoForm>) => void; errors: RepoErrors;
  open: boolean; onToggle: (open: boolean) => void;
}) {
  const [paramError, setParamError] = useState<string | null>(null);
  const pickParameters = async (file: File) => {
    setParamError(null);
    try {
      const text = await readText(file);
      JSON.parse(text);
      setForm({ parameters: text, parametersName: file.name });
    } catch {
      setParamError("That file is not valid JSON. Choose the environment's ARM parameters file.");
    }
  };
  const summary = [
    `${environmentOf(form)} environment`,
    kind === "git" && form.ref.trim() && `branch ${form.ref.trim()}`,
    form.rootFolder.trim() && `folder ${form.rootFolder.trim()}`,
    form.parametersName,
  ].filter(Boolean).join(" · ");
  return (
    <div className="more-options">
      <button type="button" className="more-toggle" aria-expanded={open} onClick={() => onToggle(!open)}>
        {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
        More options<span className="faint"> · {summary}</span>
      </button>
      {open && (
        <div className="form-grid">
          {kind === "git" && (
            <TextField label="Branch" optional value={form.ref} onChange={(e) => setForm({ ref: e.target.value })} placeholder="main"
              hint="Empty: the repository's default branch. Name a release branch to read another environment." />
          )}
          <TextField label="Root folder" optional value={form.rootFolder} onChange={(e) => setForm({ rootFolder: e.target.value })}
            placeholder={kind === "git" ? "synapse" : form.upload?.rootFolder || "synapse"}
            hint="Where the definitions are. Empty: found automatically." />
          <SelectField label="Environment" value={form.environment} onChange={(v) => setForm({ environment: v })} options={ENVIRONMENTS} placeholder="Choose the environment"
            hint="Which environment these definitions are for. Shown with the connection and in the report." />
          {form.environment === "Other" && (
            <TextField label="Environment name" value={form.customEnvironment} onChange={(e) => setForm({ customEnvironment: e.target.value })} placeholder="UAT" error={errors.environment} />
          )}
          <FilePick label="Environment parameters (optional)" accept=".json,application/json" fileName={form.parametersName}
            onPick={(f) => void pickParameters(f)} onClear={() => setForm({ parameters: "", parametersName: "" })} icon={<FileJson size={14} aria-hidden="true" />}
            error={paramError ?? errors.parameters}
            hint="For Test or Prod: the environment's ARM parameters file (such as TemplateParametersForWorkspace.json). Its server names and URLs replace the committed ones, so migrated connections point at that environment." />
        </div>
      )}
    </div>
  );
}

function RepositorySummary({ connection }: { connection: ConnectionState }) {
  const r = connection.repository!;
  return (
    <SummaryList rows={[
      ["Source", r.kind === "git" ? "Git repository" : "Workspace export (ZIP)"],
      [r.kind === "git" ? "Repository" : "File", <strong key="l">{r.label}</strong>],
      ...(r.kind === "git" ? [["Branch", `${r.ref ?? "default"}${r.commit ? ` · ${r.commit.slice(0, 7)}` : ""}`] as [string, ReactNode]] : []),
      ["Root folder", r.rootFolder ? `${r.rootFolder}/` : "Top level"],
      ["Environment", <strong key="e">{r.environment}</strong>],
      ["Parameters", r.parametersName ? `${r.parametersName} · ${r.applied.length} value${r.applied.length === 1 ? "" : "s"} applied${r.unmatched.length ? `, ${r.unmatched.length} not matched` : ""}` : "None: values as committed"],
      ["Definitions", `${r.artifacts.toLocaleString()} in ${Object.keys(r.counts).length} folders`],
      ["Dedicated SQL pool", connection.sqlPool ? `${connection.sqlPool} (${connection.workspace})` : "Not added: tables, views, procedures and data are skipped"],
      ["Last connected", connection.testedAt ? new Date(connection.testedAt).toLocaleString() : "—"],
    ]} />
  );
}

/* ---- The source panel ---------------------------------------------------------------- */

const initialSource = (c: ConnectionState, method: AuthMethod): SourceMethod =>
  c.sourceKind === "zip" || c.sourceKind === "git" ? c.sourceKind : method;

/** Connect Azure Synapse: the live workspace, its Git repository, or a ZIP export. */
export function SourceConnectionPanel() {
  const app = useAppState();
  const { connection, connectionBusy, connectionError, health, mode } = app;
  const [config, setConfig] = useState<ConnectionConfig>(() => ({ ...EMPTY, ...(remembered ?? {}) }));
  const [errors, setErrors] = useState<Errors>({});
  const [changing, setChanging] = useState(false);
  const [confirmSignOut, setConfirmSignOut] = useState(false);
  // Where the definitions come from: an Azure sign-in method (the live workspace), or a repository.
  const [source, setSource] = useState<SourceMethod>(() => initialSource(app.connection, config.method));
  const [repo, setRepoState] = useState<RepoForm>(rememberedRepo);
  const [repoErrors, setRepoErrors] = useState<RepoErrors>({});
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const setRepo = (patch: Partial<RepoForm>) => {
    setRepoState((f) => { const next = { ...f, ...patch }; rememberedRepo = next; return next; });
    setRepoErrors({});
  };
  const discovered = app.discovery.state === "completed" || app.discovery.state === "completed_with_warnings";
  const signOut = () => { setChanging(false); setConfirmSignOut(false); void app.disconnect(); };

  const connected = app.isConnected;
  const supported = mode === "mock" || !health || health.capabilities.authMethods.includes(config.method);
  const busy = connectionBusy !== null;

  // The backend may have been restarted since this page loaded; ask again
  // whenever the method changes so a stale answer never blocks a working option.
  useEffect(() => { void app.refreshHealth(); }, [config.method]); // eslint-disable-line react-hooks/exhaustive-deps

  // One sign-in reaches every subscription the account can see; the subscription is chosen after it.
  const signedInHere = !!connection.signedIn && (connection.signInMethod ?? connection.method) === config.method;
  const signedInAs = signedInHere && (
    <p className="signed-in-as muted"><CheckCircle2 size={14} className="tone-success" aria-hidden="true" /> Signed in as <strong>{connection.account ?? "your Microsoft account"}</strong></p>
  );
  // Directories the account belongs to whose policy wants their own sign-in before showing subscriptions.
  const locked = signedInHere ? (connection.directories ?? []).filter((d) => d.needsSignIn) : [];
  const otherDirectories = locked.length > 0 && (
    <div className="directory-signins" role="group" aria-label="Other directories">
      <span className="muted">
        {locked.length === 1 ? "Your account also belongs to a directory" : `Your account also belongs to ${locked.length} directories`} that
        {locked.length === 1 ? " asks" : " ask"} for its own sign-in before showing subscriptions:
      </span>
      {locked.map((d) => (
        <Button key={d.id} size="small" onClick={() => void app.authenticateDirectory(d.id)} loading={connectionBusy === "authenticate"} disabled={busy}>
          Sign in to {d.name}
        </Button>
      ))}
    </div>
  );

  const set = (patch: Partial<ConnectionConfig>) => {
    setConfig((c) => ({ ...c, ...patch }));
    setErrors((e) => { const next = { ...e }; for (const k of Object.keys(patch) as (keyof ConnectionConfig)[]) delete next[k]; return next; });
  };

  // After a reload the backend may still hold a sign-in; pick it back up.
  useEffect(() => {
    if (connection.signedIn && connection.subscriptionId && !config.subscriptionId) {
      set({ subscriptionId: connection.subscriptionId, method: authOf(connection.method), tenantId: connection.tenantId ?? "" });
    }
  }, [connection.signedIn, connection.subscriptionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectMethod = (method: SourceMethod) => {
    setErrors({});
    setRepoErrors({});
    app.clearConnectionError();
    setSource(method);
    // The workspace chosen stays chosen when moving between tiles; another sign-in method starts over.
    if (method !== "zip" && method !== "git" && method !== config.method) setConfig((c) => ({ ...c, method, resourceGroup: "", workspace: "", sqlPool: "" }));
  };

  const upload = async (file: File) => {
    setUploading(true);
    setUploadError(null);
    try {
      const found = await app.uploadZip(file);
      setRepo({ upload: found, rootFolder: found.rootFolder ?? "" });
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : "The ZIP could not be uploaded.");
    } finally {
      setUploading(false);
    }
  };

  /** Change a repository connection: the same tile and choices it was made with. */
  const changeRepository = () => {
    const r = connection.repository!;
    if (connection.subscriptionId) setConfig(configFromConnection(connection));
    // After a reload only the connection is known: the repository and the live workspace come back from it.
    if (r.kind === "git" && !repo.url.trim()) setRepo({ url: r.label, ref: r.ref ?? "", rootFolder: r.rootFolder, withPool: !!connection.sqlPool });
    setSource(r.kind);
    setChanging(true);
  };

  const connectRepository = async (kind: RepositoryKind) => {
    const problems = validateRepo(kind, repo, config, signedInHere);
    setRepoErrors(problems);
    if (problems.environment || problems.parameters) setMoreOpen(true);
    if (Object.keys(problems).length) return;
    const request: RepositoryConfig = {
      kind, environment: environmentOf(repo), rootFolder: repo.rootFolder.trim() || undefined,
      ...(kind === "git" ? { repositoryUrl: repo.url.trim(), ref: repo.ref.trim() || undefined } : { uploadId: repo.upload!.uploadId, fileName: repo.upload!.fileName }),
      ...(repo.parameters ? { parameters: repo.parameters, parametersName: repo.parametersName } : {}),
      ...(repo.withPool ? { subscriptionId: config.subscriptionId, resourceGroup: config.resourceGroup, workspace: config.workspace, sqlPool: config.sqlPool } : {}),
    };
    lastRepositoryRequest = request;
    remembered = config;
    await app.connectRepository(request);
    setChanging(false);
  };

  const submit = async (action: "authenticate" | "test") => {
    const found = validate(config, action);
    setErrors(found);
    if (Object.keys(found).length) return;
    remembered = config;
    await (action === "test" ? app.testConnection(config) : app.authenticate(config));
    if (action === "test") setChanging(false);
  };

  if (connected && !changing && connection.repository) {
    return (
      <div className="stack conn-body">
        <RepositorySummary connection={connection} />
        <CheckFold checks={connection.checks} label="Source connection checks" />
        <div className="row conn-actions">
          {lastRepositoryRequest && <Button size="small" onClick={() => void app.connectRepository(lastRepositoryRequest!)} loading={connectionBusy === "test"}>Connect again</Button>}
          <Button size="small" onClick={changeRepository}>Change</Button>
          <Button size="small" variant="ghost" onClick={() => (discovered ? setConfirmSignOut(true) : signOut())} disabled={busy || app.discovery.state === "running"}
            title={app.discovery.state === "running" ? "Discovery is running; wait for it to finish" : undefined}>Disconnect</Button>
        </div>
        {confirmSignOut && (
          <ConfirmDialog title="Disconnect the repository?" confirmLabel="Disconnect" onConfirm={signOut} onCancel={() => setConfirmSignOut(false)}>
            <p>The accelerator stops using {connection.repository.label} ({connection.repository.environment}). The repository itself is not changed.</p>
            <p>The discovered inventory ({(app.discovery.summary?.total ?? 0).toLocaleString()} objects) is cleared as well, so discovery runs again after you reconnect. Your plan, its options and the record of a migration run are kept.</p>
          </ConfirmDialog>
        )}
      </div>
    );
  }

  if (connected && !changing) {
    return (
      <div className="stack conn-body">
        <SummaryList rows={[
          ["Workspace", <strong key="w">{connection.workspace}</strong>],
          ["Resource group", connection.resourceGroup],
          ["Subscription", connection.subscriptionName ?? connection.subscriptionId],
          ["Dedicated SQL pool", connection.sqlPool || "Not configured"],
          ["Signed in as", connection.account ?? methodLabel(connection.method)],
          ["Last tested", connection.testedAt ? new Date(connection.testedAt).toLocaleString() : "—"],
        ]} />
        <CheckFold checks={connection.checks} label="Source connection checks" />
        <div className="row conn-actions">
          <Button size="small" onClick={() => void app.testConnection(configFromConnection(connection))} loading={connectionBusy === "test"}>Test again</Button>
          <Button size="small" onClick={() => { setConfig(configFromConnection(connection)); setSource(authOf(connection.method)); setChanging(true); }}>Change</Button>
          <Button size="small" variant="ghost" onClick={() => (discovered ? setConfirmSignOut(true) : signOut())} disabled={busy || app.discovery.state === "running"}
            title={app.discovery.state === "running" ? "Discovery is running; wait for it to finish" : undefined}>Disconnect</Button>
        </div>
        {confirmSignOut && (
          <ConfirmDialog title="Disconnect Azure Synapse?" confirmLabel="Disconnect" onConfirm={signOut} onCancel={() => setConfirmSignOut(false)}>
            <p>The accelerator signs out of {connection.workspace}. Your Azure CLI session is untouched.</p>
            <p>The discovered inventory ({(app.discovery.summary?.total ?? 0).toLocaleString()} objects) is cleared as well, so discovery runs again after you reconnect. Your plan, its options and the record of a migration run are kept.</p>
          </ConfirmDialog>
        )}
      </div>
    );
  }

  const repositoryProblems = (
    <>
      {connectionError && (
        <Banner tone="error" title={connectionError.title} actions={<Button size="small" onClick={app.clearConnectionError}>Dismiss</Button>}>
          {connectionError.hint && <div>{connectionError.hint}</div>}
          <div className="faint" style={{ marginTop: 4 }}>{connectionError.message}</div>
        </Banner>
      )}
      {!connectionError && connection.checks.length > 0 && !connected && <CheckList checks={connection.checks} label="Source connection checks" />}
    </>
  );
  const waitingForSignIn = connectionBusy === "authenticate" && (
    <Banner tone="info" title="Waiting for you to sign in">
      A Microsoft sign-in window has opened. Enter your email, password and any MFA prompt there; this page continues on its own.
    </Banner>
  );
  // A repository holds definitions only; the live workspace adds its dedicated SQL pool. Sign in and choose it.
  const liveAddon = (
    <div className="stack pool-addon">
      <div className="row">
        <Button variant={signedInHere ? "default" : "primary"} size="small" onClick={() => void submit("authenticate")} loading={connectionBusy === "authenticate"} disabled={busy}>
          {signedInHere ? "Sign in again" : "Sign in with Azure"}
        </Button>
        {signedInAs}
      </div>
      {waitingForSignIn}
      {otherDirectories}
      <ScopeSelectors config={config} errors={errors} set={set} signedIn={signedInHere} version={connection.subscriptionCount ?? 0} />
      {repoErrors.pool && <p className="err">{repoErrors.pool}</p>}
    </div>
  );

  if (source === "git") {
    return (
      <div className="stack conn-body">
        <MethodTiles label="How to connect to Azure Synapse" options={METHODS} value={source} onChange={selectMethod} disabled={busy} />
        <MiniSteps steps={["Repository URL", "Connect"]} current={repo.url.trim() ? 1 : 0} />

        <div className="form-grid">
          <TextField full label="Repository URL" value={repo.url} onChange={(e) => setRepo({ url: e.target.value })} error={repoErrors.url}
            placeholder="https://dev.azure.com/contoso/Data/_git/synapse"
            hint="Cloned by git on this machine with your own sign-in (credential helper or SSH key). No token is entered here." />
        </div>

        <label className="check-row">
          <input type="checkbox" checked={repo.withPool} onChange={(e) => setRepo({ withPool: e.target.checked })} />
          <span><strong>Also connect to the live Synapse workspace</strong><span className="muted"> — for its dedicated SQL pool: tables, data loads, date filters and row counts. The repository holds definitions only; without the workspace those stages are skipped.</span></span>
        </label>
        {repo.withPool && liveAddon}

        <MoreOptions kind="git" form={repo} setForm={setRepo} errors={repoErrors} open={moreOpen} onToggle={setMoreOpen} />
        {repositoryProblems}

        <div className="row conn-actions">
          <Button variant="primary" onClick={() => void connectRepository("git")} loading={connectionBusy === "test"} disabled={busy}>Connect repository</Button>
          {changing && <Button variant="ghost" onClick={() => setChanging(false)}>Cancel</Button>}
        </div>
      </div>
    );
  }

  if (source === "zip") {
    return (
      <div className="stack conn-body">
        <MethodTiles label="How to connect to Azure Synapse" options={METHODS} value={source} onChange={selectMethod} disabled={busy || uploading} />
        <MiniSteps steps={["Upload", "Connect"]} current={repo.upload ? 1 : 0} />

        <ZipUploadField form={repo} error={uploadError ?? repoErrors.upload} uploading={uploading} onUpload={(f) => void upload(f)} />

        <label className="check-row">
          <input type="checkbox" checked={repo.withPool} onChange={(e) => setRepo({ withPool: e.target.checked })} />
          <span><strong>Also read the dedicated SQL pool</strong><span className="muted"> — for tables, data loads, date filters and row counts. The export holds definitions only; without the pool those stages are skipped.</span></span>
        </label>
        {repo.withPool && liveAddon}

        <MoreOptions kind="zip" form={repo} setForm={setRepo} errors={repoErrors} open={moreOpen} onToggle={setMoreOpen} />
        {repositoryProblems}

        <div className="row conn-actions">
          <Button variant="primary" onClick={() => void connectRepository("zip")} loading={connectionBusy === "test"} disabled={busy || uploading}>Connect export</Button>
          {changing && <Button variant="ghost" onClick={() => setChanging(false)}>Cancel</Button>}
        </div>
      </div>
    );
  }

  // The live workspace: sign in, choose it, connect. Its definitions are read from what is published.
  const step = signedInHere ? (config.workspace ? 2 : 1) : 0;
  return (
    <div className="stack conn-body">
      <MethodTiles label="How to connect to Azure Synapse" options={METHODS} value={source} onChange={selectMethod} disabled={busy} />
      <MiniSteps steps={["Sign in", "Choose workspace", "Connect"]} current={step} />

      {!supported && (
        <Banner tone="warning" title="Not available in this backend">The running backend does not support {METHOD_LABEL[config.method]}. Restart it from the latest code.</Banner>
      )}
      {!signedInHere && connectionBusy !== "authenticate" && (
        <p className="muted" style={{ margin: 0 }}>Sign in with your Microsoft account and choose the workspace: there is nothing to type.</p>
      )}
      {signedInAs}
      {waitingForSignIn}
      {otherDirectories}
      <ScopeSelectors config={config} errors={errors} set={set} signedIn={signedInHere} version={connection.subscriptionCount ?? 0} />

      {connectionError && (
        <Banner tone="error" title={connectionError.title} actions={<><Button size="small" onClick={() => void submit(signedInHere ? "test" : "authenticate")} disabled={busy}>Retry</Button><Button size="small" onClick={app.clearConnectionError}>Dismiss</Button></>}>
          {connectionError.hint && <div>{connectionError.hint}</div>}
          <div className="faint" style={{ marginTop: 4 }}>{connectionError.message}</div>
        </Banner>
      )}
      {!connectionError && connection.checks.length > 0 && !connected && <CheckList checks={connection.checks} label="Source connection checks" />}

      <div className="row conn-actions">
        {signedInHere && (
          <Button variant="primary" onClick={() => void submit("test")} loading={connectionBusy === "test"} disabled={busy || !supported || !config.workspace}>Connect</Button>
        )}
        <Button variant={signedInHere ? "default" : "primary"} onClick={() => void submit("authenticate")} loading={connectionBusy === "authenticate"} disabled={busy || !supported}>
          {signedInHere ? "Sign in again" : "Sign in with Azure"}
        </Button>
        {changing && <Button variant="ghost" onClick={() => setChanging(false)}>Cancel</Button>}
      </div>
    </div>
  );
}
