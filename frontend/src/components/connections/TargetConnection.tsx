import { SquareTerminal, TerminalSquare } from "lucide-react";
import { useEffect, useState } from "react";
import { useMigration } from "../../state/MigrationState";
import type { FabricAuthMethod } from "../../types";
import { Banner, Button, SelectField } from "../shared/Shared";
import { MethodTiles, MiniSteps, type MethodOption } from "./MethodTiles";
import { CheckFold, CheckList, SummaryList } from "./SourceConnection";

const METHODS: MethodOption<FabricAuthMethod>[] = [
  { id: "azure_cli", title: "Azure CLI", desc: "az login opens a browser window to choose the account", icon: SquareTerminal },
  { id: "fabric_cli", title: "Fabric CLI", desc: "fab auth login opens a browser window to choose the account", icon: TerminalSquare },
];
const LOGIN: Record<FabricAuthMethod, string> = { azure_cli: "Sign in with Azure CLI", fabric_cli: "Sign in with Fabric CLI" };
const LABEL: Record<FabricAuthMethod, string> = { azure_cli: "Azure CLI", fabric_cli: "Fabric CLI" };

/** Connect Microsoft Fabric: sign in with a CLI, choose the workspace, test it (including its capacity). */
export function TargetConnectionPanel() {
  const { fabric, fabricBusy, fabricError, authenticateFabric, testFabric, disconnectFabric } = useMigration();
  const [method, setMethod] = useState<FabricAuthMethod>(fabric.method ?? "azure_cli");
  const [workspaceId, setWorkspaceId] = useState("");
  const [changing, setChanging] = useState(false);

  // Follow the backend's method when it already holds a session (after a reload, or in Demo data).
  useEffect(() => { if (fabric.method) setMethod(fabric.method); }, [fabric.method]);

  const mine = fabric.method === method;
  const authed = mine && (fabric.status === "authenticated" || fabric.status === "connected" || (fabric.status === "failed" && !!fabric.account));
  const connected = mine && fabric.status === "connected";
  const workspaces = authed ? fabric.workspaces ?? [] : [];
  const selected = workspaces.find((w) => w.id === workspaceId) ?? workspaces.find((w) => w.id === fabric.workspaceId);
  const signingIn = mine && fabric.status === "signing_in";
  const busy = fabricBusy !== null || signingIn;
  const checks = (fabric.checks ?? []).map((c) => ({ name: c.label, status: c.ok ? "ok" as const : "failed" as const, message: c.ok ? null : c.detail ?? null }));

  const pick = (id: FabricAuthMethod) => {
    if (id === method) return;
    setMethod(id);
    setWorkspaceId("");
    if (fabric.status !== "disconnected") void disconnectFabric();
  };
  const test = async () => {
    if (!selected) return;
    await testFabric({ method, workspaceId: selected.id, workspaceName: selected.name });
    setChanging(false);
  };

  if (connected && !changing) {
    return (
      <div className="stack conn-body">
        <SummaryList rows={[
          ["Workspace", <strong key="w">{fabric.workspaceName}</strong>],
          ["Capacity", fabric.capacityAssigned === false ? <span className="tone-error">Not assigned</span> : fabric.capacityAssigned ? "Assigned" : "Not checked"],
          ["Account", fabric.account],
          ["Signed in with", LABEL[method]],
          ["Tenant", <span key="t" className="mono">{fabric.tenantId || "—"}</span>],
        ]} />
        <CheckFold checks={checks} label="Fabric connection checks" />
        {fabric.capacityAssigned === false && (
          <Banner tone="error" title="No Fabric capacity">Fabric creates nothing in a workspace without one. Assign a Fabric or Trial capacity under Workspace settings, License info, then test again.</Banner>
        )}
        <div className="row conn-actions">
          <Button size="small" onClick={() => void test()} loading={fabricBusy === "test"} disabled={!selected}>Test again</Button>
          <Button size="small" onClick={() => setChanging(true)}>Change workspace</Button>
          <Button size="small" variant="ghost" onClick={() => { setWorkspaceId(""); void disconnectFabric(); }}>Disconnect</Button>
        </div>
      </div>
    );
  }

  const stage = authed ? (selected ? 2 : 1) : 0;
  return (
    <div className="stack conn-body">
      <MethodTiles label="How to connect to Microsoft Fabric" options={METHODS} value={method} onChange={pick} disabled={busy} />
      <MiniSteps steps={["Sign in", "Choose workspace", "Test"]} current={stage} />

      {authed ? (
        <p className="muted conn-identity">Signed in as <strong>{fabric.account || "your account"}</strong>{fabric.tenantId ? <> · tenant <span className="mono">{fabric.tenantId}</span></> : null}</p>
      ) : (
        <p className="muted">{method === "azure_cli" ? "A browser window opens so you can choose the Azure account." : "The Fabric CLI opens a browser window so you can choose the account."} No token reaches this page.</p>
      )}

      <div className="row conn-actions">
        <Button variant={authed ? "default" : "primary"} onClick={() => void authenticateFabric({ method, workspaceId: "", workspaceName: "" })} loading={fabricBusy === "authenticate" || signingIn} disabled={busy}>
          {signingIn ? "Waiting for sign-in…" : authed ? "Sign in again" : LOGIN[method]}
        </Button>
        {signingIn && <Button variant="ghost" onClick={() => void disconnectFabric()}>Cancel</Button>}
      </div>

      {signingIn && <Banner tone="info" title="Finish signing in">Choose your account in the browser window that opened{method === "fabric_cli" ? " (the Fabric CLI also opens its own console window)" : ""}. This page updates on its own.</Banner>}

      <SelectField
        label="Fabric workspace"
        value={selected?.name ?? ""}
        onChange={(name) => setWorkspaceId(workspaces.find((w) => w.name === name)?.id ?? "")}
        options={workspaces.map((w) => w.name)}
        placeholder={authed ? (workspaces.length ? "Select a workspace" : "No workspaces available") : "Sign in to load workspaces"}
        disabled={!authed || busy || !workspaces.length}
      />

      {fabricError && <Banner tone="error" title="Fabric connection">{fabricError}</Banner>}
      {mine && fabric.status === "failed" && checks.length > 0 && <CheckList checks={checks} label="Fabric connection checks" />}

      <div className="row conn-actions">
        <Button variant={authed ? "primary" : "default"} onClick={() => void test()} loading={fabricBusy === "test"} disabled={busy || !authed || !selected}>Test connection</Button>
        {changing && <Button variant="ghost" onClick={() => setChanging(false)}>Cancel</Button>}
      </div>
    </div>
  );
}
