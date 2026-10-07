"""Portable column types."""

from __future__ import annotations

from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator, UserDefinedType


class _PgVector(UserDefinedType):
    """pgvector's `vector` type without a fixed dimension (one table serves every index; the
    dimension is enforced per index by the store). Values travel as '[x,y,...]' text."""

    cache_ok = True

    def get_col_spec(self, **kw) -> str:
        return "vector"

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return "[" + ",".join(repr(float(x)) for x in value) + "]"

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None or isinstance(value, list):
                return value
            return [float(x) for x in str(value).strip("[]").split(",") if x]

        return process


class VectorType(TypeDecorator):
    """pgvector `vector` on PostgreSQL, JSON elsewhere (SQLite in tests)."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(_PgVector())
        return dialect.type_descriptor(JSON())
