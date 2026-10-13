import { CheckCircle2, ChevronDown, ChevronRight, CircleSlash, FileArchive, FileJson, GitBranch, Info, Layers, X, XCircle } from "lucide-react";
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { useAppState } from "../../state/AppState";
import { ApiRequestError } from "../../types";
import type { AuthMethod, AzureSubscription, ConnectionConfig, ConnectionState, RepositoryConfig, RepositoryKind, WorkspaceGit, ZipUpload } from "../../types";
import { Banner, Button, SelectField, StatusBadge, TextField, ConfirmDialog } from "../shared/Shared";
import { MethodTiles, MiniSteps, type MethodOption } from "./MethodTiles";

/* ---- Methods ------------------------------------------------------------------------ */

type SourceMethod = AuthMethod | "zip";

// One option for the workspace, whether its definitions are then read from its Git repository or the
// live workspace: both start with the same sign-in and the same choice of workspace and SQL pool.
const METHODS: MethodOption<SourceMethod>[] = [
  { id: "azure_cli", title: "Synapse workspace", desc: "Sign in and choose the workspace. Its Git repository is read when it has one", icon: Layers },
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
/** The workspace (subscription/group/name) the remembered branch, folder and environment choices were made for. */
let rememberedRepoKey = "";

type RepoErrors = Partial<Record<"url" | "upload" | "environment" | "parameters" | "pool", string>>;

/** The environment's name; Dev unless another one is chosen. */
function environmentOf(f: RepoForm): string {
  return (f.environment === "Other" ? f.customEnvironment.trim() : f.environment) || "Dev";
}

/** `typed`: the Git repository is a URL typed by hand rather than the one found for the workspace. */
function validateRepo(kind: RepositoryKind, f: RepoForm, config: ConnectionConfig, signedIn: boolean, typed: boolean): RepoErrors {
  const e: RepoErrors = {};
  if (kind === "git") {
    const url = f.url.trim();
    if (typed && !url) e.url = "Repository URL is required.";
    else if (typed && !/^(https:\/\/|ssh:\/\/|git@)/.test(url)) e.url = "Use the repository's HTTPS or SSH URL.";
    else if (typed && /^https:\/\/[^/@]+@/.test(url)) e.url = "Leave credentials out of the URL: git on this machine signs in with your own credential helper or SSH key.";
  } else if (!f.upload) {
    e.upload = "Choose the ZIP to upload.";
  }
  if (f.environment === "Other" && !f.customEnvironment.trim()) e.environment = "Name the environment.";
  if (kind === "zip" && f.withPool && !(signedIn && config.subscriptionId && config.resourceGroup && config.workspace && config.sqlPool)) {
    e.pool = signedIn ? "Choose the subscription, resource group, workspace and SQL pool, or switch the SQL pool off." : "Sign in to Azure to add the SQL pool, or switch it off.";
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
function MoreOptions({ kind, form, setForm, errors, found, open, onToggle }: {
  kind: RepositoryKind; form: RepoForm; setForm: (p: Partial<RepoForm>) => void; errors: RepoErrors;
  found?: WorkspaceGit | null; open: boolean; onToggle: (open: boolean) => void;
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
            <TextField label="Branch" optional value={form.ref} onChange={(e) => setForm({ ref: e.target.value })} placeholder={found?.branch || "main"}
              hint={found?.branch ? `Empty: ${found.branch}, the workspace's collaboration branch. Name a release branch to read another environment.` : "Empty: the repository's default branch."} />
          )}
          <TextField label="Root folder" optional value={form.rootFolder} onChange={(e) => setForm({ rootFolder: e.target.value })}
            placeholder={kind === "git" ? (found ? found.rootFolder || "top level" : "synapse") : form.upload?.rootFolder || "synapse"}
            hint={kind === "git" && found ? "Empty: the folder in the workspace's Git settings." : "Where the definitions are. Empty: found automatically."} />
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

/* ---- The Git repository found for the chosen workspace --------------------------------- */

/** What reading a workspace's Git integration found, for the workspace (subscription/group/name) it was read for. */
type Detected =
  | { key: string; state: "loading" }
  | { key: string; state: "found"; git: WorkspaceGit }
  | { key: string; state: "none"; workspace: string }
  | { key: string; state: "error"; workspace: string; message: string };

const providerName = (p?: string) => (p === "AzureDevOps" ? "Azure DevOps" : p || "Git");

/** "Azure DevOps · branch main · folder synapse/" */
const gitWhere = (g: WorkspaceGit) =>
  `${providerName(g.provider)} · branch ${g.branch || "default"} · ${g.rootFolder ? `folder ${g.rootFolder}/` : "top level"}`;

function detectionError(e: unknown): string {
  // An API started before this feature answers 404: say what to do rather than echo "No such list".
  if (e instanceof ApiRequestError && e.status === 404) return "The accelerator's API is running older code. Restart the API, then try again.";
  return e instanceof Error ? e.message : "The workspace's Git settings could not be read.";
}

type ReadFromChoice = "git" | "live";

/**
 * Where the chosen workspace's definitions are read from: its Git repository when it has one
 * (recommended, and chosen for you), or the live workspace. A workspace without Git is simply read live.
 */
function ReadFrom({ detection, readFrom, onReadFrom, typing, onTypeInstead, onUseFound, onRetry }: {
  detection: Detected | null; readFrom: ReadFromChoice; onReadFrom: (v: ReadFromChoice) => void;
  typing: boolean; onTypeInstead: () => void; onUseFound: () => void; onRetry: () => void;
}) {
  if (!detection) return null;
  if (detection.state === "loading") {
    return <p className="muted" role="status" style={{ margin: 0 }}><span className="spinner" aria-hidden="true" /> Checking whether the workspace is connected to Git…</p>;
  }
  if (detection.state === "none") {
    return (
      <p className="muted read-note" role="status">
        <Info size={14} aria-hidden="true" /><span>{detection.workspace} isn't connected to Git, so its definitions are read from the live workspace.</span>
      </p>
    );
  }
  if (detection.state === "error") {
    return (
      <Banner tone="warning" title={`Couldn't check ${detection.workspace}'s Git settings`} actions={<Button size="small" onClick={onRetry}>Try again</Button>}>
        {detection.message} Until then, its definitions are read from the live workspace.
      </Banner>
    );
  }
  const g = detection.git;
  if (typing) {
    return (
      <p className="muted" style={{ margin: 0, overflowWrap: "anywhere" }}>
        {g.workspace}'s own repository is {g.repositoryUrl}. <button type="button" className="link" onClick={onUseFound}>Use it instead</button>
      </p>
    );
  }
  // A long URL wraps at its slashes, never in the middle of a name.
  const url = (g.repositoryUrl ?? "").split("/").flatMap((part, i) => (i ? [<wbr key={i} />, `/${part}`] : [part]));
  return (
    <fieldset className="read-from">
      <legend>Read definitions from</legend>
      <label className={`read-option${readFrom === "git" ? " selected" : ""}`}>
        <input type="radio" name="read-from" checked={readFrom === "git"} onChange={() => onReadFrom("git")} />
        <span className="read-option-text">
          <span className="read-option-title"><strong>Git repository</strong><StatusBadge tone="success">Recommended</StatusBadge></span>
          <span className="read-url">{url}</span>
          <span className="muted">{gitWhere(g)}. Includes work merged but not yet published.</span>
        </span>
      </label>
      <label className={`read-option${readFrom === "live" ? " selected" : ""}`}>
        <input type="radio" name="read-from" checked={readFrom === "live"} onChange={() => onReadFrom("live")} />
        <span className="read-option-text">
          <strong>Live workspace</strong>
          <span className="muted">What is published in {g.workspace} now.</span>
        </span>
      </label>
      {readFrom === "git" && <button type="button" className="link read-other" onClick={onTypeInstead}>Use another repository</button>}
    </fieldset>
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
  const [source, setSource] = useState<SourceMethod>(() => (app.connection.sourceKind === "zip" ? "zip" : config.method));
  const [repo, setRepoState] = useState<RepoForm>(rememberedRepo);
  const [repoErrors, setRepoErrors] = useState<RepoErrors>({});
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // The Git repository found for the chosen workspace; typing a URL by hand is the exception.
  const [detected, setDetected] = useState<Detected | null>(null);
  const [readFrom, setReadFrom] = useState<ReadFromChoice>("git");
  const [typing, setTyping] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [detectAgain, setDetectAgain] = useState(0);
  // A workspace whose repository is already known (swapped in from the live summary) is not read again.
  const seededKey = useRef("");
  // A connected live workspace's Git integration, so it can be swapped for its repository.
  const [liveGit, setLiveGit] = useState<WorkspaceGit | null>(null);
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
    // The workspace chosen stays chosen when coming back from the ZIP tile; another sign-in method starts over.
    if (method !== "zip" && method !== config.method) setConfig((c) => ({ ...c, method, resourceGroup: "", workspace: "", sqlPool: "" }));
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

  // Choosing the workspace is enough: its Git repository, branch and root folder are read from Azure.
  const detectKey = source !== "zip" && signedInHere && config.subscriptionId && config.resourceGroup && config.workspace
    ? `${config.subscriptionId}/${config.resourceGroup}/${config.workspace}` : "";
  useEffect(() => {
    if (!detectKey) { setDetected(null); return; }
    if (seededKey.current === detectKey) { seededKey.current = ""; return; }
    let current = true;
    const workspace = config.workspace;
    setDetected({ key: detectKey, state: "loading" });
    app.azureLists.getWorkspaceGit(config.subscriptionId, config.resourceGroup, workspace).then(
      (git) => { if (current) setDetected(git.configured && git.repositoryUrl ? { key: detectKey, state: "found", git } : { key: detectKey, state: "none", workspace }); },
      (e) => { if (current) setDetected({ key: detectKey, state: "error", workspace, message: detectionError(e) }); },
    );
    return () => { current = false; };
  }, [detectKey, detectAgain]); // eslint-disable-line react-hooks/exhaustive-deps
  const detection = detected && detected.key === detectKey ? detected : null;
  const found = detection?.state === "found" ? detection.git : null;
  // A URL is typed only on request: without signing in, or to read another repository than the workspace's.
  const typed = typing;
  // Git when a URL is typed, or when the workspace has a repository and Git is the choice; otherwise the live workspace.
  const gitChosen = typed || (!!found && readFrom === "git");

  // Branch, folder and environment chosen for another workspace's repository do not carry over to this one.
  const foundKey = found ? detectKey : "";
  useEffect(() => {
    if (!foundKey || foundKey === rememberedRepoKey) return;
    rememberedRepoKey = foundKey;
    setRepo({ ref: "", rootFolder: "", environment: "Dev", customEnvironment: "", parameters: "", parametersName: "" });
  }, [foundKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Is the connected live workspace in Git? Asked once per workspace; an answer that cannot be had is not shown.
  const liveKey = connected && !connection.repository && connection.subscriptionId && connection.resourceGroup && connection.workspace
    ? `${connection.subscriptionId}/${connection.resourceGroup}/${connection.workspace}` : "";
  useEffect(() => {
    setLiveGit(null);
    if (!liveKey) return;
    let current = true;
    app.azureLists.getWorkspaceGit(connection.subscriptionId!, connection.resourceGroup!, connection.workspace!)
      .then((git) => { if (current) setLiveGit(git); }, () => undefined);
    return () => { current = false; };
  }, [liveKey]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Swap the live workspace for its repository: the Git tab, its repository already found, with the same workspace and SQL pool. */
  const switchToLiveGit = () => {
    if (!liveGit) return;
    const c = configFromConnection(connection);
    const key = `${c.subscriptionId}/${c.resourceGroup}/${c.workspace}`;
    seededKey.current = key;
    setDetected({ key, state: "found", git: liveGit });
    setConfig(c);
    setReadFrom("git");
    setTyping(false);
    setErrors({});
    app.clearConnectionError();
    setSource("azure_cli");
    setChanging(true);
  };

  /** Change a repository connection: the same choices it was made with, the workspace's repository found again. */
  const changeRepository = () => {
    const r = connection.repository!;
    if (r.kind === "zip") { setSource("zip"); setChanging(true); return; }
    setConfig(configFromConnection(connection));
    setReadFrom("git");
    // A repository connected without a workspace was a typed URL; it opens as one.
    setTyping(!connection.subscriptionId);
    setSource("azure_cli");
    setChanging(true);
  };

  const connectRepository = async (kind: RepositoryKind) => {
    const git = kind === "git";
    const problems = validateRepo(kind, repo, config, signedInHere, git && typed);
    setRepoErrors(problems);
    if (problems.environment || problems.parameters) setMoreOpen(true);
    if (Object.keys(problems).length || (git && !typed && !found)) return;
    // The found repository unless a URL was typed; a branch or folder under More options overrides what was found.
    const fromWorkspace = git && !typed ? found : null;
    const ref = repo.ref.trim() || fromWorkspace?.branch || "";
    const rootFolder = repo.rootFolder.trim() || fromWorkspace?.rootFolder || "";
    // For Git the SQL pool is simply the one chosen in the list ("None" leaves it out); a ZIP asks with its checkbox.
    const withPool = git ? !!(signedInHere && config.subscriptionId && config.resourceGroup && config.workspace && config.sqlPool) : repo.withPool;
    const request: RepositoryConfig = {
      kind, environment: environmentOf(repo), rootFolder: rootFolder || undefined,
      ...(git ? { repositoryUrl: fromWorkspace?.repositoryUrl ?? repo.url.trim(), ref: ref || undefined } : { uploadId: repo.upload!.uploadId, fileName: repo.upload!.fileName }),
      ...(repo.parameters ? { parameters: repo.parameters, parametersName: repo.parametersName } : {}),
      ...(withPool ? { subscriptionId: config.subscriptionId, resourceGroup: config.resourceGroup, workspace: config.workspace, sqlPool: config.sqlPool } : {}),
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
          ...(liveGit ? [["Git integration", liveGit.configured ? `${liveGit.repositoryUrl} · ${liveGit.branch || "default branch"}` : "None: definitions are read from the live workspace"] as [string, ReactNode]] : []),
          ["Signed in as", connection.account ?? methodLabel(connection.method)],
          ["Last tested", connection.testedAt ? new Date(connection.testedAt).toLocaleString() : "—"],
        ]} />
        <CheckFold checks={connection.checks} label="Source connection checks" />
        <div className="row conn-actions">
          <Button size="small" onClick={() => void app.testConnection(configFromConnection(connection))} loading={connectionBusy === "test"}>Test again</Button>
          {liveGit?.configured && (
            <Button size="small" onClick={switchToLiveGit} disabled={busy || app.discovery.state === "running"}
              title={`Read the definitions from ${gitWhere(liveGit)} instead of the live workspace`}>
              <GitBranch size={14} aria-hidden="true" />Use its Git repository instead
            </Button>
          )}
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
        {repo.withPool && (
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
        )}

        <MoreOptions kind="zip" form={repo} setForm={setRepo} errors={repoErrors} open={moreOpen} onToggle={setMoreOpen} />
        {repositoryProblems}

        <div className="row conn-actions">
          <Button variant="primary" onClick={() => void connectRepository("zip")} loading={connectionBusy === "test"} disabled={busy || uploading}>Connect export</Button>
          {changing && <Button variant="ghost" onClick={() => setChanging(false)}>Cancel</Button>}
        </div>
      </div>
    );
  }

  // The workspace: sign in, choose it, connect. When it is connected to Git its repository is read
  // (recommended, chosen for you); otherwise, or by choice, the live workspace. A typed URL needs no sign-in.
  const detecting = detection?.state === "loading";
  const urlOnly = typed && !signedInHere;
  const step = urlOnly ? (repo.url.trim() ? 1 : 0) : signedInHere ? (config.workspace ? 2 : 1) : 0;
  const connect = () => void (gitChosen ? connectRepository("git") : submit("test"));
  return (
    <div className="stack conn-body">
      <MethodTiles label="How to connect to Azure Synapse" options={METHODS} value={source} onChange={selectMethod} disabled={busy} />
      <MiniSteps steps={urlOnly ? ["Repository URL", "Connect"] : ["Sign in", "Choose workspace", "Connect"]} current={step} />

      {!supported && (
        <Banner tone="warning" title="Not available in this backend">The running backend does not support {METHOD_LABEL[config.method]}. Restart it from the latest code.</Banner>
      )}
      {!signedInHere && !typed && connectionBusy !== "authenticate" && (
        <p className="muted" style={{ margin: 0 }}>Sign in with your Microsoft account and choose the workspace. If it is connected to Git, its repository is read: there is nothing to type.</p>
      )}
      {signedInAs}
      {waitingForSignIn}
      {otherDirectories}
      <ScopeSelectors config={config} errors={errors} set={set} signedIn={signedInHere} version={connection.subscriptionCount ?? 0} />

      <ReadFrom detection={detection} readFrom={readFrom} onReadFrom={setReadFrom} typing={typing}
        onTypeInstead={() => setTyping(true)} onUseFound={() => setTyping(false)} onRetry={() => setDetectAgain((n) => n + 1)} />
      {typed && (
        <div className="form-grid">
          <TextField full label="Repository URL" value={repo.url} onChange={(e) => setRepo({ url: e.target.value })} error={repoErrors.url}
            placeholder="https://dev.azure.com/contoso/Data/_git/synapse"
            hint="Cloned by git on this machine with your own sign-in (credential helper or SSH key). No token is entered here." />
        </div>
      )}
      {gitChosen && <MoreOptions kind="git" form={repo} setForm={setRepo} errors={repoErrors} found={typed ? null : found} open={moreOpen} onToggle={setMoreOpen} />}

      {connectionError && (
        <Banner tone="error" title={connectionError.title} actions={<><Button size="small" onClick={() => (signedInHere || typed ? connect() : void submit("authenticate"))} disabled={busy}>Retry</Button><Button size="small" onClick={app.clearConnectionError}>Dismiss</Button></>}>
          {connectionError.hint && <div>{connectionError.hint}</div>}
          <div className="faint" style={{ marginTop: 4 }}>{connectionError.message}</div>
        </Banner>
      )}
      {!connectionError && connection.checks.length > 0 && !connected && <CheckList checks={connection.checks} label="Source connection checks" />}

      <div className="row conn-actions">
        {(signedInHere || typed) && (
          <Button variant="primary" onClick={connect} loading={connectionBusy === "test"} disabled={busy || detecting || (!typed && (!supported || !config.workspace))}>Connect</Button>
        )}
        <Button variant={signedInHere || typed ? "default" : "primary"} onClick={() => void submit("authenticate")} loading={connectionBusy === "authenticate"} disabled={busy || !supported}>
          {signedInHere ? "Sign in again" : "Sign in with Azure"}
        </Button>
        {!signedInHere && !typed && <Button variant="ghost" onClick={() => setTyping(true)}>Enter a repository URL instead</Button>}
        {changing && <Button variant="ghost" onClick={() => setChanging(false)}>Cancel</Button>}
      </div>
    </div>
  );
}
