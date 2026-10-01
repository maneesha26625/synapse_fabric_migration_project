"""Which source is authoritative for each P0 Synapse artifact.

A migration tool is only as trustworthy as its inputs. Some Synapse artifacts
are files in Git; some exist only inside a SQL database; some are ARM
resources. Guessing wrong means either missing an artifact entirely or
reporting a definition that is not the one running in production.

This module states, per artifact and per source, what can be obtained, what
cannot, and which source wins. It is data and policy only: nothing here
connects to anything, and the Discovery Agent reads it to decide what it
*would* need to connect to.

Three kinds of information are kept apart deliberately, because the same
artifact draws on different sources for each:

* **Definition** — the thing itself: pipeline JSON, view DDL, notebook code.
* **Runtime** — published state, live configuration, execution history.
* **Infrastructure** — the Azure resources and identities underneath.

Reuse, not duplication: ``SourceType`` comes from the extractor framework and
``AssetType`` from the discovery model. The three SQL database objects are
deliberately *not* added to ``AssetType`` — that enum means "artifact kinds
found in a Synapse Git repository", and a dedicated SQL table is not one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

from discovery_agent.extractors.models import SourceType
from discovery_agent.models import AssetType


class P0Artifact(str, Enum):
    """The nine frozen P0 data-engineering artifacts.

    Values match ``AssetType`` where the artifact is also a repository
    artifact, so the two can be joined without a translation table.
    """

    DEDICATED_SQL_TABLE = "dedicated_sql_table"
    SQL_VIEW = "sql_view"
    STORED_PROCEDURE = "stored_procedure"
    SQL_SCRIPT = "sqlscript"
    PIPELINE = "pipeline"
    DATASET = "dataset"
    LINKED_SERVICE = "linkedService"
    NOTEBOOK = "notebook"
    SPARK_JOB_DEFINITION = "sparkJobDefinition"

    @property
    def asset_type(self) -> Optional[AssetType]:
        """The repository asset type, or None for a database-only object."""
        try:
            return AssetType(self.value)
        except ValueError:
            return None

    @property
    def is_repository_artifact(self) -> bool:
        return self.asset_type is not None

    @classmethod
    def from_asset_type(cls, asset_type: AssetType) -> Optional["P0Artifact"]:
        """The P0 artifact for a repository asset type, or None if out of scope.

        Only the six repository artifacts map; TRIGGER, DATAFLOW and the rest
        are real asset types but are not in P0.
        """
        try:
            return cls(asset_type.value)
        except ValueError:
            return None


class SourceRole(str, Enum):
    """How much weight a source carries for one artifact."""

    PRIMARY = "primary"  # the authoritative definition comes from here
    SECONDARY = "secondary"  # authoritative for its own facet; enrichment
    INCIDENTAL = "incidental"  # may appear here, but never authoritative
    UNAVAILABLE = "unavailable"  # this source cannot supply this artifact


class InformationClass(str, Enum):
    """Which of the three kinds of information a source supplies."""

    DEFINITION = "definition"
    RUNTIME = "runtime"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class SourceEndpoint:
    """What the Discovery Agent would have to connect to, and how.

    Recorded so the connectivity and permission requirements of a future
    discovery run can be derived from this module rather than rediscovered.
    """

    source: SourceType
    endpoint: str
    protocol: str
    credential: str  # the *kind* of credential; never a credential itself

    def to_dict(self) -> dict:
        return {
            "source": self.source.value,
            "endpoint": self.endpoint,
            "protocol": self.protocol,
            "credential": self.credential,
        }


#: The four connection targets, one per source. Endpoints are templates; the
#: credential column names an auth *mechanism*, never a secret.
SOURCE_ENDPOINTS: Dict[SourceType, SourceEndpoint] = {
    SourceType.REPOSITORY: SourceEndpoint(
        source=SourceType.REPOSITORY,
        endpoint="input/repository/<repo>/ (local clone from acquisition)",
        protocol="filesystem",
        credential="none — acquisition already cloned the snapshot",
    ),
    SourceType.SYNAPSE: SourceEndpoint(
        source=SourceType.SYNAPSE,
        endpoint="https://<workspace>.dev.azuresynapse.net",
        protocol="https (Synapse Artifacts data-plane REST)",
        credential="Entra ID token, audience https://dev.azuresynapse.net",
    ),
    SourceType.SQL: SourceEndpoint(
        source=SourceType.SQL,
        endpoint=(
            "<workspace>.sql.azuresynapse.net:1433 (dedicated pool) and "
            "<workspace>-ondemand.sql.azuresynapse.net:1433 (serverless)"
        ),
        protocol="TDS",
        credential="Entra ID token or SQL login, read access to catalog views",
    ),
    SourceType.AZURE: SourceEndpoint(
        source=SourceType.AZURE,
        endpoint="https://management.azure.com",
        protocol="https (ARM)",
        credential="Entra ID token, Reader on the workspace resource group",
    ),
}


@dataclass(frozen=True)
class SourceCapability:
    """What one source can and cannot tell us about one artifact."""

    source: SourceType
    role: SourceRole
    provides: Tuple[InformationClass, ...] = ()
    available: Tuple[str, ...] = ()  # what this source does supply
    unavailable: Tuple[str, ...] = ()  # what it cannot, so nobody assumes it
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "source": self.source.value,
            "role": self.role.value,
            "provides": [p.value for p in self.provides],
            "available": list(self.available),
            "unavailable": list(self.unavailable),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ArtifactSourceStrategy:
    """Where one artifact's information comes from, source by source."""

    artifact: P0Artifact
    summary: str
    capabilities: Tuple[SourceCapability, ...]

    def __post_init__(self) -> None:
        """Every artifact has exactly one primary source, and no duplicates."""
        sources = [c.source for c in self.capabilities]
        if len(sources) != len(set(sources)):
            raise ValueError(f"{self.artifact.value}: duplicate source entries")
        primaries = [c for c in self.capabilities if c.role is SourceRole.PRIMARY]
        if len(primaries) != 1:
            raise ValueError(
                f"{self.artifact.value}: expected exactly one primary source, "
                f"found {len(primaries)}"
            )

    @property
    def asset_type(self) -> Optional[AssetType]:
        return self.artifact.asset_type

    @property
    def primary(self) -> SourceCapability:
        return next(c for c in self.capabilities if c.role is SourceRole.PRIMARY)

    @property
    def primary_source(self) -> SourceType:
        return self.primary.source

    @property
    def secondary_sources(self) -> Tuple[SourceType, ...]:
        return tuple(
            c.source for c in self.capabilities if c.role is SourceRole.SECONDARY
        )

    @property
    def required_sources(self) -> Tuple[SourceType, ...]:
        """Sources that must be reachable to discover this artifact fully."""
        return (self.primary_source,) + self.secondary_sources

    def capability(self, source: SourceType) -> Optional[SourceCapability]:
        return next((c for c in self.capabilities if c.source is source), None)

    def role_of(self, source: SourceType) -> SourceRole:
        found = self.capability(source)
        return found.role if found else SourceRole.UNAVAILABLE

    def to_dict(self) -> dict:
        return {
            "artifact": self.artifact.value,
            "asset_type": self.asset_type.value if self.asset_type else None,
            "is_repository_artifact": self.artifact.is_repository_artifact,
            "summary": self.summary,
            "primary_source": self.primary_source.value,
            "secondary_sources": [s.value for s in self.secondary_sources],
            "capabilities": [c.to_dict() for c in self.capabilities],
        }


