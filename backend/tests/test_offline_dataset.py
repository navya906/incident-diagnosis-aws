"""Phase 1 gate: dataset generation, determinism, ground truth, blinding, replay."""

import json
import re
from collections import Counter, defaultdict
from datetime import timedelta

import pytest

from app.contracts.events import CanonicalEvent, EventSource, make_event_id
from app.contracts.taxonomy import FAULT_TYPES, RootCause
from app.interfaces.collector import CollectionRequest
from app.offline.dataset import DatasetError, DatasetLoader, generate_dataset, scenario_plan
from app.offline.replay import ReplayCollector

SEED = 42


@pytest.fixture(scope="session")
def dataset_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("ds_a")
    generate_dataset(out, seed=SEED)
    return out


@pytest.fixture(scope="session")
def loader(dataset_dir):
    return DatasetLoader(dataset_dir)


@pytest.fixture(scope="session")
def all_cases(loader):
    return [(loader.load(i), loader.load_truth(i)) for i in loader.incident_ids()]


# ------------------------------------------------------------------ size and composition
def test_dataset_composition(loader):
    m = loader.manifest
    assert m.counts["total"] >= 100
    assert m.counts["category:red_herring"] >= 10
    assert m.counts["category:insufficient_evidence"] >= 5
    assert m.counts["category:compound"] >= 5
    std = Counter(e.fault_type for e in m.entries if e.category == "standard")
    assert set(std) == {f.value for f in FAULT_TYPES}
    assert all(n >= 10 for n in std.values())
    assert m.label == "smoke-test / synthetic"


def test_plan_keys_unique():
    keys = [s.key for s in scenario_plan()]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------- gate: replay into CanonicalEvents
def test_every_case_loads_through_replay_collector(dataset_dir):
    collector = ReplayCollector(dataset_dir)
    ids = collector.incident_ids()
    assert len(ids) >= 100
    for iid in ids:
        events = collector.collect(collector.default_request(iid))
        assert events, iid
        assert all(isinstance(e, CanonicalEvent) for e in events)
        assert events == sorted(events, key=lambda e: (e.timestamp, e.event_id))


def test_event_ids_are_recomputable_and_unique(all_cases):
    for obs, _ in all_cases:
        ids = [e.event_id for e in obs.events]
        assert len(ids) == len(set(ids))
        for e in obs.events[:50]:
            assert e.event_id == make_event_id(
                timestamp=e.timestamp,
                source=e.source.value,
                service=e.service,
                resource_id=e.resource_id,
                event_type=e.event_type,
                metric=e.metric,
                raw_ref=e.raw_ref,
            )


# ------------------------------------------------------------------ gate: determinism
def test_regeneration_is_byte_identical(dataset_dir, tmp_path):
    generate_dataset(tmp_path, seed=SEED)
    a = sorted(p.relative_to(dataset_dir) for p in dataset_dir.rglob("*.json"))
    b = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*.json"))
    assert a == b
    for rel in a:
        assert (dataset_dir / rel).read_bytes() == (tmp_path / rel).read_bytes(), rel


def test_different_seed_gives_different_data(loader, tmp_path):
    other = generate_dataset(tmp_path, seed=SEED + 1)
    assert other.content_sha256 != loader.manifest.content_sha256


# ------------------------------------------------------------------ gate: ground truth
def test_ground_truth_validates_against_taxonomy(all_cases):
    for obs, truth in all_cases:
        gt = truth.ground_truth
        assert gt.taxonomy_label in set(RootCause)
        inventory = {r.resource_id for r in obs.resources}
        assert gt.primary_resource_id in inventory
        event_ids = {e.event_id for e in obs.events}
        assert set(gt.evidence_event_ids) <= event_ids
        w = obs.incident
        assert w.window_start <= gt.onset_time <= w.window_end
        cat = truth.meta.category
        if cat == "insufficient_evidence":
            assert gt.taxonomy_label == RootCause.INSUFFICIENT_EVIDENCE
            assert gt.evidence_event_ids == []
        else:
            assert gt.taxonomy_label.value == truth.meta.faults[0].fault
            assert gt.evidence_event_ids, truth.meta.scenario_key
        if cat == "compound":
            assert [s.value for s in gt.secondary_labels] == [
                f.fault for f in truth.meta.faults[1:]
            ]
            assert len(gt.secondary_labels) == 1


def test_insufficient_evidence_cases_lack_discriminating_telemetry(all_cases):
    for obs, truth in all_cases:
        if truth.meta.category != "insufficient_evidence":
            continue
        sources = {e.source for e in obs.events}
        assert sources <= {EventSource.CLOUDWATCH_METRIC, EventSource.ALARM}
        assert len({e.resource_id for e in obs.events}) <= 2


def test_anomaly_labels_reference_inventory_and_window(all_cases):
    for obs, truth in all_cases:
        inventory = {r.resource_id for r in obs.resources}
        assert truth.anomaly_labels
        for lab in truth.anomaly_labels:
            assert lab.resource_id in inventory
            assert obs.incident.window_start <= lab.start <= lab.end <= obs.incident.window_end


