"""Semantic component of the evidence score.

`LexicalSemanticScorer` is the local implementation: TF-IDF cosine similarity between each event
text and the (blinded) incident description, raised to `failure_term_score` when the text
contains a generic failure term. It uses no model download and no network. Phase 5 adds
embedders; any object with the same `scores` method can replace it (DECISIONS D53).
"""

from __future__ import annotations

import re
from typing import Protocol

from sklearn.feature_extraction.text import TfidfVectorizer

#: Generic failure vocabulary (not tied to any taxonomy label).
FAILURE_TERMS = re.compile(
    r"\b(error|errors|fail\w*|exception|timed? ?out|timeout|denied|refused|unhealthy|killed|"
    r"oom\w*|exhaust\w*|throttl\w*|crash\w*|unavailable|unreachable|rejected|panic|fatal|"
    r"exited|stopped|5\d\d)\b",
    re.IGNORECASE,
)


class SemanticScorer(Protocol):
    def scores(self, query: str, texts: list[str]) -> list[float]:
        """Similarity of each text to the query, each in [0, 1]."""


class LexicalSemanticScorer:
    def __init__(self, failure_term_score: float = 0.5):
        self.failure_term_score = failure_term_score

    def scores(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        unique = sorted(set(texts))
        vec = TfidfVectorizer(token_pattern=r"(?u)\b\w\w+\b", sublinear_tf=True)
        try:
            m = vec.fit_transform([query, *unique])
        except ValueError:  # empty vocabulary
            sims = [0.0] * len(unique)
        else:
            sims = (m[1:] @ m[0].T).toarray().ravel().tolist()
        by_text = {}
        for text, sim in zip(unique, sims, strict=True):
            term = self.failure_term_score if FAILURE_TERMS.search(text) else 0.0
            by_text[text] = min(1.0, max(float(sim), term))
        return [by_text[t] for t in texts]
