from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value))
    except Exception:
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(str(value))
    except Exception:
        return default


@dataclass
class Settings:
    base_dir: Path
    data_dir: Path
    legacy_env_path: Path
    mt5_login: int
    mt5_password: str
    mt5_server: str
    mt5_path: str
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    telegram_2fa_password: str
    telegram_session_name: str
    telegram_watch_channels: tuple[str, ...]
    telegram_trade_channels: tuple[str, ...]
    channel_lot_sizes: dict[str, float]
    telegram_allowed_sender_ids: tuple[int, ...]
    telegram_allowed_post_authors: tuple[str, ...]
    signal_fixed_lot: float
    signal_lot_mode: str
    signal_dynamic_lot_enabled: bool
    signal_dynamic_lot_step_usd: float
    signal_dynamic_lot_add: float
    signal_dynamic_lot_max: float
    signal_funded_account_balance: float
    signal_funded_safe_signal_lot_per_100k: float
    signal_funded_skip_below_min: bool
    signal_entry_mode: str
    signal_sl_atr_mult: float
    signal_sl_min_points: int
    signal_tp_target_index: int
    signal_pending_expiry_minutes: float
    signal_protect_tp1_enabled: bool
    signal_protect_tp1_trigger_pct: float
    signal_session_net_profit_stop_usd: float
    signal_session_net_loss_stop_usd: float
    signal_adaptive_learning_enabled: bool
    xau_scalp_enabled: bool
    xau_scalp_magic: int
    xau_scalp_lot: float
    xau_scalp_dynamic_lot_enabled: bool
    xau_scalp_dynamic_step_usd: float
    xau_scalp_dynamic_lot_add: float
    xau_scalp_dynamic_max_lot: float
    xau_scalp_max_positions: int
    xau_scalp_max_spread_points: int
    xau_scalp_sl_usd: float
    xau_scalp_tp1_usd: float
    xau_scalp_tp2_usd: float
    xau_scalp_be_trigger_usd: float
    xau_scalp_be_buffer_usd: float
    xau_scalp_loop_seconds: float
    xau_scalp_cooldown_seconds: int
    xau_scalp_daily_dd_pct: float
    xau_scalp_total_dd_pct: float
    xau_contr_scalp_enabled: bool
    xau_contr_scalp_magic: int
    xau_contr_scalp_mode: str
    xau_contr_scalp_lot_factor: float
    xau_contr_scalp_max_positions: int
    xau_contr_scalp_max_spread_points: int
    xau_contr_scalp_tp_usd: float
    xau_contr_scalp_sl_usd: float
    xau_contr_scalp_timeout_minutes: int
    xau_contr_scalp_loop_seconds: float
    xau_contr_scalp_daily_dd_pct: float
    xau_contr_scalp_total_dd_pct: float
    btc_scalp_enabled: bool
    btc_scalp_symbol: str
    btc_scalp_magic: int
    btc_scalp_lot: float
    btc_scalp_max_positions: int
    btc_scalp_max_spread_points: int
    btc_scalp_sl_atr_mult: float
    btc_scalp_tp1_atr_mult: float
    btc_scalp_tp2_atr_mult: float
    btc_scalp_be_trigger_atr_mult: float
    btc_scalp_be_buffer_usd: float
    btc_scalp_loop_seconds: float
    btc_scalp_cooldown_seconds: int
    btc_scalp_daily_dd_pct: float
    btc_scalp_total_dd_pct: float
    symbol: str
    regime_timeframe: str
    entry_timeframe: str
    history_bars: int
    loop_seconds: float
    magic: int
    deviation: int
    risk_per_trade_pct: float
    min_lot: float
    max_lot: float
    max_total_lot: float
    max_open_positions: int
    max_daily_drawdown_pct: float
    max_total_drawdown_pct: float
    max_spread_points: int
    min_signal_score: float
    cooldown_bars: int
    trend_sl_atr: float
    trend_tp_rr: float
    breakout_sl_atr: float
    breakout_tp_rr: float
    meanrev_sl_atr: float
    meanrev_tp_rr: float
    breakeven_at_r: float
    trail_start_r: float
    trail_atr_mult: float
    partial_close_at_r: float
    partial_close_pct: float
    reversal_exit_score: float
    enable_longs: bool
    enable_shorts: bool
    dashboard_port: int


