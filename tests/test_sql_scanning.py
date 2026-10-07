"""Scanner units that are not about any one artifact type.

The SQL script extractor's own tests cover the scanner as that artifact uses
it. These cover the interpolated-identifier case, which exists because a
pipeline activity's embedded SQL routinely carries Synapse expressions where
a standalone SQL script does not.
"""

from __future__ import annotations

import pytest

from discovery_agent.extractors.sql_models import SqlObjectKind, SqlOperation
from discovery_agent.extractors.sql_scanning import (
    resolve_dynamic_sql,
    scan_objects,
    split_qualified_name,
)

SCHEMA_PARAM = "@{pipeline().parameters.SchemaName}"


# --- an interpolated name is reported, not dropped ---------------------------


def test_an_interpolated_qualifier_keeps_the_object_name():
    """The confirmed case: this used to yield a single object named "@"."""
    objects = scan_objects(f"TRUNCATE TABLE {SCHEMA_PARAM}.TripsData;", "q")

    assert len(objects) == 1
    obj = objects[0]
    assert obj.name == f"{SCHEMA_PARAM}.TripsData"
    assert obj.object_name == "TripsData"
    assert obj.schema == SCHEMA_PARAM
    assert obj.operation is SqlOperation.TRUNCATE
    assert obj.statement == "TRUNCATE TABLE"


def test_an_interpolated_name_is_marked_interpolated():
    objects = scan_objects(f"TRUNCATE TABLE {SCHEMA_PARAM}.TripsData;", "q")

    assert objects[0].kind is SqlObjectKind.INTERPOLATED
    assert objects[0].is_interpolated


def test_an_interpolated_dependency_is_not_silently_dropped():
    """It is durable, so the bridging helper keeps it on the reference channel."""
    objects = scan_objects(f"TRUNCATE TABLE {SCHEMA_PARAM}.TripsData;", "q")

    assert objects[0].is_durable


def test_no_table_name_is_fabricated_for_an_interpolation():
    objects = scan_objects(f"SELECT * FROM {SCHEMA_PARAM}", "q")

    assert [o.qualified_name for o in objects] == [SCHEMA_PARAM]
    assert objects[0].kind is SqlObjectKind.INTERPOLATED
    assert "@" not in {o.object_name for o in objects}


def test_the_bare_at_sign_is_no_longer_produced():
    """Regression guard for the truncation this replaces."""
    for sql in (
        f"TRUNCATE TABLE {SCHEMA_PARAM}.TripsData;",
        f"SELECT * FROM {SCHEMA_PARAM}",
        f"INSERT INTO {SCHEMA_PARAM}.Fares VALUES (1)",
    ):
        assert "@" not in {o.object_name for o in scan_objects(sql, "q")}


def test_an_interpolation_carries_its_location():
    objects = scan_objects(f"SELECT 1;\nTRUNCATE TABLE {SCHEMA_PARAM}.T;", "q")

    assert objects[0].location == "q:L2"


# --- what the fix must not disturb -------------------------------------------


def test_an_interpolation_inside_a_string_literal_is_still_blanked():
    """The real pipeline's Lookup interpolates inside a literal, not as a name."""
    sql = f"EXEC('CREATE SCHEMA {SCHEMA_PARAM}')\nSELECT * FROM sys.schemas"

    objects = scan_objects(sql, "q")

    assert [o.qualified_name for o in objects] == ["sys.schemas"]
    assert not any(o.is_interpolated for o in objects)


def test_an_ordinary_name_is_not_interpolated():
    objects = scan_objects("TRUNCATE TABLE dbo.TripsData;", "q")

    assert objects[0].kind is SqlObjectKind.TABLE
    assert not objects[0].is_interpolated


def test_a_table_variable_is_still_temporary():
    """``@var`` and ``@{expr}`` both start with @; only the latter interpolates."""
    objects = scan_objects("SELECT * FROM @rows;", "q")

    assert objects[0].kind is SqlObjectKind.TEMPORARY
    assert not objects[0].is_durable


def test_a_temp_table_is_still_temporary():
    objects = scan_objects("SELECT * FROM #staging;", "q")

    assert objects[0].kind is SqlObjectKind.TEMPORARY


# --- splitting a name that contains an expression ----------------------------


