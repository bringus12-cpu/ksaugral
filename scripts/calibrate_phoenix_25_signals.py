from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _phoenix_market_runner_allowed
from backtest_phoenix_runner_ladder_40d import _fetch_signals, _profit, _rates, _simulate_leg


TARGET_PLANS = {
    "tp1_all": (1, 1, 1),
    "tp1_tp1_tp2": (1, 1, 2),
    "tp1_tp1_tp3": (1, 1, 3),
    "tp1_tp2_tp3": (1, 2, 3),
    "tp1_tp2_tp4": (1, 2, 4),
    "tp1_tp2_tp5": (1, 2, 5),
    "tp1_tp3_tp5": (1, 3, 5),
    "tp1_tp3_tp6": (1, 3, 6),
}

PROTECT_PLANS = {
    "no_be": ("none", "none", "none"),
    "be_after_tp1": ("none", "be", "be"),
    "be_after_tp2": ("none", "be_after_tp2", "be_after_tp2"),
    "be_after_tp3": ("none", "be_after_tp3", "be_after_tp3"),
    "runner_ladder": ("none", "be", "phoenix_ladder"),
    "runner_delayed": ("none", "be_after_tp2", "tp1_after_tp3"),
}


@dataclass(frozen=True)
class Config:
    target_name: str
    protect_name: str
    sl_cap: float

    @property
    def name(self) -> str:
        return f"{self.target_name}__{self.protect_name}__sl{self.sl_cap:g}"


def _entry_for_signal(item: Any) -> tuple[float, bool] | None:
    entries = [float(value) for value in item.signal.entries if float(value or 0.0) > 0]
    if _phoenix_market_runner_allowed(item.signal.side, entries, item.market, item.signal.tps):
        return float(item.market), False
    valid = [
        value
        for value in entries
        if (value < item.market if item.signal.side == "buy" else value > item.market)
    ]
    if not valid:
        return None
    return min(valid, key=lambda value: abs(value - item.market)), True


