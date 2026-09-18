"""FastEmbed wrapper -- BAAI/bge-small-en-v1.5, 384 dims (PRD §5, §7).

Loaded once (module-level singleton, lazily on first use) from the model
baked into the backend image at build time (`backend/Dockerfile`'s `models`
stage, `FASTEMBED_CACHE_PATH=/opt/models`). Never downloads at request time
-- no key, no rate limit, no network call, ever.

A4 (RAG Query) imports `embed_query` for turning a user's question into a
vector for the pgvector cosine search. Keep this surface small and stable:
`embed_texts` / `embed_query` only.

FIX-3 note (query-alignment enrichment): `ingest.py` calls `embed_texts`
with each `Chunk.embed_text` (heading + salient keywords ahead of the
clause -- see `chunk.py`'s `_build_embed_text`), not `Chunk.text` (the
verbatim document content stored/displayed for citations). That's a
*passage-side* enrichment only -- both the enriched passage text and the
raw query still go through this module's existing asymmetric split
(`passage_embed` here, `query_embed` for the user's question); nothing
about that asymmetry changes.
"""

from __future__ import annotations

import os
import threading

from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

_model: TextEmbedding | None = None
_lock = threading.Lock()


def _get_model() -> TextEmbedding:
    """Lazily construct the (thread-safe, double-checked) singleton model.
    Reused across every call -- re-instantiating per request is what makes
    ingest crawl."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                cache_dir = os.environ.get("FASTEMBED_CACHE_PATH", "/opt/models")
                _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=cache_dir)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed passages (policy chunks, resume sections, ...) for
    storage. Uses FastEmbed's `passage_embed`, the document-side counterpart
    to `embed_query`'s query-side instruction prefix -- BGE is an asymmetric
    embedding model, so passages and queries are embedded differently on
    purpose. Returns one 384-dim vector per input, same order. Empty input
    returns `[]` without touching the model."""
    if not texts:
        return []
    model = _get_model()
    return [vector.tolist() for vector in model.passage_embed(list(texts))]


def embed_query(text: str) -> list[float]:
    """Embed a single search query with BGE's query-side instruction prefix
    (applied internally by `query_embed`) for asymmetric retrieval quality."""
    model = _get_model()
    return next(iter(model.query_embed([text]))).tolist()
