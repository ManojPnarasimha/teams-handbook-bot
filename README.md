# teams-handbook-bot

A free, local-first RAG (Retrieval-Augmented Generation) chatbot that answers
questions about a PDF document (e.g. an employee handbook). Runs standalone
in your terminal, or as a Microsoft Teams bot.

- **Embeddings**: sentence-transformers (`all-MiniLM-L6-v2`), runs locally — your PDF never leaves your machine.
- **Vector store**: a numpy index saved to disk (`data/index.pkl`), no database needed.
- **LLM**: [Groq](https://console.groq.com) free tier (Llama 3.3 70B) by default. Gemini is supported but not recommended (see [SETUP.md](SETUP.md)).

## Quick start (local terminal chat)

1. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and set your `GROQ_API_KEY` (free key: https://console.groq.com).
3. Put your PDF in the `document/` folder, then build the index (re-run whenever the PDF changes):
   ```powershell
   python ingest.py document\employee_handbook.pdf
   ```
4. Chat:
   ```powershell
   python chat_local.py
   ```

## Project layout

| File | Purpose |
|---|---|
| `ingest.py` | Extracts text from a PDF, chunks it, embeds it, saves `data/index.pkl` |
| `rag.py` | Core RAG engine — retrieval + LLM call (Groq/Gemini) |
| `chat_local.py` | Terminal chat client for testing, no Teams/Azure needed |
| `app.py` | Bot Framework server for running this as a Microsoft Teams bot |
| `document/` | Put your source PDF(s) here |
| `data/` | Generated vector index (gitignored) |

## Running it as a Teams bot

See [SETUP.md](SETUP.md) for the full walkthrough: Azure Bot registration,
exposing your local server with a dev tunnel, and installing the app package
into Teams.

## Troubleshooting

See the **Troubleshooting** section in [SETUP.md](SETUP.md).
