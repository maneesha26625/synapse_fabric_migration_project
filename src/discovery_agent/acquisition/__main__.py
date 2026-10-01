"""Entry point for `python -m discovery_agent.acquisition`.

Goes through ``GitConnection`` rather than calling ``acquire_repository``
directly. The clone itself is still this package's -- nothing about the
acquisition logic moved -- but the *entry* is the connection layer, so this
command is an example of the rule rather than an exception to it: nothing
outside ``discovery_agent.connections`` opens its own connection to a git
host.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from discovery_agent.acquisition.acquire import DEFAULT_INPUT_ROOT
from discovery_agent.errors import DiscoveryError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m discovery_agent.acquisition",
        description="Clone a source repository locally (or reuse an existing clone) "
        "and print the resulting repository source object.",
    )
    parser.add_argument(
        "--repository-url",
        required=True,
        help="Repository URL, e.g. https://github.com/<owner>/<repo>. "
        "Must not contain credentials.",
    )
    parser.add_argument(
        "--ref",
        default=None,
        help="Branch, tag, or commit SHA to check out. Defaults to the "
        "repository's default branch.",
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        default=DEFAULT_INPUT_ROOT,
        help=f"Directory to clone into (default: {DEFAULT_INPUT_ROOT}).",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Imported here, not at module scope: ``connections`` imports this
        # package, and an entry point may depend on the layer above it while
        # the library beneath must not.
        from discovery_agent.connections.git import (  # noqa: PLC0415
            connection_from_url,
        )

        connection = connection_from_url(args.repository_url, ref=args.ref)
        source = connection.acquire(input_root=args.input_root)
    except DiscoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(source.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
