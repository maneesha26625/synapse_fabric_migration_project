import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  EyeOff,
  Info,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

/** The browser tab's title for a page, e.g. "Plan · Migration · Migration Accelerator". */
export function useDocumentTitle(title: string) {
  useEffect(() => {
    document.title = title ? `${title} · Migration Accelerator` : "Migration Accelerator";
  }, [title]);
}

/* ---- Button ---------------------------------------------------------------- */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "primary" | "ghost" | "danger";
  size?: "small" | "medium";
  loading?: boolean;
  icon?: boolean;
};

export function Button({ variant = "default", size = "medium", loading, icon, className = "", children, disabled, ...rest }: ButtonProps) {
  const cls = ["btn", variant !== "default" && variant, size === "small" && "small", icon && "icon", className].filter(Boolean).join(" ");
  return (
    <button type="button" className={cls} disabled={disabled || loading} aria-busy={loading || undefined} {...rest}>
      {loading && <span className="spinner" aria-hidden="true" />}
      {children}
    </button>
  );
}

/* ---- StatusBadge ------------------------------------------------------------ */

export type Tone = "success" | "warning" | "error" | "info" | "neutral" | "plain";
export function StatusBadge({ tone, running, children }: { tone: Tone; running?: boolean; children: ReactNode }) {
  return <span className={`badge ${tone}${running ? " running" : ""}`}>{children}</span>;
}

/* ---- Banner ----------------------------------------------------------------- */

const BANNER_ICON = { info: Info, warning: AlertTriangle, error: XCircle, success: CheckCircle2 };
export function Banner({ tone = "info", title, children, actions }: { tone?: keyof typeof BANNER_ICON; title?: string; children?: ReactNode; actions?: ReactNode }) {
  const Icon = BANNER_ICON[tone];
  return (
    <div className={`banner ${tone}`} role={tone === "error" ? "alert" : "status"}>
      <Icon size={16} aria-hidden="true" />
      <div className="stack" style={{ gap: 4, flex: 1 }}>
        {title && <strong>{title}</strong>}
        {children && <div className="muted" style={{ color: "inherit" }}>{children}</div>}
        {actions && <div className="row" style={{ marginTop: 6 }}>{actions}</div>}
      </div>
    </div>
  );
}

/* ---- States ------------------------------------------------------------------ */

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span className="muted">{label}</span>
    </div>
  );
}

export function EmptyState({ icon, title, children, actions, tone = "" }: { icon?: ReactNode; title: string; children?: ReactNode; actions?: ReactNode; tone?: "" | "success" | "info" | "warning" }) {
  return (
    <div className={`state ${tone}`}>
      {icon && <div className="icon" aria-hidden="true">{icon}</div>}
      <h2>{title}</h2>
      {children && <p>{children}</p>}
      {actions && <div className="row" style={{ justifyContent: "center", marginTop: 6 }}>{actions}</div>}
    </div>
  );
}

export function ErrorState({ title, message, actions }: { title: string; message?: string; actions?: ReactNode }) {
  return (
    <div className="state error" role="alert">
      <div className="icon" aria-hidden="true"><XCircle size={22} /></div>
      <h2>{title}</h2>
      {message && <p>{message}</p>}
      {actions && <div className="row" style={{ justifyContent: "center", marginTop: 6 }}>{actions}</div>}
    </div>
  );
}

export function Skeleton({ width = "100%", height = 14 }: { width?: number | string; height?: number }) {
  return <div className="skeleton" style={{ width, height }} aria-hidden="true" />;
}

/* ---- Page head ----------------------------------------------------------------- */

export function PageHead({ icon: Icon, title, badge, children, actions }: { icon?: LucideIcon; title: string; badge?: ReactNode; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div className="row">
        {Icon && <span className="page-ico"><Icon size={22} aria-hidden="true" /></span>}
        <h1>{title}</h1>
        {badge}
        <span className="spacer" />
        {actions}
      </div>
      {children && <p>{children}</p>}
    </div>
  );
}

