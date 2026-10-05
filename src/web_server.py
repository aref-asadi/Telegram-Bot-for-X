"""Minimal aiohttp web server.

Free hosts (Render, Koyeb, Railway) require a Web Service to answer health
checks, otherwise the container is considered dead and gets recycled. This tiny
server runs in parallel with the bot and exposes:

* ``GET /``             - a small JSON banner (also useful for manual checks).
* ``GET /health``       - ``{"status": "ok"}`` exactly as required by the platforms.
* ``POST /auth``        - one-time handover endpoint used by the bookmarklet.
* ``GET /bookmarklet``  - drag-to-bookmarks installer page for that bookmarklet.

The port comes from the ``PORT`` environment variable (Render injects 10000).

The ``POST /auth`` endpoint closes the Telegram deep-link gap: the ``start``
parameter of a Telegram deep link only accepts 64 characters, which is far too
short for ``auth_token`` + ``ct0``. The bookmarklet therefore POSTs the two
values here; the payload is verified against X immediately and stashed under a
short-lived single-use code that the browser hands to Telegram as
``t.me/<bot>?start=auth_<code>`` - the wizard redeems it when the user lands
back in the chat, so the cookies themselves never travel through Telegram.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import ClientSession, ClientTimeout, web

from .config import get_settings
from .database import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mobile webview auth (reverse proxy to x.com)
# ---------------------------------------------------------------------------
# Published once from main.py (same pattern as ``set_bot_username``); the
# webview proxy persists captured sessions through this handle.
_webview_db: Optional[Database] = None

# chat_id (str) -> {"status": "pending|ok|error", "message": str, "at": float},
# polled by the wizard and the webview page via GET /webview/status?c=<chat>.
_webview_status: dict[str, dict[str, Any]] = {}
_WEBVIEW_STATUS_TTL = 900  # 15 minutes


def set_database(db: Database) -> None:
    """Publish the shared database handle to the webview endpoints."""
    global _webview_db
    _webview_db = db


def set_webview_status(chat_id: "int | str", status: str, message: str = "") -> None:
    """Record the outcome of a webview login attempt for status polling."""
    now = time.monotonic()
    for key, entry in list(_webview_status.items()):
        if now - entry["at"] > _WEBVIEW_STATUS_TTL:
            _webview_status.pop(key, None)
    _webview_status[str(chat_id)] = {
        "status": status,
        "message": message,
        "at": now,
    }


def get_webview_status(chat_id: "int | str") -> dict[str, Any]:
    """Return the stored webview status (defaults to ``pending``)."""
    entry = _webview_status.get(str(chat_id))
    if entry is None or time.monotonic() - entry["at"] > _WEBVIEW_STATUS_TTL:
        return {"status": "pending", "message": ""}
    return {"status": entry["status"], "message": entry["message"]}

# ---------------------------------------------------------------------------
# Pending bookmarklet handovers
# ---------------------------------------------------------------------------
# Maps a single-use handover code to the credentials posted by the bookmarklet.
# The wizard redeems a code through the ``/start auth_<code>`` deep link (or by
# pasting the code), so the credentials never travel through a Telegram deep
# link (64-char limit) and never sit in chat history. Entries expire after
# PENDING_TTL_SECONDS and are removed on use.
PENDING_TTL_SECONDS = 600  # 10 minutes - generous for a manual bookmarklet click

_pending_auth: dict[str, dict[str, Any]] = {}

# ``auth_token`` is a 40-char hex string; ``ct0`` is a longer hex/letter token.
_AUTH_TOKEN_RE = re.compile(r"^[0-9a-f]{40}$")
_CT0_RE = re.compile(r"^[0-9A-Za-z_-]{20,160}$")


def _sweep_expired() -> None:
    """Drop handover entries that are older than :data:`PENDING_TTL_SECONDS`."""
    now = time.monotonic()
    expired = [
        key
        for key, entry in _pending_auth.items()
        if now - entry["created_at"] > PENDING_TTL_SECONDS
    ]
    for key in expired:
        _pending_auth.pop(key, None)


def pop_pending_auth(code: str) -> dict[str, Any] | None:
    """Atomically claim and remove a pending handover (``None`` when unknown)."""
    _sweep_expired()
    return _pending_auth.pop(code, None)


# ---------------------------------------------------------------------------
# Bookmarklet installer page
# ---------------------------------------------------------------------------
# Set once the Telegram application knows its own @username (see main.py
# post_init); the bookmarklet uses it to build the t.me deep link.
_bot_username: Optional[str] = None


def set_bot_username(username: str) -> None:
    """Publish the bot's @username for the bookmarklet deep link."""
    global _bot_username
    _bot_username = username.lstrip("@")


