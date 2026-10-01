"""LinkedServiceExtractor: what a Synapse linked service connects to, and how.

Answers where the connection goes and how it authenticates, and stops there.
Whether the identity behind it actually has access is an Azure question the
repository cannot answer; whether it maps onto Fabric is Assessment's.

Structure mirrors the dataset extractor so the two read alike:

* ``synapse_json.scan`` finds ``*Reference`` objects and expressions.
* ``secret_scanning.scan_secrets`` finds secret references — shared, because
  a fix to security-critical detection must reach every extractor.
* ``_LINKED_SERVICE_HANDLERS`` holds one small function per family.

**Secret discipline.** No value of a secret-bearing property ever enters the
model. A connection string is decomposed into its non-secret keywords and the
raw string is discarded, because in some other repository that same field
carries a password.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.errors import MalformedArtifactError
from discovery_agent.extractors.base import ExtractionContext, Extractor
from discovery_agent.extractors.common_models import (
    AuthenticationMetadata,
    AuthenticationType,
    ConfigEntry,
    SecretKind,
    SecretReference,
    SynapseExpression,
    ValueDeclaration,
)
from discovery_agent.extractors.linked_service_models import (
    LinkedServiceCategory,
    LinkedServiceConnection,
    LinkedServiceDefinition,
    LinkedServiceResource,
    ResourceKind,
)
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionResult,
    IssueCode,
    SourceType,
)
from discovery_agent.extractors.secret_scanning import is_secret_property, scan_secrets
from discovery_agent.extractors.synapse_json import (
    classify_reference,
    render_scalar,
    scan,
)
from discovery_agent.models import AssetType, Evidence, asset_id

EXTRACTOR_NAME = "linked_service"
EXTRACTOR_VERSION = "1.0.0"
ROOT_PATH = "properties"
TYPE_PATH = "properties.typeProperties"

# Linked service properties this extractor understands. Anything else earns a
# warning rather than disappearing.
KNOWN_PROPERTIES = frozenset(
    {
        "type", "typeProperties", "parameters", "annotations", "description",
        "connectVia", "version",
    }
)

# Connection-string keywords that carry a credential. Their values are dropped
# before anything is recorded; only the keyword's presence is noted.
_SECRET_KEYWORDS = frozenset(
    {
        "password", "pwd", "accountkey", "account key", "sharedaccesssignature",
        "sig", "secret", "clientsecret", "client secret", "apikey", "api key",
        "accesskey", "access key", "usersecret", "credential",
    }
)

# Keywords mapped onto typed connection fields.
_SERVER_KEYWORDS = frozenset({"data source", "server", "addr", "address", "host"})
_DATABASE_KEYWORDS = frozenset({"initial catalog", "database"})
_USER_KEYWORDS = frozenset({"user id", "uid", "user", "username"})

# ``@{...}`` interpolation embedded mid-string. The shared scanner only
# recognizes a string that *starts* with "@", which a connection string never
# does, so its parameterization would otherwise be invisible.
_EMBEDDED_EXPRESSION = re.compile(r"@\{[^{}]*\}")

_URI_SCHEME = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://", re.IGNORECASE)


@dataclass(frozen=True)
class LinkedServiceSpecifics:
    """What a per-type handler contributes."""

    connection: Optional[LinkedServiceConnection] = None
    authentication: Tuple[AuthenticationMetadata, ...] = ()
    resources: Tuple[LinkedServiceResource, ...] = ()
    settings: Tuple[ConfigEntry, ...] = ()
    expressions: Tuple[SynapseExpression, ...] = ()
    secrets: Tuple[SecretReference, ...] = ()
    #: Locations this handler inspected itself. The generic name-based
    #: scanner is conservative; where a handler has actually parsed a
    #: property it is authoritative and its finding replaces the generic one.
    handled_secret_locations: Tuple[str, ...] = ()


def _value_of(node: Any) -> Optional[str]:
    """A property's value as a string, unwrapping an expression object."""
    if isinstance(node, dict):
        inner = node.get("value")
        return inner if isinstance(inner, str) else None
    return render_scalar(node)


