"""Phase 4: dependency graph, investigation windows, temporal ranking, event chains, candidate
causes and evidence ranking."""

import sys
from datetime import UTC, datetime, timedelta

import pytest

from app.config import CorrelationSettings, EvidenceSettings, Settings
from app.contracts.anomaly import Anomaly
from app.contracts.events import CanonicalEvent, EventSource, Severity
from app.contracts.evidence import ScoreWeights, make_evidence_id
from app.correlation.chains import CorrelationEngine, extract_signals
from app.correlation.events import event_kind, group_key, message_template
from app.correlation.temporal import (
    estimate_onset,
    find_alarm,
    rank_by_temporal_proximity,
    temporal_score,
)
from app.correlation.windows import STANDARD_WINDOWS, events_in_window, investigation_window
from app.evaluation import evidence_eval
from app.evidence.pipeline import InvestigationPipeline
from app.evidence.ranker import EvidenceRanker, rank_by_recency
from app.evidence.semantic import LexicalSemanticScorer
from app.graph.builder import build_graph
from app.graph.store import EDGE_TYPES, NetworkXGraphStore, node_type_for
from app.offline.dataset import DatasetLoader, generate_dataset
from app.offline.models import IncidentRecord, RelationshipRecord, ResourceRecord
from app.offline.replay import ReplayCollector

T = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)  # alarm time in the hand-built scenarios
ALB, APP, DB, SG, ROLE = (
    "alb/app-lb",
    "ecs/service/web",
    "rds/db-main",
    "sg/sg-0db",
    "iam/role/web-exec",
)
BATCH = "ecs/service/batch"  # inventoried but unconnected (red-herring target)


# ------------------------------------------------------------------ builders
def _res(rid, rtype, service, role):
    return ResourceRecord(resource_id=rid, resource_type=rtype, service=service, role=role)


RESOURCES = [
    _res(ALB, "AWS::ElasticLoadBalancingV2::LoadBalancer", "elasticloadbalancing", "alb"),
    _res(APP, "AWS::ECS::Service", "ecs", "app"),
    _res(DB, "AWS::RDS::DBInstance", "rds", "db"),
    _res(SG, "AWS::EC2::SecurityGroup", "ec2", "sg"),
    _res(ROLE, "AWS::IAM::Role", "iam", "role"),
    _res(BATCH, "AWS::ECS::Service", "ecs", "unrelated"),
]
RELATIONSHIPS = [
    RelationshipRecord(source_id=ALB, target_id=APP, relation_type="routes_to"),
    RelationshipRecord(source_id=APP, target_id=DB, relation_type="connects_to"),
    RelationshipRecord(source_id=DB, target_id=SG, relation_type="secured_by"),
    RelationshipRecord(source_id=APP, target_id=ROLE, relation_type="assumes_role"),
]


def graph():
    return build_graph(RESOURCES, RELATIONSHIPS)


def metric(rid, name, t, value, period=60):
    return CanonicalEvent.build(
        timestamp=t,
        source=EventSource.CLOUDWATCH_METRIC,
        service=rid.split("/")[0],
        resource_id=rid,
        event_type="metric_datapoint",
        metric=name,
        value=value,
        metadata={"namespace": "X", "stat": "Average", "period_seconds": period, "unit": ""},
        raw_ref=f"cw:X/{name}/{rid}",
    )


def trail(rid, t, name, msg, error_code=None, read_only=None):
    meta = {"eventSource": "ecs.amazonaws.com", "awsRegion": "us-east-1", "userIdentity": "u"}
    if error_code:
        meta["errorCode"] = error_code
    if read_only is not None:
        meta["readOnly"] = read_only
    return CanonicalEvent.build(
        timestamp=t,
        source=EventSource.CLOUDTRAIL,
        service="ecs",
        resource_id=rid,
        event_type=name,
        message=msg,
        severity=Severity.ERROR if error_code else Severity.INFO,
        metadata=meta,
        raw_ref=f"ct:{rid}:{name}:{t.isoformat()}",
    )


