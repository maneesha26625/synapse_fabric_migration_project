"""Synapse linked service -> Fabric connection.

A linked service describes *where* to connect (server, database, account) and
is stripped of its secrets by Synapse; a Fabric connection is the same plus a
credential. So the target is built from the discovered details and the
credential comes from the operator, once, in the request. It is passed
straight into the create call and never stored, logged or returned.

Only stores with a Fabric connection type are converted; anything else is
reported as needing a manual connection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: Synapse linked-service type -> (Fabric connection type, creation method).
SUPPORTED = {
    "AzureSqlDatabase": ("SQL", "SQL"),
    "AzureSqlDW": ("SQL", "SQL"),
    "AzureSynapseAnalytics": ("SQL", "SQL"),
    "SqlServer": ("SQL", "SQL"),
    "AzureBlobFS": ("AzureDataLakeStorage", "AzureDataLakeStorage"),
    "AzureBlobStorage": ("AzureBlobs", "AzureBlobs"),
}

#: Credential types per Fabric type, and the fields each needs, in the order the UI offers them:
#: the ones that pass no secret through this tool first. ``keyVault`` is a Fabric Azure Key Vault
#: reference (its alias or ID) and ``secretName`` the secret in that vault; neither is a secret.
_KV = ("keyVault", "secretName")
_STORAGE_AUTH: Dict[str, Tuple[str, ...]] = {
    "workspaceIdentity": (), "keyKeyVault": _KV, "sasKeyVault": _KV, "servicePrincipalKeyVault": ("tenantId", "clientId") + _KV,
    "key": ("key",), "servicePrincipal": ("tenantId", "clientId", "clientSecret"), "sas": ("token",),
}
AUTH_FIELDS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "SQL": {"workspaceIdentity": (), "basicKeyVault": ("username",) + _KV, "servicePrincipalKeyVault": ("tenantId", "clientId") + _KV,
            "basic": ("username", "password"), "servicePrincipal": ("tenantId", "clientId", "clientSecret")},
    "AzureDataLakeStorage": _STORAGE_AUTH,
    "AzureBlobs": _STORAGE_AUTH,
}
AUTH_LABELS = {
    "workspaceIdentity": "Workspace identity (no secret)",
    "basicKeyVault": "SQL login, password in Key Vault", "servicePrincipalKeyVault": "Service principal, secret in Key Vault",
    "keyKeyVault": "Account key in Key Vault", "sasKeyVault": "SAS token in Key Vault",
    "basic": "SQL login", "key": "Account key", "servicePrincipal": "Service principal", "sas": "SAS token",
}
#: What the operator must have set up for an option that takes no secret, shown beside it.
_KEY_VAULT_HINT = ("The secret stays in Azure Key Vault and never passes through this tool. Create an Azure Key Vault reference "
                   "in Fabric (Manage connections and gateways, Azure Key Vault references) and enter its alias or ID with the "
                   "secret's name. Fabric reads the secret's latest version each time it connects.")
_IDENTITY_GRANT = {
    "SQL": "a database admin runs CREATE USER [<Fabric workspace name>] FROM EXTERNAL PROVIDER and grants it read access "
           "(db_datareader) in the database",
    "AzureDataLakeStorage": "it is granted Storage Blob Data Reader (or Contributor) on the storage account",
    "AzureBlobs": "it is granted Storage Blob Data Reader (or Contributor) on the storage account",
}
#: A Key Vault secret name: 1-127 letters, digits and dashes.
_SECRET_NAME = re.compile(r"^[0-9A-Za-z-]{1,127}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_MAX_FIELD = 2048


def _hint(fabric_type: str, auth: str) -> str:
    if auth == "workspaceIdentity":
        return ("Nothing to enter: Fabric signs in as the workspace's own identity. It works once a workspace admin has created "
                f"the workspace identity (Workspace settings, Workspace identity) and {_IDENTITY_GRANT[fabric_type]}. "
                "Whoever runs the pipelines needs the Admin, Member or Contributor role in the workspace.")
    return _KEY_VAULT_HINT if auth.endswith("KeyVault") else ""


@dataclass
class ConnectionPlan:
    name: str
    ls_type: str
    fabric_type: Optional[str] = None
    creation_method: Optional[str] = None
    parameters: List[Dict[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    unsupported: Optional[str] = None

    @property
    def auth_types(self) -> List[str]:
        return list(AUTH_FIELDS.get(self.fabric_type or "", {}))

    def describe(self, stage: str = "connections") -> dict:
        """What the UI needs to ask for, and which stage asks. No credential, only the shape of one."""
        return {
            "name": self.name, "type": self.ls_type, "fabricType": self.fabric_type,
            "authTypes": [{"value": a, "label": AUTH_LABELS[a], "fields": list(AUTH_FIELDS[self.fabric_type][a]),
                           "hint": _hint(self.fabric_type, a)} for a in self.auth_types],
            "needsPath": self.fabric_type == "AzureDataLakeStorage",
            "unsupported": self.unsupported,
            "stage": stage,
        }


def _pairs(connection_string: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in str(connection_string or "").split(";"):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            out[key.strip().lower()] = value.strip()
    return out


def _host(value: str) -> str:
    host = re.sub(r"^(tcp:)", "", str(value or "").strip(), flags=re.I)
    return host.split(",")[0].strip()


def _plain(value: Any) -> str:
    """A string property, or '' when it is a Key Vault reference / secret object rather than a value."""
    return value.strip() if isinstance(value, str) else ""


_LS_PARAMETER = re.compile(r"@\{?\s*linkedService\(\)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}?")


def _defaults(props: Mapping[str, Any]) -> Dict[str, str]:
    """A parameterised linked service's parameter defaults, as text."""
    out: Dict[str, str] = {}
    for key, spec in (props.get("parameters") or {}).items():
        value = spec.get("defaultValue") if isinstance(spec, Mapping) else None
        if isinstance(value, (str, int, float)) and str(value).strip():
            out[str(key)] = str(value)
    return out


