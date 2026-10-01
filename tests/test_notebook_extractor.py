"""Tests for NotebookExtractor.

Synthetic notebooks written into temporary directories. No network, no git,
no dependency on the real repository.

Note on the credential test: it asserts that a secret value never reaches a
finding or a summary, while remaining in the preserved cell source. Both are
required — the source is the artifact and the migration worker needs it; the
derived reporting surface is what must never leak.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from discovery_agent.acquisition.models import RepositorySource
from discovery_agent.artifacts.models import (
    ArtifactCategory,
    DetectedArtifact,
    DetectionEvidence,
    DiscoverySource,
    Signal,
    SignalType,
)
from discovery_agent.extractors import (
    ExtractionContext,
    ExtractionStatus,
    ExtractorOrchestrator,
    IssueCode,
    NotebookExtractor,
    ReferenceKind,
    RepositoryArtifactSource,
    SourceType,
    default_registry,
)
from discovery_agent.extractors.notebook_models import (
    CellType,
    DetectionMethod,
    FindingCategory,
    LanguageSource,
    MagicScope,
    NotebookFormat,
    NotebookLanguage,
    PackageKind,
    ResourceCategory,
    SqlDetection,
)
from discovery_agent.models import AssetType

# A fake secret used only to prove it never escapes into a report.
FAKE_SECRET = "p@ssw0rd-not-real-12345"


# --- fixtures ----------------------------------------------------------------


def make_artifact(name="NB_Test", path=None) -> DetectedArtifact:
    return DetectedArtifact(
        artifact_type="notebook",
        artifact_name=name,
        source_path=path or f"workspace/notebook/{name}.json",
        source_format="synapse_notebook_json",
        confidence=1.0,
        discovery_source=DiscoverySource.PATH_AND_STRUCTURE,
        category=ArtifactCategory.SYNAPSE,
        evidence=DetectionEvidence((Signal(SignalType.PATH, "notebook/"),)),
        sha256="e" * 64,
    )


def cell(source, cell_type="code", **fields):
    """A cell with nbformat's line-list source, as Synapse writes it."""
    lines = source.split("\n")
    listed = [line + "\n" for line in lines[:-1]] + [lines[-1]]
    return dict(cell_type=cell_type, source=listed, **fields)


def notebook_json(cells, name="NB_Test", kernel="synapse_pyspark", **properties):
    document = {
        "name": name,
        "properties": {
            "nbformat": 4,
            "nbformat_minor": 2,
            "cells": cells,
            "metadata": {
                "kernelspec": {"name": kernel, "display_name": "Synapse PySpark"},
                "language_info": {"name": "python"},
            },
        },
    }
    document["properties"].update(properties)
    return document


def extract(tmp_path, document, artifact=None, repository=None, raw=None):
    artifact = artifact or make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        raw if raw is not None else json.dumps(document), encoding="utf-8"
    )
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path, repository))
    return NotebookExtractor().extract(artifact, context)


def findings_of(definition, category):
    return [f for f in definition.findings if f.category is category]


def constructs(definition):
    return {f.construct for f in definition.findings}


# --- 1, 2, 14: basic structure and cell order --------------------------------


def test_basic_notebook_with_multiple_cells(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell("# Title", cell_type="markdown"),
                cell("x = 1", execution_count=1),
                cell("print(x)", execution_count=2),
            ],
            name="NB_Basic",
        ),
    )

    assert result.status is ExtractionStatus.SUCCESS
    definition = result.content
    assert definition.name == "NB_Basic"
    assert definition.format is NotebookFormat.SYNAPSE_NOTEBOOK_JSON
    assert definition.nbformat == "4.2"
    assert definition.cell_count == 3
    assert len(definition.code_cells) == 2
    assert definition.cells[1].execution_count == 1


def test_python_cell_source_is_preserved_exactly(tmp_path):
    source = "# a comment\nimport os\n\n\ndef  f( x ):\n    return  x  # trailing\n"
    result = extract(tmp_path, notebook_json([cell(source)]))

    assert result.content.cells[0].source == source


