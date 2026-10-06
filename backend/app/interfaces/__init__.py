"""Abstract interfaces. Real and local implementations plug in behind these."""

from app.interfaces.collector import CollectionRequest, Collector
from app.interfaces.detector import Detector
from app.interfaces.embedder import Embedder
from app.interfaces.graph_store import GraphStore
from app.interfaces.llm_client import LLMClient, LLMRequest, LLMResponse
from app.interfaces.vector_store import IndexMetadata, SearchHit, VectorStore

__all__ = [
    "CollectionRequest",
    "Collector",
    "Detector",
    "Embedder",
    "GraphStore",
    "IndexMetadata",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "SearchHit",
    "VectorStore",
]
