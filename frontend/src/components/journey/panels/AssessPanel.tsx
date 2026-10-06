import { Info } from "lucide-react";
import { useState } from "react";
import { ComponentMapping } from "../../discovery/ComponentMapping";
import { WorkstreamSummary } from "../../discovery/DiscoverySummary";
import { Inventory } from "../../discovery/Inventory";
import { Tabs } from "../../shared/Shared";
import { useAppState } from "../../../state/AppState";
import type { Classification } from "../../../types";

const CLASSES: { cls: Classification; label: string; meaning: string }[] = [
  { cls: "DIRECT", label: "Direct", meaning: "A matching Fabric component exists" },
  { cls: "RECONFIGURE", label: "Reconfigure", meaning: "Same idea, configured differently in Fabric" },
  { cls: "TRANSFORM", label: "Transform", meaning: "A target exists; code or settings change" },
  { cls: "MANUAL", label: "Manual", meaning: "Needs a special path, by hand" },
  { cls: "REVIEW", label: "Review", meaning: "No reliable one-to-one mapping" },
];

export function AssessPanel() {
  const { discovery } = useAppState();
  const [tab, setTab] = useState<"overview" | "objects" | "mapping">("overview");
  const [cls, setCls] = useState("");
  const [workstream, setWorkstream] = useState("");
  const summary = discovery.summary;
  if (!summary) return <p className="muted">Assessment works from the discovered objects. Run discovery first.</p>;
  const count = (c: Classification) => summary.byClassification[c] ?? 0;
  const pct = (v: number) => (summary.total ? Math.round((v / summary.total) * 100) : 0);

  const open = (c: string) => { setCls(c); setTab("objects"); };

  return (
    <div className="stack panel-body">
      <ul className="class-strip" aria-label="How objects move">
        {CLASSES.map(({ cls: c, label, meaning }) => (
          <li key={c}>
            <button type="button" className={`class-tile cls-${c.toLowerCase()}-edge`} aria-pressed={tab === "objects" && cls === c}
              onClick={() => (tab === "objects" && cls === c ? setCls("") : open(c))} title={`${meaning}. Show these objects.`}>
              <span className="class-label">{label}</span>
              <span className="class-value">{count(c).toLocaleString()}</span>
              <span className="class-pct">{pct(count(c))}% · {meaning}</span>
            </button>
          </li>
        ))}
      </ul>

      <div className="stackbar" role="img" aria-label={CLASSES.map((c) => `${c.label}: ${count(c.cls)}`).join(", ")}>
        {CLASSES.filter((c) => count(c.cls) > 0).map((c) => (
          <span key={c.cls} className={`seg cls-${c.cls.toLowerCase()}-bg`} style={{ flexGrow: count(c.cls) }} title={`${c.label}: ${count(c.cls)}`} />
        ))}
      </div>
      <p className="faint note-line"><Info size={13} aria-hidden="true" /> A preliminary classification from each object's type and the Synapse-only code discovery found. A deeper code assessment is planned.</p>

      <Tabs label="Assessment sections" value={tab} onChange={setTab} tabs={[
        { id: "overview", label: "By workstream" },
        { id: "objects", label: "Objects", count: summary.total },
        { id: "mapping", label: "Component mapping" },
      ]} />

      {tab === "overview" && <WorkstreamSummary summary={summary} active={workstream} onSelect={(w) => { setWorkstream(w); if (w) setTab("objects"); }} />}
      {tab === "objects" && (
        <>
          {(cls || workstream) && (
            <div className="row">
              {cls && <span className="chip" role="status">{CLASSES.find((c) => c.cls === cls)?.label ?? cls}<button type="button" className="chip-x" aria-label="Clear the classification filter" onClick={() => setCls("")}>×</button></span>}
              {workstream && <span className="chip" role="status">{workstream}<button type="button" className="chip-x" aria-label="Clear the workstream filter" onClick={() => setWorkstream("")}>×</button></span>}
            </div>
          )}
          <Inventory summary={summary} showTabs={false} classification={cls} workstream={workstream} onWorkstream={setWorkstream} />
        </>
      )}
      {tab === "mapping" && <ComponentMapping summary={summary} />}
    </div>
  );
}
