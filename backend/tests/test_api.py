"""Phase 8: REST API, lifecycle, async jobs, alarm webhook, security."""

import base64
import datetime as dt
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from app.api import lifecycle
from app.api.alarms import SnsVerifier, resources_from_dimensions
from app.api.security import AuthConfigError, key_id
from app.config import ApiSettings, EmbeddingSettings, Settings
from app.contracts.events import CanonicalEvent, EventSource, Severity
from app.db import Base
from app.db.models import AuditLog, DiagnosisJob, Incident
from app.db.session import make_session_factory
from app.main import create_app
from app.offline.dataset import DatasetLoader, generate_dataset

KEY = "test-key-0123456789abcdef"
OTHER_KEY = "second-key-zyxwvu987654"
H = {"X-API-Key": KEY}
T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("api-ds")
    generate_dataset(out, seed=42)
    return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_settings(dataset, tmp_path, **api) -> Settings:
    base = dict(
        api_keys=[KEY, OTHER_KEY],
        dataset_dir=str(dataset),
        corpus_dir=str(tmp_path / "corpus"),
        job_workers=1,
    )
    base.update(api)
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'api.db'}",
        api=ApiSettings(**base),
        embeddings=EmbeddingSettings(provider="hashing", dimension=128),
        diagnosis={"self_consistency_samples": 1},
    )


def make_client(dataset, tmp_path, verifier=None, clock=None, raise_errors=True, **api):
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = make_settings(dataset, tmp_path, **api)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    app = create_app(settings, engine, sns_verifier=verifier, clock=clock)
    return TestClient(app, raise_server_exceptions=raise_errors), app, engine


@pytest.fixture
def client(dataset, tmp_path):
    c, app, engine = make_client(dataset, tmp_path)
    with c:
        yield c


def native(**kw):
    body = {
        "title": "5XX on web ALB",
        "description": "Users see errors",
        "alarm_time": T0.isoformat(),
        "affected_resources": ["alb/web-lb"],
    }
    body.update(kw)
    return body


def import_dev(client, dataset, i=0):
    iid = DatasetLoader(dataset).incident_ids("dev")[i]
    r = client.post("/api/offline/import", json={"offline_incident_id": iid}, headers=H)
    assert r.status_code in (200, 201), r.text
    return iid


def wait_job(client, job_id):
    client.app.state.jobs.wait(job_id)
    return client.get(f"/api/jobs/{job_id}", headers=H).json()


# ------------------------------------------------------------------ incidents
def test_health_is_public(client):
    assert client.get("/health").json()["status"] == "ok"


def test_create_list_get_and_dedup(client):
    r = client.post("/api/incidents", json=native(dedup_key="alarm:x"), headers=H)
    assert r.status_code == 201 and r.json()["created"] is True
    inc = r.json()
    assert inc["status"] == "DETECTED" and inc["source"] == "api"
    assert inc["window_start"].startswith("2026-10-07T11:00") and inc["allowed_transitions"]
    again = client.post("/api/incidents", json=native(dedup_key="alarm:x"), headers=H)
    assert again.status_code == 200 and again.json()["id"] == inc["id"]
    client.post("/api/incidents", json=native(title="second"), headers=H)
    items = client.get("/api/incidents", headers=H).json()["items"]
    assert len(items) == 2
    assert len(client.get("/api/incidents?status=DETECTED&limit=1", headers=H).json()["items"]) == 1
    detail = client.get(f"/api/incidents/{inc['id']}", headers=H).json()
    assert detail["transitions"][0]["to"] == "DETECTED" and detail["counts"]["events"] == 0
    assert client.get("/api/incidents/inc-missing0000", headers=H).status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        {"affected_resources": ["not a resource"]},
        {"affected_resources": []},
        {"title": ""},
        {"title": "x" * 301},
        {"region": "mars-1"},
        {"window_start": T0.isoformat()},
        {"window_start": T0.isoformat(), "window_end": (T0 - timedelta(hours=1)).isoformat()},
        {"window_start": T0.isoformat(), "window_end": (T0 + timedelta(days=3)).isoformat()},
        {"unexpected_field": 1},
    ],
)
def test_input_validation(client, bad):
    r = client.post("/api/incidents", json=native(**bad), headers=H)
    assert r.status_code == 422


