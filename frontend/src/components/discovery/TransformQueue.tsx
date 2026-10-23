import { CheckCircle2, Save, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useAppState } from "../../state/AppState";
import { ApiRequestError, type ProjectRequirements, type TransformCandidate, type TransformCandidates, type TransformSettings } from "../../types";
import { Banner, Button, Collapsible, ConfirmDialog, EmptyState, Skeleton, StatusBadge, type Tone } from "../shared/Shared";
import { LlmBadge } from "./LlmBadge";
import { ObjectDetails } from "./ObjectDetails";

const POLL_MS = 3000;
const BATCH = 50;
const PAGE = 100;

type Filter = "needs" | "drafts" | "accepted" | "all";
const FILTERS: { id: Filter; label: string; test: (c: TransformCandidate) => boolean }[] = [
  { id: "needs", label: "Rules can't convert", test: (c) => c.problems.length > 0 },
  { id: "drafts", label: "Drafts to review", test: (c) => c.state === "draft" },
  { id: "accepted", label: "Accepted", test: (c) => c.state === "accepted" },
  { id: "all", label: "All", test: () => true },
];
const STATE: Record<TransformCandidate["state"], { tone: Tone; label: string; running?: boolean }> = {
  none: { tone: "plain", label: "Rule-based" },
  queued: { tone: "info", label: "Queued", running: true },
  running: { tone: "info", label: "Converting…", running: true },
  failed: { tone: "error", label: "Failed" },
  draft: { tone: "warning", label: "Draft to review" },
  accepted: { tone: "success", label: "Accepted" },
};

function ProjectEditor() {
  const { api } = useAppState();
  const [req, setReq] = useState<ProjectRequirements | null>(null);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  useEffect(() => {
    api.getProjectRequirements().then((r) => { setReq(r); setText(r.text); }, (e) => setMessage({ tone: "error", text: e instanceof Error ? e.message : "Could not load the requirements." }));
  }, [api]);

  if (!req) return message ? <Banner tone="error">{message.text}</Banner> : <Skeleton height={120} />;
  const dirty = text !== req.text;
  const save = async () => {
    setSaving(true); setMessage(null);
    try {
      const saved = await api.saveProjectRequirements(text);
      setReq(saved); setText(saved.text);
      setMessage({ tone: "success", text: "Saved. The next draft uses these requirements." });
    } catch (e) {
      setMessage({ tone: "error", text: e instanceof Error ? e.message : "The requirements could not be saved." });
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="stack">
      <p className="muted">
        Put into every prompt, under "Project requirements". They take priority over the general guidance. Saved to <code>{req.path}</code> on the
        server, so the whole team shares them. Placeholders: {req.placeholders.map((p, i) => <span key={p}>{i ? ", " : ""}<code>{`{{${p}}}`}</code></span>)}.
        Conditional text: <code>{"{{#if object.schema}} … {{/if}}"}</code>. <code>{"<!-- comments -->"}</code> are not sent.
      </p>
      <textarea aria-label="Project requirements" className="json" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)}
        style={{ width: "100%", minHeight: 220, resize: "vertical", color: "var(--text)" }} />
      <div className="row">
        <Button variant="primary" size="small" loading={saving} disabled={!dirty} onClick={() => void save()}><Save size={14} aria-hidden="true" />Save requirements</Button>
        {dirty && <Button size="small" onClick={() => setText(req.text)}>Undo changes</Button>}
      </div>
      {message && <Banner tone={message.tone}>{message.text}</Banner>}
    </div>
  );
}

/**
 * Three ways through the LLM transforms: generate drafts for many objects at once, accept many
 * reviewed drafts at once, or open each object and review its draft side by side before accepting.
 */
type Mode = "generate" | "accept" | "review";
const MODES: { id: Mode; label: string }[] = [
  { id: "generate", label: "Generate drafts" },
  { id: "accept", label: "Accept drafts" },
  { id: "review", label: "Review one by one" },
];

