"""PipelineExtractor: what is inside a Synapse pipeline, and what it references.

Answers two questions and no others. Whether any of it maps onto Fabric is
Assessment's question, further downstream.

Responsibilities are kept apart deliberately:

* ``synapse_json.scan`` finds references and expressions structurally — one
  rule for the whole document, shared with future extractors.
* ``sql_scanning`` reads the SQL embedded in an activity, and
  ``sql_findings`` puts what it found onto the framework's reference and
  issue channels — the same two functions the SQL script extractor uses, so
  a table named in a pre-copy script is reported exactly like one named in a
  standalone script.
* ``_ACTIVITY_HANDLERS`` holds one small function per activity type, each
  picking out the settings that matter for that type.
* ``_CONTAINERS`` describes where each control-flow type keeps its children,
  and ``_parse_activity`` recurses through them.

Adding an activity type means adding one function and one dict entry. There
is no growing if/elif chain, and no giant method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.errors import MalformedArtifactError
from discovery_agent.extractors.base import ExtractionContext, Extractor
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionResult,
    IssueCode,
    ReferenceKind,
    SourceType,
)
from discovery_agent.extractors.common_models import (
    ConfigEntry,
    SynapseExpression,
    ValueDeclaration,
)
from discovery_agent.extractors.pipeline_models import (
    ActivityDependency,
    ActivityPolicy,
    ComputeRequest,
    DataMovement,
    EmbeddedScript,
    PipelineActivity,
    PipelineDefinition,
    ScriptLanguage,
    UserProperty,
)
from discovery_agent.extractors.sql_findings import (
    dynamic_sql_warning,
    sql_object_references,
)
from discovery_agent.extractors.sql_scanning import (
    scan_dynamic_sql,
    scan_features,
    scan_objects,
    scan_sql_secrets,
)
from discovery_agent.extractors.synapse_json import (
    JsonReference,
    classify_reference,
    render_scalar,
    scan,
)
from discovery_agent.models import AssetType, Evidence, asset_id

EXTRACTOR_NAME = "pipeline"
EXTRACTOR_VERSION = "1.0.0"
ROOT_PATH = "properties"


@dataclass(frozen=True)
class DeclaredResource:
    """A resource named by a field whose *name* declares what it is.

    Synapse states some dependencies without a ``*Reference`` wrapper —
    ``storedProcedureName``, a Web activity's ``url``. Those are still
    structural: the key declares the meaning, so reading them is not name
    guessing. A bare string in an arbitrary field never becomes one of these.
    """

    name: str
    kind: ReferenceKind
    target_type: Optional[AssetType]
    location: str


@dataclass(frozen=True)
class ActivitySpecifics:
    """What a per-type handler contributes to an activity."""

    settings: Tuple[ConfigEntry, ...] = ()
    scripts: Tuple[EmbeddedScript, ...] = ()
    data_movement: Optional[DataMovement] = None
    compute: Optional[ComputeRequest] = None
    resources: Tuple[DeclaredResource, ...] = ()


# --- small helpers -----------------------------------------------------------


def _dict_at(container: Any, *keys: str) -> Optional[dict]:
    """Follow a chain of dict keys, returning None if any step is missing."""
    current = container
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _type_of(container: Any, *keys: str) -> Optional[str]:
    node = _dict_at(container, *keys)
    value = node.get("type") if node else None
    return value if isinstance(value, str) else None


def _settings(
    properties: dict, base: str, *keys: str
) -> Tuple[ConfigEntry, ...]:
    """Curated scalar settings, by dotted key path, skipping absent ones."""
    collected: List[ConfigEntry] = []
    for key in keys:
        node: Any = properties
        for part in key.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if node is None or isinstance(node, (dict, list)):
            continue
        rendered = render_scalar(node)
        if rendered is not None:
            collected.append(ConfigEntry(key, rendered))
    return tuple(collected)


def _script_at(properties: Any, path: str, base: str) -> Tuple[EmbeddedScript, ...]:
    """Pull embedded SQL from a dotted path, whether plain or an expression.

    A query written as an expression object still contains the SQL text, and
    losing it because it was parameterized would hide the migration work.
    """
    node: Any = properties
    for part in path.split("."):
        if not isinstance(node, dict):
            return ()
        node = node.get(part)
    if isinstance(node, dict) and isinstance(node.get("value"), str):
        node = node["value"]
    if not isinstance(node, str) or not node.strip():
        return ()
    return (EmbeddedScript(ScriptLanguage.SQL, node, f"{base}.{path}"),)


def _analysed(script: EmbeddedScript) -> EmbeddedScript:
    """The same script, with the shared SQL scanner's observations attached.

    ``text`` and ``location`` pass through untouched: the scanner reads them,
    it does not rewrite them. Only SQL is scanned, so a future embedded
    language is carried unanalysed rather than run through a T-SQL matcher.

    Cross-database detection is deliberately absent. It needs the database a
    statement runs against, and a pipeline reaches its database indirectly
    through a dataset and a linked service, neither of which this stage
    resolves. The gap is better than a guess.
    """
    if script.language is not ScriptLanguage.SQL:
        return script
    return EmbeddedScript(
        language=script.language,
        text=script.text,
        location=script.location,
        objects=scan_objects(script.text, script.location),
        features=scan_features(script.text, script.location),
        dynamic_sql=scan_dynamic_sql(script.text, script.location),
        secrets=scan_sql_secrets(script.text, script.location),
    )


# --- activity-type handlers --------------------------------------------------
# Each takes an activity's typeProperties and its JSON path, and returns only
# what is specific to that type. Generic concerns (dependsOn, policy,
# references, expressions, children) are handled once, for every activity.


def _copy(properties: dict, base: str) -> ActivitySpecifics:
    movement = DataMovement(
        source_type=_type_of(properties, "source"),
        sink_type=_type_of(properties, "sink"),
        source_store_type=_type_of(properties, "source", "storeSettings"),
        sink_store_type=_type_of(properties, "sink", "storeSettings"),
        source_format_type=_type_of(properties, "source", "formatSettings"),
        sink_format_type=_type_of(properties, "sink", "formatSettings"),
        translator_type=_type_of(properties, "translator"),
        staging_enabled=properties.get("enableStaging")
        if isinstance(properties.get("enableStaging"), bool)
        else None,
    )
    return ActivitySpecifics(
        settings=_settings(
            properties,
            base,
            "sink.tableOption",
            "sink.allowPolyBase",
            "sink.writeBehavior",
            "source.partitionOption",
            "source.queryTimeout",
            "parallelCopies",
            "validateDataConsistency",
        ),
        scripts=_script_at(properties, "sink.preCopyScript", base)
        + _script_at(properties, "source.sqlReaderQuery", base)
        + _script_at(properties, "source.query", base),
        data_movement=movement,
    )


def _lookup(properties: dict, base: str) -> ActivitySpecifics:
    return ActivitySpecifics(
        settings=_settings(
            properties, base, "firstRowOnly", "source.partitionOption", "source.queryTimeout"
        ),
        scripts=_script_at(properties, "source.sqlReaderQuery", base)
        + _script_at(properties, "source.query", base),
        data_movement=DataMovement(source_type=_type_of(properties, "source")),
    )


def _execute_dataflow(properties: dict, base: str) -> ActivitySpecifics:
    compute = _dict_at(properties, "compute") or {}
    core_count = compute.get("coreCount")
    return ActivitySpecifics(
        settings=_settings(properties, base, "traceLevel", "continueOnError", "runConcurrently"),
        compute=ComputeRequest(
            compute_type=compute.get("computeType")
            if isinstance(compute.get("computeType"), str)
            else None,
            core_count=core_count if isinstance(core_count, int) else None,
        ),
    )


def _execute_pipeline(properties: dict, base: str) -> ActivitySpecifics:
    return ActivitySpecifics(
        settings=_settings(properties, base, "waitOnCompletion")
    )


def _notebook(properties: dict, base: str) -> ActivitySpecifics:
    return ActivitySpecifics(
        settings=_settings(
            properties, base, "numExecutors", "driverSize", "executorSize", "conf"
        )
    )


def _spark_job(properties: dict, base: str) -> ActivitySpecifics:
    return ActivitySpecifics(
        settings=_settings(properties, base, "file", "className", "numExecutors")
    )


def _stored_procedure(properties: dict, base: str) -> ActivitySpecifics:
    """The stored procedure name is declared by its field, not inferred."""
    name = properties.get("storedProcedureName")
    resources: Tuple[DeclaredResource, ...] = ()
    if isinstance(name, str) and name:
        resources = (
            DeclaredResource(
                name, ReferenceKind.SQL_OBJECT, None, f"{base}.storedProcedureName"
            ),
        )
    return ActivitySpecifics(resources=resources)


def _script(properties: dict, base: str) -> ActivitySpecifics:
    scripts: List[EmbeddedScript] = []
    statements = properties.get("scripts")
    if isinstance(statements, list):
        for index, statement in enumerate(statements):
            text = statement.get("text") if isinstance(statement, dict) else None
            if isinstance(text, dict) and isinstance(text.get("value"), str):
                text = text["value"]
            if isinstance(text, str) and text.strip():
                scripts.append(
                    EmbeddedScript(
                        ScriptLanguage.SQL, text, f"{base}.scripts[{index}].text"
                    )
                )
    return ActivitySpecifics(scripts=tuple(scripts))


def _web(properties: dict, base: str) -> ActivitySpecifics:
    """A Web activity's url is an external endpoint, never a Synapse artifact."""
    url = properties.get("url")
    resources: Tuple[DeclaredResource, ...] = ()
    if isinstance(url, str) and url:
        resources = (
            DeclaredResource(
                url, ReferenceKind.EXTERNAL_ENDPOINT, None, f"{base}.url"
            ),
        )
    return ActivitySpecifics(
        settings=_settings(properties, base, "method", "authentication.type"),
        resources=resources,
    )


