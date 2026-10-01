import { CheckCircle2, Cloud, SquareTerminal, TerminalSquare, XCircle, type LucideIcon } from "lucide-react";
import { Fragment, useState } from "react";
import { Banner, Button, Card, PageHead, SelectField, StatusBadge } from "../components/shared/Shared";
import { useMigration } from "../state/MigrationState";
import type { FabricAuthMethod } from "../types";

const METHODS: { id: FabricAuthMethod; title: string; desc: string; icon: LucideIcon; login: string; heading: string; blurb: string }[] = [
  { id: "azure_cli", title: "Azure CLI", desc: "Sign in with the Azure CLI", icon: SquareTerminal, login: "Login with Azure CLI", heading: "Azure CLI Authentication", blurb: "Authenticate to Microsoft Azure using the Azure CLI. `az login` opens a browser window so you can choose the account." },
  { id: "fabric_cli", title: "Fabric CLI", desc: "Sign in with the Fabric CLI", icon: TerminalSquare, login: "Login with Fabric CLI", heading: "Fabric CLI Authentication", blurb: "Authenticate using Microsoft Fabric CLI. `fab auth login` opens a browser window so you can choose the account." },
];

/** The Fabric target. Separate from the Synapse source. The backend holds the session; no token ever reaches this page. */
export function FabricTarget() {
  const { fabric, fabricBusy, fabricError, authenticateFabric, testFabric, disconnectFabric } = useMigration();
  const [method, setMethod] = useState<FabricAuthMethod>("azure_cli");
  const [workspaceId, setWorkspaceId] = useState("");

  const m = METHODS.find((x) => x.id === method)!;
  const mine = fabric.method === method;
  const authed = mine && (fabric.status === "authenticated" || fabric.status === "connected" || (fabric.status === "failed" && !!fabric.account));
  const connected = mine && fabric.status === "connected";
  const workspaces = authed ? fabric.workspaces ?? [] : [];
  const selected = workspaces.find((w) => w.id === workspaceId) ?? workspaces.find((w) => w.id === fabric.workspaceId && connected);
  const signingIn = mine && fabric.status === "signing_in";
  const busy = fabricBusy !== null || signingIn;

  const pick = (id: FabricAuthMethod) => {
    if (id === method) return;
    setMethod(id);
    setWorkspaceId("");
    if (fabric.status !== "disconnected") void disconnectFabric();
  };
  const test = () => selected && void testFabric({ method, workspaceId: selected.id, workspaceName: selected.name });

  const badge = connected ? <StatusBadge tone="success">CONNECTED</StatusBadge>
    : authed ? <StatusBadge tone="info">AUTHENTICATED</StatusBadge>
    : mine && fabric.status === "failed" ? <StatusBadge tone="error">FAILED</StatusBadge>
    : <StatusBadge tone="neutral">NOT CONNECTED</StatusBadge>;

  return (
    <div className="page">
      <PageHead icon={Cloud} title="Fabric Target">
        Connect the Microsoft Fabric environment that will receive the migrated Synapse workloads.
      </PageHead>

      <Card eyebrow="Target: Microsoft Fabric" title="Authentication Method" actions={badge}>
        <div className="stack" style={{ gap: 20 }}>
          <div className="auth-cards" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }} role="radiogroup" aria-label="Fabric authentication method">
            {METHODS.map(({ id, title, desc, icon: Icon }) => (
              <button key={id} type="button" role="radio" aria-checked={method === id} className="auth-card" onClick={() => pick(id)}>
                <span className="title"><Icon size={17} aria-hidden="true" />{title}</span>
                <span className="desc">{desc}</span>
                <span className="pick">{method === id ? "Selected" : "Select"}</span>
              </button>
            ))}
          </div>

          <div className="stack" style={{ gap: 14 }}>
            <div>
              <h3>{m.heading}</h3>
              <p className="muted">{m.blurb}</p>
            </div>

            <div className="row">
              <Button onClick={() => void authenticateFabric({ method, workspaceId: "", workspaceName: "" })} loading={fabricBusy === "authenticate" || signingIn} disabled={busy}>{signingIn ? "Waiting for sign-in…" : m.login}</Button>
              {signingIn && <Button variant="ghost" onClick={() => void disconnectFabric()}>Cancel</Button>}
            </div>

            <dl className="kv">
              <dt>Authentication Status</dt>
              <dd>{authed ? <span style={{ color: "var(--success)" }}>● Authenticated</span> : <span className="muted">● Not authenticated</span>}</dd>
              {method === "azure_cli" && <><dt>Account</dt><dd>{authed ? fabric.account || "—" : "-"}</dd></>}
              <dt>Tenant</dt><dd className={authed ? "mono" : undefined}>{authed ? fabric.tenantId || "—" : "-"}</dd>
            </dl>

            <SelectField
              label="Fabric Workspace"
              value={selected?.name ?? ""}
              onChange={(name) => setWorkspaceId(workspaces.find((w) => w.name === name)?.id ?? "")}
              options={workspaces.map((w) => w.name)}
              placeholder={authed ? (workspaces.length ? "Select Workspace" : "No workspaces available") : "Log in to load workspaces"}
              disabled={!authed || busy || !workspaces.length}
            />

            {signingIn && <Banner tone="info" title="Finish signing in">Choose your account in the browser window that opened{method === "fabric_cli" ? " (the Fabric CLI also opens its own console window)" : ""}. This page updates automatically.</Banner>}
            {fabricError && <Banner tone="error" title="Fabric connection">{fabricError}</Banner>}

            <div className="row">
              <Button variant="primary" onClick={test} loading={fabricBusy === "test"} disabled={busy || !authed || !selected}>Test Connection</Button>
              {connected && <Button variant="ghost" onClick={() => { setWorkspaceId(""); void disconnectFabric(); }}>Disconnect</Button>}
            </div>
          </div>
        </div>
      </Card>

      {mine && (fabric.checks?.length ?? 0) > 0 && (
        <Card eyebrow="Connection test" actions={connected ? <StatusBadge tone="success">PASSED</StatusBadge> : <StatusBadge tone="error">FAILED</StatusBadge>}>
          <ul className="checks" style={{ listStyle: "none", padding: 0, margin: 0 }} aria-label="Fabric connection checks">
            {fabric.checks!.map((c) => (
              <li key={c.label} className={`check ${c.ok ? "ok" : "failed"}`}>
                {c.ok ? <CheckCircle2 size={15} aria-label="passed" /> : <XCircle size={15} aria-label="failed" />}
                <span><strong>{c.label}</strong>{!c.ok && c.detail ? <span className="muted"> — {c.detail}</span> : null}</span>
              </li>
            ))}
          </ul>
          {connected && (
            <dl className="kv" style={{ marginTop: 14 }}>
              {([["Workspace", fabric.workspaceName], ["Workspace ID", fabric.workspaceId], ["Tenant", fabric.tenantId], ["Authentication", m.title]] as [string, string | null | undefined][]).map(([k, v]) => (
                <Fragment key={k}><dt>{k}</dt><dd className={k === "Workspace ID" || k === "Tenant" ? "mono" : undefined}>{v || "—"}</dd></Fragment>
              ))}
            </dl>
          )}
        </Card>
      )}
    </div>
  );
}
