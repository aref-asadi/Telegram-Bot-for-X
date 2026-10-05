"""Interactive onboarding wizard and management commands.

The wizard walks a user through, in order:

1. ``/start``            - greeting + bookmarklet installer link (manual
                            cookie instructions when PUBLIC_BASE_URL is unset).
2. Bookmarklet handover  - ``/start auth_<code>`` deep link produced by the
                            browser bookmarklet, or a single message holding
                            both cookies (bookmarklet clipboard fallback).
3. ``xai_api_key``       - optional Grok key, with an inline "skip" button.

Cookies are validated *live* against X before they are persisted and are
stored **per chat** (``chat_id``) in SQLite, so several users can share one bot
instance without any data crossover.

Management commands handled outside the conversation:
``/status``, ``/pause``, ``/resume``, ``/set_grok``, ``/logout``.
"""

from __future__ import annotations

import logging
import re
import warnings
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.warnings import PTBUserWarning
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import get_settings
from .database import Database
from .twitter_client import (
    TwitterAuthError,
    TwitterError,
    verify_credentials,
)
from .web_server import pop_pending_auth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
# BOOKMARKLET: waiting for the deep-link handover code or a pasted payload.
# CT0:          manual fallback - auth_token arrived, ct0 still missing.
BOOKMARKLET, CT0, GROK_KEY, SET_GROK_KEY = range(4)

# Only private chats are supported - this guarantees the 1:1 mapping between a
# Telegram chat_id and a single user's credentials.
PRIVATE = filters.ChatType.PRIVATE

SKIP_GROK_CALLBACK = "skip_grok"
SKIP_GROK_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton(
                "⏭️ رد کردن / فعلاً بدون هوش مصنوعی",
                callback_data=SKIP_GROK_CALLBACK,
            )
        ]
    ]
)


# ---------------------------------------------------------------------------
# Persian copy
# ---------------------------------------------------------------------------
WELCOME = (
    "👋 سلام {name} عزیز!\n\n"
    "به ربات «فید توییتر ← تلگرام» خوش آمدید. 🌐\n\n"
    "این ربات به‌صورت خودکار تایم‌لاین خانهٔ اکانت توییتر/ایکس شما را به همین "
    "چت می‌فرستد.\n\n"
    "برای شروع فقط کافیست با یک بوکمارکلت یک‌کلیکی، کوکی‌های اکانت "
    "<b>auth_token</b> و <b>ct0</b> را ثبت کنید — دیگر خبری از DevTools نیست! "
    "البته روش دستی هم پابرجاست.\n\n"
    "🔐 <b>نکتهٔ امنیتی:</b> این اطلاعات فقط روی همین سرور و در یک دیتابیس "
    "محلی ذخیره می‌شود و به هیچ سرور دیگری ارسال نمی‌گردد."
)

ALREADY_REGISTERED = (
    "ℹ️ شما قبلاً پروفایل خود را ساخته‌اید.\n"
    "اگر ادامه دهید، کوکی‌های قبلی با مقادیر جدید جایگزین می‌شوند.\n"
    "برای انصراف دستور /cancel را بزنید."
)

BOOKMARKLET_GUIDE = (
    "📌 <b>مرحلهٔ ۱ — اتصال با یک کلیک</b>\n\n"
    "۱) نوار بوکمارک‌ها را نمایش دهید: <b>Ctrl+Shift+B</b>\n"
    "۲) روی دکمهٔ زیر بزنید تا صفحهٔ نصب باز شود؛ سپس دکمهٔ "
    "<b>«⚡ اتصال X ← تلگرام»</b> را به نوار بوکمارک‌ها بکشید (Drag).\n"
    "۳) وارد <a href=\"https://x.com\">x.com</a> شوید و با اکانت خود لاگین "
    "باشید.\n"
    "۴) روی بوکمارکلت در نوار بوکمارک‌ها کلیک کنید.\n"
    "۵) چند لحظه بعد خودکار به همین چت برمی‌گردید و اتصال کامل می‌شود. ✅\n\n"
    "🔄 <b>روش جایگزین:</b> اگر بوکمارکلت کار نکرد، هر دو کوکی را از DevTools "
    "(<b>F12</b> → Application → Cookies → x.com) کپی کرده و <b>در یک پیام</b> "
    "بفرستید:\n"
    "<code>auth_token=...</code>\n"
    "<code>ct0=...</code>\n\n"
    "⏭️ برای انصراف دستور /cancel را بزنید."
)

