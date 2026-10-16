"""The migration brief: every decision, the settings they imply, and what they rest on.

A brief's draft is worked out from the answers at any moment and never stored. Submitting it makes an immutable,
numbered version, hashed over the catalog version and the decisions; every submission gets a new number, so
submitting the same decisions again always makes something a person can approve. A version's lifecycle
(submitted, then approved or rejected; an approved one superseded by the next approval) is kept as events beside
it, never by editing it.

A brief says, for each decision, who made it (a person, an accepted recommendation, a finding taken, a confirmed
proposal) or that nobody did (an assumption), and how far the accelerator acts on it (enforced, recorded, note).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional

from discovery_agent.consult import conditions as cond
from discovery_agent.consult.catalog import Catalog, Question
from discovery_agent.consult.engine import ASSUMED, SKIPPED, State, settings_of
from discovery_agent.consult.facts import FACTS, fingerprint, fingerprinted
from discovery_agent.consult.settings import ENFORCED, NOTE, RECORDED, SETTINGS

SUBMITTED, APPROVED, REJECTED, SUPERSEDED = "submitted", "approved", "rejected", "superseded"


def display(question: Question, value: Any) -> str:
    """An answer as a person reads it."""
    if value is None:
        return "—"
    if question.type == "choice":
        option = question.option(value)
        return option.label if option else str(value)
    if question.type == "multi":
        labels = {o.value: o.label for o in question.options}
        return ", ".join(labels.get(v, v.split(":", 1)[-1] if ":" in v else v) for v in value) or "None"
    if question.type == "boolean":
        return "Yes" if value else "No"
    if question.type == "principalMap":
        return "; ".join(f"{user} → {principal}" for user, principal in value.items()) or "None mapped"
    if question.type == "list":
        return ", ".join(value) or "None"
    if question.type == "number":
        return f"{value:,}"
    return str(value) or "None"


def draft(catalog: Catalog, facts: Mapping[str, Any], state: State) -> Dict[str, Any]:
    """The brief as the answers make it now."""
    titles = dict(catalog.sections)
    decisions: List[Dict[str, Any]] = []
    for q in catalog.questions:
        s = state.questions[q.id]
        if not s.applies or s.value is None:
            continue
        assumed = s.status in (ASSUMED, SKIPPED)
        decisions.append({
            "question": q.id, "section": q.section, "sectionTitle": titles.get(q.section, q.section), "text": q.text,
            "value": s.value, "display": display(q, s.value), "assumed": assumed,
            "source": None if assumed else s.source, "by": None if assumed else s.by, "at": None if assumed else s.at,
            "reason": s.note if not assumed and s.note else (s.recommendation or {}).get("reason"),
            "drives": [{"setting": name, "status": SETTINGS[name].status, "applies": SETTINGS[name].applies} for name in q.drives],
        })
    settings = settings_of(catalog, state)
    loosened = [{"question": q.id, "reason": q.separate_approver.reason} for q in catalog.questions
                if q.separate_approver is not None and state.questions[q.id].applies
                and cond.holds(q.separate_approver.when, facts, state.values)]
    return {
        "catalogVersion": catalog.version,
        "factsFingerprint": fingerprint(facts),
        "facts": fingerprinted(facts),
        "snapshot": facts.get("discovery.snapshot"),
        "decisions": decisions,
        "settings": settings,
        "enforced": {k: v for k, v in settings.items() if SETTINGS[k].status == ENFORCED},
        "assumptions": [d["question"] for d in decisions if d["assumed"]],
        "recordedOnly": [{"setting": k, "value": v, "status": SETTINGS[k].status, "applies": SETTINGS[k].applies}
                         for k, v in settings.items() if SETTINGS[k].status in (RECORDED, NOTE)],
        "keptContradictions": [{"conflict": c.id, "message": c.message, "reason": c.reason, "by": c.by, "at": c.at}
                               for c in state.conflicts if c.status == "kept"],
        #: Decisions that loosen a safeguard: approved by someone other than the submitter, or recorded as self-approved.
        "separateApprover": loosened,
    }


def content_hash(content: Mapping[str, Any]) -> str:
    """What a version is: its catalog, its decisions and who made them, and the contradictions kept."""
    body = {
        "catalogVersion": content.get("catalogVersion"),
        "decisions": [[d["question"], d["value"], d["source"], d["assumed"]] for d in content.get("decisions") or []],
        "settings": content.get("settings"),
        "kept": [[k["conflict"], k["reason"]] for k in content.get("keptContradictions") or []],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


def status_of(events: List[Mapping[str, Any]], version: int) -> Optional[str]:
    """A version's status: its last lifecycle event."""
    mine = [e for e in events if e.get("version") == version]
    return str(mine[-1]["event"]) if mine else None