/* ---- Card ---------------------------------------------------------------------- */

export function Card({ title, subtitle, actions, children, eyebrow }: { title?: string; subtitle?: string; actions?: ReactNode; children: ReactNode; eyebrow?: string }) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="card-head">
          <div style={{ flex: 1 }}>
            {eyebrow && <div className="eyebrow">{eyebrow}</div>}
            {title && <h2>{title}</h2>}
            {subtitle && <p>{subtitle}</p>}
          </div>
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

/* ---- Form controls ------------------------------------------------------------- */

type FieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: string;
  error?: string | null;
  optional?: boolean;
  full?: boolean;
  /** Masks the value and offers a reveal toggle. The value is never stored by this component. */
  secret?: boolean;
};

export function TextField({ label, hint, error, optional, full, secret, id, className = "", ...rest }: FieldProps) {
  const auto = useId();
  const inputId = id ?? auto;
  const [shown, setShown] = useState(false);
  const describedBy = [hint && `${inputId}-hint`, error && `${inputId}-err`].filter(Boolean).join(" ") || undefined;
  return (
    <div className={`field${full ? " full" : ""}`}>
      <label htmlFor={inputId}>
        {label}
        {optional && <span className="faint"> (optional)</span>}
      </label>
      <div className="input-wrap">
        <input
          id={inputId}
          className={`input ${className}`}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          type={secret ? (shown ? "text" : "password") : (rest.type ?? "text")}
          autoComplete={secret ? "new-password" : "off"}
          autoCorrect="off"
          autoCapitalize="off"
          spellCheck={false}
          {...rest}
        />
        {secret && (
          <Button variant="ghost" size="small" icon className="reveal" onClick={() => setShown((s) => !s)} aria-label={shown ? `Hide ${label}` : `Show ${label}`} aria-pressed={shown}>
            {shown ? <EyeOff size={15} /> : <Eye size={15} />}
          </Button>
        )}
      </div>
      {hint && !error && <span className="hint" id={`${inputId}-hint`}>{hint}</span>}
      {error && <span className="err" id={`${inputId}-err`}>{error}</span>}
    </div>
  );
}

type SelectFieldProps = {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
  placeholder: string;
  loading?: boolean;
  disabled?: boolean;
  optional?: boolean;
  error?: string | null;
  hint?: string;
};

/** A labelled dropdown whose options arrive asynchronously. */
export function SelectField({ label, value, onChange, options, placeholder, loading, disabled, optional, error, hint }: SelectFieldProps) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>
        {label}
        {optional && <span className="faint"> (optional)</span>}
      </label>
      <select id={id} className="select" style={{ width: "100%" }} value={value} disabled={disabled || loading} aria-invalid={error ? true : undefined} aria-busy={loading || undefined} onChange={(e) => onChange(e.target.value)}>
        <option value="">{loading ? "Loading…" : placeholder}</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
      {hint && !error && <span className="hint">{hint}</span>}
      {error && <span className="err">{error}</span>}
    </div>
  );
}

/* ---- Modal & Drawer ---------------------------------------------------------------- */

/**
 * Focus, Escape and the Tab loop for a dialog. It runs once per opening: the
 * latest ``onClose`` is read through a ref, so a parent that re-renders (a run
 * polling every second) never pulls focus back to the dialog. Focus starts on
 * an element marked ``data-autofocus``, else the first field, else the dialog.
 */
function useDialogBehaviour(onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const start = ref.current?.querySelector<HTMLElement>("[data-autofocus], input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled])");
    (start ?? ref.current)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close.current();
      if (e.key === "Tab" && ref.current) {
        const items = ref.current.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])');
        if (!items.length) return;
        const first = items[0], last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, []);
  return ref;
}

/** Dialogs render at the end of the page, so no container (a blurred bar, a scrolling panel) can clip or offset them. */
function Layer({ children }: { children: ReactNode }) {
  return typeof document === "undefined" ? <>{children}</> : createPortal(children, document.body);
}

