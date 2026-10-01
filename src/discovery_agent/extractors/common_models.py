"""Models shared by every artifact extractor.

Only concepts that are genuinely the same thing across artifact types live
here. A model belongs in this module when its fields, its meaning, and its
serialized form are identical wherever it appears — not merely when two
extractors happen to have similar-looking dataclasses.

What is deliberately *not* here: resource/path references. The notebook and
dataset extractors both record URIs, but their ``category`` and ``detection``
vocabularies describe different things (scanning source code versus reading a
declared JSON field), so a shared class would widen what each one can mean.
``ArtifactReference`` likewise stays in ``extractors.models``: a reference to
a named Synapse artifact is a different concept from a path to a resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from discovery_agent.extractors.synapse_json import ExpressionForm


@dataclass(frozen=True)
class ConfigEntry:
    """One configuration key and its rendered value.

    How every extractor preserves curated configuration without copying the
    source document: a handler picks the settings that matter and records each
    as a key path plus a stable string rendering.
    """

    key: str  # e.g. "sink.tableOption", "spark.dynamicAllocation.enabled"
    value: str

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value}


@dataclass(frozen=True)
class ValueDeclaration:
    """A declared parameter or variable.

    ``default_value`` is the default rendered as a stable string;
    ``has_default`` distinguishes "no default declared" from "defaults to
    null", which mean different things when something binds the value.
    """

    name: str
    type: Optional[str] = None
    default_value: Optional[str] = None
    has_default: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "default_value": self.default_value,
            "has_default": self.has_default,
        }


@dataclass(frozen=True)
class SynapseExpression:
    """A Synapse expression, preserved exactly as authored.

    Never evaluated and never translated — rewriting it for Fabric belongs to
    a later migration stage, which needs the original to work from.
    """

    expression: str
    location: str  # JSON path, e.g. "properties.activities[0].outputs[0].parameters.x"
    form: ExpressionForm

    def to_dict(self) -> dict:
        return {
            "expression": self.expression,
            "location": self.location,
            "form": self.form.value,
        }


class SecretKind(str, Enum):
    """The mechanism by which a secret is referenced, never its value."""

    KEY_VAULT = "key_vault"  # {"type": "AzureKeyVaultSecret", "secretName": ...}
    SECURE_STRING = "secure_string"  # {"type": "SecureString", "value": ...}
    INLINE_LITERAL = "inline_literal"  # a secret-named property with a literal


@dataclass(frozen=True)
class SecretReference:
    """That a secret is referenced, and how — never what it is.

    ``secret_name`` is a name, not a value, so it is kept. A ``SecureString``
    or an inline literal carries no value into this model at all.
    """

    kind: SecretKind
    location: str
    secret_name: Optional[str] = None
    store_name: Optional[str] = None  # the Key Vault linked service
    property_name: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "location": self.location,
            "secret_name": self.secret_name,
            "store_name": self.store_name,
            "property_name": self.property_name,
        }


class AuthenticationType(str, Enum):
    """How an artifact authenticates. The mechanism, never the credential."""

    MANAGED_IDENTITY = "managed_identity"
    SERVICE_PRINCIPAL = "service_principal"
    SQL_LOGIN = "sql_login"
    ACCOUNT_KEY = "account_key"
    SAS = "sas"
    ANONYMOUS = "anonymous"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthenticationMetadata:
    """How an artifact authenticates. Structurally incapable of holding a secret.

    There is no value field, and there never will be: the guarantee is the
    shape of the class, not a redaction step that could be forgotten.
    """

    authentication_type: AuthenticationType
    location: str
    identity_name: Optional[str] = None  # principal name or id, not a credential
    target: Optional[str] = None  # the resource being authenticated to

    def to_dict(self) -> dict:
        return {
            "authentication_type": self.authentication_type.value,
            "location": self.location,
            "identity_name": self.identity_name,
            "target": self.target,
        }
