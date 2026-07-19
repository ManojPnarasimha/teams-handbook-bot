"""Azure OpenAI embeddings client.

Uses `text-embedding-3-small` with dimensions=768 to match the DB VECTOR(768)
column. Batches requests for speed and stays comfortably inside the free
trial credit for a typical handbook.
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

from openai import AsyncAzureOpenAI

logger = logging.getLogger(__name__)

# Read at import time; the app's dotenv is loaded before any client is created.
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "emb-small")

# Matches VECTOR(768) column in db/schema.sql. text-embedding-3-small supports
# arbitrary output dimensions via the `dimensions` parameter (Matryoshka).
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))

# Chunks per API call. The endpoint accepts up to 2048 inputs; we stay small
# to keep individual requests fast and error-recoverable.
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "64"))


_client: AsyncAzureOpenAI | None = None


def _get_client() -> AsyncAzureOpenAI:
    """Lazy singleton so importing this module doesn't crash without env."""
    global _client
    if _client is not None:
        return _client
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    key = os.getenv("AZURE_OPENAI_API_KEY")
    if not endpoint or not key:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set."
        )
    _client = AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        api_key=key,
        api_version=AZURE_OPENAI_API_VERSION,
    )
    return _client


async def embed(text: str) -> list[float]:
    """Embed a single text (used by the RAG query path)."""
    client = _get_client()
    resp = await client.embeddings.create(
        model=EMBEDDING_DEPLOYMENT,
        input=text,
        dimensions=EMBEDDING_DIM,
    )
    return list(resp.data[0].embedding)


async def embed_many(texts: Sequence[str]) -> list[list[float]]:
    """Embed many texts in batches. Preserves input order."""
    if not texts:
        return []
    client = _get_client()
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = list(texts[i : i + EMBED_BATCH_SIZE])
        resp = await client.embeddings.create(
            model=EMBEDDING_DEPLOYMENT,
            input=batch,
            dimensions=EMBEDDING_DIM,
        )
        # The API guarantees order matches input.
        out.extend(list(d.embedding) for d in resp.data)
        logger.info("Embedded batch %d..%d / %d", i, i + len(batch), len(texts))
    return out
