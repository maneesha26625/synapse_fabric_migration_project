"""Shared detection of secret references in Synapse artifact JSON.

Security-critical and therefore shared: a fix here reaches every extractor,
which a copy in each would not.

The rule records that a secret is referenced and where — never its value.
Property names ending in "name" are excluded on purpose: ``keyVaultName`` and
``secretName`` are identifiers, and redacting them would destroy real
migration information while protecting nothing.
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

from discovery_agent.extractors.common_models import SecretKind, SecretReference

#: Property names that denote a secret *value*.
SECRET_PROPERTY_NAMES = re.compile(
    r"^(password|passwd|pwd|secret|token|api_?key|access_?key|account_?key|"
    r"sas_?token|sas|connection_?string|conn_?str|client_?secret|credential)$",
    re.IGNORECASE,
)


def is_secret_property(name: str) -> bool:
    """Whether a property name denotes a secret value rather than an identifier."""
    return bool(SECRET_PROPERTY_NAMES.match(name))


def scan_secrets(node: Any, path: str) -> Tuple[SecretReference, ...]:
    """Record every secret reference under ``node``. Never reads a value."""
    found: List[SecretReference] = []
    _walk(node, path, found)
    return tuple(sorted(found, key=lambda s: (s.location, s.kind.value)))


def _walk(node: Any, path: str, found: List[SecretReference]) -> None:
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type == "AzureKeyVaultSecret":
            store = node.get("store")
            found.append(
                SecretReference(
                    kind=SecretKind.KEY_VAULT,
                    location=path,
                    secret_name=node.get("secretName")
                    if isinstance(node.get("secretName"), str)
                    else None,
                    store_name=store.get("referenceName")
                    if isinstance(store, dict)
                    and isinstance(store.get("referenceName"), str)
                    else None,
                )
            )
            return  # never descend into a secret container
        if node_type == "SecureString":
            found.append(SecretReference(kind=SecretKind.SECURE_STRING, location=path))
            return
        for key in sorted(node.keys()):
            value = node[key]
            if is_secret_property(key) and isinstance(value, str) and value:
                found.append(
                    SecretReference(
                        kind=SecretKind.INLINE_LITERAL,
                        location=f"{path}.{key}",
                        property_name=key,
                    )
                )
                continue
            _walk(value, f"{path}.{key}", found)
        return
    if isinstance(node, list):
        for index, item in enumerate(node):
            _walk(item, f"{path}[{index}]", found)
