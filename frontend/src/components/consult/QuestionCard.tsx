import { CircleAlert, Lightbulb, RotateCcw, Sparkles } from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";
import { useAppState } from "../../state/AppState";
import { useConsult } from "../../state/ConsultState";
import type { AnswerPreview, AnswerSource, ConsultExplanation, ConsultQuestion, ConsultQuestionDef, ConsultStatus } from "../../types";
import { Button, StatusBadge, type Tone } from "../shared/Shared";
import { changeText, groupLabel, show } from "./format";

export const STATUS: Record<ConsultStatus, { label: string; tone: Tone }> = {
  answered: { label: "Answered", tone: "success" }, assumed: { label: "Assumed", tone: "neutral" }, open: { label: "Open", tone: "warning" },
  skipped: { label: "Skipped", tone: "neutral" }, stale: { label: "Confirm again", tone: "warning" }, parked: { label: "Parked", tone: "plain" },
  not_applicable: { label: "Does not apply", tone: "plain" },
};
export const SOURCE: Record<AnswerSource, string> = {
  person: "answered", recommendation: "recommendation accepted", finding: "finding taken", ai: "AI proposal confirmed",
  rules: "read from notes, confirmed", import: "from the questionnaire",
};

type Draft = string | boolean | string[] | Record<string, string>;

/** A value as a person reads it, in the question's own words. */
export function labelOf(def: ConsultQuestionDef, v: unknown): string {
  if (def.type === "choice") return def.options.find((o) => o.value === v)?.label ?? show(v);
  if (def.type === "boolean") return v ? "Yes" : "No";
  if (def.type === "multi") return (Array.isArray(v) ? v : []).map((x) => def.options.find((o) => o.value === x)?.label ?? groupLabel(String(x))).join(", ") || "None";
  if (def.type === "principalMap") return Object.entries((v ?? {}) as Record<string, string>).map(([u, e]) => `${u} → ${e}`).join("; ") || "None mapped";
  if (def.type === "text" && v === "") return "None";
  return show(v);
}

/** What the form holds for a value: lists as lines, numbers as text, a map as its rows. */
function toDraft(def: ConsultQuestionDef, value: unknown): Draft {
  switch (def.type) {
    case "boolean": return value === true;
    case "multi": return Array.isArray(value) ? value.map(String) : [];
    case "list": return Array.isArray(value) ? value.join("\n") : "";
    case "principalMap": return value && typeof value === "object" ? { ...(value as Record<string, string>) } : {};
    case "number": return value === null || value === undefined ? "" : String(value);
    default: return value === null || value === undefined ? "" : String(value);
  }
}
/** What is sent: the backend checks and cleans it, and says what is wrong in words. */
function fromDraft(def: ConsultQuestionDef, draft: Draft): unknown {
  if (def.type === "number") return draft === "" ? null : Number(draft);
  return draft;
}
const sameDraft = (a: Draft, b: Draft) => JSON.stringify(a) === JSON.stringify(b);

