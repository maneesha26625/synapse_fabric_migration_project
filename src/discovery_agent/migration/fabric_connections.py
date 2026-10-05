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

#: Credential types per Fabric type, and the fields each needs (name -> label).
AUTH_FIELDS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "SQL": {"basic": ("username", "password"), "servicePrincipal": ("tenantId", "clientId", "clientSecret")},
    "AzureDataLakeStorage": {"key": ("key",), "servicePrincipal": ("tenantId", "clientId", "clientSecret"), "sas": ("token",)},
    "AzureBlobs": {"key": ("key",), "servicePrincipal": ("tenantId", "clientId", "clientSecret"), "sas": ("token",)},
}
AUTH_LABELS = {"basic": "SQL login", "key": "Account key", "servicePrincipal": "Service principal", "sas": "SAS token"}
_MAX_FIELD = 2048


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

    def describe(self) -> dict:
        """What the UI needs to ask for. No credential, only the shape of one."""
        return {
            "name": self.name, "type": self.ls_type, "fabricType": self.fabric_type,
            "authTypes": [{"value": a, "label": AUTH_LABELS[a], "fields": list(AUTH_FIELDS[self.fabric_type][a])} for a in self.auth_types],
            "needsPath": self.fabric_type == "AzureDataLakeStorage",
            "unsupported": self.unsupported,
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


def parse(payload: Mapping[str, Any]) -> ConnectionPlan:
    """The Fabric connection a linked service maps to, or why it has none."""
    props = payload.get("properties") or {}
    name = str(payload.get("name") or "")
    ls_type = str(props.get("type") or "")
    plan = ConnectionPlan(name=name, ls_type=ls_type)
    mapped = SUPPORTED.get(ls_type)
    if mapped is None:
        plan.unsupported = f"A {ls_type or 'this kind of'} linked service has no Fabric connection type this tool can create. Create the connection in Fabric by hand."
        return plan
    plan.fabric_type, plan.creation_method = mapped
    tp = props.get("typeProperties") or {}
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


def credential_body(plan: ConnectionPlan, supplied: Mapping[str, Any]) -> Dict[str, Any]:
    """The Fabric ``credentials`` object from what the operator typed. Raises CredentialError when incomplete."""
    options = AUTH_FIELDS.get(plan.fabric_type or "", {})
    auth = str(supplied.get("authType") or (plan.auth_types[0] if plan.auth_types else ""))
    if auth not in options:
        raise CredentialError(f"Choose how to sign in to {plan.name}: {', '.join(AUTH_LABELS[a] for a in options)}.")
    values = {f: str(supplied.get(f) or "") for f in options[auth]}
    missing = [f for f, v in values.items() if not v.strip()]
    if missing:
        raise CredentialError(f"{AUTH_LABELS[auth]} for {plan.name} needs: {', '.join(missing)}.")
    if any(len(v) > _MAX_FIELD for v in values.values()):
        raise CredentialError(f"A credential value for {plan.name} is too long.")
    if auth == "basic":
        return {"credentialType": "Basic", "username": values["username"], "password": values["password"]}
    if auth == "key":
        return {"credentialType": "Key", "key": values["key"]}
    if auth == "sas":
        return {"credentialType": "SharedAccessSignature", "token": values["token"]}
    return {"credentialType": "ServicePrincipal", "tenantId": values["tenantId"],
            "servicePrincipalClientId": values["clientId"], "servicePrincipalSecret": values["clientSecret"]}


def create_body(plan: ConnectionPlan, supplied: Mapping[str, Any]) -> Dict[str, Any]:
    """The ``POST /v1/connections`` body. The credential goes in here and nowhere else."""
    parameters = [dict(p) for p in plan.parameters]
    path = str(supplied.get("path") or "").strip()
    if plan.fabric_type == "AzureDataLakeStorage":
        if not path:
            raise CredentialError(f"Enter the container or folder path for {plan.name} (for example: mycontainer).")
        parameters.append({"dataType": "Text", "name": "path", "value": path})
    return {
        "connectivityType": "ShareableCloud",
        "displayName": plan.name,
        "connectionDetails": {"type": plan.fabric_type, "creationMethod": plan.creation_method, "parameters": parameters},
        "privacyLevel": "Organizational",
        "credentialDetails": {
            "singleSignOnType": "None",
            "connectionEncryption": "NotEncrypted",
            "skipTestConnection": False,
            "credentials": credential_body(plan, supplied),
        },
    }
