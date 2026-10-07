"""Keep the API on the latest code, without losing its work.

``python -m discovery_agent.api`` runs this supervisor. The server runs in a
child process, and the supervisor watches the package's source files; when one
changes, it restarts the server on the new code:

* never in the middle of work: while discovery, a migration run, a validation
  or a sign-in is working (``/api/health`` lists it under ``busy``), the
  restart waits for it to finish;
* never onto code that does not load: the changed files are compiled and the
  server is imported in a separate process first, and while that fails the
  server keeps running the version that works;
* without losing state: the server saves its sign-ins, discovery and run as
  they change (``state``) and takes them back when it starts.

The child's stdin is a pipe from here. Closing it asks the server to stop, and
a supervisor that dies closes it too, so a server is never left behind holding
the port. Standard library only, like the rest of the API.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discovery_agent

PACKAGE_ROOT = Path(discovery_agent.__file__).resolve().parent
#: The exit code of a server whose port is taken.
EXIT_PORT_IN_USE = 3
POLL_SECONDS = 1.0
#: Quiet time after the last change before restarting: an editor or a checkout writes several files.
SETTLE_SECONDS = 0.8
#: A server restoring a large discovery takes a moment before it answers.
START_TIMEOUT = 90.0
STOP_TIMEOUT = 10.0
CHECK_TIMEOUT = 120.0
#: A server that stops by itself after running this long is started again at once; sooner, it waits for a fix.
STEADY_SECONDS = 15.0
#: Health checks in a row that may go unanswered before a restart stops waiting for them.
SILENT_CHECKS = 3

Snapshot = Dict[str, Tuple[int, int]]
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost never goes through a proxy

#: Run in a separate interpreter: compile the changed files, then import the server and all it imports.
_CHECK = (
    "import sys\n"
    "for name in sys.argv[1:]:\n"
    "    with open(name, 'rb') as handle:\n"
    "        compile(handle.read(), name, 'exec')\n"
    "import discovery_agent.api.server\n"
)


def source_files(root: Path = PACKAGE_ROOT) -> Snapshot:
    """Every Python file of the package: path -> (modified time, size)."""
    files: Snapshot = {}
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue  # removed while walking
        files[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return files


def changed(before: Snapshot, after: Snapshot) -> List[str]:
    """Files added, removed or modified between two snapshots."""
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def code_problem(files: List[str], env: Dict[str, str], timeout: float = CHECK_TIMEOUT) -> Optional[str]:
    """None when the changed files compile and the server imports; else the error, to show."""
    present = [f for f in files if os.path.isfile(f)]
    try:
        done = subprocess.run([sys.executable, "-c", _CHECK, *present], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return "Loading the new code took too long."
    if done.returncode == 0:
        return None
    lines = (done.stderr or done.stdout or "").strip().splitlines()
    return "\n".join(lines[-12:]) or f"It stopped with exit code {done.returncode}."


def _join(items: List[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


class Supervisor:
    """Runs the server, and restarts it on the latest code when the code changes."""

    def __init__(self, args: argparse.Namespace, root: Path = PACKAGE_ROOT) -> None:
        self.args = args
        self.root = root
        self.poll = float(getattr(args, "poll", None) or POLL_SECONDS)
        self.settle = min(SETTLE_SECONDS, self.poll * 4)
        self.child: Optional[subprocess.Popen] = None
        self.boot = ""
        self.started = 0.0
        src = str(root.parent)
        rest = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and p != src]
        # The server and the code check import the package from here, whatever else is installed.
        self.env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, *rest]), "PYTHONUNBUFFERED": "1"}

    # -- talking to the server --------------------------------------------------

    def _host(self) -> str:
        host = self.args.host
        return {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)

    def health(self, timeout: float = 3.0) -> Optional[dict]:
        host = self._host()
        url = f"http://{f'[{host}]' if ':' in host else host}:{self.args.port}/api/health"
        try:
            with _DIRECT.open(url, timeout=timeout) as response:
                answer = json.loads(response.read() or b"{}")
        except Exception:  # noqa: BLE001 - not answering is an answer
            return None
        return answer if isinstance(answer, dict) else None

    def busy(self) -> Optional[List[str]]:
        """What the server is working on, or None when it does not answer."""
        answer = self.health(timeout=5.0)
        if answer is None or answer.get("bootId") != self.boot:
            return None
        return [str(b) for b in answer.get("busy") or []]

    def _port_taken(self) -> bool:
        try:
            with socket.create_connection((self._host(), self.args.port), timeout=0.5):
                return True
        except OSError:
            return False

    def say(self, message: str) -> None:
        text = f"[accelerator] {message}"
        try:
            print(text, flush=True)
        except UnicodeEncodeError:  # a console or pipe that cannot show a character in an error message
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)

    # -- the server process -----------------------------------------------------

    def start(self) -> bool:
        """Start the server; True once it answers as the process just started."""
        self.boot = secrets.token_hex(8)
        command = [sys.executable, "-m", "discovery_agent.api", "--supervised", "--boot-id", self.boot,
                   "--host", self.args.host, "--port", str(self.args.port)]
        if self.args.static is not None:
            command += ["--static", str(self.args.static)]
        if self.args.no_state:
            command.append("--no-state")
        if self.args.state_dir is not None:
            command += ["--state-dir", str(self.args.state_dir)]
        # Its own process group: Ctrl+C comes here, and the server is then stopped in order.
        group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                 else {"start_new_session": True})
        self.child = subprocess.Popen(command, stdin=subprocess.PIPE, env=self.env, **group)
        self.started = time.monotonic()
        while time.monotonic() - self.started < START_TIMEOUT:
            if self.child.poll() is not None:
                return False
            answer = self.health(timeout=1.0)
            if answer is not None and answer.get("bootId") == self.boot:
                return True
            time.sleep(0.25)
        return False

    def stop(self) -> None:
        """Ask the server to stop (its stdin closes), and make sure it has."""
        child, self.child = self.child, None
        if child is None:
            return
        try:
            if child.stdin is not None:
                child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
            else:
                child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _start_or_explain(self) -> None:
        if self.start():
            return
        child = self.child
        if child is not None and child.poll() is None:
            self.say("The server is taking long to answer; still waiting for it.")
            return
        self.child = None
        code = child.returncode if child is not None else None
        if code == EXIT_PORT_IN_USE:
            self.say(f"Port {self.args.port} was taken by another program. It starts again when the code changes.")
        else:
            self.say(f"The server did not start (exit code {code}); the error is above. "
                     "It starts again when the code changes.")

    def _wait_for_port(self) -> Optional[int]:
        """None once the port is free; else the exit code to stop with."""
        told = False
        while self._port_taken():
            answer = self.health()
            if answer is None:
                self.say(f"Port {self.args.port} is in use by another program. Stop it, or start the API with "
                         "--port and another port.")
                return 1
            if answer.get("supervised"):
                self.say(f"The Migration Accelerator API on port {self.args.port} already restarts by itself "
                         "when the code changes. Nothing more to start.")
                return 0
            if not told:
                self.say(f"An older Migration Accelerator API is running on port {self.args.port}, and it does not "
                         "update itself. Close its window (or stop that process) and this one takes over.")
                told = True
            time.sleep(2)
        return None

    # -- the loop -----------------------------------------------------------------

    def run(self) -> int:
        stop = self._wait_for_port()
        if stop is not None:
            return stop
        self.say(f"Migration Accelerator API on http://{self.args.host}:{self.args.port}. It restarts by itself "
                 "when the code changes (after any discovery or migration run finishes) and keeps its sign-ins, "
                 "discovery and run. Press Ctrl+C to stop.")
        files = source_files(self.root)
        self._start_or_explain()
        pending: List[str] = []
        last_change = 0.0
        waiting: Optional[List[str]] = None
        silent = 0
        try:
            while True:
                time.sleep(self.poll)
                now = source_files(self.root)
                diff = changed(files, now)
                if diff:
                    files, last_change = now, time.monotonic()
                    pending = sorted(set(pending) | set(diff))
                    continue
                if self.child is not None and self.child.poll() is not None:
                    self._stopped_by_itself()
                if not pending or time.monotonic() - last_change < self.settle:
                    continue
                if self.child is not None:
                    working = self.busy()
                    if working is None:
                        silent += 1
                        if silent < SILENT_CHECKS:
                            continue
                    elif working:
                        silent = 0
                        if working != waiting:
                            self.say(f"Code changed ({self._names(pending)}). The server restarts on it when "
                                     f"{_join(working)} finishes.")
                            waiting = working
                        continue
                silent = 0
                problem = code_problem(pending, self.env)
                if problem:
                    self.say("The new code does not load, so the server keeps running the version before it:\n"
                             + textwrap.indent(problem, "    ") + "\n  It restarts once this is fixed.")
                    pending, waiting = [], None
                    continue
                self.say(f"Code changed ({self._names(pending)}). Restarting the server on the new code.")
                pending, waiting = [], None
                began = time.monotonic()
                self.stop()
                self._start_or_explain()
                if self.child is not None:
                    self.say(f"Restarted in {time.monotonic() - began:.1f}s.")
        except KeyboardInterrupt:
            self.say("Stopping.")
        finally:
            self.stop()
        return 0

    def _stopped_by_itself(self) -> None:
        child, self.child = self.child, None
        code = child.returncode if child is not None else None
        ran = time.monotonic() - self.started
        if child is not None and child.stdin is not None:
            try:
                child.stdin.close()
            except OSError:
                pass
        if ran >= STEADY_SECONDS and code != EXIT_PORT_IN_USE:
            self.say(f"The server stopped unexpectedly (exit code {code}). Starting it again; its state comes back.")
            self._start_or_explain()
        else:
            self.say(f"The server stopped (exit code {code}); the error is above. It starts again when the code changes.")

    def _names(self, files: List[str]) -> str:
        names = [os.path.relpath(f, self.root).replace(os.sep, "/") for f in files]
        return names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"
