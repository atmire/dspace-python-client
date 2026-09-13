"""
Unique search queries for every simulated user.

Solr answers a repeated query from its caches, so a load test that reuses queries measures
the cache, not the server. This generator draws terms from vocabulary harvested from the
target repository (titles, authors, subjects seen during discovery) plus a built-in
academic word list, and refuses to hand out the same query twice within a run. The RNG is
seeded per run, so two runs only repeat each other when ``--seed`` is passed on purpose.
"""

from __future__ import annotations

import random
import re
import string
from collections.abc import Iterable

FALLBACK_VOCAB = [
    "analysis",
    "model",
    "theory",
    "method",
    "data",
    "study",
    "system",
    "network",
    "approach",
    "evaluation",
    "learning",
    "quantum",
    "climate",
    "energy",
    "cell",
    "protein",
    "gene",
    "health",
    "policy",
    "education",
    "social",
    "urban",
    "history",
    "language",
    "culture",
    "economic",
    "market",
    "growth",
    "risk",
    "design",
    "optimization",
    "structure",
    "dynamics",
    "simulation",
    "measurement",
    "survey",
    "review",
    "case",
    "field",
    "water",
    "soil",
    "species",
    "ecology",
    "evolution",
    "neural",
    "cognitive",
    "behaviour",
    "memory",
    "perception",
    "decision",
    "algorithm",
    "graph",
    "statistical",
    "bayesian",
    "regression",
    "classification",
    "image",
    "signal",
    "material",
    "polymer",
    "catalyst",
    "reaction",
    "synthesis",
    "sensor",
    "device",
    "circuit",
    "control",
    "robot",
    "vehicle",
    "transport",
    "infrastructure",
    "sustainability",
    "governance",
    "law",
    "rights",
    "gender",
    "migration",
    "identity",
    "media",
    "digital",
    "archive",
    "heritage",
    "museum",
    "library",
    "open",
    "access",
    "thesis",
    "dissertation",
    "journal",
    "conference",
    "proceedings",
    "chapter",
    "report",
    "dataset",
    "sediment",
    "carbon",
    "nitrogen",
    "ocean",
    "forest",
    "agriculture",
    "crop",
    "yield",
    "irrigation",
    "drought",
    "virus",
    "vaccine",
    "infection",
    "immune",
    "clinical",
    "patient",
    "therapy",
    "diagnosis",
    "cancer",
    "diabetes",
]

BROWSE_TYPES = ("author", "title", "dateissued", "subject")
SORT_OPTIONS = (
    None,
    "dc.date.accessioned,desc",
    "dc.title,asc",
    "dc.date.issued,desc",
    "score,desc",
)

_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ]{4,}")
_STOP = frozenset(
    [
        "this",
        "that",
        "with",
        "from",
        "have",
        "were",
        "been",
        "their",
        "there",
        "which",
        "about",
        "into",
        "over",
        "under",
        "than",
        "then",
        "them",
        "they",
        "what",
        "when",
        "where",
        "while",
        "would",
        "could",
        "should",
        "also",
        "more",
        "most",
        "such",
        "these",
        "those",
        "through",
        "between",
        "among",
        "after",
        "before",
        "during",
        "within",
        "without",
        "other",
        "some",
    ]
)


class QueryGenerator:
    def __init__(self, seed: int | None = None, vocab: Iterable[str] | None = None) -> None:
        self.rng = random.Random(seed)
        self._vocab: list[str] = []
        self._vocab_set: set[str] = set()
        self._phrases: list[str] = []
        self._seen: set[str] = set()
        self.issued = 0
        self.collisions = 0
        self.add_vocabulary(FALLBACK_VOCAB)
        if vocab:
            self.add_vocabulary(vocab)

    # -- vocabulary ------------------------------------------------------------

    def add_vocabulary(self, words: Iterable[str]) -> None:
        for w in words:
            lw = w.lower().strip()
            if lw and lw not in self._vocab_set and lw not in _STOP:
                self._vocab_set.add(lw)
                self._vocab.append(lw)

    def add_text(self, text: str) -> None:
        """Harvest words and one 2-word phrase from a title or similar string."""
        words = [w.lower() for w in _WORD_RE.findall(text) if w.lower() not in _STOP]
        self.add_vocabulary(words)
        if len(words) >= 2:
            i = self.rng.randrange(0, len(words) - 1)
            self._phrases.append(f"{words[i]} {words[i + 1]}")
            if len(self._phrases) > 5000:
                del self._phrases[:1000]

    @property
    def vocabulary_size(self) -> int:
        return len(self._vocab)

    # -- queries -----------------------------------------------------------------

    def _candidate(self) -> str:
        roll = self.rng.random()
        if roll < 0.15 and self._phrases:
            return f'"{self.rng.choice(self._phrases)}"'
        if roll < 0.20:
            # A nonsense-but-plausible token: matches nothing, still a full Solr round-trip.
            return "".join(self.rng.choices(string.ascii_lowercase, k=self.rng.randint(6, 9)))
        n = self.rng.choices((1, 2, 3), weights=(45, 40, 15))[0]
        return " ".join(self.rng.sample(self._vocab, k=min(n, len(self._vocab))))

    def next_query(self) -> str:
        """A query string not issued before in this run."""
        for _ in range(50):
            q = self._candidate()
            if q not in self._seen:
                self._seen.add(q)
                self.issued += 1
                return q
            self.collisions += 1
        # Vocabulary exhausted in practice: append a counter so uniqueness still holds.
        q = f"{self._candidate()} {self.issued}"
        self._seen.add(q)
        self.issued += 1
        return q

    def next_search_params(self) -> dict[str, str]:
        """Parameters for the discovery REST search: query plus occasional sort/page/facet."""
        params: dict[str, str] = {"query": self.next_query()}
        sort = self.rng.choice(SORT_OPTIONS)
        if sort:
            params["sort"] = sort
        r = self.rng.random()
        if r < 0.15:
            params["page"] = str(self.rng.randint(1, 4))
        if r > 0.8:
            year = self.rng.randint(1990, 2025)
            params["f.dateIssued"] = f"[{year} TO {year + self.rng.randint(0, 5)}],equals"
        return params

    def next_browse(self) -> tuple[str, dict[str, str]]:
        """A browse index name and its query parameters (startsWith or a value)."""
        kind = self.rng.choice(BROWSE_TYPES)
        params: dict[str, str] = {}
        if kind == "dateissued":
            params["startsWith"] = str(self.rng.randint(1950, 2025))
        elif self.rng.random() < 0.6:
            params["startsWith"] = self.rng.choice(string.ascii_uppercase)
        else:
            params["value"] = self.rng.choice(self._vocab).title()
        if self.rng.random() < 0.3:
            params["page"] = str(self.rng.randint(1, 5))
        return kind, params

    def stats(self) -> dict:
        return {
            "vocabulary_size": self.vocabulary_size,
            "phrases": len(self._phrases),
            "queries_issued": self.issued,
            "duplicate_candidates_rejected": self.collisions,
        }


__all__ = ["BROWSE_TYPES", "FALLBACK_VOCAB", "QueryGenerator"]
