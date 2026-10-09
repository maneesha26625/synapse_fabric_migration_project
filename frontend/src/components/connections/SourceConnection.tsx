import { AppWindow, CheckCircle2, CircleSlash, FileArchive, FileJson, GitBranch, SquareTerminal, X, XCircle } from "lucide-react";
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { useAppState } from "../../state/AppState";
import type { AuthMethod, ConnectionConfig, ConnectionState, RepositoryConfig, RepositoryKind, ZipUpload } from "../../types";
import { Banner, Button, SelectField, TextField, ConfirmDialog } from "../shared/Shared";
import { MethodTiles, MiniSteps, type MethodOption } from "./MethodTiles";

/* ---- Methods ------------------------------------------------------------------------ */

type SourceMethod = AuthMethod | "zip" | "git";

const METHODS: MethodOption<SourceMethod>[] = [
  { id: "azure_cli", title: "Azure CLI", desc: "Sign in with your Azure account in a browser window", icon: SquareTerminal },
  { id: "interactive_browser", title: "Interactive browser", desc: "A Microsoft sign-in for the tenant you name; your Azure CLI session is untouched", icon: AppWindow },
  { id: "zip", title: "Workspace export (ZIP)", desc: "Upload one environment's exported repository folder as a ZIP", icon: FileArchive },
  { id: "git", title: "Git repository", desc: "Read one environment's branch of the workspace's Git repository", icon: GitBranch },
];
const METHOD_LABEL: Record<AuthMethod, string> = { azure_cli: "Azure CLI", interactive_browser: "Interactive browser" };
const isRepository = (m: string | undefined): m is RepositoryKind => m === "git" || m === "zip";
const methodLabel = (m: ConnectionState["method"]) => (m === "azure_cli" || m === "interactive_browser" ? METHOD_LABEL[m] : "—");

/* ---- Form state ------------------------------------------------------------------ */

/** The Azure sign-in method behind a connection; a repository source signs in to Azure only for its SQL pool. */
const authOf = (m: ConnectionState["method"]): AuthMethod => (m === "interactive_browser" ? "interactive_browser" : "azure_cli");

const GUID = /^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$/;
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

function validate(c: ConnectionConfig, action: "authenticate" | "test"): Errors {
  const e: Errors = {};
  const need = (k: keyof ConnectionConfig, label: string) => { if (!c[k].trim()) e[k] = `${label} is required.`; };
  const guid = (k: keyof ConnectionConfig, label: string) => { if (c[k].trim() && !GUID.test(c[k].trim())) e[k] = `${label} must be a GUID.`; };
  need("subscriptionId", "Subscription ID"); guid("subscriptionId", "Subscription ID");
  guid("tenantId", "Tenant ID");
  if (action === "authenticate") {
    // The browser sign-in must open against the operator's own tenant.
    if (c.method === "interactive_browser") need("tenantId", "Tenant ID");
  } else {
    need("resourceGroup", "Resource group");
    need("workspace", "Synapse workspace");
  }
  return e;
}

/* ---- Scope: resource group -> workspace -> SQL pool -------------------------------- */

interface ScopeProps {
  config: ConnectionConfig;
  errors: Errors;
  set: (patch: Partial<ConnectionConfig>) => void;
  signedIn: boolean;
}

/**
 * Every dropdown is filled from the signed-in identity, so nothing is typed
 * that could be misspelt, and only what that identity can see is offered.
 */
