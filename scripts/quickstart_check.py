"""Check a running stack end to end over HTTP: offline import -> diagnosis -> evidence -> close.

    docker compose up -d --build --wait
    python scripts/quickstart_check.py                         # http://localhost:3000, demo key
    python scripts/quickstart_check.py --url http://host:8080 --key "$KEY"

Standard library only, so it runs from a fresh clone without installing the backend. It goes
through the same origin as the browser (nginx in Docker), uses only data the UI shows, and
fails with a non-zero exit code and a reason at the first problem. The output is
smoke-test / synthetic: offline dataset and the stub LLM.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEMO_KEY = "demo-key-change-me"


class CheckFailed(RuntimeError):
    pass


class Client:
    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.key = key

    def call(self, method: str, path: str, body: dict | None = None, key: str | None = "default"):
        headers = {"Accept": "application/json"}
        if key == "default":
            key = self.key
        if key:
            headers["X-API-Key"] = key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"null"), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw or b"null")
            except json.JSONDecodeError:
                payload = raw.decode(errors="replace")
            return e.code, payload, dict(e.headers)


def expect(cond: bool, message: str) -> None:
    if not cond:
        raise CheckFailed(message)
    print(f"  ok  {message}")


def run(base: str, key: str, timeout: float) -> str:
    c = Client(base, key)
    status, health, _ = c.call("GET", "/health", key=None)
    expect(status == 200 and health.get("database") == "ok", "health: api and database up")
    status, _, _ = c.call("GET", "/api/incidents", key=None)
    expect(status == 401, "api refuses requests without a key")
    status, _, _ = c.call("GET", "/api/incidents", key="wrong-key-0000000000")
    expect(status == 401, "api refuses a wrong key")

    status, offline, _ = c.call("GET", "/api/offline/incidents")
    expect(status == 200 and offline["items"], "offline dataset is available in the stack")
    forbidden = {"category", "fault_type", "taxonomy_label", "root_cause", "ground_truth"}
    expect(
        not any(forbidden & set(item) for item in offline["items"]),
        "offline listing exposes no ground truth",
    )
    # Prefer an incident that is not imported yet, so the check can be run repeatedly.
    _, existing, _ = c.call("GET", "/api/incidents?limit=500")
    taken = {i["id"] for i in existing.get("items", [])}
    dev = [i for i in offline["items"] if i["split"] == "dev"]
    choice = next((i for i in dev if i["incident_id"] not in taken), None)
    expect(choice is not None, "a dev incident is available to import")
    iid = choice["incident_id"]

    status, inc, _ = c.call("POST", "/api/offline/import", {"offline_incident_id": iid})
    expect(status == 201, f"imported {iid} ({choice['title']})")
    expect(inc.get("status") == "DETECTED", "new incident is DETECTED")

    status, job, _ = c.call("POST", f"/api/incidents/{iid}/diagnose", {"condition": "Full"})
    expect(status == 202 and job.get("job_id"), "diagnosis job accepted")
    deadline = time.monotonic() + timeout
    while True:
        _, job, _ = c.call("GET", f"/api/jobs/{job['job_id']}")
        if job["status"] in ("SUCCEEDED", "FAILED") or time.monotonic() > deadline:
            break
        time.sleep(1)
    expect(job["status"] == "SUCCEEDED", f"diagnosis job finished: {job['status']}")

    status, d, _ = c.call("GET", f"/api/incidents/{iid}/diagnosis")
    expect(status == 200, "latest diagnosis is readable")
    diag = d.get("diagnosis") or {}
    expect(bool(d.get("valid")), "diagnosis passed validation")
    supporting = diag.get("supporting_evidence") or []
    expect(len(supporting) > 0, f"diagnosis cites {len(supporting)} supporting evidence items")
    events = d.get("evidence_events") or {}
    missing = [s["evidence_id"] for s in supporting if s["evidence_id"] not in events]
    expect(not missing, "every supporting citation resolves to a stored event")
    expect(d.get("severity", {}).get("level") in {"LOW", "MEDIUM", "HIGH", "CRITICAL"},
           "deterministic severity present")
    rc = diag.get("root_cause", {})
    print(f"      root cause (smoke-test / synthetic): {rc.get('taxonomy_label')} on "
          f"{rc.get('resource_id')} (confidence {rc.get('confidence')})")

    for view in ("timeline", "metrics", "logs", "cloudtrail", "graph", "evidence"):
        status, _, _ = c.call("GET", f"/api/incidents/{iid}/{view}")
        expect(status == 200, f"{view} view responds")

    _, detail, _ = c.call("GET", f"/api/incidents/{iid}")
    expect(detail["status"] == "DIAGNOSED", "lifecycle moved to DIAGNOSED by the job")
    for to in ("MITIGATING", "RESOLVED", "CLOSED"):
        status, _, _ = c.call(
            "POST", f"/api/incidents/{iid}/transitions", {"to": to, "note": "quickstart check"}
        )
        expect(status == 200, f"transition to {to}")
    return iid


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:3000")
    p.add_argument("--key", default=DEMO_KEY)
    p.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for the job")
    args = p.parse_args(argv)
    print(f"quickstart check against {args.url} (smoke-test / synthetic)")
    try:
        iid = run(args.url, args.key, args.timeout)
    except CheckFailed as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"FAILED: cannot reach {args.url}: {e}", file=sys.stderr)
        return 2
    print(f"PASSED: offline demo diagnosis of {iid} end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
