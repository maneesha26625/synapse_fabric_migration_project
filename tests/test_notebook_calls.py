"""Notebooks that run other notebooks: found by discovery, ordered by the waves, rewritten for Fabric. Offline."""

from __future__ import annotations

from discovery_agent.extractors.notebook_calls import find_calls, notebook_name, rewrite_calls
from discovery_agent.mapping.waves import assign_waves
from discovery_agent.migration import notebooks, preflight
from discovery_agent.migration.common import NOTEBOOK, Source
from discovery_agent.models import AssetType

from test_notebook_extractor import cell, extract, make_artifact, notebook_json  # noqa: E402


def test_literal_calls_are_found_with_the_notebook_name_fabric_runs():
    code = ('%run /Shared/Helpers/load_config\n'
            'x = mssparkutils.notebook.run("/Shared/clean.ipynb", 600, {"day": d})\n'
            "notebookutils.notebook.run('enrich')\n"
            '%run "My Notebook" {"p": 1}\n')
    calls = find_calls(code)
    assert [(c.target, c.line, c.magic) for c in calls] == [
        ("load_config", 1, True), ("clean", 2, False), ("enrich", 3, False), ("My Notebook", 4, True)]


def test_names_built_at_run_time_are_dynamic_never_guessed():
    code = 'mssparkutils.notebook.run(f"/jobs/{name}")\nmssparkutils.notebook.run(target, 60)\nmssparkutils.notebook.run("/a/" + x)\n'
    assert [c.target for c in find_calls(code)] == [None, None, None]
    assert notebook_name("/a/b/c.ipynb") == "c" and notebook_name("plain") == "plain"


def test_rewrite_drops_the_folder_and_leaves_names_and_dynamic_calls_alone():
    code = ('%run /Shared/load_config {"env": "dev"}\n'
            'mssparkutils.notebook.run("/Shared/clean", 600)\n'
            'mssparkutils.notebook.run("enrich")\n'
            'mssparkutils.notebook.run(f"/jobs/{n}")\n')
    text, n = rewrite_calls(code)
    assert n == 2
    assert text.splitlines() == ['%run load_config {"env": "dev"}', 'mssparkutils.notebook.run("clean", 600)',
                                 'mssparkutils.notebook.run("enrich")', 'mssparkutils.notebook.run(f"/jobs/{n}")']
    assert rewrite_calls(text) == (text, 0)


def test_discovery_records_a_called_notebook_as_a_reference(tmp_path):
    doc = notebook_json([cell("%run /Shared/load_config"), cell('mssparkutils.notebook.run("/x/clean", 60)\nmssparkutils.notebook.run(f"{y}")'),
                         cell("%run load_config", cell_type="markdown")], name="NB_Main")
    result = extract(tmp_path, doc, artifact=make_artifact("NB_Main"))
    called = [r for r in result.content.references if r.target_type is AssetType.NOTEBOOK]
    assert [(r.target_name, r.target_id) for r in called] == [
        ("load_config", "synapse://notebook/load_config"), ("clean", "synapse://notebook/clean")]
    assert called[0].location.endswith(":L1")


def test_a_called_notebook_is_never_scheduled_after_its_caller():
    waves = assign_waves({"nb:main": "Notebook", "nb:helper": "Notebook"}, [("nb:main", "nb:helper")])
    assert waves["nb:helper"] <= waves["nb:main"]


def _nb(name, *sources):
    payload = {"properties": {"cells": [{"cell_type": "code", "source": s} for s in sources],
                              "metadata": {"language_info": {"name": "python"}}}}
    return Source(f"nb:{name}", name, "Notebook", 1, NOTEBOOK, payload=payload)


def test_the_migrated_notebook_runs_its_callee_by_name_and_flags_dynamic_calls():
    doc, notes = notebooks.to_fabric_ipynb(_nb("main", "%run /Shared/helper\n", "mssparkutils.notebook.run(f'{x}')").payload)
    assert doc["cells"][0]["source"] == ["%run helper\n"]
    text = " ".join(notes)
    assert "notebook runs point at the notebook's name" in text and "built at run time" in text


def test_planner_reports_a_called_notebook_discovery_never_found():
    sources = [_nb("main", "%run /Shared/helper\n%run gone\n"), _nb("helper", "x = 1")]
    found = [f for f in preflight.content_findings(sources, discovered_notebooks={"main", "helper", "elsewhere"})
             if f.code == "CALLED_NOTEBOOK_MISSING"]
    assert len(found) == 1 and "gone" in found[0].message and "helper" not in found[0].message
    # Found by discovery but left out of the plan: the dependency risk already covers it.
    only_main = preflight.content_findings([_nb("main", "%run helper")], discovered_notebooks={"main", "helper"})
    assert not [f for f in only_main if f.code == "CALLED_NOTEBOOK_MISSING"]
