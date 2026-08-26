"""Tests for src/mem0_embedder.py — the real, local embedding provider
that resolves ADR-0028's embedding-similarity half (ADR-0045). No API key
or account is used anywhere here: `get_embedder()` loads mem0ai's
fastembed-backed embedder, downloading its small model from Hugging Face
Hub on a genuine first use on this machine and reading from the local
cache after that — real, not mocked, the same "real, no mocking for local
logic" posture this project's other structural components already take.
"""

import mem0_embedder
from mem0_embedder import cosine_similarity, get_embedder


def test_get_embedder_returns_same_cached_instance():
    assert get_embedder() is get_embedder()


def test_embed_produces_a_real_fixed_length_vector():
    vector = get_embedder().embed("Apple reported quarterly earnings", memory_action="add")
    assert isinstance(vector, list)
    assert len(vector) > 0
    assert all(isinstance(component, float) for component in vector)


def test_related_sentences_score_higher_than_unrelated_ones():
    embedder = get_embedder()
    stock_drop = embedder.embed("AAPL stock price dropped 5 percent after earnings miss", memory_action="add")
    earnings_miss = embedder.embed(
        "Apple shares fell following disappointing quarterly earnings", memory_action="search"
    )
    unrelated = embedder.embed("The weather in Paris is sunny today", memory_action="search")

    related_similarity = cosine_similarity(stock_drop, earnings_miss)
    unrelated_similarity = cosine_similarity(stock_drop, unrelated)

    assert related_similarity > unrelated_similarity
    assert related_similarity > 0.6
    assert unrelated_similarity < 0.6


def test_cosine_similarity_identical_vector_is_one():
    vector = [0.5, 0.5, 0.5, 0.5]
    assert cosine_similarity(vector, vector) == 1.0


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_similarity_zero_vector_returns_zero_not_a_crash():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine_similarity([1.0, 1.0], [0.0, 0.0]) == 0.0


def test_fastembed_model_constant_is_the_small_variant_not_mem0s_default():
    # Deliberately not mem0ai's own default ("thenlper/gte-large") — see
    # this module's docstring for why the smaller model was chosen.
    assert mem0_embedder.FASTEMBED_MODEL == "BAAI/bge-small-en-v1.5"