# --- security policy ---------------------------------------------------------

#: Discovery never retrieves these, from any source, under any configuration.
#: Not "redacts after reading" — never requests them in the first place.
NEVER_RETRIEVE: Tuple[str, ...] = (
    "secret values",
    "passwords",
    "storage account access keys",
    "SAS tokens",
    "access tokens and refresh tokens",
    "client secrets",
    "certificate private keys",
    "connection strings containing embedded credentials",
)

#: Safe metadata: describes how authentication is arranged, not what it is.
SAFE_TO_RECORD: Tuple[str, ...] = (
    "authentication type (managed identity, service principal, SQL login, key)",
    "Key Vault linked service name and secret *name*",
    "linked service name and type",
    "managed identity type and principal id",
    "endpoint hostnames and resource ids",
    "role assignment names and scopes",
    "network posture (private endpoint present, firewall enabled)",
)


# --- the strategy ------------------------------------------------------------


def _capability(source, role, provides=(), available=(), unavailable=(), notes=None):
    return SourceCapability(
        source=source,
        role=role,
        provides=tuple(provides),
        available=tuple(available),
        unavailable=tuple(unavailable),
        notes=notes,
    )


_STRATEGIES: Tuple[ArtifactSourceStrategy, ...] = (
    # ---------------------------------------------------------------- 1
    ArtifactSourceStrategy(
        artifact=P0Artifact.DEDICATED_SQL_TABLE,
        summary=(
            "Physical schema exists only inside the dedicated pool. Git may "
            "name a table but never defines its shape."
        ),
        capabilities=(
            _capability(
                SourceType.SQL,
                SourceRole.PRIMARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "sys.tables / sys.columns / sys.types: columns, types, nullability",
                    "sys.pdw_table_distribution_properties: HASH / ROUND_ROBIN / REPLICATE",
                    "sys.pdw_column_distribution_properties: the distribution column",
                    "sys.indexes: clustered columnstore, clustered, heap",
                    "sys.partitions and partition boundaries",
                    "sys.stats: statistics objects",
                    "row counts via sys.dm_pdw_nodes_db_partition_stats",
                    "constraints and defaults",
                ),
                unavailable=(
                    "which pipeline or notebook creates or loads the table",
                    "business meaning, ownership, retention intent",
                ),
                notes=(
                    "Requires a connection per database: the dedicated pool and "
                    "the serverless endpoint are different hosts."
                ),
            ),
            _capability(
                SourceType.REPOSITORY,
                SourceRole.INCIDENTAL,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "table *names* referenced by datasets (typeProperties.table)",
                    "table names appearing in pipeline preCopyScript / sqlReaderQuery",
                    "CREATE TABLE DDL only if someone committed it as a SQL script",
                ),
                unavailable=(
                    "columns, data types, distribution, indexing, partitioning",
                    "row counts and statistics",
                ),
                notes=(
                    "Verified against the sample snapshot: TripsData, FaresData and "
                    "AggregateTaxiData are named, but the Copy sink uses "
                    "tableOption=autoCreate, so the shape is created at runtime and "
                    "is not declared anywhere in Git."
                ),
            ),
            _capability(
                SourceType.AZURE,
                SourceRole.SECONDARY,
                provides=(InformationClass.INFRASTRUCTURE,),
                available=(
                    "pool SKU / DWU tier and current pause state",
                    "collation, geo-backup and restore point policy",
                    "firewall rules and private endpoint configuration",
                ),
                unavailable=("anything about the table itself",),
            ),
            _capability(
                SourceType.SYNAPSE,
                SourceRole.UNAVAILABLE,
                notes=(
                    "The Artifacts REST API serves workspace artifacts. Database "
                    "objects are not workspace artifacts and are not exposed by it."
                ),
            ),
        ),
    ),
    # ---------------------------------------------------------------- 2
    ArtifactSourceStrategy(
        artifact=P0Artifact.SQL_VIEW,
        summary=(
            "View DDL lives in the database catalog. Serverless views are the "
            "common case in Synapse and sit on a different endpoint."
        ),
        capabilities=(
            _capability(
                SourceType.SQL,
                SourceRole.PRIMARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "sys.views plus sys.sql_modules.definition: the full CREATE VIEW text",
                    "sys.columns: the projected column list and types",
                    "sys.sql_expression_dependencies: objects the view reads",
                    "schema binding and check-option flags",
                ),
                unavailable=(
                    "the definition of a view created WITH ENCRYPTION "
                    "(sys.sql_modules.definition is NULL)",
                    "objects referenced only through dynamic SQL",
                ),
                notes=(
                    "Serverless views over OPENROWSET/external tables live in the "
                    "serverless endpoint's databases, not the dedicated pool. Both "
                    "endpoints must be enumerated or serverless views are missed."
                ),
            ),
            _capability(
                SourceType.REPOSITORY,
                SourceRole.INCIDENTAL,
                provides=(InformationClass.DEFINITION,),
                available=("CREATE VIEW text if committed as a SQL script artifact",),
                unavailable=(
                    "whether the committed text matches what is deployed",
                    "views created outside source control",
                ),
                notes="No SQL scripts and no view DDL exist in the sample snapshot.",
            ),
            _capability(
                SourceType.AZURE,
                SourceRole.SECONDARY,
                provides=(InformationClass.INFRASTRUCTURE,),
                available=("which pools and endpoints exist and are reachable",),
                unavailable=("view definitions",),
            ),
            _capability(
                SourceType.SYNAPSE,
                SourceRole.UNAVAILABLE,
                notes="Database objects are not workspace artifacts.",
            ),
        ),
    ),
    # ---------------------------------------------------------------- 3
    ArtifactSourceStrategy(
        artifact=P0Artifact.STORED_PROCEDURE,
        summary=(
            "Procedure bodies live in the catalog. Pipelines reveal that a "
            "procedure is called, never what it does."
        ),
        capabilities=(
            _capability(
                SourceType.SQL,
                SourceRole.PRIMARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "sys.procedures plus sys.sql_modules.definition: the full body",
                    "sys.parameters: parameter names, types, direction, defaults",
                    "sys.sql_expression_dependencies: statically referenced objects",
                ),
                unavailable=(
                    "the body of a procedure created WITH ENCRYPTION",
                    "objects touched only via dynamic SQL built at runtime",
                    "the caller, unless a pipeline names it",
                ),
            ),
            _capability(
                SourceType.REPOSITORY,
                SourceRole.INCIDENTAL,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "procedure *names* invoked by SqlPoolStoredProcedure activities "
                    "(the pipeline extractor already emits these as SQL_OBJECT references)",
                    "CREATE PROCEDURE text if committed as a SQL script",
                ),
                unavailable=("the procedure body, parameters, and dependencies",),
            ),
            _capability(
                SourceType.AZURE,
                SourceRole.SECONDARY,
                provides=(InformationClass.INFRASTRUCTURE,),
                available=("the pool the procedure lives in, and its state",),
                unavailable=("procedure definitions",),
            ),
            _capability(
                SourceType.SYNAPSE,
                SourceRole.UNAVAILABLE,
                notes="Database objects are not workspace artifacts.",
            ),
        ),
    ),
    # ---------------------------------------------------------------- 4
    ArtifactSourceStrategy(
        artifact=P0Artifact.SQL_SCRIPT,
        summary=(
            "A workspace artifact holding T-SQL text. The script is the "
            "artifact; the objects it touches are separate and need SQL."
        ),
        capabilities=(
            _capability(
                SourceType.REPOSITORY,
                SourceRole.PRIMARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "sqlscript/*.json: properties.content.query, verbatim T-SQL",
                    "currentConnection: the pool or database the script targets",
                    "folder, type (SqlQuery), annotations",
                ),
                unavailable=(
                    "whether the script has been published",
                    "whether the objects it references exist or match",
                    "results of ever having been run",
                ),
                notes=(
                    "The sample snapshot contains no sqlscript/ folder, so this "
                    "path is unexercised against real data."
                ),
            ),
            _capability(
                SourceType.SYNAPSE,
                SourceRole.SECONDARY,
                provides=(InformationClass.RUNTIME,),
                available=(
                    "GET /sqlScripts: the published version",
                    "drift between the published version and the Git commit",
                ),
                unavailable=("anything the Git copy does not already contain",),
            ),
            _capability(
                SourceType.SQL,
                SourceRole.SECONDARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "existence and shape of the tables, views and procedures the "
                    "script references"
                ),
                unavailable=("the script text itself",),
            ),
            _capability(
                SourceType.AZURE,
                SourceRole.INCIDENTAL,
                provides=(InformationClass.INFRASTRUCTURE,),
                available=("the pool named by currentConnection, and its state",),
                unavailable=("the script text",),
            ),
        ),
    ),
    # ---------------------------------------------------------------- 5
    ArtifactSourceStrategy(
        artifact=P0Artifact.PIPELINE,
        summary=(
            "Fully defined in Git when the workspace is Git-integrated. "
            "Already implemented by PipelineExtractor."
        ),
        capabilities=(
            _capability(
                SourceType.REPOSITORY,
                SourceRole.PRIMARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "pipeline/*.json: activities, nesting, dependsOn and conditions",
                    "parameters, variables, policies, user properties",
                    "dataset / linked service / data flow / notebook references",
                    "embedded SQL and dynamic expressions, verbatim",
                ),
                unavailable=(
                    "whether the committed version is the published one",
                    "run history, durations, failures",
                    "whether referenced artifacts actually resolve",
                ),
            ),
            _capability(
                SourceType.SYNAPSE,
                SourceRole.SECONDARY,
                provides=(InformationClass.RUNTIME,),
                available=(
                    "GET /pipelines: the published definition, for drift detection",
                    "trigger state (started / stopped) and trigger-to-pipeline bindings",
                    "pipeline run history and activity run outcomes",
                ),
                unavailable=("anything not yet published",),
            ),
            _capability(
                SourceType.AZURE,
                SourceRole.SECONDARY,
                provides=(InformationClass.INFRASTRUCTURE,),
                available=(
                    "integration runtime type and configuration (Azure vs self-hosted)",
                    "managed VNet and managed private endpoint state",
                    "workspace managed identity",
                ),
                unavailable=("the pipeline definition",),
            ),
            _capability(
                SourceType.SQL,
                SourceRole.UNAVAILABLE,
                notes=(
                    "Contributes nothing about the pipeline itself. It is still "
                    "needed to resolve the SQL objects a pipeline's preCopyScript "
                    "and sqlReaderQuery touch — that is discovery of those objects, "
                    "not of the pipeline."
                ),
            ),
        ),
    ),
    # ---------------------------------------------------------------- 6
    ArtifactSourceStrategy(
        artifact=P0Artifact.DATASET,
        summary=(
            "Fully defined in Git. The physical schema behind it is not — the "
            "declared column list is the author's claim, not the table."
        ),
        capabilities=(
            _capability(
                SourceType.REPOSITORY,
                SourceRole.PRIMARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "dataset/*.json: type, linked service reference, parameters",
                    "location (file system, path, file name) or table / schema / database",
                    "declared column list and format settings",
                    "expressions, verbatim",
                ),
                unavailable=(
                    "the physical schema of the table or file it addresses",
                    "whether the target exists",
                    "published state",
                ),
            ),
            _capability(
                SourceType.SQL,
                SourceRole.SECONDARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "the real schema of the table a SQL dataset names",
                    "confirmation that the target table exists",
                ),
                unavailable=("the dataset declaration itself",),
                notes=(
                    "DatasetExtractor already separates the declared schema from "
                    "the physical one; this is the source that supplies the latter."
                ),
            ),
            _capability(
                SourceType.AZURE,
                SourceRole.SECONDARY,
                provides=(InformationClass.INFRASTRUCTURE,),
                available=(
                    "existence of the storage account and container behind a file dataset",
                    "storage firewall and private endpoint posture",
                ),
                unavailable=("the dataset declaration",),
            ),
            _capability(
                SourceType.SYNAPSE,
                SourceRole.SECONDARY,
                provides=(InformationClass.RUNTIME,),
                available=("GET /datasets: the published definition, for drift detection",),
                unavailable=("anything not yet published",),
            ),
        ),
    ),
    # ---------------------------------------------------------------- 7
    ArtifactSourceStrategy(
        artifact=P0Artifact.LINKED_SERVICE,
        summary=(
            "Configuration is in Git, but only Azure can say whether the "
            "identity behind it actually has access. Secrets are never read."
        ),
        capabilities=(
            _capability(
                SourceType.REPOSITORY,
                SourceRole.PRIMARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "linkedService/*.json: type and typeProperties",
                    "target endpoint (account name, URL, server, database)",
                    "authentication type and, where used, the Key Vault reference "
                    "(linked service name plus secret *name*)",
                    "parameters, connectVia integration runtime reference",
                ),
                unavailable=(
                    "secret values — Synapse never writes them to Git, and this "
                    "tool would not read them if it did",
                    "whether the credential is valid or the target is reachable",
                    "what permissions the identity actually holds",
                ),
            ),
            _capability(
                SourceType.AZURE,
                SourceRole.SECONDARY,
                provides=(InformationClass.INFRASTRUCTURE,),
                available=(
                    "workspace managed identity type and principal id",
                    "role assignments granting that identity access to the target",
                    "existence of the referenced Key Vault and its access model "
                    "(never the secret values)",
                    "private endpoint and firewall posture of the target resource",
                    "whether the target resource still exists",
                ),
                unavailable=("secret values, under any configuration",),
                notes=(
                    "The artifact where infrastructure metadata matters most: a "
                    "linked service that parses cleanly can still be non-functional "
                    "because a role assignment is missing."
                ),
            ),
            _capability(
                SourceType.SYNAPSE,
                SourceRole.SECONDARY,
                provides=(InformationClass.RUNTIME,),
                available=(
                    "GET /linkedservices: the published definition, with secrets masked",
                    "drift between published and committed",
                ),
                unavailable=("secret values — the API masks them too",),
            ),
            _capability(
                SourceType.SQL,
                SourceRole.UNAVAILABLE,
                notes="A linked service is not a database object.",
            ),
        ),
    ),
    # ---------------------------------------------------------------- 8
    ArtifactSourceStrategy(
        artifact=P0Artifact.NOTEBOOK,
        summary=(
            "Code and session config are in Git. The pool it runs on, and the "
            "libraries installed there, are not."
        ),
        capabilities=(
            _capability(
                SourceType.REPOSITORY,
                SourceRole.PRIMARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "notebook/*.json: every cell's source, verbatim",
                    "declared language, kernel, nbformat",
                    "attached Spark pool reference and session sizing",
                    "Synapse-specific constructs, magics, embedded SQL, imports",
                ),
                unavailable=(
                    "libraries installed on the pool rather than declared in code",
                    "the Spark runtime version actually in force",
                    "published state and execution history",
                ),
            ),
            _capability(
                SourceType.AZURE,
                SourceRole.SECONDARY,
                provides=(InformationClass.INFRASTRUCTURE,),
                available=(
                    "Spark pool node size, node count, auto-scale and auto-pause",
                    "Spark version and the pool's library requirements "
                    "(requirements.txt / workspace packages), which are not in Git",
                    "session-level configuration applied at pool scope",
                ),
                unavailable=("notebook code",),
                notes=(
                    "Pool-installed libraries are a common migration surprise: the "
                    "code imports a package that nothing in the repository declares."
                ),
            ),
            _capability(
                SourceType.SYNAPSE,
                SourceRole.SECONDARY,
                provides=(InformationClass.RUNTIME,),
                available=(
                    "GET /notebooks: the published definition, for drift detection",
                    "notebook run history",
                ),
                unavailable=("anything not yet published",),
            ),
            _capability(
                SourceType.SQL,
                SourceRole.UNAVAILABLE,
                notes=(
                    "Needed only to resolve tables a notebook reads through the "
                    "synapsesql connector — discovery of those tables, not of the "
                    "notebook."
                ),
            ),
        ),
    ),
    # ---------------------------------------------------------------- 9
    ArtifactSourceStrategy(
        artifact=P0Artifact.SPARK_JOB_DEFINITION,
        summary=(
            "The definition is in Git, but the code it runs is a JAR or script "
            "in storage that Git usually does not contain."
        ),
        capabilities=(
            _capability(
                SourceType.REPOSITORY,
                SourceRole.PRIMARY,
                provides=(InformationClass.DEFINITION,),
                available=(
                    "sparkJobDefinition/*.json: target pool, main file path, "
                    "main class, arguments",
                    "driver and executor sizing, referenced jars and files",
                    "language and folder",
                ),
                unavailable=(
                    "the contents of the main file — it is a binary or script in "
                    "storage, not a repository artifact",
                    "pool-installed libraries",
                    "published state and run history",
                ),
                notes=(
                    "The sample snapshot contains no sparkJobDefinition/ folder, so "
                    "this path is unexercised against real data."
                ),
            ),
            _capability(
                SourceType.AZURE,
                SourceRole.SECONDARY,
                provides=(InformationClass.INFRASTRUCTURE,),
                available=(
                    "the target Spark pool: size, version, scaling",
                    "existence of the main file in ADLS, and its storage account",
                ),
                unavailable=("the job definition itself",),
                notes=(
                    "Reading the referenced JAR or .py from storage is a separate "
                    "decision; the file is code, not metadata."
                ),
            ),
            _capability(
                SourceType.SYNAPSE,
                SourceRole.SECONDARY,
                provides=(InformationClass.RUNTIME,),
                available=(
                    "GET /sparkJobDefinitions: the published definition",
                    "Spark batch job run history",
                ),
                unavailable=("anything not yet published",),
            ),
            _capability(
                SourceType.SQL,
                SourceRole.UNAVAILABLE,
                notes="A Spark job definition is not a database object.",
            ),
        ),
    ),
)

