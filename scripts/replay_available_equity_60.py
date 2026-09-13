"""Read-only, M1 marked-equity sizing diagnostic on saved strategy events."""
from __future__ import annotations

import json
import argparse
import math
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import MetaTrader5 as mt5
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.merge_selected_channels_portfolio import _base_events, _candidate_events, _is_mirror


def sized_lot(equity, loss_per_lot, minimum, step, maximum, risk_pct=1.75):
    if equity <= 0 or loss_per_lot <= 0 or step <= 0:
        return 0.0
    raw = min(maximum, equity * risk_pct / 100 / loss_per_lot)
    lot = math.floor((raw + 1e-12) / step) * step
    return round(lot, 8) if lot >= minimum - 1e-10 else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rebuilt-events')
    parser.add_argument('--output', default='reports/available_equity_60sessions_r175_start1000_20260913.json')
    args = parser.parse_args()
    env = dotenv_values(ROOT / '.env.vantage')
    assert mt5.initialize(path=env.get('MT5_PATH', env.get('MT5_TERMINAL_PATH', ''))), mt5.last_error()
    try:
        account = mt5.account_info()
        assert account is not None and account.trade_mode == 0, 'Read-only test requires demo terminal'
        end = datetime(2026, 9, 13, tzinfo=UTC)
        daily = mt5.copy_rates_range('XAUUSD+', mt5.TIMEFRAME_D1, end-timedelta(days=130), end)
        assert daily is not None and len(daily) >= 60, 'Missing 60 completed daily bars'
        sessions = sorted(set(int(b['time']) // 86400 * 86400 for b in daily if int(b['time']) < end.timestamp()))[-60:]
        start = datetime.fromtimestamp(sessions[0], UTC)
        payload = json.loads((ROOT/'data_vantage/base_all_modules_start700_risk175_70sessions_20260912.json').read_text())
        events = _base_events(payload)
        rebuilt_coverage = None
        if args.rebuilt_events:
            rebuilt = json.loads((ROOT/args.rebuilt_events).read_text(encoding='utf-8'))
            rebuilt_coverage = rebuilt['coverage']
            events = [e for e in events if not e['module'].startswith('ghp:') and e['module'] != 'dany_signals']
            for event in rebuilt['events']:
                for key in ('opened','closed'):
                    event[key] = datetime.fromisoformat(event[key])
                events.append(event)
        candidates = _candidate_events(json.loads((ROOT/'data_vantage/selected8_causal_70sessions_20260913.json').read_text()))
        exclusions = Counter()
        watched = set(env['TRADE_CHANNELS'].split(','))
        for event in candidates:
            if event['module'].split(':', 1)[1] not in watched:
                continue
            if not event['symbol'].upper().startswith('XAUUSD'):
                exclusions['new_non_gold_unverified_contract'] += 1
                continue
            event['loss_per_lot'] = abs(event['entry']-event['sl'])*100
            prior = sorted([e for e in events if not e['module'].startswith('scalper')], key=lambda e:e['opened'])
            if _is_mirror(event, prior):
                exclusions['cross_channel_mirror'] += 1
            else:
                events.append(event)
        selected = []
        for e in events:
            if not start <= e['opened'] < end:
                continue
            if not all(math.isfinite(e[k]) and e[k] > 0 for k in ('entry','sl','loss_per_lot')):
                exclusions['invalid_price_or_risk'] += 1
                continue
            if e['closed'] < e['opened'] or e['closed'] >= end:
                exclusions['invalid_or_out_of_window_close'] += 1
                continue
            selected.append(e)
        events = selected
        rates, specs, coverage = {}, {}, {}
        for symbol in sorted({e['symbol'] for e in events}):
            info = mt5.symbol_info(symbol)
            bars = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start, end)
            if info is None or bars is None or len(bars) == 0:
                exclusions['unsupported_symbol_events'] += sum(e['symbol']==symbol for e in events)
                continue
            reference = float(bars[-1]['open'])
            delta = max(info.point*100,reference*.001)
            converted = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY,symbol,1.0,reference,reference+delta)
            if converted is None or converted <= 0:
                exclusions['missing_profit_conversion'] += sum(e['symbol']==symbol for e in events)
                continue
            rates[symbol] = {int(b['time']): (float(b['open']), int(b['spread'])*info.point) for b in bars}
            specs[symbol] = dict(minimum=info.volume_min, step=info.volume_step, maximum=info.volume_max, contract=converted/delta)
            coverage[symbol] = dict(bars=len(bars), first=int(bars[0]['time']), last=int(bars[-1]['time']))
        balance = peak = 1000.0
        max_dd = 0.0
        active = {}
        trades = []
        timeline = defaultdict(lambda: {'open':[], 'close':[]})
        for idx, e in enumerate(events):
            if e['symbol'] not in specs:
                continue
            timeline[int(e['opened'].timestamp())]['open'].append((idx,e))
            timeline[int(e['closed'].timestamp())]['close'].append(idx)
        if timeline:
            for moment in range(min(timeline)//60*60, max(timeline)+1, 60):
                timeline[moment]
        missing_marks = total_marks = max_concurrent = 0
        incomplete_entry_marks = 0

        def marked(moment):
            nonlocal missing_marks, total_marks
            floating = 0.0
            complete = True
            for e, lot in active.values():
                total_marks += 1
                quote = rates[e['symbol']].get(moment//60*60)
                if quote is None:
                    missing_marks += 1
                    complete = False
                    continue
                bid, spread = quote
                move = bid-e['entry'] if e['side'].lower() == 'buy' else e['entry']-(bid+spread)
                floating += move * specs[e['symbol']]['contract'] * lot
            return balance+floating, complete

        def close(idx, moment):
            nonlocal balance
            if idx not in active:
                return
            e, lot = active.pop(idx)
            pnl = e['pnl_per_lot']*lot
            balance += pnl
            trades.append(dict(module=e['module'], opened=e['opened'].isoformat(), closed=e['closed'].isoformat(),
                               lot=lot, pnl=pnl, balance_after=balance))

        for moment, batch in sorted(timeline.items()):
            for idx in batch['close']:
                close(idx, moment)
            equity, complete = marked(moment)
            if complete:
                peak = max(peak,equity)
                max_dd = max(max_dd, (peak-equity)/peak*100)
            for idx,e in batch['open']:
                equity, complete = marked(moment)
                if not complete:
                    incomplete_entry_marks += 1
                    exclusions['entry_missing_active_quote'] += 1
                    continue
                if moment//60*60 not in rates[e['symbol']]:
                    exclusions['entry_missing_quote'] += 1
                    continue
                spec = specs[e['symbol']]
                lot = sized_lot(equity,e['loss_per_lot'],spec['minimum'],spec['step'],spec['maximum'])
                if lot == 0:
                    exclusions['below_min_lot_or_nonpositive_equity'] += 1
                    continue
                active[idx] = (e,lot)
                max_concurrent = max(max_concurrent,len(active))
                # A zero-duration candle event has no known intraminute ordering.
            for idx in batch['close']:
                close(idx,moment)
        groups = defaultdict(list)
        for t in trades:
            groups[t['module']].append(t['pnl'])
        def metrics(pnls):
            win = sum(p>0 for p in pnls)
            losses = -sum(min(0,p) for p in pnls)
            return dict(legs=len(pnls), pnl=round(sum(pnls),2), win_rate=round(win/max(1,len(pnls))*100,2),
                        profit_factor=round(sum(max(0,p) for p in pnls)/losses,3) if losses else None)
        result = dict(start=start.isoformat(), end_exclusive=end.isoformat(), sessions=60, session_dates=[datetime.fromtimestamp(s,UTC).date().isoformat() for s in sessions],
                      start_balance=1000, risk_pct_per_leg=1.75, sizing_basis='M1_open_marked_equity',
                      final_balance=round(balance,2), total=metrics([t['pnl'] for t in trades]),
                      modules={k:metrics(v) for k,v in groups.items()}, max_observed_equity_dd_pct=round(max_dd,2),
                      max_concurrent=max_concurrent, exclusions=dict(exclusions), quote_coverage=coverage,
                      active_quote_checks=total_marks, missing_active_quotes=missing_marks, incomplete_entry_marks=incomplete_entry_marks,
                      source_events=len(events), rebuilt_source_coverage=rebuilt_coverage, forecast_eligible=False,
                      limitations=['Partial saved-event replay, NOT a fresh full strategy backtest.',
                                   'Dany/GHP regenerated when rebuilt_source_coverage is set; other base modules use previously accepted trades.',
                                   'Entry/SL/exit decisions reused from source simulations, not regenerated from live engine.',
                                   'M1 open quotes approximate intraminute equity; historical commission accrual not simulated; FX conversion uses current terminal conversion.',
                                   'No historical margin or broker stopout execution; observed DD excludes intervals without complete marks.',
                                   'Zero-duration events lack tick sequencing; known final source outcome booked at same timestamp.',
                                   'Current demo contract metadata, not historical PU Prime terms.'], trades=trades)
        path = ROOT/args.output
        path.write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k not in ('trades','session_dates')},indent=2))
    finally:
        mt5.shutdown()


if __name__ == '__main__':
    main()
