"""NotebookExtractor against the real local repository snapshot.

Runs walk -> detect -> extract over the clone acquisition produced. Local
reads only: no network, no git.

A note on what this notebook turned out to be: it is a portable PySpark/MLlib
notebook with **no** Synapse-specific constructs — no mssparkutils, no
synapsesql, no magics, no storage URIs. Several tests below therefore assert
the *absence* of findings. That is a real property of this artifact, not a
gap in the scanners, which are exercised against synthetic notebooks in
tests/test_notebook_extractor.py.
"""

from __future__ import annotations

import pytest

from conftest import requires_snapshot, smoke_run

from discovery_agent.extractors import (
    ExtractionContext,
    ExtractionStatus,
    ExtractorOrchestrator,
    ReferenceKind,
    RepositoryArtifactSource,
    default_registry,
)
from discovery_agent.extractors.notebook_models import (
    CellType,
    NotebookFormat,
    NotebookLanguage,
    PackageKind,
    ResourceCategory,
)
from discovery_agent.models import AssetType

pytestmark = requires_snapshot


def build_run(with_repository_metadata: bool = False):
    """One discovery run, assembled by ``discovery.run`` rather than by hand.

    ``with_repository_metadata`` used to mean "invent a RepositorySource with
    a zeroed commit SHA". It now means "go through the connection layer", so
    the provenance these tests check is the snapshot's real identity.
    """
    return smoke_run(connected=with_repository_metadata).extraction


@pytest.fixture(scope="module")
def run():
    return build_run()


@pytest.fixture(scope="module")
def notebooks(run):
    extracted = [
        result
        for result in run.results
        if result.artifact_type is AssetType.NOTEBOOK
        and result.status is not ExtractionStatus.SKIPPED
    ]
    assert extracted, "no notebooks were extracted"
    return extracted


@pytest.fixture(scope="module")
def notebook(notebooks):
    return notebooks[0].content


def test_every_notebook_extracts_without_failing(notebooks):
    for result in notebooks:
        assert result.succeeded, f"{result.artifact_name}: {result.errors}"
        assert result.content is not None


def test_notebook_count_and_names(notebooks):
    names = [result.content.name for result in notebooks]

    assert len(names) == len(set(names))
    assert any("NYC taxi" in name for name in names)


def test_format_and_language_are_declared_not_guessed(notebook):
    assert notebook.format is NotebookFormat.SYNAPSE_NOTEBOOK_JSON
    assert notebook.nbformat == "4.2"
    assert notebook.language is NotebookLanguage.PYTHON
    assert notebook.kernel == "synapse_pyspark"
    assert notebook.language_source.value in ("kernelspec", "language_info")


def test_cells_are_extracted_in_order_with_their_types(notebook):
    assert notebook.cell_count > 0
    assert set(notebook.cell_types) <= {"code", "markdown", "raw"}
    assert notebook.code_cells
    assert [c.index for c in notebook.cells] == list(range(notebook.cell_count))
    assert set(notebook.cell_languages) <= {"python", "markdown"}


def test_source_is_preserved_verbatim(notebook):
    """Spot-check against known content: comments and spacing survive."""
    code = "\n".join(c.source for c in notebook.code_cells)

    assert "from pyspark.ml import Pipeline" in code
    assert "#To make development easier" in code  # comment kept, unreformatted
    assert any(c.source != c.source.strip() or "\n" in c.source for c in notebook.code_cells)


def test_compute_binding_and_session_configuration(notebook):
    assert notebook.compute is not None
    assert notebook.compute.pool_name
    assert notebook.compute.spark_version
    assert notebook.session is not None
    assert notebook.session.num_executors is not None
    assert [c.key for c in notebook.session.conf] == sorted(
        c.key for c in notebook.session.conf
    )


def test_spark_pool_is_a_structural_reference(notebook):
    references = notebook.all_references

    assert len(references) == 1
    pool = references[0]
    assert pool.target_name == notebook.compute.pool_name
    assert pool.kind is ReferenceKind.COMPUTE
    assert pool.target_type is None, "a Spark pool is workspace compute, not a file"
    assert pool.resolved is False
    assert pool.location == "properties.bigDataPool"


