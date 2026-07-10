"""FastAPI app hosting:
- POST /api/messages         → Bot Framework channel endpoint (Teams).
- POST /internal/storage-webhook → Supabase Database Webhook receiver.
- GET  /healthz              → liveness probe for Render.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from botbuilder.core import (
    ActivityHandler,
    TurnContext,
)
from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication
from botbuilder.schema import Activity, ActivityTypes, ConversationReference
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

import ingest
from memory import (
    get_or_create_conversation,
    save_conversation_reference,
    upsert_employee,
)
from rag import answer_question

# ---- Logging -----------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
)


class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


for h in logging.getLogger().handlers:
    h.addFilter(_RequestIdFilter())

logger = logging.getLogger("app")


# ---- Bot Framework config ----------------------------------------------------
class _BotConfig:
    """CloudAdapter config keys read from env — never hardcoded."""

    APP_ID = os.getenv("MICROSOFT_APP_ID", "")
    APP_PASSWORD = os.getenv("MICROSOFT_APP_PASSWORD", "")
    APP_TYPE = os.getenv("MICROSOFT_APP_TYPE", "MultiTenant")
    APP_TENANTID = os.getenv("MICROSOFT_APP_TENANT_ID", "")


_bot_config = _BotConfig()
_adapter = CloudAdapter(ConfigurationBotFrameworkAuthentication(_bot_config))


async def _on_error(context: TurnContext, error: Exception) -> None:
    logger.exception("Unhandled turn error: %s", error)
    await context.send_activity(
        "Sorry, something went wrong on my side. Please try again in a moment."
    )


_adapter.on_turn_error = _on_error


# ---- Bot handler -------------------------------------------------------------
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


class OrgBot(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext) -> None:
        activity = turn_context.activity
        text = (activity.text or "").strip()
        if not text:
            return

        # 1. Immediate typing indicator — sent before the LLM call so Teams shows "..."
        await turn_context.send_activity(Activity(type=ActivityTypes.typing))

        # 2. Identity: aad_object_id is the stable Teams user id. Never invent our own.
        aad_object_id = getattr(activity.from_property, "aad_object_id", None) or activity.from_property.id
        name = activity.from_property.name
        email = None  # populate later if channel data exposes it

        req_id = str(uuid.uuid4())
        extra = {"request_id": req_id}
        logger.info("turn start user=%s len=%d", aad_object_id, len(text), extra=extra)

        employee_id = await upsert_employee(aad_object_id, name, email)
        conversation_id = await get_or_create_conversation(employee_id, channel="teams")

        # 3. Persist ConversationReference for future proactive messages.
        ref = TurnContext.get_conversation_reference(activity)
        try:
            ref_dict = ref.serialize() if hasattr(ref, "serialize") else _cr_to_dict(ref)
            await save_conversation_reference(employee_id, ref_dict)
        except Exception as e:
            logger.warning("Failed to persist conversation reference: %s", e, extra=extra)

        # 4. Run the full RAG turn.
        answer = await answer_question(employee_id, conversation_id, text)
        await turn_context.send_activity(answer)
        logger.info("turn done user=%s", aad_object_id, extra=extra)

    async def on_members_added_activity(self, members_added, turn_context: TurnContext) -> None:
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(
                    "Hi! I'm TriconGPT — ask me anything about company policies or handbook content."
                )


def _cr_to_dict(ref: ConversationReference) -> dict[str, Any]:
    """Minimal fallback serializer for older SDK versions."""
    return {
        "activityId": ref.activity_id,
        "user": {"id": ref.user.id, "name": ref.user.name} if ref.user else None,
        "bot": {"id": ref.bot.id, "name": ref.bot.name} if ref.bot else None,
        "conversation": {
            "id": ref.conversation.id,
            "conversationType": ref.conversation.conversation_type,
            "tenantId": ref.conversation.tenant_id,
        }
        if ref.conversation
        else None,
        "channelId": ref.channel_id,
        "serviceUrl": ref.service_url,
    }


_bot = OrgBot()


# ---- FastAPI app -------------------------------------------------------------
app = FastAPI(title="TriconGPT", version="1.0.0")


@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.post("/api/messages")
async def messages(request: Request) -> JSONResponse:
    """Bot Framework channel endpoint. Delegates to CloudAdapter."""
    body = await request.json()
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")
    try:
        response = await _adapter.process_activity(auth_header, activity, _bot.on_turn)
        if response:
            return JSONResponse(content=response.body, status_code=response.status)
        return JSONResponse(content={}, status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.exception("Adapter failed: %s", e)
        raise HTTPException(status_code=500, detail="Adapter error") from e


# ---- Supabase Database Webhook receiver --------------------------------------

def _extract_bucket_and_path(record: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not record:
        return None, None
    return record.get("bucket_id"), record.get("name")


async def _handle_storage_event(event_type: str, path: str) -> None:
    """Runs as a background task so the webhook returns fast."""
    try:
        if event_type in ("INSERT", "UPDATE"):
            await ingest.process_file(path)
        elif event_type == "DELETE":
            await ingest.delete_file_chunks(path)
        else:
            logger.warning("Ignoring unknown webhook event type: %s", event_type)
    except Exception as e:
        logger.exception("Storage event handler failed for %s %s: %s", event_type, path, e)


@app.post("/internal/storage-webhook")
async def storage_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> JSONResponse:
    """Supabase Database Webhook on storage.objects. Free event-driven ingestion."""
    if not WEBHOOK_SECRET or x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad secret")

    payload = await request.json()
    event_type = (payload.get("type") or "").upper()

    # Supabase sends `record` on INSERT/UPDATE and `old_record` on DELETE.
    record = payload.get("record") if event_type != "DELETE" else payload.get("old_record")
    bucket_id, path = _extract_bucket_and_path(record)

    expected_bucket = os.getenv("STORAGE_BUCKET", "org-docs")
    if bucket_id and bucket_id != expected_bucket:
        return JSONResponse({"skipped": "wrong bucket"}, status_code=200)

    if not path:
        return JSONResponse({"skipped": "no path"}, status_code=200)

    # Offload the heavy work; return 200 immediately so Supabase doesn't hold the socket.
    background_tasks.add_task(_handle_storage_event, event_type, path)
    return JSONResponse({"accepted": True, "type": event_type, "path": path})
