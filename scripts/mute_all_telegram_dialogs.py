from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--session-suffix", default="mute_all")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    session_name = os.getenv("TELEGRAM_SESSION_NAME", "telegram_signal_bot")
    source = data_dir / f"{session_name}.session"
    session_path = data_dir / f"{session_name}_{args.session_suffix}.session"
    if source.exists():
        shutil.copy2(source, session_path)

    client = TelegramClient(
        str(session_path.with_suffix("").resolve()),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session is not authorized")

    mute_until = datetime(2038, 1, 18, tzinfo=UTC)
    settings = types.InputPeerNotifySettings(
        show_previews=False,
        silent=True,
        mute_until=mute_until,
    )
    categories = {
        "groups": types.InputNotifyChats(),
        "channels": types.InputNotifyBroadcasts(),
    }
    for name, peer in categories.items():
        while True:
            try:
                await client(
                    functions.account.UpdateNotifySettingsRequest(
                        peer=peer,
                        settings=settings,
                    )
                )
                break
            except FloodWaitError as exc:
                print(f"flood_wait_seconds={exc.seconds}", flush=True)
                await asyncio.sleep(exc.seconds + 1)
        print(f"muted_category={name}", flush=True)

    await client.disconnect()
    print("muted_categories=2 errors=0")


if __name__ == "__main__":
    asyncio.run(main())
