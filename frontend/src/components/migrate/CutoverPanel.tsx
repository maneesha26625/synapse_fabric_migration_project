import { useEffect, useState } from "react";
import { useAppState } from "../../state/AppState";
import type { CutoverCheck, CutoverState } from "../../types";
import { Banner, Button, StatusBadge, type Tone } from "../shared/Shared";

const messageOf = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong.");
const CHECK_TONE: Record<CutoverCheck["status"], Tone> = { pass: "success", fail: "error", info: "neutral", person: "warning" };
const CHECK_LABEL: Record<CutoverCheck["status"], string> = { pass: "Ready", fail: "Not ready", info: "Note", person: "A person does this" };

/**
 * Validate → Cutover: is Fabric ready to take over, and the three things that make the switch: a final sync,
 * schedules on, and a person's approval. Switching schedules off again is the rollback lever; Synapse is never touched.
 */
export function CutoverPanel() {
  const { api, mode } = useAppState();
  const [state, setState] = useState<CutoverState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [approver, setApprover] = useState("");

  const load = () => {
    setError(null);
    api.getCutover().then(setState, (e) => setError(messageOf(e)));
  };
  useEffect(load, [api]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (what: string, fn: () => Promise<string>) => {
    setBusy(what);
    setMessage(null);
    setError(null);
    try {
      setMessage(await fn());
      load();
    } catch (e) { setError(messageOf(e)); } finally { setBusy(null); }
  };

  if (!state) return error ? <Banner tone="error" title="Cutover readiness could not be read" actions={<Button onClick={load}>Try again</Button>}>{error}</Banner>
    : <p className="muted"><span className="spinner" aria-hidden="true" /> Reading cutover readiness…</p>;

  const off = state.schedules.filter((s) => !s.enabled).length;
  const on = state.schedules.length - off;
  const blocking = state.checks.filter((c) => c.status === "fail").length;
  return (
    <div className="stack">
      <p className="muted" style={{ margin: 0 }}>
        Cutover moves users and schedules from Synapse to Fabric. Nothing here changes Synapse: it stays your fallback until you decommission it.
        {mode === "mock" ? " (Demo data.)" : ""}
      </p>
      <ul className="stack" style={{ listStyle: "none", padding: 0, margin: 0, gap: 6 }} aria-label="Cutover checks">
        {state.checks.map((c) => (
          <li key={c.label} className="row" style={{ gap: 8, alignItems: "baseline" }}>
            <StatusBadge tone={CHECK_TONE[c.status]}>{CHECK_LABEL[c.status]}</StatusBadge>
            <span><strong>{c.label}.</strong> <span className="muted">{c.detail}</span></span>
          </li>
        ))}
      </ul>

      {state.freshness.tables.length > 0 && (
        <div className="table-wrap">
          <table className="data">
            <caption className="sr-only">Tables kept in sync</caption>
            <thead><tr>{["Synced table", "Fabric has up to", "Synapse has up to", "Behind by", "State"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
            <tbody>
              {state.freshness.tables.map((t) => (
                <tr key={t.table}>
                  <td className="name">{t.table}</td><td>{t.watermark || "—"}</td><td>{t.newest ?? "—"}</td>
                  <td>{t.lagMinutes != null ? `${t.lagMinutes.toLocaleString()} min` : "—"}</td>
                  <td><StatusBadge tone={t.behind ? "error" : t.behind === false ? "success" : "neutral"}>{t.behind ? "Behind" : t.behind === false ? "Current" : "Unknown"}</StatusBadge> <span className="faint">{t.detail}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {state.freshness.note && <p className="faint" style={{ margin: 0 }}>{state.freshness.note}</p>}

      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <Button onClick={() => void act("sync", async () => { await api.runFinalSync(); return "The final sync started in Fabric. Check again when it has finished."; })}
          loading={busy === "sync"} disabled={!!busy || !state.freshness.tables.length} title={!state.freshness.tables.length ? "No table is kept in sync." : undefined}>
          Run the final sync now
        </Button>
        <Button variant="primary" onClick={() => void act("on", async () => { const r = await api.setSchedules(true); return r.changed.length ? `Switched on: ${r.changed.join(", ")}.` : "Every schedule was already on."; })}
          loading={busy === "on"} disabled={!!busy || !off} title={blocking ? `${blocking} check(s) are not ready yet.` : undefined}>
          Switch {off || "the"} schedule{off === 1 ? "" : "s"} on
        </Button>
        <Button onClick={() => void act("off", async () => { const r = await api.setSchedules(false); return r.changed.length ? `Switched off (rolled back): ${r.changed.join(", ")}.` : "Every schedule was already off."; })}
          loading={busy === "off"} disabled={!!busy || !on}>
          Roll back: switch schedules off
        </Button>
        <Button onClick={load} disabled={!!busy}>Check again</Button>
      </div>
      {blocking > 0 && off > 0 && <Banner tone="warning" title="Not ready to cut over">{blocking} check(s) above are not ready. Switching schedules on now starts loads in Fabric before it is proven.</Banner>}
      {message && <Banner tone="success">{message}</Banner>}
      {error && <Banner tone="error" title="Cutover">{error}</Banner>}

      {state.record ? (
        <Banner tone="success" title="Cutover approved">Approved by {state.record.approvedBy} at {new Date(state.record.at).toLocaleString()}. Keep Synapse until the rollback window has passed.</Banner>
      ) : (
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <input className="input" placeholder="Who approves the cutover (name and role)" value={approver} onChange={(e) => setApprover(e.target.value)}
            aria-label="Who approves the cutover" style={{ minWidth: 280 }} />
          <Button onClick={() => void act("approve", async () => { await api.completeCutover(approver.trim()); return "Cutover approval recorded."; })}
            disabled={!approver.trim() || !!busy || blocking > 0} loading={busy === "approve"} title={blocking ? "Every check must be ready first." : undefined}>
            Record the cutover approval
          </Button>
        </div>
      )}
    </div>
  );
}