def _is_dynamic(value: Optional[str]) -> bool:
    return bool(value) and ("@" in value)


def _settings(properties: dict, *keys: str) -> Tuple[ConfigEntry, ...]:
    """Curated scalar settings, skipping absent ones and anything secret."""
    collected: List[ConfigEntry] = []
    for key in keys:
        if is_secret_property(key):  # defence in depth: never curate a secret
            continue
        node = properties.get(key)
        if node is None or isinstance(node, (dict, list)):
            continue
        rendered = render_scalar(node)
        if rendered is not None:
            collected.append(ConfigEntry(key, rendered))
    return tuple(collected)


def _embedded_expressions(text: str, location: str) -> Tuple[SynapseExpression, ...]:
    """Interpolations written inside a longer string, preserved verbatim."""
    from discovery_agent.extractors.synapse_json import ExpressionForm

    return tuple(
        SynapseExpression(match.group(0), location, ExpressionForm.INLINE)
        for match in _EMBEDDED_EXPRESSION.finditer(text)
    )


def parse_connection_string(
    value: str, location: str
) -> Tuple[LinkedServiceConnection, Tuple[SynapseExpression, ...], Tuple[str, ...]]:
    """Decompose a connection string, dropping every credential keyword.

    Returns the typed connection, any embedded expressions, and the names of
    the secret keywords that were present — names only, never values. The raw
    string is discarded and never reaches the model.
    """
    server = database = user_id = None
    settings: List[ConfigEntry] = []
    expressions: List[SynapseExpression] = []
    secret_keywords: List[str] = []

    for part in value.split(";"):
        if "=" not in part:
            continue
        keyword, _, keyword_value = part.partition("=")
        keyword = keyword.strip()
        keyword_value = keyword_value.strip()
        if not keyword:
            continue

        normalized = keyword.lower()
        if normalized in _SECRET_KEYWORDS or is_secret_property(normalized):
            # Present, but the value never leaves this function.
            secret_keywords.append(keyword)
            continue

        expressions.extend(
            _embedded_expressions(keyword_value, f"{location}[{keyword}]")
        )

        if normalized in _SERVER_KEYWORDS:
            server = keyword_value
        elif normalized in _DATABASE_KEYWORDS:
            database = keyword_value
        elif normalized in _USER_KEYWORDS:
            user_id = keyword_value
        elif keyword_value:
            settings.append(ConfigEntry(keyword, keyword_value))

    connection = LinkedServiceConnection(
        server=server,
        database=database,
        user_id=user_id,
        settings=tuple(settings),
    )
    return connection, tuple(expressions), tuple(secret_keywords)


# --- type handlers -----------------------------------------------------------
# One function per family. Parameters, references, expressions and secrets are
# handled once for every linked service, whatever its type.


def _http_server(type_properties: dict, category: LinkedServiceCategory):
    url = _value_of(type_properties.get("url"))
    declared = type_properties.get("authenticationType")
    authentication = ()
    if isinstance(declared, str) and declared:
        authentication = (
            AuthenticationMetadata(
                authentication_type=_AUTH_NAMES.get(
                    declared.lower(), AuthenticationType.UNKNOWN
                ),
                location=f"{TYPE_PATH}.authenticationType",
                target=url,
            ),
        )
    resources = (
        (
            LinkedServiceResource(
                kind=ResourceKind.ENDPOINT,
                value=url,
                location=f"{TYPE_PATH}.url",
                is_dynamic=_is_dynamic(url),
            ),
        )
        if url
        else ()
    )
    return LinkedServiceSpecifics(
        connection=LinkedServiceConnection(category=category, endpoint=url),
        authentication=authentication,
        resources=resources,
        settings=_settings(
            type_properties, "enableServerCertificateValidation", "authHeaders"
        ),
    )


