import { AlertTriangle, CheckCircle2, Eye, RotateCcw, Sparkles, Trash2, XCircle } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useAppState } from "../../state/AppState";
import { ApiRequestError, type ObjectDetail, type TransformCheck, type TransformObject, type TransformSettings } from "../../types";
import { Banner, Button, Collapsible, ConfirmDialog, Skeleton, StatusBadge, Tabs, type Tone } from "../shared/Shared";

/** Synapse types a draft can be generated for; the server's settings have the final say. */
export const TRANSFORMABLE_TYPES = new Set(["Stored Procedure", "View", "Table", "SQL Script", "Pipeline", "Notebook", "Function"]);

const POLL_MS = 2000;
type Pane = "draft" | "source" | "rules";

const STATE_BADGE: Record<TransformObject["state"], { tone: Tone; label: string; running?: boolean }> = {
  none: { tone: "plain", label: "Rule-based" },
  queued: { tone: "info", label: "Queued", running: true },
  running: { tone: "info", label: "Claude is converting…", running: true },
  failed: { tone: "error", label: "Draft failed" },
  draft: { tone: "warning", label: "Draft: review needed" },
  accepted: { tone: "success", label: "Accepted: used by the next run" },
};
const CONFIDENCE_TONE = { high: "success", medium: "warning", low: "error" } as const;
const fmt = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "—");

function Code({ text, label, maxHeight = 360 }: { text: string; label: string; maxHeight?: number }) {
  return <pre className="json" style={{ maxHeight, whiteSpace: "pre" }} tabIndex={0} aria-label={label}>{text}</pre>;
}

function Checks({ checks }: { checks: TransformCheck[] }) {
  return (
    <ul className="checklist">
      {checks.map((c, i) => (
        <li key={i} className={c.level === "ok" ? "ok" : "warn"}>
          {c.level === "ok" ? <CheckCircle2 size={16} aria-label="passed" />
            : c.level === "error" ? <XCircle size={16} aria-label="error" style={{ color: "var(--error)" }} />
              : <AlertTriangle size={16} aria-label="warning" />}
          {c.message}
        </li>
      ))}
    </ul>
  );
}

function List({ title, items, tone }: { title: string; items: string[]; tone?: "warning" }) {
  if (!items.length) return null;
  const body = <ul style={{ margin: 0, paddingLeft: 18 }}>{items.map((n, i) => <li key={i}>{n}</li>)}</ul>;
  return tone ? <Banner tone={tone} title={title}>{body}</Banner> : <div className="stack" style={{ gap: 6 }}><strong>{title}</strong>{body}</div>;
}