def test_a_dot_inside_an_expression_is_not_a_name_separator():
    assert split_qualified_name(f"{SCHEMA_PARAM}.TripsData") == (
        None,
        SCHEMA_PARAM,
        "TripsData",
    )


def test_an_expression_alone_is_a_single_part():
    assert split_qualified_name(SCHEMA_PARAM) == (None, None, SCHEMA_PARAM)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Sales", (None, None, "Sales")),
        ("dbo.Sales", (None, "dbo", "Sales")),
        ("db.dbo.Sales", ("db", "dbo", "Sales")),
        ("srv.db.dbo.Sales", ("db", "dbo", "Sales")),
        ("[my db].[my schema].[my table]", ("my db", "my schema", "my table")),
    ],
)
def test_splitting_names_without_expressions_is_unchanged(raw, expected):
    assert split_qualified_name(raw) == expected


# --- dynamic SQL whose structure is static -----------------------------------

COPY_PROCEDURE = """CREATE PROC [operation].[usp_Load] @Account [varchar](100) AS
BEGIN
    DECLARE @root varchar(500) = 'https://' + @Account + '.dfs.core.windows.net/raw/';
    DECLARE @sql varchar(4000);
    SET @sql = 'COPY INTO stage.Green FROM ''' + @root + 'green/*.parquet'' WITH (FILE_TYPE = ''PARQUET'')';
    EXEC (@sql);
    SET @sql = 'COPY INTO stage.Yellow FROM ''' + @root + 'yellow/*.parquet'' WITH (FILE_TYPE = ''PARQUET'')';
    EXEC (@sql);
END;
"""


def test_dynamic_sql_with_only_runtime_values_resolves_to_its_tables():
    references, unresolved = resolve_dynamic_sql(COPY_PROCEDURE, "p")

    assert unresolved == ()
    assert [(r.qualified_name, r.statement, r.kind) for r in references] == [
        ("stage.Green", "COPY INTO", SqlObjectKind.TABLE),
        ("stage.Yellow", "COPY INTO", SqlObjectKind.TABLE),
    ]
    assert [r.location for r in references] == ["p:L6", "p:L8"]


@pytest.mark.parametrize(
    "sql,name",
    [
        ("EXEC('DELETE FROM dbo.Old')", "dbo.Old"),
        ("EXEC sp_executesql N'SELECT * FROM dbo.C WHERE id = @id', N'@id int', @id = 1", "dbo.C"),
        ("SET @sql = 'DELETE FROM dbo.D WHERE d = ''' + CONVERT(varchar(10), @d, 120) + ''''; EXEC (@sql);", "dbo.D"),
    ],
)
def test_literal_dynamic_sql_resolves(sql, name):
    references, unresolved = resolve_dynamic_sql(sql, "x")

    assert unresolved == ()
    assert [r.qualified_name for r in references] == [name]


@pytest.mark.parametrize(
    "sql",
    [
        "SET @sql = 'SELECT * FROM ' + @table; EXEC(@sql)",
        "EXEC sp_executesql @statement",
        "IF @a = 1 SET @sql = 'SELECT 1 FROM dbo.A' ELSE SET @sql = 'SELECT 1 FROM dbo.B'; EXEC(@sql)",
        "SET @sql = 'SELECT 1 FROM dbo.A'; SET @sql += ' JOIN ' + @t; EXEC(@sql)",
        "SET @sql = 'SELECT 1 FROM dbo.A'; SET @sql = @sql + ' JOIN ' + @t; EXEC(@sql)",
        "SELECT @sql = 'SELECT 1 FROM dbo.A'; EXEC(@sql)",
        "SET @sql = 'EXEC(''SELECT 1 FROM dbo.A'')'; EXEC(@sql)",
        "EXEC @procedure",
    ],
)
def test_dynamic_sql_that_could_name_anything_stays_unresolved(sql):
    references, unresolved = resolve_dynamic_sql(sql, "x")

    assert references == ()
    assert len(unresolved) == 1


def test_copy_into_options_are_not_an_object():
    objects = scan_objects(
        "COPY INTO dbo.T FROM 'https://a/b.parquet' WITH (FILE_TYPE = 'PARQUET')", "q"
    )

    assert [(o.qualified_name, o.statement) for o in objects] == [("dbo.T", "COPY INTO")]
