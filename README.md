# TriconGPT — Internal Org Chatbot

Production-grade Retrieval-Augmented Generation (RAG) chatbot for internal
company use, integrated with **Microsoft Teams** via **Azure Bot Service**,
deployed on **Render**, using **Supabase** (Postgres + pgvector) as the sole
datastore. Ingestion is **event-driven** via a Supabase Database Webhook — no
cron job, no paid scheduler.

## Architecture

```
Teams  ──►  Azure Bot Service  ──►  Render (FastAPI: /api/messages)
                                        │
                                        ├─►  rag.py  ──►  Supabase pgvector
                                        │                      ▲
                                        ├─►  memory.py ────────┤
                                        └─►  llm_client.py (Groq | Gemini)

Supabase Storage (org-docs bucket)
        │  INSERT / UPDATE / DELETE on storage.objects
        ▼
Supabase Database Webhook  ──►  Render (/internal/storage-webhook)
                                        │
                                        └─►  ingest.py  ──►  Supabase pgvector
```

## Stack

- **Backend**: Python, FastAPI (async), Bot Framework SDK
- **DB**: Supabase Postgres + `pgvector`
- **LLM**: Groq `llama-3.3-70b-versatile` (default) or Gemini `gemini-2.5-flash`
- **Embeddings**: OpenAI `text-embedding-3-small` (1536-dim — matches the schema)
- **Doc extraction**: `unstructured` (`hi_res`, layout-aware)
- **Deploy**: Render web service, auto-deploy from GitHub

## Files

| Path | Purpose |
| --- | --- |
| [app.py](app.py) | FastAPI: `/api/messages`, `/internal/storage-webhook`, `/healthz` |
| [rag.py](rag.py) | Embed → pgvector search → prompt → LLM |
| [llm_client.py](llm_client.py) | Provider resolution, grounding prompt, no-context fallback |
| [memory.py](memory.py) | Per-employee history + rolling summary + proactive refs |
| [ingest.py](ingest.py) | `process_file` / `delete_file_chunks` + `--backfill` CLI |
| [db_client.py](db_client.py) | Async Supabase client + embedding helper |
| [local_chat.py](local_chat.py) | CLI loop bypassing Bot Framework |
| [db/schema.sql](db/schema.sql) | Tables, indexes, and the `match_documents` RPC |
| [teams-app-package/README.md](teams-app-package/README.md) | Teams manifest + icons + upload instructions |
| [render.yaml](render.yaml) | Render service definition |
| [requirements.txt](requirements.txt) | Python deps |
| [.env.example](.env.example) | All env var names (no values) |

## Environment variables

Copy [.env.example](.env.example) to `.env` for local dev; in production set
them in the Render dashboard. Never commit `.env`.

Required:

- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
- `OPENAI_API_KEY` (embeddings)
- One of `GROQ_API_KEY` / `GEMINI_API_KEY` (Groq wins if both set unless `LLM_PROVIDER` overrides)
- `MICROSOFT_APP_ID`, `MICROSOFT_APP_PASSWORD` (from Azure Bot Service)
- `WEBHOOK_SECRET` (shared secret for the Supabase webhook)

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in values

# Apply schema in Supabase SQL editor:
#   copy the contents of db/schema.sql and run it.

# Local CLI (skips Teams/Azure entirely):
python local_chat.py

# Run the full web server locally:
uvicorn app:app --reload --port 8000
```

## Deployment flow

1. **Supabase Storage bucket**: create `org-docs` (private, not public). This
   is where employees/admins upload source documents.
2. **Supabase schema**: paste [db/schema.sql](db/schema.sql) into the Supabase
   SQL editor and run it.
3. **GitHub → Render**: push this repo, create a Render web service pointing at
   it, and set all env vars listed above (including a freshly generated
   `WEBHOOK_SECRET`). Render will pick up [render.yaml](render.yaml), whose
   `buildCommand` installs `poppler-utils`, `tesseract-ocr`, and `libmagic1`
   via `apt-get` before `pip install` — these are the same native libs needed
   locally for `unstructured[pdf]` `hi_res` extraction, just installed for
   Render's Debian build image instead of via `brew`.
4. **Verify Render URL**: `https://<render-app>.onrender.com/healthz` should
   return `ok`. Note the two endpoints:
   - `/api/messages` — for Azure Bot Service
   - `/internal/storage-webhook` — for the Supabase Database Webhook