def config_change(rid, t, msg):
    return CanonicalEvent.build(
        timestamp=t,
        source=EventSource.AWS_CONFIG,
        service=rid.split("/")[0],
        resource_id=rid,
        event_type="ConfigurationItemChange",
        message=msg,
        metadata={"resourceType": "AWS::X", "configurationItemStatus": "OK"},
        raw_ref=f"cfg:{rid}:{t.isoformat()}",
    )


def log(rid, t, msg, severity=Severity.ERROR):
    return CanonicalEvent.build(
        timestamp=t,
        source=EventSource.CLOUDWATCH_LOG,
        service=rid.split("/")[0],
        resource_id=rid,
        event_type="log_line",
        severity=severity,
        message=msg,
        metadata={"log_group": f"/{rid}", "log_stream": "s"},
        raw_ref=f"cwlogs:{rid}:{t.isoformat()}:{msg}",
    )


def alarm(rid, metric_name, t, value=50.0):
    return CanonicalEvent.build(
        timestamp=t,
        source=EventSource.ALARM,
        service="cloudwatch",
        resource_id=rid,
        event_type="alarm_state_change",
        metric=metric_name,
        value=value,
        severity=Severity.CRITICAL,
        message=f"ALARM: {metric_name} on {rid}",
        metadata={"alarm_name": "a", "state": "ALARM", "previous_state": "OK", "threshold": 10},
        raw_ref=f"alarm:{rid}",
    )


def anomaly(rid, metric_name, t, score=8.0):
    return Anomaly(
        resource_id=rid,
        metric=metric_name,
        timestamp=t,
        score=score,
        baseline=1.0,
        observed=9.0,
        method="zscore",
    )


def mins(m: float) -> datetime:
    return T + timedelta(minutes=m)


def deployment_scenario():
    """Bad deployment on APP at T-6m; task count drops from T-5m; ALB 5xx from T-4m; alarm at T.

    Red herrings: an unrelated deployment on BATCH at T-5.5m (no dependency path) and a DB
    config change 2 minutes after the alarm (no temporal precedence)."""
    events, anomalies = [], []
    for i in range(-40, 11):
        t = mins(i)
        tasks = 4.0 if i < -5 else 1.0
        errs = 2.0 if i < -4 else 300.0
        events += [metric(APP, "RunningTaskCount", t, tasks), metric(ALB, "5XX", t, errs)]
        events.append(metric(DB, "CPUUtilization", t, 25.0 + (i % 3)))
        if i >= -5:
            anomalies.append(anomaly(APP, "RunningTaskCount", t))
        if i >= -4:
            anomalies.append(anomaly(ALB, "5XX", t))
    deploy = trail(APP, mins(-6), "UpdateService", "UpdateService: web now uses web:42")
    herring = trail(BATCH, mins(-5.5), "UpdateService", "UpdateService: batch now uses batch:7")
    late = config_change(DB, mins(2), "DB parameter group changed")
    crash = log(APP, mins(-4.8), "Essential container in task exited: exit code 1")
    crash2 = log(APP, mins(-3.8), "Essential container in task exited: exit code 1")
    noise = log(APP, mins(-30), "Client closed connection", Severity.ERROR)
    events += [deploy, herring, late, crash, crash2, noise, alarm(ALB, "5XX", T)]
    incident = IncidentRecord(
        incident_id="inc-test",
        title="ALARM: 5XX on alb",
        description="5XX errors increased on the load balancer; users see failed requests.",
        alarm_time=T,
        window_start=mins(-40),
        window_end=mins(10),
        affected_resources=[ALB],
    )
    ids = {"deploy": deploy, "herring": herring, "late": late, "crash": crash}
    return incident, sorted(events, key=lambda e: (e.timestamp, e.event_id)), anomalies, ids


