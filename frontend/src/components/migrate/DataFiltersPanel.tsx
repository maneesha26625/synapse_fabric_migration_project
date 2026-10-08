import { CalendarRange, CircleAlert } from "lucide-react";
import { useMemo, useState } from "react";
import { Button, Collapsible } from "../shared/Shared";
import { useMigration } from "../../state/MigrationState";
import type { DataFilterInput, FilterableTable } from "../../types";

const EMPTY: DataFilterInput = { column: "", from: "", to: "", sync: false, changeColumn: "", keys: [] };
/** How many tables are listed at once; the search narrows the rest. */
const SHOWN = 50;

/** The column most likely to move when a row changes: modified/updated/changed, else the filter's own column. */
function guessChangeColumn(table: FilterableTable, column: string): string {
  const named = table.dateColumns.find((c) => /modif|updat|chang|last/i.test(c.name));
  return named?.name ?? (column || table.dateColumns[0]?.name || "");
}

/** What is wrong with a filter (blocks the run), or what to watch for (does not). Mirrors the backend's checks. */
function issueOf(table: FilterableTable, f: DataFilterInput): { text: string; blocking: boolean } | null {
  if (f.column && !f.from && !f.to) return { text: "Give a start date, an end date or both.", blocking: true };
  if (f.from && f.to && f.from >= f.to) return { text: "The start date must be before the end date.", blocking: true };
  if (f.sync && f.to) return { text: "A table kept in sync keeps receiving rows, so it cannot have an end date.", blocking: true };
  if (f.sync && !f.keys.length && !table.keyColumns.length) {
    return { text: "No key columns: a changed row arrives as a second row. Enter the columns that identify a row.", blocking: false };
  }
  return null;
}

/**
 * Which rows each table loads. A table with no filter loads every row, as before. Dates are fixed
 * when the plan is made, so every run of the plan loads the same rows and validation compares like with like.
 */
