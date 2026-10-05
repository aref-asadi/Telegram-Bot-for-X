"""Cookie-authenticated X/Twitter client.

The client reproduces the exact HTTP calls the ``x.com`` web app makes, using
only the two cookies a user copies out of their browser:

* ``auth_token`` - the session cookie (this *is* the logged-in session).
* ``ct0``        - the CSRF token, sent both as a cookie and as the
                   ``x-csrf-token`` header (double-submit pattern).

Everything else (the public web bearer token, browser-ish headers) is constant
and therefore safe to hard-code; those values are shipped inside X's own
front-end JavaScript and are not user-specific secrets.

Public API
----------
``verify_credentials(auth_token, ct0) -> bool``
``fetch_home_timeline(auth_token, ct0) -> list[dict]``

Both are thin wrappers around a lazily-created, shared :class:`TwitterClient`
so the underlying ``httpx`` connection pool is reused across users.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from typing import Any, Iterator, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Public bearer token embedded in X's web client (not a user secret).
BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# GraphQL operation used to read the "For you" home timeline. The query id
# drifts over time; this known-good id is used as the primary attempt and a
# v1.1 REST endpoint is used as a fallback if GraphQL stops responding.
HOME_TIMELINE_QUERY_ID = "c-CzHF1LboFilMpsx4ZCrQ"

# GraphQL operation behind the chronological "Following" tab (HomeLatestTimeline).
# Source: the mirrored X internal-API document (fa0311/TwitterInternalAPIDocument);
# refresh from there if X rotates query ids and this feed starts returning 4xx.
HOME_LATEST_TIMELINE_QUERY_ID = "OQPHTgwczzp9RMAPt6BH9A"

# Tweet-feedback mutation (the post ⋯ menu's "Not interested" entries).
# Verified id from the mirrored X internal-API document
# (fa0311/TwitterInternalAPIDocument): a POST mutation with a features set,
# so it needs no feature flags. Variables are sent as query params AND JSON
# body; rejected calls are logged, never fatal (see send_not_interested).
FEEDBACK_QUERY_ID = "vfVbgvTPTQ-dF_PQ5lD1WQ"

GRAPHQL_BASE = "https://x.com/i/api/graphql"
V11_BASE = "https://x.com/i/api/1.1"

# A modern desktop Chrome UA keeps the request looking like a normal browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# GraphQL "features" flags. Only the ``True`` values are sent in the URL to
# keep it well below server URI length limits.
FEATURES: dict[str, bool] = {
    "rweb_video_screen_enabled": False,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "rweb_jetfuel_frame": False,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

# HomeTimeline GraphQL variables. Count is overridden per call.
_HOME_TIMELINE_VARIABLES: dict[str, Any] = {
    "includePromotedContent": True,
    "latestControlAvailable": True,
    "requestContext": "launch",
    "withCommunity": True,
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class TwitterError(Exception):
    """Base class for every error raised by this module."""


class TwitterAuthError(TwitterError):
    """Raised when the session cookies are invalid or have expired (401/403)."""


class TwitterRateLimitError(TwitterError):
    """Raised when X rate-limits the account (HTTP 429)."""


class TwitterClientError(TwitterError):
    """Raised for any other unexpected HTTP/transport failure."""


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def build_headers(auth_token: str, ct0: str) -> dict[str, str]:
    """Return the per-request headers authenticating ``auth_token``/``ct0``."""
    return {
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "Cookie": f"auth_token={auth_token}; ct0={ct0}",
        "X-Csrf-Token": ct0,
        "X-Twitter-Auth-Type": "OAuth2Session",
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Client-Language": "en",
        "content-type": "application/json",
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://x.com",
        "Referer": "https://x.com/home",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def _raise_for_status(response: httpx.Response) -> None:
    """Translate a non-2xx X response into a domain-specific exception."""
    if response.status_code in (401, 403):
        raise TwitterAuthError(
            f"Twitter rejected the session (HTTP {response.status_code}). "
            "The cookies are invalid or expired."
        )
    if response.status_code == 429:
        raise TwitterRateLimitError(
            "Twitter rate limit reached (HTTP 429). Try again later."
        )
    if response.status_code >= 400:
        snippet = response.text[:300]
        raise TwitterClientError(
            f"Unexpected response from X (HTTP {response.status_code}): {snippet}"
        )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def _iter_tweet_results(node: Any) -> Iterator[dict]:
    """Recursively yield every ``tweet_results.result`` object in a payload.

    This tolerant walker makes the parser resilient to the layout changes that
    X regularly ships: instead of hard-coding the exact instruction path we
    simply search the whole JSON tree for tweet result containers.
    """
    if isinstance(node, dict):
        container = node.get("tweet_results")
        if isinstance(container, dict) and isinstance(container.get("result"), dict):
            yield container["result"]
        for value in node.values():
            yield from _iter_tweet_results(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_tweet_results(item)


def _unwrap_tweet(result: Any) -> Optional[dict]:
    """Return the ``legacy``-bearing tweet object, unwrapping wrappers."""
    if not isinstance(result, dict):
        return None
    if "legacy" in result:
        return result
    # ``TweetWithVisibilityResults`` nests the real tweet under ``tweet``.
    inner = result.get("tweet")
    if isinstance(inner, dict) and "legacy" in inner:
        return inner
    return None


def _extract_author(result: dict, legacy: dict) -> tuple[str, str]:
    """Return ``(display_name, screen_name)`` for a GraphQL tweet result."""
    user = None
    core = result.get("core")
    if isinstance(core, dict):
        user = core.get("user_results", {}).get("result")
    if not isinstance(user, dict):
        user = result.get("author")  # older response shape

    name = ""
    screen = ""
    if isinstance(user, dict):
        user_core = user.get("core") or {}
        user_legacy = user.get("legacy") or {}
        name = user_core.get("name") or user_legacy.get("name") or ""
        screen = user_core.get("screen_name") or user_legacy.get("screen_name") or ""
    return name or "Unknown", screen or "unknown"


def _extract_media(legacy: dict) -> tuple[list[str], list[str]]:
    """Split the tweet's media into ``(photo_urls, mp4_urls)``."""
    entities = legacy.get("extended_entities") or legacy.get("entities") or {}
    media = entities.get("media") or []
    photos: list[str] = []
    videos: list[str] = []

    for item in media:
        if not isinstance(item, dict):
            continue
        media_type = item.get("type")
        if media_type == "photo":
            url = item.get("media_url_https") or item.get("media_url")
            if url:
                photos.append(url)
        elif media_type in ("video", "animated_gif"):
            variants = (item.get("video_info") or {}).get("variants") or []
            mp4s = [
                v
                for v in variants
                if isinstance(v, dict)
                and v.get("content_type") == "video/mp4"
                and v.get("url")
            ]
            if mp4s:
                best = max(mp4s, key=lambda v: v.get("bitrate", 0) or 0)
                videos.append(best["url"])
    return photos, videos