MANUAL_GUIDE = (
    "📌 <b>مرحلهٔ ۱ — ثبت دستی کوکی‌ها</b>\n\n"
    "۱) در مرورگر (کروم/فایرفاکس) وارد سایت <b>x.com</b> شوید و لاگین کنید.\n"
    "۲) کلید <b>F12</b> را بزنید تا Developer Tools باز شود.\n"
    "۳) به تب <b>Application</b> بروید (در فایرفاکس: <b>Storage</b>).\n"
    "۴) از منوی سمت چپ: <b>Storage → Cookies → https://x.com</b>\n"
    "۵) مقادیر کوکی‌های <b>auth_token</b> و <b>ct0</b> را کپی کنید.\n"
    "۶) هر دو را <b>در یک پیام</b> با همین فرمت بفرستید: 👇\n"
    "<code>auth_token=...</code>\n"
    "<code>ct0=...</code>\n\n"
    "⏭️ برای انصراف دستور /cancel را بزنید."
)

CT0_GUIDE = (
    "📌 <b>مرحلهٔ ۱ (ادامه) — کوکی ct0</b>\n\n"
    "در همان صفحهٔ کوکی‌ها (F12 → Application → Cookies → x.com):\n"
    "۱) دنبال کوکیِ <b>ct0</b> بگردید.\n"
    "۲) مقدار آن را کپی کرده و همین‌جا بفرستید. 👇\n\n"
    "💡 <i>ct0 همان توکن CSRF است.</i>"
)

VERIFYING = "⏳ در حال بررسی کوکی‌ها با توییتر... چند لحظه صبر کنید."

INVALID_COOKIES = (
    "❌ کوکی‌ها نامعتبرند یا منقضی شده‌اند. لطفاً دوباره وارد کنید."
)

INVALID_AUTH_TOKEN_FORMAT = (
    "⚠️ مقدار <b>auth_token</b> معتبر به نظر نمی‌رسد.\n"
    "مقداری که کپی می‌کنید معمولاً یک رشتهٔ طولانی از حروف و اعداد است.\n"
    "لطفاً دوباره تلاش کنید. 👇"
)

INVALID_CT0_FORMAT = (
    "⚠️ مقدار <b>ct0</b> معتبر به نظر نمی‌رسد.\n"
    "لطفاً مقدار کامل کوکیِ ct0 را کپی کرده و دوباره بفرستید. 👇"
)

INVALID_PAYLOAD = (
    "❌ نتوانستم کوکی‌ها را از این پیام بخوانم.\n\n"
    "لطفاً هر دو مقدار را با همان فرمت در یک پیام بفرستید:\n"
    "<code>auth_token=...</code>\n"
    "<code>ct0=...</code>\n\n"
    "یا بوکمارکلت را روی x.com اجرا کنید."
)

HANDOVER_EXPIRED = (
    "⌛ کد اتصال منقضی شده، قبلاً استفاده شده یا متعلق به این چت نیست.\n\n"
    "دوباره روی بوکمارکلت در x.com کلیک کنید تا کد تازه‌ای ساخته شود."
)

AUTH_VERIFIED = "✅ هویت شما تأیید شد و کوکی‌ها ذخیره شدند."

VERIFY_NETWORK_ERROR = (
    "⚠️ در برقراری ارتباط با توییتر خطایی رخ داد.\n"
    "لطفاً چند لحظه بعد دوباره تلاش کنید یا کوکی‌ها را دوباره بفرستید."
)