def _minify_bookmarklet(source: str) -> str:
    """Collapse the readable bookmarklet source into a one-line JS payload.

    Full-line ``//`` comments and ``/* ... */`` blocks are dropped and every
    whitespace run is collapsed so the result is safe inside a single-line
    ``javascript:`` href. The source file keeps every code line free of a
    leading ``//`` and every string literal free of repeated spaces, making
    this simple minifier always safe.
    """
    lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]
    joined = re.sub(r"/\*.*?\*/", " ", " ".join(lines))
    return " ".join(joined.split())


async def health_handler(request: web.Request) -> web.Response:
    """Liveness probe used by Render / Koyeb / Railway."""
    return web.json_response({"status": "ok"})


async def root_handler(request: web.Request) -> web.Response:
    """Human-friendly status banner."""
    base = get_settings().public_base_url.strip().rstrip("/")
    return web.json_response(
        {
            "name": "Telegram-Bot-for-X",
            "status": "ok",
            "description": "Multi-user X/Twitter -> Telegram timeline forwarder",
            "bookmarklet": f"{base}/bookmarklet" if base else None,
        }
    )


def _cors_headers() -> dict[str, str]:
    """CORS headers allowing the bookmarklet to POST from x.com."""
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


async def auth_options_handler(request: web.Request) -> web.Response:
    """Pre-flight OPTIONS request for the bookmarklet POST."""
    return web.Response(status=204, headers=_cors_headers())


async def auth_handler(request: web.Request) -> web.Response:
    """Receive {auth_token, ct0} from the bookmarklet, verify and stash."""
    cors = _cors_headers()

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response(
            {"error": "Body must be valid JSON."}, status=400, headers=cors
        )

    auth_token = str(body.get("auth_token") or "").strip()
    ct0 = str(body.get("ct0") or "").strip()

    if not _AUTH_TOKEN_RE.match(auth_token):
        return web.json_response(
            {"error": "Invalid auth_token format."}, status=400, headers=cors
        )
    if not _CT0_RE.match(ct0):
        return web.json_response(
            {"error": "Invalid ct0 format."}, status=400, headers=cors
        )

    # Live verification against X avoids storing garbage credentials.
    from .twitter_client import (
        TwitterAuthError,
        TwitterClientError,
        verify_credentials,
    )

    try:
        valid = await verify_credentials(auth_token, ct0)
    except TwitterAuthError:
        valid = False
    except TwitterClientError as exc:
        logger.warning("Verification network error during bookmarklet auth: %s", exc)
        return web.json_response(
            {"error": "Could not contact X for verification. Please retry."},
            status=502,
            headers=cors,
        )

    if not valid:
        return web.json_response(
            {"error": "Twitter rejected these cookies (invalid/expired session)."},
            status=401,
            headers=cors,
        )

    # Optional chat binding: when the wizard built the installer link it
    # appended ?c=<chat_id>, so only that chat may redeem the code.
    raw_chat = body.get("chat_id")
    bound_chat: Optional[int] = None
    if isinstance(raw_chat, int) and not isinstance(raw_chat, bool):
        bound_chat = raw_chat
    elif isinstance(raw_chat, str) and raw_chat.isdigit():
        bound_chat = int(raw_chat)

    # Stash under a secure 12-char code that fits Telegram's /start parameter.
    code = secrets.token_hex(6)  # 12 hex characters
    _sweep_expired()
    _pending_auth[code] = {
        "auth_token": auth_token,
        "ct0": ct0,
        "chat_id": bound_chat,
        "created_at": time.monotonic(),
    }
    logger.info("Bookmarklet credentials stashed with handover code: %s", code)

    return web.json_response(
        {
            "ok": True,
            "code": code,
            "message": "Verified! Return to the bot to complete setup.",
        },
        status=200,
        headers=cors,
    )


