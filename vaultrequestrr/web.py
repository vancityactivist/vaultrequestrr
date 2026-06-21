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
from pathlib import Path

from aiohttp import web

from .arr import ArrClient, ArrError
from .linking import LinkStatus
from .logbuffer import get_records
from .plex import PlexAuth, PlexError
from .seerr import (
    ISSUE_OPEN,
    ISSUE_RESOLVED,
    ISSUE_TYPE_LABELS,
    REQUEST_APPROVED,
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
                web.get("/icon.png", self.icon),
                web.get("/", self.home),
                web.get("/links", self.links_page),
                web.post("/links/unlink", self.unlink_action),
                web.post("/links/remap", self.remap_action),
                web.post("/links/limit", self.limit_action),
                web.get("/invites", self.invites_page),
                web.get("/activity", self.activity_page),
                web.get("/activity/detail", self.activity_detail),
                web.get("/approvals", self.approvals_page),
                web.post("/approvals/approve", self.approval_approve_action),
                web.post("/approvals/decline", self.approval_decline_action),
                web.get("/issues", self.issues_page),
                web.post("/issues/resolve", self.issue_resolve_action),
                web.post("/issues/reopen", self.issue_reopen_action),
                web.post("/issues/research", self.issue_research_action),
                web.get("/media", self.media_page),
                web.post("/media/research", self.media_research_action),
                web.get("/media/search", self.media_search_page),
                web.post("/media/grab", self.media_grab_action),
                web.get("/logs", self.logs_page),
                web.get("/settings", self.settings_page),
                web.post("/settings", self.settings_action),
                web.post("/settings/connection", self.connection_action),
                web.post("/settings/webhook", self.webhook_action),
                web.post("/settings/admins", self.admins_action),
                web.post("/settings/issues", self.issues_action),
                web.post("/settings/anime", self.anime_action),
                web.post("/settings/arr/add", self.arr_add_action),
                web.post("/settings/arr/update", self.arr_update_action),
                web.post("/settings/arr/delete", self.arr_delete_action),
                web.post("/settings/plex/login", self.plex_login_action),
                web.get("/settings/plex/poll", self.plex_poll_action),
                web.post("/settings/plex/server", self.plex_server_action),
                web.post("/settings/plex", self.plex_invites_action),
                web.post("/settings/plex/disconnect", self.plex_disconnect_action),
                web.post("/webhook/seerr", self.seerr_webhook),
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
        # The webhook authenticates with its own shared secret, not the session.
        # The logo is public so it loads on the login page too.
        if request.path in ("/login", "/webhook/seerr", "/icon.png"):
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

    async def icon(self, request: web.Request) -> web.StreamResponse:
        """Serve the VaultRequestrr logo (used for the favicon and sidebar brand)."""
        if not _LOGO_PATH.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(
            _LOGO_PATH, headers={"Cache-Control": "public, max-age=86400"}
        )

    # -- webhook -----------------------------------------------------------

    async def seerr_webhook(self, request: web.Request) -> web.Response:
        """Inbound Seerr webhook: a trigger to re-check one request/issue now.

        We don't trust the payload's state — we just learn *which* request or
        issue changed and re-run the same finalisation the poller would, so
        notifications are idempotent and arrive in seconds instead of minutes.
        Authenticated by a shared secret (?token= or X-Webhook-Token); the
        endpoint is inert until WEBHOOK_SECRET is set.
        """
        secret = await self._effective_webhook_secret()
        provided = request.query.get("token") or request.headers.get("X-Webhook-Token", "")
        if not secret or not hmac.compare_digest(provided, secret):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - malformed body
            return web.json_response({"error": "invalid json"}, status=400)

        nt = str(payload.get("notification_type") or "")
        if nt.startswith("ISSUE"):
            issue_id = _webhook_int((payload.get("issue") or {}).get("issue_id"))
            if issue_id is not None:
                await self.bot.notifications.check_issue(issue_id)
        elif nt.startswith("MEDIA"):
            request_id = _webhook_int((payload.get("request") or {}).get("request_id"))
            if request_id is not None:
                await self.bot.notifications.check_request(request_id)
        # TEST_NOTIFICATION and anything else: acknowledge without acting.
        return web.json_response({"ok": True})

    async def _effective_webhook_secret(self) -> str:
        return await self.bot.effective_webhook_secret()

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
          <div class="card stat"><span class="tileico">{_icon("users", 22)}</span><div class="num">{len(links)}</div><div class="muted">Linked users</div></div>
          <div class="card stat"><span class="tileico">{_icon("clock", 22)}</span><div class="num">{len(pending)}</div><div class="muted">Pending requests</div></div>
          <div class="card stat"><span class="tileico">{_icon("mail", 22)}</span><div class="num">{invites_sent}</div><div class="muted"><a href="/invites">Invites sent</a></div></div>
          <div class="card stat"><span class="tileico">{_icon("server", 22)}</span><div class="num">{_dot(discord_ok)} Discord</div><div class="muted">{'Ready' if discord_ok else 'Connecting…'}</div></div>
          <div class="card stat"><span class="tileico">{_icon("server", 22)}</span><div class="num">{_dot(seerr_ok)} Seerr</div><div class="muted">{html.escape(seerr_msg)}</div></div>
          <div class="card stat"><span class="tileico">{_icon("server", 22)}</span><div class="num">{_dot(plex_ok)} Plex</div><div class="muted">{html.escape(plex_msg)}</div></div>
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
            rows = _empty_row(6, "No linked users yet.", "link")

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
        # One query for all links so each row can show the Seerr id (Discord on hover).
        link_by_discord = {link.discord_id: link for link in await self.bot.store.list_links()}
        rows = ""
        for it in items:
            rows += f"""
            <tr>
              <td>{html.escape((it.title or '—'))}</td>
              <td>{html.escape(it.media_type)}</td>
              <td>{_requester_cell(it.discord_id, link_by_discord.get(it.discord_id))}</td>
              <td>{_status_badge(it)}</td>
              <td>{html.escape((it.created_at or '')[:19])}</td>
              <td class="actions"><button class="detailtoggle" data-id="{it.request_id}" aria-expanded="false">Details</button></td>
            </tr>
            <tr class="detailrow" id="detail-{it.request_id}" hidden>
              <td colspan="6"><div class="detailbody muted small">Loading…</div></td>
            </tr>"""
        if not items:
            rows = _empty_row(6, "No activity yet.", "activity")
        body = f"""
        <div class="card">
          <h2>Recent requests ({len(items)})</h2>
          <table>
            <thead><tr><th>Title</th><th>Type</th><th>Requester</th><th>Status</th><th>When</th><th></th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        {_ACTIVITY_JS}
        """
        return _html(_layout("Activity", body))

    async def activity_detail(self, request: web.Request) -> web.Response:
        """HTML fragment with richer media details, lazy-loaded by the Activity page."""
        try:
            request_id = int(request.query.get("id", ""))
        except ValueError:
            return web.Response(text="Bad request id.", content_type="text/html")
        tracked = await self.bot.store.get_tracked(request_id)
        if tracked is None or tracked.tmdb_id is None or not tracked.media_type:
            return web.Response(
                text='<p class="muted small">No details available.</p>',
                content_type="text/html",
            )

        summary = None
        try:
            summary = await self.bot.seerr.get_media_summary(
                tracked.media_type, tracked.tmdb_id
            )
        except SeerrError:
            pass

        # Size / location come straight from the arr; best-effort (needs an arr
        # connection and the title to be resolvable there).
        detail = None
        try:
            detail = await self.bot.arr.media_detail(tracked.media_type, tracked.tmdb_id)
        except (ArrError, SeerrError):
            pass

        return web.Response(
            text=_render_activity_detail(tracked, summary, detail),
            content_type="text/html",
        )

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
            rows = _empty_row(4, "No invites sent yet.", "mail")
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

    async def approvals_page(self, request: web.Request) -> web.Response:
        try:
            pending = await self.bot.seerr.list_pending_requests()
        except SeerrError as exc:
            pending = []
            logger.debug("Could not load pending requests: %s", exc)

        rows = ""
        for req in pending:
            title = await self._resolve_request_title(req)
            kind = "📺 TV" if req.media_type == "tv" else "🎬 Movie"
            seasons = ", ".join(str(s) for s in req.seasons) or "—"
            rows += f"""
            <tr>
              <td>{html.escape(title)}</td>
              <td>{kind}</td>
              <td>{html.escape(req.requested_by_name or '—')}</td>
              <td>{html.escape(seasons)}</td>
              <td>{html.escape((req.created_at or '')[:19])}</td>
              <td class="actions">
                <form method="post" action="/approvals/approve">
                  <input type="hidden" name="request_id" value="{req.id}">
                  <button>Approve</button>
                </form>
                <form method="post" action="/approvals/decline">
                  <input type="hidden" name="request_id" value="{req.id}">
                  <button class="danger">Decline</button>
                </form>
              </td>
            </tr>"""
        if not pending:
            rows = _empty_row(6, "Nothing awaiting approval.", "approvals")

        body = f"""
        {_flash(request)}
        <div class="card">
          <h2>Pending approvals ({len(pending)})</h2>
          <table>
            <thead><tr><th>Title</th><th>Type</th><th>Requested by</th><th>Seasons</th><th>When</th><th>Actions</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
          <p class="muted small">Requests from users without auto-approve wait here. Admins
            are also DM'd (and the approvals channel pinged) when these come in.</p>
        </div>
        """
        return _html(_layout("Approvals", body))

    async def _resolve_request_title(self, req) -> str:  # type: ignore[no-untyped-def]
        tracked = await self.bot.store.get_tracked(req.id)
        if tracked is not None and tracked.title:
            return tracked.title
        if req.media_type and req.tmdb_id is not None:
            try:
                return (await self.bot.seerr.get_title(req.media_type, req.tmdb_id)) or "—"
            except SeerrError:
                return "—"
        return "—"

    async def approval_approve_action(self, request: web.Request) -> web.Response:
        await self._decide_request(request, approve=True)

    async def approval_decline_action(self, request: web.Request) -> web.Response:
        await self._decide_request(request, approve=False)

    async def _decide_request(self, request: web.Request, *, approve: bool) -> None:
        data = await request.post()
        try:
            request_id = int(str(data.get("request_id", "")))
        except ValueError:
            raise web.HTTPFound("/approvals?msg=" + _q("Missing request id."))
        try:
            if approve:
                await self.bot.seerr.approve_request(request_id)
            else:
                await self.bot.seerr.decline_request(request_id)
        except SeerrError as exc:
            raise web.HTTPFound("/approvals?msg=" + _q(f"Seerr error: {exc}"))
        await self.bot.store.mark_tracked(
            request_id,
            request_status=REQUEST_APPROVED if approve else REQUEST_DECLINED,
            notified_declined=None if approve else True,
        )
        verb = "approved" if approve else "declined"
        raise web.HTTPFound("/approvals?msg=" + _q(f"Request {verb}."))

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
            details_link = ""
            if it.tmdb_id and it.media_type:
                href = f"/media?type={html.escape(it.media_type)}&tmdb={it.tmdb_id}"
                if it.problem_season is not None and it.problem_episode is not None:
                    href += f"&season={it.problem_season}&episode={it.problem_episode}"
                details_link = f'<a class="btn ghost" href="{href}">Details</a>'
            rows += f"""
            <tr>
              <td>{html.escape(title)}</td>
              <td>{html.escape(type_label)}</td>
              <td>{html.escape(who)}</td>
              <td>{badge}</td>
              <td>{html.escape((it.created_at or '')[:19])}</td>
              <td class="actions">
                {details_link}
                <form method="post" action="/issues/{action}">
                  <input type="hidden" name="issue_id" value="{it.issue_id}">
                  <button>{action_label}</button>
                </form>
                <form method="post" action="/issues/research" onsubmit="return confirm('Find a replacement release and grab it, replacing the current file? The issue is resolved only if a release is grabbed.')">
                  <input type="hidden" name="issue_id" value="{it.issue_id}">
                  <button class="warn">Re-grab</button>
                </form>
              </td>
            </tr>"""
        if not items:
            rows = _empty_row(6, "No issues reported yet.", "issue")

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

        webhook_secret = await self._effective_webhook_secret()
        webhook_card = self._webhook_card(request, webhook_secret)
        admins_card = await self._admins_card()
        issues_card = await self._issues_card()
        anime_card = await self._anime_card()
        arr_card = await self._arr_card()
        plex_card = await self._plex_card()

        seerr_card = f"""
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
        </div>"""

        bot_card = f"""
        <div class="card">
          <h2>Bot behaviour</h2>
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
        </div>"""

        # Grouped into tabs so the page reads as sections rather than one long
        # scroll. Falls back to a flat stack when JS is off (see _SETTINGS_TABS_JS).
        tabs = [
            ("general", "General", "settings", seerr_card + bot_card),
            ("approvals", "Approvals & Issues", "approvals", admins_card + issues_card),
            ("notifications", "Notifications", "mail", webhook_card),
            ("services", "Services", "server", arr_card + anime_card),
            ("plex", "Plex", "link", plex_card),
        ]
        subnav = "".join(
            f'<a class="subtab" data-tab="{tab}" href="#{tab}">{_icon(icon, 16)}<span>{label}</span></a>'
            for tab, label, icon, _ in tabs
        )
        panels = "".join(
            f'<section class="panel" id="{tab}" data-panel="{tab}">{cards}</section>'
            for tab, _, _, cards in tabs
        )

        body = f"""
        {_flash(request)}
        <div class="settings" id="settings">
          <nav class="subnav">{subnav}</nav>
          {panels}
        </div>
        {_SETTINGS_TABS_JS}
        """
        return _html(_layout("Settings", body))

    def _webhook_card(self, request: web.Request, secret: str) -> str:
        """Webhook secret + the ready-to-paste Seerr webhook URL."""
        if secret:
            url = f"{request.scheme}://{request.host}/webhook/seerr?token={secret}"
            status = (
                '<p class="muted small">Enabled. In Seerr, add a <strong>Webhook</strong> '
                "notification agent (Settings → Notifications → Webhook) pointing at:</p>"
                f'<p><code>{html.escape(url)}</code></p>'
            )
            placeholder = "•••••••• (unchanged — leave blank to keep)"
        else:
            status = (
                '<p class="muted small">Disabled — the webhook endpoint returns 401 until a '
                "secret is set. Set one here, then point Seerr's Webhook agent at "
                f'<code>{html.escape(request.scheme)}://{html.escape(request.host)}/webhook/seerr?token=YOUR_SECRET</code>.</p>'
            )
            placeholder = "Webhook secret"
        return f"""
        <div class="card">
          <h2>Seerr webhook</h2>
          {status}
          <form method="post" action="/settings/webhook">
            <label class="field">Webhook secret
              <input type="password" name="webhook_secret" placeholder="{html.escape(placeholder)}" autocomplete="off">
            </label>
            <label class="check"><input type="checkbox" name="clear"> Disable webhook (clear the secret)</label>
            <button type="submit">Save</button>
            <button type="submit" name="action" value="generate" class="chip">Generate</button>
          </form>
          <p class="muted small">Drives instant request/issue notifications. Stored in the
            database and kept across restarts (the <code>WEBHOOK_SECRET</code> env var is only
            the first-run default).</p>
        </div>
        """

    async def _admins_card(self) -> str:
        """Manage approver Discord ids + the optional approvals channel."""
        ids = sorted(await self.bot.admin_ids())
        ids_value = ", ".join(str(i) for i in ids)
        channel = await self.bot.approvals_channel_id()
        channel_value = "" if channel is None else str(channel)
        return f"""
        <div class="card">
          <h2>Request approvals</h2>
          <form method="post" action="/settings/admins">
            <label class="field">Approver Discord IDs
              <input type="text" name="admin_discord_ids" value="{html.escape(ids_value)}" placeholder="e.g. 1234567890, 9876543210">
            </label>
            <label class="field">Approvals channel ID (optional)
              <input type="text" name="approvals_channel_id" value="{html.escape(channel_value)}" placeholder="Discord channel id to post pending requests to">
            </label>
            <button type="submit">Save</button>
          </form>
          <p class="muted small">These users can run <code>/pending</code>, use the
            Approve/Decline buttons, and are DM'd when a request needs approval. If a
            channel id is set, pending requests are also posted there. Stored in the
            database (the <code>ADMIN_DISCORD_IDS</code> / <code>APPROVALS_CHANNEL_ID</code>
            env vars are only the first-run default).</p>
        </div>
        """

    async def _issues_card(self) -> str:
        """Manage who is notified about reported issues + the optional issue channel."""
        # Show the raw configured values (blank = "use the approval settings").
        ids_value = await self.bot.store.get_setting("issue_notify_discord_ids") or ""
        channel_value = await self.bot.store.get_setting("issues_channel_id") or ""
        return f"""
        <div class="card">
          <h2>Issue notifications</h2>
          <form method="post" action="/settings/issues">
            <label class="field">Issue handler Discord IDs
              <input type="text" name="issue_notify_discord_ids" value="{html.escape(ids_value)}" placeholder="leave blank to use the approver list above">
            </label>
            <label class="field">Issue channel ID (optional)
              <input type="text" name="issues_channel_id" value="{html.escape(channel_value)}" placeholder="leave blank to use the approvals channel">
            </label>
            <button type="submit">Save</button>
          </form>
          <p class="muted small">When a user files an issue, these people are DM'd a card with
            <strong>Re-grab</strong> / <strong>Resolve</strong> buttons (and may act on them); if a
            channel id is set, the card is posted there too. Leave either field blank to reuse the
            approver list / approvals channel above. Stored in the database.</p>
        </div>
        """

    async def _anime_card(self) -> str:
        """Route /anime requests to dedicated Sonarr/Radarr instances in Seerr."""
        async def _val(key: str, env_default: object) -> str:
            value = await self.bot._anime_setting(key, env_default)
            return "" if value is None else str(value)

        cfg = self.bot.config
        sonarr_server = await _val("anime_sonarr_server_id", cfg.anime_sonarr_server_id)
        sonarr_profile = await _val("anime_sonarr_profile_id", cfg.anime_sonarr_profile_id)
        sonarr_root = await _val("anime_sonarr_root_folder", cfg.anime_sonarr_root_folder)
        radarr_server = await _val("anime_radarr_server_id", cfg.anime_radarr_server_id)
        radarr_profile = await _val("anime_radarr_profile_id", cfg.anime_radarr_profile_id)
        radarr_root = await _val("anime_radarr_root_folder", cfg.anime_radarr_root_folder)

        sonarr_select = await self._anime_server_select("sonarr", sonarr_server)
        radarr_select = await self._anime_server_select("radarr", radarr_server)

        return f"""
        <div class="card">
          <h2>Anime routing</h2>
          <p class="muted small">Pick the Seerr instance <code>/anime</code> requests go to —
            series to Sonarr, films to Radarr. Choose <em>Disabled</em> to turn <code>/anime</code>
            off for that media type. Leave the advanced overrides blank to let Seerr apply that
            instance's own anime quality profile / root folder and (for series) the anime series type.</p>
          <form method="post" action="/settings/anime">
            <h3 class="subhead">Anime series → Sonarr</h3>
            <label class="field">Sonarr instance
              <select name="anime_sonarr_server_id">{sonarr_select}</select>
            </label>
            <h3 class="subhead">Anime films → Radarr</h3>
            <label class="field">Radarr instance
              <select name="anime_radarr_server_id">{radarr_select}</select>
            </label>
            <details class="advanced">
              <summary>Advanced overrides</summary>
              <p class="muted small">Force a specific quality profile / root folder instead of the
                instance's anime defaults. Profile is the Seerr profile id.</p>
              <div class="formrow">
                <label class="field">Sonarr profile ID
                  <input type="text" name="anime_sonarr_profile_id" value="{html.escape(sonarr_profile)}" placeholder="leave blank">
                </label>
                <label class="field">Sonarr root folder
                  <input type="text" name="anime_sonarr_root_folder" value="{html.escape(sonarr_root)}" placeholder="e.g. /tv/anime">
                </label>
              </div>
              <div class="formrow">
                <label class="field">Radarr profile ID
                  <input type="text" name="anime_radarr_profile_id" value="{html.escape(radarr_profile)}" placeholder="leave blank">
                </label>
                <label class="field">Radarr root folder
                  <input type="text" name="anime_radarr_root_folder" value="{html.escape(radarr_root)}" placeholder="e.g. /movies/anime">
                </label>
              </div>
            </details>
            <button type="submit">Save anime routing</button>
          </form>
          <p class="muted small">Stored in the database (the <code>ANIME_SONARR_*</code> /
            <code>ANIME_RADARR_*</code> env vars are only the first-run default). Note: whether a
            series is added as <em>anime</em> (absolute numbering) is decided by Seerr's own anime
            detection, not the bot — a title Seerr doesn't recognise as anime lands as a standard series.</p>
        </div>
        """

    async def _anime_server_select(self, kind: str, current: str) -> str:
        """<option>s for an anime instance picker, sourced from Seerr's configured services."""
        try:
            instances = await self.bot.seerr.list_service_instances(kind)
        except SeerrError:
            instances = []

        selected_ids = set()
        options = [
            f'<option value=""{"" if current else " selected"}>— Disabled —</option>'
        ]
        for inst in instances:
            if inst.id is None:
                continue
            value = str(inst.id)
            selected = " selected" if value == current else ""
            if selected:
                selected_ids.add(value)
            tags = []
            if inst.is_default:
                tags.append("default")
            if inst.is_4k:
                tags.append("4K")
            suffix = f" · {', '.join(tags)}" if tags else ""
            label = f"{inst.name or 'Unnamed'} (id {value}{suffix})"
            options.append(f'<option value="{html.escape(value)}"{selected}>{html.escape(label)}</option>')

        # Preserve a saved id that Seerr no longer lists (or couldn't be loaded) so
        # saving the form doesn't silently wipe the existing routing.
        if current and current not in selected_ids:
            options.append(
                f'<option value="{html.escape(current)}" selected>id {html.escape(current)} (not found)</option>'
            )
        return "".join(options)

    async def _arr_card(self) -> str:
        """Editable Radarr/Sonarr connection manager (our own credentials)."""
        instances = await self.bot.store.list_arr_instances()
        rows = ""
        for inst in instances:
            flags = []
            if inst.is_default:
                flags.append('<span class="badge ok">default</span>')
            if inst.is_4k:
                flags.append('<span class="badge pend">4K</span>')
            rows += f"""
            <tr>
              <td>{html.escape(inst.label)}</td>
              <td>{html.escape(inst.kind.title())}</td>
              <td><code>{html.escape(inst.base_url)}</code></td>
              <td>{' '.join(flags) or '<span class="muted small">—</span>'}</td>
              <td class="actions">
                <details class="editbox">
                  <summary class="chip">Edit</summary>
                  <form method="post" action="/settings/arr/update" class="arrform">
                    <input type="hidden" name="id" value="{html.escape(inst.id)}">
                    <label class="field">Label
                      <input type="text" name="label" value="{html.escape(inst.label)}" required>
                    </label>
                    <label class="field">URL
                      <input type="text" name="base_url" value="{html.escape(inst.base_url)}" required>
                    </label>
                    <label class="field">API key
                      <input type="password" name="api_key" placeholder="•••••••• (unchanged — leave blank to keep)" autocomplete="off">
                    </label>
                    <label class="check"><input type="checkbox" name="is_4k" {_checked(inst.is_4k)}> 4K instance</label>
                    <label class="check"><input type="checkbox" name="is_default" {_checked(inst.is_default)}> Default {html.escape(inst.kind)}</label>
                    <button type="submit">Test &amp; save</button>
                  </form>
                </details>
                <form method="post" action="/settings/arr/delete" onsubmit="return confirm('Delete this connection?')">
                  <input type="hidden" name="id" value="{html.escape(inst.id)}">
                  <button class="danger">Delete</button>
                </form>
              </td>
            </tr>"""
        if not instances:
            rows = _empty_row(5, "No Radarr/Sonarr connections yet — add one below.", "server")

        return f"""
        <div class="card">
          <h2>Radarr / Sonarr connections</h2>
          <p class="muted small">VaultRequestrr talks to these directly with its own
            credentials for re-search, media details, and manual search. Seerr is only
            used to locate which instance holds a title.</p>
          <table>
            <thead><tr><th>Label</th><th>Type</th><th>URL</th><th>Flags</th><th>Actions</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
          <h3 class="subhead">Add a connection</h3>
          <form method="post" action="/settings/arr/add" class="arrform">
            <div class="formrow">
              <label class="field">Type
                <select name="kind"><option value="radarr">Radarr</option><option value="sonarr">Sonarr</option></select>
              </label>
              <label class="field">Label
                <input type="text" name="label" placeholder="e.g. Radarr 4K" required>
              </label>
            </div>
            <label class="field">URL
              <input type="text" name="base_url" placeholder="http://host:7878" required>
            </label>
            <label class="field">API key
              <input type="password" name="api_key" placeholder="Radarr/Sonarr API key" autocomplete="off" required>
            </label>
            <label class="check"><input type="checkbox" name="is_4k"> 4K instance</label>
            <label class="check"><input type="checkbox" name="is_default"> Default for this type</label>
            <button type="submit">Test &amp; add</button>
          </form>
        </div>
        """

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
            result = await self.bot.arr.research(
                tracked.media_type,
                tracked.tmdb_id,
                season=tracked.problem_season,
                episode=tracked.problem_episode,
            )
        except (ArrError, SeerrError) as exc:
            raise web.HTTPFound("/issues?msg=" + _q(str(exc)))

        # Only resolve the issue when a replacement was actually grabbed.
        message = result.message
        if result.grabbed:
            try:
                await self.bot.seerr.update_issue_status(issue_id, resolved=True)
                await self.bot.store.mark_issue(issue_id, status=ISSUE_RESOLVED)
                message += " Issue resolved."
            except SeerrError as exc:
                message += f" (couldn't resolve the issue in Seerr: {exc})"
        raise web.HTTPFound("/issues?msg=" + _q(message))

    # -- media detail (direct Radarr/Sonarr view) --------------------------

    @staticmethod
    def _media_query(media_type: str, tmdb_id: int,
                     season: int | None, episode: int | None) -> str:
        q = f"type={media_type}&tmdb={tmdb_id}"
        if season is not None and episode is not None:
            q += f"&season={season}&episode={episode}"
        return q

    async def media_page(self, request: web.Request) -> web.Response:
        media_type = request.query.get("type", "")
        try:
            tmdb_id = int(request.query.get("tmdb", ""))
        except ValueError:
            raise web.HTTPFound("/issues?msg=" + _q("Missing media id."))
        if media_type not in ("movie", "tv"):
            raise web.HTTPFound("/issues?msg=" + _q("Unknown media type."))
        season = _opt_int(request.query.get("season"))
        episode = _opt_int(request.query.get("episode"))
        try:
            detail = await self.bot.arr.media_detail(
                media_type, tmdb_id, season=season, episode=episode
            )
        except (ArrError, SeerrError) as exc:
            body = (
                f'{_flash(request)}<div class="card"><h2>Media</h2>'
                f'<p class="muted">{html.escape(str(exc))}</p>'
                '<a class="btn ghost" href="/issues">Back to issues</a></div>'
            )
            return _html(_layout("Media", body))
        return _html(_layout("Media", self._render_media(request, detail)))

    def _render_media(self, request: web.Request, d: dict) -> str:
        inst = d["instance"]
        flag = ' <span class="badge pend">4K</span>' if inst.is_4k else ""
        mono = (
            '<span class="badge ok">Monitored</span>' if d["monitored"]
            else '<span class="badge pend">Unmonitored</span>'
        )
        sub = f' · S{d["season"]:02d}E{d["episode"]:02d}' if d["episode"] else ""

        if d["has_file"]:
            langs = ", ".join(d["languages"]) or "—"
            file_html = f"""
            <div class="grid">
              <div class="card stat"><div class="num">{html.escape(d["quality"] or "—")}</div><div class="muted">Quality</div></div>
              <div class="card stat"><div class="num">{_fmt_size(d["size"])}</div><div class="muted">Size on disk</div></div>
              <div class="card stat"><div class="num small">{html.escape(langs)}</div><div class="muted">Languages</div></div>
            </div>"""
        else:
            file_html = '<p class="muted">No file on disk yet.</p>'

        if d["queue"]:
            qrows = ""
            for q in d["queue"]:
                prog = f'{q["progress"]}%' if q["progress"] is not None else "—"
                qrows += (
                    f'<tr><td>{html.escape(q["title"] or "—")}</td>'
                    f'<td>{html.escape(str(q["status"] or "—"))}</td>'
                    f'<td>{prog}</td><td>{html.escape(str(q["timeleft"] or "—"))}</td></tr>'
                )
            queue_html = (
                '<table><thead><tr><th>Release</th><th>Status</th><th>Progress</th>'
                f'<th>Time left</th></tr></thead><tbody>{qrows}</tbody></table>'
            )
        else:
            queue_html = '<p class="muted small">Nothing downloading right now.</p>'

        season_episode = _hidden_se(d["season"], d["episode"])

        return f"""
        {_flash(request)}
        <div class="card">
          <h2>{html.escape(d["title"] or "—")}{sub}</h2>
          <p class="muted small">Managed by <strong>{html.escape(inst.label)}</strong>{flag} · {mono}</p>
          {file_html}
        </div>
        <div class="card">
          <h2>Download queue</h2>
          {queue_html}
        </div>
        <div class="card">
          <h2>Actions</h2>
          <div class="actions">
            <form method="post" action="/media/research" onsubmit="return confirm('Find a replacement release and grab it, replacing the current file?')">
              <input type="hidden" name="type" value="{html.escape(d["media_type"])}">
              <input type="hidden" name="tmdb" value="{d["tmdb_id"]}">
              {season_episode}
              <button class="warn">Find &amp; re-grab</button>
            </form>
            <a class="btn" href="/media/search?{self._media_query(d["media_type"], d["tmdb_id"], d["season"], d["episode"])}">Manual search</a>
            <a class="btn ghost" href="/issues">Back to issues</a>
          </div>
        </div>
        """

    async def media_research_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        media_type = str(data.get("type", ""))
        try:
            tmdb_id = int(str(data.get("tmdb", "")))
        except ValueError:
            raise web.HTTPFound("/issues?msg=" + _q("Missing media id."))
        season = _opt_int(data.get("season"))
        episode = _opt_int(data.get("episode"))
        back = "/media?" + self._media_query(media_type, tmdb_id, season, episode)
        try:
            result = await self.bot.arr.research(
                media_type, tmdb_id, season=season, episode=episode
            )
        except (ArrError, SeerrError) as exc:
            raise web.HTTPFound(back + "&msg=" + _q(str(exc)))
        raise web.HTTPFound(back + "&msg=" + _q(result.message))

    async def media_search_page(self, request: web.Request) -> web.Response:
        media_type = request.query.get("type", "")
        try:
            tmdb_id = int(request.query.get("tmdb", ""))
        except ValueError:
            raise web.HTTPFound("/issues?msg=" + _q("Missing media id."))
        season = _opt_int(request.query.get("season"))
        episode = _opt_int(request.query.get("episode"))
        back = "/media?" + self._media_query(media_type, tmdb_id, season, episode)
        try:
            releases = await self.bot.arr.releases(
                media_type, tmdb_id, season=season, episode=episode
            )
        except (ArrError, SeerrError) as exc:
            body = (
                f'<div class="card"><h2>Manual search</h2>'
                f'<p class="muted">{html.escape(str(exc))}</p>'
                f'<a class="btn ghost" href="{back}">Back</a></div>'
            )
            return _html(_layout("Manual search", body))

        rows = ""
        for r in releases:
            seeders = "—" if r["seeders"] is None else str(r["seeders"])
            grab = ""
            if r["guid"] and r["indexer_id"] is not None:
                grab = f"""
                <form method="post" action="/media/grab">
                  <input type="hidden" name="type" value="{html.escape(media_type)}">
                  <input type="hidden" name="tmdb" value="{tmdb_id}">
                  {_hidden_se(season, episode)}
                  <input type="hidden" name="guid" value="{html.escape(str(r['guid']))}">
                  <input type="hidden" name="indexer_id" value="{r['indexer_id']}">
                  <button>Grab</button>
                </form>"""
            rows += f"""
            <tr>
              <td>{html.escape(r["title"] or "—")}</td>
              <td>{html.escape(r["quality"] or "—")}</td>
              <td>{_fmt_size(r["size"])}</td>
              <td>{seeders}</td>
              <td>{html.escape(r["indexer"] or "—")}</td>
              <td class="actions">{grab}</td>
            </tr>"""
        if not releases:
            rows = _empty_row(6, "No releases found.", "logs")

        body = f"""
        {_flash(request)}
        <div class="card">
          <h2>Manual search <span class="muted small">({len(releases)} releases)</span></h2>
          <table>
            <thead><tr><th>Release</th><th>Quality</th><th>Size</th><th>Seeders</th><th>Indexer</th><th></th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
          <p style="margin-top:14px"><a class="btn ghost" href="{back}">Back to media</a></p>
        </div>
        """
        return _html(_layout("Manual search", body))

    async def media_grab_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        media_type = str(data.get("type", ""))
        try:
            tmdb_id = int(str(data.get("tmdb", "")))
        except ValueError:
            raise web.HTTPFound("/issues?msg=" + _q("Missing media id."))
        season = _opt_int(data.get("season"))
        episode = _opt_int(data.get("episode"))
        guid = str(data.get("guid", ""))
        indexer_id = _opt_int(data.get("indexer_id"))
        back = "/media?" + self._media_query(media_type, tmdb_id, season, episode)
        if not guid or indexer_id is None:
            raise web.HTTPFound(back + "&msg=" + _q("Missing release."))
        try:
            await self.bot.arr.grab(media_type, tmdb_id, guid, indexer_id)
        except (ArrError, SeerrError) as exc:
            raise web.HTTPFound(back + "&msg=" + _q(str(exc)))
        raise web.HTTPFound(back + "&msg=" + _q("Release sent to the download client."))

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

    async def webhook_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        if data.get("action") == "generate":
            await self.bot.store.set_setting("webhook_secret", secrets.token_urlsafe(32))
            raise web.HTTPFound("/settings?msg=" + _q("Generated a new webhook secret."))
        if data.get("clear"):
            await self.bot.store.set_setting("webhook_secret", "")
            raise web.HTTPFound("/settings?msg=" + _q("Webhook disabled."))

        secret = str(data.get("webhook_secret", "")).strip()
        if not secret:
            # Blank with no clear request => keep the current secret unchanged.
            raise web.HTTPFound("/settings?msg=" + _q("No change to the webhook secret."))
        await self.bot.store.set_setting("webhook_secret", secret)
        raise web.HTTPFound("/settings?msg=" + _q("Webhook secret saved."))

    async def admins_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        # Normalise to a clean comma-separated list of integer ids.
        raw = str(data.get("admin_discord_ids", ""))
        ids = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip().isdigit()]
        await self.bot.store.set_setting("admin_discord_ids", ",".join(ids))

        channel = str(data.get("approvals_channel_id", "")).strip()
        await self.bot.store.set_setting(
            "approvals_channel_id", channel if channel.isdigit() else ""
        )
        raise web.HTTPFound("/settings?msg=" + _q("Approval settings saved."))

    async def issues_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        raw = str(data.get("issue_notify_discord_ids", ""))
        ids = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip().isdigit()]
        await self.bot.store.set_setting("issue_notify_discord_ids", ",".join(ids))

        channel = str(data.get("issues_channel_id", "")).strip()
        await self.bot.store.set_setting(
            "issues_channel_id", channel if channel.isdigit() else ""
        )
        raise web.HTTPFound("/settings?msg=" + _q("Issue notification settings saved."))

    async def anime_action(self, request: web.Request) -> web.Response:
        data = await request.post()

        def _int_field(name: str) -> str:
            value = str(data.get(name, "")).strip()
            return value if value.isdigit() else ""

        def _str_field(name: str) -> str:
            return str(data.get(name, "")).strip()

        await self.bot.store.set_setting("anime_sonarr_server_id", _int_field("anime_sonarr_server_id"))
        await self.bot.store.set_setting("anime_sonarr_profile_id", _int_field("anime_sonarr_profile_id"))
        await self.bot.store.set_setting("anime_sonarr_root_folder", _str_field("anime_sonarr_root_folder"))
        await self.bot.store.set_setting("anime_radarr_server_id", _int_field("anime_radarr_server_id"))
        await self.bot.store.set_setting("anime_radarr_profile_id", _int_field("anime_radarr_profile_id"))
        await self.bot.store.set_setting("anime_radarr_root_folder", _str_field("anime_radarr_root_folder"))
        raise web.HTTPFound("/settings?msg=" + _q("Anime routing saved."))

    # -- Radarr/Sonarr connections -----------------------------------------

    @staticmethod
    async def _probe_arr(base_url: str, api_key: str) -> None:
        """Validate an arr connection; raises ArrError if unreachable/unauthorised."""
        probe = ArrClient(base_url, api_key)
        try:
            await probe.system_status()
        finally:
            await probe.aclose()

    async def arr_add_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        kind = str(data.get("kind", "")).strip().lower()
        label = str(data.get("label", "")).strip()
        base_url = str(data.get("base_url", "")).strip().rstrip("/")
        api_key = str(data.get("api_key", "")).strip()
        if kind not in ("radarr", "sonarr") or not (label and base_url and api_key):
            raise web.HTTPFound("/settings?msg=" + _q("Type, label, URL and API key are required."))
        try:
            await self._probe_arr(base_url, api_key)
        except ArrError as exc:
            raise web.HTTPFound("/settings?msg=" + _q(f"Couldn't connect: {exc}"))
        await self.bot.store.add_arr_instance(
            kind=kind, label=label, base_url=base_url, api_key=api_key,
            is_4k="is_4k" in data, is_default="is_default" in data,
        )
        raise web.HTTPFound("/settings?msg=" + _q(f"{label} added."))

    async def arr_update_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        instance_id = str(data.get("id", ""))
        existing = await self.bot.store.get_arr_instance(instance_id)
        if existing is None:
            raise web.HTTPFound("/settings?msg=" + _q("No such connection."))
        label = str(data.get("label", "")).strip() or existing.label
        base_url = str(data.get("base_url", "")).strip().rstrip("/") or existing.base_url
        # Blank key field => keep the current key.
        api_key = str(data.get("api_key", "")).strip() or existing.api_key
        try:
            await self._probe_arr(base_url, api_key)
        except ArrError as exc:
            raise web.HTTPFound("/settings?msg=" + _q(f"Couldn't connect: {exc}"))
        await self.bot.store.update_arr_instance(
            instance_id, label=label, base_url=base_url, api_key=api_key,
            is_4k="is_4k" in data, is_default="is_default" in data,
        )
        raise web.HTTPFound("/settings?msg=" + _q(f"{label} saved."))

    async def arr_delete_action(self, request: web.Request) -> web.Response:
        data = await request.post()
        instance_id = str(data.get("id", ""))
        await self.bot.store.delete_arr_instance(instance_id)
        raise web.HTTPFound("/settings?msg=" + _q("Connection deleted."))

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


