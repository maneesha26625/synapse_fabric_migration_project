"""Logging: one setup, structured, redacted, and correlated from a click to the work it starts.

Offline. Each test configures the accelerator's logging into its own folder, reads the JSON lines back, and
takes the handlers off again, so nothing reaches the operator's own log.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from discovery_agent.api.server import make_handler
from discovery_agent.api.service import Session
from discovery_agent.state import context, logs

from test_migration import fake_job, service, wait_until  # noqa: E402 - shared fakes


@pytest.fixture()
def logged(tmp_path):
    path = logs.configure(directory=tmp_path / "logs", level="DEBUG")
    yield lambda: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    logs.reset()


def get(port, path, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    response = conn.getresponse()
    response.read()
    conn.close()
    return response


def test_a_request_carries_one_correlation_id_into_its_response_and_its_log_line(logged, monkeypatch):
    monkeypatch.setenv("MIGRATION_OPERATOR", "Asha Rao")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Session(), None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        sent = get(httpd.server_address[1], "/api/health", {"X-Correlation-Id": "click-0001-abcd"})
        made = get(httpd.server_address[1], "/api/health")
        refused = get(httpd.server_address[1], "/api/health", {"X-Correlation-Id": "not ok; drop table"})
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert sent.getheader("X-Correlation-Id") == "click-0001-abcd"
    assert re.fullmatch(r"[0-9a-f]{16}", made.getheader("X-Correlation-Id"))
    assert re.fullmatch(r"[0-9a-f]{16}", refused.getheader("X-Correlation-Id"))  # an odd value is replaced, never echoed
    lines = [line for line in logged() if line.get("path") == "/api/health"]
    first = next(line for line in lines if line["correlationId"] == "click-0001-abcd")
    assert first["status"] == 200 and first["method"] == "GET" and isinstance(first["durationMs"], int)
    assert first["actor"] == "person:Asha Rao" and first["logger"] == "discovery_agent.api"


def test_background_work_keeps_the_correlation_id_of_the_request_that_started_it(logged):
    done = threading.Event()

    def work():
        logs.get("test").info("working in the background")
        done.set()

    with context.bound(correlation="click-0002-abcd", actor=context.operator()):
        context.spawn(work, name="background", run="007")
    assert done.wait(5)
    line = next(line for line in logged() if line["message"] == "working in the background")
    assert line["correlationId"] == "click-0002-abcd" and line["run"] == "007" and line["actor"].startswith("person:")


def test_a_migration_run_writes_its_events_under_the_correlation_id_of_the_start(logged):
    svc = service(fake_job())
    with context.bound(correlation="click-0003-abcd", actor=context.operator()):
        svc.start({"items": [{"id": "synapse://notebook/LoadSales", "wave": 1}], "options": {"stages": ["notebooks"], "gitCommit": "off"}})
    assert wait_until(lambda: svc.state()["state"] == "completed")
    lines = [line for line in logged() if line["logger"] == "discovery_agent.run"]
    # The lines written while starting it, and those its own thread wrote, all carry the click's id.
    assert lines and all(line["correlationId"] == "click-0003-abcd" and line["runId"] == "001" for line in lines)
    completed = next(line for line in lines if line.get("event") == "COMPLETED" and line.get("object") == "LoadSales")
    assert completed["run"] == "001"  # written by the run's own thread, which has the run in its context


def test_secrets_are_masked_in_every_line_and_traceback(logged):
    log = logs.get("test")
    log.info("signed in with Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", extra={"detail": "Password=hunter2"})
    try:
        raise RuntimeError("could not connect: Server=tcp:x;Password=hunter2;")
    except RuntimeError:
        log.exception("connecting failed")
    text = json.dumps(logged())
    assert "hunter2" not in text and "eyJhbGciOiJIUzI1NiJ9" not in text and "[redacted]" in text
    assert any("Traceback" in (line.get("exception") or "") for line in logged())


def test_configuring_twice_does_not_write_each_line_twice(logged, tmp_path):
    logs.configure(directory=tmp_path / "logs", level="DEBUG")  # a reload configures again
    logs.get("test").info("only once")
    assert [line["message"] for line in logged()].count("only once") == 1
    assert len(logging.getLogger(logs.ROOT).handlers) == 2


def test_the_file_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGRATION_LOG", "off")
    try:
        assert logs.configure(directory=tmp_path / "logs") is None
        assert not (tmp_path / "logs").exists() and len(logging.getLogger(logs.ROOT).handlers) == 1
    finally:
        logs.reset()
