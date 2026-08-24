from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import get_settings


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        print(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first. "
            "Both are free: https://my.telegram.org -> API development tools."
        )
        sys.exit(1)
    try:
        from telethon import TelegramClient
    except ImportError:
        print("Telethon is not installed. Run: python -m pip install -e '.[telegram]'")
        sys.exit(1)

    client = TelegramClient(
        settings.telegram_session_file,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start()
    me = await client.get_me()
    print(
        f"Authorized as {getattr(me, 'first_name', 'user')} "
        f"(@{getattr(me, 'username', '?')}). Session saved to "
        f"{settings.telegram_session_file}."
    )
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
