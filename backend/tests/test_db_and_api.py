from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.db import Base

EXPECTED_TABLES = {
    "incidents",
    "events",
    "aws_resources",
    "resource_relationships",
    "anomalies",
    "evidence",
    "diagnoses",
    "historical_incidents",
    "evaluation_runs",
    "experiment_results",
    "vector_indexes",
    "vector_items",
}


def _upgrade(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'm.db'}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    return cfg, url


def test_migration_creates_all_tables(tmp_path):
    _, url = _upgrade(tmp_path)
    assert EXPECTED_TABLES <= set(inspect(create_engine(url)).get_table_names())


def test_migration_matches_models(tmp_path):
    _, url = _upgrade(tmp_path)
    with create_engine(url).connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_migration_downgrade(tmp_path):
    cfg, url = _upgrade(tmp_path)
    command.downgrade(cfg, "base")
    assert EXPECTED_TABLES.isdisjoint(inspect(create_engine(url)).get_table_names())


def test_health_ok_with_database(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDDIAG_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'h.db'}")
    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    try:
        r = TestClient(create_app()).get("/health")
    finally:
        get_settings.cache_clear()
    assert r.status_code == 200
    assert r.json()["database"] == "ok"


def test_health_degraded_when_db_down(monkeypatch):
    monkeypatch.setenv("CLOUDDIAG_DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:1/x")
    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    try:
        r = TestClient(create_app()).get("/health")
    finally:
        get_settings.cache_clear()
    assert r.status_code == 503
    assert r.json()["database"] == "unavailable"
