import { Activity, Bot, ChevronDown, ChevronRight, Cog, Pause, Play, Trash2, User, Workflow } from "lucide-react";
import { useState } from "react";
import { useActivity, type ActivityEntry } from "../../state/ActivityState";
import { Button, StatusBadge } from "../shared/Shared";

type Filter = "all" | "problems" | "agent" | "you" | "runs";
const FILTERS: { id: Filter; label: string; test: (e: ActivityEntry) => boolean }[] = [
  { id: "all", label: "All", test: () => true },
  { id: "problems", label: "Problems", test: (e) => e.level !== "info" },
  { id: "agent", label: "Agent", test: (e) => e.source === "agent" },
  { id: "you", label: "You", test: (e) => e.source === "you" },
  { id: "runs", label: "Runs & jobs", test: (e) => e.source === "run" },
];
const WHO = { you: { label: "You", icon: User }, agent: { label: "Agent", icon: Bot }, run: { label: "Run", icon: Workflow }, system: { label: "System", icon: Cog } };
const STORE = "ma.activity.open";

/** The application's activity, at the foot of every page: newest first, filtered, and held still on request. */
export function ActivityPanel() {
  const { entries, clear } = useActivity();
  const [open, setOpen] = useState(() => { try { return localStorage.getItem(STORE) !== "closed"; } catch { return true; } });
  const [filter, setFilter] = useState<Filter>("all");
  // Paused, the list stays as it was, so a line can be read while work goes on.
  const [frozen, setFrozen] = useState<ActivityEntry[] | null>(null);
  const toggle = () => { setOpen((o) => { try { localStorage.setItem(STORE, o ? "closed" : "open"); } catch { /* storage unavailable */ } return !o; }); };
  const source = frozen ?? entries;
  const shown = source.filter(FILTERS.find((f) => f.id === filter)!.test).slice().reverse().slice(0, 150);
  const errors = entries.filter((e) => e.level === "error").length;
  const latest = entries[entries.length - 1];

  return (
    <section className="card activity-panel" aria-label="Activity">
      <div className="activity-head">
        <button type="button" className="activity-toggle" aria-expanded={open} onClick={toggle}>
          {open ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
          <Activity size={15} aria-hidden="true" /><span className="eyebrow">Activity</span>
        </button>
        {errors > 0 && <StatusBadge tone="error">{errors} error{errors === 1 ? "" : "s"}</StatusBadge>}
        {!open && latest && <span className="faint activity-latest">{latest.text}</span>}
        <span className="spacer" />
        {open && (
          <div className="tabs panel-tabs" role="tablist" aria-label="Filter the activity">
            {FILTERS.map((f) => (
              <button key={f.id} type="button" role="tab" className="tab" aria-selected={filter === f.id} onClick={() => setFilter(f.id)}>{f.label}</button>
            ))}
          </div>
        )}
      </div>
      {open && (
        <>
          <div className="row" style={{ gap: 8 }}>
            <Button size="small" variant="ghost" onClick={() => setFrozen(frozen ? null : entries)} aria-pressed={!!frozen}>
              {frozen ? <Play size={14} aria-hidden="true" /> : <Pause size={14} aria-hidden="true" />}{frozen ? `Resume (${entries.length - frozen.length} new)` : "Pause"}
            </Button>
            <Button size="small" variant="ghost" onClick={() => { clear(); setFrozen(null); }} disabled={!entries.length}><Trash2 size={14} aria-hidden="true" />Clear</Button>
          </div>
          <ul className="activity-log" aria-label="Activity log">
            {shown.map((e, i) => {
              const who = WHO[e.source];
              return (
                <li key={`${e.at}-${i}`} className={e.level}>
                  <span className="activity-time">{new Date(e.at).toLocaleTimeString()}</span>
                  <span className="activity-who"><who.icon size={13} aria-hidden="true" />{who.label}</span>
                  <span className="activity-text">{e.text}</span>
                </li>
              );
            })}
            {!shown.length && <li className="faint">{entries.length ? "Nothing matches this filter." : "Nothing yet. Connections, discovery, the plan, runs, validation, the consultation and the agent are recorded here."}</li>}
          </ul>
        </>
      )}
    </section>
  );
}
