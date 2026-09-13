from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path


class BotStateStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.status_path = data_dir / "status.json"
        self.decisions_path = data_dir / "decisions.jsonl"
        self.trades_path = data_dir / "trades.csv"
        self.meta_path = data_dir / "position_meta.json"
        self.runtime_path = data_dir / "runtime.json"
        self._ensure_trades_file()

    def _ensure_trades_file(self) -> None:
        if self.trades_path.exists():
            return
        with self.trades_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "closed_at_utc",
                    "ticket",
                    "symbol",
                    "side",
                    "volume",
                    "entry_price",
                    "exit_price",
                    "profit",
                    "reason",
                    "strategy",
                ],
            )
            writer.writeheader()

    def load_runtime(self) -> dict:
        if not self.runtime_path.exists():
            return {}
        return json.loads(self.runtime_path.read_text(encoding="utf-8"))

    def save_runtime(self, payload: dict) -> None:
        self.runtime_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load_meta(self) -> dict:
        if not self.meta_path.exists():
            return {}
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def save_meta(self, payload: dict) -> None:
        self.meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def write_status(self, payload: dict) -> None:
        self.status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def append_decision(self, payload: dict) -> None:
        event = {"timestamp_utc": datetime.now(UTC).isoformat(), **payload}
        with self.decisions_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True) + "\n")

    def append_trade(self, payload: dict) -> None:
        row = {"closed_at_utc": datetime.now(UTC).isoformat(), **payload}
        with self.trades_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
            writer.writerow(row)

    @staticmethod
    def serialize_position(position) -> dict:
        return {
            "ticket": int(position.ticket),
            "symbol": position.symbol,
            "type": int(position.type),
            "volume": float(position.volume),
            "price_open": float(position.price_open),
            "price_current": float(position.price_current),
            "sl": float(position.sl or 0.0),
            "tp": float(position.tp or 0.0),
            "profit": float(position.profit),
            "swap": float(position.swap),
            "comment": str(getattr(position, "comment", "")),
            "time": int(position.time),
        }
