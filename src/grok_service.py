"""Per-user Persian translation via the xAI Grok API.

Each user supplies their *own* API key (collected by the onboarding wizard and
stored per-user in SQLite). This module never reads a global key - it is always
handed the key of the user who pressed the translate button, which is what keeps
billing and the translation cache completely isolated between users.

The xAI endpoint is OpenAI-compatible, so the official ``openai`` SDK is used
with ``base_url="https://api.x.ai/v1"``.
"""

from __future__ import annotations

import logging

import openai
from openai import AsyncOpenAI

from .config import get_settings

logger = logging.getLogger(__name__)

# xAI's OpenAI-compatible base URL.
XAI_BASE_URL = "https://api.x.ai/v1"

# The system prompt asks for a faithful, natural Persian rendering while
# leaving machine-readable tokens (links, mentions, hashtags) untouched.
SYSTEM_PROMPT = (
    "You are a professional translator. Translate the user's tweet from its "
    "original language into fluent, natural, everyday Persian (Farsi). "
    "Rules:\n"
    "1. Preserve all URLs, @mentions and #hashtags exactly as they are.\n"
    "2. Keep emojis and numbers unchanged.\n"
    "3. Do not add explanations, commentary, notes or quotation marks.\n"
    "4. Output ONLY the translated text."
)

USER_PROMPT_TEMPLATE = "Tweet:\n---\n{tweet}\n---\n\nPersian translation:"


class GrokError(Exception):
    """Base class for Grok-related failures."""


class GrokAuthError(GrokError):
    """Raised when the user's xAI API key is missing, invalid or unauthorised."""


class GrokRateLimitError(GrokError):
    """Raised when xAI rate-limits the user's key."""


def _create_client(user_api_key: str) -> AsyncOpenAI:
    """Build an ephemeral AsyncOpenAI client for a single user key."""
    return AsyncOpenAI(
        api_key=user_api_key,
        base_url=XAI_BASE_URL,
        timeout=60.0,
        max_retries=1,
    )


async def translate_tweet(text: str, user_api_key: str) -> str:
    """Translate ``text`` into Persian using the caller's Grok API key.

    Raises :class:`GrokAuthError` for bad keys, :class:`GrokRateLimitError` for
    429s and :class:`GrokError` for anything else so the caller can show a
    friendly, specific message to the user.
    """
    if not user_api_key or not user_api_key.strip():
        raise GrokAuthError("No xAI API key was provided.")

    settings = get_settings()
    client = _create_client(user_api_key.strip())
    try:
        response = await client.chat.completions.create(
            model=settings.xai_model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(tweet=text)},
            ],
        )
    except openai.AuthenticationError as exc:
        raise GrokAuthError(
            "xAI rejected the API key (authentication failed)."
        ) from exc
    except openai.PermissionDeniedError as exc:
        raise GrokAuthError(
            "The xAI API key is not permitted to use this model."
        ) from exc
    except openai.RateLimitError as exc:
        raise GrokRateLimitError("xAI rate limit reached.") from exc
    except openai.APIError as exc:
        raise GrokError(f"xAI API error: {exc}") from exc
    finally:
        # Release the connection pool for this short-lived client.
        await client.close()

    if not response.choices:
        raise GrokError("xAI returned an empty response.")

    translation = (response.choices[0].message.content or "").strip()
    if not translation:
        raise GrokError("xAI returned an empty translation.")
    return translation
