"""Facts: what the consultation knows about the estate, computed once per discovery snapshot. Metadata only.

Every fact is a count, a name or a short list, never a definition, a row or a credential, so the facts can be
shown as evidence, kept in a brief, and (with consent) summarised for a model.

Two scopes:

* **estate** facts come from the discovery. They are fingerprinted: a new discovery whose facts differ makes an
  approved brief need reconfirmation, and reopens each answer whose evidence changed;
* **environment** facts (the Fabric target, the model's availability) are read live. They decide whether a
  question applies or what it recommends, but a target that is briefly disconnected never stales a brief.

The values themselves are gathered in ``api.consult`` from the discovery; this module only names and fingerprints
them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping

ESTATE, ENVIRONMENT = "estate", "environment"


@dataclass(frozen=True)
class Fact:
    name: str
    description: str
    scope: str = ESTATE
    #: Shown, but never part of the fingerprint (an id that changes with every discovery).
    informational: bool = False


def _f(name: str, description: str, scope: str = ESTATE, informational: bool = False) -> Fact:
    return Fact(name, description, scope, informational)


_ALL = (
    _f("objects.total", "Objects discovered"),
    _f("objects.byType", "Objects by type"),
    _f("objects.byClass", "Objects by how they move (Direct, Reconfigure, Transform, Manual, Review)"),
    _f("waves.count", "Dependency waves"),
    _f("pool.present", "A dedicated SQL pool was discovered"),
    _f("pool.name", "The dedicated SQL pool"),
    _f("pool.warehouseName", "The Warehouse name the pool's name gives"),
    _f("pool.collation", "The Synapse pool's collation"),
    _f("pool.caseInsensitive", "The pool compares text case-insensitively"),
    _f("tables.count", "Tables (external tables aside)"),
    _f("tables.external", "External tables"),
    _f("tables.withDateColumn", "Tables with a date or time column"),
    _f("tables.withChangeColumn", "Tables with a column like ModifiedAt, which moves when a row changes"),
    _f("tables.withKeys", "Tables with a primary key or a unique constraint"),
    _f("tables.withoutKeys", "Tables with no key"),
    _f("tables.changeWithoutKeys", "Tables with a change column but no key: a sync can only append to them"),
    _f("tables.lossyTypes", "Tables with a column Fabric stores with less (7-digit time, datetimeoffset, rowversion, XML, spatial)"),
    _f("storage.locations", "Storage locations the code reads directly"),
    _f("security.principals", "Database roles and users"),
    _f("security.roles", "Database roles"),
    _f("security.sqlUsers", "SQL-authenticated users (Fabric has no SQL authentication)"),
    _f("security.sqlUserNames", "The SQL-authenticated users"),
    _f("security.masks", "Columns with a dynamic data mask"),
    _f("code.sqlModules", "Views, stored procedures and functions"),
    _f("code.unconvertibleSql", "Procedures, views, functions and scripts that still use T-SQL Fabric rejects after the rules"),
    _f("code.multiStatementFunctions", "Multi-statement table-valued functions (Fabric has none)"),
    _f("code.dotnetNotebooks", ".NET for Spark notebooks (Fabric has none)"),
    _f("code.unsupportedPipelines", "Pipelines with an activity Fabric has no equivalent for"),
    _f("code.needsRewrite", "Objects that must be rebuilt by hand (unsupported pipelines and .NET notebooks)"),
    _f("code.serverlessObjects", "Scripts and pipelines written for the serverless SQL pool"),
    _f("pipelines.count", "Pipelines"),
    _f("notebooks.count", "Notebooks"),
    _f("linkedServices.count", "Linked services"),
    _f("linkedServices.byType", "Linked services by type"),
    _f("linkedServices.keyVault", "Linked services that read a secret from Azure Key Vault"),
    _f("triggers.count", "Triggers"),
    _f("triggers.schedule", "Schedule triggers (recreated switched off)"),
    _f("triggers.event", "Event, tumbling-window and custom triggers (set up by hand)"),
    _f("integrationRuntimes.count", "Integration runtimes (set up by hand)"),
    _f("manual.setupCount", "Triggers and integration runtimes someone sets up or switches on"),
    _f("scope.groups", "The schemas and folders discovered"),
    _f("source.kind", "Where the definitions were read from (the live workspace, Git or a ZIP)"),
    _f("source.workspace", "The Synapse workspace"),
    _f("source.ref", "The Git branch or tag read"),
    _f("source.commit", "The Git commit read"),
    _f("source.environment", "The environment the definitions are for"),
    _f("discovery.complete", "Discovery read every part of the source"),
    _f("discovery.failedCategories", "The parts of the source discovery could not read"),
    _f("discovery.snapshot", "The discovery snapshot", informational=True),
    _f("target.connected", "The Fabric target is connected", ENVIRONMENT),
    _f("target.workspace", "The Fabric target workspace", ENVIRONMENT),
    # The id a brief's approval keeps (``Policy.target``): another workspace connected later makes it stale.
    _f("target.workspaceId", "The Fabric target workspace's id", ENVIRONMENT, informational=True),
    _f("target.capacity", "The Fabric workspace is on a capacity", ENVIRONMENT),
    _f("target.gitConnected", "The Fabric workspace is connected to Git", ENVIRONMENT),
    _f("llm.available", "Claude Code is installed on this machine", ENVIRONMENT),
)

FACTS: Dict[str, Fact] = {f.name: f for f in _ALL}


def describe() -> Dict[str, Dict[str, Any]]:
    """The facts, as the Demo page's copy of the engine reads them (``frontend/src/mock/consultVocabulary.json``)."""
    return {f.name: {"description": f.description, "scope": f.scope, "informational": f.informational} for f in _ALL}


def fingerprinted(values: Mapping[str, Any]) -> Dict[str, Any]:
    """The facts that stand for the estate: what a brief's freshness is judged on."""
    return {k: v for k, v in values.items() if k in FACTS and FACTS[k].scope == ESTATE and not FACTS[k].informational}


def fingerprint(values: Mapping[str, Any]) -> str:
    body = json.dumps(fingerprinted(values), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list:
    """The estate facts whose values differ, as {fact, description, before, after}: what a person reconfirms."""
    out = []
    old, new = fingerprinted(before), fingerprinted(after)
    for name in sorted(set(old) | set(new)):
        if old.get(name) != new.get(name):
            out.append({"fact": name, "description": FACTS[name].description, "before": old.get(name), "after": new.get(name)})
    return out
