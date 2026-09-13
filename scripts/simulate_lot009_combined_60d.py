from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPREAD_PER_001_LEG = 0.18
BASE_TOTAL_LOT = 0.09
STEP_PROFIT_USD = 500.0
STEP_LOT = 0.01
SELECTED_TELEGRAM = {
    "FX GOLD XAUUSD FREE SIGNALS",
    "GOLD PRO TRADER (xauusd)",
    "PHOENIX VIP",
    "XAUUSD GOLD KILLER (VIP)™",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def split_lot(total_lot: float, legs: int) -> list[float]:
    total_steps = max(1, int(round(total_lot / 0.01)))
    count = max(1, min(int(legs), total_steps))
    base, extra = divmod(total_steps, count)
    return [(base + (1 if index < extra else 0)) * 0.01 for index in range(count)]


def telegram_setups(selected_only: bool) -> list[dict]:
    report = load(ROOT / "data_vantage" / "backtest_telegram_current_60d_risk_source.json")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for leg in report.get("equity", []):
        channel = str(leg.get("channel") or "")
        if selected_only and channel not in SELECTED_TELEGRAM:
            continue
        key = (channel, int(leg.get("message_id") or 0), str(leg.get("signal_time") or ""))
        groups[key].append(leg)
    setups = []
    for (channel, message_id, signal_time), legs in groups.items():
        legs.sort(key=lambda row: (int(row.get("target_index") or 0), str(row.get("exit_time") or "")))
        setups.append(
            {
                "time": max(datetime.fromisoformat(str(row["exit_time"])) for row in legs),
                "source": f"TG:{channel}",
                "setup_id": f"{channel}:{message_id}:{signal_time}",
                "unit_legs": [float(row.get("profit_001") or 0.0) - SPREAD_PER_001_LEG for row in legs],
            }
        )
    return setups


def scalper_setups(names: list[str]) -> list[dict]:
    setups = []
    for name in names:
        folder = "data_upcomers" if name == "upcomers" else "data_vantage"
        report = load(ROOT / folder / f"sim60_lot009_{name}.json")
        groups: dict[str, list[dict]] = defaultdict(list)
        for leg in report.get("trades", []):
            groups[str(leg.get("opened") or "")].append(leg)
        for opened, legs in groups.items():
            legs.sort(key=lambda row: str(row.get("leg") or ""))
            setups.append(
                {
                    "time": max(datetime.fromisoformat(str(row["closed"])) for row in legs),
                    "source": f"SCALP:{name.upper()}",
                    "setup_id": f"{name}:{opened}",
                    "unit_legs": [float(row.get("profit_001") or 0.0) - SPREAD_PER_001_LEG for row in legs],
                }
            )
    return setups


def dynamic_lot(balance: float, reference: float) -> float:
    steps = int(max(0.0, balance - reference) // STEP_PROFIT_USD)
    return round(BASE_TOTAL_LOT + steps * STEP_LOT, 2)


def setup_profit(setup: dict, total_lot: float, positive_haircut: float = 1.0) -> float:
    units = list(setup["unit_legs"])
    volumes = split_lot(total_lot, len(units))
    value = sum(unit * (volume / 0.01) for unit, volume in zip(units, volumes))
    return value * positive_haircut if value > 0 else value


def simulate(
    setups: list[dict],
    start_balance: float,
    *,
    funded: bool = False,
    funded_initial_balance: float = 50_000.0,
    positive_haircut: float = 1.0,
) -> dict:
    balance = float(start_balance)
    reference = balance
    peak = balance
    max_dd = 0.0
    wins = losses = skipped = 0
    daily_open: dict[date, float] = {}
    daily_pnl: dict[str, float] = defaultdict(float)
    blocked_days: set[date] = set()
    overall_floor = funded_initial_balance * 0.90 if funded else -math.inf
    halted = False
    max_lot = BASE_TOTAL_LOT
    by_source: dict[str, float] = defaultdict(float)
    for setup in sorted(setups, key=lambda row: row["time"]):
        day = setup["time"].date()
        daily_open.setdefault(day, balance)
        if halted or day in blocked_days:
            skipped += 1
            continue
        if funded and balance <= overall_floor:
            halted = True
            skipped += 1
            continue
        total_lot = dynamic_lot(balance, reference)
        max_lot = max(max_lot, total_lot)
        profit = setup_profit(setup, total_lot, positive_haircut)
        balance += profit
        by_source[setup["source"]] += profit
        daily_pnl[day.isoformat()] += profit
        wins += profit > 0
        losses += profit < 0
        peak = max(peak, balance)
        max_dd = min(max_dd, balance - peak)
        if funded and balance - daily_open[day] <= -(daily_open[day] * 0.047):
            blocked_days.add(day)
        if funded and balance <= overall_floor:
            halted = True
    return {
        "start_balance": round(start_balance, 2),
        "final_balance": round(balance, 2),
        "profit": round(balance - start_balance, 2),
        "return_pct": round((balance / start_balance - 1.0) * 100.0, 2),
        "max_closed_drawdown_usd": round(max_dd, 2),
        "max_closed_drawdown_pct": round(abs(max_dd) / max(1.0, peak) * 100.0, 2),
        "setups": wins + losses,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / max(1, wins + losses) * 100.0, 2),
        "skipped_by_guard": skipped,
        "funded_halted": halted,
        "max_total_lot": round(max_lot, 2),
        "daily_pnl": dict(daily_pnl),
        "by_source": {key: round(value, 2) for key, value in sorted(by_source.items(), key=lambda item: item[1], reverse=True)},
    }


def trading_days(start: date, end: date) -> list[date]:
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def forecast(setups: list[dict], start_balance: float, *, funded: bool = False, paths: int = 5000) -> dict:
    by_day: dict[date, list[dict]] = defaultdict(list)
    for setup in setups:
        by_day[setup["time"].date()].append(setup)
    samples = [rows for _, rows in sorted(by_day.items()) if rows]
    future_days = trading_days(date(2026, 7, 15), date(2026, 12, 31))
    rng = random.Random(20260714)
    end_values = []
    month_values: dict[str, list[float]] = defaultdict(list)
    ruined = 0
    for _ in range(paths):
        balance = float(start_balance)
        reference = balance
        overall_floor = 45_000.0 if funded else -math.inf
        month_close: dict[str, float] = {}
        halted = False
        for future_day in future_days:
            day_open = balance
            rows = rng.choice(samples)
            for setup in rows:
                if halted:
                    break
                lot = dynamic_lot(balance, reference)
                balance += setup_profit(setup, lot, positive_haircut=0.5)
                if funded and balance - day_open <= -(day_open * 0.047):
                    break
                if funded and balance <= overall_floor:
                    halted = True
            month_close[future_day.strftime("%Y-%m")] = balance
        if halted:
            ruined += 1
        end_values.append(balance)
        for month, value in month_close.items():
            month_values[month].append(value)

    def quantiles(values: list[float]) -> dict:
        ordered = sorted(values)
        return {
            "p10": round(ordered[int(0.10 * (len(ordered) - 1))], 2),
            "median": round(ordered[int(0.50 * (len(ordered) - 1))], 2),
            "p90": round(ordered[int(0.90 * (len(ordered) - 1))], 2),
        }

    return {
        "method": "5000 bootstrap paths; historical trading days sampled with replacement; positive setup PnL reduced by 50%",
        "trading_days": len(future_days),
        "funded_failure_probability_pct": round(ruined / paths * 100.0, 2) if funded else None,
        "end_balance": quantiles(end_values),
        "month_end": {month: quantiles(values) for month, values in sorted(month_values.items())},
    }


def main() -> None:
    tg_all = telegram_setups(False)
    tg_selected = telegram_setups(True)
    vantage_scalpers = scalper_setups(["core", "hours", "strict", "quality", "range"])
    range_scalper = scalper_setups(["range"])
    upcomers_scalper = scalper_setups(["upcomers"])
    portfolios = {
        "vantage_all_current": (tg_all + vantage_scalpers, 8087.68, False),
        "vantage_selected_tg_plus_range": (tg_selected + range_scalper, 8087.68, False),
        "vantage_selected_tg_only": (tg_selected, 8087.68, False),
        "upcomers_all_current": (tg_all + upcomers_scalper, 47669.36, True),
        "upcomers_selected_tg_only": (tg_selected, 47669.36, True),
    }
    output = {
        "generated_utc": datetime.now().astimezone().isoformat(),
        "lot_rule": "0.09 total per setup, split across legs; +0.01 for each full +500 USD closed balance profit",
        "spread_per_001_leg": SPREAD_PER_001_LEG,
        "funded_rules": {"daily_dd_pct": 4.7, "overall_floor": 45000.0},
        "historical_60d": {},
        "forecast_to_2026_end": {},
    }
    for name, (setups, start, funded) in portfolios.items():
        output["historical_60d"][name] = simulate(setups, start, funded=funded)
        output["forecast_to_2026_end"][name] = forecast(setups, start, funded=funded)
    target = ROOT / "data_vantage" / "combined_lot009_60d_forecast_2026.json"
    target.write_text(json.dumps(output, ensure_ascii=True, indent=2), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
