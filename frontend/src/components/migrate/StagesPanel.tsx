import { CheckCircle2, CircleAlert, KeyRound, Play } from "lucide-react";
import { useMemo } from "react";
import { Banner, Button, Card, Collapsible, StatusBadge, type Tone } from "../shared/Shared";
import { useMigration } from "../../state/MigrationState";
import type { ExecItem, LinkedServiceInput, StageDef } from "../../types";

const SECRET_FIELDS = new Set(["password", "clientSecret", "key", "token"]);
const FIELD_LABEL: Record<string, string> = { username: "Username", password: "Password", tenantId: "Tenant ID", clientId: "Client ID", clientSecret: "Client secret", key: "Account key", token: "SAS token" };

/** How far a stage got in the current run, from the objects that belong to it. */
function stageProgress(stage: StageDef, items: ExecItem[]): { label: string; tone: Tone } | null {
  const mine = items.filter((i) => stage.types.includes(i.type) || (stage.key === "data" && i.type === "Table data"));
  if (!mine.length) return null;
  const failed = mine.filter((i) => i.status === "FAILED").length;
  const done = mine.filter((i) => i.status === "COMPLETED" || i.status === "SKIPPED").length;
  const deferred = mine.filter((i) => i.status === "DEFERRED").length;
  if (failed) return { label: `${failed} failed`, tone: "error" };
  if (mine.some((i) => i.status === "IN PROGRESS")) return { label: "Running", tone: "info" };
  if (done === mine.length) return { label: `Done ${done}/${mine.length}`, tone: "success" };
  if (done + deferred === mine.length) return { label: `${done} done · ${deferred} need attention`, tone: "warning" };
  return { label: `${done}/${mine.length}`, tone: "neutral" };
}

