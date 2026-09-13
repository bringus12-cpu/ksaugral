from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
import MetaTrader5 as mt5


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def engine_names(profile: str, root: Path) -> dict[int, str]:
    names = {0: "MANUAL"}
    telegram_magic = int(os.getenv("MAGIC", "0") or 0)
    if telegram_magic:
        names[telegram_magic] = "TELEGRAM"
    base_magic = int(os.getenv("XAU_SCALP_MAGIC", "0") or 0)
    if base_magic:
        names[base_magic] = "XAU-SCALP"
    patterns = [".env.vantage.scalp.*"] if "vantage" in profile.lower() else [".env.puprime.live.scalp.*"]
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            values = {}
            for raw in path.read_text(encoding="utf-8-sig").splitlines():
                if "=" in raw and not raw.lstrip().startswith("#"):
                    key, value = raw.split("=", 1)
                    values[key.strip()] = value.strip()
            magic = int(values.get("XAU_SCALP_MAGIC", "0") or 0)
            if magic:
                names[magic] = values.get("XAU_SCALP_STRATEGY_NAME", path.suffix.upper())
    return names


def deal_reason_name(reason: int) -> str:
    names = {
        int(getattr(mt5, "DEAL_REASON_CLIENT", -101)): "desktop_manual",
        int(getattr(mt5, "DEAL_REASON_MOBILE", -102)): "mobile_manual",
        int(getattr(mt5, "DEAL_REASON_WEB", -103)): "web_manual",
        int(getattr(mt5, "DEAL_REASON_EXPERT", -104)): "expert",
        int(getattr(mt5, "DEAL_REASON_SL", -105)): "stop_loss",
        int(getattr(mt5, "DEAL_REASON_TP", -106)): "take_profit",
        int(getattr(mt5, "DEAL_REASON_SO", -107)): "stop_out",
    }
    return names.get(int(reason), f"reason_{int(reason)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--base-env", default="")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--broker-time-offset-minutes", type=int, default=180)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    env_path = (root / args.env).resolve()
    load_dotenv((root / ".env").resolve(), override=True)
    if args.base_env:
        load_dotenv((root / args.base_env).resolve(), override=True)
    load_dotenv(env_path, override=True)
    data_dir = root / str(os.getenv("DATA_DIR", "data") or "data")
    path = str(os.getenv("MT5_PATH", "") or "")
    if not mt5.initialize(path=path):
        raise SystemExit(f"MT5 init failed: {mt5.last_error()}")
    try:
        account = mt5.account_info()
        now = datetime.now(UTC)
        start = now - timedelta(days=args.days)
        broker_offset = timedelta(minutes=int(args.broker_time_offset_minutes))
        all_deals = list(
            mt5.history_deals_get(
                start + broker_offset - timedelta(days=3),
                now + broker_offset + timedelta(minutes=1),
            )
            or []
        )
        closing_entries = {mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY}
        entry_comments = {}
        entry_details = {}
        for deal in all_deals:
            position_id = int(getattr(deal, "position_id", 0) or 0)
            if position_id and int(getattr(deal, "entry", -1)) == mt5.DEAL_ENTRY_IN:
                entry_comments.setdefault(position_id, str(getattr(deal, "comment", "") or ""))
                opened = datetime.fromtimestamp(int(getattr(deal, "time", 0) or 0), tz=UTC) - broker_offset
                details = entry_details.setdefault(
                    position_id,
                    {
                        "opened_utc": opened.isoformat(),
                        "entry_value": 0.0,
                        "entry_volume": 0.0,
                        "side": "buy" if int(getattr(deal, "type", -1)) == mt5.DEAL_TYPE_BUY else "sell",
                        "magic": int(getattr(deal, "magic", 0) or 0),
                    },
                )
                volume = float(getattr(deal, "volume", 0.0) or 0.0)
                details["opened_utc"] = min(details["opened_utc"], opened.isoformat())
                details["entry_value"] += float(getattr(deal, "price", 0.0) or 0.0) * volume
                details["entry_volume"] += volume

        engines = engine_names(args.env, root)
        state = load_json(data_dir / "telegram_signal_state.json")
        learned = ((state.get("adaptive_learning") or {}).get("closed_positions") or {})
        analysis = load_json(data_dir / "channel_analysis.json")
        channel_names = {str(row.get("key")): str(row.get("channel") or row.get("key")) for row in analysis.get("channels", [])}

        positions = {}
        for deal in all_deals:
            closed = datetime.fromtimestamp(int(getattr(deal, "time", 0) or 0), tz=UTC) - broker_offset
            if closed < start or int(getattr(deal, "entry", -1)) not in closing_entries:
                continue
            position_id = int(getattr(deal, "position_id", 0) or 0)
            magic = int((entry_details.get(position_id) or {}).get("magic", getattr(deal, "magic", 0)) or 0)
            if magic not in engines:
                continue
            row = positions.setdefault(
                position_id,
                {
                    "position_id": position_id,
                    "closed_utc": closed.isoformat(),
                    "engine": engines[magic],
                    "magic": magic,
                    "symbol": str(getattr(deal, "symbol", "") or ""),
                    "volume": 0.0,
                    "profit": 0.0,
                    "comment": entry_comments.get(position_id, ""),
                    "reason": int(getattr(deal, "reason", 0) or 0),
                    "exit_value": 0.0,
                    "exit_volume": 0.0,
                },
            )
            row["closed_utc"] = max(row["closed_utc"], closed.isoformat())
            row["volume"] += float(getattr(deal, "volume", 0.0) or 0.0)
            exit_volume = float(getattr(deal, "volume", 0.0) or 0.0)
            row["exit_value"] += float(getattr(deal, "price", 0.0) or 0.0) * exit_volume
            row["exit_volume"] += exit_volume
            row["profit"] += sum(float(getattr(deal, field, 0.0) or 0.0) for field in ("profit", "commission", "swap", "fee"))
            learned_row = learned.get(str(position_id)) or {}
            channel_key = str(learned_row.get("channel") or "")
            if channel_key:
                row["channel"] = channel_names.get(channel_key, channel_key)

        rows = sorted(positions.values(), key=lambda row: row["closed_utc"])
        by_engine = defaultdict(lambda: {"positions": 0, "wins": 0, "losses": 0, "be": 0, "profit": 0.0, "volume": 0.0})
        by_channel = defaultdict(lambda: {"positions": 0, "wins": 0, "losses": 0, "be": 0, "profit": 0.0})
        by_day = defaultdict(lambda: {"positions": 0, "wins": 0, "losses": 0, "be": 0, "profit": 0.0})
        for row in rows:
            details = entry_details.get(int(row["position_id"])) or {}
            entry_volume = float(details.get("entry_volume", 0.0) or 0.0)
            exit_volume = float(row.pop("exit_volume", 0.0) or 0.0)
            row["opened_utc"] = details.get("opened_utc", "")
            row["side"] = details.get("side", "")
            row["entry_price"] = round(float(details.get("entry_value", 0.0) or 0.0) / entry_volume, 5) if entry_volume else 0.0
            row["exit_price"] = round(float(row.pop("exit_value", 0.0) or 0.0) / exit_volume, 5) if exit_volume else 0.0
            row["reason_name"] = deal_reason_name(int(row["reason"]))
            profit = float(row["profit"])
            outcome = "win" if profit > 0.01 else "loss" if profit < -0.01 else "be"
            outcome_key = {"win": "wins", "loss": "losses", "be": "be"}[outcome]
            row["profit"] = round(profit, 2)
            row["volume"] = round(float(row["volume"]), 2)
            row["outcome"] = outcome
            for bucket in (by_engine[row["engine"]], by_day[row["closed_utc"][:10]]):
                bucket["positions"] += 1
                bucket[outcome_key] += 1
                bucket["profit"] += profit
            by_engine[row["engine"]]["volume"] += float(row["volume"])
            if row.get("channel"):
                bucket = by_channel[row["channel"]]
                bucket["positions"] += 1
                bucket[outcome_key] += 1
                bucket["profit"] += profit

        def finish(values: dict) -> dict:
            result = {}
            for key, value in values.items():
                value = dict(value)
                decided = int(value["wins"]) + int(value["losses"])
                value["profit"] = round(float(value["profit"]), 2)
                if "volume" in value:
                    value["volume"] = round(float(value["volume"]), 2)
                value["win_rate_pct"] = round(int(value["wins"]) / max(1, decided) * 100.0, 2)
                result[key] = value
            return result

        output = {
            "generated_utc": now.isoformat(),
            "period": {"start_utc": start.isoformat(), "end_utc": now.isoformat(), "days": args.days},
            "account": {
                "login": int(getattr(account, "login", 0) or 0),
                "server": str(getattr(account, "server", "") or ""),
                "balance": float(getattr(account, "balance", 0.0) or 0.0),
                "equity": float(getattr(account, "equity", 0.0) or 0.0),
            },
            "total": finish({"all": {**{"positions": len(rows), "wins": sum(r["outcome"] == "win" for r in rows), "losses": sum(r["outcome"] == "loss" for r in rows), "be": sum(r["outcome"] == "be" for r in rows), "profit": sum(float(r["profit"]) for r in rows)}, "volume": sum(float(r["volume"]) for r in rows)}})["all"],
            "by_engine": finish(by_engine),
            "by_channel": finish(by_channel),
            "by_day": finish(by_day),
            "positions": rows,
        }
        target = root / args.output
        target.write_text(json.dumps(output, ensure_ascii=True, indent=2), encoding="utf-8")
        print(target)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
