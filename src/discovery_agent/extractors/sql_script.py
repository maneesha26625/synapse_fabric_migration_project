"""SQLScriptExtractor: what a Synapse SQL script contains and touches.

Answers what SQL the artifact holds, which pool it targets, and which
database objects it names. It never connects to a database, never executes
anything, and never claims that static analysis resolved every dependency.

The extractor's contract is anchored to the rule the artifact detector
already uses — ``properties.content.query`` plus ``properties.type ==
"SqlQuery"`` — so detection and extraction cannot disagree about what a SQL
script is. Everything beyond that (``currentConnection``, ``resultLimit``,
``metadata.language``) is read defensively: present means extracted, absent
means absent.

Structure mirrors the other extractors:

* ``synapse_json.scan`` finds ``*Reference`` objects and expressions.
* ``sql_scanning`` does the deterministic SQL text analysis.
* ``secret_scanning`` covers the artifact JSON; ``sql_scanning`` covers
  credential literals inside the SQL itself.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.errors import MalformedArtifactError
from discovery_agent.extractors.base import ExtractionContext, Extractor
from discovery_agent.extractors.common_models import (
    ConfigEntry,
    SecretReference,
    SynapseExpression,
    ValueDeclaration,
)
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionResult,
    IssueCode,
    SourceType,
)
from discovery_agent.extractors.secret_scanning import is_secret_property, scan_secrets
from discovery_agent.extractors.sql_findings import (
    dynamic_sql_warning,
    sql_object_references,
)
from discovery_agent.extractors.sql_models import (
    SqlFeature,
    SqlFeatureKind,
    SqlObjectReference,
)
from discovery_agent.extractors.sql_scanning import (
    scan_dynamic_sql,
    scan_features,
    scan_objects,
    scan_sql_secrets,
)
from discovery_agent.extractors.sql_script_models import (
    SQLScriptDefinition,
    SqlScriptConnection,
)
from discovery_agent.extractors.synapse_json import (
    classify_reference,
    render_scalar,
    scan,
)
from discovery_agent.models import AssetType, Evidence, asset_id

EXTRACTOR_NAME = "sql_script"
EXTRACTOR_VERSION = "1.0.0"
ROOT_PATH = "properties"
QUERY_PATH = "properties.content.query"

#: The artifact type Synapse writes for a SQL script. Anything else is
#: extracted generically with a warning rather than rejected.
EXPECTED_TYPE = "SqlQuery"

KNOWN_PROPERTIES = frozenset(
    {"content", "type", "folder", "description", "annotations", "parameters"}
)
KNOWN_CONTENT_KEYS = frozenset(
    {"query", "metadata", "currentConnection", "resultLimit"}
)


class SQLScriptExtractor(Extractor[SQLScriptDefinition]):
    """Extracts the SQL text and structure of a Synapse SQL script artifact."""

    name = EXTRACTOR_NAME
    version = EXTRACTOR_VERSION
    supported_types = (AssetType.SQL_SCRIPT,)
    # Git and the live workspace serve the identical {name, properties}
    # document, so one extractor reads both. See discovery_agent.synapse.source.
    supported_sources = (SourceType.REPOSITORY, SourceType.SYNAPSE)

    def extract(
        self, artifact: DetectedArtifact, context: ExtractionContext
    ) -> ExtractionResult[SQLScriptDefinition]:
        try:
            document = context.source.read_json(artifact)
        except MalformedArtifactError as exc:
            return self._malformed(artifact, context, exc.reason, artifact.source_path)

        if not isinstance(document, dict):
            return self._malformed(
                artifact, context, "sql script json root is not an object", ""
            )

        properties = document.get("properties")
        if not isinstance(properties, dict):
            return self._malformed(
                artifact, context, "sql script has no properties object", ROOT_PATH
            )

        content = properties.get("content")
        if not isinstance(content, dict):
            return self._malformed(
                artifact,
                context,
                "sql script has no content object",
                f"{ROOT_PATH}.content",
            )

        query = content.get("query")
        if not isinstance(query, str):
            # The detector identifies a SQL script by this very field, so its
            # absence means the artifact is not what it was classified as.
            return self._malformed(
                artifact,
                context,
                "sql script content has no query string",
                QUERY_PATH,
            )

        warnings: List[ExtractionIssue] = []
        self._warn_unknown(properties, KNOWN_PROPERTIES, ROOT_PATH, warnings)
        self._warn_unknown(
            content, KNOWN_CONTENT_KEYS, f"{ROOT_PATH}.content", warnings
        )

        script_type = properties.get("type")
        script_type = script_type if isinstance(script_type, str) and script_type else ""
        recognized = script_type == EXPECTED_TYPE
        if not recognized:
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"sql script type {script_type or 'missing'!r} is not "
                    f"{EXPECTED_TYPE!r}; the SQL text and structure were still "
                    f"extracted",
                    f"{ROOT_PATH}.type",
                )
            )

        name = (
            document["name"]
            if isinstance(document.get("name"), str) and document["name"]
            else artifact.artifact_name
        )
        script_id = asset_id(AssetType.SQL_SCRIPT, name)

        connection = self._connection(content)
        objects = scan_objects(query, QUERY_PATH)
        features = self._features(query, connection, objects)
        dynamic_sql = scan_dynamic_sql(query, QUERY_PATH)

        unresolved = dynamic_sql_warning(dynamic_sql)
        if unresolved is not None:
            warnings.append(unresolved)

        references, expressions = self._observe(
            properties, artifact, script_id, warnings
        )

        definition = SQLScriptDefinition(
            name=name,
            type=script_type,
            sql_text=query,  # verbatim: this is the artifact definition
            language=self._language(content),
            description=self._optional_str(properties.get("description")),
            folder=self._folder_name(properties.get("folder")),
            annotations=self._annotations(properties.get("annotations")),
            connection=connection,
            parameters=self._parameters(properties.get("parameters")),
            objects=objects,
            features=features,
            dynamic_sql=dynamic_sql,
            settings=self._settings(content),
            references=references + sql_object_references(
                objects,
                source_artifact_id=script_id,
                source_artifact_type=AssetType.SQL_SCRIPT,
                source_path=artifact.source_path,
                extractor=EXTRACTOR_NAME,
            ),
            expressions=expressions,
            secrets=self._secrets(properties, query),
            recognized=recognized,
        )

        return self.success(
            artifact,
            context,
            definition,
            references=definition.references,
            warnings=tuple(warnings),
        )

    # -- SQL-derived observations -----------------------------------------

    @staticmethod
    def _features(
        query: str,
        connection: Optional[SqlScriptConnection],
        objects: Tuple[SqlObjectReference, ...],
    ) -> Tuple[SqlFeature, ...]:
        """Keyword-detected features, plus cross-database usage.

        A three-part name whose database differs from the script's own
        connection is a cross-database reference, which matters for migration
        and is only knowable by combining the two.
        """
        features = list(scan_features(query, QUERY_PATH))

        current = (connection.database_name or "").lower() if connection else ""
        for obj in objects:
            if obj.database and obj.database.lower() != current:
                features.append(
                    SqlFeature(
                        kind=SqlFeatureKind.CROSS_DATABASE,
                        construct=f"cross-database reference to {obj.database}",
                        location=obj.location,
                        evidence=obj.qualified_name,
                    )
                )
        return tuple(
            sorted(features, key=lambda f: (f.location, f.kind.value, f.construct))
        )

    def _secrets(self, properties: dict, query: str) -> Tuple[SecretReference, ...]:
        """Secrets in the artifact JSON and credential literals inside the SQL.

        The SQL text itself is preserved verbatim — it is the definition. Only
        derived evidence is redacted.
        """
        found = scan_secrets(properties, ROOT_PATH) + scan_sql_secrets(
            query, QUERY_PATH
        )
        return tuple(sorted(found, key=lambda s: (s.location, s.kind.value)))

    # -- artifact metadata -------------------------------------------------

    @staticmethod
    def _connection(content: dict) -> Optional[SqlScriptConnection]:
        raw = content.get("currentConnection")
        raw = raw if isinstance(raw, dict) else {}

        def _text(key: str) -> Optional[str]:
            value = raw.get(key)
            return value if isinstance(value, str) and value else None

        connection = SqlScriptConnection(
            pool_name=_text("poolName"),
            database_name=_text("databaseName"),
            connection_type=_text("type"),
        )
        return None if connection.is_empty else connection

    @staticmethod
    def _language(content: dict) -> Optional[str]:
        metadata = content.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("language"), str):
            return metadata["language"]
        return None

    @staticmethod
    def _settings(content: dict) -> Tuple[ConfigEntry, ...]:
        """Curated scalars from the content block, excluding the query itself."""
        collected: List[ConfigEntry] = []
        for key in sorted(content):
            if key == "query" or is_secret_property(key):
                continue
            value = content[key]
            if isinstance(value, (dict, list)):
                continue
            rendered = render_scalar(value)
            if rendered is not None:
                collected.append(ConfigEntry(key, rendered))
        return tuple(collected)

    @staticmethod
    def _parameters(declared: Any) -> Tuple[ValueDeclaration, ...]:
        """Parameters, if the artifact declares any.

        The standard Synapse SQL script artifact does not, but the field is
        read defensively rather than assumed absent.
        """
        if not isinstance(declared, dict):
            return ()
        parameters: List[ValueDeclaration] = []
        for key in sorted(declared.keys()):
            spec = declared[key]
            if not isinstance(spec, dict):
                parameters.append(ValueDeclaration(name=key))
                continue
            has_default = "defaultValue" in spec
            secret = is_secret_property(key)
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
        return tuple(parameters)

    def _observe(
        self,
        properties: dict,
        artifact: DetectedArtifact,
        script_id: str,
        warnings: List[ExtractionIssue],
    ) -> Tuple[Tuple[ArtifactReference, ...], Tuple[SynapseExpression, ...]]:
        """Structurally declared references and expressions in the artifact JSON."""
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
                    source_artifact_id=script_id,
                    source_artifact_type=AssetType.SQL_SCRIPT,
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
    def _warn_unknown(
        container: dict,
        known: frozenset,
        path: str,
        warnings: List[ExtractionIssue],
    ) -> None:
        for unknown in sorted(key for key in container if key not in known):
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"property {unknown!r} is not modelled; it was not extracted",
                    f"{path}.{unknown}",
                )
            )

    @staticmethod
    def _optional_str(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _folder_name(folder: Any) -> Optional[str]:
        if isinstance(folder, dict) and isinstance(folder.get("name"), str):
            return folder["name"]
        return None

    @staticmethod
    def _annotations(annotations: Any) -> Tuple[str, ...]:
        if not isinstance(annotations, list):
            return ()
        rendered = [render_scalar(a) for a in annotations]
        return tuple(sorted(r for r in rendered if r is not None))

    def _malformed(
        self,
        artifact: DetectedArtifact,
        context: ExtractionContext,
        reason: str,
        location: str,
    ) -> ExtractionResult[SQLScriptDefinition]:
        """A malformed script fails; it never becomes an empty definition."""
        return self.failure(
            artifact,
            context,
            errors=(
                ExtractionIssue(IssueCode.MALFORMED_ARTIFACT, reason, location or None),
            ),
        )