function Field({ def, q, draft, setDraft, disabled }: { def: ConsultQuestionDef; q: ConsultQuestion; draft: Draft; setDraft: (d: Draft) => void; disabled: boolean }) {
  const id = useId();
  const label = `${def.id} answer`;
  if (def.type === "choice" || def.type === "boolean") {
    const options = def.type === "boolean" ? [{ value: "true", label: "Yes", description: "" }, { value: "false", label: "No", description: "" }] : def.options;
    const current = def.type === "boolean" ? String(draft === true) : String(draft);
    return (
      <div className="consult-options" role="radiogroup" aria-label={label}>
        {options.map((o) => (
          <label key={o.value} className={`consult-option${current === o.value ? " chosen" : ""}`}>
            <input type="radio" name={id} value={o.value} checked={current === o.value} disabled={disabled}
              onChange={() => setDraft(def.type === "boolean" ? o.value === "true" : o.value)} />
            <span><strong>{o.label}</strong>{o.description && <span className="muted"> — {o.description}</span>}</span>
          </label>
        ))}
      </div>
    );
  }
  if (def.type === "multi") {
    const choices = def.optionsFrom ? (q.choices ?? []).map((v) => ({ value: v, label: groupLabel(v), description: "" })) : def.options;
    const chosen = new Set(draft as string[]);
    if (!choices.length) return <p className="faint">Nothing was discovered to choose from.</p>;
    return (
      <div className="consult-options" role="group" aria-label={label}>
        {choices.map((o) => (
          <label key={o.value} className={`consult-option${chosen.has(o.value) ? " chosen" : ""}`}>
            <input type="checkbox" checked={chosen.has(o.value)} disabled={disabled}
              onChange={() => setDraft(chosen.has(o.value) ? [...chosen].filter((v) => v !== o.value) : [...chosen, o.value])} />
            <span><strong>{o.label}</strong>{o.description && <span className="muted"> — {o.description}</span>}</span>
          </label>
        ))}
      </div>
    );
  }
  if (def.type === "principalMap") {
    const rows = q.rows ?? [];
    const map = draft as Record<string, string>;
    if (!rows.length) return <p className="faint">No SQL users were discovered.</p>;
    return (
      <div className="consult-map" role="group" aria-label={label}>
        {rows.map((user) => (
          <label key={user} className="field">
            <span>{user} <span className="faint">(SQL user)</span></span>
            <input className="input" placeholder="user@contoso.com, or a group's name" value={map[user] ?? ""} disabled={disabled}
              aria-label={`Microsoft Entra user or group for ${user}`} onChange={(e) => setDraft({ ...map, [user]: e.target.value })} />
          </label>
        ))}
      </div>
    );
  }
  if (def.type === "text" || def.type === "list") {
    return (
      <textarea className="input consult-text" aria-label={label} value={draft as string} disabled={disabled} rows={def.type === "list" ? 3 : 3}
        maxLength={def.type === "text" ? def.maxLength : undefined} placeholder={def.type === "list" ? "One per line, or separated by commas" : def.optional ? "Optional" : ""}
        onChange={(e) => setDraft(e.target.value)} />
    );
  }
  return (
    <input className="input consult-input" aria-label={label} disabled={disabled} value={draft as string}
      type={def.type === "number" ? "number" : def.type === "date" ? "date" : "text"}
      min={def.min ?? undefined} max={def.max ?? undefined} onChange={(e) => setDraft(e.target.value)} />
  );
}

/** What an answer would change, from the server's preview: what it opens, parks and revives, and the settings it moves. */
function PreviewLine({ preview }: { preview: AnswerPreview["preview"] }) {
  const parts = [
    preview.opens.length ? `opens ${preview.opens.join(", ")}` : "",
    preview.revives.length ? `brings back ${preview.revives.join(", ")}` : "",
    preview.parks.length ? `parks ${preview.parks.join(", ")} (kept, and back if it applies again)` : "",
    preview.conflictsOpened.length ? `raises contradiction ${preview.conflictsOpened.join(", ")}` : "",
    preview.conflictsClosed.length ? `settles ${preview.conflictsClosed.join(", ")}` : "",
    ...preview.settings.map((s) => `${s.setting}: ${show(s.before)} → ${show(s.after)}`),
  ].filter(Boolean);
  if (!parts.length) return null;
  return <p className="faint consult-preview" role="status">This answer {parts.join("; ")}.</p>;
}