def test_bad_bodies(client):
    r = client.post(
        "/api/incidents", content=b"not json", headers={**H, "content-type": "application/json"}
    )
    assert r.status_code == 400
    assert client.post("/api/incidents", json=[1, 2], headers=H).status_code == 400


# ------------------------------------------------------------------ lifecycle
def test_lifecycle_happy_path_and_rules(client):
    iid = client.post("/api/incidents", json=native(), headers=H).json()["id"]

    def move(to, note=""):
        return client.post(
            f"/api/incidents/{iid}/transitions", json={"to": to, "note": note}, headers=H
        )

    assert move("RESOLVED").status_code == 409  # DETECTED cannot jump to RESOLVED
    for to in ("INVESTIGATING", "DIAGNOSED", "MITIGATING", "RESOLVED"):
        r = move(to)
        assert r.status_code == 200 and r.json()["status"] == to
    assert move("INVESTIGATING").status_code == 200  # reopen
    assert move("DIAGNOSED").status_code == 200
    assert move("RESOLVED").status_code == 200
    assert move("CLOSED").status_code == 200
    assert move("INVESTIGATING").status_code == 409  # CLOSED is terminal
    detail = client.get(f"/api/incidents/{iid}", headers=H).json()
    path = [t["to"] for t in detail["transitions"]]
    assert path == [
        "DETECTED",
        "INVESTIGATING",
        "DIAGNOSED",
        "MITIGATING",
        "RESOLVED",
        "INVESTIGATING",
        "DIAGNOSED",
        "RESOLVED",
        "CLOSED",
    ]
    stamps = [t["at"] for t in detail["transitions"]]
    assert stamps == sorted(stamps)
    assert all(t["actor"] == key_id(KEY) for t in detail["transitions"])
    t = detail["timings"]
    assert t["diagnosed_at"] and t["resolved_at"] and t["closed_at"]
    assert t["time_to_diagnose_min"] >= 0 and t["time_to_resolve_min"] >= t["time_to_diagnose_min"]


