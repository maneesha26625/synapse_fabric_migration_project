"""Synapse database security -> Fabric Warehouse T-SQL.

What the Fabric Migration Assistant carries over as "security objects", rebuilt from the
catalog (``sql.security``):

* **Roles**: ``CREATE ROLE`` for each custom role; built-in roles (``db_datareader`` and
  the rest) and ``public`` already exist in a Warehouse, so only their members and
  permissions move.
* **Members**: ``ALTER ROLE ... ADD MEMBER`` for Microsoft Entra users and groups and
  for nested roles.
* **Permissions**: ``GRANT`` / ``DENY`` on the database, a schema, an object or columns,
  to roles and to Microsoft Entra users. Fabric creates a user on its first GRANT or
  DENY; ``CREATE USER`` is not supported there, so it is never issued.
* **Dynamic data masks**: written into the table's ``CREATE TABLE`` (``masked_column``),
  so no row is ever loaded into an unmasked column.

Left for a person, with the reason: SQL-authenticated users (Fabric has none; a Microsoft
Entra principal replaces each one) and Synapse resource-class roles (workload management,
which Fabric does not have). The database-level ``CONNECT`` every user holds is not
carried: in Fabric, workspace roles and item permissions decide who can connect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from discovery_agent.migration.warehouse_ddl import qualified, quote
from discovery_agent.sql.security import (
    RESOURCE_CLASS_ROLES,
    CLASS_DATABASE,
    CLASS_OBJECT,
    CLASS_SCHEMA,
    SqlMaskedColumn,
    SqlPermission,
    SqlPrincipal,
    SqlSecurity,
)

#: Database permissions that are not carried: every user has CONNECT, and Fabric grants it through the workspace.
_NOT_CARRIED = {(CLASS_DATABASE, "CONNECT")}
#: A permission name is words separated by single spaces (``SELECT``, ``ALTER ANY SCHEMA``); nothing else is emitted.
_PERMISSION = re.compile(r"^[A-Z]+(?: [A-Z]+)*$")
#: A masking function as the catalog reports it: default(), email(), random(1, 12), partial(1,"XXX",0).
_MASK = re.compile(r"""^(?:default\(\)|email\(\)|random\(\s*-?[\d.]+\s*,\s*-?[\d.]+\s*\)|partial\(\s*\d+\s*,\s*"[^"']*"\s*,\s*\d+\s*\))$""", re.I)

ROLE_EXISTS_QUERY = "SELECT DATABASE_PRINCIPAL_ID(?)"


@dataclass
class SecurityPlan:
    """What to run for one principal, and what is left for a person."""

    principal: SqlPrincipal
    create_role: Optional[str] = None
    members: List[Tuple[str, str]] = field(default_factory=list)  # (member, statement)
    permissions: List[Tuple[str, str]] = field(default_factory=list)  # (readable, statement)
    notes: List[str] = field(default_factory=list)

    @property
    def statements(self) -> List[str]:
        return ([self.create_role] if self.create_role else []) + [s for _, s in self.members] + [s for _, s in self.permissions]


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def securable(permission: SqlPermission) -> Optional[str]:
    """The ``ON ...`` clause, with a leading space; empty for the database; None when it cannot be named."""
    if permission.class_code == CLASS_DATABASE:
        return ""
    if permission.class_code == CLASS_SCHEMA and permission.schema:
        return f" ON SCHEMA::{quote(permission.schema)}"
    if permission.class_code == CLASS_OBJECT and permission.schema and permission.object_name:
        columns = f" ({quote(permission.column)})" if permission.column else ""
        return f" ON OBJECT::{qualified(permission.schema, permission.object_name)}{columns}"
    return None


def permission_statement(permission: SqlPermission, grantee: str) -> Optional[str]:
    """``GRANT`` or ``DENY`` for one catalog permission, or None when it is not carried or cannot be expressed."""
    name = permission.permission.strip().upper()
    if (permission.class_code, name) in _NOT_CARRIED or not _PERMISSION.match(name):
        return None
    on = securable(permission)
    if on is None:
        return None
    verb = "DENY" if permission.is_deny else "GRANT"
    option = " WITH GRANT OPTION" if permission.with_grant_option else ""
    return f"{verb} {name}{on} TO {quote(grantee)}{option};"


def plan_principal(security: SqlSecurity, name: str) -> SecurityPlan:
    """The statements that recreate one role or user in a Warehouse."""
    principal = security.principal(name)
    if principal is None:
        raise KeyError(name)
    plan = SecurityPlan(principal)
    if principal.is_role and not principal.is_fixed_role and not principal.is_public:
        plan.create_role = f"CREATE ROLE {quote(principal.name)};"
    if principal.is_role:
        for member in security.members_of(principal.name):
            who = security.principal(member.member)
            if who is not None and (who.is_built_in or who.is_resource_class):
                continue
            if member.member_type == "S" or (who is not None and who.is_sql_user):
                plan.notes.append(f"Member {member.member} is a SQL user: add the Microsoft Entra principal that replaces it to this role by hand.")
                continue
            plan.members.append((member.member, f"ALTER ROLE {quote(principal.name)} ADD MEMBER {quote(member.member)};"))
    skipped = 0
    for permission in security.permissions_of(principal.name):
        statement = permission_statement(permission, principal.name)
        if statement is None:
            skipped += 1
            continue
        plan.permissions.append((f"{statement.split(' TO ', 1)[0]}", statement))
    if skipped:
        plan.notes.append(f"{skipped} permission(s) not carried: CONNECT is given by the workspace in Fabric, and anything "
                          "that could not be named is left out.")
    resource = [r for r in security.roles_of(principal.name) if r.lower() in RESOURCE_CLASS_ROLES]
    if resource:
        plan.notes.append(f"Resource class {', '.join(sorted(resource))} dropped: Fabric sizes each query itself.")
    return plan


def manual_statements(plan: SecurityPlan, replacement: str = "<Microsoft Entra user or group>") -> List[str]:
    """For a SQL user: what to run once a Microsoft Entra principal replaces it."""
    out = [s.replace(f"TO {quote(plan.principal.name)}", f"TO [{replacement}]") for _, s in plan.permissions]
    return out


def mask_clause(mask: SqlMaskedColumn) -> Optional[str]:
    """``MASKED WITH (FUNCTION = '...')`` for a column definition, or None when the function is not one Fabric has."""
    function = mask.function.strip()
    if not _MASK.match(function):
        return None
    return f"MASKED WITH (FUNCTION = {_literal(function)})"


def add_mask_statement(mask: SqlMaskedColumn) -> Optional[str]:
    """``ALTER TABLE ... ALTER COLUMN ... ADD MASKED`` for a table that already exists."""
    clause = mask_clause(mask)
    if clause is None:
        return None
    return f"ALTER TABLE {qualified(mask.schema, mask.table)} ALTER COLUMN {quote(mask.column)} ADD {clause};"


#: Fabric's own view of masks, for a table that already exists and for Validate.
MASKS_SQL = ("SELECT s.name, t.name, mc.name, mc.masking_function FROM sys.masked_columns mc "
             "JOIN sys.tables t ON t.object_id = mc.object_id JOIN sys.schemas s ON s.schema_id = t.schema_id "
             "WHERE mc.is_masked = 1")
#: Fabric's roles, members and explicit permissions, for Validate.
PRINCIPALS_SQL = "SELECT name, type FROM sys.database_principals"
MEMBERS_SQL = ("SELECT r.name, m.name FROM sys.database_role_members rm "
               "JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id "
               "JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id")
PERMISSIONS_SQL = ("SELECT gp.name, pe.class, pe.permission_name, pe.state, COALESCE(os.name, ss.name), o.name, c.name "
                   "FROM sys.database_permissions pe "
                   "JOIN sys.database_principals gp ON gp.principal_id = pe.grantee_principal_id "
                   "LEFT JOIN sys.objects o ON pe.class = 1 AND o.object_id = pe.major_id "
                   "LEFT JOIN sys.schemas os ON os.schema_id = o.schema_id "
                   "LEFT JOIN sys.schemas ss ON pe.class = 3 AND ss.schema_id = pe.major_id "
                   "LEFT JOIN sys.columns c ON pe.class = 1 AND pe.minor_id > 0 AND c.object_id = pe.major_id AND c.column_id = pe.minor_id "
                   "WHERE pe.class IN (0, 1, 3)")


def permission_key(grantee: str, class_code: int, permission: str, state: str, schema: Optional[str],
                   object_name: Optional[str], column: Optional[str]) -> Tuple[str, ...]:
    """A comparable identity for one permission on either side. Granted-with-option counts as granted."""
    state = "G" if state in ("G", "W") else state
    return tuple(str(v or "").lower() for v in (grantee, class_code, permission, state, schema, object_name, column))


def expected_keys(plan: SecurityPlan, security: SqlSecurity) -> List[Tuple[str, ...]]:
    """The permission keys Fabric should hold for one principal after its plan ran."""
    out = []
    for p in security.permissions_of(plan.principal.name):
        if permission_statement(p, plan.principal.name) is not None:
            out.append(permission_key(p.grantee, p.class_code, p.permission, p.state, p.schema, p.object_name, p.column))
    return out


def masks_for(security: Optional[SqlSecurity], schema: str, table: str) -> Sequence[SqlMaskedColumn]:
    return security.masks_on(schema, table) if security is not None else ()
