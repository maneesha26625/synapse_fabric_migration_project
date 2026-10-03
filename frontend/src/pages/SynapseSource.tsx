import { Database, Plus, Radar } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ConnectionStatus, SourceConnection } from "../components/connections/SourceConnection";
import { StageTracker, type StageState } from "../components/shared/Metrics";
import { Banner, Button, Card, EmptyState, PageHead, StatusBadge } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";

export function SynapseSource() {
  const { isConnected, connection, connectionBusy, connectionError, backendError, discovery, startDiscovery, testConnection, disconnect } = useAppState();
  const navigate = useNavigate();
  const [adding, setAdding] = useState(false);

  const registered = isConnected || !!connection.signedIn;
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const failed = !!connectionError;

  // Three different things, kept apart: who you are, whether the workspace
  // answers, and whether discovery can start.
  const auth: StageState = connection.signedIn || isConnected ? "done" : failed && !connection.signedIn ? "failed" : "active";
  const test: StageState = isConnected ? "done" : failed && connection.signedIn ? "failed" : connection.signedIn ? "active" : "pending";
  const disc: StageState = discovered ? "done" : discovery.state === "running" ? "active" : isConnected ? "active" : "pending";

  const badge = failed ? <StatusBadge tone="error">FAILED</StatusBadge>
    : discovered ? <StatusBadge tone="success">DISCOVERY COMPLETE</StatusBadge>
    : isConnected ? <StatusBadge tone="success">DISCOVERY READY</StatusBadge>
    : connection.signedIn ? <StatusBadge tone="info">AUTHENTICATED</StatusBadge>
    : <StatusBadge tone="neutral">NOT CONNECTED</StatusBadge>;

  const discover = async () => {
    if (discovery.state !== "running") await startDiscovery();
    navigate("/discovery");
  };

  return (
    <div className="page">
      <PageHead icon={Database} title="Synapse Source">
        Connect to the Azure Synapse environment that will be analyzed and migrated to Microsoft Fabric.
      </PageHead>

      {backendError && <Banner tone="error" title="Backend unavailable">{backendError}</Banner>}

      <div className="row">
        <h2>Registered Synapse Workspaces ({isConnected ? 1 : 0})</h2>
        <span className="spacer" />
        <Button variant="primary" onClick={() => setAdding((a) => !a)} disabled={isConnected && adding}><Plus size={14} aria-hidden="true" />Add Synapse Workspace</Button>
      </div>

      {isConnected && (
        <ConnectionStatus
          connection={connection}
          busy={connectionBusy === "test"}
          onTest={() => void testConnection({ method: connection.method ?? "azure_cli", tenantId: connection.tenantId ?? "", subscriptionId: connection.subscriptionId ?? "", resourceGroup: connection.resourceGroup ?? "", workspace: connection.workspace ?? "", workspaceUrl: "", sqlPool: connection.sqlPool ?? "", resource: "" })}
          onChange={() => setAdding(true)}
          onDisconnect={() => { setAdding(false); void disconnect(); }}
        />
      )}

      {!registered && !adding && (
        <Card>
          <EmptyState icon={<Database size={22} />} title="No Synapse workspace connected." actions={<Button variant="primary" onClick={() => setAdding(true)}><Plus size={14} aria-hidden="true" />Add Synapse Workspace</Button>}>
            Add a workspace to authenticate, test the connection and start discovery.
          </EmptyState>
        </Card>
      )}

      {(adding || (registered && !isConnected)) && <SourceConnection hideStatus forceForm />}

      {registered && (
        <Card eyebrow="Connection status" actions={badge}>
          <StageTracker
            stages={[
              { label: "Authentication", state: auth, detail: connection.signedIn || isConnected ? "Identity proven" : "Prove an Azure identity" },
              { label: "Connection test", state: test, detail: isConnected ? "Workspace and artifacts readable" : "Check the workspace is readable" },
              { label: "Discovery", state: disc, detail: discovered ? "Completed" : discovery.state === "running" ? "Running" : isConnected ? "Ready to run" : "Needs a passed connection test" },
            ]}
          />
          <div className="row" style={{ marginTop: 16 }}>
            <Button variant="primary" disabled={!isConnected || discovery.state === "running"} onClick={() => void discover()}>
              <Radar size={14} aria-hidden="true" />Discover Workspace
            </Button>
            {!isConnected && <span className="muted">Pass the connection test first.</span>}
          </div>
        </Card>
      )}
    </div>
  );
}