export function TransformQueue() {
  const { api } = useAppState();
  const [data, setData] = useState<TransformCandidates | null>(null);
  const [settings, setSettings] = useState<TransformSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setModeState] = useState<Mode>("generate");
  const [filter, setFilter] = useState<Filter>("needs");
  const [type, setType] = useState("");
  const [search, setSearch] = useState("");
  const [shown, setShown] = useState(PAGE);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.listTransformCandidates());
      setError(null);
    } catch (e) {
      if (e instanceof ApiRequestError && e.code === "no_results") {
        setError("The API has no discovery results (it was restarted, or discovery hasn't run). Connect the source and run Discover again, then come back here.");
      } else if (e instanceof ApiRequestError && e.code === "not_found") {
        setError("The API running on this machine is older than this page and has no LLM transform endpoints. Restart it (stop it, then run .\\start-ui.ps1 again).");
      } else {
        setError(e instanceof Error ? e.message : "The objects could not be loaded.");
      }
    }
  }, [api]);

  useEffect(() => {
    void load();
    api.getTransformSettings().then(setSettings, () => setSettings(null));
  }, [api, load]);
  const working = (data?.summary.working ?? 0) > 0;
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [working, load]);

  const rows = useMemo(() => {
    // Accepting works on the drafts waiting for review, whatever the filter says.
    const test = mode === "accept" ? (c: TransformCandidate) => c.state === "draft" : FILTERS.find((f) => f.id === filter)!.test;
    const q = search.trim().toLowerCase();
    return (data?.items ?? []).filter((c) => test(c) && (!type || c.type === type) && (!q || c.name.toLowerCase().includes(q)));
  }, [data, mode, filter, type, search]);
  const types = useMemo(() => [...new Set((data?.items ?? []).map((c) => c.type))].sort(), [data]);

  if (error) return <Banner tone="error" title="Could not load the objects" actions={<Button size="small" onClick={() => void load()}>Retry</Button>}>{error}</Banner>;
  if (!data) return <div className="stack"><Skeleton height={18} width="40%" /><Skeleton /><Skeleton /></div>;

  // A stale draft (the source changed since) must be generated again before it can be accepted.
  const selectable = (c: TransformCandidate) => mode === "accept" ? c.state === "draft" && !c.stale
    : !c.unavailable && c.state !== "queued" && c.state !== "running";
  const setMode = (m: Mode) => { setModeState(m); setSelected(new Set()); setNotice(null); setShown(PAGE); };
  const visible = rows.slice(0, shown);
  const allVisibleSelected = visible.filter(selectable).length > 0 && visible.filter(selectable).every((c) => selected.has(c.objectId));
  const toggle = (id: string) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const toggleAll = () => setSelected((s) => {
    const n = new Set(s);
    for (const c of visible.filter(selectable)) { if (allVisibleSelected) n.delete(c.objectId); else n.add(c.objectId); }
    return n;
  });
  // Everything the filters match, not only the rows shown on this page.
  const everything = rows.filter(selectable);
  const allSelected = everything.length > 0 && everything.every((c) => selected.has(c.objectId));
  const selectEverything = () => setSelected(allSelected ? new Set() : new Set(everything.map((c) => c.objectId)));
  const canGenerate = !!settings?.enabled && !!settings?.available;

  const acceptSelected = async () => {
    setBusy(true); setNotice(null);
    let accepted = 0;
    const failed: string[] = [];
    for (const id of selected) {
      try { await api.acceptTransform(id); accepted += 1; } catch (e) { failed.push(e instanceof Error ? e.message : id); }
    }
    setNotice(`${accepted} draft${accepted === 1 ? "" : "s"} accepted: the next migration run uses ${accepted === 1 ? "it" : "them"}.${failed.length ? ` ${failed.length} could not be accepted: ${failed.slice(0, 3).join(" ")}` : ""}`);
    setSelected(new Set());
    setBusy(false); setConfirm(false);
    await load();
  };

  const generate = async () => {
    setBusy(true); setNotice(null);
    const ids = [...selected];
    let queued = 0;
    const rejected: string[] = [];
    try {
      for (let i = 0; i < ids.length; i += BATCH) {
        for (const r of await api.generateTransforms(ids.slice(i, i + BATCH))) {
          if (r.state === "queued") queued += 1; else rejected.push(r.error ?? r.objectId);
        }
      }
      setNotice(`${queued} draft${queued === 1 ? "" : "s"} queued.${rejected.length ? ` ${rejected.length} could not be sent: ${rejected.slice(0, 3).join(" ")}` : ""}`);
      setSelected(new Set());
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "The drafts could not be queued.");
    } finally {
      setBusy(false); setConfirm(false);
      await load();
    }
  };

  const s = data.summary;
  return (
    <div className="stack" style={{ gap: 20 }}>
      <Banner tone="info" title="Convert with an LLM">
        The rules convert most objects on their own. The ones they can't are listed first. Select them and generate drafts:
        Claude (through Claude Code on the machine running the API) converts each one to your project requirements. Nothing is
        deployed until you open the object, review its draft and accept it. Accepted code is used when the object is created
        in Fabric, in <strong>step 5, Migrate</strong>. Plan and Migrate mark those objects <LlmBadge />.
      </Banner>
      {settings && !settings.enabled && <Banner tone="warning" title="Generation is switched off">{settings.disabledReason}</Banner>}
      {settings && settings.enabled && !settings.available && <Banner tone="warning" title="Claude Code is not available">{settings.message}</Banner>}

      <dl className="kv compact" aria-label="Transformation summary">
        <dt>Objects a draft can be made for</dt><dd>{s.total.toLocaleString()}</dd>
        <dt>Rules can't convert alone</dt><dd>{s.needsWork.toLocaleString()}</dd>
        <dt>Drafts waiting for review</dt><dd>{s.drafts.toLocaleString()}</dd>
        <dt>Accepted (used by the next run)</dt><dd>{s.accepted.toLocaleString()}</dd>
        {(s.working > 0 || s.failed > 0) && <><dt>Converting / failed</dt><dd>{s.working} / {s.failed}</dd></>}
      </dl>

      <Collapsible summary="Project requirements (sent with every prompt)"><ProjectEditor /></Collapsible>

      <div className="tabs panel-tabs" role="tablist" aria-label="LLM transforms">
        {MODES.map((m) => (
          <button key={m.id} type="button" role="tab" className="tab" aria-selected={mode === m.id} onClick={() => setMode(m.id)}>
            {m.label}{m.id === "accept" && <span className="tab-count">{s.drafts.toLocaleString()}</span>}
          </button>
        ))}
      </div>
      <p className="muted" style={{ margin: 0 }}>
        {mode === "generate" ? "Select the objects to convert (or every one the filters match) and generate their drafts in one go."
          : mode === "accept" ? "Every draft waiting for review. Select the ones to accept (or all of them) and accept them in one go; the next migration run uses them."
          : "Open an object to see its source, the rule-based conversion and the LLM draft side by side, edit the draft if needed, and accept it there."}
      </p>

      <div className="row">
        {mode !== "accept" && (
          <div className="tabs tabs-pill" role="tablist" aria-label="Show">
            {FILTERS.map((f) => (
              <button key={f.id} type="button" role="tab" className="tab" aria-selected={filter === f.id} onClick={() => { setFilter(f.id); setShown(PAGE); }}>
                {f.label}<span className="tab-count">{data.items.filter(f.test).length.toLocaleString()}</span>
              </button>
            ))}
          </div>
        )}
        <select className="select" aria-label="Object type" value={type} onChange={(e) => { setType(e.target.value); setShown(PAGE); }}>
          <option value="">All types</option>{types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <input className="input" type="search" aria-label="Search by name" placeholder="Search by name" value={search} onChange={(e) => { setSearch(e.target.value); setShown(PAGE); }} />
      </div>

      {mode !== "review" && (
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="muted">{selected.size ? `${selected.size} selected` : `${rows.length.toLocaleString()} ${mode === "accept" ? "draft" : "object"}${rows.length === 1 ? "" : "s"}`}</span>
          <div className="row">
            <Button size="small" onClick={selectEverything} disabled={!everything.length}>
              {allSelected ? "Clear selection" : `Select all ${everything.length.toLocaleString()}`}
            </Button>
            {mode === "generate" ? (
              <Button size="small" variant="primary" disabled={!selected.size || !canGenerate || busy} onClick={() => setConfirm(true)}>
                <Sparkles size={14} aria-hidden="true" />Generate drafts{selected.size ? ` (${selected.size})` : ""}
              </Button>
            ) : (
              <Button size="small" variant="primary" disabled={!selected.size || busy} onClick={() => setConfirm(true)}>
                <CheckCircle2 size={14} aria-hidden="true" />Accept drafts{selected.size ? ` (${selected.size})` : ""}
              </Button>
            )}
          </div>
        </div>
      )}
      {notice && <Banner tone="info">{notice}</Banner>}

      {rows.length === 0 ? (
        <EmptyState title={mode === "accept" ? "No drafts waiting for review" : filter === "needs" ? "The rules convert everything here" : "Nothing to show"}>
          {mode === "accept" ? "Generate drafts first; they appear here once Claude has written them."
            : filter === "needs" ? "No object has something the rules can't convert. You can still generate a draft for any object under All." : "Change the filters to see more."}
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="data" style={{ minWidth: 760 }}>
            <caption className="sr-only">Objects a draft can be made for</caption>
            <thead>
              <tr>
                {mode !== "review" && <th scope="col" className="static"><input type="checkbox" aria-label="Select all shown" checked={allVisibleSelected} onChange={toggleAll} /></th>}
                {["Object", "Type", "What the rules leave", "Status", "Confidence", ...(mode === "review" ? ["Review"] : [])].map((h) => <th key={h} scope="col" className="static">{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {visible.map((c) => {
                const st = STATE[c.state];
                return (
                  <tr key={c.objectId}>
                    {mode !== "review" && <td><input type="checkbox" aria-label={`Select ${c.name}`} disabled={!selectable(c)} checked={selected.has(c.objectId)} onChange={() => toggle(c.objectId)} /></td>}
                    <td className="name"><button type="button" className="link-button" onClick={() => setOpen(c.objectId)}>{c.name}</button></td>
                    <td>{c.type}</td>
                    <td title={c.unavailable ?? c.problems.join("\n")} style={{ whiteSpace: "normal", minWidth: 240 }}>
                      {c.unavailable ? <span className="faint">{c.unavailable}</span>
                        : c.problems.length ? <>{c.problems[0]}{c.problems.length > 1 && <span className="faint"> (+{c.problems.length - 1} more)</span>}</>
                          : <span className="faint">{c.rulesChange ? "Converted by the rules" : "No change needed"}</span>}
                    </td>
                    <td>
                      <StatusBadge tone={st.tone} running={st.running}>{st.label}</StatusBadge>
                      {c.stale && <span className="faint"> (source changed)</span>}
                      {c.state === "failed" && c.error && <div className="faint" style={{ fontSize: 13 }}>{c.error}</div>}
                    </td>
                    <td>{c.confidence ?? "—"}</td>
                    {mode === "review" && (
                      <td><Button size="small" onClick={() => setOpen(c.objectId)} aria-label={`Review ${c.name}`}>{c.state === "draft" ? "Review & accept" : "Open"}</Button></td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {rows.length > shown && <Button size="small" onClick={() => setShown((n) => n + PAGE)}>Show {Math.min(PAGE, rows.length - shown)} more</Button>}

      {confirm && mode === "accept" && (
        <ConfirmDialog title={`Accept ${selected.size} draft${selected.size === 1 ? "" : "s"}?`} confirmLabel="Accept drafts" danger={false} busy={busy}
          onCancel={() => setConfirm(false)} onConfirm={() => void acceptSelected()}>
          Each accepted draft replaces the rule-based conversion when its object is created in Fabric by the next migration run.
          Accept only drafts you have reviewed (Review one by one shows each draft beside its source). An accepted draft can be discarded later from its object.
        </ConfirmDialog>
      )}
      {confirm && mode === "generate" && (
        <ConfirmDialog title={`Generate ${selected.size} draft${selected.size === 1 ? "" : "s"}?`} confirmLabel="Generate drafts" danger={false} busy={busy}
          onCancel={() => setConfirm(false)} onConfirm={() => void generate()}>
          The definition of each selected object, its rule-based conversion, its dependencies' names and column types, and your project
          requirements are sent to Claude through Claude Code ({settings?.model}). Drafts are generated a few at a time and each usually takes
          20–90 seconds. Each one uses your Claude Code usage. Nothing is deployed until you accept it.
        </ConfirmDialog>
      )}
      {open && <ObjectDetails id={open} initialTab="transform" onClose={() => { setOpen(null); void load(); }} onNavigate={setOpen} />}
    </div>
  );
}
