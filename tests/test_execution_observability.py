import json
from types import SimpleNamespace
from unittest.mock import patch, Mock

from app import mt5_gateway as gateway


def test_process_alive_does_not_mean_algo_ready():
    terminal = SimpleNamespace(connected=True, trade_allowed=False, tradeapi_disabled=False)
    account = SimpleNamespace(login=123, trade_allowed=True, trade_expert=True)
    with patch.object(gateway.mt5, 'terminal_info', return_value=terminal), patch.object(gateway.mt5, 'account_info', return_value=account):
        status = gateway.trading_status()
    assert not status['ready']
    assert status['blocked_reasons'] == ['algo_enabled']


def test_journal_records_actual_request_and_result_without_secrets(tmp_path):
    result = SimpleNamespace(retcode=10009, order=42, deal=43, price=4001.2, volume=.01, comment='done')
    tick = SimpleNamespace(bid=4001, ask=4001.2, time_msc=123456)
    request = {'symbol':'XAUUSD', 'price':4001.2, 'sl':3995, 'tp':4005, 'volume':.01, 'password':'secret'}
    with patch.dict('os.environ', {'DATA_DIR':str(tmp_path)}), patch.object(gateway.mt5,'symbol_info_tick',return_value=tick), patch.object(gateway.mt5,'order_send',return_value=result) as send:
        assert gateway._send_audited(request) is result
    send.assert_called_once_with(request)
    record = json.loads(next((tmp_path/'execution_journal').glob('*.jsonl')).read_text())
    assert record['request']['sl'] == 3995
    assert record['result']['order'] == 42
    assert record['quote']['ask'] == 4001.2
    assert 'secret' not in json.dumps(record)


def test_journal_failure_does_not_resend_order():
    result = SimpleNamespace(retcode=10009)
    with patch.object(gateway.mt5,'symbol_info_tick',return_value=None), patch.object(gateway.mt5,'order_send',return_value=result) as send, patch.object(gateway.Path,'mkdir',side_effect=OSError('disk')):
        assert gateway._send_audited({'symbol':'XAUUSD'}) is result
    assert send.call_count == 1
