import { ListChecks, Network, Radar, Rocket, ScanSearch, ShieldCheck, type LucideIcon } from "lucide-react";
import type { StepKey } from "../../state/MigrationState";

export interface StepMeta {
  key: StepKey;
  title: string;
  /** What the step is for, in one line. */
  purpose: string;
  icon: LucideIcon;
}

export const STEPS: StepMeta[] = [
  { key: "discover", title: "Discover", purpose: "Read everything in the source workspace", icon: Radar },
  { key: "assess", title: "Assess", purpose: "See how each object moves to the destination", icon: ScanSearch },
  { key: "waves", title: "Waves", purpose: "Order objects by what they depend on", icon: Network },
  { key: "plan", title: "Plan", purpose: "Choose what moves, and check it is ready", icon: ListChecks },
  { key: "migrate", title: "Migrate", purpose: "Create everything in the destination", icon: Rocket },
  { key: "validate", title: "Validate", purpose: "Compare the source with the destination", icon: ShieldCheck },
];

export const stepMeta = (key: StepKey) => STEPS.find((s) => s.key === key)!;

/** Where a deep link or an old page name points in the journey. */
export const LEGACY_STEP: Record<string, StepKey> = {
  discovery: "discover", assessment: "assess", dependencies: "waves", plan: "plan",
  migrate: "migrate", execute: "migrate", execution: "migrate", validate: "validate", validation: "validate",
};
