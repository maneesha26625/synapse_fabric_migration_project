import { CircleAlert, KeyRound, ScrollText } from "lucide-react";
import { useEffect, useMemo } from "react";
import { Banner, Button, Card, Collapsible, StatusBadge, type Tone } from "../shared/Shared";
import { useConsult } from "../../state/ConsultState";
import { useMigration } from "../../state/MigrationState";
import { DataFiltersPanel } from "./DataFiltersPanel";
import { StorageRoutesPanel } from "./StorageRoutesPanel";
import type { ConsultPolicy, ExecItem, LinkedServiceInput, RunOptions, StageDef } from "../../types";

const SECRET_FIELDS = new Set(["password", "clientSecret", "key", "token"]);
const FIELD_LABEL: Record<string, string> = { username: "Username", password: "Password", tenantId: "Tenant ID", clientId: "Client ID", clientSecret: "Client secret", key: "Account key", token: "SAS token", keyVault: "Key Vault reference (alias or ID)", secretName: "Secret name" };

/** Whether a sign-in method has been chosen and everything it needs is filled in. */
function isComplete(service: LinkedServiceInput, value: Record<string, string>): boolean {
  const auth = service.authTypes.find((a) => a.value === value.authType);
  if (!auth) return false;
  return auth.fields.every((f) => (value[f] ?? "").trim().length > 0) && (!service.needsPath || (value.path ?? "").trim().length > 0);
}

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

/** The sign-in the approved brief chose for this connection (C9 for the pool, G5 for linked services), if this
 *  connection offers it: Key Vault means the first Key Vault option the connection has. */
function preferredAuth(service: LinkedServiceInput, signIn: ConsultPolicy["signIn"]): string | null {
  if (!signIn) return null;
  const wanted = service.stage === "data" ? signIn.pool
    : signIn.linkedServices === "keyVault" ? service.authTypes.find((a) => a.value.endsWith("KeyVault"))?.value ?? null : signIn.linkedServices;
  return wanted && service.authTypes.some((a) => a.value === wanted) ? wanted : null;
}

