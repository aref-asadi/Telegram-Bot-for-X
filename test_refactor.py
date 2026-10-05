"""Unit tests for the dual-feed / not-interested / webview refactor.

Run with:  python test_refactor.py

Covers the new code paths end-to-end without touching the network:

* scheduler selection helpers (new-tweet filter, For-you thresholds, dedup)
* ``poll_user`` dual-feed flow (seeding, delivery order, pointer advances,
  cross-feed dedup, per-user feed overrides, auth deactivation)
* tweet parsing (metrics extraction, GraphQL + v1.1 builders)
* ``send_not_interested`` request shape and its top-level wrapper
* the ``ni_`` callback (ack -> delete -> best-effort relay) and the keyboard
* wizard pieces: mobile-webview button, /feeds text/keyboard, toggle snapshot
* webview helpers (URL/HTML/Cookie rewriting, status TTL) and HTTP routes
  incl. the reverse proxy's capture -> verify -> persist -> redirect flow
* config / grok OpenAI-compatible settings and the global-key fallback
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Must be set before importing src.* (config reads the environment lazily but
# is cached on first access).
os.environ["BOT_TOKEN"] = "123456789:AATestTokenForUnitTestsOnly"
os.environ["PUBLIC_BASE_URL"] = "https://bot.example.com"
os.environ.pop("AI_API_KEY", None)  # exercise the "no key" fallback branch

from multidict import CIMultiDict

import src.bot_dispatcher as bd
import src.bot_wizard as wz
import src.scheduler as sched
import src.twitter_client as tc
import src.web_server as ws
from src.config import get_settings
from src.database import Database, FeedSettings
from src.twitter_client import TwitterAuthError, TwitterError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def make_tweet(tid, likes=0, retweets=0, impressions=0, text="hello"):
    """Tweet dict in the shape produced by the timeline parser."""
    return {
        "id": str(tid),
        "text": text,
        "author_name": "Alice",
        "author_username": "alice",
        "link": f"https://x.com/alice/status/{tid}",
        "photos": [],
        "videos": [],
        "is_retweet": False,
        "retweeter_username": None,
        "quoted_text": None,
        "quoted_author": None,
        "metrics": {"likes": likes, "retweets": retweets, "impressions": impressions},
    }


class FakeBot:
    """Records ``send_message`` calls made by scheduler notifications."""

    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.messages.append((chat_id, text))


def eq(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def ok(label):
    print(f"PASS {label}")


# ---------------------------------------------------------------------------
# PASS 1 - scheduler pure selection helpers
# ---------------------------------------------------------------------------
def test_scheduler_helpers():
    # _select_new_tweets: newest-first input, numeric compare, oldest-first out
    tweets = [make_tweet(10), make_tweet(5), make_tweet(3)]
    eq(
        [t["id"] for t in sched._select_new_tweets(tweets, "2")],
        ["3", "5", "10"],
        "fresh filter oldest-first",
    )
    eq(
        [t["id"] for t in sched._select_new_tweets(tweets, None)],
        ["3", "5", "10"],
        "no pointer keeps all, oldest-first",
    )
    eq(
        sched._select_new_tweets(tweets, "10"), [], "old pointer yields none"
    )
    # A pointer scrolled out of the window still compares numerically.
    eq(
        [t["id"] for t in sched._select_new_tweets(tweets, "999999999999999999")],
        [],
        "huge pointer",
    )
    # Garbage entries are skipped instead of raising.
    eq(
        [t["id"] for t in sched._select_new_tweets([{"id": "7"}, {"no": "id"}], None)],
        ["7"],
        "garbage entry skipped",
    )
    # Non-numeric pointer falls back to "everything is new".
    eq(len(sched._select_new_tweets(tweets, "corrupt")), 3, "corrupt pointer")

    # _passes_for_you_thresholds: 0 disables a dimension; missing metrics = 0
    low = make_tweet(1, likes=5, retweets=1, impressions=100)
    assert sched._passes_for_you_thresholds(low, 0, 0, 0)
    assert not sched._passes_for_you_thresholds(low, 10, 0, 0)
    assert not sched._passes_for_you_thresholds(low, 0, 2, 0)
    assert not sched._passes_for_you_thresholds(low, 0, 0, 1000)
    assert not sched._passes_for_you_thresholds(make_tweet(1), 1, 0, 0)
    no_metrics = {"id": "1"}  # no "metrics" key at all
    assert sched._passes_for_you_thresholds(no_metrics, 0, 0, 0)
    assert not sched._passes_for_you_thresholds(no_metrics, 0, 1, 0)

    # _select_for_you_tweets: pointer + cross-feed dedup + thresholds, oldest-first
    window = [
        make_tweet(40, likes=10),
        make_tweet(30, likes=10),
        make_tweet(20, likes=3),
        make_tweet(10, likes=3),
    ]
    selected = sched._select_for_you_tweets(
        window,
        last_for_you_id="10",
        last_following_id="25",  # 20 was already delivered by Following
        min_likes=0,
        min_retweets=0,
        min_impressions=0,
    )
    eq([t["id"] for t in selected], ["30", "40"], "for-you selection + dedup")

    # Threshold filter drops below-min tweets (20/10 have only 3 likes).
    selected = sched._select_for_you_tweets(
        window,
        last_for_you_id="0",
        last_following_id="0",
        min_likes=9,
        min_retweets=0,
        min_impressions=0,
    )
    eq([t["id"] for t in selected], ["30", "40"], "for-you threshold filter")

    # _as_int coercion
    eq(sched._as_int(None), 0, "as_int None")
    eq(sched._as_int("42"), 42, "as_int str")
    eq(sched._as_int(-5), 0, "as_int negative clamped")
    ok("scheduler pure helpers")


# ---------------------------------------------------------------------------
# PASS 2 - twitter parsing: headers, metrics, GraphQL / v1.1 builders
# ---------------------------------------------------------------------------
def _graphql_result():
    """A minimal GraphQL tweet result as returned by HomeLatestTimeline."""
    return {
        "__typename": "Tweet",
        "core": {
            "user_results": {
                "result": {"core": {"name": "Alice", "screen_name": "alice"}}
            }
        },
        "legacy": {
            "id_str": "12345",
            "full_text": "Hello world",
            "favorite_count": 5,
            "retweet_count": 2,
        },
        "view_count": {"count": "1234"},
    }


def test_twitter_parsing():
    headers = tc.build_headers("AUTH1", "CT0VALUE")
    eq(headers["Cookie"], "auth_token=AUTH1; ct0=CT0VALUE", "cookie header")
    eq(headers["X-Csrf-Token"], "CT0VALUE", "csrf header")
    assert headers["Authorization"].startswith("Bearer ")

    metrics = tc._extract_metrics(
        {"view_count": {"count": "1234"}},
        {"favorite_count": 7, "retweet_count": 3},
    )
    eq(
        metrics,
        {"likes": 7, "retweets": 3, "impressions": 1234},
        "metrics extraction",
    )
    metrics = tc._extract_metrics(None, {"favorite_count": "junk", "retweet_count": None})
    eq(
        metrics,
        {"likes": 0, "retweets": 0, "impressions": 0},
        "metrics garbage -> 0",
    )
    eq(
        tc._extract_metrics({"view_count": "bad"}, {})["impressions"],
        0,
        "malformed view_count",
    )

    tweet = tc._build_tweet(_graphql_result())
    eq(tweet["id"], "12345", "build id")
    eq(tweet["text"], "Hello world", "build text")
    eq(tweet["author_name"], "Alice", "build author name")
    eq(tweet["author_username"], "alice", "build author screen")
    eq(tweet["link"], "https://x.com/alice/status/12345", "build link")
    eq(
        tweet["metrics"],
        {"likes": 5, "retweets": 2, "impressions": 1234},
        "build metrics",
    )
    eq(tweet["is_retweet"], False, "build not a retweet")

    # Retweets unwrap to the original author's tweet, tagged with the retweeter.
    original = {
        "legacy": {
            "id_str": "111",
            "full_text": "original text",
            "favorite_count": 1,
        },
        "core": {
            "user_results": {"result": {"core": {"name": "Carol", "screen_name": "carol"}}}
        },
    }
    wrapper = {
        "legacy": {
            "id_str": "999",
            "full_text": "RT @carol: original text",
            "retweeted_status_result": {"result": original},
        },
        "core": {
            "user_results": {"result": {"core": {"name": "Bob", "screen_name": "bob"}}}
        },
    }
    rt = tc._build_tweet(wrapper)
    eq(rt["id"], "111", "retweet unwraps original id")
    eq(rt["author_username"], "carol", "retweet original author")
    eq(rt["is_retweet"], True, "retweet flag")
    eq(rt["retweeter_username"], "bob", "retweeter recorded")

    # GraphQL walker de-duplicates repeated containers.
    payload = {
        "data": {
            "home": {
                "instructions": [
                    {
                        "entries": [
                            {"content": {"tweet_results": {"result": _graphql_result()}}},
                            {"content": {"tweet_results": {"result": _graphql_result()}}},
                        ]
                    }
                ]
            }
        }
    }
    tweets = tc._parse_graphql_tweets(payload)
    eq(len(tweets), 1, "graphql dedup")

    # v1.1 builder: metrics from legacy counts, dedup, retweet unwrap.
    v11 = tc._parse_v11_tweets(
        [
            {
                "id_str": "55",
                "full_text": "hi",
                "user": {"screen_name": "zoe", "name": "Zoe"},
                "favorite_count": 4,
            },
            {"id_str": "55", "full_text": "dup", "user": {"screen_name": "zoe"}},
            {
                "id_str": "70",
                "full_text": "RT",
                "retweeted_status": {
                    "id_str": "66",
                    "full_text": "orig",
                    "user": {"screen_name": "carol", "name": "Carol"},
                },
                "user": {"screen_name": "rt"},
            },
        ]
    )
    eq(len(v11), 2, "v11 dedup")
    eq(v11[0]["metrics"]["likes"], 4, "v11 likes")
    eq(v11[0]["metrics"]["impressions"], 0, "v11 has no impressions")
    eq(v11[1]["id"], "66", "v11 retweet unwrap")
    eq(v11[1]["is_retweet"], True, "v11 retweet flag")
    eq(v11[1]["retweeter_username"], "rt", "v11 retweeter")
    ok("twitter parsing")


# ---------------------------------------------------------------------------
# PASS 3 - send_not_interested: request shape + top-level wrapper
# ---------------------------------------------------------------------------
class _FakeHttpResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeHttpx:
    """Stands in for the httpx.AsyncClient owned by TwitterClient."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.posts = []

    async def post(self, url, params=None, headers=None, json=None):
        self.posts.append(
            {"url": url, "params": params, "headers": headers, "json": json}
        )
        return _FakeHttpResponse(self.status_code)


