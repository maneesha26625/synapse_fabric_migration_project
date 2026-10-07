import { AppWindow, CheckCircle2, CircleSlash, FileArchive, GitBranch, SquareTerminal, XCircle } from "lucide-react";
import { joinAnd } from "../shared/text";
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import type { AuthMethod, ConnectionConfig, ConnectionState } from "../../types";
import { Banner, Button, SelectField, TextField, ConfirmDialog } from "../shared/Shared";
import { MethodTiles, MiniSteps, type MethodOption } from "./MethodTiles";

/* ---- Methods ------------------------------------------------------------------------ */

type SourceMethod = AuthMethod | "zip" | "git";

const METHODS: MethodOption<SourceMethod>[] = [
  { id: "azure_cli", title: "Azure CLI", desc: "Sign in with your Azure account in a browser window", icon: SquareTerminal },
  { id: "interactive_browser", title: "Interactive browser", desc: "A Microsoft sign-in for the tenant you name; your Azure CLI session is untouched", icon: AppWindow },
  { id: "zip", title: "Workspace export (ZIP)", desc: "Upload an exported Synapse workspace", icon: FileArchive, soon: true },
  { id: "git", title: "Git repository", desc: "Read the workspace from its Git repository", icon: GitBranch, soon: true },
];
const METHOD_LABEL: Record<AuthMethod, string> = { azure_cli: "Azure CLI", interactive_browser: "Interactive browser" };

/* ---- Form state ------------------------------------------------------------------ */

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
    method: c.method ?? "azure_cli",
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


/* ---- The source panel ---------------------------------------------------------------- */

/** Connect Azure Synapse: choose how to sign in, sign in, choose the workspace, test. */
export function SourceConnectionPanel() {
  const app = useAppState();
  const { connection, connectionBusy, connectionError, health, mode } = app;
  const [config, setConfig] = useState<ConnectionConfig>(() => ({ ...EMPTY, ...(remembered ?? {}) }));
  const [errors, setErrors] = useState<Errors>({});
  const [changing, setChanging] = useState(false);
  const [confirmSignOut, setConfirmSignOut] = useState(false);
  const { plan, execution, validation, confirmed } = useMigration();
  const discovered = app.discovery.state === "completed" || app.discovery.state === "completed_with_warnings";
  const signOut = () => { setChanging(false); setConfirmSignOut(false); void app.disconnect(); };
  // What a new source would clear: everything here was built on this one.
  const loses = [
    discovered ? `the discovered inventory (${(app.discovery.summary?.total ?? 0).toLocaleString()} objects)` : "",
    plan.length ? `the plan (${plan.length.toLocaleString()} objects)` : "",
    execution.state !== "idle" ? `the record of migration run #${execution.runId}` : "",
    validation ? "the validation results" : "",
  ].filter(Boolean);
  const askFirst = loses.length > 0 || confirmed.length > 0;
  const runWorking = execution.state === "running";

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
    connection.method === config.method &&
    !!connection.subscriptionId &&
    connection.subscriptionId.toLowerCase() === config.subscriptionId.trim().toLowerCase();

  const set = (patch: Partial<ConnectionConfig>) => {
    setConfig((c) => ({ ...c, ...patch }));
    setErrors((e) => { const next = { ...e }; for (const k of Object.keys(patch) as (keyof ConnectionConfig)[]) delete next[k]; return next; });
  };

  // After a reload the backend may still hold a sign-in; pick it back up.
  useEffect(() => {
    if (connection.signedIn && connection.subscriptionId && !config.subscriptionId) {
      set({ subscriptionId: connection.subscriptionId, method: connection.method ?? "azure_cli", tenantId: connection.tenantId ?? "" });
    }
  }, [connection.signedIn, connection.subscriptionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectMethod = (method: SourceMethod) => {
    if (method === "zip" || method === "git") return;
    setErrors({});
    app.clearConnectionError();
    setConfig((c) => ({ ...c, method, resourceGroup: "", workspace: "", sqlPool: "" }));
  };

  const submit = async (action: "authenticate" | "test") => {
    const found = validate(config, action);
    setErrors(found);
    if (Object.keys(found).length) return;
    remembered = config;
    await (action === "test" ? app.testConnection(config) : app.authenticate(config));
    if (action === "test") setChanging(false);
  };

  if (connected && !changing) {
    return (
      <div className="stack conn-body">
        <SummaryList rows={[
          ["Workspace", <strong key="w">{connection.workspace}</strong>],
          ["Resource group", connection.resourceGroup],
          ["Subscription", connection.subscriptionName ?? connection.subscriptionId],
          ["Dedicated SQL pool", connection.sqlPool || "Not configured"],
          ["Signed in with", connection.method ? METHOD_LABEL[connection.method] : "—"],
          ["Last tested", connection.testedAt ? new Date(connection.testedAt).toLocaleString() : "—"],
        ]} />
        <CheckFold checks={connection.checks} label="Source connection checks" />
        <div className="row conn-actions">
          <Button size="small" onClick={() => void app.testConnection(configFromConnection(connection))} loading={connectionBusy === "test"}>Test again</Button>
          <Button size="small" onClick={() => { setConfig(configFromConnection(connection)); setChanging(true); }}>Change</Button>
          <Button size="small" variant="ghost" onClick={() => (askFirst ? setConfirmSignOut(true) : signOut())} disabled={busy || app.discovery.state === "running" || runWorking}
            title={app.discovery.state === "running" ? "Discovery is running; wait for it to finish" : runWorking ? "A migration run is working: pause it first" : undefined}>Disconnect</Button>
        </div>
        {confirmSignOut && (
          <ConfirmDialog title="Disconnect Azure Synapse?" confirmLabel="Disconnect" onConfirm={signOut} onCancel={() => setConfirmSignOut(false)}>
            <p>The accelerator signs out of {connection.workspace}. Your Azure CLI session is untouched.</p>
            <p>This starts the migration over: {loses.length ? `${joinAnd(loses)} ${loses.length === 1 ? "is" : "are"} cleared, and ` : ""}every step is done again after you reconnect.</p>
            <p className="muted">Nothing in Synapse changes, and anything already created in Fabric stays there.</p>
          </ConfirmDialog>
        )}
      </div>
    );
  }

  const stage = signedInHere ? (config.workspace ? 2 : 1) : 0;
  return (
    <div className="stack conn-body">
      {changing && askFirst && (
        <Banner tone="info" title="A different workspace starts the migration over">
          Testing another workspace or SQL pool clears what was built on this one ({joinAnd(loses.length ? loses : ["the confirmed steps"])}). Testing the same one again changes nothing.
        </Banner>
      )}
      <MethodTiles label="How to connect to Azure Synapse" options={METHODS} value={config.method} onChange={selectMethod} disabled={busy} />
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