def _key_vault(type_properties: dict, category: LinkedServiceCategory):
    base_url = _value_of(type_properties.get("baseUrl"))
    resources = (
        (
            LinkedServiceResource(
                kind=ResourceKind.KEY_VAULT,
                value=base_url,
                location=f"{TYPE_PATH}.baseUrl",
                is_dynamic=_is_dynamic(base_url),
            ),
        )
        if base_url
        else ()
    )
    # A vault linked service with no credential property authenticates as the
    # workspace identity; that is a declared default, not an inference.
    return LinkedServiceSpecifics(
        connection=LinkedServiceConnection(category=category, endpoint=base_url),
        authentication=(
            AuthenticationMetadata(
                authentication_type=AuthenticationType.MANAGED_IDENTITY,
                location=TYPE_PATH,
                target=base_url,
            ),
        ),
        resources=resources,
    )


def _storage(type_properties: dict, category: LinkedServiceCategory):
    url = _value_of(type_properties.get("url")) or _value_of(
        type_properties.get("serviceEndpoint")
    )
    authentication = _storage_authentication(type_properties, url)
    resources = (
        (
            LinkedServiceResource(
                kind=ResourceKind.STORAGE_ACCOUNT,
                value=url,
                location=f"{TYPE_PATH}.url",
                is_dynamic=_is_dynamic(url),
            ),
        )
        if url
        else ()
    )
    return LinkedServiceSpecifics(
        connection=LinkedServiceConnection(
            category=category,
            endpoint=url,
            tenant_id=_value_of(type_properties.get("tenant")),
        ),
        authentication=authentication,
        resources=resources,
        settings=_settings(type_properties, "accountKind", "azureCloudType"),
    )


def _storage_authentication(type_properties: dict, target: Optional[str]):
    """Which credential mechanism a storage linked service declares.

    Read from which property is *present*, never from its value.
    """
    if "accountKey" in type_properties:
        return (
            AuthenticationMetadata(
                authentication_type=AuthenticationType.ACCOUNT_KEY,
                location=f"{TYPE_PATH}.accountKey",
                target=target,
            ),
        )
    if "sasUri" in type_properties or "sasToken" in type_properties:
        return (
            AuthenticationMetadata(
                authentication_type=AuthenticationType.SAS,
                location=f"{TYPE_PATH}.sasToken"
                if "sasToken" in type_properties
                else f"{TYPE_PATH}.sasUri",
                target=target,
            ),
        )
    if "servicePrincipalId" in type_properties:
        return (
            AuthenticationMetadata(
                authentication_type=AuthenticationType.SERVICE_PRINCIPAL,
                location=f"{TYPE_PATH}.servicePrincipalId",
                identity_name=_value_of(type_properties.get("servicePrincipalId")),
                target=target,
            ),
        )
    return (
        AuthenticationMetadata(
            authentication_type=AuthenticationType.MANAGED_IDENTITY,
            location=TYPE_PATH,
            target=target,
        ),
    )