def test_cell_order_is_preserved_and_never_sorted(tmp_path):
    sources = ["zebra = 1", "alpha = 2", "middle = 3"]
    result = extract(tmp_path, notebook_json([cell(s) for s in sources]))

    assert [c.source for c in result.content.cells] == sources
    assert [c.index for c in result.content.cells] == [0, 1, 2]


def test_markdown_cells_are_not_code_scanned(tmp_path):
    result = extract(
        tmp_path,
        notebook_json([cell("Use `mssparkutils.fs.ls()` to list", cell_type="markdown")]),
    )

    markdown = result.content.cells[0]
    assert markdown.cell_type is CellType.MARKDOWN
    assert markdown.language is NotebookLanguage.MARKDOWN
    assert markdown.findings == ()
    assert "mssparkutils" in markdown.source  # still preserved verbatim


def test_empty_notebook_is_a_real_empty_notebook(tmp_path):
    result = extract(tmp_path, notebook_json([]))

    assert result.status is ExtractionStatus.SUCCESS
    assert result.content.cells == ()


# --- 3, 4: languages and %%sql -----------------------------------------------


def test_notebook_language_comes_from_the_kernelspec(tmp_path):
    result = extract(tmp_path, notebook_json([cell("x = 1")]))

    assert result.content.language is NotebookLanguage.PYTHON
    assert result.content.language_source is LanguageSource.KERNELSPEC
    assert result.content.kernel == "synapse_pyspark"
    assert result.content.cells[0].language_source is LanguageSource.NOTEBOOK_DEFAULT


@pytest.mark.parametrize(
    "kernel,expected",
    [
        ("synapse_pyspark", NotebookLanguage.PYTHON),
        ("synapse_spark", NotebookLanguage.SCALA),
        ("synapse_sparkdotnet", NotebookLanguage.CSHARP),
        ("synapse_sql", NotebookLanguage.SQL),
    ],
)
def test_kernel_names_map_to_languages(tmp_path, kernel, expected):
    result = extract(tmp_path, notebook_json([cell("x = 1")], kernel=kernel))

    assert result.content.language is expected


def test_a_magic_sets_the_cell_language(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [cell("x = 1"), cell("%%sql\nSELECT 1"), cell("%%scala\nval x = 1")]
        ),
    )

    cells = result.content.cells
    assert cells[0].language is NotebookLanguage.PYTHON
    assert cells[1].language is NotebookLanguage.SQL
    assert cells[1].language_source is LanguageSource.MAGIC
    assert cells[2].language is NotebookLanguage.SCALA
    assert set(result.content.cell_languages) == {"python", "sql", "scala"}


def test_cell_metadata_language_wins_over_the_notebook_default(tmp_path):
    result = extract(
        tmp_path,
        notebook_json([cell("SELECT 1", metadata={"language": "sql"})]),
    )

    assert result.content.cells[0].language is NotebookLanguage.SQL
    assert result.content.cells[0].language_source is LanguageSource.CELL_METADATA


def test_an_undeclared_language_is_ambiguous_not_guessed(tmp_path):
    """Python-looking code must not make the language python by itself."""
    document = notebook_json([cell("import os\nprint('hello')")])
    document["properties"]["metadata"] = {}

    result = extract(tmp_path, document)

    assert result.status is ExtractionStatus.PARTIAL
    assert result.content.language is NotebookLanguage.UNKNOWN
    assert result.content.language_source is LanguageSource.UNKNOWN
    assert any("no recognizable language" in w.message for w in result.warnings)


# --- 5: metadata and configuration -------------------------------------------


def test_notebook_metadata_and_compute(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [cell("x = 1")],
            bigDataPool={"referenceName": "sparkpool01", "type": "BigDataPoolReference"},
            metadata={
                "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
                "language_info": {"name": "python"},
                "saveOutput": True,
                "sessionKeepAliveTimeout": 30,
                "a365ComputeOptions": {
                    "id": "/subscriptions/x/bigDataPools/sparkpool01",
                    "name": "sparkpool01",
                    "sparkVersion": "3.3",
                    "nodeCount": 5,
                    "cores": 8,
                    "memory": 56,
                    "endpoint": "https://ws.dev.azuresynapse.net/livyApi",
                },
            },
        ),
    )

    definition = result.content
    assert definition.save_output is True
    assert definition.compute.pool_name == "sparkpool01"
    assert definition.compute.spark_version == "3.3"
    assert definition.compute.node_count == 5
    assert definition.compute.resource_id.endswith("sparkpool01")
    metadata = {m.key: m.value for m in definition.metadata}
    assert metadata["kernelspec.name"] == "synapse_pyspark"
    assert metadata["sessionKeepAliveTimeout"] == "30"


