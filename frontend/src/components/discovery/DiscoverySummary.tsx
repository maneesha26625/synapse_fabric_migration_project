import { Info } from "lucide-react";
import { MIGRATION_PATHS, WORKSTREAMS, type DiscoverySummary as Summary } from "../../types";
import { Card, Collapsible } from "../shared/Shared";

/** Eight cards, every number straight from the discovery result. */
export function SummaryCards({ summary }: { summary: Summary }) {
  const c = summary.byCategory;
  const tiles: [string, number, string?][] = [
    ["Total objects", summary.total],
    ["SQL objects", c["SQL"] ?? 0],
    ["Spark objects", c["Spark"] ?? 0],
    ["Pipeline / Integration", c["Integration"] ?? 0],
    ["Storage / Data", c["Storage"] ?? 0],
    ["Security / Configuration", (c["Security"] ?? 0) + (c["Networking"] ?? 0), "Includes networking and integration runtimes"],
    ["With a Fabric mapping", summary.withFabricMapping, "A named Fabric component exists. Not a migration verdict."],
    ["Requiring assessment", summary.requiringAssessment, "Every object: Discovery does not decide compatibility."],
  ];
  return (
    <div className="grid cols-4" role="list" aria-label="Discovery summary">
      {tiles.map(([label, value, hint]) => (
        <div className="tile" role="listitem" key={label} title={hint}>
          <span className="label">{label}</span>
          <span className="value">{value.toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

/** Objects grouped by the Fabric area they appear to belong to. Click to filter the table. */
export function WorkstreamSummary({ summary, active, onSelect }: { summary: Summary; active: string; onSelect: (workstream: string) => void }) {
  const entries = WORKSTREAMS.filter((w) => summary.byWorkstream[w]);
  return (
    <Card title="Fabric migration workstreams" subtitle="Objects grouped by the Fabric area they appear to correspond to. Select one to filter the inventory.">
      <div className="ws-grid">
        {entries.map((w) => (
          <button key={w} type="button" className="ws-card" aria-pressed={active === w} onClick={() => onSelect(active === w ? "" : w)}>
            <span className="ws-count">{summary.byWorkstream[w].toLocaleString()}</span>
            <span className="ws-name">{w}</span>
            <span className="faint">objects</span>
          </button>
        ))}
        <div className="ws-card static" title="Objects whose path is Requires Assessment or Manual / Special Handling, across all workstreams.">
          <span className="ws-count">{summary.manualOrAssessment.toLocaleString()}</span>
          <span className="ws-name">Manual / Assessment Required</span>
          <span className="faint">across all workstreams</span>
        </div>
      </div>
    </Card>
  );
}

/** What the classifications mean, and what this build did not discover. */
export function DiscoveryNotes({ summary }: { summary: Summary }) {
  return (
    <div className="stack">
      <Collapsible summary={<><Info size={15} aria-hidden="true" /> How to read the Fabric mapping</>}>
        <div className="stack">
          <p className="muted">These are <strong>preliminary</strong> classifications from the object type. They say where an object appears to land in Fabric, not that it will migrate or that nothing needs to change. Assessment decides that.</p>
          <dl className="kv">
            {MIGRATION_PATHS.map((p) => (
              <div key={p} style={{ display: "contents" }}>
                <dt>{p}</dt>
                <dd className="muted">{PATH_HELP[p]}</dd>
              </div>
            ))}
            <dt>Automation potential</dt>
            <dd className="muted">Candidate: an automated route may exist. Partial: only some of it. Manual: expect hand work. Not determined: unknown until Assessment.</dd>
          </dl>
        </div>
      </Collapsible>
      {summary.coverage.notDiscovered.length > 0 && (
        <Collapsible summary={<><Info size={15} aria-hidden="true" /> Not discovered in this build ({summary.coverage.notDiscovered.length} types)</>}>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {summary.coverage.notDiscovered.map((n) => <li key={n.type}><strong>{n.type}</strong> — <span className="faint">{n.reason}</span></li>)}
          </ul>
        </Collapsible>
      )}
    </div>
  );
}

const PATH_HELP: Record<(typeof MIGRATION_PATHS)[number], string> = {
  "Direct Target": "A recognisable Fabric component exists (for example Notebook → Fabric Notebook).",
  "Target With Transformation": "A Fabric target exists but configuration may change (for example Spark pool → Fabric Spark / Environment).",
  "Target With Refactoring": "A target exists but source code or logic may need changes (for example a notebook using Synapse-specific APIs).",
  "Requires Reconfiguration": "The concept exists but Fabric configures it differently (for example Linked Service → Connection).",
  "Requires Assessment": "No reliable one-to-one mapping should be assumed (for example external data sources).",
  "Manual / Special Handling": "Needs a special path (for example network configuration).",
};
