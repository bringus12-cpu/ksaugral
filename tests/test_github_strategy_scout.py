from datetime import UTC, datetime

from app.github_strategy_scout import extract_concepts, markdown_report, score_repository


def test_extract_concepts_maps_only_to_local_strategies() -> None:
    concepts, strategies = extract_concepts("Bollinger RSI mean reversion with CMF and VWAP confirmation")
    assert "bollinger" in concepts
    assert "cmf_flow_specialist" in strategies
    assert "vwap_reclaim_specialist" in strategies


def test_repository_without_known_license_is_not_safe() -> None:
    result = score_repository(
        {
            "full_name": "owner/repo",
            "html_url": "https://github.com/owner/repo",
            "stargazers_count": 1000,
            "updated_at": "2026-08-01T00:00:00Z",
            "license": None,
        },
        "Backtest RSI and MACD with pytest",
        now=datetime(2026, 8, 23, tzinfo=UTC),
    )
    assert result["safe_for_review"] is False
    assert result["remote_code_executed"] is False


def test_markdown_report_contains_discovery_and_backtest() -> None:
    text = markdown_report(
        {
            "generated_utc": "2026-08-23T00:00:00Z",
            "discovery": {
                "repositories_found": 1,
                "repositories_inspected": 1,
                "approved_for_concept_research": 1,
                "mapped_local_strategies": ["cmf_flow_specialist"],
                "repositories": [
                    {
                        "full_name": "owner/repo",
                        "url": "https://github.com/owner/repo",
                        "score": 70,
                        "stars": 100,
                        "license": "mit",
                        "concepts": ["cmf"],
                    }
                ],
                "errors": [],
            },
            "backtest": {"sessions": 60, "portfolio": {"trades": 10, "pnl": 5, "profit_factor": 1.2}},
            "analytics": {"stable_pairs": ["DJ30::cmf_flow_specialist"]},
            "errors": [],
        }
    )
    assert "owner/repo" in text
    assert "DJ30::cmf_flow_specialist" in text
