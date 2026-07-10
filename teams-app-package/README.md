# TriconGPT — Teams App Package

This folder is a ready-to-zip Teams app manifest package.

## What to replace before uploading

Open `manifest.json` and replace **both** placeholder GUIDs with the bot's real
**Microsoft App ID** from Azure Bot Service → *Configuration*:

- `"id"` at the top of the manifest
- `"bots"[0].botId"`

These two fields **must be identical**. Teams uses `id` to identify the app and
`botId` to route messages to your bot — a mismatch will cause the bot to load
but never respond.

The `"id"` is currently `00000000-0000-0000-0000-000000000000`. Do not upload
until it is replaced.

## Packaging

Teams requires a `.zip` of the **three files themselves**, not the folder that
contains them. From this directory:

```bash
zip -j tricongpt-teams.zip manifest.json color.png outline.png
```

The `-j` flag strips paths so the archive contains the files at the root, which
is what Teams expects.

## Uploading to Teams

1. In Teams, open **Apps** → **Manage your apps** → **Upload an app** → **Upload a custom app**.
2. Select `tricongpt-teams.zip`.
3. Open a personal chat with **TriconGPT** to test.

### If upload is blocked

Your tenant may have custom app uploading disabled. A Teams admin needs to enable
it in the **Teams Admin Center** → **Teams apps** → **Setup policies** →
*Upload custom apps*. After the policy update, sign out and back in to Teams.

## Scope

The manifest declares `"scopes": ["personal"]` only — TriconGPT is a 1:1
per-employee assistant with per-employee memory. Do not add `team` or
`groupchat` unless the design changes to support shared conversations.

## Icons

- `color.png` — 192×192, brand accent (`#4F46E5`) rounded square with a white
  chat-bubble glyph. Used in the Teams app catalog and app details.
- `outline.png` — 32×32, transparent background, white-only silhouette. Used in
  the Teams left rail. Teams requires this to be single-colour on transparency.

If you rebrand, keep those exact dimensions and the outline requirement.
