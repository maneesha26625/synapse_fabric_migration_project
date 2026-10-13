import { Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppState } from "../../state/AppState";

/** Marks an object that is created from an accepted LLM-assisted transform, not the rule-based conversion. */
export function LlmBadge({ edited, acceptedAt }: { edited?: boolean; acceptedAt?: string | null }) {
  const when = acceptedAt ? ` on ${new Date(acceptedAt).toLocaleString()}` : "";
  return (
    <span className="llm-badge" title={`Created from the LLM-assisted transform accepted${edited ? " and edited" : ""}${when} (Assess → LLM transforms).`}>
      <Sparkles size={11} aria-hidden="true" />LLM
    </span>
  );
}

export interface AcceptedTransform { edited: boolean; acceptedAt: string | null }

/** Objects with an accepted transform, by id. Empty while loading or when the backend has none. */
export function useAcceptedTransforms(): Map<string, AcceptedTransform> {
  const { api } = useAppState();
  const [accepted, setAccepted] = useState<Map<string, AcceptedTransform>>(new Map());
  useEffect(() => {
    let cancelled = false;
    api.listTransforms().then(
      (items) => {
        if (cancelled) return;
        setAccepted(new Map(items.filter((t) => t.accepted).map((t) => [t.objectId, { edited: !!t.accepted?.edited, acceptedAt: t.accepted?.acceptedAt ?? null }])));
      },
      () => { /* no transforms available: show nothing */ },
    );
    return () => { cancelled = true; };
  }, [api]);
  return accepted;
}