GROK_GUIDE = (
    "📌 <b>مرحلهٔ آخر — هوش مصنوعی گروک (اختیاری)</b>\n\n"
    "اگر دوست دارید هر توییت را با یک کلیک به فارسی روان ترجمه کنید، کلید API "
    "گروک (xAI) خودتان را اینجا ثبت کنید.\n\n"
    "<b>چطور کلید بگیرم؟</b>\n"
    "۱) به سایت <b>console.x.ai</b> بروید و ثبت‌نام کنید.\n"
    "۲) از بخش <b>API Keys</b> یک کلید بسازید (با پیشوند <code>xai-</code>).\n"
    "۳) کلید را کپی کرده و همین‌جا بفرستید. 👇\n\n"
    "اگر الان نمی‌خواهید، روی دکمهٔ زیر بزنید. بعداً هم می‌توانید با دستور "
    "/set_grok آن را ثبت کنید."
)

INVALID_GROK_KEY = (
    "⚠️ این کلید معتبر به نظر نمی‌رسد. کلیدهای xAI معمولاً با <code>xai-</code> "
    "شروع می‌شوند.\n"
    "لطفاً کلید را دوباره بفرستید یا روی دکمهٔ «رد کردن» بزنید. 👇"
)

SET_GROK_GUIDE = (
    "🔑 <b>ثبت/تغییر کلید گروک</b>\n\n"
    "کلید API خود از <b>console.x.ai</b> را بفرستید تا برای ترجمهٔ توییت‌ها "
    "استفاده شود. 👇\n\n"
    "⏭️ برای انصراف دستور /cancel را بزنید."
)

GROK_SAVED = "✅ کلید گروک شما با موفقیت ذخیره شد."
GROK_CLEARED_HINT = "ℹ️ بدون هوش مصنوعی ادامه می‌دهیم. هر زمان خواستید دستور /set_grok را بزنید."


def _success_message(interval: int) -> str:
    """Build the post-onboarding success text (interval is dynamic)."""
    return (
        "✅ عالی! پروفایل شما ذخیره شد و ربات فعال گردید. 🎉\n\n"
        "از این پس توییت‌های جدید تایم‌لاین شما به‌طور خودکار به این چت ارسال "
        "می‌شود.\n"
        f"⏱️ بازهٔ بررسی تایم‌لاین: هر <b>{interval}</b> ثانیه یک‌بار.\n\n"
        "<b>دستورهای مفید:</b>\n"
        "• /status — نمایش وضعیت\n"
        "• /pause — توقف ارسال\n"
        "• /resume — ادامهٔ ارسال\n"
        "• /set_grok — ثبت یا تغییر کلید گروک\n"
        "• /logout — حذف کامل اطلاعات"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    """Fetch the shared :class:`Database` from ``bot_data``."""
    return context.bot_data["db"]


async def _send(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs
) -> Optional[object]:
    """Send an HTML message, swallowing Telegram errors.

    Wizard handlers must never crash on a transient Telegram failure, so this
    helper logs and returns ``None`` instead of propagating.
    """
    kwargs.setdefault("link_preview_options", LinkPreviewOptions(is_disabled=True))
    try:
        return await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, **kwargs
        )
    except TelegramError as exc:
        logger.error("Failed to send message to %s: %s", chat_id, exc)
        return None


async def _safe_delete(message) -> None:
    """Best-effort deletion of a message (used to scrub secrets from chat)."""
    try:
        await message.delete()
    except TelegramError:
        # Deletion may be disallowed or the message may already be gone.
        pass