async def bookmarklet_handler(request: web.Request) -> web.Response:
    """Render the drag-to-bookmarks installer page (``GET /bookmarklet``).

    The page embeds a single-line ``javascript:`` bookmarklet built from
    ``bookmarklet.js`` with three runtime substitutions: the public base URL,
    the bot's @username (for the ``t.me`` deep link) and - when the wizard
    linked here as ``/bookmarklet?c=<chat_id>`` - the chat id that the
    handover code will be bound to.
    """
    settings = get_settings()
    base = settings.public_base_url.strip().rstrip("/")

    chat_raw = request.query.get("c", "")
    chat_id = chat_raw if chat_raw.isdigit() else ""

    js_source = Path(__file__).with_name("bookmarklet.js").read_text(encoding="utf-8")
    js = (
        js_source.replace("__BASE_URL__", base)
        .replace("__BOT_USERNAME__", _bot_username or "")
        .replace("__CHAT_ID__", chat_id)
    )
    href = html.escape("javascript:" + _minify_bookmarklet(js), quote=True)

    if not base:
        status = (
            "⚠️ PUBLIC_BASE_URL روی سرور تنظیم نشده است؛ صاحب ربات باید آن را "
            "تعریف کند. تا آن زمان کوکی‌ها را دستی در ربات بفرستید."
        )
    elif not _bot_username:
        status = "✅ آماده — دکمهٔ زیر را به نوار بوکمارک‌ها بکشید."
    else:
        status = "✅ آماده — پس از کلیک روی بوکمارکلت، خودکار به ربات برمی‌گردید."

    template = Path(__file__).with_name("bookmarklet.html").read_text(encoding="utf-8")
    page = (
        template.replace("__BOOKMARKLET_HREF__", href)
        .replace("__BASE_URL__", base or "(تنظیم نشده)")
        .replace("__STATUS__", status)
    )
    return web.Response(text=page, content_type="text/html", charset="utf-8")


# ---------------------------------------------------------------------------
# Mobile webview: page, status polling and the x.com reverse proxy
# ---------------------------------------------------------------------------
_WEBVIEW_PREFIX = "/webview/x"


def _rewrite_x_url(value: str) -> str:
    """Point an absolute (or root-relative) x.com URL through the proxy."""
    if not value:
        return value
    for host in ("https://x.com", "http://x.com", "https://twitter.com", "//x.com"):
        if value.startswith(host):
            tail = value[len(host):]
            if not tail.startswith("/"):
                tail = "/" + tail
            return _WEBVIEW_PREFIX + tail
    if value.startswith("/"):
        # Relative Location (e.g. "/i/flow/login") must stay inside the proxy.
        if value.startswith(_WEBVIEW_PREFIX):
            return value
        return _WEBVIEW_PREFIX + value
    return value


def _rewrite_x_html(body: str) -> str:
    """Rewrite absolute X URLs inside HTML so the page keeps flowing through
    the proxy (API calls, redirects, asset references)."""
    body = body.replace("https://x.com", _WEBVIEW_PREFIX)
    body = body.replace("http://x.com", _WEBVIEW_PREFIX)
    body = body.replace("https://twitter.com", _WEBVIEW_PREFIX)
    body = body.replace("//x.com/", _WEBVIEW_PREFIX + "/")
    return body


def _rewrite_set_cookie(header: str, *, secure: bool) -> str:
    """Bind an upstream cookie to this host (drop Domain; adapt Secure)."""
    out: list[str] = []
    for part in header.split(";"):
        stripped = part.strip()
        lower = stripped.lower()
        if lower.startswith("domain="):
            continue  # browser defaults to our host
        if lower == "secure" and not secure:
            continue  # http dev servers cannot store Secure cookies
        if lower == "samesite=none" and not secure:
            out.append(" SameSite=Lax")  # SameSite=None without Secure is rejected
            continue
        out.append(part)
    return ";".join(out)


def _extract_cookie_value(header: str, name: str) -> Optional[str]:
    """Return the value of ``name`` from a raw ``Set-Cookie`` header."""
    first = header.split(";", 1)[0]
    key, sep, value = first.partition("=")
    if sep and key.strip().lower() == name:
        return value.strip()
    return None


