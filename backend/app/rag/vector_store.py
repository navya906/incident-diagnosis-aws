"""Vector stores behind the `VectorStore` interface (cosine similarity on L2-normalised vectors).

  LocalVectorStore  FAISS `IndexFlatIP` when `faiss` is importable, otherwise NumPy exact search
                    with identical results (the "FAISS fallback" store, BRIEF Section 1). Can be
                    saved to / loaded from a directory.
  PgVectorStore     PostgreSQL + pgvector (`vector_indexes` / `vector_items`, migration 0002),
                    ordered by the `<=>` cosine-distance operator. On other dialects (SQLite in
                    tests) it computes the same similarity in NumPy.

Every index stores its embedding model name and dimension; adding or searching with another
model or dimension raises ValueError, so one index never mixes models (DECISIONS D61).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import VectorIndexRecord, VectorItemRecord
from app.interfaces.vector_store import IndexMetadata, SearchHit, VectorStore

try:  # optional extra `rag`
    import faiss  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised when faiss-cpu is absent
    faiss = None


def _normalise(vectors: list[list[float]] | np.ndarray, dim: int) -> np.ndarray:
    m = np.asarray(vectors, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    if m.shape[1] != dim:
        raise ValueError(f"vector dimension {m.shape[1]} != index dimension {dim}")
    if not np.all(np.isfinite(m)):
        raise ValueError("vectors must be finite")
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def _check_model(meta: IndexMetadata, model_name: str) -> None:
    if model_name != meta.model_name:
        raise ValueError(
            f"index {meta.name!r} was built with {meta.model_name!r}, not {model_name!r}; "
            "never mix embedding models in one index"
        )


def _top(scores: np.ndarray, ids: list[str], payloads: list[dict], k: int, exclude: set[str]):
    order = sorted(
        (i for i in range(len(ids)) if ids[i] not in exclude),
        key=lambda i: (-round(float(scores[i]), 6), ids[i]),
    )
    return [
        SearchHit(id=ids[i], score=round(float(scores[i]), 6), payload=payloads[i])
        for i in order[:k]
    ]


@dataclass
class _LocalIndex:
    meta: IndexMetadata
    ids: list[str] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    matrix: np.ndarray | None = None
    faiss_index: Any = None


class LocalVectorStore(VectorStore):
    def __init__(self, path: str | Path | None = None, use_faiss: bool | None = None):
        self.path = Path(path) if path else None
        self.use_faiss = (faiss is not None) if use_faiss is None else use_faiss
        if self.use_faiss and faiss is None:
            raise RuntimeError('faiss is not installed; pip install -e ".[rag]"')
        self._indexes: dict[str, _LocalIndex] = {}
        if self.path and self.path.is_dir():
            for meta_file in sorted(self.path.glob("*/meta.json")):
                self._load(meta_file.parent)

    @property
    def backend(self) -> str:
        return "faiss" if self.use_faiss else "numpy"

    def create_index(self, name: str, model_name: str, dimension: int) -> IndexMetadata:
        meta = IndexMetadata(name=name, model_name=model_name, dimension=dimension)
        if name in self._indexes:
            if self._indexes[name].meta != meta:
                raise ValueError(f"index {name!r} exists with {self._indexes[name].meta}")
            return meta
        self._indexes[name] = _LocalIndex(meta, matrix=np.zeros((0, dimension), np.float32))
        if self.use_faiss:
            self._indexes[name].faiss_index = faiss.IndexFlatIP(dimension)
        return meta

    def _get(self, name: str) -> _LocalIndex:
        if name not in self._indexes:
            raise KeyError(f"no index {name!r}")
        return self._indexes[name]

    def index_metadata(self, name: str) -> IndexMetadata:
        return self._get(name).meta

    def add(self, index, ids, vectors, payloads, model_name) -> None:
        idx = self._get(index)
        _check_model(idx.meta, model_name)
        if not (len(ids) == len(vectors) == len(payloads)):
            raise ValueError("ids, vectors and payloads must have the same length")
        if len(set(ids)) != len(ids) or set(ids) & set(idx.ids):
            raise ValueError("duplicate ids")
        if not ids:
            return
        m = _normalise(vectors, idx.meta.dimension)
        idx.ids += list(ids)
        idx.payloads += [dict(p) for p in payloads]
        idx.matrix = np.vstack([idx.matrix, m])
        if idx.faiss_index is not None:
            idx.faiss_index.add(m)

    def search(self, index, vector, k, model_name, exclude_ids=None) -> list[SearchHit]:
        idx = self._get(index)
        _check_model(idx.meta, model_name)
        q = _normalise(vector, idx.meta.dimension)
        exclude = set(exclude_ids or ())
        n = len(idx.ids)
        if n == 0 or k <= 0:
            return []
        if idx.faiss_index is not None:
            # Exact search over every vector so tie-breaking matches the NumPy path.
            scores_k, pos = idx.faiss_index.search(q, n)
            scores = np.empty(n, np.float32)
            scores[pos[0]] = scores_k[0]
        else:
            scores = idx.matrix @ q[0]
        return _top(scores, idx.ids, idx.payloads, k, exclude)

    def ids(self, index: str) -> set[str]:
        return set(self._get(index).ids)

    def clone(self) -> LocalVectorStore:
        """Independent in-memory copy (vectors are copied, not re-embedded)."""
        other = LocalVectorStore(use_faiss=self.use_faiss)
        for name, idx in self._indexes.items():
            m = idx.meta
            other.create_index(m.name, m.model_name, m.dimension)
            if idx.ids:
                other.add(
                    name, list(idx.ids), idx.matrix, [dict(p) for p in idx.payloads], m.model_name
                )
        return other

    # ------------------------------------------------------------------ persistence
    def save(self, path: str | Path | None = None) -> Path:
        root = Path(path) if path else self.path
        if root is None:
            raise ValueError("no path to save to")
        for name, idx in self._indexes.items():
            d = root / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(idx.meta.model_dump_json(indent=2) + "\n", "utf-8")
            np.save(d / "vectors.npy", idx.matrix)
            items = [{"id": i, "payload": p} for i, p in zip(idx.ids, idx.payloads, strict=True)]
            (d / "items.json").write_text(json.dumps(items, sort_keys=True) + "\n", "utf-8")
        return root

    def _load(self, d: Path) -> None:
        meta = IndexMetadata.model_validate_json((d / "meta.json").read_text("utf-8"))
        items = json.loads((d / "items.json").read_text("utf-8"))
        matrix = np.load(d / "vectors.npy")
        self.create_index(meta.name, meta.model_name, meta.dimension)
        self.add(
            meta.name,
            [i["id"] for i in items],
            matrix,
            [i["payload"] for i in items],
            meta.model_name,
        )


class PgVectorStore(VectorStore):
    def __init__(self, engine: Engine):
        self.engine = engine

    @property
    def is_postgres(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    def create_index(self, name: str, model_name: str, dimension: int) -> IndexMetadata:
        meta = IndexMetadata(name=name, model_name=model_name, dimension=dimension)
        with Session(self.engine) as s, s.begin():
            row = s.get(VectorIndexRecord, name)
            if row is None:
                s.add(
                    VectorIndexRecord(
                        name=name,
                        model_name=model_name,
                        dimension=dimension,
                        created_at=datetime.now(UTC),
                    )
                )
            elif (row.model_name, row.dimension) != (model_name, dimension):
                raise ValueError(f"index {name!r} exists with {row.model_name}/{row.dimension}")
        return meta

    def index_metadata(self, name: str) -> IndexMetadata:
        with Session(self.engine) as s:
            row = s.get(VectorIndexRecord, name)
            if row is None:
                raise KeyError(f"no index {name!r}")
            return IndexMetadata(name=row.name, model_name=row.model_name, dimension=row.dimension)

    def add(self, index, ids, vectors, payloads, model_name) -> None:
        meta = self.index_metadata(index)
        _check_model(meta, model_name)
        if not (len(ids) == len(vectors) == len(payloads)):
            raise ValueError("ids, vectors and payloads must have the same length")
        if len(set(ids)) != len(ids) or set(ids) & self.ids(index):
            raise ValueError("duplicate ids")
        m = _normalise(vectors, meta.dimension) if ids else np.zeros((0, meta.dimension))
        with Session(self.engine) as s, s.begin():
            for item_id, v, p in zip(ids, m, payloads, strict=True):
                s.add(
                    VectorItemRecord(
                        index_name=index, item_id=item_id, embedding=v.tolist(), payload=dict(p)
                    )
                )

    def search(self, index, vector, k, model_name, exclude_ids=None) -> list[SearchHit]:
        meta = self.index_metadata(index)
        _check_model(meta, model_name)
        q = _normalise(vector, meta.dimension)[0]
        exclude = set(exclude_ids or ())
        with Session(self.engine) as s:
            if self.is_postgres:
                literal = "[" + ",".join(repr(float(x)) for x in q) + "]"
                rows = s.execute(
                    text(
                        "SELECT item_id, payload, 1 - (embedding <=> CAST(:q AS vector)) AS score "
                        "FROM vector_items WHERE index_name = :i "
                        "ORDER BY embedding <=> CAST(:q AS vector), item_id LIMIT :n"
                    ),
                    {"q": literal, "i": index, "n": k + len(exclude)},
                ).all()
                ids = [r.item_id for r in rows]
                payloads = [r.payload for r in rows]
                scores = np.array([r.score for r in rows], dtype=np.float32)
            else:
                rows = s.execute(
                    select(VectorItemRecord).where(VectorItemRecord.index_name == index)
                ).scalars()
                rows = list(rows)
                ids = [r.item_id for r in rows]
                payloads = [r.payload for r in rows]
                if not rows:
                    return []
                scores = np.asarray([r.embedding for r in rows], dtype=np.float32) @ q
        return _top(scores, ids, payloads, k, exclude)

    def ids(self, index: str) -> set[str]:
        with Session(self.engine) as s:
            return set(
                s.execute(
                    select(VectorItemRecord.item_id).where(VectorItemRecord.index_name == index)
                ).scalars()
            )


def build_vector_store(backend: str, path: str | None = None, engine: Engine | None = None):
    if backend == "faiss":
        return LocalVectorStore(path)
    if backend == "numpy":
        return LocalVectorStore(path, use_faiss=False)
    if backend == "pgvector":
        if engine is None:
            raise ValueError("pgvector backend needs a database engine")
        return PgVectorStore(engine)
    raise ValueError(f"unknown vector store backend {backend!r}")
