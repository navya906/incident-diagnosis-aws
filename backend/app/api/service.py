"""Database operations behind the API (incidents, telemetry, diagnoses)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ai.redaction import Redactor
from app.api import lifecycle
from app.api.alarms import Alarm, resources_from_dimensions
from app.api.schemas import IncidentCreate
from app.config import RedactionSettings, Settings
from app.contracts.events import CanonicalEvent, EventSource, contract_violations
from app.contracts.evidence import make_evidence_id
from app.db.models import (
    AnomalyRecord,
    AwsResource,
    DiagnosisRecord,
    Event,
    EvidenceRecord,
    Incident,
    ResourceRelationship,
)
from app.graph.builder import build_graph
from app.offline.dataset import DatasetError, DatasetLoader
from app.offline.models import IncidentRecord, RelationshipRecord, ResourceRecord

#: Only the secrets category: identifiers stay readable for the operator (D96).
_SECRETS_ONLY = RedactionSettings(
    account_ids=False, arns=False, ips=False, principals=False, hostnames=False, strict=False
)


class NotFound(LookupError):
    pass


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def new_incident_id(seed: str) -> str:
    return "inc-" + hashlib.sha256(seed.encode()).hexdigest()[:10]


# ----------------------------------------------------------------------------- incidents
def create_incident(
    session: Session,
    data: IncidentCreate,
    source: str,
    actor: str,
    settings: Settings,
    extra: dict | None = None,
) -> tuple[Incident, bool]:
    """Create, or return the open incident with the same dedup key (created=False)."""
    if data.dedup_key:
        existing = session.scalars(
            select(Incident).where(
                Incident.dedup_key == data.dedup_key,
                Incident.status.in_([s.value for s in lifecycle.OPEN]),
            )
        ).first()
        if existing is not None:
            return existing, False
    created = lifecycle.now()
    alarm = _utc(data.alarm_time)
    api = settings.api
    start = _utc(data.window_start) or alarm - timedelta(minutes=api.window_before_minutes)
    end = _utc(data.window_end) or alarm + timedelta(minutes=api.window_after_minutes)
    inc = Incident(
        id=new_incident_id(f"{data.title}|{alarm.isoformat()}|{created.isoformat()}|{actor}"),
        title=data.title,
        description=data.description,
        status=lifecycle.Status.DETECTED.value,
        window_start=start,
        window_end=end,
        affected_resources=list(data.affected_resources),
        created_at=created,
        updated_at=created,
        alarm_time=alarm,
        onset_at=_utc(data.onset_at),
        source=source,
        dedup_key=data.dedup_key,
        extra={"region": data.region, **(extra or {})},
    )
    session.add(inc)
    session.flush()
    lifecycle.record_initial(session, inc, actor, created)
    return inc, True


def incident_from_alarm(alarm: Alarm) -> IncidentCreate:
    resources = resources_from_dimensions(alarm.namespace, alarm.dimensions)
    if not resources:
        raise ValueError(
            f"alarm {alarm.alarm_name!r} has no dimension that maps to a supported resource"
        )
    metric = alarm.metric or "metric"
    return IncidentCreate(
        title=f"ALARM: {metric} on {resources[0]}"[:300],
        description=(
            f"CloudWatch alarm {alarm.alarm_name} entered ALARM state for {metric}"
            f" ({alarm.namespace or 'unknown namespace'}) on {', '.join(resources)}."
        )[:5000],
        alarm_time=alarm.time,
        affected_resources=resources,
        region=alarm.region or "us-east-1",
        dedup_key=f"alarm:{alarm.account or '-'}:{alarm.region or '-'}:{alarm.alarm_name}"[:300],
    )


def get_incident(session: Session, incident_id: str) -> Incident:
    inc = session.get(Incident, incident_id)
    if inc is None:
        raise NotFound(f"incident {incident_id} not found")
    return inc


def incident_view(session: Session, inc: Incident, full: bool = False) -> dict:
    history = lifecycle.transitions_of(session, inc.id)
    out = {
        "id": inc.id,
        "title": inc.title,
        "description": inc.description,
        "status": inc.status,
        "severity": inc.severity,
        "source": inc.source,
        "affected_resources": inc.affected_resources,
        "alarm_time": _utc(inc.alarm_time),
        "window_start": _utc(inc.window_start),
        "window_end": _utc(inc.window_end),
        "created_at": _utc(inc.created_at),
        "updated_at": _utc(inc.updated_at),
        "timings": lifecycle.timings(inc, history),
        "allowed_transitions": sorted(
            s.value for s in lifecycle.ALLOWED[lifecycle.Status(inc.status)]
        ),
    }
    if full:
        out["transitions"] = [
            {
                "from": t.from_status,
                "to": t.to_status,
                "at": _utc(t.at),
                "actor": t.actor,
                "note": t.note,
            }
            for t in history
        ]
        out["counts"] = {
            "events": session.scalar(
                select(func.count()).select_from(Event).where(Event.incident_id == inc.id)
            ),
            "diagnoses": session.scalar(
                select(func.count())
                .select_from(DiagnosisRecord)
                .where(DiagnosisRecord.incident_id == inc.id)
            ),
        }
    return out


# ----------------------------------------------------------------------------- telemetry
def ingest_events(session: Session, incident_id: str, events: list[CanonicalEvent]) -> dict:
    problems = {e.event_id: contract_violations(e) for e in events}
    problems = {k: v for k, v in problems.items() if v}
    if problems:
        first = next(iter(problems.items()))
        raise ValueError(f"{len(problems)} event(s) violate the contract, e.g. {first}")
    ids = [e.event_id for e in events]
    existing = set(session.scalars(select(Event.event_id).where(Event.event_id.in_(ids))))
    added = 0
    for e in events:
        if e.event_id in existing:
            continue
        existing.add(e.event_id)
        session.add(
            Event(
                event_id=e.event_id,
                incident_id=incident_id,
                timestamp=e.timestamp,
                source=e.source.value,
                service=e.service,
                resource_id=e.resource_id,
                event_type=e.event_type,
                metric=e.metric,
                value=e.value,
                severity=e.severity.value,
                message=e.message,
                meta=e.metadata,
                raw_ref=e.raw_ref,
            )
        )
        added += 1
    return {"received": len(events), "added": added, "duplicates": len(events) - added}


def ingest_inventory(
    session: Session,
    incident_id: str,
    resources: list[ResourceRecord],
    relationships: list[RelationshipRecord],
    region: str | None = None,
) -> dict:
    session.execute(delete(AwsResource).where(AwsResource.incident_id == incident_id))
    session.execute(
        delete(ResourceRelationship).where(ResourceRelationship.incident_id == incident_id)
    )
    for r in resources:
        session.add(
            AwsResource(
                incident_id=incident_id,
                resource_id=r.resource_id,
                resource_type=r.resource_type,
                service=r.service,
                region=region,
                attributes={"role": r.role, **r.attributes},
            )
        )
    for rel in relationships:
        session.add(
            ResourceRelationship(
                incident_id=incident_id,
                source_id=rel.source_id,
                target_id=rel.target_id,
                relation_type=rel.relation_type,
            )
        )
    return {"resources": len(resources), "relationships": len(relationships)}


def import_offline(
    session: Session, loader: DatasetLoader, offline_id: str, actor: str, settings: Settings
) -> tuple[Incident, bool]:
    """Create an incident from the offline dataset (observable data only, never ground truth).
    Importing the same dataset incident again returns the existing one."""
    existing = session.get(Incident, offline_id)
    if existing is not None:
        return existing, False
    try:
        s = loader.load(offline_id)
    except DatasetError as e:
        raise NotFound(f"dataset incident {offline_id} not found") from e
    created = lifecycle.now()
    inc = Incident(
        id=offline_id,
        title=s.incident.title,
        description=s.incident.description,
        status=lifecycle.Status.DETECTED.value,
        scenario_id=offline_id,
        window_start=s.incident.window_start,
        window_end=s.incident.window_end,
        affected_resources=s.incident.affected_resources,
        created_at=created,
        updated_at=created,
        alarm_time=s.incident.alarm_time,
        source="replay",
        dedup_key=None,
        extra={"region": s.incident.region, "dataset_version": loader.manifest.dataset_version},
    )
    session.add(inc)
    session.flush()
    lifecycle.record_initial(session, inc, actor, created)
    ingest_events(session, inc.id, s.events)
    ingest_inventory(session, inc.id, s.resources, s.relationships, s.incident.region)
    return inc, True


def _canonical_event(row: Event) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=row.event_id,
        timestamp=_utc(row.timestamp),
        source=EventSource(row.source),
        service=row.service,
        resource_id=row.resource_id,
        event_type=row.event_type,
        metric=row.metric,
        value=row.value,
        severity=row.severity,
        message=row.message,
        metadata=row.meta or {},
        raw_ref=row.raw_ref,
    )


def load_case(session: Session, incident_id: str):
    """Everything the diagnosis engine needs, from the database."""
    inc = get_incident(session, incident_id)
    events = [
        _canonical_event(r)
        for r in session.scalars(
            select(Event)
            .where(Event.incident_id == incident_id)
            .order_by(Event.timestamp, Event.event_id)
        )
    ]
    resources = [
        ResourceRecord(
            resource_id=r.resource_id,
            resource_type=r.resource_type,
            service=r.service,
            role=str((r.attributes or {}).get("role", "unknown")),
            attributes={k: v for k, v in (r.attributes or {}).items() if k != "role"},
        )
        for r in session.scalars(select(AwsResource).where(AwsResource.incident_id == incident_id))
    ]
    relationships = [
        RelationshipRecord(
            source_id=r.source_id, target_id=r.target_id, relation_type=r.relation_type
        )
        for r in session.scalars(
            select(ResourceRelationship).where(ResourceRelationship.incident_id == incident_id)
        )
    ]
    if not resources:  # no inventory: at least the affected resources are known
        resources = [
            ResourceRecord(
                resource_id=rid, resource_type="", service=rid.split("/", 1)[0], role="affected"
            )
            for rid in inc.affected_resources
        ]
    record = IncidentRecord(
        incident_id=inc.id,
        title=inc.title,
        description=inc.description,
        alarm_time=_utc(inc.alarm_time or inc.created_at),
        window_start=_utc(inc.window_start),
        window_end=_utc(inc.window_end),
        affected_resources=list(inc.affected_resources),
        region=(inc.extra or {}).get("region", "us-east-1"),
    )
    return inc, record, events, resources, relationships


# ----------------------------------------------------------------------------- read views
class SecretFilter:
    """Removes secrets from event text before it leaves the API (D96)."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def text(self, value: str) -> str:
        return Redactor(_SECRETS_ONLY).redact(value) if self.enabled and value else value

    def obj(self, value):
        return Redactor(_SECRETS_ONLY).redact_obj(value) if self.enabled else value


