"""
Chat with your document in the terminal — no Teams, no Azure needed.
Great for testing the RAG pipeline before wiring up Teams.

Usage:
    set GROQ_API_KEY=gsk_...        (Windows)   or   export GROQ_API_KEY=gsk_...
    python chat_local.py
"""
import asyncio
from rag import generate_answer


async def main():
    print("Handbook chat — type 'quit' to exit\n")
    history = []
    while True:
        try:
            q = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in ("quit", "exit"):
            break
        answer = await generate_answer(q, history)
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": answer})
        history = history[-10:]
        print(f"\nBot: {answer}\n")


if __name__ == "__main__":
    asyncio.run(main())
