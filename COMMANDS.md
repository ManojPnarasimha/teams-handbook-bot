# TriconGPT — Command Reference

End-to-end runbook for the Azure-backed stack. Follow section 0 the first
time, then use sections 1–8 as needed.

---

## 0. One-time Azure setup

Create everything in one resource group `rg-tricongpt`, region **East US**
(best Azure OpenAI model availability).

1. **Resource group** — Portal → Resource groups → *Create* → name
   `rg-tricongpt`, region *East US*.

2. **Azure SQL Database** (always-free serverless tier)
   - Portal → *SQL databases* → *Create*.
   - Server: create new; auth = SQL authentication (set admin login + strong password).
   - Compute + storage: **check "Apply free offer"** (serverless, 100k vCore-sec + 32 GB).
   - Networking: allow your client IP + tick *"Allow Azure services and resources to access this server"*.
   - After creation, open *Connection strings* → **ODBC** and copy the full string into
     `AZURE_SQL_CONNECTION_STRING` in your `.env`.

3. **Azure Document Intelligence**
   - Portal → *Document Intelligence* → *Create*.
   - Region *East US*, pricing tier `S0` (500 free pages/mo for 12 months) or `F0` (500 total pages, always free).
   - Copy *Endpoint* + *Key 1* → `AZURE_DOC_INTELLIGENCE_ENDPOINT`, `AZURE_DOC_INTELLIGENCE_KEY`.

4. **Azure OpenAI**
   - Portal → *Azure OpenAI* → *Create*. If access isn't granted yet, submit the short access form (usually approved same day).
   - Open the resource → *Azure AI Foundry* → *Deployments* → create **two** deployments:
     - Model `text-embedding-3-small`, deployment name `emb-small`
     - Model `gpt-4o-mini`, deployment name `chat-mini`
   - Back on the resource, copy *Endpoint* + *Key 1* → `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`.