def event_view(row: Event, sf: SecretFilter) -> dict:
    return {
        "event_id": row.event_id,
        "timestamp": _utc(row.timestamp),
        "source": row.source,
        "service": row.service,
        "resource_id": row.resource_id,
        "event_type": row.event_type,
        "metric": row.metric,
        "value": row.value,
        "severity": row.severity,
        "message": sf.text(row.message),
        "metadata": sf.obj(row.meta or {}),
    }


def query_events(
    session: Session,
    incident_id: str,
    sources: list[str] | None = None,
    resource_id: str | None = None,
    q: str | None = None,
    severity: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[Event], int]:
    stmt = select(Event).where(Event.incident_id == incident_id)
    if sources:
        stmt = stmt.where(Event.source.in_(sources))
    if resource_id:
        stmt = stmt.where(Event.resource_id == resource_id)
    if severity:
        stmt = stmt.where(Event.severity == severity)
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        stmt = stmt.where((Event.message.ilike(like)) | (Event.event_type.ilike(like)))
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.scalars(
        stmt.order_by(Event.timestamp, Event.event_id).limit(limit).offset(offset)
    ).all()
    return list(rows), int(total or 0)


def metric_series(
    session: Session, incident_id: str, resource_id: str | None, metric: str | None
) -> list[dict]:
    rows, _ = query_events(session, incident_id, ["cloudwatch_metric"], resource_id, limit=100_000)
    series: dict[tuple, list] = {}
    for r in rows:
        if metric and r.metric != metric:
            continue
        series.setdefault((r.resource_id, r.metric), []).append(
            {"t": _utc(r.timestamp), "v": r.value}
        )
    anomalies = session.scalars(
        select(AnomalyRecord).where(AnomalyRecord.incident_id == incident_id)
    ).all()
    flagged: dict[tuple, list] = {}
    for a in anomalies:
        flagged.setdefault((a.resource_id, a.metric), []).append(
            {"t": _utc(a.timestamp), "score": a.score, "baseline": a.baseline}
        )
    return [
        {"resource_id": rid, "metric": m, "points": pts, "anomalies": flagged.get((rid, m), [])}
        for (rid, m), pts in sorted(series.items())
    ]


