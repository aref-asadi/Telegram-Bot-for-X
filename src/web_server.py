"""Minimal aiohttp web server.

Free hosts (Render, Koyeb, Railway) require a Web Service to answer health
checks, otherwise the container is considered dead and gets recycled. This tiny
server runs in parallel with the bot and exposes:

* ``GET /``       - a small JSON banner (also useful for manual checks).
* ``GET /health`` - ``{"status": "ok"}`` exactly as required by the platforms.

The port comes from the ``PORT`` environment variable (Render injects 10000).
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .config import get_settings

logger = logging.getLogger(__name__)


async def health_handler(request: web.Request) -> web.Response:
    """Liveness probe used by Render / Koyeb / Railway."""
    return web.json_response({"status": "ok"})


async def root_handler(request: web.Request) -> web.Response:
    """Human-friendly status banner."""
    return web.json_response(
        {
            "name": "Telegram-Bot-for-X",
            "status": "ok",
            "description": "Multi-user X/Twitter -> Telegram timeline forwarder",
        }
    )


def create_app() -> web.Application:
    """Build the aiohttp application with its routes registered."""
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_handler)
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
