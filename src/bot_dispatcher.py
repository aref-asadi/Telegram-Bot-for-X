"""Tweet formatting and safe media dispatch.

Every forwarded tweet is rendered with a consistent Persian-friendly layout and,
wherever Telegram allows it, carries an inline
``[ 🌐 ترجمه با گروک ]`` button whose callback data is ``tr_<tweet_id>``.

Media rules (Telegram constraints respected):

* 2-10 photos  -> ``send_media_group`` (albums cannot carry a keyboard, so the
  caption goes on the first photo and a small follow-up message carries the
  source link + translate button).
* 1 photo      -> ``send_photo`` (caption + button).
* video/gif    -> ``send_video`` (highest-bitrate MP4 variant) with a link
  fallback if Telegram refuses the URL.
* text only    -> ``send_message`` with link preview enabled.

Captions are always truncated so the *visible* text stays within Telegram's
1024-character caption limit (limits are applied after entity parsing).
"""

from __future__ import annotations

import html
import logging
from collections import OrderedDict
from typing import Optional

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .database import Database
from .grok_service import (
    GrokAuthError,
    GrokError,
    GrokRateLimitError,
    translate_tweet,
)

logger = logging.getLogger(__name__)

# Telegram limits (counted after entity parsing, i.e. markup does not count).
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
MAX_ALBUM_SIZE = 10

TRANSLATE_CALLBACK_PREFIX = "tr_"
TRANSLATION_BUTTON_TEXT = "🌐 ترجمه با گروک"
NO_KEY_ALERT = (
    "⚠️ شما کلید گروک تنظیم نکرده‌اید.\n"
    "برای فعال‌سازی ترجمهٔ هوشمند، دستور /set_grok را اجرا کنید."
)

# ---------------------------------------------------------------------------
# Small in-memory registry of tweet bodies, keyed by tweet id.
# The DB only caches *translations*; to translate we need the original text,
# so we remember it here when a tweet is dispatched. Bounded to avoid growth.
# ---------------------------------------------------------------------------
_TWEET_TEXT_CACHE: "OrderedDict[str, str]" = OrderedDict()
_TWEET_TEXT_CACHE_MAX = 1000


def remember_tweet_text(tweet: dict) -> None:
    """Store a tweet's source text so the translate button can reuse it."""
    tweet_id = tweet.get("id")
    text = tweet.get("text") or tweet.get("quoted_text") or ""
    if not tweet_id or not text:
        return
    _TWEET_TEXT_CACHE[tweet_id] = text
    _TWEET_TEXT_CACHE.move_to_end(tweet_id)
    while len(_TWEET_TEXT_CACHE) > _TWEET_TEXT_CACHE_MAX:
        _TWEET_TEXT_CACHE.popitem(last=False)


def recall_tweet_text(tweet_id: str) -> Optional[str]:
    """Return a remembered tweet body, if still cached in memory."""
    return _TWEET_TEXT_CACHE.get(tweet_id)


