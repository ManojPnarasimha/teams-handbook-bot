"""RAG pipeline: embed query → pgvector similarity search → build prompt → LLM."""

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
SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.72"))


async def _search_documents(embedding: list[float]) -> list[dict]:
    """Call the match_documents RPC (defined in db/schema.sql) for cosine search."""
    sb = await get_supabase()
    res = await sb.rpc(
        "match_documents",
        {
            "query_embedding": embedding,
            "match_count": TOP_K,
            "similarity_threshold": SIMILARITY_THRESHOLD,
        },
    ).execute()
    return res.data or []


async def answer_question(
    employee_id: str, conversation_id: str, question: str
) -> str:
    """Full turn: retrieve → build history (short-term + summary) → LLM → persist."""
    # 1. Persist user turn early so it's captured even if downstream fails.
    await append_message(conversation_id, "user", question)

    # 2. Embed + search. On retrieval failure, treat as no-context.
    try:
        embedding = await embed_text_async(question)
        matches = await _search_documents(embedding)
    except Exception as e:
        logger.exception("Retrieval failed: %s", e)
        matches = []

    context_chunks = [m["content"] for m in matches if m.get("content")]

    # 3. Assemble history: long-term summary as a synthetic system-adjacent turn,
    #    then verbatim short-term messages (excluding the just-appended user msg).
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
    # Drop the trailing user message we just persisted — it's passed separately.
    if recent and recent[-1]["role"] == "user" and recent[-1]["content"] == question:
        recent = recent[:-1]
    history.extend(recent)

    # 4. Generate. No-context short-circuits inside generate_answer.
    import asyncio

    answer = await asyncio.to_thread(generate_answer, context_chunks, history, question)

    # 5. Persist assistant turn and fold older turns into summary if due.
    await append_message(conversation_id, "assistant", answer)
    try:
        await maybe_refresh_summary(employee_id, conversation_id)
    except Exception as e:
        logger.warning("Summary refresh skipped: %s", e)

    return answer


# Re-export for callers that just want the fallback message constant.
__all__ = ["answer_question", "NO_CONTEXT_MESSAGE"]