async def test_send_not_interested():
    # Real method: assert the exact request X receives.
    client = tc.TwitterClient.__new__(tc.TwitterClient)  # skip httpx pool setup
    client._client = _FakeHttpx()
    await tc.TwitterClient.send_not_interested(client, "AUTH", "CT0", "999")

    eq(len(client._client.posts), 1, "one POST fired")
    post = client._client.posts[0]
    assert post["url"].endswith("/timelinesFeedback"), post["url"]
    variables = json.loads(post["params"]["variables"])
    eq(
        variables,
        {"tweet_id": "999", "feedback_type": "NotInterested"},
        "query-string variables",
    )
    eq(post["json"]["queryId"], tc.FEEDBACK_QUERY_ID, "body queryId")
    eq(post["json"]["variables"]["feedback_type"], "NotInterested", "body variables")
    eq(post["headers"]["Cookie"], "auth_token=AUTH; ct0=CT0", "feedback cookies")
    eq(post["headers"]["X-Csrf-Token"], "CT0", "feedback csrf")

    # Dead session (401/403) is raised; other rejections stay silent.
    client._client = _FakeHttpx(status_code=401)
    try:
        await tc.TwitterClient.send_not_interested(client, "AUTH", "CT0", "999")
        raise AssertionError("expected TwitterAuthError for HTTP 401")
    except TwitterAuthError:
        pass
    client._client = _FakeHttpx(status_code=429)
    await tc.TwitterClient.send_not_interested(client, "AUTH", "CT0", "999")

    # Top-level wrapper delegates through get_client().
    class _FakeClient:
        def __init__(self):
            self.calls = []

        async def send_not_interested(self, auth_token, ct0, tweet_id):
            self.calls.append((auth_token, ct0, tweet_id))

    fake = _FakeClient()
    orig_get_client = tc.get_client

    async def _fake_get_client():
        return fake

    tc.get_client = _fake_get_client
    try:
        await tc.send_not_interested("A", "B", "777")
    finally:
        tc.get_client = orig_get_client
    eq(fake.calls, [("A", "B", "777")], "wrapper passthrough")
    ok("send_not_interested")


