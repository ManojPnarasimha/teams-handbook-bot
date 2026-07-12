# TriconGPT — Command Reference

## Setup (one-time)

```bash
# Create virtual environment and install deps
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy env template and fill in secrets
cp .env.example .env
```

## Ingestion (SharePoint → Supabase)

```bash
# Backfill all files from SharePoint
python ingest.py --backfill

# Incremental sync (only new/changed files)
python ingest.py --sync

# Delete chunks for a specific file
python ingest.py --delete "Employee Handbook 2026.pdf"
```

## Run the server

```bash
# Local dev with auto-reload
uvicorn app:app --reload --port 8000
```

## Test endpoints

```bash
# Health check
curl http://127.0.0.1:8000/healthz

# Trigger SharePoint sync manually
curl -X POST -H "X-Sync-Secret: $(grep '^SYNC_SECRET=' .env | cut -d= -f2)" \
  http://127.0.0.1:8000/internal/sharepoint-sync

# OpenAPI docs
open http://127.0.0.1:8000/docs
```

## Utility

```bash
# Generate a SYNC_SECRET
openssl rand -hex 32
```
