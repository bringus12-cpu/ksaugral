from app import research_source_scout as scout


def test_research_sources_include_requested_catalogs() -> None:
    names = {item["name"] for item in scout.RESEARCH_SOURCES}
    assert {"GitHub", "GitLab", "MQL5 Code Base", "Hugging Face", "Kaggle"} <= names


def test_huggingface_scan_deduplicates_and_marks_datasets_testable(monkeypatch) -> None:
    def fake_get(url: str, timeout: int = 20):
        if "/datasets?" in url:
            return [{"id": "owner/xau", "downloads": 10, "likes": 2, "tags": ["finance"]}]
        return [{"id": "owner/model", "downloads": 20, "likes": 1, "tags": ["time-series"]}]

    monkeypatch.setattr(scout, "_get_json", fake_get)
    result = scout.scan_huggingface(per_query=2)
    assert result["count"] == 3
    dataset = next(item for item in result["items"] if item["resource"] == "datasets")
    assert dataset["testable"] is True
    assert result["errors"] == []
