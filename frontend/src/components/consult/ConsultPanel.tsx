import { ClipboardCheck, Download, FileUp, ShieldAlert, Sparkles } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { useAppState } from "../../state/AppState";
import { useConsult } from "../../state/ConsultState";
import type { BriefVersion, ConsultConflict, ConsultQuestionDef } from "../../types";
import { StatStrip } from "../shared/Metrics";
import { Banner, Button, Card, StatusBadge, Tabs, type Tone } from "../shared/Shared";
import { changeText, show } from "./format";
import { QuestionCard, SOURCE, labelOf } from "./QuestionCard";

const n = (v: number) => v.toLocaleString();
const plural = (count: number, one: string, many = `${one}s`) => `${n(count)} ${count === 1 ? one : many}`;
const VERSION_TONE: Record<BriefVersion["status"], Tone> = { submitted: "info", approved: "success", rejected: "error", superseded: "neutral" };

function save(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
/** A download: from the backend by its address, or (Demo data) made here from its text. */
function download(out: { url?: string; text?: string; fileName: string }, type: string) {
  if (out.url) {
    const a = document.createElement("a");
    a.href = out.url;
    a.download = out.fileName;
    a.click();
  } else if (out.text !== undefined) save(out.fileName, out.text, type);
}

/** An answer that contradicts the estate, or another answer: take the finding, keep the answers with a reason, or change one. */
function ConflictCard({ c, defs, onFocus }: { c: ConsultConflict; defs: Map<string, ConsultQuestionDef>; onFocus: (id: string) => void }) {
  const { resolve, busy } = useConsult();
  const [reason, setReason] = useState("");
  const [keeping, setKeeping] = useState(false);
  const take = c.resolutions.find((r) => r.kind === "take");
  const change = c.resolutions.find((r) => r.kind === "change");
  const canKeep = c.resolutions.some((r) => r.kind === "keep");
  if (c.status === "kept") {
    return (
      <li className="risk sev-low" aria-label={`Contradiction ${c.id}, kept`}>
        <div className="row" style={{ gap: 8 }}><strong className="sev">KEPT</strong><strong>{c.id}</strong></div>
        <p>{c.message} Kept by {c.by?.id ?? "a person"}: {c.reason}</p>
      </li>
    );
  }
  return (
    <li className="risk sev-high" aria-label={`Contradiction ${c.id}`}>
      <div className="row" style={{ gap: 8 }}><strong className="sev">CONTRADICTION</strong><strong>{c.id}</strong>
        <span className="faint">· {c.questions.map((q) => defs.get(q)?.id ?? q).join(", ")}</span></div>
      <p>{c.message}</p>
      {c.evidence.length > 0 && <p className="faint" style={{ margin: "4px 0 0" }}>The estate: {c.evidence.map((e) => `${e.description} (${show(e.value)})`).join("; ")}.</p>}
      <div className="row" style={{ marginTop: 8, flexWrap: "wrap" }}>
        {take && <Button size="small" variant="primary" onClick={() => void resolve(c.id, "take")} loading={busy === `resolve:${c.id}`}>{take.label}</Button>}
        {canKeep && !keeping && <Button size="small" onClick={() => setKeeping(true)}>Keep my answers</Button>}
        {change?.question && <Button size="small" variant="ghost" onClick={() => onFocus(change.question!)}>{change.label}</Button>}
        {!change && c.questions.length > 0 && <Button size="small" variant="ghost" onClick={() => onFocus(c.questions[0])}>Change an answer</Button>}
      </div>
      {keeping && (
        <div className="row" style={{ marginTop: 8 }}>
          <input className="input" style={{ flex: 1, minWidth: 220 }} aria-label={`Why ${c.id}'s answers stand`} placeholder="Why the answers stand (kept in the brief and the audit trail)"
            value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
          <Button size="small" variant="primary" disabled={!reason.trim()} onClick={() => void resolve(c.id, "keep", reason.trim())}>Keep with this reason</Button>
          <Button size="small" variant="ghost" onClick={() => setKeeping(false)}>Cancel</Button>
        </div>
      )}
    </li>
  );
}

/** Answers proposed from notes or a questionnaire: nothing is answered until a person confirms each one. */
function Proposals({ defs }: { defs: Map<string, ConsultQuestionDef> }) {
  const { consult, decideProposals, busy } = useConsult();
  const items = consult?.proposals ?? [];
  if (!items.length) return null;
  const from = { ai: "proposed by the model", rules: "read from your notes", import: "from the questionnaire" } as const;
  return (
    <Card eyebrow="Proposed answers" title={`${plural(items.length, "answer")} to confirm`}
      subtitle="Each becomes an answer only once you confirm it. It is recorded as yours, saying where it came from."
      actions={<Button size="small" variant="primary" onClick={() => void decideProposals(items.map((p) => p.id), "confirm")} loading={busy === "proposals:confirm"}>Confirm all</Button>}>
      <ul className="consult-proposals" aria-label="Proposed answers">
        {items.map((p) => (
          <li key={p.id}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <strong>{p.question}. {defs.get(p.question)?.text}</strong>
              <div>{p.display}{p.current ? <span className="faint"> (now: {p.current})</span> : null}</div>
              <div className="faint">{from[p.source]}{p.model ? ` (${p.model})` : ""}{p.reason ? `: ${p.reason}` : ""}</div>
            </div>
            <Button size="small" onClick={() => void decideProposals([p.id], "confirm")} aria-label={`Confirm ${p.question}`}>Confirm</Button>
            <Button size="small" variant="ghost" onClick={() => void decideProposals([p.id], "reject")} aria-label={`Reject ${p.question}`}>Reject</Button>
          </li>
        ))}
      </ul>
    </Card>
  );
}

type View = "person" | "all" | "answered" | "assumed";

function QuestionsTab({ focus, setFocus }: { focus: string | null; setFocus: (id: string | null) => void }) {
  const { catalog, consult, accept, busy } = useConsult();
  const [view, setView] = useState<View>("all");
  const defs = useMemo(() => new Map((catalog?.questions ?? []).map((q) => [q.id, q])), [catalog]);
  if (!catalog || !consult) return null;
  const byId = new Map(consult.questions.map((q) => [q.id, q]));
  const blocking = new Set(consult.blockers.map((b) => b.split(":")[0]));
  const live = catalog.questions.filter((d) => byId.get(d.id)?.applies);
  // What a person must do: what blocks the brief, what is to be confirmed again, and answers in a contradiction.
  // An optional question left open (a note, the timeline) needs nobody.
  const needsPerson = (d: ConsultQuestionDef) => {
    const q = byId.get(d.id)!;
    return blocking.has(d.id) || q.status === "stale" || q.conflicts.length > 0;
  };
  const keep = (d: ConsultQuestionDef) => {
    const q = byId.get(d.id)!;
    if (view === "person") return needsPerson(d);
    if (view === "answered") return q.status === "answered";
    if (view === "assumed") return q.status === "assumed" || q.status === "skipped";
    return true;
  };
  const counts = {
    person: live.filter(needsPerson).length,
    answered: live.filter((d) => byId.get(d.id)!.status === "answered").length,
    assumed: live.filter((d) => ["assumed", "skipped"].includes(byId.get(d.id)!.status)).length,
  };
  const assumedLeft = live.some((d) => ["assumed", "skipped"].includes(byId.get(d.id)!.status) && byId.get(d.id)!.recommendation);
  const focusOn = (id: string) => {
    setView("all");
    setFocus(id);
    setTimeout(() => document.getElementById(`question-${id}`)?.scrollIntoView?.({ behavior: "smooth", block: "center" }), 50);
  };
  const conflicts = consult.conflicts;
  const parked = catalog.questions.filter((d) => byId.get(d.id)?.status === "parked");
  return (
    <div className="stack">
      <div className="panel-toolbar">
        <div className="tabs tabs-pill" role="tablist" aria-label="Filter questions">
          {([["all", `All (${live.length})`], ["person", `Needs a person (${counts.person})`], ["answered", `Answered (${counts.answered})`],
            ["assumed", `Assumed (${counts.assumed})`]] as [View, string][]).map(([id, label]) => (
            <button key={id} type="button" role="tab" className="tab" aria-selected={view === id} onClick={() => setView(id)}>{label}</button>
          ))}
        </div>
        <span className="spacer" />
        <Button variant="primary" size="small" onClick={() => void accept()} loading={busy === "accept:"} disabled={!assumedLeft}
          title="Every question still assumed gets its recommendation as your answer. Questions with no recommendation stay open.">
          <ClipboardCheck size={14} aria-hidden="true" />Accept all recommendations
        </Button>
      </div>
      {consult.next && <p className="faint" style={{ margin: 0 }}>Next: <button type="button" className="link" onClick={() => focusOn(consult.next!)}>{consult.next}. {defs.get(consult.next)?.text}</button></p>}
      <Proposals defs={defs} />
      {conflicts.length > 0 && (
        <ul className="risk-list" aria-label="Contradictions">
          {conflicts.map((c) => <ConflictCard key={c.id} c={c} defs={defs} onFocus={focusOn} />)}
        </ul>
      )}
      {catalog.sections.map((section) => {
        const inSection = catalog.questions.filter((d) => d.section === section.id && byId.get(d.id)?.applies && keep(d));
        if (!inSection.length) return null;
        const open = inSection.some((d) => ["assumed", "skipped"].includes(byId.get(d.id)!.status) && byId.get(d.id)!.recommendation);
        return (
          <section key={section.id} className="stack consult-section" aria-label={section.title}>
            <div className="row">
              <h3 className="eyebrow" style={{ margin: 0, flex: 1 }}>{section.title}</h3>
              {open && <Button size="small" variant="ghost" onClick={() => void accept(section.id)} loading={busy === `accept:${section.id}`}>Accept this section</Button>}
            </div>
            {inSection.map((d) => <QuestionCard key={d.id} def={d} q={byId.get(d.id)!} focused={focus === d.id} />)}
          </section>
        );
      })}
      {parked.length > 0 && view === "all" && (
        <p className="faint">Parked: {parked.map((d) => `${d.id} (${labelOf(d, byId.get(d.id)!.value)})`).join(", ")}. Answered on a branch that no longer applies: kept, and back if it applies again.</p>
      )}
    </div>
  );
}

function BriefTab() {
  const { api } = useAppState();
  const { brief, consult, submit, decide, busy, catalog } = useConsult();
  const [note, setNote] = useState("");
  const [comment, setComment] = useState("");
  if (!brief || !consult) return null;
  const draft = brief.draft;
  const pending = brief.versions.find((v) => v.status === "submitted");
  const sections = catalog?.sections ?? [];
  const exportVersion = async (version: number, format: "md" | "json") => download(await api.exportBrief(version, format), format === "md" ? "text/markdown" : "application/json");
  return (
    <div className="stack">
      {pending && (
        <Banner tone="warning" title={`Version ${pending.version} waits for a person to approve it`}
          actions={<>
            <Button variant="primary" size="small" onClick={() => void decide(pending.version, "approve", comment.trim() || undefined)} loading={busy === "decide:approve"}>Approve version {pending.version}</Button>
            <Button size="small" onClick={() => void decide(pending.version, "reject", comment.trim() || undefined)} loading={busy === "decide:reject"}>Reject version {pending.version}</Button>
          </>}>
          <p style={{ margin: "0 0 6px" }}>Submitted by {pending.submittedBy.id} on {new Date(pending.submittedAt).toLocaleString()}{pending.note ? `: ${pending.note}` : ""}.
            Approving makes it the policy every run is checked against{brief.summary.approved ? `, in place of version ${brief.summary.approved}` : ""}.</p>
          {pending.separateApprover.length > 0 && <p className="muted" style={{ margin: "0 0 6px" }}><ShieldAlert size={13} aria-hidden="true" /> {pending.separateApprover.join(", ")} loosen{pending.separateApprover.length === 1 ? "s" : ""} a safeguard: best approved by someone other than {pending.submittedBy.id}. Who approves is recorded either way.</p>}
          <input className="input" aria-label="Approval comment" placeholder="Comment (optional, kept with the decision)" value={comment} maxLength={500} onChange={(e) => setComment(e.target.value)} />
        </Banner>
      )}
      <Card eyebrow="The brief as the answers make it now" title={brief.summary.sameAsApproved ? `The same as version ${brief.summary.approved}, in force` : "Draft"}
        subtitle={`${plural(draft.decisions.length, "decision")}, ${plural(draft.assumptions.length, "assumption")}: recommendations nobody confirmed are marked.`}
        actions={<div className="row" style={{ gap: 6 }}>
          <input className="input" style={{ width: 220 }} aria-label="Submission note" placeholder="Note (optional)" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
          <Button variant="primary" onClick={async () => { if (await submit(note.trim() || undefined)) setNote(""); }} disabled={!brief.submittable} loading={busy === "submit"}
            title={brief.submittable ? "A new, numbered version a person then approves" : brief.blockers.join(" ")}>Submit the brief</Button>
        </div>}>
        {!brief.submittable && (
          <Banner tone="warning" title={`Not ready to submit: ${plural(brief.blockers.length, "item")} need${brief.blockers.length === 1 ? "s" : ""} a person`}>
            <ul className="reset-list" style={{ margin: 0 }}>{brief.blockers.slice(0, 8).map((b) => <li key={b}>{b}</li>)}</ul>
          </Banner>
        )}
        <div className="table-wrap">
          <table className="data">
            <caption className="sr-only">Decisions in the brief</caption>
            <thead><tr>{["Question", "Decision", "Who", "Why", "Acted on"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr></thead>
            <tbody>
              {sections.flatMap((s) => draft.decisions.filter((d) => d.section === s.id)).map((d) => (
                <tr key={d.question}>
                  <td style={{ whiteSpace: "normal", minWidth: 220 }}><span className="consult-id">{d.question}</span> {d.text}</td>
                  <td style={{ whiteSpace: "normal" }}><strong>{d.display}</strong></td>
                  <td>{d.assumed ? <StatusBadge tone="neutral">Assumed</StatusBadge> : <span>{d.source ? SOURCE[d.source] : "answered"}{d.by ? ` · ${d.by.id}` : ""}</span>}</td>
                  <td className="muted" style={{ whiteSpace: "normal", maxWidth: 360 }}>{d.reason ?? "—"}</td>
                  <td className="faint" style={{ whiteSpace: "normal", maxWidth: 260 }}>{d.drives.map((x) => `${x.applies}${x.status === "enforced" ? "" : ` (${x.status})`}`).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {draft.keptContradictions.length > 0 && <p className="muted">Contradictions kept: {draft.keptContradictions.map((k) => `${k.conflict} (${k.reason})`).join("; ")}.</p>}
        {draft.recordedOnly.length > 0 && <p className="faint">Recorded, not acted on: {draft.recordedOnly.map((r) => r.setting).join(", ")}.</p>}
      </Card>
      <Card eyebrow="Versions" title={brief.versions.length ? `${plural(brief.versions.length, "version")}` : "No version submitted yet"}
        subtitle="Each submission is a new, numbered version that never changes; approving one makes it the policy in force.">
        {brief.versions.length > 0 && (
          <ul className="consult-versions" aria-label="Brief versions">
            {[...brief.versions].reverse().map((v) => (
              <li key={v.version}>
                <strong>Version {v.version}</strong>
                <StatusBadge tone={VERSION_TONE[v.status]}>{v.status.toUpperCase()}</StatusBadge>
                <span className="muted" style={{ flex: 1, minWidth: 200 }}>
                  submitted by {v.submittedBy.id} {new Date(v.submittedAt).toLocaleString()}
                  {v.decidedBy && ` · ${v.status === "rejected" ? "rejected" : "approved"} by ${v.decidedBy.id}${v.selfApproved ? " (who submitted it)" : ""}`}
                  {v.comment && `: ${v.comment}`} · {plural(v.decisions, "decision")}, {plural(v.assumptions, "assumption")} · <code>{v.hash.slice(0, 12)}</code>
                </span>
                <Button size="small" variant="ghost" onClick={() => void exportVersion(v.version, "md")} aria-label={`Download version ${v.version} as Markdown`}><Download size={13} aria-hidden="true" />Markdown</Button>
                <Button size="small" variant="ghost" onClick={() => void exportVersion(v.version, "json")} aria-label={`Download version ${v.version} as JSON`}>JSON</Button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function ChangesTab() {
  const { consult } = useConsult();
  if (!consult) return null;
  const b = consult.brief;
  if (!b.approved) return <p className="muted">No brief is approved yet, so there is nothing to compare with.</p>;
  return (
    <div className="stack">
      {b.factChanges.length > 0 ? (
        <Banner tone="warning" title={`The estate changed since version ${b.approved} was approved`}>
          <ul className="reset-list" style={{ margin: 0 }}>{b.factChanges.map((c) => <li key={c.fact}>{c.description}: {changeText(c.before, c.after)}</li>)}</ul>
          A run under a required consultation is refused until the brief is submitted again and approved.
        </Banner>
      ) : <p className="muted">The estate is as it was when version {b.approved} was approved.</p>}
      {b.changes.length > 0 ? (
        <Card title={`${plural(b.changes.length, "decision")} changed since version ${b.approved}`} subtitle="Submit the brief and have it approved for these to take effect.">
          <ul className="reset-list" style={{ margin: 0 }} aria-label="Changed decisions">
            {b.changes.map((c) => <li key={c.question}><strong>{c.question}</strong> {c.text}: {c.before ?? "—"} → {c.after ?? "—"}</li>)}
          </ul>
        </Card>
      ) : <p className="muted">The answers make the same brief as version {b.approved}.</p>}
    </div>
  );
}

function HelpTab() {
  const { api } = useAppState();
  const { consult, propose, importQuestionnaire, busy } = useConsult();
  const [text, setText] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  if (!consult) return null;
  const llm = consult.llm;
  const read = async () => {
    const out = await propose(text);
    if (out) setResult(`${plural(out.proposed?.length ?? 0, "answer")} proposed${out.rejected?.length ? `, ${plural(out.rejected.length, "line")} not usable (${out.rejected.map((r) => `${r.question}: ${r.message}`).join("; ")})` : ""}. ${out.message ?? ""}`.trim());
  };
  const exportAs = async (format: "md" | "json") => download(await api.exportQuestionnaire(format), format === "md" ? "text/markdown" : "application/json");
  const importFile = async (f: File | undefined) => {
    if (!f) return;
    try {
      const parsed = JSON.parse(await f.text()) as { answers?: Record<string, unknown> };
      const out = await importQuestionnaire(parsed.answers ?? {});
      if (out) setResult(`${plural(out.proposed?.length ?? 0, "answer")} from the questionnaire to confirm${out.rejected?.length ? `; ${plural(out.rejected.length, "answer")} could not be used` : ""}.`);
    } catch {
      setResult("That file is not a questionnaire exported from here (JSON).");
    }
    if (file.current) file.current.value = "";
  };
  return (
    <div className="stack">
      <Card eyebrow="Read answers from notes" title="Paste meeting notes or an email"
        subtitle="Answers the text gives plainly are proposed for you to confirm, one by one. Nothing is answered until you do.">
        <p className="faint" style={{ margin: 0 }}>
          <Sparkles size={13} aria-hidden="true" /> {llm.allowed ? "Claude reads the notes: what you paste, the questions and counts about the estate are sent, never a name or a definition."
            : `The notes are read with the built-in rules. ${llm.reason ?? ""}`}
        </p>
        <textarea className="input consult-text" rows={5} aria-label="Meeting notes or an email" value={text} maxLength={8000}
          placeholder="We only need the sales schema, the last 3 years of data, and every run must be signed off…" onChange={(e) => setText(e.target.value)} />
        <div className="row"><Button variant="primary" onClick={() => void read()} disabled={!text.trim()} loading={busy === "propose"}>Read answers from the notes</Button></div>
        {result && <p className="muted" role="status" style={{ margin: 0 }}>{result}</p>}
      </Card>
      <Card eyebrow="Offline" title="Questionnaire" subtitle="Send the questions to someone without access, then import their answers: each is proposed for you to confirm.">
        <div className="row">
          <Button onClick={() => void exportAs("md")}><Download size={14} aria-hidden="true" />Download questionnaire (Markdown)</Button>
          <Button onClick={() => void exportAs("json")}><Download size={14} aria-hidden="true" />Download questionnaire (JSON)</Button>
          <label className="btn">
            <FileUp size={14} aria-hidden="true" />Import answers (JSON)
            <input ref={file} type="file" accept="application/json,.json" className="sr-only" aria-label="Import a filled questionnaire (JSON)"
              onChange={(e) => void importFile(e.target.files?.[0])} />
          </label>
        </div>
      </Card>
    </div>
  );
}

/** The Consult step: the questions, the brief their answers make, and its approval. */
export function ConsultPanel() {
  const { consult, loadError, actionError, clearActionError, brief, required } = useConsult();
  const [tab, setTab] = useState<"questions" | "brief" | "changes" | "help">("questions");
  const [focus, setFocus] = useState<string | null>(null);
  if (!consult) {
    return loadError ? <Banner tone="warning" title="The consultation cannot be read yet">{loadError}</Banner>
      : <p className="muted" role="status"><span className="spinner" aria-hidden="true" /> Reading the consultation…</p>;
  }
  const p = consult.progress;
  const b = consult.brief;
  return (
    <div className="stack panel-body">
      <StatStrip label="Consultation summary" items={[
        { label: "Brief", value: b.approved ? `v${b.approved}${b.stale ? " · stale" : ""}` : b.pending ? `v${b.pending} waiting` : "Not approved",
          tone: b.approved && !b.stale ? "success" : b.stale ? "warning" : undefined, hint: b.approved ? `Approved by ${b.approvedBy?.id ?? "a person"}` : "Approved by a person" },
        { label: "Answered", value: `${n(p.answered)}/${n(p.applicable)}`, hint: "By a person, or a recommendation they accepted" },
        { label: "Assumed", value: n(p.assumed), hint: "Recommendations nobody confirmed" },
        { label: "Need a person", value: n(p.needsPerson), tone: p.needsPerson ? "warning" : undefined },
        { label: "Contradictions", value: n(p.contradictions), tone: p.contradictions ? "error" : undefined },
      ]} />
      {required
        ? !b.approved && <Banner tone="info" title="A run starts only under an approved brief">
            Answer what needs a person (or accept the recommendations), submit the brief, and have it approved. Plan then starts from it, and every run is checked against it.
          </Banner>
        : !b.approved && <Banner tone="info" title="Consultation is advisory here">Runs start without an approved brief; one that is approved is still pre-set in Plan and recorded with each run.</Banner>}
      {b.stale && <Banner tone="warning" title="The estate changed since the brief was approved">See Changes since approval: confirm what changed, then submit the brief again.</Banner>}
      {actionError && <Banner tone="error" title="That did not work" actions={<Button size="small" variant="ghost" onClick={clearActionError}>Dismiss</Button>}>{actionError}</Banner>}
      <Tabs label="Consultation sections" value={tab} onChange={setTab} tabs={[
        { id: "questions", label: "Questions", count: p.applicable },
        { id: "brief", label: "Brief", count: brief?.versions.length || undefined },
        { id: "changes", label: "Changes since approval", count: b.approved ? b.changes.length + b.factChanges.length : undefined },
        { id: "help", label: "Help" },
      ]} />
      {tab === "questions" && <QuestionsTab focus={focus} setFocus={setFocus} />}
      {tab === "brief" && <BriefTab />}
      {tab === "changes" && <ChangesTab />}
      {tab === "help" && <HelpTab />}
    </div>
  );
}