def _resolve(node: Any, values: Mapping[str, str], unresolved: List[str]) -> Any:
    """Replace ``@{linkedService().name}`` with the parameter's default; list any without one."""
    if isinstance(node, Mapping) and set(node) == {"value", "type"} and node.get("type") == "Expression":
        node = node.get("value")
    if isinstance(node, str):
        if "linkedService()" not in node:
            return node
        def repl(m: "re.Match[str]") -> str:
            if m.group(1) in values:
                return values[m.group(1)]
            unresolved.append(m.group(1))
            return m.group(0)
        text = _LS_PARAMETER.sub(repl, node)
        if "linkedService()" in text and not unresolved:
            unresolved.append("an expression built from parameters")
        return text
    if isinstance(node, Mapping):
        return {k: _resolve(v, values, unresolved) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, values, unresolved) for v in node]
    return node


def pool_plan(name: str, server: str, database: str) -> ConnectionPlan:
    """The Fabric SQL connection the data pipelines read a Synapse dedicated pool through."""
    plan = ConnectionPlan(name=name, ls_type="Synapse dedicated SQL pool", fabric_type="SQL", creation_method="SQL")
    plan.parameters = [{"dataType": "Text", "name": "server", "value": server},
                       {"dataType": "Text", "name": "database", "value": database}]
    return plan