def approved_version(events: List[Mapping[str, Any]]) -> Optional[int]:
    """The version in force: the last one approved and not superseded."""
    latest: Dict[int, str] = {}
    for e in events:
        latest[int(e["version"])] = str(e["event"])
    approved = [v for v, status in latest.items() if status == APPROVED]
    return max(approved) if approved else None


def _value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "none"
    if isinstance(value, dict):
        return "; ".join(f"{k} → {v}" for k, v in value.items()) or "none"
    return str(value) if value not in (None, "") else "—"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def to_markdown(version: Mapping[str, Any], status: Optional[str], events: List[Mapping[str, Any]]) -> str:
    """A version as a document for the client, and for the final report."""
    content = version["content"]
    number = version["version"]
    lines = [f"# Migration brief, version {number}", ""]
    decided = [e for e in events if e.get("version") == number and e.get("event") in (APPROVED, REJECTED)]
    if decided:
        last = decided[-1]
        lines.append(f"**{str(last['event']).capitalize()}** by {last['by']['id']} on {last['at']}"
                     + (" (the person who submitted it)" if last.get("selfApproved") else "")
                     + (f": {last['comment']}" if last.get("comment") else "") + ".")
    else:
        lines.append(f"**{(status or SUBMITTED).capitalize()}** on {version['submittedAt']} by {version['submittedBy']['id']}.")
    facts = content.get("facts") or {}
    source = facts.get("source.workspace") or "the source"
    where = ", ".join(str(x) for x in (facts.get("source.kind"), facts.get("source.ref"), facts.get("source.commit")) if x)
    lines += ["", f"Made for {source}" + (f" ({where})" if where else "") + f", discovery snapshot {content.get('snapshot') or '—'}, "
              f"facts fingerprint `{content.get('factsFingerprint')}`. Catalog {content.get('catalogVersion')}, content hash "
              f"`{version['hash'][:16]}`.", ""]
    lines += ["## Decisions", ""]
    section = None
    for d in content.get("decisions") or []:
        if d["sectionTitle"] != section:
            lines += ([""] if section is not None else []) + [f"### {d['sectionTitle']}", ""]
            section = d["sectionTitle"]
        who = "assumed: nobody confirmed it" if d["assumed"] else {
            "person": "answered", "recommendation": "recommendation accepted", "finding": "finding taken",
            "ai": "AI proposal confirmed", "rules": "read from notes, confirmed",
            "import": "imported answer confirmed"}.get(str(d["source"]), str(d["source"]))
        by = f" by {d['by']['id']}" if d.get("by") else ""
        lines.append(f"- **{d['text']}** {d['display']} ({who}{by})." + (f" {d['reason']}" if d.get("reason") else ""))
    lines += ["", "## What the accelerator acts on", "", "| Setting | Value | Acted on by |", "|---|---|---|"]
    for name, value in (content.get("enforced") or {}).items():
        lines.append(f"| `{name}` | {_cell(_value_text(value))} | {_cell(SETTINGS[name].applies)} |")
    assumptions = content.get("assumptions") or []
    if assumptions:
        lines += ["", "## Assumptions", "", "Recommendations nobody confirmed: " + ", ".join(assumptions) + "."]
    recorded = content.get("recordedOnly") or []
    if recorded:
        lines += ["", "## Recorded, not acted on", ""]
        lines += [f"- `{r['setting']}` ({r['status']}): {_value_text(r['value'])}. {r['applies']}." for r in recorded]
    kept = content.get("keptContradictions") or []
    if kept:
        lines += ["", "## Contradictions kept, with their reasons", ""]
        lines += [f"- {k['message']} Kept by {k['by']['id'] if k.get('by') else '—'}: {k['reason']}" for k in kept]
    loosened = content.get("separateApprover") or []
    if loosened:
        lines += ["", "## Decisions that ask for a separate approver", ""]
        lines += [f"- {item['question']}: {item['reason']}" for item in loosened]
    lines += ["", "## The estate it rests on", "", "| Fact | Value |", "|---|---|"]
    for name, value in sorted(facts.items()):
        if name in FACTS and not isinstance(value, (dict, list)):
            lines.append(f"| {_cell(FACTS[name].description)} | {_cell(_value_text(value))} |")
    return "\n".join(lines) + "\n"
