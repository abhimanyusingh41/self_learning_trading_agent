# Self-Learning Trading Agent

A fully autonomous trading agent powered by Claude that learns from its own trades. It analyzes Indian equity markets like an expert NSE trader, makes decisions with structured reasoning, and improves over time via a post-trade reflection loop.

## Architecture

```
main.py
└── TradingAgent (src/core/agent.py)
    ├── TradingBrain       — Claude claude-opus-4-7 with adaptive thinking
    ├── TradeMemory        — JSON-backed trade journal + lessons
    ├── RiskManager        — Hard position sizing & loss limits
    ├── MarketAnalyzer     — NIFTY/VIX context + per-stock indicators
    ├── TradeReflector     — Post-trade LLM reflection → stored lessons
    └── Executor
        ├── PaperTrader    — Simulated execution with virtual P&L
        ├── LiveTrader     — Real Kite Connect order placement
        └── Backtester     — Historical bar-by-bar replay
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in ANTHROPIC_API_KEY, KITE_API_KEY, KITE_API_SECRET
```

## Kite Access Token (required daily)

```bash
python main.py --kite-login
# Follow the browser login flow, paste KITE_ACCESS_TOKEN into .env
```

## Running

```bash
# Paper trading (default — safe, no real money)
python main.py

# Single analysis cycle (good for testing)
python main.py --once

# Backtest (configure dates in config/config.yaml)
python main.py --mode backtest

# Live trading (requires confirmed Kite access token)
python main.py --mode live
```

## Configuration

All settings are in `config/config.yaml`. To switch modes:

```yaml
trading_mode: paper    # change to: live | backtest
```

## Risk Limits (hard-coded, non-overridable)

| Limit | Default |
|-------|---------|
| Daily loss cap | 2% of initial capital |
| Per-trade risk | 0.5% of capital |
| Max concurrent positions | 3 |
| Max single trade value | 10% of capital |

## How the Agent Learns

1. Brain decides a trade → executed
2. Price hits stop loss or target (or EOD close)
3. `TradeReflector` asks Claude: *"What went right/wrong? What's the lesson?"*
4. Lesson is stored with a tag (e.g. `volume_surge_breakout`, `premature_entry`)
5. Next decision cycle: top 10 recent lessons are injected as context
6. Claude incorporates past mistakes into new decisions

## Watchlist

Default: RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK, SBIN, BHARTIARTL, BAJFINANCE, KOTAKBANK, AXISBANK

Edit `instruments.watchlist` in `config/config.yaml` to change.
