"""Construction guard for clients that send data out of the process.

`LLMClient` and `Embedder` use `GuardedMeta`. Instantiating a subclass whose `is_external` is
True raises `DirectConstructionError` unless the private factory key is passed, and only
`app.ai.clients` passes it, always returning the instance wrapped in redaction. Redaction
wrappers (`is_redaction_wrapper = True`) and in-process implementations are not gated
(DECISIONS D67).
"""

from __future__ import annotations

from abc import ABCMeta

#: Only `app.ai.clients` may reference this (a test scans the source tree).
_FACTORY_KEY = object()


class DirectConstructionError(TypeError):
    """An external LLM/embedding client was constructed outside the wrapping factory."""


class GuardedMeta(ABCMeta):
    def __call__(cls, *args, _factory_key: object = None, **kwargs):
        gated = getattr(cls, "is_external", True) and not getattr(
            cls, "is_redaction_wrapper", False
        )
        if gated and _factory_key is not _FACTORY_KEY:
            raise DirectConstructionError(
                f"{cls.__name__} sends data out of the process; create it with "
                "app.ai.clients.create_llm_client / create_embedder so it is wrapped in redaction"
            )
        return super().__call__(*args, **kwargs)
