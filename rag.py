"""RAG pipeline: retrieve from Azure SQL, answer with Azure OpenAI (Groq fallback)."""

from __future__ import annotations

import asyncio
import logging
import os
import re

import db
from embeddings import embed
from llm_client import generate_answer
from memory import (
    append_message,
    get_recent_history,
    get_summary,
    maybe_refresh_summary,
)

logger = logging.getLogger(__name__)

TOP_K = int(os.getenv("RAG_TOP_K", "5"))
SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.55"))


# ---- Intent classification (cheap, keyword-based) --------------------------
#
# We classify every incoming message before the vector search runs. Greetings,
# "what can you do" questions, and thanks/acknowledgements never need document
# context — answering them directly is faster (no embed + no SQL vector scan),
# cheaper (no LLM call), and gives a friendlier UX than a generic "no context"
# fallback when a user just says "hi".
#
# Kept intentionally simple: regex-anchored keyword rules. A cheap classifier
# LLM call could be swapped in here later if we grow past what regex can catch.

_GREETING_RE = re.compile(
    r"^\s*(hi+|hello+|hey+|hola|namaste|yo|howdy|greetings|"
    r"good\s+(morning|afternoon|evening|day))"
    r"(\s+(there|team|bot|everyone|folks|all|guys|dude|tricongpt))?"
    r"[\s,.!?]*$",
    re.IGNORECASE,
)

_THANKS_RE = re.compile(
    r"^\s*(thanks+|thank\s+you|thx|ty|cheers|"
    r"cool|great|awesome|nice|perfect|got\s+it|understood|"
    r"ok(ay)?|okie|k)"
    r"(\s+(so\s+much|very\s+much|a\s+lot|a\s+ton|again|man|mate|bot|tricongpt))?"
    r"[\s,.!?]*$",
    re.IGNORECASE,
)

_META_RE = re.compile(
    r"("
    r"\bwhat\s+can\s+you\s+do\b|"
    r"\bwhat\s+(are|is)\s+you\b|"
    r"\bwho\s+are\s+you\b|"
    r"\bwhat\s+is\s+your\s+(name|purpose)\b|"
    r"\bhow\s+do\s+you\s+work\b|"
    r"\bhow\s+can\s+you\s+help\b|"
    r"\bwhat\s+do\s+you\s+know\b|"
    r"\bintroduce\s+yourself\b|"
    r"^\s*/?(help|about|start|menu)\s*[?!.]*\s*$|"
    r"^\s*help\s+me\s*[?!.]*\s*$"
    r")",
    re.IGNORECASE,
)


_GREETING_REPLY = (
    "Hi! I'm TriconGPT — your internal handbook assistant. "
    "Ask me anything about company policies, benefits, or procedures."
)

_THANKS_REPLY = (
    "You're welcome! Let me know if there's anything else I can help you with."
)


async def _canned_meta_reply() -> str:
    """Reply for 'what can you do / who are you / help' style questions.

    Lists the documents currently ingested so the user immediately sees what
    topics are actually answerable.
    """
    base = (
        "I'm **TriconGPT**, an internal assistant that answers questions from "
        "your company's documents (HR policies, handbooks, procedures, etc.).\n\n"
        "You can ask me things like:\n"
        "• \"What's the laptop policy?\"\n"
        "• \"How many casual leaves do I get?\"\n"
        "• \"What's the maternity leave process?\""
    )
    try:
        rows = await db.fetch_all(
            "SELECT DISTINCT source_path FROM dbo.documents ORDER BY source_path"
        )
    except Exception as e:
        logger.warning("Meta reply: failed to list sources: %s", e)
        rows = []

    sources = [r["source_path"] for r in rows if r.get("source_path")]
    if sources:
        listing = "\n".join(f"• {s}" for s in sources)
        base += f"\n\nI currently have information from:\n{listing}"
    return base


async def _classify_intent(question: str) -> str | None:
    """Return a canned answer for meta/greeting/thanks intents, else None.

    Runs before the vector search — a match here short-circuits the RAG path.
    """
    q = question.strip()
    if not q:
        return None
    if _GREETING_RE.match(q):
        return _GREETING_REPLY
    if _THANKS_RE.match(q):
        return _THANKS_REPLY
    if _META_RE.search(q):
        return await _canned_meta_reply()
    return None


async def _search_documents(embedding: list[float]) -> list[dict]:
    """Top-K cosine similarity search directly in Azure SQL.

    similarity = 1 - VECTOR_DISTANCE('cosine', a, b) — matches the semantics
    the caller expects (higher is better).
    """
    q = db.vector_literal(embedding)
    rows = await db.fetch_all(
        """
        SELECT TOP (?)
            id,
            source_path,
            content,
            1 - VECTOR_DISTANCE('cosine', embedding, CAST(CONVERT(NVARCHAR(MAX), ?) AS VECTOR(768))) AS similarity
        FROM dbo.documents
        WHERE embedding IS NOT NULL
        ORDER BY VECTOR_DISTANCE('cosine', embedding, CAST(CONVERT(NVARCHAR(MAX), ?) AS VECTOR(768))) ASC
        """,
        (TOP_K, q, q),
    )
    return [
        {
            "id": str(r["id"]),
            "source_path": r["source_path"],
            "content": r["content"],
            "similarity": float(r["similarity"]),
        }
        for r in rows
        if r.get("content") and float(r["similarity"]) >= SIMILARITY_THRESHOLD
    ]


async def answer_question(
    employee_id: str, conversation_id: str, question: str
) -> str:
    """Handle one full RAG turn."""
    # Persist the user turn early so it appears in history immediately.
    await append_message(conversation_id, "user", question)

    # Intent gate: greetings / meta / thanks skip the whole RAG pipeline.
    # Saves an embed call + a vector scan + a chat completion, and gives
    # a much friendlier response than a generic "no context" fallback.
    canned = await _classify_intent(question)
    if canned is not None:
        await append_message(conversation_id, "assistant", canned)
        return canned

    # Embed + retrieve; fall back to no-context on failure.
    try:
        q_emb = await embed(question)
        matches = await _search_documents(q_emb)
    except Exception as e:
        logger.exception("Retrieval failed: %s", e)
        matches = []

    context_chunks = [m["content"] for m in matches if m.get("content")]

    # Build history from summary + recent turns.
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
    # Drop the just-persisted user message from the recent tail.
    if recent and recent[-1]["role"] == "user" and recent[-1]["content"] == question:
        recent = recent[:-1]
    history.extend(recent)

    # Generate answer off the event loop (openai sync client blocks).
    answer = await asyncio.to_thread(generate_answer, context_chunks, history, question)

    # Save the assistant turn and refresh the running summary.
    await append_message(conversation_id, "assistant", answer)
    try:
        await maybe_refresh_summary(employee_id, conversation_id)
    except Exception as e:
        logger.warning("Summary refresh skipped: %s", e)

    return answer


__all__ = ["answer_question"]
