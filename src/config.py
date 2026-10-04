"""Environment-backed configuration for the bot.

All values are read once at import time.  Only *infrastructure* settings live
here - per-user credentials (Twitter cookies / xAI keys) are intentionally NOT
part of the environment and are stored per-user inside SQLite instead.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env (development convenience). On hosts such as Render/Koyeb the
# variables are injected directly into the process environment, so missing
# .env files are perfectly fine.
# ---------------------------------------------------------------------------
load_dotenv(override=False)

# Project root (one level above this package) - used to resolve relative paths.
BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or malformed."""


def _get_env(name: str, default: str | None = None, *, required: bool = False) -> str:
    """Return the string value of ``name`` or ``default``.

    When ``required`` is True and the variable is missing/empty a
    :class:`ConfigError` is raised so the process fails fast on boot.
    """
    value = os.getenv(name, default)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if required:
            raise ConfigError(
                f"Required environment variable '{name}' is not set. "
                f"Copy .env.example to .env and fill it in."
            )
        return "" if value is None else value
    return value.strip()


def _get_int(name: str, default: int) -> int:
    """Return an int environment variable, falling back to ``default``."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"Environment variable '{name}' must be an integer, got '{raw}'.") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the runtime configuration."""

    bot_token: str
    port: int
    database_path: Path
    poll_interval_seconds: int
    max_tweets_per_poll: int
    xai_model: str
    log_level: str

    # Derived / constant values ------------------------------------------------
    @property
    def database_dir(self) -> Path:
        """Directory that must exist before SQLite can create its file."""
        return self.database_path.parent


def load_settings() -> Settings:
    """Build a :class:`Settings` object from the current environment."""
    # BOT_TOKEN is the only truly required value - validate it early.
    bot_token = _get_env("BOT_TOKEN", required=True)

    # Resolve DATABASE_PATH relative to the project root so the bot behaves
    # consistently no matter which working directory it is launched from.
    db_raw = _get_env("DATABASE_PATH", "data/bot.db")
    database_path = Path(db_raw)
    if not database_path.is_absolute():
        database_path = (BASE_DIR / database_path).resolve()

    settings = Settings(
        bot_token=bot_token,
        port=_get_int("PORT", 8000),
        database_path=database_path,
        poll_interval_seconds=max(30, _get_int("POLL_INTERVAL_SECONDS", 180)),
        max_tweets_per_poll=max(1, _get_int("MAX_TWEETS_PER_POLL", 20)),
        xai_model=_get_env("XAI_MODEL", "grok-3"),
        log_level=_get_env("LOG_LEVEL", "INFO").upper(),
    )
    return settings


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging with a compact, production-friendly format."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    # httpx logs every request at INFO which is far too noisy for production.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    Lazily evaluated (and cached) so importing this module never fails when
    ``BOT_TOKEN`` is absent - the error only surfaces the first time the bot
    actually needs configuration, i.e. at startup.
    """
    return load_settings()


# Public alias so callers can simply do ``from src.config import settings`` and
# still benefit from the caching/lazy behaviour above.
settings = get_settings
