"""Typed model of a Synapse linked service.

A linked service says what external system an artifact connects to and how
that connection authenticates. It is the one artifact type where getting the
security handling wrong would be actively dangerous, so the model is built so
that a secret has nowhere to live:

* Values of secret-bearing properties are never carried. ``SecretReference``
  records that a secret exists, where, and which vault holds it.
* A connection string is decomposed into its non-secret keywords. The raw
  string is never stored, because a connection string is exactly the kind of
  value that carries a password in some other repository.

What the model does not do is judge any of it. Whether an ``AzureSqlDW``
linked service maps onto a Fabric connection is Assessment's question.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from discovery_agent.extractors.common_models import (
    AuthenticationMetadata,
    ConfigEntry,
    SecretReference,
    SynapseExpression,
    ValueDeclaration,
)
from discovery_agent.extractors.models import ArtifactReference


class LinkedServiceCategory(str, Enum):
    """What family of system a linked service connects to.

    Normalized across Synapse's many concrete type names so downstream stages
    can reason about "a data lake" without enumerating every ADLS spelling.
    """

    DATA_LAKE = "data_lake"  # AzureBlobFS, AzureDataLakeStore
    BLOB_STORAGE = "blob_storage"  # AzureBlobStorage, AzureFileStorage
    SQL_DATA_WAREHOUSE = "sql_data_warehouse"  # AzureSqlDW, AzureSynapseAnalytics
    SQL_DATABASE = "sql_database"  # AzureSqlDatabase, SqlServer
    KEY_VAULT = "key_vault"  # AzureKeyVault
    HTTP = "http"  # HttpServer, Web
    REST = "rest"  # RestService
    BUSINESS_INTELLIGENCE = "business_intelligence"  # PowerBIWorkspace
    COMPUTE = "compute"  # Databricks, HDInsight, Function
    UNKNOWN = "unknown"


class ResourceKind(str, Enum):
    """What kind of external target a linked service addresses.

    Deliberately distinct from the dataset extractor's ``ResourceCategory``,
    which describes where *data* sits. A linked service addresses a *system*.
    """

    ENDPOINT = "endpoint"  # a URL
    STORAGE_ACCOUNT = "storage_account"
    SQL_SERVER = "sql_server"
    KEY_VAULT = "key_vault"
    BI_WORKSPACE = "bi_workspace"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LinkedServiceResource:
    """An external system this linked service points at.

    ``is_dynamic`` marks a target assembled at runtime from parameters — very
    common, and something a migration plan has to account for because the
    concrete target is not knowable from the repository.
    """

    kind: ResourceKind
    value: str  # the URL, server or account as written, expression included
    location: str  # JSON path
    is_dynamic: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "value": self.value,
            "location": self.location,
            "is_dynamic": self.is_dynamic,
        }


@dataclass(frozen=True)
class LinkedServiceConnection:
    """Where the linked service connects, decomposed and secret-free.

    Populated from whichever shape the type uses — a ``url``, a ``baseUrl``,
    or the non-secret keywords of a ``connectionString``. The raw connection
    string is never kept.
    """

    category: LinkedServiceCategory = LinkedServiceCategory.UNKNOWN
    endpoint: Optional[str] = None  # url / baseUrl, expression included
    server: Optional[str] = None  # Data Source from a connection string
    database: Optional[str] = None  # Initial Catalog
    user_id: Optional[str] = None  # a login name, not a credential
    tenant_id: Optional[str] = None
    workspace_id: Optional[str] = None
    settings: Tuple[ConfigEntry, ...] = ()  # remaining non-secret keywords

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.endpoint,
                self.server,
                self.database,
                self.user_id,
                self.tenant_id,
                self.workspace_id,
                self.settings,
            )
        )

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "endpoint": self.endpoint,
            "server": self.server,
            "database": self.database,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "settings": [s.to_dict() for s in self.settings],
        }


@dataclass(frozen=True)
class LinkedServiceDefinition:
    """What one Synapse linked service declares.

    ``parameters`` reuses the shared ``ValueDeclaration``; a parameter whose
    name denotes a secret has its default dropped and a ``SecretReference``
    recorded in its place.
    """

    name: str
    type: str
    category: LinkedServiceCategory = LinkedServiceCategory.UNKNOWN
    description: Optional[str] = None
    annotations: Tuple[str, ...] = ()
    parameters: Tuple[ValueDeclaration, ...] = ()
    connection: Optional[LinkedServiceConnection] = None
    authentication: Tuple[AuthenticationMetadata, ...] = ()
    resources: Tuple[LinkedServiceResource, ...] = ()
    settings: Tuple[ConfigEntry, ...] = ()
    references: Tuple[ArtifactReference, ...] = ()
    connect_via: Optional[ArtifactReference] = None  # integration runtime
    expressions: Tuple[SynapseExpression, ...] = ()
    secrets: Tuple[SecretReference, ...] = ()
    recognized: bool = True  # False when no type handler matched

    @property
    def integration_runtime(self) -> Optional[str]:
        return self.connect_via.target_name if self.connect_via else None

    @property
    def key_vault_stores(self) -> Tuple[str, ...]:
        """Vault linked services this one fetches secrets from."""
        return tuple(sorted({s.store_name for s in self.secrets if s.store_name}))

    @property
    def is_parameterized(self) -> bool:
        return bool(self.parameters) or bool(self.expressions)

    @property
    def uses_key_vault(self) -> bool:
        return bool(self.key_vault_stores)

    def summary(self) -> dict:
        """A compact overview. Carries no secret values, by construction."""
        return {
            "name": self.name,
            "type": self.type,
            "category": self.category.value,
            "recognized": self.recognized,
            "endpoint": self.connection.endpoint if self.connection else None,
            "authentication": [a.authentication_type.value for a in self.authentication],
            "integration_runtime": self.integration_runtime,
            "key_vault_stores": list(self.key_vault_stores),
            "parameter_count": len(self.parameters),
            "reference_count": len(self.references),
            "expression_count": len(self.expressions),
            "secret_count": len(self.secrets),
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "category": self.category.value,
            "description": self.description,
            "annotations": list(self.annotations),
            "parameters": [p.to_dict() for p in self.parameters],
            "connection": self.connection.to_dict() if self.connection else None,
            "authentication": [a.to_dict() for a in self.authentication],
            "resources": [r.to_dict() for r in self.resources],
            "settings": [s.to_dict() for s in self.settings],
            "references": [r.to_dict() for r in self.references],
            "connect_via": self.connect_via.to_dict() if self.connect_via else None,
            "expressions": [e.to_dict() for e in self.expressions],
            "secrets": [s.to_dict() for s in self.secrets],
            "recognized": self.recognized,
        }