# ---------------------------------------------------------------------------
# PASS 4 - poll_user dual-feed flow
# ---------------------------------------------------------------------------
async def test_poll_user():
    orig_following = sched.fetch_following_timeline
    orig_home = sched.fetch_home_timeline
    orig_dispatch = sched.dispatch_tweet
    orig_delay = sched._INTER_MESSAGE_DELAY
    sched._INTER_MESSAGE_DELAY = 0

    dispatched: list[tuple] = []

    async def _fake_dispatch(bot, chat_id, tweet):
        dispatched.append((chat_id, tweet["id"]))
        return True

    sched.dispatch_tweet = _fake_dispatch

    tmp = tempfile.TemporaryDirectory()
    db = None
    try:
        db = Database(Path(tmp.name) / "poll.db")
        await db.init()

        # -- Scenario A: dual delivery, cross-feed dedup, pointer advances --
        await db.upsert_user(1000, "A" * 40, "B" * 32)
        await db.update_last_tweet_id(1000, "100")
        await db.update_last_for_you_id(1000, "100")
        user = await db.get_user(1000)

        async def following_ab(auth, ct0, count=20):
            return [make_tweet(300), make_tweet(200)]

        async def home_ab(auth, ct0, count=20):
            return [make_tweet(400), make_tweet(300), make_tweet(200)]

        sched.fetch_following_timeline = following_ab
        sched.fetch_home_timeline = home_ab

        bot = FakeBot()
        await sched.poll_user(bot, db, user, 20)

        eq(
            dispatched,
            [(1000, "200"), (1000, "300"), (1000, "400")],
            "delivery order: Following oldest-first, then For-you",
        )
        eq(user.last_tweet_id, "300", "following pointer (newest of window)")
        eq(user.last_for_you_id, "400", "for-you pointer")
        row = await db.get_user(1000)
        eq(row.last_tweet_id, "300", "following pointer persisted")
        eq(row.last_for_you_id, "400", "for-you pointer persisted")
        eq(bot.messages, [], "no notices after seeding")

        # -- Scenario B: below-threshold For-you tweet: nothing delivered,
        #    but the pointer still advances so it is never re-checked. ------
        await db.upsert_feed_settings(
            FeedSettings(
                chat_id=1000,
                for_you_enabled=True,
                for_you_min_likes=1000,
                for_you_min_retweets=0,
                for_you_min_impressions=0,
            )
        )
        dispatched.clear()

        async def following_b(auth, ct0, count=20):
            return []

        async def home_b(auth, ct0, count=20):
            return [make_tweet(500, likes=5)]

        sched.fetch_following_timeline = following_b
        sched.fetch_home_timeline = home_b
        user = await db.get_user(1000)
        await sched.poll_user(bot, db, user, 20)

        eq(dispatched, [], "threshold-skipped tweet not delivered")
        row = await db.get_user(1000)
        eq(row.last_for_you_id, "500", "for-you pointer advances past skip")
        eq(row.last_tweet_id, "300", "following pointer untouched")

        # -- Scenario C: per-user For-you switch off -> home never fetched ---
        feed = await db.get_feed_settings(1000)
        assert feed is not None
        feed.for_you_enabled = False
        await db.upsert_feed_settings(feed)
        home_calls: list[int] = []

        async def home_c(auth, ct0, count=20):
            home_calls.append(1)
            return [make_tweet(550)]

        async def following_c(auth, ct0, count=20):
            return [make_tweet(600)]

        sched.fetch_following_timeline = following_c
        sched.fetch_home_timeline = home_c
        user = await db.get_user(1000)
        await sched.poll_user(bot, db, user, 20)

        eq(home_calls, [], "disabled For-you feed is not fetched")
        eq(dispatched, [(1000, "600")], "Following still delivered when For-you off")
        eq((await db.get_user(1000)).last_tweet_id, "600", "following pointer moves")
        ok("poll_user: delivery + dedup + thresholds + toggle")

        # -- Scenario D: first poll seeds both pointers + READY notice -------
        await db.upsert_user(2000, "C" * 40, "D" * 32)
        user2 = await db.get_user(2000)
        dispatched.clear()

        async def following_d(auth, ct0, count=20):
            return [make_tweet(10), make_tweet(5)]

        async def home_d(auth, ct0, count=20):
            return [make_tweet(12)]

        sched.fetch_following_timeline = following_d
        sched.fetch_home_timeline = home_d
        bot2 = FakeBot()
        await sched.poll_user(bot2, db, user2, 20)

        eq(dispatched, [], "seeding sends nothing")
        eq(bot2.messages, [(2000, sched.READY_NOTICE)], "READY notice once")
        row2 = await db.get_user(2000)
        eq(row2.last_tweet_id, "10", "seeded following pointer (newest)")
        eq(row2.last_for_you_id, "12", "seeded for-you pointer (newest)")

        # -- Scenario E: auth failure deactivates the user -------------------
        await db.upsert_user(3000, "E" * 40, "F" * 32)
        user3 = await db.get_user(3000)

        async def following_e(auth, ct0, count=20):
            raise TwitterAuthError("cookies rejected")

        sched.fetch_following_timeline = following_e
        bot3 = FakeBot()
        await sched.poll_user(bot3, db, user3, 20)

        row3 = await db.get_user(3000)
        eq(row3.is_active, False, "auth failure deactivates")
        eq(bot3.messages, [(3000, sched.SESSION_EXPIRED_NOTICE)], "expired notice")

        # -- Scenario F: both feeds empty -> silent (no seed, no notice) -----
        await db.upsert_user(4000, "G" * 40, "H" * 32)
        user4 = await db.get_user(4000)
        dispatched.clear()

        async def empty(auth, ct0, count=20):
            return []

        sched.fetch_following_timeline = empty
        sched.fetch_home_timeline = empty
        bot4 = FakeBot()
        await sched.poll_user(bot4, db, user4, 20)
        eq(bot4.messages, [], "empty cycle is silent")
        eq(dispatched, [], "empty cycle delivers nothing")
        eq((await db.get_user(4000)).last_tweet_id, None, "no seed on empty cycle")
        ok("poll_user: seed + auth + empty cycle")
    finally:
        sched.fetch_following_timeline = orig_following
        sched.fetch_home_timeline = orig_home
        sched.dispatch_tweet = orig_dispatch
        sched._INTER_MESSAGE_DELAY = orig_delay
        if db is not None:
            await db.close()
        tmp.cleanup()


