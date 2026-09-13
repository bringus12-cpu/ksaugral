from __future__ import annotations

import base64
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


GITHUB_API = "https://api.github.com"
SAFE_LICENSES = {
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "gpl-2.0",
    "gpl-3.0",
    "lgpl-2.1",
    "lgpl-3.0",
    "mit",
    "mpl-2.0",
}

DEFAULT_QUERIES = (
    "algorithmic trading backtest strategy language:Python stars:>20 archived:false",
    "forex trading bot MetaTrader5 language:Python stars:>5 archived:false",
    "walk forward trading strategy language:Python stars:>5 archived:false",
    "quant trading indicators portfolio language:Python stars:>20 archived:false",
    "gold XAUUSD strategy backtest archived:false",
    "swing trading strategy language:Python stars:>5 archived:false",
)

# Remote code is never executed. Concepts are mapped to implementations that
# already live in our reviewed strategy laboratory.
CONCEPT_STRATEGIES: dict[str, tuple[str, ...]] = {
    "fair value gap": ("liquidity_specialist",),
    "fvg": ("liquidity_specialist",),
    "order block": ("liquidity_specialist", "pullback_specialist"),
    "liquidity sweep": ("liquidity_specialist",),
    "market structure": ("liquidity_specialist", "breakout_specialist"),
    "breakout retest": ("breakout_specialist", "pullback_specialist"),
    "session breakout": ("breakout_specialist", "dual_thrust_specialist"),
    "london session": ("breakout_specialist", "dual_thrust_specialist"),
    "new york session": ("breakout_specialist", "dual_thrust_specialist"),
    "xgboost": (),
    "hidden markov": (),
    "bollinger": ("bollinger_rsi_specialist", "squeeze_release_specialist"),
    "breakout": ("breakout_specialist", "dual_thrust_specialist"),
    "cci": ("cci_reversal_specialist",),
    "chaikin money flow": ("cmf_flow_specialist",),
    "cmf": ("cmf_flow_specialist",),
    "donchian": ("dual_thrust_specialist",),
    "fisher": ("fisher_reversal_specialist",),
    "ichimoku": ("ichimoku_specialist",),
    "linear regression": ("linreg_pullback_specialist",),
    "macd": ("macd_ema_specialist",),
    "mean reversion": ("mean_reversion",),
    "mfi": ("mfi_reversal_specialist",),
    "momentum": ("momentum", "roc_acceleration_specialist"),
    "obv": ("obv_confirmation_specialist",),
    "pullback": ("pullback_specialist", "linreg_pullback_specialist"),
    "rsi": ("bollinger_rsi_specialist", "mean_reversion"),
    "stochastic": ("stochastic_cross_specialist",),
    "supertrend": ("supertrend_specialist",),
    "vwap": ("vwap_reclaim_specialist",),
    "williams": ("williams_reversal_specialist",),
}


@dataclass(frozen=True)
class ScanConfig:
    per_query: int = 8
    max_repositories: int = 20
    timeout_seconds: int = 20
    minimum_score: float = 45.0


class GitHubClient:
    def __init__(self, token: str = "", timeout_seconds: int = 20) -> None:
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds

    def _json(self, path: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "xao-graal-strategy-scout/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{GITHUB_API}{path}", headers=headers)
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def search(self, query: str, per_page: int) -> list[dict[str, Any]]:
        payload = self._json(
            f"/search/repositories?q={quote(query)}&sort=stars&order=desc&per_page={per_page}"
        )
        return list(payload.get("items", []))

    def readme(self, full_name: str) -> str:
        try:
            payload = self._json(f"/repos/{full_name}/readme")
        except HTTPError as exc:
            if exc.code == 404:
                return ""
            raise
        encoded = str(payload.get("content", "")).replace("\n", "")
        if not encoded:
            return ""
        return base64.b64decode(encoded).decode("utf-8", errors="replace")[:200_000]


