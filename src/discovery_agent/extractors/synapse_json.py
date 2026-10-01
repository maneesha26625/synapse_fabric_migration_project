"""Structural scanners for Synapse/ADF artifact JSON.

Two conventions run through every Synapse artifact, not just pipelines, so
they live here rather than in the pipeline extractor:

* A **reference** is an object carrying a string ``referenceName`` and a
  ``type`` ending in ``Reference``::

      {"referenceName": "tripsDataSource", "type": "DatasetReference"}

* An **expression** is either an object with ``type: "Expression"`` and a
  string ``value``, or a bare string beginning with ``@`` (ADF's own rule,
  where ``@@`` is the escape)::

      {"value": "@pipeline().parameters.KeyVaultName", "type": "Expression"}

Both are recognized by JSON *structure*, never by what a value looks like.
A field holding ``"customer-data"`` is a string; only the surrounding shape
makes something a reference. That is what keeps name guessing out of the
extractors.

Every find carries a JSON path so a reviewer can go straight to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional, Tuple

from discovery_agent.extractors.models import ReferenceKind
from discovery_agent.models import AssetType

REFERENCE_NAME_KEY = "referenceName"
REFERENCE_TYPE_SUFFIX = "Reference"
EXPRESSION_TYPE = "Expression"


class ExpressionForm(str, Enum):
    """How an expression was written in the source JSON."""

    OBJECT = "expression_object"  # {"value": "...", "type": "Expression"}
    INLINE = "inline_string"  # a bare string starting with "@"


@dataclass(frozen=True)
class JsonReference:
    """A ``*Reference`` object found in an artifact, exactly as written."""

    reference_name: str
    reference_type: str  # "DatasetReference", "LinkedServiceReference", ...
    location: str


@dataclass(frozen=True)
class JsonExpression:
    """A Synapse expression, preserved verbatim and never evaluated."""

    expression: str
    location: str
    form: ExpressionForm


# Reference type -> (the Synapse artifact it points at, the kind of link).
# A None artifact type means the target is real but is not a repository
# artifact — a Spark pool or SQL pool is workspace infrastructure, not a file.
REFERENCE_TYPES = {
    "DatasetReference": (AssetType.DATASET, ReferenceKind.ARTIFACT),
    "LinkedServiceReference": (AssetType.LINKED_SERVICE, ReferenceKind.ARTIFACT),
    "PipelineReference": (AssetType.PIPELINE, ReferenceKind.ARTIFACT),
    "DataFlowReference": (AssetType.DATAFLOW, ReferenceKind.ARTIFACT),
    "NotebookReference": (AssetType.NOTEBOOK, ReferenceKind.ARTIFACT),
    "SparkJobDefinitionReference": (
        AssetType.SPARK_JOB_DEFINITION,
        ReferenceKind.ARTIFACT,
    ),
    "TriggerReference": (AssetType.TRIGGER, ReferenceKind.ARTIFACT),
    "ManagedVirtualNetworkReference": (
        AssetType.MANAGED_VIRTUAL_NETWORK,
        ReferenceKind.ARTIFACT,
    ),
    "CredentialReference": (AssetType.CREDENTIAL, ReferenceKind.SECRET),
    "IntegrationRuntimeReference": (
        AssetType.INTEGRATION_RUNTIME,
        ReferenceKind.COMPUTE,
    ),
    "BigDataPoolReference": (None, ReferenceKind.COMPUTE),
    "SqlPoolReference": (None, ReferenceKind.COMPUTE),
    "LinkedServiceReferenceSecret": (AssetType.LINKED_SERVICE, ReferenceKind.SECRET),
}


def classify_reference(
    reference_type: str,
) -> Tuple[Optional[AssetType], ReferenceKind, bool]:
    """Map a ``*Reference`` type to (target type, kind, recognized)."""
    known = REFERENCE_TYPES.get(reference_type)
    if known is None:
        return None, ReferenceKind.UNKNOWN, False
    target_type, kind = known
    return target_type, kind, True


def is_reference_object(value: Any) -> bool:
    """Whether a JSON value is a Synapse ``*Reference`` object."""
    if not isinstance(value, dict):
        return False
    name = value.get(REFERENCE_NAME_KEY)
    reference_type = value.get("type")
    return (
        isinstance(name, str)
        and bool(name)
        and isinstance(reference_type, str)
        and reference_type.endswith(REFERENCE_TYPE_SUFFIX)
    )


def is_expression_object(value: Any) -> bool:
    """Whether a JSON value is an ``{"value": ..., "type": "Expression"}`` object."""
    return (
        isinstance(value, dict)
        and value.get("type") == EXPRESSION_TYPE
        and isinstance(value.get("value"), str)
    )


def is_inline_expression(value: Any) -> bool:
    """Whether a bare string is an ADF expression. ``@@`` is the escape."""
    return isinstance(value, str) and value.startswith("@") and not value.startswith("@@")


def join_path(base: str, key: str) -> str:
    return f"{base}.{key}" if base else key


def scan(document: Any, base_path: str = "") -> Tuple[
    Tuple[JsonReference, ...], Tuple[JsonExpression, ...]
]:
    """Walk a JSON value and collect every reference and expression in it.

    One traversal, deterministic: dictionary keys are visited in sorted order
    and both result lists are sorted by JSON path, so the same document always
    yields the same sequence.

    Expression objects are recorded and not descended into — their ``value``
    is the expression, not a nested document. Reference objects *are*
    descended into, because their ``parameters`` routinely hold expressions.
    """
    references: List[JsonReference] = []
    expressions: List[JsonExpression] = []
    _walk(document, base_path, references, expressions)
    return (
        tuple(sorted(references, key=lambda r: (r.location, r.reference_name))),
        tuple(sorted(expressions, key=lambda e: (e.location, e.expression))),
    )


def _walk(
    value: Any,
    path: str,
    references: List[JsonReference],
    expressions: List[JsonExpression],
) -> None:
    if isinstance(value, dict):
        if is_expression_object(value):
            expressions.append(
                JsonExpression(value["value"], path, ExpressionForm.OBJECT)
            )
            return
        if is_reference_object(value):
            references.append(
                JsonReference(value[REFERENCE_NAME_KEY], value["type"], path)
            )
        for key in sorted(value.keys()):
            _walk(value[key], join_path(path, key), references, expressions)
        return

    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", references, expressions)
        return

    if is_inline_expression(value):
        expressions.append(JsonExpression(value, path, ExpressionForm.INLINE))


def render_scalar(value: Any) -> Optional[str]:
    """Render a JSON scalar as a stable string, or None when it is absent.

    Booleans render as ``true``/``false`` rather than Python's ``True``, so
    the output reads like the source. Non-scalars are JSON-encoded with sorted
    keys so the rendering is deterministic.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
