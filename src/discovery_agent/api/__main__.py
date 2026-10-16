"""Run the API: ``python -m discovery_agent.api``.

``--reload`` is for working on the accelerator: the server runs as a child process that is restarted within
a second whenever a ``.py`` file under ``src/`` changes. The active project's discovery, Fabric target, runs
and audit trail come back on their own after each restart (``state.projects``); a run the restart cut short is
paused as interrupted, for Resume. Only the Synapse sign-in has to be repeated, and only to read Synapse again.

The server process claims its state folder: a second API pointed at the same folder refuses to start.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from discovery_agent.api.server import create_server
from discovery_agent.state import context, logs
from discovery_agent.state.projects import ProjectError, Projects

DEFAULT_PORT = 8001
DEFAULT_STATIC = Path(__file__).resolve().parents[3] / "frontend" / "dist"
SOURCE_ROOT = Path(__file__).resolve().parents[1]  # src/discovery_agent
POLL_SECONDS = 1.0


def _already_running(host: str, port: int) -> bool:
    """Whether this API already answers on the port. Two copies on one port answer at random: refuse instead."""
    try:
        with urllib.request.urlopen(f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/api/health", timeout=2) as r:
            return json.loads(r.read() or b"{}").get("status") == "ok"
    except Exception:  # noqa: BLE001 - nothing (or something else) on the port
        return False


def _snapshot() -> Dict[str, float]:
    out: Dict[str, float] = {}
    for path in SOURCE_ROOT.rglob("*.py"):
        try:
            out[str(path)] = path.stat().st_mtime
        except OSError:
            pass
    return out


def _changed(before: Dict[str, float], after: Dict[str, float]) -> List[str]:
    return sorted({p for p in set(before) | set(after) if before.get(p) != after.get(p)})


def _supervise(child_args: List[str]) -> int:
    """Run the server as a child and restart it when the code changes. Ctrl+C stops both."""
    child: Optional[subprocess.Popen] = None
    try:
        while True:
            seen = _snapshot()
            child = subprocess.Popen([sys.executable, "-m", "discovery_agent.api", *child_args], env=os.environ.copy())
            while True:
                time.sleep(POLL_SECONDS)
                now = _snapshot()
                changed = _changed(seen, now)
                if changed:
                    names = ", ".join(Path(p).name for p in changed[:3]) + (f" and {len(changed) - 3} more" if len(changed) > 3 else "")
                    print(f"\nCode changed ({names}): restarting the API...", flush=True)
                    break
                if child.poll() is not None:
                    print(f"\nThe API stopped (exit code {child.returncode}). Fix the cause and save a file to start it again; "
                          "Ctrl+C to quit.", flush=True)
                    while not _changed(seen, _snapshot()):
                        time.sleep(POLL_SECONDS)
                    print("Code changed: starting the API...", flush=True)
                    break
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="discovery-agent-api",
        description="Local API for the Migration Accelerator UI. Read-only discovery of Azure Synapse.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--static", type=Path, default=DEFAULT_STATIC, help="Built frontend to serve (frontend/dist).")
    parser.add_argument("--reload", action="store_true", help="Restart automatically when the code under src/ changes.")
    args = parser.parse_args()

    if _already_running(args.host, args.port):
        print(f"The Migration Accelerator API is already running on port {args.port}; use that one "
              "(or stop it first). Not starting a second copy.")
        return 0

    if args.reload:
        print(f"Migration Accelerator API on http://{args.host}:{args.port} (restarts when the code changes; Ctrl+C to stop)")
        return _supervise(["--host", args.host, "--port", str(args.port), "--static", str(args.static)])

    # The server process (never the --reload supervisor) logs to its console and to .accelerator/logs/api.jsonl.
    log_file = logs.configure()
    # One API process per state folder: a second one writing the same projects would corrupt their records.
    projects = Projects()
    owner = f"API on {args.host}:{args.port}"
    try:
        projects.claim_process(owner)
    except ProjectError as exc:
        print(exc.message)
        return 1
    try:
        server = create_server(args.host, args.port, args.static, projects=projects)
        print(f"Migration Accelerator API on http://{args.host}:{args.port}" + (f" (log: {log_file})" if log_file else ""))
        print(f"State: {projects.root}" + ("" if projects.persistent else " (MIGRATION_STATE=off: kept for this run only)"))
        print("Sign in first with `az login`. Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.orchestrator.stop()
            server.server_close()
            with context.bound(actor=context.system()):
                server.projects.audit("system.stopped", details={"pid": os.getpid()})
    finally:
        projects.release_process(owner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
