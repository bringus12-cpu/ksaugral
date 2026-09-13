from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.github_strategy_scout import ScanConfig, markdown_report, scan_repositories


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _run(command: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60 * 45,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    return completed.returncode, output[-12_000:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily GitHub strategy discovery and controlled local backtest")
    parser.add_argument("--profile", default=".env.vantage")
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--symbols", default="XAUUSD,NAS100,DJ30,EURUSD,USDJPY,BTCUSD")
    parser.add_argument("--universe", default="")
    parser.add_argument("--minimum-symbols", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--backtest-output", default="")
    parser.add_argument("--analytics-output", default="")
    parser.add_argument("--reuse-backtest", action="store_true")
    parser.add_argument("--per-query", type=int, default=8)
    parser.add_argument("--max-repositories", type=int, default=20)
    parser.add_argument("--skip-backtest", action="store_true")
    args = parser.parse_args()

    data_dir = ROOT / "data_vantage"
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir / "github_strategy_scout_cache.json"
    discovery = scan_repositories(
        ScanConfig(per_query=args.per_query, max_repositories=args.max_repositories),
        cache_path=cache_path,
    )
    strategies = list(discovery.get("mapped_local_strategies", []))
    errors: list[str] = []
    backtest_path = Path(args.backtest_output) if args.backtest_output else data_dir / "github_strategy_scout_backtest_60sessions.json"
    analytics_path = Path(args.analytics_output) if args.analytics_output else data_dir / "github_strategy_scout_analytics.json"

    if not args.skip_backtest and not args.reuse_backtest and strategies:
        if args.universe:
            command = [
                sys.executable,
                str(ROOT / "scripts" / "backtest_market_machine_universe.py"),
                "--profile",
                args.profile,
                "--universe",
                args.universe,
                "--minimum-symbols",
                str(args.minimum_symbols),
                "--sessions",
                str(args.sessions),
                "--strategies",
                ",".join(strategies),
                "--workers",
                str(args.workers),
                "--batch-size",
                str(args.batch_size),
                "--output",
                str(backtest_path),
            ]
        else:
            command = [
                sys.executable,
                str(ROOT / "scripts" / "backtest_market_machine.py"),
                "--profile",
                args.profile,
                "--sessions",
                str(args.sessions),
                "--symbols",
                args.symbols,
                "--strategies",
                ",".join(strategies),
                "--output",
                str(backtest_path),
            ]
        code, output = _run(command)
        if code not in {0, 2}:
            errors.append(f"backtest exit {code}: {output}")
        if backtest_path.exists():
            code, output = _run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "analyze_market_machine.py"),
                    str(backtest_path),
                    "--output",
                    str(analytics_path),
                    "--starting-balance",
                    "1000",
                ]
            )
            if code != 0:
                errors.append(f"analytics exit {code}: {output}")

    backtest = _load(backtest_path)
    analytics = _load(analytics_path)
    stable_keys = set(analytics.get("stable_pairs", [])) | set(analytics.get("diversified_pairs", []))
    analytics_pairs = analytics.get("pairs", {})
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "mode": "discovery plus controlled local backtest",
        "discovery": discovery,
        "backtest": {key: value for key, value in backtest.items() if key not in {"trades", "symbol_strategy_matrix"}},
        "analytics": {
            **{key: value for key, value in analytics.items() if key not in {"pairs", "top_absolute_correlations"}},
            "pairs": {key: analytics_pairs[key] for key in sorted(stable_keys) if key in analytics_pairs},
        },
        "artifacts": {
            "backtest": str(backtest_path.resolve()),
            "analytics": str(analytics_path.resolve()),
            "universe": str((ROOT / args.universe).resolve()) if args.universe else None,
        },
        "errors": errors,
    }
    report_path = data_dir / "github_strategy_scout_report.json"
    markdown_path = ROOT / "reports" / "github_strategy_scout_daily.md"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(report_path),
                "markdown": str(markdown_path),
                "repositories": discovery.get("repositories_inspected", 0),
                "mapped_strategies": strategies,
                "stable_pairs": report["analytics"].get("stable_pairs", []),
                "errors": errors + list(discovery.get("errors", [])),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
