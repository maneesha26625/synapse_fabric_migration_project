"""Run the API: ``python -m discovery_agent.api``.

By default the server runs under a supervisor that restarts it whenever its
code changes, and it keeps its sign-ins, discovery and migration run across
those restarts (see ``supervisor`` and ``state``). Nothing has to be restarted
by hand after an update. ``--no-reload`` serves from this process only;
``--no-state`` keeps nothing from one run of the server to the next.
"""

from __future__ import annotations

import argparse
import errno
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional

from discovery_agent.api.supervisor import EXIT_PORT_IN_USE, POLL_SECONDS, Supervisor

DEFAULT_PORT = 8001
DEFAULT_STATIC = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def parse(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="discovery-agent-api",
        description="Local API for the Migration Accelerator UI. Read-only discovery of Azure Synapse.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--static", type=Path, default=DEFAULT_STATIC, help="Built frontend to serve (frontend/dist).")
    parser.add_argument("--no-reload", action="store_true",
                        help="Serve from this process only: do not restart when the code changes.")
    parser.add_argument("--no-state", action="store_true",
                        help="Keep nothing across restarts: no sign-in, discovery or run is saved.")
    parser.add_argument("--state-dir", type=Path, default=None,
                        help="Where the state is kept (default: ~/.synapse-discovery/api-state/<port>).")
    # Set by the supervisor for the server it runs, and by tests.
    parser.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--boot-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--poll", type=float, default=POLL_SECONDS, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse(argv)
    if args.supervised or args.no_reload:
        return serve(args)
    return Supervisor(args).run()


def _in_use(exc: OSError) -> bool:
    return exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048


def serve(args: argparse.Namespace) -> int:
    """The server itself, in this process."""
    from discovery_agent.api.server import create_server  # noqa: PLC0415 - the supervisor never needs the API
    from discovery_agent.api.state import StateStore, default_directory  # noqa: PLC0415

    directory = args.state_dir or default_directory(args.port)
    store = None if args.no_state else StateStore(directory)
    try:
        server = create_server(args.host, args.port, args.static, store=store,
                               supervised=args.supervised, boot_id=args.boot_id)
    except OSError as exc:
        if not _in_use(exc):
            raise
        print(f"Port {args.port} is already in use: another API is running there. "
              "Stop it, or choose another port with --port.", file=sys.stderr, flush=True)
        return EXIT_PORT_IN_USE
    if args.supervised:
        _stop_when_stdin_closes(server)
    print(f"Migration Accelerator API on http://{args.host}:{args.port}", flush=True)
    if not args.supervised:
        if store is not None:
            print(f"Keeping its state in {directory}", flush=True)
        print("It does not restart by itself (--no-reload). Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.save_state()
    return 0


def _stop_when_stdin_closes(server: Any) -> None:
    """The supervisor asks the server to stop by closing its stdin, and a supervisor that
    dies closes it too: a server is never left behind holding the port."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return

    def watch() -> None:
        try:
            while stream.read(1024):
                pass  # nothing is ever sent; only the end matters
        except (OSError, ValueError):
            pass
        server.shutdown()

    threading.Thread(target=watch, name="supervisor-watch", daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