def correlate(events, anomalies, g, minutes=30, onset=None, affected=(ALB,)):
    w = investigation_window(T, minutes, 10)
    return CorrelationEngine(CorrelationSettings()).correlate(
        incident_id="inc-test",
        affected_resources=list(affected),
        events=events,
        anomalies=anomalies,
        window=w,
        onset=onset or mins(-5),
        graph=g,
    )


# ------------------------------------------------------------------ dependency graph
def test_graph_node_and_edge_types_and_queries():
    g = graph()
    assert g.node_attrs(APP)["node_type"] == "ecs_service"
    assert g.node_attrs(SG)["node_type"] == "security_group"
    assert g.edge_type(APP, DB) == "connects_to"
    assert g.downstream(ALB) == {APP, DB, SG, ROLE}
    assert g.downstream(ALB, max_depth=1) == {APP}
    assert g.upstream(DB) == {APP, ALB}
    assert g.upstream(BATCH) == set() and g.downstream(BATCH) == set()
    assert g.path(ALB, SG) == [ALB, APP, DB, SG]
    assert g.path(SG, ALB) is None
    # A failing security group impacts the DB and everything that depends on it.
    assert g.blast_radius(SG) == {DB, APP, ALB}
    assert g.blast_radius(ALB) == set()
    assert g.impact_path(DB, ALB) == [DB, APP, ALB]
    assert g.impact_distance(ROLE, ALB) == 2
    assert g.impact_path(BATCH, ALB) is None
    assert g.impact_sources({ALB}) == {ALB, APP, DB, SG, ROLE}


def test_polls_edge_propagates_impact_both_ways():
    g = NetworkXGraphStore()
    g.add_edge("lambda/function/worker", "sqs/queue/jobs", "polls")
    g.add_edge("ecs/service/web", "sqs/queue/jobs", "sends_to")
    # A failing consumer backs the queue up; a failing queue starves the consumer.
    assert "sqs/queue/jobs" in g.blast_radius("lambda/function/worker")
    assert "lambda/function/worker" in g.blast_radius("sqs/queue/jobs")
    # sends_to is one-way: a broken producer does not appear in the queue's impact sources.
    assert g.impact_path("ecs/service/web", "sqs/queue/jobs") is None


def test_graph_rejects_unknown_types_and_self_loops_and_exports_stably():
    g = graph()
    with pytest.raises(ValueError):
        g.add_edge(APP, DB, "teleports_to")
    with pytest.raises(ValueError):
        g.add_edge(APP, APP, "routes_to")
    with pytest.raises(ValueError):
        g.add_node("x/y", "spaceship")
    d = g.to_dict()
    assert {e["edge_type"] for e in d["edges"]} <= EDGE_TYPES
    assert d == build_graph(list(reversed(RESOURCES)), list(reversed(RELATIONSHIPS))).to_dict()
    assert node_type_for(None, "lambda/function/x") == "lambda_function"
    assert node_type_for("AWS::Weird::Thing", "zz/1") == "unknown"


def test_graph_builds_from_dataset_inventory(dataset):
    collector = ReplayCollector(dataset)
    kinds = set()
    for iid in collector.incident_ids("dev")[:20]:
        resources, relationships = collector.inventory(iid)
        g = build_graph(resources, relationships)
        assert set(g.nodes()) >= {r.resource_id for r in resources}
        assert {k for _, _, k in g.edges()} <= EDGE_TYPES
        incident = collector.incident(iid)
        truth = collector.loader.load_truth(iid)
        # The true primary resource can always reach the alarmed resource.
        primary = truth.ground_truth.primary_resource_id
        assert primary in g.impact_sources(set(incident.affected_resources))
        kinds.add(g.node_attrs(primary)["node_type"])
    assert len(kinds) >= 3