function CredentialForm({ service }: { service: LinkedServiceInput }) {
  const { credentials, setCredential } = useMigration();
  const { policy } = useConsult();
  const value = credentials[service.name] ?? {};
  const preferred = service.unsupported ? null : preferredAuth(service, policy?.signIn);
  // The brief decided how this connection signs in: preselected once, never over a choice made here. Choosing a
  // method sends no secret; any secret is still typed by a person.
  useEffect(() => {
    if (preferred && !value.authType) setCredential(service.name, { authType: preferred });
  }, [preferred]); // eslint-disable-line react-hooks/exhaustive-deps
  if (service.unsupported) {
    return <div className="cred"><strong>{service.name}</strong> <span className="muted">({service.type})</span><p className="muted">{service.unsupported}</p></div>;
  }
  // Nothing is chosen until the operator picks: an option with no fields (workspace identity)
  // must not look selected while nothing would be sent for it.
  const auth = service.authTypes.find((a) => a.value === value.authType);
  // A new method starts empty: what was typed for the old one (a password, say) is blanked, not sent along.
  const choose = (authType: string) =>
    setCredential(service.name, { ...Object.fromEntries(Object.keys(value).filter((k) => k !== "path").map((k) => [k, ""])), authType });
  return (
    <div className="cred">
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <strong>{service.name}</strong><span className="muted">{service.type} → Fabric {service.fabricType}</span>
        <span className="spacer" />
        <select className="select" style={{ height: 30 }} aria-label={`How to sign in to ${service.name}`} value={auth?.value ?? ""} onChange={(e) => choose(e.target.value)}>
          <option value="">Choose how to sign in…</option>
          {service.authTypes.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
        </select>
      </div>
      {preferred && auth?.value === preferred && <p className="faint" style={{ margin: "6px 0 0" }}>Preselected from the approved brief ({service.stage === "data" ? "C9" : "G5"}).</p>}
      {auth?.hint && <p className="muted" style={{ margin: "6px 0 0" }}>{auth.hint}</p>}
      {auth && <div className="form-grid" style={{ marginTop: 8 }}>
        {auth.fields.map((f) => (
          <label key={f} className="field">
            <span>{FIELD_LABEL[f] ?? f}</span>
            <input className="input" type={SECRET_FIELDS.has(f) ? "password" : "text"} autoComplete="off" aria-label={`${FIELD_LABEL[f] ?? f} for ${service.name}`}
              value={value[f] ?? ""} onChange={(e) => setCredential(service.name, { authType: auth.value, [f]: e.target.value })} />
          </label>
        ))}
        {service.needsPath && (
          <label className="field">
            <span>Container or folder path</span>
            <input className="input" autoComplete="off" placeholder="mycontainer" aria-label={`Storage path for ${service.name}`} value={value.path ?? ""} onChange={(e) => setCredential(service.name, { authType: auth.value, path: e.target.value })} />
          </label>
        )}
      </div>}
    </div>
  );
}

/** Marks an option that differs from the approved brief: the brief says one thing, the plan another. */
function Differs({ option }: { option: string }) {
  const { analysis } = useMigration();
  const found = analysis?.brief?.differences.find((d) => d.option === option);
  return found ? <span className="badge warning consult-differs" title={found.message}>Differs from the brief</span> : null;
}

/**
 * Which stages run, and how. Configuration only: nothing is written from the
 * Plan step. The Migrate step runs every stage that is on, or one on its own.
 */
/** What a run takes from the approved brief itself, as it takes the names: shown here, never changed here. */
function FromTheBrief() {
  const { policy } = useConsult();
  const run = policy?.run;
  if (!policy?.inForce || !run) return null;
  const items = [
    run.parallelCopies ? `the load pipeline copies ${run.parallelCopies} table${run.parallelCopies === 1 ? "" : "s"} at a time (C8)` : "",
    run.schedules ? `schedules are created ${run.schedules === "match" ? "switched on where Synapse had started them" : "switched off"} (E5)` : "",
    run.maskColumns.length ? `the default mask goes on ${run.maskColumns.length} sensitive column pattern${run.maskColumns.length === 1 ? "" : "s"} without one (G1)` : "",
    run.targetRole === "prod" ? "a production target: every run waits for a person's sign-off (E1)" : "",
    policy.names?.warehouse ? `the Warehouse is ${policy.names.warehouse}${policy.names.lakehouse ? `, the Lakehouse ${policy.names.lakehouse}` : ""} (B2)` : "",
  ].filter(Boolean);
  if (!items.length) return null;
  return (
    <p className="faint" style={{ margin: 0 }}><ScrollText size={13} aria-hidden="true" /> From brief version {policy.inForce.version}: {items.join("; ")}.</p>
  );
}

export function StagesPanel() {
  const { capabilities, options, setOptions, graph, plan, execution: run, credentials, clearCredentials, fabricGit } = useMigration();
  const waves = useMemo(() => new Set(plan.map((p) => p.wave)).size, [plan]);

  const nodes = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const typesInPlan = useMemo(() => {
    const counts = new Map<string, number>();
    for (const p of plan) { const n = nodes.get(p.id); if (n) counts.set(n.type, (counts.get(n.type) ?? 0) + 1); }
    return counts;
  }, [plan, nodes]);

  if (!capabilities) return null;
  const stages = capabilities.stages;
  const inPlan = new Set(plan.map((p) => p.id));
  /** The plan's tables a date filter can apply to. */
  const filterable = (capabilities.dataTables ?? []).filter((t) => inPlan.has(t.id));
  const filteredCount = filterable.filter((t) => options.dataFilters?.[t.key]).length;
  const enabled = new Set(options.stages);
  const toggle = (key: string) => setOptions({ stages: enabled.has(key) ? options.stages.filter((k) => k !== key) : [...options.stages, key] });
  const countFor = (s: StageDef) => s.types.reduce((a, t) => a + (typesInPlan.get(t) ?? 0), 0);
  const entered = (service: LinkedServiceInput) => isComplete(service, credentials[service.name] ?? {});
  /** The credentials a stage asks for: linked services under the Connections stage, the Synapse pool under Table data. */
  const servicesFor = (key: string) => capabilities.linkedServices.filter((l) => (l.stage ?? "connections") === key);
  const optionValue = (key: string, fallback: string) => (options as unknown as Record<string, string>)[key] ?? fallback;

  return (
    <Card eyebrow="Migration stages" title="Choose what to migrate"
      subtitle="Switch stages on or off and choose how each one works. The Migrate step runs every stage that is on, in dependency order, or a single stage on its own."
      actions={<div className="row" style={{ gap: 6 }}>
        <Differs option="stages" />
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
          const services = servicesFor(s.key);
          const usable = services.filter((l) => !l.unsupported);
          const enteredCount = usable.filter((l) => entered(l)).length;
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

              {s.options.map((o) => {
                const value = optionValue(o.key, o.default);
                return (
                  <div key={o.key} className="stage-option">
                    <label htmlFor={`opt-${s.key}-${o.key}`}>{o.label} <Differs option={o.key} /></label>
                    <select id={`opt-${s.key}-${o.key}`} className="select" disabled={!on} value={value}
                      onChange={(e) => setOptions({ [o.key]: e.target.value } as Partial<RunOptions>)}>
                      {o.choices.map((c) => <option key={c.value} value={c.value}>{c.label ?? c.value}</option>)}
                    </select>
                    <span className="faint">{o.choices.find((c) => c.value === value)?.description}</span>
                  </div>
                );
              })}

              {s.key === "data" && on && filterable.length > 0 && (
                <p className="faint" style={{ margin: 0 }}>
                  {filteredCount ? `Date filters on ${filteredCount} of ${filterable.length} tables` : "Every table loads all its rows"}: set under Date filters below.
                </p>
              )}

              {missing.length > 0 && on && (
                <p className="stage-warn"><CircleAlert size={14} aria-hidden="true" /> Needs {missing.map((k) => stages.find((x) => x.key === k)?.label).join(", ")}, which {missing.length === 1 ? "is" : "are"} off. Those must already exist in Fabric.</p>
              )}

              {credsNeeded && on && (
                <Collapsible summary={<span><KeyRound size={14} aria-hidden="true" /> Credentials <span className="muted">· {enteredCount} of {usable.length} entered</span></span>}>
                  <p className="muted" style={{ marginTop: 0 }}>
                    {s.key === "data"
                      ? "The data pipelines read the Synapse pool through a Fabric connection that can read the pool: the workspace identity, a SQL login or a service principal. If Fabric already has a connection to this pool, it is reused and this can stay empty."
                      : "Synapse does not give up the secret behind a linked service, so Fabric needs a way to sign in."}
                    {" "}Workspace identity and Key Vault options pass no secret through this tool; prefer them.
                    {" "}Anything typed is sent to the backend once, when the run starts. It is kept in memory for that run and never saved in the browser.</p>
                  {services.length === 0 && <p className="muted">{s.key === "data" ? "Connect the Synapse source with its dedicated SQL pool to set up the pool connection." : "No linked services were discovered."}</p>}
                  {services.map((l) => <CredentialForm key={l.name} service={l} />)}
                  {Object.keys(credentials).length > 0 && <Button size="small" variant="ghost" onClick={clearCredentials}>Clear all credentials</Button>}
                </Collapsible>
              )}

            </section>
          );
        })}
      </div>

      <FromTheBrief />
      {enabled.has("data") && filterable.length > 0 && <DataFiltersPanel tables={filterable} />}
      <StorageRoutesPanel />

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
          <span><strong>Stop at the end of a wave that has failures</strong> <Differs option="stopOnFailure" /><span className="muted"> — so a failed table never lets the views that read it start. Off: every object is attempted and failures are reported.</span></span>
        </label>
        <label className="check-row">
          <input type="checkbox" checked={!!options.waveGate} onChange={(e) => setOptions({ waveGate: e.target.checked })} />
          <span><strong>A person signs off each wave before the next one starts</strong> <Differs option="waveGate" /><span className="muted"> — the run pauses
            before wave 2, 3…; Resume, by a person, signs that wave off and is recorded in the audit trail.{waves < 2 ? " This plan has one wave, so it never pauses." : ""}</span></span>
        </label>
        <label className="check-row">
          <input type="checkbox" checked={options.existing === "update"} onChange={(e) => setOptions({ existing: e.target.checked ? "update" : "skip" })} />
          <span><strong>Update objects that are already in Fabric</strong> <Differs option="existing" /><span className="muted"> — to redeploy fixes or accepted LLM transforms. Views and
            procedures are updated with CREATE OR ALTER (the old one stays if the new one is rejected); notebooks, pipelines, SQL scripts and Spark
            jobs get the new definition and keep their id, so whatever calls them still works. Tables are never replaced: they hold data.
            Off: anything already there is left unchanged.</span></span>
        </label>
        {options.existing === "update" && (
          <Banner tone="warning" title="Changes made in Fabric will be overwritten">
            Any edit someone made in Fabric to a view, procedure, notebook, pipeline, SQL script or Spark job in this plan is replaced by the migrated version.
          </Banner>
        )}
        <label className="check-row">
          <input type="checkbox" checked={options.gitCommit !== "off"} onChange={(e) => setOptions({ gitCommit: e.target.checked ? "auto" : "off" })} />
          <span><strong>Commit to Fabric Git after the run</strong> <Differs option="gitCommit" /><span className="muted"> — what the run created is committed to the target workspace's Git branch, ready for a pull request. Data-load pipelines are left out: they are rebuilt per environment. Off: commit by hand from the Migrate step.</span>
            {fabricGit?.state === "connected" && <span className="faint git-where"> {fabricGit.workspace ?? "The workspace"} commits to <strong>{fabricGit.branch}</strong> in {fabricGit.repository}.</span>}
            {fabricGit?.state === "not_connected" && <span className="faint git-where"> {fabricGit.workspace ?? "The target workspace"} is not connected to Git yet, so the commit is skipped; the run says so.</span>}
          </span>
        </label>
        {options.gitCommit !== "off" && (
          <div className="stage-option" style={{ marginTop: 8 }}>
            <label htmlFor="opt-commit-scope">What the commit takes <Differs option="commitScope" /></label>
            <select id="opt-commit-scope" className="select" value={options.commitScope ?? "run"} onChange={(e) => setOptions({ commitScope: e.target.value as "run" | "all" })}>
              <option value="run">Only what the run created or updated</option>
              <option value="all">Everything waiting in the workspace</option>
            </select>
            <span className="faint">{(options.commitScope ?? "run") === "run" ? "Other changes waiting in the workspace are left for a person, and listed." : "Every change waiting in the workspace is committed with the run's, including ones the run did not make."}</span>
          </div>
        )}
      </div>
    </Card>
  );
}
