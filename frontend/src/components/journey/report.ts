import type { ExecutionRun, ValidationRow } from "../../types";

interface ReportInput {
  run: ExecutionRun;
  validation: ValidationRow[] | null;
  project: string;
  source: string | null;
  target: string | null;
}

/** One CSV field: quoted, with quotes doubled, so commas and line breaks survive. */
const field = (value: unknown) => `"${String(value ?? "").replace(/"/g, '""')}"`;
const line = (values: unknown[]) => values.map(field).join(",");

/**
 * The migration's outcome as one CSV a spreadsheet opens: every object the run
 * handled, then every validation check. Built in the browser from what the page
 * already shows; nothing is fetched and no credential is ever part of it.
 */
export function buildReport({ run, validation, project, source, target }: ReportInput): string {
  const rows: string[] = [
    line(["Migration report", project]),
    line(["Source", source ?? ""]),
    line(["Destination", target ?? ""]),
    line(["Run", run.runId ? `#${run.runId}` : "none"]),
    line(["Created", new Date().toISOString()]),
    "",
    line(["Section", "Object", "Type", "Wave", "Status", "Result", "Fabric target", "Details", "Completed"]),
  ];
  for (const i of run.items) {
    rows.push(line(["Migration", i.name, i.type, i.wave ?? "", i.status, i.step ?? "", i.target ?? "", i.error ?? (i.notes ?? []).join(" "), i.completedAt ?? ""]));
  }
  if (validation?.length) {
    rows.push("", line(["Section", "Object", "Category", "Synapse", "Fabric", "Status", "Details"]));
    for (const v of validation) rows.push(line(["Validation", v.object, v.category, v.source, v.target, v.status, v.detail ?? ""]));
  }
  return rows.join("\r\n");
}

/** Saves the report as a file, named after the project and today's date. */
export function downloadReport(input: ReportInput): void {
  const slug = input.project.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "migration";
  // The byte-order mark makes Excel read the file as UTF-8.
  const blob = new Blob(["﻿" + buildReport(input)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${slug}-report-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