# ---------------------------------------------------------------------------
# PASS 5 - per-tweet keyboard + "Not interested" callback
# ---------------------------------------------------------------------------
class _FakeMessage:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.deleted = False
        self.edits = []

    async def delete(self):
        self.deleted = True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class _FakeCallbackQuery:
    def __init__(self, data, message=None, from_user_id=0):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=from_user_id)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


async def test_not_interested_callback():
    # Keyboard: two rows - translate first, then not-interested.
    kb = bd.build_keyboard("123")
    rows = kb.inline_keyboard
    eq(len(rows), 2, "two keyboard rows")
    eq(rows[0][0].callback_data, "tr_123", "translate button")
    eq(rows[1][0].callback_data, "ni_123", "not-interested button")

    tmp = tempfile.TemporaryDirectory()
    db = None
    orig_send = bd.send_not_interested
    ni_calls: list[tuple] = []

    async def _fake_send(auth_token, ct0, tweet_id):
        ni_calls.append((auth_token, ct0, tweet_id))

    bd.send_not_interested = _fake_send
    try:
        db = Database(Path(tmp.name) / "ni.db")
        await db.init()
        ctx = SimpleNamespace(bot_data={"db": db})

        # Happy path: ack (alert) -> delete -> relay with the user's session.
        await db.upsert_user(111, "A" * 40, "B" * 32)
        msg = _FakeMessage(111)
        query = _FakeCallbackQuery("ni_4242", message=msg)
        update = SimpleNamespace(callback_query=query)
        await bd.not_interested_callback(update, ctx)
        eq(query.answers, [(bd.NOT_INTERESTED_ALERT, True)], "alert acknowledged")
        assert msg.deleted, "tweet message deleted"
        eq(ni_calls, [("A" * 40, "B" * 32, "4242")], "relayed with user cookies")

        # Unknown session: still ack + delete, no X call.
        msg2 = _FakeMessage(222)
        query2 = _FakeCallbackQuery("ni_4343", message=msg2)
        await bd.not_interested_callback(SimpleNamespace(callback_query=query2), ctx)
        assert msg2.deleted, "deleted even without session"
        eq(len(ni_calls), 1, "no X call without session")

        # X-side failure must never raise into Telegram.
        async def _boom(auth_token, ct0, tweet_id):
            raise TwitterError("X said no")

        bd.send_not_interested = _boom
        await db.upsert_user(333, "C" * 40, "D" * 32)
        msg3 = _FakeMessage(333)
        query3 = _FakeCallbackQuery("ni_4444", message=msg3)
        await bd.not_interested_callback(SimpleNamespace(callback_query=query3), ctx)
        assert msg3.deleted, "deleted despite X failure"
        eq(len(query3.answers), 1, "acknowledged despite X failure")

        # Edited / already-deleted messages: chat id falls back to the sender.
        bd.send_not_interested = _fake_send
        await db.upsert_user(444, "E" * 40, "F" * 32)
        query4 = _FakeCallbackQuery("ni_4545", message=None, from_user_id=444)
        await bd.not_interested_callback(SimpleNamespace(callback_query=query4), ctx)
        eq(ni_calls[-1], ("E" * 40, "F" * 32, "4545"), "fallback chat id works")

        # A malformed query (no data) is ignored outright.
        query5 = _FakeCallbackQuery("")
        await bd.not_interested_callback(SimpleNamespace(callback_query=query5), ctx)
        eq(query5.answers, [], "empty callback data ignored")
        ok("not_interested callback")
    finally:
        bd.send_not_interested = orig_send
        if db is not None:
            await db.close()
        tmp.cleanup()


