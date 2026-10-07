"""REST endpoints (all under /api; every route needs an API key).

  POST   /api/incidents                         create: native JSON, SNS notification or
                                                EventBridge alarm event (webhook)
  GET    /api/incidents                         list (status filter, paging)
  GET    /api/incidents/{id}                    detail: lifecycle, timings, counts
  POST   /api/incidents/{id}/transitions        lifecycle change
  POST   /api/incidents/{id}/events             ingest CanonicalEvents (collectors, capture)
  POST   /api/incidents/{id}/inventory          ingest resources and relationships
  GET    /api/incidents/{id}/events             search (source, resource, text, severity)
  GET    /api/incidents/{id}/logs               logs only (text search)
  GET    /api/incidents/{id}/cloudtrail         CloudTrail and Config changes
  GET    /api/incidents/{id}/metrics            metric series with anomaly flags
  GET    /api/incidents/{id}/timeline           events, anomalies, transitions in time order
  GET    /api/incidents/{id}/graph              dependency graph with affected/upstream/...
  GET    /api/incidents/{id}/evidence           ranked evidence with its events
  POST   /api/incidents/{id}/diagnose           start an async diagnosis job (202)
  GET    /api/incidents/{id}/diagnoses          all diagnoses (newest first)
  GET    /api/incidents/{id}/diagnosis          latest diagnosis with severity
  GET    /api/jobs/{job_id}                     job status
  GET    /api/offline/incidents                 dataset incidents available for import
  POST   /api/offline/import                    create an incident from the offline dataset
  GET    /api/metrics/lifecycle                 mean time to detect / diagnose / resolve
  GET    /api/audit                             audit log (newest first)
  GET    /api/experiments, /api/experiments/{id} stored experiment summaries
(DECISIONS D91)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select

from app.api import lifecycle, service
from app.api.alarms import AlarmError, SignatureError, kind_of, parse_alarm
from app.api.schemas import (
    DiagnoseIn,
    EventsIn,
    ImportOfflineIn,
    IncidentCreate,
    InventoryIn,
    TransitionIn,
)
from app.db.models import AuditLog, DiagnosisJob, DiagnosisRecord, Event, EvidenceRecord, Incident
from app.experiments.runner import DEFAULT_RUNS
from app.offline.dataset import DatasetLoader

router = APIRouter(prefix="/api")


def _state(request: Request):
    return request.app.state


def _actor(request: Request) -> str:
    return request.state.actor


def _sf(request: Request) -> service.SecretFilter:
    return service.SecretFilter(_state(request).settings.api.redact_secrets_in_responses)


def _not_found(e: service.NotFound) -> HTTPException:
    return HTTPException(status_code=404, detail=str(e))


def _validation(e: ValidationError) -> HTTPException:
    # Never echo submitted values (they may contain secrets): location and message only.
    return HTTPException(
        status_code=422,
        detail=[{"loc": list(err["loc"]), "msg": err["msg"]} for err in e.errors()],
    )


# ----------------------------------------------------------------------------- incidents
@router.post("/incidents")
async def create_incident(request: Request) -> JSONResponse:
    st = _state(request)
    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="body is not JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    kind = kind_of(body)
    request.state.audit_action = f"incident.create.{kind}"
    source = "api"
    if kind in ("sns-notification", "sns-subscription"):
        if st.settings.api.verify_sns_signature:
            try:
                st.sns_verifier.verify(body)
            except SignatureError as e:
                raise HTTPException(status_code=403, detail=str(e)) from e
        topics = st.settings.api.allowed_sns_topic_arns
        if topics and body.get("TopicArn") not in topics:
            raise HTTPException(status_code=403, detail="SNS topic not allowed")
        if kind == "sns-subscription":
            return JSONResponse(
                status_code=202,
                content={
                    "status": "subscription_not_confirmed",
                    "detail": "verified; confirm the subscription manually (never automatic)",
                },
            )
    if kind == "native":
        try:
            data = IncidentCreate.model_validate(body)
        except ValidationError as e:
            raise _validation(e) from e
    else:
        try:
            alarm = parse_alarm(body)
        except AlarmError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if alarm.state != "ALARM":
            return JSONResponse(
                status_code=202,
                content={"status": "ignored", "detail": f"alarm state {alarm.state}"},
            )
        try:
            data = service.incident_from_alarm(alarm)
        except ValidationError as e:
            raise _validation(e) from e
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)[:500]) from e
        source = f"alarm-{alarm.via}"
    with st.sessions() as s:
        inc, created = service.create_incident(s, data, source, _actor(request), st.settings)
        s.commit()
        request.state.audit_incident = inc.id
        view = service.incident_view(s, inc)
    return JSONResponse(
        status_code=201 if created else 200,
        content=jsonable_encoder({"created": created, **view}),
    )


@router.get("/incidents")
def list_incidents(
    request: Request,
    status: lifecycle.Status | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    with _state(request).sessions() as s:
        stmt = select(Incident)
        if status:
            stmt = stmt.where(Incident.status == status.value)
        rows = s.scalars(
            stmt.order_by(Incident.created_at.desc(), Incident.id).limit(limit).offset(offset)
        ).all()
        return {
            "items": [service.incident_view(s, r) for r in rows],
            "limit": limit,
            "offset": offset,
        }


@router.get("/incidents/{incident_id}")
def get_incident(request: Request, incident_id: str) -> dict:
    with _state(request).sessions() as s:
        try:
            return service.incident_view(s, service.get_incident(s, incident_id), full=True)
        except service.NotFound as e:
            raise _not_found(e) from e


@router.post("/incidents/{incident_id}/transitions")
def post_transition(request: Request, incident_id: str, body: TransitionIn) -> dict:
    request.state.audit_action, request.state.audit_incident = "incident.transition", incident_id
    with _state(request).sessions() as s:
        try:
            inc = service.get_incident(s, incident_id)
            lifecycle.transition(s, inc, body.to, _actor(request), body.note)
        except service.NotFound as e:
            raise _not_found(e) from e
        except lifecycle.TransitionError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        s.commit()
        return service.incident_view(s, inc, full=True)


# ----------------------------------------------------------------------------- telemetry in
@router.post("/incidents/{incident_id}/events")
def post_events(request: Request, incident_id: str, body: EventsIn) -> dict:
    request.state.audit_action, request.state.audit_incident = "events.ingest", incident_id
    st = _state(request)
    if len(body.events) > st.settings.api.max_events_per_request:
        raise HTTPException(status_code=413, detail="too many events in one request")
    with st.sessions() as s:
        try:
            service.get_incident(s, incident_id)
            out = service.ingest_events(s, incident_id, body.events)
        except service.NotFound as e:
            raise _not_found(e) from e
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)[:500]) from e
        s.commit()
        return out


@router.post("/incidents/{incident_id}/inventory")
def post_inventory(request: Request, incident_id: str, body: InventoryIn) -> dict:
    request.state.audit_action, request.state.audit_incident = "inventory.ingest", incident_id
    with _state(request).sessions() as s:
        try:
            inc = service.get_incident(s, incident_id)
        except service.NotFound as e:
            raise _not_found(e) from e
        out = service.ingest_inventory(
            s, incident_id, body.resources, body.relationships, (inc.extra or {}).get("region")
        )
        s.commit()
        return out


@router.get("/offline/incidents")
def offline_incidents(
    request: Request, split: str | None = Query(None, pattern="^(dev|test)$")
) -> dict:
    """Dataset incidents available for import: id, split and title (observable fields only;
    never category, labels or any other ground truth)."""
    from app.api.jobs import dataset_dir

    try:
        loader = DatasetLoader(dataset_dir(_state(request).settings))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="offline dataset not available") from e
    items = []
    for iid in loader.incident_ids(split):
        inc = loader.load(iid).incident
        items.append(
            {
                "incident_id": iid,
                "split": loader.entry(iid).split,
                "title": inc.title,
                "alarm_time": inc.alarm_time,
            }
        )
    return {"items": items, "dataset_version": loader.manifest.dataset_version}


@router.post("/offline/import")
def import_offline(request: Request, body: ImportOfflineIn) -> JSONResponse:
    request.state.audit_action = "incident.import_offline"
    st = _state(request)
    from app.api.jobs import dataset_dir

    try:
        loader = DatasetLoader(dataset_dir(st.settings))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="offline dataset not available") from e
    with st.sessions() as s:
        try:
            inc, created = service.import_offline(
                s, loader, body.offline_incident_id, _actor(request), st.settings
            )
        except service.NotFound as e:
            raise _not_found(e) from e
        s.commit()
        request.state.audit_incident = inc.id
        view = service.incident_view(s, inc)
    return JSONResponse(
        status_code=201 if created else 200,
        content=jsonable_encoder({"created": created, **view}),
    )


# ----------------------------------------------------------------------------- telemetry out
def _events(request: Request, incident_id: str, sources, resource_id, q, severity, limit, offset):
    with _state(request).sessions() as s:
        try:
            service.get_incident(s, incident_id)
        except service.NotFound as e:
            raise _not_found(e) from e
        rows, total = service.query_events(
            s, incident_id, sources, resource_id, q, severity, limit, offset
        )
        sf = _sf(request)
        return {
            "items": [service.event_view(r, sf) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


_SOURCES = {"cloudwatch_metric", "cloudwatch_log", "cloudtrail", "aws_config", "alarm"}


@router.get("/incidents/{incident_id}/events")
def get_events(
    request: Request,
    incident_id: str,
    source: list[str] | None = Query(None),
    resource_id: str | None = Query(None, max_length=300),
    q: str | None = Query(None, max_length=200),
    severity: str | None = Query(None, pattern="^(INFO|WARNING|ERROR|CRITICAL)$"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict:
    if source and set(source) - _SOURCES:
        raise HTTPException(status_code=422, detail=f"source must be one of {sorted(_SOURCES)}")
    return _events(request, incident_id, source, resource_id, q, severity, limit, offset)


@router.get("/incidents/{incident_id}/logs")
def get_logs(
    request: Request,
    incident_id: str,
    q: str | None = Query(None, max_length=200),
    severity: str | None = Query(None, pattern="^(INFO|WARNING|ERROR|CRITICAL)$"),
    resource_id: str | None = Query(None, max_length=300),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict:
    return _events(
        request, incident_id, ["cloudwatch_log"], resource_id, q, severity, limit, offset
    )


@router.get("/incidents/{incident_id}/cloudtrail")
def get_cloudtrail(
    request: Request,
    incident_id: str,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict:
    return _events(request, incident_id, ["cloudtrail", "aws_config"], None, q, None, limit, offset)


@router.get("/incidents/{incident_id}/metrics")
def get_metrics(
    request: Request,
    incident_id: str,
    resource_id: str | None = Query(None, max_length=300),
    metric: str | None = Query(None, max_length=128),
) -> dict:
    with _state(request).sessions() as s:
        try:
            service.get_incident(s, incident_id)
        except service.NotFound as e:
            raise _not_found(e) from e
        return {"series": service.metric_series(s, incident_id, resource_id, metric)}


@router.get("/incidents/{incident_id}/timeline")
def get_timeline(
    request: Request, incident_id: str, limit: int = Query(500, ge=1, le=5000)
) -> dict:
    with _state(request).sessions() as s:
        try:
            service.get_incident(s, incident_id)
        except service.NotFound as e:
            raise _not_found(e) from e
        return {"items": service.timeline_view(s, incident_id, _sf(request), limit)}


@router.get("/incidents/{incident_id}/graph")
def get_graph(request: Request, incident_id: str) -> dict:
    with _state(request).sessions() as s:
        try:
            return service.graph_view(s, incident_id)
        except service.NotFound as e:
            raise _not_found(e) from e


@router.get("/incidents/{incident_id}/evidence")
def get_evidence(request: Request, incident_id: str) -> dict:
    with _state(request).sessions() as s:
        try:
            service.get_incident(s, incident_id)
        except service.NotFound as e:
            raise _not_found(e) from e
        rows = s.scalars(
            select(EvidenceRecord)
            .where(EvidenceRecord.incident_id == incident_id)
            .order_by(EvidenceRecord.rank)
        ).all()
        events = {
            e.event_id: e
            for e in s.scalars(select(Event).where(Event.event_id.in_([r.event_id for r in rows])))
        }
        sf = _sf(request)
        return {
            "items": [
                {
                    "evidence_id": r.evidence_id,
                    "rank": r.rank,
                    "score": r.score,
                    "components": r.components,
                    "event": service.event_view(events[r.event_id], sf)
                    if r.event_id in events
                    else None,
                }
                for r in rows
            ]
        }


# ----------------------------------------------------------------------------- diagnosis
@router.post("/incidents/{incident_id}/diagnose", status_code=202)
def start_diagnosis(request: Request, incident_id: str, body: DiagnoseIn | None = None) -> dict:
    request.state.audit_action, request.state.audit_incident = "diagnosis.start", incident_id
    body = body or DiagnoseIn()
    try:
        job = _state(request).jobs.submit(incident_id, body.model_dump(), _actor(request))
    except service.NotFound as e:
        raise _not_found(e) from e
    return {
        "job_id": job.id,
        "status": job.status,
        "incident_id": incident_id,
        "poll": f"/api/jobs/{job.id}",
    }


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict:
    with _state(request).sessions() as s:
        job = s.get(DiagnosisJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        return {
            "job_id": job.id,
            "incident_id": job.incident_id,
            "status": job.status,
            "params": job.params,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "diagnosis_id": job.diagnosis_id,
            "error": job.error,
            "requested_by": job.requested_by,
        }


@router.get("/incidents/{incident_id}/diagnoses")
def list_diagnoses(request: Request, incident_id: str) -> dict:
    with _state(request).sessions() as s:
        rows = s.scalars(
            select(DiagnosisRecord)
            .where(DiagnosisRecord.incident_id == incident_id)
            .order_by(DiagnosisRecord.id.desc())
        ).all()
        return {"items": [service.diagnosis_view(r, s, _sf(request)) for r in rows]}


@router.get("/incidents/{incident_id}/diagnosis")
def latest_diagnosis(request: Request, incident_id: str) -> dict:
    with _state(request).sessions() as s:
        row = s.scalars(
            select(DiagnosisRecord)
            .where(DiagnosisRecord.incident_id == incident_id)
            .order_by(DiagnosisRecord.id.desc())
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="no diagnosis yet")
        return service.diagnosis_view(row, s, _sf(request))


# ----------------------------------------------------------------------------- reporting
@router.get("/metrics/lifecycle")
def lifecycle_metrics(request: Request) -> dict:
    with _state(request).sessions() as s:
        return lifecycle.aggregate(s)


@router.get("/audit")
def audit(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    incident_id: str | None = Query(None, max_length=64),
) -> dict:
    with _state(request).sessions() as s:
        stmt = select(AuditLog)
        if incident_id:
            stmt = stmt.where(AuditLog.incident_id == incident_id)
        rows = s.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit)).all()
        return {
            "items": [
                {
                    "at": r.at,
                    "actor": r.actor,
                    "method": r.method,
                    "path": r.path,
                    "status_code": r.status_code,
                    "action": r.action,
                    "incident_id": r.incident_id,
                    "detail": r.detail,
                }
                for r in rows
            ]
        }


def _runs_dir(request: Request) -> Path:
    return Path(getattr(_state(request), "runs_dir", DEFAULT_RUNS))


@router.get("/experiments")
def experiments(request: Request) -> dict:
    items: list[dict[str, Any]] = []
    for m in sorted(_runs_dir(request).glob("exp-*/manifest.json")):
        d = json.loads(m.read_text(encoding="utf-8"))
        items.append(
            {
                k: d.get(k)
                for k in (
                    "experiment_id",
                    "name",
                    "label",
                    "split",
                    "started_at",
                    "records",
                    "conditions",
                )
            }
        )
    return {"items": items}


@router.get("/experiments/{experiment_id}")
def experiment(request: Request, experiment_id: str) -> dict:
    if not experiment_id.startswith("exp-") or not experiment_id[4:].isalnum():
        raise HTTPException(status_code=422, detail="bad experiment id")
    d = _runs_dir(request) / experiment_id
    if not (d / "manifest.json").is_file():
        raise HTTPException(status_code=404, detail="experiment not found")
    out = {"manifest": json.loads((d / "manifest.json").read_text(encoding="utf-8"))}
    for name in ("summary", "comparisons", "strata"):
        f = d / f"{name}.json"
        out[name] = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
    return out
