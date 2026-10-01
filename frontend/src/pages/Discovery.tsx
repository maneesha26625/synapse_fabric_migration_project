import { Check, Circle, Loader2, Play, PlugZap, RefreshCw, Radar, SearchX, Settings2 } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { DiscoveryNotes } from "../components/discovery/DiscoverySummary";
import { Inventory } from "../components/discovery/Inventory";
import { MetricCard } from "../components/shared/Metrics";
import { Banner, Button, Card, EmptyState, ErrorState, LoadingState, PageHead, StatusBadge } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import type { DiscoveryStatus } from "../types";

const fmt = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "Never");

const PLURAL: Record<string, string> = {
  "Dedicated SQL Pool": "SQL Pools", Table: "Tables", "External Table": "External Tables", View: "Views",
  "Stored Procedure": "Procedures", "SQL Script": "SQL Scripts", Schema: "Schemas", Pipeline: "Pipelines",
  Trigger: "Triggers", Notebook: "Notebooks", "Spark Pool": "Spark Pools", "Spark Job Definition": "Spark Job Definitions",
  Dataset: "Datasets", "Linked Service": "Linked Services", "Integration Runtime": "Integration Runtimes",
  "Storage Reference": "Storage Connections", "Spark Library": "Spark Libraries", Function: "Functions",
};

function Progress({ status }: { status: DiscoveryStatus }) {
  return (
    <div className="state">
      <span className="spinner" aria-hidden="true" />
      <h2>Discovering Synapse objects…</h2>
      {status.progress ? (
        <ul className="progress" aria-label="Discovery progress">
          {status.progress.map((p) => (
            <li key={p.label} className={p.state}>
              {p.state === "done" ? <Check size={15} aria-label="done" /> : p.state === "active" ? <Loader2 size={15} className="spinner" style={{ border: 0 }} aria-label="in progress" /> : <Circle size={13} aria-label="pending" />}
              {p.label}
            </li>
          ))}
        </ul>
      ) : (
        <p>Reading the workspace and identifying each object's Fabric equivalent. This can take a few minutes for a large workspace. The backend does not report per-stage progress, so none is shown.</p>
      )}
    </div>
  );
}