class GitLabClient:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def _json(self, path: str):
        request = Request("https://gitlab.com/api/v4" + path,
                          headers={"User-Agent": "xao-graal-strategy-scout/1.0"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read(2_000_000).decode("utf-8"))

    def search(self, query: str, per_page: int) -> list[dict[str, Any]]:
        rows = self._json(f"/projects?search={quote(query)}&simple=true&visibility=public&per_page={per_page}")
        return [{"full_name": "gitlab:" + row["path_with_namespace"],
                 "html_url": row["web_url"], "description": row.get("description"),
                 "stargazers_count": row.get("star_count", 0), "updated_at": row.get("last_activity_at"),
                 "archived": row.get("archived", False), "license": None,
                 "gitlab_id": row["id"], "provider": "gitlab"} for row in rows]

    def readme(self, repo: dict[str, Any]) -> str:
        detail = self._json(f"/projects/{repo['gitlab_id']}?license=true")
        license_info = detail.get("license") or {}
        repo["license"] = {"spdx_id": license_info.get("key", "")}
        branch = detail.get("default_branch")
        if not branch:
            return ""
        for name in ("README.md", "README.rst", "README"):
            try:
                payload = self._json(f"/projects/{repo['gitlab_id']}/repository/files/{quote(name, safe='')}?ref={quote(branch, safe='')}")
                return base64.b64decode(payload.get("content", "")).decode("utf-8", errors="replace")[:200_000]
            except HTTPError as exc:
                if exc.code != 404:
                    raise
        return ""


def extract_concepts(text: str) -> tuple[list[str], list[str]]:
    lowered = re.sub(r"\s+", " ", text.lower())
    concepts: list[str] = []
    strategies: set[str] = set()
    for concept, mapped in CONCEPT_STRATEGIES.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(concept)}(?![a-z0-9])", lowered):
            concepts.append(concept)
            strategies.update(mapped)
    return sorted(concepts), sorted(strategies)


def score_repository(repo: dict[str, Any], readme: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    license_id = str((repo.get("license") or {}).get("spdx_id", "")).lower()
    stars = max(0, int(repo.get("stargazers_count", 0) or 0))
    updated_raw = str(repo.get("pushed_at") or repo.get("updated_at") or "")
    try:
        updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
        age_days = max(0, (now - updated.astimezone(UTC)).days)
    except ValueError:
        age_days = 10_000
    combined = f"{repo.get('description') or ''}\n{readme}"
    concepts, strategies = extract_concepts(combined)
    lowered = combined.lower()

    score = min(30.0, 5.0 * math.log10(stars + 1.0))
    score += 20.0 if license_id in SAFE_LICENSES else -25.0
    score += 15.0 if age_days <= 180 else 8.0 if age_days <= 730 else -5.0
    score += 8.0 if any(word in lowered for word in ("pytest", "unit test", "test suite", "ci/")) else 0.0
    score += 8.0 if any(word in lowered for word in ("backtest", "walk-forward", "walk forward")) else 0.0
    score += 6.0 if any(word in lowered for word in ("risk management", "stop loss", "position sizing")) else 0.0
    score += min(12.0, 2.0 * len(concepts))
    score -= 12.0 if bool(repo.get("fork")) else 0.0
    score -= 30.0 if bool(repo.get("archived")) else 0.0

    return {
        "full_name": str(repo.get("full_name", "")),
        "url": str(repo.get("html_url", "")),
        "description": str(repo.get("description") or ""),
        "stars": stars,
        "license": license_id or "missing",
        "age_days": age_days,
        "score": round(score, 2),
        "concepts": concepts,
        "mapped_local_strategies": strategies,
        "safe_for_review": license_id in SAFE_LICENSES and not bool(repo.get("archived")),
        "remote_code_executed": False,
    }


def scan_repositories(
    config: ScanConfig = ScanConfig(),
    queries: tuple[str, ...] = DEFAULT_QUERIES,
    cache_path: Path | None = None,
    include_gitlab: bool = False,
) -> dict[str, Any]:
    client = GitHubClient(os.getenv("GITHUB_TOKEN", ""), config.timeout_seconds)
    found: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for query in queries:
        try:
            for repo in client.search(query, config.per_query):
                full_name = str(repo.get("full_name", ""))
                if full_name:
                    found[full_name] = repo
        except (HTTPError, URLError, TimeoutError) as exc:
            errors.append(f"search {query!r}: {type(exc).__name__}: {exc}")
        time.sleep(0.3)

    gitlab = GitLabClient(config.timeout_seconds)
    if include_gitlab:
        for query in ("trading bot", "backtest", "forex"):
            try:
                for repo in gitlab.search(query, config.per_query):
                    found[repo["full_name"]] = repo
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                errors.append(f"gitlab search {query!r}: {type(exc).__name__}: {exc}")

    ranked_source = sorted(
        found.values(),
        key=lambda item: int(item.get("stargazers_count", 0) or 0),
        reverse=True,
    )[: config.max_repositories]
    repositories: list[dict[str, Any]] = []
    for repo in ranked_source:
        full_name = str(repo.get("full_name", ""))
        try:
            readme = gitlab.readme(repo) if repo.get("provider") == "gitlab" else client.readme(full_name)
            repositories.append(score_repository(repo, readme))
        except (HTTPError, URLError, TimeoutError) as exc:
            errors.append(f"readme {full_name!r}: {type(exc).__name__}: {exc}")
            repositories.append(score_repository(repo, ""))

    repositories.sort(key=lambda item: (item["score"], item["stars"]), reverse=True)
    approved = [
        item
        for item in repositories
        if item["safe_for_review"] and item["score"] >= config.minimum_score
    ]
    mapped = sorted(
        {
            strategy
            for item in approved
            for strategy in item.get("mapped_local_strategies", [])
        }
    )
    result = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "queries": list(queries),
        "repositories_found": len(found),
        "repositories_inspected": len(repositories),
        "approved_for_concept_research": len(approved),
        "mapped_local_strategies": mapped,
        "security_policy": "metadata and README only; remote code is never executed",
        "test_scope": "local concept implementations, not downloaded repository strategies",
        "providers": ["github", "gitlab"] if include_gitlab else ["github"],
        "repositories": repositories,
        "errors": errors,
    }
    if not repositories and cache_path and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        cached["cache_used"] = True
        cached["errors"] = errors
        return cached
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result, indent=2, ensure_ascii=True), encoding="utf-8")
    return result


