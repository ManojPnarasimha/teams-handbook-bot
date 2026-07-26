# TriconGPT — Internal Org Chatbot

Retrieval-Augmented Generation (RAG) chatbot that answers employees' HR and
company-policy questions inside Microsoft Teams, grounded only in approved
company documents. When no relevant document is found, it replies with a fixed
"contact HR" message instead of guessing.

**Stack**

- **Chat surface**: Microsoft Teams (Azure Bot Service)
- **Web service**: FastAPI on Render
- **DB + vectors**: Azure SQL Database (native `VECTOR(768)`)
- **Documents**: Microsoft SharePoint (Microsoft Graph API)
- **LLM**: Azure OpenAI `gpt-4o-mini` (primary), Groq `llama-3.3-70b-versatile` (fallback)
- **Embeddings**: Azure OpenAI `text-embedding-3-small` @ 768 dims
- **Doc extraction**: PyMuPDF (primary), Azure Document Intelligence OCR (fallback)

---

## Architecture

```
Teams ─► Azure Bot Service ─► Render (FastAPI: /api/messages)
                                    │
                                    ├─► rag.py ─► Azure SQL (VECTOR_DISTANCE)
                                    ├─► memory.py (history + rolling summary)
                                    └─► llm_client.py (Azure OpenAI → Groq fallback)

Scheduler (optional) ─► POST /internal/sharepoint-sync ─► ingest.sync_from_sharepoint()
```

## Files

| Path | Purpose |
| --- | --- |
| [app.py](app.py) | FastAPI endpoints: `/api/messages`, `/internal/sharepoint-sync`, `/healthz` |
| [rag.py](rag.py) | Intent gate → embed → vector search → prompt → LLM |
| [llm_client.py](llm_client.py) | Azure OpenAI primary, Groq fallback, grounding prompt |
| [memory.py](memory.py) | Per-employee history, rolling summary, proactive refs |
| [sharepoint_client.py](sharepoint_client.py) | MSAL auth + Graph file listing / download |
| [ingest.py](ingest.py) | Extract → chunk → embed → upsert (CLI) |
| [document_extractor.py](document_extractor.py) | PyMuPDF + Azure DI OCR fallback |
| [embeddings.py](embeddings.py) | Azure OpenAI embeddings client |
| [db.py](db.py) | Async Azure SQL client + `VECTOR` helpers |
| [db/schema.sql](db/schema.sql) | Tables and indexes |
| [render.yaml](render.yaml) | Render Blueprint (build + start command) |
| [COMMANDS.md](COMMANDS.md) | Full runbook: setup, ingest, deploy, troubleshoot |

---

## Quick start

Requires Python 3.12 and the Microsoft ODBC Driver 18 for SQL Server
(macOS: `brew install msodbcsql18`; Render installs it automatically).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                  # then fill in the variables below

python ingest.py --backfill           # index SharePoint documents
uvicorn app:app --reload --port 8000
curl http://127.0.0.1:8000/healthz    # → ok
```

Applying the schema, ingest options, Render deploy, scheduled sync, and
troubleshooting are all documented in [COMMANDS.md](COMMANDS.md).

## Environment variables

Copy [.env.example](.env.example) → `.env` and set every value.

| Env var | Where to get it |
| --- | --- |
| `AZURE_SQL_CONNECTION_STRING` | Azure Portal → SQL DB → *Connection strings → ODBC* |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` | Azure Portal → Azure OpenAI → *Keys and Endpoint* |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `AZURE_OPENAI_CHAT_DEPLOYMENT` | Deployment **names** from Azure AI Foundry |
| `AZURE_DOC_INTELLIGENCE_ENDPOINT`, `AZURE_DOC_INTELLIGENCE_KEY` | Azure Portal → Document Intelligence |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) (fallback only) |
| `MICROSOFT_APP_ID`, `MICROSOFT_APP_PASSWORD`, `MICROSOFT_APP_TENANT_ID` | Azure Bot resource's App Registration |
| `SHAREPOINT_TENANT_ID`, `SHAREPOINT_CLIENT_ID`, `SHAREPOINT_CLIENT_SECRET` | Separate Entra app with `Sites.Selected` (below) |
| `SHAREPOINT_SITE_ID`, `SHAREPOINT_DRIVE_ID` | Microsoft Graph (`/sites/…`, `/drives`) |
| `SYNC_SECRET` | `openssl rand -hex 32` |

## SharePoint & Teams (one-time)

**SharePoint** — Create a *separate* Entra app registration (not the bot's own),
grant it Microsoft Graph `Sites.Selected` (application permission + admin
consent), add a client secret, then grant that app `read` on the target site.
Put the client / tenant / secret and the site + drive IDs into the
`SHAREPOINT_*` variables.

**Teams** — Create an Azure Bot resource with messaging endpoint
`https://<host>/api/messages`, enable the **Microsoft Teams** channel, then
upload a Teams app package whose manifest `botId` is the bot's `MICROSOFT_APP_ID`.

---

## Behaviour & security

- **Grounded** — answers only from retrieved document context and prior
  conversation; no outside knowledge. No relevant match → fixed HR-referral
  reply, without calling the LLM.
- **Anti-injection** — the system prompt refuses role changes, instruction
  extraction, and training-data framings.
- **Intent gate** — greetings / thanks / meta questions get canned replies,
  skipping the embed, vector search, and LLM call.
- **Safe by default** — internal errors surface only a short, safe message
  (details are logged server-side); all secrets come from env vars (`.env` is
  git-ignored); the SharePoint app is separate and scoped to one site;
  `SYNC_SECRET` and `SCHEDULED_SYNC_ENABLED` gate the sync endpoint; Azure SQL
  uses TLS via ODBC Driver 18.

See [COMMANDS.md](COMMANDS.md) for the full command reference.