export function DataFiltersPanel({ tables }: { tables: FilterableTable[] }) {
  const { options, setOptions } = useMigration();
  const filters = options.dataFilters ?? {};
  const [query, setQuery] = useState("");
  const [bulk, setBulk] = useState({ column: "", from: "", to: "", sync: false, changeColumn: "" });

  const set = (table: FilterableTable, patch: Partial<DataFilterInput>) => {
    const next = { ...filters };
    const merged = { ...EMPTY, ...(next[table.key] ?? {}), ...patch };
    if (patch.sync && !merged.changeColumn) merged.changeColumn = guessChangeColumn(table, merged.column);
    if (patch.sync && !merged.keys.length) merged.keys = [...table.keyColumns];
    if (!merged.column && !merged.sync) delete next[table.key];
    else next[table.key] = merged;
    setOptions({ dataFilters: next });
  };
  const clear = (table: FilterableTable) => {
    const next = { ...filters };
    delete next[table.key];
    setOptions({ dataFilters: next });
  };

  /** Date column names across the plan's tables, the most common first: what "apply to every table" offers. */
  const common = useMemo(() => {
    const counts = new Map<string, number>();
    for (const t of tables) for (const c of t.dateColumns) counts.set(c.name, (counts.get(c.name) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [tables]);

  const applyToAll = () => {
    const next = { ...filters };
    for (const t of tables) {
      const column = t.dateColumns.find((c) => c.name.toLowerCase() === bulk.column.toLowerCase())?.name;
      if (!column) continue;
      const change = bulk.sync ? (t.dateColumns.find((c) => c.name.toLowerCase() === bulk.changeColumn.toLowerCase())?.name ?? guessChangeColumn(t, column)) : "";
      next[t.key] = { column, from: bulk.from, to: bulk.sync ? "" : bulk.to, sync: bulk.sync, changeColumn: change, keys: bulk.sync ? [...t.keyColumns] : [] };
    }
    setOptions({ dataFilters: next });
  };
  const reach = tables.filter((t) => t.dateColumns.some((c) => c.name.toLowerCase() === bulk.column.toLowerCase())).length;

  const q = query.trim().toLowerCase();
  const matching = tables.filter((t) => !q || t.key.includes(q));
  const shown = matching.slice(0, SHOWN);
  const filtered = tables.filter((t) => filters[t.key]).length;
  const blocking = tables.filter((t) => filters[t.key] && issueOf(t, filters[t.key])?.blocking).length;

  return (
    <section className="data-filters" aria-label="Date filters">
      <Collapsible summary={<span><CalendarRange size={14} aria-hidden="true" /> Date filters for table data <span className="muted">· {filtered} of {tables.length} tables filtered{blocking ? ` · ${blocking} to fix` : ""}</span></span>}>
        <p className="muted" style={{ marginTop: 0 }}>
          Load only part of a table, for example the rows from a given date onwards. A table with no filter loads every row.
          <strong> Keep in sync</strong> loads new and changed rows after the first load: the sync pipeline reads rows whose change column
          (such as ModifiedAt) moved since the last sync, replaces rows with the same key columns, and adds the rest. It is created with a daily
          schedule switched off. Rows deleted in Synapse are not removed; soft deletes that move the change column are carried over.
        </p>

        <div className="data-filters-bulk" role="group" aria-label="Set a filter for every table with a column">
          <label className="field"><span>For every table with column</span>
            <select className="select" aria-label="Column to filter every table by" value={bulk.column} onChange={(e) => setBulk({ ...bulk, column: e.target.value })}>
              <option value="">Choose a column…</option>
              {common.map(([name, n]) => <option key={name} value={name}>{name} ({n} table{n === 1 ? "" : "s"})</option>)}
            </select>
          </label>
          <label className="field"><span>From</span>
            <input className="input" type="date" aria-label="From date for every table" value={bulk.from} onChange={(e) => setBulk({ ...bulk, from: e.target.value })} />
          </label>
          <label className="field"><span>Until (not included)</span>
            <input className="input" type="date" aria-label="Until date for every table" value={bulk.to} disabled={bulk.sync} onChange={(e) => setBulk({ ...bulk, to: e.target.value })} />
          </label>
          <label className="check-row" style={{ marginTop: 22 }}>
            <input type="checkbox" checked={bulk.sync} aria-label="Keep every one of them in sync" onChange={(e) => setBulk({ ...bulk, sync: e.target.checked, to: e.target.checked ? "" : bulk.to })} />
            <span>Keep in sync</span>
          </label>
          {bulk.sync && (
            <label className="field"><span>Change column</span>
              <select className="select" aria-label="Change column for every table" value={bulk.changeColumn} onChange={(e) => setBulk({ ...bulk, changeColumn: e.target.value })}>
                <option value="">Best guess per table</option>
                {common.map(([name]) => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
          )}
          <Button size="small" disabled={!bulk.column || !(bulk.from || bulk.to)} onClick={applyToAll}>
            {bulk.column ? `Apply to ${reach} table${reach === 1 ? "" : "s"}` : "Apply"}
          </Button>
          {filtered > 0 && <Button size="small" variant="ghost" onClick={() => setOptions({ dataFilters: {} })}>Clear all filters</Button>}
        </div>

        <div className="toolbar" style={{ margin: "12px 0 8px" }}>
          <input className="input" style={{ maxWidth: 320 }} placeholder="Find a table" aria-label="Find a table to filter" value={query} onChange={(e) => setQuery(e.target.value)} />
          <span className="faint">{matching.length > SHOWN ? `Showing ${SHOWN} of ${matching.length}; search to narrow.` : `${matching.length} table${matching.length === 1 ? "" : "s"} with a date column.`}</span>
        </div>
        <div className="table-wrap">
          <table className="data" style={{ minWidth: 1080 }}>
            <thead><tr>
              <th><span className="th">Table</span></th><th><span className="th">Date column</span></th><th><span className="th">From</span></th>
              <th><span className="th">Until</span></th><th><span className="th">Keep in sync</span></th><th><span className="th">Change column</span></th>
              <th><span className="th">Key columns</span></th><th aria-label="Actions" />
            </tr></thead>
            <tbody>
              {shown.map((t) => {
                const f = filters[t.key] ?? EMPTY;
                const issue = filters[t.key] ? issueOf(t, f) : null;
                const label = `${t.schema}.${t.name}`;
                return (
                  <tr key={t.key}>
                    <td className="name" title={label}>
                      {label}
                      {issue && <div className={issue.blocking ? "err" : "faint"} style={{ whiteSpace: "normal", fontWeight: 400, maxWidth: 300 }}><CircleAlert size={12} aria-hidden="true" /> {issue.text}</div>}
                    </td>
                    <td>
                      <select className="select" aria-label={`Date column for ${label}`} value={f.column} onChange={(e) => set(t, { column: e.target.value, ...(e.target.value ? {} : { from: "", to: "" }) })}>
                        <option value="">Every row</option>
                        {t.dateColumns.map((c) => <option key={c.name} value={c.name}>{c.name} ({c.type})</option>)}
                      </select>
                    </td>
                    <td><input className="input" type="date" aria-label={`From date for ${label}`} disabled={!f.column} value={f.from} onChange={(e) => set(t, { from: e.target.value })} /></td>
                    <td><input className="input" type="date" aria-label={`Until date for ${label}`} disabled={!f.column || f.sync} value={f.to} onChange={(e) => set(t, { to: e.target.value })} /></td>
                    <td style={{ textAlign: "center" }}>
                      <input type="checkbox" aria-label={`Keep ${label} in sync`} checked={f.sync} onChange={(e) => set(t, { sync: e.target.checked, ...(e.target.checked ? { to: "" } : {}) })} />
                    </td>
                    <td>
                      <select className="select" aria-label={`Change column for ${label}`} disabled={!f.sync} value={f.sync ? f.changeColumn : ""} onChange={(e) => set(t, { changeColumn: e.target.value })}>
                        {!f.sync && <option value="">—</option>}
                        {t.dateColumns.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                      </select>
                    </td>
                    <td>
                      {/* Committed on blur, so a comma can be typed between names. */}
                      <input key={`${t.key}:${f.keys.join(",")}`} className="input" style={{ minWidth: 150 }} aria-label={`Key columns for ${label}`} disabled={!f.sync}
                        placeholder={t.keyColumns.length ? t.keyColumns.join(", ") : "none: append only"} defaultValue={f.keys.join(", ")}
                        onBlur={(e) => set(t, { keys: e.target.value.split(",").map((k) => k.trim()).filter(Boolean) })} />
                    </td>
                    <td>{filters[t.key] && <Button size="small" variant="ghost" aria-label={`Clear the filter for ${label}`} onClick={() => clear(t)}>Clear</Button>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Collapsible>
    </section>
  );
}