def _clean_text(text: str, legacy: dict) -> str:
    """Strip trailing t.co media links and normalise whitespace."""
    entities = legacy.get("extended_entities") or legacy.get("entities") or {}
    for item in entities.get("media") or []:
        short = item.get("url") if isinstance(item, dict) else None
        if short:
            text = text.replace(short, "")
    # Collapse the blank lines left behind by the removed media links.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()




def _extract_text(result: dict, legacy: dict) -> str:
    """Return the tweet body, preferring long-form ``note_tweet`` text."""
    text = legacy.get("full_text") or legacy.get("text") or ""
    note = result.get("note_tweet")
    if isinstance(note, dict):
        note_result = (note.get("note_tweet_results") or {}).get("result")
        if isinstance(note_result, dict) and note_result.get("text"):
            text = note_result["text"]
    return text


def _extract_metrics(result: Optional[dict], legacy: dict) -> dict[str, int]:
    """Best-effort engagement counters used by the For-you quality filters.

    ``result`` is the GraphQL tweet object (carries ``view_count``); legacy is
    the v1.1-style ``legacy`` dict (carries the favourite/retweet counters).
    Values that are missing or malformed become ``0``.
    """

    def _as_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    impressions = 0
    if isinstance(result, dict):
        view = result.get("view_count")
        if isinstance(view, dict):
            impressions = _as_int(view.get("count"))
    return {
        "likes": _as_int(legacy.get("favorite_count")),
        "retweets": _as_int(legacy.get("retweet_count")),
        "impressions": impressions,
    }