function Section({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="stack" style={{ gap: 10 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 className="eyebrow" style={{ margin: 0 }}>{title}</h3>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function TransformPanel({ object }: { object: ObjectDetail }) {
  const { api } = useAppState();
  const [settings, setSettings] = useState<TransformSettings | null>(null);
  const [t, setT] = useState<TransformObject | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"generate" | "accept" | "discard" | null>(null);
  const [edited, setEdited] = useState<string | null>(null);
  const [pane, setPane] = useState<Pane>("draft");
  const [prompt, setPrompt] = useState<string | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const lastDraft = useRef<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.getTransform(object.id);
      setT(next);
      setError(null);
      // A new draft replaces any unsaved edits of the previous one.
      if (next.draft?.generatedAt !== lastDraft.current) {
        lastDraft.current = next.draft?.generatedAt ?? null;
        setEdited(null);
      }
    } catch (e) {
      if (e instanceof ApiRequestError && e.code === "no_results") {
        setError("The API has no discovery results (it was restarted, or discovery hasn't run). Connect the source and run Discover again.");
      } else if (e instanceof ApiRequestError && e.code === "not_found") {
        setError("The API running on this machine is older than this page and has no LLM transform endpoints. Restart it (stop it, then run .\\start-ui.ps1 again).");
      } else {
        setError(e instanceof Error ? e.message : "The transformation state could not be loaded.");
      }
    }
  }, [api, object.id]);

  useEffect(() => {
    let cancelled = false;
    setT(null); setPrompt(null); setEdited(null); setPane("draft"); lastDraft.current = null;
    api.getTransformSettings().then((s) => !cancelled && setSettings(s), () => !cancelled && setSettings(null));
    void load();
    return () => { cancelled = true; };
  }, [api, load]);

  const working = t?.state === "queued" || t?.state === "running";
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [working, load]);

  const act = async (what: "generate" | "accept" | "discard", fn: () => Promise<unknown>) => {
    setBusy(what); setActionError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "The request failed.");
    } finally {
      setBusy(null);
    }
  };

  const generate = () => act("generate", async () => {
    const [queued] = await api.generateTransforms([object.id]);
    if (queued?.state === "rejected") throw new Error(queued.error ?? "This object cannot be sent for a draft.");
    setPane("draft");
  });
  const showPrompt = async () => {
    if (prompt !== null) { setPrompt(null); return; }
    try {
      setPrompt((await api.getTransformPrompt(object.id)).prompt);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "The prompt could not be built.");
    }
  };

  if (error) return <Banner tone="error" title="Could not load the transformation" actions={<Button size="small" onClick={() => void load()}>Retry</Button>}>{error}</Banner>;
  if (!t || !t.source) return <div className="stack"><Skeleton width="50%" height={18} /><Skeleton /><Skeleton width="80%" /></div>;

  const source = t.source;
  const canGenerate = !!settings?.enabled && !!settings?.available && settings?.consent?.allowed !== false;
  const badge = STATE_BADGE[t.state];
  const draft = t.draft;
  const code = edited ?? draft?.code ?? "";
  const isEdited = draft !== null && edited !== null && edited.trim() !== draft.code.trim();

  return (
    <div className="stack" style={{ gap: 24 }}>
      <Banner tone="info" title="Convert with an LLM (Claude, through Claude Code)">
        This sends the object's definition, its rule-based conversion and your project requirements
        (<code>prompts/transform/project.md</code>) to Claude, using the Claude Code CLI on the machine running the API.
        A draft is never deployed as it is: you review it here, and only what you accept is used by the next migration run.
      </Banner>
      {settings && !settings.enabled && <Banner tone="warning" title="Generation is switched off">{settings.disabledReason}</Banner>}
      {settings?.enabled && settings.consent && !settings.consent.allowed && <Banner tone="warning" title="The migration brief does not allow AI drafts now">{settings.consent.reason}</Banner>}
      {settings && settings.enabled && !settings.available && <Banner tone="warning" title="Claude Code is not available">{settings.message}</Banner>}

      <div className="row" style={{ justifyContent: "space-between" }}>
        <StatusBadge tone={badge.tone} running={badge.running}>{badge.label}</StatusBadge>
        <div className="row">
          <Button size="small" onClick={() => void showPrompt()}><Eye size={14} aria-hidden="true" />{prompt !== null ? "Hide prompt" : "Preview prompt"}</Button>
          <Button size="small" variant="primary" loading={busy === "generate"} disabled={!canGenerate || working}
            title={canGenerate ? undefined : settings?.disabledReason ?? settings?.message ?? undefined} onClick={() => void generate()}>
            <Sparkles size={14} aria-hidden="true" />{draft || t.accepted ? "Regenerate with LLM" : "Generate with LLM"}
          </Button>
        </div>
      </div>
      {working && <p className="muted" role="status">Claude is converting {object.name}. This usually takes 20–90 seconds; you can close this panel and come back.</p>}
      {t.state === "failed" && t.error && <Banner tone="error" title="The draft could not be generated">{t.error}</Banner>}
      {actionError && <Banner tone="error" title="That did not work">{actionError}</Banner>}
      {prompt !== null && (
        <Section title={`Prompt sent to the model (${prompt.length.toLocaleString()} characters)`}>
          <Code text={prompt} label="Prompt" maxHeight={420} />
          <p className="faint">Built from <code>{settings?.promptsDir ?? "prompts/transform"}</code>. Edit those Markdown files to change what every prompt says.</p>
        </Section>
      )}

      {source.problems.length > 0 && <List title="Why the rules cannot convert it alone" items={source.problems} tone="warning" />}

      <Section title="Compare">
        <Tabs<Pane> label="Code to compare" value={pane} onChange={setPane} tabs={[
          { id: "draft", label: draft ? (isEdited ? "LLM draft (edited)" : "LLM draft") : t.accepted ? "Accepted" : "LLM draft" },
          { id: "source", label: "Synapse source" },
          { id: "rules", label: "Rule-based output" },
        ]} />
        {pane === "source" && <Code text={source.code} label="Synapse source" />}
        {pane === "rules" && (source.ruleOutput
          ? <><Code text={source.ruleOutput} label="Rule-based output" /><List title="What the rules changed" items={source.ruleNotes} /></>
          : <p className="muted">{source.problems.length ? "The rules cannot produce a working version of this object on their own." : "The rules make no change to this object; it is created as it is."}</p>)}
        {pane === "draft" && (draft ? (
          <div className="stack">
            <textarea aria-label="LLM draft (editable)" className="json" spellCheck={false} value={code}
              onChange={(e) => setEdited(e.target.value)} style={{ width: "100%", minHeight: 300, resize: "vertical", color: "var(--text)" }} />
            <div className="row">
              <Button variant="primary" size="small" loading={busy === "accept"} disabled={!!draft.stale || working}
                onClick={() => void act("accept", () => api.acceptTransform(object.id, isEdited ? code : undefined))}>
                <CheckCircle2 size={14} aria-hidden="true" />{isEdited ? "Accept edited version" : "Accept draft"}
              </Button>
              {isEdited && <Button size="small" onClick={() => setEdited(null)}><RotateCcw size={14} aria-hidden="true" />Undo edits</Button>}
              <Button size="small" loading={busy === "discard"} disabled={working} onClick={() => void act("discard", () => api.discardTransform(object.id, "draft"))}>
                <Trash2 size={14} aria-hidden="true" />Discard draft
              </Button>
            </div>
            {draft.stale && <Banner tone="warning" title="The source changed">This draft was made from an older definition. Generate a new one.</Banner>}
          </div>
        ) : t.accepted ? (
          <Code text={t.accepted.code} label="Accepted transform" />
        ) : (
          <p className="muted">No draft yet. Choose <strong>Generate with LLM</strong>. Until you accept one, the run uses the rule-based output.</p>
        ))}
      </Section>

      {draft && pane === "draft" && (
        <Section title="Review the draft">
          <div className="row">
            <StatusBadge tone={CONFIDENCE_TONE[draft.confidence]}>Model confidence: {draft.confidence}</StatusBadge>
            <span className="faint">{draft.model || "model"} · {fmt(draft.generatedAt)} · {(draft.durationMs / 1000).toFixed(0)} s{draft.costUsd != null ? ` · $${draft.costUsd.toFixed(3)}` : ""}</span>
          </div>
          <Checks checks={draft.checks} />
          <List title="Left for a person" items={draft.unconverted} tone="warning" />
          <List title="What the model changed" items={draft.notes} />
        </Section>
      )}

      {t.accepted && (
        <Section title="Accepted" actions={
          <Button size="small" variant="danger" disabled={working} onClick={() => setConfirmReset(true)}><RotateCcw size={14} aria-hidden="true" />Go back to the rules</Button>
        }>
          {t.accepted.stale
            ? <Banner tone="warning" title="Out of date">The source changed after this was accepted, so the next run uses the rule-based output instead. Generate and accept a new draft.</Banner>
            : <p className="muted">The next migration run creates this object from the accepted code{t.accepted.edited ? " (edited by the reviewer)" : ""}, accepted {fmt(t.accepted.acceptedAt)}.</p>}
          {draft && <Collapsible summary="Show the accepted code"><Code text={t.accepted.code} label="Accepted transform" /></Collapsible>}
          <List title="Left for a person" items={t.accepted.unconverted} tone="warning" />
        </Section>
      )}

      {confirmReset && (
        <ConfirmDialog title="Go back to the rule-based conversion?" confirmLabel="Go back to the rules" busy={busy === "discard"}
          onCancel={() => setConfirmReset(false)}
          onConfirm={() => { void act("discard", () => api.discardTransform(object.id, "all")).then(() => setConfirmReset(false)); }}>
          The accepted transform and any draft for {object.name} are deleted. The next run uses the rule-based output.
        </ConfirmDialog>
      )}
    </div>
  );
}
