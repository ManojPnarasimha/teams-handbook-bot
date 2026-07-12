# TriconGPT — Internal Org Chatbot

Production-grade Retrieval-Augmented Generation (RAG) chatbot for internal
company use.

- **Chat surface**: Microsoft Teams (via Azure Bot Service)
- **Deploy target**: Render (FastAPI web service)
- **DB + vectors**: Supabase Postgres + `pgvector`
- **Document library**: Microsoft SharePoint (via Microsoft Graph API)
- **Scheduled ingestion**: Supabase `pg_cron` + `pg_net` (free) → `POST /internal/sharepoint-sync`
- **LLM**: Groq `llama-3.3-70b-versatile` (default) or Gemini `gemini-2.5-flash`
- **Embeddings**: Google Gemini `text-embedding-004` (768-dim)
- **Doc extraction**: `unstructured` (`hi_res`, layout-aware)

---

## Architecture

```
Teams  ──►  Azure Bot Service  ──►  Render (FastAPI: /api/messages)
                                        │
                                        ├─►  rag.py  ──►  Supabase pgvector
                                        │                      ▲
                                        ├─►  memory.py ────────┤
                                        └─►  llm_client.py (Groq | Gemini)

Supabase pg_cron (every 15–30 min, pg_net HTTP POST)
        │  X-Sync-Secret: <SYNC_SECRET>
        ▼
Render /internal/sharepoint-sync
        │
        └─►  ingest.sync_from_sharepoint()
                    │
                    ├─►  sharepoint_client.list_files()  ──►  Microsoft Graph
                    └─►  process_file / delete_file_chunks  ──►  Supabase pgvector
```

## Files

| Path | Purpose |
| --- | --- |
| [app.py](app.py) | FastAPI: `/api/messages`, `/internal/sharepoint-sync`, `/healthz` |
| [rag.py](rag.py) | Embed query → pgvector search → prompt → LLM |
| [llm_client.py](llm_client.py) | Provider resolution, grounding prompt, no-context fallback |
| [memory.py](memory.py) | Per-employee history + rolling summary + proactive refs |
| [sharepoint_client.py](sharepoint_client.py) | MSAL auth + Graph `list_files()` / `download_file()` |
| [ingest.py](ingest.py) | `process_file` / `delete_file_chunks` / `sync_from_sharepoint` + CLI |
| [db_client.py](db_client.py) | Async Supabase client + Gemini embedding helper |
| [local_chat.py](local_chat.py) | CLI chat loop bypassing Bot Framework |
| [db/schema.sql](db/schema.sql) | Tables, indexes, `match_documents` RPC |
| [teams-app-package/README.md](teams-app-package/README.md) | Teams manifest + upload steps |
| [render.yaml](render.yaml) | Render service definition |
| [requirements.txt](requirements.txt) | Python deps |
| [.env.example](.env.example) | All env var names (no values) |

---

## 1. Prerequisites

Install once per machine.

**macOS** (Homebrew):

```bash
brew install python@3.12 poppler tesseract libmagic
```

**Debian/Ubuntu**:

```bash
sudo apt-get update && sudo apt-get install -y \
  python3.12 python3.12-venv poppler-utils tesseract-ocr libmagic1
```

`poppler`, `tesseract`, `libmagic` are the native libs `unstructured[pdf]`
needs for `hi_res` PDF extraction. Render installs the Debian equivalents
automatically via [render.yaml](render.yaml).

---

## 2. Get your secrets

Fill these into `.env` (copied from [.env.example](.env.example)). Never commit
`.env` — it is already in [.gitignore](.gitignore).

