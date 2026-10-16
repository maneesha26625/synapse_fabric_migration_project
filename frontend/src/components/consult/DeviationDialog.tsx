import { useState } from "react";
import type { PlanDifference } from "../../types";
import { Banner, Button, Modal } from "../shared/Shared";

/**
 * A plan that differs from the approved brief: say how, and ask why. The reason is kept in the audit trail and with
 * the run, and under a required consultation it is what lets the run start.
 */
export function DeviationDialog({ differences, version, confirmLabel, onConfirm, onCancel }: {
  differences: PlanDifference[];
  version: number | null;
  confirmLabel: string;
  /** Resolves false (with the error shown by the caller's state) when it did not go through. */
  onConfirm: (reason: string) => Promise<boolean | void>;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      if ((await onConfirm(reason.trim())) === false) setError("It was not accepted: see the message on the page.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "It was not accepted.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={`The plan differs from the approved brief${version ? ` (version ${version})` : ""}`} onClose={() => { if (!busy) onCancel(); }}
      onSubmit={() => { if (reason.trim()) void go(); }}
      footer={<>
        <Button onClick={onCancel} disabled={busy}>Cancel</Button>
        <Button variant="primary" type="submit" disabled={!reason.trim()} loading={busy}>{confirmLabel}</Button>
      </>}>
      <ul className="reset-list" style={{ margin: 0 }}>{differences.map((d) => <li key={`${d.option}-${d.message}`}>{d.message}</li>)}</ul>
      <label className="field">
        <span>Why does this plan differ from the brief?</span>
        <textarea className="input consult-text" rows={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} data-autofocus
          aria-label="Why the plan differs from the brief" placeholder="Kept with the run and in the audit trail" />
      </label>
      <p className="faint" style={{ margin: 0 }}>Or change the plan to match the brief, or change the brief in Consult and have it approved again.</p>
      {error && <Banner tone="error" title="Not recorded">{error}</Banner>}
    </Modal>
  );
}
