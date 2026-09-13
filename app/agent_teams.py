from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from .indicators import enrich
from .mt5_gateway import (
    Mt5Credentials,
    account_info,
    calc_loss_per_lot,
    close_position,
    connect,
    ensure_symbol,
    get_rates_df,
    get_tick,
    mt5,
    modify_position,
    positions_by_magic,
    send_market_order,
    shutdown,
    symbol_info,
)
from .risk import normalize_volume
from .strategy_lab import LAB_STRATEGY_DESCRIPTIONS, LAB_STRATEGY_WEIGHTS, indicator_strategy_votes


STATUS_FILE = "agent_teams_status.json"
EVENTS_FILE = "agent_teams_events.jsonl"
TRADES_FILE = "agent_teams_trades.csv"
RUNTIME_FILE = "agent_teams_runtime.json"
LEARNING_FILE = "agent_teams_learning.json"
SCOUT_FILE = "agent_teams_scout.json"
CONTROL_FILE = "agent_teams_control.json"


TEAM_SPECS = [
    ("xau", "XAU Agent Team", "XAUUSD", "XAU AGENT TEAM"),
    ("nasdaq", "Nasdaq Agent Team", "NAS100", "NAS AGENT TEAM"),
    ("us30", "US30 Agent Team", "DJ30", "US30 AGENT TEAM"),
    ("sp500", "SP500 Agent Team", "SP500", "SP500 AGENT TEAM"),
    ("eurusd", "EURUSD Agent Team", "EURUSD", "FX EURUSD TEAM"),
    ("gbpusd", "GBPUSD Agent Team", "GBPUSD", "FX GBPUSD TEAM"),
    ("usdjpy", "USDJPY Agent Team", "USDJPY", "FX USDJPY TEAM"),
    ("audusd", "AUDUSD Agent Team", "AUDUSD", "FX AUDUSD TEAM"),
    ("usdcad", "USDCAD Agent Team", "USDCAD", "FX USDCAD TEAM"),
    ("btcusd", "Bitcoin Agent Team", "BTCUSD", "BTC AGENT TEAM"),
    ("ethusd", "Ethereum Agent Team", "ETHUSD", "ETH AGENT TEAM"),
    ("usdchf", "USDCHF Agent Team", "USDCHF", "FX USDCHF TEAM"),
    ("nzdusd", "NZDUSD Agent Team", "NZDUSD", "FX NZDUSD TEAM"),
    ("eurjpy", "EURJPY Agent Team", "EURJPY", "FX EURJPY TEAM"),
    ("gbpjpy", "GBPJPY Agent Team", "GBPJPY", "FX GBPJPY TEAM"),
    ("eurgbp", "EURGBP Agent Team", "EURGBP", "FX EURGBP TEAM"),
    ("audjpy", "AUDJPY Agent Team", "AUDJPY", "FX AUDJPY TEAM"),
    ("nzdjpy", "NZDJPY Agent Team", "NZDJPY", "FX NZDJPY TEAM"),
    ("cadjpy", "CADJPY Agent Team", "CADJPY", "FX CADJPY TEAM"),
    ("chfjpy", "CHFJPY Agent Team", "CHFJPY", "FX CHFJPY TEAM"),
    ("euraud", "EURAUD Agent Team", "EURAUD", "FX EURAUD TEAM"),
    ("eurcad", "EURCAD Agent Team", "EURCAD", "FX EURCAD TEAM"),
    ("eurchf", "EURCHF Agent Team", "EURCHF", "FX EURCHF TEAM"),
    ("eurnzd", "EURNZD Agent Team", "EURNZD", "FX EURNZD TEAM"),
    ("gbpaud", "GBPAUD Agent Team", "GBPAUD", "FX GBPAUD TEAM"),
    ("gbpcad", "GBPCAD Agent Team", "GBPCAD", "FX GBPCAD TEAM"),
    ("gbpchf", "GBPCHF Agent Team", "GBPCHF", "FX GBPCHF TEAM"),
    ("gbpnzd", "GBPNZD Agent Team", "GBPNZD", "FX GBPNZD TEAM"),
    ("audcad", "AUDCAD Agent Team", "AUDCAD", "FX AUDCAD TEAM"),
    ("audchf", "AUDCHF Agent Team", "AUDCHF", "FX AUDCHF TEAM"),
    ("audnzd", "AUDNZD Agent Team", "AUDNZD", "FX AUDNZD TEAM"),
    ("nzdcad", "NZDCAD Agent Team", "NZDCAD", "FX NZDCAD TEAM"),
    ("nzdchf", "NZDCHF Agent Team", "NZDCHF", "FX NZDCHF TEAM"),
    ("cadchf", "CADCHF Agent Team", "CADCHF", "FX CADCHF TEAM"),
    ("xagusd", "Silver Agent Team", "XAGUSD", "XAG AGENT TEAM"),
    ("copper", "Copper Agent Team", "COPPER", "COPPER AGENT TEAM"),
    ("ger40", "Germany 40 Agent Team", "GER40", "GER40 AGENT TEAM"),
    ("uk100", "UK 100 Agent Team", "UK100", "UK100 AGENT TEAM"),
    ("fra40", "France 40 Agent Team", "FRA40", "FRA40 AGENT TEAM"),
    ("eu50", "Euro 50 Agent Team", "EU50", "EU50 AGENT TEAM"),
    ("hk50", "Hong Kong 50 Agent Team", "HK50", "HK50 AGENT TEAM"),
    ("jpn225", "Japan 225 Agent Team", "JPN225", "JPN225 AGENT TEAM"),
    ("us2000", "US 2000 Agent Team", "US2000", "US2000 AGENT TEAM"),
    ("nflx", "Netflix Agent Team", "NFLX", "TECH NFLX TEAM"),
    ("asml", "ASML Agent Team", "ASML", "TECH ASML TEAM"),
    ("tsm", "TSMC Agent Team", "TSM", "TECH TSM TEAM"),
    ("aapl", "Apple Agent Team", "AAPL", "TECH AAPL TEAM"),
    ("msft", "Microsoft Agent Team", "MSFT", "TECH MSFT TEAM"),
    ("nvda", "Nvidia Agent Team", "NVDAUSD", "TECH NVDA TEAM"),
    ("amzn", "Amazon Agent Team", "AMZNUSD", "TECH AMZN TEAM"),
    ("goog", "Alphabet Agent Team", "GOOG", "TECH GOOG TEAM"),
    ("meta", "Meta Agent Team", "META", "TECH META TEAM"),
    ("tsla", "Tesla Agent Team", "TSLA", "TECH TSLA TEAM"),
    ("avgo", "Broadcom Agent Team", "AVGO", "TECH AVGO TEAM"),
    ("amd", "AMD Agent Team", "AMD", "TECH AMD TEAM"),
    ("orcl", "Oracle Agent Team", "ORCL", "TECH ORCL TEAM"),
]

# Nasdaq is intentionally split into independent views.  The variants share
# the same execution account but do not share a signal decision, which makes
# it possible to measure whether fast entries or higher-timeframe confirmation
# is actually adding value.
NASDAQ_TEAM_SPECS = [
    ("nasdaq_fast", "Nasdaq Fast Team", "NAS100", "NASDAQ FAST TEAM", "nasdaq_fast"),
    ("nasdaq_swing", "Nasdaq Swing Team", "NAS100", "NASDAQ SWING TEAM", "nasdaq_swing"),
    ("nasdaq_macro", "Nasdaq Macro Team", "NAS100", "NASDAQ MACRO TEAM", "nasdaq_macro"),
    ("nasdaq_pending", "Nasdaq Pending Team", "NAS100", "NASDAQ PENDING TEAM", "nasdaq_pending"),
    ("nasdaq_scraper", "Nasdaq Scraper Team", "NAS100", "NASDAQ SCRAPER TEAM", "nasdaq_scraper"),
]

DEDICATED_STRATEGY_TEAM_SPECS = [
    (
        "nas100_dual_thrust",
        "NAS100 Dual Thrust Team",
        "NAS100",
        "NAS100 DUAL THRUST",
        "dedicated:dual_thrust_specialist",
    ),
    (
        "dj30_roc",
        "DJ30 ROC Acceleration Team",
        "DJ30",
        "DJ30 ROC",
        "dedicated:roc_acceleration_specialist",
    ),
]

LONG_TERM_TEAM_SPECS = [
    ("long_xau", "Long Term XAU Brigade", "XAUUSD", "LT XAU BRIGADE"),
    ("long_nasdaq", "Long Term Nasdaq Brigade", "NAS100", "LT NAS BRIGADE"),
    ("long_sp500", "Long Term SP500 Brigade", "SP500", "LT SP500 BRIGADE"),
    ("long_btc", "Long Term Bitcoin Brigade", "BTCUSD", "LT BTC BRIGADE"),
    ("long_eth", "Long Term Ethereum Brigade", "ETHUSD", "LT ETH BRIGADE"),
]


AGENT_DESCRIPTIONS = {
    "trend": "Wyznacza kierunek M5/M15 przez EMA20/EMA50 i sile trendu ADX.",
    "momentum": "Ocenia RSI, tempo M1/M5 i zgodnosc impetu z kierunkiem.",
    "structure": "Czyta lokalne wybicia, wyzsze szczyty/dolki i polozenie ceny.",
    "price_action": "Ocenia korpus, knoty i miejsce zamkniecia w swiecy M1.",
    "volatility": "Pilnuje ATR, spreadu i odrzuca rynek bez jakosci wykonania.",
    "mean_reversion": "Szuka kontrolowanego powrotu od Bollingera i skrajnego RSI.",
    "breakout_specialist": "Specjalista tworzony dla wybic z wolumenem i rosnacym ADX.",
    "pullback_specialist": "Specjalista tworzony dla powrotow do EMA zgodnych z trendem.",
    "liquidity_specialist": "Specjalista tworzony dla falszywych wybic i zebrania plynnosci.",
    "multi_tf_specialist": "Specjalista tworzony dla pelnej zgodnosci M1/M5/M15.",
    "bollinger_rsi_specialist": "Kupuje lub sprzedaje dopiero po powrocie ceny do wnetrza Bollingera z ekstremum RSI.",
    "macd_ema_specialist": "Laczy przeciecie histogramu MACD z kierunkiem EMA na wyzszym interwale.",
    "dual_thrust_specialist": "Szuka potwierdzonego wybicia adaptacyjnego zakresu intraday w obie strony.",
    "squeeze_release_specialist": "Wchodzi po wyjsciu Bollingera z kanalu Keltnera, gdy momentum i wolumen potwierdzaja ruch.",
    "vwap_reclaim_specialist": "Rozgrywa odzyskanie albo odrzucenie dziennego VWAP zgodne z trendem M5.",
    "meta_learner": "Ocenia trafnosc agentow, reguluje ich wagi i awansuje nowych specjalistow.",
    "instrument_scout": "Ocenia katalog MT5, wybiera instrumenty i buduje dla nich testowe zespoly.",
    "trade_auditor": "Ocenia jakosc zakonczonych transakcji i przekazuje wnioski wszystkim ekipom.",
    "conflict_controller": "Wykrywa sprzeczne propozycje i kolizje decyzji miedzy zespolami.",
    "performance_manager": "Porownuje biezaca propozycje z historia wynikow danej ekipy.",
    "chief_coordinator": "Zbiera glosy kierownikow i wydaje ostateczna zgode przed egzekucja.",
    "long_term_trend": "Wyznacza dominujacy trend H4/D1 przez EMA20/EMA50/EMA200.",
    "macro_momentum": "Ocenia momentum H1/H4, RSI oraz sile ADX w dluzszym horyzoncie.",
    "market_regime": "Rozpoznaje trend, konsolidacje i ryzyko zmiany rezimu rynku.",
    "swing_structure": "Analizuje szczyty, dolki i wybicia struktury H4/D1.",
    "long_term_risk": "Sprawdza relacje szerokiego SL/TP do zmiennosci H1/H4.",
    "supervisor": "Laczy wazone glosy, wymaga przewagi i podejmuje jedna decyzje zespolu.",
    "regime_filter": "Odrzuca wejscia, gdy wyzszy interwal jest w konsolidacji lub zmienia kierunek.",
    "execution_guard": "Kontroluje odleglosc wejscia, zmiennosc i powtarzanie sygnalu w tej samej fali.",
    "pending_planner": "Wyznacza spokojne wejscia limit/stop z poziomu struktury H1/H4.",
    "signal_scraper": "Zbiera setupy Nasdaq z wielu interwalow, usuwa duplikaty i porownuje ich jakosc.",
}
AGENT_DESCRIPTIONS.update(LAB_STRATEGY_DESCRIPTIONS)


AGENT_WEIGHTS = {
    "trend": 1.40,
    "momentum": 1.20,
    "structure": 1.20,
    "price_action": 1.00,
    "volatility": 0.80,
    "mean_reversion": 0.70,
    "long_term_trend": 1.50,
    "macro_momentum": 1.20,
    "market_regime": 1.10,
    "swing_structure": 1.30,
    "long_term_risk": 0.90,
    "regime_filter": 1.20,
    "execution_guard": 1.15,
}


CANDIDATE_AGENT_WEIGHTS = {
    "breakout_specialist": 1.00,
    "pullback_specialist": 1.00,
    "liquidity_specialist": 1.00,
    "multi_tf_specialist": 1.10,
    "bollinger_rsi_specialist": 0.95,
    "macd_ema_specialist": 1.05,
    "dual_thrust_specialist": 1.00,
    "squeeze_release_specialist": 1.05,
    "vwap_reclaim_specialist": 1.00,
}
CANDIDATE_AGENT_WEIGHTS.update(LAB_STRATEGY_WEIGHTS)