def test_closing_unresolved_needs_a_note(client):
    iid = client.post("/api/incidents", json=native(), headers=H).json()["id"]
    url = f"/api/incidents/{iid}/transitions"
    assert client.post(url, json={"to": "CLOSED"}, headers=H).status_code == 409
    r = client.post(url, json={"to": "CLOSED", "note": "false alarm"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "CLOSED"
    assert client.post(url, json={"to": "FLYING"}, headers=H).status_code == 422


def test_timings_and_aggregate_means(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as s:
        for i, (diag, res) in enumerate([(10, 40), (20, None)]):
            inc = Incident(
                id=f"inc-{i:010d}",
                title="t",
                description="",
                status="DETECTED",
                affected_resources=["alb/x"],
                created_at=T0,
                extra={},
                alarm_time=T0,
                onset_at=T0 - timedelta(minutes=4 + 2 * i),
            )
            s.add(inc)
            s.flush()
            lifecycle.record_initial(s, inc, "test", T0)
            lifecycle.transition(s, inc, "INVESTIGATING", "test", at=T0 + timedelta(minutes=1))
            lifecycle.transition(s, inc, "DIAGNOSED", "test", at=T0 + timedelta(minutes=diag))
            if res:
                lifecycle.transition(s, inc, "RESOLVED", "test", at=T0 + timedelta(minutes=res))
        s.commit()
        one = s.get(Incident, "inc-0000000000")
        t = lifecycle.timings(one, lifecycle.transitions_of(s, one.id))
        assert (t["time_to_detect_min"], t["time_to_diagnose_min"], t["time_to_resolve_min"]) == (
            4.0,
            10.0,
            40.0,
        )
        agg = lifecycle.aggregate(s)
    assert agg["incidents"] == 2 and agg["by_status"]["RESOLVED"] == 1
    assert agg["mean_time_to_detect_min"] == 5.0 and agg["time_to_detect_n"] == 2
    assert agg["mean_time_to_diagnose_min"] == 15.0
    assert agg["mean_time_to_resolve_min"] == 40.0 and agg["time_to_resolve_n"] == 1
    with pytest.raises(lifecycle.TransitionError):
        with sessions() as s:
            lifecycle.transition(s, s.get(Incident, "inc-0000000001"), "DETECTED", "x")


def test_lifecycle_metrics_endpoint(client):
    client.post(
        "/api/incidents", json=native(onset_at=(T0 - timedelta(minutes=6)).isoformat()), headers=H
    )
    m = client.get("/api/metrics/lifecycle", headers=H).json()
    assert m["incidents"] == 1 and m["mean_time_to_detect_min"] == 6.0
    assert m["by_status"]["DETECTED"] == 1 and m["mean_time_to_resolve_min"] is None


# ------------------------------------------------------------------ telemetry
def _event(i, msg="GET /x 200", source=EventSource.CLOUDWATCH_LOG, severity=Severity.INFO):
    return CanonicalEvent.build(
        timestamp=T0 + timedelta(seconds=i),
        source=source,
        service="ecs",
        resource_id="ecs/service/web",
        event_type="log_line",
        message=msg,
        severity=severity,
        metadata={"log_group": "/ecs/web", "log_stream": "s"},
        raw_ref=f"cwlogs:x:{i}",
    ).model_dump(mode="json")


def test_event_and_inventory_ingest(client):
    iid = client.post("/api/incidents", json=native(), headers=H).json()["id"]
    url = f"/api/incidents/{iid}/events"
    r = client.post(url, json={"events": [_event(1), _event(2)]}, headers=H)
    assert r.json() == {"received": 2, "added": 2, "duplicates": 0}
    assert client.post(url, json={"events": [_event(1)]}, headers=H).json()["duplicates"] == 1
    bad = _event(3)
    bad["event_id"] = "ev_0000000000000000"
    assert client.post(url, json={"events": [bad]}, headers=H).status_code == 422
    assert (
        client.post(
            "/api/incidents/inc-nope000000/events", json={"events": [_event(4)]}, headers=H
        ).status_code
        == 404
    )
    inv = {
        "resources": [
            {
                "resource_id": "alb/web-lb",
                "resource_type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
                "service": "elb",
                "role": "alb",
            }
        ],
        "relationships": [
            {
                "source_id": "alb/web-lb",
                "target_id": "ecs/service/web",
                "relation_type": "routes_to",
            }
        ],
    }
    assert client.post(f"/api/incidents/{iid}/inventory", json=inv, headers=H).json() == {
        "resources": 1,
        "relationships": 1,
    }
    g = client.get(f"/api/incidents/{iid}/graph", headers=H).json()
    alb = next(n for n in g["nodes"] if n["id"] == "alb/web-lb")
    web = next(n for n in g["nodes"] if n["id"] == "ecs/service/web")
    assert alb["affected"] and web["downstream"] and web["can_cause"]


def test_too_many_events_and_body_too_large(dataset, tmp_path):
    c, _, _ = make_client(dataset, tmp_path, max_events_per_request=2, max_body_bytes=4096)
    with c:
        iid = c.post("/api/incidents", json=native(), headers=H).json()["id"]
        r = c.post(
            f"/api/incidents/{iid}/events",
            json={"events": [_event(i) for i in range(3)]},
            headers=H,
        )
        assert r.status_code == 413
        huge = c.post(
            "/api/incidents",
            content=b"{" + b" " * 5000 + b"}",
            headers={**H, "content-type": "application/json"},
        )
        assert huge.status_code == 413


def test_offline_import_and_read_endpoints(client, dataset):
    iid = import_dev(client, dataset)
    again = client.post("/api/offline/import", json={"offline_incident_id": iid}, headers=H)
    assert again.status_code == 200 and again.json()["created"] is False
    assert (
        client.post(
            "/api/offline/import", json={"offline_incident_id": "inc-ffffffffff"}, headers=H
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/offline/import", json={"offline_incident_id": "../etc"}, headers=H
        ).status_code
        == 422
    )
    detail = client.get(f"/api/incidents/{iid}", headers=H).json()
    assert detail["source"] == "replay" and detail["counts"]["events"] > 100
    assert "ground_truth" not in json.dumps(detail) and "taxonomy_label" not in json.dumps(detail)
    ev = client.get(f"/api/incidents/{iid}/events?source=cloudtrail&limit=5", headers=H).json()
    assert ev["items"] and all(e["source"] == "cloudtrail" for e in ev["items"])
    assert ev["total"] >= len(ev["items"])
    logs = client.get(f"/api/incidents/{iid}/logs?severity=ERROR", headers=H).json()["items"]
    assert all(e["source"] == "cloudwatch_log" and e["severity"] == "ERROR" for e in logs)
    ct = client.get(f"/api/incidents/{iid}/cloudtrail", headers=H).json()["items"]
    assert {e["source"] for e in ct} <= {"cloudtrail", "aws_config"}
    word = logs[0]["message"].split()[-1] if logs else "GET"
    found = client.get(f"/api/incidents/{iid}/logs", params={"q": word}, headers=H).json()
    assert found["total"] >= 1
    assert client.get(f"/api/incidents/{iid}/events?source=bogus", headers=H).status_code == 422
    series = client.get(f"/api/incidents/{iid}/metrics", headers=H).json()["series"]
    assert series and all(s["points"] for s in series)
    one = client.get(
        f"/api/incidents/{iid}/metrics",
        params={"metric": series[0]["metric"], "resource_id": series[0]["resource_id"]},
        headers=H,
    ).json()["series"]
    assert len(one) == 1
    tl = client.get(f"/api/incidents/{iid}/timeline", headers=H).json()["items"]
    assert any(i["kind"] == "transition" for i in tl) and any(i["kind"] == "event" for i in tl)
    assert [i["at"] for i in tl] == sorted(i["at"] for i in tl)


# ------------------------------------------------------------------ async diagnosis
def test_async_diagnosis_job_end_to_end(client, dataset):
    iid = import_dev(client, dataset, 1)
    r = client.post(f"/api/incidents/{iid}/diagnose", json={"samples": 1}, headers=H)
    assert r.status_code == 202 and r.json()["status"] == "PENDING"
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "SUCCEEDED", job
    assert job["diagnosis_id"] and job["requested_by"] == key_id(KEY)
    detail = client.get(f"/api/incidents/{iid}", headers=H).json()
    assert detail["status"] == "DIAGNOSED" and detail["severity"]
    sys = [t for t in detail["transitions"] if t["actor"] == "system"]
    assert [t["to"] for t in sys] == ["INVESTIGATING", "DIAGNOSED"]
    assert detail["timings"]["time_to_diagnose_min"] is not None
    assert detail["timings"]["time_to_detect_min"] is not None  # onset from the pipeline
    d = client.get(f"/api/incidents/{iid}/diagnosis", headers=H).json()
    assert d["valid"] and d["diagnosis"]["root_cause"]["taxonomy_label"]
    assert d["severity"]["method"] == "deterministic-v1" and d["llm_severity_suggestion"]
    assert d["citation"]["cited"] and d["label"] == "smoke-test / synthetic"
    assert len(client.get(f"/api/incidents/{iid}/diagnoses", headers=H).json()["items"]) == 1
    evidence = client.get(f"/api/incidents/{iid}/evidence", headers=H).json()["items"]
    assert [e["rank"] for e in evidence] == list(range(1, len(evidence) + 1))
    assert all(e["event"] for e in evidence)
    series = client.get(f"/api/incidents/{iid}/metrics", headers=H).json()["series"]
    assert any(s["anomalies"] for s in series)
    # Re-diagnosis from DIAGNOSED keeps the status (no automatic transition).
    r2 = client.post(f"/api/incidents/{iid}/diagnose", json={"condition": "A1"}, headers=H)
    assert wait_job(client, r2.json()["job_id"])["status"] == "SUCCEEDED"
    assert client.get(f"/api/incidents/{iid}", headers=H).json()["status"] == "DIAGNOSED"


def test_job_failures_and_unknowns(client):
    iid = client.post("/api/incidents", json=native(), headers=H).json()["id"]
    job = wait_job(
        client, client.post(f"/api/incidents/{iid}/diagnose", headers=H).json()["job_id"]
    )
    assert job["status"] == "FAILED" and "no telemetry" in job["error"]
    assert client.get("/api/jobs/job-nothere", headers=H).status_code == 404
    assert client.post("/api/incidents/inc-none000000/diagnose", headers=H).status_code == 404
    assert (
        client.post(
            f"/api/incidents/{iid}/diagnose", json={"condition": "B1"}, headers=H
        ).status_code
        == 422
    )
    assert client.get(f"/api/incidents/{iid}/diagnosis", headers=H).status_code == 404


def test_interrupted_jobs_are_failed_at_startup(dataset, tmp_path):
    settings = make_settings(dataset, tmp_path)
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        s.add(
            Incident(
                id="inc-aaaaaaaaaa",
                title="t",
                description="",
                status="INVESTIGATING",
                affected_resources=["alb/x"],
                created_at=T0,
                extra={},
            )
        )
        s.add(
            DiagnosisJob(
                id="job-stale",
                incident_id="inc-aaaaaaaaaa",
                status="RUNNING",
                params={},
                created_at=T0,
                requested_by="x",
            )
        )
        s.commit()
    with TestClient(create_app(settings, engine)) as c:
        job = c.get("/api/jobs/job-stale", headers=H).json()
    assert job["status"] == "FAILED" and "interrupted" in job["error"]


# ------------------------------------------------------------------ alarm webhook
def eventbridge(state="ALARM", name="web-5xx"):
    return {
        "version": "0",
        "id": "e1",
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": "123456789012",
        "time": "2026-10-07T12:00:00Z",
        "region": "us-east-1",
        "detail": {
            "alarmName": name,
            "state": {
                "value": state,
                "reason": "Threshold Crossed",
                "timestamp": "2026-10-07T12:00:00.000+0000",
            },
            "previousState": {"value": "OK"},
            "configuration": {
                "metrics": [
                    {
                        "id": "m1",
                        "metricStat": {
                            "metric": {
                                "namespace": "AWS/ApplicationELB",
                                "name": "HTTPCode_Target_5XX_Count",
                                "dimensions": {"LoadBalancer": "app/web-lb/50dc6c495c0c9188"},
                            }
                        },
                    }
                ]
            },
        },
    }


def test_eventbridge_alarm_creates_and_dedups(client):
    r = client.post("/api/incidents", json=eventbridge(), headers=H)
    assert r.status_code == 201, r.text
    inc = r.json()
    assert inc["affected_resources"] == ["alb/web-lb"] and inc["source"] == "alarm-eventbridge"
    assert inc["alarm_time"].startswith("2026-10-07T12:00")
    again = client.post("/api/incidents", json=eventbridge(), headers=H)
    assert again.status_code == 200 and again.json()["id"] == inc["id"]
    ok = client.post("/api/incidents", json=eventbridge(state="OK"), headers=H)
    assert ok.status_code == 202 and ok.json()["status"] == "ignored"
    nodims = eventbridge()
    nodims["detail"]["configuration"]["metrics"][0]["metricStat"]["metric"]["dimensions"] = {}
    assert client.post("/api/incidents", json=nodims, headers=H).status_code == 422
    wrong = eventbridge()
    wrong["detail-type"] = "EC2 Instance State-change Notification"
    assert client.post("/api/incidents", json=wrong, headers=H).status_code == 400


def test_dimension_mapping():
    assert resources_from_dimensions("AWS/RDS", {"DBInstanceIdentifier": "db-main"}) == [
        "rds/db-main"
    ]
    assert resources_from_dimensions("AWS/ECS", {"ClusterName": "c", "ServiceName": "web"}) == [
        "ecs/service/web"
    ]
    assert resources_from_dimensions("AWS/Lambda", {"FunctionName": "api"}) == [
        "lambda/function/api"
    ]
    assert resources_from_dimensions("AWS/SQS", {"QueueName": "jobs"}) == ["sqs/queue/jobs"]
    assert resources_from_dimensions("AWS/EC2", {"InstanceId": "i-0abc"}) == ["ec2/instance/i-0abc"]


class SnsKit:
    """Signs SNS messages with a locally generated key; serves the cert to the verifier."""

    URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem"

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
        now = dt.datetime.now(dt.UTC)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(1)
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1))
            .sign(self.key, hashes.SHA256())
        )
        self.fetched: list[str] = []

    def fetch(self, url):
        self.fetched.append(url)
        return self.cert.public_bytes(serialization.Encoding.PEM)

    def sign(self, msg, version="2"):
        msg = dict(msg, SignatureVersion=version, SigningCertURL=self.URL)
        algo = hashes.SHA256() if version == "2" else hashes.SHA1()
        sig = self.key.sign(SnsVerifier.string_to_sign(msg), padding.PKCS1v15(), algo)
        msg["Signature"] = base64.b64encode(sig).decode()
        return msg


