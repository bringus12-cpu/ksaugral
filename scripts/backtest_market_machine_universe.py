from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_market_machine import _statistics


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def run_batch(
    index: int,
    symbols: list[str],
    profile: str,
    sessions: int,
    strategies: str,
    batch_dir: Path,
) -> tuple[int, Path, int, str]:
    output = batch_dir / f"batch_{index:03d}.json"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "backtest_market_machine.py"),
        "--profile",
        profile,
        "--sessions",
        str(sessions),
        "--symbols",
        ",".join(symbols),
        "--strategies",
        strategies,
        "--output",
        str(output),
    ]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    diagnostic = "\n".join(part for part in (completed.stdout, completed.stderr) if part)[-6000:]
    return index, output, completed.returncode, diagnostic


def main() -> int:
    parser = argparse.ArgumentParser(description="Parallel Market Machine backtest across a verified MT5 universe")
    parser.add_argument("--profile", default=".env.vantage")
    parser.add_argument("--universe", required=True)
    parser.add_argument("--minimum-symbols", type=int, default=100)
    parser.add_argument("--sessions", type=int, default=60)
    parser.add_argument("--strategies", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--output", default="data_vantage/market_machine_backtest_100symbols_60sessions.json")
    args = parser.parse_args()

    universe_path = ROOT / args.universe
    universe = json.loads(universe_path.read_text(encoding="utf-8-sig"))
    symbols = [str(item["symbol"]) for item in universe.get("symbols", [])]
    if len(symbols) < args.minimum_symbols:
        raise SystemExit(f"Universe has {len(symbols)} symbols; minimum is {args.minimum_symbols}")
    symbols = symbols[: max(args.minimum_symbols, len(symbols))]

    batch_dir = ROOT / "tmp" / "market_machine_100_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[int, Path, int, str]] = []
    symbol_batches = chunks(symbols, max(1, args.batch_size))
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                run_batch,
                index,
                batch,
                args.profile,
                args.sessions,
                args.strategies,
                batch_dir,
            )
            for index, batch in enumerate(symbol_batches, start=1)
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"BATCH={result[0]}/{len(symbol_batches)} EXIT={result[2]} REPORT={result[1]}", flush=True)

    all_trades: list[dict] = []
    errors: list[dict] = []
    resolved: dict[str, str] = {}
    failed_batches: list[dict] = []
    for index, path, code, diagnostic in sorted(results):
        if path.exists():
            report = json.loads(path.read_text(encoding="utf-8-sig"))
            all_trades.extend(report.get("trades", []))
            errors.extend(report.get("errors", []))
            resolved.update(report.get("resolved_symbols", {}))
        if code not in {0, 2}:
            failed_batches.append({"batch": index, "exit_code": code, "diagnostic": diagnostic})

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    by_pair: dict[str, list[dict]] = defaultdict(list)
    for trade in all_trades:
        by_strategy[trade["strategy"]].append(trade)
        by_symbol[trade["symbol"]].append(trade)
        by_pair[f"{trade['symbol']}::{trade['strategy']}"] .append(trade)
    strategy_stats = {key: _statistics(value) for key, value in sorted(by_strategy.items())}
    symbol_stats = {key: _statistics(value) for key, value in sorted(by_symbol.items())}
    pair_stats = {key: _statistics(value) for key, value in sorted(by_pair.items())}
    promoted = [
        key
        for key, stats in pair_stats.items()
        if stats["trades"] >= 20 and stats["pnl"] > 0 and (stats["profit_factor"] or 0.0) >= 1.05
    ]
    output_report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "method": "parallel batches; closed bars, next M5 open, historical spread, conservative SL-first ambiguity",
        "sessions": args.sessions,
        "profile": args.profile,
        "universe": str(universe_path),
        "symbols_requested": len(symbols),
        "symbols_resolved": len(resolved),
        "workers": args.workers,
        "batch_size": args.batch_size,
        "strategies_filter": [item for item in args.strategies.split(",") if item],
        "resolved_symbols": resolved,
        "portfolio": _statistics(all_trades),
        "strategies": strategy_stats,
        "symbols": symbol_stats,
        "symbol_strategy_matrix": pair_stats,
        "research_promotion_candidates": promoted,
        "errors": errors,
        "failed_batches": failed_batches,
        "trades": sorted(all_trades, key=lambda item: item["entry_time"]),
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(output_report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(output),
                "symbols_requested": len(symbols),
                "symbols_resolved": len(resolved),
                "trades": len(all_trades),
                "portfolio": output_report["portfolio"],
                "errors": len(errors),
                "failed_batches": failed_batches,
            },
            indent=2,
        )
    )
    return 0 if len(resolved) >= args.minimum_symbols and not failed_batches else 2


if __name__ == "__main__":
    raise SystemExit(main())
