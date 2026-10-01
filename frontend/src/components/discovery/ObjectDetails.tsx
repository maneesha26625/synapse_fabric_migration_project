import { AlertTriangle, ArrowRight, CheckCircle2, Download, FileCode2, ListPlus, Route } from "lucide-react";
import { Fragment, useEffect, useState, type ReactNode } from "react";
import { useAppState } from "../../state/AppState";
import { useMigration } from "../../state/MigrationState";
import { ClassificationBadge } from "../shared/Metrics";
import type { ObjectDetail } from "../../types";
import { Banner, Button, Collapsible, Drawer, JsonViewer, Skeleton } from "../shared/Shared";
import { DependencyView } from "./DependencyView";
import { AssessmentBadge, MappingStatusBadge, ObjectStatusBadge, PathBadge } from "./ObjectStatusBadge";

type Tab = "overview" | "dependencies" | "metadata" | "activities" | "raw";

const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
const isPlain = (v: unknown) => v === null || ["string", "number", "boolean"].includes(typeof v);

/** Scalars inline; anything structured stays collapsed until asked for. */
function Properties({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data).filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (!entries.length) return <p className="muted">Nothing was reported.</p>;
  return (
    <dl className="kv">
      {entries.map(([k, v]) => (
        <Fragment key={k}>
          <dt>{k}</dt>
          <dd>{isPlain(v) ? String(v) : <Collapsible summary={Array.isArray(v) ? `${v.length} item${v.length === 1 ? "" : "s"}` : "Details"}><JsonViewer value={v} /></Collapsible>}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="stack" style={{ gap: 10 }}>
      <h3 className="eyebrow" style={{ margin: 0 }}>{title}</h3>
      {children}
    </section>
  );
}

function Overview({ o, onTab }: { o: ObjectDetail; onTab: (tab: Tab) => void }) {
  const constructs = o.overview.synapseSpecificConstructs as string[] | undefined;
  const { addToPlan, inPlan } = useMigration();
  const exportObject = () => {
    const blob = new Blob([JSON.stringify({ ...o, rawMetadata: undefined }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${o.name.replace(/[^\w.-]+/g, "_")}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="stack" style={{ gap: 24 }}>
      <div className="row">
        <Button size="small" onClick={() => onTab("metadata")}><FileCode2 size={14} aria-hidden="true" />View Source Definition</Button>
        <Button size="small" onClick={() => onTab("dependencies")}><Route size={14} aria-hidden="true" />View Mapping</Button>
        <Button size="small" onClick={exportObject}><Download size={14} aria-hidden="true" />Export</Button>
        <Button size="small" variant="primary" disabled={inPlan(o.id)} onClick={() => addToPlan([o.id])}><ListPlus size={14} aria-hidden="true" />{inPlan(o.id) ? "In Migration Plan" : "Add to Migration Plan"}</Button>
      </div>
      <Section title="Source object">
        <dl className="kv">
          <dt>Name</dt><dd>{o.name}</dd>
          <dt>Type</dt><dd>{o.type}</dd>
          <dt>Category</dt><dd>{o.category}</dd>
          <dt>Synapse workspace</dt><dd>{o.workspace || "—"}</dd>
          <dt>Discovery status</dt><dd><ObjectStatusBadge status={o.status} /></dd>
          <dt>Discovered</dt><dd>{fmt(o.discoveredAt)}</dd>
        </dl>
      </Section>

      <Section title="Fabric target">
        <div className="route" aria-label="Preliminary route">
          <span>Synapse {o.type}</span><ArrowRight size={16} aria-hidden="true" /><strong>{o.target.component}</strong>
        </div>
        <dl className="kv">
          <dt>Target</dt><dd>{o.target.component}</dd>
          <dt>Target type</dt><dd>{o.target.componentType}</dd>
          <dt>Platform</dt><dd>{o.target.platform}</dd>
          <dt>Classification</dt><dd><ClassificationBadge value={o.migration.classification} /></dd>
          <dt>Migration path</dt><dd><PathBadge path={o.migration.path} /></dd>
          <dt>Migration action</dt><dd>{o.migration.action}</dd>
          <dt>Mapping status</dt><dd><MappingStatusBadge status={o.migration.mappingStatus} /></dd>
          <dt>Workstream</dt><dd>{o.migration.workstream}</dd>
          <dt>Automation potential</dt><dd>{o.migration.automationPotential}</dd>
          <dt>Assessment</dt><dd><AssessmentBadge required={o.migration.assessmentRequired} /></dd>
        </dl>
        <p className="faint">Preliminary. Discovery maps by object type and does not decide compatibility; Assessment does.</p>
      </Section>

      <Section title="Dependencies">
        {o.dependencies.length === 0 ? <p className="muted">None discovered.</p> : (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {o.dependencies.slice(0, 8).map((d) => <li key={`${d.kind}|${d.name}`}>{d.name} <span className="faint">({d.type ?? d.kind})</span></li>)}
            {o.dependencies.length > 8 && <li className="faint">…and {o.dependencies.length - 8} more (see the Dependencies tab)</li>}
          </ul>
        )}
      </Section>

      <Section title="Migration actions">
        <ul className="checklist">
          {o.actions.map((a) => (
            <li key={a.label} className={a.state}>
              {a.state === "ok" ? <CheckCircle2 size={16} aria-label="ok" /> : <AlertTriangle size={16} aria-label="needs attention" />}
              {a.label}
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Recommended migration steps">
        <ol style={{ margin: 0, paddingLeft: 20 }}>{o.steps.map((s) => <li key={s}>{s}</li>)}</ol>
        <p className="faint">Guidance for the type of object. Nothing here is executed from this panel.</p>
      </Section>

      <Section title="Migration notes">
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {o.notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
        {constructs && constructs.length > 0 && (
          <Banner tone="info" title="Synapse-specific constructs found">{constructs.join(", ")}</Banner>
        )}
        {o.issues.map((i, n) => (
          <Banner key={n} tone={i.code === "missing_information" ? "info" : "warning"} title={i.code.replace(/_/g, " ")}>{i.message}</Banner>
        ))}
      </Section>
    </div>
  );
}

function Activities({ o }: { o: ObjectDetail }) {
  return (
    <div className="stack">
      <p className="muted">Each activity with its preliminary Fabric Data Factory equivalent. "Requires Assessment" means no established equivalent is recorded.</p>
      <div className="table-wrap">
        <table className="data" style={{ minWidth: 640 }}>
          <caption className="sr-only">Pipeline activities and Fabric equivalents</caption>
          <thead>
            <tr>{["Activity", "Type", "Fabric equivalent", "Transformation", "Manual review"].map((h) => <th key={h} scope="col" className="static">{h}</th>)}</tr>
          </thead>
          <tbody>
            {o.activities.map((a, i) => (
              <tr key={`${a.name}-${i}`} title={a.note}>
                <td className="name">{a.name}</td>
                <td>{a.type}</td>
                <td>{a.fabricEquivalent}{a.equivalence === "Requires Assessment" && <span className="faint"> (no established equivalent)</span>}</td>
                <td>{a.requiresTransformation ? "May be needed" : "—"}</td>
                <td>{a.requiresManualReview ? "Yes" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ObjectDetails({ id, onClose, onNavigate }: { id: string; onClose: () => void; onNavigate: (id: string) => void }) {
  const { getObject } = useAppState();
  const [object, setObject] = useState<ObjectDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setObject(null);
    setError(null);
    setTab("overview");
    getObject(id).then(
      (o) => !cancelled && setObject(o),
      (e) => !cancelled && setError(e instanceof Error ? e.message : "The object could not be loaded."),
    );
    return () => { cancelled = true; };
  }, [id, getObject, attempt]);

  const tabs: { id: Tab; label: string }[] = [
    { id: "overview", label: "Overview" },
    { id: "dependencies", label: "Dependencies" },
    { id: "metadata", label: "Source metadata" },
    ...(object && object.activities.length ? [{ id: "activities" as Tab, label: `Activities (${object.activities.length})` }] : []),
    { id: "raw", label: "Raw metadata" },
  ];

  let body: ReactNode;
  if (error) {
    body = <Banner tone="error" title="Could not load this object" actions={<Button size="small" onClick={() => setAttempt((n) => n + 1)}>Retry</Button>}>{error}</Banner>;
  } else if (!object) {
    body = <div className="stack"><Skeleton width="60%" height={18} /><Skeleton /><Skeleton /><Skeleton width="80%" /></div>;
  } else if (tab === "overview") body = <Overview o={object} onTab={setTab} />;
  else if (tab === "dependencies") body = <DependencyView root={object} onOpen={onNavigate} />;
  else if (tab === "metadata") body = object.configuration ? <Properties data={object.configuration} /> : <p className="muted">No source metadata was extracted for this object{object.status === "Failed" ? " because its definition is unavailable" : ""}.</p>;
  else if (tab === "activities") body = <Activities o={object} />;
  else body = <Collapsible summary="Show raw metadata (JSON)"><JsonViewer value={object.rawMetadata} /></Collapsible>;

  return (
    <Drawer
      label="Object details"
      onClose={onClose}
      header={
        <>
          <div className="eyebrow">Object details</div>
          <h2 style={{ overflowWrap: "anywhere" }}>{object?.name ?? "Loading…"}</h2>
          {object && <div className="muted">{object.type} → {object.fabricTarget}</div>}
        </>
      }
    >
      <div className="tabs" role="tablist" aria-label="Object detail sections">
        {tabs.map((t) => (
          <button key={t.id} role="tab" type="button" className="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </div>
      <div className="drawer-body" role="tabpanel">{body}</div>
    </Drawer>
  );
}