def graph_view(session: Session, incident_id: str) -> dict:
    inc, _, _, resources, relationships = load_case(session, incident_id)
    g = build_graph(resources, relationships)
    affected = set(inc.affected_resources)
    sources = set()
    for a in affected:
        if g.has_node(a):
            sources |= g.impact_sources({a})
    upstream = set().union(*(g.upstream(a) for a in affected if g.has_node(a)))
    downstream = set().union(*(g.downstream(a) for a in affected if g.has_node(a)))
    data = g.to_dict()
    for n in data["nodes"]:
        nid = n["id"]
        n["affected"] = nid in affected
        n["upstream"] = nid in upstream
        n["downstream"] = nid in downstream
        n["can_cause"] = nid in sources and nid not in affected
    return data


def timeline_view(session: Session, incident_id: str, sf: SecretFilter, limit: int) -> list[dict]:
    """Non-metric events, anomalies and lifecycle transitions in time order."""
    items = []
    rows, _ = query_events(
        session,
        incident_id,
        ["cloudwatch_log", "cloudtrail", "aws_config", "alarm"],
        limit=limit,
    )
    for r in rows:
        items.append({"kind": "event", "at": _utc(r.timestamp), **event_view(r, sf)})
    for a in session.scalars(
        select(AnomalyRecord).where(AnomalyRecord.incident_id == incident_id)
    ).all():
        items.append(
            {
                "kind": "anomaly",
                "at": _utc(a.timestamp),
                "resource_id": a.resource_id,
                "metric": a.metric,
                "score": a.score,
                "baseline": a.baseline,
                "observed": a.observed,
                "method": a.method,
            }
        )
    for t in lifecycle.transitions_of(session, incident_id):
        items.append(
            {
                "kind": "transition",
                "at": _utc(t.at),
                "from": t.from_status,
                "to": t.to_status,
                "actor": t.actor,
                "note": t.note,
            }
        )
    items.sort(key=lambda x: (x["at"], x["kind"]))
    return items[:limit]


