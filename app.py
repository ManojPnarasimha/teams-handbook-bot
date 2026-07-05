"""
Microsoft Teams RAG bot server (Bot Framework SDK + aiohttp).

Free stack:
  - Runs on your own machine (or any free host)
  - Azure Bot registration: Free F0 tier
  - Exposed to the internet with a free dev tunnel (see SETUP.md)

Env vars:
  MicrosoftAppId        - from your Azure Bot registration
  MicrosoftAppPassword  - client secret
  MicrosoftAppType      - "SingleTenant" (default for new bots) or "MultiTenant"
  MicrosoftAppTenantId  - your tenant ID (required for SingleTenant)
  GROQ_API_KEY          - free key from https://console.groq.com
"""

import os
import sys
import traceback
from datetime import datetime

from aiohttp import web
from botbuilder.core import (
    ActivityHandler,
    TurnContext,
    ConversationState,
    MemoryStorage,
)
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.schema import Activity, ActivityTypes

from rag import generate_answer


class Config:
    """Read config from environment (Bot Framework auth reads these names)."""
    PORT = int(os.environ.get("PORT", 3978))
    APP_ID = os.environ.get("MicrosoftAppId", "")
    APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")
    APP_TYPE = os.environ.get("MicrosoftAppType", "SingleTenant")
    APP_TENANTID = os.environ.get("MicrosoftAppTenantId", "")


class HandbookBot(ActivityHandler):
    def __init__(self, conversation_state: ConversationState):
        self.conversation_state = conversation_state
        self.history_accessor = conversation_state.create_property("history")

    async def on_message_activity(self, turn_context: TurnContext):
        question = (turn_context.activity.text or "").strip()
        # Strip the @mention if used in a channel
        question = TurnContext.remove_recipient_mention(turn_context.activity) or question
        question = question.strip()

        if not question:
            await turn_context.send_activity("Ask me anything about the handbook!")
            return

        # typing indicator while we think
        await turn_context.send_activity(Activity(type=ActivityTypes.typing))

        history = await self.history_accessor.get(turn_context, lambda: [])
        try:
            answer = await generate_answer(question, history)
        except FileNotFoundError as e:
            answer = str(e)
        except Exception as e:
            answer = f"Sorry, something went wrong: {e}"

        # remember the exchange for follow-up questions
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        await self.history_accessor.set(turn_context, history[-10:])
        await self.conversation_state.save_changes(turn_context)

        await turn_context.send_activity(answer)

    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(
                    "Hi! I'm your document assistant. Ask me anything about the "
                    "employee handbook — leave policy, benefits, notice period, "
                    "work hours, anything."
                )


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------
config = Config()
auth = ConfigurationBotFrameworkAuthentication(config)
adapter = CloudAdapter(auth)


async def on_error(context: TurnContext, error: Exception):
    print(f"\n[on_turn_error] {error}", file=sys.stderr)
    traceback.print_exc()
    await context.send_activity("The bot hit an error. Check the server logs.")

adapter.on_turn_error = on_error

memory = MemoryStorage()
conversation_state = ConversationState(memory)
bot = HandbookBot(conversation_state)


async def messages(req: web.Request) -> web.Response:
    return await adapter.process(req, bot)


async def health(req: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "time": datetime.utcnow().isoformat()})


app = web.Application(middlewares=[aiohttp_error_middleware])
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)

if __name__ == "__main__":
    print(f"Bot running on http://localhost:{config.PORT}/api/messages")
    web.run_app(app, host="0.0.0.0", port=config.PORT)
