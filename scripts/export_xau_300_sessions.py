from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mt5_gateway import Mt5Credentials, connect, ensure_symbol, mt5, shutdown


TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def _credentials() -> Mt5Credentials:
    return Mt5Credentials(
        login=int(os.environ["MT5_LOGIN"]),
        password=os.environ["MT5_PASSWORD"],
        server=os.environ["MT5_SERVER"],
        path=os.getenv("MT5_PATH") or None,
    )


def _frame(raw: Any) -> pd.DataFrame:
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    result = pd.DataFrame(raw)
    result["time"] = pd.to_datetime(result["time"], unit="s", utc=True)
    return result.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def _completed_sessions(symbol: str, count: int) -> pd.DataFrame:
    raw = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, count + 150)
    daily = _frame(raw)
    if daily.empty:
        raise RuntimeError(f"MT5 did not return D1 history: {mt5.last_error()}")
    today = datetime.now(UTC).date()
    daily = daily[daily["time"].dt.date < today].tail(count).reset_index(drop=True)
    if len(daily) != count:
        raise RuntimeError(f"Expected {count} completed sessions, received {len(daily)}")
    return daily


def _rates(symbol: str, timeframe: int, start: datetime, end: datetime) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=7))
        chunk = _frame(mt5.copy_rates_range(symbol, timeframe, cursor, chunk_end))
        if not chunk.empty:
            parts.append(chunk)
        cursor = chunk_end
    if not parts:
        raise RuntimeError(f"MT5 did not return rates: {mt5.last_error()}")
    result = pd.concat(parts, ignore_index=True)
    result = result.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    return result[(result["time"] >= start) & (result["time"] < end)].reset_index(drop=True)


def _session_coverage(frame: pd.DataFrame, sessions: pd.DataFrame) -> dict[str, Any]:
    expected = {stamp.date() for stamp in sessions["time"]}
    actual = set(frame["time"].dt.date)
    missing = sorted(expected - actual)
    return {
        "expected_sessions": len(expected),
        "covered_sessions": len(expected & actual),
        "missing_sessions": [value.isoformat() for value in missing],
    }