# ---------------------------------------------------------------------------
# PASS 6 - wizard: webview guide button, /feeds view + toggle snapshot
# ---------------------------------------------------------------------------
def _pattern_matches(pattern_obj, text):
    """Handle PTB giving us either a str or a compiled regex."""
    if pattern_obj is None:
        return False
    import re as _re

    if hasattr(pattern_obj, "match"):
        return bool(pattern_obj.match(text))
    return bool(_re.compile(pattern_obj).match(text))


async def test_wizard_feeds():
    # Mobile webview button sits next to the bookmarklet installer link.
    guide_text, kb = wz._bookmarklet_guide(42)
    assert kb is not None, "keyboard present when PUBLIC_BASE_URL set"
    urls = [btn.url for row in kb.inline_keyboard for btn in row]
    eq(
        urls,
        [
            "https://bot.example.com/bookmarklet?c=42",
            "https://bot.example.com/webview?c=42",
        ],
        "bookmarklet + webview buttons",
    )
    assert guide_text

    # /feeds status text + toggle button.
    view = wz._feeds_text(True, 1, 2, 3)
    assert "Following" in view and "For you" in view, view
    assert "<code>1</code>" in view and "<code>3</code>" in view, view
    kb2 = wz._feeds_keyboard(False)
    eq(
        kb2.inline_keyboard[0][0].callback_data,
        wz.FEEDS_TOGGLE_CALLBACK,
        "toggle callback data",
    )

    # /feeds is registered among the management handlers + documented.
    handlers = wz.get_management_handlers()

    def _commands(handler):
        cmds = getattr(handler, "commands", None) or getattr(handler, "command", ())
        return [cmds] if isinstance(cmds, str) else list(cmds)

    from telegram.ext import CallbackQueryHandler

    assert any(
        type(h).__name__ == "CommandHandler" and "feeds" in _commands(h)
        for h in handlers
    ), "/feeds command handler registered"
    feeds_cb = [
        h
        for h in handlers
        if isinstance(h, CallbackQueryHandler)
        and _pattern_matches(getattr(h, "pattern", None), "feeds_toggle")
    ]
    eq(len(feeds_cb), 1, "feeds toggle callback registered")
    assert any(
        isinstance(v, str) and "/feeds" in v for v in vars(wz).values()
    ), "/feeds documented in help text"

    # Toggle semantics: first press snapshots server defaults into the DB,
    # later presses flip only the switch.
    tmp = tempfile.TemporaryDirectory()
    db = None
    try:
        db = Database(Path(tmp.name) / "feeds.db")
        await db.init()
        await db.upsert_user(5000, "A" * 40, "B" * 32)
        ctx = SimpleNamespace(bot_data={"db": db})
        msg = _FakeMessage(5000)
        query = _FakeCallbackQuery(wz.FEEDS_TOGGLE_CALLBACK, message=msg)
        update = SimpleNamespace(callback_query=query)

        await wz.feeds_toggle_callback(update, ctx)
        feed = await db.get_feed_settings(5000)
        assert feed is not None, "row created on first toggle"
        settings = get_settings()
        eq(feed.for_you_enabled, False, "effective default-on flips to off")
        eq(feed.for_you_min_likes, settings.for_you_min_likes, "snapshot likes")
        eq(feed.for_you_min_retweets, settings.for_you_min_retweets, "snapshot retweets")
        eq(
            feed.for_you_min_impressions,
            settings.for_you_min_impressions,
            "snapshot impressions",
        )
        eq(len(query.answers), 1, "toggle acknowledged")
        assert query.edits, "message refreshed in place"

        # Second toggle flips the switch but keeps operator-set thresholds.
        feed.for_you_min_likes = 5
        await db.upsert_feed_settings(feed)
        msg2 = _FakeMessage(5000)
        query2 = _FakeCallbackQuery(wz.FEEDS_TOGGLE_CALLBACK, message=msg2)
        await wz.feeds_toggle_callback(SimpleNamespace(callback_query=query2), ctx)
        feed2 = await db.get_feed_settings(5000)
        eq(feed2.for_you_enabled, True, "second toggle flips back on")
        eq(feed2.for_you_min_likes, 5, "threshold snapshot preserved")
        ok("wizard: webview guide + /feeds")
    finally:
        if db is not None:
            await db.close()
        tmp.cleanup()


