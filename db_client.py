"""Supabase client + embedding helper. Async, no blocking calls in request path."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from supabase import AsyncClient, acreate_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")  # service_role — server-side only

# 1536-dim to match documents.embedding vector(1536) in schema.sql.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "org-docs")


_supabase_client: Optional[AsyncClient] = None


async def get_supabase() -> AsyncClient:
    """Lazy singleton async Supabase client."""
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in the environment."
            )
        _supabase_client = await acreate_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase_client


@lru_cache(maxsize=1)
def _openai_client():
    from openai import OpenAI

    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY must be set for embeddings.")
    return OpenAI(api_key=OPENAI_API_KEY)


def embed_text(text: str) -> list[float]:
    """Synchronous embedding call. Runs in a threadpool from async callers."""
    client = _openai_client()
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


async def embed_text_async(text: str) -> list[float]:
    """Async wrapper — offload the blocking SDK call to a threadpool."""
    import asyncio

    return await asyncio.to_thread(embed_text, text)
