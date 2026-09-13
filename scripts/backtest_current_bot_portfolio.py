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
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.risk import normalize_volume


def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _dt(value: str) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.astimezone(UTC) if result.tzinfo else result.replace(tzinfo=UTC)


def _normal(symbol: str, cfg: Any, lot: float) -> float:
    return normalize_volume(symbol, lot, float(cfg.min_lot), float(cfg.max_lot))


def _loss_per_lot(symbol: str, side: str, entry: float, sl: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return abs(float(mt5.order_calc_profit(order_type, symbol, 1.0, entry, sl) or 0.0))


def _margin(symbol: str, side: str, lot: float, entry: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    return max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, entry) or 0.0))


def _events(phoenix: dict[str, Any], direction: dict[str, Any], scalper: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in phoenix.get("trades", []):
        output.append(
            {
                "source": "phoenix_full",
                "module": f"Phoenix TP{int(row['target_index'])}",
                "setup": f"phoenix:{int(row['message_id'])}",
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": float(row["sl"]),
                "pnl_base": float(row["pnl_001"]),
                "base_lot": 0.01,
                "sizing": "risk",
            }
        )
    for row in direction.get("trades", []):
        output.append(
            {
                "source": "phoenix_direction",
                "module": "Phoenix direction runner",
                "setup": f"direction:{int(row['message_id'])}",
                "opened": _dt(row["entry_time"]),
                "closed": _dt(row["exit_time"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": float(row["sl"]),
                "pnl_base": float(row["pnl_001"]),
                "base_lot": 0.01,
                "sizing": "fixed",
            }
        )
    for index, row in enumerate(scalper.get("trades", [])):
        original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        setup_tag = str(row.get("setup_tag", "scalper") or "scalper")
        opened = _dt(row["opened"])
        output.append(
            {
                "source": "scalper",
                "module": setup_tag,
                "setup": f"scalper:{opened.isoformat()}:{setup_tag}",
                "opened": opened,
                "closed": _dt(row["closed"]),
                "side": str(row["side"]),
                "entry": float(row["entry"]),
                "sl": float(row["entry"]) - 3.75 if str(row["side"]) == "buy" else float(row["entry"]) + 3.75,
                "pnl_base": float(row["profit"]),
                "base_lot": original_lot,
                "sizing": "fixed",
                "sequence": index,
            }
        )
    return sorted(output, key=lambda row: (row["opened"], row["setup"], row.get("sequence", 0)))


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
        "average_win": round(gross_win / max(1, len(wins)), 3),
        "average_loss": round(-gross_loss / max(1, len(losses)), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--phoenix", required=True)
    parser.add_argument("--direction", required=True)
    parser.add_argument("--scalper", required=True)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--phoenix-risk-pct", type=float, default=1.0)
    parser.add_argument("--fixed-lot", type=float, default=0.01)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        events = _events(_load(args.phoenix), _load(args.direction), _load(args.scalper))
        setup_counts: dict[str, int] = defaultdict(int)
        for event in events:
            setup_counts[event["setup"]] += 1

        balance = float(args.start_balance)
        peak = balance
        max_dd = 0.0
        max_dd_pct = 0.0
        used_margin = 0.0
        max_margin = 0.0
        max_concurrent = 0
        max_concurrent_lot = 0.0
        margin_skips = 0
        first_margin_skip = None
        open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
        closed: list[dict[str, Any]] = []
        sequence = 0
        risk_lots: dict[str, float] = {}
        initial_risks: dict[str, float] = defaultdict(float)

        def close_until(moment: datetime) -> None:
            nonlocal balance, peak, max_dd, max_dd_pct, used_margin
            while open_heap and open_heap[0][0] <= moment:
                _time, _seq, trade = heapq.heappop(open_heap)
                used_margin = max(0.0, used_margin - float(trade["margin"]))
                balance += float(trade["pnl"])
                peak = max(peak, balance)
                max_dd = min(max_dd, balance - peak)
                if peak > 0.0:
                    max_dd_pct = max(max_dd_pct, 100.0 * (peak - balance) / peak)
                closed.append({**trade, "balance": round(balance, 2)})

        for event in events:
            close_until(event["opened"])
            if balance <= 0.0:
                break
            if event["sizing"] == "risk":
                if event["setup"] not in risk_lots:
                    count = max(1, setup_counts[event["setup"]])
                    loss_per_lot = _loss_per_lot(
                        symbol, event["side"], event["entry"], event["sl"]
                    )
                    risk_per_leg = balance * max(0.0, args.phoenix_risk_pct) / 100.0 / count
                    requested = risk_per_leg / loss_per_lot if loss_per_lot > 0.0 else args.fixed_lot
                    risk_lots[event["setup"]] = _normal(
                        symbol, cfg, max(args.fixed_lot, requested)
                    )
                lot = risk_lots[event["setup"]]
            else:
                lot = _normal(symbol, cfg, args.fixed_lot)

            loss_per_lot = _loss_per_lot(symbol, event["side"], event["entry"], event["sl"])
            initial_risks[event["setup"]] += loss_per_lot * lot
            margin = _margin(symbol, event["side"], lot, event["entry"])
            free_margin = balance - used_margin
            if margin > free_margin + 0.01:
                margin_skips += 1
                if first_margin_skip is None:
                    first_margin_skip = {
                        "time": event["opened"].isoformat(),
                        "source": event["source"],
                        "balance": round(balance, 2),
                        "free_margin": round(free_margin, 2),
                        "required_margin": round(margin, 2),
                        "lot": round(lot, 2),
                    }
                continue
            pnl = float(event["pnl_base"]) * lot / float(event["base_lot"])
            trade = {
                **event,
                "opened": event["opened"].isoformat(),
                "closed": event["closed"].isoformat(),
                "lot": round(lot, 2),
                "margin": round(margin, 2),
                "pnl": round(pnl, 4),
            }
            sequence += 1
            heapq.heappush(open_heap, (event["closed"], sequence, trade))
            used_margin += margin
            max_margin = max(max_margin, used_margin)
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
        by_month = {
            month: round(sum(float(row["pnl"]) for row in closed if row["closed"][:7] == month), 2)
            for month in sorted({str(row["closed"])[:7] for row in closed})
        }
        setup_pnl: dict[str, float] = defaultdict(float)
        for row in closed:
            setup_pnl[str(row["setup"])] += float(row["pnl"])
        signal_wins = sum(value > 0.005 for value in setup_pnl.values())
        signal_losses = sum(value < -0.005 for value in setup_pnl.values())
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {
                "start": min(event["opened"] for event in events).isoformat(),
                "end": max(event["closed"] for event in events).isoformat(),
            },
            "method": "Chronological current-bot portfolio replay on one balance; Phoenix 1% package risk with broker minimum lot; direction runner and scalper fixed 0.01; historical spread and commission inherited from source tests; broker margin enforced",
            "start_balance": round(float(args.start_balance), 2),
            "final_balance": round(balance, 2),
            "profit": round(balance - float(args.start_balance), 2),
            "return_pct": round(100.0 * (balance / float(args.start_balance) - 1.0), 2),
            "max_closed_drawdown_usd": round(max_dd, 2),
            "max_closed_drawdown_pct_of_peak": round(max_dd_pct, 2),
            "positions": _summary(closed),
            "setups": {
                "count": len(setup_pnl),
                "wins": signal_wins,
                "losses": signal_losses,
                "flat": len(setup_pnl) - signal_wins - signal_losses,
                "win_rate_pct": round(100.0 * signal_wins / max(1, signal_wins + signal_losses), 2),
            },
            "by_source": by_source,
            "by_module": by_module,
            "by_month": by_month,
            "execution": {
                "candidate_positions": len(events),
                "accepted_positions": len(closed),
                "margin_skips": margin_skips,
                "first_margin_skip": first_margin_skip,
                "max_concurrent_positions": max_concurrent,
                "max_concurrent_lot": round(max_concurrent_lot, 2),
                "max_used_margin": round(max_margin, 2),
            },
            "sizing": {
                "phoenix_total_risk_pct": float(args.phoenix_risk_pct),
                "minimum_and_fixed_leg_lot": float(args.fixed_lot),
                "max_initial_setup_risk_usd": round(max(initial_risks.values(), default=0.0), 2),
                "max_initial_setup_risk_pct_of_start": round(100.0 * max(initial_risks.values(), default=0.0) / float(args.start_balance), 2),
            },
            "inputs": {
                "phoenix": args.phoenix,
                "direction": args.direction,
                "scalper": args.scalper,
            },
            "limitations": [
                "Closed-balance drawdown is reconstructed; tick-level floating drawdown and slippage are unavailable.",
                "Phoenix parameters were tuned on part of this history, so this is not a fully independent out-of-sample result.",
                "The first cached session has no earlier warm-up bars; indicator modules may skip its initial bars.",
            ],
            "closed_trades": closed,
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({key: output[key] for key in ("range_utc", "start_balance", "final_balance", "profit", "return_pct", "max_closed_drawdown_usd", "positions", "setups", "by_source", "execution", "sizing")}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
