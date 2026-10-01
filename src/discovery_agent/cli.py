"""Command-line interface for the Discovery Agent.

The operator says *where* to look. The tool works out *what is there* -- that
is what discovery means, so nothing here asks which pipelines, notebooks or
tables exist, and there is no flag with which to name one.

Nothing here accepts a credential either. Git authenticates with the
machine's own credential helper or SSH key; Azure authenticates with the
sign-in the operator already performed. There is no ``--token``,
``--password`` or ``--pat``, and no environment variable is read for one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from discovery_agent import __version__, discovery
from discovery_agent.config import DEFAULT_MAX_FILE_BYTES, DEFAULT_OUTPUT_DIR, DiscoveryConfig
from discovery_agent.errors import ConfigError, DiscoveryError
from discovery_agent.source_strategy import P0Artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discovery-agent",
        description="Discover the Synapse estate: repository artifacts, live "
        "workspace artifacts and dedicated SQL pool objects. Read-only.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    source = parser.add_argument_group(
        "repository", "Where the committed artifact definitions are."
    )
    source.add_argument(
        "--source",
        type=Path,
        help="Path to a local clone of the Synapse repository. Use this or "
        "--repository-url, not both.",
    )
    source.add_argument(
        "--repository-url",
        help="Repository to acquire and scan, e.g. https://github.com/<owner>/<repo>. "
        "Authentication is your own git credential helper or SSH key; this "
        "tool never accepts a token.",
    )
    source.add_argument(
        "--ref",
        help="Branch, tag or commit to acquire. Defaults to the repository's "
        "default branch. Only valid with --repository-url.",
    )
    source.add_argument(
        "--input-root",
        type=Path,
        default=None,
        help="Directory to acquire into. Only valid with --repository-url.",
    )

    live = parser.add_argument_group(
        "live sources",
        "Where the running estate is. Discovery enumerates what is there; "
        "you never list artifacts yourself.",
    )
    live.add_argument(
        "--workspace",
        help="Synapse workspace name. Enables live artifact discovery over "
        "the Synapse Artifacts API.",
    )
    live.add_argument(
        "--resource-group",
        help="Resource group holding the workspace. Required with --workspace.",
    )
    live.add_argument(
        "--subscription",
        help="Azure subscription id. Optional: it is needed to read workspace "
        "and pool metadata from Azure Resource Manager, which is also what "
        "proves a repository belongs to a workspace.",
    )
    live.add_argument(
        "--tenant",
        help="Entra tenant id, for a machine signed in to more than one.",
    )
    live.add_argument(
        "--credential-method",
        choices=("azure_cli", "interactive_browser"),
        default="azure_cli",
        help="How to sign in to Azure: the existing `az login` (default), or "
        "an interactive browser sign-in that leaves the Azure CLI session "
        "untouched. Needs --subscription.",
    )
    live.add_argument(
        "--client-id",
        help="Application (client) id for interactive_browser, only for a "
        "tenant that has not consented to Microsoft's default sign-in app. "
        "An identifier, not a secret.",
    )
    live.add_argument(
        "--sql-pool",
        help="Dedicated SQL pool to read tables, views and stored procedures "
        "from. Requires --workspace.",
    )

    output = parser.add_argument_group("output and filtering")
    output.add_argument(
        "--out",
        type=Path,
        default=Path(DEFAULT_OUTPUT_DIR),
        help=f"Directory to write discovery output into (default: {DEFAULT_OUTPUT_DIR}).",
    )
    output.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="Only scan paths matching this glob. Repeatable.",
    )
    output.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Skip paths matching this glob. Repeatable.",
    )
    output.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help="Skip source files larger than this.",
    )
    output.add_argument(
        "--on-parse-error",
        choices=("skip", "fail"),
        default="skip",
        help="Record and continue, or abort the run, when a file cannot be parsed.",
    )
    output.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the run summary as JSON instead of text.",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> DiscoveryConfig:
    """The run configuration.

    ``source`` is a placeholder when the repository is being acquired: the
    real path is not known until acquisition has finished, so
    ``discovery.run`` replaces it. Nothing validates this configuration before
    that happens.
    """
    return DiscoveryConfig(
        source=args.source if args.source is not None else Path("."),
        out=args.out,
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        max_file_bytes=args.max_file_bytes,
        on_parse_error=args.on_parse_error,
    )


def check_arguments(args: argparse.Namespace) -> None:
    """Refuse an incomplete or contradictory combination, with the reason.

    Two repository modes exist and they are not mixable: scan a clone you
    already have, or acquire one. Silently preferring either when both are
    given would scan something the operator did not ask for.
    """
    if args.source is None and args.repository_url is None and args.workspace is None:
        raise ConfigError(
            "nothing to scan: pass --source <path> for a local clone, "
            "--repository-url <url> to acquire one, or --workspace <name> to "
            "discover a live workspace"
        )
    if args.source is not None and args.repository_url is not None:
        raise ConfigError(
            "--source and --repository-url are alternatives: --source scans a "
            "clone you already have, --repository-url acquires one"
        )
    if args.source is not None:
        for flag, value in (("--ref", args.ref), ("--input-root", args.input_root)):
            if value is not None:
                raise ConfigError(
                    f"{flag} applies to --repository-url; a local --source run "
                    f"acquires nothing"
                )
    if args.workspace and not args.resource_group:
        raise ConfigError(
            "--workspace needs --resource-group: a workspace name is only "
            "unique within one"
        )
    if args.resource_group and not args.workspace:
        raise ConfigError("--resource-group applies to --workspace")
    if args.sql_pool and not args.workspace:
        raise ConfigError(
            "--sql-pool needs --workspace: the pool's endpoint is derived "
            "from the workspace it belongs to"
        )

def connections_for(args: argparse.Namespace):
    """A ConnectionManager, only when connected configuration was supplied.

    Returns None for a purely local ``--source`` run, and the import stays
    inside this function so that run never loads the connection layer at all.
    A repository-only scan must not acquire a credential requirement, and the
    cheapest way to guarantee that is for the code that could to remain
    unimported.

    Each section is independent. A Git-only run needs no subscription and
    requests none; a workspace-only run needs no repository. An unconfigured
    section is reported as not configured rather than failing.
    """
    if args.repository_url is None and args.workspace is None:
        return None

    from discovery_agent.connections.manager import ConnectionManager
    from discovery_agent.connections.models import (
        AzureConnectionConfig,
        ConnectionSettings,
        CredentialMethod,
        GitRepositoryConfig,
        SynapseConnectionConfig,
    )

    git = (
        GitRepositoryConfig(repository_url=args.repository_url, ref=args.ref)
        if args.repository_url
        else None
    )
    azure = (
        AzureConnectionConfig(
            subscription_id=args.subscription,
            tenant_id=args.tenant,
            credential_method=CredentialMethod(args.credential_method),
            client_id=args.client_id,
        )
        if args.subscription
        else None
    )
    synapse = (
        SynapseConnectionConfig(
            resource_group=args.resource_group,
            workspace_name=args.workspace,
            sql_pool_name=args.sql_pool,
        )
        if args.workspace
        else None
    )
    return ConnectionManager(
        ConnectionSettings(azure=azure, synapse=synapse, git=git)
    )


def render(run: discovery.DiscoveryRun) -> str:
    """The console summary. Contains no credential, by construction.

    Every line comes from a result object: a count, a path, a commit SHA or a
    name. There is no other source of text here.
    """
    summary = run.summary()
    lines = []

    snapshot = run.snapshot
    if snapshot is not None:
        lines.append(f"repository: {snapshot.repository_url}")
        lines.append(f"  ref: {snapshot.ref}")
        lines.append(f"  commit: {snapshot.commit_sha}")
        lines.append(f"  reused: {'yes' if snapshot.reused else 'no'}")
    lines.append(f"source: {summary['root']}")
    lines.append(
        f"files walked: {summary['files_walked']} "
        f"(skipped {summary['files_skipped']})"
    )
    lines.append(
        f"artifacts detected: {summary['artifacts_detected']} "
        f"({summary['synapse_artifacts']} synapse)"
    )

    by_status = summary["extraction_by_status"]
    if by_status:
        rendered = ", ".join(f"{status} {count}" for status, count in by_status.items())
        lines.append(f"extraction: {rendered}")
    lines.append(f"references observed: {summary['references_observed']}")

    workspace = run.synapse
    if workspace is not None:
        lines.append(f"workspace: {workspace.workspace}")
        for artifact, count in workspace.counts_by_artifact().items():
            lines.append(f"  {artifact}: {count}")
        for unreachable in workspace.unreachable:
            # Never a count. An endpoint that did not answer says nothing
            # about how many artifacts are behind it.
            lines.append(f"  {unreachable.value}: not listed")

    catalog = run.catalog
    if catalog is not None:
        lines.append(f"sql database: {catalog.database.name}")
        lines.append(f"  tables: {catalog.table_count}")
        lines.append(f"  views: {catalog.view_count}")
        lines.append(f"  stored procedures: {catalog.procedure_count}")
        for opaque in catalog.opaque_modules:
            lines.append(f"  definition withheld: {opaque}")
        if not catalog.is_complete:
            lines.append(
                "  note: the catalog run has gaps; counts are what the "
                "connected identity could see"
            )

    lines.append("")
    lines.append(f"P0 coverage ({len(run.records.records)} records):")
    for artifact in P0Artifact:
        entry = summary["p0_coverage"][artifact.value]
        sources = ", ".join(entry["sources"]) or "none"
        lines.append(
            f"  {artifact.value:<22} {entry['records']:>4}  "
            f"primary={entry['primary_source']:<10} sources={sources}"
        )

    if run.resolution is not discovery.IdentityResolution.NOT_APPLICABLE:
        lines.append("")
        lines.append(f"cross-source identity: {run.resolution.value}")
        lines.append(f"  {run.resolution_reason}")

    issues = run.all_issues
    if issues:
        lines.append("")
        lines.append(f"issues: {len(issues)}")
        for issue in issues[:10]:
            lines.append(f"  [{issue.code.value}] {issue.message}")
        if len(issues) > 10:
            lines.append(f"  ... and {len(issues) - 10} more")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        check_arguments(args)
        run = discovery.run(
            config_from_args(args),
            connections=connections_for(args),
            input_root=args.input_root,
            # A workspace-only run has no repository to walk, and must not
            # fall back to scanning whatever directory it was started in.
            scan_repository=bool(args.source or args.repository_url),
        )
    except DiscoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(run.summary(), indent=2, sort_keys=True))
    else:
        print(render(run), end="")
    return 0
