# Free RAG Chatbot for Microsoft Teams — Setup Guide

Everything in this stack is free. Your PDF is embedded and searched **locally on your machine** — it is never uploaded anywhere. Only the few retrieved paragraphs relevant to each question are sent to the LLM.

## The free stack

| Layer | Choice | Cost | Trains on your data? |
|---|---|---|---|
| LLM | Groq free tier (Llama 3.3 70B) | Free, no card | No (not retained by default; ZDR option available) |
| Embeddings | sentence-transformers (local) | Free | Never leaves your PC |
| Vector store | numpy file on disk | Free | Local |
| Bot registration | Azure Bot, F0 tier | Free | — |
| Hosting | Your own machine + free dev tunnel | Free | — |

> ⚠️ **Why not Gemini free tier?** Google's free-tier terms allow using your prompts and responses to improve their models. Since your handbook is confidential, use Groq (or a *paid* Gemini key). Gemini support is still included — set `LLM_PROVIDER=gemini` — but only use it with a paid key.

---

## Part 1 — Get it answering questions locally (10 minutes, no Azure needed)

1. Install Python 3.10+ and then:
   ```bash
   cd teams-handbook-bot
   pip install -r requirements.txt
   ```
2. Get a free Groq API key: https://console.groq.com → API Keys → Create. No credit card needed.
   Optional: in Groq console **Settings → Data Controls**, enable **Zero Data Retention**.
3. Set the key:
   ```bash
   # Windows (PowerShell)
   $env:GROQ_API_KEY="gsk_..."
   # macOS/Linux
   export GROQ_API_KEY="gsk_..."
   ```
4. Index your PDF (one time; re-run if the PDF changes):
   ```bash
   python ingest.py employee_handbook.pdf
   ```
   The first run downloads the ~90MB embedding model once.
5. Chat in the terminal to verify:
   ```bash
   python chat_local.py
   ```
   Try: *"How many earned leave days do I get?"* — it should answer with page references.

If this works, the whole RAG pipeline works. Teams is just a front-end on top.

---

## Part 2 — Important reality check about Teams

A **personal (consumer) Microsoft account cannot install custom bots** into Teams. Custom app upload only works in a Microsoft 365 **organizational tenant**, and only when the admin has enabled "Upload custom apps" (Teams Admin Center → Teams apps → Setup policies).

Your free options, in order of ease:

- **Option A — Your company's tenant (recommended if this bot is for work).** Ask your IT admin to enable custom app upload for your account. Everything else is free.
- **Option B — Create your own free tenant.** An Azure free account (azure.com/free) comes with its own Microsoft Entra tenant. You can add a Microsoft 365 Business Basic **1-month free trial** to that tenant to get Teams, and you are the admin, so you can enable custom app upload yourself. (After the trial, Teams-side testing stops unless you pay — the bot itself stays free.)
- **Option C — Skip Teams for now.** Use `chat_local.py` or the free **Bot Framework Emulator** (github.com/microsoft/BotFramework-Emulator) as your chat UI. Zero Microsoft setup.

## Part 3 — Register the bot in Azure (free)

1. Sign up at https://azure.microsoft.com/free (card needed for identity verification only — bot registration on the **F0 Free** tier is never charged).
2. Azure Portal → **Create a resource** → search **"Azure Bot"** → Create:
   - Pricing tier: **Free (F0)**
   - Type of App: **Single Tenant**
   - Creation type: *Create new Microsoft App ID*
3. Once created, open the bot resource → **Configuration**:
   - Copy the **Microsoft App ID**.
   - Click **Manage** next to it → **Certificates & secrets** → **New client secret** → copy the **Value**.
   - Also note your **Directory (tenant) ID** from the app registration Overview page.
4. Bot resource → **Channels** → add **Microsoft Teams** → agree → Apply.

## Part 4 — Expose your local bot to the internet (free)

Pick one:

**Dev tunnels (Microsoft, free):**
```bash
winget install Microsoft.devtunnel      # or: brew install --cask devtunnel
devtunnel user login
devtunnel host -p 3978 --allow-anonymous
```

**ngrok (free tier):**
```bash
ngrok http 3978
```

Copy the HTTPS URL it gives you (e.g. `https://abc123.devtunnels.ms`).

Back in Azure → your Bot → **Configuration** → **Messaging endpoint**:
```
https://YOUR-TUNNEL-URL/api/messages
```

## Part 5 — Run the bot server

```bash
# Windows PowerShell
$env:MicrosoftAppId="<App ID>"
$env:MicrosoftAppPassword="<client secret value>"
$env:MicrosoftAppType="SingleTenant"
$env:MicrosoftAppTenantId="<tenant ID>"
$env:GROQ_API_KEY="gsk_..."
python app.py
```

You should see `Bot running on http://localhost:3978/api/messages`.
Quick test in Azure: Bot resource → **Test in Web Chat** — it should answer handbook questions right there.

## Part 6 — Install it in Teams

1. Edit `appPackage/manifest.json` — replace **both** occurrences of `REPLACE_WITH_YOUR_MICROSOFT_APP_ID` with your Microsoft App ID.
2. Zip the **contents** of the `appPackage` folder (manifest.json + color.png + outline.png — the files must be at the zip root, not inside a subfolder).
3. In Teams (signed into the tenant from Part 2): **Apps → Manage your apps → Upload an app → Upload a custom app** → pick the zip.
4. Open the app → chat with it. In a channel, @mention the bot.

## Troubleshooting

- **401 Unauthorized** — App ID/secret/tenant ID mismatch, or `MicrosoftAppType` wrong. New bots default to SingleTenant.
- **Bot doesn't reply in Teams but Web Chat works** — Teams channel not added (Part 3 step 4), or the tunnel URL changed (free tunnels get a new URL on each restart; update the messaging endpoint).
- **"Upload a custom app" missing in Teams** — your tenant admin hasn't enabled it, or you're on a personal account (see Part 2).
- **429 from Groq** — free-tier rate limit; wait a minute. Fine for personal use.
- **Slow first answer** — the embedding model loads on first query; subsequent answers are fast.

## Keeping it free long-term

- Groq free tier: ~1,000 requests/day — far more than personal use needs.
- Azure Bot F0: free forever for standard channels like Teams.
- Your PC is the server: the bot only works while `app.py` and the tunnel are running. For 24/7 uptime later, free-tier hosts like Render/Railway/Fly.io can run this same code.