def markdown_report(report: dict[str, Any]) -> str:
    discovery = report.get("discovery", {})
    backtest = report.get("backtest", {})
    analytics = report.get("analytics", {})
    lines = [
        "# GitHub Strategy Scout",
        "",
        f"Generated: {report.get('generated_utc', '-')}",
        "",
        "Remote repository code was not executed. Concepts were tested through local reviewed implementations.",
        "",
        "## Discovery",
        "",
        f"- Found: {discovery.get('repositories_found', 0)}",
        f"- Inspected: {discovery.get('repositories_inspected', 0)}",
        f"- Approved for concept research: {discovery.get('approved_for_concept_research', 0)}",
        f"- Local strategies mapped: {', '.join(discovery.get('mapped_local_strategies', [])) or '-'}",
        "",
        "| Repository | Score | Stars | License | Concepts |",
        "|---|---:|---:|---|---|",
    ]
    for item in discovery.get("repositories", [])[:15]:
        lines.append(
            f"| [{item.get('full_name', '-')}]({item.get('url', '')}) | {item.get('score', 0)} | "
            f"{item.get('stars', 0)} | {item.get('license', '-')} | {', '.join(item.get('concepts', [])) or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Controlled Backtest",
            "",
            f"- Sessions: {backtest.get('sessions', 0)}",
            f"- Trades: {(backtest.get('portfolio') or {}).get('trades', 0)}",
            f"- PnL: {(backtest.get('portfolio') or {}).get('pnl', 0)}",
            f"- Profit factor: {(backtest.get('portfolio') or {}).get('profit_factor', 0)}",
            f"- Stable pairs: {', '.join(analytics.get('stable_pairs', [])) or 'none'}",
            "",
            "## Errors",
            "",
        ]
    )
    errors = list(discovery.get("errors", [])) + list(report.get("errors", []))
    lines.extend([f"- {error}" for error in errors] or ["- None"])
    return "\n".join(lines) + "\n"