def _result_value(result: dict[str, Any], spread_cost: float) -> float:
    return round(float(result.get("pnl", 0.0) or 0.0) - spread_cost, 2)


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["message_id"]), []).append(row)
    signal_pnls = [round(sum(float(leg["net_pnl"]) for leg in legs), 2) for legs in grouped.values()]
    positive = sum(value > 0.01 for value in signal_pnls)
    negative = sum(value < -0.01 for value in signal_pnls)
    flat = len(signal_pnls) - positive - negative
    return {
        "signals": len(grouped),
        "legs": len(rows),
        "positive_signals": positive,
        "negative_signals": negative,
        "flat_signals": flat,
        "signal_win_rate_pct": round(100.0 * positive / max(1, positive + negative), 2),
        "non_loss_rate_pct": round(100.0 * (positive + flat) / max(1, len(signal_pnls)), 2),
        "net_pnl_001_per_leg": round(sum(signal_pnls), 2),
        "avg_signal_pnl": round(sum(signal_pnls) / max(1, len(signal_pnls)), 2),
        "loss_signals": negative,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--days", type=int, default=40)
    parser.add_argument("--signals", type=int, default=25)
    parser.add_argument("--broker-time-offset-minutes", type=int, default=180)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    for env_file in args.env:
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        offset = timedelta(minutes=int(args.broker_time_offset_minutes))
        end = datetime.now(UTC) + offset
        start = end - timedelta(days=max(5, int(args.days)) + 2)
        rates = _rates(symbol, start, end)
        all_signals = await _fetch_signals(rates, int(args.days), int(args.broker_time_offset_minutes))
        executable_signals = [item for item in all_signals if _entry_for_signal(item) is not None]
        signals = executable_signals[-max(1, int(args.signals)):]
        if not signals:
            raise RuntimeError("No Phoenix signals found")

        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        median_spread = float(rates["spread"].median()) * point if "spread" in rates.columns else 0.0
        anchor = float(rates.iloc[-1]["close"])
        spread_cost = abs(_profit(symbol, "buy", anchor, anchor + median_spread)) if median_spread > 0 else 0.0

        configs = [Config(target, protect, cap) for target in TARGET_PLANS for protect in PROTECT_PLANS for cap in (2.5, 3.0, 4.0, 5.0, 6.0)]
        rows_by_config: dict[str, list[dict[str, Any]]] = {item.name: [] for item in configs}
        skipped_by_config: dict[str, int] = {item.name: 0 for item in configs}
        split_index = max(1, int(len(signals) * 0.60))

        for signal_index, item in enumerate(signals):
            entry_plan = _entry_for_signal(item)
            if entry_plan is None:
                continue
            entry, pending = entry_plan
            for config in configs:
                for leg_no, (target, protect) in enumerate(zip(TARGET_PLANS[config.target_name], PROTECT_PLANS[config.protect_name]), start=1):
                    result = _simulate_leg(
                        symbol,
                        item.signal,
                        rates,
                        item.start_idx,
                        entry,
                        pending,
                        target,
                        protect,
                        sl_cap=config.sl_cap,
                    )
                    if result.get("status") == "skip":
                        skipped_by_config[config.name] += 1
                        rows_by_config[config.name].append(
                            {
                                "signal_index": signal_index,
                                "time": item.dt.isoformat(),
                                "message_id": item.message_id,
                                "side": item.signal.side,
                                "entry": round(entry, 2),
                                "pending": pending,
                                "leg": leg_no,
                                "target": target,
                                "protect": protect,
                                "status": "not_entered",
                                "reason": result.get("reason", "skip"),
                                "exit": None,
                                "gross_pnl": 0.0,
                                "net_pnl": 0.0,
                            }
                        )
                        continue
                    rows_by_config[config.name].append(
                        {
                            "signal_index": signal_index,
                            "time": item.dt.isoformat(),
                            "message_id": item.message_id,
                            "side": item.signal.side,
                            "entry": round(entry, 2),
                            "pending": pending,
                            "leg": leg_no,
                            "target": target,
                            "protect": protect,
                            "status": result.get("status"),
                            "exit": result.get("exit"),
                            "gross_pnl": result.get("pnl"),
                            "net_pnl": _result_value(result, spread_cost),
                        }
                    )

        ranked: list[dict[str, Any]] = []
        for config in configs:
            rows = rows_by_config[config.name]
            train = [row for row in rows if int(row["signal_index"]) < split_index]
            holdout = [row for row in rows if int(row["signal_index"]) >= split_index]
            ranked.append(
                {
                    "config": config.name,
                    "target_plan": TARGET_PLANS[config.target_name],
                    "protect_plan": PROTECT_PLANS[config.protect_name],
                    "sl_cap": config.sl_cap,
                    "train": _summarize(train),
                    "holdout": _summarize(holdout),
                    "full": _summarize(rows),
                    "skipped_legs": skipped_by_config[config.name],
                }
            )
        ranked.sort(
            key=lambda row: (
                min(row["train"]["signal_win_rate_pct"], row["holdout"]["signal_win_rate_pct"]),
                row["full"]["net_pnl_001_per_leg"],
                row["holdout"]["net_pnl_001_per_leg"],
            ),
            reverse=True,
        )
        robust = [
            row for row in ranked
            if row["train"]["net_pnl_001_per_leg"] > 0
            and row["holdout"]["net_pnl_001_per_leg"] > 0
        ]
        distinct_ranked = [row for row in ranked if len(set(row["target_plan"])) == 3]
        distinct_robust = [
            row for row in distinct_ranked
            if row["train"]["net_pnl_001_per_leg"] > 0
            and row["holdout"]["net_pnl_001_per_leg"] > 0
        ]
        winner = robust[0] if robust else max(
            ranked,
            key=lambda row: (
                min(row["train"]["net_pnl_001_per_leg"], row["holdout"]["net_pnl_001_per_leg"]),
                row["full"]["net_pnl_001_per_leg"],
                row["full"]["signal_win_rate_pct"],
            ),
        )
        winner_rows = rows_by_config[winner["config"]]
        no_be = next(
            (
                row for row in ranked
                if tuple(row["target_plan"]) == tuple(winner["target_plan"])
                and row["protect_plan"] == PROTECT_PLANS["no_be"]
                and float(row["sl_cap"]) == float(winner["sl_cap"])
            ),
            None,
        )
        no_be_rows = rows_by_config[no_be["config"]] if no_be else []
        per_signal: list[dict[str, Any]] = []
        be_improved = be_reduced = be_same = 0
        for item in signals:
            legs = [row for row in winner_rows if int(row["message_id"]) == int(item.message_id)]
            baseline_legs = [row for row in no_be_rows if int(row["message_id"]) == int(item.message_id)]
            net_pnl = round(sum(float(row["net_pnl"]) for row in legs), 2)
            no_be_pnl = round(sum(float(row["net_pnl"]) for row in baseline_legs), 2)
            delta = round(net_pnl - no_be_pnl, 2)
            if delta > 0.01:
                effect = "improved"
                be_improved += 1
            elif delta < -0.01:
                effect = "reduced"
                be_reduced += 1
            else:
                effect = "same"
                be_same += 1
            per_signal.append(
                {
                    "time": item.dt.isoformat(),
                    "message_id": item.message_id,
                    "side": item.signal.side,
                    "announced_entry": item.signal.entries,
                    "sl": item.signal.sl,
                    "tps": item.signal.tps,
                    "legs": legs,
                    "net_pnl": net_pnl,
                    "same_targets_no_be_pnl": no_be_pnl,
                    "be_delta": delta,
                    "be_effect": effect,
                }
            )
        payload = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "channel": "PHOENIX VIP",
            "sample_signals": len(signals),
            "sample_range": {"start": signals[0].dt.isoformat(), "end": signals[-1].dt.isoformat()},
            "timeframe": "M1",
            "broker_time_offset_minutes": int(args.broker_time_offset_minutes),
            "median_spread_usd": round(median_spread, 4),
            "spread_cost_001_per_leg": round(spread_cost, 4),
            "train_signals": split_index,
            "holdout_signals": len(signals) - split_index,
            "tested_configs": len(configs),
            "robust_winner_found": bool(robust),
            "best": winner,
            "best_distinct_targets": distinct_robust[0] if distinct_robust else (distinct_ranked[0] if distinct_ranked else None),
            "same_targets_without_be": no_be,
            "be_effect_by_signal": {"improved": be_improved, "reduced": be_reduced, "same": be_same},
            "top10": ranked[:10],
            "per_signal_best": per_signal,
            "selection_note": "Winner must be profitable in train and holdout; then maximize the weaker split win rate, full PnL and holdout PnL.",
        }
        Path(args.output).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({key: payload[key] for key in ("sample_signals", "tested_configs", "best", "same_targets_without_be")}, indent=2, ensure_ascii=False))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