# ---------------------------------------------------------------------------
# PASS 7 - config: OpenAI-compatible AI settings + For-you thresholds
# ---------------------------------------------------------------------------
async def test_config():
    settings = get_settings()
    assert settings.ai_base_url.startswith("http"), "AI base url default"
    assert settings.ai_model, "AI model configured"
    assert settings.ai_api_key is None or isinstance(settings.ai_api_key, str)
    for name in (
        "for_you_min_likes",
        "for_you_min_retweets",
        "for_you_min_impressions",
    ):
        value = getattr(settings, name)
        assert isinstance(value, int) and value >= 0, name
    eq(settings.public_base_url, "https://bot.example.com", "public base url")

    import src.grok_service as g

    eq(g.DEFAULT_BASE_URL, "https://api.x.ai/v1", "grok default endpoint")
    # No AI_API_KEY in this test environment: a user without a personal key
    # must fail fast with the documented error instead of hitting the network.
    if not settings.ai_api_key:
        try:
            await g.translate_tweet("hello", None)
            raise AssertionError("expected GrokAuthError without any key")
        except g.GrokAuthError as exc:
            assert "AI_API_KEY" in str(exc), str(exc)
    ok("config: AI + thresholds + global-key fallback")


# ---------------------------------------------------------------------------
# PASS 8 - webview helpers + status store
# ---------------------------------------------------------------------------
def test_webview_helpers():
    eq(
        ws._rewrite_x_url("https://x.com/i/flow/login"),
        "/webview/x/i/flow/login",
        "absolute url",
    )
    eq(ws._rewrite_x_url("//x.com/home"), "/webview/x/home", "protocol-relative")
    eq(ws._rewrite_x_url("/i/flow/login"), "/webview/x/i/flow/login", "relative url")
    eq(ws._rewrite_x_url("/webview/x/home"), "/webview/x/home", "already proxied")
    eq(
        ws._rewrite_x_url("https://abs.twimg.com/a.js"),
        "https://abs.twimg.com/a.js",
        "other host untouched",
    )
    eq(ws._rewrite_x_url(""), "", "empty url")

    body = ws._rewrite_x_html(
        '<script src="https://x.com/a.js"></script><img src="//x.com/b.png">'
    )
    assert "/webview/x/a.js" in body and "/webview/x/b.png" in body, body
    assert "x.com" not in body, body

    insecure = ws._rewrite_set_cookie(
        "auth_token=abc; Domain=.x.com; Path=/; Secure; SameSite=None", secure=False
    )
    assert "domain" not in insecure.lower(), insecure
    assert "secure" not in insecure.lower(), insecure
    assert "SameSite=Lax" in insecure, insecure

    secure = ws._rewrite_set_cookie(
        "auth_token=abc; Domain=.x.com; Path=/; Secure; SameSite=None", secure=True
    )
    assert "domain" not in secure.lower(), secure
    assert "Secure" in secure and "SameSite=None" in secure, secure

    eq(ws._extract_cookie_value("ct0=xyz123; Path=/", "ct0"), "xyz123", "extract ct0")
    eq(
        ws._extract_cookie_value("auth_token=abc; ct0=zz", "ct0"),
        None,
        "only the first pair counts",
    )
    eq(ws._extract_cookie_value("guest_id=1", "auth_token"), None, "missing cookie")

    # Status store: defaults to pending; TTL-expired entries fall back too.
    eq(
        ws.get_webview_status("99999"),
        {"status": "pending", "message": ""},
        "unset status",
    )
    ws.set_webview_status("7", "ok")
    eq(ws.get_webview_status(7), {"status": "ok", "message": ""}, "str/int keys unify")
    ws._webview_status["7"]["at"] -= ws._WEBVIEW_STATUS_TTL + 1
    eq(ws.get_webview_status("7")["status"], "pending", "expired status -> pending")
    ok("webview helpers + status TTL")


