from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import os


BOT_INDICATORS: tuple[dict[str, object], ...] = (
    {"name": "EMA 20/50/200", "category": "Trend", "status": "active", "modules": ["Autonomous", "Scalper", "Strategy Lab"], "purpose": "Kierunek trendu, pullback i filtr wyzszego interwalu."},
    {"name": "SMA 20/50", "category": "Trend", "status": "active", "modules": ["Strategy Lab"], "purpose": "Trend bazowy i odchylenie ceny od sredniej."},
    {"name": "WMA / DEMA / TEMA / HMA", "category": "Trend", "status": "active", "modules": ["Strategy Lab"], "purpose": "Szybsze srednie i przecięcia kierunkowe."},
    {"name": "KAMA", "category": "Trend", "status": "active", "modules": ["Strategy Lab"], "purpose": "Adaptacyjny trend zależny od efektywnosci ruchu."},
    {"name": "VWAP / VWMA", "category": "Trend i wolumen", "status": "active", "modules": ["Scalper", "Strategy Lab"], "purpose": "Cena godziwa sesji, reclaim i trend wsparty wolumenem."},
    {"name": "ADX 14", "category": "Sila trendu", "status": "active", "modules": ["Scalper", "Autonomous"], "purpose": "Odróżnia trend od konsolidacji i filtruje wybicia."},
    {"name": "Supertrend", "category": "Trend", "status": "active", "modules": ["Strategy Lab"], "purpose": "Zmiana i kontynuacja kierunku z buforem ATR."},
    {"name": "Ichimoku", "category": "Trend", "status": "active", "modules": ["Strategy Lab"], "purpose": "Tenkan, Kijun i polozenie ceny wzgledem chmury."},
    {"name": "Aroon", "category": "Trend", "status": "active", "modules": ["Strategy Lab"], "purpose": "Sila i swiezosc ekstremow trendu."},
    {"name": "Parabolic SAR", "category": "Trend", "status": "available", "modules": [], "purpose": "Kandydat do kroczacego SL; jeszcze nie steruje live."},
    {"name": "ATR / NATR", "category": "Zmiennosc", "status": "active", "modules": ["Telegram", "Scalper", "Autonomous"], "purpose": "Odleglosc SL/TP, spread do zmiennosci i normalizacja ryzyka."},
    {"name": "Bollinger Bands", "category": "Zmiennosc", "status": "active", "modules": ["BBKELT", "DB60", "Strategy Lab"], "purpose": "Reclaim, wybicie, mean reversion i szerokosc rynku."},
    {"name": "Keltner Channel", "category": "Zmiennosc", "status": "active", "modules": ["BBKELT"], "purpose": "Squeeze Bollinger-Keltner i wybicie po kompresji."},
    {"name": "Donchian Channel", "category": "Wybicie", "status": "active", "modules": ["Autonomous", "Strategy Lab"], "purpose": "Ekstrema 20 swiec i wybicia zakresu."},
    {"name": "Standard Deviation / realized volatility", "category": "Zmiennosc", "status": "active", "modules": ["Strategy Lab"], "purpose": "Pomiar rozrzutu i rezimu zmiennosci."},
    {"name": "Bollinger %B / width", "category": "Zmiennosc", "status": "active", "modules": ["Scalper"], "purpose": "Polozenie ceny w pasmie oraz kompresja/ekspansja."},
    {"name": "RSI 14", "category": "Oscylator", "status": "active", "modules": ["Scalper", "Autonomous"], "purpose": "Momentum, skrajnosc i potwierdzenie odwrocenia."},
    {"name": "MACD 12/26/9 + fast/slow", "category": "Momentum", "status": "active", "modules": ["BBKELT", "DB60", "Strategy Lab"], "purpose": "Przeciecia, histogram i potwierdzenie impulsu."},
    {"name": "Stochastic", "category": "Oscylator", "status": "active", "modules": ["Scalper", "Strategy Lab"], "purpose": "Przeciecia K/D w skrajnych strefach."},
    {"name": "CCI", "category": "Oscylator", "status": "active", "modules": ["Strategy Lab"], "purpose": "Powrot z ekstremum i cykl ceny."},
    {"name": "Williams %R", "category": "Oscylator", "status": "active", "modules": ["Strategy Lab"], "purpose": "Wykupienie, wyprzedanie i powrot do zakresu."},
    {"name": "MFI", "category": "Momentum i wolumen", "status": "active", "modules": ["Strategy Lab"], "purpose": "Momentum ważone wolumenem tickowym."},
    {"name": "Ultimate Oscillator", "category": "Oscylator", "status": "active", "modules": ["Strategy Lab"], "purpose": "Momentum na trzech horyzontach."},
    {"name": "CMO", "category": "Momentum", "status": "active", "modules": ["Strategy Lab"], "purpose": "Symetryczny pomiar przewagi wzrostow lub spadkow."},
    {"name": "ROC / Momentum", "category": "Momentum", "status": "active", "modules": ["Strategy Lab"], "purpose": "Tempo zmiany i przyspieszenie ceny."},
    {"name": "PPO", "category": "Momentum", "status": "active", "modules": ["Strategy Lab"], "purpose": "Procentowy odpowiednik MACD."},
    {"name": "TRIX", "category": "Momentum", "status": "active", "modules": ["Strategy Lab"], "purpose": "Wygladzone momentum i zmiana trendu."},
    {"name": "KST", "category": "Momentum", "status": "active", "modules": ["Strategy Lab"], "purpose": "Wielohoryzontowe tempo zmian."},
    {"name": "Awesome Oscillator", "category": "Momentum", "status": "active", "modules": ["Strategy Lab"], "purpose": "Impuls oparty o mediane ceny."},
    {"name": "Fisher Transform", "category": "Oscylator", "status": "active", "modules": ["Strategy Lab"], "purpose": "Detekcja ekstremow i odwrocen."},
    {"name": "OBV", "category": "Wolumen", "status": "active", "modules": ["Strategy Lab"], "purpose": "Potwierdzenie trendu przeplywem wolumenu."},
    {"name": "Chaikin Money Flow", "category": "Wolumen", "status": "active", "modules": ["Strategy Lab"], "purpose": "Presja kupna i sprzedazy w oknie 20 swiec."},
    {"name": "A/D Line + Chaikin Oscillator", "category": "Wolumen", "status": "active", "modules": ["Strategy Lab"], "purpose": "Akumulacja/dystrybucja i zmiana przeplywu."},
    {"name": "Force Index", "category": "Wolumen", "status": "active", "modules": ["Strategy Lab"], "purpose": "Sila ruchu ceny ważona tick volume."},
    {"name": "Balance of Power", "category": "Price action", "status": "active", "modules": ["Strategy Lab"], "purpose": "Przewaga kupujacych lub sprzedajacych w swiecy."},
    {"name": "Choppiness Index", "category": "Rezim", "status": "active", "modules": ["Strategy Lab"], "purpose": "Rozpoznanie trendu i rynku bocznego."},
    {"name": "Efficiency Ratio", "category": "Rezim", "status": "active", "modules": ["Strategy Lab"], "purpose": "Jak kierunkowy jest ruch wzgledem szumu."},
    {"name": "Linear Regression Slope", "category": "Trend", "status": "active", "modules": ["Strategy Lab"], "purpose": "Nachylenie trendu bez korzystania z przyszlych swiec."},
    {"name": "Z-score", "category": "Statystyka", "status": "active", "modules": ["Strategy Lab"], "purpose": "Odchylenie ceny od sredniej dla mean reversion."},
    {"name": "Fractals / ZigZag", "category": "Struktura", "status": "candidate", "modules": [], "purpose": "Kandydat do swingow i struktury; wymaga ochrony przed repaintingiem."},
    {"name": "Market Profile", "category": "Wolumen", "status": "candidate", "modules": [], "purpose": "Kandydat do value area i stref akceptacji ceny."},
)


