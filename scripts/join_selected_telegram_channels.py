from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.errors import UserAlreadyParticipantError


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--session-suffix", default="")
    parser.add_argument("--leave", action="store_true")
    parser.add_argument("channels", nargs="+")
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    session_name = os.getenv("TELEGRAM_SESSION_NAME", "telegram_signal_bot")
    source = data_dir / f"{session_name}.session"
    if args.session_suffix:
        session_path = data_dir / f"{session_name}_{args.session_suffix}.session"
        if source.exists() and not session_path.exists():
            shutil.copy2(source, session_path)
    else:
        session_path = source
    client = TelegramClient(
        str(session_path.with_suffix("").resolve()),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session is not authorized")

    for channel in args.channels:
        try:
            if args.leave:
                await client(functions.channels.LeaveChannelRequest(channel))
                print(f"left {channel}")
            else:
                await client(functions.channels.JoinChannelRequest(channel))
                print(f"joined {channel}")
        except UserAlreadyParticipantError:
            print(f"already_joined {channel}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
