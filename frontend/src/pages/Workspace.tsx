import { AlertTriangle, ArrowLeft, ArrowRight, Check, CheckCircle2, Circle, Download, Lock, PanelLeftClose, PanelLeftOpen, Pause, PartyPopper, PlugZap, ServerCrash, Settings2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ConsultationView } from "../agent/ConsultationView";
import { OrchestratorView } from "../agent/OrchestratorView";
import { AssessPanel } from "../components/journey/panels/AssessPanel";
import { DiscoverPanel } from "../components/journey/panels/DiscoverPanel";
import { CompletionSummary } from "../components/journey/panels/ValidatePanel";
import { WavesPanel } from "../components/journey/panels/WavesPanel";
import { WorkspaceExplorer } from "../components/journey/WorkspaceExplorer";
import { downloadReport } from "../components/journey/report";
import { ResetStepButton } from "../components/journey/ResetStep";
import { locate, usePhases, type DiscoverTab, type OrchestrateTab, type PhaseKey, type PhaseStatus } from "../components/journey/phases";
import type { StepStatus } from "../components/journey/useJourney";
import { PlatformMark } from "../components/setup/PlatformPicker";
import { DESTINATIONS, SOURCES } from "../components/setup/platforms";
import { Banner, Button, EmptyState, Tabs, useDocumentTitle } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";

/** The badge on a phase's box: one word or two for where it stands. */
function stateOf(s: PhaseStatus): { cls: string; label: string } {
  if (s.locked) return { cls: "locked", label: "Locked" };
  if (s.running) return { cls: "running", label: "Running" };
  if (s.paused) return { cls: "current attention", label: "Paused" };
  if (s.stale) return { cls: "current attention", label: s.key === "consult" ? "Approve again" : "Run again" };
  if (s.confirmed) return { cls: s.attention ? "done attention" : "done", label: "Done" };
  if (s.complete) return { cls: s.attention ? "current attention" : "current", label: "Ready to confirm" };
  return { cls: s.attention ? "current attention" : "current", label: s.attention ? "Needs attention" : "To do" };
}

