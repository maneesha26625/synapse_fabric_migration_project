"""What the Fabric Migration Assistant for Data Warehouse carries over, and this tool now does too:
user-defined functions, security (roles, members, GRANT/DENY, dynamic data masks) and IDENTITY columns.

Offline. The pool is a set of canned catalog rows, the Warehouse a fake DB-API connection that records
what it was asked to run.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from discovery_agent.api import mapping
from discovery_agent.api.migration import sources_for
from discovery_agent.extractors.models import ExtractionProvenance
from discovery_agent.migration import datafilter, datapipeline, security, tsql_rules, warehouse_ddl
from discovery_agent.migration.common import (
    COMPLETED, DEFERRED, FAILED, FUNCTION, SECURITY, SKIPPED, TABLE, Source,
)
from discovery_agent.migration.preflight import content_findings
from discovery_agent.migration.runner import MigrationRun, Migrator
from discovery_agent.migration.validation import MATCH, MISMATCH, Validator
from discovery_agent.sql.mapping import build_functions
from discovery_agent.sql.models import (
    CatalogDatabase, CatalogPrincipal, SqlColumn, SqlObjectKey, SqlTable,
)
from discovery_agent.sql.result import SqlCatalogDiscovery
from discovery_agent.sql.security import build_security

from test_migration import FakeRest, WS, table  # noqa: E402 - shared fixtures

DB = "pool01"


# --- catalog fixtures ------------------------------------------------------------------------------

def function_rows():
    return [
        {"schema_name": "dbo", "object_name": "fnTax", "object_id": 501, "type_code": "FN"},
        {"schema_name": "sales", "object_name": "fnOrders", "object_id": 502, "type_code": "IF"},
        {"schema_name": "sales", "object_name": "fnMulti", "object_id": 503, "type_code": "TF"},
    ]


def module_rows():
    return [
        {"object_id": 501, "definition": "CREATE FUNCTION dbo.fnTax (@x decimal(18,2)) RETURNS decimal(18,2) AS BEGIN RETURN @x * 0.2 END"},
        {"object_id": 502, "definition": "CREATE FUNCTION sales.fnOrders () RETURNS TABLE AS RETURN SELECT OrderId FROM sales.Orders"},
        {"object_id": 503, "definition": "CREATE FUNCTION sales.fnMulti () RETURNS @t TABLE (a int) AS BEGIN INSERT @t SELECT 1 RETURN END"},
    ]


def security_catalog():
    principals = [
        {"principal_id": 0, "principal_name": "public", "type_code": "R"},
        {"principal_id": 1, "principal_name": "dbo", "type_code": "S"},
        {"principal_id": 5, "principal_name": "analyst@contoso.com", "type_code": "E"},
        {"principal_id": 6, "principal_name": "Finance Team", "type_code": "X"},
        {"principal_id": 7, "principal_name": "etl_login", "type_code": "S"},
        {"principal_id": 8, "principal_name": "reporting", "type_code": "R"},
        {"principal_id": 9, "principal_name": "largerc", "type_code": "R"},
        {"principal_id": 16390, "principal_name": "db_datareader", "type_code": "R"},
    ]
    members = [
        {"role_name": "reporting", "member_name": "analyst@contoso.com", "member_type": "E"},
        {"role_name": "reporting", "member_name": "etl_login", "member_type": "S"},
        {"role_name": "largerc", "member_name": "etl_login", "member_type": "S"},
        {"role_name": "db_datareader", "member_name": "Finance Team", "member_type": "X"},
    ]
    permissions = [
        {"class_code": 1, "permission_name": "SELECT", "state_code": "G", "grantee_name": "reporting", "grantee_type": "R",
         "schema_name": "sales", "object_name": "Orders", "object_type": "U", "column_name": None},
        {"class_code": 1, "permission_name": "SELECT", "state_code": "D", "grantee_name": "reporting", "grantee_type": "R",
         "schema_name": "sales", "object_name": "Orders", "object_type": "U", "column_name": "Customer"},
        {"class_code": 3, "permission_name": "EXECUTE", "state_code": "W", "grantee_name": "reporting", "grantee_type": "R",
         "schema_name": "sales", "object_name": None, "object_type": None, "column_name": None},
        {"class_code": 0, "permission_name": "CONNECT", "state_code": "G", "grantee_name": "reporting", "grantee_type": "R",
         "schema_name": None, "object_name": None, "object_type": None, "column_name": None},
        {"class_code": 0, "permission_name": "VIEW DEFINITION", "state_code": "G", "grantee_name": "analyst@contoso.com",
         "grantee_type": "E", "schema_name": None, "object_name": None, "object_type": None, "column_name": None},
        {"class_code": 1, "permission_name": "SELECT", "state_code": "G", "grantee_name": "etl_login", "grantee_type": "S",
         "schema_name": "sales", "object_name": "Orders", "object_type": "U", "column_name": None},
    ]
    masks = [{"schema_name": "sales", "table_name": "Orders", "column_name": "Customer", "masking_function": 'partial(1,"XXXX",0)'}]
    return build_security(DB, principals, members, permissions, masks)


def catalog():
    functions, _ = build_functions(DB, function_rows(), module_rows())
    return SqlCatalogDiscovery(
        database=CatalogDatabase(DB, "ws.sql.azuresynapse.net"), principal=CatalogPrincipal("reader"),
        provenance=ExtractionProvenance(source_type="sql", source_format="catalog", source_path=None, resource_id="pool01"),
        functions=functions, security=security_catalog(),
    )


# --- discovery -------------------------------------------------------------------------------------

def test_functions_are_discovered_with_their_kind_and_body():
    functions, issues = build_functions(DB, function_rows(), module_rows())
    assert not issues
    by = {f.key.name: f for f in functions}
    assert by["fnTax"].kind_label == "scalar function" and by["fnTax"].definition.is_readable
    assert by["fnOrders"].kind_label == "inline table-valued function"
    assert by["fnMulti"].is_multi_statement
    assert "sales.Orders" in by["fnOrders"].referenced_names


def test_security_is_read_into_principals_members_permissions_and_masks():
    sec = security_catalog()
    assert sec.principal("etl_login").is_sql_user and sec.principal("Finance Team").is_external
    assert sec.principal("largerc").is_resource_class and sec.principal("db_datareader").is_fixed_role
    assert {m.member for m in sec.members_of("reporting")} == {"analyst@contoso.com", "etl_login"}
    assert len(sec.permissions_of("reporting")) == 4
    assert sec.masks_on("sales", "Orders")[0].function == 'partial(1,"XXXX",0)'


def test_inventory_gets_functions_and_the_principals_that_carry_access():
    cat = catalog()
    functions = {e.name: e for e in mapping.function_extras(cat)}
    assert set(functions) == {"pool01.dbo.fnTax", "pool01.sales.fnOrders", "pool01.sales.fnMulti"}
    assert functions["pool01.sales.fnMulti"].status == mapping.STATUS_WARNING
    assert functions["pool01.sales.fnOrders"].dependencies[0]["targetId"] == "sql://pool01/sales/Orders"

    principals = {e.name: e for e in mapping.security_extras(cat)}
    # built-in principals, resource classes, and public without permissions are not inventory entries
    assert set(principals) == {"analyst@contoso.com", "Finance Team", "etl_login", "reporting", "db_datareader"}
    assert principals["etl_login"].status == mapping.STATUS_WARNING and "Entra" in principals["etl_login"].metadata["fabricSupport"]
    assert principals["etl_login"].metadata["resourceClasses"] == ["largerc"]
    reporting = principals["reporting"]
    assert reporting.source_type == "Security Object" and reporting.metadata["principalKind"] == "Database role"
    assert {d["targetId"] for d in reporting.dependencies} == {"sql://pool01/sales/Orders", "sql://pool01/sales"}


# --- T-SQL for Fabric ------------------------------------------------------------------------------

def test_a_role_becomes_create_role_members_and_grants():
    plan = security.plan_principal(security_catalog(), "reporting")
    assert plan.create_role == "CREATE ROLE [reporting];"
    assert [s for _, s in plan.members] == ["ALTER ROLE [reporting] ADD MEMBER [analyst@contoso.com];"]
    statements = [s for _, s in plan.permissions]
    assert statements == [
        "GRANT SELECT ON OBJECT::[sales].[Orders] TO [reporting];",
        "DENY SELECT ON OBJECT::[sales].[Orders] ([Customer]) TO [reporting];",
        "GRANT EXECUTE ON SCHEMA::[sales] TO [reporting] WITH GRANT OPTION;",
    ]  # CONNECT is given by the workspace in Fabric
    assert any("etl_login is a SQL user" in n for n in plan.notes)


def test_built_in_roles_are_not_created_and_users_get_grants_directly():
    sec = security_catalog()
    reader = security.plan_principal(sec, "db_datareader")
    assert reader.create_role is None and [s for _, s in reader.members] == ["ALTER ROLE [db_datareader] ADD MEMBER [Finance Team];"]
    analyst = security.plan_principal(sec, "analyst@contoso.com")
    assert [s for _, s in analyst.permissions] == ["GRANT VIEW DEFINITION TO [analyst@contoso.com];"]


def test_masks_become_column_clauses_and_unknown_functions_are_refused():
    mask = security_catalog().masks[0]
    assert security.mask_clause(mask) == "MASKED WITH (FUNCTION = 'partial(1,\"XXXX\",0)')"
    sql, notes = warehouse_ddl.create_table(table(), [("Customer", security.mask_clause(mask))])
    assert "[Customer] varchar(400) MASKED WITH (FUNCTION = 'partial(1,\"XXXX\",0)') NULL" in sql
    assert any("dynamic data mask kept" in n for n in notes)
    odd = SimpleNamespace(schema="s", table="t", column="c", function="custom(); DROP TABLE x")
    assert security.mask_clause(odd) is None and security.add_mask_statement(odd) is None


def test_identity_is_kept_as_bigint_identity_and_staged_without_it():
    sql, notes = warehouse_ddl.create_table(table())
    assert "[OrderId] bigint IDENTITY NOT NULL" in sql
    staged, _ = warehouse_ddl.create_table(table(), identity=False, schema="migration_load", name="sales__Orders")
    assert staged.startswith("CREATE TABLE [migration_load].[sales__Orders] (") and "IDENTITY" not in staged
    assert "[OrderId] bigint NOT NULL" in staged  # the same type, so the key values copy as they are


def test_an_identity_table_loads_through_staging_with_identity_insert_then_reseeds():
    e = datapipeline.entry(table(), replace=False, identity=True)
    assert (e["schema"], e["table"]) == ("migration_load", "sales__Orders")
    assert e["preCopyScript"] == "TRUNCATE TABLE [migration_load].[sales__Orders]"
    assert e["postCopyScript"].startswith("SET IDENTITY_INSERT [sales].[Orders] ON; INSERT INTO [sales].[Orders] ([OrderId], ")
    assert e["postCopyScript"].endswith("SET IDENTITY_INSERT [sales].[Orders] OFF;")
    assert e["postCopyCleanup"] == "TRUNCATE TABLE [migration_load].[sales__Orders]; DBCC CHECKIDENT('[sales].[Orders]', RESEED);"
    plain = datapipeline.entry(table(), replace=True)
    assert plain["postCopyScript"] == "" and plain["preCopyScript"] == "TRUNCATE TABLE [sales].[Orders]"

    target = datapipeline.WarehouseTarget("wh-1", "abc.datawarehouse.fabric.microsoft.com", "pool01", WS)
    inner = datapipeline.definition([e], "conn-1", target)["properties"]["activities"][0]["typeProperties"]["activities"]
    assert [a["name"] for a in inner] == ["Copy table", "After copy"]
    after = inner[1]
    assert after["type"] == "Script" and after["dependsOn"][0]["activity"] == "Copy table"
    assert "postCopyScript" in json.dumps(after) and datapipeline.NOTHING in json.dumps(after)


def test_a_synced_identity_table_keeps_its_keys_and_is_reseeded():
    tbl = table()
    resolved = SimpleNamespace(keys=("OrderId",))
    sql = datafilter.apply_sql(tbl, resolved, "sales", "Orders")
    assert "SET IDENTITY_INSERT [sales].[Orders] ON; INSERT INTO [sales].[Orders]" in sql
    assert sql.endswith("COMMIT TRANSACTION; DBCC CHECKIDENT('[sales].[Orders]', RESEED);")
    ddl = datafilter.staging_ddl("sales", "Orders", tbl)
    assert "CAST([OrderId] AS bigint) AS [OrderId]" in ddl  # CTAS through CAST leaves IDENTITY behind


def test_identity_insert_survives_the_rules_unless_the_table_has_no_identity():
    text = "CREATE PROCEDURE p AS SET IDENTITY_INSERT dbo.D ON; INSERT dbo.D (k) VALUES (1); SET IDENTITY_INSERT dbo.D OFF;"
    assert tsql_rules.rewrite(text).text == text
    assert tsql_rules.rewrite(text, without_identity={"dbo.d"}).text.count("removed for Fabric") == 2


# --- the run ---------------------------------------------------------------------------------------

class SecurityCursor:
    """A Warehouse that knows which roles and tables exist, and refuses statements containing ``reject``."""

    def __init__(self, db):
        self.db, self._row, self._rows = db, None, []

    def execute(self, sql, *params):
        self.db.statements.append(sql)
        if sql == security.ROLE_EXISTS_QUERY:
            self._row = (5,) if params[0] in self.db.roles else (None,)
        elif sql == warehouse_ddl.EXISTS_QUERY:
            self._row = (1,) if params[0] in self.db.existing else (None,)
        elif sql == security.MASKS_SQL:
            self._rows = list(self.db.masks)
        elif self.db.reject and self.db.reject in sql:
            raise RuntimeError("Cannot find the user 'ghost'")
        else:
            self._row, self._rows = None, []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class SecurityDb:
    def __init__(self, roles=(), existing=(), masks=(), reject=None):
        self.roles, self.existing, self.masks, self.reject = set(roles), set(existing), list(masks), reject
        self.statements = []

    def cursor(self):
        return SecurityCursor(self)

    def close(self):
        pass


def run_sources(sources, db):
    run = MigrationRun("001", sources, WS, "Sales WS", "pool01")
    Migrator(run, lambda: FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}]), lambda host, database: db).execute()
    return {i.source.name: i for i in run.items}


def plan_job():
    cat = catalog()
    extras = mapping.function_extras(cat) + mapping.security_extras(cat)
    items = [{"id": e.id, "name": e.name, "type": e.source_type} for e in extras]
    job = SimpleNamespace(items=items, extras_by_id={e.id: e for e in extras}, records_by_id={},
                          run=SimpleNamespace(catalog=cat), repository_artifacts={})
    return job, [{"id": e.id, "wave": 3} for e in extras]


def test_sources_defer_multi_statement_functions_and_sql_users_with_the_reason():
    job, plan = plan_job()
    by = {s.name: s for s in sources_for(plan, job, "pool01")}
    assert by["pool01.dbo.fnTax"].kind == FUNCTION and by["pool01.dbo.fnTax"].object_name == "fnTax"
    assert by["pool01.sales.fnMulti"].kind == DEFERRED and "multi-statement" in by["pool01.sales.fnMulti"].reason
    assert by["reporting"].kind == SECURITY
    etl = by["etl_login"]
    assert etl.kind == DEFERRED and "no SQL authentication" in etl.reason and "reporting" in etl.reason
    assert "GRANT SELECT ON OBJECT::[sales].[Orders] TO [<Microsoft Entra user or group>];" in etl.reason
    findings = {(f.object_id, f.code) for f in content_findings(list(by.values()))}
    assert (by["etl_login"].id, "NEEDS_PERSON") in findings and (by["reporting"].id, "SECURITY_DENY") in findings


def test_functions_are_created_in_the_warehouse_before_views_and_roles_after_them():
    job, plan = plan_job()
    sources = [s for s in sources_for(plan, job, "pool01") if s.kind in (FUNCTION, SECURITY)]
    db = SecurityDb()
    items = run_sources(sources, db)
    tax = items["pool01.dbo.fnTax"]
    assert tax.status == COMPLETED and tax.step == "Function created"
    assert any(s.startswith("CREATE FUNCTION dbo.fnTax") for s in db.statements)
    reporting = items["reporting"]
    assert reporting.status == COMPLETED and reporting.step.startswith("Role created; 1 member(s) and 3 permission(s)")
    assert "CREATE ROLE [reporting];" in db.statements and "GRANT EXECUTE ON SCHEMA::[sales] TO [reporting] WITH GRANT OPTION;" in db.statements
    assert items["analyst@contoso.com"].step.startswith("1 permission(s) applied")
    assert items["Finance Team"].step.startswith("Nothing granted directly")
    run = MigrationRun("x", sources, WS, "w", "pool01")
    kinds = [i.source.kind for i in run.items]
    assert kinds.index(FUNCTION) < kinds.index(SECURITY)


def test_an_existing_role_is_reused_and_rejected_grants_fail_the_role_with_every_reason():
    job, plan = plan_job()
    reporting = next(s for s in sources_for(plan, job, "pool01") if s.name == "reporting")
    db = SecurityDb(roles={"reporting"}, reject="ADD MEMBER")
    item = run_sources([reporting], db)["reporting"]
    assert "CREATE ROLE [reporting];" not in db.statements
    assert item.status == FAILED and "rejected 1 of 4 statement(s)" in item.error and "Cannot find the user" in item.error
    assert "GRANT SELECT ON OBJECT::[sales].[Orders] TO [reporting];" in db.statements  # the rest still applied


def test_a_table_is_created_with_its_masks_and_an_existing_one_gets_the_missing_mask():
    masks = tuple(security_catalog().masks)
    src = Source("t", "sales.Orders", "Table", 2, TABLE, payload=table(), schema="sales", object_name="Orders", masks=masks)
    db = SecurityDb()
    assert run_sources([src], db)["sales.Orders"].status == COMPLETED
    create = next(s for s in db.statements if s.startswith("CREATE TABLE"))
    assert "MASKED WITH (FUNCTION = 'partial(1,\"XXXX\",0)')" in create

    db = SecurityDb(existing={"[sales].[Orders]"})
    item = run_sources([src], db)["sales.Orders"]
    assert item.status == COMPLETED and item.step == "Already in the warehouse; dynamic data masks added"
    assert "ALTER TABLE [sales].[Orders] ALTER COLUMN [Customer] ADD MASKED WITH (FUNCTION = 'partial(1,\"XXXX\",0)');" in db.statements

    db = SecurityDb(existing={"[sales].[Orders]"}, masks=[("sales", "Orders", "Customer", 'partial(1,"XXXX",0)')])
    assert run_sources([src], db)["sales.Orders"].status == SKIPPED


def test_a_mask_fabric_does_not_have_stops_the_table_before_any_row_can_land():
    odd = (SimpleNamespace(schema="sales", table="Orders", column="Customer", function="custom()"),)
    src = Source("t", "sales.Orders", "Table", 2, TABLE, payload=table(), schema="sales", object_name="Orders", masks=odd)
    db = SecurityDb()
    item = run_sources([src], db)["sales.Orders"]
    assert item.status == FAILED and "never loaded unmasked" in item.error
    assert not any(s.startswith("CREATE TABLE") for s in db.statements)


# --- validation ------------------------------------------------------------------------------------

class WarehouseState:
    """A Fabric Warehouse as Validate reads it, with roles, members, permissions and masks."""

    def __init__(self, principals, members, permissions, masks=()):
        self.answers = {security.PRINCIPALS_SQL: principals, security.MEMBERS_SQL: members,
                        security.PERMISSIONS_SQL: permissions, security.MASKS_SQL: list(masks)}

    def cursor(self):
        db = self

        class Cursor:
            rows = []

            def execute(self, sql, *params):
                self.rows = list(db.answers.get(sql, []))

            def fetchall(self):
                return self.rows

        return Cursor()

    def close(self):
        pass


def validate(sources, wh):
    rest = FakeRest(warehouses=[{"id": "wh-1", "displayName": "pool01"}])
    validator = Validator(rest, WS, "Sales WS", "pool01", lambda host, database: wh)
    return validator.run(sources)


def test_validate_compares_roles_members_and_permissions():
    sec = security_catalog()
    src = Source("r", "reporting", "Security Object", 3, SECURITY, payload=sec)
    complete = WarehouseState(
        principals=[("reporting", "R")], members=[("reporting", "analyst@contoso.com")],
        permissions=[("reporting", 1, "SELECT", "G", "sales", "Orders", None),
                     ("reporting", 1, "SELECT", "D", "sales", "Orders", "Customer"),
                     ("reporting", 3, "EXECUTE", "W", "sales", None, None)])
    (row,) = validate([src], complete)
    assert row["category"] == "Security" and row["status"] == MATCH
    partial = WarehouseState(principals=[("reporting", "R")], members=[], permissions=[("reporting", 1, "SELECT", "G", "sales", "Orders", None)])
    (row,) = validate([src], partial)
    assert row["status"] == MISMATCH and "analyst@contoso.com" in row["detail"] and "2 permission(s) missing" in row["detail"]
    missing = WarehouseState(principals=[], members=[], permissions=[])
    assert validate([src], missing)[0]["detail"] == "The role is not in the Warehouse."


# --- LLM-assisted rewrite of a multi-statement function -------------------------------------------

def test_a_multi_statement_function_can_be_drafted_and_an_accepted_inline_rewrite_brings_it_back():
    from discovery_agent.api.migration import rewritable
    from discovery_agent.llm import checks, context

    job, plan = plan_job()
    multi = next(s for s in sources_for(plan, job, "pool01") if s.name == "pool01.sales.fnMulti")
    assert multi.kind == DEFERRED and rewritable(multi)
    multi.kind = FUNCTION
    subject = context.extract(multi)
    assert subject.kind == FUNCTION and "RETURNS @t TABLE" in subject.code
    still_multi = checks.run(FUNCTION, subject.code, [])
    assert checks.blocking(still_multi) and any("multi-statement" in c["message"] for c in still_multi)
    inline = "CREATE FUNCTION sales.fnMulti () RETURNS TABLE AS RETURN SELECT 1 AS a"
    assert not checks.blocking(checks.run(FUNCTION, inline, []))
