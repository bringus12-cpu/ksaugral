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


def _channel_variants(value: Any) -> set[str]:
    raw = str(value or "").strip().lower().lstrip("@")
    output = {raw} if raw else set()
    try:
        numeric = str(abs(int(raw)))
        output.update({numeric, str(int(raw))})
        if numeric.startswith("100"):
            output.add(numeric[3:])
        else:
            output.update({f"100{numeric}", f"-100{numeric}"})
    except Exception:
        pass
    return {item for item in output if item}


def _channel_override(cfg: Any, chat_id: Any, title: str) -> float | None:
    candidates = _channel_variants(chat_id) | _channel_variants(title)
    for key, value in cfg.channel_lot_sizes.items():
        if _channel_variants(key) & candidates:
            return float(value)
    return None


def _profit_dynamic_lot(cfg: Any, balance: float, start_balance: float, symbol: str) -> float:
    net_profit = max(0.0, balance - start_balance)
    steps = math.floor(net_profit / float(cfg.signal_dynamic_lot_step_usd))
    raw = float(cfg.signal_fixed_lot) + (steps * float(cfg.signal_dynamic_lot_add))
    raw = min(float(cfg.signal_dynamic_lot_max), raw)
    return normalize_volume(symbol, raw, float(cfg.min_lot), max(float(cfg.max_lot), float(cfg.signal_dynamic_lot_max)))


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    value = mt5.order_calc_margin(order_type, symbol, lot, entry)
    return max(0.0, float(value or 0.0))


def _spread_cost(symbol: str, lot: float, entry: float, spread_price: float) -> float:
    if spread_price <= 0:
        return 0.0
    value = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, lot, entry, entry + spread_price)
    return abs(float(value or 0.0))


