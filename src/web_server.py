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

from aiohttp import web

from .config import get_settings

logger = logging.getLogger(__name__)

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


def create_app() -> web.Application:
    """Build the aiohttp application with its routes registered."""
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/auth", auth_handler)
    app.router.add_options("/auth", auth_options_handler)
    app.router.add_get("/bookmarklet", bookmarklet_handler)
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
