"""Read Telegram history and rebuild research events without placing orders."""
import asyncio
import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.utils import get_peer_id
import MetaTrader5 as mt5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import load_settings
from scripts.simulate_current_stack_with_ghp_60sessions import _ghp_events, _dany_events

TOKENS = ['ghptrading', '-1001958009741', '-1003306025363', '-1003495213392', '-1004410781005']
START = datetime(2026, 6, 22, tzinfo=UTC)
END = datetime(2026, 9, 13, tzinfo=UTC)


async def fetch(cfg):
    client = TelegramClient(str(ROOT/'data_vantage/channel_parser_audit_20260904'), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    rows, coverage = [], {}
    try:
        assert await client.is_user_authorized(), 'Research Telegram session is not authorized'
        for token in TOKENS:
            entity = await client.get_entity(int(token) if token.startswith('-') else token)
            count = 0
            earliest = None
            async for message in client.iter_messages(entity, offset_date=END):
                if message.date < START:
                    break
                rows.append(dict(channel=token, chat_id=get_peer_id(entity), title=entity.title, message_id=message.id, id=message.id,
                                 date=message.date.isoformat(), text=message.raw_text or '',
                                 edit_date=message.edit_date.isoformat() if message.edit_date else None,
                                 reply_to=message.reply_to_msg_id))
                count += 1
                earliest = message.date.isoformat()
            coverage[token] = dict(messages=count, earliest=earliest)
            print(json.dumps({token:coverage[token]}),flush=True)
    finally:
        await client.disconnect()
    return rows, coverage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cached', action='store_true')
    args = parser.parse_args()
    load_dotenv(ROOT/'.env.vantage', override=True)
    load_dotenv(ROOT/'.env.vantage.signal', override=True)
    cfg = load_settings()
    ghp = ROOT/'data_vantage/ghp_refreshed_60sessions_20260913.jsonl'
    dany = ROOT/'data_vantage/dany_refreshed_60sessions_20260913.json'
    if args.cached:
        rows = [json.loads(line) for line in ghp.read_text(encoding='utf-8').splitlines()]
        rows += json.loads(dany.read_text(encoding='utf-8'))['messages']
        for row in rows:
            row.setdefault('chat_id', -1002033681012 if row['channel']=='ghptrading' else int(row['channel']))
        coverage = {t:dict(messages=sum(r['channel']==t for r in rows), earliest=min((r['date'] for r in rows if r['channel']==t),default=None)) for t in TOKENS}
    else:
        rows, coverage = asyncio.run(fetch(cfg))
    ghp.write_text(''.join(json.dumps(r)+'\n' for r in rows if r['channel'] != TOKENS[-1]),encoding='utf-8')
    dany.write_text(json.dumps(dict(messages=[r for r in rows if r['channel']==TOKENS[-1]])),encoding='utf-8')
    assert mt5.initialize(path=cfg.mt5_path), mt5.last_error()
    try:
        assert mt5.account_info().trade_mode == 0, 'Demo research only'
        events = _ghp_events(ghp, START, END, set(TOKENS[:-1])) + _dany_events(dany, START, END)
    finally:
        mt5.shutdown()
    for e in events:
        assert e['sl'] > 0
        e.pop('fixed_lot', None)
    result = dict(start=START.isoformat(),end=END.isoformat(),coverage=coverage,
                  counts=dict(Counter(e['module'] for e in events)),events=events,
                  limitations=['Final edited Telegram text only; simplified action/BE simulation, not full live-engine parity.'])
    out=ROOT/'reports/ghp_dany_rebuilt_60sessions_20260913.json'
    out.write_text(json.dumps(result,default=lambda x:x.isoformat(),indent=2),encoding='utf-8')
    print(json.dumps(result['counts']),flush=True)


if __name__=='__main__':
    main()
