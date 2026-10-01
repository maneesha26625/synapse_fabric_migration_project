import { Cloud, KeyRound, SquareTerminal, TerminalSquare, type LucideIcon } from "lucide-react";
import { Fragment, useState } from "react";
import { Banner, Button, Card, PageHead, StatusBadge, TextField } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";
import type { FabricAuthMethod, FabricConfig } from "../types";

const GUID = /^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$/;
const METHODS: { id: FabricAuthMethod; title: string; desc: string; icon: LucideIcon }[] = [
  { id: "azure_cli", title: "Azure CLI", desc: "Sign in with the Azure CLI", icon: SquareTerminal },
  { id: "fabric_cli", title: "Fabric CLI", desc: "Sign in with the Fabric CLI", icon: TerminalSquare },
  { id: "service_principal", title: "Service Principal", desc: "Application-based authentication", icon: KeyRound },
];
const EMPTY: FabricConfig = { method: "azure_cli", tenantId: "", workspaceId: "", workspaceName: "", clientId: "", clientSecret: "" };

type Errors = Partial<Record<keyof FabricConfig, string>>;

function validate(c: FabricConfig, action: "authenticate" | "test"): Errors {
  const e: Errors = {};
  const guid = (k: keyof FabricConfig, label: string) => { if (c[k].trim() && !GUID.test(c[k].trim())) e[k] = `${label} must be a GUID.`; };
  guid("tenantId", "Tenant ID"); guid("workspaceId", "Workspace ID"); guid("clientId", "Client ID");
  if (c.method === "service_principal" && action === "authenticate") {
    if (!c.tenantId.trim()) e.tenantId = "Tenant ID is required.";
    if (!c.clientId.trim()) e.clientId = "Client ID is required.";
    if (!c.clientSecret) e.clientSecret = "Client secret is required.";
  }
  if (action === "test" && !c.workspaceId.trim() && !c.workspaceName.trim()) e.workspaceId = "Enter a workspace ID or name.";
  return e;
}