def test_session_configuration(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [cell("x = 1")],
            sessionProperties={
                "driverMemory": "56g",
                "driverCores": 8,
                "executorMemory": "56g",
                "executorCores": 8,
                "numExecutors": 2,
                "conf": {
                    "spark.dynamicAllocation.enabled": "false",
                    "spark.dynamicAllocation.maxExecutors": "2",
                },
            },
        ),
    )

    session = result.content.session
    assert session.driver_memory == "56g"
    assert session.num_executors == 2
    assert [c.key for c in session.conf] == [
        "spark.dynamicAllocation.enabled",
        "spark.dynamicAllocation.maxExecutors",
    ]


def test_cell_tags_and_outputs_are_captured_without_output_content(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell(
                    "x = 1",
                    metadata={"tags": ["parameters", "skip"]},
                    outputs=[
                        {
                            "output_type": "stream",
                            "text": ["sensitive result data"],
                            "execution_count": 3,
                        }
                    ],
                )
            ]
        ),
    )

    target = result.content.cells[0]
    assert target.tags == ("parameters", "skip")
    assert target.outputs[0].output_type == "stream"
    assert target.outputs[0].size_bytes > 0
    # Output content is never carried into the model.
    assert "sensitive result data" not in json.dumps(result.content.to_dict())


# --- 6, 7, 8: Synapse-specific construct detection ---------------------------


def test_mssparkutils_detection(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell(
                    "files = mssparkutils.fs.ls('abfss://c@a.dfs.core.windows.net/')\n"
                    "mssparkutils.notebook.run('Child', 60)\n"
                    "mssparkutils.env.getWorkspaceName()"
                )
            ]
        ),
    )

    found = constructs(result.content)
    assert "mssparkutils.fs" in found
    assert "mssparkutils.notebook" in found
    assert "mssparkutils.env" in found
    assert findings_of(result.content, FindingCategory.SYNAPSE_UTILS)
    assert findings_of(result.content, FindingCategory.NOTEBOOK_EXECUTION)
    fs_finding = [f for f in result.content.findings if f.construct == "mssparkutils.fs"][0]
    assert fs_finding.cell_index == 0
    assert fs_finding.location == "properties.cells[0].source:L1"
    assert "mssparkutils.fs.ls" in fs_finding.evidence


def test_synapsesql_detection(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell(
                    "df = spark.read.synapsesql('pool.dbo.Trips')\n"
                    "df.write.synapsesql('pool.dbo.Out')"
                )
            ]
        ),
    )

    found = constructs(result.content)
    assert "spark.read.synapsesql" in found
    assert "spark.write.synapsesql" in found
    assert findings_of(result.content, FindingCategory.SYNAPSE_SQL_CONNECTOR)


def test_notebookutils_and_token_library_detection(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell(
                    "notebookutils.notebook.exit('done')\n"
                    "cs = TokenLibrary.getConnectionString('MyLinkedService')"
                )
            ]
        ),
    )

    found = constructs(result.content)
    assert "notebookutils.notebook" in found
    assert "TokenLibrary.getConnectionString" in found
    assert findings_of(result.content, FindingCategory.LINKED_SERVICE_API)


def test_spark_and_workspace_configuration_detection(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell(
                    "spark.conf.set('spark.sql.shuffle.partitions', 200)\n"
                    "sc._jsc.hadoopConfiguration().set('fs.key', 'v')"
                )
            ]
        ),
    )

    found = constructs(result.content)
    assert "spark.conf.set" in found
    assert "hadoopConfiguration.set" in found
    assert findings_of(result.content, FindingCategory.WORKSPACE_CONFIG)


def test_findings_stay_tied_to_their_cell(tmp_path):
    result = extract(
        tmp_path,
        notebook_json([cell("x = 1"), cell("mssparkutils.fs.ls('/')")]),
    )

    assert result.content.cells[0].findings == ()
    assert result.content.cells[1].findings
    assert all(f.cell_index == 1 for f in result.content.cells[1].findings)


