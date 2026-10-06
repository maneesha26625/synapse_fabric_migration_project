import { Lock, type LucideIcon } from "lucide-react";

export interface MethodOption<T extends string> {
  id: T;
  title: string;
  desc: string;
  icon: LucideIcon;
  /** Shown, but not selectable yet. */
  soon?: boolean;
}

/** How to connect: one tile per method. Methods that are not built yet are visible and dimmed. */
export function MethodTiles<T extends string>({ label, options, value, onChange, disabled }: {
  label: string;
  options: MethodOption<T>[];
  value: T;
  onChange: (id: T) => void;
  disabled?: boolean;
}) {
  return (
    <div className="method-tiles" role="radiogroup" aria-label={label}>
      {options.map(({ id, title, desc, icon: Icon, soon }) => (
        <button
          key={id}
          type="button"
          role="radio"
          aria-checked={value === id}
          aria-disabled={soon || disabled || undefined}
          className={`method${soon ? " soon" : ""}`}
          title={soon ? `${title}: coming soon` : desc}
          onClick={() => { if (!soon && !disabled) onChange(id); }}
        >
          <span className="method-icon"><Icon size={18} aria-hidden="true" /></span>
          <span className="method-title">{title}</span>
          <span className="method-desc">{desc}</span>
          {soon && <span className="soon-tag"><Lock size={11} aria-hidden="true" />Coming soon</span>}
        </button>
      ))}
    </div>
  );
}

/** "1 Sign in · 2 Choose · 3 Test": where the operator is in connecting one side. */
export function MiniSteps({ steps, current }: { steps: string[]; current: number }) {
  return (
    <ol className="mini-steps" aria-label="Connection steps">
      {steps.map((s, i) => (
        <li key={s} className={i < current ? "done" : i === current ? "active" : ""} aria-current={i === current ? "step" : undefined}>
          <span className="mini-num" aria-hidden="true">{i < current ? "✓" : i + 1}</span>{s}
        </li>
      ))}
    </ol>
  );
}
