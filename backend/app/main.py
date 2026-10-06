from __future__ import annotations

import logging

from fastapi import FastAPI, Response
from sqlalchemy import text

from app import __version__
from app.config import get_settings
from app.db.session import make_engine
from app.logging_config import configure_logging

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    app = FastAPI(title="Cloud Incident Diagnosis", version=__version__)
    engine = make_engine(settings.database_url)

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

    return app


app = create_app()
