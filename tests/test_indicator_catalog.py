from pathlib import Path

from app import indicator_catalog as catalog


def test_catalog_marks_live_indicators_and_candidates() -> None:
    payload = catalog.indicator_catalog()
    items = payload["bot"]["items"]

    assert payload["bot"]["active_count"] >= 30
    assert any(item["name"].startswith("Bollinger Bands") and item["status"] == "active" for item in items)
    assert any(item["name"].startswith("Parabolic SAR") and item["status"] == "available" for item in items)


def test_mt5_scan_deduplicates_compiled_and_source_files(tmp_path: Path, monkeypatch) -> None:
    indicator_dir = tmp_path / "Terminal-A" / "MQL5" / "Indicators" / "Examples"
    indicator_dir.mkdir(parents=True)
    (indicator_dir / "MACD.ex5").write_bytes(b"compiled")
    (indicator_dir / "MACD.mq5").write_text("// source", encoding="utf-8")
    (indicator_dir / "RSI.ex5").write_bytes(b"compiled")
    monkeypatch.setattr(catalog, "_indicator_roots", lambda: [tmp_path])

    result = catalog.scan_mt5_indicators()

    assert result["file_count"] == 3
    assert result["unique_count"] == 2
    macd = next(item for item in result["indicators"] if item["name"] == "MACD")
    assert macd["formats"] == ["ex5", "mq5"]
    assert macd["copies"] == 2
    assert result["terminals"] == [{"path": str(tmp_path / "Terminal-A"), "indicator_count": 2}]
