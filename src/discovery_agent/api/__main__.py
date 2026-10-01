"""Run the API: ``python -m discovery_agent.api``."""

from __future__ import annotations

import argparse
from pathlib import Path

from discovery_agent.api.server import create_server

DEFAULT_PORT = 8000
DEFAULT_STATIC = Path(__file__).resolve().parents[3] / "frontend" / "dist"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="discovery-agent-api",
        description="Local API for the Migration Accelerator UI. Read-only discovery of Azure Synapse.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--static", type=Path, default=DEFAULT_STATIC, help="Built frontend to serve (frontend/dist).")
    args = parser.parse_args()

    server = create_server(args.host, args.port, args.static)
    print(f"Migration Accelerator API on http://{args.host}:{args.port}")
    print("Sign in first with `az login`. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