/** One question: where it stands, who answered it and why, what it rests on, and the controls to answer it. */
export function QuestionCard({ def, q, focused }: { def: ConsultQuestionDef; q: ConsultQuestion; focused?: boolean }) {
  const { api } = useAppState();
  const { answer, skip, withdraw, explain, busy } = useConsult();
  const current = useMemo(() => toDraft(def, q.value), [def, q.value]);
  const [draft, setDraft] = useState<Draft>(current);
  const [note, setNote] = useState("");
  const [preview, setPreview] = useState<AnswerPreview["preview"] | null>(null);
  const [explained, setExplained] = useState<ConsultExplanation | null>(null);
  useEffect(() => { setDraft(current); setPreview(null); }, [current]);
  const dirty = !sameDraft(draft, current);
  const working = !!busy && busy.endsWith(`:${def.id}`);
  // A changed choice says what it would change before it is saved.
  useEffect(() => {
    if (!dirty || !["choice", "boolean", "multi"].includes(def.type)) { setPreview(null); return; }
    let live = true;
    const t = setTimeout(() => {
      api.previewAnswer(def.id, fromDraft(def, draft)).then((p) => { if (live) setPreview(p.preview); }, () => { if (live) setPreview(null); });
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [api, def, draft, dirty]);

  const st = STATUS[q.status];
  const save = async () => { if (await answer(def.id, fromDraft(def, draft), note.trim() || undefined)) setNote(""); };
  const empty = draft === "" || (Array.isArray(draft) && !draft.length);
  // Saved when changed, when an answer is confirmed again, or when an open question gets one.
  const canSave = dirty || q.status === "stale" || (q.status === "open" && !empty);
  const confirmed = q.status === "answered" || q.status === "stale";
  const canAccept = (q.status === "assumed" || q.status === "skipped") && q.recommendation;
  return (
    <section className={`consult-q${q.status === "stale" || q.conflicts.length ? " attention" : ""}${focused ? " focused" : ""}`} aria-label={`${def.id}. ${def.text}`} id={`question-${def.id}`}>
      <header className="row" style={{ gap: 8, alignItems: "flex-start" }}>
        <h3 style={{ flex: 1 }}><span className="consult-id">{def.id}</span> {def.text}</h3>
        {def.required && <StatusBadge tone="info">A person answers</StatusBadge>}
        <StatusBadge tone={st.tone}>{st.label}</StatusBadge>
      </header>
      {def.help && <p className="muted consult-help">{def.help}</p>}

      {q.status === "stale" && (
        <div className="note-strip"><CircleAlert size={15} aria-hidden="true" />
          <span>What this answer rests on changed since it was given: {q.changed.map((c) => `${c.description}: ${changeText(c.before, c.after)}`).join("; ")}. Confirm it, or change it.</span>
        </div>
      )}

      <Field def={def} q={q} draft={draft} setDraft={setDraft} disabled={working} />
      {preview && <PreviewLine preview={preview} />}

      {q.recommendation && (
        <p className="faint consult-rec"><Lightbulb size={13} aria-hidden="true" /> Recommended: <strong>{labelOf(def, q.recommendation.value)}</strong>. {q.recommendation.reason}</p>
      )}
      {q.evidence.length > 0 && (
        <p className="faint" style={{ margin: 0 }}>Rests on: {q.evidence.map((e) => `${e.description} (${show(e.value)})`).join("; ")}.</p>
      )}
      {(confirmed || q.status === "skipped") && q.by && (
        <p className="faint" style={{ margin: 0 }}>
          {q.status === "skipped" ? "Skipped" : q.source ? SOURCE[q.source].charAt(0).toUpperCase() + SOURCE[q.source].slice(1) : "Answered"} by {q.by.id}{q.at ? ` on ${new Date(q.at).toLocaleString()}` : ""}{q.note ? `: ${q.note}` : ""}.
        </p>
      )}
      {explained && <p className="consult-explain" role="note"><Sparkles size={13} aria-hidden="true" /> {explained.text} <span className="faint">({explained.source === "ai" ? `from ${explained.model ?? "Claude"}` : "from the catalog"})</span></p>}

      <div className="row consult-actions">
        {(dirty || q.status === "stale" || q.status === "open") && (
          <input className="input consult-note" placeholder="Why (optional, kept with the answer)" aria-label={`Note for ${def.id}`} value={note}
            maxLength={500} onChange={(e) => setNote(e.target.value)} />
        )}
        <Button size="small" variant="primary" onClick={() => void save()} loading={busy === `answer:${def.id}`} disabled={working || !canSave}>
          {q.status === "stale" && !dirty ? "Confirm answer" : "Save answer"}
        </Button>
        {canAccept && !dirty && (
          <Button size="small" onClick={() => void answer(def.id, q.recommendation!.value)} disabled={working}>Use the recommendation</Button>
        )}
        {dirty && <Button size="small" variant="ghost" onClick={() => setDraft(current)} disabled={working}><RotateCcw size={13} aria-hidden="true" />Undo</Button>}
        {!def.required && q.status !== "skipped" && q.status !== "answered" && q.recommendation && (
          <Button size="small" variant="ghost" onClick={() => void skip(def.id)} disabled={working}>Skip</Button>
        )}
        {(q.status === "answered" || q.status === "skipped" || q.status === "stale") && (
          <Button size="small" variant="ghost" onClick={() => void withdraw(def.id)} disabled={working}>Withdraw</Button>
        )}
        <span className="spacer" />
        <Button size="small" variant="ghost" onClick={async () => setExplained(await explain(def.id))} loading={busy === `explain:${def.id}`}>Explain</Button>
      </div>
    </section>
  );
}