5. **Supabase Database Webhook** — the free, event-driven ingestion trigger:
   - Supabase Dashboard → **Database → Webhooks → Create a new hook**
   - Table: `storage.objects`
   - Events: `INSERT`, `UPDATE`, `DELETE`
   - Type: HTTP request → `POST https://<render-app>.onrender.com/internal/storage-webhook`
   - HTTP Headers: `X-Webhook-Secret: <same value as WEBHOOK_SECRET>`
   - (Optional) filter payloads to `bucket_id = org-docs` — the endpoint also
     rejects other buckets defensively.
6. **Backfill existing files**: if `org-docs` already has files, process them
   once (the webhook only fires on future changes):
   ```bash
   python ingest.py --backfill
   ```
   Run this locally with `.env` populated, or from a one-off Render shell.
7. **Azure Bot Service**:
   - Create an Azure Bot resource (Multi-tenant).
   - Configuration → **Messaging endpoint** =
     `https://<render-app>.onrender.com/api/messages`.
   - Copy the **Microsoft App ID** and generate a client secret (**App
     Password**); set both in Render as `MICROSOFT_APP_ID` /
     `MICROSOFT_APP_PASSWORD`.
   - Redeploy Render so the new env vars take effect.
8. **Test in Web Chat**: Azure Bot resource → *Test in Web Chat*.
9. **Enable Teams channel**: Azure Bot resource → *Channels* → add **Microsoft
   Teams**. (Requires a Microsoft 365 work/school tenant with sideloading
   enabled.)
10. **Teams app package**:
    - Open [teams-app-package/manifest.json](teams-app-package/manifest.json).
    - Replace the placeholder GUID in both `id` and `bots[0].botId` with the
      real Microsoft App ID from step 7. They must match.
    - Zip the three files (see
      [teams-app-package/README.md](teams-app-package/README.md)):
      ```bash
      cd teams-app-package
      zip -j tricongpt-teams.zip manifest.json color.png outline.png
      ```
    - In Teams → *Apps* → *Manage your apps* → *Upload a custom app* → select
      the zip. If blocked, ask a Teams admin to enable *Upload custom apps* in
      the setup policy.
11. **Verify end-to-end webhook**: upload a small PDF to `org-docs`; within
    seconds a `SELECT count(*) FROM documents WHERE source_path = '<path>'`
    should return the new chunk rows. Delete the file — the rows should
    disappear.

## Behavioural guarantees

- **Grounding**: `llm_client.py` instructs the model to answer only from
  retrieved CONTEXT and prior HISTORY. General knowledge is disallowed.
- **Anti-injection**: the system prompt explicitly rejects role changes,
  instruction extraction, and training-data framings.
- **No-context short-circuit**: if retrieval returns no chunks above the
  similarity threshold, the LLM is **not called** — a fixed HR-referral message
  is returned.
- **Safe errors**: LLM / Supabase failures are logged with details server-side
  but only ever surface a short, safe message to the employee.
- **Async everywhere**: no blocking calls in the request path; heavy work
  (ingestion, embeddings, summarisation) runs off the event loop.
- **Real-time feel**: a `typing` activity is sent immediately on every message,
  before the LLM call starts, so Teams shows the "…" indicator.
- **Proactive messaging**: every turn persists the Bot Framework
  `ConversationReference` so `adapter.continue_conversation` can push messages
  to the employee later.

## Security notes

- The `service_role` Supabase key is only used server-side by the Render
  service. It is never exposed to Teams clients.
- `WEBHOOK_SECRET` gates `/internal/storage-webhook`; missing or wrong headers
  return HTTP 401.
- All secrets come from environment variables. `.env` is git-ignored.
- Bot Framework authenticates inbound Teams calls via the `Authorization`
  header and the configured `MICROSOFT_APP_ID` / `MICROSOFT_APP_PASSWORD`.
