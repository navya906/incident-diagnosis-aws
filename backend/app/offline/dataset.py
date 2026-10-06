"""Dataset plan, generation (with manifest and dev/test split) and loading."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from app.contracts.taxonomy import FAULT_TYPES
from app.contracts.taxonomy import RootCause as RC
from app.offline.faults import COMPATIBLE_TOPOLOGIES
from app.offline.models import Manifest, ManifestEntry, ObservableScenario, TruthRecord
from app.offline.simulator import GENERATOR_VERSION, ScenarioSpec, generate_scenario

DEFAULT_SEED = 42
DEFAULT_VERSION = "synthetic-v1"
TEST_FRACTION = 0.3
VARIANTS_PER_FAULT = 10

COMPOUND_PAIRS: tuple[tuple[RC, RC, str], ...] = (
    (RC.DEPLOYMENT_FAILURE, RC.DB_LATENCY_INCREASE, "ecs"),
    (RC.CPU_SATURATION, RC.CONNECTION_EXHAUSTION, "ecs"),
    (RC.CPU_SATURATION, RC.LB_ERROR_SPIKE, "ec2"),
    (RC.IAM_PERMISSION_FAILURE, RC.CONNECTION_EXHAUSTION, "lambda"),
    (RC.FUNCTION_TIMEOUT, RC.DEPLOYMENT_FAILURE, "lambda"),
    (RC.QUEUE_BACKLOG, RC.IAM_PERMISSION_FAILURE, "sqs"),
)
INSUFFICIENT_FAULTS: tuple[RC, ...] = (
    RC.DEPLOYMENT_FAILURE,
    RC.CONNECTION_EXHAUSTION,
    RC.CPU_SATURATION,
    RC.DB_LATENCY_INCREASE,
    RC.LB_ERROR_SPIKE,
    RC.IAM_PERMISSION_FAILURE,
)
RED_HERRING_COUNT = 12


def scenario_plan() -> list[ScenarioSpec]:
    """10 fault types x 10 variants, plus red-herring, insufficient-evidence and compound cases."""
    specs: list[ScenarioSpec] = []
    for fault in FAULT_TYPES:
        topos = COMPATIBLE_TOPOLOGIES[fault]
        for v in range(VARIANTS_PER_FAULT):
            specs.append(
                ScenarioSpec(
                    f"std:{fault.value}:{v:02d}", "standard", (fault,), topos[v % len(topos)], v
                )
            )
    for i in range(RED_HERRING_COUNT):
        fault = FAULT_TYPES[i % len(FAULT_TYPES)]
        topos = COMPATIBLE_TOPOLOGIES[fault]
        specs.append(
            ScenarioSpec(
                f"rh:{fault.value}:{i:02d}",
                "red_herring",
                (fault,),
                topos[i % len(topos)],
                i,
                red_herring=True,
            )
        )
    for i, fault in enumerate(INSUFFICIENT_FAULTS):
        mode = "alarm_only" if i % 2 == 0 else "sparse"
        specs.append(
            ScenarioSpec(
                f"ie:{fault.value}:{i:02d}",
                "insufficient_evidence",
                (fault,),
                COMPATIBLE_TOPOLOGIES[fault][0],
                i,
                degradation=mode,
            )
        )
    for i, (a, b, topo) in enumerate(COMPOUND_PAIRS):
        specs.append(ScenarioSpec(f"cmp:{a.value}+{b.value}:{i:02d}", "compound", (a, b), topo, i))
    return specs


def _stratum(spec: ScenarioSpec) -> str:
    return f"standard:{spec.faults[0].value}" if spec.category == "standard" else spec.category


def assign_splits(specs: list[ScenarioSpec], seed: int) -> dict[str, str]:
    """Stratified, deterministic dev/test split (~30% test per stratum)."""
    strata: dict[str, list[ScenarioSpec]] = defaultdict(list)
    for s in specs:
        strata[_stratum(s)].append(s)
    split: dict[str, str] = {}
    for members in strata.values():
        order = sorted(
            members, key=lambda s: hashlib.sha256(f"{seed}:split:{s.key}".encode()).hexdigest()
        )
        n_test = round(len(members) * TEST_FRACTION)
        for i, s in enumerate(order):
            split[s.key] = "test" if i < n_test else "dev"
    return split


def _dump(model) -> bytes:
    data = model.model_dump(mode="json")
    return (
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def generate_dataset(
    out_dir: str | Path, seed: int = DEFAULT_SEED, dataset_version: str = DEFAULT_VERSION
) -> Manifest:
    out = Path(out_dir)
    inc_dir = out / "incidents"
    inc_dir.mkdir(parents=True, exist_ok=True)
    for stale in inc_dir.glob("*.json"):
        stale.unlink()
    specs = scenario_plan()
    splits = assign_splits(specs, seed)
    entries: list[ManifestEntry] = []
    for spec in specs:
        observable, truth = generate_scenario(spec, seed)
        truth.meta.split = splits[spec.key]
        obs_b, truth_b = _dump(observable), _dump(truth)
        iid = observable.incident.incident_id
        (inc_dir / f"{iid}.json").write_bytes(obs_b)
        (inc_dir / f"{iid}.truth.json").write_bytes(truth_b)
        entries.append(
            ManifestEntry(
                incident_id=iid,
                split=truth.meta.split,
                category=spec.category,
                fault_type=truth.ground_truth.taxonomy_label.value,
                topology=spec.topology,
                period_seconds=truth.meta.period_seconds,
                sha256_observable=_sha(obs_b),
                sha256_truth=_sha(truth_b),
            )
        )
    entries.sort(key=lambda e: e.incident_id)
    counts = Counter(f"category:{e.category}" for e in entries)
    counts.update(f"split:{e.split}" for e in entries)
    counts["total"] = len(entries)
    content = _sha(
        "".join(f"{e.incident_id}{e.sha256_observable}{e.sha256_truth}" for e in entries).encode()
    )
    manifest = Manifest(
        dataset_version=dataset_version,
        generator_version=GENERATOR_VERSION,
        seed=seed,
        counts=dict(sorted(counts.items())),
        content_sha256=content,
        entries=entries,
    )
    (out / "manifest.json").write_bytes(
        (json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2) + "\n").encode()
    )
    return manifest


class DatasetError(Exception):
    pass


class DatasetLoader:
    """Reads a generated dataset. Observable data and ground truth are loaded separately."""

    def __init__(self, root: str | Path, verify: bool = True):
        self.root = Path(root)
        path = self.root / "manifest.json"
        if not path.is_file():
            raise DatasetError(f"no manifest.json in {self.root}")
        self.manifest = Manifest.model_validate_json(path.read_bytes())
        self._entries = {e.incident_id: e for e in self.manifest.entries}
        self.verify = verify

    def incident_ids(self, split: str | None = None) -> list[str]:
        return [e.incident_id for e in self.manifest.entries if split in (None, e.split)]

    def entry(self, incident_id: str) -> ManifestEntry:
        try:
            return self._entries[incident_id]
        except KeyError:
            raise DatasetError(f"unknown incident {incident_id!r}") from None

    def _read(self, incident_id: str, truth: bool) -> bytes:
        e = self.entry(incident_id)
        name = f"{incident_id}.truth.json" if truth else f"{incident_id}.json"
        data = (self.root / "incidents" / name).read_bytes()
        expected = e.sha256_truth if truth else e.sha256_observable
        if self.verify and _sha(data) != expected:
            raise DatasetError(f"checksum mismatch for {name}")
        return data

    def load(self, incident_id: str) -> ObservableScenario:
        return _parse_observable(self._read(incident_id, truth=False))

    def load_truth(self, incident_id: str) -> TruthRecord:
        return TruthRecord.model_validate_json(self._read(incident_id, truth=True))


@lru_cache(maxsize=256)
def _parse_observable(data: bytes) -> ObservableScenario:
    return ObservableScenario.model_validate_json(data)
