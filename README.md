# TriconGPT — Internal Org Chatbot

Retrieval-Augmented Generation (RAG) chatbot for internal company use.

- **Chat surface**: Microsoft Teams (via Azure Bot Service)
- **Deploy target**: Render (FastAPI web service)
- **DB + vectors**: Azure SQL Database with the native `VECTOR(768)` type
- **Document library**: Microsoft SharePoint (Microsoft Graph API)
- **LLM**: Azure OpenAI `gpt-4o-mini` (primary), Groq `llama-3.3-70b-versatile` (fallback)
- **Embeddings**: Azure OpenAI `text-embedding-3-small` @ 768 dims
- **Doc extraction**: PyMuPDF (embedded text, primary) with Azure Document
  Intelligence `prebuilt-layout` as an OCR fallback for scanned PDFs

---

## Architecture

```
Teams ─► Azure Bot Service ─► Render (FastAPI: /api/messages)
                                    │
                                    ├─► rag.py ─► Azure SQL (VECTOR_DISTANCE)
                                    ├─► memory.py (history + rolling summary)
                                    └─► llm_client.py (Azure OpenAI → Groq fallback)

External scheduler (optional) ─► POST /internal/sharepoint-sync
                                    │ X-Sync-Secret: <SYNC_SECRET>
                                    └─► ingest.sync_from_sharepoint()
                                            │
                                            ├─► sharepoint_client.list_files()
                                            └─► process_file / delete_file_chunks
```

## Files

| Path | Purpose |
| --- | --- |
| [app.py](app.py) | FastAPI: `/api/messages`, `/internal/sharepoint-sync`, `/healthz` |
| [rag.py](rag.py) | Intent gate → embed → VECTOR_DISTANCE search → prompt → LLM |
| [llm_client.py](llm_client.py) | Azure OpenAI primary, Groq fallback, grounding prompt |
| [memory.py](memory.py) | Per-employee history, rolling summary, proactive refs |
| [sharepoint_client.py](sharepoint_client.py) | MSAL auth + Graph `list_files` / `download_file` |
| [ingest.py](ingest.py) | Extract → chunk → embed → upsert (CLI: `--backfill`, `--sync`, `--file`, `--delete`) |
| [document_extractor.py](document_extractor.py) | PyMuPDF (primary) + Azure DI OCR (fallback) |
| [embeddings.py](embeddings.py) | Azure OpenAI embeddings client (`text-embedding-3-small` @ 768 dims) |
| [db.py](db.py) | Async Azure SQL client (aioodbc) + `VECTOR` helpers |
| [db/schema.sql](db/schema.sql) | Tables and indexes (native `VECTOR(768)` column) |
| [teams-app-package/](teams-app-package/) | Teams manifest + icons |
| [render.yaml](render.yaml) | Render Blueprint (build + env + start command) |
| [COMMANDS.md](COMMANDS.md) | Command runbook (setup, ingest, deploy, troubleshoot) |
| [.env.example](.env.example) | All env var names |

---

## 1. Prerequisites (local dev)

**macOS**:

```bash
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18 mssql-tools18
brew install python@3.12
```

