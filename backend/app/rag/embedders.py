"""Embedders behind the `Embedder` interface.

  hashing                LOCAL-ONLY, deterministic feature hashing of word uni/bigrams (no model
                         download, no network). Used by tests and offline smoke runs.
  sentence_transformers  local model (default in config; needs the `embeddings` extra).
  openai                 OpenAI-compatible /embeddings endpoint (external).
  gemini                 Google Gemini batchEmbedContents (external).

`build_embedder` wraps every external embedder in `RedactingEmbedder` when redaction is on, so
no raw identifier is sent out (DECISIONS D59, D60).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import urllib.request
from collections import Counter
from collections.abc import Callable
from typing import Any

from app.ai.redaction import Redactor
from app.config import EmbeddingSettings, RedactionSettings, Settings
from app.interfaces.embedder import Embedder

log = logging.getLogger(__name__)

Post = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[A-Za-z0-9]+")


def _http_post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (configured URL)
        return json.loads(resp.read().decode())


def tokenize(text: str) -> list[str]:
    """Lower-case words; CamelCase and snake_case are split (HTTPCode_Target_5XX -> http code
    target 5xx), so metric and API names share tokens with prose."""
    out = []
    for word in _WORD.findall(text.replace("_", " ")):
        out += [p.lower() for p in _CAMEL.split(word) if p]
    return out


class HashingEmbedder(Embedder):
    """LOCAL-ONLY. Signed feature hashing of unigrams and bigrams, log term frequency, L2."""

    is_external = False

    def __init__(self, dimension: int = 384):
        if dimension < 8:
            raise ValueError("dimension must be >= 8")
        self._dim = dimension

    @property
    def model_name(self) -> str:
        return f"hashing-v1-{self._dim}"

    @property
    def dimension(self) -> int:
        return self._dim

    def _slot(self, feature: str) -> tuple[int, float]:
        h = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        n = int.from_bytes(h, "big")
        return n % self._dim, (1.0 if (n >> 63) & 1 else -1.0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            toks = tokenize(text)
            feats = Counter(toks) + Counter(
                f"{a} {b}" for a, b in zip(toks, toks[1:], strict=False)
            )
            v = [0.0] * self._dim
            for f, c in feats.items():
                i, sign = self._slot(f)
                v[i] += sign * (1.0 + math.log(c))
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class SentenceTransformerEmbedder(Embedder):
    is_external = False  # runs in-process (the model download itself sends no incident data)

    def __init__(self, model: str, batch_size: int = 64):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # pragma: no cover - depends on the optional extra
            raise RuntimeError(
                'sentence-transformers is not installed; run `pip install -e ".[embeddings]"` '
                "or set embeddings.provider to `hashing` (LOCAL-ONLY) for offline runs"
            ) from e
        self._name = model
        self._model = SentenceTransformer(model)
        self._batch = batch_size

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def dimension(self) -> int:
        return int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(texts, batch_size=self._batch, normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


class _RemoteEmbedder(Embedder):
    is_external = True

    def __init__(self, settings: EmbeddingSettings, post: Post | None = None):
        if settings.api_key is None:
            raise ValueError(f"{type(self).__name__} needs CLOUDDIAG_EMBEDDINGS__API_KEY")
        self.settings = settings
        self._post = post or _http_post
        self._dim: int | None = None

    @property
    def model_name(self) -> str:
        return self.settings.model

    @property
    def dimension(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        b = self.settings.batch_size
        for i in range(0, len(texts), b):
            out += self._batch(texts[i : i + b])
        if out:
            self._dim = len(out[0])
        return out

    def _batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class OpenAIEmbedder(_RemoteEmbedder):
    """OpenAI-compatible POST {base_url}/embeddings (OpenAI, Azure-style proxies, local servers)."""

    def _batch(self, texts: list[str]) -> list[list[float]]:
        base = (self.settings.base_url or "https://api.openai.com/v1").rstrip("/")
        key = self.settings.api_key.get_secret_value()
        data = self._post(
            f"{base}/embeddings",
            {"Authorization": f"Bearer {key}"},
            {"model": self.settings.model, "input": texts},
            self.settings.timeout_seconds,
        )
        rows = sorted(data["data"], key=lambda r: r["index"])
        return [list(map(float, r["embedding"])) for r in rows]


class GeminiEmbedder(_RemoteEmbedder):
    def _batch(self, texts: list[str]) -> list[list[float]]:
        base = (
            self.settings.base_url or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        model = self.settings.model
        name = model if model.startswith("models/") else f"models/{model}"
        data = self._post(
            f"{base}/{name}:batchEmbedContents",
            {"x-goog-api-key": self.settings.api_key.get_secret_value()},
            {"requests": [{"model": name, "content": {"parts": [{"text": t}]}} for t in texts]},
            self.settings.timeout_seconds,
        )
        return [list(map(float, e["values"])) for e in data["embeddings"]]


class RedactingEmbedder(Embedder):
    """Redacts each text (its own pseudonym mapping, so the same document always embeds the
    same way) and refuses to send text that fails the strict check."""

    is_external = True

    def __init__(self, inner: Embedder, settings: RedactionSettings):
        self.inner = inner
        self.settings = settings

    @property
    def model_name(self) -> str:
        return self.inner.model_name

    @property
    def dimension(self) -> int:
        return self.inner.dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        safe = []
        for t in texts:
            r = Redactor(self.settings)
            safe.append(r.ensure_safe(r.redact(t)))
        return self.inner.embed(safe)


def build_embedder(settings: Settings, post: Post | None = None) -> Embedder:
    e = settings.embeddings
    if e.provider == "hashing":
        emb: Embedder = HashingEmbedder(e.dimension)
    elif e.provider == "sentence_transformers":
        emb = SentenceTransformerEmbedder(e.model, e.batch_size)
    elif e.provider == "openai":
        emb = OpenAIEmbedder(e, post)
    elif e.provider == "gemini":
        emb = GeminiEmbedder(e, post)
    else:
        raise ValueError(f"unknown embeddings provider {e.provider!r}")
    if emb.is_external:
        if settings.redaction.enabled:
            return RedactingEmbedder(emb, settings.redaction)
        log.warning("redaction is disabled: %s receives raw text", e.provider)
    return emb