# ---------------------------------------------------------------------------
# PASS 9 - webview HTTP routes + reverse proxy capture flow
# ---------------------------------------------------------------------------
class _FakeUpstream:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self.headers = headers if headers is not None else CIMultiDict()
        self._body = body
        self.released = False

    async def read(self):
        return self._body

    def release(self):
        self.released = True


class _FakeProxySession:
    def __init__(self, upstream):
        self.upstream = upstream
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.upstream


async def test_webview_routes_and_proxy():
    from aiohttp.test_utils import TestClient, TestServer

    tmp = tempfile.TemporaryDirectory()
    db = None
    client = None
    orig_session_factory = ws._get_proxy_session
    orig_db = ws._webview_db
    orig_verify = tc.verify_credentials
    try:
        db = Database(Path(tmp.name) / "wv.db")
        await db.init()
        ws.set_database(db)

        app = ws.create_app()
        client = TestClient(TestServer(app))
        await client.start_server()

        # -- Entry page -----------------------------------------------------
        resp = await client.get("/webview")
        eq(resp.status, 400, "entry page requires ?c=")
        resp = await client.get("/webview?c=7")
        eq(resp.status, 200, "entry page renders")
        page = await resp.text()
        assert "__CHAT_ID__" not in page, "placeholder replaced"
        assert 'var chat = "7"' in page, "chat id embedded"
        set_cookie = resp.headers.get("Set-Cookie", "")
        assert "wv_chat=7" in set_cookie and "Path=/webview" in set_cookie, set_cookie

        # -- Status polling -------------------------------------------------
        resp = await client.get("/webview/status?c=7")
        eq(await resp.json(), {"status": "pending", "message": ""}, "status pending")
        ws.set_webview_status("7", "error", "boom")
        resp = await client.get("/webview/status?c=7")
        eq(await resp.json(), {"status": "error", "message": "boom"}, "status error")
        ws.set_webview_status("7", "pending")

        # -- Proxy pass-through: HTML rewrite + cookie rebind + header drops --
        upstream = _FakeUpstream(
            200,
            CIMultiDict(
                [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Security-Policy", "default-src 'self'"),
                    ("X-Frame-Options", "DENY"),
                    (
                        "Set-Cookie",
                        "guest_id=v1%3Aabc; Domain=.x.com; Path=/; Secure; SameSite=None",
                    ),
                ]
            ),
            b'<a href="https://x.com/home">go</a>',
        )
        session = _FakeProxySession(upstream)
        ws._get_proxy_session = lambda: session
        resp = await client.get("/webview/x/home", allow_redirects=False)
        eq(resp.status, 200, "proxy status passthrough")
        body = await resp.text()
        assert "/webview/x/home" in body and "https://x.com" not in body, body
        cookies = resp.headers.getall("Set-Cookie")
        eq(len(cookies), 1, "one rebound cookie")
        assert "domain" not in cookies[0].lower(), cookies[0]
        assert "secure" not in cookies[0].lower(), cookies[0]
        assert "SameSite=Lax" in cookies[0], cookies[0]
        assert "Content-Security-Policy" not in resp.headers, "CSP dropped"
        assert "X-Frame-Options" not in resp.headers, "X-Frame-Options dropped"
        call = session.calls[0]
        eq(call["url"], "https://x.com/home", "upstream target")
        eq(call["headers"].get("Origin"), "https://x.com", "Origin rewritten")
        eq(call["headers"].get("Referer"), "https://x.com/", "Referer rewritten")
        eq(call["headers"].get("Accept-Encoding"), "identity", "gzip disabled")
        assert call["allow_redirects"] is False, "redirects handled manually"
        assert not any(k.lower() == "host" for k in call["headers"]), "Host stripped"

        # -- XHR redirect passes through with a rewritten Location -----------
        upstream2 = _FakeUpstream(
            302, CIMultiDict([("Location", "https://x.com/i/flow/login")])
        )
        session2 = _FakeProxySession(upstream2)
        ws._get_proxy_session = lambda: session2
        resp = await client.get("/webview/x/i/flow/login", allow_redirects=False)
        eq(resp.status, 302, "upstream 302 passthrough")
        eq(resp.headers["Location"], "/webview/x/i/flow/login", "Location proxied")

        # -- Document login: capture -> verify -> persist -> redirect ---------
        token = "deadbeef" * 5  # 40 hex chars
        ct0_value = "c0" * 16  # 32 chars
        upstream3 = _FakeUpstream(
            302,
            CIMultiDict(
                [
                    ("Location", "https://x.com/home"),
                    (
                        "Set-Cookie",
                        f"auth_token={token}; Domain=.x.com; Path=/; Secure; SameSite=None",
                    ),
                    (
                        "Set-Cookie",
                        f"ct0={ct0_value}; Domain=.x.com; Path=/; Secure; SameSite=None",
                    ),
                ]
            ),
        )
        session3 = _FakeProxySession(upstream3)
        ws._get_proxy_session = lambda: session3

        async def _fake_verify(auth_token, ct0_arg):
            return auth_token == token and ct0_arg == ct0_value

        tc.verify_credentials = _fake_verify
        resp = await client.get(
            "/webview/x/home",
            headers={"Cookie": "wv_chat=7", "Sec-Fetch-Dest": "document"},
            allow_redirects=False,
        )
        eq(resp.status, 302, "document login redirects")
        eq(resp.headers["Location"], "/webview/done?c=7", "redirects to success page")
        assert upstream3.released, "unread upstream body released"
        eq(ws.get_webview_status("7")["status"], "ok", "status flips to ok")
        user = await db.get_user(7)
        assert user is not None, "session stored"
        eq(user.auth_token, token, "auth_token persisted")
        eq(user.ct0, ct0_value, "ct0 persisted")

        resp = await client.get("/webview/status?c=7")
        eq((await resp.json())["status"], "ok", "polling reports success")
        resp = await client.get("/webview/done?c=7")
        eq(resp.status, 200, "success page renders")
        done_page = await resp.text()
        assert "__CHAT_ID__" not in done_page, "done placeholder replaced"
        ok("webview routes + reverse proxy")
    finally:
        ws._get_proxy_session = orig_session_factory
        ws._webview_db = orig_db
        tc.verify_credentials = orig_verify
        if client is not None:
            await client.close()
        if db is not None:
            await db.close()
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def run_all():
    test_scheduler_helpers()
    test_twitter_parsing()
    await test_send_not_interested()
    await test_poll_user()
    await test_not_interested_callback()
    await test_wizard_feeds()
    await test_config()
    test_webview_helpers()
    await test_webview_routes_and_proxy()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(run_all())
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