# ------------------------------------------------------------------ windows and temporal
def test_investigation_windows():
    assert STANDARD_WINDOWS == (5, 15, 30, 60)
    w = investigation_window(T, 15, 10)
    assert w.start == mins(-15) and w.end == mins(10)
    assert w.contains(mins(-15)) and w.contains(mins(10)) and not w.contains(mins(-15.1))
    _, events, _, _ = deployment_scenario()
    inside = events_in_window(events, investigation_window(T, 5, 0))
    assert inside and all(mins(-5) <= e.timestamp <= T for e in inside)
    assert len(events_in_window(events, investigation_window(T, 60, 10))) > len(inside)
    with pytest.raises(ValueError):
        investigation_window(T, -1)


def test_temporal_score_and_proximity_ranking():
    onset = mins(-5)
    assert temporal_score(onset, onset, 5, 10) == 1.0
    # Same distance: the after-onset side decays more slowly with these constants.
    assert temporal_score(mins(0), onset, 5, 10) > temporal_score(mins(-10), onset, 5, 10)
    _, events, _, ids = deployment_scenario()
    ranked = rank_by_temporal_proximity(events, onset)
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)
    position = {e.event_id: i for i, (e, _) in enumerate(ranked)}
    noise_log = next(e for e in events if e.message == "Client closed connection")
    assert position[ids["deploy"].event_id] < position[noise_log.event_id]


def test_onset_estimate_uses_related_runs_and_ignores_noise():
    incident, events, anomalies, _ = deployment_scenario()
    al = find_alarm(events, incident.affected_resources, T)
    assert al is not None and al.metric == "5XX"
    # Alarmed series only: the ALB run starts at T-4m.
    assert estimate_onset(T, al, anomalies, events) == mins(-4)
    # With related resources, the earlier task-count run on APP sets the onset.
    assert estimate_onset(T, al, anomalies, events, related_resources={ALB, APP}) == mins(-5)
    # A lone noisy flag long before the alarm is not a run that is active at the alarm.
    noisy = [*anomalies, anomaly(DB, "CPUUtilization", mins(-25))]
    assert estimate_onset(T, al, noisy, events, related_resources={ALB, APP, DB}) == mins(-5)
    # No anomaly detection (A3) -> the alarm time.
    assert estimate_onset(T, al, None, events) == T
    # Lookback bound.
    assert estimate_onset(T, al, anomalies, events, {ALB, APP}, lookback_minutes=4.5) == mins(-4)


def test_event_classification_and_groups():
    assert event_kind(trail(APP, T, "UpdateService", "x")) == "change"
    assert event_kind(trail(APP, T, "DescribeServices", "x")) == "read_only"
    assert event_kind(trail(APP, T, "PutObject", "x", read_only=True)) == "read_only"
    assert event_kind(trail(APP, T, "DescribeServices", "x", error_code="AccessDenied")) == (
        "api_error"
    )
    assert event_kind(config_change(DB, T, "x")) == "change"
    assert event_kind(log(APP, T, "x", Severity.WARNING)) == "warning_log"
    assert event_kind(log(APP, T, "x", Severity.INFO)) == "info_log"
    assert event_kind(alarm(ALB, "5XX", T)) == "alarm"
    assert message_template("req 0x1f took 1234 ms (id 9f8e7d6c)") == "req # took # ms (id #)"
    a = log(APP, T, "timeout after 3.00 s")
    b = log(APP, mins(1), "timeout after 4.50 s")
    assert group_key(a) == group_key(b)
    assert group_key(metric(APP, "CPU", T, 1)) != group_key(metric(DB, "CPU", T, 1))