def _escape(text: str) -> str:
    """Escape user-controlled text for Telegram's HTML parse mode."""
    return html.escape(text or "", quote=False)


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` characters, adding an ellipsis if cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def build_keyboard(tweet_id: str) -> InlineKeyboardMarkup:
    """Build the per-tweet inline keyboard (single translate button)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=TRANSLATION_BUTTON_TEXT,
                    callback_data=f"{TRANSLATE_CALLBACK_PREFIX}{tweet_id}",
                )
            ]
        ]
    )


def format_tweet_text(
    tweet: dict, limit: int = TEXT_LIMIT, include_link: bool = True
) -> str:
    """Render a tweet into an HTML-formatted message body.

    ``limit`` is the *visible* character budget (1024 for media captions, 4096
    for plain text messages). ``include_link`` is set to ``False`` for albums,
    where the source link is delivered in a separate follow-up message.
    """
    name = tweet.get("author_name") or "Unknown"
    username = tweet.get("author_username") or "unknown"
    retweeter = tweet.get("retweeter_username") or ""
    is_retweet = bool(tweet.get("is_retweet")) and bool(retweeter)
    link = tweet.get("link") or ""
    body_raw = tweet.get("text") or ""
    quoted = tweet.get("quoted_text") or ""
    quoted_author = tweet.get("quoted_author") or ""

    # -- plain-text skeleton (length accounting only) ----------------------
    header_lines: list[str] = []
    if is_retweet:
        header_lines.append(f"🔁 ریتوییت از @{retweeter}")
    header_lines.append(f"👤 {name} (@{username})")
    header_plain = "\n".join(header_lines)

    link_plain = f"\n\n🔗 {link}" if (link and include_link) else ""
    quote_plain = f"\n\n📎 نقل‌قول از @{quoted_author}:\n{quoted}" if quoted else ""
    separator_len = 2 if body_raw else 0

    fixed_len = len(header_plain) + separator_len + len(link_plain) + len(quote_plain)
    budget = max(0, limit - fixed_len)
    body = _truncate(body_raw, budget)

    # Defensive second pass in case the estimate was off by a character.
    for _ in range(5):
        if fixed_len + len(body) <= limit:
            break
        budget = max(0, budget - (fixed_len + len(body) - limit) - 1)
        body = _truncate(body_raw, budget)

    # -- HTML rendering ----------------------------------------------------
    parts: list[str] = []
    if is_retweet:
        parts.append(f"🔁 ریتوییت از @{_escape(retweeter)}")
    parts.append(f"👤 <b>{_escape(name)}</b> (@{_escape(username)})")
    if body:
        parts.append("")
        parts.append(_escape(body))
    if quoted:
        parts.append("")
        parts.append(f"📎 نقل‌قول از @{_escape(quoted_author)}:")
        parts.append(_escape(quoted))
    if link and include_link:
        parts.append("")
        parts.append(f"🔗 {_escape(link)}")
    return "\n".join(parts)



# ---------------------------------------------------------------------------
# Media dispatch
# ---------------------------------------------------------------------------
async def dispatch_tweet(bot: Bot, chat_id: int, tweet: dict) -> bool:
    """Deliver a single tweet to ``chat_id`` using the appropriate method.

    Returns ``True`` if at least one message was successfully delivered.
    """
    remember_tweet_text(tweet)

    tweet_id = tweet.get("id") or ""
    keyboard = build_keyboard(tweet_id)
    photos = tweet.get("photos") or []
    videos = tweet.get("videos") or []

    # 1) Videos / GIFs win - a tweet with video rarely has other useful media.
    if videos:
        return await _send_video(bot, chat_id, tweet, videos[0], keyboard)

    # 2) Photo album (2-10 images) via send_media_group.
    if len(photos) >= 2:
        return await _send_album(bot, chat_id, tweet, photos[:MAX_ALBUM_SIZE], keyboard)

    # 3) A single photo.
    if len(photos) == 1:
        return await _send_photo(bot, chat_id, tweet, photos[0], keyboard)

    # 4) Plain text with link preview.
    return await _send_text(bot, chat_id, tweet, keyboard)


async def _send_video(
    bot: Bot, chat_id: int, tweet: dict, video_url: str, keyboard: InlineKeyboardMarkup
) -> bool:
    """Send a video (or GIF) or fall back to a text message on failure."""
    caption = format_tweet_text(tweet, CAPTION_LIMIT)
    try:
        await bot.send_video(
            chat_id=chat_id,
            video=video_url,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            supports_streaming=True,
        )
        return True
    except TelegramError as exc:
        logger.warning(
            "send_video failed for tweet %s (%s) - falling back to text.",
            tweet.get("id"),
            exc,
        )
        return await _send_text(bot, chat_id, tweet, keyboard)


async def _send_photo(
    bot: Bot, chat_id: int, tweet: dict, photo_url: str, keyboard: InlineKeyboardMarkup
) -> bool:
    """Send a single photo with the caption, or fall back to text."""
    caption = format_tweet_text(tweet, CAPTION_LIMIT)
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo_url,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return True
    except TelegramError as exc:
        logger.warning(
            "send_photo failed for tweet %s (%s) - falling back to text.",
            tweet.get("id"),
            exc,
        )
        return await _send_text(bot, chat_id, tweet, keyboard)



async def _send_album(
    bot: Bot,
    chat_id: int,
    tweet: dict,
    photos: list[str],
    keyboard: InlineKeyboardMarkup,
) -> bool:
    """Send 2-10 photos as an album, then a follow-up message with the button.

    Telegram media groups cannot carry an inline keyboard, so the source link
    and the translate button are delivered in a separate short message.
    """
    caption = format_tweet_text(tweet, CAPTION_LIMIT, include_link=False)
    media: list[InputMediaPhoto] = []
    for index, url in enumerate(photos):
        if index == 0 and caption:
            media.append(
                InputMediaPhoto(media=url, caption=caption, parse_mode=ParseMode.HTML)
            )
        else:
            media.append(InputMediaPhoto(media=url))

    try:
        await bot.send_media_group(chat_id=chat_id, media=media)
    except TelegramError as exc:
        logger.warning(
            "send_media_group failed for tweet %s (%s) - falling back to text.",
            tweet.get("id"),
            exc,
        )
        return await _send_text(bot, chat_id, tweet, keyboard)

    link = tweet.get("link") or ""
    follow_up = f"🔗 {_escape(link)}" if link else TRANSLATION_BUTTON_TEXT
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=follow_up,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramError as exc:
        logger.warning("Album follow-up message failed for tweet %s: %s", tweet.get("id"), exc)
    return True


async def _send_text(
    bot: Bot, chat_id: int, tweet: dict, keyboard: InlineKeyboardMarkup
) -> bool:
    """Send a text-only tweet (or a media fallback) with a link preview."""
    text = format_tweet_text(tweet, TEXT_LIMIT)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=False),
        )
        return True
    except TelegramError as exc:
        logger.error("send_message failed for tweet %s: %s", tweet.get("id"), exc)
        return False



# ---------------------------------------------------------------------------
# Translate-button callback
# ---------------------------------------------------------------------------
def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    """Fetch the shared :class:`Database` from ``bot_data``."""
    return context.bot_data["db"]


def _extract_body_from_message(message: object) -> str:
    """Best-effort recovery of a tweet body from an already-sent message.

    Used only as a fallback when the in-memory body cache no longer holds the
    tweet (e.g. after a restart). Our own formatting lines are stripped out.
    """
    raw = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    skip = ("🔁 ", "👤 ", "📎 ", "🔗 ", "🌐 ")
    kept = [line for line in raw.splitlines() if not line.startswith(skip)]
    return "\n".join(kept).strip()


async def _send_translation(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    query,
    translation: str,
    *,
    cached: bool,
) -> None:
    """Post the Persian translation as a reply to the original tweet message."""
    badge = " ♻️" if cached else ""
    body = _truncate(translation, TEXT_LIMIT - 80)
    text = f"🌐 <b>ترجمهٔ فارسی گروک</b>{badge}\n\n{_escape(body)}"
    reply_to = query.message.message_id if query.message else None
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to,
            allow_sending_without_reply=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramError as exc:
        logger.error("Failed to deliver translation to %s: %s", chat_id, exc)


async def _send_translation_error(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, query, message: str
) -> None:
    """Deliver a short error explanation as a reply."""
    reply_to = query.message.message_id if query.message else None
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to,
            allow_sending_without_reply=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramError as exc:
        logger.error("Failed to deliver error message to %s: %s", chat_id, exc)


async def translate_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ``tr_<tweet_id>`` presses from the per-tweet translate button."""
    query = update.callback_query
    if query is None or not query.data:
        return

    tweet_id = query.data[len(TRANSLATE_CALLBACK_PREFIX):]
    chat_id = query.message.chat_id if query.message else query.from_user.id
    db = _get_db(context)

    # 1) The user must exist and must own a Grok key.
    user = await db.get_user(chat_id)
    if user is None:
        await query.answer(
            text="⚠️ ابتدا با ارسال /start پروفایل خود را بسازید.", show_alert=True
        )
        return
    if not user.xai_api_key:
        await query.answer(text=NO_KEY_ALERT, show_alert=True)
        return

    # 2) Recover the original tweet text.
    source_text = recall_tweet_text(tweet_id)
    if not source_text:
        source_text = _extract_body_from_message(query.message)
    if not source_text:
        await query.answer(
            text="⚠️ متن این توییت در دسترس نیست. لطفاً روی توییت تازه‌تری بزنید.",
            show_alert=True,
        )
        return

    # 3) Serve from cache when possible (never bill the user twice).
    cached = await db.get_cached_translation(tweet_id, chat_id)
    if cached:
        await query.answer()
        await _send_translation(context, chat_id, query, cached, cached=True)
        return

    # 4) Fresh translation - acknowledge first so the spinner stops promptly.
    await query.answer(text="⏳ در حال ترجمه با گروک...")
    try:
        translation = await translate_tweet(source_text, user.xai_api_key)
    except GrokAuthError:
        await _send_translation_error(
            context,
            chat_id,
            query,
            "❌ کلید گروک شما نامعتبر است یا منقضی شده.\n"
            "لطفاً با دستور /set_grok یک کلید تازه ثبت کنید.",
        )
        return
    except GrokRateLimitError:
        await _send_translation_error(
            context,
            chat_id,
            query,
            "⏳ سهمیهٔ شما برای گروک به‌طور موقت پر شده است. چند دقیقه بعد دوباره تلاش کنید.",
        )
        return
    except GrokError as exc:
        logger.error("Grok translation failed for chat_id=%s: %s", chat_id, exc)
        await _send_translation_error(
            context, chat_id, query, "❌ ترجمه ناموفق بود. لطفاً بعداً دوباره تلاش کنید."
        )
        return

    # 5) Persist then deliver.
    await db.save_translation(tweet_id, chat_id, translation)
    await _send_translation(context, chat_id, query, translation, cached=False)

