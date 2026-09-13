from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.mt5_gateway import Mt5Credentials, connect, mt5, shutdown


EXCLUDED_NAME_PARTS = (".R", "_OLD", "-OLD", "DELIST", "CASHQ")
CORE_SYMBOL_PRIORITY = (
    "XAUUSD",
    "NAS100",
    "DJ30",
    "SP500",
    "BTCUSD",
    "XAGUSD",
    "GER40",
)


def classify(info: object) -> str:
    text = " ".join(
        str(getattr(info, name, "") or "")
        for name in ("name", "path", "description", "currency_base", "currency_profit")
    ).lower()
    if any(word in text for word in ("crypto", "bitcoin", "ethereum", "btc", "eth", "solana")):
        return "crypto"
    if any(word in text for word in ("metal", "gold", "silver", "xau", "xag", "copper")):
        return "metals"
    path = str(getattr(info, "path", "") or "").lower()
    if any(word in path for word in ("stock", "share", "equities")):
        return "stocks"
    if any(word in text for word in ("index", "indices", "nasdaq", "dax", "sp500", "nikkei")):
        return "indices"
    if any(word in text for word in ("energy", "oil", "gas", "brent", "wti")):
        return "energy"
    if any(word in text for word in ("stock", "share", "equities", "nyse", "nasdaq\\", "amex")):
        return "stocks"
    name = str(getattr(info, "name", "") or "")
    base = str(getattr(info, "currency_base", "") or "")
    profit = str(getattr(info, "currency_profit", "") or "")
    if len(base) == 3 and len(profit) == 3 and re.match(r"^[A-Z]{6}[+._-]?$", name.upper()):
        return "forex"
    return "other"


def canonical_key(info: object) -> str:
    asset_class = classify(info)
    name = str(getattr(info, "name", "") or "").upper()
    key = re.sub(r"[+._-]", "", name)
    if asset_class == "stocks":
        key = re.sub(r"24H$", "", key)
        path = str(getattr(info, "path", "") or "").lower()
        if "247 product" in path and key.endswith("USD"):
            key = key[:-3]
    elif asset_class == "indices":
        key = re.sub(r"FT$", "", key)
    return f"{asset_class}:{key}"


def eligible(info: object) -> bool:
    name = str(getattr(info, "name", "") or "")
    if not name or any(part in name.upper() for part in EXCLUDED_NAME_PARTS):
        return False
    trade_mode = int(getattr(info, "trade_mode", 0) or 0)
    return trade_mode != int(getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0))


def history_summary(symbol: str, sessions: int, days: int) -> dict | None:
    mt5.symbol_select(symbol, True)
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    raw = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M5, start, end)
    if raw is None or len(raw) < max(600, sessions * 15):
        return None
    dates = pd.to_datetime(raw["time"], unit="s", utc=True).date
    trading_days = len(set(dates))
    if trading_days < sessions:
        return None
    return {
        "bars_m5": int(len(raw)),
        "trading_days": trading_days,
        "first_bar_utc": datetime.fromtimestamp(int(raw[0]["time"]), UTC).isoformat(),
        "last_bar_utc": datetime.fromtimestamp(int(raw[-1]["time"]), UTC).isoformat(),
    }


def round_robin(groups: dict[str, list[object]]) -> list[object]:
    priority = ("forex", "stocks", "indices", "metals", "energy", "crypto", "other")
    queues = {key: deque(groups.get(key, [])) for key in priority}
    output: list[object] = []
    while any(queues.values()):
        for key in priority:
            if queues[key]:
                output.append(queues[key].popleft())
    return output


def prioritize_core_symbols(items: list[object]) -> list[object]:
    """Keep the diverse order while guaranteeing core markets are tested first."""
    priority = {name: index for index, name in enumerate(CORE_SYMBOL_PRIORITY)}
    original_order = {id(item): index for index, item in enumerate(items)}

    def rank(item: object) -> tuple[int, int]:
        name = re.sub(r"[+._-]", "", str(getattr(item, "name", "") or "").upper())
        name = re.sub(r"FT$", "", name)
        return priority.get(name, len(priority)), original_order[id(item)]

    return sorted(items, key=rank)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a diverse MT5 symbol universe with verified M5 history")
    parser.add_argument("--profile", default=".env.vantage")
    parser.add_argument("--minimum-symbols", type=int, default=100)
    parser.add_argument("--target-symbols", type=int, default=120)
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--history-days", type=int, default=220)
    parser.add_argument("--output", default="data_vantage/mt5_symbol_universe_120.json")
    args = parser.parse_args()

    load_dotenv(args.profile, override=True)
    cfg = load_settings()
    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    try:
        infos = [info for info in (mt5.symbols_get() or []) if eligible(info)]
        grouped: dict[str, list[object]] = defaultdict(list)
        seen_keys: set[str] = set()
        for info in infos:
            key = canonical_key(info)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            grouped[classify(info)].append(info)
        for items in grouped.values():
            items.sort(key=lambda info: str(getattr(info, "name", "")))

        selected: list[dict] = []
        rejected = 0
        for info in prioritize_core_symbols(round_robin(grouped)):
            if len(selected) >= args.target_symbols:
                break
            symbol = str(getattr(info, "name", ""))
            history = history_summary(symbol, args.sessions, args.history_days)
            if history is None:
                rejected += 1
                continue
            selected.append(
                {
                    "symbol": symbol,
                    "asset_class": classify(info),
                    "description": str(getattr(info, "description", "") or ""),
                    "path": str(getattr(info, "path", "") or ""),
                    "volume_min": float(getattr(info, "volume_min", 0.0) or 0.0),
                    "point": float(getattr(info, "point", 0.0) or 0.0),
                    **history,
                }
            )
    finally:
        shutdown()

    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "profile": args.profile,
        "sessions_required": args.sessions,
        "minimum_symbols_required": args.minimum_symbols,
        "eligible_catalog_symbols": len(infos),
        "unique_catalog_instruments": len(seen_keys),
        "history_rejected": rejected,
        "selected_count": len(selected),
        "asset_classes": dict(
            sorted(
                {
                    key: sum(1 for item in selected if item["asset_class"] == key)
                    for key in {item["asset_class"] for item in selected}
                }.items()
            )
        ),
        "symbols": selected,
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "symbols"}, indent=2))
    print(f"REPORT={output}")
    return 0 if len(selected) >= args.minimum_symbols else 2


if __name__ == "__main__":
    raise SystemExit(main())
