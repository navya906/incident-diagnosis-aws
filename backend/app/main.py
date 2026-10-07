"""FastAPI application: /health (public) and the /api routes (API key, rate limit, audit)."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.alarms import SnsVerifier
from app.api.jobs import JobRunner
from app.api.lifecycle import now
from app.api.routes import router
from app.api.security import ApiKeyAuth, RateLimiter
from app.config import Settings, get_settings
from app.db.models import AuditLog
from app.db.session import make_engine, make_session_factory
from app.logging_config import configure_logging, scrub

log = logging.getLogger(__name__)
AUDITED_DENIALS = {401, 403, 413, 429}


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    sns_verifier: SnsVerifier | None = None,
    clock=None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = engine or make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    auth = ApiKeyAuth(settings)  # raises in aws mode with auth disabled (fail closed)
    limiter = (
        RateLimiter(settings.api.rate_limit_per_minute, settings.api.rate_limit_burst, clock)
        if clock
        else RateLimiter(settings.api.rate_limit_per_minute, settings.api.rate_limit_burst)
    )
    jobs = JobRunner(sessions, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            recovered = jobs.recover()
            if recovered:
                log.warning("marked %d interrupted diagnosis job(s) as failed", recovered)
        except Exception:  # noqa: BLE001 - the database may not be migrated yet
            log.warning("could not check for interrupted jobs (database not ready?)")
        yield
        jobs.shutdown()

    app = FastAPI(title="Cloud Incident Diagnosis", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.auth = auth
    app.state.limiter = limiter
    app.state.jobs = jobs
    app.state.sns_verifier = sns_verifier or SnsVerifier()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Body limit, rate limit and authentication for /api; audit for changes and denials."""
        request.state.request_id = uuid.uuid4().hex[:12]
        request.state.actor = "unauthenticated"
        request.state.audit_action = None
        request.state.audit_incident = None
        if not request.url.path.startswith("/api"):
            return await call_next(request)
        actor = auth.actor(request.headers)
        client = actor or f"ip:{request.client.host if request.client else 'unknown'}"
        response: Response
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.api.max_body_bytes:
            response = JSONResponse(status_code=413, content={"detail": "request body too large"})
        elif (wait := limiter.take(client)) > 0:
            response = JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(int(wait))},
            )
        elif not auth.configured:
            response = JSONResponse(
                status_code=401, content={"detail": "API keys are not configured (fail closed)"}
            )
        elif actor is None:
            response = JSONResponse(
                status_code=401,
                content={"detail": "missing or invalid API key"},
                headers={"WWW-Authenticate": 'Basic realm="api"'},
            )
        else:
            request.state.actor = actor
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        if request.method != "GET" or response.status_code in AUDITED_DENIALS:
            _audit(request, response.status_code, actor or client)
        return response

    def _audit(request: Request, status: int, actor: str) -> None:
        action = request.state.audit_action or (
            "denied" if status in AUDITED_DENIALS else f"{request.method.lower()}"
        )
        detail = {"request_id": request.state.request_id}
        if request.query_params:
            detail["query"] = scrub(str(request.query_params))[:300]
        try:
            with sessions() as s:
                s.add(
                    AuditLog(
                        at=now(),
                        actor=actor[:64],
                        method=request.method,
                        path=request.url.path[:300],
                        status_code=status,
                        action=action[:64],
                        incident_id=request.state.audit_incident,
                        detail=detail,
                    )
                )
                s.commit()
        except Exception:  # noqa: BLE001 - auditing must not break the response
            log.exception("could not write audit record")
        log.info(
            scrub(
                f"audit actor={actor} {request.method} {request.url.path} -> {status} "
                f"action={action} incident={request.state.audit_incident}"
            )
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        # Location and message only: never echo submitted values back (D96).
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, list) else scrub(str(exc.detail))
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.error("unhandled error %s: %s", rid, scrub(f"{type(exc).__name__}: {exc}"))
        return JSONResponse(
            status_code=500, content={"detail": "internal error", "request_id": rid}
        )

    @app.get("/health")
    def health(response: Response) -> dict[str, str]:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            db = "ok"
        except Exception:  # noqa: BLE001 - health must never raise
            log.exception("database health check failed")
            db = "unavailable"
            response.status_code = 503
        status = "ok" if db == "ok" else "degraded"
        return {"status": status, "database": db, "version": __version__}

    app.include_router(router)
    return app


app = create_app()