5. **(Optional) External cron**
   - If you want scheduled syncs later, point a **GitHub Actions cron** or
     [cron-job.org](https://cron-job.org) at `POST /internal/sharepoint-sync`
     with the `X-Sync-Secret` header. Leave `SCHEDULED_SYNC_ENABLED=0` until
     you set this up.

---

## 1. Local setup

```bash
# Microsoft ODBC Driver 18 for SQL Server (macOS with Homebrew)
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
brew update
HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18 mssql-tools18

# Python 3.12 environment
brew install python@3.12
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Environment
cp .env.example .env
# Then edit .env and fill in every empty value.
```

---

## 2. Apply the database schema

```bash
# Runs db/schema.sql against your Azure SQL DB (SQL auth).
sqlcmd \
  -S <server>.database.windows.net \
  -d <database> \
  -U <admin-user> \
  -P '<admin-password>' \
  -N -C \
  -i db/schema.sql
```

Verify the vector column exists:

```bash
sqlcmd -S <server>.database.windows.net -d <database> -U <user> -P '<pwd>' -N -C \
  -Q "SELECT name, system_type_name FROM sys.dm_exec_describe_first_result AS q
      OUTER APPLY sys.dm_exec_describe_first_result_set('SELECT embedding FROM dbo.documents', NULL, 0) AS x;"
```

Or open Azure Data Studio → Tables → `dbo.documents` and confirm `embedding` is `VECTOR(768)`.

---

## 3. Ingest documents from SharePoint

Manual ingestion is the default. Run once after schema is applied, then re-run
whenever you want to refresh.

```bash
# One-shot: process every file in SharePoint (ignores DB state).
python ingest.py --backfill

# Incremental diff: only new / changed / deleted files.
python ingest.py --sync

# Ingest one or a few specific files.
python ingest.py --file "HR Documents/Employee Handbook 2026.pdf"
python ingest.py --file "Policies/leave.pdf" "Policies/expenses.pdf"

# Remove chunks for a single file.
python ingest.py --delete "HR Documents/Employee Handbook 2026.pdf"
```

Sanity check:

```bash
sqlcmd -S <server>.database.windows.net -d <database> -U <user> -P '<pwd>' -N -C \
  -Q "SELECT COUNT(*) AS chunks, COUNT(DISTINCT source_path) AS files FROM dbo.documents;"
```

---

## 4. Run the bot locally

```bash
uvicorn app:app --reload --port 8000
```

Test endpoints:

```bash
# Health
curl http://127.0.0.1:8000/healthz

# OpenAPI docs
open http://127.0.0.1:8000/docs
```

Point the Bot Framework Emulator at `http://localhost:8000/api/messages` and
chat with the bot.

---

## 5. Toggle scheduled sync

The `/internal/sharepoint-sync` endpoint is disabled by default. To enable it:

```bash
# In .env
SCHEDULED_SYNC_ENABLED=1
SYNC_SECRET=$(openssl rand -hex 32)   # then paste the printed value into .env
```

Restart the server. Test:

```bash
# 403 when disabled
curl -i -X POST http://127.0.0.1:8000/internal/sharepoint-sync \
  -H "X-Sync-Secret: $SYNC_SECRET"

# 202 Accepted when enabled + secret matches
curl -i -X POST http://127.0.0.1:8000/internal/sharepoint-sync \
  -H "X-Sync-Secret: $SYNC_SECRET"
```

Then point an external scheduler at that URL. Options:
- **GitHub Actions** on a `schedule:` cron (free for public repos, generous for private).
- **cron-job.org** (free) — hits an HTTPS URL with custom headers.
- **Render Cron Job** (paid Starter tier).

---

## 6. Deploy to Render

The [render.yaml](render.yaml) blueprint installs the Microsoft ODBC Driver 18,
all Python deps, and starts uvicorn.

1. Push this repo to GitHub.
2. Render dashboard → *New* → *Blueprint* → connect the repo → apply.
3. In the service's *Environment* tab, paste values for every `sync: false`
   key from [render.yaml](render.yaml). Pre-filled `value:` keys (chunking
   knobs, model names, embedding dim, log level, etc.) need no action.
4. Wait for the build to go green (first build takes 3–5 min while apt
   installs `msodbcsql18`; subsequent deploys are ~1 min). Verify:
   ```
   https://<service>.onrender.com/healthz   →  ok
   ```
5. Azure Portal → Azure Bot resource → *Configuration* → **Messaging endpoint**
   = `https://<service>.onrender.com/api/messages`.
6. Optional performance: Render's free plan spins down after 15 min idle
   (30–60 s cold start). Upgrade to *Starter* ($7/mo) to keep the service
   warm, or point a lightweight uptime pinger at `/healthz` every 10 min.
   Set *Settings → Region* to **Ohio** for lowest latency to Azure East US
   resources.

---

## 7. Utilities

```bash
# Generate a scheduling secret
openssl rand -hex 32

# Sanity-check DB connectivity from a shell
python - <<'PY'
import asyncio
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")  # explicit path required inside heredocs
import db
async def main():
    row = await db.fetch_one("SELECT 1 AS ok")
    print(row)
    await db.close_pool()
asyncio.run(main())
PY

# Sanity-check embedding round-trip
python - <<'PY'
import asyncio
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")
import embeddings
async def main():
    v = await embeddings.embed("hello")
    print("dim:", len(v))
asyncio.run(main())
PY

# Count chunks per SharePoint file (retrieval sanity check)
sqlcmd -S <server>.database.windows.net -d <database> -U <user> -P '<pwd>' -N -C \
  -Q "SELECT source_path, COUNT(*) AS chunks FROM dbo.documents GROUP BY source_path ORDER BY chunks DESC;"
```

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Data source name not found and no default driver specified` | ODBC Driver 18 missing | Install `msodbcsql18` (see §1). |
| `Library not loaded: .../libodbc.2.dylib` (macOS) | unixODBC missing | `brew install unixodbc` (pulled in automatically by `msodbcsql18`). |
| `Login failed for user ... Azure Active Directory only authentication is enabled` | Server has AAD-only mode on | Azure Portal → SQL Server → *Security → Microsoft Entra ID* → uncheck "Support only Microsoft Entra authentication for this server". |
| `Login failed for user ...` (18456) with no detail | Wrong SQL password | Azure Portal → SQL Server → *Reset password*. Avoid ODBC-sensitive chars (`; { } ' " =`) in the new password. |
| Firewall block from `pyodbc` | Client IP not allow-listed | *SQL Server → Networking → Firewall rules* → add your public IP. |
| `Explicit conversion from data type ntext to vector is not allowed` | Passing embedding param directly to `CAST(? AS VECTOR(768))` | SQL must be `CAST(CONVERT(NVARCHAR(MAX), ?) AS VECTOR(768))` (already fixed in [ingest.py](ingest.py) and [rag.py](rag.py)). |
| `ODBC SQL type -155 is not yet supported` | `DATETIMEOFFSET` decoder missing on connection | Handled by the output converter in [db.py](db.py). If you add a new pool elsewhere, register it there too. |
| Retrieval returns 0 matches for real questions | Threshold too high, empty DB, or wrong embedding dim | Confirm `SELECT COUNT(*) FROM dbo.documents;` > 0, and that `EMBEDDING_DIM=768` matches the DB `VECTOR(<n>)` column and the Azure deployment. Try lowering `RAG_SIMILARITY_THRESHOLD`. |
| `Inserted 2 chunks` for a large PDF | Extraction under-produced or chunker mis-tuned | Check logs for `PyMuPDF extracted <N> blocks (<C> chars)`. If `C` is tiny, the PDF is probably scanned — DI OCR fallback kicks in. Otherwise verify chunking envs (`CHUNK_TARGET_CHARS=500`, `CHUNK_MAX_CHARS=800`). |
| Log says `PyMuPDF yielded 0 chars ...` | PDF has no embedded text (scanned/image) | Ingestion falls back to Azure DI OCR automatically. Confirm `AZURE_DOC_INTELLIGENCE_*` env vars are set. |
| `RESOURCE_LIMIT_EXCEEDED` from Azure OpenAI | Deployment TPM quota | Increase quota in Azure AI Foundry → *Quotas*, or wait and retry. |
| `HTTP 403` from `/internal/sharepoint-sync` | Toggle is off | Set `SCHEDULED_SYNC_ENABLED=1` and restart. |
| `HTTP 401` from `/internal/sharepoint-sync` | Wrong or missing secret | Match the `X-Sync-Secret` header to `SYNC_SECRET` in `.env`. |
| Render build fails on `msodbcsql18` | apt keys / Debian version drift | Bump the Debian codename in the `packages.microsoft.com` URL in [render.yaml](render.yaml). |
| Render service cold-starts (30–60 s) after idle | Free plan spins down | See §6 step 6 — upgrade to Starter, or ping `/healthz` every 10 min. |
