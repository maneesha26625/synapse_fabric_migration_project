import { ArrowRight, Check, Circle, Info, MoveRight, Rocket } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { SourceConnectionPanel } from "../components/connections/SourceConnection";
import { TargetConnectionPanel } from "../components/connections/TargetConnection";
import { STEPS } from "../components/journey/steps";
import { PlatformMark, PlatformPicker } from "../components/setup/PlatformPicker";
import { DESTINATIONS, SOURCES, platformById, type Platform } from "../components/setup/platforms";
import { Button, StatusBadge, useDocumentTitle, type Tone } from "../components/shared/Shared";
import { useAppState } from "../state/AppState";
import { useMigration } from "../state/MigrationState";

const ROUTE_KEY = "ma.route";
interface Route { source: string | null; destination: string | null }

function loadRoute(): Route {
  try {
    const raw = localStorage.getItem(ROUTE_KEY);
    return raw ? (JSON.parse(raw) as Route) : { source: null, destination: null };
  } catch {
    return { source: null, destination: null };
  }
}

/** The route the operator picked. Shared with the header, which shows it. */
export function useRoute(): [Route, (patch: Partial<Route>) => void] {
  const [route, setRoute] = useState<Route>(loadRoute);
  const update = (patch: Partial<Route>) => setRoute((r) => {
    const next = { ...r, ...patch };
    try { localStorage.setItem(ROUTE_KEY, JSON.stringify(next)); } catch { /* storage unavailable */ }
    return next;
  });
  return [route, update];
}

function SideCard({ platform, side, status, children }: { platform: Platform; side: string; status: { tone: Tone; label: string; running?: boolean }; children: ReactNode }) {
  return (
    <section className={`conn-card${status.tone === "success" ? " ok" : ""}`} aria-label={`${side} connection: ${platform.name}`}>
      <header className="conn-head">
        <PlatformMark platform={platform} size={42} />
        <div className="conn-title">
          <span className="eyebrow">{side}</span>
          <h3>{platform.name}</h3>
        </div>
        <StatusBadge tone={status.tone} running={status.running}>{status.label}</StatusBadge>
      </header>
      {children}
    </section>
  );
}

/**
 * The start of every migration: choose where from and where to, connect both,
 * then start. Everything after this happens on the migration page.
 */
