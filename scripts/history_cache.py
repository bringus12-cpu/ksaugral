from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_cached_rates(
    history_dir: str | Path,
    timeframe: str,
    start=None,
    end=None,
) -> pd.DataFrame:
    directory = Path(history_dir)
    matches = sorted(directory.glob(f"*_{str(timeframe).lower()}_300s.csv.gz"))
    if not matches:
        raise FileNotFoundError(
            f"No cached {timeframe} history in {directory}"
        )
    frame = pd.read_csv(matches[0], compression="gzip")
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    if start is not None:
        frame = frame[frame["time"] >= pd.Timestamp(start)]
    if end is not None:
        frame = frame[frame["time"] <= pd.Timestamp(end)]
    return frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)
