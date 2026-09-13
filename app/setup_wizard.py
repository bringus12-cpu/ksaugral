from __future__ import annotations

import getpass
from pathlib import Path

DEFAULT_WATCH_CHANNELS = (
    "https://t.me/Gold_Pro_Trader_Forex_Signal,"
    "https://t.me/cryptoalertyt"
)

DEFAULT_ENV = {
    "LEGACY_ENV_PATH": "",
    "MT5_LOGIN": "",
    "MT5_PASSWORD": "",
    "MT5_SERVER": "",
    "MT5_PATH": "",
    "SYMBOL": "XAUUSD",
    "REGIME_TIMEFRAME": "M15",
    "ENTRY_TIMEFRAME": "M5",
    "HISTORY_BARS": "800",
    "LOOP_SECONDS": "5",
    "MAGIC": "994241",
    "DEVIATION": "50",
    "RISK_PER_TRADE_PCT": "0.40",
    "MIN_LOT": "0.01",
    "MAX_LOT": "0.01",
    "MAX_TOTAL_LOT": "0.01",
    "MAX_OPEN_POSITIONS": "1",
    "MAX_DAILY_DRAWDOWN_PCT": "2.5",
    "MAX_SPREAD_POINTS": "120",
    "MIN_SIGNAL_SCORE": "60",
    "COOLDOWN_BARS": "3",
    "TREND_SL_ATR": "1.5",
    "TREND_TP_RR": "2.4",
    "BREAKOUT_SL_ATR": "1.3",
    "BREAKOUT_TP_RR": "2.8",
    "MEANREV_SL_ATR": "1.1",
    "MEANREV_TP_RR": "1.8",
    "BREAKEVEN_AT_R": "1.0",
    "TRAIL_START_R": "1.4",
    "TRAIL_ATR_MULT": "1.1",
    "PARTIAL_CLOSE_AT_R": "1.6",
    "PARTIAL_CLOSE_PCT": "0.35",
    "REVERSAL_EXIT_SCORE": "72",
    "ENABLE_LONGS": "true",
    "ENABLE_SHORTS": "true",
    "DATA_DIR": "data",
    "DASHBOARD_PORT": "8787",
    "TELEGRAM_API_ID": "",
    "TELEGRAM_API_HASH": "",
    "TELEGRAM_PHONE": "",
    "TELEGRAM_2FA_PASSWORD": "",
    "TELEGRAM_SESSION_NAME": "xauusd_signal_bot",
    "WATCH_CHANNELS": DEFAULT_WATCH_CHANNELS,
    "ALLOWED_SENDER_IDS": "",
    "ALLOWED_POST_AUTHORS": "",
    "SIGNAL_FIXED_LOT": "0.01",
    "SIGNAL_SL_ATR_MULT": "1.5",
    "SIGNAL_SL_MIN_POINTS": "50",
    "SIGNAL_TP_TARGET_INDEX": "2",
    "SIGNAL_PROTECT_TP1_ENABLED": "true",
    "SIGNAL_PROTECT_TP1_TRIGGER_PCT": "0.20",
}