def _listener_events(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for report in reports:
        symbol = str(report["symbol"])
        spread_price = float(report.get("spread_price", 0.0) or 0.0)
        for row in report.get("equity", []):
            output.append(
                {
                    "source": "telegram",
                    "channel": str(row.get("channel") or "unknown"),
                    "chat_id": int(row.get("chat_id", 0) or 0),
                    "setup_id": f"tg:{row.get('chat_id')}:{row.get('message_id')}",
                    "opened": _dt(row["entry_time"]),
                    "closed": _dt(row["exit_time"]),
                    "symbol": symbol,
                    "side": str(row["side"]),
                    "entry": float(row["entry"]),
                    "profit_001": float(row["profit_001"]),
                    "spread_price": spread_price,
                    "plan_index": int(row.get("plan_index", 0) or 0),
                    "status": str(row.get("status") or ""),
                }
            )
    return output


def _scalper_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    symbol = str(report["symbol"])
    output: list[dict[str, Any]] = []
    for row in report.get("trades", []):
        leg_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        spread_per_001 = float(row.get("spread_cost", 0.0) or 0.0) / max(1.0, leg_lot / 0.01)
        opened = _dt(row["opened"])
        output.append(
            {
                "source": "scalper",
                "channel": "XAU SCALPER",
                "chat_id": 0,
                "setup_id": f"scalp:{opened.isoformat()}",
                "opened": opened,
                "closed": _dt(row["closed"]),
                "symbol": symbol,
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "profit_001": float(row["profit_001"]),
                "spread_per_001": spread_per_001,
                "plan_index": 0,
                "status": str(row.get("status") or ""),
            }
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--listener", action="append", default=[])
    parser.add_argument("--scalper", required=True)
    parser.add_argument("--start-balance", type=float, default=2000.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    for env_file in args.env:
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        listener_reports = [_load(path) for path in args.listener]
        scalper_report = _load(args.scalper)
        events = _listener_events(listener_reports) + _scalper_events(scalper_report)
        events.sort(key=lambda row: (row["opened"], row["closed"], row["source"]))
        symbols = {candidate: ensure_symbol(candidate) for candidate in {row["symbol"] for row in events}}

        balance = float(args.start_balance)
        peak = balance
        max_dd = 0.0
        used_margin = 0.0
        max_used_margin = 0.0
        max_concurrent = 0
        max_concurrent_lot = 0.0
        max_listener_lot = float(cfg.signal_fixed_lot)
        open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
        sequence = 0
        accepted = skipped_margin = 0
        legs_won = legs_lost = legs_flat = 0
        by_source: dict[str, float] = defaultdict(float)
        by_channel: dict[str, float] = defaultdict(float)
        daily_pnl: dict[str, float] = defaultdict(float)
        lot_milestones: list[dict[str, Any]] = []
        first_margin_skip: dict[str, Any] | None = None
        first_nonpositive_balance: dict[str, Any] | None = None

        def close_until(moment: datetime) -> None:
            nonlocal balance, peak, max_dd, used_margin, legs_won, legs_lost, legs_flat, first_nonpositive_balance
            while open_heap and open_heap[0][0] <= moment:
                _closed, _seq, trade = heapq.heappop(open_heap)
                used_margin = max(0.0, used_margin - float(trade["margin"]))
                pnl = float(trade["net_profit"])
                balance += pnl
                peak = max(peak, balance)
                max_dd = min(max_dd, balance - peak)
                by_source[str(trade["source"])] += pnl
                by_channel[str(trade["channel"])] += pnl
                daily_pnl[trade["closed"].date().isoformat()] += pnl
                if pnl > 0.005:
                    legs_won += 1
                elif pnl < -0.005:
                    legs_lost += 1
                else:
                    legs_flat += 1
                if balance <= 0 and first_nonpositive_balance is None:
                    first_nonpositive_balance = {"time": trade["closed"].isoformat(), "balance": round(balance, 2)}

        for event in events:
            close_until(event["opened"])
            if balance <= 0:
                skipped_margin += 1
                continue
            symbol = symbols[event["symbol"]]
            if event["source"] == "scalper":
                total_lot = min(float(cfg.xau_scalp_lot), float(cfg.xau_scalp_dynamic_max_lot))
                leg_count = max(1, int(getattr(cfg, "xau_scalp_leg_count", 3) or 3))
                lot = normalize_volume(symbol, total_lot / leg_count, float(cfg.min_lot), float(cfg.max_lot))
                spread_cost = float(event.get("spread_per_001", 0.0)) * (lot / 0.01)
            else:
                override = _channel_override(cfg, event["chat_id"], event["channel"])
                if int(event["plan_index"]) == 999:
                    lot = normalize_volume(symbol, float(__import__("os").getenv("SIGNAL_EXTRA_MARKET_TP1_LOT", "0.10")), float(cfg.min_lot), float(cfg.max_lot))
                elif override is not None:
                    lot = normalize_volume(symbol, override, float(cfg.min_lot), float(cfg.max_lot))
                else:
                    lot = _profit_dynamic_lot(cfg, balance, float(args.start_balance), symbol)
                    if lot > max_listener_lot:
                        max_listener_lot = lot
                        lot_milestones.append({"time": event["opened"].isoformat(), "balance": round(balance, 2), "leg_lot": round(lot, 2)})
                spread_cost = _spread_cost(symbol, lot, event["entry"], event.get("spread_price", 0.0))
            margin = _margin(symbol, event["side"], lot, event["entry"])
            free_margin = max(0.0, balance - used_margin)
            if margin > free_margin + 0.01:
                skipped_margin += 1
                if first_margin_skip is None:
                    first_margin_skip = {
                        "time": event["opened"].isoformat(),
                        "source": event["source"],
                        "channel": event["channel"],
                        "required_margin": round(margin, 2),
                        "free_margin": round(free_margin, 2),
                        "lot": round(lot, 2),
                    }
                continue
            gross = float(event["profit_001"]) * (lot / 0.01)
            trade = {**event, "lot": lot, "margin": margin, "net_profit": gross - spread_cost}
            sequence += 1
            heapq.heappush(open_heap, (trade["closed"], sequence, trade))
            used_margin += margin
            accepted += 1
            max_used_margin = max(max_used_margin, used_margin)
            max_concurrent = max(max_concurrent, len(open_heap))
            max_concurrent_lot = max(max_concurrent_lot, sum(float(row[2]["lot"]) for row in open_heap))

        close_until(datetime.max.replace(tzinfo=UTC))
        active_days = len(daily_pnl)
        result = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "start_balance": round(float(args.start_balance), 2),
            "final_balance": round(balance, 2),
            "profit": round(balance - float(args.start_balance), 2),
            "return_pct": round((balance / float(args.start_balance) - 1.0) * 100.0, 2),
            "max_closed_drawdown_usd": round(max_dd, 2),
            "max_closed_drawdown_pct_of_start": round(abs(max_dd) / float(args.start_balance) * 100.0, 2),
            "candidate_legs": len(events),
            "accepted_legs": accepted,
            "skipped_for_margin": skipped_margin,
            "first_margin_skip": first_margin_skip,
            "first_nonpositive_balance": first_nonpositive_balance,
            "legs": {"wins": legs_won, "losses": legs_lost, "flat": legs_flat, "win_rate_decided_pct": round(100.0 * legs_won / max(1, legs_won + legs_lost), 2)},
            "concurrency": {"max_positions": max_concurrent, "max_total_lot": round(max_concurrent_lot, 2), "max_used_margin": round(max_used_margin, 2)},
            "listener_lot_milestones": lot_milestones,
            "max_listener_leg_lot": round(max_listener_lot, 2),
            "active_days": active_days,
            "average_daily_pnl": round((balance - float(args.start_balance)) / max(1, active_days), 2),
            "best_day": max(daily_pnl.items(), key=lambda item: item[1]) if daily_pnl else None,
            "worst_day": min(daily_pnl.items(), key=lambda item: item[1]) if daily_pnl else None,
            "by_source": {key: round(value, 2) for key, value in sorted(by_source.items(), key=lambda item: item[1], reverse=True)},
            "by_channel": {key: round(value, 2) for key, value in sorted(by_channel.items(), key=lambda item: item[1], reverse=True)},
            "daily_pnl": {key: round(value, 2) for key, value in sorted(daily_pnl.items())},
            "notes": [
                "Positions are opened concurrently and retain their entry-time lot.",
                "Margin is checked against closed balance minus reserved margin; intratrade floating PnL is not reconstructed.",
                "Listener profit-dynamic lot starts at profile base and increases per closed-profit step.",
            ],
        }
        Path(args.output).write_text(json.dumps(result, indent=2, ensure_ascii=True), encoding="utf-8")
        print(args.output)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
