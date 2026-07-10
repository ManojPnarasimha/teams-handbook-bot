"""Per-employee conversation memory:
- Short-term: last N messages from the active conversation.
- Long-term: rolling summary in memory_summaries, injected into every prompt.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from db_client import get_supabase
from llm_client import resolve_provider, _call_groq, _call_gemini, LLMError

logger = logging.getLogger(__name__)

SHORT_TERM_LIMIT = int(os.getenv("SHORT_TERM_LIMIT", "10"))  # last N msgs kept verbatim
SUMMARY_EVERY_TURNS = int(os.getenv("SUMMARY_EVERY_TURNS", "20"))  # summarise cadence


# ---- Employee identity -------------------------------------------------------

async def upsert_employee(aad_object_id: str, name: str | None, email: str | None) -> str:
    """Upsert by aad_object_id; return internal employees.id."""
    sb = await get_supabase()
    res = (
        await sb.table("employees")
        .upsert(
            {"aad_object_id": aad_object_id, "name": name, "email": email},
            on_conflict="aad_object_id",
        )
        .execute()
    )
    if res.data:
        return res.data[0]["id"]
    # Fallback: fetch after upsert if the client returns no data.
    got = (
        await sb.table("employees")
        .select("id")
        .eq("aad_object_id", aad_object_id)
        .single()
        .execute()
    )
    return got.data["id"]


# ---- Conversation lifecycle --------------------------------------------------

async def get_or_create_conversation(employee_id: str, channel: str) -> str:
    """Return the most recent conversation for this employee+channel, or create one."""
    sb = await get_supabase()
    existing = (
        await sb.table("conversations")
        .select("id")
        .eq("employee_id", employee_id)
        .eq("channel", channel)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]["id"]
    created = (
        await sb.table("conversations")
        .insert({"employee_id": employee_id, "channel": channel})
        .execute()
    )
    return created.data[0]["id"]


# ---- Message read/write ------------------------------------------------------

async def append_message(conversation_id: str, role: str, content: str) -> None:
    sb = await get_supabase()
    await sb.table("messages").insert(
        {"conversation_id": conversation_id, "role": role, "content": content}
    ).execute()


async def get_recent_history(conversation_id: str, limit: int = SHORT_TERM_LIMIT) -> list[dict]:
    """Return recent messages oldest→newest for prompt injection."""
    sb = await get_supabase()
    res = (
        await sb.table("messages")
        .select("role,content,created_at")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = list(reversed(res.data or []))
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def count_messages(conversation_id: str) -> int:
    sb = await get_supabase()
    res = (
        await sb.table("messages")
        .select("id", count="exact")
        .eq("conversation_id", conversation_id)
        .execute()
    )
    return res.count or 0


# ---- Long-term summary -------------------------------------------------------

async def get_summary(employee_id: str) -> Optional[str]:
    sb = await get_supabase()
    res = (
        await sb.table("memory_summaries")
        .select("summary_text")
        .eq("employee_id", employee_id)
        .maybe_single()
        .execute()
    )
    if res and res.data:
        return res.data.get("summary_text")
    return None


async def _write_summary(employee_id: str, summary_text: str) -> None:
    sb = await get_supabase()
    await sb.table("memory_summaries").upsert(
        {"employee_id": employee_id, "summary_text": summary_text},
        on_conflict="employee_id",
    ).execute()


def _summarise_sync(existing: str | None, older_msgs: list[dict]) -> str:
    """Use the same LLM provider to compress older turns into a running summary."""
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in older_msgs)
    prompt = (
        "You maintain a running summary of an employee's chat with an internal assistant. "
        "Merge the EXISTING SUMMARY with the NEW MESSAGES into a concise summary "
        "(<= 200 words) capturing durable facts and open questions. No preamble.\n\n"
        f"EXISTING SUMMARY:\n{existing or '(none)'}\n\n"
        f"NEW MESSAGES:\n{convo}"
    )
    messages = [
        {"role": "system", "content": "You are a concise summariser."},
        {"role": "user", "content": prompt},
    ]
    try:
        provider = resolve_provider()
        return _call_groq(messages) if provider == "groq" else _call_gemini(messages)
    except LLMError as e:
        logger.warning("Summary generation failed, keeping previous summary: %s", e)
        return existing or ""


async def maybe_refresh_summary(employee_id: str, conversation_id: str) -> None:
    """Every SUMMARY_EVERY_TURNS messages, fold older turns into memory_summaries.

    Cheap heuristic: if total messages is a multiple of the cadence, summarise
    everything older than the short-term window.
    """
    import asyncio

    total = await count_messages(conversation_id)
    if total == 0 or total % SUMMARY_EVERY_TURNS != 0:
        return

    sb = await get_supabase()
    # Fetch all but the most recent SHORT_TERM_LIMIT messages for summarisation.
    res = (
        await sb.table("messages")
        .select("role,content,created_at")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=True)
        .range(SHORT_TERM_LIMIT, total)
        .execute()
    )
    older = list(reversed(res.data or []))
    if not older:
        return

    existing = await get_summary(employee_id)
    new_summary = await asyncio.to_thread(_summarise_sync, existing, older)
    if new_summary:
        await _write_summary(employee_id, new_summary)


# ---- Proactive messaging refs ------------------------------------------------

async def save_conversation_reference(employee_id: str, reference: dict) -> None:
    sb = await get_supabase()
    await sb.table("conversation_references").upsert(
        {"employee_id": employee_id, "reference": reference},
        on_conflict="employee_id",
    ).execute()


async def load_conversation_reference(employee_id: str) -> Optional[dict]:
    sb = await get_supabase()
    res = (
        await sb.table("conversation_references")
        .select("reference")
        .eq("employee_id", employee_id)
        .maybe_single()
        .execute()
    )
    if res and res.data:
        return res.data.get("reference")
    return None