function ScopeSelectors({ config, errors, set, signedIn }: ScopeProps) {
  const { azureLists } = useAppState();
  const [groups, setGroups] = useState<string[]>([]);
  const [workspaces, setWorkspaces] = useState<string[]>([]);
  const [pools, setPools] = useState<string[]>([]);
  const [loading, setLoading] = useState<"groups" | "workspaces" | "pools" | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  const load = async (kind: "groups" | "workspaces" | "pools", fn: () => Promise<string[]>, apply: (v: string[]) => void) => {
    setLoading(kind);
    setListError(null);
    try { apply(await fn()); } catch (e) { setListError(e instanceof Error ? e.message : "The list could not be loaded."); apply([]); }
    finally { setLoading(null); }
  };

  // A list with exactly one entry is chosen for the operator: nobody should have to
  // open a dropdown to pick its only item, and the SQL pool is never skipped by accident.
  useEffect(() => {
    if (!signedIn) { setGroups([]); return; }
    void load("groups", () => azureLists.listResourceGroups(), (v) => {
      setGroups(v);
      if (v.length === 1 && !config.resourceGroup) set({ resourceGroup: v[0], workspace: "", sqlPool: "" });
    });
  }, [signedIn]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setWorkspaces([]); setPools([]);
    if (!signedIn || !config.resourceGroup) return;
    void load("workspaces", () => azureLists.listWorkspaces(config.resourceGroup), (v) => {
      setWorkspaces(v);
      if (v.length === 1 && !config.workspace) set({ workspace: v[0], sqlPool: "" });
    });
  }, [signedIn, config.resourceGroup]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setPools([]);
    if (!signedIn || !config.resourceGroup || !config.workspace) return;
    void load("pools", () => azureLists.listSqlPools(config.resourceGroup, config.workspace), (v) => {
      setPools(v);
      if (v.length === 1 && !config.sqlPool) set({ sqlPool: v[0] });
    });
  }, [signedIn, config.resourceGroup, config.workspace]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!signedIn) return null;
  return (
    <>
      <div className="form-grid">
        <SelectField label="Resource group" value={config.resourceGroup} onChange={(v) => set({ resourceGroup: v, workspace: "", sqlPool: "" })} options={groups} loading={loading === "groups"} placeholder="Select a resource group" error={errors.resourceGroup} />
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
const EMPTY_REPO: RepoForm = { url: "", ref: "", rootFolder: "", environment: "", customEnvironment: "", parameters: "", parametersName: "", upload: null, withPool: false };
// Like the Azure form: kept in memory so it survives navigation, never in browser storage.
let rememberedRepo: RepoForm = EMPTY_REPO;
let lastRepositoryRequest: RepositoryConfig | null = null;

type RepoErrors = Partial<Record<"url" | "upload" | "environment" | "parameters" | "pool", string>>;

function environmentOf(f: RepoForm): string {
  return f.environment === "Other" ? f.customEnvironment.trim() : f.environment;
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
  if (!environmentOf(f)) e.environment = f.environment === "Other" ? "Name the environment." : "Choose the environment.";
  if (f.withPool && !(signedIn && config.resourceGroup && config.workspace && config.sqlPool)) {
    e.pool = signedIn ? "Choose the resource group, workspace and SQL pool, or switch the SQL pool off." : "Sign in to Azure to add the SQL pool, or switch it off.";
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

function RepositoryFields({ kind, form, setForm, errors, uploadError, uploading, onUpload }: {
  kind: RepositoryKind; form: RepoForm; setForm: (p: Partial<RepoForm>) => void; errors: RepoErrors;
  uploadError: string | null; uploading: boolean; onUpload: (f: File) => void;
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
  const found = form.upload ? Object.values(form.upload.counts).reduce((a, b) => a + b, 0) : 0;
  return (
    <div className="form-grid">
      {kind === "git" ? (
        <>
          <TextField full label="Repository URL" value={form.url} onChange={(e) => setForm({ url: e.target.value })} error={errors.url}
            placeholder="https://github.com/contoso/synapse-workspace.git"
            hint="Cloned by git on this machine with your own credentials (credential helper or SSH key). No token is entered here." />
          <TextField label="Branch" optional value={form.ref} onChange={(e) => setForm({ ref: e.target.value })} placeholder="main"
            hint="This environment's branch, e.g. main for Dev or a release branch. Empty: the default branch." />
          <TextField label="Root folder" optional value={form.rootFolder} onChange={(e) => setForm({ rootFolder: e.target.value })} placeholder="synapse"
            hint="The root folder in the workspace's Git settings. Empty: found automatically." />
        </>
      ) : (
        <>
          <FilePick label="Workspace export (ZIP)" accept=".zip,application/zip" fileName={form.upload?.fileName ?? ""} busy={uploading} onPick={onUpload}
            icon={<FileArchive size={14} aria-hidden="true" />} error={uploadError ?? errors.upload}
            hint={form.upload
              ? (form.upload.rootFolder !== null ? `${found.toLocaleString()} definitions found${form.upload.rootFolder ? ` in ${form.upload.rootFolder}/` : ""}.` : form.upload.note ?? "No Synapse folders found yet: name the root folder.")
              : "A ZIP of the workspace's Git repository (for example the branch downloaded from GitHub or Azure DevOps). Up to 200 MB."} />
          <TextField label="Root folder" optional value={form.rootFolder} onChange={(e) => setForm({ rootFolder: e.target.value })}
            placeholder={form.upload?.rootFolder || "synapse"} hint="Where the definitions are inside the ZIP. Empty: found automatically." />
        </>
      )}
      <SelectField label="Environment" value={form.environment} onChange={(v) => setForm({ environment: v })} options={ENVIRONMENTS} placeholder="Choose the environment" error={errors.environment}
        hint="Which environment these definitions are for. Shown with the connection and in the report." />
      {form.environment === "Other" && (
        <TextField label="Environment name" value={form.customEnvironment} onChange={(e) => setForm({ customEnvironment: e.target.value })} placeholder="UAT" />
      )}
      <FilePick label="Environment parameters (optional)" accept=".json,application/json" fileName={form.parametersName}
        onPick={(f) => void pickParameters(f)} onClear={() => setForm({ parameters: "", parametersName: "" })} icon={<FileJson size={14} aria-hidden="true" />}
        error={paramError ?? errors.parameters}
        hint="The environment's ARM parameters file, such as TemplateParametersForWorkspace.json or a Test/Prod copy. Its linked-service values (servers, URLs) replace the ones committed in the repository, so migrated connections point at this environment. Without it, the committed values are used." />
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

/** Connect Azure Synapse: choose how to sign in, sign in, choose the workspace, test. */
export function SourceConnectionPanel() {
  const app = useAppState();
  const { connection, connectionBusy, connectionError, health, mode } = app;
  const [config, setConfig] = useState<ConnectionConfig>(() => ({ ...EMPTY, ...(remembered ?? {}) }));
  const [errors, setErrors] = useState<Errors>({});
  const [changing, setChanging] = useState(false);
  const [confirmSignOut, setConfirmSignOut] = useState(false);
  // Where the definitions come from: an Azure sign-in method (the live workspace), or a repository.
  const [source, setSource] = useState<SourceMethod>(() => (isRepository(app.connection.sourceKind) ? app.connection.sourceKind : config.method));
  const [repo, setRepoState] = useState<RepoForm>(rememberedRepo);
  const [repoErrors, setRepoErrors] = useState<RepoErrors>({});
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const setRepo = (patch: Partial<RepoForm>) => {
    setRepoState((f) => { const next = { ...f, ...patch }; rememberedRepo = next; return next; });
    setRepoErrors({});
  };
  const discovered = app.discovery.state === "completed" || app.discovery.state === "completed_with_warnings";
  const signOut = () => { setChanging(false); setConfirmSignOut(false); void app.disconnect(); };

  const connected = app.isConnected;
  const supported = mode === "mock" || !health || health.capabilities.authMethods.includes(config.method);
  const busy = connectionBusy !== null;
  const browser = config.method === "interactive_browser";

  // The backend may have been restarted since this page loaded; ask again
  // whenever the method changes so a stale answer never blocks a working option.
  useEffect(() => { void app.refreshHealth(); }, [config.method]); // eslint-disable-line react-hooks/exhaustive-deps

  // Signed in *as this method, for the subscription shown*: changing either
  // invalidates the dropdowns until the operator signs in again.
  const signedInHere =
    !!connection.signedIn &&
    (connection.signInMethod ?? connection.method) === config.method &&
    !!connection.subscriptionId &&
    connection.subscriptionId.toLowerCase() === config.subscriptionId.trim().toLowerCase();

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
    if (isRepository(method)) return;
    setConfig((c) => ({ ...c, method, resourceGroup: "", workspace: "", sqlPool: "" }));
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

  const connectRepository = async (kind: RepositoryKind) => {
    const found = validateRepo(kind, repo, config, signedInHere);
    setRepoErrors(found);
    if (Object.keys(found).length) return;
    const request: RepositoryConfig = {
      kind, environment: environmentOf(repo), rootFolder: repo.rootFolder.trim() || undefined,
      ...(kind === "git" ? { repositoryUrl: repo.url.trim(), ref: repo.ref.trim() || undefined } : { uploadId: repo.upload!.uploadId, fileName: repo.upload!.fileName }),
      ...(repo.parameters ? { parameters: repo.parameters, parametersName: repo.parametersName } : {}),
      ...(repo.withPool ? { resourceGroup: config.resourceGroup, workspace: config.workspace, sqlPool: config.sqlPool } : {}),
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
          <Button size="small" onClick={() => { setSource(connection.repository!.kind); setChanging(true); }}>Change</Button>
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
          ["Signed in with", methodLabel(connection.method)],
          ["Last tested", connection.testedAt ? new Date(connection.testedAt).toLocaleString() : "—"],
        ]} />
        <CheckFold checks={connection.checks} label="Source connection checks" />
        <div className="row conn-actions">
          <Button size="small" onClick={() => void app.testConnection(configFromConnection(connection))} loading={connectionBusy === "test"}>Test again</Button>
          <Button size="small" onClick={() => { setConfig(configFromConnection(connection)); setChanging(true); }}>Change</Button>
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

  if (isRepository(source)) {
    const signInFields = config.method === "interactive_browser" ? (
      <>
        <TextField label="Tenant ID" value={config.tenantId} onChange={(e) => set({ tenantId: e.target.value })} error={errors.tenantId} placeholder="00000000-0000-0000-0000-000000000000" />
        <TextField label="Subscription ID" value={config.subscriptionId} onChange={(e) => set({ subscriptionId: e.target.value })} error={errors.subscriptionId} placeholder="00000000-0000-0000-0000-000000000000" />
      </>
    ) : (
      <>
        <TextField label="Subscription ID" value={config.subscriptionId} onChange={(e) => set({ subscriptionId: e.target.value })} error={errors.subscriptionId} placeholder="00000000-0000-0000-0000-000000000000" />
        <TextField label="Tenant ID" optional value={config.tenantId} onChange={(e) => set({ tenantId: e.target.value })} error={errors.tenantId} />
      </>
    );
    return (
      <div className="stack conn-body">
        <MethodTiles label="How to connect to Azure Synapse" options={METHODS} value={source} onChange={selectMethod} disabled={busy || uploading} />
        <MiniSteps steps={[source === "git" ? "Repository" : "Upload", "Environment", "Connect"]} current={(source === "git" ? repo.url.trim() : repo.upload) ? (environmentOf(repo) ? 2 : 1) : 0} />

        <RepositoryFields kind={source} form={repo} setForm={setRepo} errors={repoErrors} uploadError={uploadError} uploading={uploading} onUpload={(f) => void upload(f)} />

        <label className="check-row">
          <input type="checkbox" checked={repo.withPool} onChange={(e) => setRepo({ withPool: e.target.checked })} />
          <span><strong>Also read the dedicated SQL pool</strong><span className="muted"> — for tables, data loads, date filters and row counts. The repository holds definitions only; without the pool those stages are skipped.</span></span>
        </label>
        {repo.withPool && (
          <div className="stack pool-addon">
            <div className="form-grid">{signInFields}</div>
            <div className="row">
              <Button variant={signedInHere ? "default" : "primary"} size="small" onClick={() => void submit("authenticate")} loading={connectionBusy === "authenticate"} disabled={busy}>
                {signedInHere ? "Sign in again" : "Sign in with Azure"}
              </Button>
              {signedInHere && <span className="muted">Signed in to {connection.subscriptionName ?? connection.subscriptionId}</span>}
            </div>
            <ScopeSelectors config={config} errors={errors} set={set} signedIn={signedInHere} />
            {repoErrors.pool && <p className="err">{repoErrors.pool}</p>}
          </div>
        )}

        {connectionError && (
          <Banner tone="error" title={connectionError.title} actions={<Button size="small" onClick={app.clearConnectionError}>Dismiss</Button>}>
            {connectionError.hint && <div>{connectionError.hint}</div>}
            <div className="faint" style={{ marginTop: 4 }}>{connectionError.message}</div>
          </Banner>
        )}
        {!connectionError && connection.checks.length > 0 && !connected && <CheckList checks={connection.checks} label="Source connection checks" />}

        <div className="row conn-actions">
          <Button variant="primary" onClick={() => void connectRepository(source)} loading={connectionBusy === "test"} disabled={busy || uploading}>
            {source === "git" ? "Connect repository" : "Connect export"}
          </Button>
          {changing && <Button variant="ghost" onClick={() => setChanging(false)}>Cancel</Button>}
        </div>
      </div>
    );
  }

  const stage = signedInHere ? (config.workspace ? 2 : 1) : 0;
  return (
    <div className="stack conn-body">
      <MethodTiles label="How to connect to Azure Synapse" options={METHODS} value={source} onChange={selectMethod} disabled={busy} />
      <MiniSteps steps={["Sign in", "Choose workspace", "Test"]} current={stage} />

      {!supported && (
        <Banner tone="warning" title="Not available in this backend">The running backend does not support {METHOD_LABEL[config.method]}. Restart it from the latest code.</Banner>
      )}

      <div className="form-grid">
        {browser ? (
          <>
            <TextField label="Tenant ID" value={config.tenantId} onChange={(e) => set({ tenantId: e.target.value })} error={errors.tenantId} placeholder="00000000-0000-0000-0000-000000000000" hint="The sign-in window opens against this tenant." />
            <TextField label="Subscription ID" value={config.subscriptionId} onChange={(e) => set({ subscriptionId: e.target.value })} error={errors.subscriptionId} placeholder="00000000-0000-0000-0000-000000000000" />
          </>
        ) : (
          <>
            <TextField label="Subscription ID" value={config.subscriptionId} onChange={(e) => set({ subscriptionId: e.target.value })} error={errors.subscriptionId} placeholder="00000000-0000-0000-0000-000000000000" />
            <TextField label="Tenant ID" optional value={config.tenantId} onChange={(e) => set({ tenantId: e.target.value })} error={errors.tenantId} hint="Found from the subscription; enter it only if sign-in picks the wrong tenant." />
          </>
        )}
      </div>

      {connectionBusy === "authenticate" && (
        <Banner tone="info" title="Waiting for you to sign in">
          {browser ? "A sign-in window should open on the machine running the accelerator. Complete it there; this page continues on its own." : "A sign-in window has opened in your browser. Complete it there; this page continues on its own."}
        </Banner>
      )}

      <ScopeSelectors config={config} errors={errors} set={set} signedIn={signedInHere} />

      {connectionError && (
        <Banner tone="error" title={connectionError.title} actions={<><Button size="small" onClick={() => void submit(signedInHere ? "test" : "authenticate")} disabled={busy}>Retry</Button><Button size="small" onClick={app.clearConnectionError}>Dismiss</Button></>}>
          {connectionError.hint && <div>{connectionError.hint}</div>}
          <div className="faint" style={{ marginTop: 4 }}>{connectionError.message}</div>
        </Banner>
      )}
      {!connectionError && connection.checks.length > 0 && !connected && <CheckList checks={connection.checks} label="Source connection checks" />}

      <div className="row conn-actions">
        <Button variant={signedInHere ? "default" : "primary"} onClick={() => void submit("authenticate")} loading={connectionBusy === "authenticate"} disabled={busy || !supported}>
          {signedInHere ? "Sign in again" : browser ? "Authenticate" : "Sign in with Azure"}
        </Button>
        <Button variant={signedInHere ? "primary" : "default"} onClick={() => void submit("test")} loading={connectionBusy === "test"} disabled={busy || !supported || !(signedInHere && config.workspace)}>Test connection</Button>
        {changing && <Button variant="ghost" onClick={() => setChanging(false)}>Cancel</Button>}
      </div>
    </div>
  );
}
