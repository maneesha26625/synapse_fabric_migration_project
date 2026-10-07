import { useAppState } from "../../state/AppState";
import { useMigration, type StepKey } from "../../state/MigrationState";
import { STEPS, type StepMeta } from "./steps";

export interface StepStatus extends StepMeta {
  index: number;
  /** Reviewed and confirmed with Next. */
  confirmed: boolean;
  /** An earlier step is not confirmed yet (or must be done again). */
  locked: boolean;
  /** Its work is finished, so Next can be pressed. */
  complete: boolean;
  running: boolean;
  /** The run is paused part-way (Migrate only). */
  paused: boolean;
  /** Finished, but with something worth a look first (failures, warnings, blocking risks). */
  attention: string | null;
  /** The one number shown on the step's box. */
  metric: string;
  /** What was achieved, for the Next bar. */
  outcome: string;
  /** What is still missing before Next, for the Next bar. */
  waiting: string;
  /**
   * Confirmed earlier, but its results are no longer here: the source was signed
   * out, the step was reset, the server restarted, or Demo data was reloaded.
   * It is the open step again, and the steps after it wait until it is redone.
   */
  stale: boolean;
  /** Why a stale step needs doing again, for the Next bar. */
  lostReason: string;
  /** A stale step's badge ("Run again", "Rebuild", "Fix risks") and the Next bar's title. */
  staleLabel: string;
  staleTitle: string;
}

type Facts = Omit<StepStatus, keyof StepMeta | "index" | "confirmed" | "locked" | "stale" | "lostReason" | "staleLabel" | "staleTitle">;
interface Lost { reason: string; label: string; title: string }

const n = (v: number) => v.toLocaleString();
const plural = (count: number, one: string, many = `${one}s`) => `${n(count)} ${count === 1 ? one : many}`;

