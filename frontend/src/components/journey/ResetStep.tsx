import { RotateCcw } from "lucide-react";
import { useCallback, useState } from "react";
import { useAppState } from "../../state/AppState";
import { STEP_KEYS, useMigration, type StepKey } from "../../state/MigrationState";
import { Button, ConfirmDialog } from "../shared/Shared";
import { notify } from "../shared/notify";
import { stepMeta } from "./steps";

const n = (v: number) => v.toLocaleString();

/** What resetting ``step`` would clear, and why it cannot be done right now. */
export function useResetPlan(step: StepKey) {
  const { discovery, isConnected } = useAppState();
  const { plan, credentials, execution: run, validation, validationBusy, confirmed, projects } = useMigration();
  const from = STEP_KEYS.indexOf(step);
  const covers = (k: StepKey) => from <= STEP_KEYS.indexOf(k);
  const discovered = discovery.state === "completed" || discovery.state === "completed_with_warnings";

  const clears: string[] = [];
  if (step === "discover" && discovered) clears.push(`The discovered inventory: ${n(discovery.summary?.total ?? 0)} objects${discovery.workspace ? ` from ${discovery.workspace}` : ""}.`);
  if (step === "discover" && discovery.state === "failed") clears.push("The failed discovery attempt.");
  if (step === "assess" && discovered) clears.push("Nothing of its own: the assessment is read again from the discovery.");
  if (step === "waves" && discovered) clears.push("The dependency graph, which is built again from the discovery.");
  if (covers("plan") && plan.length) {
    const typed = Object.keys(credentials).length;
    clears.push(`The plan: ${n(plan.length)} objects, the stage options${typed ? ` and the credentials typed for ${typed} connection${typed === 1 ? "" : "s"}` : ""}.`);
  }
  if (covers("migrate") && run.state !== "idle") clears.push(`The record of migration run #${run.runId}: ${n(run.completed)} migrated, ${n(run.failed)} failed, ${n(run.deferred ?? 0)} left for a person.`);
  if (validation) clears.push(`The validation results: ${n(validation.length)} checks.`);
  const unconfirm = STEP_KEYS.filter((k) => covers(k) && confirmed.includes(k));
  if (unconfirm.length) clears.push(`Your confirmation of ${unconfirm.map((k) => stepMeta(k).title).join(", ")}.`);

  const blocked =
    discovery.state === "running" ? "Discovery is running. Wait for it to finish." :
    covers("migrate") && run.state === "running" ? "The migration is running. Pause it first, then reset." :
    validationBusy ? "Validation is running. Wait for it to finish." :
    clears.length === 0 ? "Nothing to reset yet." : null;

  const notes = ["Not affected: your connections, everything in Synapse, and anything already created in Fabric (a new run skips what already exists; nothing is deleted)."];
  if (step === "discover" && !isConnected) notes.push("The source is not connected, so discovery can run again only after you reconnect it.");
  if (projects.length > 1 && ((step === "discover" && discovery.state !== "idle") || (covers("migrate") && run.state !== "idle")))
    notes.push("The backend keeps one discovery and one run record for all your projects, so the other projects see this reset too.");
  return { clears, blocked, notes };
}

/** The Reset button in a step's header: asks first, then starts the step and every step after it over. */
export function ResetStepButton({ step }: { step: StepKey }) {
  const { resetStep } = useMigration();
  const { clears, blocked, notes } = useResetPlan(step);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const title = stepMeta(step).title;
  const last = step === STEP_KEYS[STEP_KEYS.length - 1];
  const close = useCallback(() => { setOpen(false); setError(null); }, []);

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await resetStep(step);
      setOpen(false);
      notify(`${title} was reset${last ? "" : ", with the steps after it"}. You can do ${last ? "it" : "them"} again now.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "The reset did not complete.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button size="small" variant="ghost" className="reset-step" onClick={() => setOpen(true)} disabled={!!blocked}
        title={blocked ?? `Clear ${title}${last ? "" : " and the steps after it"}, to do ${last ? "it" : "them"} again from the start`}>
        <RotateCcw size={14} aria-hidden="true" />Reset step
      </Button>
      {open && (
        <ConfirmDialog title={`Reset ${title}?`} confirmLabel={`Reset ${title}`} busy={busy} error={error} onConfirm={() => void confirm()} onCancel={close}>
          <p>{last ? "This clears:" : `This starts ${title} over, and every step after it. It clears:`}</p>
          <ul className="reset-list">{clears.map((c) => <li key={c}>{c}</li>)}</ul>
          {notes.map((t) => <p key={t} className="muted">{t}</p>)}
        </ConfirmDialog>
      )}
    </>
  );
}