def _sql(type_properties: dict, category: LinkedServiceCategory):
    raw = type_properties.get("connectionString")
    connection = LinkedServiceConnection(category=category)
    expressions: Tuple[SynapseExpression, ...] = ()
    secret_keywords: Tuple[str, ...] = ()

    text = _value_of(raw)
    if text:
        parsed, expressions, secret_keywords = parse_connection_string(
            text, f"{TYPE_PATH}.connectionString"
        )
        connection = LinkedServiceConnection(
            category=category,
            endpoint=parsed.server,
            server=parsed.server,
            database=parsed.database,
            user_id=parsed.user_id,
            settings=parsed.settings,
        )

    authentication = _sql_authentication(type_properties, connection, secret_keywords)
    resources = (
        (
            LinkedServiceResource(
                kind=ResourceKind.SQL_SERVER,
                value=connection.server,
                location=f"{TYPE_PATH}.connectionString[Data Source]",
                is_dynamic=_is_dynamic(connection.server),
            ),
        )
        if connection.server
        else ()
    )
    connection_string_location = f"{TYPE_PATH}.connectionString"
    handled = (connection_string_location,) if text else ()
    # Only a keyword that actually carries a credential becomes a finding. A
    # connection string whose password is externalized to Key Vault contains
    # no secret, and reporting one would send Assessment chasing a phantom.
    secrets = tuple(
        SecretReference(
            kind=SecretKind.INLINE_LITERAL,
            location=f"{connection_string_location}[{keyword}]",
            property_name=keyword,
        )
        for keyword in secret_keywords
    )
    return LinkedServiceSpecifics(
        connection=connection,
        authentication=authentication,
        resources=resources,
        expressions=expressions,
        secrets=secrets,
        handled_secret_locations=handled,
    )


def _sql_authentication(
    type_properties: dict,
    connection: LinkedServiceConnection,
    secret_keywords: Tuple[str, ...],
):
    if "servicePrincipalId" in type_properties:
        return (
            AuthenticationMetadata(
                authentication_type=AuthenticationType.SERVICE_PRINCIPAL,
                location=f"{TYPE_PATH}.servicePrincipalId",
                identity_name=_value_of(type_properties.get("servicePrincipalId")),
                target=connection.server,
            ),
        )
    if "password" in type_properties or secret_keywords or connection.user_id:
        return (
            AuthenticationMetadata(
                authentication_type=AuthenticationType.SQL_LOGIN,
                location=f"{TYPE_PATH}.password"
                if "password" in type_properties
                else f"{TYPE_PATH}.connectionString",
                identity_name=connection.user_id,
                target=connection.server,
            ),
        )
    return (
        AuthenticationMetadata(
            authentication_type=AuthenticationType.MANAGED_IDENTITY,
            location=TYPE_PATH,
            target=connection.server,
        ),
    )


def _power_bi(type_properties: dict, category: LinkedServiceCategory):
    workspace_id = _value_of(type_properties.get("workspaceID"))
    tenant_id = _value_of(type_properties.get("tenantID"))
    resources = (
        (
            LinkedServiceResource(
                kind=ResourceKind.BI_WORKSPACE,
                value=workspace_id,
                location=f"{TYPE_PATH}.workspaceID",
                is_dynamic=_is_dynamic(workspace_id),
            ),
        )
        if workspace_id
        else ()
    )
    return LinkedServiceSpecifics(
        connection=LinkedServiceConnection(
            category=category, workspace_id=workspace_id, tenant_id=tenant_id
        ),
        authentication=(
            AuthenticationMetadata(
                authentication_type=AuthenticationType.MANAGED_IDENTITY,
                location=TYPE_PATH,
                target=workspace_id,
            ),
        ),
        resources=resources,
    )


_AUTH_NAMES: Dict[str, AuthenticationType] = {
    "anonymous": AuthenticationType.ANONYMOUS,
    "basic": AuthenticationType.SQL_LOGIN,
    "managedidentity": AuthenticationType.MANAGED_IDENTITY,
    "msi": AuthenticationType.MANAGED_IDENTITY,
    "systemassignedmanagedidentity": AuthenticationType.MANAGED_IDENTITY,
    "serviceprincipal": AuthenticationType.SERVICE_PRINCIPAL,
    "sql": AuthenticationType.SQL_LOGIN,
    "clientcertificate": AuthenticationType.SERVICE_PRINCIPAL,
}

