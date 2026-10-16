import { Bot, Check, X } from "lucide-react";
import { useState } from "react";
import { useOrchestrator } from "../../state/OrchestratorState";
import type { AgentAction } from "../../types";
import { Button, StatusBadge, type Tone } from "../shared/Shared";

const TIER: Record<AgentAction["tier"], { label: string; tone: Tone; words: string }> = {
  read: { label: "Read", tone: "neutral", words: "Changes nothing." },
  prepare: { label: "Prepare", tone: "info", words: "Writes nothing to Fabric." },
  change: { label: "Change", tone: "warning", words: "Writes to the target workspace." },
  high: { label: "High", tone: "error", words: "Overwrites, or really runs things: decided on its own." },
};
const STATUS: Record<AgentAction["status"], { label: string; tone: Tone }> = {
  proposed: { label: "Waiting for you", tone: "warning" }, done: { label: "Done", tone: "success" }, failed: { label: "Failed", tone: "error" },
  rejected: { label: "Rejected", tone: "neutral" }, stale: { label: "No longer applies", tone: "plain" }, expired: { label: "Lapsed", tone: "plain" },
};
/** What a preview says beyond its summary, in words. */
const DETAIL_KEYS: [string, string][] = [["plan", "Plan"], ["objects", "Objects"], ["stages", "Stages"], ["depth", "Depth"],
  ["overwrites", "Overwrites"], ["signoff", "Sign-off"], ["note", "Note"]];

function show(v: unknown): string {
  if (Array.isArray(v)) return v.map(String).join(", ") || "none";
  if (v && typeof v === "object") return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k} ${show(x)}`).join("; ");
  return v === null || v === undefined || v === "" ? "—" : String(v);
}

/** One thing the agent proposed: exactly what it would do, why, and a person's confirm (with a note) or reject (with a reason). */
export function ActionCard({ action, compact }: { action: AgentAction; compact?: boolean }) {
  const { decide, busy } = useOrchestrator();
  const [rejecting, setRejecting] = useState(false);
  const [text, setText] = useState("");
  const tier = TIER[action.tier];
  const status = STATUS[action.status];
  const open = action.status === "proposed";
  const working = busy === `decide:${action.id}`;
  return (
    <article className={`action-card tier-${action.tier}`} aria-label={`Proposed: ${action.preview.summary ?? action.tool}`}>
      <header className="row" style={{ gap: 8, alignItems: "flex-start" }}>
        <Bot size={15} aria-hidden="true" style={{ marginTop: 3 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <strong>{action.preview.summary ?? action.tool}</strong>
          <div className="faint">{action.tool} · proposed by the {action.proposedBy.id === "assistant" ? "assistant" : "agent"}
            {action.autonomous ? ", done on its own (act on Dev)" : ""} · {new Date(action.createdAt).toLocaleString()}</div>
        </div>
        <StatusBadge tone={tier.tone}>{tier.label}</StatusBadge>
        <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
      </header>
      {action.rationale && <p className="muted" style={{ margin: "6px 0 0" }}>{action.rationale}</p>}
      {!compact && (
        <dl className="action-preview">
          {DETAIL_KEYS.filter(([k]) => action.preview[k] !== undefined && action.preview[k] !== null
            && !(Array.isArray(action.preview[k]) && !(action.preview[k] as unknown[]).length)).map(([k, label]) => (
            <div key={k}><dt>{label}</dt><dd>{show(action.preview[k])}</dd></div>
          ))}
          <div><dt>Tier</dt><dd>{tier.words}</dd></div>
        </dl>
      )}
      {action.reason && <p className="faint" style={{ margin: "6px 0 0" }}>{action.status === "rejected" ? "Rejected" : "Why"}: {action.reason}</p>}
      {action.error && <p className="faint" style={{ margin: "6px 0 0" }}>It failed: {action.error}</p>}
      {open && (
        <div className="row" style={{ marginTop: 8, flexWrap: "wrap", gap: 6 }}>
          <input className="input" style={{ flex: 1, minWidth: 200 }} aria-label={rejecting ? `Why reject ${action.tool}` : `Note for ${action.tool}`}
            placeholder={rejecting ? "Why not (kept with it, and told to the agent)" : "Note (optional, kept with the decision)"} value={text} maxLength={500}
            onChange={(e) => setText(e.target.value)} />
          {!rejecting ? <>
            <Button size="small" variant="primary" loading={working} onClick={() => void decide(action.id, "confirm", text.trim() || undefined)}>
              <Check size={13} aria-hidden="true" />Confirm
            </Button>
            <Button size="small" onClick={() => setRejecting(true)} disabled={working}><X size={13} aria-hidden="true" />Reject</Button>
          </> : <>
            <Button size="small" variant="primary" disabled={!text.trim()} loading={working} onClick={() => void decide(action.id, "reject", text.trim())}>Reject with this reason</Button>
            <Button size="small" variant="ghost" onClick={() => setRejecting(false)}>Cancel</Button>
          </>}
        </div>
      )}
    </article>
  );
}
