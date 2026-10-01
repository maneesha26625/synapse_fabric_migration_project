"""The extractor framework, and later the extractors themselves.

The detector answers "what is this artifact?". An extractor answers "what is
inside it, and what does it reference?". How to migrate it is Assessment's
question, further downstream.

    DetectedArtifact -> ExtractorRegistry -> Extractor -> ExtractionResult

The framework is here now; the artifact-specific extractors (pipeline,
notebook, dataset, ...) arrive one module at a time and plug in by declaring
``supported_types`` and ``supported_sources``, without the orchestration
changing.

Modules:
  models       the contract: source types, provenance, references, results
  sources      where content comes from; only the repository is implemented
  base         the Extractor abstract class and ExtractionContext
  registry     artifact type + source type -> extractor
  orchestrator runs extractors over detected artifacts, deterministically
"""

from discovery_agent.extractors.base import ExtractionContext, Extractor
from discovery_agent.extractors.models import (
    ArtifactReference,
    ExtractionIssue,
    ExtractionProvenance,
    ExtractionResult,
    ExtractionRun,
    ExtractionStatus,
    ExtractorInfo,
    IssueCode,
    ReferenceKind,
    SourceType,
    resolve_asset_type,
)
from discovery_agent.extractors.orchestrator import (
    ExtractorOrchestrator,
    run_extraction,
)
from discovery_agent.extractors.dataset import DatasetExtractor
from discovery_agent.extractors.dataset_models import DatasetDefinition
from discovery_agent.extractors.linked_service import LinkedServiceExtractor
from discovery_agent.extractors.linked_service_models import LinkedServiceDefinition
from discovery_agent.extractors.notebook import NotebookExtractor
from discovery_agent.extractors.notebook_models import (
    NotebookCell,
    NotebookDefinition,
)
from discovery_agent.extractors.pipeline import PipelineExtractor
from discovery_agent.extractors.pipeline_models import (
    PipelineActivity,
    PipelineDefinition,
)
from discovery_agent.extractors.registry import ExtractorRegistry
from discovery_agent.extractors.spark_job_definition import SparkJobDefinitionExtractor
from discovery_agent.extractors.spark_job_definition_models import (
    SparkJobDefinition,
    SparkJobProperties,
)
from discovery_agent.extractors.sql_script import SQLScriptExtractor
from discovery_agent.extractors.sql_script_models import SQLScriptDefinition
from discovery_agent.extractors.sources import ArtifactSource, RepositoryArtifactSource

#: Every extractor shipped by default. Adding one here is all the wiring a new
#: artifact extractor needs; the orchestrator selects it from the artifact type.
DEFAULT_EXTRACTORS = (
    PipelineExtractor,
    NotebookExtractor,
    DatasetExtractor,
    LinkedServiceExtractor,
    SQLScriptExtractor,
    SparkJobDefinitionExtractor,
)


def default_registry() -> ExtractorRegistry:
    """A registry with every shipped extractor registered.

    Callers never name an extractor or a path: the registry resolves one from
    the DetectedArtifact's type and the source's type.
    """
    return ExtractorRegistry([extractor() for extractor in DEFAULT_EXTRACTORS])

__all__ = [
    "ArtifactReference",
    "ArtifactSource",
    "ExtractionContext",
    "ExtractionIssue",
    "ExtractionProvenance",
    "ExtractionResult",
    "ExtractionRun",
    "ExtractionStatus",
    "Extractor",
    "ExtractorInfo",
    "ExtractorOrchestrator",
    "ExtractorRegistry",
    "DEFAULT_EXTRACTORS",
    "DatasetDefinition",
    "DatasetExtractor",
    "IssueCode",
    "LinkedServiceDefinition",
    "LinkedServiceExtractor",
    "NotebookCell",
    "NotebookDefinition",
    "NotebookExtractor",
    "PipelineActivity",
    "PipelineDefinition",
    "PipelineExtractor",
    "ReferenceKind",
    "RepositoryArtifactSource",
    "SQLScriptDefinition",
    "SQLScriptExtractor",
    "SparkJobDefinition",
    "SparkJobDefinitionExtractor",
    "SparkJobProperties",
    "SourceType",
    "default_registry",
    "resolve_asset_type",
    "run_extraction",
]