export function Discovery() {
  const { isConnected, connection, discovery, startDiscovery, discoveryStartError, ready, backendError, mode } = useAppState();
  const navigate = useNavigate();
  const [type, setType] = useState("");
  const running = discovery.state === "running";
  const done = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const summary = discovery.summary;

  const runButton = (label: string, variant: "primary" | "default" = "primary") => (
    <Button variant={variant} onClick={() => void startDiscovery()} disabled={!isConnected || running}><Play size={14} aria-hidden="true" />{label}</Button>
  );

  const statusBadge = running ? <StatusBadge tone="info" running>Discovering</StatusBadge>
    : discovery.state === "failed" ? <StatusBadge tone="error">Failed</StatusBadge>
    : done ? <StatusBadge tone="success">Completed</StatusBadge>
    : isConnected ? <StatusBadge tone="success">Connected</StatusBadge>
    : <StatusBadge tone="neutral">Disconnected</StatusBadge>;

  let body;
  if (!ready) body = <Card><LoadingState label="Loading…" /></Card>;
  else if (!isConnected) {
    body = (
      <Card>
        <EmptyState icon={<PlugZap size={22} />} title="No Synapse workspace connected." actions={<Button variant="primary" onClick={() => navigate("/synapse")}>Connect Synapse</Button>}>
          Discovery is read-only and needs a tested Synapse source connection.
        </EmptyState>
      </Card>
    );
  } else if (running) body = <Card><Progress status={discovery} /></Card>;
  else if (discovery.state === "failed") {
    body = (
      <Card>
        <ErrorState title="Discovery failed." message={discovery.error ?? "The workspace could not be read."} actions={<><Button variant="primary" onClick={() => void startDiscovery()}><RefreshCw size={14} aria-hidden="true" />Retry Discovery</Button><Button onClick={() => navigate("/synapse")}><Settings2 size={14} aria-hidden="true" />Connection Settings</Button></>} />
      </Card>
    );
  } else if (done && summary) {
    const types = Object.entries(summary.byType).sort((a, b) => b[1] - a[1]);
    body = (
      <>
        {discovery.state === "completed_with_warnings" ? (
          <Banner tone="warning" title="Discovery completed with warnings." actions={<Button size="small" onClick={() => void startDiscovery()}><RefreshCw size={13} aria-hidden="true" />Retry discovery</Button>}>
            <div className="stack" style={{ gap: 8 }}>
              <div><strong>Successful:</strong> {Object.keys(summary.byType).join(", ") || "none"}</div>
              {summary.failedCategories.length > 0 && (
                <div><strong>Failed:</strong>
                  <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{summary.failedCategories.map((f) => <li key={f.name}>{f.name} — <span className="faint">{f.reason.length > 280 ? `${f.reason.slice(0, 280)}…` : f.reason}</span></li>)}</ul>
                </div>
              )}
              {summary.warnings.length > 0 && (
                <details><summary style={{ cursor: "pointer" }}>{summary.warningCount} note{summary.warningCount === 1 ? "" : "s"} from discovery</summary>
                  <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{summary.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
                </details>
              )}
            </div>
          </Banner>
        ) : (
          <Banner tone="success" title="Discovery completed successfully.">Total objects discovered: {summary.total.toLocaleString()}.</Banner>
        )}
        {summary.total === 0 ? (
          <Card><EmptyState icon={<SearchX size={22} />} title="No Synapse objects were discovered." actions={runButton("Run Discovery Again", "default")}>The workspace was read successfully but returned no objects.</EmptyState></Card>
        ) : (
          <>
            <div className="grid cols-6" role="list" aria-label="Discovered object types">
              <MetricCard label="Workspaces" value={1} hint="The connected Synapse workspace" />
              {types.map(([t, n]) => (
                <MetricCard key={t} label={PLURAL[t] ?? t} value={n.toLocaleString()} active={type === t} onClick={() => setType(type === t ? "" : t)} hint={`Show only ${PLURAL[t] ?? t}`} />
              ))}
            </div>
            <DiscoveryNotes summary={summary} />
            <Inventory summary={summary} type={type} />
          </>
        )}
      </>
    );
  } else {
    body = (
      <Card>
        <EmptyState tone="info" icon={<Play size={20} />} title="Workspace connected. Discovery has not been executed." actions={runButton("Run Discovery")}>
          Discovery reads what exists in the workspace and shows the Fabric equivalent of each object. It creates and changes nothing.
        </EmptyState>
      </Card>
    );
  }

  return (
    <div className="page">
      <PageHead icon={Radar} title="Discovery" badge={<StatusBadge tone={mode === "mock" ? "warning" : "success"}>{mode === "mock" ? "DEMO DATA" : "LIVE SYNAPSE"}</StatusBadge>}>
        Read the Synapse environment and discover all objects, metadata, dependencies and execution artifacts required for migration assessment.
      </PageHead>

      {backendError && <Banner tone="error" title="Backend unavailable">{backendError}</Banner>}
      {discoveryStartError && <Banner tone="error" title="Discovery could not start">{discoveryStartError}</Banner>}

      <Card>
        <div className="row" style={{ gap: 32 }}>
          <div><div className="eyebrow">Source platform</div><div>{connection.sourcePlatform ?? "Azure Synapse"}</div></div>
          <div><div className="eyebrow">Workspace</div><div>{connection.workspace ?? "—"}</div></div>
          <div><div className="eyebrow">Status</div>{statusBadge}</div>
          <div><div className="eyebrow">Last discovery</div><div>{fmt(discovery.finishedAt)}</div></div>
          <div className="spacer" />
          {(done || discovery.state === "failed") && runButton("Run Discovery")}
        </div>
      </Card>

      {body}
    </div>
  );
}