def _reset_wizard(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop any half-collected credentials from the per-user session store."""
    if context.user_data is not None:
        context.user_data.pop("auth_token", None)
        context.user_data.pop("ct0", None)


def _looks_like_token(value: str, min_length: int = 20) -> bool:
    """Light sanity check for cookie/token shaped strings."""
    if not value or len(value) < min_length:
        return False
    if any(ch.isspace() for ch in value):
        return False
    return all(ch.isalnum() or ch in "-_." for ch in value)


# Shapes of the two cookies as they appear inside a payload.
_AUTH_VALUE_RE = re.compile(r"auth_token[\"']?\s*[=:]\s*[\"']?([0-9a-fA-F]{40})")
_CT0_VALUE_RE = re.compile(r"ct0[\"']?\s*[=:]\s*[\"']?([0-9A-Za-z_-]{20,160})")
# Bare handover code, with or without the deep-link "auth_" prefix.
_HANDOVER_CODE_RE = re.compile(r"(?:auth_)?([0-9a-f]{12})\Z")


def _bookmarklet_guide(
    chat_id: int,
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Return the step-1 guide text plus its install button (if configured).

    Falls back to the manual DevTools instructions when the server has no
    PUBLIC_BASE_URL - the bookmarklet cannot POST without one.
    """
    base = get_settings().public_base_url.strip().rstrip("/")
    if not base:
        return MANUAL_GUIDE, None
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🧩 صفحهٔ نصب بوکمارکلت",
                    url=f"{base}/bookmarklet?c={chat_id}",
                )
            ]
        ]
    )
    return BOOKMARKLET_GUIDE, keyboard