def _indicator_roots() -> list[Path]:
    roots: list[Path] = []
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal",
        Path.home() / "MT5",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
    ]
    for root in candidates:
        if root.exists() and root not in roots:
            roots.append(root)
    return roots


def scan_mt5_indicators() -> dict[str, object]:
    grouped: dict[str, dict[str, object]] = {}
    terminals: defaultdict[str, set[str]] = defaultdict(set)
    file_count = 0
    for root in _indicator_roots():
        try:
            files = root.rglob("*")
            for path in files:
                if not path.is_file() or path.suffix.lower() not in {".ex5", ".mq5"}:
                    continue
                normalized = str(path).replace("/", "\\")
                if "\\MQL5\\Indicators\\" not in normalized:
                    continue
                file_count += 1
                marker = normalized.split("\\MQL5\\Indicators\\", 1)
                terminal = marker[0]
                relative = marker[1]
                name = path.stem
                key = name.casefold()
                item = grouped.setdefault(
                    key,
                    {"name": name, "formats": set(), "locations": set(), "copies": 0},
                )
                item["formats"].add(path.suffix.lower().lstrip("."))
                item["locations"].add(relative)
                item["copies"] = int(item["copies"]) + 1
                terminals[terminal].add(name)
        except (OSError, PermissionError):
            continue

    indicators = []
    for item in sorted(grouped.values(), key=lambda value: str(value["name"]).casefold()):
        indicators.append(
            {
                "name": item["name"],
                "formats": sorted(item["formats"]),
                "locations": sorted(item["locations"])[:6],
                "copies": item["copies"],
            }
        )
    terminal_rows = [
        {"path": path, "indicator_count": len(names)}
        for path, names in sorted(terminals.items(), key=lambda pair: pair[0].casefold())
    ]
    return {
        "file_count": file_count,
        "unique_count": len(indicators),
        "terminals": terminal_rows,
        "indicators": indicators,
    }


def indicator_catalog() -> dict[str, object]:
    active = sum(1 for item in BOT_INDICATORS if item["status"] == "active")
    return {
        "bot": {"active_count": active, "items": list(BOT_INDICATORS)},
        "mt5": scan_mt5_indicators(),
        "notes": [
            "Wskaznik znaleziony w terminalu nie jest automatycznie aktywny w bocie.",
            "Nowy wskaznik powinien przejsc test out-of-sample, walk-forward i test kosztow transakcyjnych.",
            "ZigZag i podobne narzedzia moga zmieniac historyczne punkty, dlatego nie wolno testowac ich naiwnie.",
        ],
    }