_proxy_session: Optional[ClientSession] = None


def _get_proxy_session() -> ClientSession:
    """Lazily create the shared upstream session used by the proxy."""
    global _proxy_session
    if _proxy_session is None or _proxy_session.closed:
        _proxy_session = ClientSession(timeout=ClientTimeout(total=30))
    return _proxy_session


async def _webview_try_finalize(
    request: web.Request, token: str, ct0_value: str
) -> Optional[str]:
    """Verify + persist captured cookies; returns a redirect URL on success."""
    chat_raw = request.cookies.get("wv_chat", "")
    if not chat_raw.isdigit():
        return None
    chat_id = int(chat_raw)
    if get_webview_status(chat_id)["status"] == "ok":
        return f"/webview/done?c={chat_id}"  # already stored - jump to success
    if not _AUTH_TOKEN_RE.match(token) or not _CT0_RE.match(ct0_value):
        return None

    # Live verification avoids storing garbage credentials (same rule as the
    # bookmarklet endpoint above).
    from .twitter_client import (
        TwitterAuthError,
        TwitterClientError,
        verify_credentials,
    )

    try:
        valid = await verify_credentials(token, ct0_value)
    except TwitterAuthError:
        valid = False
    except TwitterClientError as exc:
        logger.warning("Webview verification network error: %s", exc)
        return None

    if not valid or _webview_db is None:
        set_webview_status(
            chat_id, "error", "X این کوکی‌ها را نپذیرفت؛ دوباره تلاش کنید."
        )
        return None

    await _webview_db.save_session(chat_id, token, ct0_value)
    set_webview_status(chat_id, "ok")
    logger.info("Webview session captured for chat_id=%s", chat_id)
    return f"/webview/done?c={chat_id}"