/** The three phases, left to right. Each box opens its phase below; a locked box opens once Discover is confirmed. */
function JourneyFlow({ phases, selected, onSelect }: { phases: PhaseStatus[]; selected: PhaseKey; onSelect: (k: PhaseKey) => void }) {
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const ol = list.current;
    const box = ol?.querySelector<HTMLElement>(".jstep.selected");
    if (!ol || !box || ol.scrollWidth <= ol.clientWidth) return;
    ol.scrollTo?.({ left: Math.max(0, box.offsetLeft - (ol.clientWidth - box.offsetWidth) / 2), behavior: "smooth" });
  }, [selected]);

  return (
    <nav className="journey phases" aria-label="Migration steps">
      <ol ref={list}>
        {phases.map((s) => {
          const st = stateOf(s);
          const Icon = s.icon;
          return (
            <li key={s.key} className={`jstep ${st.cls}${s.key === selected ? " selected" : ""}`}>
              <button type="button" disabled={s.locked} aria-current={s.key === selected ? "step" : undefined}
                aria-label={`Step ${s.index + 1}: ${s.title}. ${st.label}${s.attention ? `, ${s.attention}` : ""}. ${s.metric}`}
                onClick={() => onSelect(s.key)} title={s.locked ? "Finish and confirm Discover to open this one" : s.purpose}>
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

/** Below every phase: where it stands, and the way forward once it is good. */
function NextBar({ phase, phases, onGo }: { phase: PhaseStatus; phases: PhaseStatus[]; onGo: (k: PhaseKey) => void }) {
  const { confirmStep } = useMigration();
  const next = phases[phase.index + 1];
  const prev = phases[phase.index - 1];
  const confirm = () => { for (const k of phase.steps) confirmStep(k); if (next) onGo(next.key); };

  let icon: ReactNode, title: string, detail: string, action: ReactNode;
  const busy = phase.running || phase.paused;
  if (phase.confirmed && !phase.stale && !busy) {
    icon = <CheckCircle2 size={20} className="tone-success" aria-hidden="true" />;
    title = `${phase.title} ${phase.key === "consult" ? "approved" : "confirmed"}`;
    detail = phase.outcome || "Reviewed and confirmed.";
    action = next ? <Button variant="primary" onClick={() => onGo(next.key)}>Go to {next.title}<ArrowRight size={15} aria-hidden="true" /></Button> : null;
  } else if (phase.complete && !busy && phase.key !== "consult") {
    icon = phase.attention ? <AlertTriangle size={20} className="tone-warning" aria-hidden="true" /> : <CheckCircle2 size={20} className="tone-success" aria-hidden="true" />;
    title = phase.attention ? `${phase.title} finished · ${phase.attention}` : `${phase.title} finished`;
    detail = phase.attention
      ? `${phase.outcome} Review the issues above, or continue and come back to them.`
      : `${phase.outcome} If everything looks right, confirm it to move on.`;
    action = (
      <Button variant="primary" onClick={confirm}>
        {next ? (phase.attention ? `Continue to ${next.title} anyway` : `Looks good, continue to ${next.title}`) : "Finish migration"}
        <ArrowRight size={15} aria-hidden="true" />
      </Button>
    );
  } else {
    icon = phase.running ? <span className="spinner" aria-hidden="true" />
      : phase.paused || phase.stale ? <AlertTriangle size={20} className="tone-warning" aria-hidden="true" />
      : <Circle size={18} className="faint" aria-hidden="true" />;
    title = phase.running ? `${phase.title} in progress`
      : phase.paused ? `${phase.title} is paused`
      : phase.stale ? `${phase.title} needs to be done again`
      : phase.key === "consult" ? "The brief is not approved yet" : `${phase.title} is not finished`;
    detail = phase.stale ? `${phase.lostReason} ${phase.waiting}` : phase.waiting;
    // The plan, run and validation can be worked by hand, so the consultation never shuts the way on.
    action = phase.key === "consult" && next && !next.locked
      ? <Button onClick={() => onGo(next.key)}>Continue without an approved brief<ArrowRight size={15} aria-hidden="true" /></Button>
      : <Button variant="primary" disabled title={phase.waiting}>{next ? `Continue to ${next.title}` : "Finish migration"}<ArrowRight size={15} aria-hidden="true" /></Button>;
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

/** Discover: the inventory, how each object moves, and the waves it moves in, as tabs of one step. */
function DiscoverPhase({ tab, onTab, steps }: { tab: DiscoverTab; onTab: (t: DiscoverTab) => void; steps: StepStatus[] }) {
  const { graph } = useMigration();
  const { discovery } = useAppState();
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const by = (k: string) => steps.find((s) => s.key === k)!;
  return (
    <div className="stack" style={{ gap: 16 }}>
      <Tabs label="Discover sections" value={tab} onChange={onTab} tabs={[
        { id: "objects", label: "Discovered objects", count: discovered ? discovery.summary?.total : undefined },
        { id: "assessment", label: "Assessment" },
        { id: "waves", label: "Dependency waves", count: graph ? graph.waves.length : undefined },
      ]} />
      {tab === "objects" && <DiscoverPanel />}
      {tab === "assessment" && (by("assess").complete ? <AssessPanel /> : <p className="muted">{by("assess").waiting}</p>)}
      {tab === "waves" && (discovered ? <WavesPanel /> : <p className="muted">{by("waves").waiting}</p>)}
    </div>
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
              {problems.length ? `${problems.join(" and ")}: open the Migration or Validation tab of the Agent orchestrator to deal with ${problems.length > 1 ? "them" : "it"}.` : "Objects left for a person are listed under Migration, and the full comparison under Validation."}
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

const EXPLORER_KEY = "ma.explorer";

/** Whether the explorer is shown: a per-browser preference, on unless it was closed. */
function useExplorerOpen(): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(() => {
    try { return localStorage.getItem(EXPLORER_KEY) !== "closed"; } catch { return true; }
  });
  const set = (next: boolean) => {
    setOpen(next);
    try { localStorage.setItem(EXPLORER_KEY, next ? "open" : "closed"); } catch { /* storage unavailable: kept for this visit */ }
  };
  return [open, set];
}

const DISCOVER_TABS: DiscoverTab[] = ["objects", "assessment", "waves"];
const ORCHESTRATE_TABS: OrchestrateTab[] = ["agent", "plan", "migrate", "validate"];

export function Workspace() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const { isConnected, connection, discovery, mode, ready, backendError, reload } = useAppState();
  const { fabric, resetNonce } = useMigration();
  const { phases, current, steps, finished } = usePhases();
  const flow = useRef<HTMLDivElement>(null);
  const [explorerOpen, setExplorerOpen] = useExplorerOpen();

  // ?step= names a phase, or one of the six steps (old links), which opens its phase on that tab.
  const requested = params.get("step");
  const where = locate(requested);
  const selected = where && !phases.find((p) => p.key === where.phase)!.locked ? where.phase : current;
  const phase = phases.find((p) => p.key === selected)!;
  const rawTab = params.get("tab") ?? (where?.phase === selected ? where.tab : null);
  const discoverTab: DiscoverTab = DISCOVER_TABS.includes(rawTab as DiscoverTab) ? (rawTab as DiscoverTab) : "objects";
  // A phase whose step is still locked opens the agent's run instead.
  const tabOpen = (t: string | null) => ORCHESTRATE_TABS.includes(t as OrchestrateTab) && (t === "agent" || !steps.find((s) => s.key === t)?.locked);
  const orchestrateTab: OrchestrateTab = tabOpen(rawTab) ? (rawTab as OrchestrateTab) : "agent";
  const select = (key: PhaseKey, tab?: string) => setParams(tab ? { step: key, tab } : { step: key }, { replace: false });
  useDocumentTitle(`${phase.title} · Migration`);
  // Pin the phase being shown in the address once the page has loaded, so the view never moves on its own.
  useEffect(() => {
    if (ready && !requested) setParams({ step: current }, { replace: true });
  }, [ready, requested, current]); // eslint-disable-line react-hooks/exhaustive-deps

  // Next sits at the foot of a long panel; when the phase changes from there, bring the strip back into view.
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

  const done = phases.filter((s) => s.confirmed && !s.stale).length;
  const body = selected === "discover"
    ? <DiscoverPhase tab={discoverTab} onTab={(t) => select("discover", t)} steps={steps} />
    : selected === "consult"
      ? <ConsultationView embedded onAgent={() => select("orchestrate")} />
      : <OrchestratorView embedded tab={orchestrateTab} onTab={(t) => select("orchestrate", t)} steps={steps}
          onConsult={() => select("consult")} onStep={(k) => { const w = locate(k)!; select(w.phase, w.tab ?? undefined); }} onConnections={() => navigate("/")} />;
  const resetKey = selected === "discover" ? "discover" : selected === "orchestrate" && orchestrateTab !== "agent" ? orchestrateTab : null;

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
        <div className="ws-progress" aria-label={`${done} of ${phases.length} steps done`}>
          <span className="muted">Steps done</span>
          <span className="ws-meter"><span style={{ width: `${(done / phases.length) * 100}%` }} /></span>
          <strong>{done}/{phases.length}</strong>
        </div>
        <Button size="small" variant="ghost" aria-pressed={explorerOpen} onClick={() => setExplorerOpen(!explorerOpen)}>
          {explorerOpen ? <PanelLeftClose size={14} aria-hidden="true" /> : <PanelLeftOpen size={14} aria-hidden="true" />}{explorerOpen ? "Hide explorer" : "Show explorer"}
        </Button>
        <Button size="small" variant="ghost" onClick={() => navigate("/")}><Settings2 size={14} aria-hidden="true" />Connections</Button>
      </div>

      {(!isConnected || fabric.status !== "connected") && (
        <Banner tone="warning" title={!isConnected ? "The source is disconnected" : "The destination is disconnected"}
          actions={<Button size="small" onClick={() => navigate("/")}>Reconnect</Button>}>
          {!isConnected ? "Results already read stay available, but discovery cannot run again until Synapse is connected." : "Planning works, but nothing can be migrated or validated until Fabric is connected."}
        </Banner>
      )}

      <div className={`ws-body${explorerOpen ? " with-explorer" : ""}`}>
        {explorerOpen && <WorkspaceExplorer />}
        <div className="ws-main">
          <div ref={flow} className="journey-anchor"><JourneyFlow phases={phases} selected={selected} onSelect={(k) => select(k)} /></div>
          {finished && <CompletionCard />}

          <section className="step-panel" aria-labelledby="step-title">
            <header className="step-head">
              <span className="step-head-icon" aria-hidden="true"><phase.icon size={20} /></span>
              <div className="step-head-text">
                <span className="eyebrow">Step {phase.index + 1} of {phases.length}{mode === "mock" ? " · demo data" : ""}</span>
                <h2 id="step-title">{phase.title}</h2>
                <p className="muted">{phase.purpose}.</p>
              </div>
              {resetKey && <ResetStepButton step={resetKey} />}
            </header>
            <div className="step-body" key={`${selected}-${resetNonce}`}>{body}</div>
            <NextBar phase={phase} phases={phases} onGo={(k) => select(k)} />
          </section>
        </div>
      </div>
    </div>
  );
}