def test_no_severity_is_assigned_to_findings(tmp_path):
    """Severity would be an assessment; extraction only observes."""
    result = extract(tmp_path, notebook_json([cell("mssparkutils.fs.ls('/')")]))

    assert not hasattr(result.content.findings[0], "severity")


# --- 9, 10: paths and endpoints ----------------------------------------------


def test_storage_path_detection(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell(
                    "p = 'abfss://raw@lake.dfs.core.windows.net/trips'\n"
                    "q = 'wasbs://c@acct.blob.core.windows.net/x'\n"
                    "m = 'synfs:/43/mount/data'"
                )
            ]
        ),
    )

    resources = {r.uri: r for r in result.content.all_resources}
    assert "abfss://raw@lake.dfs.core.windows.net/trips" in resources
    assert resources["abfss://raw@lake.dfs.core.windows.net/trips"].category is (
        ResourceCategory.STORAGE
    )
    assert resources["wasbs://c@acct.blob.core.windows.net/x"].scheme == "wasbs"
    assert resources["synfs:/43/mount/data"].category is ResourceCategory.MOUNT
    assert all(
        r.detection is DetectionMethod.URI_SCHEME
        for r in result.content.cells[0].resources
    )


def test_url_endpoint_detection(tmp_path):
    result = extract(
        tmp_path,
        notebook_json([cell("r = requests.get('https://api.example.com/v1/data')")]),
    )

    resource = result.content.cells[0].resources[0]
    assert resource.uri == "https://api.example.com/v1/data"
    assert resource.category is ResourceCategory.ENDPOINT
    assert resource.location == "properties.cells[0].source:L1"


def test_notebook_metadata_endpoint_is_a_notebook_level_resource(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [cell("x = 1")],
            metadata={
                "kernelspec": {"name": "synapse_pyspark"},
                "a365ComputeOptions": {
                    "endpoint": "https://ws.dev.azuresynapse.net/livyApi"
                },
            },
        ),
    )

    resource = result.content.resources[0]
    assert resource.detection is DetectionMethod.NOTEBOOK_METADATA
    assert resource.cell_index is None
    assert resource.category is ResourceCategory.ENDPOINT


# --- 11, 12: packages and configuration --------------------------------------


def test_pip_install_detection(tmp_path):
    result = extract(
        tmp_path,
        notebook_json([cell("%pip install pandas==1.5.3 requests\n!pip install numpy")]),
    )

    installs = [
        p for p in result.content.packages if p.kind is PackageKind.PIP_INSTALL
    ]
    by_name = {p.name: p for p in installs}
    assert set(by_name) == {"pandas", "requests", "numpy"}
    assert by_name["pandas"].specifier == "==1.5.3"
    assert by_name["requests"].specifier is None


def test_imports_are_distinguished_from_installs(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell(
                    "import os\n"
                    "import pandas as pd\n"
                    "from pyspark.sql import SparkSession\n"
                    "%pip install azure-identity"
                )
            ]
        ),
    )

    packages = {(p.name, p.kind) for p in result.content.packages}
    assert ("os", PackageKind.IMPORT) in packages
    assert ("pandas", PackageKind.IMPORT) in packages
    assert ("pyspark.sql", PackageKind.IMPORT) in packages
    assert ("azure-identity", PackageKind.PIP_INSTALL) in packages

    by_name = {p.name: p for p in result.content.packages}
    assert by_name["os"].is_standard_library is True
    assert by_name["pandas"].is_standard_library is False
    # An ordinary import is not reported as an install.
    assert not any(
        f.construct.startswith("import:") for f in result.content.findings
    )


def test_configure_magic_declares_spark_packages(tmp_path):
    body = json.dumps(
        {
            "driverMemory": "28g",
            "jars": ["abfss://libs@lake.dfs.core.windows.net/custom.jar"],
            "packages": ["com.microsoft.azure:spark-mssql-connector:1.0.0"],
        }
    )
    result = extract(tmp_path, notebook_json([cell("%%configure -f\n" + body)]))

    spark_packages = [
        p for p in result.content.packages if p.kind is PackageKind.SPARK_PACKAGE
    ]
    names = {p.name for p in spark_packages}
    assert "abfss://libs@lake.dfs.core.windows.net/custom.jar" in names
    assert any("spark-mssql-connector" in n for n in names)
    assert any(m.name == "configure" for m in result.content.magics)