def _build_tweet(result: Any, _depth: int = 0) -> Optional[dict]:
    """Convert a raw GraphQL tweet result into the clean public dict shape.

    Retweets are unwrapped so the user receives the original author's content
    and media, tagged with ``is_retweet``/``retweeter_username``.
    """
    inner = _unwrap_tweet(result)
    if inner is None or _depth > 3:
        return None

    legacy = inner.get("legacy") or {}
    tweet_id = legacy.get("id_str")
    if not tweet_id and legacy.get("id") is not None:
        tweet_id = str(legacy["id"])
    if not tweet_id:
        return None

    # -- retweet: forward the original tweet instead of the "RT @x:" wrapper.
    retweeted = legacy.get("retweeted_status_result")
    if isinstance(retweeted, dict) and isinstance(retweeted.get("result"), dict):
        original = _build_tweet(retweeted["result"], _depth + 1)
        if original is not None:
            _, retweeter = _extract_author(inner, legacy)
            original["is_retweet"] = True
            original["retweeter_username"] = retweeter
            return original

    text = _extract_text(inner, legacy)
    name, screen = _extract_author(inner, legacy)
    photos, videos = _extract_media(legacy)

    tweet: dict[str, Any] = {
        "id": tweet_id,
        "text": _clean_text(text, legacy),
        "author_name": name,
        "author_username": screen,
        "link": f"https://x.com/{screen}/status/{tweet_id}",
        "photos": photos,
        "videos": videos,
        "is_retweet": False,
        "retweeter_username": None,
        "quoted_text": None,
        "quoted_author": None,
    }

    # -- quoted tweet: keep the quoted body so context is not lost.
    quoted = legacy.get("quoted_status_result")
    if isinstance(quoted, dict) and isinstance(quoted.get("result"), dict):
        quoted_tweet = _build_tweet(quoted["result"], _depth + 1)
        if quoted_tweet and quoted_tweet.get("text"):
            tweet["quoted_text"] = quoted_tweet["text"]
            tweet["quoted_author"] = quoted_tweet["author_username"]

    tweet["metrics"] = _extract_metrics(inner, legacy)
    return tweet



def _build_tweet_v11(item: Any, _depth: int = 0) -> Optional[dict]:
    """Convert a legacy v1.1 timeline item into the clean public dict shape."""
    if not isinstance(item, dict) or _depth > 3:
        return None

    # Unwrap retweets (v1.1 nests the original under ``retweeted_status``).
    retweeted = item.get("retweeted_status")
    if isinstance(retweeted, dict):
        original = _build_tweet_v11(retweeted, _depth + 1)
        if original is not None:
            original["is_retweet"] = True
            original["retweeter_username"] = (item.get("user") or {}).get("screen_name")
            return original

    tweet_id = item.get("id_str")
    if not tweet_id and item.get("id") is not None:
        tweet_id = str(item["id"])
    if not tweet_id:
        return None

    user = item.get("user") or {}
    screen = user.get("screen_name") or "unknown"
    photos, videos = _extract_media(item)

    tweet: dict[str, Any] = {
        "id": tweet_id,
        "text": _clean_text(item.get("full_text") or item.get("text") or "", item),
        "author_name": user.get("name") or "Unknown",
        "author_username": screen,
        "link": f"https://x.com/{screen}/status/{tweet_id}",
        "photos": photos,
        "videos": videos,
        "is_retweet": False,
        "retweeter_username": None,
        "quoted_text": None,
        "quoted_author": None,
    }

    quoted = item.get("quoted_status")
    if isinstance(quoted, dict):
        quoted_text = quoted.get("full_text") or quoted.get("text") or ""
        if quoted_text:
            tweet["quoted_text"] = _clean_text(quoted_text, quoted)
            tweet["quoted_author"] = (quoted.get("user") or {}).get("screen_name")

    # v1.1 payloads do not expose impression counts, so they stay at 0 here.
    tweet["metrics"] = _extract_metrics(None, item)
    return tweet


