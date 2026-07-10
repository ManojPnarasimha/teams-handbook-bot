"""Local CLI chat: bypasses Bot Framework entirely.

Uses the same RAG + memory pipeline against a stable fake employee. Ideal for
iterating on retrieval quality without needing Azure/Teams plumbing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from memory import get_or_create_conversation, upsert_employee
from rag import answer_question

LOCAL_AAD_ID = os.getenv("LOCAL_AAD_ID", "local-dev-user")
LOCAL_NAME = os.getenv("LOCAL_NAME", "Local Dev")
LOCAL_EMAIL = os.getenv("LOCAL_EMAIL", "local@example.com")


async def _run() -> None:
    employee_id = await upsert_employee(LOCAL_AAD_ID, LOCAL_NAME, LOCAL_EMAIL)
    conversation_id = await get_or_create_conversation(employee_id, channel="local")

    print(f"TriconGPT (local). employee_id={employee_id}")
    print("Type your question. Ctrl+C or 'exit' to quit.\n")

    while True:
        try:
            q = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not q:
            continue
        if q.lower() in {"exit", "quit"}:
            return
        answer = await answer_question(employee_id, conversation_id, q)
        print(f"bot > {answer}\n")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
