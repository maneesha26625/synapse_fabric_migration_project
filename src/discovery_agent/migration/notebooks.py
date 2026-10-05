"""Synapse notebook -> Fabric notebook.

A Synapse notebook is an ipynb document wrapped in a Synapse resource
(``properties`` carries ``cells``, ``metadata``, ``nbformat`` plus Synapse-only
keys such as ``bigDataPool`` and ``sessionProperties``). Fabric takes a plain
ipynb document, so the conversion keeps the cells and their language, drops
what only Synapse understands, and says what it dropped.

Cell code is never rewritten. Code that uses Synapse-only APIs is flagged for
review instead, because a silent rewrite of someone's code is worse than an
honest note.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, List, Mapping, Tuple

#: Synapse language names -> the language_info name a Fabric notebook uses.
_LANGUAGES = {
    "python": "python", "pyspark": "python",
    "scala": "scala", "spark": "scala",
    "sql": "sql", "sparksql": "sql",
    "r": "r", "sparkr": "r",
}
_DOTNET = {"csharp", "c#", "dotnet", ".net", "dotnetspark"}

#: Code that runs in Synapse but needs a look before it runs in Fabric.
_REVIEW = (
    (re.compile(r"\bTokenLibrary\b"), "TokenLibrary (use notebookutils.credentials in Fabric)"),
    (re.compile(r"\bsynapsesql\s*\("), "synapsesql connector (point it at a Fabric Warehouse or Lakehouse)"),
    (re.compile(r"mssparkutils\.credentials\.getConnectionStringOrCreds|getSecretWithLS|getPropertiesAll"), "linked-service credentials (no linked services in Fabric)"),
    (re.compile(r"\bmssparkutils\.env\b"), "mssparkutils.env (Synapse workspace details)"),
    (re.compile(r"\.dfs\.core\.windows\.net"), "ADLS paths (still reachable, consider OneLake shortcuts)"),
)


class NotebookNotMigratable(Exception):
    """The notebook cannot become a Fabric notebook without a rewrite."""


def _lines(source: Any) -> List[str]:
    if isinstance(source, list):
        return [str(s) for s in source]
    return str(source or "").splitlines(keepends=True)


def _language(properties: Mapping[str, Any]) -> str:
    metadata = properties.get("metadata") or {}
    raw = str(
        (metadata.get("language_info") or {}).get("name")
        or (metadata.get("kernelspec") or {}).get("language")
        or "python"
    ).strip().lower()
    if raw in _DOTNET:
        raise NotebookNotMigratable(".NET for Spark (C#) notebooks have no Fabric equivalent; rewrite it in PySpark or Scala.")
    return _LANGUAGES.get(raw, "python")


def to_fabric_ipynb(payload: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The Fabric ipynb document for one Synapse notebook resource, and what changed."""
    properties = payload.get("properties") or {}
    language = _language(properties)
    notes: List[str] = []

    pool = (properties.get("bigDataPool") or {}).get("referenceName")
    if pool:
        notes.append(f"Attached Spark pool '{pool}' dropped: Fabric runs it on the workspace's Spark settings.")
    if properties.get("sessionProperties"):
        notes.append("Spark session size settings dropped: set them in a Fabric environment if needed.")
    folder = (properties.get("folder") or {}).get("name")
    if folder:
        notes.append(f"Synapse folder '{folder}' not recreated: the notebook is placed at the workspace root.")

    cells: List[Dict[str, Any]] = []
    had_outputs = False
    flagged: List[str] = []
    for cell in properties.get("cells") or []:
        kind = cell.get("cell_type") if cell.get("cell_type") in ("code", "markdown", "raw") else "code"
        source = _lines(cell.get("source"))
        out: Dict[str, Any] = {"cell_type": kind, "source": source, "metadata": {}}
        language_override = ((cell.get("metadata") or {}).get("microsoft") or {}).get("language")
        if language_override:
            out["metadata"]["microsoft"] = {"language": str(language_override)}
        if kind == "code":
            had_outputs = had_outputs or bool(cell.get("outputs"))
            out["outputs"] = []
            out["execution_count"] = None
            text = "".join(source)
            for pattern, label in _REVIEW:
                if label not in flagged and pattern.search(text):
                    flagged.append(label)
        cells.append(out)

    if had_outputs:
        notes.append("Saved cell outputs were not copied.")
    if flagged:
        notes.append("Review before running, uses: " + "; ".join(flagged) + ".")

    document = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "language_info": {"name": language},
            "kernel_info": {"name": "synapse_pyspark"},
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
        },
        "cells": cells,
    }
    return document, notes


def create_body(display_name: str, document: Mapping[str, Any], description: str = "") -> Dict[str, Any]:
    """The Fabric ``POST /workspaces/{id}/notebooks`` body for an ipynb document."""
    payload = base64.b64encode(json.dumps(document).encode("utf-8")).decode("ascii")
    body: Dict[str, Any] = {
        "displayName": display_name,
        "definition": {
            "format": "ipynb",
            "parts": [{"path": "notebook-content.ipynb", "payload": payload, "payloadType": "InlineBase64"}],
        },
    }
    if description:
        body["description"] = description[:256]
    return body
