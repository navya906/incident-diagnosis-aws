"""End-to-end over HTTP: a separately started server process (python -m app.demo: SQLite,
offline dataset, stub LLM) driven only through its public interface. smoke-test / synthetic.

The browser end-to-end tests live in frontend/e2e (Playwright); scripts/quickstart_check.py is
the same check used against the Docker stack (docs/deployment.md).
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
KEY = "e2e-http-key-0123456789"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _load_quickstart_check():
    spec = importlib.util.spec_from_file_location(
        "quickstart_check", REPO / "scripts" / "quickstart_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    db = tmp_path_factory.mktemp("e2e") / "demo.db"
    env = {**os.environ, "CLOUDDIAG_DEMO_KEY": KEY, "PYTHONUNBUFFERED": "1"}
    env.pop("CLOUDDIAG_DATABASE_URL", None)  # the demo sets its own SQLite file
    log = tmp_path_factory.mktemp("e2e-log") / "server.log"
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.demo", "--port", str(port), "--db", str(db), "--fresh"],
            cwd=REPO / "backend",
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 300  # first start may generate the offline dataset
        try:
            while True:
                if proc.poll() is not None:
                    pytest.fail(f"demo server exited:\n{log.read_text(encoding='utf-8')[-3000:]}")
                try:
                    with urllib.request.urlopen(base + "/health", timeout=2) as r:
                        if r.status == 200:
                            break
                except (urllib.error.URLError, OSError):
                    pass
                if time.monotonic() > deadline:
                    pytest.fail("demo server did not become healthy")
                time.sleep(0.5)
            yield base, log
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()


def _call(base, method, path, body=None, key=KEY):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), e.headers


def test_quickstart_flow_import_diagnose_and_close(server):
    base, _ = server
    check = _load_quickstart_check()
    iid = check.run(base, KEY, timeout=180)
    status, detail, _ = _call(base, "GET", f"/api/incidents/{iid}")
    assert status == 200 and detail["status"] == "CLOSED"
    assert detail["timings"]["time_to_resolve_min"] is not None


def test_alarm_webhook_opens_one_incident_per_alarm(server):
    base, _ = server
    from tests.test_api import eventbridge

    status, created, _ = _call(base, "POST", "/api/incidents", eventbridge(name="e2e-5xx"))
    assert status == 201 and created["status"] == "DETECTED"
    assert created["affected_resources"] == ["alb/web-lb"]
    status, again, _ = _call(base, "POST", "/api/incidents", eventbridge(name="e2e-5xx"))
    assert status == 200 and again["id"] == created["id"]
    status, ignored, _ = _call(base, "POST", "/api/incidents", eventbridge("OK", name="e2e-5xx"))
    assert status == 202
    # No telemetry was ingested for this incident: a diagnosis job fails clearly, it does not
    # invent a conclusion.
    status, job, _ = _call(base, "POST", f"/api/incidents/{created['id']}/diagnose", {})
    assert status == 202
    for _ in range(60):
        _, job, _ = _call(base, "GET", f"/api/jobs/{job['job_id']}")
        if job["status"] in ("SUCCEEDED", "FAILED"):
            break
        time.sleep(0.5)
    assert job["status"] == "FAILED" and "event" in job["error"].lower()
    status, _, _ = _call(base, "GET", f"/api/incidents/{created['id']}/diagnosis")
    assert status == 404


def test_auth_rate_limit_and_audit_over_the_wire(server):
    base, log = server
    assert _call(base, "GET", "/api/incidents", key=None)[0] == 401
    assert _call(base, "GET", "/api/incidents", key="not-the-key-000000")[0] == 401
    # Failed attempts are limited per client address (burst 30, then 429 with Retry-After).
    codes = [_call(base, "GET", "/api/incidents", key=f"guess-{i:06d}")[0] for i in range(40)]
    assert codes.count(429) > 0 and set(codes) <= {401, 429}
    status, _, headers = _call(base, "GET", "/api/incidents", key="guess-final")
    assert status == 429 and int(headers["Retry-After"]) >= 1
    # A valid key is limited separately and still works.
    status, audit, _ = _call(base, "GET", "/api/audit?limit=500")
    assert status == 200
    assert any(a["status_code"] == 429 for a in audit["items"])
    # Keys never reach the log or the audit trail.
    text = log.read_text(encoding="utf-8") + json.dumps(audit)
    assert KEY not in text and "not-the-key-000000" not in text