| Env var | Where to get it |
| --- | --- |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Supabase Dashboard → *Project Settings → API*. `SUPABASE_SERVICE_KEY` is the **`service_role`** key (secret). |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Required for embeddings and (optionally) chat. |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys). Only needed if Groq is the chat provider (the default). |
| `LLM_PROVIDER` | Optional override: `groq` or `gemini`. If unset, Groq wins when both keys are present. |
| `MICROSOFT_APP_ID`, `MICROSOFT_APP_PASSWORD`, `MICROSOFT_APP_TENANT_ID` | Azure Bot resource → *Configuration* (App ID) and *Certificates & secrets* (App Password) of the associated Entra ID App Registration. |
| `MICROSOFT_APP_TYPE` | `MultiTenant` or `SingleTenant` — matches how the Bot's App Registration was created. |
| `SHAREPOINT_TENANT_ID` | Entra ID → your tenant → *Overview* → Tenant ID. |
| `SHAREPOINT_CLIENT_ID`, `SHAREPOINT_CLIENT_SECRET` | A **dedicated** Entra ID App Registration (separate from the bot's) with application permission `Sites.Selected`. See §5 for the one-time site-grant step. |
| `SHAREPOINT_SITE_ID` | `GET https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{site-path}` in [Graph Explorer](https://developer.microsoft.com/graph/graph-explorer). |
| `SHAREPOINT_DRIVE_ID` | `GET https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives` — pick the document library you want indexed. |
| `SHAREPOINT_FOLDER_PATH` | Optional; leave empty for drive root, or e.g. `HR Documents` for a subfolder. |
| `SHAREPOINT_RECURSIVE` | `1` to walk subfolders; default `0`. |
| `SYNC_SECRET` | Any long random string. `openssl rand -hex 32` is fine. |

---

## 3. Run locally

### 3a. Install Python deps

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in secrets from §2
```

### 3b. Apply the Supabase schema

1. Supabase Dashboard → *Database → Extensions* → enable **`vector`**.
2. Supabase Dashboard → *SQL Editor* → paste [db/schema.sql](db/schema.sql) → run.

### 3c. First-time backfill from SharePoint

Grab whatever is already in the SharePoint library and index it:

```bash
python ingest.py --backfill
```

Other ingest commands:

```bash
# Same diff-sync the scheduled endpoint runs (incremental):
python ingest.py --sync

# Remove chunks for a single path (e.g. after deleting a file):
python ingest.py --delete "HR Documents/handbook.pdf"
```

### 3d. Talk to the bot locally (no Teams needed)

```bash
python local_chat.py
```

This hits the same `rag.py` + `memory.py` pipeline against a fake `local-dev-user`
employee, so you can iterate on retrieval quality without any Azure/Teams setup.

### 3e. Run the web server

```bash
uvicorn app:app --reload --port 8000
```

Then:

```bash
curl http://127.0.0.1:8000/healthz              # → ok
curl -X POST http://127.0.0.1:8000/internal/sharepoint-sync   # → 401 (correct, no secret)
curl -X POST -H "X-Sync-Secret: $(grep '^SYNC_SECRET=' .env | cut -d= -f2)" \
  http://127.0.0.1:8000/internal/sharepoint-sync             # → {"accepted":true}
```

`GET /docs` renders the FastAPI OpenAPI page.

---

## 4. Deploy to Render

1. Push this repo to GitHub.
2. Render Dashboard → **New → Web Service** → connect the repo.
3. Render auto-detects [render.yaml](render.yaml). Set every env var listed in
   §2 in the Render dashboard (never commit them).
4. Wait for the first deploy. Verify:
   ```
   https://<render-app>.onrender.com/healthz   → ok
   ```
5. Note the two endpoints for the next steps:
   - `/api/messages` — for Azure Bot Service
   - `/internal/sharepoint-sync` — for the Supabase pg_cron job

Note: [render.yaml](render.yaml)'s `buildCommand` installs `poppler-utils`,
`tesseract-ocr`, and `libmagic1` via `apt-get` before `pip install` so
`unstructured[pdf]` `hi_res` works on Render's Debian build image.

---

## 5. Wire up SharePoint (one-time Entra ID setup)

Do this once, in the tenant that owns the target SharePoint site.

1. **Create the App Registration** (separate from the bot's own registration):
   - Azure Portal → Entra ID → *App registrations* → *New registration*.
   - Any name; single-tenant is fine.
2. **Grant `Sites.Selected`**:
   - *API permissions* → *Add* → Microsoft Graph → **Application permissions**
     → `Sites.Selected` → *Add permission* → **Grant admin consent**.
3. **Create a client secret**:
   - *Certificates & secrets* → *New client secret* → save the **value** now
     (it is shown only once). This is `SHAREPOINT_CLIENT_SECRET`.
4. **Record IDs**:
   - *Overview* → **Application (client) ID** = `SHAREPOINT_CLIENT_ID`.
   - *Overview* → **Directory (tenant) ID** = `SHAREPOINT_TENANT_ID`.
5. **Grant this app access to the target site** — `Sites.Selected` gives no
   access until you do this. In [Graph Explorer](https://developer.microsoft.com/graph/graph-explorer)
   (signed in as an admin):
   ```http
   POST https://graph.microsoft.com/v1.0/sites/{SITE_ID}/permissions
   Content-Type: application/json

   {
     "roles": ["read"],
     "grantedToIdentities": [
       { "application": {
           "id": "<SHAREPOINT_CLIENT_ID>",
           "displayName": "TriconGPT Sync"
       } }
     ]
   }
   ```
6. **Find `SHAREPOINT_SITE_ID` / `SHAREPOINT_DRIVE_ID`**:
   ```
   GET https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{site-path}
   GET https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives
   ```

---

## 6. Schedule the sync from Supabase (free, no paid cron)

In the Supabase SQL editor:

```sql
-- Enable extensions (once)
create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Schedule the diff sync (every 15 min; use '*/30 * * * *' for 30 min)
select cron.schedule(
  'sharepoint-sync',
  '*/15 * * * *',
  $$
  select net.http_post(
    url     := 'https://<render-app>.onrender.com/internal/sharepoint-sync',
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'X-Sync-Secret', '<same value as SYNC_SECRET>'
    ),
    body    := '{}'::jsonb
  );
  $$
);

-- Verify
select * from cron.job;
```

Graph webhook subscriptions were rejected here: they expire every few days and
would need an auto-renewal job for no benefit at this scale.

---

## 7. Wire up Teams (Azure Bot Service + Teams app package)

1. **Azure Bot resource**:
   - Azure Portal → *Create resource → Azure Bot* (Multi-tenant is easiest).
   - *Configuration → Messaging endpoint* = `https://<render-app>.onrender.com/api/messages`.
   - Copy the **Microsoft App ID** into Render as `MICROSOFT_APP_ID`.
   - Under the associated App Registration → *Certificates & secrets* → new
     client secret → set as `MICROSOFT_APP_PASSWORD` in Render.
   - Redeploy Render so the new env vars take effect.
2. **Test in Web Chat**: Azure Bot resource → *Test in Web Chat*.
3. **Enable Teams channel**: Azure Bot resource → *Channels* → add **Microsoft Teams**.
4. **Package + upload the Teams app**:
   - Edit [teams-app-package/manifest.json](teams-app-package/manifest.json) —
     replace the placeholder GUID in **both** `id` and `bots[0].botId` with
     the real Microsoft App ID (they must match).
   - Zip and upload:
     ```bash
     cd teams-app-package
     zip -j tricongpt-teams.zip manifest.json color.png outline.png
     ```
   - Teams → *Apps → Manage your apps → Upload a custom app* → select the zip.
     If blocked, ask a Teams admin to enable *Upload custom apps* in the setup
     policy.

---

## 8. End-to-end verification

- Upload a small PDF to the SharePoint library.
- Wait one polling interval (15 min by default) or trigger manually:
  ```bash
  curl -X POST -H "X-Sync-Secret: <SYNC_SECRET>" \
    https://<render-app>.onrender.com/internal/sharepoint-sync
  ```
- Check Supabase:
  ```sql
  select count(*) from documents where source_path = '<path>';
  ```
- Message the bot in Teams (or `python local_chat.py`) with a question the PDF
  answers. You should get a grounded reply.
- Delete the PDF from SharePoint → next sync → rows disappear.

---

## Behavioural guarantees

- **Grounding**: [llm_client.py](llm_client.py) instructs the model to answer
  only from retrieved CONTEXT and prior HISTORY. General knowledge is disallowed.
- **Anti-injection**: the system prompt explicitly rejects role changes,
  instruction extraction, and training-data framings.
- **No-context short-circuit**: if retrieval returns no chunks above the
  similarity threshold, the LLM is **not called** — a fixed HR-referral message
  is returned.
- **Safe errors**: LLM / Supabase / SharePoint failures are logged with details
  server-side but only ever surface a short, safe message to the employee.
- **Async everywhere**: no blocking calls in the request path; heavy work
  (ingestion, embeddings, summarisation, Graph calls) runs off the event loop.
- **Real-time feel**: a `typing` activity is sent immediately on every message,
  before the LLM call starts, so Teams shows the "…" indicator.
- **Proactive messaging**: every turn persists the Bot Framework
  `ConversationReference` so `adapter.continue_conversation` can push messages
  to the employee later.

## Security notes

- The `service_role` Supabase key is only used server-side by the Render
  service. It is never exposed to Teams clients.
- The SharePoint Entra ID App Registration is **separate** from the bot's own
  App Registration and holds `Sites.Selected` — access is scoped to the single
  target site, not to the whole tenant.
- `SYNC_SECRET` gates `/internal/sharepoint-sync`; missing or wrong headers
  return HTTP 401.
- All secrets come from environment variables. `.env` is git-ignored.
- Bot Framework authenticates inbound Teams calls via the `Authorization`
  header and the configured `MICROSOFT_APP_ID` / `MICROSOFT_APP_PASSWORD`.

---

## Command cheat sheet

```bash
# One-time system deps (macOS)
brew install python@3.12 poppler tesseract libmagic

# Project bootstrap
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in secrets

# Ingestion (SharePoint → Supabase pgvector)
python ingest.py --backfill                        # full re-index
python ingest.py --sync                            # incremental diff
python ingest.py --delete "path/inside/drive.pdf"  # remove one file's chunks

# Local chat (no Teams)
python local_chat.py

# Web server
uvicorn app:app --reload --port 8000
curl http://127.0.0.1:8000/healthz

# Generate a strong SYNC_SECRET
openssl rand -hex 32
```
