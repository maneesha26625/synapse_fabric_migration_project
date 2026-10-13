"""Database security, as the catalog describes it: principals, role members,
explicit permissions and dynamic data masks.

These are what the Fabric Migration Assistant for Data Warehouse carries over as
"security objects". The models are plain facts read from four catalog views;
deciding what Fabric can recreate, and how, is ``migration.security``'s job.

Each part degrades on its own. A principal who may read ``sys.database_principals``
but not ``sys.database_permissions`` gets every role and user with an issue saying
permissions are unknown -- never an empty permission list that reads as "nobody was
granted anything".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from discovery_agent.extractors.models import ExtractionIssue, IssueCode

#: ``sys.database_principals.type`` values this module distinguishes.
SQL_USER, ENTRA_USER, ENTRA_GROUP, ROLE = "S", "E", "X", "R"
#: Windows users and groups (U, G) do not occur in a dedicated pool but are Entra-like if they do.
_EXTERNAL = {ENTRA_USER, ENTRA_GROUP, "U", "G"}

#: Principals every database has; they are never migrated as such.
BUILT_IN_PRINCIPALS = {"dbo", "guest", "information_schema", "sys"}
#: The public role: it exists everywhere, so only its permissions move.
PUBLIC = "public"
#: Fixed database roles: they exist in a Fabric Warehouse too, so only their members move.
FIXED_ROLES = {
    "db_owner", "db_accessadmin", "db_securityadmin", "db_ddladmin", "db_backupoperator",
    "db_datareader", "db_datawriter", "db_denydatareader", "db_denydatawriter",
}
#: Synapse workload-management roles (resource classes). Fabric has no resource classes:
#: the Warehouse sizes each query itself, so these roles and their members are dropped.
RESOURCE_CLASS_ROLES = {
    "smallrc", "mediumrc", "largerc", "xlargerc",
    "staticrc10", "staticrc20", "staticrc30", "staticrc40", "staticrc50", "staticrc60", "staticrc70", "staticrc80",
}

#: ``sys.database_permissions.class`` values read: the database, an object or column, a schema.
CLASS_DATABASE, CLASS_OBJECT, CLASS_SCHEMA = 0, 1, 3
#: ``sys.database_permissions.state``: granted, granted with grant option, denied.
GRANTED, GRANTED_WITH_OPTION, DENIED = "G", "W", "D"


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SqlPrincipal:
    """One database user or role."""

    name: str
    type_code: str
    type_desc: Optional[str] = None
    principal_id: Optional[int] = None

    @property
    def is_role(self) -> bool:
        return self.type_code == ROLE

    @property
    def is_sql_user(self) -> bool:
        """A user signed in with SQL authentication, which Fabric does not have."""
        return self.type_code == SQL_USER

    @property
    def is_external(self) -> bool:
        """A Microsoft Entra user or group: Fabric can grant to it by name."""
        return self.type_code in _EXTERNAL

    @property
    def is_built_in(self) -> bool:
        return self.name.lower() in BUILT_IN_PRINCIPALS

    @property
    def is_fixed_role(self) -> bool:
        return self.is_role and self.name.lower() in FIXED_ROLES

    @property
    def is_resource_class(self) -> bool:
        return self.is_role and self.name.lower() in RESOURCE_CLASS_ROLES

    @property
    def is_public(self) -> bool:
        return self.is_role and self.name.lower() == PUBLIC

    @property
    def kind_label(self) -> str:
        if self.is_role:
            return "Database role"
        if self.is_sql_user:
            return "SQL user"
        if self.type_code == ENTRA_GROUP or self.type_code == "G":
            return "Microsoft Entra group"
        if self.is_external:
            return "Microsoft Entra user"
        return self.type_desc or "Database principal"

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type_code, "typeDesc": self.type_desc, "kind": self.kind_label}


@dataclass(frozen=True)
class SqlRoleMember:
    role: str
    member: str
    member_type: str

    def to_dict(self) -> dict:
        return {"role": self.role, "member": self.member, "memberType": self.member_type}


@dataclass(frozen=True)
class SqlPermission:
    """One explicit GRANT or DENY, on the database, a schema, an object or a column."""

    grantee: str
    grantee_type: str
    permission: str
    state: str
    class_code: int
    schema: Optional[str] = None
    object_name: Optional[str] = None
    object_type: Optional[str] = None
    column: Optional[str] = None

    @property
    def is_deny(self) -> bool:
        return self.state == DENIED

    @property
    def with_grant_option(self) -> bool:
        return self.state == GRANTED_WITH_OPTION

    @property
    def securable(self) -> str:
        """A readable name for what the permission is on."""
        if self.class_code == CLASS_SCHEMA:
            return f"schema {self.schema}"
        if self.class_code == CLASS_OBJECT:
            name = f"{self.schema}.{self.object_name}"
            return f"{name} ({self.column})" if self.column else name
        return "the database"

    def to_dict(self) -> dict:
        return {
            "grantee": self.grantee, "permission": self.permission,
            "state": {GRANTED: "GRANT", GRANTED_WITH_OPTION: "GRANT WITH GRANT OPTION", DENIED: "DENY"}.get(self.state, self.state),
            "on": self.securable,
        }


@dataclass(frozen=True)
class SqlMaskedColumn:
    schema: str
    table: str
    column: str
    function: str

    def to_dict(self) -> dict:
        return {"table": f"{self.schema}.{self.table}", "column": self.column, "function": self.function}


@dataclass(frozen=True)
class SqlSecurity:
    """Everything the catalog says about who may do what, and what it could not read."""

    principals: Tuple[SqlPrincipal, ...] = ()
    members: Tuple[SqlRoleMember, ...] = ()
    permissions: Tuple[SqlPermission, ...] = ()
    masks: Tuple[SqlMaskedColumn, ...] = ()
    #: Which parts could be read. False means "unknown", never "none".
    principals_readable: bool = True
    members_readable: bool = True
    permissions_readable: bool = True
    masks_readable: bool = True
    issues: Tuple[ExtractionIssue, ...] = field(default=())

    def principal(self, name: str) -> Optional[SqlPrincipal]:
        lowered = name.lower()
        return next((p for p in self.principals if p.name.lower() == lowered), None)

    def members_of(self, role: str) -> Tuple[SqlRoleMember, ...]:
        lowered = role.lower()
        return tuple(m for m in self.members if m.role.lower() == lowered)

    def roles_of(self, member: str) -> Tuple[str, ...]:
        lowered = member.lower()
        return tuple(m.role for m in self.members if m.member.lower() == lowered)

    def permissions_of(self, grantee: str) -> Tuple[SqlPermission, ...]:
        lowered = grantee.lower()
        return tuple(p for p in self.permissions if p.grantee.lower() == lowered)

    def masks_on(self, schema: str, table: str) -> Tuple[SqlMaskedColumn, ...]:
        return tuple(m for m in self.masks if m.schema.lower() == schema.lower() and m.table.lower() == table.lower())

    def summary(self) -> dict:
        return {
            "roles": sum(1 for p in self.principals if p.is_role and not p.is_fixed_role and not p.is_public),
            "users": sum(1 for p in self.principals if not p.is_role and not p.is_built_in),
            "sqlUsers": sum(1 for p in self.principals if p.is_sql_user and not p.is_built_in),
            "permissions": len(self.permissions),
            "maskedColumns": len(self.masks),
        }


def build_security(
    database: str,
    principal_rows: Sequence[Mapping[str, Any]] = (),
    member_rows: Sequence[Mapping[str, Any]] = (),
    permission_rows: Sequence[Mapping[str, Any]] = (),
    mask_rows: Sequence[Mapping[str, Any]] = (),
    *,
    principals_readable: bool = True,
    members_readable: bool = True,
    permissions_readable: bool = True,
    masks_readable: bool = True,
    issues: Sequence[ExtractionIssue] = (),
) -> SqlSecurity:
    """Assemble the security catalog from its rows. Pure; rows without a name are counted, not kept."""
    own: List[ExtractionIssue] = list(issues)
    principals: List[SqlPrincipal] = []
    for row in principal_rows:
        name, type_code = _text(row.get("principal_name")), _text(row.get("type_code"))
        if name and type_code:
            principals.append(SqlPrincipal(name, type_code.upper(), _text(row.get("type_desc")), _int(row.get("principal_id"))))

    members = [SqlRoleMember(r, m, (_text(row.get("member_type")) or "").upper())
               for row in member_rows
               for r, m in [(_text(row.get("role_name")), _text(row.get("member_name")))] if r and m]

    permissions: List[SqlPermission] = []
    unresolved = 0
    for row in permission_rows:
        grantee, permission = _text(row.get("grantee_name")), _text(row.get("permission_name"))
        class_code, state = _int(row.get("class_code")), (_text(row.get("state_code")) or "").upper()
        if not grantee or not permission or class_code is None or state not in (GRANTED, GRANTED_WITH_OPTION, DENIED):
            unresolved += 1
            continue
        schema, obj = _text(row.get("schema_name")), _text(row.get("object_name"))
        if (class_code == CLASS_OBJECT and not (schema and obj)) or (class_code == CLASS_SCHEMA and not schema):
            unresolved += 1  # an object or schema the principal cannot see by name
            continue
        permissions.append(SqlPermission(
            grantee=grantee, grantee_type=(_text(row.get("grantee_type")) or "").upper(), permission=permission.upper(),
            state=state, class_code=class_code, schema=schema, object_name=obj,
            object_type=_text(row.get("object_type")), column=_text(row.get("column_name")),
        ))
    if unresolved:
        own.append(ExtractionIssue(
            IssueCode.MISSING_INFORMATION,
            f"{unresolved} permission row(s) could not be tied to a named principal and securable and were not kept",
            f"{database}.permissions",
        ))

    masks = [SqlMaskedColumn(s, t, c, f)
             for row in mask_rows
             for s, t, c, f in [(_text(row.get("schema_name")), _text(row.get("table_name")),
                                 _text(row.get("column_name")), _text(row.get("masking_function")))]
             if s and t and c and f]

    return SqlSecurity(
        principals=tuple(principals), members=tuple(members), permissions=tuple(permissions), masks=tuple(masks),
        principals_readable=principals_readable, members_readable=members_readable,
        permissions_readable=permissions_readable, masks_readable=masks_readable, issues=tuple(own),
    )


def security_by_principal(security: SqlSecurity) -> Dict[str, Dict[str, Any]]:
    """For display: each principal with its roles, members and permissions."""
    out: Dict[str, Dict[str, Any]] = {}
    for p in security.principals:
        out[p.name] = {
            **p.to_dict(),
            "memberOf": list(security.roles_of(p.name)),
            "members": [m.member for m in security.members_of(p.name)] if p.is_role else [],
            "permissions": [x.to_dict() for x in security.permissions_of(p.name)],
        }
    return out