# ------------------------------------------------------------------ chains and candidate causes
def test_chain_detection_finds_deployment_and_rejects_red_herrings():
    _, events, anomalies, ids = deployment_scenario()
    res = correlate(events, anomalies, graph())
    causes = res.candidate_causes
    assert causes, "expected candidate causes"
    top = causes[0]
    assert top.label == "candidate cause"
    assert top.signal.kind == "change" and top.signal.event_ids == [ids["deploy"].event_id]
    assert top.dependency_path == [APP, ALB]
    # The chain runs forward in time from the deployment to the alarm, through APP then ALB.
    chain = top.chain
    times = [
        next(s for s in res.signals if s.signal_id == sid).timestamp for sid in chain.signal_ids
    ]
    assert times == sorted(times)
    assert chain.resource_path[0] == APP and chain.resource_path[-1] == ALB
    assert next(s for s in res.signals if s.signal_id == chain.signal_ids[-1]).kind == "alarm"
    assert ids["deploy"].event_id in chain.event_ids
    # Red herring 1: unrelated deployment close to onset, but no dependency path.
    # Red herring 2: change on a dependency, but after the alarm (no temporal precedence).
    bad = {ids["herring"].event_id, ids["late"].event_id}
    for c in causes:
        assert not bad & set(c.signal.event_ids)
    for ch in res.chains:
        assert not bad & set(ch.event_ids)
    # Both red herrings are still observed as signals; they are only excluded as causes.
    observed = {eid for s in res.signals for eid in s.event_ids}
    assert bad <= observed


def test_red_herring_closer_to_onset_still_loses_to_the_connected_change():
    _, events, anomalies, ids = deployment_scenario()
    # Move the unrelated deployment to exactly the onset estimate: closest in time, still no path.
    moved = trail(BATCH, mins(-5), "UpdateService", "UpdateService: batch now uses batch:8")
    events = [e for e in events if e.event_id != ids["herring"].event_id] + [moved]
    res = correlate(sorted(events, key=lambda e: e.timestamp), anomalies, graph())
    assert all(moved.event_id not in c.signal.event_ids for c in res.candidate_causes)
    assert res.candidate_causes[0].signal.event_ids == [ids["deploy"].event_id]


def test_dependency_cause_reaches_alarm_through_intermediate_resource():
    """Security-group change on the DB's SG -> DB connection errors -> APP errors -> ALB alarm."""
    events, anomalies = [], []
    for i in range(-30, 5):
        t = mins(i)
        events += [metric(DB, "DatabaseConnections", t, 60 if i < -6 else 0)]
        events += [metric(ALB, "5XX", t, 2 if i < -4 else 200)]
        if i >= -6:
            anomalies.append(anomaly(DB, "DatabaseConnections", t))
        if i >= -4:
            anomalies.append(anomaly(ALB, "5XX", t))
    sg_change = trail(SG, mins(-7), "RevokeSecurityGroupIngress", "tcp/5432 revoked")
    app_err = log(APP, mins(-5.5), "connection to database timed out")
    events += [sg_change, app_err, alarm(ALB, "5XX", T)]
    events.sort(key=lambda e: e.timestamp)
    res = correlate(events, anomalies, graph(), onset=mins(-6))
    top = res.candidate_causes[0]
    assert top.signal.event_ids == [sg_change.event_id]
    assert top.dependency_path == [SG, DB, APP, ALB]
    assert set(top.chain.resource_path) >= {SG, ALB}
    # Without the graph, only signals on the alarmed resource itself can be candidate causes.
    no_graph = correlate(events, anomalies, None, onset=mins(-6))
    assert all(c.signal.resource_id == ALB for c in no_graph.candidate_causes)


def test_link_gap_limits_chains():
    _, events, anomalies, _ = deployment_scenario()
    old = trail(APP, mins(-28), "UpdateService", "an old deployment")
    res = correlate(sorted([*events, old], key=lambda e: e.timestamp), anomalies, graph())
    # 22 minutes before the next signal on a linked resource: beyond the 10-minute link gap.
    assert all(old.event_id not in c.signal.event_ids for c in res.candidate_causes)


