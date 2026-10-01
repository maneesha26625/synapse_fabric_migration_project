"""Dev command: connect to a dedicated SQL pool and print what was discovered.

    python -m discovery_agent.sql --server <host> --database <db>

Separate from the ``discovery-agent`` CLI on purpose. That command is
repository-only, works offline and needs no credentials; this one opens a
network connection and signs in. Keeping them apart means the offline path
cannot acquire a credential requirement by accident.

Nothing printed here is sensitive. The summary names a server, a database, an
authentication mechanism and the identity that connected — never a token, a
password or a connection string.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from discovery_agent.errors import DiscoveryError
from discovery_agent.sql.config import (
    ENV_DATABASE,
    ENV_SERVER,
    SqlConnectionConfig,
    config_from_environment,
)
from discovery_agent.sql.connection import Connector
from discovery_agent.sql.dedicated_pool import DedicatedPoolSource
from discovery_agent.sql.result import SqlCatalogDiscovery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m discovery_agent.sql",
        description=(
            "Connect to a Synapse Dedicated SQL Pool and print a summary of "
            "the tables discovered. Read-only; runs only fixed catalog "
            "queries."
        ),
    )
    parser.add_argument(
        "--server",
        help=f"Pool endpoint host, e.g. <workspace>.sql.azuresynapse.net "
        f"(or set {ENV_SERVER}).",
    )
    parser.add_argument(
        "--database",
        help=f"Database (pool) to discover (or set {ENV_DATABASE}).",
    )
    parser.add_argument(
        "--show-columns",
        action="store_true",
        help="List each table's columns as well as the count.",
    )
    return parser


def render(discovery: SqlCatalogDiscovery, show_columns: bool = False) -> str:
    """The console summary. Contains no credential, by construction.

    Every value here comes from a catalog row or from non-secret
    configuration; there is no code path that could place a token in it.
    """
    lines: List[str] = [
        f"server: {discovery.database.server}",
        f"database: {discovery.database.name}",
        f"identity: {discovery.principal.described}",
        f"tables discovered: {discovery.table_count}",
        "",
    ]

    for table in discovery.tables:
        lines.append(f"{table.key.schema}.{table.key.name}")
        lines.append(f"  columns: {table.column_count}")
        if table.is_external:
            # Printed only when true: a reader scanning for migration effort
            # needs external tables to stand out, and annotating every ordinary
            # table with "external: no" would bury them.
            lines.append("  external: yes")
        lines.append(f"  storage: {table.storage.value}")
        base = table.base_index
        if base is not None and base.ordering_columns:
            order = ", ".join(
                c.name or f"column_id {c.column_id}" for c in base.ordering_columns
            )
            lines.append(f"  order by: {order}")
        for index in table.secondary_indexes:
            flags = [
                label
                for label, on in (
                    ("primary key", index.is_primary_key),
                    ("unique constraint", index.is_unique_constraint),
                    ("unique", index.is_unique and not index.is_primary_key),
                )
                if on
            ]
            suffix = f" ({', '.join(flags)})" if flags else ""
            lines.append(
                f"  index: {index.name or f'index_id {index.index_id}'} "
                f"[{index.kind.value}]{suffix}"
            )
        lines.append(f"  distribution: {table.distribution.value}")
        if table.distribution_columns:
            key = ", ".join(
                c.name or f"column_id {c.column_id}" for c in table.distribution_columns
            )
            lines.append(f"  distribution key: {key}")
        if show_columns:
            for column in table.columns:
                nullable = "NULL" if column.is_nullable else "NOT NULL"
                lines.append(
                    f"    {column.column_id:>3}  {column.name}  "
                    f"{column.data_type or 'unknown'}  {nullable}"
                )
        for issue in table.issues:
            lines.append(f"  ! {issue.code.value}: {issue.message}")
        lines.append("")

    issues = discovery.issues
    if issues:
        lines.append(f"issues: {len(issues)}")
        for issue in issues:
            lines.append(f"  ! {issue.code.value}: {issue.message}")
        lines.append("")

    if not discovery.is_complete:
        lines.append(
            "note: this run has gaps. Counts above are what the connected "
            "identity could see, not necessarily what exists."
        )
    return "\n".join(lines).rstrip() + "\n"


def source_for(
    config: SqlConnectionConfig, connector: Optional[Connector] = None
) -> DedicatedPoolSource:
    """The live source, or one built on an injected connector for testing.

    The whole connection comes from ``discovery_agent.connections``: this
    module names an endpoint and a database, and the connection layer decides
    what identity reaches them. Imported here rather than at module scope
    because ``connections`` imports this package -- an entry point may depend
    on the layer above it, but the library must not.
    """
    if connector is not None:
        return DedicatedPoolSource(config, connector)

    from discovery_agent.connections.azure import (  # noqa: PLC0415 - see above
        credential_provider,
    )
    from discovery_agent.connections.sql import SqlConnection  # noqa: PLC0415

    return SqlConnection(config, credential_provider()).source()


def main(argv: Optional[List[str]] = None, connector: Optional[Connector] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = config_from_environment(
            server=args.server, database=args.database
        )
        with source_for(config, connector) as source:
            discovery = source.discover_tables()
    except DiscoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(render(discovery, show_columns=args.show_columns), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
