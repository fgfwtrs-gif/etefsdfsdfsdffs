from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

from .app import build_application
from .settings import load_settings


def _clear_broken_local_proxies() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        value = os.environ.get(key, "")
        if value.strip().lower() == "http://127.0.0.1:9":
            os.environ.pop(key, None)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    load_dotenv()
    _clear_broken_local_proxies()
    settings = load_settings()
    if not settings.bot.token:
        raise RuntimeError("BOT_TOKEN is empty. Fill .env or config.toml before starting the bot.")
    app = build_application(settings)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