def _for_each(properties: dict, base: str) -> ActivitySpecifics:
    return ActivitySpecifics(settings=_settings(properties, base, "isSequential", "batchCount"))


def _until(properties: dict, base: str) -> ActivitySpecifics:
    return ActivitySpecifics(settings=_settings(properties, base, "timeout"))


def _switch(properties: dict, base: str) -> ActivitySpecifics:
    return ActivitySpecifics()


def _variable(properties: dict, base: str) -> ActivitySpecifics:
    return ActivitySpecifics(settings=_settings(properties, base, "variableName"))


def _wait(properties: dict, base: str) -> ActivitySpecifics:
    return ActivitySpecifics(settings=_settings(properties, base, "waitTimeInSeconds"))


_ACTIVITY_HANDLERS: Dict[str, Callable[[dict, str], ActivitySpecifics]] = {
    "Copy": _copy,
    "Lookup": _lookup,
    "ExecuteDataFlow": _execute_dataflow,
    "ExecutePipeline": _execute_pipeline,
    "SynapseNotebook": _notebook,
    "DatabricksNotebook": _notebook,
    "SparkJob": _spark_job,
    "SqlPoolStoredProcedure": _stored_procedure,
    "SqlServerStoredProcedure": _stored_procedure,
    "Script": _script,
    "WebActivity": _web,
    "WebHook": _web,
    "ForEach": _for_each,
    "Until": _until,
    "IfCondition": lambda properties, base: ActivitySpecifics(),
    "Switch": _switch,
    "SetVariable": _variable,
    "AppendVariable": _variable,
    "Wait": _wait,
    "Filter": lambda properties, base: ActivitySpecifics(),
    "Validation": lambda properties, base: ActivitySpecifics(),
    "GetMetadata": lambda properties, base: ActivitySpecifics(),
    "Delete": lambda properties, base: ActivitySpecifics(),
    "Fail": lambda properties, base: ActivitySpecifics(),
}