# ----------------------------------------------------------------------------- diagnoses
def persist_result(session: Session, incident_id: str, result, investigation, extra: dict) -> int:
    """Store anomalies, ranked evidence and the diagnosis (with severity and citations)."""
    session.execute(delete(AnomalyRecord).where(AnomalyRecord.incident_id == incident_id))
    for a in investigation.anomalies or []:
        session.add(
            AnomalyRecord(
                incident_id=incident_id,
                resource_id=a.resource_id,
                metric=a.metric,
                timestamp=a.timestamp,
                score=a.score,
                baseline=a.baseline,
                observed=a.observed,
                method=a.method,
            )
        )
    session.execute(delete(EvidenceRecord).where(EvidenceRecord.incident_id == incident_id))
    for item in investigation.ranking.items:
        session.add(
            EvidenceRecord(
                evidence_id=item.evidence_id,
                incident_id=incident_id,
                event_id=item.event_id,
                rank=item.rank,
                score=item.score,
                components=item.components.model_dump(),
            )
        )
    output = {
        "diagnosis": result.diagnosis.model_dump(mode="json") if result.diagnosis else None,
        "severity": result.severity.model_dump(mode="json"),
        "llm_severity_suggestion": result.llm_severity_suggestion,
        "citation": result.citation.model_dump(),
        "self_consistency": result.self_consistency.model_dump(),
        "verbalized_confidence": result.verbalized_confidence,
        "policy_overrides": result.policy_overrides,
        "retrieved_incident_ids": result.retrieved_incident_ids,
        "context_sections": [s.model_dump() for s in result.context_sections],
        "context_evidence_ids": result.context_evidence_ids,
        "label": result.label,
        "prompt_sha": result.prompt_sha,
        "cost_usd": result.cost_usd,
        **extra,
    }
    row = DiagnosisRecord(
        incident_id=incident_id,
        condition=result.condition,
        model=result.model,
        prompt_version=result.prompt_version,
        valid=result.valid,
        output=output,
        failure=result.failure,
        latency_ms=result.latency_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        created_at=lifecycle.now(),
    )
    session.add(row)
    inc = get_incident(session, incident_id)
    inc.severity = result.severity.level.value
    inc.onset_at = inc.onset_at or _utc(investigation.onset)
    inc.updated_at = lifecycle.now()
    session.flush()
    return row.id


