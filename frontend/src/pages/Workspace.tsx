import { AlertTriangle, ArrowLeft, ArrowRight, Check, CheckCircle2, Circle, Download, Lock, Pause, PartyPopper, PlugZap, ServerCrash, Settings2 } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AssessPanel } from "../components/journey/panels/AssessPanel";
import { DiscoverPanel } from "../components/journey/panels/DiscoverPanel";
import { MigratePanel } from "../components/journey/panels/MigratePanel";
import { PlanPanel } from "../components/journey/panels/PlanPanel";
import { CompletionSummary, ValidatePanel } from "../components/journey/panels/ValidatePanel";
import { WavesPanel } from "../components/journey/panels/WavesPanel";
import { downloadReport } from "../components/journey/report";
import { ResetStepButton } from "../components/journey/ResetStep";
import { useJourney, type StepStatus } from "../components/journey/useJourney";
import { PlatformMark } from "../components/setup/PlatformPicker";
import { DESTINATIONS, SOURCES } from "../components/setup/platforms";
import { Banner, Button, EmptyState, useDocumentTitle } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration, type StepKey } from "../state/MigrationState";

const PANELS: Record<StepKey, () => ReactNode> = {
  discover: () => <DiscoverPanel />,
  assess: () => <AssessPanel />,
  waves: () => <WavesPanel />,
  plan: () => <PlanPanel />,
  migrate: () => <MigratePanel />,
  validate: () => <ValidatePanel />,
};

/** The badge on a step's box: one word or two for where it stands. */
function stateOf(s: StepStatus): { cls: string; label: string } {
  if (s.locked) return { cls: "locked", label: "Locked" };
  if (s.running) return { cls: "running", label: "Running" };
  if (s.paused) return { cls: "current attention", label: "Paused" };
  if (s.stale) return { cls: "current attention", label: s.key === "plan" ? "Rebuild" : "Run again" };
  if (s.confirmed) return { cls: s.attention ? "done attention" : "done", label: "Done" };
  if (s.complete) return { cls: s.attention ? "current attention" : "current", label: "Ready to confirm" };
  return { cls: s.attention ? "current attention" : "current", label: s.attention ? "Needs attention" : "To do" };
}