def sns_alarm(state="ALARM", topic="arn:aws:sns:us-east-1:123456789012:alarms"):
    return {
        "Type": "Notification",
        "MessageId": "m-1",
        "TopicArn": topic,
        "Subject": "ALARM",
        "Timestamp": "2026-10-07T12:00:05.000Z",
        "Message": json.dumps(
            {
                "AlarmName": "db-latency",
                "AWSAccountId": "123456789012",
                "NewStateValue": state,
                "OldStateValue": "OK",
                "NewStateReason": "Threshold crossed",
                "StateChangeTime": "2026-10-07T12:00:00.000+0000",
                "Region": "US East (N. Virginia)",
                "Trigger": {
                    "MetricName": "ReadLatency",
                    "Namespace": "AWS/RDS",
                    "Threshold": 0.05,
                    "Dimensions": [{"name": "DBInstanceIdentifier", "value": "db-main"}],
                },
            }
        ),
    }


def test_sns_webhook_verifies_signatures(dataset, tmp_path):
    kit = SnsKit()
    c, _, _ = make_client(
        dataset,
        tmp_path,
        verifier=SnsVerifier(kit.fetch),
        allowed_sns_topic_arns=["arn:aws:sns:us-east-1:123456789012:alarms"],
    )
    with c:
        for version in ("1", "2"):
            r = c.post("/api/incidents", json=kit.sign(sns_alarm(), version), headers=H)
            assert r.status_code in (200, 201), r.text
        assert r.json()["affected_resources"] == ["rds/db-main"]
        assert r.json()["source"] == "alarm-sns" and kit.fetched
        tampered = kit.sign(sns_alarm())
        tampered["Message"] = tampered["Message"].replace("db-main", "db-evil")
        assert c.post("/api/incidents", json=tampered, headers=H).status_code == 403
        unsigned = sns_alarm()
        assert c.post("/api/incidents", json=unsigned, headers=H).status_code == 403
        evil_host = kit.sign(sns_alarm())
        evil_host["SigningCertURL"] = "https://attacker.example.com/cert.pem"
        assert c.post("/api/incidents", json=evil_host, headers=H).status_code == 403
        other_topic = kit.sign(sns_alarm(topic="arn:aws:sns:us-east-1:123456789012:other"))
        assert c.post("/api/incidents", json=other_topic, headers=H).status_code == 403
        sub = kit.sign(
            {
                "Type": "SubscriptionConfirmation",
                "MessageId": "m2",
                "Token": "t",
                "TopicArn": "arn:aws:sns:us-east-1:123456789012:alarms",
                "Message": "confirm",
                "SubscribeURL": "https://sns.example/confirm",
                "Timestamp": "2026-10-07T12:00:00Z",
            }
        )
        r = c.post("/api/incidents", json=sub, headers=H)
        assert r.status_code == 202 and r.json()["status"] == "subscription_not_confirmed"
        ok = c.post("/api/incidents", json=kit.sign(sns_alarm("OK")), headers=H)
        assert ok.status_code == 202


