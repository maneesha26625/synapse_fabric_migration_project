import { Check, ChevronDown, Lock } from "lucide-react";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import type { Platform } from "./platforms";

/** A platform's tile: a tinted monogram. */
export function PlatformMark({ platform, size = 40 }: { platform: Platform; size?: number }) {
  return (
    <span className="p-mark" style={{ ["--tint" as string]: platform.tint, width: size, height: size, fontSize: size * 0.36 }} aria-hidden="true">
      {platform.mark}
    </span>
  );
}

/**
 * A dropdown of platforms. Unavailable ones are listed (so the roadmap is
 * visible) but cannot be chosen: they are dimmed, announced as unavailable,
 * and skipped by Enter and click.
 */
export function PlatformPicker({ label, placeholder, platforms, value, onChange, direction }: {
  label: string;
  placeholder: string;
  platforms: Platform[];
  value: string | null;
  onChange: (id: string) => void;
  /** "from" or "to", shown above the picker. */
  direction: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const labelId = useId();
  const listId = useId();
  const selected = platforms.find((p) => p.id === value) ?? null;

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (root.current && !root.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    list.current?.focus();
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const openList = () => {
    setActive(Math.max(0, platforms.findIndex((p) => p.id === value)));
    setOpen(true);
  };
  const choose = (p: Platform) => {
    if (!p.available) return;
    onChange(p.id);
    setOpen(false);
    trigger.current?.focus();
  };
  const onListKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(platforms.length - 1, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(0, i - 1)); }
    else if (e.key === "Home") { e.preventDefault(); setActive(0); }
    else if (e.key === "End") { e.preventDefault(); setActive(platforms.length - 1); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(platforms[active]); }
    else if (e.key === "Escape" || e.key === "Tab") { setOpen(false); trigger.current?.focus(); }
  };

  return (
    <div className={`pp${open ? " open" : ""}`} ref={root}>
      <span className="pp-direction" id={labelId}>{direction} · {label}</span>
      <button
        ref={trigger}
        type="button"
        className={`pp-trigger${selected ? "" : " empty"}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-labelledby={labelId}
        onClick={() => (open ? setOpen(false) : openList())}
        onKeyDown={(e) => { if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); openList(); } }}
      >
        {selected ? (
          <>
            <PlatformMark platform={selected} />
            <span className="pp-text"><strong>{selected.name}</strong><span>{selected.caption}</span></span>
          </>
        ) : (
          <span className="pp-text"><strong>{placeholder}</strong><span>{platforms.filter((p) => p.available).length} available · {platforms.filter((p) => !p.available).length} coming soon</span></span>
        )}
        <ChevronDown size={18} className="pp-chevron" aria-hidden="true" />
      </button>
      {open && (
        <ul ref={list} id={listId} className="pp-list" role="listbox" aria-labelledby={labelId} tabIndex={-1}
          aria-activedescendant={`${listId}-${platforms[active]?.id}`} onKeyDown={onListKey}>
          {platforms.map((p, i) => (
            <li key={p.id} id={`${listId}-${p.id}`} role="option" aria-selected={p.id === value} aria-disabled={!p.available || undefined}
              className={`pp-option${i === active ? " active" : ""}${p.available ? "" : " soon"}`}
              onMouseEnter={() => setActive(i)} onClick={() => choose(p)}>
              <PlatformMark platform={p} size={34} />
              <span className="pp-text"><strong>{p.name}</strong><span>{p.caption}</span></span>
              {p.available
                ? p.id === value ? <Check size={16} className="pp-check" aria-hidden="true" /> : null
                : <span className="soon-tag"><Lock size={11} aria-hidden="true" />Coming soon</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
