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
    *, source: str, module: str, setup: str, opened: str, closed: str,
    side: str, entry: float, pnl_001: float, sequence: int,
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
        "sequence": int(sequence),
    }


def _load_events(args: argparse.Namespace) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    phoenix = _load(args.phoenix)
    for index, row in enumerate(phoenix.get("trades", [])):
        events.append(_event(
            source="phoenix_full",
            module=f"Phoenix TP{int(row['target_index'])}",
            setup=f"phoenix:{int(row['message_id'])}",
            opened=row["entry_time"], closed=row["exit_time"],
            side=row["side"], entry=row["entry"], pnl_001=row["pnl_001"], sequence=index,
        ))

    direction = _load(args.direction)
    for index, row in enumerate(direction.get("trades", [])):
        events.append(_event(
            source="phoenix_direction",
            module="Phoenix direction runner",
            setup=f"direction:{int(row['message_id'])}",
            opened=row["entry_time"], closed=row["exit_time"],
            side=row["side"], entry=row["entry"], pnl_001=row["pnl_001"], sequence=index,
        ))

    scalper = _load(args.scalper)
    for index, row in enumerate(scalper.get("trades", [])):
        original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        pnl_001 = float(row["profit"]) * 0.01 / original_lot
        tag = str(row.get("setup_tag", "SC-current") or "SC-current")
        opened = str(row["opened"])
        events.append(_event(
            source="scalper_current", module=tag,
            setup=f"scalper:{opened}:{tag}", opened=opened, closed=row["closed"],
            side=row["side"], entry=row["entry"], pnl_001=pnl_001, sequence=index,
        ))

    adx = _load(args.adx)
    for index, row in enumerate(adx.get("trades", [])):
        original_lot = max(0.01, float(row.get("leg_lot", 0.01) or 0.01))
        pnl_001 = float(row["profit"]) * 0.01 / original_lot
        events.append(_event(
            source="sc_adx_break_07", module="SC-AdxBreak-07",
            setup=f"adx:{row['opened']}", opened=row["opened"], closed=row["closed"],
            side=row["side"], entry=row["entry"], pnl_001=pnl_001, sequence=index,
        ))

    indicator = _load(args.indicator)
    selected = {"IND-BB-KELT-MACD", "IND-BB-MACD-RCL"}
    for finalist in indicator.get("finalists", []):
        strategy = str(finalist.get("strategy", ""))
        if strategy not in selected:
            continue
        for index, row in enumerate(finalist.get("trades", [])):
            events.append(_event(
                source=strategy.lower().replace("-", "_"), module=strategy,
                setup=f"{strategy}:{row['entry_time']}",
                opened=row["entry_time"], closed=row["exit_time"],
                side=row["side"], entry=row["entry"], pnl_001=row["pnl"], sequence=index,
            ))
    return sorted(events, key=lambda row: (row["opened"], row["source"], row["sequence"]))


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
    parser.add_argument("--adx", required=True)
    parser.add_argument("--indicator", required=True)
    parser.add_argument("--start-balance", type=float, default=1000.0)
    parser.add_argument("--base-leg-lot", type=float, default=0.01)
    parser.add_argument("--step-usd", type=float, default=500.0)
    parser.add_argument("--step-add", type=float, default=0.01)
    parser.add_argument("--max-leg-lot", type=float, default=10.0)
    parser.add_argument("--exclude-current-scalper", action="store_true")
    parser.add_argument("--only-scalpers", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    load_dotenv(args.env, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        events = _load_events(args)
        if args.exclude_current_scalper:
            events = [row for row in events if row["source"] != "scalper_current"]
        if args.only_scalpers:
            events = [row for row in events if not row["source"].startswith("phoenix_")]
        balance = float(args.start_balance)
        peak = balance
        max_dd = 0.0
        max_dd_pct = 0.0
        used_margin = 0.0
        max_margin = 0.0
        max_concurrent = 0
        max_concurrent_lot = 0.0
        margin_skips = 0
        closed: list[dict[str, Any]] = []
        open_heap: list[tuple[datetime, int, dict[str, Any]]] = []
        sequence = 0

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

        for row in events:
            close_until(row["opened"])
            steps = max(0, math.floor((balance - float(args.start_balance) + 1e-9) / float(args.step_usd)))
            requested_lot = min(
                float(args.max_leg_lot),
                float(args.base_leg_lot) + steps * float(args.step_add),
            )
            lot = normalize_volume(symbol, requested_lot, float(cfg.min_lot), float(cfg.max_lot))
            order_type = mt5.ORDER_TYPE_BUY if row["side"] == "buy" else mt5.ORDER_TYPE_SELL
            margin = max(0.0, float(mt5.order_calc_margin(order_type, symbol, lot, row["entry"]) or 0.0))
            if margin > balance - used_margin + 0.01:
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
            max_margin = max(max_margin, used_margin)
            max_concurrent = max(max_concurrent, len(open_heap))
            max_concurrent_lot = max(max_concurrent_lot, sum(float(item[2]["lot"]) for item in open_heap))
        close_until(datetime.max.replace(tzinfo=UTC))

        sources = sorted({str(row["source"]) for row in closed})
        modules = sorted({str(row["module"]) for row in closed})
        by_source = {source: _summary([row for row in closed if row["source"] == source]) for source in sources}
        by_module = {module: _summary([row for row in closed if row["module"] == module]) for module in modules}
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range_utc": {
                "start": min(row["opened"] for row in events).isoformat(),
                "end": max(row["closed"] for row in events).isoformat(),
            },
            "method": "One chronological balance; no software position cap; native setup cooldowns retained; historical spread and commission inherited; broker margin enforced; lot recalculated from closed balance at every entry.",
            "start_balance": round(float(args.start_balance), 2),
            "final_balance": round(balance, 2),
            "profit": round(balance - float(args.start_balance), 2),
            "return_pct": round(100.0 * (balance / float(args.start_balance) - 1.0), 2),
            "max_closed_drawdown_usd": round(max_dd, 2),
            "max_closed_drawdown_pct_of_peak": round(max_dd_pct, 2),
            "positions": _summary(closed),
            "by_source": by_source,
            "by_module": by_module,
            "execution": {
                "candidate_positions": len(events),
                "accepted_positions": len(closed),
                "margin_skips": margin_skips,
                "max_concurrent_positions": max_concurrent,
                "max_concurrent_lot": round(max_concurrent_lot, 2),
                "max_used_margin": round(max_margin, 2),
            },
            "sizing": {
                "base_leg_lot": float(args.base_leg_lot),
                "step_usd_above_start": float(args.step_usd),
                "step_add_per_leg": float(args.step_add),
                "max_leg_lot": float(args.max_leg_lot),
            },
            "limitations": [
                "Drawdown is based on closed balance, not tick-level floating equity.",
                "Phoenix and the added modules were selected or tuned on portions of this history.",
                "The result is a historical replay, not a forecast or guarantee.",
            ],
            "closed_trades": closed,
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
        print(json.dumps({key: output[key] for key in (
            "range_utc", "start_balance", "final_balance", "profit", "return_pct",
            "max_closed_drawdown_usd", "max_closed_drawdown_pct_of_peak", "positions",
            "by_source", "execution", "sizing",
        )}, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