# ------------------------------------------------------------------ blinding / leakage
FORBIDDEN = {
    "deployment_failure": ["deploy", "release", "rollout"],
    "connection_exhaustion": ["connection", "max_connections", "pool"],
    "function_timeout": ["timeout", "timed out"],
    "cpu_saturation": ["cpu", "saturat"],
    "lb_error_spike": ["listener"],
    "iam_permission_failure": ["iam", "permission", "accessdenied", "policy"],
    "security_group_misconfiguration": ["security group", "ingress", "sg-"],
    "db_latency_increase": ["database", "rds", "latency", "query"],
    "task_crash_loop": ["crash", "oom", "memory", "restart"],
    "queue_backlog": ["backlog", "throttl", "concurrency"],
}


def test_descriptions_are_blinded(all_cases):
    for obs, truth in all_cases:
        text = (obs.incident.description + " " + obs.incident.title).lower()
        for fault in truth.meta.faults:
            for word in FORBIDDEN[fault.fault]:
                assert word not in text, (truth.meta.scenario_key, word, text)
        assert truth.meta.scenario_key.split(":")[1] not in text


def test_observable_files_contain_no_ground_truth(loader, dataset_dir, all_cases):
    for obs, truth in all_cases[:40]:
        raw = (dataset_dir / "incidents" / f"{obs.incident.incident_id}.json").read_text()
        for leak in (
            "ground_truth",
            "taxonomy_label",
            "scenario_key",
            "red_herring",
            "smoke-test",
            '"split"',
        ):
            assert leak not in raw
        assert truth.meta.scenario_key.split(":")[1] not in obs.incident.incident_id


def test_red_herrings_are_off_the_causal_path(all_cases):
    seen = 0
    for obs, truth in all_cases:
        rh = set(truth.meta.red_herring_event_ids)
        if truth.meta.category != "red_herring":
            assert not rh
            continue
        seen += 1
        events = {e.event_id: e for e in obs.events}
        assert len(rh) == 2 and rh <= set(events)
        assert not rh & set(truth.ground_truth.evidence_event_ids)
        linked = {r.source_id for r in obs.relationships} | {r.target_id for r in obs.relationships}
        onset = truth.ground_truth.onset_time
        for eid in rh:
            e = events[eid]
            assert e.resource_id not in linked
            assert abs(e.timestamp - onset) <= timedelta(minutes=10)
    assert seen >= 10


# ------------------------------------------------------------------ split and variation
def test_split_is_stratified_and_covers_every_fault(loader):
    by_stratum = defaultdict(Counter)
    for e in loader.manifest.entries:
        stratum = e.fault_type if e.category == "standard" else e.category
        by_stratum[stratum][e.split] += 1
    for stratum, c in by_stratum.items():
        assert c["dev"] > 0 and c["test"] > 0, stratum
        assert 0.2 <= c["test"] / (c["dev"] + c["test"]) <= 0.4, stratum
    assert loader.incident_ids("dev") and loader.incident_ids("test")
    assert set(loader.incident_ids("dev")).isdisjoint(loader.incident_ids("test"))


def test_variation_in_resolution_noise_and_missing_data(all_cases):
    periods = defaultdict(set)
    gaps = 0
    for obs, truth in all_cases:
        p = truth.meta.period_seconds
        periods[truth.meta.faults[0].fault].add(p)
        series = defaultdict(list)
        for e in obs.events:
            if e.source == EventSource.CLOUDWATCH_METRIC:
                assert e.metadata["period_seconds"] == p
                assert (e.timestamp - obs.incident.window_start).total_seconds() % p == 0
                series[(e.resource_id, e.metric)].append(e)
        expected = (
            int((obs.incident.window_end - obs.incident.window_start).total_seconds() // p) + 1
        )
        gaps += sum(1 for pts in series.values() if len(pts) < expected)
    for fault in FAULT_TYPES:
        assert periods[fault.value] == {60, 300}, fault
    assert gaps > 0
    assert len({t.meta.noise for _, t in all_cases}) > 1
    assert len({t.meta.severity for _, t in all_cases}) > 50


# ------------------------------------------------------------------ replay behaviour
def test_replay_filters_by_window_resource_and_source(dataset_dir, loader):
    collector = ReplayCollector(dataset_dir)
    iid = loader.incident_ids()[0]
    inc = collector.incident(iid)
    resources, relationships = collector.inventory(iid)
    assert relationships
    rid = inc.affected_resources[0]
    req = CollectionRequest(
        incident_id=iid,
        resource_ids=[rid],
        window_start=inc.alarm_time - timedelta(minutes=5),
        window_end=inc.alarm_time,
        sources=[EventSource.CLOUDWATCH_METRIC],
    )
    events = collector.collect(req)
    assert events
    for e in events:
        assert e.resource_id == rid
        assert req.window_start <= e.timestamp <= req.window_end
        assert e.source == EventSource.CLOUDWATCH_METRIC


def test_replay_unknown_incident_and_tampering(dataset_dir, tmp_path):
    collector = ReplayCollector(dataset_dir)
    with pytest.raises(DatasetError):
        collector.incident("inc-doesnotexist")
    # tamper with a copy of one file and confirm checksum verification catches it
    copy = tmp_path / "ds"
    generate_dataset(copy, seed=SEED)
    target = next((copy / "incidents").glob("inc-*[!h].json"))
    data = json.loads(target.read_text())
    data["incident"]["description"] = "tampered"
    target.write_text(json.dumps(data))
    with pytest.raises(DatasetError, match="checksum"):
        DatasetLoader(copy).load(target.stem)


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(DatasetError):
        DatasetLoader(tmp_path)


def test_incident_ids_are_opaque(loader):
    for iid in loader.incident_ids():
        assert re.fullmatch(r"inc-[0-9a-f]{10}", iid)
