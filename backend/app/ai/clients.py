"""The only way to obtain an LLM or embedding client.

`create_llm_client` / `create_embedder` construct the implementation (external classes cannot
be constructed anywhere else, see `app.interfaces._guard`) and hand back:

- in-process implementations (stub LLM, hashing / SentenceTransformers embedders) as they are;
- external implementations wrapped in `RedactingLLMClient` / `RedactingEmbedder`.

Real-AWS mode (`data_mode: aws`) fails closed: an external client is refused unless redaction
is enabled, strict and every built-in category is on (DECISIONS D67, D68). In offline mode an
operator may disable redaction for synthetic data; a warning is logged.
"""

from __future__ import annotations

import logging
from typing import TypeVar

from app.ai.redaction import RedactingLLMClient
from app.config import REQUIRED_AWS_CATEGORIES, Settings
from app.interfaces._guard import _FACTORY_KEY
from app.interfaces.embedder import Embedder
from app.interfaces.llm_client import LLMClient

log = logging.getLogger(__name__)

L = TypeVar("L", bound=LLMClient)
E = TypeVar("E", bound=Embedder)


class RedactionPolicyError(RuntimeError):
    """The configuration would let raw identifiers leave the process in real-AWS mode."""


def redaction_policy_violations(settings: Settings) -> list[str]:
    """Why the redaction settings are not acceptable for real AWS data (empty = acceptable)."""
    r = settings.redaction
    problems = []
    if not r.enabled:
        problems.append("redaction.enabled is false")
    if not r.strict:
        problems.append("redaction.strict is false")
    problems += [f"redaction.{c} is false" for c in REQUIRED_AWS_CATEGORIES if not getattr(r, c)]
    return problems


def _check_external(settings: Settings, what: str) -> bool:
    """True when the external client must be wrapped; raises in aws mode if unsafe."""
    if settings.data_mode == "aws":
        problems = redaction_policy_violations(settings)
        if problems:
            raise RedactionPolicyError(
                f"refusing to create external {what} in real-AWS mode: " + "; ".join(problems)
            )
        return True
    if not settings.redaction.enabled:
        log.warning("redaction is disabled (offline mode): %s receives raw text", what)
        return False
    return True


def create_llm_client(cls: type[L], settings: Settings, *args, **kwargs) -> LLMClient:
    if not getattr(cls, "is_external", True):
        return cls(*args, **kwargs)
    wrap = _check_external(settings, f"LLM client {cls.__name__}")
    client = cls(*args, _factory_key=_FACTORY_KEY, **kwargs)
    return RedactingLLMClient(client, settings.redaction) if wrap else client


def create_embedder(cls: type[E], settings: Settings, *args, **kwargs) -> Embedder:
    from app.rag.embedders import RedactingEmbedder  # avoid an import cycle

    if not getattr(cls, "is_external", True):
        return cls(*args, **kwargs)
    wrap = _check_external(settings, f"embedder {cls.__name__}")
    emb = cls(*args, _factory_key=_FACTORY_KEY, **kwargs)
    return RedactingEmbedder(emb, settings.redaction) if wrap else emb
