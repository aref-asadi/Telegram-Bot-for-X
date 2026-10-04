"""Interactive onboarding wizard and management commands.

The wizard walks a user through, in order:

1. ``/start``            - greeting + explanation.
2. ``auth_token``        - Persian guide to copy the cookie from DevTools.
3. ``ct0``               - Persian guide + *live* verification against X.
4. ``xai_api_key``       - optional Grok key, with an inline "skip" button.

Everything is stored **per chat** (``chat_id``) in SQLite, so several users can
share one bot instance without any data crossover.

Management commands handled outside the conversation:
``/status``, ``/pause``, ``/resume``, ``/set_grok``, ``/logout``.
"""

from __future__ import annotations

import logging
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
    TwitterClientError,
    verify_credentials,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
AUTH_TOKEN, CT0, GROK_KEY, SET_GROK_KEY = range(4)

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
    "برای شروع باید دو مقدار از کوکی‌های مرورگر خود را ثبت کنید: "
    "<b>auth_token</b> و <b>ct0</b>.\n\n"
    "🔐 <b>نکتهٔ امنیتی:</b> این اطلاعات فقط روی همین سرور و در یک دیتابیس "
    "محلی ذخیره می‌شود و به هیچ سرور دیگری ارسال نمی‌گردد."
)

ALREADY_REGISTERED = (
    "ℹ️ شما قبلاً پروفایل خود را ساخته‌اید.\n"
    "اگر ادامه دهید، کوکی‌های قبلی با مقادیر جدید جایگزین می‌شوند.\n"
    "برای انصراف دستور /cancel را بزنید."
)

AUTH_TOKEN_GUIDE = (
    "📌 <b>مرحلهٔ ۱ از ۳ — گرفتن auth_token</b>\n\n"
    "۱) در مرورگر (کروم/فایرفاکس) وارد سایت <b>x.com</b> شوید.\n"
    "۲) کلید <b>F12</b> را بزنید تا Developer Tools باز شود.\n"
    "۳) به تب <b>Application</b> بروید (در فایرفاکس: <b>Storage</b>).\n"
    "۴) از منوی سمت چپ: <b>Storage → Cookies → https://x.com</b>\n"
    "۵) مقدار کوکیِ <b>auth_token</b> را کپی کنید.\n"
    "۶) آن را همین‌جا برای من بفرستید. 👇\n\n"
    "⏭️ برای انصراف دستور /cancel را بزنید."
)

CT0_GUIDE = (
    "📌 <b>مرحلهٔ ۲ از ۳ — گرفتن ct0</b>\n\n"
    "در همان صفحهٔ کوکی‌ها (که قبلاً باز کردید):\n"
    "۱) دنبال کوکیِ <b>ct0</b> بگردید.\n"
    "۲) مقدار آن را کپی کنید و همین‌جا بفرستید. 👇\n\n"
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

VERIFY_NETWORK_ERROR = (
    "⚠️ در برقراری ارتباط با توییتر خطایی رخ داد.\n"
    "لطفاً مقدار ct0 را دوباره بفرستید یا کمی بعد تلاش کنید."
)

GROK_GUIDE = (
    "📌 <b>مرحلهٔ ۳ از ۳ — هوش مصنوعی گروک (اختیاری)</b>\n\n"
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


# ---------------------------------------------------------------------------
# Conversation entry / control
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: greet the user and begin the credential wizard."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    name = (user.first_name if user else None) or "دوست"

    db = _get_db(context)
    existing = await db.get_user(chat_id)
    _reset_wizard(context)

    if existing is not None:
        # Already onboarded: explain that continuing replaces the cookies.
        await _send(context, chat_id, ALREADY_REGISTERED)
        await _send(context, chat_id, AUTH_TOKEN_GUIDE)
    else:
        await _send(context, chat_id, WELCOME.format(name=name))
        await _send(context, chat_id, AUTH_TOKEN_GUIDE)
    return AUTH_TOKEN


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
async def auth_token_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """State 1: capture and softly validate the ``auth_token`` cookie."""
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()

    # Remove the message containing the secret from the chat history.
    await _safe_delete(update.message)

    if not _looks_like_token(raw):
        await _send(context, chat_id, INVALID_AUTH_TOKEN_FORMAT)
        return AUTH_TOKEN

    context.user_data["auth_token"] = raw
    await _send(context, chat_id, CT0_GUIDE)
    return CT0


async def ct0_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """State 2: capture ``ct0`` and live-verify both cookies against X."""
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
        await _send(context, chat_id, AUTH_TOKEN_GUIDE)
        return AUTH_TOKEN

    await _send(context, chat_id, VERIFYING)
    try:
        valid = await verify_credentials(auth_token, raw)
    except TwitterAuthError:
        valid = False
    except TwitterClientError as exc:
        logger.warning("Verification failed for chat %s: %s", chat_id, exc)
        await _send(context, chat_id, VERIFY_NETWORK_ERROR)
        return CT0

    if not valid:
        # Bad cookies: tell the user and start the cookie steps over.
        _reset_wizard(context)
        await _send(context, chat_id, INVALID_COOKIES)
        await _send(context, chat_id, AUTH_TOKEN_GUIDE)
        return AUTH_TOKEN

    # Cookies are valid - remember ct0 and offer the optional Grok setup.
    context.user_data["ct0"] = raw
    await _send(context, chat_id, GROK_GUIDE, reply_markup=SKIP_GROK_KEYBOARD)
    return GROK_KEY



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

    if not auth_token or not ct0:
        # Defensive: the session data vanished, restart from step 1.
        _reset_wizard(context)
        await _send(context, chat_id, AUTH_TOKEN_GUIDE)
        return AUTH_TOKEN

    db = _get_db(context)
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
                AUTH_TOKEN: [MessageHandler(text_input, auth_token_received)],
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