function CredentialForm({ service }: { service: LinkedServiceInput }) {
  const { credentials, setCredential } = useMigration();
  const value = credentials[service.name] ?? {};
  if (service.unsupported) {
    return <div className="cred"><strong>{service.name}</strong> <span className="muted">({service.type})</span><p className="muted">{service.unsupported}</p></div>;
  }
  const auth = service.authTypes.find((a) => a.value === value.authType) ?? service.authTypes[0];
  return (
    <div className="cred">
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <strong>{service.name}</strong><span className="muted">{service.type} → Fabric {service.fabricType}</span>
        <span className="spacer" />
        <select className="select" style={{ height: 30 }} aria-label={`How to sign in to ${service.name}`} value={auth?.value ?? ""} onChange={(e) => setCredential(service.name, { authType: e.target.value })}>
          {service.authTypes.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
        </select>
      </div>
      <div className="form-grid" style={{ marginTop: 8 }}>
        {(auth?.fields ?? []).map((f) => (
          <label key={f} className="field">
            <span>{FIELD_LABEL[f] ?? f}</span>
            <input className="input" type={SECRET_FIELDS.has(f) ? "password" : "text"} autoComplete="off" aria-label={`${FIELD_LABEL[f] ?? f} for ${service.name}`}
              value={value[f] ?? ""} onChange={(e) => setCredential(service.name, { authType: auth.value, [f]: e.target.value })} />
          </label>
        ))}
        {service.needsPath && (
          <label className="field">
            <span>Container or folder path</span>
            <input className="input" autoComplete="off" placeholder="mycontainer" aria-label={`Storage path for ${service.name}`} value={value.path ?? ""} onChange={(e) => setCredential(service.name, { authType: auth?.value ?? "", path: e.target.value })} />
          </label>
        )}
      </div>
    </div>
  );
}

/** Which stages run, how, and a button to run any one of them on its own. */
export function StagesPanel() {
  const { capabilities, options, setOptions, graph, plan, fabric, execution: run, startExecution, credentials, clearCredentials } = useMigration();

  const nodes = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const typesInPlan = useMemo(() => {
    const counts = new Map<string, number>();
    for (const p of plan) { const n = nodes.get(p.id); if (n) counts.set(n.type, (counts.get(n.type) ?? 0) + 1); }
    return counts;
  }, [plan, nodes]);

  if (!capabilities) return null;
  const stages = capabilities.stages;
  const enabled = new Set(options.stages);
  const toggle = (key: string) => setOptions({ stages: enabled.has(key) ? options.stages.filter((k) => k !== key) : [...options.stages, key] });
  const countFor = (s: StageDef) => s.types.reduce((a, t) => a + (typesInPlan.get(t) ?? 0), 0);
  const running = run.state === "running";
  const connected = fabric.status === "connected";
  const entered = (name: string) => Object.keys(credentials[name] ?? {}).some((k) => k !== "authType" && (credentials[name][k] ?? "").length > 0);
  const usable = capabilities.linkedServices.filter((l) => !l.unsupported);

  return (
    <Card eyebrow="Migration stages" title="Choose what to migrate"
      subtitle="Switch stages on or off, set each one's strategy, and run any stage on its own. Start migration runs every stage that is on, in dependency order."
      actions={<div className="row" style={{ gap: 6 }}>
        <Button size="small" variant="ghost" onClick={() => setOptions({ stages: stages.map((s) => s.key) })}>All on</Button>
        <Button size="small" variant="ghost" onClick={() => setOptions({ stages: [] })}>All off</Button>
      </div>}>
      <div className="stage-grid">
        {stages.map((s) => {
          const on = enabled.has(s.key);
          const count = countFor(s) + (s.key === "data" ? 0 : 0);
          const progress = stageProgress(s, run.items);
          const missing = s.needs.filter((k) => !enabled.has(k) && stages.find((x) => x.key === k) && countFor(stages.find((x) => x.key === k)!) > 0);
          const credsNeeded = s.needsInput === "credentials";
          const enteredCount = usable.filter((l) => entered(l.name)).length;
          const canRun = on && count > 0 && connected && !running;
          return (
            <section key={s.key} className={`stage-card${on ? "" : " off"}`} aria-label={s.label}>
              <header>
                <label className="stage-toggle">
                  <input type="checkbox" checked={on} onChange={() => toggle(s.key)} aria-label={`Include ${s.label}`} />
                  <strong>{s.label}</strong>
                </label>
                <span className="spacer" />
                {progress ? <StatusBadge tone={progress.tone} running={progress.label === "Running"}>{progress.label}</StatusBadge>
                  : <span className="faint">{count ? `${count} in plan` : "none in plan"}</span>}
              </header>
              <p className="muted">{s.summary}</p>
              <p className="faint" style={{ margin: 0 }}>Creates: {s.creates}</p>

              {s.options.map((o) => (
                <div key={o.key} className="stage-option">
                  <label htmlFor={`opt-${o.key}`}>{o.label}</label>
                  <select id={`opt-${o.key}`} className="select" disabled={!on} value={options.dataMode} onChange={(e) => setOptions({ dataMode: e.target.value as "if_empty" | "replace" })}>
                    {o.choices.map((c) => <option key={c.value} value={c.value}>{c.value === "if_empty" ? "Skip it (safe)" : "Replace it"}</option>)}
                  </select>
                  <span className="faint">{o.choices.find((c) => c.value === options.dataMode)?.description}</span>
                  {s.key === "data" && (
                    <label className="stage-rows">Row limit per table
                      <input className="input" type="number" min={1} max={capabilities.maxRowsCeiling} disabled={!on} value={options.maxRows}
                        onChange={(e) => setOptions({ maxRows: Math.max(1, Math.min(capabilities.maxRowsCeiling, Number(e.target.value) || 1)) })} />
                      <span className="faint">Larger tables are left for a pipeline Copy activity.</span>
                    </label>
                  )}
                </div>
              ))}

              {missing.length > 0 && on && (
                <p className="stage-warn"><CircleAlert size={14} aria-hidden="true" /> Needs {missing.map((k) => stages.find((x) => x.key === k)?.label).join(", ")}, which {missing.length === 1 ? "is" : "are"} off. Those must already exist in Fabric.</p>
              )}

              {credsNeeded && on && (
                <Collapsible summary={<span><KeyRound size={14} aria-hidden="true" /> Credentials <span className="muted">· {enteredCount} of {usable.length} entered</span></span>}>
                  <p className="muted" style={{ marginTop: 0 }}>Synapse does not give up the secret behind a linked service, so Fabric needs it from you. It is sent once to the backend, kept in memory for this run, and never saved in the browser.</p>
                  {capabilities.linkedServices.length === 0 && <p className="muted">No linked services were discovered.</p>}
                  {capabilities.linkedServices.map((l) => <CredentialForm key={l.name} service={l} />)}
                  {Object.keys(credentials).length > 0 && <Button size="small" variant="ghost" onClick={clearCredentials}>Clear all credentials</Button>}
                </Collapsible>
              )}

              <footer>
                <Button size="small" onClick={() => void startExecution(s.key)} disabled={!canRun} title={!connected ? "Connect the Fabric target first" : !count ? "Nothing in the plan for this stage" : undefined}>
                  <Play size={13} aria-hidden="true" />Run this stage
                </Button>
                {progress?.tone === "success" && <CheckCircle2 size={15} color="var(--success)" aria-label="done" />}
              </footer>
            </section>
          );
        })}
      </div>

      <div className="stage-run-options">
        <div className="auth-cards" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }} role="radiogroup" aria-label="Run scope">
          {([
            { id: "automated", title: "Automated objects only", desc: "Lists only what the selected stages create: the shortest, cleanest run." },
            { id: "all", title: "Everything in the plan", desc: "Also lists every other object as Deferred, with the reason, so the whole plan is on one screen." },
          ] as const).map((o) => (
            <button key={o.id} type="button" role="radio" aria-checked={options.scope === o.id} className="auth-card" onClick={() => setOptions({ scope: o.id })}>
              <span className="title">{o.title}</span><span className="desc">{o.desc}</span><span className="pick">{options.scope === o.id ? "Selected" : "Select"}</span>
            </button>
          ))}
        </div>
        <label className="check-row">
          <input type="checkbox" checked={options.stopOnFailure} onChange={(e) => setOptions({ stopOnFailure: e.target.checked })} />
          <span><strong>Stop at the end of a wave that has failures</strong><span className="muted"> — so a failed table never lets the views that read it start. Off: every object is attempted and failures are reported.</span></span>
        </label>
      </div>
      {!connected && <Banner tone="warning" title="Fabric target not connected">Connect it on the Fabric Target page to run any stage.</Banner>}
    </Card>
  );
}
