import { Check, Circle, Loader2, Play, Radar, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { DiscoveryNotes } from "../../discovery/DiscoverySummary";
import { Inventory } from "../../discovery/Inventory";
import { StatStrip } from "../../shared/Metrics";
import { Banner, Button, ConfirmDialog, ErrorState, Tabs } from "../../shared/Shared";
import { useAppState } from "../../../state/AppState";
import { useMigration } from "../../../state/MigrationState";

const PLURAL: Record<string, string> = {
  "Dedicated SQL Pool": "SQL pools", Table: "Tables", "External Table": "External tables", View: "Views",
  "Stored Procedure": "Stored procedures", "SQL Script": "SQL scripts", Schema: "Schemas", Pipeline: "Pipelines",
  Trigger: "Triggers", Notebook: "Notebooks", "Spark Pool": "Spark pools", "Spark Job Definition": "Spark job definitions",
  Dataset: "Datasets", "Linked Service": "Linked services", "Integration Runtime": "Integration runtimes",
  "Storage Reference": "Storage connections", "Spark Library": "Spark libraries", Function: "Functions",
};

export function DiscoverPanel() {
  const { isConnected, connection, discovery, startDiscovery, discoveryStartError, health } = useAppState();
  const { resetJourney, confirmed } = useMigration();
  const navigate = useNavigate();
  const [tab, setTab] = useState<"overview" | "inventory">("overview");
  const [type, setType] = useState("");
  const [askRerun, setAskRerun] = useState(false);
  // Running again replaces what the confirmed steps were built on: ask before undoing them.
  const laterConfirmed = confirmed.some((k) => k !== "discover");
  const running = discovery.state === "running";
  const done = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const summary = discovery.summary;

  // Discovering again replaces what every later step was built on, so their confirmations go too.
  const run = async () => { resetJourney("discover"); setType(""); setTab("overview"); await startDiscovery(); };

  if (running) {
    return (
      <div className="panel-center" aria-live="polite">
        <span className="spinner large" aria-hidden="true" />
        <h3>Reading {connection.workspace ?? "the workspace"}…</h3>
        {discovery.progress ? (
          <ul className="progress" aria-label="Discovery progress">
            {discovery.progress.map((p) => (
              <li key={p.label} className={p.state}>
                {p.state === "done" ? <Check size={15} aria-label="done" /> : p.state === "active" ? <Loader2 size={15} className="spin" aria-label="in progress" /> : <Circle size={13} aria-label="pending" />}
                {p.label}
              </li>
            ))}
          </ul>
        ) : <p className="muted">This can take a few minutes for a large workspace.</p>}
      </div>
    );
  }

  if (discovery.state === "failed") {
    return (
      <ErrorState title="Discovery failed" message={discovery.error ?? "The workspace could not be read."}
        actions={<><Button variant="primary" onClick={() => void run()} disabled={!isConnected}><RefreshCw size={14} aria-hidden="true" />Run discovery again</Button><Button onClick={() => navigate("/")}>Check the connection</Button></>} />
    );
  }

  if (!done || !summary) {
    return (
      <div className="panel-center">
        <span className="intro-icon" aria-hidden="true"><Radar size={26} /></span>
        <h3>Read the {connection.workspace ?? "source"} workspace</h3>
        <p className="muted">Discovery lists every object and the Fabric component it maps to. It only reads: nothing in Synapse is changed.</p>
        {health?.capabilities.discoveryScope?.length ? (
          <ul className="scope-chips" aria-label="What discovery reads">
            {health.capabilities.discoveryScope.map((s) => <li key={s}>{s}</li>)}
          </ul>
        ) : null}
        {discoveryStartError && <Banner tone="error" title="Discovery could not start">{discoveryStartError}</Banner>}
        <Button variant="primary" onClick={() => void run()} disabled={!isConnected}><Play size={14} aria-hidden="true" />Run discovery</Button>
        {!isConnected && <p className="faint">Connect the source first. <button type="button" className="link" onClick={() => navigate("/")}>Open Connections</button></p>}
      </div>
    );
  }

  const types = Object.entries(summary.byType).sort((a, b) => b[1] - a[1]);
  return (
    <div className="stack panel-body">
      <div className="panel-toolbar">
        <span className="muted">{connection.workspace ?? "Workspace"} · read {discovery.finishedAt ? new Date(discovery.finishedAt).toLocaleString() : "—"}</span>
        <span className="spacer" />
        <Button size="small" onClick={() => (laterConfirmed ? setAskRerun(true) : void run())} disabled={!isConnected}
          title={isConnected ? "Read the workspace again" : "Connect the source to run discovery again"}><RefreshCw size={13} aria-hidden="true" />Run again</Button>
      </div>
      {askRerun && (
        <ConfirmDialog title="Run discovery again?" confirmLabel="Run discovery again" danger={false}
          onConfirm={() => { setAskRerun(false); void run(); }} onCancel={() => setAskRerun(false)}>
          <p>Discovery reads {connection.workspace ?? "the workspace"} again and replaces the inventory. The steps after Discover will need to be confirmed again.</p>
          <p className="muted">Your plan, the record of a migration run and the validation results are kept. To clear those as well, use Reset step instead.</p>
        </ConfirmDialog>
      )}

      <StatStrip label="Discovery summary" items={[
        { label: "Objects", value: summary.total.toLocaleString(), tone: "accent" },
        { label: "Object types", value: types.length },
        { label: "With a Fabric mapping", value: summary.withFabricMapping.toLocaleString(), hint: "A named Fabric component exists" },
        { label: "Manual or review", value: summary.manualOrAssessment.toLocaleString(), hint: "Needs a decision before it moves" },
        { label: "Warnings", value: summary.warningCount, tone: summary.warningCount ? "warning" : undefined },
      ]} />

      {discovery.state === "completed_with_warnings" && (
        <Banner tone="warning" title="Completed with warnings">
          {summary.failedCategories.length ? <>Not read: {summary.failedCategories.map((f) => f.name).join(", ")}. </> : null}
          {summary.warnings.slice(0, 2).join(" ")}
        </Banner>
      )}

      <Tabs label="Discovery sections" value={tab} onChange={setTab} tabs={[
        { id: "overview", label: "Overview" },
        { id: "inventory", label: "Inventory", count: summary.total },
      ]} />

      {tab === "overview" ? (
        <div className="stack">
          <ul className="type-grid" aria-label="Discovered object types">
            {types.map(([t, count]) => (
              <li key={t}>
                <button type="button" className="type-tile" onClick={() => { setType(t); setTab("inventory"); }} title={`Show the ${PLURAL[t] ?? t}`}>
                  <span className="type-count">{count.toLocaleString()}</span>
                  <span className="type-name">{PLURAL[t] ?? t}</span>
                </button>
              </li>
            ))}
          </ul>
          <DiscoveryNotes summary={summary} />
        </div>
      ) : (
        <>
          {type && <div className="row"><span className="chip" role="status">{PLURAL[type] ?? type}<button type="button" className="chip-x" aria-label="Show every type" onClick={() => setType("")}>×</button></span></div>}
          <Inventory summary={summary} type={type} />
        </>
      )}
    </div>
  );
}
