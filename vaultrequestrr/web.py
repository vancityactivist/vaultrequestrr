"""Password-protected admin dashboard (aiohttp), served in the bot's event loop.

View: health, links, recent activity. Manage: unlink/remap a user and toggle
runtime settings. Auth is a single admin password (WEB_PASSWORD) with an
in-memory session cookie — appropriate for a LAN admin tool.
"""
from __future__ import annotations

import hmac
import html
import logging
import secrets
from datetime import datetime

from aiohttp import web

from .arr import ArrError, research_media
from .linking import LinkStatus
from .logbuffer import get_records
from .plex import PlexAuth, PlexError
from .seerr import (
    ISSUE_OPEN,
    ISSUE_RESOLVED,
    ISSUE_TYPE_LABELS,
    REQUEST_DECLINED,
    STATUS_AVAILABLE,
    SeerrClient,
    SeerrError,
)

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

logger = logging.getLogger(__name__)

COOKIE = "vr_session"


class WebDashboard:
    def __init__(self, bot) -> None:  # type: ignore[no-untyped-def]
        self.bot = bot
        self._runner: web.AppRunner | None = None
        self._sessions: set[str] = set()
        # Short-lived Plex login PINs in flight: pin_id -> code.
        self._plex_pins: dict[int, str] = {}

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        app.add_routes(
            [
                web.get("/login", self.login_page),
                web.post("/login", self.login_submit),
                web.get("/logout", self.logout),
                web.get("/", self.home),
                web.get("/links", self.links_page),
                web.post("/links/unlink", self.unlink_action),
                web.post("/links/remap", self.remap_action),
                web.post("/links/limit", self.limit_action),
                web.get("/invites", self.invites_page),
                web.get("/activity", self.activity_page),
                web.get("/issues", self.issues_page),
                web.post("/issues/resolve", self.issue_resolve_action),
                web.post("/issues/reopen", self.issue_reopen_action),
                web.post("/issues/research", self.issue_research_action),
                web.get("/logs", self.logs_page),
                web.get("/settings", self.settings_page),
                web.post("/settings", self.settings_action),
                web.post("/settings/connection", self.connection_action),
                web.post("/settings/plex/login", self.plex_login_action),
                web.get("/settings/plex/poll", self.plex_poll_action),
                web.post("/settings/plex/server", self.plex_server_action),
                web.post("/settings/plex", self.plex_invites_action),
                web.post("/settings/plex/disconnect", self.plex_disconnect_action),
            ]
        )
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="0.0.0.0", port=self.bot.config.web_port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- auth --------------------------------------------------------------

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if request.path in ("/login",):
            return await handler(request)
        token = request.cookies.get(COOKIE)
        if token and token in self._sessions:
            return await handler(request)
        raise web.HTTPFound("/login")

    async def login_page(self, request: web.Request) -> web.Response:
        error = "Incorrect password." if request.query.get("error") else ""
        body = f"""
        <form method="post" action="/login" class="card login">
          <h1>VaultRequestrr</h1>
          <p class="muted">Admin dashboard</p>
          <input type="password" name="password" placeholder="Password" autofocus required>
          <button type="submit">Sign in</button>
          <p class="error">{error}</p>
        </form>
        """
        return _html(_layout("Sign in", body, nav=False))

    async def login_submit(self, request: web.Request) -> web.Response:
        data = await request.post()
        password = str(data.get("password", ""))
        if self.bot.config.web_password and hmac.compare_digest(
            password, self.bot.config.web_password
        ):
            token = secrets.token_urlsafe(32)
            self._sessions.add(token)
            response = web.HTTPFound("/")
            response.set_cookie(COOKIE, token, httponly=True, samesite="Lax", max_age=86400)
            raise response
        raise web.HTTPFound("/login?error=1")

    async def logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(COOKIE)
        if token:
            self._sessions.discard(token)
        response = web.HTTPFound("/login")
        response.del_cookie(COOKIE)
        raise response

    # -- pages -------------------------------------------------------------

    async def home(self, request: web.Request) -> web.Response:
        discord_ok = self.bot.is_ready()
        try:
            await self.bot.seerr.test_connection()
            seerr_ok, seerr_msg = True, "Connected"
        except SeerrError as exc:
            seerr_ok, seerr_msg = False, str(exc)

        links = await self.bot.store.list_links()
        pending = await self.bot.store.pending_tracked()
        msg = _flash(request)

        plex_ok = self.bot.plex is not None
        if not plex_ok:
            plex_msg = "Not connected"
        elif (await self.bot.store.get_setting("plex_invites_enabled")) == "1":
            plex_msg = "Invites on"
        else:
            plex_msg = "Invites off"
        invites_sent = await self.bot.store.invites_sent_total()

        body = f"""
        {msg}
        <div class="grid">
          <div class="card stat"><div class="num">{len(links)}</div><div class="muted">Linked users</div></div>
          <div class="card stat"><div class="num">{len(pending)}</div><div class="muted">Pending requests</div></div>
          <div class="card stat"><div class="num">{invites_sent}</div><div class="muted"><a href="/invites">Invites sent</a></div></div>
          <div class="card stat"><div class="num">{_dot(discord_ok)} Discord</div><div class="muted">{'Ready' if discord_ok else 'Connecting…'}</div></div>
          <div class="card stat"><div class="num">{_dot(seerr_ok)} Seerr</div><div class="muted">{html.escape(seerr_msg)}</div></div>
          <div class="card stat"><div class="num">{_dot(plex_ok)} Plex</div><div class="muted">{html.escape(plex_msg)}</div></div>
        </div>
        <p class="muted small">Manage the Seerr/Plex connections and bot behaviour on the
          <a href="/settings">Settings</a> page.</p>
        """
        return _html(_layout("Dashboard", body))

    async def links_page(self, request: web.Request) -> web.Response:
        links = await self.bot.store.list_links()
        global_limit = await self.bot.store.get_setting("plex_invite_limit") or "3"
        rows = ""
        for link in links:
            who = html.escape(link.plex_username or link.email or "—")
            used = await self.bot.store.count_invites(link.discord_id)
            effective = link.invite_limit if link.invite_limit is not None else global_limit
            override_val = "" if link.invite_limit is None else str(link.invite_limit)
            placeholder = f"default ({html.escape(str(global_limit))})"
            rows += f"""
            <tr>
              <td><code>{html.escape(link.discord_id)}</code></td>
              <td>{who}</td>
              <td>{link.seerr_user_id}</td>
              <td>{used} / {html.escape(str(effective))}
                <form method="post" action="/links/limit" class="inline">
                  <input type="hidden" name="discord_id" value="{html.escape(link.discord_id)}">
                  <input type="number" name="limit" min="0" value="{override_val}" placeholder="{placeholder}" style="width:7em">
                  <button>Set</button>
                </form>
              </td>
              <td>{html.escape(link.linked_at[:19])}</td>
              <td class="actions">
                <form method="post" action="/links/unlink" onsubmit="return confirm('Unlink this user?')">
                  <input type="hidden" name="discord_id" value="{html.escape(link.discord_id)}">
                  <button class="danger">Unlink</button>
                </form>
                <form method="post" action="/links/remap" class="inline">
                  <input type="hidden" name="discord_id" value="{html.escape(link.discord_id)}">
                  <input type="text" name="plex_identity" placeholder="new plex username/email" required>
                  <button>Remap</button>
                </form>
              </td>
            </tr>"""
        if not links:
            rows = '<tr><td colspan="6" class="muted">No linked users yet.</td></tr>'

        body = f"""
        {_flash(request)}
        <div class="card">
          <h2>Linked accounts ({len(links)})</h2>
          <table>
            <thead><tr><th>Discord ID</th><th>Plex/Seerr</th><th>Seerr ID</th><th>Invites (used / limit)</th><th>Linked</th><th>Actions</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
          <p class="muted small">Leave an invite limit blank to use the global default
            ({html.escape(str(global_limit))}, set on the <a href="/settings">Settings</a> page).</p>
        </div>
        """
        return _html(_layout("Links", body))

    async def activity_page(self, request: web.Request) -> web.Response:
        items = await self.bot.store.recent_tracked(100)
        rows = ""
        for it in items:
            rows += f"""
            <tr>
              <td>{html.escape((it.title or '—'))}</td>
              <td>{html.escape(it.media_type)}</td>
              <td><code>{html.escape(it.discord_id)}</code></td>
              <td>{_status_badge(it)}</td>
              <td>{html.escape((it.created_at or '')[:19])}</td>
            </tr>"""
        if not items:
            rows = '<tr><td colspan="5" class="muted">No activity yet.</td></tr>'
        body = f"""
        <div class="card">
          <h2>Recent requests ({len(items)})</h2>
          <table>
            <thead><tr><th>Title</th><th>Type</th><th>Requester</th><th>Status</th><th>When</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """
        return _html(_layout("Activity", body))

    async def invites_page(self, request: web.Request) -> web.Response:
        items = await self.bot.store.recent_invites(200)
        rows = ""
        for it in items:
            link = await self.bot.store.get(it.inviter_discord_id)
            who = it.inviter_discord_id
            if link is not None:
                who = link.plex_username or link.email or it.inviter_discord_id
            badge = (
                '<span class="badge ok">Sent</span>'
                if it.status == "sent"
                else '<span class="badge bad">Failed</span>'
            )
            rows += f"""
            <tr>
              <td>{html.escape(who)}</td>
              <td>{html.escape(it.invited_email)}</td>
              <td>{badge}</td>
              <td>{html.escape((it.created_at or '')[:19])}</td>
            </tr>"""
        if not items:
            rows = '<tr><td colspan="4" class="muted">No invites sent yet.</td></tr>'
        body = f"""
        <div class="card">
          <h2>Plex invites ({len(items)})</h2>
          <table>
            <thead><tr><th>Inviter</th><th>Invited email</th><th>Status</th><th>When</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
          <p class="muted small">Per-user invite caps are set on the <a href="/links">Links</a> page.</p>
        </div>
        """
        return _html(_layout("Invites", body))

    async def issues_page(self, request: web.Request) -> web.Response:
        items = await self.bot.store.recent_issues(100)

        # Overlay current status from Seerr so the page reflects resolutions that
        # happened outside the bot (best-effort; fall back to the tracked status).
        live: dict[int, int | None] = {}
        try:
            for issue in await self.bot.seerr.list_issues():
                live[issue.id] = issue.status
        except SeerrError as exc:
            logger.debug("Could not load live issue statuses: %s", exc)

        rows = ""
        for it in items:
            status = live.get(it.issue_id, it.status)
            resolved = status == ISSUE_RESOLVED
            badge = (
                '<span class="badge ok">Resolved</span>'
                if resolved
                else '<span class="badge pend">Open</span>'
            )
            type_label = ISSUE_TYPE_LABELS.get(it.issue_type or 0, "—")
            who = it.discord_id
            link = await self.bot.store.get(it.discord_id)
            if link is not None:
                who = link.plex_username or link.email or it.discord_id
            action = "reopen" if resolved else "resolve"
            action_label = "Reopen" if resolved else "Resolve"
            title = it.title or "—"
            if it.problem_season is not None and it.problem_episode is not None:
                title += f" S{it.problem_season:02d}E{it.problem_episode:02d}"
            rows += f"""
            <tr>
              <td>{html.escape(title)}</td>
              <td>{html.escape(type_label)}</td>
              <td>{html.escape(who)}</td>
              <td>{badge}</td>
              <td>{html.escape((it.created_at or '')[:19])}</td>
              <td class="actions">
                <form method="post" action="/issues/{action}">
                  <input type="hidden" name="issue_id" value="{it.issue_id}">
                  <button>{action_label}</button>
                </form>
                <form method="post" action="/issues/research" onsubmit="return confirm('Delete the current file and search for a replacement?')">
                  <input type="hidden" name="issue_id" value="{it.issue_id}">
                  <button class="warn">Re-search</button>
                </form>
              </td>
            </tr>"""
        if not items:
            rows = '<tr><td colspan="6" class="muted">No issues reported yet.</td></tr>'

        body = f"""
        {_flash(request)}
        <div class="card">
          <h2>Reported issues ({len(items)})</h2>
          <table>
            <thead><tr><th>Title</th><th>Type</th><th>Reporter</th><th>Status</th><th>When</th><th>Actions</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        """
        return _html(_layout("Issues", body))

    async def logs_page(self, request: web.Request) -> web.Response:
        level = request.query.get("level", "").upper()
        auto = request.query.get("auto") == "1"
        min_level = _LEVELS.get(level, 0)

        lines = ""
        for r in reversed(get_records()):  # newest first
            if _LEVELS.get(r.level, 0) < min_level:
                continue
            ts = datetime.fromtimestamp(r.created).strftime("%m-%d %H:%M:%S")
            lines += (
                f'<div class="logline lvl-{html.escape(r.level)}">'
                f'<span class="ts">{ts}</span>'
                f'<span class="lvl">{html.escape(r.level)}</span>'
                f'<span class="lname">{html.escape(_short_name(r.name))}</span>'
                f'<span class="lmsg">{html.escape(r.message)}</span></div>'
            )
        if not lines:
            lines = '<p class="muted">No log records yet.</p>'

        def flink(label: str, value: str) -> str:
            q = f"?level={value}" if value else "?"
            if auto:
                q += ("&" if "=" in q else "") + "auto=1"
            active = "active" if level == value else ""
            return f'<a class="chip {active}" href="/logs{q}">{label}</a>'

        filters = "".join(
            flink(lbl, val)
            for lbl, val in (("All", ""), ("Info", "INFO"), ("Warning", "WARNING"), ("Error", "ERROR"))
        )
        auto_href = "/logs" + (f"?level={level}" if level else "")
        auto_toggle = (
            f'<a class="chip {"active" if auto else ""}" href="{auto_href}{"&" if level else "?"}auto={"0" if auto else "1"}">Auto-refresh</a>'
        )
        refresh_script = "<script>setTimeout(function(){location.reload()},10000)</script>" if auto else ""

        body = f"""
        <div class="card">
          <div class="logbar">
            <h2>Logs</h2>
            <div class="filters">{filters}{auto_toggle}<a class="chip" href="/logs{('?level=' + level) if level else ''}">↻ Refresh</a></div>
          </div>
          <div class="logs">{lines}</div>
        </div>
        {refresh_script}
        """
        return _html(_layout("Logs", body))

    async def settings_page(self, request: web.Request) -> web.Response:
        rt = self.bot.runtime
        seerr_url = await self.bot.store.get_setting("seerr_url") or self.bot.config.seerr_url
        key_set = bool(
            await self.bot.store.get_setting("seerr_api_key") or self.bot.config.seerr_api_key
        )
        key_placeholder = "•••••••• (unchanged — leave blank to keep)" if key_set else "Seerr API key"

        # Read-only view of the download managers Seerr already knows about.
        arr_rows = ""
        try:
            instances = []
            for kind in ("radarr", "sonarr"):
                instances.extend(await self.bot.seerr.list_service_instances(kind))
            for inst in instances:
                tags = []
                if inst.is_default:
                    tags.append('<span class="badge ok">default</span>')
                if inst.is_4k:
                    tags.append('<span class="badge pend">4K</span>')
                arr_rows += f"""
                <tr>
                  <td>{html.escape((inst.kind or '').title())}</td>
                  <td>{html.escape(inst.name or '—')}</td>
                  <td><code>{html.escape(inst.url)}</code></td>
                  <td>{html.escape(inst.profile or '—')}</td>
                  <td>{' '.join(tags)}</td>
                </tr>"""
            arr_note = (
                '<tr><td colspan="5" class="muted">Seerr has no Radarr/Sonarr configured.</td></tr>'
                if not arr_rows
                else ""
            )
        except SeerrError as exc:
            arr_rows = ""
            arr_note = f'<tr><td colspan="5" class="muted">Couldn\'t reach Seerr: {html.escape(str(exc))}</td></tr>'

        plex_card = await self._plex_card()

        body = f"""
        {_flash(request)}
        <div class="card">
          <h2>Seerr connection</h2>
          <form method="post" action="/settings/connection">
            <label class="field">Seerr URL
              <input type="text" name="seerr_url" value="{html.escape(seerr_url)}" placeholder="http://host:5055" required>
            </label>
            <label class="field">API key
              <input type="password" name="seerr_api_key" placeholder="{html.escape(key_placeholder)}" autocomplete="off">
            </label>
            <button type="submit">Test &amp; save</button>
          </form>
          <p class="muted small">The connection is validated before saving, then applied
            immediately. Stored in the database and kept across restarts (environment
            variables are only the first-run default).</p>
        </div>

        <div class="card">
          <h2>Bot settings</h2>
          <form method="post" action="/settings">
            <label class="check"><input type="checkbox" name="require_linking" {_checked(rt.require_linking)}> Require Plex linking before first request</label>
            <label class="check"><input type="checkbox" name="notify_on_available" {_checked(rt.notify_on_available)}> DM users when media becomes available</label>
            <label class="check"><input type="checkbox" name="notify_on_declined" {_checked(rt.notify_on_declined)}> DM users when a request is declined</label>
            <label class="check"><input type="checkbox" name="notify_on_issue_resolved" {_checked(rt.notify_on_issue_resolved)}> DM users when their reported issue is resolved</label>
            <label class="field">Log level
              <select name="log_level">{_log_options(rt.log_level)}</select>
            </label>
            <button type="submit">Save settings</button>
          </form>
          <p class="muted small">These apply immediately but reset to env defaults on restart.</p>
        </div>

        {plex_card}

        <div class="card">
          <h2>Download managers <span class="muted small">(from Seerr — read only)</span></h2>
          <table>
            <thead><tr><th>Service</th><th>Name</th><th>URL</th><th>Profile</th><th></th></tr></thead>
            <tbody>{arr_rows}{arr_note}</tbody>
          </table>
          <p class="muted small">Radarr/Sonarr are configured in Seerr; VaultRequestrr reads
            them from there for the issue <strong>Re-search</strong> action.</p>
        </div>
        """
        return _html(_layout("Settings", body))

    # -- actions -----------------------------------------------------------

    async def unlink_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        discord_id = str(data.get("discord_id", ""))
        if discord_id:
            await self.bot.linker.unlink(discord_id)
        raise web.HTTPFound("/links?msg=" + _q("Unlinked."))

    async def remap_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        discord_id = str(data.get("discord_id", ""))
        identity = str(data.get("plex_identity", "")).strip()
        if not (discord_id and identity):
            raise web.HTTPFound("/links?msg=" + _q("Missing fields."))
        result = await self.bot.linker.link(discord_id, identity)
        if result.status is LinkStatus.LINKED:
            who = result.user.plex_username or result.user.email or result.user.id
            raise web.HTTPFound("/links?msg=" + _q(f"Remapped to {who}."))
        if result.status is LinkStatus.NOT_FOUND:
            raise web.HTTPFound("/links?msg=" + _q("No matching Seerr user found."))
        raise web.HTTPFound("/links?msg=" + _q("Seerr error during remap."))

    async def limit_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        discord_id = str(data.get("discord_id", ""))
        raw = str(data.get("limit", "")).strip()
        if not discord_id:
            raise web.HTTPFound("/links?msg=" + _q("Missing user."))
        limit = int(raw) if raw.isdigit() else None  # blank clears the override
        await self.bot.store.set_invite_limit(discord_id, limit)
        note = "Invite limit cleared (uses default)." if limit is None else f"Invite limit set to {limit}."
        raise web.HTTPFound("/links?msg=" + _q(note))

    async def issue_resolve_action(self, request: web.Request) -> web.Response:
        await self._set_issue_status(request, resolved=True)

    async def issue_reopen_action(self, request: web.Request) -> web.Response:
        await self._set_issue_status(request, resolved=False)

    async def _set_issue_status(self, request: web.Request, *, resolved: bool) -> None:
        data = await request.post()
        try:
            issue_id = int(str(data.get("issue_id", "")))
        except ValueError:
            raise web.HTTPFound("/issues?msg=" + _q("Missing issue id."))
        try:
            await self.bot.seerr.update_issue_status(issue_id, resolved=resolved)
        except SeerrError as exc:
            raise web.HTTPFound("/issues?msg=" + _q(f"Seerr error: {exc}"))
        await self.bot.store.mark_issue(
            issue_id, status=ISSUE_RESOLVED if resolved else ISSUE_OPEN
        )
        verb = "resolved" if resolved else "reopened"
        raise web.HTTPFound("/issues?msg=" + _q(f"Issue {verb}."))

    async def issue_research_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        try:
            issue_id = int(str(data.get("issue_id", "")))
        except ValueError:
            raise web.HTTPFound("/issues?msg=" + _q("Missing issue id."))
        tracked = await self.bot.store.get_tracked_issue(issue_id)
        if tracked is None or tracked.tmdb_id is None or not tracked.media_type:
            raise web.HTTPFound("/issues?msg=" + _q("Can't re-search this issue."))
        try:
            result = await research_media(
                self.bot.seerr,
                tracked.media_type,
                tracked.tmdb_id,
                season=tracked.problem_season,
                episode=tracked.problem_episode,
            )
        except ArrError as exc:
            raise web.HTTPFound("/issues?msg=" + _q(str(exc)))
        raise web.HTTPFound("/issues?msg=" + _q(result))

    async def connection_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        url = str(data.get("seerr_url", "")).strip().rstrip("/")
        new_key = str(data.get("seerr_api_key", "")).strip()
        if not url:
            raise web.HTTPFound("/settings?msg=" + _q("Seerr URL is required."))

        # Blank key field => keep the current effective key.
        effective_key = (
            new_key
            or await self.bot.store.get_setting("seerr_api_key")
            or self.bot.config.seerr_api_key
        )

        # Validate before persisting — don't break a working connection on a typo.
        probe = SeerrClient(url, effective_key)
        try:
            await probe.test_connection()
        except SeerrError as exc:
            raise web.HTTPFound("/settings?msg=" + _q(f"Couldn't connect: {exc}"))
        finally:
            await probe.aclose()

        await self.bot.store.set_setting("seerr_url", url)
        if new_key:
            await self.bot.store.set_setting("seerr_api_key", new_key)
        await self.bot.apply_seerr_connection(url, effective_key)
        raise web.HTTPFound("/settings?msg=" + _q("Seerr connection saved."))

    # -- Plex invites ------------------------------------------------------

    async def _plex_card(self) -> str:
        """Render the Plex Invites card in one of three states."""
        token = await self.bot.store.get_setting("plex_token")
        machine_id = await self.bot.store.get_setting("plex_machine_id")

        if not token:
            # State A: not authenticated — offer Login with Plex.
            inner = """
            <p class="muted small">Connect your Plex account so members can invite friends
              with <code>/invite</code>. We use Plex's own "Login with Plex" — no token to copy.</p>
            <button type="button" id="plexLogin">Login with Plex</button>
            <p class="muted small" id="plexLoginMsg"></p>
            """ + _PLEX_LOGIN_JS
        elif not machine_id:
            # State B: authenticated, pick a server.
            inner = await self._plex_server_picker(token)
        else:
            # State C: fully connected — invite controls.
            inner = await self._plex_invite_controls()

        return f'<div class="card"><h2>Plex Invites</h2>{inner}</div>'

    async def _plex_server_picker(self, token: str) -> str:
        client_id = await self.bot.plex_client_id()
        auth = PlexAuth()
        try:
            servers = await auth.list_servers(token, client_id)
        except PlexError as exc:
            return (
                f'<p class="muted small">Connected to Plex, but couldn\'t list your servers: '
                f'{html.escape(str(exc))}</p>'
                '<form method="post" action="/settings/plex/disconnect">'
                '<button class="danger">Disconnect Plex</button></form>'
            )
        finally:
            await auth.aclose()

        if not servers:
            return (
                '<p class="muted small">No owned Plex servers found on this account.</p>'
                '<form method="post" action="/settings/plex/disconnect">'
                '<button class="danger">Disconnect Plex</button></form>'
            )
        options = "".join(
            f'<option value="{html.escape(s.machine_id)}|{html.escape(s.name)}">'
            f'{html.escape(s.name)}</option>'
            for s in servers
        )
        return f"""
        <p class="muted small">Authenticated with Plex. Choose which server to share:</p>
        <form method="post" action="/settings/plex/server">
          <label class="field">Server
            <select name="server">{options}</select>
          </label>
          <button type="submit">Use this server</button>
        </form>
        """

    async def _plex_invite_controls(self) -> str:
        server_name = await self.bot.store.get_setting("plex_server_name") or "your Plex server"
        enabled = (await self.bot.store.get_setting("plex_invites_enabled")) == "1"
        limit = await self.bot.store.get_setting("plex_invite_limit") or "3"
        selected = {
            part.strip()
            for part in (await self.bot.store.get_setting("plex_shared_libraries") or "").split(",")
            if part.strip()
        }

        lib_rows = ""
        if self.bot.plex is not None:
            try:
                for lib in await self.bot.plex.list_libraries():
                    checked = _checked(str(lib.section_id) in selected)
                    lib_rows += (
                        f'<label class="check"><input type="checkbox" name="library" '
                        f'value="{lib.section_id}" {checked}> {html.escape(lib.title)} '
                        f'<span class="muted small">({html.escape(lib.kind)})</span></label>'
                    )
            except PlexError as exc:
                lib_rows = f'<p class="muted small">Couldn\'t load libraries: {html.escape(str(exc))}</p>'
        if not lib_rows:
            lib_rows = '<p class="muted small">No libraries found. Friends would get access to all libraries.</p>'

        return f"""
        <p class="muted small">Connected to <strong>{html.escape(server_name)}</strong>.
          <form method="post" action="/settings/plex/disconnect" class="inline" style="display:inline">
            <button class="danger">Disconnect</button>
          </form>
        </p>
        <form method="post" action="/settings/plex">
          <label class="check"><input type="checkbox" name="enabled" {_checked(enabled)}> Enable <code>/invite</code> for linked users</label>
          <label class="field">Invites per user
            <input type="number" name="limit" min="0" value="{html.escape(limit)}">
          </label>
          <p class="muted small">Libraries to share with invited friends:</p>
          {lib_rows}
          <button type="submit">Save invite settings</button>
        </form>
        """

    async def plex_login_action(self, request: web.Request) -> web.Response:
        client_id = await self.bot.plex_client_id()
        auth = PlexAuth()
        try:
            pin_id, code, auth_url = await auth.create_pin(client_id)
        except PlexError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        finally:
            await auth.aclose()
        self._plex_pins[pin_id] = code
        return web.json_response({"pin_id": pin_id, "auth_url": auth_url})

    async def plex_poll_action(self, request: web.Request) -> web.Response:
        try:
            pin_id = int(request.query.get("pin_id", ""))
        except ValueError:
            return web.json_response({"error": "bad pin"}, status=400)
        client_id = await self.bot.plex_client_id()
        code = self._plex_pins.get(pin_id)
        auth = PlexAuth()
        try:
            token = await auth.check_pin(pin_id, client_id, code)
        except PlexError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        finally:
            await auth.aclose()
        if not token:
            return web.json_response({"authenticated": False})
        await self.bot.store.set_setting("plex_token", token)
        self._plex_pins.pop(pin_id, None)
        return web.json_response({"authenticated": True})

    async def plex_server_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        machine_id, _, name = str(data.get("server", "")).partition("|")
        token = await self.bot.store.get_setting("plex_token")
        if not (machine_id and token):
            raise web.HTTPFound("/settings?msg=" + _q("Pick a server first."))
        await self.bot.store.set_setting("plex_machine_id", machine_id)
        await self.bot.store.set_setting("plex_server_name", name or machine_id)
        await self.bot.apply_plex_connection(token, machine_id)
        raise web.HTTPFound("/settings?msg=" + _q("Plex server connected."))

    async def plex_invites_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        await self.bot.store.set_setting(
            "plex_invites_enabled", "1" if "enabled" in data else "0"
        )
        limit = str(data.get("limit", "3")).strip()
        if limit.isdigit():
            await self.bot.store.set_setting("plex_invite_limit", limit)
        libraries = ",".join(
            str(v) for v in data.getall("library", []) if str(v).strip().isdigit()
        )
        await self.bot.store.set_setting("plex_shared_libraries", libraries)
        raise web.HTTPFound("/settings?msg=" + _q("Invite settings saved."))

    async def plex_disconnect_action(self, request: web.Request) -> web.Response:
        for key in ("plex_token", "plex_machine_id", "plex_server_name"):
            await self.bot.store.set_setting(key, "")
        if self.bot.plex is not None:
            await self.bot.plex.aclose()
            self.bot.plex = None
        raise web.HTTPFound("/settings?msg=" + _q("Plex disconnected."))

    async def settings_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        rt = self.bot.runtime
        rt.require_linking = "require_linking" in data
        rt.notify_on_available = "notify_on_available" in data
        rt.notify_on_declined = "notify_on_declined" in data
        rt.notify_on_issue_resolved = "notify_on_issue_resolved" in data
        level = str(data.get("log_level", rt.log_level)).upper()
        if level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            rt.log_level = level
            logging.getLogger("vaultrequestrr").setLevel(level)
        raise web.HTTPFound("/settings?msg=" + _q("Settings saved."))


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def _html(markup: str) -> web.Response:
    return web.Response(text=markup, content_type="text/html")


