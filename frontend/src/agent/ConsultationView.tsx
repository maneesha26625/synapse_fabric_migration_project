import { ArrowLeft, ArrowRight, Bot, Check, CheckCircle2, ChevronDown, ChevronRight, Circle, Download, FileUp, Lightbulb, MessageSquare, Send, Sparkles, Upload, Wand2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useMigration } from "../state/MigrationState";
import { Banner, Button, StatusBadge } from "../components/shared/Shared";
import { briefMarkdown, type Resolved } from "./brief";
import { TOPICS, answerLabel, type AnswerValue, type Option, type Question, type TopicId } from "./catalog";
import { useConsult } from "./ConsultState";
import { useAgent } from "./OrchestratorState";

type Tab = "answer" | "review";

function download(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const SOURCE_BADGE: Record<Resolved["source"], { tone: "success" | "info" | "warning"; label: string }> = {
  person: { tone: "success", label: "Answered" },
  recommendation: { tone: "info", label: "Recommendation assumed" },
  open: { tone: "warning", label: "Needs a person" },
};

/** The three steps of the consultation, as cards: answer, review & approve, hand to the agent. */
function StepCards({ tab, onTab, onAgent }: { tab: Tab; onTab: (t: Tab) => void; onAgent: () => void }) {
  const c = useConsult();
  const { data } = useAgent();
  const done = c.counts.answered + c.counts.assumed;
  const answeredAll = c.counts.needPerson === 0;
  const agentLabel = data.status === "idle" ? (c.approved ? "Ready to start" : "Waits for the approved brief")
    : data.status === "done" ? "Finished" : data.status === "running" ? "Running" : data.status === "waiting" ? "Waiting for you" : data.status === "failed" ? "Stopped at a failure" : "Stopped";
  const cards = [
    { id: "answer" as const, n: 1, title: "Answer questions", meta: `${c.counts.answered} of ${c.counts.applicable} answered${c.counts.assumed ? `, ${c.counts.assumed} by recommendation` : ""}`, done: answeredAll && done === c.counts.applicable },
    { id: "review" as const, n: 2, title: "Review & approve", meta: c.state === "approved" ? `Version ${c.current?.version} approved` : c.state === "submitted" ? `Version ${c.current?.version} waits for approval` : c.changedSinceApproval ? "Changed since approval" : "Not submitted", done: c.state === "approved" },
    { id: "agent" as const, n: 3, title: "Agent orchestrator", meta: agentLabel, done: data.status === "done" },
  ];
  return (
    <ol className="ag-steps" aria-label="Consultation steps">
      {cards.map((k) => (
        <li key={k.id}>
          <button type="button" className={`ag-step${tab === k.id ? " selected" : ""}`} aria-current={tab === k.id ? "step" : undefined}
            onClick={() => (k.id === "agent" ? onAgent() : onTab(k.id))}>
            <span className={`ag-step-num${k.done ? " done" : ""}`} aria-hidden="true">{k.done ? <Check size={15} /> : k.n}</span>
            <span><strong>{k.title}</strong><span className="muted">{k.meta}</span></span>
          </button>
        </li>
      ))}
    </ol>
  );
}

/** The answer a question shows: a choice of options, several, or text. */
function AnswerInput({ q, options, value, onChange }: { q: Question; options: Option[]; value: AnswerValue | undefined; onChange: (v: AnswerValue) => void }) {
  if (q.kind === "text") {
    return <textarea className="input ag-text" rows={3} aria-label={q.text} placeholder={q.placeholder} value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value)} />;
  }
  if (q.kind === "multi") {
    const set = new Set(Array.isArray(value) ? value : []);
    return (
      <div className="ag-options" role="group" aria-label={q.text}>
        {options.map((o) => (
          <label key={o.value} className={`ag-option${set.has(o.value) ? " selected" : ""}`}>
            <input type="checkbox" checked={set.has(o.value)} onChange={(e) => {
              const next = new Set(set);
              if (e.target.checked) next.add(o.value); else next.delete(o.value);
              onChange(options.map((x) => x.value).filter((x) => next.has(x)));
            }} />
            <span><strong>{o.label}</strong>{o.description && <span className="muted">{o.description}</span>}</span>
          </label>
        ))}
      </div>
    );
  }
  return (
    <div className="ag-options" role="radiogroup" aria-label={q.text}>
      {options.map((o) => (
        <label key={o.value} className={`ag-option${value === o.value ? " selected" : ""}`}>
          <input type="radio" name={q.id} checked={value === o.value} onChange={() => onChange(o.value)} />
          <span><strong>{o.label}</strong>{o.description && <span className="muted">{o.description}</span>}</span>
        </label>
      ))}
    </div>
  );
}

