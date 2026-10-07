"""Capture a real incident from AWS into the offline observable format.

    python -m app.collectors.capture --seed-arn <alarmed resource ARN> \
        --start 2026-10-06T10:00:00Z --end 2026-10-06T11:30:00Z \
        --title "ALARM: 5xx on web ALB" --description "Users see 5xx errors" --out captured/

Writes ``<out>/<incident_id>.json`` (an ObservableScenario: incident, inventory, CanonicalEvents).
Ground truth for real captures is written by a human separately; it is never inferred here.
Uses the standard AWS credential chain and only read-only APIs (infra/iam/).
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
from pathlib import Path

from app.ai.clients import redaction_policy_violations
from app.collectors.aws_collector import AwsCollector
from app.collectors.resources import canonical_id_from_arn
from app.config import get_settings
from app.interfaces.collector import CollectionRequest
from app.offline.models import IncidentRecord, ObservableScenario


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Capture a real AWS incident (read-only).")
    p.add_argument("--seed-arn", action="append", required=True, help="repeatable")
    p.add_argument("--start", required=True, type=_ts)
    p.add_argument("--end", required=True, type=_ts)
    p.add_argument("--alarm-time", type=_ts, default=None)
    p.add_argument("--title", required=True)
    p.add_argument("--description", required=True, help="symptoms only (blinded)")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    full = get_settings()
    # Real telemetry only enters the system in real-AWS mode, where redaction is mandatory and
    # external LLM/embedding clients fail closed (DECISIONS D68).
    if full.data_mode != "aws":
        p.error("capturing real AWS data requires data_mode=aws (CLOUDDIAG_DATA_MODE=aws)")
    if problems := redaction_policy_violations(full):
        p.error("redaction policy not met for real AWS data: " + "; ".join(problems))
    settings = full.aws
    collector, inventory = AwsCollector.from_settings(settings, args.seed_arn)
    resource_ids = [r.resource_id for r in inventory.resources]
    incident_id = (
        "real-"
        + hashlib.sha256(f"{args.seed_arn}|{args.start.isoformat()}".encode()).hexdigest()[:10]
    )
    request = CollectionRequest(
        incident_id=incident_id,
        resource_ids=resource_ids,
        window_start=args.start,
        window_end=args.end,
    )
    events = collector.collect(request)
    resources, relationships = inventory.records()
    scenario = ObservableScenario(
        incident=IncidentRecord(
            incident_id=incident_id,
            title=args.title,
            description=args.description,
            alarm_time=args.alarm_time or args.start,
            window_start=args.start,
            window_end=args.end,
            affected_resources=[canonical_id_from_arn(a) for a in args.seed_arn],
            region=settings.region,
        ),
        resources=resources,
        relationships=relationships,
        events=events,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{incident_id}.json"
    path.write_text(scenario.model_dump_json(indent=1))
    print(f"captured {len(events)} events over {len(resources)} resources -> {path}")
    for w in inventory.warnings + collector.warnings:
        print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
