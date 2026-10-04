"""Application entry point.

Boot sequence:

1. Load configuration and configure logging (fail fast if ``BOT_TOKEN`` is
   missing).
2. Open (and migrate) the SQLite database.
3. Build the Telegram ``Application``, register every handler.
4. Start the bot without BlockingPolling (so we keep control of the loop).
5. Launch the aiohttp health-check server and the background scheduler
   concurrently with :func:`asyncio.gather` / ``create_task``.
6. Wait for a stop signal, then shut everything down gracefully.

Run with::

    python main.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler

from src.bot_dispatcher import TRANSLATE_CALLBACK_PREFIX, translate_callback
from src.bot_wizard import get_conversation_handler, get_management_handlers
from src.config import ConfigError, configure_logging, get_settings
from src.database import Database
from src.scheduler import run_scheduler
from src.twitter_client import close_client
from src.web_server import run_web_server

logger = logging.getLogger(__name__)

# Commands shown in Telegram's "/" menu.
_BOT_COMMANDS = [
    BotCommand("start", "شروع و ثبت کوکی‌های توییتر"),
    BotCommand("status", "نمایش وضعیت ربات"),
    BotCommand("pause", "توقف ارسال توییت‌ها"),
    BotCommand("resume", "ادامهٔ ارسال توییت‌ها"),
    BotCommand("set_grok", "ثبت یا تغییر کلید گروک"),
    BotCommand("logout", "حذف کامل اطلاعات"),
    BotCommand("help", "راهنمای دستورها"),
    BotCommand("cancel", "لغو عملیات جاری"),
]


async def post_init(application: Application) -> None:
    """Publish the slash-command menu once the bot is initialised."""
    try:
        await application.bot.set_my_commands(_BOT_COMMANDS)
        me = await application.bot.get_me()
        logger.info("Bot started as @%s (id=%s)", me.username, me.id)
    except TelegramError as exc:
        logger.warning("Could not register bot commands: %s", exc)


def build_application(bot_token: str, db: Database) -> Application:
    """Create the Telegram application and wire up every handler."""
    application = (
        Application.builder()
        .token(bot_token)
        .concurrent_updates(True)  # several users can be served in parallel
        .post_init(post_init)
        .build()
    )

    # Share long-lived resources with every handler via ``bot_data``.
    application.bot_data["db"] = db

    # Group 0 - the onboarding conversation takes priority, followed by the
    # per-tweet translate button (callback data "tr_<tweet_id>").
    application.add_handler(get_conversation_handler())
    application.add_handler(
        CallbackQueryHandler(
            translate_callback, pattern=f"^{TRANSLATE_CALLBACK_PREFIX}.+$"
        )
    )

    # Group 1 - standalone management commands, only reached when the
    # conversation is inactive (or does not claim the update).
    for handler in get_management_handlers():
        application.add_handler(handler, group=1)

    return application


async def _shutdown(
    application: Application, tasks: list[asyncio.Task], db: Database
) -> None:
    """Cancel background tasks and tear everything down cleanly."""
    logger.info("Shutting down...")

    # 1) Stop the web server and scheduler first.
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # 2) Stop Telegram polling, then the application itself.
    with contextlib.suppress(Exception):
        updater = application.updater
        if updater is not None and updater.running:
            await updater.stop()
    with contextlib.suppress(Exception):
        if application.running:
            await application.stop()
    with contextlib.suppress(Exception):
        await application.shutdown()

    # 3) Release the HTTP pool and the database connection.
    with contextlib.suppress(Exception):
        await close_client()
    with contextlib.suppress(Exception):
        await db.close()

    logger.info("Shutdown complete.")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGINT/SIGTERM to a friendly stop event where the platform allows.

    ``loop.add_signal_handler`` is unavailable on the Windows event loop, so
    there we rely on ``KeyboardInterrupt`` handling in ``__main__`` instead.
    """
    if sys.platform == "win32":
        return
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            logger.debug("Could not install handler for signal %s", sig)


async def main() -> None:
    """Wire everything together and run until stopped."""
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    configure_logging(settings.log_level)
    logger.info("Starting Telegram-Bot-for-X ...")
    logger.info("Database path: %s", settings.database_path)

    # 1) Database (also creates the data directory and schema).
    db = Database(settings.database_path)
    await db.init()

    # 2) Telegram application + handlers.
    application = build_application(settings.bot_token, db)
    await application.initialize()
    await application.start()

    if application.updater is None:
        raise RuntimeError("Telegram Updater is unavailable - cannot poll.")
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    logger.info("Telegram polling started.")

    # 3) Background services: health-check server + scheduler.
    background: list[asyncio.Task] = [
        asyncio.create_task(run_web_server(), name="web-server"),
        asyncio.create_task(run_scheduler(application), name="scheduler"),
    ]

    # 4) Wait for a shutdown signal.
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    try:
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Cancelled by asyncio.run() on Ctrl+C - fall through to cleanup.
        pass
    finally:
        await _shutdown(application, background, db)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user - exiting.")