def _parse_graphql_tweets(payload: dict) -> list[dict]:
    """Extract a de-duplicated list of tweets from a GraphQL payload."""
    tweets: list[dict] = []
    seen: set[str] = set()
    for result in _iter_tweet_results(payload):
        tweet = _build_tweet(result)
        if tweet is not None and tweet["id"] not in seen:
            seen.add(tweet["id"])
            tweets.append(tweet)
    return tweets


def _parse_v11_tweets(payload: list) -> list[dict]:
    """Extract a de-duplicated list of tweets from a v1.1 timeline list."""
    tweets: list[dict] = []
    seen: set[str] = set()
    if not isinstance(payload, list):
        return tweets
    for item in payload:
        tweet = _build_tweet_v11(item)
        if tweet is not None and tweet["id"] not in seen:
            seen.add(tweet["id"])
            tweets.append(tweet)
    return tweets


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class TwitterClient:
    """Async X/Twitter client authenticating with ``auth_token`` + ``ct0``."""

    def __init__(self, timeout: float = 30.0) -> None:
        # HTTP/2 + a browser UA keep the TLS/HTTP fingerprint close to Chrome.
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=True,
        )

    # -- lifecycle ---------------------------------------------------------
    async def close(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> "TwitterClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # -- public API --------------------------------------------------------
    async def verify_credentials(self, auth_token: str, ct0: str) -> bool:
        """Return ``True`` when the cookies map to a live X session.

        The legacy v1.1 ``account/settings.json`` endpoint that used to serve
        this check has been deprecated/blocked by X and made *valid* sessions
        fail during onboarding, so verification now mirrors the real polling
        path instead:

        1. **Primary** - GraphQL ``HomeTimeline`` with ``count=1``. A 200 with
           a ``data`` payload proves the session is authenticated; an empty
           timeline is still a perfectly valid session.
        2. **Fallback** - v1.1 ``account/verify_credentials.json``, the
           dedicated session-check endpoint (not ``account/settings.json``).

        Only authentication failures (401/403) are reported as ``False``.
        Endpoint-shape churn (404/405/429/5xx) is treated as "could not
        verify" and accepts the cookies rather than blocking initial setup -
        the scheduler surfaces any genuine session death later. Real
        transport failures still raise :class:`TwitterClientError` so callers
        can distinguish "bad cookies" from "X is unreachable".
        """
        headers = build_headers(auth_token, ct0)

        # 1) Modern GraphQL check - the same call the timeline poller makes.
        try:
            payload = await self._graphql_home_timeline(headers, 1)
        except TwitterAuthError:
            return False
        except TwitterRateLimitError:
            # Reaching the rate limiter still means the session authenticated.
            return True
        except TwitterClientError as exc:
            logger.info("GraphQL verification failed (%s) - trying fallback.", exc)
        else:
            if isinstance(payload, dict) and "data" in payload:
                return True
            logger.info("GraphQL verification returned no data - trying fallback.")

        # 2) Fallback: canonical v1.1 session check (NOT account/settings.json).
        url = f"{V11_BASE}/account/verify_credentials.json"
        try:
            response = await self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise TwitterClientError(f"Network error while contacting X: {exc}") from exc

        if response.status_code == 200:
            # With follow_redirects an unauthenticated cookie jar can end up
            # 200 on the login page - that is a rejected session, not a valid one.
            if "/login" in str(response.url):
                return False
            return True
        if response.status_code in (401, 403):
            return False

        logger.warning(
            "Verification endpoint returned HTTP %s - accepting the cookies; "
            "the scheduler will surface any genuine session problem.",
            response.status_code,
        )
        return True

    async def fetch_home_timeline(
        self, auth_token: str, ct0: str, count: int = 20
    ) -> list[dict]:
        """Return the user's home timeline as clean tweet dictionaries.

        The modern GraphQL endpoint is attempted first; if it yields nothing
        (query-id drift, soft failure) the legacy v1.1 REST endpoint is used as
        a fallback. Authentication errors are never swallowed.
        """
        headers = build_headers(auth_token, ct0)

        try:
            payload = await self._graphql_home_timeline(headers, count)
            tweets = _parse_graphql_tweets(payload)
            if tweets:
                return tweets
            logger.warning("GraphQL HomeTimeline returned no tweets - trying v1.1.")
        except TwitterAuthError:
            raise
        except TwitterError as exc:
            logger.warning("GraphQL HomeTimeline failed (%s) - trying v1.1.", exc)

        return await self._v11_home_timeline(headers, count)



    async def fetch_following_timeline(
        self, auth_token: str, ct0: str, count: int = 20
    ) -> list[dict]:
        """Return tweets from the chronological "Following" timeline.

        The modern GraphQL ``HomeLatestTimeline`` operation - the one behind
        X's Following tab - is the only source for this feed; unlike the
        algorithmic home timeline there is no v1.1 equivalent. Endpoint-shape
        churn (query-id drift) surfaces as :class:`TwitterClientError`, which
        the scheduler logs and skips for that cycle.
        """
        headers = build_headers(auth_token, ct0)
        variables = dict(_HOME_TIMELINE_VARIABLES)
        variables["count"] = count
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(
                {k: v for k, v in FEATURES.items() if v is not False},
                separators=(",", ":"),
            ),
        }
        url = f"{GRAPHQL_BASE}/{HOME_LATEST_TIMELINE_QUERY_ID}/HomeLatestTimeline"
        try:
            response = await self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise TwitterClientError(
                f"Network error talking to X GraphQL: {exc}"
            ) from exc

        try:
            _raise_for_status(response)
        except (TwitterAuthError, TwitterRateLimitError):
            raise
        except TwitterError as exc:
            # Query-id drift shows up as a 4xx here - degrade gracefully so
            # the scheduler can skip this feed for the cycle.
            raise TwitterClientError(
                f"HomeLatestTimeline request failed: {exc}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise TwitterClientError(
                "X returned a non-JSON GraphQL response."
            ) from exc
        return _parse_graphql_tweets(payload)

    async def send_not_interested(
        self, auth_token: str, ct0: str, tweet_id: str
    ) -> None:
        """Report "Not interested" for ``tweet_id`` to X.

        Fires X's timeline-feedback mutation (``timelinesFeedback``, the POST
        operation behind the post ⋯ menu's feedback entries) with
        ``feedback_type: "NotInterested"``. The variables are sent both as
        query parameters (the shape x.com itself uses for POSTs) and in the
        JSON body, so the call survives either server-side convention.

        Best-effort by design: X rotates GraphQL query ids periodically, so a
        rejected mutation is logged as a warning instead of failing the
        Telegram-side acknowledgement. Only a dead session (401/403) is
        raised so callers can still notice expired cookies.
        """
        headers = build_headers(auth_token, ct0)
        variables = {
            "tweet_id": tweet_id,
            "feedback_type": "NotInterested",
        }
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(
                {k: v for k, v in FEATURES.items() if v is not False},
                separators=(",", ":"),
            ),
        }
        url = f"{GRAPHQL_BASE}/{FEEDBACK_QUERY_ID}/timelinesFeedback"
        try:
            response = await self._client.post(
                url,
                params=params,
                headers=headers,
                json={"variables": variables, "queryId": FEEDBACK_QUERY_ID},
            )
        except httpx.HTTPError as exc:
            raise TwitterClientError(
                f"Network error talking to X GraphQL: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise TwitterAuthError(
                "Twitter rejected the session while sending feedback "
                f"(HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            logger.warning(
                "Timeline feedback rejected for tweet %s (HTTP %s): %s",
                tweet_id,
                response.status_code,
                response.text[:200],
            )
            return
        # GraphQL reports semantic failures inside a 200 body - surface them.
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("errors"):
            logger.warning(
                "timelinesFeedback errors for tweet %s: %s",
                tweet_id,
                json.dumps(payload["errors"])[:300],
            )
            return
        logger.info("Sent NotInterested feedback for tweet %s.", tweet_id)

    # -- internals ---------------------------------------------------------
    async def _graphql_home_timeline(self, headers: dict, count: int) -> dict:
        """Call the GraphQL HomeTimeline operation and return the raw JSON."""
        variables = dict(_HOME_TIMELINE_VARIABLES)
        variables["count"] = count
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(
                {k: v for k, v in FEATURES.items() if v is not False},
                separators=(",", ":"),
            ),
        }
        url = f"{GRAPHQL_BASE}/{HOME_TIMELINE_QUERY_ID}/HomeTimeline"
        try:
            response = await self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise TwitterClientError(f"Network error talking to X GraphQL: {exc}") from exc

        _raise_for_status(response)
        try:
            return response.json()
        except ValueError as exc:
            raise TwitterClientError("X returned a non-JSON GraphQL response.") from exc

    async def _v11_home_timeline(self, headers: dict, count: int) -> list[dict]:
        """Fallback: fetch the timeline through the legacy v1.1 endpoint."""
        params = {
            "count": count,
            "tweet_mode": "extended",
            "include_entities": True,
            "include_ext_alt_text": True,
        }
        url = f"{V11_BASE}/statuses/home_timeline.json"
        try:
            response = await self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise TwitterClientError(f"Network error talking to X v1.1: {exc}") from exc

        _raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TwitterClientError("X returned a non-JSON v1.1 response.") from exc
        return _parse_v11_tweets(payload)



