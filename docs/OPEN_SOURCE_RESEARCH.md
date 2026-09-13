# Open-source research audit

This audit records architecture and research projects reviewed for Market
Machine. The production implementation remains original code. Public projects
were used to identify engineering patterns, validation checks, and standard
indicator definitions; their strategy source was not copied.

## Adopted design patterns

| Project | License | Pattern used |
| --- | --- | --- |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | LGPL-3.0 | Chronological replay, separate runs, explicit execution semantics |
| [Microsoft Qlib](https://github.com/microsoft/qlib) | MIT | Loose coupling of data, features, research, evaluation, and workflow |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | MIT | Train, test, and trade separation for future ML experiments |
| [TA-Lib](https://github.com/TA-Lib/ta-lib) | BSD-3-Clause | Conventional indicator names and public mathematical definitions |
| [Tulip Indicators](https://github.com/TulipCharts/tulipindicators) | LGPL-3.0 | Indicator catalog completeness checks |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | GPL-3.0 | Look-ahead and recursive-analysis test concepts only |
| [pyfolio](https://github.com/quantopian/pyfolio) | Apache-2.0 | Performance, drawdown, and tail-risk report concepts |

## Agent frameworks reviewed

| Project | Decision |
| --- | --- |
| [LangGraph Supervisor](https://github.com/langchain-ai/langgraph-supervisor-py) | Supervisor/specialist concept retained; runtime dependency rejected |
| [Microsoft AutoGen](https://github.com/microsoft/autogen) | Reviewed; maintenance mode and nondeterminism make it unsuitable for execution |
| [CrewAI](https://github.com/crewAIInc/crewAI) | Role and delegation concepts reviewed; runtime dependency rejected |
| [Ray RLlib multi-agent](https://github.com/ray-project/ray/blob/master/rllib/env/multi_agent_env.py) | Multi-agent environment concept retained for future offline research |

The live decision loop stays deterministic and auditable. Signal specialists,
the supervisor, control council, instrument scout, learner, and trade auditor
communicate through structured votes and JSON state. No language model is
allowed to place an order directly.

## Validation borrowed as concepts

- closed-bar features and next-bar execution
- conservative SL-first handling when one bar touches both SL and TP
- historical spread from MT5 data
- no-look-ahead regression test
- four chronological walk-forward folds
- daily bootstrap with drawdown and ruin estimates
- per-instrument strategy isolation
- correlation-aware portfolio selection
- broker-aware loss-per-lot position sizing

## Deliberately not integrated

- classic or unbounded martingale
- direct promotion from in-sample PnL
- LLM-generated orders in the real-time loop
- Donchian breakout execution
- Trend D1 plus pullback H4 execution
- projects whose licensing is incompatible with the intended commercial use

Backtests and shadow observations are measurements, not evidence of guaranteed
future profit.