def test_queue_backlog_chain_through_polls_edge():
    consumer, queue = "lambda/function/worker", "sqs/queue/jobs"
    resources = [
        _res(consumer, "AWS::Lambda::Function", "lambda", "consumer"),
        _res(queue, "AWS::SQS::Queue", "sqs", "queue"),
    ]
    g = build_graph(
        resources,
        [RelationshipRecord(source_id=consumer, target_id=queue, relation_type="polls")],
    )
    events, anomalies = [], []
    for i in range(-30, 5):
        t = mins(i)
        events += [metric(consumer, "Errors", t, 1 if i < -6 else 80)]
        events += [metric(queue, "AgeOfOldestMessage", t, 5 if i < -4 else 900)]
        if i >= -6:
            anomalies.append(anomaly(consumer, "Errors", t))
        if i >= -4:
            anomalies.append(anomaly(queue, "AgeOfOldestMessage", t))
    err = log(consumer, mins(-6.5), "Task timed out after 60.00 seconds")
    events += [err, alarm(queue, "AgeOfOldestMessage", T)]
    events.sort(key=lambda e: e.timestamp)
    res = correlate(events, anomalies, g, onset=mins(-6), affected=(queue,))
    assert res.candidate_causes[0].signal.resource_id == consumer
    assert res.candidate_causes[0].dependency_path == [consumer, queue]


def test_signals_skip_background_log_templates_and_short_anomaly_runs():
    _, events, anomalies, _ = deployment_scenario()
    w = investigation_window(T, 15, 10)
    # "Client closed connection" first appears 30 min before the alarm: outside and before w.
    sigs = extract_signals(events, [*anomalies, anomaly(DB, "CPUUtilization", mins(-12))], w)
    assert not [s for s in sigs if "Client closed" in s.summary]
    assert not [s for s in sigs if s.resource_id == DB and s.kind == "anomaly"]
    crash = [s for s in sigs if s.kind == "error_log"]
    assert len(crash) == 1 and crash[0].count == 2


def test_correlation_is_deterministic():
    _, events, anomalies, _ = deployment_scenario()
    a = correlate(events, anomalies, graph()).model_dump_json()
    b = correlate(list(reversed(events)), list(reversed(anomalies)), graph()).model_dump_json()
    assert a == b


# ------------------------------------------------------------------ evidence ranking
def _rank(weights=None, use_graph=True, anomalies="default", k=10, minutes=30):
    incident, events, anoms, ids = deployment_scenario()
    ranker = EvidenceRanker(EvidenceSettings(), anomaly_threshold=6.0)
    ranking = ranker.rank(
        incident_id=incident.incident_id,
        alarm_time=T,
        affected_resources=incident.affected_resources,
        description=incident.description,
        events=events,
        onset=mins(-5),
        anomalies=anoms if anomalies == "default" else anomalies,
        graph=graph() if use_graph else None,
        known_resources={r.resource_id for r in RESOURCES},
        window_minutes=minutes,
        top_k=k,
        weights=weights,
    )
    return ranking, ids, events


def test_ranking_scores_components_and_weights():
    ranking, ids, events = _rank()
    assert [i.rank for i in ranking.items] == list(range(1, len(ranking.items) + 1))
    assert len(ranking.items) == 10
    by_event = {i.event_id: i for i in ranking.items}
    w = ranking.weights
    for item in ranking.items:
        c = item.components.model_dump()
        assert all(0 <= v <= 1 for v in c.values())
        assert item.score == pytest.approx(w.combine(item.components), abs=1e-5)
    # The deployment has no numeric anomaly but gets the change prior, not 0 (BRIEF Section 2).
    deploy = by_event[ids["deploy"].event_id]
    assert deploy.components.anomaly == EvidenceSettings().event_priors["change"]
    assert deploy.components.dependency == 1.0
    # Red herrings and the alarm itself are not in the top-K.
    assert ids["herring"].event_id not in by_event
    assert not [e for e in events if e.event_id in by_event and e.source == EventSource.ALARM]