REQUIRED_KEYS = (
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_PHONE",
    "WATCH_CHANNELS",
    "SIGNAL_FIXED_LOT",
)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    groups = [
        ("MT5", ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH")),
        (
            "Trading",
            (
                "SYMBOL",
                "REGIME_TIMEFRAME",
                "ENTRY_TIMEFRAME",
                "HISTORY_BARS",
                "LOOP_SECONDS",
                "MAGIC",
                "DEVIATION",
                "RISK_PER_TRADE_PCT",
                "MIN_LOT",
                "MAX_LOT",
                "MAX_TOTAL_LOT",
                "MAX_OPEN_POSITIONS",
                "MAX_DAILY_DRAWDOWN_PCT",
                "MAX_SPREAD_POINTS",
                "MIN_SIGNAL_SCORE",
                "COOLDOWN_BARS",
                "TREND_SL_ATR",
                "TREND_TP_RR",
                "BREAKOUT_SL_ATR",
                "BREAKOUT_TP_RR",
                "MEANREV_SL_ATR",
                "MEANREV_TP_RR",
                "BREAKEVEN_AT_R",
                "TRAIL_START_R",
                "TRAIL_ATR_MULT",
                "PARTIAL_CLOSE_AT_R",
                "PARTIAL_CLOSE_PCT",
                "REVERSAL_EXIT_SCORE",
                "ENABLE_LONGS",
                "ENABLE_SHORTS",
            ),
        ),
        ("Runtime", ("LEGACY_ENV_PATH", "DATA_DIR", "DASHBOARD_PORT")),
        (
            "Telegram",
            (
                "TELEGRAM_API_ID",
                "TELEGRAM_API_HASH",
                "TELEGRAM_PHONE",
                "TELEGRAM_2FA_PASSWORD",
                "TELEGRAM_SESSION_NAME",
                "WATCH_CHANNELS",
                "ALLOWED_SENDER_IDS",
                "ALLOWED_POST_AUTHORS",
            ),
        ),
        (
            "Signal bot",
            (
                "SIGNAL_FIXED_LOT",
                "SIGNAL_SL_ATR_MULT",
                "SIGNAL_SL_MIN_POINTS",
                "SIGNAL_TP_TARGET_INDEX",
                "SIGNAL_PROTECT_TP1_ENABLED",
                "SIGNAL_PROTECT_TP1_TRIGGER_PCT",
            ),
        ),
    ]

    lines: list[str] = []
    written: set[str] = set()
    for title, keys in groups:
        lines.append(f"# {title}")
        for key in keys:
            lines.append(f"{key}={values.get(key, DEFAULT_ENV.get(key, ''))}")
            written.add(key)
        lines.append("")
    for key in sorted(set(values) - written):
        lines.append(f"{key}={values[key]}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _ask(prompt: str, current: str = "", *, required: bool = False, secret: bool = False) -> str:
    while True:
        label = f"{prompt}"
        if current:
            label += f" [{current}]"
        label += ": "
        value = getpass.getpass(label) if secret else input(label)
        value = value.strip()
        if not value and current:
            return current
        if value or not required:
            return value
        print("To pole jest wymagane.")


def ensure_env_configured(force: bool = False) -> Path:
    base_dir = Path(__file__).resolve().parent.parent
    env_path = base_dir / ".env"
    values = {**DEFAULT_ENV, **_read_env(env_path)}
    missing = [key for key in REQUIRED_KEYS if not values.get(key, "").strip()]
    if env_path.exists() and not force and not missing:
        return env_path

    print("")
    print("Pierwsza konfiguracja XAU Autonomous Bot")
    print("Dane zostana zapisane lokalnie w pliku .env. Nie wrzucaj tego pliku do GitHuba.")
    print("")

    values["MT5_LOGIN"] = _ask("MT5 login", values.get("MT5_LOGIN", ""), required=True)
    values["MT5_PASSWORD"] = _ask("MT5 password", values.get("MT5_PASSWORD", ""), required=True, secret=True)
    values["MT5_SERVER"] = _ask("MT5 server, np. ICMarketsSC-Demo", values.get("MT5_SERVER", ""), required=True)
    values["MT5_PATH"] = _ask("Opcjonalna sciezka terminal64.exe MT5", values.get("MT5_PATH", ""))

    lot = _ask("Lot dla sygnalow Telegram", values.get("SIGNAL_FIXED_LOT", "0.01"), required=True)
    values["SIGNAL_FIXED_LOT"] = lot
    values["MIN_LOT"] = _ask("Minimalny lot", values.get("MIN_LOT", "0.01"), required=True)
    values["MAX_LOT"] = _ask("Maksymalny lot", values.get("MAX_LOT", lot), required=True)
    values["MAX_TOTAL_LOT"] = _ask("Maksymalny laczny lot", values.get("MAX_TOTAL_LOT", lot), required=True)

    print("")
    print("Telegram API_ID i API_HASH pobierzesz z https://my.telegram.org/apps")
    values["TELEGRAM_API_ID"] = _ask("Telegram API ID", values.get("TELEGRAM_API_ID", ""), required=True)
    values["TELEGRAM_API_HASH"] = _ask("Telegram API HASH", values.get("TELEGRAM_API_HASH", ""), required=True, secret=True)
    values["TELEGRAM_PHONE"] = _ask("Telefon Telegram z numerem kierunkowym", values.get("TELEGRAM_PHONE", ""), required=True)
    values["TELEGRAM_2FA_PASSWORD"] = _ask("Telegram 2FA password, jezeli masz", values.get("TELEGRAM_2FA_PASSWORD", ""), secret=True)

    print("")
    print("Domyslne kanaly sa juz wpisane. Mozesz dopisac kolejne po przecinku.")
    values["WATCH_CHANNELS"] = _ask("Kanaly Telegram", values.get("WATCH_CHANNELS", DEFAULT_WATCH_CHANNELS), required=True)

    values["LEGACY_ENV_PATH"] = ""
    _write_env(env_path, values)
    (base_dir / values.get("DATA_DIR", "data")).mkdir(parents=True, exist_ok=True)
    (base_dir / "logs").mkdir(parents=True, exist_ok=True)
    print("")
    print(f"Gotowe. Konfiguracja zapisana: {env_path}")
    print("")
    return env_path


if __name__ == "__main__":
    ensure_env_configured(force=True)
