"""Output writers.

Serialize the in-memory result to the discovery output directory
(manifest, inventory, graph, coverage). Writes must be deterministic:
sorted keys, sorted collections, no wall-clock outside the manifest.
Empty until we start building them.
"""
