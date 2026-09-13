# Market Machine

Market Machine is the research and execution layer built on top of `Agent Teams`.
It is designed for multi-instrument, long/short testing on MT5 without silently
promoting an unverified idea to live execution.

## Architecture

The structure follows the separation used by mature open-source engines:

1. Signal agents generate independent long, short, or hold votes.
2. The supervisor aggregates weighted votes and requires a directional margin.
3. The control council checks conflicts, execution quality, and team history.
4. The execution layer owns broker symbol mapping, sizing, SL, TP, and comments.
5. The learner keeps candidates in shadow mode until they pass observation and
   accuracy thresholds.

Short-horizon teams operate on M1/M5/M15 or profile-specific combinations.
Long-term brigades operate on H1/H4/D1. The explicitly excluded strategies
`Trend D1 + pullback H4` and `Donchian breakout` are not enabled for execution.

## Instruments

The default cross-market backtest basket contains 30 instruments: XAUUSD,
NAS100, US30, SP500, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, BTCUSD, ETHUSD,
USDCHF, NZDUSD, EURJPY, GBPJPY, EURGBP, AUDJPY, NZDJPY, CADJPY, CHFJPY,
EURAUD, EURCAD, EURCHF, EURNZD, GBPAUD, GBPCAD, GBPCHF, GBPNZD, XAGUSD, and
GER40. The runtime catalog also contains additional FX crosses, European and
Asian indices, copper, US2000, and selected technology shares. Broker symbols
are resolved dynamically, including suffixes such as `+`.

Only keys listed in `AGENT_TEAM_LIVE_KEYS` may send demo orders. Long-term and
scouted teams use separate gates:

- `AGENT_TEAM_LONG_TERM_LIVE_KEYS`
- `AGENT_TEAM_SCOUT_LIVE_DEMO`
- `AGENT_TEAM_ALLOW_LIVE_ACCOUNT`

The default Vantage profile leaves new instruments and long-term teams in
shadow mode. This is intentional.

## Strategies

Base agents cover trend, momentum, structure, price action, volatility, mean
reversion, regime, and execution quality. Candidate agents add:

- Bollinger plus RSI re-entry
- MACD histogram cross plus EMA regime
- adaptive Dual Thrust-style intraday breakout
- Bollinger/Keltner squeeze release
- VWAP reclaim or rejection

Candidate strategies begin as shadow agents. Learning state and promotion are
isolated per team, so a setup proven on XAU cannot automatically become active
on Nasdaq or EURUSD. Promotion requires a minimum sample and measured
directional accuracy configured in `.env.vantage.agent_teams`.
`AGENT_TEAM_CANDIDATE_ALLOWLIST_JSON` adds a second gate based on the 60-session
instrument/strategy matrix. A candidate must pass both gates.

The indicator laboratory adds 20 independent candidate strategies:

- CCI, Williams %R, Stochastic, MFI, Fisher, and Ultimate Oscillator reversals
- Supertrend, Ichimoku, Aroon, KST, PPO, ROC, CMO, and HMA/DEMA trend systems
- CMF, OBV, Force Index, and A/D Oscillator flow confirmation
- linear-regression pullback and efficiency/choppiness trend selection

The feature frame contains more than 30 useful indicators, including EMA,
SMA, WMA, DEMA, TEMA, HMA, KAMA, VWMA, RSI, ATR, ADX, Bollinger Bands, MACD,
Stochastic, CCI, Williams %R, MFI, OBV, CMF, A/D, PPO, CMO, TRIX, Ultimate
Oscillator, Awesome Oscillator, Aroon, NATR, Choppiness, Efficiency Ratio,
z-score, linear-regression slope, Fisher Transform, Supertrend, Ichimoku,
Balance of Power, Force Index, Ease of Movement, Mass Index, KST, and realized
volatility. Donchian levels are analytics-only and are not an execution setup.

## Position sizing

`AGENT_TEAM_LOT_MODE=dynamic_risk` sizes each position from account equity,
the broker-calculated loss for one lot at the proposed SL, and
`AGENT_TEAM_RISK_PER_TRADE_PCT`. Broker minimum, step, and configured maximum
lot are enforced. This makes the same logic portable across FX, metals,
indices, crypto, and shares without pretending one lot has the same risk on
every instrument.

Bounded and classic martingale variants are implemented only in the research
analyzer. Live martingale is disabled by default because increasing size after
a loss raises tail drawdown and risk of ruin. It may not be promoted merely
because one in-sample result has a higher ending balance.