export function Modal({ title, onClose, children, footer, onSubmit }: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  /** Makes the dialog a form: Enter in a field submits it. */
  onSubmit?: () => void;
}) {
  const ref = useDialogBehaviour(onClose);
  const titleId = useId();
  const body = (
    <>
      <div className="stack">{children}</div>
      {footer && <div className="row modal-footer">{footer}</div>}
    </>
  );
  return (
    <Layer>
      <div className="scrim modal-scrim" onClick={onClose} />
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={ref} tabIndex={-1}>
        <div className="row" style={{ marginBottom: 12 }}>
          <h2 id={titleId} style={{ flex: 1 }}>{title}</h2>
          <Button variant="ghost" size="small" icon onClick={onClose} aria-label="Close"><X size={16} /></Button>
        </div>
        {onSubmit ? <form onSubmit={(e) => { e.preventDefault(); onSubmit(); }}>{body}</form> : body}
      </div>
    </Layer>
  );
}

/**
 * Asks before something that cannot be undone. Focus starts on Cancel, so
 * Enter never confirms by accident; Escape cancels unless the action is under way.
 */
export function ConfirmDialog({ title, children, confirmLabel, danger = true, busy, error, onConfirm, onCancel }: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancel = () => { if (!busy) onCancel(); };
  return (
    <Modal title={title} onClose={cancel} footer={<>
      <Button onClick={cancel} disabled={busy} data-autofocus>Cancel</Button>
      <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} loading={busy}>{confirmLabel}</Button>
    </>}>
      {children}
      {error && <Banner tone="error" title="That did not work">{error}</Banner>}
    </Modal>
  );
}

export function Drawer({ label, onClose, header, children }: { label: string; onClose: () => void; header: ReactNode; children: ReactNode }) {
  const ref = useDialogBehaviour(onClose);
  return (
    <Layer>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={label} ref={ref} tabIndex={-1}>
        <div className="drawer-head">
          <div style={{ flex: 1, minWidth: 0 }}>{header}</div>
          <Button variant="ghost" size="small" icon onClick={onClose} aria-label="Close details"><X size={16} /></Button>
        </div>
        {children}
      </aside>
    </Layer>
  );
}

/* ---- Collapsible & JSON --------------------------------------------------------------- */

/** Children are mounted only while open, so a large JSON body costs nothing until asked for. */
export function Collapsible({ summary, children, defaultOpen = false }: { summary: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <details className="collapsible" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>{summary}</summary>
      {open && <div className="body">{children}</div>}
    </details>
  );
}

const TOKEN = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

/** Minimal JSON syntax highlighting, no dependency. Renders text nodes only, so no injection surface. */
export function JsonViewer({ value }: { value: unknown }) {
  const text = JSON.stringify(value ?? null, null, 2);
  const parts: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(TOKEN)) {
    const at = m.index ?? 0;
    if (at > last) parts.push(text.slice(last, at));
    if (m[1]) {
      parts.push(<span key={i++} className={m[2] ? "k" : "s"}>{m[1]}</span>);
      if (m[2]) parts.push(m[2]);
    } else if (m[3]) parts.push(<span key={i++} className="b">{m[0]}</span>);
    else if (m[0] === "null") parts.push(<span key={i++} className="z">null</span>);
    else parts.push(<span key={i++} className="n">{m[0]}</span>);
    last = at + m[0].length;
  }
  parts.push(text.slice(last));
  return <pre className="json" tabIndex={0} aria-label="JSON">{parts}</pre>;
}


/* ---- Tabs ------------------------------------------------------------------------- */

/** A step's sections. Only the selected one is rendered, so detail costs nothing until asked for. */
export function Tabs<T extends string>({ label, tabs, value, onChange }: {
  label: string;
  tabs: { id: T; label: string; count?: number }[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="tabs panel-tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" className="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}>
          {t.label}{t.count !== undefined && <span className="tab-count">{t.count.toLocaleString()}</span>}
        </button>
      ))}
    </div>
  );
}
