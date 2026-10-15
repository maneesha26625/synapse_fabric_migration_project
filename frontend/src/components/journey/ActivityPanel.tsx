import { Download, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import { ApiRequestError, type AuditRecord, type AuditVerification } from "../../types";
import { Banner, Button, Card, StatusBadge, type Tone } from "../shared/Shared";

const OUTCOME_TONE: Record<AuditRecord["outcome"], Tone> = { done: "success", failed: "error", refused: "warning" };
const WHO: Record<AuditRecord["actor"]["kind"], string> = { person: "", agent: "LLM: ", system: "Accelerator" };
const FAMILIES = [["", "Everything"], ["run", "Runs"], ["plan", "Plans"], ["project", "Projects"], ["source", "Synapse source"],
  ["discovery", "Discovery"], ["fabric", "Fabric target"], ["validation", "Validation"], ["git", "Fabric Git"], ["transform", "LLM transforms"],
  ["journey", "Journey"], ["system", "API"]] as const;
const messageOf = (e: unknown) => (e instanceof ApiRequestError || e instanceof Error ? e.message : "Something went wrong.");

/** A record's details, as short "name: value" pairs. */
function details(r: AuditRecord): string {
  return Object.entries(r.details ?? {})
    .filter(([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" · ");
}

function save(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * The project's audit trail: who did what, to what, when, and how it ended. A person, the LLM (as an agent), or
 * the accelerator itself, each named for what it did. Read-only; the trail's hash chain can be checked here.
 */
export function ActivityPanel() {
  const { api } = useAppState();
  const { projectEpoch, backendProjects, project } = useMigration();
  const [open, setOpen] = useState(false);
  const [records, setRecords] = useState<AuditRecord[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [catalog, setCatalog] = useState<Record<string, string>>({});
  const [family, setFamily] = useState("");
  const [actor, setActor] = useState("");
  const [verified, setVerified] = useState<AuditVerification | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (before?: number) => {
    setBusy(true);
    setError(null);
    try {
      const page = await api.getAudit({ limit: 50, before, action: family || undefined, actor: actor || undefined });
      setRecords((had) => (before === undefined ? page.records : [...had, ...page.records]));
      setNext(page.next);
      setCatalog(page.catalog ?? {});
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  }, [api, family, actor]);
  useEffect(() => { if (open) void load(); }, [open, load, projectEpoch]);
  useEffect(() => { setVerified(null); }, [projectEpoch]);
  if (!backendProjects) return null; // an older backend keeps no trail

  const verify = async () => {
    setError(null);
    try { setVerified(await api.verifyAudit()); } catch (e) { setError(messageOf(e)); }
  };
  const exportAs = async (format: "csv" | "jsonl") => {
    setError(null);
    try {
      const out = await api.exportAudit(format);
      if (out.url) {
        const a = document.createElement("a");
        a.href = out.url;
        a.download = out.fileName;
        a.click();
      } else if (out.text !== undefined) {
        save(out.fileName, out.text, format === "csv" ? "text/csv" : "application/x-ndjson");
      }
      if (open) void load();
    } catch (e) { setError(messageOf(e)); }
  };

  return (
    <Card eyebrow="Audit trail" title="Activity" subtitle={`Who did what in ${project.name}, and when: people, the LLM and the accelerator itself, each for what it did.`}
      actions={<Button size="small" variant="ghost" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{open ? "Hide activity" : "Show activity"}</Button>}>
      {open && (
        <div className="stack" style={{ gap: 10 }}>
          <div className="row">
            <select className="select" aria-label="Show actions of" value={family} onChange={(e) => setFamily(e.target.value)}>
              {FAMILIES.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
            <select className="select" aria-label="Done by" value={actor} onChange={(e) => setActor(e.target.value)}>
              <option value="">By anyone</option>
              <option value="person">By a person</option>
              <option value="system">By the accelerator</option>
              <option value="agent">By the LLM</option>
            </select>
            <span className="spacer" />
            <Button size="small" onClick={() => void verify()}><ShieldCheck size={14} aria-hidden="true" />Check the trail</Button>
            <Button size="small" onClick={() => void exportAs("csv")}><Download size={14} aria-hidden="true" />CSV</Button>
            <Button size="small" onClick={() => void exportAs("jsonl")}><Download size={14} aria-hidden="true" />JSON lines</Button>
          </div>
          {error && <Banner tone="error" title="Activity">{error}</Banner>}
          {verified && (verified.ok
            ? <Banner tone="success" title="The trail is intact">All {verified.records.toLocaleString()} records follow on from each other: none was changed, removed or moved since it was written.</Banner>
            : <Banner tone="error" title={`The trail was changed at record ${verified.brokenAt}`}>{verified.problem}</Banner>)}
          <div className="table-wrap">
            <table className="data">
              <caption className="sr-only">Audit trail, newest first</caption>
              <thead><tr>{["When", "Who", "What", "On", "Outcome", "Details"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
              <tbody>
                {records.map((r) => (
                  <tr key={r.seq}>
                    <td className="muted" title={r.correlationId ? `Request ${r.correlationId}` : undefined}>{new Date(r.at).toLocaleString()}</td>
                    <td>{r.actor.kind === "person" ? r.actor.id : `${WHO[r.actor.kind]}${r.actor.kind === "agent" ? r.actor.id : ""}`}</td>
                    <td title={r.action}>{catalog[r.action] ?? r.action}{r.reason ? <div className="faint">{r.reason}</div> : null}</td>
                    <td className="muted">{r.target ? `${r.target.type}${r.target.id ? ` ${r.target.id}` : ""}` : "—"}</td>
                    <td><StatusBadge tone={OUTCOME_TONE[r.outcome] ?? "neutral"}>{r.outcome.toUpperCase()}</StatusBadge></td>
                    <td className="muted" style={{ whiteSpace: "normal", maxWidth: 420 }}>{details(r) || "—"}</td>
                  </tr>
                ))}
                {!records.length && !busy && <tr><td colSpan={6} className="muted">Nothing recorded {family || actor ? "that matches" : "yet"}.</td></tr>}
              </tbody>
            </table>
          </div>
          {busy && <p className="muted" role="status"><span className="spinner" aria-hidden="true" /> Reading the trail…</p>}
          {next !== null && !busy && <div><Button size="small" onClick={() => void load(next)}>Show older</Button></div>}
        </div>
      )}
    </Card>
  );
}
