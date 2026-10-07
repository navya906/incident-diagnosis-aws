from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class IndexMetadata(BaseModel):
    """Stored with every index so a mismatched embedder is rejected."""

    name: str
    model_name: str
    dimension: int


class SearchHit(BaseModel):
    id: str
    score: float
    payload: dict[str, Any] = {}


class VectorStore(ABC):
    @abstractmethod
    def create_index(self, name: str, model_name: str, dimension: int) -> IndexMetadata: ...

    @abstractmethod
    def index_metadata(self, name: str) -> IndexMetadata: ...

    @abstractmethod
    def add(
        self,
        index: str,
        ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        model_name: str,
    ) -> None:
        """Add vectors. Raises ValueError if model_name or dimension mismatches the index."""

    @abstractmethod
    def search(
        self,
        index: str,
        vector: list[float],
        k: int,
        model_name: str,
        exclude_ids: set[str] | None = None,
    ) -> list[SearchHit]:
        """Top-k by cosine similarity, never returning `exclude_ids` (leave-one-out).
        Raises ValueError on model/dimension mismatch."""

    @abstractmethod
    def ids(self, index: str) -> set[str]: ...
