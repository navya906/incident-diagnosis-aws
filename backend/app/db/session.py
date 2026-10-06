from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    connect_args = {} if url.startswith("sqlite") else {"connect_timeout": 3}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    factory = make_session_factory(make_engine())
    with factory() as session:
        yield session
