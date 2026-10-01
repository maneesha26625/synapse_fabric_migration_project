import { Check, CircleDot, Circle, XCircle, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import type { Classification } from "../../types";

/** A summary number. The value is always supplied by the caller from real data. */
export function MetricCard({ label, value, hint, onClick, active, icon: Icon, tone }: {
  label: string;
  value: ReactNode;
  hint?: string;
  onClick?: () => void;
  active?: boolean;
  icon?: LucideIcon;
  tone?: Classification | "neutral";
}) {
  const body = (
    <>
      <span className="metric-label">{Icon && <Icon size={14} aria-hidden="true" />}{label}</span>
      <span className="metric-value">{value}</span>
      {hint && <span className="metric-hint">{hint}</span>}
    </>
  );
  const cls = `metric${tone && tone !== "neutral" ? ` cls-${tone.toLowerCase().replace(" ", "-")}-edge` : ""}`;
  return onClick ? (
    <button type="button" className={cls} aria-pressed={active} onClick={onClick} title={hint}>{body}</button>
  ) : (
    <div className={cls} title={hint} role="group" aria-label={label}>{body}</div>
  );
}

/** DIRECT / RECONFIGURE / TRANSFORM / MANUAL / REVIEW / NOT SUPPORTED. Preliminary, from Discovery. */
export function ClassificationBadge({ value }: { value: Classification }) {
  return <span className={`cls cls-${value.toLowerCase().replace(" ", "-")}`}>{value}</span>;
}

export function ProgressCard({ title, percent, children, tone = "info" }: { title: string; percent: number; children?: ReactNode; tone?: "info" | "success" | "error" }) {
  const clamped = Math.max(0, Math.min(100, Math.round(percent)));
  return (
    <div className="progress-card">
      <div className="row">
        <strong>{title}</strong>
        <span className="spacer" />
        <strong style={{ fontVariantNumeric: "tabular-nums" }}>{clamped}%</strong>
      </div>
      <div className="bar" role="progressbar" aria-valuenow={clamped} aria-valuemin={0} aria-valuemax={100} aria-label={title}>
        <span className={`fill ${tone}`} style={{ width: `${clamped}%` }} />
      </div>
      {children}
    </div>
  );
}

export type StageState = "done" | "active" | "pending" | "failed";

/** A short list of stages that are different things: authenticate, test, discover. */
export function StageTracker({ stages }: { stages: { label: string; state: StageState; detail?: string }[] }) {
  return (
    <ol className="stages" aria-label="Connection stages">
      {stages.map((s, i) => (
        <li key={s.label} className={`stage ${s.state}`}>
          <span className="stage-dot" aria-hidden="true">
            {s.state === "done" ? <Check size={14} /> : s.state === "failed" ? <XCircle size={14} /> : s.state === "active" ? <CircleDot size={14} /> : <Circle size={12} />}
          </span>
          <span>
            <strong>{i + 1}. {s.label}</strong>
            {s.detail && <span className="faint" style={{ display: "block", fontSize: 13 }}>{s.detail}</span>}
          </span>
        </li>
      ))}
    </ol>
  );
}
