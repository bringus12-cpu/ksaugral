from __future__ import annotations

import gzip
import hashlib
import json

import pandas as pd
import pytest

from app.historical_data import load_xau_history


def _archive(tmp_path):
    data_path = tmp_path / "xau_m1.csv.gz"
    with gzip.open(data_path, "wt", encoding="utf-8", newline="") as stream:
        stream.write("time,open,high,low,close\n")
        stream.write("2026-01-02T00:01:00Z,1,2,0,1.5\n")
    manifest = {
        "timeframes": {
            "M1": {
                "file": data_path.name,
                "sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
            }
        }
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return data_path


def test_load_xau_history_validates_and_parses_time(tmp_path):
    _archive(tmp_path)

    frame = load_xau_history("m1", tmp_path)

    assert len(frame) == 1
    assert str(frame.loc[0, "time"]) == "2026-01-02 00:01:00+00:00"


def test_load_xau_history_rejects_modified_archive(tmp_path):
    data_path = _archive(tmp_path)
    data_path.write_bytes(data_path.read_bytes() + b"changed")

    with pytest.raises(ValueError, match="Checksum mismatch"):
        load_xau_history("M1", tmp_path)
