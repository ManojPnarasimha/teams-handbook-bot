"""Per-employee conversation memory (Azure SQL backend)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import db
from llm_client import LLMError, summarise

logger = logging.getLogger(__name__)

SHORT_TERM_LIMIT = int(os.getenv("SHORT_TERM_LIMIT", "10"))       # last N msgs kept verbatim
SUMMARY_EVERY_TURNS = int(os.getenv("SUMMARY_EVERY_TURNS", "20")) # summarise cadence


# ---- Employee identity -----------------------------------------------------

async def upsert_employee(aad_object_id: str, name: str | None, email: str | None) -> str:
    """Upsert an employee and return their internal id."""
    # MERGE gives us upsert-and-return-id in one round trip.
    sql = """
    MERGE dbo.employees AS target
    USING (SELECT ? AS aad_object_id, ? AS name, ? AS email) AS src
      ON target.aad_object_id = src.aad_object_id
    WHEN MATCHED THEN
      UPDATE SET name = src.name, email = src.email
    WHEN NOT MATCHED THEN
      INSERT (aad_object_id, name, email)
      VALUES (src.aad_object_id, src.name, src.email)
    OUTPUT inserted.id;
    """
    row = await db.fetch_one(sql, (aad_object_id, name, email))
    if not row:
        raise RuntimeError(f"Failed to upsert employee {aad_object_id}")
    return str(row["id"])


# ---- Conversation lifecycle ------------------------------------------------

async def get_or_create_conversation(employee_id: str, channel: str) -> str:
    """Return the latest conversation id, creating one if needed."""
    existing = await db.fetch_one(
        """
        SELECT TOP (1) id
        FROM dbo.conversations
        WHERE employee_id = ? AND channel = ?
        ORDER BY created_at DESC
        """,
        (employee_id, channel),
    )
    if existing:
        return str(existing["id"])
    created = await db.fetch_one(
        """
        INSERT INTO dbo.conversations (employee_id, channel)
        OUTPUT inserted.id
        VALUES (?, ?)
        """,
        (employee_id, channel),
    )
    if not created:
        raise RuntimeError("Failed to create conversation")
    return str(created["id"])


# ---- Message read/write ----------------------------------------------------

async def append_message(conversation_id: str, role: str, content: str) -> None:
    await db.execute(
        "INSERT INTO dbo.messages (conversation_id, role, content) VALUES (?, ?, ?)",
        (conversation_id, role, content),
    )


async def get_recent_history(conversation_id: str, limit: int = SHORT_TERM_LIMIT) -> list[dict]:
    """Return recent messages in chronological order for prompt injection."""
    rows = await db.fetch_all(
        """
        SELECT TOP (?) role, content, created_at
        FROM dbo.messages
        WHERE conversation_id = ?
        ORDER BY created_at DESC
        """,
        (limit, conversation_id),
    )
    rows.reverse()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def count_messages(conversation_id: str) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM dbo.messages WHERE conversation_id = ?",
        (conversation_id,),
    )
    return int(row["n"]) if row else 0


# ---- Long-term summary -----------------------------------------------------

async def get_summary(employee_id: str) -> Optional[str]:
    row = await db.fetch_one(
        "SELECT summary_text FROM dbo.memory_summaries WHERE employee_id = ?",
        (employee_id,),
    )
    return row["summary_text"] if row else None


async def _write_summary(employee_id: str, summary_text: str) -> None:
    sql = """
    MERGE dbo.memory_summaries AS target
    USING (SELECT ? AS employee_id, ? AS summary_text) AS src
      ON target.employee_id = src.employee_id
    WHEN MATCHED THEN
      UPDATE SET summary_text = src.summary_text, updated_at = SYSUTCDATETIME()
    WHEN NOT MATCHED THEN
      INSERT (employee_id, summary_text) VALUES (src.employee_id, src.summary_text);
    """
    await db.execute(sql, (employee_id, summary_text))


def _summarise_sync(existing: str | None, older_msgs: list[dict]) -> str:
    """Compress older turns into a running summary."""
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
        return summarise(messages)
    except LLMError as e:
        logger.warning("Summary generation failed, keeping previous: %s", e)
        return existing or ""


async def maybe_refresh_summary(employee_id: str, conversation_id: str) -> None:
    """Refresh the summary at a fixed message cadence."""
    total = await count_messages(conversation_id)
    if total == 0 or total % SUMMARY_EVERY_TURNS != 0:
        return

    # Fetch the older messages (everything except the SHORT_TERM_LIMIT tail).
    # OFFSET / FETCH gives us pagination on Azure SQL.
    rows = await db.fetch_all(
        """
        SELECT role, content
        FROM dbo.messages
        WHERE conversation_id = ?
        ORDER BY created_at DESC
        OFFSET ? ROWS
        FETCH NEXT ? ROWS ONLY
        """,
        (conversation_id, SHORT_TERM_LIMIT, max(total - SHORT_TERM_LIMIT, 0)),
    )
    if not rows:
        return
    rows.reverse()

    existing = await get_summary(employee_id)
    new_summary = await asyncio.to_thread(_summarise_sync, existing, rows)
    if new_summary:
        await _write_summary(employee_id, new_summary)


# ---- Proactive messaging refs ----------------------------------------------

async def save_conversation_reference(employee_id: str, reference: dict) -> None:
    payload = json.dumps(reference)
    sql = """
    MERGE dbo.conversation_references AS target
    USING (SELECT ? AS employee_id, ? AS reference) AS src
      ON target.employee_id = src.employee_id
    WHEN MATCHED THEN
      UPDATE SET reference = src.reference, updated_at = SYSUTCDATETIME()
    WHEN NOT MATCHED THEN
      INSERT (employee_id, reference) VALUES (src.employee_id, src.reference);
    """
    await db.execute(sql, (employee_id, payload))


async def load_conversation_reference(employee_id: str) -> Optional[dict]:
    row = await db.fetch_one(
        "SELECT reference FROM dbo.conversation_references WHERE employee_id = ?",
        (employee_id,),
    )
    if not row or not row.get("reference"):
        return None
    return json.loads(row["reference"])