def _write_csv_gz(frame: pd.DataFrame, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    export = frame.copy()
    export["time"] = export["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        export.to_csv(stream, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _svg_chart(daily: pd.DataFrame, path: Path) -> None:
    width, height = 1800, 820
    left, right, top, bottom = 90, 35, 70, 90
    plot_w = width - left - right
    plot_h = height - top - bottom
    lows = daily["low"].astype(float)
    highs = daily["high"].astype(float)
    y_min = math.floor(float(lows.min()) / 50.0) * 50.0
    y_max = math.ceil(float(highs.max()) / 50.0) * 50.0
    span = max(1.0, y_max - y_min)

    def x(index: int) -> float:
        return left + (index + 0.5) * plot_w / len(daily)

    def y(price: float) -> float:
        return top + (y_max - price) / span * plot_h

    body_w = max(1.8, plot_w / len(daily) * 0.68)
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#0b1118"/>',
        '<text x="90" y="38" fill="#f2f5f7" font-family="Segoe UI,Arial" font-size="24" font-weight="700">XAUUSD - 300 zakonczonych sesji</text>',
        f'<text x="90" y="61" fill="#91a0ad" font-family="Segoe UI,Arial" font-size="14">{daily.time.iloc[0].date()} - {daily.time.iloc[-1].date()} | D1 | dane brokera MT5</text>',
    ]
    for step in range(9):
        price = y_min + span * step / 8
        yy = y(price)
        lines.append(f'<line x1="{left}" y1="{yy:.2f}" x2="{width-right}" y2="{yy:.2f}" stroke="#26313b" stroke-width="1"/>')
        lines.append(f'<text x="{left-10}" y="{yy+5:.2f}" text-anchor="end" fill="#91a0ad" font-family="Segoe UI,Arial" font-size="13">{price:.0f}</text>')
    for idx in range(0, len(daily), 25):
        xx = x(idx)
        label = daily.iloc[idx]["time"].strftime("%Y-%m-%d")
        lines.append(f'<line x1="{xx:.2f}" y1="{top}" x2="{xx:.2f}" y2="{height-bottom}" stroke="#1c2630" stroke-width="1"/>')
        lines.append(f'<text x="{xx:.2f}" y="{height-bottom+28}" text-anchor="middle" fill="#91a0ad" font-family="Segoe UI,Arial" font-size="12">{label}</text>')
    for idx, row in daily.iterrows():
        xx = x(idx)
        open_price = float(row["open"])
        close_price = float(row["close"])
        color = "#20b486" if close_price >= open_price else "#ef5b5b"
        high_y, low_y = y(float(row["high"])), y(float(row["low"]))
        body_top = min(y(open_price), y(close_price))
        body_h = max(1.0, abs(y(open_price) - y(close_price)))
        lines.append(f'<line x1="{xx:.2f}" y1="{high_y:.2f}" x2="{xx:.2f}" y2="{low_y:.2f}" stroke="{color}" stroke-width="1.2"/>')
        lines.append(f'<rect x="{xx-body_w/2:.2f}" y="{body_top:.2f}" width="{body_w:.2f}" height="{body_h:.2f}" fill="{color}"/>')
    lines.append(f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="none" stroke="#52616f" stroke-width="1"/>')
    lines.append('</svg>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export complete XAU history for reproducible backtests.")
    parser.add_argument("--env", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=300)
    parser.add_argument("--output", default="data_vantage/history/xau_300_sessions")
    args = parser.parse_args()

    load_dotenv(Path(args.env), override=True)
    connect(_credentials())
    try:
        symbol = ensure_symbol(os.getenv("GOLD_SYMBOL", "XAUUSD"))
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        sessions = _completed_sessions(symbol, args.sessions)
        start = sessions["time"].iloc[0].to_pydatetime()
        end = (sessions["time"].iloc[-1] + pd.Timedelta(days=1)).to_pydatetime()
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)

        metadata: dict[str, Any] = {
            "generated_utc": datetime.now(UTC).isoformat(),
            "broker": getattr(account, "company", None),
            "server": getattr(account, "server", None),
            "account_type": "demo" if "demo" in str(getattr(account, "server", "")).lower() else "live",
            "symbol": symbol,
            "requested_completed_sessions": args.sessions,
            "first_session_utc": start.isoformat(),
            "last_session_utc": sessions["time"].iloc[-1].isoformat(),
            "terminal_max_bars": getattr(terminal, "maxbars", None),
            "timeframes": {},
        }

        frames: dict[str, pd.DataFrame] = {}
        for name, value in TIMEFRAMES.items():
            frame = _rates(symbol, value, start, end)
            coverage = _session_coverage(frame, sessions)
            if coverage["covered_sessions"] != args.sessions:
                raise RuntimeError(
                    f"{name} history is incomplete: {coverage['covered_sessions']}/{args.sessions} sessions. "
                    "Increase MT5 MaxBars and restart the terminal before exporting."
                )
            path = output / f"{symbol.lower().replace('+', '_plus')}_{name.lower()}_{args.sessions}s.csv.gz"
            digest = _write_csv_gz(frame, path)
            frames[name] = frame
            metadata["timeframes"][name] = {
                **coverage,
                "bars": len(frame),
                "first_bar_utc": frame["time"].iloc[0].isoformat(),
                "last_bar_utc": frame["time"].iloc[-1].isoformat(),
                "file": path.name,
                "sha256": digest,
            }

        chart_path = output / f"{symbol.lower().replace('+', '_plus')}_d1_{args.sessions}s_chart.svg"
        _svg_chart(frames["D1"], chart_path)
        metadata["chart"] = chart_path.name
        metadata_path = output / "manifest.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(metadata, indent=2, ensure_ascii=False))
    finally:
        shutdown()


if __name__ == "__main__":
    main()
