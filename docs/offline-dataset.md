# Offline dataset (Phase 1)

> All data described here is **smoke-test / synthetic**. It is produced by a simulator, not by AWS.

## Generate

```bash
cd backend
python -m app.offline.generate                    # seed 42 -> ../data/generated/synthetic-v1
python -m app.offline.generate --seed 7 --out ../data/generated/seed7
```

Generated data is git-ignored. It is reproducible: the same seed and generator version give
byte-identical files. Reference value (seed 42, generator 1.1.0):
`content_sha256 = 06cec245b951be8e9b08524367439b5ee3d37fa75d6551d3bb8e2922d8388f8b`.
(Generator 1.0.0 gave `4460094203eb...`, identical on Windows/Python 3.12 and Linux/Python 3.11, D30.)
If you get a different hash on another OS, record it in DECISIONS.md (see D22).

## Composition (seed 42)

| Category | Count | Notes |
|---|---|---|
| standard | 100 | 10 fault types x 10 variants |
| red_herring | 12 | real fault + an unrelated deployment/config change on an off-path resource within ±10 min of onset |
| insufficient_evidence | 6 | real fault, but telemetry stripped (`alarm_only` or `sparse`); label is `insufficient_evidence` |
| compound | 6 | two concurrent faults, second starts 3 to 7 min later; primary = earlier fault |
| **total** | **124** | dev 86 / test 38 (stratified, ~30% test per stratum) |

Variants change topology, onset time, severity (0.55 to 1.0), noise (x0.7 to x2.0), metric resolution
(60 s or 300 s, half each per fault type), random missing points (0 to 8%), pre-onset collection gaps,
and whether the triggering CloudTrail/Config change is visible.

Topologies: `ecs` (ALB -> ECS -> RDS), `lambda` (ALB -> Lambda -> RDS), `ec2` (ALB -> EC2 -> RDS),
`sqs` (ALB -> ECS -> SQS <- Lambda consumer -> RDS). Each also has a DB security group and an IAM role.

## Files

```
manifest.json                 version, seed, counts, split, per-file sha256, content hash
incidents/<id>.json           OBSERVABLE: incident (blinded description, alarm, window),
                              resources, relationships, CanonicalEvents
incidents/<id>.truth.json     GROUND TRUTH: label, primary resource, text, onset, evidence event ids,
                              resolution, anomaly labels (injected windows), generator metadata
```

Ground truth is kept in a separate file so it cannot leak into what the system sees.
`ReplayCollector` and `DatasetLoader.load()` read only the observable file; experiment code reads
truth through `DatasetLoader.load_truth()`. Incident ids are opaque hashes (`inc-xxxxxxxxxx`).

## Blinding

The description is built only from the alarm that fired (metric + resource) and a generic symptom
sentence for that alarm. It never names the root cause. `tests/test_offline_dataset.py` checks every
description against per-fault forbidden words.

## Use from code

```python
from app.offline.replay import ReplayCollector
c = ReplayCollector("../data/generated/synthetic-v1")
iid = c.incident_ids(split="dev")[0]
events = c.collect(c.default_request(iid))     # list[CanonicalEvent]
resources, relationships = c.inventory(iid)     # for the dependency graph
```

## Real incidents (later)

`infra/test-stack/template.yaml` and `python -m app.offline.fault_injection` let you deploy a small
test stack and inject each fault for real. See `infra/README.md`. Not run yet.
