from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


RESEARCH_SOURCES = (
    {"name": "GitHub", "url": "https://github.com/topics/algorithmic-trading", "kind": "repositories"},
    {"name": "GitLab", "url": "https://gitlab.com/explore/projects?name=trading", "kind": "repositories"},
    {"name": "MQL5 Code Base", "url": "https://www.mql5.com/en/code/mt5", "kind": "MQL5 source"},
    {"name": "TradingView Community Scripts", "url": "https://www.tradingview.com/scripts/", "kind": "Pine strategies"},
    {"name": "QuantConnect Community", "url": "https://www.quantconnect.com/forum/", "kind": "algorithms and backtests"},
    {"name": "ProRealCode", "url": "https://www.prorealcode.com/prorealtime-trading-strategies/", "kind": "ProRealTime strategies"},
    {"name": "SourceForge Finance", "url": "https://sourceforge.net/directory/financial/", "kind": "tools and older projects"},
    {"name": "Hugging Face", "url": "https://huggingface.co/", "kind": "models, datasets and spaces"},
    {"name": "Kaggle", "url": "https://www.kaggle.com/search?q=xauusd", "kind": "datasets and notebooks"},
)


def _get_json(url: str, timeout: int = 20) -> Any:
    request = Request(url, headers={"User-Agent": "xao-graal-research-scout/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(3_000_000).decode("utf-8"))


def scan_huggingface(per_query: int = 8) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    for resource in ("models", "datasets", "spaces"):
        for query in ("xauusd", "financial time series trading"):
            url = f"https://huggingface.co/api/{resource}?search={quote(query)}&limit={per_query}&full=true"
            try:
                payload = _get_json(url)
            except Exception as exc:
                errors.append(f"{resource}/{query}: {type(exc).__name__}: {exc}")
                continue
            for item in payload if isinstance(payload, list) else []:
                item_id = str(item.get("id") or item.get("modelId") or "")
                if not item_id:
                    continue
                tags = [str(tag) for tag in item.get("tags", [])][:30]
                rows.append(
                    {
                        "provider": "huggingface",
                        "resource": resource,
                        "id": item_id,
                        "url": f"https://huggingface.co/{'datasets/' if resource == 'datasets' else 'spaces/' if resource == 'spaces' else ''}{item_id}",
                        "downloads": int(item.get("downloads", 0) or 0),
                        "likes": int(item.get("likes", 0) or 0),
                        "updated": str(item.get("lastModified") or ""),
                        "tags": tags,
                        "status": "discovered",
                        "testable": resource == "datasets",
                    }
                )
    deduped = {f"{item['resource']}:{item['id']}": item for item in rows}
    ranked = sorted(deduped.values(), key=lambda item: (int(item["downloads"]), int(item["likes"])), reverse=True)
    return {
        "generated_utc": datetime.now(UTC).isoformat(),
        "items": ranked,
        "count": len(ranked),
        "errors": errors,
        "policy": "metadata only; model weights and code are not executed by the scout",
    }


def research_source_report(per_query: int = 8) -> dict[str, object]:
    return {
        "generated_utc": datetime.now(UTC).isoformat(),
        "sources": list(RESEARCH_SOURCES),
        "huggingface": scan_huggingface(per_query),
    }
