from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SOURCE_LOTS = {
    "phoenix_full": 0.10,
    "phoenix_direction": 0.01,
    "sc_adx_break_07": 0.05,
    "ind_bb_kelt_macd": 0.05,
    "ind_bb_macd_rcl": 0.05,
}

# Conservative stop-risk estimates for one 0.01-lot leg, including a small cost buffer.
SOURCE_RISK_001 = {
    "phoenix_full": 12.06,
    "phoenix_direction": 6.06,
    "sc_adx_break_07": 5.06,
    "ind_bb_kelt_macd": 12.06,
    "ind_bb_macd_rcl": 18.06,
}


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def round2(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Replay the audited portfolio under an FTMO 50K safety profile.")
    parser.add_argument(
        "--input",
        type=Path,
        default=root / "data_vantage" / "audit_all_modules_fixed001_300sessions_start1000_20260813.json",
    )
    parser.add_argument("--start-balance", type=float, default=50_000.0)
    parser.add_argument("--internal-daily-pct", type=float, default=2.0)
    parser.add_argument("--internal-total-pct", type=float, default=4.0)
    parser.add_argument("--official-daily-pct", type=float, default=5.0)
    parser.add_argument("--official-total-pct", type=float, default=10.0)
    parser.add_argument("--target-pct", type=float, default=10.0)
    parser.add_argument("--max-open-risk-pct", type=float, default=1.5)
    parser.add_argument("--sessions", type=int, default=0, help="Use only the latest N UTC trading dates; 0 uses all rows.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    raw = json.loads(args.input.read_text(encoding="utf-8"))
    rows = sorted(raw["closed_trades"], key=lambda row: (row["opened"], row["closed"], row["source"]))
    selected_dates = sorted({row["opened"][:10] for row in rows})
    if args.sessions > 0:
        selected_dates = selected_dates[-args.sessions :]
        selected_date_set = set(selected_dates)
        rows = [row for row in rows if row["opened"][:10] in selected_date_set]
    warsaw = ZoneInfo("Europe/Warsaw")

    events: list[tuple[datetime, int, dict]] = []
    for index, row in enumerate(rows):
        source = row["source"]
        if source not in SOURCE_LOTS:
            continue
        events.append((parse_time(row["opened"]), 0, {"index": index, "row": row}))
        events.append((parse_time(row["closed"]), 1, {"index": index, "row": row}))
    events.sort(key=lambda item: (item[0], item[1]))  # zero-duration trades must open before they close

    start = args.start_balance
    balance = start
    peak = start
    max_closed_dd = 0.0
    internal_daily_usd = start * args.internal_daily_pct / 100.0
    internal_total_floor = start * (1.0 - args.internal_total_pct / 100.0)
    official_total_floor = start * (1.0 - args.official_total_pct / 100.0)
    max_open_risk_usd = start * args.max_open_risk_pct / 100.0
    target_balance = start * (1.0 + args.target_pct / 100.0)

    open_rows: dict[int, dict] = {}
    accepted: set[int] = set()
    skipped: list[dict] = []
    current_day = None
    day_start_balance = start
    day_min_balance = start
    daily_records: dict[str, dict] = {}
    trading_days: set[str] = set()
    target_reached_at = None
    max_concurrent_lot = 0.0
    max_concurrent_positions = 0
    max_projected_open_risk = 0.0
    min_projected_equity = start
    max_projected_daily_loss = 0.0
    official_breaches: list[dict] = []
    internal_breaches: list[dict] = []
    by_source = defaultdict(lambda: {"positions": 0, "wins": 0, "losses": 0, "pnl": 0.0})

    def finish_day(day_key: str | None) -> None:
        if day_key is None:
            return
        loss = max(0.0, day_start_balance - day_min_balance)
        daily_records[day_key] = {
            "start_balance": round2(day_start_balance),
            "end_balance": round2(balance),
            "max_closed_loss_usd": round2(loss),
            "max_closed_loss_pct_of_start": round(loss / day_start_balance * 100.0, 4) if day_start_balance else 0.0,
        }

    for timestamp, event_type, payload in events:
        local_day = timestamp.astimezone(warsaw).date().isoformat()
        if local_day != current_day:
            finish_day(current_day)
            current_day = local_day
            day_start_balance = balance
            day_min_balance = balance

        index = payload["index"]
        row = payload["row"]
        source = row["source"]
        lot = SOURCE_LOTS[source]
        risk = SOURCE_RISK_001[source] * (lot / 0.01)

        if event_type == 0:
            open_risk = sum(item["risk"] for item in open_rows.values())
            projected_floor = balance - open_risk - risk
            internal_daily_floor = day_start_balance - internal_daily_usd
            reason = None
            if open_risk + risk > max_open_risk_usd + 1e-9:
                reason = "max_open_risk"
            elif projected_floor < internal_daily_floor - 1e-9:
                reason = "internal_daily_floor"
            elif projected_floor < internal_total_floor - 1e-9:
                reason = "internal_total_floor"
            if reason:
                skipped.append({"source": source, "opened": row["opened"], "reason": reason, "risk_usd": round2(risk)})
                continue

            accepted.add(index)
            open_rows[index] = {"risk": risk, "lot": lot, "source": source}
            trading_days.add(local_day)
            concurrent_lot = sum(item["lot"] for item in open_rows.values())
            concurrent_risk = sum(item["risk"] for item in open_rows.values())
            max_concurrent_lot = max(max_concurrent_lot, concurrent_lot)
            max_concurrent_positions = max(max_concurrent_positions, len(open_rows))
            max_projected_open_risk = max(max_projected_open_risk, concurrent_risk)
            min_projected_equity = min(min_projected_equity, balance - concurrent_risk)
            max_projected_daily_loss = max(max_projected_daily_loss, day_start_balance - (balance - concurrent_risk))
            continue

        if index not in accepted:
            continue
        open_rows.pop(index, None)
        pnl = float(row["pnl_001"]) * (lot / 0.01)
        balance += pnl
        peak = max(peak, balance)
        max_closed_dd = max(max_closed_dd, peak - balance)
        day_min_balance = min(day_min_balance, balance)
        stats = by_source[source]
        stats["positions"] += 1
        stats["pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1
        elif pnl < 0:
            stats["losses"] += 1

        daily_loss = day_start_balance - balance
        if daily_loss > start * args.official_daily_pct / 100.0 and not any(x["day"] == local_day for x in official_breaches):
            official_breaches.append({"day": local_day, "kind": "daily_closed", "loss_usd": round2(daily_loss)})
        if balance < official_total_floor and not any(x["kind"] == "overall_closed" for x in official_breaches):
            official_breaches.append({"day": local_day, "kind": "overall_closed", "balance": round2(balance)})
        if daily_loss > internal_daily_usd and not any(x["day"] == local_day for x in internal_breaches):
            internal_breaches.append({"day": local_day, "kind": "daily_closed", "loss_usd": round2(daily_loss)})
        if balance < internal_total_floor and not any(x["kind"] == "overall_closed" for x in internal_breaches):
            internal_breaches.append({"day": local_day, "kind": "overall_closed", "balance": round2(balance)})

        if target_reached_at is None and balance >= target_balance and len(trading_days) >= 4:
            target_reached_at = timestamp.isoformat()

        remaining_risk = sum(item["risk"] for item in open_rows.values())
        min_projected_equity = min(min_projected_equity, balance - remaining_risk)
        max_projected_daily_loss = max(max_projected_daily_loss, day_start_balance - (balance - remaining_risk))

    finish_day(current_day)

    by_source_output = {}
    for source, stats in sorted(by_source.items()):
        decided = stats["wins"] + stats["losses"]
        by_source_output[source] = {
            "lot_per_position": SOURCE_LOTS[source],
            "positions": stats["positions"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate_pct": round(stats["wins"] / decided * 100.0, 2) if decided else 0.0,
            "pnl_usd": round2(stats["pnl"]),
        }

    result = {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_report": str(args.input),
        "selected_sessions": len(selected_dates),
        "selected_range_utc": {"from": selected_dates[0], "to": selected_dates[-1]} if selected_dates else None,
        "profile": "FTMO 50K 2-Step conservative-current",
        "start_balance": round2(start),
        "final_balance": round2(balance),
        "net_profit": round2(balance - start),
        "return_pct": round((balance - start) / start * 100.0, 3),
        "target_balance": round2(target_balance),
        "target_reached_at": target_reached_at,
        "trading_days": len(trading_days),
        "accepted_positions": len(accepted),
        "skipped_positions": len(skipped),
        "skip_reasons": dict(sorted((reason, sum(1 for row in skipped if row["reason"] == reason)) for reason in {row["reason"] for row in skipped})),
        "max_closed_drawdown_usd": round2(max_closed_dd),
        "max_closed_drawdown_pct_of_peak": round(max_closed_dd / peak * 100.0, 3) if peak else 0.0,
        "max_projected_open_risk_usd": round2(max_projected_open_risk),
        "max_projected_open_risk_pct": round(max_projected_open_risk / start * 100.0, 3),
        "min_projected_equity": round2(min_projected_equity),
        "max_projected_daily_loss_usd": round2(max_projected_daily_loss),
        "max_projected_daily_loss_pct_of_initial": round(max_projected_daily_loss / start * 100.0, 3),
        "max_concurrent_positions": max_concurrent_positions,
        "max_concurrent_lot": round(max_concurrent_lot, 2),
        "internal_limits": {
            "daily_pct": args.internal_daily_pct,
            "overall_pct": args.internal_total_pct,
            "max_open_risk_pct": args.max_open_risk_pct,
            "breaches_on_closed_balance": internal_breaches,
        },
        "official_limits_checked": {
            "daily_pct": args.official_daily_pct,
            "overall_pct": args.official_total_pct,
            "breaches_on_closed_balance": official_breaches,
        },
        "by_source": by_source_output,
        "daily": daily_records,
        "limitations": [
            "Replay uses previously audited closed trades and scales their modeled PnL by lot.",
            "Tick-level floating equity and FTMO midnight equity snapshots are unavailable, so formal rule compliance cannot be guaranteed.",
            "Projected open risk is a conservative stop-distance estimate, not an observed floating drawdown path.",
            "The source strategy was selected or tuned on portions of this same history; this is not an out-of-sample forecast.",
        ],
    }

    output = args.output or root / "data_vantage" / "ftmo50k_current_portfolio_300sessions_20260814.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "profile", "start_balance", "final_balance", "net_profit", "return_pct", "target_reached_at",
        "trading_days", "accepted_positions", "skipped_positions", "max_closed_drawdown_usd",
        "max_closed_drawdown_pct_of_peak", "max_projected_open_risk_usd", "max_concurrent_lot",
    )}, indent=2))
    print(f"Report: {output}")


if __name__ == "__main__":
    main()
