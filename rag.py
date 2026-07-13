"""RAG pipeline for retrieval and answer generation."""

from __future__ import annotations

import logging
import os

from db_client import embed_text_async, get_supabase
from llm_client import NO_CONTEXT_MESSAGE, generate_answer
from memory import (
    append_message,
    get_recent_history,
    get_summary,
    maybe_refresh_summary,
)

logger = logging.getLogger(__name__)

TOP_K = int(os.getenv("RAG_TOP_K", "5"))
SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.55"))


async def _search_documents(embedding: list[float]) -> list[dict]:
    """Search documents by cosine similarity computed in Python.

    This avoids IVFFlat index recall issues with small datasets.
    """
    import json
    import numpy as np

    sb = await get_supabase()
    res = await sb.table("documents").select("id,source_path,content,embedding").not_.is_("embedding", "null").execute()
    data = res.data
    if not isinstance(data, list) or not data:
        return []

    query_vec = np.array(embedding, dtype=np.float32)
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return []

    results = []
    for row in data:
        if not isinstance(row, dict) or not row.get("embedding") or not row.get("content"):
            continue
        raw_emb = row["embedding"]
        doc_vec = np.array(json.loads(raw_emb) if isinstance(raw_emb, str) else raw_emb, dtype=np.float32)
        doc_norm = np.linalg.norm(doc_vec)
        if doc_norm == 0:
            continue
        similarity = float(np.dot(query_vec, doc_vec) / (query_norm * doc_norm))
        if similarity >= SIMILARITY_THRESHOLD:
            results.append({
                "id": row["id"],
                "source_path": row["source_path"],
                "content": row["content"],
                "similarity": similarity,
            })

    results.sort(key=lambda r: r["similarity"], reverse=True)
    return results[:TOP_K]


async def answer_question(
    employee_id: str, conversation_id: str, question: str
) -> str:
    """Handle one full RAG turn."""
    # Persist the user turn early.
    await append_message(conversation_id, "user", question)

    # Embed and search; fall back to no-context on failure.
    try:
        embedding = await embed_text_async(question)
        matches = await _search_documents(embedding)
    except Exception as e:
        logger.exception("Retrieval failed: %s", e)
        matches = []

    context_chunks = [m["content"] for m in matches if m.get("content")]

    # Build history from the summary and recent messages.
    summary = await get_summary(employee_id)
    history: list[dict] = []
    if summary:
        history.append(
            {
                "role": "user",
                "content": f"[Prior conversation summary for context]\n{summary}",
            }
        )

    recent = await get_recent_history(conversation_id)
    # Drop the just-persisted user message from history.
    if recent and recent[-1]["role"] == "user" and recent[-1]["content"] == question:
        recent = recent[:-1]
    history.extend(recent)

    # Generate an answer.
    import asyncio

    answer = await asyncio.to_thread(generate_answer, context_chunks, history, question)

    # Save the assistant turn and refresh memory if needed.
    await append_message(conversation_id, "assistant", answer)
    try:
        await maybe_refresh_summary(employee_id, conversation_id)
    except Exception as e:
        logger.warning("Summary refresh skipped: %s", e)

    return answer


# Re-export for callers that just want the fallback message constant.
__all__ = ["answer_question", "NO_CONTEXT_MESSAGE"]
