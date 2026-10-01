import { AppWindow, CheckCircle2, CircleSlash, SquareTerminal, XCircle, type LucideIcon } from "lucide-react";
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { useAppState } from "../../state/AppState";
import type { AuthMethod, ConnectionConfig, ConnectionState } from "../../types";
import { Banner, Button, Card, SelectField, StatusBadge, TextField } from "../shared/Shared";

/* ---- Authentication selector ---------------------------------------------------- */

const METHODS: { id: AuthMethod; title: string; desc: string; icon: LucideIcon }[] = [
  { id: "azure_cli", title: "Azure CLI", desc: "Authenticate using Azure CLI", icon: SquareTerminal },
  { id: "interactive_browser", title: "Interactive browser", desc: "Sign in through a Microsoft window; your Azure CLI session is untouched", icon: AppWindow },
];

export function AuthenticationSelector({ value, onChange }: { value: AuthMethod; onChange: (m: AuthMethod) => void }) {
  return (
    <div className="auth-cards" role="radiogroup" aria-label="Authentication method">
      {METHODS.map(({ id, title, desc, icon: Icon }) => (
        <button key={id} type="button" role="radio" aria-checked={value === id} className="auth-card" onClick={() => onChange(id)}>
          <span className="title"><Icon size={17} aria-hidden="true" />{title}</span>
          <span className="desc">{desc}</span>
          <span className="pick">{value === id ? "Selected" : "Select"}</span>
        </button>
      ))}
    </div>
  );
}

/* ---- Form state ------------------------------------------------------------------ */

const GUID = /^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$/;
const EMPTY: ConnectionConfig = {
  method: "azure_cli", tenantId: "", subscriptionId: "", resourceGroup: "", workspace: "",
  workspaceUrl: "", sqlPool: "", clientId: "", resource: "",
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
    // Every method proves an identity first, from the subscription alone; the
    // group, workspace and pool are chosen afterwards.
    if (c.method === "interactive_browser") {
      // The window must open against the operator's own tenant.
      need("tenantId", "Tenant ID");
      guid("clientId", "Client ID");
    }
  } else {
    need("resourceGroup", "Resource group");
    need("workspace", "Synapse workspace");
  }
  return e;
}

/* ---- Forms ------------------------------------------------------------------------ */

interface FormProps {
  config: ConnectionConfig;
  errors: Errors;
  set: (patch: Partial<ConnectionConfig>) => void;
  /** Authenticated as this method, for the subscription shown. */
  signedIn: boolean;
  authenticating: boolean;
}

/**
 * Resource group -> workspace (for that group) -> optional SQL pool (for that
 * workspace). Every dropdown is filled from the authenticated identity, so
 * nothing is typed that could be misspelt, and the lists only offer what that
 * identity can actually see.
 */
function ScopeSelectors({ config, errors, set, signedIn }: FormProps) {
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

  useEffect(() => {
    if (!signedIn) { setGroups([]); return; }
    void load("groups", () => azureLists.listResourceGroups(), setGroups);
  }, [signedIn]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setWorkspaces([]); setPools([]);
    if (!signedIn || !config.resourceGroup) return;
    void load("workspaces", () => azureLists.listWorkspaces(config.resourceGroup), setWorkspaces);
  }, [signedIn, config.resourceGroup]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setPools([]);
    if (!signedIn || !config.resourceGroup || !config.workspace) return;
    void load("pools", () => azureLists.listSqlPools(config.resourceGroup, config.workspace), setPools);
  }, [signedIn, config.resourceGroup, config.workspace]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!signedIn) return null;
  return (
    <>
      <Banner tone="success">Authenticated. Now choose the resource group and workspace.</Banner>
      <div className="form-grid">
        <SelectField label="Resource group" value={config.resourceGroup} onChange={(v) => set({ resourceGroup: v, workspace: "", sqlPool: "" })} options={groups} loading={loading === "groups"} placeholder="Select a resource group" error={errors.resourceGroup} />
        <SelectField label="Synapse workspace" value={config.workspace} onChange={(v) => set({ workspace: v, sqlPool: "" })} options={workspaces} loading={loading === "workspaces"} disabled={!config.resourceGroup} placeholder={config.resourceGroup ? (workspaces.length ? "Select a workspace" : "No Synapse workspaces in this group") : "Select a resource group first"} error={errors.workspace} />
        <SelectField label="Dedicated SQL pool" optional value={config.sqlPool} onChange={(v) => set({ sqlPool: v })} options={pools} loading={loading === "pools"} disabled={!config.workspace} placeholder={config.workspace ? "None (skip SQL tables, views, procedures)" : "Select a workspace first"} hint="Needed to discover tables, views and stored procedures." />
      </div>
      {listError && <Banner tone="error" title="Could not load the list">{listError}</Banner>}
    </>
  );
}