def test_unconnected_resource_gets_zero_dependency_and_ablation_by_weight():
    incident, events, anoms, ids = deployment_scenario()
    ranker = EvidenceRanker(EvidenceSettings(), anomaly_threshold=6.0)
    herring = ids["herring"]
    kw = dict(
        affected_resources=[ALB],
        description=incident.description,
        onset=mins(-5),
        anomalies=anoms,
        known_resources={r.resource_id for r in RESOURCES},
    )
    scored = {
        s.event.event_id: s for s in ranker.score_events(candidates=events, graph=graph(), **kw)
    }
    assert scored[herring.event_id].components.dependency == 0.0
    no_graph = ranker.score_events(candidates=events, graph=None, **kw)
    assert all(s.components.dependency == 0.0 for s in no_graph)
    only_t = ScoreWeights(temporal=1, resource=0, anomaly=0, semantic=0, dependency=0)
    weighted = ranker.score_events(candidates=events, graph=graph(), weights=only_t, **kw)
    assert all(s.score == pytest.approx(s.components.temporal) for s in weighted)


def test_evidence_ids_are_stable_across_k_window_and_weights():
    a, ids, _ = _rank(k=5)
    b, _, _ = _rank(k=20, minutes=60)
    c, _, _ = _rank(
        weights=ScoreWeights(temporal=1, resource=0, anomaly=0, semantic=0, dependency=0)
    )
    for ranking in (a, b, c):
        for item in ranking.items:
            assert item.evidence_id == make_evidence_id("inc-test", item.event_id)
    common = {i.event_id for i in a.items} & {i.event_id for i in b.items}
    assert common
    ev_a = {i.event_id: i.evidence_id for i in a.items}
    ev_b = {i.event_id: i.evidence_id for i in b.items}
    assert all(ev_a[e] == ev_b[e] for e in common)
    assert make_evidence_id("inc-a", "ev_1") != make_evidence_id("inc-b", "ev_1")


def test_group_cap_keeps_first_occurrences():
    ranking, _, events = _rank(k=40)
    by_id = {e.event_id: e for e in events}
    picked = [by_id[i.event_id] for i in ranking.items]
    counts = {}
    for e in picked:
        counts[group_key(e)] = counts.get(group_key(e), 0) + 1
    assert max(counts.values()) <= 2
    # From the ALB 5XX series, the first anomalous datapoints are taken, not later repeats.
    alb = sorted(e.timestamp for e in picked if e.resource_id == ALB and e.metric == "5XX")
    assert alb and alb[0] == mins(-4)
    uncapped = EvidenceRanker(EvidenceSettings(max_per_group=0), anomaly_threshold=6.0)
    assert uncapped.settings.max_per_group == 0


def test_recency_baseline_and_semantic_scorer():
    _, events, _, _ = deployment_scenario()
    items = rank_by_recency("inc-test", events, T, 30, 5)
    stamps = [next(e for e in events if e.event_id == i.event_id).timestamp for i in items]
    assert stamps == sorted(stamps, reverse=True) and len(items) == 5
    s = LexicalSemanticScorer().scores(
        "5XX errors increased on the load balancer",
        ["5XX alb/app-lb", "Cache refresh completed", "connection timed out"],
    )
    assert s[0] > s[1] and s[2] == 0.5 and all(0 <= v <= 1 for v in s)
    assert LexicalSemanticScorer().scores("x", []) == []


# ------------------------------------------------------------------ pipeline and gate on dev
@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("phase4-ds")
    generate_dataset(out, seed=42)
    return out


