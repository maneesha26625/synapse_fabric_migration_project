"""Synapse Apache Spark pool -> Fabric custom Spark pool + Environment.

A Synapse Spark pool is compute: a node size, a node count (autoscale), a Spark
version and optionally libraries. Fabric splits that into two things, both made
here: a workspace **custom Spark pool** carrying the node size and scale, and an
**Environment** that selects that pool, the runtime version and the executor
shape. Notebooks then attach to the Environment.

A pool's **library requirements** (the requirements.txt or environment.yml it was
given) become the Environment's public libraries (``Libraries/PublicLibraries/environment.yml``
in its definition), and its **Spark configuration** becomes the Environment's Spark properties.
Workspace packages (wheel and jar files uploaded to Synapse) are not copied: the pool records
their names, not their files, so the notes name each one to add by hand.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

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
    #: The Environment's public libraries as an environment.yml, or None when the pool had none.
    libraries_yml: Optional[str] = None


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
    libraries_yml, library_notes = environment_yml(str((meta.get("_libraryRequirements") or {}).get("content") or ""))
    notes.extend(library_notes)
    if meta.get("libraryRequirementsFile") and not libraries_yml and not library_notes:
        notes.append(f"The pool's requirements file ({meta.get('libraryRequirementsFile')}) was not readable: add its packages in the "
                     "Environment's Libraries section.")
    custom = [str(c.get("name")) for c in (meta.get("customLibraries") or []) if isinstance(c, dict) and c.get("name")]
    if custom:
        notes.append(f"Workspace packages not copied (their files stay in Synapse): {', '.join(custom[:6])}"
                     f"{' and more' if len(custom) > 6 else ''}. Upload them to the Environment's custom libraries.")
    spark_properties, config_notes = spark_conf(str((meta.get("_sparkConfig") or {}).get("content") or ""))
    notes.extend(config_notes)

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
    if spark_properties:
        compute["sparkProperties"] = spark_properties
        notes.append(f"{len(spark_properties)} Spark setting(s) carried from the pool's Spark configuration.")
    return EnvironmentPlan(name, pool, compute, notes, libraries_yml)


_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?\s*((==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+\s*(,\s*(==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+\s*)*)?$")


def environment_yml(content: str) -> Tuple[Optional[str], List[str]]:
    """A pool's library requirements as the environment.yml a Fabric Environment takes, and what was left out.

    An environment.yml (conda) is kept as it is; a requirements.txt becomes its ``pip`` section. Options (``-r``,
    ``--index-url``), URLs and local paths are left out and named: Fabric resolves public packages from PyPI and conda."""
    text = (content or "").strip()
    if not text:
        return None, []
    if re.search(r"^\s*dependencies\s*:", text, re.MULTILINE):
        return text + "\n", ["The pool's environment.yml became the Environment's public libraries."]
    packages: List[str] = []
    skipped: List[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if _REQUIREMENT.match(line):
            packages.append(line.replace(" ", ""))
        else:
            skipped.append(line)
    notes: List[str] = []
    if packages:
        notes.append(f"{len(packages)} package(s) from the pool's requirements became the Environment's public libraries.")
    if skipped:
        notes.append(f"Not carried from the requirements (options, URLs or local files): {'; '.join(skipped[:4])}. Add them by hand.")
    if not packages:
        return None, notes
    body = "dependencies:\n  - pip:\n" + "".join(f"    - {p}\n" for p in packages)
    return body, notes


def spark_conf(content: str) -> Tuple[Dict[str, str], List[str]]:
    """A pool's Spark configuration file (``key value`` or ``key=value`` per line) as Spark properties."""
    out: Dict[str, str] = {}
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        first = line.split(None, 1)[0]
        if "=" in first:
            key, _, value = line.partition("=")
        else:
            key, value = first, line[len(first):]
        key, value = key.strip(), value.strip()
        if key.startswith("spark.") and value:
            out[key] = value
    return out, []


def libraries_part(yml: str) -> Dict[str, Any]:
    """The Environment definition part that holds its public libraries."""
    return {"path": "Libraries/PublicLibraries/environment.yml",
            "payload": base64.b64encode(yml.encode("utf-8")).decode("ascii"), "payloadType": "InlineBase64"}


def starter_compute(compute: Mapping[str, Any]) -> Dict[str, Any]:
    """The same Environment shaped for the Starter Pool (Medium nodes), used if a custom pool is refused."""
    _, cores, memory = NODE_SHAPES["medium"]
    body = dict(compute)
    body.update(driverCores=cores, driverMemory=memory, executorCores=cores, executorMemory=memory)
    body["dynamicExecutorAllocation"] = {"enabled": True, "minExecutors": 1, "maxExecutors": 9}
    return body
