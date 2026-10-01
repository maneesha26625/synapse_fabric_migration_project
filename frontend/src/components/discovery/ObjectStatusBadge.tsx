import type { MappingStatus, MigrationPath, ObjectStatus } from "../../types";
import { StatusBadge, type Tone } from "../shared/Shared";

// Discovery status says only whether the object was read. Nothing here rates
// an object or says anything about how hard it is to migrate.
const DISCOVERY_TONE: Record<ObjectStatus, Tone> = {
  Discovered: "success",
  Warning: "warning",
  Partial: "warning",
  Failed: "error",
};

export function ObjectStatusBadge({ status }: { status: ObjectStatus }) {
  return <StatusBadge tone={DISCOVERY_TONE[status] ?? "plain"}>{status}</StatusBadge>;
}

// Mapping status says how far the *mapping* got. Deliberately not red/green:
// "Requires Assessment" is not a failure, and "Mapped" is not a promise.
const MAPPING_TONE: Record<MappingStatus, Tone> = {
  Mapped: "info",
  "Mapped with Transformation": "info",
  "Requires Assessment": "warning",
  "No Automatic Mapping": "neutral",
};

export function MappingStatusBadge({ status }: { status: MappingStatus }) {
  return <StatusBadge tone={MAPPING_TONE[status] ?? "plain"}>{status}</StatusBadge>;
}

/** The six preliminary classifications, as plain text with a tint. */
export function PathBadge({ path }: { path: MigrationPath }) {
  const tone: Tone = path === "Direct Target" ? "info" : path === "Requires Assessment" ? "warning" : path === "Manual / Special Handling" ? "neutral" : "plain";
  return <StatusBadge tone={tone}>{path}</StatusBadge>;
}

export function AssessmentBadge({ required }: { required: boolean }) {
  return <StatusBadge tone={required ? "warning" : "neutral"}>{required ? "Required" : "Not required"}</StatusBadge>;
}