## Backtest

Run the portfolio walk-forward test with:

```powershell
python scripts/backtest_market_machine.py --profile .env.vantage --sessions 60
```

The test uses closed bars only, enters at the next M5 open, includes historical
spread, and assumes the stop is hit first when one bar touches both SL and TP.
The output contains per-strategy and per-symbol statistics plus a list of
research candidates that satisfy the promotion criteria.

Robustness and sizing reports are generated with:

```powershell
python scripts/analyze_market_machine.py data_vantage/market_machine_backtest_20sessions_extended.json
python scripts/analyze_sizing_variants.py data_vantage/market_machine_backtest_20sessions_extended.json --pairs-from data_vantage/market_machine_analytics.json
```

Promotion analytics require at least 40 trades and 20 trading days, positive
PnL, profit factor of at least 1.05, at least three positive chronological
folds, and no more than 35% probability of loss in the daily bootstrap. The
dashboard displays these results in the Agent Teams view.

## Validation snapshot - 2026-08-23

- all 20 laboratory strategies plus existing agents, tested blindly across
  eight markets for 20 sessions: 54,317 trades, -12,294.22 USD, profit factor
  0.882. This configuration was rejected.
- eight preliminary finalists retested for 60 sessions: seven failed the
  longer sample.
- the only new stable pair was `DJ30::cmf_flow_specialist`: 628 trades,
  +136.91 USD at the broker minimum volume, profit factor 1.121, maximum closed
  drawdown 63.53 USD, and three positive chronological folds out of four.
- dynamic 0.10% sizing without martingale changed a 1,000 USD research balance
  to 1,135.77 USD with 6.35% closed drawdown in this sample.
- classic 2x martingale increased ending balance but also raised drawdown to
  17.93%; it remains disabled and research-only.

All 30 symbols in the default basket were resolved successfully on the Vantage
demo terminal. The new DJ30/CMF pair was added only to the US30 shadow
allowlist; it has no permission to place a live order.

## Open-source research

The implementation uses original code informed by public architecture and
indicator concepts. No third-party strategy source was copied into the bot.

- NautilusTrader, LGPL-3.0: deterministic chronological replay, matching and
  execution semantics, and independent backtest runs.
- Microsoft Qlib, MIT: loose coupling of data, features, models, evaluation,
  and workflow layers.
- FinRL, MIT: explicit train, test, and trade separation for future research.
- TA-Lib, BSD-3-Clause, and Tulip Indicators, LGPL-3.0: indicator naming and
  conventional public formulas; no source code was copied.
- Freqtrade, GPL-3.0: lookahead and recursive-analysis checks as research

## GitHub Strategy Scout

`scripts/run_github_strategy_scout.py` performs the daily open-source research
cycle. It searches public GitHub repository metadata and README files, scores
license, maintenance, tests, backtesting, risk controls, and strategy concepts,
then maps those concepts to reviewed local implementations. Remote repository
code is never imported or executed.

The controlled test covers the latest 60 sessions, historical spread, next-bar
execution, conservative SL-first ambiguity, four chronological folds, and a
daily bootstrap. A result is research-only until the same instrument/strategy
pair passes all existing promotion thresholds. Reports are written to:

- `data_vantage/github_strategy_scout_report.json`
- `data_vantage/github_strategy_scout_backtest_60sessions.json`
- `data_vantage/github_strategy_scout_analytics.json`
- `reports/github_strategy_scout_daily.md`

Large-universe validation uses `build_mt5_symbol_universe.py` to verify M5
history and remove broker aliases, then `backtest_market_machine_universe.py`
to split at least 100 instruments into parallel deterministic batches. The
full trade ledger remains separate from the dashboard summary.

Set `GITHUB_TOKEN` only when a higher GitHub API allowance is needed. The token
is optional and must not be committed or written into generated reports.
  guidance; no source code was incorporated.
- LangGraph Supervisor, AutoGen, CrewAI, and Ray RLlib were reviewed for
  multi-agent concepts. Heavy LLM frameworks were deliberately kept outside
  the execution loop; the bot uses deterministic specialist, supervisor,
  control, and audit agents that can be replayed and tested.

Projects with licenses unsuitable for planned commercial distribution were not
incorporated. In particular, vectorbt adds Commons Clause restrictions and
backtesting.py uses AGPL-3.0.

Backtests and shadow results are measurements, not a guarantee of profit.
