"""Discovery Agent for Synapse-to-Fabric migration.

Scope (v1): repository-only, read-only, deterministic. The agent walks a
Synapse Git-integrated repository, normalizes every asset into a canonical
model, and emits an inventory plus a dependency graph.
"""

__version__ = "0.1.0"
