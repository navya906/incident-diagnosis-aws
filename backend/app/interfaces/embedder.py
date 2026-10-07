from abc import ABC, abstractmethod

from app.interfaces._guard import GuardedMeta


class Embedder(ABC, metaclass=GuardedMeta):
    """Text embedder. One embedding model per vector index; never mix models in an index."""

    #: True when calls send data outside this process. External implementations can only be
    #: created through `app.ai.clients` (wrapped in redaction); in-process ones set False.
    is_external: bool = True

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    @abstractmethod
    def dimension(self) -> int: ...

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector of length `dimension` per input text."""
