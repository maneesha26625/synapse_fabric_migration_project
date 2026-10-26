"""What the consultation needs to know about the discovered estate, beyond counts by type.

Each fact is a count of something discovery actually recorded (a construct in code, a property of a
table or linked service), so a consultation question tied to it is asked only when the estate has it.
Nothing here reads Synapse again: it walks the completed discovery job. A fact discovery does not
record is left out, never guessed; ``notRecorded`` names those, so the consultation can say so.

Every fact is computed on its own: one object that cannot be read never hides the others.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable


def _value(x: Any) -> str:
    return str(getattr(x, "value", x) or "").lower()


def _kind(record: Any) -> str:
    return _value(getattr(record, "artifact", None))


def _records(job: Any, *kinds: str) -> Iterable[Any]:
    for r in (getattr(job, "records_by_id", None) or {}).values():
        if _kind(r) in kinds:
            yield r


def _extras(job: Any, source_type: str) -> Iterable[Any]:
    for e in (getattr(job, "extras_by_id", None) or {}).values():
        if getattr(e, "source_type", None) == source_type:
            yield e


def _catalog(job: Any) -> Any:
    run = getattr(job, "run", None)
    return getattr(run, "catalog", None) if run is not None else None


def _content(record: Any) -> Any:
    return getattr(record, "content", None)


# ---- notebooks and Spark ------------------------------------------------------------------

def _notebook_findings(job: Any, test: Callable[[Any], bool]) -> int:
    n = 0
    for r in _records(job, "notebook"):
        findings = getattr(_content(r), "findings", None) or ()
        if any(test(f) for f in findings):
            n += 1
    return n


def _mssparkutils(job: Any) -> int:
    return _notebook_findings(job, lambda f: str(getattr(f, "construct", "")).startswith(("mssparkutils", "notebookutils")))


def _notebook_secrets(job: Any) -> int:
    return _notebook_findings(job, lambda f: _value(getattr(f, "category", "")) == "credential")


def _notebook_storage(job: Any) -> int:
    n = 0
    for r in _records(job, "notebook"):
        resources = getattr(_content(r), "all_resources", None) or ()
        if any(_value(getattr(x, "category", "")) == "storage" for x in resources):
            n += 1
    return n


def _notebook_packages(job: Any) -> int:
    n = 0
    for r in _records(job, "notebook"):
        packages = getattr(_content(r), "packages", None) or ()
        if any(_value(getattr(p, "kind", "")) in ("pip_install", "conda_install", "spark_package") for p in packages):
            n += 1
    return n


def _custom_libraries(job: Any) -> int:
    libraries = sum(1 for _ in _extras(job, "Spark Library"))
    pools = sum(1 for e in _extras(job, "Spark Pool")
                if (getattr(e, "metadata", {}) or {}).get("customLibraries") or (getattr(e, "metadata", {}) or {}).get("libraryRequirementsFile"))
    return libraries + pools


# ---- SQL ----------------------------------------------------------------------------------

def _modules(job: Any) -> Iterable[Any]:
    cat = _catalog(job)
    if cat is None:
        return ()
    return tuple(getattr(cat, "views", ()) or ()) + tuple(getattr(cat, "procedures", ()) or ()) + tuple(getattr(cat, "functions", ()) or ())


def _script_features(job: Any, *kinds: str) -> int:
    n = 0
    for r in _records(job, "sqlscript"):
        features = getattr(_content(r), "features", None) or ()
        if any(_value(getattr(f, "kind", "")) in kinds for f in features):
            n += 1
    return n


def _dynamic_sql(job: Any) -> int:
    modules = sum(1 for m in _modules(job) if getattr(m, "dynamic_sql", None))
    scripts = sum(1 for r in _records(job, "sqlscript") if getattr(_content(r), "dynamic_sql", None))
    return modules + scripts


def _cross_database(job: Any) -> int:
    modules = sum(1 for m in _modules(job) if any(getattr(ref, "database", None) for ref in (getattr(m, "referenced_objects", None) or ())))
    return modules + _script_features(job, "cross_database")


def _tables(job: Any) -> Iterable[Any]:
    cat = _catalog(job)
    return tuple(getattr(cat, "tables", ()) or ()) if cat is not None else ()


def _distribution(job: Any) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for t in _tables(job):
        if getattr(t, "is_external", False):
            continue
        d = _value(getattr(t, "distribution", ""))
        if d and d != "unknown":
            out[d] = out.get(d, 0) + 1
    return out


def _columnstore(job: Any) -> int:
    return sum(1 for t in _tables(job) if "columnstore" in _value(getattr(t, "storage", "")))


def _identity(job: Any) -> int:
    return sum(1 for t in _tables(job) if any(getattr(c, "is_identity", False) for c in (getattr(t, "columns", None) or ())))


def _primary_keys(job: Any) -> int:
    return sum(1 for t in _tables(job) if any(getattr(i, "is_primary_key", False) for i in (getattr(t, "indexes", None) or ())))


def _external_tables(job: Any) -> int:
    return sum(1 for t in _tables(job) if getattr(t, "is_external", False))


def _security(job: Any) -> Any:
    cat = _catalog(job)
    return getattr(cat, "security", None) if cat is not None else None


def _masked_columns(job: Any) -> int:
    return len(getattr(_security(job), "masks", None) or ())


def _column_grants(job: Any) -> int:
    return sum(1 for p in (getattr(_security(job), "permissions", None) or ()) if getattr(p, "column", None))


def _multi_statement_functions(job: Any) -> int:
    return sum(1 for e in _extras(job, "Function") if (getattr(e, "metadata", {}) or {}).get("fabricSupport") not in (None, "", "supported"))


# ---- pipelines, connections, runtimes ----------------------------------------------------

def _linked_services(job: Any) -> Iterable[Any]:
    for r in _records(job, "linkedservice"):
        c = _content(r)
        if c is not None:
            yield c


def _runtime_types(job: Any) -> Dict[str, str]:
    return {getattr(e, "name", ""): str((getattr(e, "metadata", {}) or {}).get("type") or "") for e in _extras(job, "Integration Runtime")}


def _self_hosted_runtimes(job: Any) -> int:
    return sum(1 for t in _runtime_types(job).values() if t.lower() == "selfhosted")


def _managed_vnet_runtimes(job: Any) -> int:
    return sum(1 for e in _extras(job, "Integration Runtime") if (getattr(e, "metadata", {}) or {}).get("managedVirtualNetwork"))


def _linked_on_self_hosted(job: Any) -> int:
    runtimes = {name for name, t in _runtime_types(job).items() if t.lower() == "selfhosted"}
    return sum(1 for ls in _linked_services(job) if getattr(ls, "integration_runtime", None) in runtimes)


def _key_vault_linked(job: Any) -> int:
    return sum(1 for ls in _linked_services(job) if getattr(ls, "uses_key_vault", False))


def _managed_identity_linked(job: Any) -> int:
    return sum(1 for ls in _linked_services(job)
               if any(_value(getattr(a, "authentication_type", "")) == "managed_identity" for a in (getattr(ls, "authentication", None) or ())))


def _unsupported_activities(job: Any) -> Dict[str, int]:
    activities = pipelines = 0
    for r in _records(job, "pipeline"):
        unknown = [a for a in (getattr(_content(r), "all_activities", None) or ()) if not getattr(a, "recognized", True)]
        activities += len(unknown)
        pipelines += 1 if unknown else 0
    return {"activities": activities, "pipelines": pipelines}


def _triggers(job: Any) -> Dict[str, int]:
    out = {"schedule": 0, "tumblingWindow": 0, "event": 0, "other": 0, "started": 0}
    for e in _extras(job, "Trigger"):
        meta = getattr(e, "metadata", {}) or {}
        t = str(meta.get("triggerType") or "")
        key = "schedule" if t == "ScheduleTrigger" else "tumblingWindow" if t == "TumblingWindowTrigger" else "event" if "Event" in t else "other"
        out[key] += 1
        if str(meta.get("runtimeState") or "").lower() == "started":
            out["started"] += 1
    return out


def _estate(job: Any) -> Any:
    cat = _catalog(job)
    return getattr(cat, "estate", None) if cat is not None else None


def _estate_part(readable: str, value: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """A fact from the estate: None without a SQL pool, an error (reported unreadable) when that part could not be read."""
    def read(job: Any) -> Any:
        e = _estate(job)
        if e is None:
            return None
        if not getattr(e, readable):
            raise LookupError(readable)
        return value(e)
    return read


FACTS: Dict[str, Callable[[Any], Any]] = {
    "securityPolicies": _estate_part("policies_readable", lambda e: len(e.policies)),
    "blockPredicates": _estate_part("policies_readable", lambda e: sum(len(p.blocks) for p in e.policies)),
    "externalDataSources": _estate_part("external_readable", lambda e: len(e.data_sources)),
    "dataGb": _estate_part("sizes_readable", lambda e: round(e.total_kb / 1024 / 1024, 2)),
    "dataRows": _estate_part("sizes_readable", lambda e: e.total_rows),
    "largeTables": _estate_part("sizes_readable", lambda e: sum(1 for s in e.sizes if s.reserved_kb >= 100 * 1024 * 1024)),
    "peakConcurrency": _estate_part("workload_readable", lambda e: e.workload.peak_concurrency),
    "replayableQueries": _estate_part("workload_readable", lambda e: sum(1 for q in e.workload.samples if q.replayable)),
    "lakeDatabases": lambda job: sum(1 for _ in _extras(job, "Lake Database")),
    "mssparkutilsNotebooks": _mssparkutils,
    "notebooksWithSecrets": _notebook_secrets,
    "notebooksWithStoragePaths": _notebook_storage,
    "notebooksInstallingPackages": _notebook_packages,
    "customLibraries": _custom_libraries,
    "dynamicSqlObjects": _dynamic_sql,
    "crossDatabaseObjects": _cross_database,
    "distribution": _distribution,
    "columnstoreTables": _columnstore,
    "identityTables": _identity,
    "primaryKeyTables": _primary_keys,
    "externalTables": _external_tables,
    "externalSqlFeatures": lambda job: _script_features(job, "external_data_source", "external_file_format", "openrowset"),
    "maskedColumns": _masked_columns,
    "columnGrants": _column_grants,
    "multiStatementFunctions": _multi_statement_functions,
    "selfHostedRuntimes": _self_hosted_runtimes,
    "managedVnetRuntimes": _managed_vnet_runtimes,
    "linkedServicesOnSelfHostedRuntime": _linked_on_self_hosted,
    "keyVaultLinkedServices": _key_vault_linked,
    "managedIdentityLinkedServices": _managed_identity_linked,
    "unsupportedActivities": _unsupported_activities,
    "triggers": _triggers,
}

#: What the consultation may ask about but discovery cannot see, so it is never inferred.
NOT_RECORDED = [
    "materialized views", "sequences", "foreign keys",
    "managed private endpoints and firewall rules", "items already in the Fabric workspace",
]


def collect(job: Any) -> Dict[str, Any]:
    facts: Dict[str, Any] = {}
    unreadable = []
    for name, fn in FACTS.items():
        try:
            facts[name] = fn(job)
        except Exception:  # noqa: BLE001 - a fact that cannot be read is reported, never guessed
            unreadable.append(name)
    return {"facts": facts, "unreadable": unreadable, "notRecorded": NOT_RECORDED}