def resolve_evidence(
    session: Session, incident_id: str, evidence_ids: list[str], sf: SecretFilter
) -> dict[str, dict]:
    """Event behind each cited evidence id (ids that do not resolve are simply absent)."""
    wanted = set(evidence_ids)
    out = {}
    for row in session.scalars(select(Event).where(Event.incident_id == incident_id)):
        evd = make_evidence_id(incident_id, row.event_id)
        if evd in wanted:
            out[evd] = event_view(row, sf)
    return out


def dependency_path(session: Session, incident_id: str, resource_id: str | None) -> list[str]:
    """How a failure of the root-cause resource reaches the alarmed resource (impact graph)."""
    if not resource_id:
        return []
    inc, _, _, resources, relationships = load_case(session, incident_id)
    g = build_graph(resources, relationships)
    best: list[str] = []
    for target in inc.affected_resources:
        if resource_id == target:
            return [resource_id]
        if g.has_node(resource_id) and g.has_node(target):
            p = g.impact_path(resource_id, target)
            if p and (not best or len(p) < len(best)):
                best = p
    return best


def diagnosis_view(
    row: DiagnosisRecord, session: Session | None = None, sf: SecretFilter | None = None
) -> dict:
    """With a session: also the cited events (`evidence_events`) and the dependency path from
    the root-cause resource to the alarmed resource, so a client never has to show a
    conclusion without its evidence (D99)."""
    out = _diagnosis_fields(row)
    if session is not None:
        d = (row.output or {}).get("diagnosis") or {}
        cited = [e["evidence_id"] for e in d.get("supporting_evidence", [])]
        cited += [e["evidence_id"] for e in d.get("contradicting_evidence", [])]
        out["evidence_events"] = resolve_evidence(
            session, row.incident_id, cited, sf or SecretFilter(True)
        )
        out["dependency_path"] = dependency_path(
            session, row.incident_id, (d.get("root_cause") or {}).get("resource_id")
        )
    return out


def _diagnosis_fields(row: DiagnosisRecord) -> dict:
    return {
        "id": row.id,
        "incident_id": row.incident_id,
        "condition": row.condition,
        "model": row.model,
        "prompt_version": row.prompt_version,
        "valid": row.valid,
        "created_at": _utc(row.created_at),
        "latency_ms": row.latency_ms,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        **(row.output or {}),
        "failure": row.failure,
    }