def test_sns_basic_auth_form_used_by_subscriptions(dataset, tmp_path):
    kit = SnsKit()
    c, _, _ = make_client(dataset, tmp_path, verifier=SnsVerifier(kit.fetch))
    basic = "Basic " + base64.b64encode(f"sns:{KEY}".encode()).decode()
    with c:
        r = c.post("/api/incidents", json=kit.sign(sns_alarm()), headers={"Authorization": basic})
        assert r.status_code == 201
        wrong = "Basic " + base64.b64encode(b"sns:nope-nope").decode()
        assert (
            c.post(
                "/api/incidents", json=kit.sign(sns_alarm()), headers={"Authorization": wrong}
            ).status_code
            == 401
        )


# ------------------------------------------------------------------ security
def _all_api_calls(iid="inc-0000000000"):
    return [
        ("post", "/api/incidents"),
        ("get", "/api/incidents"),
        ("get", f"/api/incidents/{iid}"),
        ("post", f"/api/incidents/{iid}/transitions"),
        ("post", f"/api/incidents/{iid}/events"),
        ("post", f"/api/incidents/{iid}/inventory"),
        ("get", f"/api/incidents/{iid}/events"),
        ("get", f"/api/incidents/{iid}/logs"),
        ("get", f"/api/incidents/{iid}/cloudtrail"),
        ("get", f"/api/incidents/{iid}/metrics"),
        ("get", f"/api/incidents/{iid}/timeline"),
        ("get", f"/api/incidents/{iid}/graph"),
        ("get", f"/api/incidents/{iid}/evidence"),
        ("post", f"/api/incidents/{iid}/diagnose"),
        ("get", f"/api/incidents/{iid}/diagnoses"),
        ("get", f"/api/incidents/{iid}/diagnosis"),
        ("get", "/api/jobs/job-1"),
        ("get", "/api/offline/incidents"),
        ("post", "/api/offline/import"),
        ("get", "/api/metrics/lifecycle"),
        ("get", "/api/audit"),
        ("get", "/api/experiments"),
        ("get", "/api/experiments/exp-000000000000"),
    ]


