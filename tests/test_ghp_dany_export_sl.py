import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import simulate_current_stack_with_ghp_60sessions as replay
from app.ghp_parser import parse_ghp_message


@pytest.fixture
def stub_execution(monkeypatch):
    now = datetime(2026, 9, 7, 8, tzinfo=UTC)
    frame = pd.DataFrame({'time':pd.to_datetime([now],utc=True),'open':[4300.]})
    monkeypatch.setattr(replay,'_resolve_symbol',lambda asset:'XAUUSD+')
    monkeypatch.setattr(replay,'_rates',lambda *a:frame)
    monkeypatch.setattr(replay,'calc_loss_per_lot',lambda *a:1000.)
    monkeypatch.setattr(replay,'simulate_ghp',lambda *a:dict(status='tp1',entry=4300.,sl=4290.,pnl_001=5.,opened=now,closed=now))
    return now


def test_ghp_export_preserves_initial_sl(tmp_path,monkeypatch,stub_execution):
    now=stub_execution
    signal=SimpleNamespace(asset='gold',side='buy',entries=[4300.],sl=4290.,tps=[4305.,4310.],order_kind='limit')
    monkeypatch.setattr(replay,'parse_ghp_message',lambda *a:SimpleNamespace(signal=signal))
    monkeypatch.setattr(replay,'_attach_actions',lambda rows:None)
    monkeypatch.setattr(replay,'_deduplicate',lambda rows:(rows,0))
    path=tmp_path/'messages.jsonl'
    path.write_text(json.dumps(dict(channel='ghptrading',date=now.isoformat(),title='GHP',text='signal',message_id=1)))
    events=replay._ghp_events(path,now,now)
    assert len(events)==3
    assert all(e['sl']==4290. and e['loss_per_lot']==1000. for e in events)


def test_dany_export_preserves_initial_sl(tmp_path,monkeypatch,stub_execution):
    now=stub_execution
    signal=SimpleNamespace(asset='gold',side='buy',entries=[4300.],sl=4290.,tps=[4305.,4310.],order_kind='limit')
    monkeypatch.setattr(replay,'_parse_signal',lambda *a:signal)
    monkeypatch.setattr(replay,'_relay_content_signature',lambda *a:'signature')
    monkeypatch.setattr(replay,'_channel_strategy',lambda *a:SimpleNamespace(split_target_indices=(1,2),split_protect_modes=('none','be'),target_index=1,protect_mode='none',pending_expiry_minutes=60))
    path=tmp_path/'messages.json'
    path.write_text(json.dumps(dict(messages=[dict(id=1,date=now.isoformat(),text='signal')])))
    events=replay._dany_events(path,now,now)
    assert len(events)==2
    assert all(e['sl']==4290. and e['loss_per_lot']==1000. for e in events)


def test_take_profit_brand_is_not_close_instruction():
    assert parse_ghp_message('150% bonusu do pierwszej wplaty. Jedna aplikacja, TAKE PROFIT.').kind=='commentary'
    assert parse_ghp_message('Take profit now!').kind=='close'
    assert parse_ghp_message('Close all positions now').kind=='close'
