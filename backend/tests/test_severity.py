"""Phase 6: deterministic severity engine."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import CriticalityRule, SeveritySettings
from app.contracts.anomaly import Anomaly
from app.contracts.events import CanonicalEvent, EventSource
from app.evidence.pipeline import InvestigationPipeline
from app.graph.builder import build_graph
from app.offline.dataset import DatasetLoader, generate_dataset
from app.offline.models import IncidentRecord, RelationshipRecord, ResourceRecord
from app.offline.replay import ReplayCollector
from app.severity.engine import LEVELS, SeverityEngine, _points

T0 = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
ONSET = T0 + timedelta(minutes=30)
ALB, APP = "alb/web-lb", "ecs/service/web"


def metric(rid, name, minute, value):
    return CanonicalEvent.build(
        timestamp=T0 + timedelta(minutes=minute),
        source=EventSource.CLOUDWATCH_METRIC,
        service=rid.split("/")[0],
        resource_id=rid,
        event_type="metric_datapoint",
        metric=name,
        value=value,
        metadata={"namespace": "X", "stat": "Sum", "period_seconds": 60, "unit": ""},
        raw_ref=f"cw:X/{name}/{rid}",
    )


def scenario(err_frac=0.0, tasks_after=4, trt_factor=1.0, minutes=20, extra_anomalous=0):
    """ALB in front of a 4-task ECS service; impact starts at minute 30."""
    events, anomalies = [], []
    for m in range(0, 30 + minutes + 1):
        after = m >= 30
        req = 1000.0
        events += [
            metric(ALB, "RequestCount", m, req),
            metric(ALB, "HTTPCode_Target_5XX_Count", m, req * err_frac if after else 1.0),
            metric(ALB, "TargetResponseTime", m, 0.1 * (trt_factor if after else 1.0)),
            metric(APP, "RunningTaskCount", m, float(tasks_after if after else 4)),
        ]
        if after and (err_frac or trt_factor > 1 or tasks_after < 4):
            anomalies.append(
                Anomaly(
                    resource_id=ALB,
                    metric="5XX",
                    timestamp=T0 + timedelta(minutes=m),
                    score=9,
                    baseline=1,
                    observed=9,
                    method="zscore",
                )
            )
            for k in range(extra_anomalous):
                anomalies.append(
                    Anomaly(
                        resource_id=f"sqs/queue/q{k}",
                        metric="Age",
                        timestamp=T0 + timedelta(minutes=m),
                        score=9,
                        baseline=1,
                        observed=9,
                        method="zscore",
                    )
                )
    incident = IncidentRecord(
        incident_id="inc-sev",
        title="t",
        description="d",
        alarm_time=ONSET + timedelta(minutes=3),
        window_start=T0,
        window_end=T0 + timedelta(minutes=30 + minutes),
        affected_resources=[ALB],
    )
    resources = [
        ResourceRecord(
            resource_id=ALB,
            resource_type="AWS::ElasticLoadBalancingV2::LoadBalancer",
            service="elasticloadbalancing",
            role="alb",
        ),
        ResourceRecord(
            resource_id=APP,
            resource_type="AWS::ECS::Service",
            service="ecs",
            role="app",
            attributes={"desired_count": 4},
        ),
    ]
    graph = build_graph(
        resources, [RelationshipRecord(source_id=ALB, target_id=APP, relation_type="routes_to")]
    )
    return incident, events, anomalies, resources, graph


def assess(engine=None, **kw):
    incident, events, anomalies, resources, graph = scenario(**kw)
    return (engine or SeverityEngine()).assess(
        incident=incident,
        events=events,
        onset=ONSET,
        anomalies=anomalies,
        resources=resources,
        graph=graph,
    )


def test_points_follow_bounds():
    assert [_points(v, [0.01, 0.05, 0.25]) for v in (0.0, 0.01, 0.02, 0.1, 0.9)] == [0, 0, 1, 2, 3]
    assert _points(None, [1, 2]) == 0


def test_quiet_incident_is_low_and_factors_are_measured():
    a = assess(err_frac=0.0, minutes=3)
    assert a.level.value == "LOW"
    assert a.factor("error_rate").value == 0.0
    assert a.factor("availability").value == 0.0 and a.factor("latency").value == pytest.approx(1.0)
    assert a.data_complete and a.criticality == "MEDIUM" and a.method == "deterministic-v1"


def test_each_factor_raises_severity():
    base = assess(err_frac=0.0, minutes=20)
    assert assess(err_frac=0.3, minutes=20).score > base.score
    assert assess(tasks_after=1, minutes=20).factor("availability").value == pytest.approx(0.75)
    assert assess(tasks_after=1, minutes=20).score > base.score
    assert assess(trt_factor=6.0, minutes=20).factor("latency").points == 2
    assert assess(err_frac=0.02, minutes=90).factor("duration").points == 3
    wide = assess(err_frac=0.02, extra_anomalous=4)
    assert (
        wide.factor("affected_resources").value == 5
        and wide.factor("affected_resources").points == 3
    )


def test_major_outage_forces_at_least_high():
    a = assess(err_frac=0.6, minutes=3)
    assert a.factor("error_rate").value == pytest.approx(0.6)
    assert LEVELS.index(a.level.value) >= LEVELS.index("HIGH")
    assert "major outage" in a.rationale


def test_business_criticality_comes_from_static_config():
    rules = [
        CriticalityRule(match=r"^alb/web", level="CRITICAL"),
        CriticalityRule(match="ecs", level="LOW"),
    ]
    crit = SeverityEngine(SeveritySettings(criticality_rules=rules))
    low = SeverityEngine(SeveritySettings(default_criticality="LOW"))
    mid = assess(err_frac=0.02)
    hi = assess(crit, err_frac=0.02)
    lo = assess(low, err_frac=0.02)
    assert hi.criticality == "CRITICAL" and lo.criticality == "LOW"
    assert hi.score == mid.score + 2 and lo.score == mid.score - 1
    assert LEVELS.index(hi.level.value) >= LEVELS.index(mid.level.value)
    with pytest.raises(ValidationError):
        CriticalityRule(match="([", level="HIGH")


def test_monotonic_and_deterministic():
    levels = [LEVELS.index(assess(err_frac=f).level.value) for f in (0.0, 0.02, 0.1, 0.3, 0.6)]
    assert levels == sorted(levels)
    assert assess(err_frac=0.1).model_dump() == assess(err_frac=0.1).model_dump()


def test_missing_metrics_are_reported_as_unknown():
    incident, events, anomalies, resources, graph = scenario(err_frac=0.1)
    only_tasks = [e for e in events if e.metric == "RunningTaskCount"]
    a = SeverityEngine().assess(
        incident=incident,
        events=only_tasks,
        onset=ONSET,
        anomalies=anomalies,
        resources=resources,
        graph=graph,
    )
    assert not a.factor("error_rate").known and a.factor("error_rate").points == 0
    assert not a.factor("latency").known and not a.data_complete


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("sev-ds")
    generate_dataset(out, seed=42)
    return out


def test_every_dev_incident_gets_a_severity(dataset):
    loader = DatasetLoader(dataset)
    collector = ReplayCollector(loader)
    pipeline = InvestigationPipeline()
    engine = SeverityEngine()
    for iid in loader.incident_ids("dev"):
        inv = pipeline.run_offline(collector, iid)
        s = loader.load(iid)
        a = engine.assess(
            incident=s.incident,
            events=s.events,
            onset=inv.onset,
            anomalies=inv.anomalies,
            resources=s.resources,
            graph=inv.graph,
        )
        assert a.level.value in LEVELS
        assert s.incident.affected_resources[0] in a.affected_resources
        assert a.factor("duration").value >= 0
