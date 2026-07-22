"""FastAPI app for the Teams bot and sync endpoint."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from dotenv import load_dotenv

load_dotenv()  # must run before any os.getenv() reads below

from botbuilder.core import (
    ActivityHandler,
    CardFactory,
    MessageFactory,
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
from quick_prompts import ROOT_ID, build_category_card, build_root_card
from rag import answer_question

# Logging
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


# Bot config
class _BotConfig:
    """Env-backed CloudAdapter config."""

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


# Bot handler
# Shared secret for scheduled sync jobs.
SYNC_SECRET = os.getenv("SYNC_SECRET", "")

# Master switch for the scheduled sync endpoint. When "0" (default), the
# endpoint returns 403 regardless of the secret so that no cron can trigger
# ingestion accidentally. Flip to "1" in the environment to enable.
SCHEDULED_SYNC_ENABLED = os.getenv("SCHEDULED_SYNC_ENABLED", "0") == "1"


class OrgBot(ActivityHandler):
    async def on_message_activity(self, turn_context: TurnContext) -> None:
        activity = turn_context.activity

        # Quick-prompt menu navigation: Action.Submit from the topic/category
        # cards carries no text, just a `quickPromptCategory` value.
        value = activity.value or {}
        category_id = value.get("quickPromptCategory") if isinstance(value, dict) else None
        if category_id is not None:
            card = build_root_card() if category_id == ROOT_ID else build_category_card(category_id)
            await turn_context.send_activity(
                MessageFactory.attachment(CardFactory.adaptive_card(card))
            )
            return

        text = (activity.text or "").strip()
        if not text:
            return

        # Show typing first so Teams feels responsive.
        await turn_context.send_activity(Activity(type=ActivityTypes.typing))

        # Use the stable Teams user id from aad_object_id.
        sender = activity.from_property
        if sender is None:
            logger.warning("Dropping message with no from_property")
            return
        aad_object_id = getattr(sender, "aad_object_id", None) or sender.id
        name = sender.name
        email = None  # populate later if channel data exposes it

        req_id = str(uuid.uuid4())
        extra = {"request_id": req_id}
        logger.info("turn start user=%s len=%d", aad_object_id, len(text), extra=extra)

        employee_id = await upsert_employee(aad_object_id, name, email)
        conversation_id = await get_or_create_conversation(employee_id, channel="teams")

        # Save the conversation reference for later proactive messages.
        ref = TurnContext.get_conversation_reference(activity)
        try:
            ref_dict = ref.serialize() if hasattr(ref, "serialize") else _cr_to_dict(ref)
            await save_conversation_reference(employee_id, ref_dict)
        except Exception as e:
            logger.warning("Failed to persist conversation reference: %s", e, extra=extra)

        # Run the full RAG turn.
        answer = await answer_question(employee_id, conversation_id, text)
        await turn_context.send_activity(answer)
        logger.info("turn done user=%s", aad_object_id, extra=extra)

    async def on_members_added_activity(self, members_added, turn_context: TurnContext) -> None:
        recipient = turn_context.activity.recipient
        recipient_id = recipient.id if recipient else None
        for member in members_added:
            if member.id != recipient_id:
                await turn_context.send_activity(
                    MessageFactory.attachment(CardFactory.adaptive_card(build_root_card()))
                )


def _cr_to_dict(ref: ConversationReference) -> dict[str, Any]:
    """Small fallback serializer for older SDKs."""
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


# FastAPI app
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


# SharePoint sync endpoint
# Gated by SCHEDULED_SYNC_ENABLED so a stray cron cannot trigger ingestion by
# accident. Manual ingestion is always available via `python ingest.py`.

async def _run_sharepoint_sync() -> None:
    """Run the sync off the request thread."""
    try:
        await ingest.sync_from_sharepoint()
    except Exception as e:
        logger.exception("SharePoint sync failed: %s", e)


@app.post("/internal/sharepoint-sync")
async def sharepoint_sync(
    background_tasks: BackgroundTasks,
    x_sync_secret: str | None = Header(default=None, alias="X-Sync-Secret"),
) -> JSONResponse:
    """Trigger SharePoint sync with a shared secret.

    - Requires SCHEDULED_SYNC_ENABLED=1 in the environment.
    - Requires the X-Sync-Secret header to match SYNC_SECRET.
    - Kicks off ingestion in a background task so schedulers get a fast 202.
    """
    if not SCHEDULED_SYNC_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Scheduled sync is disabled (set SCHEDULED_SYNC_ENABLED=1).",
        )
    if not SYNC_SECRET or x_sync_secret != SYNC_SECRET:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad secret")

    background_tasks.add_task(_run_sharepoint_sync)
    return JSONResponse(
        {"accepted": True}, status_code=status.HTTP_202_ACCEPTED
    )