# ---------------------------------------------------------------------------
# Shared client + module-level convenience functions (public API)
# ---------------------------------------------------------------------------
_shared_client: Optional[TwitterClient] = None


async def get_client() -> TwitterClient:
    """Return the process-wide shared :class:`TwitterClient` (created lazily)."""
    global _shared_client
    if _shared_client is None:
        _shared_client = TwitterClient()
        logger.info("Created shared TwitterClient (HTTP/2 connection pool).")
    return _shared_client


async def verify_credentials(auth_token: str, ct0: str) -> bool:
    """Validate a user's Twitter cookies. Returns ``True`` when still valid."""
    client = await get_client()
    return await client.verify_credentials(auth_token, ct0)


async def fetch_home_timeline(
    auth_token: str, ct0: str, count: int = 20
) -> list[dict]:
    """Fetch the home timeline for a single user's cookies."""
    client = await get_client()
    return await client.fetch_home_timeline(auth_token, ct0, count=count)


async def fetch_following_timeline(
    auth_token: str, ct0: str, count: int = 20
) -> list[dict]:
    """Fetch the chronological "Following" timeline for one user's cookies."""
    client = await get_client()
    return await client.fetch_following_timeline(auth_token, ct0, count=count)


async def send_not_interested(auth_token: str, ct0: str, tweet_id: str) -> None:
    """Best-effort "Not interested" feedback for ``tweet_id`` on X."""
    client = await get_client()
    return await client.send_not_interested(auth_token, ct0, tweet_id)


async def close_client() -> None:
    """Close the shared client (called on graceful shutdown)."""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.close()
        _shared_client = None