#: Type -> (category, handler). The five types present in the repository, plus
#: aliases registered only where the structure is identical.
_LINKED_SERVICE_HANDLERS: Dict[
    str, Tuple[LinkedServiceCategory, Callable[[dict, LinkedServiceCategory], LinkedServiceSpecifics]]
] = {
    # Present in the repository.
    "HttpServer": (LinkedServiceCategory.HTTP, _http_server),
    "AzureKeyVault": (LinkedServiceCategory.KEY_VAULT, _key_vault),
    "AzureBlobFS": (LinkedServiceCategory.DATA_LAKE, _storage),
    "AzureSqlDW": (LinkedServiceCategory.SQL_DATA_WAREHOUSE, _sql),
    "PowerBIWorkspace": (LinkedServiceCategory.BUSINESS_INTELLIGENCE, _power_bi),
    # Same structure, registered as aliases rather than new logic.
    "Web": (LinkedServiceCategory.HTTP, _http_server),
    "RestService": (LinkedServiceCategory.REST, _http_server),
    "AzureDataLakeStore": (LinkedServiceCategory.DATA_LAKE, _storage),
    "AzureBlobStorage": (LinkedServiceCategory.BLOB_STORAGE, _storage),
    "AzureFileStorage": (LinkedServiceCategory.BLOB_STORAGE, _storage),
    "AzureSynapseAnalytics": (LinkedServiceCategory.SQL_DATA_WAREHOUSE, _sql),
    "AzureSqlDatabase": (LinkedServiceCategory.SQL_DATABASE, _sql),
    "SqlServer": (LinkedServiceCategory.SQL_DATABASE, _sql),
    "AzureSqlMI": (LinkedServiceCategory.SQL_DATABASE, _sql),
}


