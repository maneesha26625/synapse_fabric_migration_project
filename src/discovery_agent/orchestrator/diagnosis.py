"""What may be done about a failure, and what the rules recommend. Pure.

A failed or blocked object already carries its category (``migration.failures``), worked out once, where it happened.
Each category allows a closed set of actions, the rule's choice first. An explanation from the model (when the brief
allows it) may recommend only an allowed action: anything else is replaced by the rule's, and the override is said.

The actions are what a person (or the agent, within its autonomy) can do next:

* ``retry``: Retry failed (a new pass over what failed);
* ``draft``: an LLM draft of the object, for a person to review and accept (needs consent, D1);
* ``rewrite``: a person rewrites it by hand;
* ``update``: a new run that updates what is already in Fabric (a high-tier action, always a person's decision);
* ``skip``: leave it as it is in Fabric;
* ``credentials``: enter what the run needs, then retry;
* ``escalate``: a person fixes the cause outside the accelerator (sign in again, resume the capacity), then retries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from discovery_agent.migration import failures

RETRY, DRAFT, REWRITE, UPDATE, SKIP, CREDENTIALS, ESCALATE, ADD_DEPENDENCY, ACCEPT, RELOAD = (
    "retry", "draft", "rewrite", "update", "skip", "credentials", "escalate", "add_dependency", "accept", "reload")
ACTIONS = (RETRY, DRAFT, REWRITE, UPDATE, SKIP, CREDENTIALS, ESCALATE, ADD_DEPENDENCY, ACCEPT, RELOAD)

#: Each category: the actions it allows, the rule's choice first.
ALLOWED: Dict[str, Tuple[str, ...]] = {
    failures.TRANSIENT: (RETRY, ESCALATE),
    failures.AUTH: (ESCALATE,),
    failures.CAPACITY: (ESCALATE,),
    failures.CONFIG: (CREDENTIALS, ESCALATE),
    failures.UNSUPPORTED: (DRAFT, REWRITE, SKIP),
    failures.DEPENDENCY: (ADD_DEPENDENCY, RETRY),
    failures.CONFLICT: (SKIP, UPDATE),
    failures.INTERRUPTED: (RETRY,),
    failures.UNKNOWN: (ESCALATE,),
}
#: What each action means for the person reading the card.
WORDS = {
    RETRY: "Retry it: a new pass over what failed.",
    DRAFT: "Draft it with AI, review and accept the draft, then retry.",
    REWRITE: "Rewrite it by hand (accepted as an LLM transform would be), then retry.",
    UPDATE: "Start a run that updates what is already in Fabric in place. It overwrites changes made there, so it asks for sign-off.",
    SKIP: "Leave it: what Fabric has stays as it is.",
    CREDENTIALS: "Enter what the run needs in Plan (credentials are never kept), then retry.",
    ESCALATE: "A person fixes the cause first (it is outside the accelerator), then retries.",
    ADD_DEPENDENCY: "Add what it needs to the plan, or create it first, then retry.",
    ACCEPT: "Accept the difference, with the reason (kept in the trail).",
    RELOAD: "Reload the table with Replace their rows (it empties the table first, so it asks for sign-off).",
}
#: Escalations that say exactly what the person does.
_ESCALATE = {
    failures.AUTH: "Sign in to Fabric again on Connections, then Retry failed.",
    failures.CAPACITY: "Resume or assign the workspace's capacity in the Fabric admin portal, then Retry failed.",
    failures.UNKNOWN: "Read the error and the run's log: the cause is not one the accelerator recognises.",
    failures.TRANSIENT: "If it keeps failing after retries, check Fabric's service status, then Retry failed.",
}


@dataclass
class Diagnosis:
    object_id: str
    name: str
    category: str
    meaning: str
    error: str
    allowed: List[str]
    recommended: str
    advice: str
    #: Why the recommendation is not the rule's first choice, when something in the brief or the estate rules it out.
    because: Optional[str] = None
    #: An explanation from the model, when one was asked for; never a recommendation outside ``allowed``.
    explanation: Optional[str] = None
    overridden: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"objectId": self.object_id, "name": self.name, "category": self.category, "meaning": self.meaning,
                "error": self.error, "allowed": [{"action": a, "words": WORDS[a]} for a in self.allowed],
                "recommended": self.recommended, "advice": self.advice, "because": self.because,
                "explanation": self.explanation, "overridden": self.overridden}


def diagnose(item: Mapping[str, Any], consent: Optional[bool], kind: Optional[str] = None,
             drafted: Optional[str] = None) -> Diagnosis:
    """The diagnosis of one failed or blocked object (a run item, as ``MigrationRun.to_dict`` lists it).

    ``consent``: whether the brief allows LLM drafts (D1). ``drafted``: the state of the object's LLM transform, if any:
    a draft already accepted turns *draft* into *retry*."""
    category = str(item.get("category") or failures.classify(str(item.get("error") or "")))
    if category not in ALLOWED:
        category = failures.UNKNOWN
    allowed = list(ALLOWED[category])
    because = None
    if category == failures.UNSUPPORTED:
        if drafted == "accepted":
            allowed = [RETRY] + [a for a in allowed if a != DRAFT]
            because = "A reviewed transform is accepted for it: retry, and the run uses it."
        elif not consent:
            allowed = [a for a in allowed if a != DRAFT]
            because = "The approved brief does not allow AI drafts (D1)."
        if kind is not None and kind not in ("procedure", "view", "table", "script", "pipeline", "notebook", "function"):
            allowed = [a for a in allowed if a != DRAFT]
            because = because or "Drafts are not made for this kind of object."
    if item.get("status") == "BLOCKED":
        category, allowed, because = failures.DEPENDENCY, [RETRY], "It did not start: something it needs failed in this pass."
    recommended = allowed[0]
    advice = _ESCALATE.get(category, WORDS[recommended]) if recommended == ESCALATE else WORDS[recommended]
    return Diagnosis(str(item.get("id") or ""), str(item.get("name") or ""), category, failures.MEANING.get(category, ""),
                     str(item.get("error") or ""), allowed, recommended, advice, because)


def with_explanation(d: Diagnosis, text: str, suggested: Optional[str]) -> Diagnosis:
    """The model's explanation added to a diagnosis. A suggestion outside what the category allows is replaced by the
    rule's, and that is said: the model never widens what may be done."""
    d.explanation = text
    if suggested and suggested not in d.allowed:
        d.overridden = f"The model suggested '{suggested}', which this kind of failure does not allow; the rule's '{d.recommended}' stands."
    elif suggested and suggested != d.recommended:
        d.recommended, d.advice = suggested, WORDS[suggested]
    return d


def summary(items: List[Mapping[str, Any]], consent: Optional[bool], kinds: Optional[Mapping[str, str]] = None,
            drafts: Optional[Mapping[str, str]] = None) -> List[Diagnosis]:
    """A diagnosis for each failed or blocked object of a run, in the run's order."""
    kinds, drafts = kinds or {}, drafts or {}
    return [diagnose(i, consent, kinds.get(str(i.get("id"))), drafts.get(str(i.get("id"))))
            for i in items if i.get("status") in ("FAILED", "BLOCKED")]
