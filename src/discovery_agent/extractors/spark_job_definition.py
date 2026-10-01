"""SparkJobDefinitionExtractor: what a Spark job definition runs, and on what.

Answers which application file the job invokes, which class or entry point,
which arguments and Spark configuration it carries, and which pool it asks
for. It never fetches the application file, never executes anything, and
never claims to know what the code does.

The extractor's contract is anchored to the rule the artifact detector
already uses -- ``properties.targetBigDataPool`` or ``properties.jobProperties``
-- so detection and extraction cannot disagree about what a Spark job
definition is. Everything beyond that is read defensively: present means
extracted, absent means absent.

Structure mirrors the other extractors:

* ``synapse_json.scan`` finds ``*Reference`` objects and expressions, so a
  Big Data pool reference is recognised by exactly the mechanism that
  recognises a pipeline's dataset reference.
* ``secret_scanning`` covers the artifact JSON.
* Storage URIs are recorded as pointers; nothing is dereferenced.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from discovery_agent.artifacts.models import DetectedArtifact
from discovery_agent.errors import MalformedArtifactError
from discovery_agent.extractors.base import ExtractionContext, Extractor
from discovery_agent.extractors.common_models import (
    ConfigEntry,
    SynapseExpression,
    ValueDeclaration,
)
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionResult,
    IssueCode,
    ReferenceKind,
    SourceType,
)
from discovery_agent.extractors.secret_scanning import is_secret_property, scan_secrets
from discovery_agent.extractors.spark_job_definition_models import (
    SparkArtifactKind,
    SparkJobArtifact,
    SparkJobDefinition,
    SparkJobProperties,
    SparkResourceRequest,
)
from discovery_agent.extractors.synapse_json import (
    classify_reference,
    render_scalar,
    scan,
)
from discovery_agent.models import AssetType, Evidence, asset_id

EXTRACTOR_NAME = "spark_job_definition"
EXTRACTOR_VERSION = "1.0.0"
ROOT_PATH = "properties"
JOB_PATH = "properties.jobProperties"

#: Properties this extractor models. Anything else is reported as an
#: unmodelled construct rather than silently dropped.
KNOWN_PROPERTIES = frozenset(
    {
        "targetBigDataPool",
        "targetSparkConfiguration",
        "requiredSparkVersion",
        "language",
        "jobProperties",
        "folder",
        "description",
        "annotations",
        "parameters",
    }
)

#: ``jobProperties`` keys this extractor models.
KNOWN_JOB_PROPERTIES = frozenset(
    {
        "name",
        "file",
        "className",
        "args",
        "conf",
        "jars",
        "pyFiles",
        "files",
        "archives",
        "driverMemory",
        "driverCores",
        "executorMemory",
        "executorCores",
        "numExecutors",
    }
)

#: Which ``jobProperties`` list holds which kind of dependency.
_ARTIFACT_LISTS = (
    ("jars", SparkArtifactKind.JAR),
    ("pyFiles", SparkArtifactKind.PYTHON_FILE),
    ("files", SparkArtifactKind.FILE),
    ("archives", SparkArtifactKind.ARCHIVE),
)

#: A URI scheme, when the value has one. A bare path has none, which is a
#: finding rather than a gap: it depends on a workspace default.
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://")

#: What a value is replaced by when it cannot be rendered safely. A key is a
#: fact worth keeping; the value behind it is not this model's to carry.
WITHHELD = "<not extracted; see secrets>"

#: The reference location Synapse writes the target pool at.
_POOL_LOCATION = f"{ROOT_PATH}.targetBigDataPool"
_SPARK_CONFIGURATION_LOCATION = f"{ROOT_PATH}.targetSparkConfiguration"


class SparkJobDefinitionExtractor(Extractor[SparkJobDefinition]):
    """Extracts the application, dependencies and compute of a Spark job."""

    name = EXTRACTOR_NAME
    version = EXTRACTOR_VERSION
    supported_types = (AssetType.SPARK_JOB_DEFINITION,)
    # Git and the live workspace serve the identical {name, properties}
    # document, so one extractor reads both. See discovery_agent.synapse.source.
    supported_sources = (SourceType.REPOSITORY, SourceType.SYNAPSE)

    def extract(
        self, artifact: DetectedArtifact, context: ExtractionContext
    ) -> ExtractionResult[SparkJobDefinition]:
        try:
            document = context.source.read_json(artifact)
        except MalformedArtifactError as exc:
            return self._malformed(artifact, context, exc.reason, artifact.source_path)

        if not isinstance(document, dict):
            return self._malformed(
                artifact, context, "spark job definition json root is not an object", ""
            )

        properties = document.get("properties")
        if not isinstance(properties, dict):
            return self._malformed(
                artifact,
                context,
                "spark job definition has no properties object",
                ROOT_PATH,
            )

        warnings: List[ExtractionIssue] = []
        for unknown in sorted(
            key for key in properties if key not in KNOWN_PROPERTIES
        ):
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"spark job definition property {unknown!r} is not "
                    f"modelled; it was not extracted",
                    f"{ROOT_PATH}.{unknown}",
                )
            )

        name = (
            document["name"]
            if isinstance(document.get("name"), str) and document["name"]
            else artifact.artifact_name
        )
        job_id = asset_id(AssetType.SPARK_JOB_DEFINITION, name)

        references, expressions = self._observe(properties, artifact, job_id, warnings)
        job = self._job_properties(properties, warnings)

        if not job.has_main_file:
            # A job definition with no application file cannot run, and a
            # migration that copied it would move something broken. Said
            # plainly rather than left as an absent field.
            warnings.append(
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    "no jobProperties.file is declared, so the application "
                    "this job runs could not be determined",
                    f"{JOB_PATH}.file",
                )
            )

        definition = SparkJobDefinition(
            name=name,
            language=self._optional_str(properties.get("language")),
            required_spark_version=self._optional_str(
                properties.get("requiredSparkVersion")
            ),
            description=self._optional_str(properties.get("description")),
            folder=self._folder_name(properties.get("folder")),
            job=job,
            compute=self._reference_at(references, _POOL_LOCATION),
            spark_configuration=self._reference_at(
                references, _SPARK_CONFIGURATION_LOCATION
            ),
            parameters=self._parameters(properties.get("parameters")),
            references=references,
            expressions=expressions,
            secrets=scan_secrets(properties, ROOT_PATH),
        )

        if definition.compute is None:
            warnings.append(
                ExtractionIssue(
                    IssueCode.MISSING_INFORMATION,
                    "no targetBigDataPool is declared, so the pool this job "
                    "runs on could not be determined",
                    _POOL_LOCATION,
                )
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
        job_id: str,
        warnings: List[ExtractionIssue],
    ) -> Tuple[Tuple[ArtifactReference, ...], Tuple[SynapseExpression, ...]]:
        """References and expressions, found by the shared structural scanner.

        A ``*Reference`` object is a reference; an arbitrary string is not.
        A ``BigDataPoolReference`` classifies as COMPUTE with no target type,
        because a Spark pool is workspace infrastructure rather than a
        repository artifact.
        """
        found_references, found_expressions = scan(properties, ROOT_PATH)

        references: List[ArtifactReference] = []
        for found in found_references:
            target_type, kind, recognized = classify_reference(found.reference_type)
            if not recognized:
                warnings.append(
                    ExtractionIssue(
                        IssueCode.UNSUPPORTED_CONSTRUCT,
                        f"reference type {found.reference_type!r} is not "
                        f"modelled; recorded with an unknown target type",
                        found.location,
                    )
                )
            references.append(
                ArtifactReference(
                    source_artifact_id=job_id,
                    source_artifact_type=AssetType.SPARK_JOB_DEFINITION,
                    kind=kind,
                    target_type=target_type,
                    target_name=found.reference_name,
                    location=found.location,
                    evidence=Evidence(artifact.source_path, None, EXTRACTOR_NAME),
                )
            )

        expressions = tuple(
            SynapseExpression(found.expression, found.location, found.form)
            for found in found_expressions
        )
        return (
            tuple(sorted(references, key=lambda r: (r.location, r.target_name))),
            expressions,
        )

    @staticmethod
    def _reference_at(
        references: Tuple[ArtifactReference, ...], location: str
    ) -> Optional[ArtifactReference]:
        """The reference written at one JSON path, promoted for convenience.

        Still present in ``references`` -- this is a view, not a second source
        of truth.
        """
        return next((r for r in references if r.location == location), None)

    # -- job properties ----------------------------------------------------

    def _job_properties(
        self, properties: dict, warnings: List[ExtractionIssue]
    ) -> SparkJobProperties:
        """The ``jobProperties`` block.

        An absent or non-object block produces an empty ``SparkJobProperties``
        and a warning, rather than a failure: the artifact still has an
        identity, a pool and a language worth reporting.
        """
        raw = properties.get("jobProperties")
        if not isinstance(raw, dict):
            if raw is not None:
                warnings.append(
                    ExtractionIssue(
                        IssueCode.MALFORMED_ARTIFACT,
                        f"jobProperties is a {type(raw).__name__}, not an "
                        f"object; the job's application and resources were "
                        f"not extracted",
                        JOB_PATH,
                    )
                )
            return SparkJobProperties()

        for unknown in sorted(key for key in raw if key not in KNOWN_JOB_PROPERTIES):
            warnings.append(
                ExtractionIssue(
                    IssueCode.UNSUPPORTED_CONSTRUCT,
                    f"jobProperties key {unknown!r} is not modelled; it was "
                    f"not extracted",
                    f"{JOB_PATH}.{unknown}",
                )
            )

        main_file = self._artifact(
            raw.get("file"), SparkArtifactKind.MAIN_FILE, f"{JOB_PATH}.file"
        )

        artifacts: List[SparkJobArtifact] = []
        for key, kind in _ARTIFACT_LISTS:
            values = raw.get(key)
            if values is None:
                continue
            if not isinstance(values, list):
                warnings.append(
                    ExtractionIssue(
                        IssueCode.MALFORMED_ARTIFACT,
                        f"jobProperties.{key} is a {type(values).__name__}, "
                        f"not a list; its entries were not extracted",
                        f"{JOB_PATH}.{key}",
                    )
                )
                continue
            for index, value in enumerate(values):
                found = self._artifact(value, kind, f"{JOB_PATH}.{key}[{index}]")
                if found is not None:
                    artifacts.append(found)

        return SparkJobProperties(
            job_name=self._optional_str(raw.get("name")),
            main_file=main_file,
            class_name=self._optional_str(raw.get("className")),
            arguments=self._arguments(raw.get("args"), warnings),
            artifacts=tuple(artifacts),
            configuration=self._configuration(raw.get("conf"), warnings),
            resources=SparkResourceRequest(
                driver_memory=self._optional_str(raw.get("driverMemory")),
                driver_cores=self._integer(raw.get("driverCores")),
                executor_memory=self._optional_str(raw.get("executorMemory")),
                executor_cores=self._integer(raw.get("executorCores")),
                executor_count=self._integer(raw.get("numExecutors")),
            ),
        )

    @staticmethod
    def _artifact(
        value: Any, kind: SparkArtifactKind, location: str
    ) -> Optional[SparkJobArtifact]:
        """One file pointer, preserved verbatim. Nothing is dereferenced."""
        if not isinstance(value, str) or not value.strip():
            return None
        uri = value.strip()
        match = _SCHEME.match(uri)
        return SparkJobArtifact(
            kind=kind,
            uri=uri,
            location=location,
            scheme=match.group(1).lower() if match else None,
        )

    @staticmethod
    def _safe_scalar(value: Any) -> Optional[str]:
        """A scalar rendered as a string, or None for anything else.

        Scalars only, deliberately. ``render_scalar`` falls back to JSON for a
        dict, and a dict in this position is exactly where a ``SecureString``
        or a Key Vault reference lives -- so rendering one would copy a secret
        value into the extracted model. The reference itself is still recorded,
        by ``scan_secrets``, which carries no value by construction.
        """
        if isinstance(value, (dict, list)):
            return None
        return render_scalar(value)

    def _arguments(
        self, value: Any, warnings: List[ExtractionIssue]
    ) -> Tuple[str, ...]:
        """Command-line arguments, in order. Order is meaning here.

        Rendered rather than filtered to strings: an argument written as a
        number is still an argument, and dropping it would change what the
        job is recorded as running. A structured argument is replaced rather
        than rendered, for the reason ``_safe_scalar`` gives.
        """
        if value is None:
            return ()
        if not isinstance(value, list):
            warnings.append(
                ExtractionIssue(
                    IssueCode.MALFORMED_ARTIFACT,
                    f"jobProperties.args is a {type(value).__name__}, not a "
                    f"list; the job's arguments were not extracted",
                    f"{JOB_PATH}.args",
                )
            )
            return ()
        rendered = [self._safe_scalar(item) or WITHHELD for item in value]
        return tuple(rendered)

    def _configuration(
        self, value: Any, warnings: List[ExtractionIssue]
    ) -> Tuple[ConfigEntry, ...]:
        """Spark configuration, sorted by key -- a JSON object has no order.

        A secret-named key, or one whose value is a structure rather than a
        scalar, keeps its key and loses its value. Which keys a job sets is
        migration-relevant; what a password is set to is not, and this model
        has no business carrying it.
        """
        if value is None:
            return ()
        if not isinstance(value, dict):
            warnings.append(
                ExtractionIssue(
                    IssueCode.MALFORMED_ARTIFACT,
                    f"jobProperties.conf is a {type(value).__name__}, not an "
                    f"object; the job's spark configuration was not extracted",
                    f"{JOB_PATH}.conf",
                )
            )
            return ()
        entries: List[ConfigEntry] = []
        for key in sorted(value):
            if is_secret_property(key.rsplit(".", 1)[-1]):
                entries.append(ConfigEntry(key, WITHHELD))
                continue
            rendered = self._safe_scalar(value[key])
            entries.append(ConfigEntry(key, WITHHELD if rendered is None else rendered))
        return tuple(entries)

    # -- metadata ----------------------------------------------------------

    @staticmethod
    def _parameters(declared: Any) -> Tuple[ValueDeclaration, ...]:
        """Declared parameters, sorted by name. Rarely present on this type."""
        if not isinstance(declared, dict):
            return ()
        parameters: List[ValueDeclaration] = []
        for key in sorted(declared):
            spec = declared[key]
            if not isinstance(spec, dict):
                parameters.append(ValueDeclaration(name=key))
                continue
            has_default = "defaultValue" in spec
            parameters.append(
                ValueDeclaration(
                    name=key,
                    type=spec.get("type") if isinstance(spec.get("type"), str) else None,
                    default_value=(
                        render_scalar(spec.get("defaultValue")) if has_default else None
                    ),
                    has_default=has_default,
                )
            )
        return tuple(parameters)

    @staticmethod
    def _optional_str(value: Any) -> Optional[str]:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _integer(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _folder_name(folder: Any) -> Optional[str]:
        if isinstance(folder, dict) and isinstance(folder.get("name"), str):
            return folder["name"] or None
        return None

    def _malformed(
        self,
        artifact: DetectedArtifact,
        context: ExtractionContext,
        reason: str,
        location: str,
    ) -> ExtractionResult[SparkJobDefinition]:
        """A malformed job definition fails. It never becomes an empty one."""
        return self.failure(
            artifact,
            context,
            errors=(
                ExtractionIssue(IssueCode.MALFORMED_ARTIFACT, reason, location or None),
            ),
        )
