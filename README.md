# Employee Handbook Assistant (teams-handbook-bot)

A Retrieval-Augmented Generation (RAG) chatbot, deployed as a Microsoft Teams
bot, that answers employee questions directly from the company handbook —
with page-level citations and no hallucinated policy.

## Problem

Employees routinely ask HR the same recurring questions — leave balance,
notice period, benefits, work hours — that are already answered in the
handbook PDF, but few people read a 40+ page document to find one paragraph.
This creates repetitive interruptions for HR and slow, inconsistent answers
for employees.

This bot puts the handbook directly inside the tool employees already use
every day — Microsoft Teams — and answers questions in seconds, grounded
strictly in the source document, with the page number cited so the answer
is verifiable.

## What it does

- Answers natural-language questions about company policy inside a Teams chat.
- Retrieves only the relevant passages from the handbook before answering —
  it does not answer from general knowledge, and says so explicitly when the
  handbook doesn't cover a question.
- Cites the page number(s) used, so answers are auditable against the source PDF.
- Retains short conversation history per user, so follow-up questions
  ("what about part-time employees?") work without repeating context.

## How it works (current implementation)

1. **Ingestion (offline, one-time):** the handbook PDF is parsed, split into
   overlapping text chunks, and embedded locally using a sentence-transformer
   model. The resulting vectors are stored in a local index file.
2. **Retrieval (per question):** the incoming question is embedded and
   compared against the index via cosine similarity to pull the top matching
   passages.
3. **Generation:** the question, retrieved passages, and recent conversation
   history are sent to an LLM, constrained by a system prompt to answer only
   from the provided excerpts.
4. **Delivery:** the answer is returned through the Bot Framework adapter to
   the Teams conversation.

*(Architecture diagram to be added separately.)*

## Tech stack

| Layer | Technology |
|---|---|
| Bot framework | Microsoft Bot Framework SDK (Python) + aiohttp server |
| Identity / Auth | Microsoft Entra ID App Registration (Single Tenant) via Azure Bot Service |
| Embeddings | `sentence-transformers` (`all-MiniLM-L6-v2`), run locally |
| Vector store | numpy index persisted to disk (`data/index.pkl`) |
| LLM | Groq-hosted Llama 3.3 70B (pluggable — Gemini also supported) |
| Dev tunneling | Cloudflare Tunnel (local dev only) |
| Client surface | Microsoft Teams (personal, team, and group chat scopes) |

## What's set up so far

- Azure Bot resource registered (Free tier) with a Single-Tenant Entra ID
  app registration backing it.
- Bot server (`app.py`) implemented with the Bot Framework `CloudAdapter`,
  exposing `POST /api/messages` and `GET /health`.
- RAG pipeline (`rag.py`, `ingest.py`) implemented and validated end-to-end
  against the employee handbook PDF via the terminal client (`chat_local.py`).
- Local server exposed to the internet during development via a Cloudflare
  Tunnel, wired to the Azure Bot's messaging endpoint.
- Verified working via Azure's **Test in Web Chat** and directly inside
  Microsoft Teams.

## Project structure

| Path | Purpose |
|---|---|
| `app.py` | Bot Framework server — Teams entry point (`/api/messages`) |
| `rag.py` | Core RAG engine — retrieval + LLM call |
| `ingest.py` | One-time/PDF-change pipeline: extract → chunk → embed → save index |
| `chat_local.py` | Terminal chat client for testing the RAG pipeline without Teams/Azure |
| `document/` | Source PDF(s) to be indexed |
| `data/` | Generated vector index (gitignored, regenerated via `ingest.py`) |
| `appPackage/` | Teams app manifest + icons, for sideloading into Teams |
| `SETUP.md` | Detailed Azure Bot registration + tunnel walkthrough |

## Prerequisites

- Python 3.10+
- A Groq API key ([console.groq.com](https://console.groq.com))
- An Azure Bot resource (Free F0 tier is sufficient) with its App ID, client
  secret, and tenant ID
- A Microsoft 365 tenant where custom Teams apps can be sideloaded (see
  [SETUP.md](SETUP.md) if you don't have one)

## Running it locally

1. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in:
   - `GROQ_API_KEY`
   - `MicrosoftAppId`, `MicrosoftAppPassword`, `MicrosoftAppType`, `MicrosoftAppTenantId`

   ### pip install --upgrade certifi

   ### export SSL_CERT_FILE=$(python3 -c "import certifi; print(certifi.where())")

3. Build the vector index from your PDF (re-run whenever the document changes):
   ```powershell
   python ingest.py document\employee_handbook.pdf
   ```
4. Sanity-check the RAG pipeline in isolation, no Teams/Azure needed:
   ```powershell
   python chat_local.py
   ```
5. Start the bot server:
   ```powershell
   python app.py
   ```
   You should see `Bot running on http://localhost:3978/api/messages`.

## Testing against Azure Bot Service

1. **Expose your local server** with a dev tunnel (e.g. Cloudflare Tunnel or
   Microsoft `devtunnel`) and copy the HTTPS URL it gives you.
2. In the Azure Portal, open your Bot resource → **Configuration** → set
   **Messaging endpoint** to:
   ```
   https://<your-tunnel-url>/api/messages
   ```
3. With `app.py` and the tunnel both running, go to the Bot resource →
   **Test in Web Chat** and ask a handbook question — this validates the
   full path (Azure auth → your server → RAG → LLM → response) without
   needing Teams installed.
4. To test inside Teams itself: update `appPackage/manifest.json` with your
   real App ID, zip the contents of `appPackage/`, and sideload it via
   **Teams → Apps → Manage your apps → Upload a custom app**. Full walkthrough
   in [SETUP.md](SETUP.md).

Note: free dev tunnels issue a new URL on every restart — update the
Messaging endpoint each time the tunnel restarts, or the bot will stop
responding in Teams/Web Chat with no visible client-side error.

## Future enhancements

- Move off local machine + dev tunnel onto persistent hosting (Azure App
  Service, Render, Railway, or Fly.io) for 24/7 availability.
- Replace the flat-file numpy index with a proper vector database (e.g.
  FAISS, Chroma, or Azure AI Search) as document volume grows.
- Support multiple source documents / departments (not just one handbook).
- Add an ingestion/admin UI so non-engineers can update source documents
  without running a CLI script.
- Capture answer feedback (thumbs up/down) in Teams to identify weak spots
  in retrieval or gaps in the handbook itself.
- Add structured logging/analytics on question topics to surface FAQs HR
  should proactively document.
- Richer Teams responses via Adaptive Cards (e.g. highlighted citations,
  quick-reply follow-ups) instead of plain text.
- Role- or group-based access to different document sets.
- Automated tests and CI for the retrieval and ingestion pipeline.

## Troubleshooting

- **401 Unauthorized** — App ID / secret / tenant ID mismatch, or
  `MicrosoftAppType` incorrect (new bots default to `SingleTenant`).
- **Bot replies in Web Chat but not in Teams** — Teams channel not added on
  the Bot resource, or the tunnel URL changed since the endpoint was last set.
- **429 from Groq** — free-tier rate limit; wait and retry.
- **Slow first response** — the embedding model loads lazily on first query;
  subsequent responses are fast.

See [SETUP.md](SETUP.md) for the full Azure Bot registration and tunnel setup guide.