#: The strategy, keyed by artifact. Ordered as the P0 list is ordered.
P0_SOURCE_STRATEGY: Dict[P0Artifact, ArtifactSourceStrategy] = {
    strategy.artifact: strategy for strategy in _STRATEGIES
}


def strategy_for(artifact: P0Artifact) -> ArtifactSourceStrategy:
    """The source strategy for one P0 artifact."""
    return P0_SOURCE_STRATEGY[artifact]


def artifacts_with_primary(source: SourceType) -> Tuple[P0Artifact, ...]:
    """Every P0 artifact whose authoritative definition lives in ``source``."""
    return tuple(
        artifact
        for artifact, strategy in P0_SOURCE_STRATEGY.items()
        if strategy.primary_source is source
    )


def required_endpoints() -> Tuple[SourceEndpoint, ...]:
    """Every connection a complete P0 discovery run would need."""
    needed = {
        source
        for strategy in P0_SOURCE_STRATEGY.values()
        for source in strategy.required_sources
    }
    return tuple(
        SOURCE_ENDPOINTS[source]
        for source in (
            SourceType.REPOSITORY,
            SourceType.SYNAPSE,
            SourceType.SQL,
            SourceType.AZURE,
        )
        if source in needed
    )


def coverage_matrix() -> Dict[str, Dict[str, str]]:
    """Artifact -> source -> role, for reporting and for the docs table."""
    return {
        artifact.value: {
            source.value: strategy.role_of(source).value
            for source in (
                SourceType.REPOSITORY,
                SourceType.SYNAPSE,
                SourceType.SQL,
                SourceType.AZURE,
            )
        }
        for artifact, strategy in P0_SOURCE_STRATEGY.items()
    }


def to_dict() -> dict:
    """The whole strategy, serializable, for inclusion in a discovery manifest."""
    return {
        "artifacts": [s.to_dict() for s in _STRATEGIES],
        "endpoints": [e.to_dict() for e in SOURCE_ENDPOINTS.values()],
        "security": {
            "never_retrieve": list(NEVER_RETRIEVE),
            "safe_to_record": list(SAFE_TO_RECORD),
        },
    }
