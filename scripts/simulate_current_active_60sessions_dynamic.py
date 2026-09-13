from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume


def _load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _event(
    *,
    source: str,
    module: str,
    setup: str,
    opened: str,
    closed: str,
    side: str,
    entry: float,
    pnl_001: float,
) -> dict[str, Any]:
    return {
        "source": source,
        "module": module,
        "setup": setup,
        "opened": _dt(opened),
        "closed": _dt(closed),
        "side": str(side),
        "entry": float(entry),
        "pnl_001": float(pnl_001),
    }


def _events(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    phoenix = _load(args.phoenix)
    fixed = phoenix["variants"]["fixed_cycle_plus_market"]
    events: list[dict[str, Any]] = []
    for row in fixed.get("trades", []):
        events.append(
            _event(
                source="phoenix_full",
                module=f"Phoenix TP{int(row['target_index'])}",
                setup=f"phoenix:{int(row['message_id'])}",
                opened=row["entry_time"],
                closed=row["exit_time"],
                side=row["side"],
                entry=row["entry"],
                pnl_001=row["pnl_001"],
            )
        )

    direction = _load(args.direction)
    for row in direction.get("trades", []):
        events.append(
            _event(
                source="phoenix_direction",
                module="Phoenix pre-signal pullback",
                setup=f"direction:{int(row['message_id'])}",
                opened=row["entry_time"],
                closed=row["exit_time"],
                side=row["side"],
                entry=row["entry"],
                pnl_001=row["pnl_001"],
            )
        )

    scalper_inputs = (
        ("sc_adx_break_07", "SC-AdxBreak-07", args.adx),
        ("ind_bb_kelt_macd", "IND-BB-KELT-MACD", args.bbkelt),
        ("ind_bb_macd_rcl", "IND-BB-MACD-RCL", args.bbrcl),
    )
    for source, module, path in scalper_inputs:
        report = _load(path)
        for row in report.get("trades", []):
            original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
            pnl_001 = float(row.get("profit", 0.0) or 0.0) * 0.01 / original_lot
            events.append(
                _event(
                    source=source,
                    module=module,
                    setup=f"{module}:{row['opened']}",
                    opened=row["opened"],
                    closed=row["closed"],
                    side=row["side"],
                    entry=row["entry"],
                    pnl_001=pnl_001,
                )
            )

    metadata = {
        "range_utc": phoenix.get("range_utc"),
        "sessions_included": phoenix.get("sessions_included", []),
        "phoenix_signals": int(phoenix.get("signals", 0) or 0),
        "phoenix_executed_signals": int(
            fixed.get("summary_001_per_leg", {}).get("executed_signals", 0) or 0
        ),
        "phoenix_decisions": fixed.get("decisions", {}),
    }
    return sorted(events, key=lambda row: (row["opened"], row["source"], row["setup"])), metadata


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [row for row in rows if float(row["pnl"]) > 0.005]
    losses = [row for row in rows if float(row["pnl"]) < -0.005]
    gross_win = sum(float(row["pnl"]) for row in wins)
    gross_loss = abs(sum(float(row["pnl"]) for row in losses))
    return {
        "positions": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(rows) - len(wins) - len(losses),
        "win_rate_pct": round(100.0 * len(wins) / max(1, len(wins) + len(losses)), 2),
        "pnl": round(sum(float(row["pnl"]) for row in rows), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "average_win": round(gross_win / max(1, len(wins)), 2),
        "average_loss": round(-gross_loss / max(1, len(losses)), 2),
    }


def _simulate(
    *,
    events: list[dict[str, Any]],
    symbol: str,
    cfg: Any,
    start_balance: float,
    base_leg_lot: float,
    step_usd: float,
    step_add: float,
    max_leg_lot: float,
    dynamic: bool,
) -> dict[str, Any]:
    balance = float(start_balance)
    peak = balance
    minimum_balance = balance
    max_dd = 0.0
    max_dd_pct = 0.0
    used_margin = 0.0
    max_used_margin = 0.0
    max_concurrent = 0
    max_concurrent_lot = 0.0
    margin_skips = 0
    sequence = 0
    closed: list[dict[str, Any]] = []
    open_heap: list[tuple[datetime, int, dict[str, Any]]] = []

    def close_until(moment: datetime) -> None:
        nonlocal balance, peak, minimum_balance, max_dd, max_dd_pct, used_margin
        while open_heap and open_heap[0][0] <= moment:
            _, _, trade = heapq.heappop(open_heap)
            used_margin = max(0.0, used_margin - float(trade["margin"]))
            balance += float(trade["pnl"])
            peak = max(peak, balance)
            minimum_balance = min(minimum_balance, balance)
            drawdown = balance - peak
            max_dd = min(max_dd, drawdown)
            if peak > 0.0:
                max_dd_pct = max(max_dd_pct, 100.0 * abs(min(0.0, drawdown)) / peak)
            closed.append({**trade, "balance_after": round(balance, 2)})

    for row in events:
        close_until(row["opened"])
        steps = max(0, math.floor((balance - start_balance + 1e-9) / step_usd)) if dynamic else 0
        requested_lot = min(max_leg_lot, base_leg_lot + steps * step_add)
        lot = normalize_volume(symbol, requested_lot, float(cfg.min_lot), max_leg_lot)
        order_type = mt5.ORDER_TYPE_BUY if row["side"] == "buy" else mt5.ORDER_TYPE_SELL
        margin = max(
            0.0,
            float(mt5.order_calc_margin(order_type, symbol, lot, float(row["entry"])) or 0.0),
        )
        if balance <= 0.0 or margin > balance - used_margin + 0.01:
            margin_skips += 1
            continue
        trade = {
            **row,
            "opened": row["opened"].isoformat(),
            "closed": row["closed"].isoformat(),
            "lot": round(lot, 2),
            "margin": round(margin, 2),
            "pnl": round(float(row["pnl_001"]) * lot / 0.01, 4),
        }
        sequence += 1
        heapq.heappush(open_heap, (row["closed"], sequence, trade))
        used_margin += margin
        max_used_margin = max(max_used_margin, used_margin)
        max_concurrent = max(max_concurrent, len(open_heap))
        max_concurrent_lot = max(
            max_concurrent_lot,
            sum(float(item[2]["lot"]) for item in open_heap),
        )
    close_until(datetime.max.replace(tzinfo=UTC))

    by_source = {
        source: _summary([row for row in closed if row["source"] == source])
        for source in sorted({str(row["source"]) for row in closed})
    }
    by_module = {
        module: _summary([row for row in closed if row["module"] == module])
        for module in sorted({str(row["module"]) for row in closed})
    }
    daily: dict[str, float] = defaultdict(float)
    for row in closed:
        daily[_dt(row["closed"]).date().isoformat()] += float(row["pnl"])
    daily = dict(sorted(daily.items()))
    positive_days = sum(value > 0.005 for value in daily.values())
    negative_days = sum(value < -0.005 for value in daily.values())
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round(100.0 * (balance / start_balance - 1.0), 2),
        "minimum_closed_balance": round(minimum_balance, 2),
        "max_closed_drawdown_usd": round(max_dd, 2),
        "max_closed_drawdown_pct_of_peak": round(max_dd_pct, 2),
        "positions": _summary(closed),
        "by_source": by_source,
        "by_module": by_module,
        "days_with_trades": len(daily),
        "positive_days": positive_days,
        "negative_days": negative_days,
        "daily_win_rate_pct": round(
            100.0 * positive_days / max(1, positive_days + negative_days), 2
        ),
        "average_daily_pnl": round(sum(daily.values()) / max(1, len(daily)), 2),
        "execution": {
            "candidate_positions": len(events),
            "accepted_positions": len(closed),
            "margin_skips": margin_skips,
            "max_concurrent_positions": max_concurrent,
            "max_concurrent_lot": round(max_concurrent_lot, 2),
            "max_used_margin": round(max_used_margin, 2),
        },
        "daily_pnl": {key: round(value, 2) for key, value in daily.items()},
        "closed_trades": closed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--phoenix", required=True)
    parser.add_argument("--direction", required=True)
    parser.add_argument("--adx", required=True)
    parser.add_argument("--bbkelt", required=True)
    parser.add_argument("--bbrcl", required=True)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--base-leg-lot", type=float, default=0.01)
    parser.add_argument("--step-usd", type=float, default=200.0)
    parser.add_argument("--step-add", type=float, default=0.01)
    parser.add_argument("--max-leg-lot", type=float, default=10.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        events, metadata = _events(args)
        dynamic = _simulate(
            events=events,
            symbol=symbol,
            cfg=cfg,
            start_balance=float(args.start_balance),
            base_leg_lot=float(args.base_leg_lot),
            step_usd=float(args.step_usd),
            step_add=float(args.step_add),
            max_leg_lot=float(args.max_leg_lot),
            dynamic=True,
        )
        fixed = _simulate(
            events=events,
            symbol=symbol,
            cfg=cfg,
            start_balance=float(args.start_balance),
            base_leg_lot=float(args.base_leg_lot),
            step_usd=float(args.step_usd),
            step_add=float(args.step_add),
            max_leg_lot=float(args.max_leg_lot),
            dynamic=False,
        )
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            **metadata,
            "symbol": symbol,
            "method": (
                "Exact active Phoenix cycle-plus-market replay, current pre-signal pullback runner and the three "
                "active scalper profiles merged chronologically on one balance. Historical M1 spread and commission "
                "are inherited from source replays; broker margin and concurrent positions are enforced."
            ),
            "dynamic_sizing": {
                "base_leg_lot_at_1000": float(args.base_leg_lot),
                "step_usd_above_start": float(args.step_usd),
                "step_add_per_leg": float(args.step_add),
                "max_leg_lot": float(args.max_leg_lot),
                "basis": "closed balance profit above the 1000 USD start",
            },
            "dynamic": dynamic,
            "fixed_001_control": fixed,
            "inputs": {
                "phoenix": args.phoenix,
                "direction": args.direction,
                "adx": args.adx,
                "bbkelt": args.bbkelt,
                "bbrcl": args.bbrcl,
            },
            "limitations": [
                "Drawdown is calculated from closed balance; tick-level floating equity drawdown is unavailable.",
                "The current strategies were selected or tuned using portions of this same history, so this is in-sample and not a forecast.",
                "M1 bars use conservative SL-first ordering when TP and SL are both touched in one candle.",
                "Real fills can differ because of slippage, latency, edits and Telegram delivery timing.",
            ],
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(out),
                    "range_utc": output["range_utc"],
                    "sessions": len(output["sessions_included"]),
                    "dynamic": {key: dynamic[key] for key in (
                        "final_balance",
                        "profit",
                        "return_pct",
                        "max_closed_drawdown_usd",
                        "max_closed_drawdown_pct_of_peak",
                        "positions",
                        "by_source",
                        "daily_win_rate_pct",
                    )},
                    "fixed_001_control": {key: fixed[key] for key in (
                        "final_balance",
                        "profit",
                        "return_pct",
                        "max_closed_drawdown_usd",
                        "positions",
                    )},
                    "execution": dynamic["execution"],
                },
                indent=2,
            )
        )
    finally:
        shutdown()


if __name__ == "__main__":
    main()