def _configured_dedicated_strategy_team_specs() -> list[tuple[str, str, str, str, str]]:
    raw = str(os.getenv("AGENT_TEAM_DEDICATED_PAIRS_JSON", "") or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    result: list[tuple[str, str, str, str, str]] = []
    seen: set[str] = set()
    valid_strategies = set(AGENT_WEIGHTS) | set(CANDIDATE_AGENT_WEIGHTS)
    for item in payload:
        if not isinstance(item, dict):
            continue
        key = re.sub(r"[^a-z0-9_]+", "_", str(item.get("key", "")).lower()).strip("_")
        symbol = str(item.get("symbol", "") or "").strip()
        strategy = str(item.get("strategy", "") or "").strip()
        if not key or key in seen or not symbol or strategy not in valid_strategies:
            continue
        name = str(item.get("name", "") or f"{symbol} {strategy}").strip()
        comment = str(item.get("comment", "") or f"MM {symbol} {strategy}")[:31]
        result.append((key, name, symbol, comment, f"dedicated:{strategy}"))
        seen.add(key)
    return result


SCOUT_PRIORITY_SYMBOLS = [
    "DJ30",
    "SP500",
    "EU50",
    "US2000",
    "CHINA50",
    "SPI200",
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "COPPER-C",
    "NG-C",
    "GAS-C",
    "ASML",
    "TSM",
    "NFLX",
    "CRM",
    "IBM",
    "INTEL",
    "JPM",
    "EXXON",
]


SCOUT_ALLOWED_PATHS = (
    "CFDS\\INDICES",
    "CRYPTO CURRENCY",
    "COMMODITIES",
    "STOCKS\\US",
    "STOCKS\\EU",
    "FOREX+\\FOREX MAJOR",
)

SCOUT_MAJOR_CRYPTO = {"BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "LTCUSD", "BCHUSD"}


@dataclass
class TeamSpec:
    key: str
    name: str
    preferred_symbol: str
    comment: str
    symbol: str = ""
    magic: int = 0
    min_volume: float = 0.0
    requested_volume: float = 0.01
    trade_mode: str = "shadow_0.01"
    source: str = "configured"
    recruited_agents: tuple[str, ...] = ()
    scout_score: float = 0.0
    strategy_profile: str = "intraday"


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, str(default)) or str(default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return default


def _open_capacity_available(open_count: int, max_open_per_team: int) -> bool:
    return max_open_per_team <= 0 or open_count < max_open_per_team


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    content = json.dumps(payload, indent=2, ensure_ascii=True)
    temp.write_text(content, encoding="utf-8")
    for attempt in range(8):
        try:
            temp.replace(path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    path.write_text(content, encoding="utf-8")


def _default_scout_state() -> dict:
    return {
        "schema": 1,
        "enabled": True,
        "scan_count": 0,
        "last_scan_utc": None,
        "catalog_count": 0,
        "scanned_count": 0,
        "recruited": [],
        "retired": [],
        "candidates": [],
        "rejected": [],
    }


def _ensure_scout_state(raw: dict) -> dict:
    state = raw if isinstance(raw, dict) else {}
    default = _default_scout_state()
    for key, value in default.items():
        state.setdefault(key, value)
    if not isinstance(state.get("recruited"), list):
        state["recruited"] = []
    if not isinstance(state.get("retired"), list):
        state["retired"] = []
    return state


def _scout_key(symbol: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(symbol).lower()).strip("_")
    return f"scout_{slug[:24]}"


def _scout_agent_pack(metrics: dict) -> list[str]:
    efficiency = float(metrics.get("efficiency", 0.0) or 0.0)
    atr_pct = float(metrics.get("atr_pct", 0.0) or 0.0)
    spread_atr = float(metrics.get("spread_atr", 99.0) or 99.0)
    agents = ["trend", "momentum", "structure", "price_action", "volatility"]
    if efficiency >= 0.28:
        agents.extend(["breakout_specialist", "multi_tf_specialist"])
    else:
        agents.extend(["mean_reversion", "pullback_specialist", "liquidity_specialist"])
    if atr_pct >= 0.002:
        agents.append("liquidity_specialist")
    if spread_atr > 0.18:
        agents.append("volatility")
    return list(dict.fromkeys(agents))


def _score_instrument_frame(frame: pd.DataFrame, spread: float, tick_age_seconds: float) -> dict:
    if tick_age_seconds > 259200:
        return {
            "score": 0.0,
            "reason": "Tick starszy niz 72h",
            "tick_age_seconds": int(tick_age_seconds),
        }
    if frame is None or len(frame) < 120:
        return {"score": 0.0, "reason": "Za malo swiec M1"}
    work = frame.tail(240).copy()
    for column in ("open", "high", "low", "close"):
        work[column] = pd.to_numeric(work[column], errors="coerce")
    work = work.dropna(subset=["open", "high", "low", "close"])
    if len(work) < 120:
        return {"score": 0.0, "reason": "Niepelne dane OHLC"}
    close = work["close"]
    ranges = (work["high"] - work["low"]).abs()
    median_range = float(ranges.median())
    median_price = max(abs(float(close.median())), 0.000001)
    if median_range <= 0.0:
        return {"score": 0.0, "reason": "Brak zmiennosci"}
    path = float(close.diff().abs().dropna().sum())
    net = abs(float(close.iloc[-1]) - float(close.iloc[0]))
    efficiency = net / max(path, 0.000001)
    atr_pct = median_range / median_price
    spread_atr = max(0.0, float(spread)) / median_range
    tick_volume = pd.to_numeric(work.get("tick_volume", pd.Series(dtype=float)), errors="coerce")
    volume_coverage = float((tick_volume.fillna(0) > 0).mean()) if len(tick_volume) else 0.5

    data_score = min(10.0, len(work) / 24.0)
    freshness_score = max(0.0, 15.0 * (1.0 - max(0.0, tick_age_seconds - 180.0) / 259200.0))
    spread_score = max(0.0, 25.0 * (1.0 - spread_atr / 0.45))
    volatility_score = min(20.0, 20.0 * atr_pct / 0.0008)
    if atr_pct > 0.04:
        volatility_score *= max(0.0, 1.0 - (atr_pct - 0.04) / 0.08)
    efficiency_score = min(20.0, 20.0 * efficiency / 0.35)
    volume_score = 10.0 * volume_coverage
    score = max(0.0, min(100.0, data_score + freshness_score + spread_score + volatility_score + efficiency_score + volume_score))
    return {
        "score": round(score, 2),
        "reason": "OK" if score > 0 else "Brak jakosci",
        "bars": int(len(work)),
        "atr_pct": round(atr_pct, 6),
        "spread_atr": round(spread_atr, 4),
        "efficiency": round(efficiency, 4),
        "volume_coverage": round(volume_coverage, 3),
        "tick_age_seconds": int(max(0.0, tick_age_seconds)),
    }


def _scout_catalog(existing_symbols: set[str], max_catalog: int) -> list:
    priority = {name.upper(): index for index, name in enumerate(SCOUT_PRIORITY_SYMBOLS)}
    discovered = []
    for item in mt5.symbols_get() or []:
        name = str(getattr(item, "name", "") or "")
        path = str(getattr(item, "path", "") or "")
        upper_name, upper_path = name.upper(), path.upper()
        if not name or upper_name in existing_symbols:
            continue
        if int(getattr(item, "trade_mode", 0) or 0) != int(getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4)):
            continue
        if not any(token in upper_path for token in SCOUT_ALLOWED_PATHS):
            continue
        if "CRYPTO" in upper_path and upper_name not in SCOUT_MAJOR_CRYPTO:
            continue
        if upper_name.endswith("FT") or "FUTURE" in upper_path:
            continue
        preferred_rank = priority.get(upper_name, 10000)
        category_rank = 0 if "INDICES" in upper_path else 1 if "COMMOD" in upper_path else 2 if "CRYPTO" in upper_path else 3
        discovered.append((preferred_rank, category_rank, upper_name, item))
    discovered.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in discovered[:max_catalog]]


def _scan_instruments(
    scout: dict,
    existing_symbols: set[str],
    max_catalog: int,
    max_recruits: int,
    minimum_score: float,
    scout_magic_base: int,
    events_path: Path,
) -> dict:
    catalog = _scout_catalog(existing_symbols, max_catalog)
    evaluated = []
    rejected = []
    now_epoch = time.time()
    for item in catalog:
        raw_symbol = str(getattr(item, "name", "") or "")
        try:
            symbol = ensure_symbol(raw_symbol)
            tick = get_tick(symbol)
            tick_age = max(0.0, now_epoch - float(getattr(tick, "time", 0) or 0))
            spread = max(0.0, float(tick.ask) - float(tick.bid))
            metrics = _score_instrument_frame(get_rates_df(symbol, "M1", 260), spread, tick_age)
            record = {
                "symbol": symbol,
                "path": str(getattr(item, "path", "") or ""),
                **metrics,
            }
            record["agents"] = _scout_agent_pack(record)
            if float(record["score"]) >= minimum_score:
                evaluated.append(record)
            else:
                rejected.append(record)
        except Exception as exc:
            rejected.append({"symbol": raw_symbol, "score": 0.0, "reason": f"{type(exc).__name__}: {exc}"})
    evaluated.sort(key=lambda item: (-float(item.get("score", 0.0)), str(item.get("symbol", ""))))
    assessments = {
        str(item.get("symbol", "")).upper(): item
        for item in [*evaluated, *rejected]
        if item.get("symbol")
    }
    recruited = []
    retired = list(scout.get("retired", []))
    for recruit in scout.get("recruited", []):
        symbol_key = str(recruit.get("symbol", "")).upper()
        assessment = assessments.get(symbol_key)
        score = float(assessment.get("score", 0.0)) if assessment else 0.0
        weak_scans = 0 if score >= minimum_score else int(recruit.get("weak_scans", 0) or 0) + 1
        recruit["weak_scans"] = weak_scans
        recruit["last_score"] = round(score, 2)
        if assessment and assessment.get("agents"):
            recruit["agents"] = list(assessment["agents"])
            recruit["score"] = assessment["score"]
        has_open_position = False
        try:
            has_open_position = bool(
                positions_by_magic(str(recruit.get("symbol", "")), int(recruit.get("magic", 0) or 0))
            )
        except Exception:
            pass
        if weak_scans >= 3 and not has_open_position:
            retirement = {
                **recruit,
                "retired_at_utc": datetime.now(UTC).isoformat(),
                "reason": "three_scans_below_quality_threshold",
            }
            retired.append(retirement)
            _append_jsonl(events_path, {"type": "instrument_scout_retired", **retirement})
            continue
        recruited.append(recruit)
    recruited_symbols = {str(item.get("symbol", "")).upper() for item in recruited}
    used_magics = {int(item.get("magic", 0) or 0) for item in recruited}
    next_magic = scout_magic_base
    for candidate in evaluated:
        if len(recruited) >= max_recruits:
            break
        if str(candidate["symbol"]).upper() in recruited_symbols:
            continue
        while next_magic in used_magics:
            next_magic += 1
        recruit = {
            "key": _scout_key(candidate["symbol"]),
            "name": f"Scout {candidate['symbol']} Team",
            "symbol": candidate["symbol"],
            "comment": f"SCOUT {candidate['symbol']} TEAM"[:31],
            "magic": next_magic,
            "lot": 0.01,
            "status": "live_demo",
            "score": candidate["score"],
            "agents": candidate["agents"],
            "weak_scans": 0,
            "last_score": candidate["score"],
            "recruited_at_utc": datetime.now(UTC).isoformat(),
        }
        recruited.append(recruit)
        recruited_symbols.add(str(candidate["symbol"]).upper())
        used_magics.add(next_magic)
        _append_jsonl(events_path, {"type": "instrument_scout_recruited", **recruit})
        next_magic += 1
    scout.update(
        {
            "enabled": True,
            "scan_count": int(scout.get("scan_count", 0) or 0) + 1,
            "last_scan_utc": datetime.now(UTC).isoformat(),
            "catalog_count": len(mt5.symbols_get() or []),
            "scanned_count": len(catalog),
            "recruited": recruited,
            "retired": retired[-50:],
            "candidates": evaluated[:20],
            "rejected": sorted(rejected, key=lambda item: -float(item.get("score", 0.0)))[:20],
        }
    )
    return scout


def _resolve_scout_teams(scout: dict, allow_live_demo: bool = False) -> list[TeamSpec]:
    teams = []
    for record in scout.get("recruited", []):
        team = TeamSpec(
            key=str(record["key"]),
            name=str(record["name"]),
            preferred_symbol=str(record["symbol"]),
            comment=str(record["comment"]),
            magic=int(record["magic"]),
            requested_volume=0.01,
            source="instrument_scout",
            recruited_agents=tuple(record.get("agents", [])),
            scout_score=float(record.get("score", 0.0) or 0.0),
        )
        try:
            team.symbol = ensure_symbol(team.preferred_symbol)
            info = symbol_info(team.symbol)
            team.min_volume = float(getattr(info, "volume_min", 0.01) or 0.01)
            if "XAU" not in team.symbol.upper():
                team.requested_volume = team.min_volume
            team.trade_mode = (
                "live_demo"
                if allow_live_demo and team.requested_volume >= team.min_volume
                else f"shadow_{team.requested_volume:.2f}"
            )
        except Exception:
            team.trade_mode = "unavailable"
        teams.append(team)
    return teams


def _default_control_state() -> dict:
    return {
        "schema": 1,
        "enabled": True,
        "message_sequence": 0,
        "messages": [],
        "trade_reviews": [],
        "processed_trade_count": 0,
        "approved_entries": 0,
        "blocked_entries": 0,
        "last_update_utc": None,
    }


def _ensure_control_state(raw: dict) -> dict:
    state = raw if isinstance(raw, dict) else {}
    for key, value in _default_control_state().items():
        state.setdefault(key, value)
    if not isinstance(state.get("messages"), list):
        state["messages"] = []
    if not isinstance(state.get("trade_reviews"), list):
        state["trade_reviews"] = []
    return state


def _publish_control_message(
    control: dict,
    sender: str,
    role: str,
    kind: str,
    message: str,
    team: str = "",
    symbol: str = "",
    payload: dict | None = None,
) -> dict:
    sequence = int(control.get("message_sequence", 0) or 0) + 1
    item = {
        "id": sequence,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "sender": sender,
        "role": role,
        "kind": kind,
        "team": team,
        "symbol": symbol,
        "message": message,
        "payload": payload or {},
    }
    control["message_sequence"] = sequence
    control.setdefault("messages", []).append(item)
    control["messages"] = control["messages"][-300:]
    control["last_update_utc"] = item["timestamp_utc"]
    return item


def _recent_opposite_proposal(
    control: dict,
    team: str,
    symbol: str,
    side: str,
    now: datetime,
    window_seconds: float,
) -> dict | None:
    for item in reversed(control.get("messages", [])):
        if item.get("kind") != "trade_proposal" or str(item.get("team", "")) == team:
            continue
        if str(item.get("symbol", "")).upper() != symbol.upper():
            continue
        proposal_side = str((item.get("payload") or {}).get("side", "")).lower()
        if proposal_side not in {"buy", "sell"} or proposal_side == side:
            continue
        try:
            age = (now - datetime.fromisoformat(str(item["timestamp_utc"]))).total_seconds()
        except Exception:
            continue
        if 0 <= age <= window_seconds:
            return item
    return None


def _control_review(
    team: TeamSpec,
    decision: dict,
    stats: dict,
    control: dict,
    minimum_confidence: float = 0.58,
    conflict_window_seconds: float = 180.0,
    now: datetime | None = None,
) -> dict:
    side = str(decision.get("decision", "hold")).lower()
    if side not in {"buy", "sell"}:
        return {
            "verdict": "not_applicable",
            "approved": False,
            "quality_score": 0.0,
            "votes": [],
            "reason": "Brak propozycji kierunkowej",
        }
    now = now or datetime.now(UTC)
    confidence = float(decision.get("confidence", 0.0) or 0.0)
    margin = float(decision.get("margin", 0.0) or 0.0)
    trades = int(stats.get("trades", 0) or 0)
    win_rate = float(stats.get("win_rate", 0.0) or 0.0)
    pnl = float(stats.get("pnl", 0.0) or 0.0)
    conflict = _recent_opposite_proposal(
        control,
        team.key,
        team.symbol or team.preferred_symbol,
        side,
        now,
        conflict_window_seconds,
    )
    votes = [
        {
            "manager": "trade_auditor",
            "approve": confidence >= minimum_confidence and margin >= 0.16,
            "reason": f"confidence {confidence:.2f}, margin {margin:.2f}",
        },
        {
            "manager": "performance_manager",
            "approve": trades < 5 or win_rate >= 40.0 or pnl >= 0.0,
            "reason": f"historia {trades} trade, WR {win_rate:.1f}%, PnL {pnl:.2f}",
        },
        {
            "manager": "conflict_controller",
            "approve": conflict is None,
            "reason": (
                "brak konfliktu na wspolnej magistrali"
                if conflict is None
                else f"sprzeczna propozycja od {conflict.get('team', '-')}"
            ),
        },
    ]
    approvals = sum(1 for vote in votes if vote["approve"])
    approved = approvals >= 2
    quality_score = (
        min(45.0, confidence * 60.0)
        + min(25.0, margin * 80.0)
        + (20.0 if votes[1]["approve"] else 0.0)
        + (10.0 if votes[2]["approve"] else 0.0)
    )
    rejected_by = [vote["manager"] for vote in votes if not vote["approve"]]
    return {
        "verdict": "approved" if approved else "blocked",
        "approved": approved,
        "quality_score": round(min(100.0, quality_score), 2),
        "votes": votes,
        "reason": (
            f"Rada zatwierdzila {approvals}/3"
            if approved
            else f"Rada zablokowala; sprzeciw: {', '.join(rejected_by)}"
        ),
        "description": AGENT_DESCRIPTIONS["chief_coordinator"],
    }


def _refresh_trade_reviews(control: dict, trades_path: Path) -> bool:
    if not trades_path.exists():
        return False
    with trades_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    processed = max(0, int(control.get("processed_trade_count", 0) or 0))
    if processed > len(rows):
        processed = 0
    changed = False
    for row in rows[processed:]:
        profit = float(row.get("profit", 0.0) or 0.0)
        confidence = float(row.get("confidence", 0.0) or 0.0)
        if profit > 0:
            grade, diagnosis = "GOOD", "Kierunek i egzekucja zakonczyly sie zyskiem"
        elif profit < 0 and confidence >= 0.7:
            grade, diagnosis = "BAD_HIGH_CONFIDENCE", "Silny konsensus byl bledny; obnizyc wage mylacych glosow"
        elif profit < 0:
            grade, diagnosis = "BAD", "Wejscie stratne przy umiarkowanym konsensusie"
        else:
            grade, diagnosis = "FLAT", "Transakcja bez wyniku kierunkowego"
        review = {
            "closed_at_utc": row.get("closed_at_utc"),
            "team": row.get("team"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "profit": round(profit, 2),
            "confidence": round(confidence, 3),
            "grade": grade,
            "diagnosis": diagnosis,
        }
        control.setdefault("trade_reviews", []).append(review)
        _publish_control_message(
            control,
            "trade_auditor",
            "control_team",
            "trade_review",
            f"{grade}: {row.get('team', '-')} {profit:.2f}",
            str(row.get("team", "") or ""),
            str(row.get("symbol", "") or ""),
            review,
        )
        changed = True
    control["trade_reviews"] = control.get("trade_reviews", [])[-300:]
    control["processed_trade_count"] = len(rows)
    return changed


def _control_summary(control: dict) -> dict:
    reviews = list(control.get("trade_reviews", []))
    good = sum(1 for item in reviews if str(item.get("grade", "")).startswith("GOOD"))
    bad = sum(1 for item in reviews if str(item.get("grade", "")).startswith("BAD"))
    return {
        "enabled": bool(control.get("enabled", True)),
        "description": AGENT_DESCRIPTIONS["chief_coordinator"],
        "members": [
            {"agent": name, "description": AGENT_DESCRIPTIONS[name]}
            for name in ("trade_auditor", "conflict_controller", "performance_manager", "chief_coordinator")
        ],
        "approved_entries": int(control.get("approved_entries", 0) or 0),
        "blocked_entries": int(control.get("blocked_entries", 0) or 0),
        "reviewed_trades": len(reviews),
        "good_trades": good,
        "bad_trades": bad,
        "last_update_utc": control.get("last_update_utc"),
        "messages": list(control.get("messages", []))[-80:],
        "trade_reviews": reviews[-80:],
    }


def _append_jsonl(path: Path, payload: dict) -> None:
    event = {"timestamp_utc": datetime.now(UTC).isoformat(), **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")


def _ensure_trades(path: Path) -> None:
    if path.exists():
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "closed_at_utc",
                "team",
                "symbol",
                "mode",
                "ticket",
                "side",
                "volume",
                "entry",
                "exit",
                "profit",
                "reason",
                "confidence",
            ],
        )
        writer.writeheader()


def _append_trade(path: Path, payload: dict) -> None:
    _ensure_trades(path)
    row = {"closed_at_utc": datetime.now(UTC).isoformat(), **payload}
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        writer.writerow(row)


def _round_price(symbol: str, value: float) -> float:
    info = symbol_info(symbol)
    return round(float(value), int(getattr(info, "digits", 2) or 2))


def _side_price(symbol: str, side: str) -> float:
    tick = get_tick(symbol)
    return float(tick.ask if side == "buy" else tick.bid)


def _vote(name: str, side: str, confidence: float, reason: str, status: str = "active") -> dict:
    return {
        "agent": name,
        "side": side,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "reason": reason,
        "description": AGENT_DESCRIPTIONS[name],
        "status": status,
    }


def _agent_votes(m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame, spread: float) -> list[dict]:
    one = m1.iloc[-2]
    prev_one = m1.iloc[-3]
    five = m5.iloc[-2]
    prev_five = m5.iloc[-3]
    fifteen = m15.iloc[-2]
    atr1 = max(float(one["atr14"]), 0.00001)
    atr5 = max(float(five["atr14"]), atr1)
    close = float(one["close"])
    body = close - float(one["open"])
    progress1 = close - float(prev_one["close"])
    progress5 = float(five["close"]) - float(prev_five["close"])

    trend_buy = float(fifteen["ema20"]) > float(fifteen["ema50"]) and float(five["ema20"]) > float(five["ema50"])
    trend_sell = float(fifteen["ema20"]) < float(fifteen["ema50"]) and float(five["ema20"]) < float(five["ema50"])
    adx_value = float(five["adx14"])
    if trend_buy or trend_sell:
        trend_side = "buy" if trend_buy else "sell"
        trend_conf = min(0.98, 0.55 + max(0.0, adx_value - 12.0) / 60.0)
        trend_reason = f"EMA M5/M15 zgodne, ADX {adx_value:.1f}"
    else:
        trend_side, trend_conf, trend_reason = "hold", 0.45, "EMA M5 i M15 bez zgodnego kierunku"

    rsi1 = float(one["rsi14"])
    momentum_value = (progress1 / atr1) * 0.55 + (progress5 / atr5) * 0.45
    if momentum_value > 0.18 and rsi1 >= 51:
        momentum_side = "buy"
    elif momentum_value < -0.18 and rsi1 <= 49:
        momentum_side = "sell"
    else:
        momentum_side = "hold"
    momentum_conf = min(0.95, 0.45 + abs(momentum_value) * 0.28)

    recent = m1.iloc[-14:-2]
    prior_high = float(recent["high"].max())
    prior_low = float(recent["low"].min())
    position = (close - prior_low) / max(prior_high - prior_low, 0.00001)
    if close >= prior_high or position >= 0.72:
        structure_side = "buy"
    elif close <= prior_low or position <= 0.28:
        structure_side = "sell"
    else:
        structure_side = "hold"
    structure_conf = min(0.92, 0.48 + abs(position - 0.5))

    candle_range = max(float(one["high"]) - float(one["low"]), 0.00001)
    close_location = (close - float(one["low"])) / candle_range
    body_ratio = abs(body) / candle_range
    if body > 0 and close_location >= 0.65 and body_ratio >= 0.35:
        action_side = "buy"
    elif body < 0 and close_location <= 0.35 and body_ratio >= 0.35:
        action_side = "sell"
    else:
        action_side = "hold"
    action_conf = min(0.90, 0.42 + body_ratio * 0.50)

    spread_atr = spread / atr1
    if spread <= 0 or spread_atr > 0.35 or atr1 <= 0:
        volatility_side = "hold"
        volatility_conf = 0.90
        volatility_reason = f"Jakosc wykonania slaba, spread/ATR {spread_atr:.2f}"
    elif abs(progress1) >= atr1 * 0.12:
        volatility_side = "buy" if progress1 > 0 else "sell"
        volatility_conf = min(0.85, 0.48 + abs(progress1 / atr1) * 0.22)
        volatility_reason = f"ATR aktywny, spread/ATR {spread_atr:.2f}"
    else:
        volatility_side = "hold"
        volatility_conf = 0.55
        volatility_reason = f"Brak wyraznego ruchu, spread/ATR {spread_atr:.2f}"

    lower = float(one["bb_lower"])
    upper = float(one["bb_upper"])
    if close <= lower and rsi1 <= 35:
        reversion_side = "buy"
        reversion_conf = min(0.90, 0.58 + (35.0 - rsi1) / 50.0)
    elif close >= upper and rsi1 >= 65:
        reversion_side = "sell"
        reversion_conf = min(0.90, 0.58 + (rsi1 - 65.0) / 50.0)
    else:
        reversion_side = "hold"
        reversion_conf = 0.40

    # Independent veto-style experts.  They are deliberately conservative:
    # they can support a direction, but they hold when the market regime or
    # execution quality is unclear.
    regime_adx = float(fifteen["adx14"])
    regime_buy = trend_buy and regime_adx >= 17.0
    regime_sell = trend_sell and regime_adx >= 17.0
    regime_side = "buy" if regime_buy else "sell" if regime_sell else "hold"
    regime_conf = min(0.92, 0.56 + max(0.0, regime_adx - 17.0) / 80.0)
    guard_ok = spread > 0.0 and spread_atr <= 0.22 and abs(progress1) <= atr1 * 0.85
    guard_side = trend_side if guard_ok and trend_side in {"buy", "sell"} else "hold"
    guard_conf = 0.86 if guard_ok else 0.90

    return [
        _vote("trend", trend_side, trend_conf, trend_reason),
        _vote("momentum", momentum_side, momentum_conf, f"RSI {rsi1:.1f}, impet {momentum_value:.2f} ATR"),
        _vote("structure", structure_side, structure_conf, f"Cena w {position * 100:.0f}% lokalnego zakresu"),
        _vote("price_action", action_side, action_conf, f"Korpus {body_ratio:.2f}, close location {close_location:.2f}"),
        _vote("volatility", volatility_side, volatility_conf, volatility_reason),
        _vote("mean_reversion", reversion_side, reversion_conf, f"Bollinger i RSI {rsi1:.1f}"),
        _vote("regime_filter", regime_side, regime_conf, f"ADX M15 {regime_adx:.1f}; zgodnosc wyzszego rezimu"),
        _vote("execution_guard", guard_side, guard_conf, f"spread/ATR {spread_atr:.2f}; kontrola skoku i wykonania"),
    ]


def _long_term_votes(h1: pd.DataFrame, h4: pd.DataFrame, d1: pd.DataFrame, spread: float) -> list[dict]:
    hour = h1.iloc[-2]
    prev_hour = h1.iloc[-3]
    four = h4.iloc[-2]
    prev_four = h4.iloc[-3]
    day = d1.iloc[-2]
    atr1 = max(float(hour["atr14"]), 0.000001)
    atr4 = max(float(four["atr14"]), atr1)

    trend_buy = (
        float(day["ema20"]) > float(day["ema50"]) > float(day["ema200"])
        and float(four["ema20"]) > float(four["ema50"])
    )
    trend_sell = (
        float(day["ema20"]) < float(day["ema50"]) < float(day["ema200"])
        and float(four["ema20"]) < float(four["ema50"])
    )
    if trend_buy or trend_sell:
        trend_side = "buy" if trend_buy else "sell"
        trend_conf = min(0.96, 0.62 + max(0.0, float(four["adx14"]) - 18.0) / 70.0)
        trend_reason = "EMA H4/D1/EMA200 zgodne"
    else:
        trend_side, trend_conf, trend_reason = "hold", 0.52, "Brak pelnej zgodnosci trendu H4/D1"

    momentum_value = (
        (float(hour["close"]) - float(prev_hour["close"])) / atr1 * 0.45
        + (float(four["close"]) - float(prev_four["close"])) / atr4 * 0.55
    )
    rsi4 = float(four["rsi14"])
    if momentum_value > 0.18 and rsi4 >= 52:
        momentum_side = "buy"
    elif momentum_value < -0.18 and rsi4 <= 48:
        momentum_side = "sell"
    else:
        momentum_side = "hold"
    momentum_conf = min(0.94, 0.50 + abs(momentum_value) * 0.32)

    adx4 = float(four["adx14"])
    if adx4 >= 20 and trend_side in {"buy", "sell"}:
        regime_side = trend_side
        regime_conf = min(0.94, 0.58 + (adx4 - 20.0) / 80.0)
        regime_reason = f"Rezim trendowy H4, ADX {adx4:.1f}"
    else:
        regime_side, regime_conf, regime_reason = "hold", 0.58, f"Konsolidacja lub zmiana rezimu, ADX {adx4:.1f}"

    close4 = float(four["close"])
    hh4 = float(four["hh20"])
    ll4 = float(four["ll20"])
    if close4 > hh4:
        structure_side, structure_conf, structure_reason = "buy", 0.82, "Wybicie 20-okresowego szczytu H4"
    elif close4 < ll4:
        structure_side, structure_conf, structure_reason = "sell", 0.82, "Wybicie 20-okresowego dolka H4"
    elif trend_side in {"buy", "sell"}:
        structure_side, structure_conf, structure_reason = trend_side, 0.60, "Struktura H4 wspiera trend D1"
    else:
        structure_side, structure_conf, structure_reason = "hold", 0.50, "Struktura swingowa bez przewagi"

    spread_atr = max(0.0, spread) / atr1
    if spread_atr <= 0.12 and trend_side in {"buy", "sell"}:
        risk_side, risk_conf = trend_side, max(0.55, 0.78 - spread_atr)
        risk_reason = f"Koszt wykonania akceptowalny: spread/ATR H1 {spread_atr:.2f}"
    else:
        risk_side, risk_conf = "hold", min(0.90, 0.55 + spread_atr)
        risk_reason = f"Brak przewagi kosztowej lub trendu: spread/ATR H1 {spread_atr:.2f}"

    return [
        _vote("long_term_trend", trend_side, trend_conf, trend_reason),
        _vote("macro_momentum", momentum_side, momentum_conf, f"Momentum {momentum_value:.2f}, RSI H4 {rsi4:.1f}"),
        _vote("market_regime", regime_side, regime_conf, regime_reason),
        _vote("swing_structure", structure_side, structure_conf, structure_reason),
        _vote("long_term_risk", risk_side, risk_conf, risk_reason),
    ]


def _candidate_votes(
    m1: pd.DataFrame,
    m5: pd.DataFrame,
    m15: pd.DataFrame,
    learning: dict,
    spread: float = 0.0,
) -> list[dict]:
    one = m1.iloc[-2]
    prev_one = m1.iloc[-3]
    five = m5.iloc[-2]
    fifteen = m15.iloc[-2]
    close = float(one["close"])
    atr1 = max(float(one["atr14"]), 0.00001)
    body = abs(close - float(one["open"]))
    volume_ratio = float(one["tick_volume"]) / max(float(one["volume_ma20"]), 1.0)
    adx5 = float(five["adx14"])

    if close > float(one["hh20"]) and body >= atr1 * 0.45 and volume_ratio >= 1.05 and adx5 >= 16:
        breakout_side = "buy"
    elif close < float(one["ll20"]) and body >= atr1 * 0.45 and volume_ratio >= 1.05 and adx5 >= 16:
        breakout_side = "sell"
    else:
        breakout_side = "hold"
    breakout_conf = min(0.94, 0.48 + body / atr1 * 0.18 + max(0.0, volume_ratio - 1.0) * 0.20)

    trend_buy = float(fifteen["ema20"]) > float(fifteen["ema50"]) and float(five["ema20"]) > float(five["ema50"])
    trend_sell = float(fifteen["ema20"]) < float(fifteen["ema50"]) and float(five["ema20"]) < float(five["ema50"])
    if trend_buy and float(one["low"]) <= float(one["ema20"]) and close > float(one["ema20"]) and close > float(one["open"]):
        pullback_side = "buy"
    elif trend_sell and float(one["high"]) >= float(one["ema20"]) and close < float(one["ema20"]) and close < float(one["open"]):
        pullback_side = "sell"
    else:
        pullback_side = "hold"
    pullback_conf = min(0.92, 0.55 + abs(close - float(one["ema20"])) / atr1 * 0.20)

    prior = m1.iloc[-18:-3]
    prior_high = float(prior["high"].max())
    prior_low = float(prior["low"].min())
    if float(one["low"]) < prior_low and close > prior_low and close > float(one["open"]):
        liquidity_side = "buy"
    elif float(one["high"]) > prior_high and close < prior_high and close < float(one["open"]):
        liquidity_side = "sell"
    else:
        liquidity_side = "hold"
    liquidity_conf = min(0.92, 0.58 + max(float(one["high"]) - prior_high, prior_low - float(one["low"]), 0.0) / atr1 * 0.18)

    aligned_buy = (
        float(one["ema20"]) > float(one["ema50"])
        and float(five["ema20"]) > float(five["ema50"])
        and float(fifteen["ema20"]) > float(fifteen["ema50"])
        and float(one["rsi14"]) >= 52
    )
    aligned_sell = (
        float(one["ema20"]) < float(one["ema50"])
        and float(five["ema20"]) < float(five["ema50"])
        and float(fifteen["ema20"]) < float(fifteen["ema50"])
        and float(one["rsi14"]) <= 48
    )
    multi_side = "buy" if aligned_buy else "sell" if aligned_sell else "hold"
    multi_conf = min(0.95, 0.60 + max(0.0, adx5 - 12.0) / 80.0)

    # Re-entry after an exhaustion move.  Requiring the close back inside the
    # band avoids catching a still-expanding move merely because RSI is extreme.
    prev_lower = float(prev_one["bb_lower"])
    prev_upper = float(prev_one["bb_upper"])
    if float(prev_one["close"]) <= prev_lower and close > float(one["bb_lower"]) and float(one["rsi14"]) <= 42:
        bollinger_side = "buy"
    elif float(prev_one["close"]) >= prev_upper and close < float(one["bb_upper"]) and float(one["rsi14"]) >= 58:
        bollinger_side = "sell"
    else:
        bollinger_side = "hold"
    bollinger_excursion = max(
        prev_lower - float(prev_one["close"]),
        float(prev_one["close"]) - prev_upper,
        0.0,
    )
    bollinger_conf = min(0.93, 0.57 + bollinger_excursion / atr1 * 0.18)

    prev_five = m5.iloc[-3]
    macd_cross_up = float(prev_five["macd_hist"]) <= 0 < float(five["macd_hist"])
    macd_cross_down = float(prev_five["macd_hist"]) >= 0 > float(five["macd_hist"])
    if macd_cross_up and trend_buy and float(five["close"]) > float(five["ema20"]):
        macd_side = "buy"
    elif macd_cross_down and trend_sell and float(five["close"]) < float(five["ema20"]):
        macd_side = "sell"
    else:
        macd_side = "hold"
    macd_strength = abs(float(five["macd_hist"])) / max(float(five["atr14"]), atr1)
    macd_conf = min(0.94, 0.60 + macd_strength * 0.65 + max(0.0, adx5 - 18.0) / 100.0)

    # Dual Thrust-inspired adaptive intraday range.  This is an original,
    # compact implementation of the public formula, not copied project code.
    range_frame = m5.iloc[-26:-2]
    range_high = float(range_frame["high"].max())
    range_low = float(range_frame["low"].min())
    last_close = float(range_frame.iloc[-1]["close"])
    thrust_range = max(range_high - last_close, last_close - range_low, atr1)
    if "time" in m5.columns:
        five_times = pd.to_datetime(m5["time"], utc=True)
        current_day = five_times.iloc[-2].date()
        day_rows = m5.loc[five_times.dt.date == current_day]
        day_open = float(day_rows.iloc[0]["open"]) if not day_rows.empty else float(five["open"])
    else:
        day_open = float(five["open"])
    upper_trigger = day_open + thrust_range * 0.50
    lower_trigger = day_open - thrust_range * 0.50
    if float(five["close"]) > upper_trigger and float(five["close"]) > float(prev_five["close"]) and adx5 >= 18:
        thrust_side = "buy"
    elif float(five["close"]) < lower_trigger and float(five["close"]) < float(prev_five["close"]) and adx5 >= 18:
        thrust_side = "sell"
    else:
        thrust_side = "hold"
    thrust_distance = max(float(five["close"]) - upper_trigger, lower_trigger - float(five["close"]), 0.0)
    thrust_conf = min(0.94, 0.58 + thrust_distance / max(float(five["atr14"]), atr1) * 0.22)

    squeeze_released = bool(prev_one["bb_keltner_fast_squeeze"]) and not bool(one["bb_keltner_fast_squeeze"])
    fast_hist = float(one["macd_fast_hist"])
    if squeeze_released and fast_hist > 0 and close > float(one["ema20"]) and volume_ratio >= 1.0:
        squeeze_side = "buy"
    elif squeeze_released and fast_hist < 0 and close < float(one["ema20"]) and volume_ratio >= 1.0:
        squeeze_side = "sell"
    else:
        squeeze_side = "hold"
    squeeze_conf = min(0.94, 0.58 + abs(fast_hist) / atr1 * 0.45 + max(0.0, volume_ratio - 1.0) * 0.18)

    prev_vwap = float(prev_one["vwap"])
    current_vwap = float(one["vwap"])
    vwap_trend_buy = float(five["ema20"]) > float(five["ema50"])
    vwap_trend_sell = float(five["ema20"]) < float(five["ema50"])
    if float(prev_one["close"]) <= prev_vwap and close > current_vwap and vwap_trend_buy:
        vwap_side = "buy"
    elif float(prev_one["close"]) >= prev_vwap and close < current_vwap and vwap_trend_sell:
        vwap_side = "sell"
    else:
        vwap_side = "hold"
    vwap_distance = abs(close - current_vwap) / atr1
    vwap_conf = min(0.92, 0.57 + vwap_distance * 0.22 + max(0.0, adx5 - 16.0) / 120.0)

    candidates = [
        ("breakout_specialist", breakout_side, breakout_conf, f"Body/ATR {body / atr1:.2f}, volume {volume_ratio:.2f}, ADX {adx5:.1f}"),
        ("pullback_specialist", pullback_side, pullback_conf, "Powrot do EMA20 zgodny z trendem M5/M15"),
        ("liquidity_specialist", liquidity_side, liquidity_conf, "Test zebrania plynnosci nad/pod lokalnym zakresem"),
        ("multi_tf_specialist", multi_side, multi_conf, f"Zgodnosc EMA M1/M5/M15, RSI {float(one['rsi14']):.1f}"),
        ("bollinger_rsi_specialist", bollinger_side, bollinger_conf, f"Powrot do Bollingera, RSI {float(one['rsi14']):.1f}"),
        ("macd_ema_specialist", macd_side, macd_conf, f"MACD cross i EMA M5/M15, ADX {adx5:.1f}"),
        ("dual_thrust_specialist", thrust_side, thrust_conf, f"Zakres {thrust_range:.5f}, progi {lower_trigger:.5f}/{upper_trigger:.5f}"),
        ("squeeze_release_specialist", squeeze_side, squeeze_conf, f"Squeeze release, volume {volume_ratio:.2f}"),
        ("vwap_reclaim_specialist", vwap_side, vwap_conf, f"VWAP reclaim/reject, odleglosc {vwap_distance:.2f} ATR"),
    ]
    candidates.extend(indicator_strategy_votes(m1, m5, m15, spread=spread))
    records = learning.get("agents", {})
    return [
        _vote(name, side, confidence, reason, str(records.get(name, {}).get("status", "shadow")))
        for name, side, confidence, reason in candidates
    ]


def _default_learning_state() -> dict:
    agents = {}
    for name, weight in AGENT_WEIGHTS.items():
        agents[name] = {
            "kind": "base",
            "status": "active",
            "base_weight": weight,
            "weight_multiplier": 1.0,
            "observations": 0,
            "correct": 0,
            "incorrect": 0,
            "accuracy": 50.0,
        }
    for name, weight in CANDIDATE_AGENT_WEIGHTS.items():
        agents[name] = {
            "kind": "candidate",
            "status": "shadow",
            "base_weight": weight,
            "weight_multiplier": 1.0,
            "observations": 0,
            "correct": 0,
            "incorrect": 0,
            "accuracy": 50.0,
        }
    return {
        "schema": 2,
        "enabled": True,
        "agents": agents,
        "team_agents": {},
        "promotions": [],
        "demotions": [],
        "closed_trades_learned": 0,
        "last_update_utc": None,
    }


def _ensure_learning_state(raw: dict) -> dict:
    default = _default_learning_state()
    if not isinstance(raw, dict):
        return default
    raw["schema"] = max(2, int(raw.get("schema", 1) or 1))
    raw.setdefault("enabled", True)
    raw.setdefault("agents", {})
    raw.setdefault("team_agents", {})
    if not isinstance(raw.get("team_agents"), dict):
        raw["team_agents"] = {}
    raw.setdefault("promotions", [])
    raw.setdefault("demotions", [])
    raw.setdefault("closed_trades_learned", 0)
    raw.setdefault("last_update_utc", None)
    for name, record in default["agents"].items():
        target = raw["agents"].setdefault(name, {})
        for key, value in record.items():
            target.setdefault(key, value)
    return raw


def _team_learning_records(learning: dict, team_key: str | None = None) -> dict:
    if not team_key:
        return learning.get("agents", {})
    records = learning.setdefault("team_agents", {}).setdefault(team_key, {})
    for name, default_record in _default_learning_state()["agents"].items():
        target = records.setdefault(name, {})
        for key, value in default_record.items():
            target.setdefault(key, value)
    return records


def _candidate_is_eligible(team_key: str | None, agent_name: str) -> bool:
    raw = str(os.getenv("AGENT_TEAM_CANDIDATE_ALLOWLIST_JSON", "") or "").strip()
    if not raw or not team_key:
        return True
    try:
        payload = json.loads(raw)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    allowed = payload.get(team_key, [])
    if not isinstance(allowed, list):
        return False
    return agent_name in {str(item).strip() for item in allowed}


def _effective_weights(learning: dict, team_key: str | None = None) -> dict[str, float]:
    weights: dict[str, float] = {}
    for name, record in _team_learning_records(learning, team_key).items():
        if str(record.get("status", "shadow")) != "active":
            continue
        base = float(record.get("base_weight", AGENT_WEIGHTS.get(name, CANDIDATE_AGENT_WEIGHTS.get(name, 1.0))) or 1.0)
        multiplier = float(record.get("weight_multiplier", 1.0) or 1.0)
        weights[name] = max(0.20, min(2.50, base * multiplier))
    return weights


def _annotate_votes(votes: list[dict], learning: dict, team_key: str | None = None) -> list[dict]:
    records = _team_learning_records(learning, team_key)
    weights = _effective_weights(learning, team_key)
    for vote in votes:
        record = records.get(str(vote.get("agent", "")), {})
        vote["status"] = str(record.get("status", vote.get("status", "active")))
        vote["weight"] = round(float(weights.get(str(vote.get("agent", "")), 0.0)), 3)
        vote["learned_accuracy"] = round(float(record.get("accuracy", 50.0) or 50.0), 1)
        vote["observations"] = int(record.get("observations", 0) or 0)
        vote["research_eligible"] = _candidate_is_eligible(team_key, str(vote.get("agent", "")))
    return votes


def _update_learning(
    learning: dict,
    votes: list[dict],
    traded_side: str,
    profit: float,
    events_path: Path,
    team_key: str | None = None,
) -> bool:
    if not bool(learning.get("enabled", True)) or profit == 0.0 or not votes:
        return False
    correct_side = traded_side if profit > 0.0 else ("sell" if traded_side == "buy" else "buy")
    min_observations = max(4, int(_env_float("AGENT_TEAM_LEARNING_MIN_OBSERVATIONS", 12)))
    promotion_accuracy = max(0.50, min(0.90, _env_float("AGENT_TEAM_LEARNING_PROMOTION_ACCURACY", 0.64)))
    demotion_observations = max(min_observations, int(_env_float("AGENT_TEAM_LEARNING_DEMOTION_OBSERVATIONS", 24)))
    demotion_accuracy = max(0.30, min(promotion_accuracy, _env_float("AGENT_TEAM_LEARNING_DEMOTION_ACCURACY", 0.47)))
    changed = False
    records = _team_learning_records(learning, team_key)
    for vote in votes:
        side = str(vote.get("side", "hold"))
        if side not in {"buy", "sell"}:
            continue
        name = str(vote.get("agent", ""))
        record = records.get(name)
        if not record:
            continue
        record["observations"] = int(record.get("observations", 0) or 0) + 1
        if side == correct_side:
            record["correct"] = int(record.get("correct", 0) or 0) + 1
        else:
            record["incorrect"] = int(record.get("incorrect", 0) or 0) + 1
        observations = int(record["observations"])
        correct = int(record.get("correct", 0) or 0)
        smoothed_accuracy = (correct + 2.0) / (observations + 4.0)
        record["accuracy"] = round(smoothed_accuracy * 100.0, 2)
        record["weight_multiplier"] = round(max(0.65, min(1.35, 1.0 + (smoothed_accuracy - 0.5) * 1.4)), 3)
        if record.get("kind") == "candidate" and record.get("status") == "shadow":
            if (
                _candidate_is_eligible(team_key, name)
                and observations >= min_observations
                and smoothed_accuracy >= promotion_accuracy
            ):
                record["status"] = "active"
                event = {
                    "agent": name,
                    "team": team_key or "global",
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "observations": observations,
                    "accuracy": round(smoothed_accuracy * 100.0, 2),
                    "reason": "candidate_passed_shadow_validation",
                }
                learning.setdefault("promotions", []).append(event)
                _append_jsonl(events_path, {"type": "meta_agent_promoted", **event})
                changed = True
        elif record.get("kind") == "candidate" and record.get("status") == "active":
            if observations >= demotion_observations and smoothed_accuracy < demotion_accuracy:
                record["status"] = "shadow"
                event = {
                    "agent": name,
                    "team": team_key or "global",
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "observations": observations,
                    "accuracy": round(smoothed_accuracy * 100.0, 2),
                    "reason": "active_candidate_lost_edge",
                }
                learning.setdefault("demotions", []).append(event)
                _append_jsonl(events_path, {"type": "meta_agent_demoted", **event})
                changed = True
    learning["closed_trades_learned"] = int(learning.get("closed_trades_learned", 0) or 0) + 1
    learning["last_update_utc"] = datetime.now(UTC).isoformat()
    return changed


def _learning_summary(learning: dict) -> dict:
    records = learning.get("agents", {})
    ranked = sorted(
        (
            {
                "agent": name,
                "kind": record.get("kind"),
                "status": record.get("status"),
                "observations": int(record.get("observations", 0) or 0),
                "accuracy": float(record.get("accuracy", 50.0) or 50.0),
                "effective_weight": round(_effective_weights(learning).get(name, 0.0), 3),
                "description": AGENT_DESCRIPTIONS.get(name, ""),
            }
            for name, record in records.items()
        ),
        key=lambda item: (item["observations"], item["accuracy"]),
        reverse=True,
    )
    return {
        "enabled": bool(learning.get("enabled", True)),
        "description": AGENT_DESCRIPTIONS["meta_learner"],
        "closed_trades_learned": int(learning.get("closed_trades_learned", 0) or 0),
        "active_agents": sum(1 for item in ranked if item["status"] == "active"),
        "shadow_candidates": sum(1 for item in ranked if item["kind"] == "candidate" and item["status"] == "shadow"),
        "promotions": len(learning.get("promotions", [])),
        "demotions": len(learning.get("demotions", [])),
        "last_update_utc": learning.get("last_update_utc"),
        "agents": ranked,
        "recent_promotions": list(learning.get("promotions", []))[-10:],
        "recent_demotions": list(learning.get("demotions", []))[-10:],
        "team_models": {
            team_key: {
                "active_candidates": [
                    name
                    for name, record in records.items()
                    if record.get("kind") == "candidate" and record.get("status") == "active"
                ],
                "observations": sum(int(record.get("observations", 0) or 0) for record in records.values()),
            }
            for team_key, records in learning.get("team_agents", {}).items()
        },
    }


def supervise(votes: list[dict], threshold: float, min_directional_votes: int, weights: dict[str, float] | None = None) -> dict:
    weights = dict(weights or AGENT_WEIGHTS)
    scores = {"buy": 0.0, "sell": 0.0}
    counts = {"buy": 0, "sell": 0, "hold": 0}
    active_votes = [vote for vote in votes if str(vote.get("status", "active")) == "active" and str(vote.get("agent", "")) in weights]
    total_weight = sum(float(weights[str(vote["agent"])]) for vote in active_votes)
    for vote in active_votes:
        side = str(vote["side"])
        counts[side] = counts.get(side, 0) + 1
        if side in scores:
            scores[side] += float(weights[vote["agent"]]) * float(vote["confidence"])
    winner = "buy" if scores["buy"] > scores["sell"] else "sell"
    loser = "sell" if winner == "buy" else "buy"
    confidence = scores[winner] / max(total_weight, 0.00001)
    margin = (scores[winner] - scores[loser]) / max(total_weight, 0.00001)
    trade = counts[winner] >= min_directional_votes and confidence >= threshold and margin >= 0.16
    return {
        "decision": winner if trade else "hold",
        "candidate": winner,
        "confidence": round(confidence, 3),
        "margin": round(margin, 3),
        "counts": counts,
        "scores": {key: round(value, 3) for key, value in scores.items()},
        "reason": (
            f"{counts[winner]} glosy {winner}, confidence {confidence:.2f}, przewaga {margin:.2f}"
            if trade
            else f"Brak konsensusu: {counts[winner]} glosy, confidence {confidence:.2f}, przewaga {margin:.2f}"
        ),
        "description": AGENT_DESCRIPTIONS["supervisor"],
    }


def _levels(symbol: str, side: str, m1: pd.DataFrame, m5: pd.DataFrame, spread: float) -> tuple[float, float, float]:
    entry = _side_price(symbol, side)
    atr1 = max(float(m1.iloc[-2]["atr14"]), spread * 2.0)
    atr5 = max(float(m5.iloc[-2]["atr14"]), atr1)
    stop_distance = max(spread * 4.0, atr1 * 1.8, atr5 * 0.38)
    target_distance = max(spread * 3.0, atr1 * 1.15, stop_distance * 0.72)
    if side == "buy":
        return entry, _round_price(symbol, entry - stop_distance), _round_price(symbol, entry + target_distance)
    return entry, _round_price(symbol, entry + stop_distance), _round_price(symbol, entry - target_distance)


def _dedicated_strategy_levels(
    symbol: str,
    side: str,
    strategy: str,
    entry_frame: pd.DataFrame,
    spread: float,
) -> tuple[float, float, float]:
    entry = _side_price(symbol, side)
    atr = max(float(entry_frame.iloc[-2]["atr14"]), spread * 2.0)
    stop_atr, reward_risk, _ = _dedicated_strategy_parameters(strategy)
    stop_distance = max(spread * 4.0, atr * stop_atr)
    target_distance = stop_distance * reward_risk
    if side == "buy":
        return entry, _round_price(symbol, entry - stop_distance), _round_price(symbol, entry + target_distance)
    return entry, _round_price(symbol, entry + stop_distance), _round_price(symbol, entry - target_distance)


def _dedicated_strategy_parameters(strategy: str) -> tuple[float, float, int]:
    if strategy in {"breakout_specialist", "dual_thrust_specialist", "squeeze_release_specialist"}:
        return 1.30, 2.00, 72
    if strategy in {"bollinger_rsi_specialist", "liquidity_specialist", "mean_reversion"}:
        return 1.00, 1.25, 48
    if strategy in {"vwap_reclaim_specialist", "price_action"}:
        return 1.05, 1.45, 48
    if strategy in {"macd_ema_specialist", "pullback_specialist", "multi_tf_specialist"}:
        return 1.20, 1.80, 72
    if strategy in {
        "cci_reversal_specialist",
        "williams_reversal_specialist",
        "stochastic_cross_specialist",
        "mfi_reversal_specialist",
        "fisher_reversal_specialist",
        "ultimate_reversal_specialist",
    }:
        return 1.00, 1.25, 48
    if strategy in {
        "cmf_flow_specialist",
        "obv_confirmation_specialist",
        "force_index_specialist",
        "adosc_flow_specialist",
    }:
        return 1.05, 1.45, 60
    if strategy in {
        "supertrend_specialist",
        "ichimoku_specialist",
        "aroon_specialist",
        "kst_specialist",
        "ppo_specialist",
        "roc_acceleration_specialist",
        "linreg_pullback_specialist",
        "efficiency_trend_specialist",
        "hma_dema_cross_specialist",
        "cmo_momentum_specialist",
    }:
        return 1.20, 1.80, 72
    return 1.20, 1.55, 60


def _dedicated_max_hold_seconds(team: TeamSpec) -> int:
    if not team.strategy_profile.startswith("dedicated:"):
        return 0
    strategy = team.strategy_profile.split(":", 1)[1]
    return _dedicated_strategy_parameters(strategy)[2] * 5 * 60


def _dedicated_exit_policy(team: TeamSpec) -> dict:
    """Return an explicitly configured R-based protection policy for one team."""
    try:
        configured = json.loads(os.getenv("AGENT_TEAM_EXIT_PROTECTION_JSON", "{}") or "{}")
    except (TypeError, ValueError):
        configured = {}
    raw = configured.get(team.key, {}) if isinstance(configured, dict) else {}
    if not isinstance(raw, dict):
        return {}
    trigger_r = max(0.0, float(raw.get("trigger_r", 0.0) or 0.0))
    if trigger_r <= 0.0:
        return {}
    return {
        "trigger_r": trigger_r,
        "lock_r": float(raw.get("lock_r", 0.0) or 0.0),
        "trail_r": max(0.0, float(raw.get("trail_r", 0.0) or 0.0)),
    }


def _protected_stop(
    side: str,
    entry: float,
    initial_sl: float,
    current: float,
    peak_r: float,
    policy: dict,
) -> tuple[float | None, float]:
    distance = abs(entry - initial_sl)
    if distance <= 0.0 or not policy:
        return None, peak_r
    favorable_r = (current - entry) / distance if side == "buy" else (entry - current) / distance
    peak_r = max(peak_r, favorable_r)
    if peak_r < float(policy["trigger_r"]):
        return None, peak_r
    stop_r = float(policy.get("lock_r", 0.0) or 0.0)
    trail_r = float(policy.get("trail_r", 0.0) or 0.0)
    if trail_r > 0.0:
        stop_r = max(stop_r, peak_r - trail_r)
    stop = entry + distance * stop_r if side == "buy" else entry - distance * stop_r
    return stop, peak_r


def _long_term_levels(
    symbol: str,
    side: str,
    h1: pd.DataFrame,
    h4: pd.DataFrame,
    spread: float,
) -> tuple[float, float, float]:
    entry = _side_price(symbol, side)
    atr1 = max(float(h1.iloc[-2]["atr14"]), spread * 3.0)
    atr4 = max(float(h4.iloc[-2]["atr14"]), atr1)
    stop_distance = max(spread * 8.0, atr1 * 2.4, atr4 * 0.85)
    target_distance = max(stop_distance * 1.8, atr4 * 1.35)
    if side == "buy":
        return entry, _round_price(symbol, entry - stop_distance), _round_price(symbol, entry + target_distance)
    return entry, _round_price(symbol, entry + stop_distance), _round_price(symbol, entry - target_distance)


def _profit(symbol: str, side: str, volume: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    result = mt5.order_calc_profit(order_type, symbol, volume, entry, exit_price)
    if result is not None:
        return float(result)
    direction = 1.0 if side == "buy" else -1.0
    return direction * (exit_price - entry) * volume


def _position_volume(
    team: TeamSpec,
    side: str,
    entry: float,
    sl: float,
    equity: float,
    runtime: dict,
) -> tuple[float, dict]:
    mode = str(os.getenv("AGENT_TEAM_LOT_MODE", "fixed") or "fixed").strip().lower()
    base_volume = max(team.min_volume, team.requested_volume)
    risk_pct = max(0.01, min(5.0, _env_float("AGENT_TEAM_RISK_PER_TRADE_PCT", 0.10)))
    max_lot = max(team.min_volume, _env_float("AGENT_TEAM_DYNAMIC_MAX_LOT", base_volume))
    equity_base_usd = max(0.0, _env_float("AGENT_TEAM_EQUITY_BASE_USD", 1000.0))
    equity_base_lot = max(team.min_volume, _env_float("AGENT_TEAM_EQUITY_BASE_LOT", base_volume))
    equity_step_usd = max(1.0, _env_float("AGENT_TEAM_EQUITY_STEP_USD", 300.0))
    equity_step_lot = max(0.0, _env_float("AGENT_TEAM_EQUITY_STEP_LOT", 0.01))
    equity_steps = max(0, int((max(0.0, equity) - equity_base_usd) // equity_step_usd))
    loss_per_lot = max(0.0, calc_loss_per_lot(team.symbol, side, entry, sl))
    risk_amount = max(0.0, equity) * risk_pct / 100.0
    raw_volume = base_volume
    if mode == "dynamic_risk" and loss_per_lot > 0.0:
        raw_volume = risk_amount / loss_per_lot
    elif mode == "equity_step":
        raw_volume = equity_base_lot + (equity_steps * equity_step_lot)

    martingale_enabled = _env_bool("AGENT_TEAM_MARTINGALE_ENABLED", False)
    loss_streak = int(runtime.setdefault("loss_streaks", {}).get(team.key, 0) or 0)
    martingale_multiplier = 1.0
    if martingale_enabled and loss_streak > 0:
        factor = max(1.0, min(2.0, _env_float("AGENT_TEAM_MARTINGALE_FACTOR", 1.25)))
        max_steps = max(0, min(3, int(_env_float("AGENT_TEAM_MARTINGALE_MAX_STEPS", 1))))
        max_multiplier = max(1.0, min(3.0, _env_float("AGENT_TEAM_MARTINGALE_MAX_MULTIPLIER", 1.50)))
        martingale_multiplier = min(max_multiplier, factor ** min(loss_streak, max_steps))
        raw_volume *= martingale_multiplier

    volume = normalize_volume(team.symbol, raw_volume, team.min_volume, max_lot)
    return volume, {
        "mode": mode,
        "risk_pct": risk_pct,
        "risk_amount": round(risk_amount, 2),
        "loss_per_lot": round(loss_per_lot, 2),
        "equity_base_usd": round(equity_base_usd, 2),
        "equity_base_lot": round(equity_base_lot, 2),
        "equity_step_usd": round(equity_step_usd, 2),
        "equity_step_lot": round(equity_step_lot, 2),
        "equity_steps": equity_steps,
        "raw_volume": round(raw_volume, 4),
        "volume": volume,
        "loss_streak": loss_streak,
        "martingale_enabled": martingale_enabled,
        "martingale_multiplier": round(martingale_multiplier, 3),
    }


def _update_loss_streak(runtime: dict, team_key: str, profit: float) -> None:
    streaks = runtime.setdefault("loss_streaks", {})
    streaks[team_key] = int(streaks.get(team_key, 0) or 0) + 1 if profit < 0.0 else 0


def _trade_stats(path: Path) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    if not path.exists():
        return stats
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            team = str(row.get("team", "") or "")
            item = stats.setdefault(team, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
            profit = float(row.get("profit", 0.0) or 0.0)
            item["trades"] += 1
            item["wins"] += int(profit > 0)
            item["losses"] += int(profit < 0)
            item["pnl"] += profit
    for item in stats.values():
        item["pnl"] = round(item["pnl"], 2)
        item["win_rate"] = round(100.0 * item["wins"] / max(1, item["trades"]), 1)
    return stats


def _resolve_teams(magic_base: int, live_keys: set[str], xau_lot: float, default_lot: float) -> list[TeamSpec]:
    teams: list[TeamSpec] = []
    raw_teams = [
        *TEAM_SPECS,
        *NASDAQ_TEAM_SPECS,
        *DEDICATED_STRATEGY_TEAM_SPECS,
        *_configured_dedicated_strategy_team_specs(),
    ]
    for index, raw in enumerate(raw_teams):
        team = TeamSpec(*raw[:4])
        if len(raw) > 4:
            team.strategy_profile = str(raw[4])
        if team.key.startswith("mm_"):
            team.source = "validated_repository_research"
        team.magic = magic_base + index
        team.requested_volume = xau_lot if team.key == "xau" else default_lot
        try:
            team.symbol = ensure_symbol(team.preferred_symbol)
            info = symbol_info(team.symbol)
            team.min_volume = float(getattr(info, "volume_min", 0.01) or 0.01)
            if team.key != "xau":
                team.requested_volume = team.min_volume
            team.trade_mode = (
                "live_demo"
                if team.key in live_keys and team.requested_volume >= team.min_volume
                else f"shadow_{team.requested_volume:.2f}"
            )
        except Exception:
            team.trade_mode = "unavailable"
        teams.append(team)
    return teams


def _resolve_long_term_teams(
    magic_base: int,
    enabled_keys: set[str],
    live_keys: set[str],
    xau_lot: float,
    default_lot: float,
) -> list[TeamSpec]:
    teams: list[TeamSpec] = []
    for index, raw in enumerate(LONG_TERM_TEAM_SPECS):
        team = TeamSpec(*raw)
        if team.key not in enabled_keys:
            continue
        team.magic = magic_base + index
        team.requested_volume = xau_lot if team.key == "long_xau" else default_lot
        team.source = "long_term_brigade"
        team.strategy_profile = "long_term"
        team.recruited_agents = (
            "long_term_trend",
            "macro_momentum",
            "market_regime",
            "swing_structure",
            "long_term_risk",
        )
        try:
            team.symbol = ensure_symbol(team.preferred_symbol)
            info = symbol_info(team.symbol)
            team.min_volume = float(getattr(info, "volume_min", 0.01) or 0.01)
            if team.key != "long_xau":
                team.requested_volume = team.min_volume
            team.trade_mode = (
                "live_demo"
                if team.key in live_keys and team.requested_volume >= team.min_volume
                else f"shadow_{team.requested_volume:.2f}"
            )
        except Exception:
            team.trade_mode = "unavailable"
        teams.append(team)
    return teams


def _manage_shadow(
    team: TeamSpec,
    runtime: dict,
    trades_path: Path,
    events_path: Path,
    learning: dict,
    learning_path: Path,
) -> None:
    item = (runtime.setdefault("shadow_positions", {})).get(team.key)
    if not item or not team.symbol:
        return
    tick = get_tick(team.symbol)
    side = str(item["side"])
    price = float(tick.bid if side == "buy" else tick.ask)
    if price <= 0:
        return
    policy = _dedicated_exit_policy(team)
    protected_sl, peak_r = _protected_stop(
        side,
        float(item["entry"]),
        float(item.get("initial_sl", item["sl"])),
        price,
        float(item.get("protect_peak_r", 0.0) or 0.0),
        policy,
    )
    item["protect_peak_r"] = round(peak_r, 6)
    if protected_sl is not None:
        current_sl = float(item["sl"])
        better = protected_sl > current_sl if side == "buy" else protected_sl < current_sl
        if better:
            item["sl"] = _round_price(team.symbol, protected_sl)
            _append_jsonl(events_path, {"type": "shadow_protect", "team": team.key, "new_sl": item["sl"], "policy": policy})
    opened = pd.Timestamp(item.get("opened_utc", datetime.now(UTC).isoformat()))
    elapsed = max(0.0, (pd.Timestamp.now(tz="UTC") - opened).total_seconds())
    timed_out = int(item.get("max_hold_seconds", 0) or 0) > 0 and elapsed >= int(item["max_hold_seconds"])
    hit_tp = price >= float(item["tp"]) if side == "buy" else price <= float(item["tp"])
    hit_sl = price <= float(item["sl"]) if side == "buy" else price >= float(item["sl"])
    if not hit_tp and not hit_sl and not timed_out:
        return
    exit_price = float(item["tp"] if hit_tp else item["sl"] if hit_sl else price)
    reason = "tp" if hit_tp else "sl" if hit_sl else "timeout"
    profit = _profit(team.symbol, side, float(item["volume"]), float(item["entry"]), exit_price)
    _append_trade(
        trades_path,
        {
            "team": team.key,
            "symbol": team.symbol,
            "mode": team.trade_mode,
            "ticket": item["ticket"],
            "side": side,
            "volume": item["volume"],
            "entry": item["entry"],
            "exit": exit_price,
            "profit": round(profit, 2),
            "reason": reason,
            "confidence": item.get("confidence", 0.0),
        },
    )
    _append_jsonl(events_path, {"type": "shadow_closed", "team": team.key, "profit": round(profit, 2), "reason": reason})
    _update_loss_streak(runtime, team.key, profit)
    _update_learning(learning, list(item.get("votes", [])), side, profit, events_path, team.key)
    _write_json(learning_path, learning)
    runtime["shadow_positions"].pop(team.key, None)


def _manage_live(
    team: TeamSpec,
    deviation: int,
    runtime: dict,
    trades_path: Path,
    events_path: Path,
    learning: dict,
    learning_path: Path,
) -> None:
    live_meta = runtime.setdefault("live_positions", {})
    current = {str(position.ticket): position for position in positions_by_magic(team.symbol, team.magic)}
    for ticket, item in list(live_meta.items()):
        if str(item.get("team", "") or "") != team.key:
            continue
        position = current.get(ticket)
        max_hold_seconds = int(item.get("max_hold_seconds", 0) or 0)
        opened = pd.Timestamp(item.get("opened_utc", datetime.now(UTC).isoformat()))
        elapsed = max(0.0, (pd.Timestamp.now(tz="UTC") - opened).total_seconds())
        if position is not None:
            side = str(item.get("side", "") or "")
            entry = float(item.get("entry", getattr(position, "price_open", 0.0)) or 0.0)
            initial_sl = float(item.get("initial_sl", 0.0) or 0.0)
            if initial_sl <= 0.0:
                initial_sl = float(getattr(position, "sl", 0.0) or 0.0)
                item["initial_sl"] = initial_sl
            current_price = float(getattr(position, "price_current", 0.0) or 0.0)
            policy = _dedicated_exit_policy(team)
            protected_sl, peak_r = _protected_stop(
                side,
                entry,
                initial_sl,
                current_price,
                float(item.get("protect_peak_r", 0.0) or 0.0),
                policy,
            )
            item["protect_peak_r"] = round(peak_r, 6)
            if protected_sl is not None:
                protected_sl = _round_price(team.symbol, protected_sl)
                old_sl = float(getattr(position, "sl", 0.0) or 0.0)
                better = protected_sl > old_sl if side == "buy" else old_sl <= 0.0 or protected_sl < old_sl
                if better:
                    result = modify_position(position, protected_sl, float(getattr(position, "tp", 0.0) or 0.0))
                    _append_jsonl(
                        events_path,
                        {
                            "type": "live_protect_attempt",
                            "team": team.key,
                            "ticket": ticket,
                            "new_sl": protected_sl,
                            "policy": policy,
                            "retcode": int(getattr(result, "retcode", -1) or -1),
                        },
                    )
        if position is not None and max_hold_seconds > 0 and elapsed >= max_hold_seconds:
            last_request = pd.Timestamp(item.get("timeout_close_requested_utc", "1970-01-01T00:00:00+00:00"))
            if (pd.Timestamp.now(tz="UTC") - last_request).total_seconds() >= 30:
                result = close_position(position, deviation)
                item["timeout_close_requested_utc"] = datetime.now(UTC).isoformat()
                _append_jsonl(
                    events_path,
                    {
                        "type": "live_timeout_close_attempt",
                        "team": team.key,
                        "ticket": ticket,
                        "retcode": int(getattr(result, "retcode", -1) or -1),
                    },
                )
            continue
        if position is not None:
            continue
        deals = list(mt5.history_deals_get(position=int(ticket)) or [])
        if not deals:
            continue
        profit = sum(
            float(getattr(deal, "profit", 0.0) or 0.0)
            + float(getattr(deal, "commission", 0.0) or 0.0)
            + float(getattr(deal, "swap", 0.0) or 0.0)
            for deal in deals
        )
        exit_price = float(getattr(deals[-1], "price", item["entry"]) or item["entry"])
        _append_trade(
            trades_path,
            {
                "team": team.key,
                "symbol": team.symbol,
                "mode": "live_demo",
                "ticket": ticket,
                "side": item["side"],
                "volume": item["volume"],
                "entry": item["entry"],
                "exit": exit_price,
                "profit": round(profit, 2),
                "reason": "broker_close",
                "confidence": item.get("confidence", 0.0),
            },
        )
        _append_jsonl(events_path, {"type": "live_closed", "team": team.key, "ticket": ticket, "profit": round(profit, 2)})
        _update_loss_streak(runtime, team.key, profit)
        _update_learning(learning, list(item.get("votes", [])), str(item["side"]), profit, events_path, team.key)
        _write_json(learning_path, learning)
        live_meta.pop(ticket, None)


def _open_shadow(
    team: TeamSpec,
    decision: dict,
    votes: list[dict],
    entry: float,
    sl: float,
    tp: float,
    runtime: dict,
    events_path: Path,
    volume: float | None = None,
    sizing: dict | None = None,
) -> None:
    actual_volume = float(volume if volume is not None else team.requested_volume)
    ticket = f"shadow-{team.key}-{int(time.time())}"
    runtime.setdefault("shadow_positions", {})[team.key] = {
        "ticket": ticket,
        "side": decision["decision"],
        "entry": entry,
        "sl": sl,
        "initial_sl": sl,
        "tp": tp,
        "volume": actual_volume,
        "sizing": dict(sizing or {}),
        "opened_utc": datetime.now(UTC).isoformat(),
        "max_hold_seconds": _dedicated_max_hold_seconds(team),
        "confidence": decision["confidence"],
        "votes": votes,
    }
    _append_jsonl(events_path, {"type": "shadow_opened", "team": team.key, "symbol": team.symbol, "ticket": ticket, "side": decision["decision"], "entry": entry, "sl": sl, "tp": tp, "volume": actual_volume, "sizing": dict(sizing or {})})


def _open_live(
    team: TeamSpec,
    cfg,
    decision: dict,
    votes: list[dict],
    entry: float,
    sl: float,
    tp: float,
    runtime: dict,
    events_path: Path,
    volume: float | None = None,
    sizing: dict | None = None,
) -> None:
    actual_volume = normalize_volume(
        team.symbol,
        float(volume if volume is not None else team.requested_volume),
        team.min_volume,
        max(float(volume if volume is not None else team.requested_volume), team.min_volume),
    )
    result = send_market_order(
        team.symbol,
        decision["decision"],
        actual_volume,
        sl,
        tp,
        int(cfg.deviation),
        team.magic,
        team.comment,
    )
    retcode = int(getattr(result, "retcode", -1) or -1)
    payload = {
        "type": "live_order_attempt",
        "team": team.key,
        "symbol": team.symbol,
        "side": decision["decision"],
        "volume": actual_volume,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "retcode": retcode,
        "comment": str(getattr(result, "comment", "") or ""),
        "sizing": dict(sizing or {}),
    }
    if retcode in {10008, 10009, 10010}:
        opened_positions = positions_by_magic(team.symbol, team.magic)
        newest_position = max(
            opened_positions,
            key=lambda position: int(getattr(position, "time_msc", 0) or getattr(position, "time", 0) or 0),
            default=None,
        )
        ticket = int(
            getattr(newest_position, "ticket", 0)
            or getattr(result, "order", 0)
            or getattr(result, "deal", 0)
            or 0
        )
        runtime.setdefault("live_positions", {})[str(ticket)] = {
            "team": team.key,
            "side": decision["decision"],
            "entry": entry,
            "initial_sl": sl,
            "volume": actual_volume,
            "opened_utc": datetime.now(UTC).isoformat(),
            "max_hold_seconds": _dedicated_max_hold_seconds(team),
            "confidence": decision["confidence"],
            "votes": votes,
        }
        payload["ticket"] = ticket
    _append_jsonl(events_path, payload)


def run() -> None:
    from .config import load_settings

    cfg = load_settings()
    data_dir = Path(os.getenv("AGENT_TEAM_DATA_DIR", "data_vantage_agent_teams"))
    if not data_dir.is_absolute():
        data_dir = cfg.base_dir / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    status_path = data_dir / STATUS_FILE
    events_path = data_dir / EVENTS_FILE
    trades_path = data_dir / TRADES_FILE
    runtime_path = data_dir / RUNTIME_FILE
    learning_path = data_dir / LEARNING_FILE
    scout_path = data_dir / SCOUT_FILE
    control_path = data_dir / CONTROL_FILE
    if not _env_bool("AGENT_TEAM_ENABLED", True):
        _write_json(
            status_path,
            {
                "heartbeat_utc": datetime.now(UTC).isoformat(),
                "enabled": False,
                "running": False,
                "reason": "disabled_by_profile",
                "teams": [],
                "summary": {"teams": 0, "live_demo": 0, "open_positions": 0},
            },
        )
        return
    _ensure_trades(trades_path)
    runtime = _read_json(runtime_path, {"last_bars": {}, "last_trade_epoch": {}, "shadow_positions": {}, "live_positions": {}})
    learning = _ensure_learning_state(_read_json(learning_path, {}))
    learning["enabled"] = _env_bool("AGENT_TEAM_LEARNING_ENABLED", True)
    _write_json(learning_path, learning)
    scout = _ensure_scout_state(_read_json(scout_path, {}))
    scout["enabled"] = _env_bool("AGENT_TEAM_SCOUT_ENABLED", True)
    control = _ensure_control_state(_read_json(control_path, {}))
    control["enabled"] = _env_bool("AGENT_TEAM_CONTROL_ENABLED", True)

    connect(Mt5Credentials(cfg.mt5_login, cfg.mt5_password, cfg.mt5_server, cfg.mt5_path))
    account = account_info()
    allow_live_account = _env_bool("AGENT_TEAM_ALLOW_LIVE_ACCOUNT", False)
    is_demo = "demo" in str(getattr(account, "server", "") or "").lower()
    if not is_demo and not allow_live_account:
        raise RuntimeError("Agent Teams refuses a non-demo account. Set AGENT_TEAM_ALLOW_LIVE_ACCOUNT=true explicitly.")

    enabled_keys = {value.strip().lower() for value in os.getenv("AGENT_TEAM_ENABLED_KEYS", "xau,nasdaq,aapl,msft,nvda,amzn,goog,meta,tsla,avgo,amd,orcl").split(",") if value.strip()}
    live_keys = {value.strip().lower() for value in os.getenv("AGENT_TEAM_LIVE_KEYS", "xau").split(",") if value.strip()}
    xau_lot = max(0.01, _env_float("AGENT_TEAM_XAU_LOT", 0.01))
    default_lot = max(0.01, _env_float("AGENT_TEAM_DEFAULT_LOT", 1.0))
    configured_teams = [
        team
        for team in _resolve_teams(
            int(_env_float("AGENT_TEAM_MAGIC_BASE", 995100)),
            live_keys,
            xau_lot,
            default_lot,
        )
        if team.key in enabled_keys
    ]
    long_term_enabled = _env_bool("AGENT_TEAM_LONG_TERM_ENABLED", True)
    long_term_keys = {
        value.strip().lower()
        for value in os.getenv(
            "AGENT_TEAM_LONG_TERM_KEYS",
            "long_xau,long_nasdaq,long_sp500,long_btc,long_eth",
        ).split(",")
        if value.strip()
    }
    long_term_live_keys = {
        value.strip().lower()
        for value in os.getenv("AGENT_TEAM_LONG_TERM_LIVE_KEYS", "").split(",")
        if value.strip()
    }
    long_term_xau_lot = max(0.01, _env_float("AGENT_TEAM_LONG_TERM_XAU_LOT", 0.01))
    long_term_default_lot = max(0.01, _env_float("AGENT_TEAM_LONG_TERM_DEFAULT_LOT", 0.10))
    long_term_teams = (
        _resolve_long_term_teams(
            int(_env_float("AGENT_TEAM_LONG_TERM_MAGIC_BASE", 997000)),
            long_term_keys,
            long_term_live_keys,
            long_term_xau_lot,
            long_term_default_lot,
        )
        if long_term_enabled
        else []
    )
    base_teams = configured_teams + long_term_teams
    scout_scan_seconds = max(300.0, _env_float("AGENT_TEAM_SCOUT_SCAN_SECONDS", 1800.0))
    scout_max_catalog = max(10, int(_env_float("AGENT_TEAM_SCOUT_MAX_CATALOG", 80)))
    scout_max_recruits = max(1, int(_env_float("AGENT_TEAM_SCOUT_MAX_RECRUITS", 5)))
    scout_minimum_score = min(95.0, max(40.0, _env_float("AGENT_TEAM_SCOUT_MIN_SCORE", 68.0)))
    scout_magic_base = int(_env_float("AGENT_TEAM_SCOUT_MAGIC_BASE", 996000))
    scout_live_demo = _env_bool("AGENT_TEAM_SCOUT_LIVE_DEMO", False)
    last_scout_scan_epoch = 0.0
    if scout["enabled"]:
        existing_symbols = {
            str(team.symbol or team.preferred_symbol).upper()
            for team in base_teams
        }
        scout = _scan_instruments(
            scout,
            existing_symbols,
            scout_max_catalog,
            scout_max_recruits,
            scout_minimum_score,
            scout_magic_base,
            events_path,
        )
        _write_json(scout_path, scout)
        last_scout_scan_epoch = time.time()
    teams = base_teams + (_resolve_scout_teams(scout, scout_live_demo) if scout["enabled"] else [])
    loop_seconds = max(0.5, _env_float("AGENT_TEAM_LOOP_SECONDS", 1.0))
    cooldown_seconds = max(60.0, _env_float("AGENT_TEAM_COOLDOWN_SECONDS", 900.0))
    max_open_per_team = max(0, int(_env_float("AGENT_TEAM_MAX_OPEN_PER_TEAM", 1)))
    max_tick_age_seconds = max(30.0, _env_float("AGENT_TEAM_MAX_TICK_AGE_SECONDS", 180.0))
    threshold = min(0.95, max(0.30, _env_float("AGENT_TEAM_SUPERVISOR_THRESHOLD", 0.56)))
    min_votes = max(2, int(_env_float("AGENT_TEAM_MIN_DIRECTIONAL_VOTES", 3)))
    long_term_threshold = min(0.95, max(0.40, _env_float("AGENT_TEAM_LONG_TERM_THRESHOLD", 0.62)))
    long_term_min_votes = max(2, int(_env_float("AGENT_TEAM_LONG_TERM_MIN_VOTES", 3)))
    long_term_cooldown_seconds = max(3600.0, _env_float("AGENT_TEAM_LONG_TERM_COOLDOWN_SECONDS", 14400.0))
    control_minimum_confidence = min(0.90, max(0.40, _env_float("AGENT_TEAM_CONTROL_MIN_CONFIDENCE", 0.58)))
    control_conflict_seconds = max(30.0, _env_float("AGENT_TEAM_CONTROL_CONFLICT_SECONDS", 180.0))

    try:
        while True:
            _refresh_trade_reviews(control, trades_path)
            if scout["enabled"] and time.time() - last_scout_scan_epoch >= scout_scan_seconds:
                existing_symbols = {
                    str(team.symbol or team.preferred_symbol).upper()
                    for team in base_teams
                }
                scout = _scan_instruments(
                    scout,
                    existing_symbols,
                    scout_max_catalog,
                    scout_max_recruits,
                    scout_minimum_score,
                    scout_magic_base,
                    events_path,
                )
                _write_json(scout_path, scout)
                teams = base_teams + _resolve_scout_teams(scout, scout_live_demo)
                last_scout_scan_epoch = time.time()
            snapshots = []
            trade_stats = _trade_stats(trades_path)
            market_clock = 0
            for clock_team in teams:
                if not clock_team.symbol:
                    continue
                try:
                    market_clock = max(market_clock, int(getattr(get_tick(clock_team.symbol), "time", 0) or 0))
                except Exception:
                    continue
            for team in teams:
                snapshot = {
                    "key": team.key,
                    "name": team.name,
                    "symbol": team.symbol or team.preferred_symbol,
                    "preferred_symbol": team.preferred_symbol,
                    "magic": team.magic,
                    "mode": team.trade_mode,
                    "requested_lot": team.requested_volume,
                    "broker_min_lot": team.min_volume,
                    "comment": team.comment,
                    "source": team.source,
                    "strategy_profile": team.strategy_profile,
                    "recruited_agents": list(team.recruited_agents),
                    "scout_score": team.scout_score,
                    "agents": [],
                    "control": {"verdict": "waiting", "approved": False, "reason": "Oczekiwanie na propozycje"},
                    "supervisor": {"decision": "hold", "reason": "Oczekiwanie na dane"},
                    "stats": trade_stats.get(team.key, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "win_rate": 0.0}),
                }
                try:
                    if team.trade_mode == "unavailable":
                        snapshot["supervisor"] = {"decision": "hold", "reason": "Symbol niedostepny u brokera"}
                        snapshots.append(snapshot)
                        continue
                    if team.trade_mode == "live_demo":
                        _manage_live(team, int(cfg.deviation), runtime, trades_path, events_path, learning, learning_path)
                    tick = get_tick(team.symbol)
                    bid, ask = float(tick.bid), float(tick.ask)
                    if bid <= 0 or ask <= 0:
                        snapshot["supervisor"] = {"decision": "hold", "reason": "Rynek zamkniety albo brak aktualnego ticka"}
                        snapshots.append(snapshot)
                        continue
                    tick_epoch = int(getattr(tick, "time", 0) or 0)
                    tick_age = max(0, market_clock - tick_epoch) if market_clock and tick_epoch else 999999
                    snapshot["market"] = {
                        "bid": bid,
                        "ask": ask,
                        "spread": round(ask - bid, 6),
                        "tick_age_seconds": tick_age,
                    }
                    if tick_age > max_tick_age_seconds:
                        snapshot["supervisor"] = {
                            "decision": "hold",
                            "reason": f"Rynek poza sesja: tick starszy o {tick_age}s",
                        }
                        snapshots.append(snapshot)
                        continue
                    _manage_shadow(team, runtime, trades_path, events_path, learning, learning_path)
                    spread = ask - bid
                    dedicated_agent = ""
                    if team.strategy_profile == "long_term":
                        entry_frame = enrich(get_rates_df(team.symbol, "H1", 260))
                        trend_frame = enrich(get_rates_df(team.symbol, "H4", 260))
                        macro_frame = enrich(get_rates_df(team.symbol, "D1", 260))
                        bar_key = pd.Timestamp(entry_frame.iloc[-2]["time"]).isoformat()
                        votes = _long_term_votes(entry_frame, trend_frame, macro_frame, spread)
                        team_threshold = long_term_threshold
                        team_min_votes = long_term_min_votes
                        team_cooldown = long_term_cooldown_seconds
                    else:
                        timeframe_map = {
                            "nasdaq_swing": ("M5", "M15", "H1"),
                            "nasdaq_macro": ("M15", "H1", "H4"),
                            "nasdaq_pending": ("M15", "H1", "H4"),
                            "nasdaq_scraper": ("M1", "M5", "M15"),
                            "dedicated:dual_thrust_specialist": ("M5", "M15", "H1"),
                            "dedicated:roc_acceleration_specialist": ("M5", "M15", "H1"),
                        }
                        default_timeframes = (
                            ("M5", "M15", "H1")
                            if team.strategy_profile.startswith("dedicated:")
                            else ("M1", "M5", "M15")
                        )
                        entry_tf, trend_tf, macro_tf = timeframe_map.get(team.strategy_profile, default_timeframes)
                        entry_frame = enrich(get_rates_df(team.symbol, entry_tf, 260))
                        trend_frame = enrich(get_rates_df(team.symbol, trend_tf, 260))
                        macro_frame = enrich(get_rates_df(team.symbol, macro_tf, 260))
                        bar_key = pd.Timestamp(entry_frame.iloc[-2]["time"]).isoformat()
                        votes = _agent_votes(entry_frame, trend_frame, macro_frame, spread)
                        votes.extend(_candidate_votes(entry_frame, trend_frame, macro_frame, learning, spread))
                        if team.source == "instrument_scout" and team.recruited_agents:
                            assigned = set(team.recruited_agents)
                            votes = [vote for vote in votes if str(vote.get("agent", "")) in assigned]
                        if team.strategy_profile.startswith("dedicated:"):
                            dedicated_agent = team.strategy_profile.split(":", 1)[1]
                            votes = [vote for vote in votes if str(vote.get("agent", "")) == dedicated_agent]
                        profile_rules = {
                            # The live Nasdaq path is deliberately stricter after
                            # the observed whipsaw clusters.
                            "nasdaq": (max(threshold, 0.65), max(min_votes, 4), max(cooldown_seconds, 300.0)),
                            "nasdaq_fast": (max(threshold, 0.66), max(min_votes, 4), max(cooldown_seconds, 180.0)),
                            "nasdaq_swing": (max(threshold, 0.64), max(min_votes, 4), max(cooldown_seconds, 300.0)),
                            "nasdaq_macro": (max(threshold, 0.67), max(min_votes, 4), max(cooldown_seconds, 600.0)),
                            "nasdaq_pending": (max(threshold, 0.68), max(min_votes, 4), max(cooldown_seconds, 900.0)),
                            "nasdaq_scraper": (max(threshold, 0.70), max(min_votes, 4), max(cooldown_seconds, 600.0)),
                            "dedicated:dual_thrust_specialist": (0.58, 1, max(cooldown_seconds, 900.0)),
                            "dedicated:roc_acceleration_specialist": (0.58, 1, max(cooldown_seconds, 900.0)),
                        }
                        team_threshold, team_min_votes, team_cooldown = profile_rules.get(
                            team.strategy_profile,
                            (threshold, min_votes, cooldown_seconds),
                        )
                    votes = _annotate_votes(votes, learning, team.key)
                    decision_weights = _effective_weights(learning, team.key)
                    if dedicated_agent:
                        for vote in votes:
                            vote["status"] = "active"
                            vote["research_eligible"] = True
                            vote["weight"] = 1.0
                        decision_weights = {dedicated_agent: 1.0}
                    decision = supervise(votes, team_threshold, team_min_votes, decision_weights)
                    snapshot["agents"] = votes
                    snapshot["supervisor"] = decision
                    snapshot["market"] = {
                        "bid": bid,
                        "ask": ask,
                        "spread": round(spread, 6),
                        "tick_age_seconds": tick_age,
                        "bar_utc": bar_key,
                        "timeframes": (
                            "H1/H4/D1"
                            if team.strategy_profile == "long_term"
                            else "/".join((
                                {"nasdaq_swing": ("M5", "M15", "H1"),
                                 "nasdaq_macro": ("M15", "H1", "H4"),
                                 "nasdaq_pending": ("M15", "H1", "H4"),
                                 "nasdaq_scraper": ("M1", "M5", "M15"),
                                 "dedicated:dual_thrust_specialist": ("M5", "M15", "H1"),
                                 "dedicated:roc_acceleration_specialist": ("M5", "M15", "H1")}
                                .get(
                                    team.strategy_profile,
                                    ("M5", "M15", "H1")
                                    if team.strategy_profile.startswith("dedicated:")
                                    else ("M1", "M5", "M15"),
                                )
                            ))
                        ),
                    }
                    open_shadow = team.key in runtime.get("shadow_positions", {})
                    live_positions = positions_by_magic(team.symbol, team.magic) if team.trade_mode == "live_demo" else []
                    open_live_count = len(live_positions)
                    has_live_capacity = _open_capacity_available(open_live_count, max_open_per_team)
                    snapshot["open_position"] = runtime.get("shadow_positions", {}).get(team.key) if open_shadow else None
                    snapshot["open_positions_count"] = open_live_count + int(open_shadow)
                    snapshot["open_positions"] = []
                    if live_positions:
                        for position in live_positions:
                            position_side = "buy" if int(position.type) == int(mt5.POSITION_TYPE_BUY) else "sell"
                            snapshot["open_positions"].append(
                                {
                                    "ticket": int(position.ticket),
                                    "side": position_side,
                                    "entry": float(position.price_open),
                                    "sl": float(position.sl),
                                    "tp": float(position.tp),
                                    "volume": float(position.volume),
                                    "profit": float(position.profit),
                                }
                            )
                            runtime.setdefault("live_positions", {}).setdefault(
                                str(int(position.ticket)),
                                {
                                    "team": team.key,
                                    "side": position_side,
                                    "entry": float(position.price_open),
                                    "volume": float(position.volume),
                                    "confidence": 0.0,
                                    "votes": [],
                                    "recovered_after_restart": True,
                                },
                            )
                        snapshot["open_position"] = snapshot["open_positions"][0]
                    is_new_bar = runtime.setdefault("last_bars", {}).get(team.key) != bar_key
                    runtime["last_bars"][team.key] = bar_key
                    elapsed = time.time() - float(runtime.setdefault("last_trade_epoch", {}).get(team.key, 0.0) or 0.0)
                    if (
                        decision["decision"] in {"buy", "sell"}
                        and is_new_bar
                        and elapsed >= team_cooldown
                        and not open_shadow
                        and has_live_capacity
                    ):
                        council_approved = True
                        if control["enabled"]:
                            _publish_control_message(
                                control,
                                f"{team.key}_manager",
                                "team_manager",
                                "trade_proposal",
                                f"{team.name} proponuje {decision['decision'].upper()}",
                                team.key,
                                team.symbol,
                                {
                                    "side": decision["decision"],
                                    "confidence": decision["confidence"],
                                    "margin": decision["margin"],
                                    "agent_votes": [
                                        {
                                            "agent": vote.get("agent"),
                                            "side": vote.get("side"),
                                            "confidence": vote.get("confidence"),
                                        }
                                        for vote in votes
                                    ],
                                },
                            )
                            council = _control_review(
                                team,
                                decision,
                                snapshot["stats"],
                                control,
                                control_minimum_confidence,
                                control_conflict_seconds,
                            )
                            snapshot["control"] = council
                            control["approved_entries" if council["approved"] else "blocked_entries"] = (
                                int(control.get("approved_entries" if council["approved"] else "blocked_entries", 0) or 0) + 1
                            )
                            _publish_control_message(
                                control,
                                "chief_coordinator",
                                "control_team",
                                "council_verdict",
                                f"{council['verdict'].upper()}: {team.name}; {council['reason']}",
                                team.key,
                                team.symbol,
                                council,
                            )
                            if not council["approved"]:
                                snapshot["last_action"] = f"CONTROL BLOCK: {council['reason']}"
                                council_approved = False
                        else:
                            snapshot["control"] = {
                                "verdict": "disabled",
                                "approved": True,
                                "reason": "Rada Kontroli wylaczona",
                            }
                        if council_approved:
                            if dedicated_agent:
                                entry, sl, tp = _dedicated_strategy_levels(
                                    team.symbol,
                                    decision["decision"],
                                    dedicated_agent,
                                    entry_frame,
                                    spread,
                                )
                            elif team.strategy_profile == "long_term":
                                entry, sl, tp = _long_term_levels(
                                    team.symbol,
                                    decision["decision"],
                                    entry_frame,
                                    trend_frame,
                                    spread,
                                )
                            else:
                                entry, sl, tp = _levels(
                                    team.symbol,
                                    decision["decision"],
                                    entry_frame,
                                    trend_frame,
                                    spread,
                                )
                            current_account = account_info()
                            trade_volume, sizing = _position_volume(
                                team,
                                decision["decision"],
                                entry,
                                sl,
                                float(current_account.equity),
                                runtime,
                            )
                            snapshot["sizing"] = sizing
                            if team.trade_mode == "live_demo":
                                _open_live(
                                    team,
                                    cfg,
                                    decision,
                                    votes,
                                    entry,
                                    sl,
                                    tp,
                                    runtime,
                                    events_path,
                                    trade_volume,
                                    sizing,
                                )
                            else:
                                _open_shadow(
                                    team,
                                    decision,
                                    votes,
                                    entry,
                                    sl,
                                    tp,
                                    runtime,
                                    events_path,
                                    trade_volume,
                                    sizing,
                                )
                            runtime["last_trade_epoch"][team.key] = time.time()
                            snapshot["last_action"] = f"OPEN {decision['decision'].upper()} {trade_volume:.2f}"
                except Exception as exc:
                    snapshot["error"] = f"{type(exc).__name__}: {exc}"
                    snapshot["supervisor"] = {"decision": "hold", "reason": snapshot["error"]}
                    _append_jsonl(events_path, {"type": "team_error", "team": team.key, "error": snapshot["error"]})
                snapshots.append(snapshot)

            account = account_info()
            payload = {
                "heartbeat_utc": datetime.now(UTC).isoformat(),
                "enabled": _env_bool("AGENT_TEAM_ENABLED", True),
                "account": {
                    "login": int(account.login),
                    "server": str(account.server),
                    "balance": float(account.balance),
                    "equity": float(account.equity),
                    "demo_guard": not allow_live_account,
                },
                "requested_lot": {"xau": xau_lot, "default": default_lot},
                "live_keys": sorted(live_keys),
                "lot_policy": {
                    "mode": str(os.getenv("AGENT_TEAM_LOT_MODE", "fixed") or "fixed"),
                    "risk_per_trade_pct": _env_float("AGENT_TEAM_RISK_PER_TRADE_PCT", 0.10),
                    "dynamic_max_lot": _env_float("AGENT_TEAM_DYNAMIC_MAX_LOT", default_lot),
                    "equity_base_usd": _env_float("AGENT_TEAM_EQUITY_BASE_USD", 1000.0),
                    "equity_base_lot": _env_float("AGENT_TEAM_EQUITY_BASE_LOT", default_lot),
                    "equity_step_usd": _env_float("AGENT_TEAM_EQUITY_STEP_USD", 300.0),
                    "equity_step_lot": _env_float("AGENT_TEAM_EQUITY_STEP_LOT", 0.01),
                    "martingale_enabled": _env_bool("AGENT_TEAM_MARTINGALE_ENABLED", False),
                    "martingale_factor": _env_float("AGENT_TEAM_MARTINGALE_FACTOR", 1.25),
                    "martingale_max_steps": int(_env_float("AGENT_TEAM_MARTINGALE_MAX_STEPS", 1)),
                },
                "supervisor_threshold": threshold,
                "min_directional_votes": min_votes,
                "max_open_per_team": max_open_per_team,
                "concurrent_position_policy": "unlimited" if max_open_per_team == 0 else str(max_open_per_team),
                "long_term_brigade": {
                    "enabled": long_term_enabled,
                    "teams": len(long_term_teams),
                    "symbols": [team.symbol or team.preferred_symbol for team in long_term_teams],
                    "timeframes": "H1/H4/D1",
                    "threshold": long_term_threshold,
                    "min_votes": long_term_min_votes,
                    "cooldown_seconds": long_term_cooldown_seconds,
                    "xau_lot": long_term_xau_lot,
                    "default_lot": long_term_default_lot,
                    "agents": [
                        {"agent": name, "description": AGENT_DESCRIPTIONS[name]}
                        for name in (
                            "long_term_trend",
                            "macro_momentum",
                            "market_regime",
                            "swing_structure",
                            "long_term_risk",
                        )
                    ],
                },
                "agent_descriptions": AGENT_DESCRIPTIONS,
                "learning": _learning_summary(learning),
                "control_team": {
                    **_control_summary(control),
                    "minimum_confidence": control_minimum_confidence,
                    "conflict_window_seconds": control_conflict_seconds,
                },
                "instrument_scout": {
                    **scout,
                    "description": AGENT_DESCRIPTIONS["instrument_scout"],
                    "scan_interval_seconds": scout_scan_seconds,
                    "minimum_score": scout_minimum_score,
                    "max_recruits": scout_max_recruits,
                    "test_lot": "broker_minimum",
                },
                "teams": snapshots,
                "summary": {
                    "teams": len(snapshots),
                    "live_demo": sum(1 for item in snapshots if item["mode"] == "live_demo"),
                    "long_term": sum(1 for item in snapshots if item.get("strategy_profile") == "long_term"),
                    "shadow": sum(1 for item in snapshots if str(item["mode"]).startswith("shadow_")),
                    "open_positions": sum(int(item.get("open_positions_count", 0) or 0) for item in snapshots),
                    "trades": sum(int(item["stats"].get("trades", 0)) for item in snapshots),
                    "wins": sum(int(item["stats"].get("wins", 0)) for item in snapshots),
                    "losses": sum(int(item["stats"].get("losses", 0)) for item in snapshots),
                    "pnl": round(sum(float(item["stats"].get("pnl", 0.0)) for item in snapshots), 2),
                },
            }
            _write_json(status_path, payload)
            _write_json(runtime_path, runtime)
            _write_json(control_path, control)
            time.sleep(loop_seconds)
    finally:
        shutdown()