def _webhook_int(value: object) -> int | None:
    """Seerr sends ids as strings in webhook payloads; coerce best-effort."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Inline SVG path data (24x24 viewBox, stroke=currentColor) for nav + tiles.
_ICON_PATHS = {
    "home": '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "issue": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "mail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 5L2 7"/>',
    "logs": '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    "menu": '<line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "server": '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>',
    "approvals": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
}

# Sidebar nav: (href, label, icon). `label` doubles as the active-state key —
# it matches the `title` each page handler already passes to _layout().
_NAV = [
    ("/", "Dashboard", "home"),
    ("/links", "Links", "link"),
    ("/activity", "Activity", "activity"),
    ("/approvals", "Approvals", "approvals"),
    ("/issues", "Issues", "issue"),
    ("/invites", "Invites", "mail"),
    ("/logs", "Logs", "logs"),
    ("/settings", "Settings", "settings"),
]

# The VaultRequestrr logo, served by the /icon.png route. A 256px copy bundled in
# the package (the 1254px source lives in unraid/ for the Community Apps template).
# Resolved relative to the package so it works regardless of CWD, and in Docker.
_LOGO_PATH = Path(__file__).resolve().parent / "static" / "icon.png"


def _icon(name: str, size: int = 18) -> str:
    return (
        f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round">{_ICON_PATHS.get(name, "")}</svg>'
    )


def _empty_row(colspan: int, message: str, icon: str = "activity") -> str:
    return (
        f'<tr class="empty"><td colspan="{colspan}">'
        f'<div class="emptybox">{_icon(icon, 22)}<span>{html.escape(message)}</span></div>'
        f"</td></tr>"
    )


def _layout(title: str, body: str, *, nav: bool = True) -> str:
    if nav:
        items = ""
        for href, label, icon in _NAV:
            active = " active" if label == title else ""
            items += f'<a class="navitem{active}" href="{href}">{_icon(icon)}<span>{label}</span></a>'
        shell = f"""
        <input type="checkbox" id="navtoggle" class="navtoggle" hidden>
        <label for="navtoggle" class="scrim"></label>
        <aside class="sidebar">
          <a class="brand" href="/"><img class="logo" src="/icon.png" alt="" width="30" height="30">VaultRequestrr</a>
          <nav class="navlist">{items}</nav>
          <nav class="navlist foot"><a class="navitem" href="/logout">{_icon("logout")}<span>Sign out</span></a></nav>
        </aside>
        <div class="content">
          <header class="topbar">
            <label for="navtoggle" class="burger" aria-label="Toggle menu">{_icon("menu", 20)}</label>
            <h1 class="page">{html.escape(title)}</h1>
          </header>
          <main>{body}</main>
        </div>
        """
    else:
        shell = f'<main class="solo">{body}</main>'
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/icon.png">
<title>{html.escape(title)} · VaultRequestrr</title>
<style>{_CSS}</style>
</head><body>{shell}</body></html>"""


