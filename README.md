# VaultRequestrr

A Discord bot for requesting movies and TV shows through
[Seerr](https://github.com/seerr-team/seerr) (the unified successor to
Overseerr/Jellyseerr), with **self-service Plex account linking** so that each
user's requests are attributed to *their* Seerr account and their per-user
quotas/limits are respected.

## Why this exists

Tools like [requestrr](https://github.com/thomst08/requestrr) attribute a
request to the right Seerr user by matching a Discord ID that an **admin** has
to hand-enter into each Seerr user's notification settings. After the
Overseerr → Seerr migration those IDs are typically empty, so every request
falls through to a single default user (or fails) and per-user quotas stop
working.

VaultRequestrr fixes this without any admin busywork: the first time a user
requests something, the bot asks for their Plex username/email, resolves it to a
Seerr user, remembers the link, and from then on submits every request as that
user. The link is also written back into Seerr's notification settings so it's
visible in the Seerr UI.

## How it works

1. A user runs `/movie <title>` or `/tv <title>`.
2. The bot searches Seerr and shows the results to pick from (TV adds a season picker).
3. On the user's **first** request, a popup asks for their Plex username/email.
   - It must match an existing Seerr user (log into Seerr once if not yet imported from Plex).
4. The link is saved (SQLite) and written back to Seerr.
5. The request is submitted with that user's `userId`, so Seerr applies their quota/permissions.

Commands:

| Command | Description |
| --- | --- |
| `/movie <title>` | Search for and request a movie |
| `/tv <title>` | Search for and request a TV show (with season selection) |
| `/linkstatus` | Show which Seerr account you're linked to |
| `/unlink` | Remove your link (you'll be asked again on the next request) |

## Setup

### 1. Create a Discord bot
- Go to <https://discord.com/developers/applications> → New Application → Bot.
- Copy the **bot token**.
- Invite it to your server with the `applications.commands` and `bot` scopes.
  No privileged intents are required.

### 2. Get your Seerr API key
- Seerr → Settings → General → **API Key**. It must belong to an **admin** user
  (needed to look up users and submit requests on their behalf).

### 3. Configure
```bash
cp .env.example .env
# edit .env: DISCORD_TOKEN, SEERR_URL, SEERR_API_KEY
# set DISCORD_GUILD_ID to your server id for instant slash-command registration
```

### 4a. Run with Python
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m vaultrequestrr
```

### 4b. Run with Docker
```bash
docker compose up -d --build
```
The SQLite link store is persisted to `./data`.

To run the pre-built published image instead of building locally:
```bash
docker run -d --name vaultrequestrr --restart unless-stopped \
  -e DISCORD_TOKEN=... \
  -e SEERR_URL=http://10.10.0.10:5055 \
  -e SEERR_API_KEY=... \
  -e DISCORD_GUILD_ID=... \
  -v /path/to/data:/data \
  ghcr.io/vancityactivist/vaultrequestrr:latest
```

### 4c. Run on Unraid

The image is published to GHCR and there's a ready-made Unraid template at
[`unraid/vaultrequestrr.xml`](unraid/vaultrequestrr.xml).

1. **Make the GHCR package public** (one-time, after the first CI build):
   GitHub → your profile → Packages → `vaultrequestrr` → Package settings →
   Change visibility → Public. (Otherwise Unraid needs registry credentials.)
2. On Unraid: **Docker** tab → **Add Container**.
3. In the **Template** dropdown paste the template URL:
   `https://raw.githubusercontent.com/vancityactivist/vaultrequestrr/main/unraid/vaultrequestrr.xml`
   — or copy `unraid/vaultrequestrr.xml` to
   `/boot/config/plugins/dockerMan/templates-user/my-vaultrequestrr.xml` and pick it
   from the **User templates** section.
4. Fill in the fields the template exposes — **Discord Bot Token**, **Seerr URL**,
   **Seerr API Key**, and optionally **Discord Guild ID** — set the **Data Directory**
   (defaults to `/mnt/user/appdata/vaultrequestrr`), then **Apply**.

The bot needs no inbound ports or WebUI; it only makes outbound connections to
Discord and Seerr.

## Configuration reference

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | yes | — | Discord bot token |
| `DISCORD_GUILD_ID` | no | — | Register commands to one guild (instant). Blank = global (~1h) |
| `SEERR_URL` | yes | `http://localhost:5055` | Base URL of your Seerr instance |
| `SEERR_API_KEY` | yes | — | Seerr admin API key |
| `REQUIRE_LINKING` | no | `true` | Require Plex linking before the first request |
| `DEFAULT_SEERR_USER_ID` | no | — | Fallback user id when linking is disabled |
| `DATABASE_PATH` | no | `data/vaultrequestrr.sqlite3` | SQLite path for links |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

## Development

```bash
pip install -r requirements.txt pytest
pytest
```