# Where each control-flow type keeps its children: (typeProperties key, branch
# label). Switch is handled separately because its branches carry case values.
_CONTAINERS: Dict[str, Tuple[Tuple[str, Optional[str]], ...]] = {
    "ForEach": (("activities", None),),
    "Until": (("activities", None),),
    "IfCondition": (
        ("ifTrueActivities", "ifTrue"),
        ("ifFalseActivities", "ifFalse"),
    ),
}

# Activity types whose children this extractor understands.
CONTROL_FLOW_TYPES = tuple(sorted(set(_CONTAINERS) | {"Switch"}))


class PipelineExtractor(Extractor[PipelineDefinition]):
    """Extracts the contents and references of a Synapse pipeline artifact."""

    name = EXTRACTOR_NAME
    version = EXTRACTOR_VERSION
    supported_types = (AssetType.PIPELINE,)
    # Git and the live workspace serve the identical {name, properties}
    # document, so one extractor reads both. See discovery_agent.synapse.source.
    supported_sources = (SourceType.REPOSITORY, SourceType.SYNAPSE)

    def extract(
        self, artifact: DetectedArtifact, context: ExtractionContext
    ) -> ExtractionResult[PipelineDefinition]:
        try:
            document = context.source.read_json(artifact)
        except MalformedArtifactError as exc:
            return self._malformed(artifact, context, exc.reason, artifact.source_path)

        if not isinstance(document, dict):
            return self._malformed(
                artifact, context, "pipeline json root is not an object", ""
            )

        properties = document.get("properties")
        if not isinstance(properties, dict):
            return self._malformed(
                artifact, context, "pipeline has no properties object", ROOT_PATH
            )

        raw_activities = properties.get("activities")
        if not isinstance(raw_activities, list):
            # Absent is malformed; an empty list is a real, if unusual, pipeline.
            return self._malformed(
                artifact,
                context,
                "pipeline properties has no activities list",
                f"{ROOT_PATH}.activities",
            )

        warnings: List[ExtractionIssue] = []
        pipeline_name = (
            document["name"]
            if isinstance(document.get("name"), str) and document["name"]
            else artifact.artifact_name
        )
        pipeline_id = asset_id(AssetType.PIPELINE, pipeline_name)

        activities = self._parse_activities(
            raw_activities,
            base=f"{ROOT_PATH}.activities",
            parent=None,
            branch=None,
            source_path=artifact.source_path,
            pipeline_id=pipeline_id,
            warnings=warnings,
        )

        definition = PipelineDefinition(
            name=pipeline_name,
            description=self._optional_str(properties.get("description")),
            folder=self._folder_name(properties.get("folder")),
            concurrency=properties.get("concurrency")
            if isinstance(properties.get("concurrency"), int)
            else None,
            annotations=self._annotations(properties.get("annotations")),
            parameters=self._declarations(properties.get("parameters")),
            variables=self._declarations(properties.get("variables")),
            activities=activities,
        )

        return self.success(
            artifact,
            context,
            definition,
            references=definition.references,
            warnings=tuple(warnings),
        )

    # -- pipeline-level parsing -------------------------------------------

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

    @staticmethod
    def _declarations(declared: Any) -> Tuple[ValueDeclaration, ...]:
        """Parameters and variables, sorted by name — a JSON object has no order."""
        if not isinstance(declared, dict):
            return ()
        result: List[ValueDeclaration] = []
        for key in sorted(declared.keys()):
            spec = declared[key]
            if not isinstance(spec, dict):
                result.append(ValueDeclaration(name=key, type=None))
                continue
            has_default = "defaultValue" in spec
            result.append(
                ValueDeclaration(
                    name=key,
                    type=spec.get("type") if isinstance(spec.get("type"), str) else None,
                    default_value=render_scalar(spec.get("defaultValue"))
                    if has_default
                    else None,
                    has_default=has_default,
                )
            )
        return tuple(result)

    # -- activity parsing --------------------------------------------------

    def _parse_activities(
        self,
        raw_activities: list,
        base: str,
        parent: Optional[str],
        branch: Optional[str],
        source_path: str,
        pipeline_id: str,
        warnings: List[ExtractionIssue],
    ) -> Tuple[PipelineActivity, ...]:
        """Parse a list of activities, keeping source order (execution order)."""
        parsed: List[PipelineActivity] = []
        for index, raw in enumerate(raw_activities):
            path = f"{base}[{index}]"
            activity = self._parse_activity(
                raw, path, parent, branch, source_path, pipeline_id, warnings
            )
            if activity is not None:
                parsed.append(activity)
        return tuple(parsed)

    def _parse_activity(
        self,
        raw: Any,
        path: str,
        parent: Optional[str],
        branch: Optional[str],
        source_path: str,
        pipeline_id: str,
        warnings: List[ExtractionIssue],
    ) -> Optional[PipelineActivity]:
        if not isinstance(raw, dict):
            warnings.append(
                ExtractionIssue(
                    IssueCode.MALFORMED_ARTIFACT, "activity is not an object", path
                )
            )
            return None

        name = raw.get("name")
        activity_type = raw.get("type")
        if not isinstance(name, str) or not name:
            warnings.append(
                ExtractionIssue(
                    IssueCode.MALFORMED_ARTIFACT, "activity has no name", path
                )
            )
            return None
        if not isinstance(activity_type, str) or not activity_type:
            warnings.append(
                ExtractionIssue(
                    IssueCode.MALFORMED_ARTIFACT,
                    f"activity {name!r} has no type",
                    path,
                )
            )
            return None

        type_properties = raw.get("typeProperties")
        type_properties = type_properties if isinstance(type_properties, dict) else {}
        type_path = f"{path}.typeProperties"

        handler = _ACTIVITY_HANDLERS.get(activity_type)
        recognized = handler is not None
        if recognized:
            specifics = handler(type_properties, type_path)
        else:
            specifics = self._unrecognized(activity_type, name, type_properties, type_path)
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"activity {name!r} has type {activity_type!r}, which this "
                    f"extractor does not model; generic configuration preserved",
                    path,
                )
            )

        children, child_warnings = self._parse_children(
            raw, activity_type, name, type_path, source_path, pipeline_id, recognized
        )
        warnings.extend(child_warnings)

        scripts = tuple(_analysed(script) for script in specifics.scripts)
        unresolved = dynamic_sql_warning(
            tuple(site for script in scripts for site in script.dynamic_sql)
        )
        if unresolved is not None:
            warnings.append(unresolved)

        references, expressions = self._observe(
            raw, path, name, pipeline_id, source_path, warnings
        )
        references = references + tuple(
            self._reference_from_resource(resource, name, pipeline_id, source_path)
            for resource in specifics.resources
        )
        references = references + self._sql_references(
            scripts, pipeline_id, source_path
        )

        return PipelineActivity(
            name=name,
            type=activity_type,
            path=path,
            description=self._optional_str(raw.get("description")),
            parent=parent,
            branch=branch,
            depends_on=self._dependencies(raw.get("dependsOn")),
            policy=self._policy(raw.get("policy")),
            user_properties=self._user_properties(raw.get("userProperties")),
            settings=specifics.settings,
            scripts=scripts,
            data_movement=specifics.data_movement,
            compute=specifics.compute,
            references=tuple(sorted(references, key=lambda r: (r.location, r.target_name))),
            expressions=expressions,
            children=children,
            recognized=recognized,
        )

    def _parse_children(
        self,
        raw: dict,
        activity_type: str,
        name: str,
        type_path: str,
        source_path: str,
        pipeline_id: str,
        recognized: bool,
    ) -> Tuple[Tuple[PipelineActivity, ...], List[ExtractionIssue]]:
        """Recurse into a container activity, preserving the parent-child link."""
        type_properties = raw.get("typeProperties")
        if not isinstance(type_properties, dict):
            return (), []

        warnings: List[ExtractionIssue] = []
        children: List[PipelineActivity] = []

        if activity_type == "Switch":
            cases = type_properties.get("cases")
            if isinstance(cases, list):
                for index, case in enumerate(cases):
                    if not isinstance(case, dict):
                        continue
                    label = render_scalar(case.get("value"))
                    children.extend(
                        self._parse_activities(
                            case.get("activities")
                            if isinstance(case.get("activities"), list)
                            else [],
                            f"{type_path}.cases[{index}].activities",
                            name,
                            f"case={label}" if label is not None else f"case[{index}]",
                            source_path,
                            pipeline_id,
                            warnings,
                        )
                    )
            children.extend(
                self._parse_activities(
                    type_properties.get("defaultActivities")
                    if isinstance(type_properties.get("defaultActivities"), list)
                    else [],
                    f"{type_path}.defaultActivities",
                    name,
                    "default",
                    source_path,
                    pipeline_id,
                    warnings,
                )
            )
            return tuple(children), warnings

        branches = _CONTAINERS.get(activity_type)
        if branches is None:
            # An unmodeled type can still be a container. Synapse always names
            # such lists "activities" or "<something>Activities", so nesting is
            # preserved rather than silently flattened away.
            branches = tuple(
                (key, key if key != "activities" else None)
                for key in sorted(type_properties)
                if key == "activities" or key.endswith("Activities")
            )

        for key, label in branches:
            nested = type_properties.get(key)
            if not isinstance(nested, list):
                continue
            children.extend(
                self._parse_activities(
                    nested,
                    f"{type_path}.{key}",
                    name,
                    label,
                    source_path,
                    pipeline_id,
                    warnings,
                )
            )
        return tuple(children), warnings

    # -- generic per-activity concerns -------------------------------------

    @staticmethod
    def _sql_references(
        scripts: Tuple[EmbeddedScript, ...], pipeline_id: str, source_path: str
    ) -> Tuple[ArtifactReference, ...]:
        """SQL objects this activity's scripts name, on the reference channel.

        Deduplicated per activity rather than per script, so a Copy naming
        the same table in both its pre-copy script and its source query
        yields one reference; each script still records its own occurrence.

        Nothing is resolved and no target type is inferred — a table is not a
        repository artifact, and whether it exists is not knowable here.
        """
        return sql_object_references(
            tuple(obj for script in scripts for obj in script.objects),
            source_artifact_id=pipeline_id,
            source_artifact_type=AssetType.PIPELINE,
            source_path=source_path,
            extractor=EXTRACTOR_NAME,
        )

    def _observe(
        self,
        raw: dict,
        path: str,
        activity_name: str,
        pipeline_id: str,
        source_path: str,
        warnings: List[ExtractionIssue],
    ) -> Tuple[Tuple[ArtifactReference, ...], Tuple[SynapseExpression, ...]]:
        """Scan one activity for references and expressions, excluding children.

        Nested activities are scanned when they are parsed, so a container's
        own references stay its own and a child's stay the child's.
        """
        shallow = {
            key: value
            for key, value in raw.items()
            if key != "typeProperties"
        }
        type_properties = raw.get("typeProperties")
        if isinstance(type_properties, dict):
            shallow["typeProperties"] = {
                key: value
                for key, value in type_properties.items()
                if not self._is_child_container(key, value)
            }

        json_references, json_expressions = scan(shallow, path)

        references: List[ArtifactReference] = []
        for found in json_references:
            references.append(
                self._reference_from_json(
                    found, activity_name, pipeline_id, source_path, warnings
                )
            )

        expressions = tuple(
            SynapseExpression(found.expression, found.location, found.form)
            for found in json_expressions
        )
        return tuple(references), expressions

    @staticmethod
    def _is_child_container(key: str, value: Any) -> bool:
        """Whether a typeProperties key holds nested activities."""
        if key == "cases" and isinstance(value, list):
            return True
        return isinstance(value, list) and (
            key == "activities" or key.endswith("Activities")
        )

    def _reference_from_json(
        self,
        found: JsonReference,
        activity_name: str,
        pipeline_id: str,
        source_path: str,
        warnings: List[ExtractionIssue],
    ) -> ArtifactReference:
        target_type, kind, recognized = classify_reference(found.reference_type)
        if not recognized:
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"reference type {found.reference_type!r} is not modelled; "
                    f"recorded with an unknown target type",
                    found.location,
                )
            )
        return ArtifactReference(
            source_artifact_id=pipeline_id,
            source_artifact_type=AssetType.PIPELINE,
            kind=kind,
            target_type=target_type,
            target_name=found.reference_name,
            location=found.location,
            evidence=Evidence(source_path, None, f"{EXTRACTOR_NAME}:{activity_name}"),
        )

    @staticmethod
    def _reference_from_resource(
        resource: DeclaredResource,
        activity_name: str,
        pipeline_id: str,
        source_path: str,
    ) -> ArtifactReference:
        return ArtifactReference(
            source_artifact_id=pipeline_id,
            source_artifact_type=AssetType.PIPELINE,
            kind=resource.kind,
            target_type=resource.target_type,
            target_name=resource.name,
            location=resource.location,
            evidence=Evidence(source_path, None, f"{EXTRACTOR_NAME}:{activity_name}"),
        )

    @staticmethod
    def _unrecognized(
        activity_type: str, name: str, type_properties: dict, type_path: str
    ) -> ActivitySpecifics:
        """Preserve an unmodelled activity's top-level scalars, nothing more.

        Enough for a reviewer to see what was configured, without copying the
        raw document into the result.
        """
        settings = tuple(
            ConfigEntry(key, rendered)
            for key in sorted(type_properties)
            for rendered in [render_scalar(type_properties[key])]
            if rendered is not None
            and not isinstance(type_properties[key], (dict, list))
        )
        return ActivitySpecifics(settings=settings)

    @staticmethod
    def _dependencies(depends_on: Any) -> Tuple[ActivityDependency, ...]:
        if not isinstance(depends_on, list):
            return ()
        dependencies: List[ActivityDependency] = []
        for entry in depends_on:
            if not isinstance(entry, dict):
                continue
            upstream = entry.get("activity")
            if not isinstance(upstream, str) or not upstream:
                continue
            raw_conditions = entry.get("dependencyConditions")
            conditions = (
                tuple(sorted(c for c in raw_conditions if isinstance(c, str)))
                if isinstance(raw_conditions, list)
                else ()
            )
            dependencies.append(ActivityDependency(upstream, conditions))
        return tuple(dependencies)

    @staticmethod
    def _policy(policy: Any) -> Optional[ActivityPolicy]:
        if not isinstance(policy, dict):
            return None
        return ActivityPolicy(
            timeout=policy.get("timeout") if isinstance(policy.get("timeout"), str) else None,
            retry=policy.get("retry") if isinstance(policy.get("retry"), int) else None,
            retry_interval_seconds=policy.get("retryIntervalInSeconds")
            if isinstance(policy.get("retryIntervalInSeconds"), int)
            else None,
            secure_input=policy.get("secureInput")
            if isinstance(policy.get("secureInput"), bool)
            else None,
            secure_output=policy.get("secureOutput")
            if isinstance(policy.get("secureOutput"), bool)
            else None,
        )

    @staticmethod
    def _user_properties(user_properties: Any) -> Tuple[UserProperty, ...]:
        if not isinstance(user_properties, list):
            return ()
        properties: List[UserProperty] = []
        for entry in user_properties:
            if not isinstance(entry, dict):
                continue
            key = entry.get("name")
            rendered = render_scalar(entry.get("value"))
            if isinstance(key, str) and key and rendered is not None:
                properties.append(UserProperty(key, rendered))
        return tuple(sorted(properties, key=lambda p: p.name))

    def _malformed(
        self,
        artifact: DetectedArtifact,
        context: ExtractionContext,
        reason: str,
        location: str,
    ) -> ExtractionResult[PipelineDefinition]:
        """A malformed pipeline fails. It never comes back as activities = []."""
        return self.failure(
            artifact,
            context,
            errors=(
                ExtractionIssue(IssueCode.MALFORMED_ARTIFACT, reason, location or None),
            ),
        )