def _is_secure(request: web.Request) -> bool:
    """True when the original client connection was HTTPS (incl. proxies)."""
    forwarded = request.headers.get("X-Forwarded-Proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


async def webview_handler(request: web.Request) -> web.Response:
    """Serve the webview entry page (``GET /webview?c=<chat_id>``).

    Also binds the chat id to this browser through a path-scoped cookie so the
    reverse proxy below knows whose session to store once X issues it, and
    resets that chat's status to ``pending`` for a fresh attempt. The same
    template doubles as the ``/webview/done`` success page (the page detects
    the path client-side).
    """
    chat_raw = request.query.get("c", "")
    if not chat_raw.isdigit():
        return web.json_response({"error": "Missing chat id (c=...)."}, status=400)

    set_webview_status(chat_raw, "pending")
    template = Path(__file__).with_name("webview.html").read_text(encoding="utf-8")
    page = template.replace("__CHAT_ID__", html.escape(chat_raw, quote=True))
    response = web.Response(text=page, content_type="text/html", charset="utf-8")
    response.set_cookie(
        "wv_chat",
        chat_raw,
        path="/webview",
        httponly=True,
        samesite="Lax",
        secure=_is_secure(request),
    )
    return response


async def webview_status_handler(request: web.Request) -> web.Response:
    """Poll the outcome of a webview login attempt (``GET /webview/status``)."""
    chat_raw = request.query.get("c", "")
    if not chat_raw.isdigit():
        return web.json_response({"status": "pending", "message": ""})
    return web.json_response(get_webview_status(chat_raw))


async def webview_proxy_handler(request: web.Request) -> web.Response:
    """Reverse-proxy one request to ``https://x.com/<tail>``.

    Every proxied response has its cookies rebound to this host, so the login
    SPA behaves exactly as on x.com while the browser actually stores (and
    replays) the session on our origin. When ``auth_token`` + ``ct0`` become
    available - from a ``Set-Cookie`` on this response or from cookies the
    browser already replays - they are verified live and persisted through
    :meth:`Database.save_session`. Document requests are then redirected to
    the success page; XHR responses pass through untouched so the login flow
    keeps working (the page's status polling reports success there).
    """
    tail = request.match_info.get("tail", "")
    target = f"https://x.com/{tail}"
    if request.query_string:
        target = f"{target}?{request.query_string}"

    method = request.method.upper()
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        # Hop-bytes we replace ourselves: Origin/Referer are rewritten to x.com
        # below so X's CSRF checks pass when the browser posts from our origin.
        if lower in (
            "host",
            "content-length",
            "connection",
            "accept-encoding",
            "origin",
            "referer",
        ):
            continue
        headers[key] = value
    headers["Origin"] = "https://x.com"
    headers["Referer"] = "https://x.com/"
    headers["Accept-Encoding"] = "identity"  # aiohttp decodes gzip only

    body = await request.read() if method in ("POST", "PUT", "PATCH") else None
    try:
        upstream = await _get_proxy_session().request(
            method,
            target,
            headers=headers,
            data=body,
            allow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001 - network/timeout/TLS
        logger.warning("Webview proxy error for %s: %s", target, exc)
        return web.json_response({"error": "Could not reach x.com."}, status=502)

    # Rebind cookies to this host and remember any session values.
    captured_token: Optional[str] = None
    captured_ct0: Optional[str] = None
    set_cookies: list[str] = []
    for header in upstream.headers.getall("Set-Cookie", []):
        value = _extract_cookie_value(header, "auth_token")
        if value:
            captured_token = value
        value = _extract_cookie_value(header, "ct0")
        if value:
            captured_ct0 = value
        set_cookies.append(_rewrite_set_cookie(header, secure=_is_secure(request)))

    # Cookies captured on an earlier proxied response are replayed by the
    # browser on our origin, so fall back to the request's cookie jar.
    if captured_token is None:
        captured_token = request.cookies.get("auth_token")
    if captured_ct0 is None:
        captured_ct0 = request.cookies.get("ct0")

    redirect_to: Optional[str] = None
    if captured_token and captured_ct0:
        redirect_to = await _webview_try_finalize(request, captured_token, captured_ct0)

    # Redirect only full page loads; XHR/fetch redirects do not navigate the
    # browser and would just break the login SPA (status polling covers those).
    is_document = request.headers.get("Sec-Fetch-Dest", "") == "document"
    if redirect_to and is_document:
        upstream.release()  # body unread - hand the connection back explicitly
        return web.HTTPFound(redirect_to)

    raw = await upstream.read()
    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
    if "text/html" in content_type and raw:
        text = _rewrite_x_html(raw.decode("utf-8", errors="replace"))
        raw = text.encode("utf-8")
        content_type = "text/html; charset=utf-8"

    response = web.Response(status=upstream.status, body=raw)
    response.headers["Content-Type"] = content_type
    for header in set_cookies:
        response.headers.add("Set-Cookie", header)
    location = upstream.headers.get("Location")
    if location:
        response.headers["Location"] = _rewrite_x_url(location)
    # Headers that no longer apply after decompression/rewriting - CSP and
    # X-Frame-Options are dropped so x.com's policy cannot lock our proxy out.
    for hop in (
        "Content-Length",
        "Content-Encoding",
        "Transfer-Encoding",
        "Content-Security-Policy",
        "Content-Security-Policy-Report-Only",
        "X-Frame-Options",
        "Strict-Transport-Security",
    ):
        response.headers.pop(hop, None)
    return response


def create_app() -> web.Application:
    """Build the aiohttp application with its routes registered."""
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/auth", auth_handler)
    app.router.add_options("/auth", auth_options_handler)
    app.router.add_get("/bookmarklet", bookmarklet_handler)
    app.router.add_get("/webview", webview_handler)
    app.router.add_get("/webview/done", webview_handler)
    app.router.add_get("/webview/status", webview_status_handler)
    app.router.add_route("*", "/webview/x/{tail:.*}", webview_proxy_handler)
    return app


async def run_web_server() -> None:
    """Serve the health-check endpoints until cancelled.

    Designed to be launched with :func:`asyncio.create_task` so it can be
    cancelled cleanly during shutdown.
    """
    settings = get_settings()
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()

    # Bind on all interfaces so container platforms can reach the port.
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)
    await site.start()
    logger.info("Health-check web server listening on http://0.0.0.0:%s", settings.port)

    try:
        # Sleep forever; the task is cancelled on shutdown.
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Web server shutting down.")
        raise
    finally:
        await runner.cleanup()
        if _proxy_session is not None and not _proxy_session.closed:
            await _proxy_session.close()