export function Setup() {
  const navigate = useNavigate();
  const { isConnected, connection, connectionBusy, connectionError, mode, ready } = useAppState();
  const { fabric, fabricBusy, fabricReady, confirmed, project } = useMigration();
  const [route, setRoute] = useRoute();
  useDocumentTitle("Connections");
  // Until both sides have been read once, say so rather than flash "Not connected".
  const checking = { tone: "info" as Tone, label: "Checking…", running: true };

  // A side that is already connected implies its platform: nothing to choose again.
  useEffect(() => { if ((isConnected || connection.signedIn) && !route.source) setRoute({ source: "synapse" }); }, [isConnected, connection.signedIn]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (fabric.status !== "disconnected" && !route.destination) setRoute({ destination: "fabric" }); }, [fabric.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const source = platformById(SOURCES, route.source);
  const destination = platformById(DESTINATIONS, route.destination);
  const routeChosen = !!source && !!destination;
  const targetConnected = fabric.status === "connected";
  const capacityMissing = targetConnected && fabric.capacityAssigned === false;
  const startable = routeChosen && isConnected && targetConnected && !capacityMissing;
  const resuming = confirmed.length > 0;

  const sourceStatus = !ready ? checking
    : isConnected ? { tone: "success" as Tone, label: "Connected" }
    : connectionBusy ? { tone: "info" as Tone, label: connectionBusy === "test" ? "Testing…" : "Signing in…", running: true }
    : connectionError ? { tone: "error" as Tone, label: "Failed" }
    : connection.signedIn ? { tone: "info" as Tone, label: "Signed in" }
    : { tone: "neutral" as Tone, label: "Not connected" };
  const targetStatus = !fabricReady ? checking
    : targetConnected ? (capacityMissing ? { tone: "warning" as Tone, label: "No capacity" } : { tone: "success" as Tone, label: "Connected" })
    : fabric.status === "signing_in" || fabricBusy ? { tone: "info" as Tone, label: fabricBusy === "test" ? "Testing…" : "Signing in…", running: true }
    : fabric.status === "failed" ? { tone: "error" as Tone, label: "Failed" }
    : fabric.status === "authenticated" ? { tone: "info" as Tone, label: "Signed in" }
    : { tone: "neutral" as Tone, label: "Not connected" };

  const checklist: [string, boolean][] = [
    ["Route chosen", routeChosen],
    [`${source?.name ?? "Source"} connected`, isConnected],
    [`${destination?.name ?? "Destination"} connected`, targetConnected && !capacityMissing],
  ];

  return (
    <div className="setup">
      <section className="setup-hero">
        <span className="eyebrow">{project.name}</span>
        <h1>Start a migration</h1>
        <p>Choose where you are moving from and to, connect both sides, and the accelerator takes you from discovery to validation, one confirmed step at a time.</p>
        <ol className="journey-preview" aria-label="What happens after you start">
          {STEPS.map(({ key, title, icon: Icon }, i) => (
            <li key={key}>{i > 0 && <MoveRight size={14} className="faint" aria-hidden="true" />}<span><Icon size={14} aria-hidden="true" />{title}</span></li>
          ))}
        </ol>
      </section>

      {mode === "mock" && (
        <div className="note-strip" role="note">
          <Info size={15} aria-hidden="true" />
          <span><strong>Demo data.</strong> Sample Synapse and Fabric workspaces are already connected, so no Azure sign-in is needed. Disconnect a side to try the connection steps; reload the page to restore the demo.</span>
        </div>
      )}

      <section className="setup-section" aria-labelledby="route-title">
        <header className="section-head">
          <span className={`section-num${routeChosen ? " done" : ""}`} aria-hidden="true">{routeChosen ? <Check size={15} /> : 1}</span>
          <div>
            <h2 id="route-title">Choose the route</h2>
            <p>The platform you are leaving, and the one you are moving to.</p>
          </div>
        </header>
        <div className="route-picker">
          <PlatformPicker direction="From" label="Source" placeholder="Choose a source platform" platforms={SOURCES} value={route.source} onChange={(id) => setRoute({ source: id })} />
          <span className="route-arrow" aria-hidden="true"><ArrowRight size={22} /></span>
          <PlatformPicker direction="To" label="Destination" placeholder="Choose a destination platform" platforms={DESTINATIONS} value={route.destination} onChange={(id) => setRoute({ destination: id })} />
        </div>
      </section>

      <section className="setup-section" aria-labelledby="connect-title">
        <header className="section-head">
          <span className={`section-num${isConnected && targetConnected ? " done" : ""}`} aria-hidden="true">{isConnected && targetConnected ? <Check size={15} /> : 2}</span>
          <div>
            <h2 id="connect-title">Connect both sides</h2>
            <p>Sign-in happens on the machine running the accelerator. No password or token is shown in, or stored by, this browser.</p>
          </div>
        </header>
        {routeChosen ? (
          <div className="conn-grid">
            <SideCard platform={source!} side="Source" status={sourceStatus}>
              {ready ? <SourceConnectionPanel /> : <p className="muted conn-checking"><span className="spinner" aria-hidden="true" />Checking the connection…</p>}
            </SideCard>
            <SideCard platform={destination!} side="Destination" status={targetStatus}>
              {fabricReady ? <TargetConnectionPanel /> : <p className="muted conn-checking"><span className="spinner" aria-hidden="true" />Checking the connection…</p>}
            </SideCard>
          </div>
        ) : (
          <div className="placeholder-card">
            <Circle size={18} aria-hidden="true" />
            <span>Choose a source and a destination above, and their connection settings appear here.</span>
          </div>
        )}
      </section>

      <div className="launch-bar" role="region" aria-label="Start the migration">
        <ul className="launch-checks">
          {checklist.map(([label, ok]) => (
            <li key={label} className={ok ? "ok" : ""}>{ok ? <Check size={14} aria-hidden="true" /> : <Circle size={12} aria-hidden="true" />}{label}<span className="sr-only">{ok ? " — done" : " — not yet"}</span></li>
          ))}
        </ul>
        <span className="spacer" />
        {!startable && <span className="faint launch-hint">{!ready || !fabricReady ? "Checking the connections…" : !routeChosen ? "Choose the route first." : capacityMissing ? "The Fabric workspace needs a capacity." : "Connect and test both sides to start."}</span>}
        <Button variant="primary" className="launch-btn" disabled={!startable} onClick={() => navigate("/migration")}>
          <Rocket size={16} aria-hidden="true" />{resuming ? "Continue migration" : "Start migration"}<ArrowRight size={16} aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}
