"""What the accelerator keeps about its own work: who did what, what ran, and what it left behind.

* ``context``: the correlation id and the actor that travel with a request into the work it starts.
* ``logs``: one logging setup, structured, redacted and correlated.
* ``store``: a project's durable record on disk: versioned documents, append-only streams, blobs, leases.
* ``projects``: the projects the backend owns, one active at a time.
* ``runs``: a migration run's record: its document, its objects and its event stream.
* ``audit``: the hash-chained audit trail.
* ``approvals``: sign-off before a run writes to Fabric.

Standard library only, like the rest of the core.
"""
