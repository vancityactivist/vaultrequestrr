# VaultRequestrr

[![CI](https://github.com/vancityactivist/vaultrequestrr/actions/workflows/ci.yml/badge.svg)](https://github.com/vancityactivist/vaultrequestrr/actions/workflows/ci.yml)
[![Docker](https://github.com/vancityactivist/vaultrequestrr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/vancityactivist/vaultrequestrr/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](Dockerfile)
[![GHCR](https://img.shields.io/badge/GHCR-vaultrequestrr-2496ED?logo=docker&logoColor=white)](https://github.com/vancityactivist/vaultrequestrr/pkgs/container/vaultrequestrr)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-vancityactivist-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/vancityactivist)

A Discord bot for requesting movies and TV shows through
[Seerr](https://github.com/seerr-team/seerr) (the unified successor to
Overseerr/Jellyseerr), with **self-service Plex account linking** so that each
user's requests are attributed to *their* Seerr account and their per-user
quotas/limits are respected.

📖 **Full documentation lives on the [wiki](https://github.com/vancityactivist/vaultrequestrr/wiki)** —
setup guides, the full configuration reference, feature deep-dives, and troubleshooting.

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
user.

## Features

- **Per-user attribution** — quotas and permissions come from each user's own
  Seerr account ([Account Linking](https://github.com/vancityactivist/vaultrequestrr/wiki/Account-Linking))
- **Requests with availability at a glance** — ✅ / 🟡 / ⏳ / 🕒 badges on search
  results and the season picker ([Requests and Approvals](https://github.com/vancityactivist/vaultrequestrr/wiki/Requests-and-Approvals))
- **Approval workflow** — pending requests DM the admins with persistent
  Approve/Decline buttons, plus `/pending` and a dashboard queue
- **Issue reporting with automated re-grab** — users report bad video/audio/subtitles;
  one button blocklists the bad release, grabs a replacement, and watches it through
  import ([Issue Reporting and Re-grab](https://github.com/vancityactivist/vaultrequestrr/wiki/Issue-Reporting-and-Re-grab))
- **DM notifications** — on availability, decline, and issue resolution — even for
  requests made in the Seerr web UI ([Notifications](https://github.com/vancityactivist/vaultrequestrr/wiki/Notifications))
- **Plex invites** — linked users can invite friends, with per-user caps
  ([Plex Invites](https://github.com/vancityactivist/vaultrequestrr/wiki/Plex-Invites))
- **Anime routing** — `/anime` targets dedicated anime Sonarr/Radarr instances
  ([Anime Requests](https://github.com/vancityactivist/vaultrequestrr/wiki/Anime-Requests))
- **Admin dashboard** — health, links, activity, approvals, issues, invites, live
  logs, and live-editable settings ([Admin Dashboard](https://github.com/vancityactivist/vaultrequestrr/wiki/Admin-Dashboard))

## Commands

| Command | Description |
| --- | --- |
| `/movie <title>` | Search for and request a movie |
| `/tv <title>` | Search for and request a TV show (with season selection) |
| `/anime <title>` | Search anime and route it to the anime library (when configured) |
| `/issue <title>` | Report a Video/Audio/Subtitle/Other problem with media on the server |
| `/invite` | Invite a friend to Plex by email (linked users; admin-enabled) |
| `/quota` | Show your remaining request quota and when it resets |
| `/myrequests` | List your recent requests and their current status |
| `/pending` | (Admin) Review, approve, and decline requests awaiting approval |
| `/linkstatus` | Show which Seerr account you're linked to |
| `/unlink` | Remove your link (you'll be asked again on the next request) |

## Quick start

1. Create a Discord bot at <https://discord.com/developers/applications>, copy the
   **bot token**, and invite it with the `applications.commands` and `bot` scopes
   (no privileged intents needed).
2. Grab a Seerr **admin** API key (Seerr → Settings → General → API Key).
3. Configure and run:

```bash
cp .env.example .env
# edit .env: DISCORD_TOKEN, SEERR_URL, SEERR_API_KEY
# set DISCORD_GUILD_ID for instant slash-command registration
docker compose up -d --build
```

Or run the published image directly:

```bash
docker run -d --name vaultrequestrr --restart unless-stopped \
  -e DISCORD_TOKEN=... \
  -e SEERR_URL=http://10.10.0.10:5055 \
  -e SEERR_API_KEY=... \
  -e DISCORD_GUILD_ID=... \
  -v /path/to/data:/data \
  ghcr.io/vancityactivist/vaultrequestrr:latest
```

Set `WEB_PASSWORD` (and publish port `5056`) to enable the admin dashboard.

More options — bare Python and a ready-made **Unraid template** — are on the
[Installation](https://github.com/vancityactivist/vaultrequestrr/wiki/Installation) page,
and every environment variable is documented in the
[Configuration reference](https://github.com/vancityactivist/vaultrequestrr/wiki/Configuration).

## Development

```bash
pip install -r requirements.txt pytest
pytest
```

See [Development](https://github.com/vancityactivist/vaultrequestrr/wiki/Development)
for the project layout and CI/release notes.
