/** How the consultation's values read to a person. */

export const show = (v: unknown): string => v === null || v === undefined ? "unknown" : typeof v === "boolean" ? (v ? "yes" : "no")
  : Array.isArray(v) ? v.join(", ") || "none" : typeof v === "object" ? Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k} ${show(x)}`).join(", ") || "none"
    : typeof v === "number" ? v.toLocaleString() : String(v);

const isMap = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/** A fact that changed, as "before → after"; for counts by kind, only the kinds that moved ("SQL Script 12 → 11"). */
export function changeText(before: unknown, after: unknown): string {
  if (isMap(before) && isMap(after)) {
    const moved = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()
      .filter((k) => JSON.stringify(before[k] ?? null) !== JSON.stringify(after[k] ?? null))
      .map((k) => `${k} ${show(before[k] ?? 0)} → ${show(after[k] ?? 0)}`);
    return moved.join("; ") || "reordered";
  }
  return `${show(before)} → ${show(after)}`;
}

/** A scope group as a person reads it: "schema:sales" is the schema sales. */
export const groupLabel = (v: string) => v.startsWith("schema:") ? `Schema ${v.slice(7)}` : v.startsWith("folder:") ? `Folder ${v.slice(7)}` : v;
