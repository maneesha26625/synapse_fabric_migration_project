// Demo "does it run in Fabric?" checks for validation rows. Deterministic per object, a few failures on purpose.
import type { RunnableKind, RuntimeCheck, ValidationRow, ValidationRuntimeOptions } from "../types";

export const RUN_KIND: Record<string, RunnableKind> = {
  Pipeline: "pipeline", Notebook: "notebook", "SQL Script": "script", "Spark Job Definition": "sparkjob", "Stored Procedure": "procedure",
};
const LAST_RUN = new Set(["Pipeline", "Notebook", "SQL Script", "Spark Job Definition"]);

function check(type: string, h: number, opts: ValidationRuntimeOptions): RuntimeCheck | null {
  const when = new Date(Date.now() - (h % 72) * 3_600_000).toISOString();
  if (type === "Table" || type === "View") {
    if (!opts.probe) return null;
    const what = type === "View" ? "view" : "table";
    if (type === "View" && h % 17 === 0) {
      return { status: "FAIL", check: `Queried the ${what}`, detail: "The Warehouse rejected SELECT TOP (1) *: Invalid object name 'stg.Customers' (demo)." };
    }
    return { status: "PASS", check: `Queried the ${what}`, detail: "SELECT TOP (1) * ran without an error (demo)." };
  }
  if (type === "Stored Procedure") {
    const execute = opts.run.includes("procedure");
    if (!opts.probe && !execute) return null;
    if (h % 15 === 0) return { status: "FAIL", check: execute ? "Executed (rolled back)" : "Compiled", detail: "Invalid column name 'LoadDt' (demo)." };
    return execute
      ? { status: "PASS", check: "Executed (rolled back)", detail: "Ran without an error; its changes were rolled back (demo)." }
      : { status: "PASS", check: "Compiled", detail: "It compiles against the Warehouse's current objects (demo). Execute it to be sure every branch runs." };
  }
  if (LAST_RUN.has(type)) {
    if (opts.run.includes(RUN_KIND[type])) {
      return h % 9 === 0
        ? { status: "FAIL", check: "Ran now in Fabric", detail: "Failed: Notebook cell 3 raised Py4JJavaError: Path does not exist (demo).", runAt: new Date().toISOString() }
        : { status: "PASS", check: "Ran now in Fabric", detail: "Succeeded (demo).", runAt: new Date().toISOString() };
    }
    if (h % 3 === 0) return { status: "NOT RUN", check: "Last run in Fabric", detail: "It has never run in Fabric. Run it there, or choose to run it from this step (demo)." };
    if (h % 11 === 0) return { status: "FAIL", check: "Last run in Fabric", detail: "Failed: the connection 'LS_ADLS' was not authorized (demo).", runAt: when };
    return { status: "PASS", check: "Last run in Fabric", detail: `Succeeded (${when}, demo).`, runAt: when };
  }
  return null;
}

/** Same rule as the server: a failed run check is a MISMATCH; never run is not a MATCH. */
export function applyDemoRuntime(row: ValidationRow, type: string, h: number, opts: ValidationRuntimeOptions): void {
  if (row.category === "Data Count" || row.category === "Manual") return;
  const rt = check(type, h, opts);
  if (!rt) return;
  row.runtime = rt;
  if (rt.status === "FAIL") row.status = "MISMATCH";
  else if (rt.status === "NOT RUN" && row.status === "MATCH") row.status = "REVIEW";
}