def test_every_api_route_requires_a_key(dataset, tmp_path):
    # Large burst so this test measures authentication only (rate limits: own test).
    client, _, _ = make_client(dataset, tmp_path, rate_limit_burst=1000)
    paths = client.app.openapi()["paths"]
    routes = {(m, p) for p, ops in paths.items() if p.startswith("/api") for m in ops}
    calls = _all_api_calls()
    covered = {
        (
            m,
            p.replace("inc-0000000000", "{incident_id}")
            .replace("job-1", "{job_id}")
            .replace("exp-000000000000", "{experiment_id}"),
        )
        for m, p in calls
    }
    assert routes == covered, routes ^ covered  # the list above covers every route
    for method, path in calls:
        for headers in (
            {},
            {"X-API-Key": "wrong-key-123456"},
            {"Authorization": "Basic " + base64.b64encode(b"u:wrong").decode()},
        ):
            r = getattr(client, method)(path, headers=headers)
            assert r.status_code == 401, (method, path, headers, r.status_code)
    assert client.get("/api/incidents", headers={"X-API-Key": OTHER_KEY}).status_code == 200


def test_fail_closed_without_keys_and_in_aws_mode(dataset, tmp_path):
    c, _, _ = make_client(dataset, tmp_path, api_keys=[])
    with c:
        r = c.get("/api/incidents", headers=H)
        assert r.status_code == 401 and "not configured" in r.json()["detail"]
    open_c, _, _ = make_client(dataset, tmp_path / "x", api_keys=[], auth_disabled=True)
    with open_c:
        assert open_c.get("/api/incidents").status_code == 200
    settings = make_settings(dataset, tmp_path, auth_disabled=True).model_copy(
        update={"data_mode": "aws"}
    )
    with pytest.raises(AuthConfigError):
        create_app(settings, create_engine("sqlite://"))