/** The Fabric target. Deliberately separate from the Synapse source and never mixed with its authentication. */
export function FabricTarget() {
  const { mode } = useAppState();
  const { fabric, fabricBusy, fabricError, authenticateFabric, testFabric, disconnectFabric } = useMigration();
  const [config, setConfig] = useState<FabricConfig>(EMPTY);
  const [errors, setErrors] = useState<Errors>({});

  const set = (patch: Partial<FabricConfig>) => {
    setConfig((c) => ({ ...c, ...patch }));
    setErrors((e) => { const n = { ...e }; for (const k of Object.keys(patch) as (keyof FabricConfig)[]) delete n[k]; return n; });
  };
  const submit = async (action: "authenticate" | "test") => {
    const found = validate(config, action);
    setErrors(found);
    if (Object.keys(found).length) return;
    try {
      await (action === "test" ? testFabric(config) : authenticateFabric(config));
    } finally {
      set({ clientSecret: "" }); // the secret never outlives the request
    }
  };

  const busy = fabricBusy !== null;
  const connected = fabric.status === "connected";
  const m = config.method;

  return (
    <div className="page">
      <PageHead icon={Cloud} title="Fabric Target">
        Connect the Microsoft Fabric environment that will receive the migrated Synapse workloads.
      </PageHead>

      {mode === "real" && (
        <Banner tone="info" title="Fabric connection is not implemented in the backend yet">
          This page is ready for it, but Authenticate and Test Connection will report "not implemented" until the backend supports them. Switch to Demo data to preview the flow. Nothing here is ever shown as connected unless the backend says so.
        </Banner>
      )}

      {connected && (
        <Card eyebrow="Microsoft Fabric target" actions={<StatusBadge tone="success">CONNECTED</StatusBadge>}>
          <h2 style={{ fontSize: 22 }}>{fabric.workspaceName}</h2>
          <dl className="kv" style={{ marginTop: 12 }}>
            {([["Target workspace", fabric.workspaceName], ["Connection status", "CONNECTED"], ["Tenant", fabric.tenantId || "—"], ["Workspace ID", fabric.workspaceId || "—"], ["Authentication", METHODS.find((x) => x.id === fabric.method)?.title ?? "—"]] as [string, string | undefined][]).map(([k, v]) => (
              <Fragment key={k}><dt>{k}</dt><dd className={k === "Workspace ID" ? "mono" : undefined}>{v}</dd></Fragment>
            ))}
          </dl>
          <div className="row" style={{ marginTop: 16 }}>
            <Button onClick={() => void testFabric({ ...config, workspaceId: fabric.workspaceId ?? "", workspaceName: fabric.workspaceName ?? "" })} loading={fabricBusy === "test"}>Test Connection</Button>
            <Button variant="ghost" onClick={() => void disconnectFabric()}>Disconnect</Button>
          </div>
        </Card>
      )}

      <Card
        eyebrow="Target connection"
        title="Microsoft Fabric"
        subtitle="Authenticate, then test that the workspace is reachable. These are separate steps."
        actions={<StatusBadge tone={fabric.status === "connected" ? "success" : fabric.status === "authenticated" ? "info" : fabric.status === "failed" ? "error" : "neutral"}>{fabric.status === "connected" ? "CONNECTED" : fabric.status === "authenticated" ? "AUTHENTICATED" : fabric.status === "failed" ? "FAILED" : "NOT CONNECTED"}</StatusBadge>}
      >
        <div className="stack" style={{ gap: 20 }}>
          <div className="auth-cards" role="radiogroup" aria-label="Fabric authentication method">
            {METHODS.map(({ id, title, desc, icon: Icon }) => (
              <button key={id} type="button" role="radio" aria-checked={m === id} className="auth-card" onClick={() => { setErrors({}); setConfig({ ...EMPTY, method: id }); }}>
                <span className="title"><Icon size={17} aria-hidden="true" />{title}</span>
                <span className="desc">{desc}</span>
                <span className="pick">{m === id ? "Selected" : "Select"}</span>
              </button>
            ))}
          </div>

          <div className="form-grid">
            {m === "azure_cli" && (
              <>
                <TextField label="Fabric tenant" value={config.tenantId} onChange={(e) => set({ tenantId: e.target.value })} error={errors.tenantId} placeholder="00000000-0000-0000-0000-000000000000" />
                <TextField label="Workspace ID" value={config.workspaceId} onChange={(e) => set({ workspaceId: e.target.value })} error={errors.workspaceId} />
                <TextField label="Workspace name" value={config.workspaceName} onChange={(e) => set({ workspaceName: e.target.value })} full />
              </>
            )}
            {m === "fabric_cli" && (
              <>
                <TextField label="Fabric workspace" value={config.workspaceName} onChange={(e) => set({ workspaceName: e.target.value })} />
                <TextField label="Workspace ID" value={config.workspaceId} onChange={(e) => set({ workspaceId: e.target.value })} error={errors.workspaceId} />
              </>
            )}
            {m === "service_principal" && (
              <>
                <TextField label="Tenant ID" value={config.tenantId} onChange={(e) => set({ tenantId: e.target.value })} error={errors.tenantId} />
                <TextField label="Client ID" value={config.clientId} onChange={(e) => set({ clientId: e.target.value })} error={errors.clientId} />
                <TextField label="Client Secret" secret value={config.clientSecret} onChange={(e) => set({ clientSecret: e.target.value })} error={errors.clientSecret} hint="Sent once to the backend and cleared from this form. Never stored in the browser." full />
                <TextField label="Workspace ID" value={config.workspaceId} onChange={(e) => set({ workspaceId: e.target.value })} error={errors.workspaceId} />
                <TextField label="Workspace name" optional value={config.workspaceName} onChange={(e) => set({ workspaceName: e.target.value })} />
              </>
            )}
          </div>

          {fabricError && <Banner tone="error" title="Fabric connection">{fabricError}</Banner>}

          <div className="row">
            <Button onClick={() => void submit("authenticate")} loading={fabricBusy === "authenticate"} disabled={busy}>
              {m === "azure_cli" ? "Login with Azure CLI" : m === "fabric_cli" ? "Login with Fabric CLI" : "Authenticate"}
            </Button>
            <Button variant="primary" onClick={() => void submit("test")} loading={fabricBusy === "test"} disabled={busy || fabric.status === "disconnected"}>Test Connection</Button>
            {fabric.status === "disconnected" && <span className="muted">Authenticate first.</span>}
          </div>
        </div>
      </Card>
    </div>
  );
}
