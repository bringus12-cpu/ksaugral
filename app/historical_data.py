from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_XAU_ARCHIVE = (
    Path(__file__).resolve().parent.parent
    / "data_vantage"
    / "history"
    / "xau_300_sessions"
)


def load_manifest(archive: str | Path = DEFAULT_XAU_ARCHIVE) -> dict[str, Any]:
    archive_path = Path(archive)
    manifest_path = archive_path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Historical-data manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def load_xau_history(
    timeframe: str = "M1",
    archive: str | Path = DEFAULT_XAU_ARCHIVE,
    *,
    verify_checksum: bool = True,
) -> pd.DataFrame:
    archive_path = Path(archive)
    manifest = load_manifest(archive_path)
    key = timeframe.strip().upper()
    details = manifest.get("timeframes", {}).get(key)
    if not details:
        available = ", ".join(sorted(manifest.get("timeframes", {})))
        raise ValueError(f"Timeframe {key!r} is unavailable; choose one of: {available}")

    data_path = archive_path / str(details["file"])
    if verify_checksum:
        digest = hashlib.sha256(data_path.read_bytes()).hexdigest()
        if digest != details.get("sha256"):
            raise ValueError(f"Checksum mismatch for historical data: {data_path}")

    with gzip.open(data_path, "rt", encoding="utf-8", newline="") as stream:
        frame = pd.read_csv(stream)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)