function sameValue(a: AnswerValue | undefined, b: AnswerValue | undefined) {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/** What typed text means for the current question, without an AI model: an option it names, or a note. */
function interpret(q: Question, options: Option[], text: string): AnswerValue | null {
  if (q.kind === "text") return text;
  const t = text.toLowerCase();
  const hit = options.filter((o) => {
    const words = [o.value, o.label].map((w) => w.toLowerCase().replace(/[^a-z0-9 ]/g, " ").trim()).filter(Boolean);
    return words.some((w) => new RegExp(`(^|\\W)${w.replace(/\s+/g, "\\s+")}($|\\W)`).test(t));
  });
  if (q.kind === "multi") return hit.length ? hit.map((o) => o.value) : null;
  return hit.length === 1 ? hit[0].value : null;
}

function AnswerTab({ focus, onFocus }: { focus: string; onFocus: (id: string) => void }) {
  const c = useConsult();
  const live = c.resolved.filter((r) => r.applies);
  const idx = Math.max(0, live.findIndex((r) => r.q.id === focus));
  const r = live[idx];
  const options = useMemo(() => r?.q.options?.(c.facts) ?? [], [r, c.facts]);
  const [draft, setDraft] = useState<AnswerValue | undefined>(r?.value);
  const [openTopic, setOpenTopic] = useState<TopicId | undefined>(r?.q.topic);
  const [proposal, setProposal] = useState<AnswerValue | null>(null);
  const [text, setText] = useState("");
  useEffect(() => { setDraft(r?.value); setProposal(null); setOpenTopic(r?.q.topic); }, [r?.q.id]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!r) return <Banner tone="info" title="No questions apply yet">Run discovery: the questions follow what it finds.</Banner>;

  const topic = TOPICS.find((t) => t.id === r.q.topic)!;
  const go = (d: -1 | 1) => { const n = live[idx + d]; if (n) onFocus(n.q.id); };
  const changed = !sameValue(draft, r.source === "person" ? r.value : undefined);
  const empty = draft === undefined || (typeof draft === "string" && !draft.trim()) || (Array.isArray(draft) && !draft.length && r.q.required);
  const save = () => {
    if (empty) return;
    c.setAnswer(r.q.id, typeof draft === "string" ? draft.trim() : draft!);
    c.addNote(`${r.q.text} → ${answerLabel(r.q, c.facts, draft)}`, "note");
    go(1);
  };
  const send = () => {
    const t = text.trim();
    if (!t) return;
    c.addNote(t, "you");
    setText("");
    if (t.endsWith("?") || t === "?") {
      c.addNote(`${r.q.help}${r.rec ? ` Suggested: ${answerLabel(r.q, c.facts, r.rec.value)}. ${r.rec.reason}` : ""}`, "system");
      return;
    }
    const v = interpret(r.q, options, t);
    if (v === null) { c.addNote("Kept as a note. The AI model is off, so I match what you type to the current question's options: name one, or ask \"?\" for help.", "system"); return; }
    setProposal(v);
    c.addNote(`Did you mean "${answerLabel(r.q, c.facts, v)}"? Confirm it on the question.`, "system");
  };

  return (
    <div className="ag-answer">
      <nav className="ag-topics card" aria-label="Topics">
        <span className="eyebrow">Topics</span>
        <ul>
          {TOPICS.map((t) => {
            const items = live.filter((x) => x.q.topic === t.id);
            if (!items.length) return null;
            const complete = items.every((x) => x.source === "person" || (!x.q.required && x.source === "recommendation"));
            const answered = items.filter((x) => x.source === "person").length;
            const open = openTopic === t.id;
            return (
              <li key={t.id}>
                <button type="button" className={`ag-topic${t.id === r.q.topic ? " current" : ""}`} aria-expanded={open}
                  onClick={() => { setOpenTopic(open ? undefined : t.id); if (!open) onFocus(items[0].q.id); }}>
                  {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
                  <span className="ag-topic-title">{t.title.split(":")[0]}</span>
                  <span className="faint">{answered}/{items.length}</span>
                  {complete ? <CheckCircle2 size={15} className="tone-success" aria-label="complete" /> : <Circle size={14} className="faint" aria-hidden="true" />}
                </button>
                {open && (
                  <ul className="ag-topic-qs">
                    {items.map((x) => (
                      <li key={x.q.id}>
                        <button type="button" className={x.q.id === r.q.id ? "current" : ""} onClick={() => onFocus(x.q.id)} title={x.q.text}>
                          {x.source === "person" ? <Check size={13} className="tone-success" aria-label="answered" /> : x.source === "recommendation" ? <Lightbulb size={13} className="tone-info" aria-label="recommendation assumed" /> : <Circle size={11} className="tone-warning" aria-label="open" />}
                          <span>{x.q.text}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="stack" style={{ gap: 16, minWidth: 0 }}>
        <section className="card ag-question" aria-labelledby="ag-q-text">
          <div className="ag-q-nav">
            <Button size="small" variant="ghost" onClick={() => go(-1)} disabled={idx === 0}><ArrowLeft size={14} aria-hidden="true" />Back</Button>
            <span className="eyebrow">{topic.title} · {idx + 1} of {live.length}</span>
            <Button size="small" variant="ghost" onClick={() => go(1)} disabled={idx === live.length - 1}>Next<ArrowRight size={14} aria-hidden="true" /></Button>
          </div>
          <h3 id="ag-q-text">{r.q.text}</h3>
          <div className="row" style={{ gap: 8 }}>
            {r.q.required && <StatusBadge tone="error">Required</StatusBadge>}
            <StatusBadge tone={SOURCE_BADGE[r.source].tone}>{SOURCE_BADGE[r.source].label}</StatusBadge>
          </div>
          <p className="muted">{r.q.help}</p>

          {r.rec && (
            <div className="ag-rec">
              <Lightbulb size={16} aria-hidden="true" />
              <div style={{ flex: 1 }}>
                <strong>Recommended: {answerLabel(r.q, c.facts, r.rec.value)}</strong>
                <span className="muted">{r.rec.reason}</span>
              </div>
              {!sameValue(draft, r.rec.value) && <Button size="small" onClick={() => setDraft(r.rec!.value)}>Use it</Button>}
            </div>
          )}
          {proposal !== null && (
            <div className="ag-rec proposal">
              <MessageSquare size={16} aria-hidden="true" />
              <div style={{ flex: 1 }}><strong>From what you typed: {answerLabel(r.q, c.facts, proposal)}</strong></div>
              <Button size="small" variant="primary" onClick={() => { setDraft(proposal); setProposal(null); }}>Confirm</Button>
              <Button size="small" variant="ghost" onClick={() => setProposal(null)}><X size={14} aria-hidden="true" />Dismiss</Button>
            </div>
          )}

          <AnswerInput q={r.q} options={options} value={draft} onChange={setDraft} />

          <div className="row" style={{ gap: 8 }}>
            <Button variant="primary" onClick={save} disabled={empty || (!changed && r.source === "person")}><Check size={15} aria-hidden="true" />Save answer</Button>
            {r.source === "person" && <Button variant="ghost" onClick={() => c.clearAnswer(r.q.id)}>Clear answer</Button>}
          </div>
          <p className="faint ag-applies">Changes: {r.q.applies}</p>
        </section>

        <section className="card ag-ask" aria-labelledby="ag-ask-title">
          <div className="row" style={{ gap: 8 }}>
            <MessageSquare size={16} aria-hidden="true" />
            <h3 id="ag-ask-title" style={{ margin: 0, flex: 1 }}>Ask or describe in your own words</h3>
            <StatusBadge tone="neutral"><Sparkles size={12} aria-hidden="true" /> AI off</StatusBadge>
          </div>
          <ul className="ag-notes" aria-label="Consultation notes">
            {c.notes.slice(-6).map((n, i) => (
              <li key={`${n.at}-${i}`} className={n.kind}><span className="ag-note-kind">{n.kind === "you" ? "You" : n.kind === "system" ? "Assistant" : "Note"}</span>{n.text}</li>
            ))}
            {!c.notes.length && <li className="faint">Nothing yet. Answers and notes appear here.</li>}
          </ul>
          <div className="ag-compose">
            <textarea className="input" rows={2} aria-label="Ask or describe" placeholder='Type an option for the current question, or ask "?" for help'
              value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
            <Button variant="primary" onClick={send} disabled={!text.trim()}><Send size={14} aria-hidden="true" />Send</Button>
          </div>
        </section>
      </div>
    </div>
  );
}

function ReviewTab({ onFocus, onAgent }: { onFocus: (id: string) => void; onAgent: () => void }) {
  const c = useConsult();
  const { project } = useMigration();
  const [approver, setApprover] = useState(() => {
    const owner = c.resolved.find((r) => r.q.id === "context.owner")?.value;
    return typeof owner === "string" ? owner : "";
  });
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const blockers = c.counts.needPerson + c.counts.conflicts;
  const live = c.resolved.filter((r) => r.applies);

  const exportQuestionnaire = () => {
    const questions = live.map((r) => ({
      id: r.q.id, topic: TOPICS.find((t) => t.id === r.q.topic)?.title, question: r.q.text, help: r.q.help,
      options: (r.q.options?.(c.facts) ?? []).map((o) => ({ value: o.value, label: o.label })),
      multiple: r.q.kind === "multi", recommended: r.rec?.value ?? null, why: r.rec?.reason ?? null,
      answer: r.source === "person" ? r.value : null,
    }));
    download(`questionnaire-${project.name.replace(/\W+/g, "-").toLowerCase()}.json`, JSON.stringify({ format: "migration-questionnaire/1", project: project.name, questions }, null, 2), "application/json");
  };
  const importQuestionnaire = async (f: File) => {
    try {
      const parsed = JSON.parse(await f.text()) as { questions?: { id: string; answer?: AnswerValue | null }[] };
      const answers = Object.fromEntries((parsed.questions ?? []).filter((q) => q.answer !== null && q.answer !== undefined && q.answer !== "").map((q) => [q.id, q.answer as AnswerValue]));
      const n = c.importAnswers(answers);
      setImportMsg(n ? `Imported ${n} answer${n === 1 ? "" : "s"}. Review them below before submitting.` : "The file held no answers to import.");
    } catch {
      setImportMsg("That file is not a questionnaire exported from here (JSON).");
    }
  };

  const stateLine = c.state === "approved"
    ? `Version ${c.current!.version} is approved by ${c.current!.approvedBy}. The agent orchestrator plans and runs from it.`
    : c.state === "submitted" ? `Version ${c.current!.version} is submitted and waits for approval.`
    : c.changedSinceApproval ? "The answers changed since the last approved version. Submit them again for approval." : "Not submitted yet.";

  return (
    <div className="stack" style={{ gap: 16 }}>
      <section className="card ag-brief" aria-labelledby="ag-brief-title">
        <span className="eyebrow">Migration brief</span>
        <h3 id="ag-brief-title" className="row" style={{ gap: 8, margin: 0 }}>
          {c.state === "approved" ? <CheckCircle2 size={18} className="tone-success" aria-hidden="true" /> : <Circle size={16} className="faint" aria-hidden="true" />}{stateLine}
        </h3>

        <div className="ag-tiles">
          <div className="ag-tile"><strong className="tone-success">{c.counts.answered}</strong><span>Answered</span></div>
          <div className="ag-tile"><strong className="tone-info">{c.counts.assumed}</strong><span>Recommendation assumed</span></div>
          <div className="ag-tile"><strong className={c.counts.needPerson ? "tone-warning" : "tone-success"}>{c.counts.needPerson}</strong><span>Need a person</span></div>
          <div className="ag-tile"><strong className={c.counts.conflicts ? "tone-error" : "tone-success"}>{c.counts.conflicts}</strong><span>Conflicts</span></div>
        </div>

        {c.conflicts.map((x) => <Banner key={x} tone="error" title="Conflict">{x}</Banner>)}
        {c.counts.needPerson > 0 && (
          <Banner tone="warning" title={`${c.counts.needPerson} question${c.counts.needPerson === 1 ? "" : "s"} only a person can answer`}
            actions={<Button size="small" onClick={() => onFocus(live.find((r) => r.q.required && r.source === "open")!.q.id)}>Answer it</Button>}>
            Required questions without a recommendation must be answered before the brief can be submitted.
          </Banner>
        )}

        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {c.state === "draft" && <Button variant="primary" onClick={c.submit} disabled={blockers > 0} title={blockers ? "Answer the open questions and resolve the conflicts first" : undefined}>Submit for approval</Button>}
          {c.state === "submitted" && (
            <>
              <input className="input ag-approver" aria-label="Approved by" placeholder="Approved by (name)" value={approver} onChange={(e) => setApprover(e.target.value)} />
              <Button variant="primary" onClick={() => c.approve(approver)}><Check size={15} aria-hidden="true" />Approve version {c.current!.version}</Button>
            </>
          )}
        </div>
        {c.counts.assumed > 0 && c.state === "draft" && (
          <p className="faint" style={{ margin: 0 }}>Recommendations are used where nothing was answered. Accepting them records them as answers.</p>
        )}

        <div className="ag-links">
          <button type="button" className="link" onClick={() => c.acceptRecommendations()} disabled={!c.counts.assumed}><Wand2 size={14} aria-hidden="true" />Accept remaining recommendations</button>
          <button type="button" className="link" onClick={() => download(`migration-brief-${project.name.replace(/\W+/g, "-").toLowerCase()}.md`, briefMarkdown(c.facts, c.resolved, { project: project.name, version: c.current?.version ?? null, status: c.state }), "text/markdown")}>
            <Download size={14} aria-hidden="true" />Download brief
          </button>
          <button type="button" className="link" onClick={exportQuestionnaire}><Upload size={14} aria-hidden="true" />Questionnaire for the client</button>
          <button type="button" className="link" onClick={() => file.current?.click()}><FileUp size={14} aria-hidden="true" />Import filled questionnaire</button>
          <input ref={file} type="file" accept=".json,application/json" hidden aria-label="Import filled questionnaire"
            onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void importQuestionnaire(f); }} />
        </div>
        {importMsg && <p className="muted" role="status" style={{ margin: 0 }}>{importMsg}</p>}
      </section>

      {TOPICS.map((t) => {
        const items = live.filter((r) => r.q.topic === t.id);
        if (!items.length) return null;
        const answered = items.filter((r) => r.source === "person").length;
        const isOpen = open.has(t.id);
        const complete = items.every((r) => r.source !== "open" || !r.q.required);
        return (
          <section key={t.id} className="card ag-topic-card">
            <button type="button" className="ag-topic-head" aria-expanded={isOpen}
              onClick={() => setOpen((o) => { const n = new Set(o); if (n.has(t.id)) n.delete(t.id); else n.add(t.id); return n; })}>
              {isOpen ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
              <strong>{t.title}</strong>
              <span className="spacer" />
              <span className="faint">{answered}/{items.length} answered</span>
              {complete ? <CheckCircle2 size={16} className="tone-success" aria-label="complete" /> : <Circle size={15} className="tone-warning" aria-label="incomplete" />}
            </button>
            {isOpen && (
              <dl className="ag-answers">
                {items.map((r) => (
                  <div key={r.q.id}>
                    <dt>{r.q.text}</dt>
                    <dd>
                      <span>{answerLabel(r.q, c.facts, r.value)}</span>
                      <StatusBadge tone={SOURCE_BADGE[r.source].tone}>{SOURCE_BADGE[r.source].label}</StatusBadge>
                      <button type="button" className="link" onClick={() => onFocus(r.q.id)}>Change</button>
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </section>
        );
      })}

      {c.state === "approved" && (
        <Banner tone="success" title={`Brief v${c.current!.version} is approved.`} actions={<Button variant="primary" size="small" onClick={onAgent}>Continue<ArrowRight size={14} aria-hidden="true" /></Button>}>
          Next: the agent orchestrator plans, runs and validates the migration from it.
        </Banner>
      )}
    </div>
  );
}

/** Agree with the client how the migration is done, question by question, before the agent acts. */
export function ConsultationView({ onAgent, onClose }: { onAgent: () => void; onClose: () => void }) {
  const c = useConsult();
  const [tab, setTab] = useState<Tab>("answer");
  const live = c.resolved.filter((r) => r.applies);
  // Start at the first question a person has not answered yet, so the questions are met in order.
  const [focus, setFocus] = useState<string>(() => (live.find((r) => r.source !== "person") ?? live[0])?.q.id ?? "");
  const show = (id: string) => { setFocus(id); setTab("answer"); };
  return (
    <section className="ag-view" aria-labelledby="ag-consult-title">
      <header className="ag-head">
        <span className="step-head-icon" aria-hidden="true"><MessageSquare size={20} /></span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <span className="eyebrow">Agent mode · preview</span>
          <h2 id="ag-consult-title">Consultation</h2>
          <p className="muted">Agree with the client how the migration is done. Every question comes with a recommendation based on what discovery found{c.facts.total ? ` (${c.facts.total.toLocaleString()} objects)` : ""}. The approved answers drive the agent orchestrator.</p>
        </div>
        <Button size="small" variant="ghost" onClick={onClose}><X size={14} aria-hidden="true" />Back to the steps</Button>
      </header>
      {!c.facts.discovered && <Banner tone="warning" title="Discovery has not finished">The questions and recommendations follow what discovery finds. Run Discover first.</Banner>}
      <StepCards tab={tab} onTab={setTab} onAgent={onAgent} />
      {tab === "answer" ? <AnswerTab focus={focus} onFocus={setFocus} /> : <ReviewTab onFocus={show} onAgent={onAgent} />}
      {tab === "answer" && (
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <Button variant="primary" onClick={() => setTab("review")}><Bot size={15} aria-hidden="true" />Review the brief<ArrowRight size={14} aria-hidden="true" /></Button>
        </div>
      )}
    </section>
  );
}