# --- 13: SQL extraction ------------------------------------------------------


def test_sql_magic_cell_is_extracted_verbatim(tmp_path):
    sql = "SELECT tipAmount, fareAmount\nFROM trips\nWHERE passengerCount > 0"
    result = extract(tmp_path, notebook_json([cell("%%sql\n" + sql)]))

    block = result.content.sql_blocks[0]
    assert block.detection is SqlDetection.MAGIC_CELL
    assert block.sql == sql
    assert block.cell_index == 0
    assert block.location == "properties.cells[0].source:L2"


def test_spark_sql_literal_is_extracted(tmp_path):
    result = extract(
        tmp_path,
        notebook_json([cell('df = spark.sql("SELECT * FROM dbo.Trips LIMIT 10")')]),
    )

    block = result.content.sql_blocks[0]
    assert block.detection is SqlDetection.SPARK_SQL_CALL
    assert block.sql == "SELECT * FROM dbo.Trips LIMIT 10"


def test_spark_sql_with_a_variable_yields_no_sql_block(tmp_path):
    """No literal means no SQL, and no guessing about what it might be."""
    result = extract(tmp_path, notebook_json([cell("df = spark.sql(query)")]))

    assert result.content.sql_blocks == ()
    assert "spark.sql" in constructs(result.content)  # still recorded as an API use


def test_sql_is_not_parsed_into_dependencies(tmp_path):
    result = extract(tmp_path, notebook_json([cell("%%sql\nSELECT * FROM dbo.Trips")]))

    assert result.content.sql_blocks[0].sql == "SELECT * FROM dbo.Trips"
    assert result.content.all_references == ()  # table analysis is a later stage


# --- magic commands ----------------------------------------------------------


def test_magic_commands_are_captured_structurally(tmp_path):
    result = extract(
        tmp_path, notebook_json([cell("%%sql\nSELECT 1"), cell("%run /Shared/Setup")])
    )

    magics = result.content.magics
    assert magics[0].name == "sql"
    assert magics[0].scope is MagicScope.CELL
    assert magics[1].name == "run"
    assert magics[1].scope is MagicScope.LINE
    assert magics[1].arguments.strip() == "/Shared/Setup"


def test_modulo_and_percent_formatting_are_not_magics(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [cell("x = 10 % 3\nprint('Area = %s' % value)\nd = t.strftime('%m-%d')")]
        ),
    )

    assert result.content.magics == ()


def test_a_cell_magic_only_counts_on_the_first_line(tmp_path):
    result = extract(tmp_path, notebook_json([cell("x = 1\n%%sql\nSELECT 1")]))

    assert not any(m.scope is MagicScope.CELL for m in result.content.magics)


# --- 15, 16: unknown constructs ----------------------------------------------


def test_unknown_cell_type_is_preserved_with_a_warning(tmp_path):
    result = extract(
        tmp_path, notebook_json([cell("some content", cell_type="widget")])
    )

    assert result.status is ExtractionStatus.PARTIAL
    target = result.content.cells[0]
    assert target.cell_type is CellType.UNKNOWN
    assert target.raw_cell_type == "widget"
    assert target.source == "some content"
    assert any("widget" in w.message for w in result.warnings)


def test_unknown_notebook_property_is_warned_about(tmp_path):
    document = notebook_json([cell("x = 1")])
    document["properties"]["someFutureProperty"] = {"a": 1}

    result = extract(tmp_path, document)

    assert result.status is ExtractionStatus.PARTIAL
    assert any("someFutureProperty" in w.message for w in result.warnings)


def test_unknown_magic_is_preserved_with_a_warning(tmp_path):
    result = extract(tmp_path, notebook_json([cell("%%futuremagic\nbody")]))

    assert result.status is ExtractionStatus.PARTIAL
    magic = result.content.magics[0]
    assert magic.name == "futuremagic"
    assert magic.recognized is False
    assert any("futuremagic" in w.message for w in result.warnings)