def parse(payload: Mapping[str, Any]) -> ConnectionPlan:
    """The Fabric connection a linked service maps to, or why it has none."""
    props = payload.get("properties") or {}
    name = str(payload.get("name") or "")
    ls_type = str(props.get("type") or "")
    plan = ConnectionPlan(name=name, ls_type=ls_type)
    mapped = SUPPORTED.get(ls_type)
    if mapped is None:
        plan.unsupported = (f"A {ls_type or 'this kind of'} linked service has no Fabric connection type this tool can create. "
                            f"Create the connection in Fabric by hand and name it '{name}': pipelines that use it find it by that name.")
        return plan
    plan.fabric_type, plan.creation_method = mapped
    unresolved: List[str] = []
    defaults = _defaults(props)
    tp = _resolve(props.get("typeProperties") or {}, defaults, unresolved)
    if unresolved:
        plan.unsupported = (f"The linked service takes parameters with no default value ({', '.join(sorted(set(unresolved)))}), "
                            "so there is no single address to connect to. Create one Fabric connection per value by hand.")
        return plan
    if defaults and props.get("parameters"):
        plan.notes.append("Parameterised linked service: the connection uses the parameters' default values ("
                          + ", ".join(f"{k}={v}" for k, v in sorted(defaults.items()))
                          + "). If pipelines pass other values, create a connection for each of them.")
    cs = _pairs(_plain(tp.get("connectionString")))

    def param(key: str, value: str) -> None:
        plan.parameters.append({"dataType": "Text", "name": key, "value": value})

    if plan.fabric_type == "SQL":
        server = _host(_plain(tp.get("server")) or cs.get("server") or cs.get("data source") or "")
        database = _plain(tp.get("database")) or cs.get("database") or cs.get("initial catalog") or ""
        if not server or not database:
            plan.unsupported = "The server or database name is not in the linked service (it may come from a Key Vault secret or a parameter). Create the connection in Fabric by hand."
            return plan
        param("server", server)
        param("database", database)
    elif plan.fabric_type == "AzureDataLakeStorage":
        url = _plain(tp.get("url"))
        if not url:
            plan.unsupported = "The storage URL is not in the linked service. Create the connection in Fabric by hand."
            return plan
        param("server", url.rstrip("/"))
        plan.notes.append("The storage path (container or folder) is asked for when the connection is created.")
    else:  # AzureBlobs
        account = cs.get("accountname") or ""
        endpoint = _plain(tp.get("serviceEndpoint"))
        if not account and endpoint:
            m = re.match(r"https?://([^.]+)\.blob\.", endpoint)
            account = m.group(1) if m else ""
        if not account:
            plan.unsupported = "The storage account name is not in the linked service. Create the connection in Fabric by hand."
            return plan
        param("account", account)
        param("domain", "blob.core.windows.net")
    if props.get("connectVia"):
        plan.notes.append(f"Integration runtime '{(props['connectVia'] or {}).get('referenceName')}' is not carried over: Fabric connections use the cloud, or an on-premises gateway you set up separately.")
    return plan


class CredentialError(Exception):
    pass


def _auth_type(plan: ConnectionPlan, supplied: Mapping[str, Any]) -> str:
    """The chosen credential type: the one named, else the one whose fields were all filled in.

    An option with no fields (workspace identity) is never inferred: it only works once someone has
    granted the identity access, so it has to be chosen, not assumed.
    """
    named = str(supplied.get("authType") or "")
    if named:
        return named
    options = AUTH_FIELDS.get(plan.fabric_type or "", {})
    return next((a for a, fields in options.items()
                 if fields and all(str(supplied.get(f) or "").strip() for f in fields)), "")


def _key_vault_reference(plan: ConnectionPlan, values: Mapping[str, str], key_vaults: Mapping[str, str]) -> Dict[str, str]:
    """A Fabric ``KeyVaultSecretReference``: the Key Vault reference's connection ID and the secret's name."""
    ref, secret = values["keyVault"].strip(), values["secretName"].strip()
    if not _SECRET_NAME.match(secret):
        raise CredentialError(f"The Key Vault secret name for {plan.name} is the secret's name in the vault "
                              "(letters, digits and dashes), not its value.")
    if not _UUID.match(ref):
        found = key_vaults.get(ref.lower())
        if not found:
            raise CredentialError(f"No Azure Key Vault reference named '{ref}' is visible in Fabric for {plan.name}. "
                                  "Create it under Manage connections and gateways, Azure Key Vault references, or enter its ID.")
        ref = found
    return {"connectionId": ref, "secretName": secret}


