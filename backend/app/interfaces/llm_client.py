from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from app.interfaces._guard import GuardedMeta


class LLMRequest(BaseModel):
    system: str = ""
    prompt: str
    temperature: float = 0.0
    max_tokens: int = 4096
    seed: int | None = None
    json_mode: bool = True


class LLMResponse(BaseModel):
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0
    extra: dict = Field(default_factory=dict)


class LLMClient(ABC, metaclass=GuardedMeta):
    """OpenAI-compatible, Gemini, and the deterministic LOCAL-ONLY stub implement this."""

    #: True when calls send data outside this process. External implementations can only be
    #: created through `app.ai.clients` (wrapped in redaction); in-process ones set False.
    is_external: bool = True

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse: ...
