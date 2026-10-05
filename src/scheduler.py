"""Background scheduler - dual-feed polling.

A single cooperative :func:`run_scheduler` coroutine polls every *active* user
once per ``POLL_INTERVAL_SECONDS`` against **two** feeds:

``Following``
    The chronological Following tab (``HomeLatestTimeline``). Every new tweet
    is forwarded; progress is tracked by ``users.last_tweet_id``.

``For you``
    The algorithmic home timeline, filtered by the per-user (or server-wide)
    thresholds ``FOR_YOU_MIN_LIKES`` / ``FOR_YOU_MIN_RETWEETS`` /
    ``FOR_YOU_MIN_IMPRESSIONS``. Tracked by ``users.last_for_you_id`` and
    deduplicated against the Following feed, so no tweet is ever delivered
    twice to the same chat.

The selection logic lives in pure helpers (:func:`_select_new_tweets`,
:func:`_select_for_you_tweets`, :func:`_passes_for_you_thresholds`) so it can
be unit-tested without any I/O.

Per-user isolation: each timeline is fetched with *that user's* own cookies and
delivered to *their* own ``chat_id``. A failure for one user can never affect
another - every user is wrapped in its own try/except.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

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
    fetch_following_timeline,
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


def _as_int(value: object) -> int:
    """Best-effort non-negative int coercion (``None``/garbage become 0)."""
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _passes_for_you_thresholds(
    tweet: dict,
    min_likes: int,
    min_retweets: int,
    min_impressions: int,
) -> bool:
    """Return ``True`` when the tweet satisfies the For-you quality filters.

    A threshold of ``0`` disables that dimension; missing metrics count as 0.
    """
    metrics = tweet.get("metrics") or {}
    return (
        _as_int(metrics.get("likes")) >= _as_int(min_likes)
        and _as_int(metrics.get("retweets")) >= _as_int(min_retweets)
        and _as_int(metrics.get("impressions")) >= _as_int(min_impressions)
    )


def _select_for_you_tweets(
    tweets: list[dict],
    *,
    last_for_you_id: Optional[str],
    last_following_id: Optional[str],
    min_likes: int,
    min_retweets: int,
    min_impressions: int,
) -> list[dict]:
    """Filter the raw For-you window down to tweets worth forwarding.

    Keeps a tweet when it is (a) newer than the For-you pointer, (b) not
    already delivered by the Following feed (cross-feed dedup) and (c) over
    the configured engagement thresholds. Returns the result oldest-first,
    matching :func:`_select_new_tweets`.
    """
    you_value = _as_int(last_for_you_id)
    following_value = _as_int(last_following_id)
    selected: list[dict] = []
    for tweet in tweets:
        tweet_id = _as_int(tweet.get("id"))
        if tweet_id <= 0 or tweet_id <= you_value:
            continue
        if tweet_id <= following_value:
            continue  # already forwarded by the Following feed
        if not _passes_for_you_thresholds(
            tweet, min_likes, min_retweets, min_impressions
        ):
            continue
        selected.append(tweet)
    selected.reverse()
    return selected


async def _deliver_tweets(bot: Bot, chat_id: int, tweets: list[dict]) -> int:
    """Forward tweets one by one; returns how many were actually sent."""
    delivered = 0
    for tweet in tweets:
        try:
            if await dispatch_tweet(bot, chat_id, tweet):
                delivered += 1
        except TelegramError as exc:
            logger.warning("Failed to forward tweet %s: %s", tweet.get("id"), exc)
        await asyncio.sleep(_INTER_MESSAGE_DELAY)
    return delivered


async def _deactivate(bot: Bot, db: Database, chat_id: int) -> None:
    """Stop polling a user whose cookies X has rejected."""
    logger.warning("Session expired for chat_id=%s - deactivating.", chat_id)
    await db.set_active(chat_id, False)
    await _notify(bot, chat_id, SESSION_EXPIRED_NOTICE)


async def poll_user(bot: Bot, db: Database, user: UserRecord, max_tweets: int) -> None:
    """Poll both feeds for a single user and forward anything new.

    The Following feed forwards everything; the For-you feed applies the
    configured engagement thresholds and never repeats a tweet the Following
    feed already delivered (cross-feed dedup).
    """
    settings = get_settings()
    feed = await db.get_feed_settings(user.chat_id)
    for_you_enabled = True if feed is None else feed.for_you_enabled
    min_likes = settings.for_you_min_likes if feed is None else feed.for_you_min_likes
    min_retweets = (
        settings.for_you_min_retweets if feed is None else feed.for_you_min_retweets
    )
    min_impressions = (
        settings.for_you_min_impressions
        if feed is None
        else feed.for_you_min_impressions
    )

    # -- Feed 1: Following (chronological - forwards everything) ----------
    following: Optional[list[dict]] = None
    try:
        following = await fetch_following_timeline(
            user.auth_token, user.ct0, count=max_tweets
        )
    except TwitterAuthError:
        # Cookies are dead: stop polling this user and ask them to re-onboard.
        await _deactivate(bot, db, user.chat_id)
        return
    except TwitterRateLimitError as exc:
        logger.warning(
            "Rate limited (Following) for chat_id=%s: %s", user.chat_id, exc
        )
    except TwitterClientError as exc:
        logger.warning(
            "Following fetch failed for chat_id=%s: %s", user.chat_id, exc
        )

    # -- Feed 2: filtered "For you" (algorithmic) --------------------------
    for_you: Optional[list[dict]] = None
    if for_you_enabled:
        try:
            for_you = await fetch_home_timeline(
                user.auth_token, user.ct0, count=max_tweets
            )
        except TwitterAuthError:
            await _deactivate(bot, db, user.chat_id)
            return
        except TwitterRateLimitError as exc:
            logger.warning(
                "Rate limited (ForYou) for chat_id=%s: %s", user.chat_id, exc
            )
        except TwitterClientError as exc:
            logger.warning(
                "ForYou fetch failed for chat_id=%s: %s", user.chat_id, exc
            )

    if not following and not for_you:
        return  # nothing fetched (both feeds empty or failed)

    # First ever poll: seed the pointers instead of flooding the chat with the
    # whole current timeline, then confirm to the user that monitoring works.
    seeded = False
    if following and not user.last_tweet_id:
        await db.update_last_tweet_id(user.chat_id, following[0]["id"])
        user.last_tweet_id = following[0]["id"]
        seeded = True
        logger.info(
            "Seeded last_tweet_id=%s for chat_id=%s",
            user.last_tweet_id,
            user.chat_id,
        )
    if for_you and not user.last_for_you_id:
        await db.update_last_for_you_id(user.chat_id, for_you[0]["id"])
        user.last_for_you_id = for_you[0]["id"]
        seeded = True
        logger.info(
            "Seeded last_for_you_id=%s for chat_id=%s",
            user.last_for_you_id,
            user.chat_id,
        )
    if seeded:
        await _notify(bot, user.chat_id, READY_NOTICE)
        return

    # -- Following delivery ------------------------------------------------
    if following:
        fresh = _select_new_tweets(following, user.last_tweet_id)
        if fresh:
            logger.info(
                "Forwarding %d new Following tweet(s) to chat_id=%s",
                len(fresh),
                user.chat_id,
            )
            delivered = await _deliver_tweets(bot, user.chat_id, fresh)
            logger.info(
                "Delivered %d/%d Following tweet(s) to chat_id=%s",
                delivered,
                len(fresh),
                user.chat_id,
            )
        # Advance the pointer so the same tweets are never forwarded twice,
        # even if an individual delivery failed (avoids a retry loop).
        await db.update_last_tweet_id(user.chat_id, following[0]["id"])
        user.last_tweet_id = following[0]["id"]

    # -- For-you delivery (thresholds + cross-feed dedup) ------------------
    if for_you:
        selected = _select_for_you_tweets(
            for_you,
            last_for_you_id=user.last_for_you_id,
            last_following_id=user.last_tweet_id,
            min_likes=min_likes,
            min_retweets=min_retweets,
            min_impressions=min_impressions,
        )
        if selected:
            logger.info(
                "Forwarding %d new For-you tweet(s) to chat_id=%s",
                len(selected),
                user.chat_id,
            )
            delivered = await _deliver_tweets(bot, user.chat_id, selected)
            logger.info(
                "Delivered %d/%d For-you tweet(s) to chat_id=%s",
                delivered,
                len(selected),
                user.chat_id,
            )
        await db.update_last_for_you_id(user.chat_id, for_you[0]["id"])
        user.last_for_you_id = for_you[0]["id"]



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

