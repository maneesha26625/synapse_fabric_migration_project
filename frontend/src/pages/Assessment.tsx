import { ScanSearch } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ComponentMapping } from "../components/discovery/ComponentMapping";
import { WorkstreamSummary } from "../components/discovery/DiscoverySummary";
import { Inventory } from "../components/discovery/Inventory";
import { MetricCard } from "../components/shared/Metrics";
import { Banner, Button, Card, EmptyState, PageHead } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import type { Classification } from "../types";

type Tab = "assessment" | "mapping";

const CARDS: { label: string; cls: Classification; hint: string }[] = [
  { label: "Direct migration", cls: "DIRECT", hint: "A recognisable Fabric target exists" },
  { label: "Requires configuration", cls: "RECONFIGURE", hint: "Fabric configures this differently" },
  { label: "Requires transformation", cls: "TRANSFORM", hint: "A target exists; configuration or code may change" },
  { label: "Manual migration", cls: "MANUAL", hint: "Needs a special path" },
  { label: "Requires review", cls: "REVIEW", hint: "No reliable one-to-one mapping" },
];

export function Assessment() {
  const { discovery, isConnected } = useAppState();
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("assessment");
  const [cls, setCls] = useState("");
  const [workstream, setWorkstream] = useState("");
  const summary = discovery.summary;
  const done = (discovery.state === "completed" || discovery.state === "completed_with_warnings") && !!summary;

  return (
    <div className="page">
      <PageHead icon={ScanSearch} title="Migration Assessment">
        Analyze every discovered Synapse object and determine how it can be migrated to Microsoft Fabric.
      </PageHead>

      <div className="tabs tabs-pill" role="tablist" aria-label="Assessment sections">
        <button role="tab" type="button" className="tab" aria-selected={tab === "assessment"} onClick={() => setTab("assessment")}>Assessment</button>
        <button role="tab" type="button" className="tab" aria-selected={tab === "mapping"} onClick={() => setTab("mapping")}>Component Mapping</button>
      </div>

      {tab === "mapping" && <ComponentMapping summary={summary} />}

      {tab === "assessment" && !done && (
        <Card>
          <EmptyState
            title={isConnected ? "Discovery has not been executed." : "No Synapse workspace connected."}
            actions={<Button variant="primary" onClick={() => navigate(isConnected ? "/discovery" : "/synapse")}>{isConnected ? "Run Discovery" : "Connect Synapse"}</Button>}
          >
            Assessment works from the discovered objects.
          </EmptyState>
        </Card>
      )}

      {tab === "assessment" && done && summary && (
        <>
          <Banner tone="info" title="Preliminary classification from Discovery">
            The assessment engine, which analyses code and data in detail, is not implemented yet. These counts are the classification Discovery assigns from each object's type, plus Synapse-specific code it found. "Not supported" can only be decided by that engine, so it is not counted.
          </Banner>

          <div className="grid cols-4" role="list" aria-label="Assessment summary">
            <MetricCard label="Total objects" value={summary.total.toLocaleString()} active={cls === ""} onClick={() => setCls("")} />
            {CARDS.map((c) => (
              <MetricCard key={c.cls} label={c.label} value={(summary.byClassification[c.cls] ?? 0).toLocaleString()} hint={c.hint} tone={c.cls} active={cls === c.cls} onClick={() => setCls(cls === c.cls ? "" : c.cls)} />
            ))}
            <MetricCard label="Not supported" value="n/a" hint="Only the assessment engine can decide this; Discovery never does." tone="NOT SUPPORTED" />
          </div>

          <Card title="Classification breakdown">
            <div className="stackbar" role="img" aria-label={CARDS.map((c) => `${c.cls}: ${summary.byClassification[c.cls] ?? 0}`).join(", ")}>
              {CARDS.filter((c) => (summary.byClassification[c.cls] ?? 0) > 0).map((c) => (
                <span key={c.cls} className={`seg cls-${c.cls.toLowerCase()}-bg`} style={{ flexGrow: summary.byClassification[c.cls] }} title={`${c.cls}: ${summary.byClassification[c.cls]}`} />
              ))}
            </div>
            <ul className="legend">
              {CARDS.map((c) => (
                <li key={c.cls}><span className={`dot cls-${c.cls.toLowerCase()}-bg`} />{c.cls} <strong>{(summary.byClassification[c.cls] ?? 0).toLocaleString()}</strong></li>
              ))}
            </ul>
          </Card>

          <WorkstreamSummary summary={summary} active={workstream} onSelect={setWorkstream} />

          <Card title="Objects" subtitle={cls ? `Showing ${cls} objects. Select the card again to clear.` : "All discovered objects. Select a card above to filter."}>
            <Inventory summary={summary} showTabs={false} classification={cls} workstream={workstream} onWorkstream={setWorkstream} />
          </Card>
        </>
      )}
    </div>
  );
}