def test_pipeline_runs_offline_and_ablations_remove_context(dataset):
    collector = ReplayCollector(dataset)
    iid = collector.incident_ids("dev")[0]
    p = InvestigationPipeline(Settings())
    full = p.run_offline(collector, iid)
    assert full.correlation is not None and full.graph is not None and full.anomalies
    assert len(full.ranking.items) == p.settings.evidence.top_k
    assert full.ranking.window_minutes == 30
    a2 = p.run_offline(collector, iid, use_graph=False)
    assert a2.graph is None
    assert all(i.components.dependency == 0 for i in a2.ranking.items)
    a3 = p.run_offline(collector, iid, use_anomalies=False)
    assert a3.anomalies is None and a3.onset == collector.incident(iid).alarm_time
    a4 = p.run_offline(collector, iid, use_chains=False)
    assert a4.correlation is None
    assert not [m for m in sys.modules if m.startswith(("app.ai", "openai", "google.generativeai"))]


def test_gate_red_herrings_never_become_candidate_causes_on_dev(dataset):
    """Gate: chain detection on every dev red-herring case."""
    loader = DatasetLoader(dataset)
    collector = ReplayCollector(loader)
    p = InvestigationPipeline(Settings())
    rh_cases = [
        i for i in loader.incident_ids("dev") if loader.load_truth(i).meta.red_herring_event_ids
    ]
    assert len(rh_cases) >= 5
    found_primary = 0
    for iid in rh_cases:
        truth = loader.load_truth(iid)
        herrings = set(truth.meta.red_herring_event_ids)
        for minutes in STANDARD_WINDOWS:
            inv = p.run_offline(collector, iid, window_minutes=minutes)
            for c in inv.correlation.candidate_causes:
                assert not herrings & set(c.signal.event_ids), (iid, minutes)
            for ch in inv.correlation.chains:
                assert not herrings & set(ch.event_ids), (iid, minutes)
            # The red-herring change is observed in the window (so this is a real test).
            if minutes == 30:
                observed = {e for s in inv.correlation.signals for e in s.event_ids}
                assert herrings & observed
                causes = [c.signal.resource_id for c in inv.correlation.candidate_causes]
                found_primary += truth.ground_truth.primary_resource_id in causes
    assert found_primary / len(rh_cases) >= 0.75


def test_gate_evidence_precision_recall_at_k_on_dev(dataset, tmp_path):
    payload = evidence_eval.run(dataset, "dev", tmp_path, Settings())
    meta = payload["meta"]
    assert meta["label"] == "smoke-test / synthetic" and meta["split"] == "dev"
    full = payload["ablations"][0]
    assert full["config"] == "Full" and full["k"] == 10
    recency = next(r for r in payload["ablations"] if r["config"].startswith("A5"))
    # The ranking must beat the unranked recency baseline by a wide margin.
    assert full["recall"] > 0.4 and full["precision"] > 0.25
    assert full["recall"] > 5 * max(recency["recall"], 0.01)
    assert full["red_herring_rate"] == 0.0
    assert {(r["window_min"], r["k"]) for r in payload["grid"]} == {
        (w, k) for w in STANDARD_WINDOWS for k in evidence_eval.K_VALUES
    }
    for r in payload["candidate_causes"]:
        assert r["red_herring_candidate_causes"] == 0 and r["red_herring_chain_events"] == 0
    md = (tmp_path / "phase4-evidence-ranking-dev.md").read_text(encoding="utf-8")
    assert "SMOKE-TEST / SYNTHETIC" in md and meta["content_sha256"] in md


def test_eval_cli_refuses_test_split(tmp_path):
    with pytest.raises(SystemExit):
        evidence_eval.main(["--split", "test", "--dataset", str(tmp_path), "--out", str(tmp_path)])
    with pytest.raises(SystemExit):
        evidence_eval.main(["--split", "test", "--final", "--tune", "--dataset", str(tmp_path)])


def test_committed_evidence_table_is_labelled_synthetic():
    text = (evidence_eval.DEFAULT_OUT / "phase4-evidence-ranking-dev.md").read_text(
        encoding="utf-8"
    )
    assert "SMOKE-TEST / SYNTHETIC" in text and "Split: `dev`" in text
    assert "| Full |" in text and "A5 recency" in text
