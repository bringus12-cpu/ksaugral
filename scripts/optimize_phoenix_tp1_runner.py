from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown
from app.telegram_signal_bot import _phoenix_entry_brain
from backtest_phoenix_complete_60d import _fetch_history, _rates, _simulate_tp1_runner


def _summary(rows: list[dict]) -> dict:
    pnl_values = [float(row.get("pnl", 0.0) or 0.0) for row in rows]
    positive = sum(value > 0.0 for value in pnl_values)
    negative = sum(value < 0.0 for value in pnl_values)
    gross_win = sum(max(0.0, value) for value in pnl_values)
    gross_loss = abs(sum(min(0.0, value) for value in pnl_values))
    return {
        "positions": len(rows),
        "positive": positive,
        "negative": negative,
        "win_rate_pct": round(100.0 * positive / max(1, positive + negative), 2),
        "pnl": round(sum(pnl_values), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=126)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    for env_file in (".env.vantage", ".env.vantage.signal"):
        load_dotenv(Path(env_file).resolve(), override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        symbol = ensure_symbol(cfg.symbol)
        end = datetime.now(UTC)
        cutoff = end - timedelta(days=int(args.days))
        rates = _rates(symbol, cutoff - timedelta(days=1), end + timedelta(hours=1))
        point = float(getattr(mt5.symbol_info(symbol), "point", 0.01) or 0.01)
        spread_price = float(rates["spread"].median()) * point
        session = cfg.data_dir / "xauusd_signal_bot_backtest_copy.session"
        signals, _announcements = await _fetch_history(rates, cutoff, session)
        split = max(1, int(len(signals) * 0.60))
        rows = []
        for stop_cap in (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0):
            for min_reward in (0.5, 1.0, 1.5, 2.0, 2.5):
                results = []
                for signal_index, item in enumerate(signals):
                    brain = _phoenix_entry_brain(item.signal, item.market, [float(value) for value in item.signal.entries])
                    if brain.get("decision") == "skip_too_late_after_tp":
                        continue
                    result = _simulate_tp1_runner(
                        rates=rates,
                        symbol=symbol,
                        item=item,
                        lot=0.01,
                        spread_price=spread_price,
                        stop_cap=stop_cap,
                        min_reward_floor=min_reward,
                    )
                    if result.get("status") != "skip":
                        results.append((signal_index, result))
                train = _summary([result for signal_index, result in results if signal_index < split])
                holdout = _summary([result for signal_index, result in results if signal_index >= split])
                full = _summary([result for _signal_index, result in results])
                rows.append({"stop_cap": stop_cap, "min_reward": min_reward, "train": train, "holdout": holdout, "full": full})
        robust = [row for row in rows if row["train"]["pnl"] > 0 and row["holdout"]["pnl"] > 0]
        robust.sort(key=lambda row: (row["holdout"]["pnl"], row["full"]["profit_factor"] or 0.0), reverse=True)
        output = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "range": {"start": cutoff.isoformat(), "end": end.isoformat(), "days": int(args.days)},
            "signals": len(signals),
            "tested_configs": len(rows),
            "robust_configs": len(robust),
            "best": robust[0] if robust else None,
            "top10": robust[:10],
            "all_configs": rows,
        }
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output, indent=2), encoding="utf-8")
        print(json.dumps(output, indent=2))
    finally:
        shutdown()


if __name__ == "__main__":
    asyncio.run(main())