function SubscriptionField({ config, set, errors }: FormProps) {
  return (
    <TextField label="Subscription ID" value={config.subscriptionId} onChange={(e) => set({ subscriptionId: e.target.value })} error={errors.subscriptionId} placeholder="00000000-0000-0000-0000-000000000000" />
  );
}

function AzureCliForm(p: FormProps) {
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="form-grid">
        <SubscriptionField {...p} />
        <TextField label="Tenant ID" optional value={p.config.tenantId} onChange={(e) => p.set({ tenantId: e.target.value })} error={p.errors.tenantId} hint="Found automatically from the subscription. Enter it only if sign-in picks the wrong tenant." />
      </div>
      {p.authenticating && <Banner tone="info" title="Waiting for you to sign in">A sign-in window has opened in your browser. Complete it there; this page continues automatically.</Banner>}
      <ScopeSelectors {...p} />
    </div>
  );
}

/** Shown while a browser sign-in is pending, so the operator is not left watching a spinner. */
const WAITING_FOR_WINDOW = "A sign-in window should open on this machine — complete it there. Waiting…";

function InteractiveBrowserForm(p: FormProps) {
  const { health } = useAppState();
  const note = health?.capabilities.authMethodDetails?.find((d) => d.id === "interactive_browser");
  const takesClientId = note?.takesClientId ?? true;
  return (
    <div className="stack" style={{ gap: 16 }}>
      {note?.caveat && <p className="muted">{note.caveat}</p>}
      <div className="form-grid">
        <TextField label="Tenant ID" value={p.config.tenantId} onChange={(e) => p.set({ tenantId: e.target.value })} error={p.errors.tenantId} placeholder="00000000-0000-0000-0000-000000000000" hint="The tenant the sign-in window opens against." />
        <SubscriptionField {...p} />
        {takesClientId && (
          <TextField id="client-id" label="Client ID" optional value={p.config.clientId} onChange={(e) => p.set({ clientId: e.target.value })} error={p.errors.clientId} placeholder="Application (client) ID" hint="Only if the tenant has not consented to Microsoft's default sign-in app. An identifier, not a secret." />
        )}
      </div>
      {p.authenticating && <Banner tone="info" title="Waiting for you to sign in">{WAITING_FOR_WINDOW}</Banner>}
      <ScopeSelectors {...p} />
    </div>
  );
}

/* ---- Connection status --------------------------------------------------------------- */

const METHOD_LABEL: Record<AuthMethod, string> = { azure_cli: "Azure CLI", interactive_browser: "Interactive browser" };

export function ConnectionStatus({ connection, busy, onTest, onChange, onDisconnect }: { connection: ConnectionState; busy: boolean; onTest: () => void; onChange: () => void; onDisconnect: () => void }) {
  const rows: [string, ReactNode][] = [
    ["Source", connection.sourcePlatform ?? "Azure Synapse"],
    ["Workspace", connection.workspace],
    ["Resource group", connection.resourceGroup],
    ["Subscription", connection.subscriptionName ? `${connection.subscriptionName} (${connection.subscriptionId})` : connection.subscriptionId],
    ["Tenant", connection.tenantId || "—"],
    ["Authentication", connection.method ? METHOD_LABEL[connection.method] : "—"],
    ["Dedicated SQL pool", connection.sqlPool || "Not configured"],
    ["Last tested", connection.testedAt ? new Date(connection.testedAt).toLocaleString() : "—"],
  ];
  return (
    <Card eyebrow="Source connection" actions={<StatusBadge tone="success">Connected</StatusBadge>}>
      <dl className="kv">
        {rows.map(([k, v]) => (<Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>))}
      </dl>
      <CheckList checks={connection.checks} />
      <div className="row" style={{ marginTop: 16 }}>
        <Button onClick={onTest} loading={busy}>Test Connection</Button>
        <Button onClick={onChange}>Change Authentication</Button>
        <Button variant="ghost" onClick={onDisconnect} disabled={busy}>Disconnect</Button>
      </div>
    </Card>
  );
}

function CheckList({ checks }: { checks: ConnectionState["checks"] }) {
  if (!checks.length) return null;
  return (
    <ul className="checks" style={{ listStyle: "none", padding: 0, margin: "16px 0 0" }} aria-label="Connection checks">
      {checks.map((c) => (
        <li key={c.name} className={`check ${c.status}`}>
          {c.status === "ok" ? <CheckCircle2 size={15} aria-label="passed" /> : c.status === "failed" ? <XCircle size={15} aria-label="failed" /> : <CircleSlash size={15} aria-label="skipped" />}
          <span><strong>{c.name}</strong> <span className="muted">— {c.message}</span></span>
        </li>
      ))}
    </ul>
  );
}

/* ---- Composite ------------------------------------------------------------------------- */

const HELP: Record<AuthMethod, string> = {
  azure_cli: "Enter the subscription ID and sign in. A browser window opens asking you to authenticate with your Azure account.",
  interactive_browser: "Enter the tenant and subscription IDs, then authenticate. A Microsoft sign-in window opens on the machine running the server; choose your account there.",
};

