import { TARGET_LABEL, useMigrationOptional } from "../../state/MigrationState";
import type { SqlTarget } from "../../types";

/** Where the SQL pool goes, chosen where code is converted. The same run option as Plan's Warehouse & schema stage. */
export function SqlTargetPicker({ id = "sql-target" }: { id?: string }) {
  const migration = useMigrationOptional();
  if (!migration) return null;
  const target = migration.options.target ?? "warehouse";
  return (
    <div className="stage-option">
      <label htmlFor={id}>Convert for</label>
      <select id={id} className="select" value={target}
        onChange={(e) => migration.setOptions({ target: e.target.value as SqlTarget })}>
        {(Object.keys(TARGET_LABEL) as SqlTarget[]).map((t) => <option key={t} value={t}>{TARGET_LABEL[t]}</option>)}
      </select>
      <span className="faint">
        {target === "lakehouse"
          ? "Tables become Delta tables, procedures and scripts become Spark notebooks; views and functions go to the SQL analytics endpoint."
          : "Tables, views, functions and procedures stay T-SQL in the Warehouse."}{" "}
        The same choice as Plan (Warehouse &amp; schema stage); each target keeps its own drafts.
      </span>
    </div>
  );
}