def test_workspace_endpoint_is_captured_as_a_resource(notebook):
    endpoints = [
        r for r in notebook.all_resources if r.category is ResourceCategory.ENDPOINT
    ]

    assert endpoints
    assert any("azuresynapse.net" in r.uri for r in endpoints)
    assert all(r.cell_index is None for r in endpoints)  # notebook-level metadata


def test_imports_are_captured_and_classified(notebook):
    imports = [p for p in notebook.packages if p.kind is PackageKind.IMPORT]
    names = {p.name for p in imports}

    assert names, "this notebook imports libraries and they must be captured"
    assert any(n.startswith("pyspark") for n in names)
    assert "datetime" in names

    third_party = {p.name for p in imports if p.is_standard_library is False}
    standard = {p.name for p in imports if p.is_standard_library is True}
    assert "datetime" in standard
    assert any(n.startswith("pyspark") for n in third_party)

    # Ordinary imports are never reported as installations.
    assert not [p for p in notebook.packages if p.kind is PackageKind.PIP_INSTALL]


def test_this_notebook_has_no_synapse_specific_constructs(notebook):
    """A real property of this artifact, verified rather than assumed.

    It is a portable PySpark/MLlib notebook: no mssparkutils, no synapsesql,
    no magics, no storage URIs in code. If the upstream repository ever adds
    them, this test is the one that should be revisited.
    """
    assert notebook.findings_by_category() == {}
    assert notebook.sql_blocks == ()
    assert notebook.magics == ()
    assert not [r for r in notebook.all_resources if r.cell_index is not None]


def test_no_warnings_on_the_real_notebook(notebooks):
    for result in notebooks:
        assert result.warnings == (), (
            f"{result.artifact_name}: {[w.message for w in result.warnings]}"
        )
        assert result.status is ExtractionStatus.SUCCESS


def test_both_extractors_run_and_the_rest_is_recorded_as_skipped(run):
    handled = {
        result.artifact_type
        for result in run.results
        if result.status is not ExtractionStatus.SKIPPED
    }
    skipped = {
        result.artifact_type
        for result in run.results
        if result.status is ExtractionStatus.SKIPPED
    }

    assert AssetType.NOTEBOOK in handled
    assert AssetType.PIPELINE in handled
    # Types without an extractor yet are recorded rather than dropped. The
    # specific types shrink as extractors land, so only the rule is asserted.
    assert skipped, "expected artifact types still awaiting an extractor"
    assert not (handled & skipped)
    for result in run.results:
        if result.status is ExtractionStatus.SKIPPED:
            assert result.errors, "a skipped artifact must say why"


def test_provenance_is_complete(notebooks):
    run_with_metadata = build_run(with_repository_metadata=True)
    result = [
        r
        for r in run_with_metadata.results
        if r.artifact_type is AssetType.NOTEBOOK and r.content is not None
    ][0]

    assert result.provenance.repository_url.endswith("1-click-POC")
    assert result.provenance.ref == "main"
    # The real SHA acquisition reported, not a placeholder. This assertion
    # used to pass against a hand-built "0" * 40 because nothing connected
    # acquisition to extraction.
    assert result.provenance.commit_sha
    assert result.provenance.commit_sha != "0" * 40
    assert len(result.provenance.commit_sha) == 40
    assert result.provenance.source_path.endswith(".json")
    assert result.provenance.sha256
    assert result.provenance.source_format == "synapse_notebook_json"
    assert result.extractor.name == "notebook"


def test_summary_is_source_free(notebook):
    """The summary surface never carries cell source, so it cannot leak one."""
    import json

    payload = json.dumps(notebook.summary())

    assert notebook.name in payload
    for code_cell in notebook.code_cells:
        first_line = code_cell.source.splitlines()[0] if code_cell.source else ""
        if len(first_line) > 20:
            assert first_line not in payload


def test_extraction_is_reproducible():
    first = build_run()
    second = build_run()

    assert [r.content for r in first.results] == [r.content for r in second.results]