def credential_body(plan: ConnectionPlan, supplied: Mapping[str, Any],
                    key_vaults: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The Fabric ``credentials`` object from what the operator chose. Raises CredentialError when incomplete.

    ``key_vaults`` maps a Key Vault reference's display name (lowercase) to its connection ID, so the
    operator can name a reference by its alias instead of its ID.
    """
    options = AUTH_FIELDS.get(plan.fabric_type or "", {})
    auth = _auth_type(plan, supplied)
    if auth not in options:
        raise CredentialError(f"Choose how to sign in to {plan.name}: {', '.join(AUTH_LABELS[a] for a in options)}.")
    values = {f: str(supplied.get(f) or "") for f in options[auth]}
    missing = [f for f, v in values.items() if not v.strip()]
    if missing:
        raise CredentialError(f"{AUTH_LABELS[auth]} for {plan.name} needs: {', '.join(missing)}.")
    if any(len(v) > _MAX_FIELD for v in values.values()):
        raise CredentialError(f"A credential value for {plan.name} is too long.")
    if auth == "workspaceIdentity":
        return {"credentialType": "WorkspaceIdentity"}
    if auth.endswith("KeyVault"):
        ref = _key_vault_reference(plan, values, key_vaults or {})
        if auth == "basicKeyVault":
            return {"credentialType": "Basic", "username": values["username"], "passwordReference": ref}
        if auth == "keyKeyVault":
            return {"credentialType": "Key", "keyReference": ref}
        if auth == "sasKeyVault":
            return {"credentialType": "SharedAccessSignature", "tokenReference": ref}
        return {"credentialType": "ServicePrincipal", "tenantId": values["tenantId"],
                "servicePrincipalClientId": values["clientId"], "servicePrincipalSecretReference": ref}
    if auth == "basic":
        return {"credentialType": "Basic", "username": values["username"], "password": values["password"]}
    if auth == "key":
        return {"credentialType": "Key", "key": values["key"]}
    if auth == "sas":
        return {"credentialType": "SharedAccessSignature", "token": values["token"]}
    return {"credentialType": "ServicePrincipal", "tenantId": values["tenantId"],
            "servicePrincipalClientId": values["clientId"], "servicePrincipalSecret": values["clientSecret"]}


def key_vault_ids(connections: Mapping[str, Mapping[str, Any]]) -> Dict[str, str]:
    """Fabric connections by lowercase display name -> ID, for naming a Key Vault reference by its alias."""
    return {str(name).lower(): str(c["id"]) for name, c in connections.items() if c.get("id")}


def create_body(plan: ConnectionPlan, supplied: Mapping[str, Any],
                key_vaults: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The ``POST /v1/connections`` body. The credential goes in here and nowhere else."""
    parameters = [dict(p) for p in plan.parameters]
    path = str(supplied.get("path") or "").strip()
    if plan.fabric_type == "AzureDataLakeStorage":
        if not path:
            raise CredentialError(f"Enter the container or folder path for {plan.name} (for example: mycontainer).")
        parameters.append({"dataType": "Text", "name": "path", "value": path})
    credentials = credential_body(plan, supplied, key_vaults)
    return {
        "connectivityType": "ShareableCloud",
        "displayName": plan.name,
        "connectionDetails": {"type": plan.fabric_type, "creationMethod": plan.creation_method, "parameters": parameters},
        "privacyLevel": "Organizational",
        "credentialDetails": {
            "singleSignOnType": "None",
            "connectionEncryption": "NotEncrypted",
            # Fabric cannot test a workspace-identity connection, so asking it to fails the create.
            "skipTestConnection": credentials["credentialType"] == "WorkspaceIdentity",
            "credentials": credentials,
        },
    }
