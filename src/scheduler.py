"""Background scheduler.

A single cooperative :func:`run_scheduler` coroutine polls every *active* user's
home timeline once per ``POLL_INTERVAL_SECONDS`` and forwards any tweets that
appeared since that user's last poll.

Per-user isolation: each timeline is fetched with *that user's* own cookies and
delivered to *their* own ``chat_id``. A failure for one user can never affect
another - every user is wrapped in its own try/except.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import TelegramError
from telegram.ext import Application

from .bot_dispatcher import dispatch_tweet
from .config import get_settings
from .database import Database, UserRecord
from .twitter_client import (
    TwitterAuthError,
    TwitterClientError,
    TwitterRateLimitError,
    fetch_home_timeline,
)

logger = logging.getLogger(__name__)

# Sent once, the first time a user's timeline is successfully polled.
READY_NOTICE = (
    "🔄 پایش تایم‌لاین شما آغاز شد.\n"
    "از این پس توییت‌های جدید به‌صورت خودکار اینجا نمایش داده می‌شوند."
)

# Sent when X rejects a user's cookies - they must re-onboard with /start.
SESSION_EXPIRED_NOTICE = (
    "⚠️ نشست توییتر شما منقضی شد.\n"
    "لطفاً با ارسال /start کوکی‌های جدید را ثبت کنید."
)

# A small pause between outgoing tweets keeps us well under Telegram's flood
# limits when a single poll produces several new tweets.
_INTER_MESSAGE_DELAY = 0.35


async def _notify(bot: Bot, chat_id: int, text: str) -> None:
    """Best-effort notification that never raises into the scheduler loop."""
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except TelegramError as exc:
        logger.warning("Could not notify chat %s: %s", chat_id, exc)


def _select_new_tweets(tweets: list[dict], last_tweet_id: str | None) -> list[dict]:
    """Return tweets newer than ``last_tweet_id``, oldest first (for reading).

    Tweet ids are snowflake integers that increase monotonically, so a numeric
    comparison is a reliable "newer than" test - and unlike equality matching it
    still works when the previous pointer has scrolled out of the fetched
    window.
    """
    try:
        last_value = int(last_tweet_id) if last_tweet_id else 0
    except (TypeError, ValueError):
        last_value = 0

    fresh: list[dict] = []
    for tweet in tweets:
        try:
            if int(tweet["id"]) > last_value:
                fresh.append(tweet)
        except (KeyError, TypeError, ValueError):
            continue

    # The API returns newest-first; reverse so the chat reads chronologically.
    fresh.reverse()
    return fresh


async def poll_user(bot: Bot, db: Database, user: UserRecord, max_tweets: int) -> None:
    """Poll a single user's timeline and forward any new tweets."""
    try:
        tweets = await fetch_home_timeline(user.auth_token, user.ct0, count=max_tweets)
    except TwitterAuthError:
        # Cookies are dead: stop polling this user and ask them to re-onboard.
        logger.warning("Session expired for chat_id=%s - deactivating.", user.chat_id)
        await db.set_active(user.chat_id, False)
        await _notify(bot, user.chat_id, SESSION_EXPIRED_NOTICE)
        return
    except TwitterRateLimitError as exc:
        logger.warning("Rate limited for chat_id=%s: %s", user.chat_id, exc)
        return
    except TwitterClientError as exc:
        logger.warning("Timeline fetch failed for chat_id=%s: %s", user.chat_id, exc)
        return

    if not tweets:
        return

    # Newest tweet in the fetched window (the API returns newest-first).
    newest_id = tweets[0]["id"]

    # First ever poll: seed the pointer instead of flooding the chat with the
    # whole current timeline, then confirm to the user that monitoring works.
    if not user.last_tweet_id:
        await db.update_last_tweet_id(user.chat_id, newest_id)
        await _notify(bot, user.chat_id, READY_NOTICE)
        logger.info("Seeded last_tweet_id=%s for chat_id=%s", newest_id, user.chat_id)
        return

    fresh = _select_new_tweets(tweets, user.last_tweet_id)
    if not fresh:
        return

    logger.info("Forwarding %d new tweet(s) to chat_id=%s", len(fresh), user.chat_id)

    delivered_count = 0
    for tweet in fresh:
        try:
            if await dispatch_tweet(bot, user.chat_id, tweet):
                delivered_count += 1
        except TelegramError as exc:
            logger.warning("Failed to forward tweet %s: %s", tweet.get("id"), exc)
        await asyncio.sleep(_INTER_MESSAGE_DELAY)

    logger.info(
        "Delivered %d/%d tweet(s) to chat_id=%s",
        delivered_count,
        len(fresh),
        user.chat_id,
    )

    # Advance the pointer so the same tweets are never forwarded twice, even if
    # an individual delivery failed (avoids an infinite retry loop).
    await db.update_last_tweet_id(user.chat_id, newest_id)



async def run_cycle(bot: Bot, db: Database, max_tweets: int) -> int:
    """Run one polling pass over every active user. Returns the user count."""
    users = await db.get_active_users()
    if not users:
        logger.debug("No active users to poll.")
        return 0

    logger.debug("Polling %d active user(s).", len(users))
    for user in users:
        try:
            await poll_user(bot, db, user, max_tweets)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - strict isolation between users
            logger.exception(
                "Unexpected error while polling chat_id=%s: %s", user.chat_id, exc
            )
    return len(users)


async def run_scheduler(application: Application) -> None:
    """Infinite background loop, started concurrently from ``main.py``."""
    settings = get_settings()
    db: Database = application.bot_data["db"]
    bot: Bot = application.bot
    interval = settings.poll_interval_seconds

    logger.info("Scheduler started (interval=%s seconds).", interval)

    while True:
        try:
            count = await run_cycle(bot, db, settings.max_tweets_per_poll)
            logger.debug("Scheduler cycle finished for %d user(s).", count)
        except asyncio.CancelledError:
            logger.info("Scheduler cancelled - shutting down.")
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            logger.exception("Scheduler cycle failed: %s", exc)

        await asyncio.sleep(interval)

