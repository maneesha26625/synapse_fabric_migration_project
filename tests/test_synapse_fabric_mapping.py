"""The Synapse -> Fabric mapping rules: deterministic, preliminary, never overclaiming."""

from __future__ import annotations

import pytest

from discovery_agent.mapping import (
    ACTIVITY_RULES,
    MIGRATION_PATHS,
    RULES,
    WORKSTREAMS,
    map_activity,
    map_synapse_object,
    reference_target,
)

# The source types the spec names, each with the Fabric component it must land on.
EXPECTED_TARGETS = {
    "Dedicated SQL Pool": "Fabric Data Warehouse",
    "Table": "Fabric Warehouse Table",
    "View": "Fabric Warehouse View",
    "Stored Procedure": "Fabric Warehouse Stored Procedure",
    "Function": "Fabric Warehouse Function",
    "Schema": "Fabric Warehouse Schema",
    "Notebook": "Fabric Notebook",
    "Spark Job Definition": "Fabric Spark Job Definition",
    "Pipeline": "Fabric Data Factory Pipeline",
    "Linked Service": "Fabric Connection",
    "Lake Database": "Fabric Lakehouse",
}


@pytest.mark.parametrize("source_type,target", EXPECTED_TARGETS.items())
def test_known_types_map_to_their_fabric_component(source_type, target):
    assert map_synapse_object(source_type).fabric_target == target


def test_the_mapping_is_deterministic():
    assert map_synapse_object("Pipeline") == map_synapse_object("Pipeline")


@pytest.mark.parametrize("source_type", sorted(RULES))
def test_every_rule_requires_assessment_and_uses_known_vocabulary(source_type):
    m = map_synapse_object(source_type)
    assert m.assessment_required is True  # Discovery never decides compatibility
    assert m.migration_path in MIGRATION_PATHS
    assert m.workstream in WORKSTREAMS


@pytest.mark.parametrize("source_type", ["External Table", "External Data Source", "External File Format", "Storage Reference"])
def test_ambiguous_objects_are_not_forced_into_one_component(source_type):
    assert map_synapse_object(source_type).migration_path == "Requires Assessment"


def test_unknown_types_are_not_guessed_at():
    m = map_synapse_object("Something New")
    assert m.fabric_target == "Requires Assessment"
    assert m.migration_path == "Requires Assessment"


def test_a_notebook_with_synapse_specific_code_needs_refactoring_and_says_which():
    plain = map_synapse_object("Notebook", {"synapse_specific": []})
    specific = map_synapse_object("Notebook", {"synapse_specific": ["mssparkutils.fs"]})
    assert plain.migration_path == "Direct Target"
    assert specific.migration_path == "Target With Refactoring"
    assert any("mssparkutils.fs" in n for n in specific.notes)


def test_linked_services_and_datasets_are_reconfigured_not_renamed():
    assert map_synapse_object("Linked Service").migration_path == "Requires Reconfiguration"
    assert "Linked Service" not in map_synapse_object("Linked Service").fabric_target


def test_no_rule_overstates_compatibility():
    banned = ("automatically migrated", "100%", "no changes required", "fully compatible")
    for rule in RULES.values():
        text = " ".join((rule.fabric_target, *rule.notes)).lower()
        assert not any(b in text for b in banned)


def test_activities_have_known_equivalents_or_say_they_need_assessment():
    assert map_activity("Copy").fabric_equivalent == "Copy activity"
    assert map_activity("SynapseNotebook").requires_manual_review is True
    unknown = map_activity("SomeFutureActivity")
    assert unknown.equivalence == "Requires Assessment"
    assert set(ACTIVITY_RULES) >= {"Copy", "Lookup", "GetMetadata", "SparkJob", "WebActivity"}


def test_dependencies_get_a_conceptual_fabric_label():
    assert reference_target("storage_path", None) == "OneLake"
    assert reference_target("artifact", "Linked Service") == "Fabric Connection"
    assert reference_target("nonsense", None) is None