def load_settings() -> Settings:
    base_dir = Path(__file__).resolve().parent.parent
    legacy_env_raw = os.getenv("LEGACY_ENV_PATH", "")
    legacy_env = Path(legacy_env_raw) if legacy_env_raw else base_dir / ".env.disabled"
    local_env = base_dir / ".env"

    legacy_values = dotenv_values(legacy_env) if legacy_env.exists() else {}
    local_values = dotenv_values(local_env) if local_env.exists() else {}

    def pick(key: str, default: str, *, allow_legacy: bool = False) -> str:
        if key in os.environ:
            return str(os.environ[key])
        if key in local_values and local_values[key] is not None:
            return str(local_values[key])
        if allow_legacy and key in legacy_values and legacy_values[key] is not None:
            return str(legacy_values[key])
        return default

    def pick_csv(key: str, default: str = "", *, allow_legacy: bool = False) -> tuple[str, ...]:
        raw = pick(key, default, allow_legacy=allow_legacy)
        tokens: list[str] = []
        for item in raw.split(","):
            token = item.strip()
            if not token:
                continue
            lowered = token.lower()
            for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/", "t.me/", "telegram.me/"):
                if lowered.startswith(prefix):
                    token = token[len(prefix) :].strip("/")
                    break
            if "/" in token and not token.startswith("+"):
                token = token.split("/", 1)[0].strip()
            tokens.append(token)
        return tuple(tokens)

    def pick_int_csv(key: str, default: str = "", *, allow_legacy: bool = False) -> tuple[int, ...]:
        values: list[int] = []
        for item in pick_csv(key, default, allow_legacy=allow_legacy):
            try:
                values.append(int(item))
            except Exception:
                continue
        return tuple(values)

    def pick_channel_lot_sizes(key: str = "CHANNEL_LOT_SIZES") -> dict[str, float]:
        try:
            payload = json.loads(pick(key, "{}"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, float] = {}
        for channel, raw_lot in payload.items():
            name = str(channel or "").strip()
            try:
                lot = float(raw_lot)
            except Exception:
                continue
            if name and lot > 0:
                result[name] = lot
        return result

    data_dir = base_dir / pick("DATA_DIR", "data")
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        base_dir=base_dir,
        data_dir=data_dir,
        legacy_env_path=legacy_env,
        mt5_login=_int(pick("MT5_LOGIN", "0", allow_legacy=True), 0),
        mt5_password=pick("MT5_PASSWORD", "", allow_legacy=True),
        mt5_server=pick("MT5_SERVER", "", allow_legacy=True),
        mt5_path=pick("MT5_PATH", "", allow_legacy=True),
        telegram_api_id=_int(pick("TELEGRAM_API_ID", "0"), 0),
        telegram_api_hash=pick("TELEGRAM_API_HASH", ""),
        telegram_phone=pick("TELEGRAM_PHONE", ""),
        telegram_2fa_password=pick("TELEGRAM_2FA_PASSWORD", ""),
        telegram_session_name=pick("TELEGRAM_SESSION_NAME", "xauusd_signal_bot"),
        telegram_watch_channels=pick_csv("WATCH_CHANNELS", pick("WATCH_CHANNELS", "")),
        telegram_trade_channels=pick_csv("TRADE_CHANNELS", pick("WATCH_CHANNELS", "")),
        channel_lot_sizes=pick_channel_lot_sizes(),
        telegram_allowed_sender_ids=pick_int_csv("ALLOWED_SENDER_IDS"),
        telegram_allowed_post_authors=pick_csv("ALLOWED_POST_AUTHORS"),
        signal_fixed_lot=_float(pick("SIGNAL_FIXED_LOT", "0.1"), 0.1),
        signal_lot_mode=pick("SIGNAL_LOT_MODE", "fixed").strip().lower(),
        signal_dynamic_lot_enabled=_bool(pick("SIGNAL_DYNAMIC_LOT_ENABLED", "false"), False),
        signal_dynamic_lot_step_usd=max(1.0, _float(pick("SIGNAL_DYNAMIC_LOT_STEP_USD", "300"), 300.0)),
        signal_dynamic_lot_add=max(0.0, _float(pick("SIGNAL_DYNAMIC_LOT_ADD", "0.01"), 0.01)),
        signal_dynamic_lot_max=max(0.01, _float(pick("SIGNAL_DYNAMIC_LOT_MAX", pick("MAX_LOT", "0.1")), 0.1)),
        signal_funded_account_balance=max(0.0, _float(pick("SIGNAL_FUNDED_ACCOUNT_BALANCE", "0"), 0.0)),
        signal_funded_safe_signal_lot_per_100k=max(0.0, _float(pick("SIGNAL_FUNDED_SAFE_SIGNAL_LOT_PER_100K", "0.60"), 0.60)),
        signal_funded_skip_below_min=_bool(pick("SIGNAL_FUNDED_SKIP_BELOW_MIN", "true"), True),
        signal_entry_mode=pick("SIGNAL_ENTRY_MODE", "single").strip().lower(),
        signal_sl_atr_mult=_float(pick("SIGNAL_SL_ATR_MULT", "1.5"), 1.5),
        signal_sl_min_points=_int(pick("SIGNAL_SL_MIN_POINTS", "50"), 50),
        signal_tp_target_index=max(1, _int(pick("SIGNAL_TP_TARGET_INDEX", "2"), 2)),
        signal_pending_expiry_minutes=max(1.0, _float(pick("SIGNAL_PENDING_EXPIRY_MINUTES", "180"), 180.0)),
        signal_protect_tp1_enabled=_bool(pick("SIGNAL_PROTECT_TP1_ENABLED", "true"), True),
        signal_protect_tp1_trigger_pct=max(0.0, _float(pick("SIGNAL_PROTECT_TP1_TRIGGER_PCT", "0.20"), 0.20)),
        signal_session_net_profit_stop_usd=max(0.0, _float(pick("SIGNAL_SESSION_NET_PROFIT_STOP_USD", "0"), 0.0)),
        signal_session_net_loss_stop_usd=abs(_float(pick("SIGNAL_SESSION_NET_LOSS_STOP_USD", "0"), 0.0)),
        signal_adaptive_learning_enabled=_bool(pick("SIGNAL_ADAPTIVE_LEARNING_ENABLED", "false"), False),
        xau_scalp_enabled=_bool(pick("XAU_SCALP_ENABLED", "true"), True),
        xau_scalp_magic=_int(pick("XAU_SCALP_MAGIC", "994242"), 994242),
        xau_scalp_lot=max(0.01, _float(pick("XAU_SCALP_LOT", "0.01"), 0.01)),
        xau_scalp_dynamic_lot_enabled=_bool(pick("XAU_SCALP_DYNAMIC_LOT_ENABLED", "false"), False),
        xau_scalp_dynamic_step_usd=max(1.0, _float(pick("XAU_SCALP_DYNAMIC_STEP_USD", "1000"), 1000.0)),
        xau_scalp_dynamic_lot_add=max(0.0, _float(pick("XAU_SCALP_DYNAMIC_LOT_ADD", "0.01"), 0.01)),
        xau_scalp_dynamic_max_lot=max(0.01, _float(pick("XAU_SCALP_DYNAMIC_MAX_LOT", pick("MAX_LOT", "1.0")), 1.0)),
        # Zero disables the position-count ceiling. Broker margin still applies.
        xau_scalp_max_positions=max(0, _int(pick("XAU_SCALP_MAX_POSITIONS", "3"), 3)),
        xau_scalp_max_spread_points=max(1, _int(pick("XAU_SCALP_MAX_SPREAD_POINTS", "80"), 80)),
        xau_scalp_sl_usd=max(1.0, _float(pick("XAU_SCALP_SL_USD", "6.0"), 6.0)),
        xau_scalp_tp1_usd=max(0.5, _float(pick("XAU_SCALP_TP1_USD", "2.0"), 2.0)),
        xau_scalp_tp2_usd=max(1.0, _float(pick("XAU_SCALP_TP2_USD", "4.0"), 4.0)),
        xau_scalp_be_trigger_usd=max(0.2, _float(pick("XAU_SCALP_BE_TRIGGER_USD", pick("XAU_SCALP_TP1_USD", "2.0")), 2.0)),
        xau_scalp_be_buffer_usd=max(0.0, _float(pick("XAU_SCALP_BE_BUFFER_USD", "0.10"), 0.10)),
        xau_scalp_loop_seconds=max(1.0, _float(pick("XAU_SCALP_LOOP_SECONDS", "1.0"), 1.0)),
        xau_scalp_cooldown_seconds=max(0, _int(pick("XAU_SCALP_COOLDOWN_SECONDS", "600"), 600)),
        xau_scalp_daily_dd_pct=max(0.1, _float(pick("XAU_SCALP_DAILY_DD_PCT", "4.0"), 4.0)),
        xau_scalp_total_dd_pct=max(0.1, _float(pick("XAU_SCALP_TOTAL_DD_PCT", "9.0"), 9.0)),
        xau_contr_scalp_enabled=_bool(pick("XAU_CONTR_SCALP_ENABLED", "false"), False),
        xau_contr_scalp_magic=_int(pick("XAU_CONTR_SCALP_MAGIC", "994244"), 994244),
        xau_contr_scalp_mode=pick("XAU_CONTR_SCALP_MODE", "normal").strip().lower(),
        xau_contr_scalp_lot_factor=max(0.1, _float(pick("XAU_CONTR_SCALP_LOT_FACTOR", "0.75"), 0.75)),
        xau_contr_scalp_max_positions=max(1, _int(pick("XAU_CONTR_SCALP_MAX_POSITIONS", "3"), 3)),
        xau_contr_scalp_max_spread_points=max(1, _int(pick("XAU_CONTR_SCALP_MAX_SPREAD_POINTS", "70"), 70)),
        xau_contr_scalp_tp_usd=max(0.5, _float(pick("XAU_CONTR_SCALP_TP_USD", "6.0"), 6.0)),
        xau_contr_scalp_sl_usd=max(0.5, _float(pick("XAU_CONTR_SCALP_SL_USD", "6.0"), 6.0)),
        xau_contr_scalp_timeout_minutes=max(1, _int(pick("XAU_CONTR_SCALP_TIMEOUT_MINUTES", "15"), 15)),
        xau_contr_scalp_loop_seconds=max(1.0, _float(pick("XAU_CONTR_SCALP_LOOP_SECONDS", "1.0"), 1.0)),
        xau_contr_scalp_daily_dd_pct=max(0.1, _float(pick("XAU_CONTR_SCALP_DAILY_DD_PCT", "8.0"), 8.0)),
        xau_contr_scalp_total_dd_pct=max(0.1, _float(pick("XAU_CONTR_SCALP_TOTAL_DD_PCT", "20.0"), 20.0)),
        btc_scalp_enabled=_bool(pick("BTC_SCALP_ENABLED", "true"), True),
        btc_scalp_symbol=pick("BTC_SCALP_SYMBOL", "BTCUSD").upper(),
        btc_scalp_magic=_int(pick("BTC_SCALP_MAGIC", "994243"), 994243),
        btc_scalp_lot=max(0.01, _float(pick("BTC_SCALP_LOT", "0.03"), 0.03)),
        btc_scalp_max_positions=max(1, _int(pick("BTC_SCALP_MAX_POSITIONS", "3"), 3)),
        btc_scalp_max_spread_points=max(1, _int(pick("BTC_SCALP_MAX_SPREAD_POINTS", "3000"), 3000)),
        btc_scalp_sl_atr_mult=max(0.2, _float(pick("BTC_SCALP_SL_ATR_MULT", "2.0"), 2.0)),
        btc_scalp_tp1_atr_mult=max(0.1, _float(pick("BTC_SCALP_TP1_ATR_MULT", "0.8"), 0.8)),
        btc_scalp_tp2_atr_mult=max(0.2, _float(pick("BTC_SCALP_TP2_ATR_MULT", "1.4"), 1.4)),
        btc_scalp_be_trigger_atr_mult=max(0.1, _float(pick("BTC_SCALP_BE_TRIGGER_ATR_MULT", "0.6"), 0.6)),
        btc_scalp_be_buffer_usd=max(0.0, _float(pick("BTC_SCALP_BE_BUFFER_USD", "5.0"), 5.0)),
        btc_scalp_loop_seconds=max(1.0, _float(pick("BTC_SCALP_LOOP_SECONDS", "1.0"), 1.0)),
        btc_scalp_cooldown_seconds=max(30, _int(pick("BTC_SCALP_COOLDOWN_SECONDS", "900"), 900)),
        btc_scalp_daily_dd_pct=max(0.1, _float(pick("BTC_SCALP_DAILY_DD_PCT", "999"), 999.0)),
        btc_scalp_total_dd_pct=max(0.1, _float(pick("BTC_SCALP_TOTAL_DD_PCT", "999"), 999.0)),
        symbol=pick("MT5_SYMBOL", pick("SYMBOL", "XAUUSD")).upper(),
        regime_timeframe=pick("REGIME_TIMEFRAME", "M15").upper(),
        entry_timeframe=pick("ENTRY_TIMEFRAME", "M5").upper(),
        history_bars=_int(pick("HISTORY_BARS", "800"), 800),
        loop_seconds=_float(pick("LOOP_SECONDS", "5.0"), 5.0),
        magic=_int(pick("MAGIC", "994241"), 994241),
        deviation=_int(pick("DEVIATION", "50"), 50),
        risk_per_trade_pct=_float(pick("RISK_PER_TRADE_PCT", "0.40"), 0.40),
        min_lot=_float(pick("MIN_LOT", "0.01"), 0.01),
        max_lot=_float(pick("MAX_LOT", "0.08"), 0.08),
        max_total_lot=_float(pick("MAX_TOTAL_LOT", "0.08"), 0.08),
        max_open_positions=_int(pick("MAX_OPEN_POSITIONS", "1"), 1),
        max_daily_drawdown_pct=_float(pick("MAX_DAILY_DRAWDOWN_PCT", "2.5"), 2.5),
        max_total_drawdown_pct=_float(pick("MAX_TOTAL_DRAWDOWN_PCT", "5.0"), 5.0),
        max_spread_points=_int(pick("MAX_SPREAD_POINTS", "120"), 120),
        min_signal_score=_float(pick("MIN_SIGNAL_SCORE", "60.0"), 60.0),
        cooldown_bars=_int(pick("COOLDOWN_BARS", "3"), 3),
        trend_sl_atr=_float(pick("TREND_SL_ATR", "1.5"), 1.5),
        trend_tp_rr=_float(pick("TREND_TP_RR", "2.4"), 2.4),
        breakout_sl_atr=_float(pick("BREAKOUT_SL_ATR", "1.3"), 1.3),
        breakout_tp_rr=_float(pick("BREAKOUT_TP_RR", "2.8"), 2.8),
        meanrev_sl_atr=_float(pick("MEANREV_SL_ATR", "1.1"), 1.1),
        meanrev_tp_rr=_float(pick("MEANREV_TP_RR", "1.8"), 1.8),
        breakeven_at_r=_float(pick("BREAKEVEN_AT_R", "1.0"), 1.0),
        trail_start_r=_float(pick("TRAIL_START_R", "1.4"), 1.4),
        trail_atr_mult=_float(pick("TRAIL_ATR_MULT", "1.1"), 1.1),
        partial_close_at_r=_float(pick("PARTIAL_CLOSE_AT_R", "1.6"), 1.6),
        partial_close_pct=_float(pick("PARTIAL_CLOSE_PCT", "0.35"), 0.35),
        reversal_exit_score=_float(pick("REVERSAL_EXIT_SCORE", "72.0"), 72.0),
        enable_longs=_bool(pick("ENABLE_LONGS", "true"), True),
        enable_shorts=_bool(pick("ENABLE_SHORTS", "true"), True),
        dashboard_port=_int(pick("DASHBOARD_PORT", "8787"), 8787),
    )
