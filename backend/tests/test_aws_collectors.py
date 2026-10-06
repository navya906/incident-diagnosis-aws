"""Phase 2: real AWS collectors against moto (and botocore Stubber where moto lacks the API)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aws_env
import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from moto import mock_aws

from app.collectors.aws_collector import AwsCollector, EventCache, required_iam_actions
from app.collectors.aws_common import (
    AwsClients,
    RateLimiter,
    call_with_backoff,
    paginate,
    sanitize,
)
from app.collectors.cloudtrail import CloudTrailCollector
from app.collectors.cloudwatch_logs import CloudWatchLogsCollector, infer_severity
from app.collectors.cloudwatch_metrics import (
    METRIC_CATALOG,
    CloudWatchMetricsCollector,
    choose_period,
)
from app.collectors.config_history import ConfigHistoryCollector
from app.collectors.inventory import InventoryDiscovery
from app.collectors.resources import AwsResource, ResourceIndex, canonical_id_from_arn
from app.config import AwsSettings
from app.contracts.events import (
    SOURCE_SCHEMAS,
    CanonicalEvent,
    EventSource,
    Severity,
    contract_violations,
)
from app.interfaces.collector import CollectionRequest
from app.offline.dataset import generate_dataset
from app.offline.replay import ReplayCollector
from app.offline.topology import build_topology

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def aws(monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": aws_env.REGION,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        t0 = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=2)
        env = aws_env.build(t0)
        settings = AwsSettings(region=aws_env.REGION, logs_filter_pattern=None)
        clients = AwsClients(settings)
        inventory = InventoryDiscovery(clients).discover([env.alb_arn, env.function_arn])
        yield env, clients, inventory


def _request(env, inventory, **kw) -> CollectionRequest:
    return CollectionRequest(
        incident_id="inc-real",
        resource_ids=[r.resource_id for r in inventory.resources],
        window_start=env.t0 - timedelta(minutes=5),
        window_end=env.t0 + timedelta(minutes=30),
        **kw,
    )


# ------------------------------------------------------------------ canonical ids
@pytest.mark.parametrize(
    ("arn", "expected"),
    [
        ("arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/web/abc", "alb/web"),
        ("arn:aws:ecs:us-east-1:1:service/prod/web", "ecs/service/web"),
        ("arn:aws:ecs:us-east-1:1:service/web", "ecs/service/web"),
        ("arn:aws:rds:us-east-1:1:db:orders-db", "rds/orders-db"),
        ("arn:aws:lambda:us-east-1:1:function:worker:live", "lambda/function/worker"),
        ("arn:aws:ec2:us-east-1:1:instance/i-0abc", "ec2/instance/i-0abc"),
        ("arn:aws:ec2:us-east-1:1:security-group/sg-1", "sg/sg-1"),
        ("arn:aws:sqs:us-east-1:1:jobs", "sqs/queue/jobs"),
        ("arn:aws:iam::1:role/service-role/web-task", "iam/role/web-task"),
    ],
)
def test_canonical_id_from_arn(arn, expected):
    assert canonical_id_from_arn(arn) == expected


def test_canonical_id_rejects_unsupported():
    with pytest.raises(ValueError):
        canonical_id_from_arn("arn:aws:dynamodb:us-east-1:1:table/t")
    with pytest.raises(ValueError):
        canonical_id_from_arn("not-an-arn")


# ------------------------------------------------------------------ inventory discovery
def test_inventory_discovers_topology_and_relationships(aws):
    env, _, inv = aws
    ids = {r.resource_id for r in inv.resources}
    assert {
        "alb/app-lb",
        "ecs/service/web",
        "rds/orders-db",
        f"sg/{env.db_sg}",
        "iam/role/web-task",
        "lambda/function/worker",
        "sqs/queue/jobs",
        "iam/role/worker-exec",
    } <= ids
    edges = {(e.source_id, e.relation_type, e.target_id) for e in inv.relationships}
    assert ("alb/app-lb", "routes_to", "ecs/service/web") in edges
    assert ("ecs/service/web", "connects_to", "rds/orders-db") in edges
    assert ("rds/orders-db", "secured_by", f"sg/{env.db_sg}") in edges
    assert ("ecs/service/web", "assumes_role", "iam/role/web-task") in edges
    assert ("lambda/function/worker", "polls", "sqs/queue/jobs") in edges
    idx = inv.index
    assert idx.get("ecs/service/web").log_groups == [env.log_group]
    assert idx.get("rds/orders-db").config_id.startswith("db-")
    assert idx.get("alb/app-lb").dimensions["per_target_group"]
    resources, rels = inv.records()
    assert all(r.resource_id in ids for r in resources) and rels


def test_inventory_bounded_and_tolerates_bad_seed(aws):
    _, clients, _ = aws
    inv = InventoryDiscovery(clients, max_resources=1).discover(
        [aws[0].alb_arn, "arn:aws:rds:us-east-1:123456789012:db:missing"]
    )
    assert len(inv.resources) <= 2  # the seed plus at most a linked DB
    assert any("max_resources" in w or "could not describe" in w for w in inv.warnings)


# ------------------------------------------------------------------ metrics
def test_metrics_collector_batches_and_sums_dimension_sets(aws):
    env, clients, inv = aws
    c = CloudWatchMetricsCollector(clients, inv.index)
    events = c.collect(_request(env, inv))
    five = [e for e in events if e.metric == "HTTPCode_Target_5XX_Count"]
    assert len(five) == 10 and max(e.value for e in five) == 400.0
    assert {e.resource_id for e in five} == {"alb/app-lb"}
    unhealthy = [e for e in events if e.metric == "UnHealthyHostCount"]
    assert unhealthy and max(e.value for e in unhealthy) == 2.0
    conns = [e for e in events if e.metric == "DatabaseConnections"]
    assert [e.value for e in conns] == [60.0 + 4 * i for i in range(10)]
    assert all(e.metadata["period_seconds"] == 60 for e in events)
    assert events == sorted(events, key=lambda e: (e.timestamp, e.event_id))


def test_metrics_skip_unknown_resource_and_respect_sources(aws):
    env, clients, inv = aws
    c = CloudWatchMetricsCollector(clients, inv.index)
    req = CollectionRequest(
        incident_id="i",
        resource_ids=["rds/does-not-exist"],
        window_start=env.t0,
        window_end=env.t0 + timedelta(minutes=5),
    )
    assert c.collect(req) == [] and "unknown resource" in c.warnings[0]
    assert c.collect(_request(env, inv, sources=[EventSource.CLOUDTRAIL])) == []


def test_choose_period_follows_cloudwatch_retention():
    now = datetime(2026, 1, 31, tzinfo=UTC)
    assert choose_period(now - timedelta(days=1), now) == 60
    assert choose_period(now - timedelta(days=20), now) == 300
    assert choose_period(now - timedelta(days=90), now) == 3600


# ------------------------------------------------------------------ logs
def test_logs_collector_reads_owned_groups_and_scrubs_secrets(aws):
    env, clients, inv = aws
    c = CloudWatchLogsCollector(clients, inv.index)
    events = c.collect(_request(env, inv))
    assert [e.severity for e in events] == [Severity.INFO, Severity.ERROR, Severity.WARNING]
    assert {e.resource_id for e in events} == {"ecs/service/web"}
    assert all(e.metadata["log_group"] == env.log_group for e in events)
    assert "hunter2" not in events[2].message


def test_logs_collector_filters_and_bounds(aws):
    env, _, inv = aws
    clients = AwsClients(AwsSettings(logs_filter_pattern="ERROR", logs_max_events_per_group=5))
    events = CloudWatchLogsCollector(clients, inv.index).collect(_request(env, inv))
    assert [e.severity for e in events] == [Severity.ERROR]
    capped = AwsClients(AwsSettings(logs_filter_pattern=None, logs_max_events_per_group=1))
    c = CloudWatchLogsCollector(capped, inv.index)
    assert len(c.collect(_request(env, inv))) == 1
    assert any("truncated" in w for w in c.warnings)


def test_logs_missing_group_is_a_warning(aws):
    env, clients, inv = aws
    idx = ResourceIndex(list(inv.index))
    idx.add(idx.get("ecs/service/web").model_copy(update={"log_groups": ["/nope"]}))
    c = CloudWatchLogsCollector(clients, idx)
    assert c.collect(_request(env, inv)) == []
    assert any("not found" in w for w in c.warnings)


@pytest.mark.parametrize(
    ("msg", "level"),
    [
        ("FATAL: too many clients", Severity.CRITICAL),
        ("AccessDeniedException: not authorized", Severity.ERROR),
        ("WARN slow", Severity.WARNING),
        ("GET / 200", Severity.INFO),
    ],
)
def test_infer_severity(msg, level):
    assert infer_severity(msg) == level


# ------------------------------------------------------------------ CloudTrail (Stubber)
def _ct_event(eid: str, name: str, ts: datetime, **detail) -> dict:
    body = {
        "eventName": name,
        "eventSource": "ec2.amazonaws.com",
        "awsRegion": "us-east-1",
        "userIdentity": {"type": "IAMUser", "arn": "arn:aws:iam::123456789012:user/deployer"},
        **detail,
    }
    return {
        "EventId": eid,
        "EventName": name,
        "EventSource": "ec2.amazonaws.com",
        "EventTime": ts,
        "ReadOnly": "true" if name.startswith("Describe") else "false",
        "CloudTrailEvent": json.dumps(body),
    }


def _stubbed(service: str, clients: AwsClients) -> Stubber:
    client = boto3.client(service, region_name=aws_env.REGION)
    clients.set_client(service, client)
    stub = Stubber(client)
    stub.activate()
    return stub


def test_cloudtrail_rate_limited_paginated_dedup_and_sanitized():
    t0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    clients = AwsClients(AwsSettings())
    sg = AwsResource(
        resource_id="sg/sg-1",
        resource_type="AWS::EC2::SecurityGroup",
        service="ec2",
        kind="sg",
        name="sg-1",
        region="us-east-1",
        cloudtrail_names=["sg-1", "db-sg"],
    )
    stub = _stubbed("cloudtrail", clients)
    revoke = _ct_event(
        "e1",
        "RevokeSecurityGroupIngress",
        t0,
        requestParameters={"groupId": "sg-1", "password": "x"},
    )
    denied = _ct_event(
        "e2",
        "AuthorizeSecurityGroupIngress",
        t0 + timedelta(minutes=1),
        errorCode="UnauthorizedOperation",
        errorMessage="not allowed",
    )
    read = _ct_event("e3", "DescribeSecurityGroups", t0 + timedelta(minutes=2))
    stub.add_response(
        "lookup_events",
        {"Events": [revoke], "NextToken": "p2"},
        {
            "LookupAttributes": [{"AttributeKey": "ResourceName", "AttributeValue": "sg-1"}],
            "StartTime": ANY,
            "EndTime": ANY,
            "MaxResults": 50,
        },
    )
    stub.add_client_error(
        "lookup_events", service_error_code="ThrottlingException", http_status_code=400
    )
    stub.add_response("lookup_events", {"Events": [denied, read]})
    stub.add_response("lookup_events", {"Events": [revoke]})  # duplicate via 2nd name
    calls: list[float] = []
    limiter = RateLimiter(2.0, clock=lambda: 0.0, sleep=calls.append)
    c = CloudTrailCollector(
        clients, ResourceIndex([sg]), rate_limiter=limiter, now=t0 + timedelta(hours=2)
    )
    import app.collectors.aws_common as common

    orig = common.call_with_backoff
    common.call_with_backoff = lambda fn, *a, **k: orig(fn, *a, sleep=lambda s: None, **k)
    try:
        events = c.collect(
            CollectionRequest(
                incident_id="i",
                resource_ids=["sg/sg-1"],
                window_start=t0 - timedelta(minutes=5),
                window_end=t0 + timedelta(minutes=30),
            )
        )
    finally:
        common.call_with_backoff = orig
    stub.assert_no_pending_responses()
    assert [e.event_type for e in events] == [
        "RevokeSecurityGroupIngress",
        "AuthorizeSecurityGroupIngress",
    ]
    assert events[1].severity == Severity.ERROR and events[1].metadata["errorCode"]
    assert events[0].metadata["requestParameters"]["password"] == "***"
    assert all(e.resource_id == "sg/sg-1" and e.raw_ref.startswith("ct:") for e in events)
    assert calls, "rate limiter must have waited between LookupEvents calls"
    assert not c.warnings


def test_cloudtrail_warns_inside_ingestion_lag():
    clients = AwsClients(AwsSettings())
    now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    c = CloudTrailCollector(clients, ResourceIndex(), now=now)
    c.collect(
        CollectionRequest(
            incident_id="i",
            resource_ids=["sg/x"],
            window_start=now - timedelta(minutes=30),
            window_end=now,
        )
    )
    assert any("ingestion lag" in w for w in c.warnings)


# ------------------------------------------------------------------ AWS Config (Stubber + moto)
def _sg_resource(config: bool = True) -> AwsResource:
    return AwsResource(
        resource_id="sg/sg-1",
        resource_type="AWS::EC2::SecurityGroup",
        service="ec2",
        kind="sg",
        name="sg-1",
        region="us-east-1",
        config_type="AWS::EC2::SecurityGroup" if config else None,
        config_id="sg-1" if config else None,
    )


def test_config_history_items_become_events():
    t0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    clients = AwsClients(AwsSettings())
    stub = _stubbed("config", clients)
    stub.add_response(
        "get_resource_config_history",
        {
            "configurationItems": [
                {
                    "configurationItemCaptureTime": t0,
                    "configurationItemStatus": "OK",
                    "configurationStateId": "17",
                    "resourceName": "db-sg",
                    "resourceType": "AWS::EC2::SecurityGroup",
                    "resourceId": "sg-1",
                }
            ]
        },
    )
    c = ConfigHistoryCollector(clients, ResourceIndex([_sg_resource()]))
    events = c.collect(
        CollectionRequest(
            incident_id="i",
            resource_ids=["sg/sg-1"],
            window_start=t0 - timedelta(minutes=5),
            window_end=t0 + timedelta(minutes=5),
        )
    )
    assert len(events) == 1 and events[0].raw_ref == "cfg:AWS::EC2::SecurityGroup:sg-1:17"
    assert not contract_violations(events[0])


def test_config_without_recorder_is_skipped_with_warning():
    t0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    clients = AwsClients(AwsSettings())
    stub = _stubbed("config", clients)
    stub.add_client_error(
        "get_resource_config_history",
        service_error_code="NoAvailableConfigurationRecorderException",
    )
    c = ConfigHistoryCollector(clients, ResourceIndex([_sg_resource()]))
    req = CollectionRequest(
        incident_id="i",
        resource_ids=["sg/sg-1"],
        window_start=t0,
        window_end=t0 + timedelta(minutes=5),
    )
    assert c.collect(req) == [] and not c.recorder_available
    assert "no configuration recorder" in c.warnings[0]


def test_config_undiscovered_resource_on_moto(aws):
    # moto returns malformed Tags for resource types it records (e.g. security groups), so this
    # exercises the "not discovered" path on a type moto does not record.
    env, clients, inv = aws
    c = ConfigHistoryCollector(clients, inv.index)
    req = _request(env, inv).model_copy(update={"resource_ids": ["rds/orders-db"]})
    assert c.collect(req) == []
    assert any("ResourceNotDiscovered" in w for w in c.warnings)
    disabled = ConfigHistoryCollector(AwsClients(AwsSettings(config_enabled=False)), inv.index)
    assert disabled.collect(_request(env, inv)) == []


# ------------------------------------------------------------------ composite + cache
def test_composite_collector_and_cache(aws, tmp_path):
    env, clients, inv = aws
    clients.set_client("cloudtrail", boto3.client("cloudtrail", region_name=aws_env.REGION))
    stub = Stubber(clients.client("cloudtrail"))
    for _ in range(sum(len(r.cloudtrail_names) for r in inv.resources)):
        stub.add_response("lookup_events", {"Events": []})
    stub.activate()
    cfg = _stubbed("config", clients)  # moto's Config history is unusable here (see above)
    cfg.add_client_error(
        "get_resource_config_history",
        service_error_code="NoAvailableConfigurationRecorderException",
    )
    clock = [1000.0]
    cache = EventCache(tmp_path, ttl_seconds=60, clock=lambda: clock[0])
    collector = AwsCollector(clients, inv.index, cache=cache)
    req = _request(env, inv)
    first = collector.collect(req)
    assert {e.source for e in first} == {EventSource.CLOUDWATCH_METRIC, EventSource.CLOUDWATCH_LOG}
    cached_files = list(tmp_path.glob("*.json"))
    # Only warning-free results are cached: metrics and (empty) CloudTrail. Logs warned about
    # missing log groups and Config about the missing recorder, so they are re-collected.
    assert any("log group" in w for w in collector.warnings)
    assert len(cached_files) == 2
    assert collector.collect(req) == first  # served from cache; no further AWS calls needed
    clock[0] += 120
    assert EventCache(tmp_path, 60, clock=lambda: clock[0]).get(cached_files[0].stem) is None


def test_from_settings_builds_collector(aws):
    env, _, _ = aws
    collector, inv = AwsCollector.from_settings(AwsSettings(), [env.alb_arn])
    assert "ecs/service/web" in collector.index and inv.relationships


# ------------------------------------------------------------------ contract: real == replay schema
def test_real_and_replay_collectors_emit_schema_identical_events(aws, tmp_path):
    env, clients, inv = aws
    real = CloudWatchMetricsCollector(clients, inv.index).collect(_request(env, inv))
    real += CloudWatchLogsCollector(clients, inv.index).collect(_request(env, inv))
    t0 = env.t0
    ct_clients = AwsClients(AwsSettings())
    stub = _stubbed("cloudtrail", ct_clients)
    stub.add_response(
        "lookup_events",
        {"Events": [_ct_event("e9", "RevokeSecurityGroupIngress", t0, errorCode="Client.X")]},
    )
    real += CloudTrailCollector(
        ct_clients,
        ResourceIndex(
            [
                AwsResource(
                    resource_id=f"sg/{env.db_sg}",
                    resource_type="AWS::EC2::SecurityGroup",
                    service="ec2",
                    kind="sg",
                    name=env.db_sg,
                    region="us-east-1",
                    cloudtrail_names=[env.db_sg],
                )
            ]
        ),
        now=t0 + timedelta(days=1),
    ).collect(
        CollectionRequest(
            incident_id="i",
            resource_ids=[f"sg/{env.db_sg}"],
            window_start=t0,
            window_end=t0 + timedelta(minutes=30),
        )
    )
    cfg_clients = AwsClients(AwsSettings())
    cstub = _stubbed("config", cfg_clients)
    cstub.add_response(
        "get_resource_config_history",
        {
            "configurationItems": [
                {
                    "configurationItemCaptureTime": t0,
                    "configurationItemStatus": "OK",
                    "configurationStateId": "3",
                }
            ]
        },
    )
    real += ConfigHistoryCollector(cfg_clients, ResourceIndex([_sg_resource()])).collect(
        CollectionRequest(
            incident_id="i",
            resource_ids=["sg/sg-1"],
            window_start=t0,
            window_end=t0 + timedelta(minutes=5),
        )
    )

    generate_dataset(tmp_path, seed=3)
    replay = ReplayCollector(tmp_path)
    replay_events = []
    for iid in replay.incident_ids()[:20]:
        replay_events += replay.collect(replay.default_request(iid))

    real_sources = {e.source for e in real}
    assert real_sources == {
        EventSource.CLOUDWATCH_METRIC,
        EventSource.CLOUDWATCH_LOG,
        EventSource.CLOUDTRAIL,
        EventSource.AWS_CONFIG,
    }
    for events in (real, replay_events):
        assert all(type(e) is CanonicalEvent for e in events)
        bad = [(e.source, p) for e in events for p in contract_violations(e)]
        assert not bad, bad[:5]
    fields = set(CanonicalEvent.model_fields)
    for src in real_sources:
        r = [e for e in real if e.source == src]
        p = [e for e in replay_events if e.source == src]
        assert r and p
        assert (
            {k for e in r for k in e.model_dump()}
            == fields
            == {k for e in p for k in e.model_dump()}
        )
        schema = SOURCE_SCHEMAS[src]
        assert {e.event_type for e in r if schema.fixed_event_type} <= {schema.fixed_event_type}


def test_simulator_metrics_match_real_catalog():
    """Replay metric names, namespaces, stats and units must be what the real collector emits."""
    import random

    role_kind = {"alb": "alb", "db": "rds", "queue": "sqs", "consumer": "lambda"}
    app_kind = {"ecs": "ecs_service", "lambda": "lambda", "ec2": "ec2", "sqs": "ecs_service"}
    for topo_kind in ("ecs", "lambda", "ec2", "sqs"):
        topo = build_topology(topo_kind, random.Random(0))
        for spec in topo.metrics:
            kind = app_kind[topo_kind] if spec.role == "app" else role_kind[spec.role]
            catalog = {(m.namespace, m.name): m for m in METRIC_CATALOG[kind]}
            m = catalog.get((spec.namespace, spec.name))
            assert m is not None, (topo_kind, spec.name)
            assert (m.stat, m.unit) == (spec.stat, spec.unit), (topo_kind, spec.name)


# ------------------------------------------------------------------ plumbing
def test_call_with_backoff_retries_throttling_only():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "Op")
        return "ok"

    assert call_with_backoff(flaky, sleep=sleeps.append) == "ok" and len(sleeps) == 2

    def broken():
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "Op")

    with pytest.raises(ClientError):
        call_with_backoff(broken, sleep=sleeps.append)


def test_rate_limiter_enforces_rate():
    now, waits = [0.0], []
    rl = RateLimiter(
        2.0, clock=lambda: now[0], sleep=lambda s: (waits.append(s), now.__setitem__(0, now[0] + s))
    )
    for _ in range(5):
        rl.acquire()
    assert sum(waits) == pytest.approx(2.0)


def test_paginate_stops_on_repeated_token():
    pages = iter([{"NextToken": "a"}, {"NextToken": "a"}])
    assert len(list(paginate(lambda **_: next(pages), "NextToken", "NextToken"))) == 2


def test_sanitize_drops_sensitive_keys_and_values():
    out = sanitize(
        {
            "DBPassword": "p",
            "nested": {"SecretString": "s", "ok": "api_key=abc"},
            "list": ["token: zzz"],
        }
    )
    assert out["DBPassword"] == "***" and out["nested"]["SecretString"] == "***"
    assert "abc" not in out["nested"]["ok"] and "zzz" not in out["list"][0]


# ------------------------------------------------------------------ security
def test_iam_policy_covers_every_api_call_and_is_read_only():
    policy = json.loads((REPO / "infra" / "iam" / "collector-readonly-policy.json").read_text())
    allowed = {a for s in policy["Statement"] if s["Effect"] == "Allow" for a in s["Action"]}
    assert required_iam_actions() <= allowed
    write_verbs = re.compile(
        r":(Put|Create|Delete|Update|Modify|Attach|Detach|Revoke|Authorize|Set)"
    )
    assert not [a for a in allowed if write_verbs.search(a)]
    assert all("*" not in a for a in allowed)


def test_no_credentials_in_source():
    key = re.compile(r"AKIA[0-9A-Z]{16}")
    secret = re.compile(r"aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{40}", re.I)
    skip = {".git", ".venv", "node_modules", ".pytest_tmp", "data", "__pycache__", ".ruff_cache"}
    hits = []
    for path in REPO.rglob("*"):
        if path.is_dir() or skip & set(path.relative_to(REPO).parts):
            continue
        if path.suffix not in {
            ".py",
            ".md",
            ".yaml",
            ".yml",
            ".json",
            ".toml",
            ".ini",
            ".txt",
            ".ps1",
            ".sh",
            ".example",
            ".cfg",
            "",
        }:
            continue
        text = path.read_text(errors="ignore")
        for m in key.finditer(text):
            if not m.group().endswith("EXAMPLE"):
                hits.append((str(path), m.group()))
        hits += [(str(path), "secret") for _ in secret.finditer(text)]
    assert not hits


def test_capture_cli_writes_replayable_observable_scenario(aws, tmp_path, monkeypatch):
    from app.collectors import capture
    from app.offline.models import ObservableScenario

    env, clients, inv = aws
    ct = _stubbed("cloudtrail", clients)
    for _ in range(sum(len(r.cloudtrail_names) for r in inv.resources)):
        ct.add_response("lookup_events", {"Events": []})
    cfg = _stubbed("config", clients)
    cfg.add_client_error(
        "get_resource_config_history",
        service_error_code="NoAvailableConfigurationRecorderException",
    )
    monkeypatch.setattr(
        capture.AwsCollector,
        "from_settings",
        classmethod(lambda cls, settings, seeds, session=None: (cls(clients, inv.index), inv)),
    )
    start, end = env.t0 - timedelta(minutes=5), env.t0 + timedelta(minutes=30)
    assert (
        capture.main(
            [
                "--seed-arn",
                env.alb_arn,
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
                "--title",
                "ALARM: 5xx",
                "--description",
                "Users see errors",
                "--out",
                str(tmp_path),
            ]
        )
        == 0
    )
    (path,) = tmp_path.glob("real-*.json")
    scenario = ObservableScenario.model_validate_json(path.read_text())
    assert scenario.incident.affected_resources == ["alb/app-lb"]
    assert scenario.events and not [p for e in scenario.events for p in contract_violations(e)]
    assert scenario.relationships
