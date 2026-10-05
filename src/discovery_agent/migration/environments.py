"""Synapse Apache Spark pool -> Fabric custom Spark pool + Environment.

A Synapse Spark pool is compute: a node size, a node count (autoscale), a Spark
version and optionally libraries. Fabric splits that into two things, both made
here: a workspace **custom Spark pool** carrying the node size and scale, and an
**Environment** that selects that pool, the runtime version and the executor
shape. Notebooks then attach to the Environment.

Libraries are not copied: a pool only records their names, not their files, so
the notes say what to add by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

#: Fabric node size -> (name, cores, memory) for the driver and executors.
NODE_SHAPES = {
    "small": ("Small", 4, "28g"),
    "medium": ("Medium", 8, "56g"),
    "large": ("Large", 16, "112g"),
    "xlarge": ("XLarge", 32, "224g"),
    "xxlarge": ("XXLarge", 64, "400g"),
}
#: Synapse Spark version -> Fabric runtime version, and the Spark each runtime ships.
RUNTIMES = {"3.5": "1.3", "3.4": "1.2", "3.3": "1.2", "3.2": "1.2", "3.1": "1.2"}
RUNTIME_SPARK = {"1.3": "3.5", "1.2": "3.4"}
DEFAULT_RUNTIME = "1.3"
STARTER_POOL = {"name": "Starter Pool", "type": "Workspace"}


@dataclass
class EnvironmentPlan:
    pool_name: str
    pool: Dict[str, Any]      # body for POST /spark/pools
    compute: Dict[str, Any]   # body for PATCH .../staging/sparkcompute (instancePool added by the caller)
    notes: List[str] = field(default_factory=list)


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def plan(meta: Mapping[str, Any], name: str) -> EnvironmentPlan:
    """The Fabric pool and Environment settings for one discovered Spark pool."""
    notes: List[str] = []
    size_key = str(meta.get("nodeSize") or "").strip().lower()
    if size_key not in NODE_SHAPES:
        notes.append(f"Node size '{meta.get('nodeSize') or 'unknown'}' has no Fabric equivalent; Medium was used.")
        size_key = "medium"
    size, cores, memory = NODE_SHAPES[size_key]

    family = str(meta.get("nodeSizeFamily") or "MemoryOptimized")
    if family.lower() != "memoryoptimized":
        notes.append(f"Node family '{family}' is not available in Fabric; MemoryOptimized was used.")

    scale = meta.get("autoScale") or {}
    if scale.get("enabled"):
        low = _int(scale.get("minNodeCount")) or 1
        high = _int(scale.get("maxNodeCount")) or low
    else:
        low = high = _int(meta.get("nodeCount")) or 3
    high = max(high, low)

    dynamic = meta.get("dynamicExecutorAllocation") or {}
    dynamic_body: Dict[str, Any] = {"enabled": bool(dynamic.get("enabled"))}
    if dynamic_body["enabled"]:
        dynamic_body["minExecutors"] = _int(dynamic.get("minExecutors")) or 1
        dynamic_body["maxExecutors"] = max(_int(dynamic.get("maxExecutors")) or 1, dynamic_body["minExecutors"])

    version = str(meta.get("sparkVersion") or "")
    runtime = RUNTIMES.get(version)
    if runtime is None:
        notes.append(f"Spark {version or 'version unknown'} has no matching Fabric runtime; Runtime {DEFAULT_RUNTIME} was used.")
        runtime = DEFAULT_RUNTIME
    elif version != RUNTIME_SPARK[runtime]:
        notes.append(f"Spark {version} is not offered by Fabric; Runtime {runtime} (Spark {RUNTIME_SPARK[runtime]}) was used.")

    if meta.get("autoPause"):
        notes.append("Auto-pause dropped: Fabric ends idle Spark sessions on its own timeout.")
    if meta.get("customLibraries") or meta.get("libraryRequirementsFile"):
        notes.append("Libraries were not copied: add the pool's packages in the Environment's Libraries section.")

    pool = {
        "name": name,
        "nodeFamily": "MemoryOptimized",
        "nodeSize": size,
        "autoScale": {"enabled": True, "minNodeCount": low, "maxNodeCount": high},
        "dynamicExecutorAllocation": dynamic_body,
    }
    compute = {
        "driverCores": cores, "driverMemory": memory,
        "executorCores": cores, "executorMemory": memory,
        "dynamicExecutorAllocation": dynamic_body,
        "runtimeVersion": runtime,
    }
    return EnvironmentPlan(name, pool, compute, notes)


def starter_compute(compute: Mapping[str, Any]) -> Dict[str, Any]:
    """The same Environment shaped for the Starter Pool (Medium nodes), used if a custom pool is refused."""
    _, cores, memory = NODE_SHAPES["medium"]
    body = dict(compute)
    body.update(driverCores=cores, driverMemory=memory, executorCores=cores, executorMemory=memory)
    body["dynamicExecutorAllocation"] = {"enabled": True, "minExecutors": 1, "maxExecutors": 9}
    return body