def test_rate_limit_per_client(dataset, tmp_path):
    clock = Clock()
    c, _, _ = make_client(
        dataset, tmp_path, clock=clock, rate_limit_per_minute=60, rate_limit_burst=3
    )
    with c:
        assert [c.get("/api/incidents", headers=H).status_code for _ in range(3)] == [200] * 3
        r = c.get("/api/incidents", headers=H)
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        # Another key has its own bucket.
        assert c.get("/api/incidents", headers={"X-API-Key": OTHER_KEY}).status_code == 200
        clock.t += 2  # 60/min refills one token per second
        assert c.get("/api/incidents", headers=H).status_code == 200
        # Guessing keys is limited per client address too.
        guesses = [
            c.get("/api/incidents", headers={"X-API-Key": f"guess-{i:08d}"}).status_code
            for i in range(5)
        ]
        assert guesses[:3] == [401] * 3 and 429 in guesses


def test_audit_log_records_changes_and_denials_without_keys(client, caplog):
    caplog.set_level(logging.INFO)
    iid = client.post("/api/incidents", json=native(), headers=H).json()["id"]
    client.post(f"/api/incidents/{iid}/transitions", json={"to": "INVESTIGATING"}, headers=H)
    client.get("/api/incidents", headers={"X-API-Key": "bad-key-77777777"})
    items = client.get("/api/audit", headers=H).json()["items"]
    actions = [i["action"] for i in items]
    assert "incident.create.native" in actions and "incident.transition" in actions
    assert "denied" in actions
    denial = next(i for i in items if i["action"] == "denied")
    assert denial["actor"].startswith("ip:") and denial["status_code"] == 401
    create = next(i for i in items if i["action"] == "incident.create.native")
    assert create["actor"] == key_id(KEY) and create["incident_id"] == iid
    assert client.get(f"/api/audit?incident_id={iid}", headers=H).json()["items"]
    dump = json.dumps(items) + caplog.text
    assert KEY not in dump and "bad-key-77777777" not in dump