class LinkedServiceExtractor(Extractor[LinkedServiceDefinition]):
    """Extracts the connection and authentication shape of a linked service."""

    name = EXTRACTOR_NAME
    version = EXTRACTOR_VERSION
    supported_types = (AssetType.LINKED_SERVICE,)
    # Git and the live workspace serve the identical {name, properties}
    # document, so one extractor reads both. See discovery_agent.synapse.source.
    supported_sources = (SourceType.REPOSITORY, SourceType.SYNAPSE)

    def extract(
        self, artifact: DetectedArtifact, context: ExtractionContext
    ) -> ExtractionResult[LinkedServiceDefinition]:
        try:
            document = context.source.read_json(artifact)
        except MalformedArtifactError as exc:
            return self._malformed(artifact, context, exc.reason, artifact.source_path)

        if not isinstance(document, dict):
            return self._malformed(
                artifact, context, "linked service json root is not an object", ""
            )

        properties = document.get("properties")
        if not isinstance(properties, dict):
            return self._malformed(
                artifact, context, "linked service has no properties object", ROOT_PATH
            )

        service_type = properties.get("type")
        if not isinstance(service_type, str) or not service_type:
            return self._malformed(
                artifact,
                context,
                "linked service properties has no type",
                f"{ROOT_PATH}.type",
            )

        warnings: List[ExtractionIssue] = []
        for unknown in sorted(
            key for key in properties if key not in KNOWN_PROPERTIES
        ):
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"linked service property {unknown!r} is not modelled; it was "
                    f"not extracted",
                    f"{ROOT_PATH}.{unknown}",
                )
            )

        name = (
            document["name"]
            if isinstance(document.get("name"), str) and document["name"]
            else artifact.artifact_name
        )
        service_id = asset_id(AssetType.LINKED_SERVICE, name)

        type_properties = properties.get("typeProperties")
        type_properties = type_properties if isinstance(type_properties, dict) else {}

        registered = _LINKED_SERVICE_HANDLERS.get(service_type)
        recognized = registered is not None
        if recognized:
            category, handler = registered
            specifics = handler(type_properties, category)
        else:
            category = LinkedServiceCategory.UNKNOWN
            specifics = self._unrecognized(type_properties)
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"linked service type {service_type!r} has no type handler; "
                    f"generic configuration, references, expressions and secret "
                    f"references were still extracted",
                    f"{ROOT_PATH}.type",
                )
            )

        references, scanned_expressions = self._observe(
            properties, artifact, service_id, warnings
        )
        parameters, parameter_secrets = self._parameters(properties.get("parameters"))

        definition = LinkedServiceDefinition(
            name=name,
            type=service_type,
            category=category,
            description=self._optional_str(properties.get("description")),
            annotations=self._annotations(properties.get("annotations")),
            parameters=parameters,
            connection=None
            if specifics.connection is None or specifics.connection.is_empty
            else specifics.connection,
            authentication=specifics.authentication,
            resources=specifics.resources,
            settings=specifics.settings,
            references=references,
            connect_via=self._connect_via(references),
            expressions=self._merge_expressions(
                scanned_expressions, specifics.expressions
            ),
            secrets=self._secrets(
                properties, specifics, parameter_secrets
            ),
            recognized=recognized,
        )

        return self.success(
            artifact,
            context,
            definition,
            references=definition.references,
            warnings=tuple(warnings),
        )

    # -- structural observation --------------------------------------------

    def _observe(
        self,
        properties: dict,
        artifact: DetectedArtifact,
        service_id: str,
        warnings: List[ExtractionIssue],
    ) -> Tuple[Tuple[ArtifactReference, ...], Tuple[SynapseExpression, ...]]:
        """References and expressions, via the shared structural scanner."""
        found_references, found_expressions = scan(properties, ROOT_PATH)

        references: List[ArtifactReference] = []
        for found in found_references:
            target_type, kind, known = classify_reference(found.reference_type)
            if not known:
                warnings.append(
                    ExtractionIssue(
                        IssueCode.UNSUPPORTED_CONSTRUCT,
                        f"reference type {found.reference_type!r} is not modelled; "
                        f"recorded with an unknown target type",
                        found.location,
                    )
                )
            references.append(
                ArtifactReference(
                    source_artifact_id=service_id,
                    source_artifact_type=AssetType.LINKED_SERVICE,
                    kind=kind,
                    target_type=target_type,
                    target_name=found.reference_name,
                    location=found.location,
                    evidence=Evidence(artifact.source_path, None, EXTRACTOR_NAME),
                )
            )

        expressions = tuple(
            SynapseExpression(f.expression, f.location, f.form)
            for f in found_expressions
        )
        return (
            tuple(sorted(references, key=lambda r: (r.location, r.target_name))),
            expressions,
        )

    @staticmethod
    def _merge_expressions(
        scanned: Tuple[SynapseExpression, ...], extra: Tuple[SynapseExpression, ...]
    ) -> Tuple[SynapseExpression, ...]:
        """Scanner findings plus interpolations the scanner cannot see.

        The shared scanner only recognizes a string that *starts* with "@"; a
        connection string never does, so its embedded ``@{...}`` parameters
        are contributed by the connection-string parser instead.
        """
        merged = list(scanned)
        seen = {(e.expression, e.location) for e in scanned}
        for expression in extra:
            if (expression.expression, expression.location) not in seen:
                merged.append(expression)
                seen.add((expression.expression, expression.location))
        return tuple(sorted(merged, key=lambda e: (e.location, e.expression)))

    @staticmethod
    def _connect_via(
        references: Tuple[ArtifactReference, ...]
    ) -> Optional[ArtifactReference]:
        """The integration runtime, promoted out of the reference list.

        Still present in ``references`` — a convenience view, not a second
        source of truth.
        """
        return next(
            (
                reference
                for reference in references
                if reference.location == f"{ROOT_PATH}.connectVia"
                and reference.target_type is AssetType.INTEGRATION_RUNTIME
            ),
            None,
        )

    @staticmethod
    def _secrets(
        properties: dict,
        specifics: LinkedServiceSpecifics,
        parameter_secrets: Tuple[SecretReference, ...],
    ) -> Tuple[SecretReference, ...]:
        """Generic secret findings, refined by whatever the handler parsed.

        The shared scanner flags a property by name, which is the right
        default. Where a handler has actually read the property it knows
        better, so its finding replaces the generic one at that location.
        """
        handled = set(specifics.handled_secret_locations)
        generic = tuple(
            secret
            for secret in scan_secrets(properties, ROOT_PATH)
            if secret.location not in handled
        )
        return tuple(
            sorted(
                generic + specifics.secrets + parameter_secrets,
                key=lambda s: (s.location, s.kind.value),
            )
        )

    # -- parameters and metadata -------------------------------------------

    @staticmethod
    def _parameters(
        declared: Any,
    ) -> Tuple[Tuple[ValueDeclaration, ...], Tuple[SecretReference, ...]]:
        """Parameters, sorted by name, with secret-named defaults dropped.

        A parameter called ``password`` keeps its shape — name, type, that a
        default exists — but never its value.
        """
        if not isinstance(declared, dict):
            return (), ()
        parameters: List[ValueDeclaration] = []
        secrets: List[SecretReference] = []
        for key in sorted(declared.keys()):
            spec = declared[key]
            if not isinstance(spec, dict):
                parameters.append(ValueDeclaration(name=key))
                continue
            has_default = "defaultValue" in spec
            secret = is_secret_property(key)
            if secret and has_default:
                secrets.append(
                    SecretReference(
                        kind=SecretKind.INLINE_LITERAL,
                        location=f"{ROOT_PATH}.parameters.{key}.defaultValue",
                        property_name=key,
                    )
                )
            parameters.append(
                ValueDeclaration(
                    name=key,
                    type=spec.get("type") if isinstance(spec.get("type"), str) else None,
                    default_value=None
                    if secret
                    else (render_scalar(spec.get("defaultValue")) if has_default else None),
                    has_default=has_default,
                )
            )
        return tuple(parameters), tuple(secrets)

    @staticmethod
    def _optional_str(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _annotations(annotations: Any) -> Tuple[str, ...]:
        if not isinstance(annotations, list):
            return ()
        rendered = [render_scalar(a) for a in annotations]
        return tuple(sorted(r for r in rendered if r is not None))

    @staticmethod
    def _unrecognized(type_properties: dict) -> LinkedServiceSpecifics:
        """Preserve an unmodelled type's non-secret top-level scalars.

        Enough to see what was configured, without copying the document and
        without ever curating a secret-bearing property.
        """
        settings = tuple(
            ConfigEntry(key, rendered)
            for key in sorted(type_properties)
            if not isinstance(type_properties[key], (dict, list))
            and not is_secret_property(key)
            for rendered in [render_scalar(type_properties[key])]
            if rendered is not None
        )
        endpoint = None
        for key in ("url", "baseUrl", "serviceEndpoint", "endpoint", "host"):
            endpoint = _value_of(type_properties.get(key))
            if endpoint:
                break
        resources = ()
        if endpoint and _URI_SCHEME.match(endpoint):
            resources = (
                LinkedServiceResource(
                    kind=ResourceKind.ENDPOINT,
                    value=endpoint,
                    location=f"{TYPE_PATH}.{key}",
                    is_dynamic=_is_dynamic(endpoint),
                ),
            )
        return LinkedServiceSpecifics(
            connection=LinkedServiceConnection(
                category=LinkedServiceCategory.UNKNOWN, endpoint=endpoint
            ),
            settings=settings,
            resources=resources,
        )

    def _malformed(
        self,
        artifact: DetectedArtifact,
        context: ExtractionContext,
        reason: str,
        location: str,
    ) -> ExtractionResult[LinkedServiceDefinition]:
        """A malformed linked service fails; it never becomes an empty one."""
        return self.failure(
            artifact,
            context,
            errors=(
                ExtractionIssue(IssueCode.MALFORMED_ARTIFACT, reason, location or None),
            ),
        )
