"""Real embedding provider — resolves ADR-0028's embedding half via
mem0ai's own embedder abstraction, with no external account or API key.

Design (ADR-0045): mem0ai's `FastEmbedEmbedding` wraps `fastembed`, which
runs a small sentence-embedding model fully locally (downloaded once from
Hugging Face Hub on first use, cached on disk after that — no per-call
network dependency, no API key, no account). This module exposes that
embedder through mem0ai's own `EmbedderFactory`/`EmbedderConfig` classes
(not hand-rolled), so `Mem0EntityLinker` (`src/components/c06_memory.py`)
is genuinely using mem0ai's embedding infrastructure, not a lookalike.

Deliberately does not configure mem0ai's `Memory` class (its full
add/infer/vector-store product) — ADR-0010 already decided against using
Mem0 as this project's storage backend, and `EntityLinker.link(candidate,
existing)`'s signature compares a candidate against an explicitly-provided
list, not against Mem0's own persistent index, so nothing here needs one.
"""

from functools import lru_cache

from mem0.embeddings.configs import EmbedderConfig
from mem0.utils.factory import EmbedderFactory

# BAAI/bge-small-en-v1.5: a small (~130MB), fast, capable-enough sentence
# embedding model for this project's actual job (deciding whether two
# short pieces of financial-analysis text are about the same thing) —
# not mem0ai's own default ("thenlper/gte-large", several times larger)
# since nothing here needs that model's extra capacity.
FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"


@lru_cache(maxsize=1)
def get_embedder():
    """Returns a real, local mem0ai `FastEmbedEmbedding` instance —
    memoized so the model loads (and downloads, on a genuinely first use
    on this machine) exactly once per process, not once per `link()`
    call. No API key, no account, no network access after the first
    download populates the local Hugging Face Hub cache."""
    config = EmbedderConfig(provider="fastembed", config={"model": FASTEMBED_MODEL})
    return EmbedderFactory.create("fastembed", config.config, None)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Standard cosine similarity between two embedding vectors. Returns
    0.0 for a zero vector rather than dividing by zero — an all-zero
    embedding never happens for real text with this model, but a defensive
    default is cheap and avoids a crash on a genuinely degenerate input."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
