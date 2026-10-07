from abc import ABC, abstractmethod


class Embedder(ABC):
    """Text embedder. One embedding model per vector index; never mix models in an index."""

    #: True when calls send data outside this process. External implementations are wrapped
    #: with redaction (`app.ai.redaction`); only in-process implementations may set False.
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
