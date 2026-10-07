"""The supervisor: the API restarts itself on new code, waits for work to finish,
and never swaps a working server for code that does not load.

The first tests drive its pieces directly, with the server played by the test.
The last two run real processes: a server that stops when its supervisor's pipe
closes, and a supervisor over a copy of the package in a temporary folder,
whose code the test edits while it watches the server restart.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from discovery_agent.api import supervisor as supervisor_module
from discovery_agent.api.supervisor import Supervisor, changed, code_problem, source_files

SRC = Path(supervisor_module.__file__).resolve().parents[2]  # .../src


# --- the pieces -----------------------------------------------------------------------


def test_the_watch_sees_python_files_change_appear_and_go_and_ignores_caches(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("y = 1\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "c.py").write_text("")
    (tmp_path / "notes.txt").write_text("")
    before = source_files(tmp_path)
    assert sorted(Path(p).name for p in before) == ["a.py", "b.py"]

    (tmp_path / "a.py").write_text("x = 22\n")
    (tmp_path / "pkg" / "b.py").unlink()
    (tmp_path / "d.py").write_text("")
    assert sorted(Path(p).name for p in changed(before, source_files(tmp_path))) == ["a.py", "b.py", "d.py"]
    assert changed(before, before) == []


def test_new_code_that_does_not_compile_is_caught_before_any_restart(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n    pass\n")
    problem = code_problem([str(bad)], {**os.environ, "PYTHONPATH": str(SRC)})
    assert problem is not None and "SyntaxError" in problem


def test_code_that_compiles_and_lets_the_server_import_passes(tmp_path):
    good = tmp_path / "good.py"
    good.write_text("x = 1\n")
    assert code_problem([str(good), str(tmp_path / "deleted.py")], {**os.environ, "PYTHONPATH": str(SRC)}) is None


class Rehearsal(Supervisor):
    """The supervisor's loop, with the server process played by the test: no process, no port.

    ``start`` and ``busy`` answer from scripts; a second ``start`` ends the rehearsal."""

    def __init__(self, root, busy=(), on_start=None):
        super().__init__(Namespace(host="127.0.0.1", port=1, static=None, no_state=True, state_dir=None, poll=0.02), root=root)
        self.answers = list(busy)
        self.on_start = on_start
        self.events = []

    def _wait_for_port(self):
        return None

    def start(self):
        self.events.append("start")
        if self.events.count("start") > 1:
            raise KeyboardInterrupt  # the restart happened: end here
        self.child = SimpleNamespace(poll=lambda: None, returncode=None, stdin=None)
        if self.on_start:
            self.on_start()
        return True

    def stop(self):
        if self.child is not None:
            self.events.append("stop")
        self.child = None

    def busy(self):
        self.events.append("busy")
        return self.answers.pop(0) if self.answers else []


def test_a_restart_waits_for_the_work_in_progress_to_finish(tmp_path, monkeypatch, capsys):
    source = tmp_path / "service.py"
    source.write_text("x = 1\n")
    monkeypatch.setattr(supervisor_module, "code_problem", lambda files, env: None)
    rehearsal = Rehearsal(tmp_path, busy=[["the migration run"]] * 3,
                          on_start=lambda: source.write_text("x = 2  # an update\n"))
    assert rehearsal.run() == 0
    assert rehearsal.events == ["start", "busy", "busy", "busy", "busy", "stop", "start"]
    said = capsys.readouterr().out
    assert said.count("The server restarts on it when the migration run finishes.") == 1  # said once, not every poll
    assert "Restarting the server on the new code" in said


def test_a_change_that_does_not_load_leaves_the_running_server_alone_until_it_is_fixed(tmp_path, monkeypatch, capsys):
    source = tmp_path / "service.py"
    source.write_text("x = 1\n")
    checks = []

    def check(files, env):
        checks.append(source.read_text())
        if len(checks) == 1:
            source.write_text("x = 3  # the fix\n")  # fixed while the broken version is reported
            return "SyntaxError: invalid syntax"
        return None

    monkeypatch.setattr(supervisor_module, "code_problem", check)
    rehearsal = Rehearsal(tmp_path, on_start=lambda: source.write_text("def broken(:\n"))
    assert rehearsal.run() == 0
    assert rehearsal.events == ["start", "busy", "busy", "stop", "start"]  # stopped only for the fix
    assert checks == ["def broken(:\n", "x = 3  # the fix\n"]
    said = capsys.readouterr().out
    assert "does not load, so the server keeps running the version before it" in said
    assert "SyntaxError: invalid syntax" in said


def test_a_server_that_does_not_answer_is_restarted_only_after_several_tries(tmp_path, monkeypatch):
    source = tmp_path / "service.py"
    source.write_text("x = 1\n")
    monkeypatch.setattr(supervisor_module, "code_problem", lambda files, env: None)
    rehearsal = Rehearsal(tmp_path, busy=[None, None, None], on_start=lambda: source.write_text("x = 2\n"))
    assert rehearsal.run() == 0
    assert rehearsal.events == ["start", "busy", "busy", "busy", "stop", "start"]


# --- real processes -------------------------------------------------------------------


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def health(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            return json.loads(response.read())
    except Exception:  # noqa: BLE001 - not answering
        return None


def eventually(check, seconds=60.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    return None


def test_a_supervised_server_stops_when_its_supervisors_pipe_closes(tmp_path):
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"}
    server = subprocess.Popen(
        [sys.executable, "-m", "discovery_agent.api", "--supervised", "--boot-id", "b1", "--port", str(port),
         "--no-state", "--static", str(tmp_path / "no-ui")],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert eventually(lambda: (health(port) or {}).get("bootId") == "b1")
        server.stdin.close()  # what a supervisor does to stop it, and what happens when one dies
        assert server.wait(timeout=30) == 0
        assert health(port) is None
    finally:
        if server.poll() is None:
            server.kill()


def test_a_real_supervisor_restarts_on_new_code_and_keeps_code_that_does_not_load_out(tmp_path):
    copy = tmp_path / "src"
    shutil.copytree(SRC / "discovery_agent", copy / "discovery_agent", ignore=shutil.ignore_patterns("__pycache__"))
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(copy), "PYTHONIOENCODING": "utf-8"}
    log_path = tmp_path / "supervisor.log"
    log = open(log_path, "wb")  # noqa: SIM115 - closed below, after the process
    supervisor = subprocess.Popen(
        [sys.executable, "-m", "discovery_agent.api", "--port", str(port), "--poll", "0.2",
         "--state-dir", str(tmp_path / "state"), "--static", str(tmp_path / "no-ui")],
        env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

    def said():
        return log_path.read_text(encoding="utf-8", errors="replace")

    def answering_other_than(*boots):
        answer = health(port)
        return answer if answer and answer.get("bootId") not in boots else None

    try:
        first = eventually(lambda: health(port))
        assert first and first["supervised"] is True, said()
        target = copy / "discovery_agent" / "api" / "service.py"
        original = target.read_text(encoding="utf-8")

        target.write_text(original + "\n# an update\n", encoding="utf-8")
        second = eventually(lambda: answering_other_than(first["bootId"]))
        assert second, said()

        target.write_text(original + "\ndef broken(:\n", encoding="utf-8")
        assert eventually(lambda: "does not load" in said()), said()
        assert health(port)["bootId"] == second["bootId"]  # still the server that works

        target.write_text(original, encoding="utf-8")
        assert eventually(lambda: answering_other_than(first["bootId"], second["bootId"])), said()
    finally:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(supervisor.pid), "/T", "/F"], capture_output=True)
        else:
            supervisor.kill()  # the server has its own session: it stops when the pipe closes
        supervisor.wait(timeout=30)
        log.close()
    assert eventually(lambda: health(port) is None, seconds=30)
