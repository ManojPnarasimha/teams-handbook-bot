"""Supabase client and Gemini embedding helper."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from supabase import AsyncClient, acreate_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")  # service_role — server-side only

# Match the 768-dim embedding column.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "768"))

# Gemini task_type hints for retrieval.
TASK_DOCUMENT = "retrieval_document"
TASK_QUERY = "retrieval_query"


_supabase_client: Optional[AsyncClient] = None


async def get_supabase() -> AsyncClient:
    """Lazy async Supabase client."""
    global _supabase_client, SUPABASE_URL, SUPABASE_SERVICE_KEY
    # Refresh env vars if dotenv loaded later.
    if SUPABASE_URL is None:
        SUPABASE_URL = os.getenv("SUPABASE_URL")
    if SUPABASE_SERVICE_KEY is None:
        SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in the environment."
            )
        _supabase_client = await acreate_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase_client


@lru_cache(maxsize=1)
def _configure_gemini():
    """Configure Gemini once and cache it."""
    # Legacy package name; runtime exports exist.
    import google.generativeai as genai  # type: ignore[import-untyped]

    # Read at call time so dotenv can populate env first.
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY must be set for embeddings.")
    genai.configure(api_key=api_key)  # type: ignore[attr-defined]
    return genai


def embed_text(text: str, task_type: str = TASK_DOCUMENT) -> list[float]:
    """Call the embedding API synchronously."""
    genai = _configure_gemini()
    # The legacy SDK needs the models/ prefix.
    model_name = EMBEDDING_MODEL if EMBEDDING_MODEL.startswith("models/") else f"models/{EMBEDDING_MODEL}"
    # The SDK returns an embedding payload.
    resp = genai.embed_content(  # type: ignore[attr-defined]
        model=model_name,
        content=text,
        task_type=task_type,
        output_dimensionality=EMBEDDING_DIM,
    )
    return list(resp["embedding"])


async def embed_text_async(text: str, task_type: str = TASK_QUERY) -> list[float]:
    """Run the blocking SDK call off the event loop."""
    import asyncio

    return await asyncio.to_thread(embed_text, text, task_type)