/**
 * ``hideStatus`` leaves the connected summary to the page, and ``forceForm``
 * shows the form even while connected (the page's "Add Synapse Workspace").
 */
export function SourceConnection({ hideStatus = false, forceForm = false }: { hideStatus?: boolean; forceForm?: boolean } = {}) {
  const app = useAppState();
  const { connection, connectionBusy, connectionError, health, mode } = app;
  const [config, setConfig] = useState<ConnectionConfig>(() => ({ ...EMPTY, ...(remembered ?? {}) }));
  const [errors, setErrors] = useState<Errors>({});
  const [changing, setChanging] = useState(false);

  const connected = app.isConnected;
  const showForm = !connected || changing || forceForm;
  const supported = mode === "mock" || !health || health.capabilities.authMethods.includes(config.method);
  const busy = connectionBusy !== null;
  const azure = config.method === "azure_cli";

  // The backend may have been restarted since this page loaded; ask again
  // whenever the method changes so a stale answer never blocks a working option.
  useEffect(() => { void app.refreshHealth(); }, [config.method]); // eslint-disable-line react-hooks/exhaustive-deps

  // Authenticated *as this method, for the subscription shown*: changing either
  // invalidates the dropdowns until the operator authenticates again.
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
      set({ subscriptionId: connection.subscriptionId, method: connection.method ?? "azure_cli" });
    }
  }, [connection.signedIn, connection.subscriptionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectMethod = (method: AuthMethod) => {
    setErrors({});
    app.clearConnectionError();
    setConfig((c) => ({ ...c, method, clientId: "", resourceGroup: "", workspace: "", sqlPool: "" }));
  };

  const submit = async (action: "authenticate" | "test") => {
    const found = validate(config, action);
    setErrors(found);
    if (Object.keys(found).length) return;
    remembered = config;
    await (action === "test" ? app.testConnection(config) : app.authenticate(config));
    if (action === "test") setChanging(false);
  };

  const retest = () => app.testConnection(configFromConnection(connection));

  const formProps: FormProps = { config, errors, set, signedIn: signedInHere, authenticating: connectionBusy === "authenticate" };
  const Form = azure ? AzureCliForm : InteractiveBrowserForm;

  return (
    <div className="stack" style={{ gap: 16 }}>
      {connected && !hideStatus && (
        <ConnectionStatus connection={connection} busy={connectionBusy === "test"} onTest={retest} onChange={() => setChanging(true)} onDisconnect={() => { setChanging(false); void app.disconnect(); }} />
      )}
      {showForm && (
        <Card
          eyebrow="Source connection"
          title="Azure Synapse"
          subtitle="Connect the migration accelerator to the Azure Synapse source environment."
          actions={<StatusBadge tone={connection.status === "connected" ? "success" : connection.checks.length ? "warning" : "neutral"}>{connection.status === "connected" ? "Connected" : connection.checks.length ? "Not verified" : "Not connected"}</StatusBadge>}
        >
          <div className="stack" style={{ gap: 20 }}>
            <AuthenticationSelector value={config.method} onChange={selectMethod} />

            <div>
              <div className="eyebrow" style={{ marginBottom: 6 }}>Authentication method</div>
              <strong>{METHOD_LABEL[config.method]}</strong>
              <p className="muted" style={{ marginTop: 4 }}>{HELP[config.method]} After that, pick the resource group, workspace and SQL pool from lists. No access token is ever shown or stored in the browser.</p>
            </div>

            {!supported && (
              <Banner tone="warning" title="Not available in this backend">
                The running backend does not support {METHOD_LABEL[config.method]}. Restart it from the latest code.
              </Banner>
            )}

            <Form {...formProps} />

            {connectionError && (
              <Banner tone="error" title={connectionError.title} actions={<><Button size="small" onClick={() => void submit(signedInHere ? "test" : "authenticate")} disabled={busy}>Retry</Button><Button size="small" onClick={app.clearConnectionError}>Dismiss</Button></>}>
                {connectionError.hint && <div>{connectionError.hint}</div>}
                <div className="faint" style={{ marginTop: 4 }}>{connectionError.message}</div>
              </Banner>
            )}

            {!connectionError && connection.checks.length > 0 && !connected && <CheckList checks={connection.checks} />}

            <div className="row">
              <Button onClick={() => void submit("authenticate")} loading={connectionBusy === "authenticate"} disabled={busy || !supported}>
                {azure ? (signedInHere ? "Sign in again" : "Sign in with Azure") : signedInHere ? "Authenticate again" : "Authenticate"}
              </Button>
              <Button variant="primary" onClick={() => void submit("test")} loading={connectionBusy === "test"} disabled={busy || !supported || !(signedInHere && config.workspace)}>Test Connection</Button>
              {changing && <Button variant="ghost" onClick={() => setChanging(false)}>Cancel</Button>}
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
