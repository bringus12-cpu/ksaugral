from __future__ import annotations

import argparse
import heapq
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, mt5, shutdown, symbol_info
from app.risk import normalize_volume


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _profit(symbol: str, side: str, lot: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return float(mt5.order_calc_profit(order_type, symbol, lot, entry, exit_price) or 0.0)


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, entry) or 0.0))


def _bucket(values: dict[str, float]) -> dict[str, Any]:
    wins = int(values["wins"])
    losses = int(values["losses"])
    gross_loss = abs(float(values["gross_loss"]))
    lots = values.get("lots", [])
    return {
        "positions": wins + losses + int(values["flat"]),
        "wins": wins,
        "losses": losses,
        "flat": int(values["flat"]),
        "win_rate_pct": round(100.0 * wins / max(1, wins + losses), 2),
        "pnl": round(float(values["pnl"]), 2),
        "profit_factor": round(float(values["gross_win"]) / gross_loss, 3) if gross_loss else None,
        "min_lot": round(min(lots), 2) if lots else 0.0,
        "max_lot": round(max(lots), 2) if lots else 0.0,
    }


def simulate(
    trades: list[dict[str, Any]],
    selected_pairs: set[str],
    start_balance: float,
    risk_pct: float,
    enforce_margin: bool,
) -> dict[str, Any]:
    events = [
        {
            **row,
            "opened": _dt(row["entry_time"]),
            "closed": _dt(row["exit_time"]),
            "pair": f"{row['symbol']}::{row['strategy']}",
        }
        for row in trades
        if f"{row['symbol']}::{row['strategy']}" in selected_pairs
    ]
    events.sort(key=lambda row: (row["opened"], row["closed"], row["pair"]))
    infos = {symbol: symbol_info(symbol) for symbol in {str(row["symbol"]) for row in events}}

    balance = float(start_balance)
    peak = balance
    max_drawdown = 0.0
    used_margin = 0.0
    max_used_margin = 0.0
    max_concurrent = 0
    accepted = 0
    margin_skips = 0
    sequence = 0
    open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
    by_pair: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    by_strategy: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    overall: dict[str, Any] = defaultdict(float)
    daily: dict[str, float] = defaultdict(float)

    def close_until(moment: datetime) -> None:
        nonlocal balance, peak, max_drawdown, used_margin
        while open_heap and open_heap[0][0] <= moment:
            _, _, trade = heapq.heappop(open_heap)
            used_margin = max(0.0, used_margin - float(trade["margin"]))
            pnl = float(trade["pnl"])
            balance += pnl
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, peak - balance)
            daily[trade["closed"].date().isoformat()] += pnl
            outcome = "wins" if pnl > 0.005 else "losses" if pnl < -0.005 else "flat"
            for bucket in (overall, by_pair[trade["pair"]], by_strategy[trade["strategy"]]):
                bucket["pnl"] += pnl
                bucket["gross_win"] += max(0.0, pnl)
                bucket["gross_loss"] += min(0.0, pnl)
                bucket[outcome] += 1
                bucket.setdefault("lots", []).append(float(trade["lot"]))

    for event in events:
        close_until(event["opened"])
        if balance <= 0.0:
            margin_skips += 1
            continue
        symbol = str(event["symbol"])
        side = str(event["side"])
        entry = float(event["entry"])
        sl = float(event["sl"])
        loss_per_lot = abs(_profit(symbol, side, 1.0, entry, sl))
        if loss_per_lot <= 0.0:
            margin_skips += 1
            continue
        requested = balance * risk_pct / 100.0 / loss_per_lot
        info = infos[symbol]
        minimum = float(getattr(info, "volume_min", 0.01) or 0.01)
        maximum = float(getattr(info, "volume_max", 100.0) or 100.0)
        lot = normalize_volume(symbol, requested, minimum, maximum)
        margin = _margin(symbol, side, lot, entry)
        if enforce_margin and margin > max(0.0, balance - used_margin) + 0.01:
            margin_skips += 1
            continue
        pnl = _profit(symbol, side, lot, entry, float(event["exit"]))
        sequence += 1
        heapq.heappush(
            open_heap,
            (event["closed"], sequence, {**event, "lot": lot, "margin": margin, "pnl": pnl}),
        )
        used_margin += margin
        accepted += 1
        max_used_margin = max(max_used_margin, used_margin)
        max_concurrent = max(max_concurrent, len(open_heap))
    close_until(datetime.max.replace(tzinfo=UTC))

    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "risk_pct_per_position_leg": risk_pct,
        "max_closed_drawdown_usd": round(max_drawdown, 2),
        "max_closed_drawdown_pct_of_peak": round(100.0 * max_drawdown / peak, 2) if peak else 0.0,
        "candidate_positions": len(events),
        "accepted_positions": accepted,
        "margin_or_balance_skips": margin_skips,
        "max_concurrent_positions": max_concurrent,
        "max_used_margin": round(max_used_margin, 2),
        "overall": _bucket(overall),
        "by_strategy": {key: _bucket(value) for key, value in sorted(by_strategy.items())},
        "by_pair": {key: _bucket(value) for key, value in sorted(by_pair.items())},
        "daily_pnl": {key: round(value, 2) for key, value in sorted(daily.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Chronological per-leg risk replay for Market Machine")
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--backtest", required=True)
    parser.add_argument("--analytics", required=True)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--risk-per-leg-pct", type=float, default=1.75)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    backtest = json.loads(Path(args.backtest).read_text(encoding="utf-8-sig"))
    analytics = json.loads(Path(args.analytics).read_text(encoding="utf-8-sig"))
    selected = set(analytics.get("stable_pairs", []))
    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        result = simulate(
            backtest.get("trades", []),
            selected,
            float(args.start_balance),
            float(args.risk_per_leg_pct),
            True,
        )
    finally:
        shutdown()

    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "method": "chronological closed-balance sizing; broker lot normalization and margin enforced",
        "sessions": backtest.get("sessions"),
        "selected_pairs": sorted(selected),
        "result": result,
        "limitations": [
            "Sizing uses closed balance at entry because intrabar floating equity is not reconstructed.",
            "Historical broker profit and margin calculations are used; slippage and stop-out are not replayed.",
            "The selected strategies were chosen on the same 60-session sample and require forward validation.",
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
