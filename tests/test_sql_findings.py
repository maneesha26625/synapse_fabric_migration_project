"""The shared conversions from SQL scan findings onto framework channels.

Tested directly rather than through an extractor, because the point of the
module is that it is not tied to one: the same findings must surface the same
way whether they came from a SQL script or from a pipeline activity's
embedded SQL.
"""

from __future__ import annotations

from discovery_agent.extractors.models import IssueCode, ReferenceKind
from discovery_agent.extractors.sql_findings import (
    dynamic_sql_warning,
    sql_object_references,
)
from discovery_agent.extractors.sql_models import (
    DynamicSqlSite,
    SqlObjectKind,
    SqlObjectReference,
    SqlOperation,
)
from discovery_agent.models import AssetType

SCHEMA_PARAM = "@{pipeline().parameters.SchemaName}"


def obj(
    object_name="Trips",
    schema="dbo",
    kind=SqlObjectKind.TABLE,
    operation=SqlOperation.READ,
    location="q:L1",
    database=None,
):
    return SqlObjectReference(
        name=f"{schema}.{object_name}" if schema else object_name,
        object_name=object_name,
        kind=kind,
        operation=operation,
        statement="FROM",
        location=location,
        schema=schema,
        database=database,
    )


def bridge(objects, **overrides):
    kwargs = dict(
        source_artifact_id="synapse://sqlscript/S1",
        source_artifact_type=AssetType.SQL_SCRIPT,
        source_path="sqlscript/S1.json",
        extractor="sql_script",
    )
    kwargs.update(overrides)
    return sql_object_references(objects, **kwargs)


# --- the reference channel ---------------------------------------------------


def test_a_durable_object_becomes_a_sql_object_reference():
    references = bridge([obj()])

    assert len(references) == 1
    reference = references[0]
    assert reference.kind is ReferenceKind.SQL_OBJECT
    assert reference.target_name == "dbo.Trips"
    assert reference.location == "q:L1"
    assert reference.source_artifact_id == "synapse://sqlscript/S1"
    assert reference.source_artifact_type is AssetType.SQL_SCRIPT


def test_a_sql_object_is_never_a_resolved_synapse_artifact():
    reference = bridge([obj()])[0]

    assert reference.target_type is None
    assert reference.target_id is None
    assert reference.resolved is False


def test_evidence_records_the_carrying_file_and_extractor():
    reference = bridge([obj()])[0]

    assert reference.evidence.source_file == "sqlscript/S1.json"
    assert reference.evidence.extractor == "sql_script"
    assert reference.evidence.line is None


def test_repeated_names_yield_one_reference_at_the_first_location():
    references = bridge(
        [
            obj(location="q:L3"),
            obj(location="q:L9", operation=SqlOperation.WRITE),
        ]
    )

    assert [r.target_name for r in references] == ["dbo.Trips"]
    assert references[0].location == "q:L3"


def test_references_are_sorted_by_qualified_name():
    references = bridge([obj("Zulu"), obj("Alpha"), obj("Mike")])

    assert [r.target_name for r in references] == [
        "dbo.Alpha",
        "dbo.Mike",
        "dbo.Zulu",
    ]


def test_temporary_objects_are_excluded():
    references = bridge(
        [obj(), obj("#staging", schema=None, kind=SqlObjectKind.TEMPORARY)]
    )

    assert [r.target_name for r in references] == ["dbo.Trips"]


def test_an_interpolated_object_is_kept_as_an_unresolved_dependency():
    """A dependency nobody can resolve is still a dependency."""
    references = bridge(
        [obj("TripsData", schema=SCHEMA_PARAM, kind=SqlObjectKind.INTERPOLATED)]
    )

    assert [r.target_name for r in references] == [f"{SCHEMA_PARAM}.TripsData"]
    assert references[0].resolved is False


def test_no_objects_yields_no_references():
    assert bridge([]) == ()


def test_the_carrier_is_what_the_reference_is_attributed_to():
    """The same findings, reported by a different extractor for a different asset."""
    references = bridge(
        [obj()],
        source_artifact_id="synapse://pipeline/TripFares",
        source_artifact_type=AssetType.PIPELINE,
        source_path="pipeline/TripFares.json",
        extractor="pipeline",
    )

    assert references[0].source_artifact_type is AssetType.PIPELINE
    assert references[0].source_artifact_id == "synapse://pipeline/TripFares"
    assert references[0].evidence.source_file == "pipeline/TripFares.json"
    assert references[0].evidence.extractor == "pipeline"
    assert references[0].kind is ReferenceKind.SQL_OBJECT


# --- the issue channel -------------------------------------------------------


def site(construct="EXEC(...)", location="q:L4"):
    return DynamicSqlSite(construct=construct, location=location, evidence="EXEC(@s)")


def test_no_dynamic_sql_means_nothing_to_admit():
    assert dynamic_sql_warning(()) is None


def test_dynamic_sql_produces_one_unsupported_construct_issue():
    issue = dynamic_sql_warning([site(), site(location="q:L7")])

    assert issue is not None
    assert issue.code is IssueCode.UNSUPPORTED_CONSTRUCT
    assert issue.location == "q:L4"  # the first site
    assert "2 dynamic SQL site(s)" in issue.message
    assert "not statically determinable" in issue.message
