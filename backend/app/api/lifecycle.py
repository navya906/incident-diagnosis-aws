"""Incident lifecycle: states, allowed transitions, timings.

    DETECTED -> INVESTIGATING -> DIAGNOSED -> MITIGATING -> RESOLVED -> CLOSED

Allowed moves (anything else is rejected with 409):

  DETECTED       -> INVESTIGATING, CLOSED*
  INVESTIGATING  -> DIAGNOSED, CLOSED*
  DIAGNOSED      -> INVESTIGATING (re-diagnose), MITIGATING, RESOLVED, CLOSED*
  MITIGATING     -> RESOLVED, INVESTIGATING
  RESOLVED       -> CLOSED, INVESTIGATING (reopen)
  CLOSED         -> (terminal)

  * closing an unresolved incident (e.g. a false alarm) requires a note.

A diagnosis job moves DETECTED -> INVESTIGATING when it starts and INVESTIGATING -> DIAGNOSED
when it succeeds (actor "system"). Timings (DECISIONS D93):

  time_to_detect   alarm_time - onset_at          (monitoring delay; onset from the pipeline)
  time_to_diagnose first DIAGNOSED - created_at   (from the incident entering the system)
  time_to_resolve  first RESOLVED  - created_at
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Incident, IncidentTransition


class Status(StrEnum):
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    MITIGATING = "MITIGATING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


ALLOWED: dict[Status, frozenset[Status]] = {
    Status.DETECTED: frozenset({Status.INVESTIGATING, Status.CLOSED}),
    Status.INVESTIGATING: frozenset({Status.DIAGNOSED, Status.CLOSED}),
    Status.DIAGNOSED: frozenset(
        {Status.INVESTIGATING, Status.MITIGATING, Status.RESOLVED, Status.CLOSED}
    ),
    Status.MITIGATING: frozenset({Status.RESOLVED, Status.INVESTIGATING}),
    Status.RESOLVED: frozenset({Status.CLOSED, Status.INVESTIGATING}),
    Status.CLOSED: frozenset(),
}
OPEN = frozenset(set(Status) - {Status.CLOSED, Status.RESOLVED})


class TransitionError(ValueError):
    """The requested status change is not allowed from the current status."""


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def now() -> datetime:
    return datetime.now(UTC)


def record_initial(session: Session, incident: Incident, actor: str, at: datetime) -> None:
    session.add(
        IncidentTransition(
            incident_id=incident.id,
            from_status=None,
            to_status=Status.DETECTED.value,
            at=at,
            actor=actor,
            note="incident created",
        )
    )


def transition(
    session: Session,
    incident: Incident,
    to: Status | str,
    actor: str,
    note: str = "",
    at: datetime | None = None,
) -> IncidentTransition:
    current, target = Status(incident.status), Status(to)
    if target not in ALLOWED[current]:
        raise TransitionError(f"cannot move from {current.value} to {target.value}")
    unresolved = current not in (Status.RESOLVED,)
    if target == Status.CLOSED and unresolved and not note.strip():
        raise TransitionError("closing an unresolved incident needs a note (e.g. false alarm)")
    at = at or now()
    row = IncidentTransition(
        incident_id=incident.id,
        from_status=current.value,
        to_status=target.value,
        at=at,
        actor=actor,
        note=note,
    )
    session.add(row)
    incident.status = target.value
    incident.updated_at = at
    return row


def transitions_of(session: Session, incident_id: str) -> list[IncidentTransition]:
    return list(
        session.scalars(
            select(IncidentTransition)
            .where(IncidentTransition.incident_id == incident_id)
            .order_by(IncidentTransition.at, IncidentTransition.id)
        )
    )


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    a, b = _utc(a), _utc(b)
    if a is None or b is None:
        return None
    return round((a - b).total_seconds() / 60.0, 2)


def timings(incident: Incident, history: list[IncidentTransition]) -> dict:
    def first(status: Status) -> datetime | None:
        return next((t.at for t in history if t.to_status == status.value), None)

    diagnosed, resolved = first(Status.DIAGNOSED), first(Status.RESOLVED)
    return {
        "detected_at": _utc(incident.alarm_time or incident.created_at),
        "onset_at": _utc(incident.onset_at),
        "diagnosed_at": _utc(diagnosed),
        "resolved_at": _utc(resolved),
        "closed_at": _utc(first(Status.CLOSED)),
        "time_to_detect_min": _minutes(incident.alarm_time, incident.onset_at),
        "time_to_diagnose_min": _minutes(diagnosed, incident.created_at),
        "time_to_resolve_min": _minutes(resolved, incident.created_at),
    }


def aggregate(session: Session) -> dict:
    incidents = list(session.scalars(select(Incident)))
    by_status = {s.value: 0 for s in Status}
    values: dict[str, list[float]] = {
        "time_to_detect_min": [],
        "time_to_diagnose_min": [],
        "time_to_resolve_min": [],
    }
    for inc in incidents:
        by_status[inc.status] = by_status.get(inc.status, 0) + 1
        t = timings(inc, transitions_of(session, inc.id))
        for k in values:
            if t[k] is not None:
                values[k].append(t[k])
    out = {"incidents": len(incidents), "by_status": by_status}
    for k, v in values.items():
        name = k.replace("time_to_", "mean_time_to_")
        out[name] = round(statistics.fmean(v), 2) if v else None
        out[k.replace("_min", "_n")] = len(v)
    return out
