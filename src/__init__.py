"""Telegram-Bot-for-X - stream a personal X/Twitter home timeline into Telegram.

This package contains the modular building blocks of the bot:

``config``          - Typed access to environment configuration.
``database``        - Async SQLite persistence (per-user credentials + cache).
``twitter_client``  - Cookie-authenticated X/Twitter timeline client.
``grok_service``    - per-user Persian translation through the xAI Grok API.
``bot_wizard``      - Interactive Persian onboarding wizard + management commands.
``bot_dispatcher``  - Tweet formatting and safe media dispatch + translate button.
``scheduler``       - Background polling loop that fans tweets out to each user.
``web_server``      - Tiny aiohttp health-check server for free hosting.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
