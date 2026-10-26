import { useEffect, useState } from "react";
import { useAppState } from "../../state/AppState";
import type { CapacityAssessment } from "../../types";
import { StatStrip } from "../shared/Metrics";
import { Banner, Button } from "../shared/Shared";

const messageOf = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong.");

/** Plan → Capacity: a Fabric capacity starting point from the discovered estate, with its evidence and assumptions. */
export function CapacityPanel() {
  const { api } = useAppState();
  const [data, setData] = useState<CapacityAssessment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => {
    setBusy(true);
    setError(null);
    api.getCapacity().then(setData, (e) => setError(messageOf(e))).finally(() => setBusy(false));
  };
  useEffect(load, [api]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <Banner tone="error" title="The capacity assessment could not be made" actions={<Button onClick={load}>Try again</Button>}>{error}</Banner>;
  if (!data) return <p className="muted"><span className="spinner" aria-hidden="true" /> Assessing capacity…</p>;
  const rec = data.recommendation;
  return (
    <div className="stack">
      <StatStrip label="Capacity assessment" items={[
        { label: "Starting point", value: rec.sku ?? "—", hint: rec.cu ? `${rec.cu} capacity units` : "Nothing measurable discovered", tone: rec.sku ? "success" : undefined },
        { label: "SQL and Spark at once", value: rec.togetherSku ?? "—", hint: "If they run at the same time" },
        { label: "SQL", value: data.sql.cu != null ? `${data.sql.cu} CU` : "—", hint: data.sql.dwu ? `DW${data.sql.dwu}c, ${data.sql.basis}` : "No pool size" },
        { label: "Spark", value: data.spark.cu != null ? `${data.spark.cu} CU` : "—", hint: data.spark.vcores ? `${data.spark.vcores} vCores, ${data.spark.basis}` : "No Spark pool" },
        { label: "Data to load", value: data.data ? `${data.data.gb.toLocaleString()} GB` : "—", hint: data.data ? `${data.data.rows.toLocaleString()} rows` : "Sizes not read" },
        { label: "Peak SQL concurrency", value: data.workload?.peakConcurrency ?? "—", hint: data.workload ? `${data.workload.requests.toLocaleString()} recent requests` : "History not read" },
      ]} />
      <p className="muted" style={{ margin: 0 }}>{rec.why}</p>
      {data.target && (
        <Banner tone={data.target.fits === false ? "warning" : data.target.fits ? "success" : "info"} title={`The target workspace runs on ${data.target.sku}`}>
          {data.target.detail}
        </Banner>
      )}
      {data.evidence.length > 0 && (
        <div>
          <h4 style={{ margin: "4px 0" }}>Evidence</h4>
          <ul className="muted" style={{ margin: 0 }}>{data.evidence.map((e) => <li key={e}>{e}</li>)}</ul>
        </div>
      )}
      {data.data?.largest.length ? (
        <div className="table-wrap">
          <table className="data">
            <caption className="sr-only">Largest tables</caption>
            <thead><tr><th scope="col" className="static">Largest tables</th><th scope="col" className="static">GB</th><th scope="col" className="static">Rows</th></tr></thead>
            <tbody>{data.data.largest.map((t) => <tr key={t.table}><td className="name">{t.table}</td><td>{t.gb.toLocaleString()}</td><td>{t.rows.toLocaleString()}</td></tr>)}</tbody>
          </table>
        </div>
      ) : null}
      <div>
        <h4 style={{ margin: "4px 0" }}>Assumptions</h4>
        <ul className="faint" style={{ margin: 0 }}>{data.caveats.map((c) => <li key={c}>{c}</li>)}</ul>
      </div>
      <div><Button onClick={load} loading={busy}>Assess again</Button></div>
    </div>
  );
}
