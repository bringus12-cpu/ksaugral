"""Read-only research aggregation; never sends orders or changes trading profiles."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, shutdown
from scripts.simulate_current_stack_with_ghp_60sessions import _current_events, _ghp_events, _portfolio


def main():
    os.chdir(ROOT)
    for path in ('.env.vantage', '.env.vantage.signal'):
        load_dotenv(path, override=True)
    for name in ('range', 'direction', 'profit', 'extra_market'):
        os.environ['AUDIT_SOURCE_PHOENIX_' + name.upper()] = f'data_vantage/audit_phoenix_{name}_60_20260906.json'
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        start = datetime(2026, 6, 15, tzinfo=UTC)
        end = datetime(2026, 9, 6, tzinfo=UTC)
        events = _current_events(symbol, start, end,
            'data_vantage/audit_demo_db60_60_20260906.json',
            'data_vantage/audit_demo_bbkelt_60_20260906.json')
        events += _ghp_events(ROOT/'data_vantage/ghp_parser_audit_66sessions_20260904_fixed_messages.jsonl', start, end)
        events = [e for e in events if e['closed'] <= end]
        modules = {}
        for name in sorted({e['module'] for e in events}):
            rows = [e for e in events if e['module'] == name]
            normalized = [e['pnl_per_lot'] * .01 for e in rows]
            gains = sum(p for p in normalized if p > 0)
            losses = -sum(p for p in normalized if p < 0)
            modules[name] = {
                'positions': len(rows),
                'win_rate_pct': 100 * sum(p > 0 for p in normalized) / len(rows),
                'pnl_at_001': sum(normalized),
                'profit_factor_at_001': gains/losses if losses else None,
                'standalone_closed_balance_model': _portfolio(rows, cfg, 1000, 1.5, True),
                'execution_quality': 'provider-price replay, costs incomplete' if name.startswith('ghp:') else 'M1 model; historical edits and intrabar order unavailable',
            }
        combined = _portfolio(events, cfg, 1000, 1.5, True)
        without_ghp = _portfolio([e for e in events if not e['module'].startswith('ghp:')], cfg, 1000, 1.5, True)
    finally:
        shutdown()
    scalpers = {}
    for name in ('demo_db60', 'demo_bbkelt', 'live_db60', 'live_bbkelt', 'demo_db60_laterbe', 'demo_bbkelt_fast'):
        raw = json.loads((ROOT/f'data_vantage/audit_{name}_60_20260906.json').read_text())
        scalpers[name] = {k: raw.get(k) for k in ('symbol','range_utc','final_balance','profit','legs_closed','win_rate_closed_legs_pct','profit_factor','max_drawdown_from_peak','open_legs_at_end')}
        parts = {'first_40_sessions': [], 'last_20_sessions': []}
        for trade in raw['trades']:
            part = 'last_20_sessions' if trade['opened'][:10] >= '2026-08-10' else 'first_40_sessions'
            parts[part].append(float(trade['profit']) * .01 / float(trade['leg_lot']))
        scalpers[name]['fixed_001_chronological_check'] = {
            key: {'positions':len(values),'net':round(sum(values),2)} for key,values in parts.items()
        }
        if name.startswith('live_'):
            rescaled = json.loads((ROOT/f'data_vantage/audit_{name}_rescaled_60_20260906.json').read_text())['results']
            scalpers[name].update(final_balance=rescaled['final_balance'], profit=rescaled['profit'],
                max_drawdown_from_peak=-rescaled['max_closed_drawdown_usd'],
                sizing='1.6 percent per leg, chronologically corrected',
                profit_factor=None)
    report = {
        'generated_utc': datetime.now(UTC).isoformat(),
        'range': {'start': start.isoformat(), 'end': end.isoformat()},
        'starting_balance': 1000, 'risk_pct_per_leg': 1.5,
        'active_modules': modules, 'scalper_fresh_tests': scalpers,
        'combined_closed_balance_diagnostic_only': combined,
        'without_ghp_closed_balance_diagnostic_only': without_ghp,
        'limitations': [
            'Not a verified achievable return: margin uses balance, not floating equity; no liquidation simulation.',
            'GHP includes final edited text and provider entry prices; costs incomplete and historical message versions unavailable.',
            'Phoenix is replayed on Vantage candles; this is not a separate PU Prime listener backtest.',
            'Scalper standalone tests do not enforce broker margin and use an assumed commission of USD 0.06 per 0.01 lot.',
            'M1 bars cannot resolve every entry/SL/BE/TP sequence; SL-first is used for an already active stop.',
            'Module PnLs from separate USD 1000 accounts cannot be added to get one shared account result.',
        ],
    }
    out = ROOT/'data_vantage/current_modules_audit_60sessions_20260906.json'
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'modules': modules, 'scalpers': scalpers, 'combined': {k:v for k,v in combined.items() if k not in ('by_module','daily_pnl')}, 'report': str(out)}, indent=2))


if __name__ == '__main__':
    main()
