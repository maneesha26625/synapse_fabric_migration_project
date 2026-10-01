"""Putting SQL scan findings onto the extractor framework's channels.

``sql_scanning`` answers what the SQL text says. This module answers how that
shows up in an ``ExtractionResult`` — as references other stages can follow,
and as issues that admit what could not be determined. The two are kept apart
so the text scanner stays free of framework types, and so every extractor
that reads SQL reports it the same way rather than each inventing a variant.

Both conversions are deliberately parameterized on the *carrier*: a SQL
script and a pipeline activity's embedded script produce the same SQL
findings, but attribute them to different artifacts.
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    IssueCode,
    ReferenceKind,
)
from discovery_agent.extractors.sql_models import DynamicSqlSite, SqlObjectReference
from discovery_agent.models import AssetType, Evidence


def sql_object_references(
    objects: Sequence[SqlObjectReference],
    *,
    source_artifact_id: str,
    source_artifact_type: AssetType,
    source_path: str,
    extractor: str,
) -> Tuple[ArtifactReference, ...]:
    """Durable SQL objects, surfaced on the framework reference channel.

    ``target_type`` is None because a SQL object is not a repository
    artifact, and ``resolved`` stays False: naming a table is an observation,
    not proof the table exists. Temporary objects are left out — a ``#temp``
    is not a dependency on anything — while an interpolated name is kept,
    because a dependency nobody can resolve is still a dependency.

    One reference per distinct qualified name, keeping the first occurrence's
    location; the richer per-occurrence view stays in the typed model.
    """
    first: Dict[str, SqlObjectReference] = {}
    for obj in objects:
        if not obj.is_durable:
            continue
        first.setdefault(obj.qualified_name, obj)
    return tuple(
        ArtifactReference(
            source_artifact_id=source_artifact_id,
            source_artifact_type=source_artifact_type,
            kind=ReferenceKind.SQL_OBJECT,
            target_type=None,
            target_name=name,
            location=obj.location,
            evidence=Evidence(source_path, None, extractor),
        )
        for name, obj in sorted(first.items())
    )


def dynamic_sql_warning(
    sites: Sequence[DynamicSqlSite],
) -> Optional[ExtractionIssue]:
    """The issue that says runtime-assembled SQL hid its dependencies.

    None when there is nothing to admit. The count matters more than the
    detail: a reviewer needs to know how much of the SQL went unread, and the
    sites themselves are already in the model.
    """
    if not sites:
        return None
    return ExtractionIssue(
        IssueCode.UNSUPPORTED_CONSTRUCT,
        f"{len(sites)} dynamic SQL site(s) present; their dependencies are "
        f"not statically determinable and were not resolved",
        sites[0].location,
    )
