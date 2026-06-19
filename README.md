# VaultRequestrr

[![CI](https://github.com/vancityactivist/vaultrequestrr/actions/workflows/ci.yml/badge.svg)](https://github.com/vancityactivist/vaultrequestrr/actions/workflows/ci.yml)
[![Docker](https://github.com/vancityactivist/vaultrequestrr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/vancityactivist/vaultrequestrr/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](Dockerfile)
[![GHCR](https://img.shields.io/badge/GHCR-vaultrequestrr-2496ED?logo=docker&logoColor=white)](https://github.com/vancityactivist/vaultrequestrr/pkgs/container/vaultrequestrr)

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

Search results and the season picker show availability at a glance (✅ available,
🟡 partial, ⏳ processing, 🕒 requested), and the Request button greys out when
the selected media/seasons are already present. Results beyond 25 paginate.

### Reporting issues

Users run `/issue <title>` to report a problem (Video / Audio / Subtitle /
Other) with media **already on the server**. The bot searches movies and TV,
limits results to in-library titles, applies the same link gate as requests, then
collects a short description and files it to Seerr's issue tracker. For TV the
form also asks which **season and episode** is affected, so issues are pinned to
a single episode. Because Seerr's API attributes every API-key issue to the admin
account, the real reporter is recorded in the issue message and tracked locally so
the dashboard and resolution DMs know who filed it.

### Inviting friends to Plex

Linked users can run `/invite` to bring a friend onto the Plex server. The bot
asks for the friend's Plex email and issues a real Plex share, so **Plex emails
the friend** the invite — Discord only triggers it. Invites are gated three ways:
an admin must connect Plex and enable invites, the inviter must have linked their
own account, and each user has a configurable cap (default **3**, overridable
per-user on the Links page). Connect Plex from the Settings page with **Login
with Plex** (PIN OAuth — no token to copy), then pick which server and which
libraries to share. Sent invites are listed on the dashboard **Invites** page.

### Notifications

The bot **DMs the requester** when their request becomes available or is declined,
and **DMs the reporter** when their issue is marked resolved. Only requests and
issues made through the bot are tracked.

Delivery is driven by a **Seerr webhook** for near-instant DMs. Set a webhook secret
— either the `WEBHOOK_SECRET` env var or, more conveniently, the **Seerr webhook** card
on the dashboard Settings page (which also shows the exact URL to paste) — then in Seerr
enable **Settings → Notifications → Webhook** and point it at:

```
http://<vaultrequestrr-host>:<WEB_PORT>/webhook/seerr?token=<WEBHOOK_SECRET>
```

(The default JSON payload works as-is — no template needed.) A background poller
still runs as a reconciliation backstop on `POLL_INTERVAL_SECONDS` (default `600`),
so notifications are never lost if a webhook is missed; without the webhook
configured, the poller alone delivers them, just more slowly.

### Admin dashboard

If `WEB_PASSWORD` is set, a small web dashboard is served on `WEB_PORT` (default
`5056`): health (Discord/Seerr/Plex status), linked accounts (with unlink/remap
and per-user invite caps), recent request activity, reported issues (with
resolve/reopen and **re-search** actions), sent Plex invites, a live log viewer
(level filter + auto-refresh), and a **Settings** page. Sign in with the password.

The Settings page lets you edit the **Seerr connection** (URL + API key) — the
connection is validated before saving, applied immediately without a restart,
and persisted to the database (the `SEERR_URL` / `SEERR_API_KEY` env vars are
only the first-run default). It also exposes a **Seerr webhook** card (set/clear the
webhook secret and copy the ready-to-paste URL), the bot behaviour toggles, a
**Plex Invites** section (Login with Plex, server + library selection, enable
toggle and per-user invite cap), and a **Radarr / Sonarr connections** manager.

VaultRequestrr talks to Radarr/Sonarr **directly with its own credentials**.
Add one or more instances (URL + API key, with optional 4K and default flags)
on the Settings page — each is validated before saving and stored in the
database. Seerr is still used to *locate* which instance holds a given title
(it resolves the internal movie/series id), but all data reads and actions run
against your configured connections. This unlocks richer media tooling:

* **Media details** — from an issue's **Details** action, see the current file's
  quality/size/languages, monitored state, and live download-queue progress,
  read straight from the arr.
* **Re-search** acts on a bad download: it deletes the current file in
  Radarr/Sonarr and triggers a fresh search for a replacement — for a movie, the
  movie file; for TV, just the reported episode's file. (Radarr/Sonarr can't
  "blocklist but keep the file" for an already-imported release, so removing the
  file is what reliably forces a new grab.)
* **Manual search** — run an interactive indexer search and **grab a specific
  release** from the candidate list, instead of relying on auto-search.

Commands:

| Command | Description |
| --- | --- |
| `/movie <title>` | Search for and request a movie |
| `/tv <title>` | Search for and request a TV show (with season selection) |
| `/issue <title>` | Report a Video/Audio/Subtitle/Other problem with media on the server |
| `/invite` | Invite a friend to Plex by email (linked users; admin-enabled) |
| `/quota` | Show your remaining request quota and when it resets |
| `/myrequests` | List your recent requests and their current status |
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

1. **Install the template into Unraid's user-templates folder.** The Add Container
   **Template** field is a dropdown — you cannot paste a URL into it, so the XML has
   to live in `/boot/config/plugins/dockerMan/templates-user/` first. From the Unraid
   web terminal:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-vaultrequestrr.xml \
     https://raw.githubusercontent.com/vancityactivist/vaultrequestrr/main/unraid/vaultrequestrr.xml
   ```
   (Or copy `unraid/vaultrequestrr.xml` onto the `flash` share at
   `config/plugins/dockerMan/templates-user/my-vaultrequestrr.xml`.) The `my-` prefix
   and this folder are what make it appear in the dropdown.
2. On Unraid: **Docker** tab → **Add Container** → in the **Template** dropdown, under
   **User templates**, select **VaultRequestrr**.
3. Fill in the fields the template exposes — **Discord Bot Token**, **Seerr URL**,
   **Seerr API Key**, and optionally **Discord Guild ID** — set the **Data Directory**
   (defaults to `/mnt/user/appdata/vaultrequestrr`), then **Apply**.

The bot makes outbound connections to Discord and Seerr. The only inbound port
is the optional admin dashboard (`5056`), which is served only when you set a
**Dashboard Password** in the template.

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
| `POLL_INTERVAL_SECONDS` | no | `600` | Reconciliation poll interval; backstop to the webhook (0 disables) |
| `NOTIFY_ON_AVAILABLE` | no | `true` | DM requester when media becomes available |
| `NOTIFY_ON_DECLINED` | no | `true` | DM requester when a request is declined |
| `NOTIFY_ON_ISSUE_RESOLVED` | no | `true` | DM reporter when their issue is resolved |
| `WEB_PASSWORD` | no | — | Set to enable the admin dashboard |
| `WEB_PORT` | no | `5056` | Port the dashboard listens on |
| `WEBHOOK_SECRET` | no | — | Shared secret for the inbound Seerr webhook (blank = endpoint disabled) |

## Development

```bash
pip install -r requirements.txt pytest
pytest
```