/** The six steps, left to right. Each box opens its step below; locked boxes open once the step before is confirmed. */
function JourneyFlow({ steps, selected, onSelect }: { steps: StepStatus[]; selected: StepKey; onSelect: (k: StepKey) => void }) {
  // On a narrow screen the strip scrolls sideways; keep the selected step in view.
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const ol = list.current;
    const box = ol?.querySelector<HTMLElement>(".jstep.selected");
    if (!ol || !box || ol.scrollWidth <= ol.clientWidth) return;
    ol.scrollTo?.({ left: Math.max(0, box.offsetLeft - (ol.clientWidth - box.offsetWidth) / 2), behavior: "smooth" });
  }, [selected]);

  return (
    <nav className="journey" aria-label="Migration steps">
      <ol ref={list}>
        {steps.map((s) => {
          const st = stateOf(s);
          const Icon = s.icon;
          return (
            <li key={s.key} className={`jstep ${st.cls}${s.key === selected ? " selected" : ""}`}>
              <button type="button" disabled={s.locked} aria-current={s.key === selected ? "step" : undefined}
                aria-label={`Step ${s.index + 1}: ${s.title}. ${st.label}${s.attention ? `, ${s.attention}` : ""}. ${s.metric}`}
                onClick={() => onSelect(s.key)} title={s.locked ? "Finish and confirm the step before to open this one" : s.purpose}>
                <span className="jstep-top">
                  <span className="jstep-icon" aria-hidden="true">
                    {s.locked ? <Lock size={15} /> : s.running ? <span className="spinner" /> : s.paused ? <Pause size={15} /> : s.confirmed && !s.stale ? <Check size={16} /> : <Icon size={16} />}
                  </span>
                  <span className="jstep-num">Step {s.index + 1}</span>
                  {s.attention && !s.locked && <AlertTriangle size={14} className="jstep-warn" aria-hidden="true" />}
                </span>
                <span className="jstep-title">{s.title}</span>
                <span className="jstep-metric">{s.locked ? s.purpose : s.metric}</span>
                <span className="jstep-state">{st.label}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/** Below every step: where it stands, and the way forward once it is good. */
function NextBar({ step, steps, onGo }: { step: StepStatus; steps: StepStatus[]; onGo: (k: StepKey) => void }) {
  const { confirmStep } = useMigration();
  const next = steps[step.index + 1];
  const prev = steps[step.index - 1];
  const confirm = () => { confirmStep(step.key); if (next) onGo(next.key); };

  let icon: ReactNode, title: string, detail: string, action: ReactNode;
  // Work under way comes first: a confirmed step being run again is not "confirmed" until it finishes.
  const busy = step.running || step.paused;
  if (step.confirmed && !step.stale && !busy) {
    icon = <CheckCircle2 size={20} className="tone-success" aria-hidden="true" />;
    title = `${step.title} confirmed`;
    detail = step.outcome || "Reviewed and confirmed.";
    action = next ? <Button variant="primary" onClick={() => onGo(next.key)}>Go to {next.title}<ArrowRight size={15} aria-hidden="true" /></Button> : null;
  } else if (step.complete && !busy) {
    icon = step.attention ? <AlertTriangle size={20} className="tone-warning" aria-hidden="true" /> : <CheckCircle2 size={20} className="tone-success" aria-hidden="true" />;
    title = step.attention ? `${step.title} finished · ${step.attention}` : `${step.title} finished`;
    detail = step.attention
      ? `${step.outcome} Review the issues above, or continue and come back to them.`
      : `${step.outcome} If everything looks right, confirm it to move on.`;
    action = (
      <Button variant="primary" onClick={confirm}>
        {next ? (step.attention ? `Continue to ${next.title} anyway` : `Looks good, continue to ${next.title}`) : "Finish migration"}
        <ArrowRight size={15} aria-hidden="true" />
      </Button>
    );
  } else {
    icon = step.running ? <span className="spinner" aria-hidden="true" />
      : step.paused || step.stale ? <AlertTriangle size={20} className="tone-warning" aria-hidden="true" />
      : <Circle size={18} className="faint" aria-hidden="true" />;
    title = step.running ? `${step.title} in progress`
      : step.paused ? `${step.title} is paused`
      : step.stale ? (step.key === "plan" ? "The plan needs rebuilding" : `${step.title} needs to run again`)
      : `${step.title} is not finished`;
    detail = step.stale
      ? `Confirmed earlier, but ${step.lostReason.charAt(0).toLowerCase()}${step.lostReason.slice(1)} ${step.waiting}${next ? " The steps after it return once it is done again." : ""}`
      : step.waiting;
    action = <Button variant="primary" disabled title={step.waiting}>{next ? `Continue to ${next.title}` : "Finish migration"}<ArrowRight size={15} aria-hidden="true" /></Button>;
  }

  return (
    <footer className="next-bar" aria-label="Step status">
      <div className="next-status" role="status">
        {icon}
        <div><strong>{title}</strong><span className="muted">{detail}</span></div>
      </div>
      <div className="row next-actions">
        {prev && <Button variant="ghost" onClick={() => onGo(prev.key)}><ArrowLeft size={15} aria-hidden="true" />{prev.title}</Button>}
        {action}
      </div>
    </footer>
  );
}

/** Shown once every step is confirmed: the outcome, honestly, and the report to take away. */
function CompletionCard() {
  const { execution: run, validation, project, fabric } = useMigration();
  const { connection } = useAppState();
  const mismatched = (validation ?? []).filter((r) => r.status === "MISMATCH").length;
  const problems = [
    run.failed ? `${run.failed.toLocaleString()} object${run.failed === 1 ? "" : "s"} failed` : "",
    mismatched ? `${mismatched.toLocaleString()} check${mismatched === 1 ? "" : "s"} did not match` : "",
  ].filter(Boolean);
  return (
    <section className="complete-card" aria-labelledby="complete-title">
      <span className="complete-icon" aria-hidden="true"><PartyPopper size={22} /></span>
      <div className="stack" style={{ gap: 10, flex: 1, minWidth: 0 }}>
        <div className="row" style={{ alignItems: "flex-start" }}>
          <div style={{ flex: 1, minWidth: 220 }}>
            <h2 id="complete-title">Migration complete</h2>
            <p className="muted">
              Every step is confirmed.{" "}
              {problems.length ? `${problems.join(" and ")}: open Migrate or Validate to deal with ${problems.length > 1 ? "them" : "it"}.` : "Objects left for a person are listed under Migrate, and the full comparison under Validate."}
            </p>
          </div>
          <Button onClick={() => downloadReport({ run, validation, project: project.name, source: connection.workspace ?? null, target: fabric.workspaceName ?? null })}>
            <Download size={14} aria-hidden="true" />Download report (CSV)
          </Button>
        </div>
        <CompletionSummary />
      </div>
    </section>
  );
}

export function Workspace() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const { isConnected, connection, discovery, mode, ready, backendError, reload } = useAppState();
  const { fabric, resetNonce } = useMigration();
  const { steps, current, finished } = useJourney();
  const flow = useRef<HTMLDivElement>(null);

  const requested = params.get("step") as StepKey | null;
  const selected = steps.find((s) => s.key === requested && !s.locked)?.key ?? current;
  const step = steps.find((s) => s.key === selected)!;
  const select = (key: StepKey) => {
    setParams({ step: key }, { replace: false });
  };
  useDocumentTitle(`${step.title} · Migration`);
  // Pin the step being shown in the address once the page has loaded, so the view
  // never moves on its own (say, to Validate the moment a run starts).
  useEffect(() => {
    if (ready && !requested) setParams({ step: current }, { replace: true });
  }, [ready, requested, current]); // eslint-disable-line react-hooks/exhaustive-deps

  // Next sits at the foot of a long panel; when the step changes from there,
  // bring the steps back into view (clear of the top bar) so the new step is read from its top.
  const shown = useRef(selected);
  useEffect(() => {
    if (shown.current === selected) return;
    shown.current = selected;
    const el = flow.current;
    if (!el) return;
    const margin = parseFloat(getComputedStyle(el).scrollMarginTop) || 0;
    if (el.getBoundingClientRect().top < margin - 2) el.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [selected]);

  if (!ready) {
    return (
      <div className="workspace">
        <div className="panel-center" role="status"><span className="spinner large" aria-hidden="true" /><p className="muted">Loading the migration…</p></div>
      </div>
    );
  }

  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  if (mode === "real" && backendError && !isConnected && !discovered) {
    return (
      <div className="workspace">
        <EmptyState icon={<ServerCrash size={22} />} title="Waiting for the backend"
          actions={<Button variant="primary" onClick={reload}>Retry</Button>}>
          The migration is read from the accelerator's backend, which is not answering. Start it, then retry.
        </EmptyState>
      </div>
    );
  }
  if (!isConnected && !discovered) {
    return (
      <div className="workspace">
        <EmptyState icon={<PlugZap size={22} />} title="Connect both sides first"
          actions={<Button variant="primary" onClick={() => navigate("/")}>Open Connections</Button>}>
          The migration starts from a connected source. Choose the route and connect Azure Synapse and Microsoft Fabric.
        </EmptyState>
      </div>
    );
  }

  const done = steps.filter((s) => s.confirmed && !s.stale).length;
  return (
    <div className="workspace">
      <div className="ws-head">
        <div className="route-summary" aria-label="Migration route">
          <PlatformMark platform={SOURCES[0]} size={30} />
          <span><span className="eyebrow">From</span><strong>{connection.workspace ?? "Azure Synapse"}</strong></span>
          <ArrowRight size={16} className="faint" aria-hidden="true" />
          <PlatformMark platform={DESTINATIONS[0]} size={30} />
          <span><span className="eyebrow">To</span><strong>{fabric.workspaceName ?? "Microsoft Fabric"}</strong></span>
        </div>
        <span className="spacer" />
        <div className="ws-progress" aria-label={`${done} of ${steps.length} steps done`}>
          <span className="muted">Steps done</span>
          <span className="ws-meter"><span style={{ width: `${(done / steps.length) * 100}%` }} /></span>
          <strong>{done}/{steps.length}</strong>
        </div>
        <Button size="small" variant="ghost" onClick={() => navigate("/")}><Settings2 size={14} aria-hidden="true" />Connections</Button>
      </div>

      {(!isConnected || fabric.status !== "connected") && (
        <Banner tone="warning" title={!isConnected ? "The source is disconnected" : "The destination is disconnected"}
          actions={<Button size="small" onClick={() => navigate("/")}>Reconnect</Button>}>
          {!isConnected ? "Results already read stay available, but discovery cannot run again until Synapse is connected." : "Planning works, but nothing can be migrated or validated until Fabric is connected."}
        </Banner>
      )}

      <div ref={flow} className="journey-anchor"><JourneyFlow steps={steps} selected={selected} onSelect={select} /></div>

      {finished && <CompletionCard />}

      <section className="step-panel" aria-labelledby="step-title">
        <header className="step-head">
          <span className="step-head-icon" aria-hidden="true"><step.icon size={20} /></span>
          <div className="step-head-text">
            <span className="eyebrow">Step {step.index + 1} of {steps.length}{mode === "mock" ? " · demo data" : ""}</span>
            <h2 id="step-title">{step.title}</h2>
            <p className="muted">{step.purpose}.</p>
          </div>
          <ResetStepButton step={selected} />
        </header>
        <div className="step-body" key={`${selected}-${resetNonce}`}>{PANELS[selected]()}</div>
        <NextBar step={step} steps={steps} onGo={select} />
      </section>
    </div>
  );
}