_CSS = """
:root{
  --bg:#0d0f13;--bg-elev:#15181f;--card:#1a1d24;--card-2:#1f232b;--line:#2a2e38;
  --fg:#e6e8ee;--muted:#8b91a0;--accent:#5865f2;--accent-soft:rgba(88,101,242,.14);
  --ok:#3ba55d;--bad:#ed4245;--warn:#e3a008;
  --radius:14px;--radius-sm:9px;--shadow:0 1px 2px rgba(0,0,0,.3),0 10px 28px rgba(0,0,0,.18)
}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;background:var(--bg);color:var(--fg);
  font:15px/1.6 system-ui,Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit}
.ic{flex:0 0 auto}

/* Sidebar */
.sidebar{width:236px;flex:0 0 236px;background:var(--bg-elev);border-right:1px solid var(--line);
  display:flex;flex-direction:column;padding:16px 14px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:11px;font-weight:700;font-size:16px;
  text-decoration:none;color:var(--fg);padding:6px 8px 18px}
.brand .logo{display:block;width:30px;height:30px;border-radius:8px;object-fit:cover;
  box-shadow:0 4px 12px rgba(0,0,0,.35)}
.navlist{display:flex;flex-direction:column;gap:2px}
.navlist.foot{margin-top:auto;padding-top:10px;border-top:1px solid var(--line)}
.navitem{display:flex;align-items:center;gap:11px;padding:10px 12px;border-radius:var(--radius-sm);
  color:var(--muted);text-decoration:none;font-weight:500;font-size:14px;
  border-left:2px solid transparent;transition:background .15s,color .15s}
.navitem:hover{background:var(--card);color:var(--fg)}
.navitem.active{background:var(--accent-soft);color:var(--fg);border-left-color:var(--accent)}
.navitem.active .ic{color:var(--accent)}

/* Content + topbar */
.content{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:14px;
  padding:15px 28px;background:rgba(13,15,19,.82);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line)}
.topbar .page{margin:0;font-size:18px;font-weight:650}
.burger{display:none;align-items:center;justify-content:center;width:36px;height:36px;
  border-radius:var(--radius-sm);color:var(--fg);cursor:pointer}
.burger:hover{background:var(--card)}
.scrim{display:none}
main{padding:24px 28px 48px;max-width:1180px;width:100%}
main.solo{padding:0;max-width:none;display:flex;align-items:center;justify-content:center;min-height:100vh}

/* Cards + stat tiles */
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:22px;margin:0 0 20px;box-shadow:var(--shadow)}
.card h2{margin:0 0 16px;font-size:16px;font-weight:650}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:16px;margin:0 0 22px}
.stat{position:relative;display:flex;flex-direction:column;gap:5px;margin:0;
  transition:transform .15s,border-color .15s}
.stat:hover{transform:translateY(-2px);border-color:#3a3f4c}
.stat .num{font-size:30px;font-weight:700;letter-spacing:-.5px;display:flex;align-items:center;gap:8px}
.stat .muted{font-size:13px}
.stat .tileico{position:absolute;top:18px;right:18px;color:var(--muted);opacity:.45}

/* Typography + tables */
h1,h2{margin:0 0 12px}.muted{color:var(--muted)}.small{font-size:13px}
code{background:#0a0c10;border:1px solid var(--line);padding:2px 7px;border-radius:6px;font-size:12.5px}
table{width:100%;border-collapse:collapse;font-size:14px}
thead th{position:sticky;top:0;text-align:left;padding:11px 12px;color:var(--muted);
  font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.5px;
  background:var(--card);border-bottom:1px solid var(--line)}
tbody td{padding:12px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--card-2)}
tr.empty:hover{background:transparent}
.emptybox{display:flex;flex-direction:column;align-items:center;gap:8px;color:var(--muted);
  padding:26px 10px;text-align:center}
.emptybox .ic{opacity:.6}

/* Buttons + inputs */
button,.btn{display:inline-flex;align-items:center;gap:6px;background:var(--accent);color:#fff;
  border:0;border-radius:var(--radius-sm);padding:9px 15px;cursor:pointer;font-size:13.5px;
  font-weight:600;font-family:inherit;text-decoration:none;transition:filter .15s,background .15s}
button:hover,.btn:hover{filter:brightness(1.08)}button:active,.btn:active{filter:brightness(.94)}
button.danger,.btn.danger{background:var(--bad)}
button.warn,.btn.warn{background:var(--warn);color:#1c1403}
button.ghost,.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
button.ghost:hover,.btn.ghost:hover{border-color:var(--accent);background:var(--accent-soft);filter:none}
input,select{background:#0a0c10;color:var(--fg);border:1px solid var(--line);
  border-radius:var(--radius-sm);padding:9px 11px;font-size:14px;font-family:inherit}
input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
input[type=checkbox]{accent-color:var(--accent);width:16px;height:16px}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.inline{display:inline-flex;gap:6px;align-items:center}
label.check{display:flex;align-items:center;gap:9px;margin:10px 0;cursor:pointer}
label.field{display:block;margin:14px 0;color:var(--muted);font-size:13px}
label.field input,label.field select{display:block;margin-top:6px;width:100%;max-width:440px;color:var(--fg)}
h3.subhead{margin:20px 0 4px;font-size:14px;font-weight:650}
summary{cursor:pointer}
details.editbox{display:inline-block}
details.editbox>summary{list-style:none}
details.editbox>summary::-webkit-details-marker{display:none}
.arrform{margin-top:10px;max-width:460px}
.formrow{display:flex;gap:12px;flex-wrap:wrap}
.formrow .field{flex:1;min-width:150px;margin-top:0}
details.advanced{margin:14px 0 6px;border:1px solid var(--line);border-radius:var(--radius-sm);
  padding:0 14px;background:var(--card-2);max-width:480px}
details.advanced>summary{padding:12px 0;cursor:pointer;font-size:13.5px;font-weight:600;
  color:var(--muted);list-style:none}
details.advanced[open]>summary{color:var(--fg)}
details.advanced>summary::-webkit-details-marker{display:none}
details.advanced>summary::before{content:"\\25B8";display:inline-block;margin-right:8px;
  transition:transform .15s}
details.advanced[open]>summary::before{transform:rotate(90deg)}
details.advanced .formrow{padding-bottom:8px}

/* Activity requester + details flyout */
.who{cursor:help;border-bottom:1px dotted var(--muted)}
button.detailtoggle{background:transparent;border:1px solid var(--line);color:var(--muted);
  padding:5px 12px;font-size:12.5px;font-weight:600}
button.detailtoggle:hover{border-color:var(--accent);color:var(--fg);filter:none}
button.detailtoggle[aria-expanded="true"]{border-color:var(--accent);color:var(--fg);
  background:var(--accent-soft)}
.detailrow td{background:var(--card-2);padding:16px 18px}
.detailrow:hover td{background:var(--card-2)}
.detailflex{display:flex;gap:18px;align-items:flex-start}
.detailposter{width:96px;height:auto;border-radius:8px;flex:0 0 auto;box-shadow:var(--shadow)}
.detailinfo{flex:1;min-width:0}
.detailmeta{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px 20px;margin:0}
.detailmeta dt{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin:0}
.detailmeta dd{margin:3px 0 0;font-size:13.5px;word-break:break-word}
.detailinfo .overview{margin:14px 0 0;line-height:1.55}

/* Settings tabs (segmented pill nav; degrades to a flat stack without JS) */
.subnav{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 22px}
.subtab{display:inline-flex;align-items:center;gap:8px;padding:9px 15px;border-radius:999px;
  color:var(--muted);text-decoration:none;font-size:13.5px;font-weight:600;
  border:1px solid var(--line);background:transparent;cursor:pointer;transition:.15s}
.subtab:hover{color:var(--fg);border-color:#3a3f4c;background:var(--card)}
.settings.tabbed .subtab.active{background:var(--accent);border-color:var(--accent);color:#fff}
.settings.tabbed .subtab.active .ic{color:#fff}
.settings.tabbed .panel{display:none}
.settings.tabbed .panel.active{display:block;animation:panelin .18s ease}
@keyframes panelin{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}

/* Login */
.login{max-width:360px;width:100%;text-align:center}
.login h1{font-size:22px}.login input{width:100%;margin:8px 0}.login button{width:100%;margin-top:4px}
.error{color:var(--bad);min-height:18px}

/* Flash banner */
.flash{display:flex;align-items:center;gap:10px;background:var(--accent-soft);
  border:1px solid var(--accent);color:var(--fg);padding:12px 16px;
  border-radius:var(--radius-sm);margin:0 0 20px;font-size:14px}

/* Badges + chips */
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.badge.ok{background:rgba(59,165,93,.16);color:#5fcf86}
.badge.bad{background:rgba(237,66,69,.16);color:#f2787a}
.badge.pend{background:rgba(139,145,160,.16);color:var(--muted)}
.logbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:14px}
.filters{display:flex;gap:8px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;font-size:13px;padding:6px 12px;border-radius:999px;
  border:1px solid var(--line);color:var(--muted);text-decoration:none;font-weight:500;transition:.15s}
.chip:hover{border-color:var(--accent);color:var(--fg)}
.chip.active{background:var(--accent);border-color:var(--accent);color:#fff}

/* Logs */
.logs{font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:64vh;overflow:auto;
  background:#0a0c10;border:1px solid var(--line);border-radius:var(--radius-sm);padding:12px}
.logline{display:grid;grid-template-columns:108px 70px 160px 1fr;gap:10px;padding:3px 4px;
  border-bottom:1px solid rgba(42,46,56,.4);white-space:pre-wrap;word-break:break-word;border-radius:4px}
.logline:hover{background:rgba(255,255,255,.025)}
.logline .ts,.logline .lname{color:var(--muted)}
.logline .lvl{font-weight:700}
.lvl-WARNING .lvl{color:var(--warn)}.lvl-ERROR .lvl,.lvl-CRITICAL .lvl{color:var(--bad)}.lvl-DEBUG{opacity:.65}
.lvl-ERROR .lmsg,.lvl-CRITICAL .lmsg{color:#f7a6a7}

/* Responsive: collapse sidebar to an off-canvas drawer */
@media(max-width:760px){
  .sidebar{position:fixed;z-index:30;left:0;top:0;transform:translateX(-100%);
    transition:transform .22s ease;box-shadow:var(--shadow)}
  #navtoggle:checked ~ .sidebar{transform:translateX(0)}
  #navtoggle:checked ~ .scrim{display:block;position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.5)}
  .burger{display:inline-flex}
  main{padding:18px 16px 40px}.topbar{padding:13px 16px}
  .card{overflow-x:auto}
}
"""