def _parse_credentials_payload(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Extract ``(auth_token, ct0)`` from a bookmarklet/manual paste.

    Accepts every shape the flows can produce: ``key=value`` / ``key: value``
    pairs (line-, ``;``- or JSON-separated), or two bare tokens on separate
    lines (the clipboard payload without keys). A half that is missing or
    malformed comes back as ``None``.
    """
    text = (raw or "").strip()
    if not text:
        return None, None

    auth_token: Optional[str] = None
    ct0: Optional[str] = None

    match = _AUTH_VALUE_RE.search(text)
    if match:
        auth_token = match.group(1).lower()
    match = _CT0_VALUE_RE.search(text)
    if match:
        ct0 = match.group(1)

    if auth_token is None and ct0 is None:
        # Two bare tokens (e.g. the clipboard payload pasted as-is).
        parts = [
            p.strip().strip("\"',;")
            for p in re.split(r"[\r\n]+", text)
            if p.strip()
        ]
        if len(parts) == 2:
            for part in parts:
                if auth_token is None and re.fullmatch(r"[0-9a-fA-F]{40}", part):
                    auth_token = part.lower()
                elif ct0 is None and _looks_like_token(part):
                    ct0 = part

    return auth_token, ct0


def _match_handover_code(raw: str) -> Optional[str]:
    """Return the bare 12-hex handover code in ``raw`` (or ``None``)."""
    match = _HANDOVER_CODE_RE.fullmatch((raw or "").strip().lower())
    return match.group(1) if match else None


async def _accept_credentials(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, auth_token: str, ct0: str
) -> None:
    """Persist freshly verified cookies so polling can start immediately.

    Saving up-front (before the optional Grok step) means closing the app at
    any later point never loses a successful connection.
    """
    db = _get_db(context)
    await db.upsert_user(chat_id, auth_token, ct0, None)
    context.user_data["auth_token"] = auth_token
    context.user_data["ct0"] = ct0
    await _send(context, chat_id, AUTH_VERIFIED)
    _kick_first_poll(context, chat_id)


def _kick_first_poll(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Poll the timeline once right away instead of waiting a full interval.

    This only seeds ``last_tweet_id`` (or forwards what appeared meanwhile);
    the scheduler keeps polling on its normal cadence afterwards.
    """
    from .scheduler import poll_user  # deferred: keeps module import light

    db = _get_db(context)
    bot = context.bot

    async def _run() -> None:
        try:
            user = await db.get_user(chat_id)
            if user is not None and user.is_active:
                await poll_user(bot, db, user, get_settings().max_tweets_per_poll)
        except Exception as exc:  # noqa: BLE001 - best-effort confirmation
            logger.warning(
                "Immediate first poll failed for chat %s: %s", chat_id, exc
            )

    context.application.create_task(_run())


async def _offer_grok_or_finish(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Ask for the optional Grok key, or finish right away when one exists."""
    chat_id = update.effective_chat.id
    db = _get_db(context)
    existing = await db.get_user(chat_id)
    if existing is not None and existing.xai_api_key:
        # Already linked to Grok - no need to ask again.
        return await _finalize(update, context, None)
    await _send(context, chat_id, GROK_GUIDE, reply_markup=SKIP_GROK_KEYBOARD)
    return GROK_KEY


# ---------------------------------------------------------------------------
# Conversation entry / control
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: redeem a bookmarklet handover or show the installer guide."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    name = (user.first_name if user else None) or "دوست"

    # Deep link from the bookmarklet: /start auth_<12-hex-code>.
    args = context.args or []
    if args and _match_handover_code(args[0]) is not None:
        return await _redeem_handover(update, context, args[0])

    db = _get_db(context)
    existing = await db.get_user(chat_id)
    _reset_wizard(context)

    if existing is not None:
        # Already onboarded: explain that continuing replaces the cookies.
        await _send(context, chat_id, ALREADY_REGISTERED)
    else:
        await _send(context, chat_id, WELCOME.format(name=name))

    guide, keyboard = _bookmarklet_guide(chat_id)
    await _send(context, chat_id, guide, reply_markup=keyboard)
    return BOOKMARKLET


async def _redeem_handover(
    update: Update, context: ContextTypes.DEFAULT_TYPE, token: str
) -> int:
    """Consume the one-time code produced by the bookmarklet's ``POST /auth``.

    The cookies were already verified server-side when the code was minted, so
    accepting them here only needs a successful, unexpired, chat-matching pop.
    """
    chat_id = update.effective_chat.id
    cleaned = (token or "").strip().lower()
    if cleaned.startswith("auth_"):
        cleaned = cleaned[len("auth_"):]
    if not re.fullmatch(r"[0-9a-f]{12}", cleaned):
        cleaned = ""

    creds = pop_pending_auth(cleaned) if cleaned else None
    if creds is not None:
        bound = creds.get("chat_id")
        if bound is not None and bound != chat_id:
            # Wrong chat. The code is already burned so an attacker cannot
            # retry it; the legitimate owner simply clicks the bookmark again.
            logger.warning(
                "Handover code rejected: chat %s is not the bound chat.", chat_id
            )
            creds = None

    if creds is None:
        await _send(context, chat_id, HANDOVER_EXPIRED)
        guide, keyboard = _bookmarklet_guide(chat_id)
        await _send(context, chat_id, guide, reply_markup=keyboard)
        return BOOKMARKLET

    await _accept_credentials(context, chat_id, creds["auth_token"], creds["ct0"])
    return await _offer_grok_or_finish(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fallback: abort the wizard and clear any collected credentials."""
    _reset_wizard(context)
    await _send(
        context,
        update.effective_chat.id,
        "❌ عملیات لغو شد. هر زمان خواستید با دستور /start دوباره شروع کنید.",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Wizard steps
# ---------------------------------------------------------------------------
async def credentials_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """State 1: accept a one-shot payload (bookmarklet fallback or manual).

    Everything arrives in a single message - a bare handover code, the
    clipboard payload (``auth_token=...\\nct0=...``), JSON, or both cookies
    pasted from DevTools - and is parsed in one step.
    """
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()

    # Remove the message containing the secret from the chat history.
    await _safe_delete(update.message)

    # A bare handover code (e.g. copied from the bookmarklet's alert).
    if _match_handover_code(raw) is not None:
        return await _redeem_handover(update, context, raw)

    auth_token, ct0 = _parse_credentials_payload(raw)

    if auth_token and not ct0:
        # Manual user sent auth_token first - ask for ct0 only.
        context.user_data["auth_token"] = auth_token
        if re.search(r"ct0", raw, re.IGNORECASE):
            await _send(context, chat_id, INVALID_CT0_FORMAT)
        else:
            await _send(context, chat_id, CT0_GUIDE)
        return CT0

    if not auth_token or not ct0:
        if re.search(r"auth_token", raw, re.IGNORECASE):
            await _send(context, chat_id, INVALID_AUTH_TOKEN_FORMAT)
        elif re.search(r"ct0", raw, re.IGNORECASE):
            await _send(context, chat_id, INVALID_CT0_FORMAT)
        else:
            await _send(context, chat_id, INVALID_PAYLOAD)
        guide, keyboard = _bookmarklet_guide(chat_id)
        await _send(context, chat_id, guide, reply_markup=keyboard)
        return BOOKMARKLET

    await _send(context, chat_id, VERIFYING)
    try:
        valid = await verify_credentials(auth_token, ct0)
    except TwitterAuthError:
        valid = False
    except TwitterError as exc:
        logger.warning("Verification failed for chat %s: %s", chat_id, exc)
        await _send(context, chat_id, VERIFY_NETWORK_ERROR)
        return BOOKMARKLET

    if not valid:
        # Bad cookies: tell the user and offer the guide again.
        _reset_wizard(context)
        await _send(context, chat_id, INVALID_COOKIES)
        guide, keyboard = _bookmarklet_guide(chat_id)
        await _send(context, chat_id, guide, reply_markup=keyboard)
        return BOOKMARKLET

    # Cookies are valid - persist them and offer the optional Grok setup.
    await _accept_credentials(context, chat_id, auth_token, ct0)
    return await _offer_grok_or_finish(update, context)


async def ct0_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """State 1b (manual only): capture ``ct0`` and live-verify both cookies."""
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()
    await _safe_delete(update.message)

    if not _looks_like_token(raw):
        await _send(context, chat_id, INVALID_CT0_FORMAT)
        return CT0

    auth_token: Optional[str] = context.user_data.get("auth_token")
    if not auth_token:
        # Session data was lost (e.g. bot restart) - restart from step 1.
        _reset_wizard(context)
        guide, keyboard = _bookmarklet_guide(chat_id)
        await _send(context, chat_id, guide, reply_markup=keyboard)
        return BOOKMARKLET

    await _send(context, chat_id, VERIFYING)
    try:
        valid = await verify_credentials(auth_token, raw)
    except TwitterAuthError:
        valid = False
    except TwitterError as exc:
        logger.warning("Verification failed for chat %s: %s", chat_id, exc)
        await _send(context, chat_id, VERIFY_NETWORK_ERROR)
        return CT0

    if not valid:
        # Bad cookies: tell the user and start the cookie steps over.
        _reset_wizard(context)
        await _send(context, chat_id, INVALID_COOKIES)
        guide, keyboard = _bookmarklet_guide(chat_id)
        await _send(context, chat_id, guide, reply_markup=keyboard)
        return BOOKMARKLET

    # Cookies are valid - persist them and offer the optional Grok setup.
    await _accept_credentials(context, chat_id, auth_token, raw)
    return await _offer_grok_or_finish(update, context)



def _looks_like_grok_key(value: str) -> bool:
    """Basic shape check for an xAI API key (``xai-...``)."""
    return bool(value) and value.startswith("xai-") and len(value) >= 20


async def _finalize(
    update: Update, context: ContextTypes.DEFAULT_TYPE, grok_key: Optional[str]
) -> int:
    """Persist the collected profile and finish the conversation."""
    chat_id = update.effective_chat.id
    auth_token: Optional[str] = context.user_data.get("auth_token")
    ct0: Optional[str] = context.user_data.get("ct0")
    db = _get_db(context)

    if not auth_token or not ct0:
        # Session data vanished (e.g. bot restart). The cookies were already
        # persisted when they were accepted, so recover them from SQLite.
        row = await db.get_user(chat_id)
        if row is not None and row.auth_token and row.ct0:
            auth_token, ct0 = row.auth_token, row.ct0
        else:
            # Never connected in the first place - restart from step 1.
            _reset_wizard(context)
            guide, keyboard = _bookmarklet_guide(chat_id)
            await _send(context, chat_id, guide, reply_markup=keyboard)
            return BOOKMARKLET

    await db.upsert_user(chat_id, auth_token, ct0, grok_key)
    _reset_wizard(context)

    interval = get_settings().poll_interval_seconds
    await _send(context, chat_id, _success_message(interval))
    return ConversationHandler.END


async def grok_key_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """State 3: capture the optional Grok API key and finish onboarding."""
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()
    await _safe_delete(update.message)

    if not _looks_like_grok_key(raw):
        await _send(context, chat_id, INVALID_GROK_KEY)
        return GROK_KEY

    return await _finalize(update, context, raw)


async def grok_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """State 3 (inline button / ``/skip``): finish onboarding without a key."""
    chat_id = update.effective_chat.id
    if update.callback_query is not None:
        await update.callback_query.answer()
    await _send(context, chat_id, GROK_CLEARED_HINT)
    return await _finalize(update, context, None)


async def set_grok_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point of ``/set_grok``: ask for a (new) key."""
    chat_id = update.effective_chat.id
    db = _get_db(context)
    user = await db.get_user(chat_id)
    if user is None:
        await _send(
            context,
            chat_id,
            "ℹ️ ابتدا با دستور /start پروفایل خود را بسازید و کوکی‌های توییتر را ثبت کنید.",
        )
        return ConversationHandler.END
    await _send(context, chat_id, SET_GROK_GUIDE)
    return SET_GROK_KEY


async def set_grok_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store a Grok key supplied through ``/set_grok``."""
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()
    await _safe_delete(update.message)

    if not _looks_like_grok_key(raw):
        await _send(context, chat_id, INVALID_GROK_KEY)
        return SET_GROK_KEY

    db = _get_db(context)
    await db.set_grok_key(chat_id, raw)
    await _send(context, chat_id, GROK_SAVED)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Management commands
# ---------------------------------------------------------------------------
_NOT_REGISTERED = "ℹ️ ابتدا با دستور /start پروفایل خود را بسازید."

HELP_TEXT = (
    "🤖 <b>راهنمای ربات فید توییتر ← تلگرام</b>\n\n"
    "/start — شروع و ثبت کوکی‌های توییتر\n"
    "/status — نمایش وضعیت ربات\n"
    "/pause — توقف ارسال توییت‌ها\n"
    "/resume — ادامهٔ ارسال توییت‌ها\n"
    "/set_grok — ثبت یا تغییر کلید گروک\n"
    "/logout — حذف کامل اطلاعات\n"
    "/cancel — لغو عملیات جاری"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the command reference."""
    await _send(context, update.effective_chat.id, HELP_TEXT)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report whether forwarding is active and whether Grok is linked."""
    chat_id = update.effective_chat.id
    db = _get_db(context)
    user = await db.get_user(chat_id)
    if user is None:
        await _send(context, chat_id, _NOT_REGISTERED)
        return

    status_line = "✅ فعال" if user.is_active else "⏸️ متوقف"
    grok_line = "✅ متصل" if user.xai_api_key else "➖ متصل نیست"
    last_tweet = user.last_tweet_id or "—"

    text = (
        "📊 <b>وضعیت ربات</b>\n\n"
        f"• وضعیت ارسال: <b>{status_line}</b>\n"
        "• کوکی توییتر: ✅ ثبت شده\n"
        f"• هوش مصنوعی گروک: {grok_line}\n"
        f"• آخرین توییت ارسالی: <code>{last_tweet}</code>\n"
        f"• بازهٔ بررسی: هر {get_settings().poll_interval_seconds} ثانیه"
    )
    await _send(context, chat_id, text)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable forwarding for this user."""
    chat_id = update.effective_chat.id
    db = _get_db(context)
    if await db.get_user(chat_id) is None:
        await _send(context, chat_id, _NOT_REGISTERED)
        return
    await db.set_active(chat_id, False)
    await _send(
        context,
        chat_id,
        "⏸️ ارسال توییت‌ها متوقف شد.\nبرای ادامه دستور /resume را بزنید.",
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-enable forwarding for this user."""
    chat_id = update.effective_chat.id
    db = _get_db(context)
    if await db.get_user(chat_id) is None:
        await _send(context, chat_id, _NOT_REGISTERED)
        return
    await db.set_active(chat_id, True)
    await _send(context, chat_id, "▶️ ارسال توییت‌ها از سر گرفته شد.")


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe every credential and cached translation for this user."""
    chat_id = update.effective_chat.id
    db = _get_db(context)
    if await db.get_user(chat_id) is None:
        await _send(context, chat_id, _NOT_REGISTERED)
        return
    await db.delete_user(chat_id)
    _reset_wizard(context)
    await _send(
        context,
        chat_id,
        "🗑️ تمام اطلاعات شما (کوکی‌های توییتر، کلید گروک و ترجمه‌های ذخیره‌شده) "
        "حذف شد.\nبرای شروع دوباره دستور /start را بزنید.",
    )


# ---------------------------------------------------------------------------
# Handler factories (wired up in main.py)
# ---------------------------------------------------------------------------
def get_conversation_handler() -> ConversationHandler:
    """Build the onboarding + ``/set_grok`` conversation handler.

    ``per_message`` is left ``False`` because the conversation mixes
    ``MessageHandler`` and ``CallbackQueryHandler`` (the skip button), which is
    exactly the case where per-message tracking must be disabled.
    """
    text_input = filters.TEXT & ~filters.COMMAND & PRIVATE

    with warnings.catch_warnings():
        # Mixing MessageHandler and CallbackQueryHandler inside one
        # conversation requires ``per_message=False``. PTB emits an
        # informational PTBUserWarning for that combination; it does not apply
        # here, so we silence exactly that warning.
        warnings.simplefilter("ignore", PTBUserWarning)
        return ConversationHandler(
            entry_points=[
                CommandHandler("start", start, filters=PRIVATE),
                CommandHandler("set_grok", set_grok_start, filters=PRIVATE),
            ],
            states={
                BOOKMARKLET: [MessageHandler(text_input, credentials_received)],
                CT0: [MessageHandler(text_input, ct0_received)],
                GROK_KEY: [
                    MessageHandler(text_input, grok_key_received),
                    CommandHandler("skip", grok_skip, filters=PRIVATE),
                    CallbackQueryHandler(grok_skip, pattern=f"^{SKIP_GROK_CALLBACK}$"),
                ],
                SET_GROK_KEY: [MessageHandler(text_input, set_grok_received)],
            },
            fallbacks=[
                CommandHandler("cancel", cancel, filters=PRIVATE),
                CommandHandler("start", start, filters=PRIVATE),
                CommandHandler("set_grok", set_grok_start, filters=PRIVATE),
            ],
            allow_reentry=True,
            per_message=False,
            name="onboarding_wizard",
        )


def get_management_handlers() -> list[CommandHandler]:
    """Build the standalone command handlers (registered in a lower group)."""
    return [
        CommandHandler("help", help_command, filters=PRIVATE),
        CommandHandler("status", status_command, filters=PRIVATE),
        CommandHandler("pause", pause_command, filters=PRIVATE),
        CommandHandler("resume", resume_command, filters=PRIVATE),
        CommandHandler("logout", logout_command, filters=PRIVATE),
    ]

