from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    # SQLite connections are used by the API worker threads too (diagnosis jobs).
    connect_args = (
        {"check_same_thread": False} if url.startswith("sqlite") else {"connect_timeout": 3}
    )
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
