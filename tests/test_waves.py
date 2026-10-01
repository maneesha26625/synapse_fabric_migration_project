"""Dependency-ordered waves, the classification vocabulary and the component table."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from discovery_agent.api.server import create_server
from discovery_agent.mapping import CLASSIFICATIONS, component_table, map_synapse_object
from discovery_agent.mapping.waves import assign_waves, summarise_waves


def test_an_object_is_never_scheduled_before_what_it_references():
    types = {"ls": "Linked Service", "ds": "Dataset", "pl": "Pipeline", "nb": "Notebook", "tr": "Trigger"}
    edges = [("ds", "ls"), ("pl", "ds"), ("pl", "nb"), ("tr", "pl")]
    waves = assign_waves(types, edges)
    for dependent, dependency in edges:
        assert waves[dependent] > waves[dependency] or waves[dependent] >= waves[dependency]
    assert waves["ls"] == 1 and waves["ds"] == 2
    assert waves["pl"] > waves["nb"]  # a pipeline calling a notebook comes after it
    assert waves["tr"] > waves["pl"]


def test_waves_follow_type_when_there_are_no_dependencies():
    waves = assign_waves({"t": "Table", "v": "View", "n": "Notebook"}, [])
    assert (waves["t"], waves["v"], waves["n"]) == (2, 3, 4)


def test_a_cycle_does_not_hang_or_crash():
    types = {"a": "Pipeline", "b": "Pipeline"}
    waves = assign_waves(types, [("a", "b"), ("b", "a")])
    assert set(waves) == {"a", "b"}


def test_edges_to_unknown_objects_are_ignored():
    assert assign_waves({"a": "Table"}, [("a", "ghost")]) == {"a": 2}


def test_wave_summary_counts_types():
    waves = {"a": 2, "b": 2, "c": 3}
    types = {"a": "Table", "b": "Table", "c": "View"}
    summary = summarise_waves(waves, types)
    assert summary[0] == {"wave": 2, "count": 2, "types": {"Table": 2}}


def test_every_classification_comes_from_the_fixed_vocabulary():
    for row in component_table():
        assert row["classification"] in CLASSIFICATIONS
        assert row["classification"] != "NOT SUPPORTED"  # only Assessment can say so
        assert row["action"]


def test_the_component_table_covers_the_spec_components():
    sources = {r["sourceType"] for r in component_table()}
    assert {"Dedicated SQL Pool", "Serverless SQL", "Pipeline", "Spark Pool", "Notebook", "Spark Job Definition",
            "Linked Service", "Dataset", "Trigger", "External Table", "External Data Source",
            "Security Object", "Networking Configuration"} <= sources


@pytest.mark.parametrize("source_type,code", [
    ("Linked Service", "RECONFIGURE"), ("Pipeline", "TRANSFORM"), ("Notebook", "DIRECT"),
    ("External Data Source", "REVIEW"), ("Integration Runtime", "MANUAL"),
])
def test_classification_codes(source_type, code):
    assert map_synapse_object(source_type).classification == code


def test_components_endpoint_needs_no_discovery():
    httpd = create_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/api/mapping/components")
        response = conn.getresponse()
        body = json.loads(response.read())
        assert response.status == 200 and len(body["components"]) >= 20
        conn.request("GET", "/api/dependencies")
        response = conn.getresponse()
        response.read()
        assert response.status == 409  # nothing discovered yet: refused, not empty
    finally:
        httpd.shutdown()
        httpd.server_close()