def test_unclassifiable_code_produces_no_warning(tmp_path):
    """Most code matches no rule; that is normal, not a limitation."""
    result = extract(
        tmp_path, notebook_json([cell("def add(a, b):\n    return a + b")])
    )

    assert result.status is ExtractionStatus.SUCCESS
    assert result.warnings == ()


def test_a_malformed_cell_is_warned_about_not_dropped_silently(tmp_path):
    document = notebook_json([cell("x = 1")])
    document["properties"]["cells"].append("not a cell object")

    result = extract(tmp_path, document)

    assert result.status is ExtractionStatus.PARTIAL
    assert result.content.cell_count == 1
    assert any("cell is not an object" in w.message for w in result.warnings)


# --- 17, 18: malformed input -------------------------------------------------


def test_malformed_json_fails(tmp_path):
    result = extract(tmp_path, None, raw="{not json")

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert result.errors[0].code is IssueCode.MALFORMED_ARTIFACT


def test_missing_notebook_structure_fails(tmp_path):
    result = extract(tmp_path, {"name": "NB_Test", "properties": {"metadata": {}}})

    assert result.status is ExtractionStatus.FAILED
    assert result.content is None
    assert "no notebook reader recognizes" in result.errors[0].message


def test_cells_of_the_wrong_type_fails(tmp_path):
    result = extract(tmp_path, {"name": "NB", "properties": {"cells": "nope"}})

    assert result.status is ExtractionStatus.FAILED


def test_json_root_that_is_not_an_object_fails(tmp_path):
    result = extract(tmp_path, None, raw="[1, 2, 3]")

    assert result.status is ExtractionStatus.FAILED
    assert "not an object" in result.errors[0].message


# --- 19: no name guessing ----------------------------------------------------


def test_ordinary_strings_never_become_references(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell(
                    "table = 'sales'\n"
                    "dataset = 'tripsDataSource'\n"
                    "linked_service = 'TripFaresDataLakeStorageLinkedService'\n"
                    "df = spark.table(table)"
                )
            ]
        ),
    )

    assert result.content.all_references == ()


def test_a_linked_service_named_in_code_is_a_finding_not_a_reference(tmp_path):
    """The literal cannot be proven to be what runs, so it stays an observation."""
    result = extract(
        tmp_path,
        notebook_json([cell("cs = TokenLibrary.getConnectionString('MyLinkedService')")]),
    )

    assert result.content.all_references == ()
    finding = findings_of(result.content, FindingCategory.LINKED_SERVICE_API)[0]
    assert "MyLinkedService" in finding.evidence
    assert finding.cell_index == 0


def test_structural_references_are_still_extracted(tmp_path):
    """A bigDataPool is a declared *Reference object, so it is a reference."""
    result = extract(
        tmp_path,
        notebook_json(
            [cell("x = 1")],
            bigDataPool={"referenceName": "sparkpool01", "type": "BigDataPoolReference"},
        ),
    )

    reference = result.content.all_references[0]
    assert reference.target_name == "sparkpool01"
    assert reference.kind is ReferenceKind.COMPUTE
    assert reference.target_type is None  # a pool is workspace compute, not a file
    assert reference.resolved is False
    assert reference.source_artifact_id == "synapse://notebook/NB_Test"


# --- 20: secrets -------------------------------------------------------------


def test_secret_values_never_reach_findings_or_summaries(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [
                cell(
                    f'password = "{FAKE_SECRET}"\n'
                    "key = mssparkutils.credentials.getSecret('kv', 'my-secret')"
                )
            ]
        ),
    )
    definition = result.content

    credential_findings = findings_of(definition, FindingCategory.CREDENTIAL)
    assert len(credential_findings) >= 2

    # The value is redacted everywhere a report could surface it.
    for finding in definition.findings:
        assert FAKE_SECRET not in finding.evidence
    assert FAKE_SECRET not in json.dumps(definition.summary())
    assert FAKE_SECRET not in json.dumps(
        [f.to_dict() for f in definition.findings]
    )
    assert FAKE_SECRET not in json.dumps(definition.to_dict(include_source=False))

    # The existence and location are still recorded.
    literal = [
        f for f in credential_findings if f.construct.startswith("literal_assignment")
    ][0]
    assert literal.construct == "literal_assignment:password"
    assert literal.location == "properties.cells[0].source:L1"
    assert "***" in literal.evidence

    # The source itself is preserved: it is the artifact, and migration needs it.
    assert FAKE_SECRET in definition.cells[0].source


