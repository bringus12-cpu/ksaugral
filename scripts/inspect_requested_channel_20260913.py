"""Read-only inspection of the user-linked Telegram message and its context."""
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import load_settings
from app.ghp_parser import parse_ghp_message


async def main():
    load_dotenv(ROOT/'.env.vantage',override=True)
    cfg=load_settings()
    client=TelegramClient(str(ROOT/'data_vantage/channel_parser_audit_20260904'),cfg.telegram_api_id,cfg.telegram_api_hash)
    await client.connect()
    try:
        assert await client.is_user_authorized()
        entity=await client.get_entity(-1001798665296)
        messages=await client.get_messages(entity,ids=list(range(2938,2964)))
        rows=[]
        for m in messages:
            if m is None:
                continue
            parsed=parse_ghp_message(m.raw_text or '',entity.title)
            rows.append(dict(id=m.id,date=m.date.isoformat(),text=m.raw_text,reply_to=m.reply_to_msg_id,
                             edited=m.edit_date.isoformat() if m.edit_date else None,
                             parsed_kind=parsed.kind,signal=vars(parsed.signal) if parsed.signal else None))
        result=dict(title=entity.title,username=getattr(entity,'username',None),chat_id=-1001798665296,
                    requested_message=2948,messages=rows)
        out=ROOT/'reports/channel_1798665296_message_2948_20260913.json'
        out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=True,indent=2))
    finally:
        await client.disconnect()


if __name__=='__main__':
    asyncio.run(main())
