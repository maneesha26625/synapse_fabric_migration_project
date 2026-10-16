"""The orchestrator: the execution plan a migration follows, and the agent that keeps it moving.

**The agent proposes and sequences. The engine executes. A person approves.**

* ``plan``: the execution plan, built deterministically from the approved brief, the recorded plan version and the
  discovery snapshot: the same inputs always give the same steps and the same hash. Each step says who owns it (a
  person, the agent, the accelerator), what it waits for, and which brief answers it came from. Pure.
* ``diagnosis``: what a failure's category allows (retry, a draft, a person's fix), and the rule's recommendation.
  Pure.
* ``tools``: one registry of what the agent may do, each tool a call the page's own buttons make, with a tier (read,
  prepare, change, high, person only), a preview, a re-check and an idempotency key. Nothing in it writes to Synapse,
  runs arbitrary SQL or commands, reads a credential, deletes a Fabric item or edits the audit trail; approving,
  accepting a draft and entering credentials are not tools at all.
* ``assistant``: the conversation: fixed intents answered from read tools, and with consent from an approved brief, a
  task loop in which the model chooses one step at a time (read, change, ask, finish) and every change waits for a
  person as an action card.

The service, the supervisor's loop and the routes are in ``api.orchestrator``. The engine never imports any of this:
the orchestrator calls the same service operations a person's request does, so leases, gates, the run's state machine
and the audit trail apply to it exactly as they do to a person.
"""