_SETTINGS_TABS_JS = """
<script>
(function(){
  var root = document.getElementById('settings');
  if(!root) return;
  var tabs = Array.prototype.slice.call(root.querySelectorAll('.subtab'));
  var panels = Array.prototype.slice.call(root.querySelectorAll('.panel'));
  if(!tabs.length) return;
  root.classList.add('tabbed');  // hides inactive panels; without JS all show
  function activate(id, save){
    var found = false;
    tabs.forEach(function(t){
      var on = t.dataset.tab === id;
      t.classList.toggle('active', on);
      if(on) found = true;
    });
    if(!found) return false;
    panels.forEach(function(p){ p.classList.toggle('active', p.dataset.panel === id); });
    if(save){ try{ localStorage.setItem('vrSettingsTab', id); }catch(e){} }
    return true;
  }
  tabs.forEach(function(t){
    t.addEventListener('click', function(e){ e.preventDefault(); activate(t.dataset.tab, true); });
  });
  // Restore the last-used tab across the save/redirect; #hash wins if present.
  var hash = (location.hash || '').replace('#','');
  var stored; try{ stored = localStorage.getItem('vrSettingsTab'); }catch(e){}
  activate(hash) || activate(stored) || activate(tabs[0].dataset.tab);
})();
</script>
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


_ACTIVITY_JS = """
<script>
(function(){
  document.querySelectorAll('.detailtoggle').forEach(function(btn){
    btn.addEventListener('click', async function(){
      var row = document.getElementById('detail-' + btn.dataset.id);
      if(!row) return;
      if(!row.hidden){ row.hidden = true; btn.setAttribute('aria-expanded','false'); return; }
      row.hidden = false; btn.setAttribute('aria-expanded','true');
      if(btn.dataset.loaded) return;
      btn.dataset.loaded = '1';
      var box = row.querySelector('.detailbody');
      try {
        var r = await fetch('/activity/detail?id=' + encodeURIComponent(btn.dataset.id));
        box.innerHTML = await r.text();
        box.classList.remove('muted','small');
      } catch(e) {
        box.textContent = 'Could not load details.'; btn.dataset.loaded = '';
      }
    });
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


def _requester_cell(discord_id: str, link) -> str:  # type: ignore[no-untyped-def]
    """Show the Seerr account name, with the Discord id on hover (falls back to Discord)."""
    if link is not None:
        name = link.plex_username or link.email or f"Seerr #{link.seerr_user_id}"
        tip = f"Discord ID: {discord_id} · Seerr #{link.seerr_user_id}"
        return f'<span class="who" title="{html.escape(tip)}">{html.escape(name)}</span>'
    return (
        f'<code class="who" title="Discord ID: {html.escape(discord_id)} · no linked Seerr account">'
        f'{html.escape(discord_id)}</code>'
    )


def _render_activity_detail(tracked, summary, detail) -> str:  # type: ignore[no-untyped-def]
    """Build the lazy-loaded details fragment for one Activity row."""
    poster = ""
    if summary is not None and summary.poster_url:
        poster = f'<img class="detailposter" src="{html.escape(summary.poster_url)}" alt="" loading="lazy">'

    rows: list[str] = []

    def add(label: str, value: str | None) -> None:
        if value:
            rows.append(f"<div><dt>{label}</dt><dd>{value}</dd></div>")

    if summary is not None:
        add("Released", html.escape(summary.release_date or ""))
        if summary.runtime:
            add("Runtime", f"{summary.runtime} min")
        if summary.genres:
            add("Genres", html.escape(", ".join(summary.genres)))
    if tracked.media_type == "tv" and tracked.seasons:
        seasons = "All seasons" if tracked.seasons == "all" else f"Seasons {html.escape(tracked.seasons)}"
        add("Requested", seasons)
    if detail is not None:
        instance = detail.get("instance")
        location = html.escape(instance.label) if instance is not None else ""
        if detail.get("path"):
            location += f' · <code>{html.escape(str(detail["path"]))}</code>'
        add("Location", location or None)
        if detail.get("size"):
            add("Size on disk", _fmt_size(detail["size"]))
        if detail.get("quality"):
            add("Quality", html.escape(str(detail["quality"])))
    kind = "tv" if tracked.media_type == "tv" else "movie"
    add(
        "TMDB",
        f'<a href="https://www.themoviedb.org/{kind}/{tracked.tmdb_id}" '
        f'target="_blank" rel="noopener">#{tracked.tmdb_id}</a>',
    )

    overview = ""
    if summary is not None and summary.overview:
        text = summary.overview if len(summary.overview) <= 320 else summary.overview[:319].rstrip() + "…"
        overview = f'<p class="muted small overview">{html.escape(text)}</p>'

    meta = f'<dl class="detailmeta">{"".join(rows)}</dl>' if rows else ""
    if not (poster or meta or overview):
        return '<p class="muted small">No extra details available.</p>'
    return f'<div class="detailflex">{poster}<div class="detailinfo">{meta}{overview}</div></div>'


def _flash(request: web.Request) -> str:
    msg = request.query.get("msg")
    return f'<div class="flash">{html.escape(msg)}</div>' if msg else ""


def _q(text: str) -> str:
    from urllib.parse import quote

    return quote(text)


def _opt_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _hidden_se(season: int | None, episode: int | None) -> str:
    if season is None or episode is None:
        return ""
    return (
        f'<input type="hidden" name="season" value="{season}">'
        f'<input type="hidden" name="episode" value="{episode}">'
    )


def _fmt_size(num: int | None) -> str:
    if not num:
        return "—"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