def test_secret_api_use_is_recorded_as_a_credential_finding(tmp_path):
    result = extract(
        tmp_path, notebook_json([cell("s = TokenLibrary.getSecret('kv', 'name')")])
    )

    assert findings_of(result.content, FindingCategory.CREDENTIAL)


# --- 21, 22, 23: framework integration ---------------------------------------


def test_provenance_is_preserved(tmp_path):
    repository = RepositorySource(
        provider="github",
        repository_url="https://github.com/contoso/synapse-workspace",
        ref="main",
        local_path=tmp_path,
        commit_sha="f" * 40,
    )
    result = extract(
        tmp_path, notebook_json([cell("x = 1")]), repository=repository
    )

    provenance = result.provenance
    assert provenance.source_type is SourceType.REPOSITORY
    assert provenance.repository_url.endswith("synapse-workspace")
    assert provenance.ref == "main"
    assert provenance.commit_sha == "f" * 40
    assert provenance.source_path == "workspace/notebook/NB_Test.json"
    assert provenance.sha256 == "e" * 64
    assert provenance.source_format == "synapse_notebook_json"
    assert result.extractor.name == "notebook"
    assert result.artifact_id == "synapse://notebook/NB_Test"


def test_registry_selects_the_notebook_extractor():
    registry = default_registry()

    selected = registry.require(AssetType.NOTEBOOK, SourceType.REPOSITORY)

    assert isinstance(selected, NotebookExtractor)
    assert selected.supported_types == (AssetType.NOTEBOOK,)
    assert AssetType.NOTEBOOK in registry.supported_types()


def test_extraction_runs_through_the_orchestrator(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(notebook_json([cell("mssparkutils.fs.ls('/')")])), encoding="utf-8"
    )
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path))

    run = ExtractorOrchestrator(default_registry(), context).run([artifact])

    assert len(run.results) == 1
    assert run.results[0].succeeded
    assert run.results[0].artifact_type is AssetType.NOTEBOOK
    assert run.results[0].content.findings


def test_malformed_notebook_through_the_orchestrator(tmp_path):
    artifact = make_artifact()
    target = tmp_path / artifact.source_path
    target.parent.mkdir(parents=True)
    target.write_text("{oops", encoding="utf-8")
    context = ExtractionContext(source=RepositoryArtifactSource(tmp_path))

    run = ExtractorOrchestrator(default_registry(), context).run([artifact])

    assert run.results[0].status is ExtractionStatus.FAILED


# --- determinism -------------------------------------------------------------


def test_extraction_is_deterministic(tmp_path):
    document = notebook_json(
        [
            cell("%%sql\nSELECT 1"),
            cell("import pandas\nmssparkutils.fs.ls('abfss://c@a.dfs.core.windows.net/')"),
        ],
        bigDataPool={"referenceName": "pool", "type": "BigDataPoolReference"},
        sessionProperties={"conf": {"b": "2", "a": "1"}},
    )

    first = extract(tmp_path, document)
    second = extract(tmp_path, document)

    assert first.content == second.content
    assert first.references == second.references


def test_unordered_collections_are_sorted(tmp_path):
    result = extract(
        tmp_path,
        notebook_json(
            [cell("x = 1", metadata={"tags": ["zebra", "alpha"]})],
            sessionProperties={"conf": {"z.setting": "1", "a.setting": "2"}},
        ),
    )

    assert result.content.cells[0].tags == ("alpha", "zebra")
    assert [c.key for c in result.content.session.conf] == ["a.setting", "z.setting"]


def test_to_dict_is_json_serializable(tmp_path):
    result = extract(
        tmp_path, notebook_json([cell("%%sql\nSELECT 1"), cell("import os")])
    )

    payload = result.content.to_dict()
    json.dumps(payload)
    assert payload["cells"][0]["source"].startswith("%%sql")
    assert json.dumps(result.content.summary())
