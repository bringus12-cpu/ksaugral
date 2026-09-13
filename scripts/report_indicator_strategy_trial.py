from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
TRIAL_ROOT = ROOT / "data_vantage" / "indicator_strategies"
OUTPUT = TRIAL_ROOT / "indicator_strategy_trial_summary.json"


def _json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _trades(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def build_report() -> dict[str, Any]:
    strategies: list[dict[str, Any]] = []
    if not TRIAL_ROOT.exists():
        return {"generated_utc": datetime.now(UTC).isoformat(), "strategies": [], "totals": {}}

    for directory in sorted(path for path in TRIAL_ROOT.iterdir() if path.is_dir()):
        status = _json(directory / "xau_scalp_status.json")
        rows = _trades(directory / "xau_scalp_trades.csv")
        pnl = [float(row.get("profit", 0.0) or 0.0) for row in rows]
        wins = [value for value in pnl if value > 0.005]
        losses = [value for value in pnl if value < -0.005]
        strategies.append(
            {
                "key": directory.name,
                "strategy": status.get("strategy", directory.name),
                "magic": status.get("magic"),
                "heartbeat_utc": status.get("heartbeat_utc"),
                "account": (status.get("account") or {}).get("login"),
                "trades": len(rows),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate_pct": round(100.0 * len(wins) / max(1, len(wins) + len(losses)), 2),
                "realized_pnl": round(sum(pnl), 2),
                "average_win": round(sum(wins) / max(1, len(wins)), 2),
                "average_loss": round(sum(losses) / max(1, len(losses)), 2),
                "open_positions": int(status.get("positions_count", 0) or 0),
                "open_pnl": float(status.get("open_profit", 0.0) or 0.0),
                "last_reason": status.get("last_reason"),
                "signal": status.get("signal"),
            }
        )

    all_trades = sum(int(row["trades"]) for row in strategies)
    all_wins = sum(int(row["wins"]) for row in strategies)
    all_losses = sum(int(row["losses"]) for row in strategies)
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "trial_account": "VantageMarkets-Demo",
        "strategies": strategies,
        "totals": {
            "strategies": len(strategies),
            "trades": all_trades,
            "wins": all_wins,
            "losses": all_losses,
            "win_rate_pct": round(100.0 * all_wins / max(1, all_wins + all_losses), 2),
            "realized_pnl": round(sum(float(row["realized_pnl"]) for row in strategies), 2),
            "open_positions": sum(int(row["open_positions"]) for row in strategies),
            "open_pnl": round(sum(float(row["open_pnl"]) for row in strategies), 2),
        },
    }
    return report


def main() -> None:
    report = build_report()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
