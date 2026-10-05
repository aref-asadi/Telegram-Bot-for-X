"""Per-user Persian translation via an OpenAI-compatible chat API.

Each user may supply their *own* API key (collected by the onboarding wizard
and stored per-user in SQLite). When the user has no personal key, the global
``AI_API_KEY`` from the environment is used instead - per-user keys still keep
billing and the translation cache completely isolated between users.

The endpoint is fully configurable (``AI_BASE_URL`` / ``AI_MODEL``), so any
OpenAI-compatible provider works: xAI's Grok (the default), OpenAI, OpenRouter
or a self-hosted vLLM/Ollama server.
"""

from __future__ import annotations

import logging

import openai
from openai import AsyncOpenAI

from .config import get_settings

logger = logging.getLogger(__name__)

# Default base URL - xAI's OpenAI-compatible endpoint, used when AI_BASE_URL
# is not configured.
DEFAULT_BASE_URL = "https://api.x.ai/v1"

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


def _create_client(api_key: str, base_url: str) -> AsyncOpenAI:
    """Build an ephemeral AsyncOpenAI client for a single request."""
    return AsyncOpenAI(
        api_key=api_key,
        base_url=(base_url or "").rstrip("/") or DEFAULT_BASE_URL,
        timeout=60.0,
        max_retries=1,
    )


async def translate_tweet(text: str, user_api_key: str | None = None) -> str:
    """Translate ``text`` into Persian.

    ``user_api_key`` is the key of the user who pressed the translate button;
    when it is missing/empty the global ``AI_API_KEY`` configured on the server
    falls back in. Raises :class:`GrokAuthError` for missing/bad keys,
    :class:`GrokRateLimitError` for 429s and :class:`GrokError` for anything
    else so the caller can show a friendly, specific message to the user.
    """
    settings = get_settings()
    api_key = (user_api_key or "").strip() or (settings.ai_api_key or "").strip()
    if not api_key:
        raise GrokAuthError(
            "No API key configured. Set a personal key with /key or ask the "
            "operator to configure AI_API_KEY."
        )

    client = _create_client(api_key, settings.ai_base_url)
    try:
        response = await client.chat.completions.create(
            model=settings.ai_model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(tweet=text)},
            ],
        )
    except openai.AuthenticationError as exc:
        raise GrokAuthError(
            "The API key was rejected (authentication failed)."
        ) from exc
    except openai.PermissionDeniedError as exc:
        raise GrokAuthError(
            "The API key is not permitted to use this model."
        ) from exc
    except openai.RateLimitError as exc:
        raise GrokRateLimitError("AI provider rate limit reached.") from exc
    except openai.APIError as exc:
        raise GrokError(f"AI provider error: {exc}") from exc
    finally:
        # Release the connection pool for this short-lived client.
        await client.close()

    if not response.choices:
        raise GrokError("The AI provider returned an empty response.")

    translation = (response.choices[0].message.content or "").strip()
    if not translation:
        raise GrokError("The AI provider returned an empty translation.")
    return translation