/** Every step's state, from the live discovery, plan, run and validation. */
export function useJourney(): { steps: StepStatus[]; current: StepKey; finished: boolean } {
  const { discovery, ready, mode } = useAppState();
  const { graph, graphLoading, graphError, plan, analysis, analysisBusy, execution: run, executionReady, validation, validationBusy, confirmed } = useMigration();
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";
  const summary = discovery.summary;
  const total = summary?.total ?? 0;
  const cls = summary?.byClassification ?? {};

  const facts: Record<StepKey, Facts> = {
    discover: {
      complete: discovered,
      running: discovery.state === "running",
      paused: false,
      attention: discovery.state === "completed_with_warnings" ? "Completed with warnings" : null,
      metric: discovery.state === "running" ? "Discovering…" : discovered ? plural(total, "object") : discovery.state === "failed" ? "Failed" : "Not started",
      outcome: discovered && summary ? `${plural(total, "object")} found across ${plural(Object.keys(summary.byType).length, "object type")}${summary.warningCount ? `, with ${plural(summary.warningCount, "warning")}` : ""}.` : "",
      waiting: discovery.state === "running" ? "Discovery is reading the workspace…" : discovery.state === "failed" ? "Discovery failed. Run it again." : "Run discovery to read the source workspace.",
    },
    assess: {
      complete: discovered,
      running: false,
      paused: false,
      attention: null,
      metric: discovered && total ? `${Math.round(((cls.DIRECT ?? 0) / total) * 100)}% direct` : "—",
      outcome: discovered ? `${n(cls.DIRECT ?? 0)} move directly, ${n((cls.RECONFIGURE ?? 0) + (cls.TRANSFORM ?? 0))} need changes, ${n((cls.MANUAL ?? 0) + (cls.REVIEW ?? 0))} need a person.` : "",
      waiting: "Waiting for discovery.",
    },
    waves: {
      complete: !!graph,
      running: graphLoading && !graph,
      paused: false,
      attention: null,
      metric: graph ? plural(graph.waves.length, "wave") : graphLoading ? "Building…" : "—",
      outcome: graph ? `${plural(graph.nodes.length, "object")} in ${plural(graph.waves.length, "wave")}, linked by ${plural(graph.edges.length, "dependency", "dependencies")}.` : "",
      waiting: graphError ?? (graphLoading ? "Building the dependency graph…" : "Waiting for discovery."),
    },
    plan: {
      complete: plan.length > 0 && !!analysis && analysis.blocking === 0,
      // Scoring again in the background (after an edit) keeps the last score on show: not "running".
      running: analysisBusy && !analysis,
      paused: false,
      attention: analysis?.blocking ? plural(analysis.blocking, "blocking risk") : null,
      metric: !plan.length ? "No plan yet" : analysis ? `Readiness ${analysis.readiness}` : plural(plan.length, "object"),
      outcome: analysis ? `${plural(analysis.objects, "object")} in ${plural(analysis.waves.length, "wave")}, readiness ${analysis.readiness}/100, about ${n(Math.round(analysis.effortDays * 10) / 10)} working days.` : "",
      waiting: !plan.length ? "Build the plan: add the objects to migrate." : !analysis ? "Scoring the plan…" : `Resolve the ${plural(analysis.blocking, "blocking risk")} first.`,
    },
    migrate: {
      complete: run.state === "completed",
      running: run.state === "running",
      paused: run.state === "paused",
      attention: run.failed ? plural(run.failed, "object") + " failed" : null,
      metric: run.state === "idle" ? "Not started" : run.state === "running" ? `${run.total ? Math.round(((run.completed + run.failed + (run.skipped ?? 0) + (run.deferred ?? 0)) / run.total) * 100) : 0}%` : run.state === "paused" ? "Paused" : `${n(run.completed)} migrated`,
      outcome: run.state === "completed" ? `${n(run.completed)} migrated, ${n(run.skipped ?? 0)} already there, ${n(run.deferred ?? 0)} left for a person${run.failed ? `, ${n(run.failed)} failed` : ""}.` : "",
      waiting: run.state === "running" ? "The migration is running…" : run.state === "paused" ? "The run is paused. Resume it to finish." : "Start the migration run.",
    },
    validate: {
      complete: validation !== null,
      running: validationBusy,
      paused: false,
      attention: validation?.some((r) => r.status === "MISMATCH") ? plural(validation.filter((r) => r.status === "MISMATCH").length, "mismatch", "mismatches") : null,
      metric: validationBusy ? "Comparing…" : validation ? `${n(validation.filter((r) => r.status === "MATCH").length)} match` : "Not run",
      outcome: validation ? `${n(validation.filter((r) => r.status === "MATCH").length)} match, ${n(validation.filter((r) => r.status === "REVIEW").length)} to review, ${n(validation.filter((r) => r.status === "MISMATCH").length)} mismatched.` : "",
      waiting: "Run validation to compare both sides.",
    },
  };

  // A confirmed step's results can disappear. Each check waits until its data
  // has been read once, so a page that is still loading never looks "lost".
  const lost: Record<StepKey, Lost | null> = {
    discover: ready && !discovered && discovery.state !== "running"
      ? { reason: "the discovery result is no longer here: the source was signed out or reconnected, discovery was reset, or the server restarted.", label: "Run again", title: "Discover needs to run again" }
      : null,
    assess: null,
    waves: null,
    plan: plan.length === 0
      ? { reason: "the plan is empty now.", label: "Rebuild", title: "The plan needs rebuilding" }
      : analysis && analysis.blocking > 0
        ? { reason: `the plan has changed and now has ${plural(analysis.blocking, "blocking risk")}.`, label: "Fix risks", title: "The plan has blocking risks" }
        : null,
    migrate: executionReady && run.state === "idle"
      ? { reason: mode === "mock" ? "Demo data starts afresh when the page reloads, so the run's record is gone." : "the run's record is gone: it was reset, or the server restarted.", label: "Run again", title: "Migrate needs to run again" }
      : null,
    validate: validation === null && !validationBusy
      ? { reason: "validation results are kept only while the page is open.", label: "Run again", title: "Validate needs to run again" }
      : null,
  };

  const isStale = (key: StepKey) => confirmed.includes(key) && !!lost[key];
  const firstOpen = STEPS.findIndex((s) => !confirmed.includes(s.key) || isStale(s.key));
  const open = firstOpen === -1 ? STEPS.length - 1 : firstOpen;
  const steps = STEPS.map((meta, index) => ({
    ...meta,
    ...facts[meta.key],
    index,
    confirmed: confirmed.includes(meta.key) && index <= open,
    locked: index > open,
    stale: index === open && isStale(meta.key),
    lostReason: lost[meta.key]?.reason ?? "",
    staleLabel: lost[meta.key]?.label ?? "Run again",
    staleTitle: lost[meta.key]?.title ?? `${meta.title} needs doing again`,
  }));
  return { steps, current: STEPS[open].key, finished: firstOpen === -1 };
}
