import { ArrowDown, ArrowRight } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppState } from "../../state/AppState";
import type { ComponentRow, DiscoverySummary } from "../../types";
import { ClassificationBadge } from "../shared/Metrics";
import { Banner, Button, ErrorState, LoadingState } from "../shared/Shared";

/**
 * Synapse component -> Fabric component -> migration action, straight from the
 * backend's mapping table. Not every row is a one-to-one move, and the
 * classification badge says which are not.
 */
export function ComponentMapping({ summary }: { summary: DiscoverySummary | null }) {
  const { api } = useAppState();
  const [rows, setRows] = useState<ComponentRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setError(null);
    api.getComponents().then(
      (r) => !cancelled && setRows(r),
      (e) => !cancelled && setError(e instanceof Error ? e.message : "The mapping could not be loaded."),
    );
    return () => { cancelled = true; };
  }, [api, attempt]);

  if (error) return <ErrorState title="Could not load the component mapping" message={error} actions={<Button onClick={() => setAttempt((n) => n + 1)}>Retry</Button>} />;
  if (!rows) return <LoadingState label="Loading component mapping…" />;

  return (
    <div className="stack">
      <Banner tone="info">
        These are preliminary, rule-based mappings. Few objects have a one-to-one Fabric equivalent: DIRECT means a recognisable target exists; RECONFIGURE, TRANSFORM, MANUAL and REVIEW mean something has to change or be decided.
      </Banner>
      <div className="mapping-list">
        {rows.map((r) => {
          const found = summary?.byType[r.sourceType];
          return (
            <div key={r.sourceType} className="mapping-row">
              <div className="mapping-cell">
                <span className="eyebrow">Synapse component</span>
                <strong>{r.sourceType}</strong>
                {found !== undefined && <span className="faint">{found.toLocaleString()} discovered</span>}
              </div>
              <ArrowRight className="mapping-arrow wide" size={18} aria-hidden="true" /><ArrowDown className="mapping-arrow narrow" size={18} aria-hidden="true" />
              <div className="mapping-cell">
                <span className="eyebrow">Fabric component</span>
                <strong>{r.fabricTarget}</strong>
                <span className="faint">{r.targetType}</span>
              </div>
              <ArrowRight className="mapping-arrow wide" size={18} aria-hidden="true" /><ArrowDown className="mapping-arrow narrow" size={18} aria-hidden="true" />
              <div className="mapping-cell">
                <span className="eyebrow">Migration action</span>
                <span>{r.action}</span>
                <span><ClassificationBadge value={r.classification} /> <span className="faint">{r.migrationPath}</span></span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