def test_no_secret_leakage_in_responses_logs_and_errors(dataset, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    client, _, _ = make_client(dataset, tmp_path, raise_errors=False)
    iid = client.post("/api/incidents", json=native(), headers=H).json()["id"]
    leaky = _event(
        5,
        "db connect failed: password=Sup3rS3cret! token=eyJhbGciOi.eyJzdWIiOiIx.c2ln",
        severity=Severity.ERROR,
    )
    client.post(f"/api/incidents/{iid}/events", json={"events": [leaky]}, headers=H)
    for path in (
        f"/api/incidents/{iid}/events",
        f"/api/incidents/{iid}/logs",
        f"/api/incidents/{iid}/timeline",
    ):
        text = client.get(path, headers=H).text
        assert "Sup3rS3cret!" not in text and "eyJhbGciOi.eyJzdWIiOiIx.c2ln" not in text, path
        assert "SECRET_" in text
    # Validation errors never echo submitted values.
    r = client.post(
        "/api/incidents", json=native(title="x" * 400, description="api_key=sk-LEAK123"), headers=H
    )
    assert r.status_code == 422 and "sk-LEAK123" not in r.text and "xxxxxxxxxx" not in r.text
    # Unhandled errors: generic body, scrubbed log.
    from app.api import service

    def boom(*a, **k):
        raise RuntimeError("database password=hunter2-boom")

    monkeypatch.setattr(service, "query_events", boom)
    err = client.get(f"/api/incidents/{iid}/events", headers=H)
    assert err.status_code == 500 and err.json()["detail"] == "internal error"
    assert "hunter2-boom" not in err.text and "hunter2-boom" not in caplog.text
    assert KEY not in caplog.text and OTHER_KEY not in caplog.text


def test_job_errors_are_scrubbed(client, dataset, monkeypatch):
    iid = import_dev(client, dataset, 2)
    jobs = client.app.state.jobs

    def broken():
        raise RuntimeError("LLM gateway rejected Authorization: Bearer sk-abcdefghijklmnop")

    monkeypatch.setattr(jobs, "engine", broken)
    job = wait_job(
        client, client.post(f"/api/incidents/{iid}/diagnose", headers=H).json()["job_id"]
    )
    assert job["status"] == "FAILED" and "sk-abcdefghijklmnop" not in job["error"]


def test_experiments_endpoints(client):
    items = client.get("/api/experiments", headers=H).json()["items"]
    assert any(i["label"] == "smoke-test / synthetic" for i in items)
    exp = items[0]["experiment_id"]
    detail = client.get(f"/api/experiments/{exp}", headers=H).json()
    assert detail["manifest"]["experiment_id"] == exp and detail["summary"]
    assert client.get("/api/experiments/exp-..%2F..", headers=H).status_code in (404, 422)
    assert client.get("/api/experiments/exp-000000000000", headers=H).status_code == 404


def test_audit_rows_exist_in_db(client):
    client.post("/api/incidents", json=native(), headers=H)
    with client.app.state.sessions() as s:
        assert s.scalars(select(AuditLog)).first() is not None


def test_offline_listing_and_enriched_diagnosis(client, dataset):
    listing = client.get("/api/offline/incidents?split=dev", headers=H).json()
    assert listing["items"] and all(i["split"] == "dev" for i in listing["items"])
    assert set(listing["items"][0]) == {"incident_id", "split", "title", "alarm_time"}
    assert "category" not in json.dumps(listing) and "taxonomy" not in json.dumps(listing)
    assert client.get("/api/offline/incidents?split=prod", headers=H).status_code == 422
    iid = import_dev(client, dataset, 3)
    job = client.post(f"/api/incidents/{iid}/diagnose", json={"samples": 0}, headers=H).json()
    assert wait_job(client, job["job_id"])["status"] == "SUCCEEDED"
    d = client.get(f"/api/incidents/{iid}/diagnosis", headers=H).json()
    cited = [e["evidence_id"] for e in d["diagnosis"]["supporting_evidence"]]
    assert cited and set(cited) <= set(d["evidence_events"])
    for evd in cited:
        assert d["evidence_events"][evd]["event_id"] and d["evidence_events"][evd]["timestamp"]
    path = d["dependency_path"]
    root = d["diagnosis"]["root_cause"]["resource_id"]
    assert path[0] == root and path[-1] in d["diagnosis"]["impact_analysis"]["resources"] + [
        r for r in client.get(f"/api/incidents/{iid}", headers=H).json()["affected_resources"]
    ]
    items = client.get(f"/api/incidents/{iid}/diagnoses", headers=H).json()["items"]
    assert items[0]["evidence_events"] == d["evidence_events"]
