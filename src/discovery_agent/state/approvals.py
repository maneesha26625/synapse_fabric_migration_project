"""Sign-off before a run writes to Fabric.

The gate always exists; the policy (``MIGRATION_SIGNOFF``) only decides who turns it:

* ``overwrite`` (the default): a person signs off a run that changes what is already in Fabric, that is one that
  updates objects in place (*Update objects that are already in Fabric*) or empties tables to load them again
  (*Replace it*). Any other run is approved by the accelerator itself.
* ``always``: a person signs off every run. ``never``: the accelerator approves every run.

An approval the accelerator gives is recorded as the accelerator's (``by: system``, ``automatic: true``), never
as a person's: one that looks like a person's would be worse than none. What is approved is the run's exact
inputs, hashed (``subjectHash``): its plan, options and stages, which a run never changes once it is made.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from discovery_agent.state import context
from discovery_agent.state.store import now

NEVER, OVERWRITE, ALWAYS = "never", "overwrite", "always"
POLICIES = (NEVER, OVERWRITE, ALWAYS)
_STRICTNESS = {NEVER: 0, OVERWRITE: 1, ALWAYS: 2}


def configured() -> Optional[str]:
    """The rule this machine is set to (``MIGRATION_SIGNOFF``), or None when it is not set."""
    value = str(os.environ.get("MIGRATION_SIGNOFF") or "").strip().lower()
    return value if value in POLICIES else None


def policy() -> str:
    return configured() or OVERWRITE


def rule_for(brief: Optional[str]) -> str:
    """The rule a run is signed off under. The approved brief's (G3) decides, but never loosens a rule this machine is
    set to: the stricter of the two holds. With neither, a person signs off what overwrites."""
    machine = configured()
    if brief not in POLICIES:
        return machine or OVERWRITE
    if machine is None:
        return brief
    return brief if _STRICTNESS[brief] >= _STRICTNESS[machine] else machine


def reasons(options: Dict[str, Any], stages: List[str]) -> List[str]:
    """What in these options changes what Fabric already has."""
    out = []
    if options.get("existing") == "update":
        out.append("it updates objects that are already in Fabric in place")
    if options.get("dataMode") == "replace" and "data" in stages:
        out.append("it empties tables that already have rows and loads them again")
    return out


def required(options: Dict[str, Any], stages: List[str], rule: Optional[str] = None) -> Optional[str]:
    """Why a person must sign this run off before it writes to Fabric, or None when the accelerator approves it."""
    rule = rule or policy()
    found = reasons(options, stages)
    if rule == ALWAYS:
        return "Every run is signed off before it writes to Fabric" + (f": {'; '.join(found)}" if found else ".")
    if rule == OVERWRITE and found:
        return "This run changes what Fabric already has: " + "; ".join(found) + "."
    return None


def subject_hash(plan: List[Dict[str, Any]], options: Dict[str, Any], stages: List[str]) -> str:
    body = json.dumps({"plan": plan, "options": options, "stages": stages}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def request(reason: Optional[str], digest: str, rule: Optional[str] = None) -> Dict[str, Any]:
    """The approval record a new run starts with: waiting for a person, or approved by the accelerator."""
    record: Dict[str, Any] = {"policy": rule or policy(), "required": bool(reason), "reason": reason, "subjectHash": digest,
                              "requestedAt": now(), "requestedBy": context.actor().to_dict(), "decision": None,
                              "by": None, "at": None, "comment": None, "automatic": False}
    if not reason:
        record.update(decision="approved", by=context.system().to_dict(), at=now(), automatic=True)
    return record


def decide(record: Dict[str, Any], approved: bool, comment: str = "") -> Dict[str, Any]:
    """A person's decision. Only a person decides: the accelerator approves only what no person needs to."""
    who = context.actor()
    if who.kind != context.PERSON:
        raise PermissionError("Only a person can sign a run off.")
    return {**record, "decision": "approved" if approved else "rejected", "by": who.to_dict(), "at": now(),
            "comment": " ".join(str(comment or "").split())[:500] or None, "automatic": False}