**Debian/Ubuntu**: install `msodbcsql18` from Microsoft's apt repo (see
[Microsoft ODBC install docs](https://learn.microsoft.com/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server)).

Render installs the driver automatically via [render.yaml](render.yaml)'s
`buildCommand`.

---

## 2. Env vars

Copy [.env.example](.env.example) → `.env` and fill in every value.

| Env var | Where to get it |
| --- | --- |
| `AZURE_SQL_CONNECTION_STRING` | Azure Portal → SQL DB → *Connection strings → ODBC* |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` | Azure Portal → Azure OpenAI resource → *Keys and Endpoint* |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `AZURE_OPENAI_CHAT_DEPLOYMENT` | Deployment **names** from Azure AI Foundry (not model names) |
| `AZURE_DOC_INTELLIGENCE_ENDPOINT`, `AZURE_DOC_INTELLIGENCE_KEY` | Azure Portal → Document Intelligence resource |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) (fallback only) |
| `MICROSOFT_APP_ID`, `MICROSOFT_APP_PASSWORD`, `MICROSOFT_APP_TENANT_ID` | Azure Bot resource's App Registration → *Overview* + *Certificates & secrets* |
| `SHAREPOINT_TENANT_ID`, `SHAREPOINT_CLIENT_ID`, `SHAREPOINT_CLIENT_SECRET` | A **separate** Entra App Registration with `Sites.Selected` (see §5) |
| `SHAREPOINT_SITE_ID`, `SHAREPOINT_DRIVE_ID` | [Graph Explorer](https://developer.microsoft.com/graph/graph-explorer): `GET /sites/{hostname}:/sites/{path}` and `GET /sites/{SITE_ID}/drives` |
| `SYNC_SECRET` | `openssl rand -hex 32` |

---

## 3. Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then fill in secrets from §2
```

Apply the schema (once):

```bash
sqlcmd -S <server>.database.windows.net -d <db> \
  -U <user> -P '<pwd>' -N -C -i db/schema.sql
```

Ingest documents:

```bash
python ingest.py --backfill                          # full re-index
python ingest.py --sync                              # incremental diff
python ingest.py --file "HR/handbook.pdf"            # one or more files
python ingest.py --delete "HR/handbook.pdf"          # remove one file's chunks
```

Run the bot server:

```bash
uvicorn app:app --reload --port 8000
curl http://127.0.0.1:8000/healthz    # → ok
```

Point the [Bot Framework Emulator](https://github.com/microsoft/BotFramework-Emulator/releases)
at `http://localhost:8000/api/messages` to chat with the bot locally.

---

## 4. Deploy to Render

1. Push this repo to GitHub.
2. Render Dashboard → **New → Blueprint** → connect the repo. Render reads
   [render.yaml](render.yaml) and creates the service (build installs the
   ODBC driver + Python deps; start runs `uvicorn app:app`).
3. In the service's *Environment* tab, paste values for every `sync: false`
   key from [render.yaml](render.yaml) (Azure SQL / OpenAI / DI / Bot / Groq
   / SharePoint / `SYNC_SECRET`).
4. Wait for the build to go green. Verify:
   ```
   https://<service>.onrender.com/healthz   →  ok
   ```
5. Azure Portal → Azure Bot resource → *Configuration* → **Messaging
   endpoint** = `https://<service>.onrender.com/api/messages`.

See [COMMANDS.md](COMMANDS.md) §6 for the full runbook (region choice,
cold-start behaviour, keep-alive options).

---

## 5. Wire up SharePoint (one-time Entra ID setup)

Do this once, in the tenant that owns the target SharePoint site.

1. **App Registration** — Azure Portal → Entra ID → *App registrations* → *New*.
   Use a **separate** registration from the bot's own.
2. **Grant `Sites.Selected`** — *API permissions* → Microsoft Graph → *Application
   permissions* → `Sites.Selected` → *Grant admin consent*.
3. **Client secret** — *Certificates & secrets* → *New client secret* → save
   the **value** (shown once) as `SHAREPOINT_CLIENT_SECRET`.
4. **Record IDs** — *Overview* → **Application (client) ID** = `SHAREPOINT_CLIENT_ID`,
   **Directory (tenant) ID** = `SHAREPOINT_TENANT_ID`.
5. **Grant this app access to the site** (`Sites.Selected` alone gives no access).
   In [Graph Explorer](https://developer.microsoft.com/graph/graph-explorer) as an admin:

   ```http
   POST https://graph.microsoft.com/v1.0/sites/{SITE_ID}/permissions
   Content-Type: application/json

   {
     "roles": ["read"],
     "grantedToIdentities": [
       { "application": { "id": "<SHAREPOINT_CLIENT_ID>", "displayName": "TriconGPT Sync" } }
     ]
   }
   ```
6. **Find IDs**:
   ```
   GET https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{site-path}
   GET https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives
   ```

---

## 6. Optional: scheduled SharePoint sync

Manual ingestion (`python ingest.py --sync`) is always available. To automate:

1. Set `SCHEDULED_SYNC_ENABLED=1` and a strong `SYNC_SECRET` in your host env.
2. Point an external scheduler at `POST /internal/sharepoint-sync` with an
   `X-Sync-Secret: <SYNC_SECRET>` header. Options:
   - **Azure Container Apps Job** on a cron schedule (recommended — same cloud).
   - **GitHub Actions** on a `schedule:` cron.
   - **cron-job.org** (free HTTPS pinger with custom headers).

The endpoint returns `403` if the toggle is off, `401` on bad/missing secret,
and `202 Accepted` on a valid trigger (ingestion runs in a background task).

---

## 7. Wire up Teams

1. **Azure Bot resource** — Portal → *Create resource → Azure Bot*.
   *Configuration → Messaging endpoint* = `https://<host>/api/messages`.
2. **Enable Teams channel** — Azure Bot → *Channels* → add *Microsoft Teams*.
3. **Package the app**:
   ```bash
   cd teams-app-package
   # Fill in real MICROSOFT_APP_ID in manifest.json's `id` AND `bots[0].botId`
   zip -j tricongpt-teams.zip manifest.json color.png outline.png
   ```
4. **Upload** — Teams → *Apps → Manage your apps → Upload a custom app* →
   select the zip. If blocked, ask a Teams admin to enable *Upload custom apps*
   in the setup policy.

Full step-by-step in [teams-app-package/README.md](teams-app-package/README.md).

---

## Behavioural guarantees

- **Intent gate** ([rag.py](rag.py)): greetings / meta / thanks match a regex
  and get canned replies — no embed / vector search / LLM call needed.
- **Grounding**: the LLM is instructed to answer only from retrieved CONTEXT
  and prior HISTORY. General knowledge is disallowed.
- **Anti-injection**: the system prompt explicitly rejects role changes,
  instruction extraction, and training-data framings.
- **No-context short-circuit**: if retrieval returns no chunks above the
  similarity threshold, the LLM is not called — a fixed HR-referral message
  is returned.
- **Safe errors**: retrieval / DB / SharePoint failures are logged with
  details server-side but only surface a short, safe message to the employee.
- **Async everywhere**: no blocking calls on the request path; heavy work
  (ingestion, embeddings, summarisation, Graph calls) runs off the event loop.
- **Real-time feel**: a `typing` activity is sent immediately on every
  message, before the LLM call starts.
- **Proactive messaging**: every turn persists the Bot Framework
  `ConversationReference` so `adapter.continue_conversation` can push messages
  to the employee later.

## Security notes

- The SharePoint Entra App Registration is **separate** from the bot's own,
  and holds `Sites.Selected` scoped to a single site.
- `SYNC_SECRET` gates `/internal/sharepoint-sync`; `SCHEDULED_SYNC_ENABLED=0`
  additionally hard-disables the endpoint (returns 403 regardless of secret).
- All secrets come from environment variables. `.env` is git-ignored.
- Bot Framework authenticates inbound Teams calls via the `Authorization`
  header and the configured `MICROSOFT_APP_ID` / `MICROSOFT_APP_PASSWORD`.
- The Azure SQL connection uses `Encrypt=yes` (TLS 1.2+) via ODBC Driver 18.

---

See [COMMANDS.md](COMMANDS.md) for the full command reference (setup, ingest,
deploy, troubleshoot).
