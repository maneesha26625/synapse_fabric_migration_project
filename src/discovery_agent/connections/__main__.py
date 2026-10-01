"""Dev command: validate the whole connection chain, end to end.

    python -m discovery_agent.connections \
        --subscription-id <guid> \
        --resource-group <rg> \
        --workspace <workspace> \
        --sql-pool <pool> \
        --repository-url https://github.com/<owner>/<repo>

Walks the chain in dependency order and prints one line per connection:

    Azure -> Synapse workspace -> dedicated SQL pool -> SQL session -> Git

Nothing printed here is sensitive. Every line is rendered from a
``ConnectionValidation``, whose message and details are redacted on
construction, and no code path in this file can reach a token: the manager
holds the credential and this module never asks it for one.

Separate from the ``discovery-agent`` command for the same reason
``python -m discovery_agent.sql`` is: that command is repository-only, works
offline and needs no credentials, and it must not acquire a credential
requirement by accident.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from discovery_agent.connections.manager import ConnectionManager
from discovery_agent.connections.models import (
    ENV_REPOSITORY_REF,
    ENV_REPOSITORY_URL,
    ENV_RESOURCE_GROUP,
    ENV_SQL_POOL,
    ENV_SUBSCRIPTION_ID,
    ENV_TENANT_ID,
    ENV_WORKSPACE_NAME,
    AzureConnectionConfig,
    ConnectionSettings,
    GitRepositoryConfig,
    SynapseConnectionConfig,
    settings_from_environment,
)
from discovery_agent.connections.validation import ValidationReport, ValidationStatus
from discovery_agent.errors import DiscoveryError
from discovery_agent.sql.config import ENV_DATABASE, ENV_SERVER

_MARKS = {
    ValidationStatus.OK: "OK",
    ValidationStatus.FAILED: "FAIL",
    ValidationStatus.SKIPPED: "SKIP",
}

#: Display names, so the summary reads as the chain an operator is checking
#: rather than as the enum values the code happens to use.
_LABELS = {
    "azure": "Azure",
    "synapse": "Synapse",
    "sql": "SQL",
    "repository": "Git",
}

#: Detail keys worth putting on screen, per connection, in this order. A
#: whitelist rather than "print everything": it keeps the summary readable,
#: and it means a detail added to a result later cannot silently appear in
#: console output that someone pastes into a ticket.
_SHOWN_DETAILS = {
    "azure": (
        "subscription_id",
        "subscription_name",
        "subscription_state",
        "tenant_id",
        "credential",
    ),
    "synapse": (
        "workspace_name",
        "workspace_id",
        "location",
        "provisioning_state",
        "sql_endpoint",
        "sql_pool",
        "sql_pool_status",
        "sql_pool_sku",
    ),
    "sql": (
        "server",
        "connected_database",
        "principal",
        "authentication",
        "driver",
    ),
    "repository": (
        "repository_url",
        "provider",
        "transport",
        "authentication",
        "repository",
        "ref",
        "reachable",
        "refs_visible",
        "git_client",
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m discovery_agent.connections",
        description=(
            "Validate the connection configuration: Azure, the Synapse "
            "workspace, the dedicated SQL pool and database, the SQL session, "
            "and the git repository. Read-only; prints no credentials."
        ),
    )
    parser.add_argument(
        "--subscription-id", help=f"Azure subscription id (or set {ENV_SUBSCRIPTION_ID})."
    )
    parser.add_argument(
        "--tenant-id",
        help=f"Azure tenant id, if the CLI is signed in to several "
        f"(or set {ENV_TENANT_ID}).",
    )
    parser.add_argument(
        "--resource-group",
        help=f"Resource group holding the workspace (or set {ENV_RESOURCE_GROUP}).",
    )
    parser.add_argument(
        "--workspace", help=f"Synapse workspace name (or set {ENV_WORKSPACE_NAME})."
    )
    parser.add_argument(
        "--sql-pool", help=f"Dedicated SQL pool name (or set {ENV_SQL_POOL})."
    )
    parser.add_argument(
        "--database",
        help=f"Database to connect to; defaults to the pool name "
        f"(or set {ENV_DATABASE}).",
    )
    parser.add_argument(
        "--sql-endpoint",
        help=f"SQL endpoint host; defaults to <workspace>.sql.azuresynapse.net "
        f"(or set {ENV_SERVER}).",
    )
    parser.add_argument(
        "--repository-url", help=f"Source repository URL (or set {ENV_REPOSITORY_URL})."
    )
    parser.add_argument(
        "--ref", help=f"Branch, tag or commit to check (or set {ENV_REPOSITORY_REF})."
    )
    parser.add_argument(
        "--skip-remote-probe",
        action="store_true",
        help="Do not contact the git remote; only check the URL and client.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the structured report instead of the rendered summary.",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> ConnectionSettings:
    """Command-line arguments over environment variables, section by section.

    The environment is the fallback rather than the interface: this is exactly
    the object a UI will build, and the command exists to prove it can be
    built and used without one.
    """
    base = settings_from_environment()

    azure = base.azure
    if args.subscription_id or args.tenant_id:
        subscription = args.subscription_id or (
            base.azure.subscription_id if base.azure else ""
        )
        tenant = args.tenant_id or (base.azure.tenant_id if base.azure else None)
        method = base.azure.credential_method if base.azure else None
        azure = AzureConnectionConfig(
            subscription_id=subscription,
            tenant_id=tenant,
            **({"credential_method": method} if method else {}),
        )

    synapse = base.synapse
    if args.resource_group or args.workspace or args.sql_pool or args.database or args.sql_endpoint:
        existing = base.synapse
        synapse = SynapseConnectionConfig(
            resource_group=args.resource_group
            or (existing.resource_group if existing else ""),
            workspace_name=args.workspace
            or (existing.workspace_name if existing else ""),
            workspace_id=existing.workspace_id if existing else None,
            sql_pool_name=args.sql_pool or (existing.sql_pool_name if existing else None),
            database_name=args.database
            or (existing.database_name if existing else None),
            sql_endpoint=args.sql_endpoint
            or (existing.sql_endpoint if existing else None),
        )

    git = base.git
    if args.repository_url or args.ref:
        url = args.repository_url or (base.git.repository_url if base.git else "")
        ref = args.ref or (base.git.ref if base.git else None)
        git = GitRepositoryConfig(repository_url=url, ref=ref)

    return ConnectionSettings(azure=azure, synapse=synapse, git=git)


def render(report: ValidationReport) -> str:
    """The console summary. Contains no credential, by construction.

    Every line comes from a ``ConnectionValidation``, which redacted its
    message and its details when it was built. There is no other source of
    text here.
    """
    lines: List[str] = []
    for result in report.results:
        kind = result.connection.value
        lines.append(f"[{_MARKS[result.status]}] {_LABELS.get(kind, kind)}")
        if result.category is not None:
            # Before the message: the category is what an operator acts on,
            # and the sentence is the detail behind it.
            lines.append(f"     category: {result.category.value.upper()}")
        lines.append(f"     message: {result.message}")
        for key in _SHOWN_DETAILS.get(kind, ()):
            value = result.details.get(key)
            if value:
                lines.append(f"     {key}: {value}")
        lines.append("")

    def named(results):
        return ", ".join(
            sorted(_LABELS.get(r.connection.value, r.connection.value) for r in results)
        )

    if report.ok:
        lines.append("Overall: READY")
    else:
        lines.append("Overall: NOT READY")
        if report.failures:
            lines.append(f"  failed: {named(report.failures)}")
        not_attempted = [
            r for r in report.results if r.status is ValidationStatus.SKIPPED
        ]
        if not_attempted:
            lines.append(f"  not attempted: {named(not_attempted)}")
    return "\n".join(lines).rstrip() + "\n"


def _has_any(settings: ConnectionSettings) -> bool:
    return any((settings.azure, settings.synapse, settings.git))


def main(argv: Optional[List[str]] = None, manager: Optional[ConnectionManager] = None) -> int:
    """Validate and print. Exit 0 if every configured connection passed.

    ``manager`` is injectable so the command itself is testable without a
    subscription, a driver or a network -- the same seam the SQL dev command
    provides through its ``connector`` argument.
    """
    args = build_parser().parse_args(argv)
    try:
        if manager is None:
            settings = settings_from_args(args)
            if not _has_any(settings):
                print(
                    "error: nothing is configured to validate; pass "
                    "--subscription-id, --workspace or --repository-url, or "
                    "set the matching environment variables",
                    file=sys.stderr,
                )
                return 2
            manager = ConnectionManager(settings)
        report = manager.validate_all(probe_remote=not args.skip_remote_probe)
    except DiscoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(render(report), end="")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
