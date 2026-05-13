# Self-Learning Trading Agent

Autonomous multi-market trading agent (NSE options, MCX commodities, Binance crypto) that uses Claude as its decision brain. Paper trading mode by default.

## Architecture

```
main.py                        Entry point, config loading, scheduler launch
src/core/
  brain.py                     All Anthropic API calls (analyze_and_decide, reflect_on_trade)
  agent.py                     Orchestration loop — market context → brain → execute
  memory.py                    Trade journal (JSON file, no API calls)
src/analysis/
  market_analyzer.py           Builds the text prompt fed to Claude (Kite + Binance data)
  trade_reflector.py           Triggers post-trade reflection API call
src/data/
  market_data.py               Kite Connect wrapper (NSE/MCX quotes, option chains)
  binance_data.py              Binance REST API wrapper
  indicators.py                TA-lib indicators (EMA, RSI, ATR, Bollinger)
src/execution/
  paper_trader.py              Paper NSE/MCX executor
  binance_trader.py            Paper + live Binance executor
  live_trader.py               Live Kite executor
src/risk/
  risk_manager.py              Daily loss limit, position limits
dashboard/                     Flask web UI (read-only, no API calls)
config/config.yaml             All tuneable parameters
data/memory/trade_memory.json  Persistent trade journal + lessons
```

## Running

```bash
# Start paper trading loop
python main.py

# Single analysis cycle (debug)
python main.py --once

# Override mode
python main.py --mode paper|live|backtest

# Refresh Kite access token (run once each morning)
python main.py --kite-login
```

## Environment variables (`.env`)

```
ANTHROPIC_API_KEY=
KITE_API_KEY=
KITE_API_SECRET=
KITE_ACCESS_TOKEN=          # Expires daily — refresh with --kite-login
BINANCE_API_KEY=
BINANCE_API_SECRET=
```

## Key config knobs (`config/config.yaml`)

| Key | Default | Effect |
|-----|---------|--------|
| `agent.model` | `claude-sonnet-4-6` | Anthropic model for decisions |
| `agent.max_tokens` | `2000` | Max output tokens per decision |
| `agent.analysis_interval_minutes` | `10` | Brain cycle when hunting for a trade |
| `agent.in_trade_interval_minutes` | `3` | Brain cycle when any position is open |
| `agent.enable_mcx` | `false` | Pause MCX trading (code intact, set `true` to re-enable) |
| `agent.enable_crypto` | `false` | Pause crypto trading (code intact, set `true` to re-enable) |
| `agent.reflection_enabled` | `true` | Post-trade reflection API calls |
| `risk.nse_capital` | `50000` | INR allocated to NSE options |
| `risk.mcx_capital` | `50000` | INR allocated to MCX commodities |
| `risk.crypto_capital_usdt` | `500` | USDT allocated to crypto |
| `risk.daily_loss_limit_pct` | `2.0` | Auto-halt trading if exceeded |

## API cost profile

Two API call types:

**1. `brain.analyze_and_decide`** — called every `analysis_interval_minutes`
- System prompt (~3,500 tokens): cached with `cache_control: ephemeral`
- User prompt (~5,000–7,000 tokens): market context + portfolio + lessons
- Uses `thinking={"type": "adaptive"}, output_config={"effort": "high"}` — extended thinking for complex decisions
- `max_tokens: 2000` (actual output is ~400–600 tokens)

**2. `brain.reflect_on_trade`** — called once per closed trade
- Same cached system prompt
- No thinking — simple structured text extraction
- `max_tokens: 512`

**Call frequency (per day — NSE only, MCX/crypto paused):**
- NSE hours (09:15–15:30, ~6.25 h): 10-min interval = ~37 calls when hunting
- If in a trade: 3-min interval, adds ~7–10 calls per trade held
- Agent sleeps 15:30–09:00 IST (no overnight calls)
- Reflections: 2–5 calls/day
- **Total: ~37–55 calls/day**

**Approximate daily cost (claude-sonnet-4-6): ~$1–2/day**

## Scheduler loop (`agent.run_scheduler`)

```
15:30 IST → sleep until 09:00 IST next working day (Mon–Fri); Fri close → Mon 09:00
Every  3 min:  _check_exit_conditions(options_only=True)   # SL/target hits — no API
Every 10 min:  run_once()  when no open positions           # hunting — 1 API call
Every  3 min:  run_once()  when any position is open        # in-trade — 1 API call
```

The brain call is skipped entirely when all tradeable pools are at max positions.

## Market context structure (tokens sent to Claude)

```
BROAD MARKET          ~150 tokens  (NIFTY, BANKNIFTY, VIX)
NSE STOCK OPTIONS     ~3,000 tokens (10 stocks × option chain, only during 09:15–15:30)
MCX COMMODITIES       ~300 tokens  (6 commodities, only during 09:00–23:30)
CRYPTO (Binance)      ~400 tokens  (7 pairs × 50-bar indicators)
PORTFOLIO STATE       ~500 tokens  (open positions)
LESSONS               ~300 tokens  (top 10 recent lessons)
```

## Decision output format

Claude returns JSON matching `TradeDecision` dataclass:
```json
{
  "action": "BUY|SELL|SHORT|COVER|HOLD|WAIT",
  "symbol": "exact tradingsymbol or null",
  "quantity": "lots (NFO/MCX) or units (crypto)",
  "entry_price": float,
  "stop_loss": float,
  "target_1": float,
  "target_2": float,
  "confidence": 0.0–1.0,
  "rationale": "...",
  "key_risks": ["..."],
  "time_horizon": "intraday|swing|positional",
  "setup_type": "breakout|momentum|..."
}
```

Minimum confidence thresholds enforced in the system prompt: 0.60 for NSE/MCX, 0.55 for crypto.

## Capital pool separation

Three completely independent pools, each with its own executor and cash balance:
- `executor` (PaperTrader) → NSE options via Kite NFO
- `executor.mcx_paper` (PaperTrader) → MCX commodities via Kite MCX  
- `executor.binance_paper` (BinancePaperTrader) → Binance USDT pairs

The brain receives all three balances and decides which pool to trade each cycle.

## Common tasks

**Add a new option underlying:**
Add entry to `config.yaml` under `instruments.option_underlyings` with `symbol` and `lot_size`.

**Change model:**
Update `agent.model` in `config.yaml`. Both `claude-sonnet-4-6` (default, cost-efficient) and `claude-opus-4-7` (higher quality, 5× more expensive) work.

**Tune decision frequency:**
Adjust `agent.analysis_interval_minutes` (NSE/MCX hours) and `agent.crypto_only_interval_minutes` (overnight) in `config.yaml`.

**Disable reflections:**
Set `agent.reflection_enabled: false` in `config.yaml`.