def _layout(title: str, body: str, *, nav: bool = True) -> str:
    navbar = (
        """
        <nav class="nav">
          <a class="brand" href="/">VaultRequestrr</a>
          <div class="links">
            <a href="/">Dashboard</a><a href="/links">Links</a><a href="/activity">Activity</a><a href="/issues">Issues</a><a href="/invites">Invites</a><a href="/logs">Logs</a><a href="/settings">Settings</a>
            <a href="/logout" class="muted">Sign out</a>
          </div>
        </nav>
        """
        if nav
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · VaultRequestrr</title>
<style>{_CSS}</style>
</head><body>{navbar}<main>{body}</main></body></html>"""


_CSS = """
:root{--bg:#0f1115;--card:#1a1d24;--line:#2a2e38;--fg:#e6e8ee;--muted:#8b91a0;--accent:#5865f2;--ok:#3ba55d;--bad:#ed4245}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px}
.nav{display:flex;justify-content:space-between;align-items:center;padding:14px 24px;background:var(--card);border-bottom:1px solid var(--line)}
.nav .brand{font-weight:700;color:var(--fg);text-decoration:none}
.nav .links a{color:var(--fg);text-decoration:none;margin-left:18px}.nav .links a:hover{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin:16px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:16px 0}
.stat .num{font-size:26px;font-weight:700}
h1,h2{margin:0 0 12px}.muted{color:var(--muted)}.small{font-size:13px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:13px}
code{background:#0c0e12;padding:2px 6px;border-radius:6px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:14px}
button:hover{filter:brightness(1.1)}button.danger{background:var(--bad)}button.warn{background:#e3a008}
input,select{background:#0c0e12;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:14px}
.actions{display:flex;gap:8px;flex-wrap:wrap}.inline{display:flex;gap:6px}
label.check{display:block;margin:8px 0}label.field{display:block;margin:12px 0}
.login{max-width:340px;margin:80px auto;text-align:center}.login input{width:100%;margin:8px 0}.login button{width:100%}
.error{color:var(--bad);min-height:18px}.flash{background:#23314a;border:1px solid var(--accent);padding:10px 14px;border-radius:8px;margin:8px 0}
.badge{padding:2px 8px;border-radius:999px;font-size:12px}.badge.ok{background:rgba(59,165,93,.2);color:var(--ok)}
.badge.bad{background:rgba(237,66,69,.2);color:var(--bad)}.badge.pend{background:rgba(136,145,160,.2);color:var(--muted)}
.logbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:13px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);color:var(--fg);text-decoration:none}
.chip:hover{border-color:var(--accent)}.chip.active{background:var(--accent);border-color:var(--accent)}
.logs{margin-top:12px;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:65vh;overflow:auto;background:#0c0e12;border:1px solid var(--line);border-radius:8px;padding:10px}
.logline{display:grid;grid-template-columns:96px 64px 150px 1fr;gap:8px;padding:2px 0;border-bottom:1px solid rgba(42,46,56,.5);white-space:pre-wrap;word-break:break-word}
.logline .ts{color:var(--muted)}.logline .lname{color:var(--muted)}
.logline .lvl{font-weight:600}
.lvl-WARNING .lvl{color:#e3a008}.lvl-ERROR .lvl,.lvl-CRITICAL .lvl{color:var(--bad)}.lvl-DEBUG{opacity:.7}
.lvl-ERROR .lmsg,.lvl-CRITICAL .lmsg{color:#f7a6a7}
"""


_PLEX_LOGIN_JS = """
<script>
(function(){
  var btn = document.getElementById('plexLogin');
  var msg = document.getElementById('plexLoginMsg');
  if(!btn) return;
  btn.addEventListener('click', async function(){
    btn.disabled = true; msg.textContent = 'Opening Plex…';
    try {
      var r = await fetch('/settings/plex/login', {method:'POST'});
      var d = await r.json();
      if(d.error){ msg.textContent = d.error; btn.disabled = false; return; }
      var popup = window.open(d.auth_url, 'plexAuth', 'width=600,height=700');
      msg.textContent = 'Waiting for you to authorise in Plex…';
      var tries = 0;
      var timer = setInterval(async function(){
        tries++;
        if(tries > 150){ clearInterval(timer); msg.textContent = 'Timed out — try again.'; btn.disabled = false; return; }
        try {
          var pr = await fetch('/settings/plex/poll?pin_id=' + d.pin_id);
          var pd = await pr.json();
          if(pd.authenticated){ clearInterval(timer); if(popup) popup.close(); location.reload(); }
        } catch(e) {}
      }, 2000);
    } catch(e) {
      msg.textContent = 'Could not start Plex login.'; btn.disabled = false;
    }
  });
})();
</script>
"""


def _short_name(name: str) -> str:
    # "discord.gateway" -> "discord.gateway"; trim very long names.
    return name if len(name) <= 28 else name[:27] + "…"


def _dot(ok: bool) -> str:
    color = "var(--ok)" if ok else "var(--bad)"
    return f'<span style="color:{color}">●</span>'


def _checked(value: bool) -> str:
    return "checked" if value else ""


def _log_options(current: str) -> str:
    out = ""
    for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
        sel = "selected" if level == current else ""
        out += f'<option value="{level}" {sel}>{level}</option>'
    return out


def _status_badge(tracked) -> str:  # type: ignore[no-untyped-def]
    if tracked.notified_available or tracked.media_status == STATUS_AVAILABLE:
        return '<span class="badge ok">Available</span>'
    if tracked.notified_declined or tracked.request_status == REQUEST_DECLINED:
        return '<span class="badge bad">Declined</span>'
    return '<span class="badge pend">Pending</span>'


def _flash(request: web.Request) -> str:
    msg = request.query.get("msg")
    return f'<div class="flash">{html.escape(msg)}</div>' if msg else ""


def _q(text: str) -> str:
    from urllib.parse import quote

    return quote(text)
