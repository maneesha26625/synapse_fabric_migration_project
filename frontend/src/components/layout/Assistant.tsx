import { AtSign, Bot, Lightbulb, Paperclip, Plus, SendHorizontal, Sparkles, Wrench, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import { STEPS } from "../journey/steps";

const SUGGESTIONS = [
  { icon: Wrench, text: "Why did this object fail, and how do I fix it?" },
  { icon: Lightbulb, text: "Rewrite this T-SQL so Fabric Warehouse accepts it" },
  { icon: Bot, text: "What still needs to be done by hand?" },
];

/** The round "+" that opens the assistant. */
export function AssistantLauncher({ onOpen }: { onOpen: () => void }) {
  return (
    <button type="button" className="ai-fab" onClick={onOpen} aria-label="Open the migration assistant (Ctrl+I)" title="Migration assistant · Ctrl+I">
      <span className="ai-fab-glow" aria-hidden="true" />
      <Sparkles size={20} aria-hidden="true" />
      <span className="ai-fab-plus" aria-hidden="true"><Plus size={11} strokeWidth={3} /></span>
      <span className="ai-fab-label">Ask AI</span>
    </button>
  );
}

/**
 * The migration assistant, docked on the right like an editor's chat. It will
 * read the failing object, its error and definition, and suggest a fix to
 * review and apply without leaving the screen. Preview only: nothing is sent.
 */
export function AssistantPanel({ onClose }: { onClose: () => void }) {
  const { pathname } = useLocation();
  const [params] = useSearchParams();
  const { connection } = useAppState();
  const { execution, fabric } = useMigration();
  const ref = useRef<HTMLElement>(null);
  useEffect(() => { ref.current?.focus(); }, []);

  const step = pathname.startsWith("/migration") ? STEPS.find((s) => s.key === params.get("step"))?.title ?? "Migration" : "Connections";
  const failed = execution.items.filter((i) => i.status === "FAILED");
  const context = [
    `Step: ${step}`,
    connection.workspace && `Source: ${connection.workspace}`,
    fabric.workspaceName && `Target: ${fabric.workspaceName}`,
    failed.length > 0 && `${failed.length} failed object${failed.length === 1 ? "" : "s"}`,
  ].filter(Boolean) as string[];

  return (
    <aside className="ai-panel" aria-label="Migration assistant" ref={ref} tabIndex={-1} onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <header className="ai-head">
        <span className="ai-avatar" aria-hidden="true"><Sparkles size={16} /></span>
        <div className="ai-title"><strong>Migration Assistant</strong><span className="ai-badge">Preview</span></div>
        <span className="spacer" />
        <button type="button" className="btn ghost icon small" onClick={onClose} aria-label="Close the assistant"><X size={16} /></button>
      </header>

      <div className="ai-context" aria-label="What the assistant will use">
        {context.map((c) => <span key={c} className="ai-chip"><AtSign size={11} aria-hidden="true" />{c}</span>)}
      </div>

      <div className="ai-thread">
        <div className="ai-msg">
          <span className="ai-avatar small" aria-hidden="true"><Sparkles size={13} /></span>
          <div className="ai-bubble">
            <p><strong>Fix migration problems without leaving this screen.</strong></p>
            <p>When it is switched on, the assistant will:</p>
            <ul>
              <li>explain why an object failed, from its error and its definition;</li>
              <li>suggest a fix, such as T-SQL a Fabric Warehouse accepts, for you to review;</li>
              <li>apply the fix you approve and retry just that object.</li>
            </ul>
            <p className="faint">It is being built for a later release. Nothing you type here is sent anywhere yet.</p>
          </div>
        </div>

        {failed.length > 0 && (
          <div className="ai-failures">
            <span className="eyebrow">It will help with</span>
            {failed.slice(0, 3).map((f) => (
              <div key={f.id} className="ai-failure">
                <strong title={f.name}>{f.name}</strong>
                <span className="faint">{f.error}</span>
              </div>
            ))}
            {failed.length > 3 && <span className="faint">and {failed.length - 3} more</span>}
          </div>
        )}

        <div className="ai-suggestions" aria-label="Suggested questions">
          {SUGGESTIONS.map(({ icon: Icon, text }) => (
            <button key={text} type="button" className="ai-suggestion" aria-disabled="true" title="Available in a later release">
              <Icon size={14} aria-hidden="true" />{text}
            </button>
          ))}
        </div>
      </div>

      <form className="ai-composer" onSubmit={(e) => e.preventDefault()}>
        <label className="sr-only" htmlFor="ai-input">Message the assistant</label>
        <textarea id="ai-input" rows={3} disabled placeholder="Ask about an error, an object or a step…" />
        <div className="ai-composer-row">
          <button type="button" className="btn ghost icon small" disabled aria-label="Attach an object"><Paperclip size={15} /></button>
          <span className="faint">Coming in a later release</span>
          <span className="spacer" />
          <button type="submit" className="btn primary icon small" disabled aria-label="Send"><SendHorizontal size={15} /></button>
        </div>
      </form>
    </aside>
  );
}
