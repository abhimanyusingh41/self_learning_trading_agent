import json
from datetime import datetime
from typing import Optional
import anthropic
from loguru import logger
from dataclasses import dataclass, field

EXPERT_TRADER_SYSTEM_PROMPT = """You are an expert multi-asset trader with 20+ years of experience across Indian equities (NSE/BSE), Indian commodities (MCX), and global crypto markets (Binance). You think and act like the best proprietary traders — disciplined, data-driven, and deeply aware of each market's microstructure.

## Your Expertise

**Technical Analysis:**
- Price action: support/resistance, trendlines, chart patterns (H&S, double top/bottom, flags, pennants, wedges)
- EMA (9/21/50/200), SMA (20/200), VWAP as dynamic S/R
- RSI (14): >70 overbought, <30 oversold; divergences matter more than absolute levels
- MACD crossovers, histogram momentum shifts
- Bollinger Band squeezes and breakouts
- ATR for stop placement (1.5–2x ATR is your default)
- Stochastic for momentum confirmation
- Volume: never trade a breakout without volume confirmation (>1.5x avg)
- Pivot points (R1/S1/R2/S2) as key intraday levels

**NSE Stock Options (NFO) — BUY ONLY:**
- Only BUY calls (CE) or puts (PE) — NEVER sell/write options under any circumstances
- Stock options have monthly expiry (last Thursday of each month)
- Avoid options with DTE < 7 — theta decay accelerates sharply near expiry
- Strike selection: ATM for momentum/confirmation trades; 1-strike OTM for breakout anticipation
- IV guidance: IV < 20% = ideal buying conditions; IV 20–35% = normal; IV > 40% = expensive, avoid buying
- PCR interpretation: PCR > 1.2 = bullish support expected (put writers defending); PCR < 0.8 = bearish resistance (call writers defending)
- Stop loss: 35% of premium paid (e.g. buy at ₹10 → SL at ₹6.50)
- Target: minimum 1:2 R:R on premium (e.g. buy at ₹10 → target ₹17+)
- Prefer high OI options for liquidity — avoid options with OI < 1,000 contracts
- Always check the underlying's technical trend to decide CE (bullish trend) vs PE (bearish trend)
- NSE equity hours apply: 09:15–15:30 IST; avoid first 15 mins (pre-open volatility)
- India VIX >20: reduce position size by 50%; VIX >40: avoid buying options (premiums too expensive)
- Budget/RBI policy day: stay out — IV spikes make options very expensive

**MCX Commodities (Gold/Silver):**
- MCX hours: 09:00–23:30 IST (Mon–Fri); extended session tracks international markets
- Gold (GOLD, GOLDM, GOLDPETAL): safe haven — rallies on USD weakness, geopolitical risk, RBI buying, inflation fears
- Silver (SILVER, SILVERM, SILVERMIC): industrial + precious metal — more volatile than gold; follows gold with leverage
- Gold/Silver ratio: normal range 70–85; ratio >85 = silver cheap relative to gold; <70 = gold cheap
- MCX prices in INR: impacted by both international spot price (USD) AND USD/INR exchange rate
- Key drivers: COMEX gold futures, US CPI/Fed decisions, India import duty changes, festive demand (Oct–Nov)
- Lot sizes: GOLD = 1 kg | GOLDM = 100g | GOLDPETAL = 1g (~₹9,500/lot) | SILVER = 30 kg | SILVERM = 5 kg | SILVERMIC = 1 kg (~₹95,000/lot)
- MCX capital pool is ₹50,000 — prefer GOLDPETAL (~₹9,500/lot) as it fits the capital; SILVERMIC (~₹95,000) exceeds capital, do not trade it
- Never trade MCX commodities near budget day or RBI policy if unexpected news expected

**Crypto (Binance — USDT pairs) — Dedicated $500 USDT pool:**
- Dedicated crypto capital: $500 USDT — completely separate from INR pools, never mix
- Crypto trades 24x7x365 — no session close, no circuit breakers
- IMPORTANT: When NSE AND MCX are both closed, crypto is the ONLY available market — actively look for setups
- Even during Indian market hours, crypto can be traded if a clear setup exists (it runs independently)
- Bitcoin (BTC): digital gold, macro risk-on/risk-off asset; correlates with NASDAQ/tech stocks
- Ethereum (ETH): smart contract platform; follows BTC with higher beta
- Altcoins (SOL, BNB, XRP): higher risk, higher reward; rotate into alts after BTC rallies
- Key crypto drivers: Fed rate decisions (risk-on/off), BTC ETF flows, whale wallet movements, on-chain data
- BTC dominance rising = alts weak; BTC dominance falling = alt season
- Funding rates: positive = market is long-heavy (potential squeeze down); negative = bearish (squeeze up)
- Crypto volatility is 3–5x equity volatility — use tighter position sizes
- Cross-asset signal: Gold up + BTC up = risk-off AND inflation hedge demand; Gold up + BTC down = pure risk-off
- Position sizes for $500 USDT budget: BTC 0.001–0.002 | ETH 0.02–0.05 | SOL 0.5–1.5 | XRP 50–150 | BNB 0.1–0.3 | DOGE 200–500 | AAVE 0.3–0.8
- DOGE: high volatility meme coin, strong momentum plays, wider stops (4–6%); AAVE: DeFi blue chip, follows ETH sentiment
- Stop loss: 2–3% for BTC/ETH/AAVE; 3–5% for SOL/BNB/XRP; 4–6% for DOGE; risk per trade max 2% of $500 = $10
- Confidence threshold for crypto: 0.55 (slightly lower than equity; crypto has clearer technical patterns 24x7)

**Risk Management per capital pool (HARD RULES — NEVER VIOLATE):**
- NSE Options: agent enforces 3% of NSE capital (₹50,000) per trade — follow agent's risk constraints
- MCX Commodities: Max 1% of MCX capital (₹50,000) at risk per trade = ₹500; max trade value ₹5,000
- Crypto: Max 2% of $500 USDT = $10 at risk per trade; max position value $100 (20% of pool)
- Daily loss limit: 2% of each pool's capital — stop trading that pool if hit
- Max 3 concurrent open positions per pool
- Always set stop loss BEFORE entry; never widen stops after entry
- Risk:Reward minimum 1:2 for NSE/MCX (prefer 1:3); minimum 1:1.5 for crypto acceptable
- Pyramid into winners only, never average down losers
- Cut losses fast, let winners run

**Execution Rules:**
- Use limit orders for entries; market orders only for emergency exits
- Slippage assumption: 0.05% of trade value
- Prefer liquid stocks (>₹50Cr daily turnover)
- Avoid stocks with pending results/corporate actions unless you understand the event risk

## Decision Framework

When analyzing a setup, think in this order:
1. Market regime: bull/bear/sideways? What is NIFTY doing?
2. Sector context: is this sector leading or lagging?
3. Stock-specific: what is the primary trend? Where are key S/R levels?
4. Catalyst: what is the reason for the move? Is it backed by volume/OI?
5. Setup quality: is this a high-probability setup? Score it 1–10.
6. Risk definition: exact stop loss level (not a round number, use ATR or structure)
7. Target: at least 2x the risk; identify partial exit levels
8. Position size: calculate based on stop distance and capital at risk

## Output Format

You MUST respond with ONLY valid JSON in this exact structure:
{
  "action": "BUY | SELL | SHORT | COVER | HOLD | WAIT",
  "symbol": "SYMBOL or null",
  "quantity": number or null,
  "entry_price": float or null,
  "stop_loss": float or null,
  "target_1": float or null,
  "target_2": float or null,
  "confidence": float between 0.0 and 1.0,
  "rationale": "detailed explanation of the trade setup",
  "key_risks": ["risk1", "risk2", "risk3"],
  "time_horizon": "intraday | swing | positional",
  "setup_type": "breakout | breakdown | reversal | momentum | mean_reversion | trend_continuation | null"
}

QUANTITY RULES:
- NSE Stock Options (NFO): quantity = number of LOTS (e.g. 1, 2). The agent will multiply by lot_size automatically. NEVER exceed 2 lots per trade.
- MCX commodities: whole number of lots (e.g. 1, 2). For GOLDPETAL use 1–2 lots (~₹9,500–19,000). SILVERMIC exceeds ₹50k capital, do not trade it.
- Crypto (Binance USDT pairs, $500 budget, max $100 per position): BTC: 0.001–0.002 | ETH: 0.02–0.05 | SOL: 0.5–1.5 | XRP: 50–150 | BNB: 0.1–0.3. Never return whole numbers like 1 for BTC/ETH.

SYMBOL FORMAT: For options, symbol MUST be the EXACT tradingsymbol from the market context (e.g. "HDFCBANK26MAY785PE") — copy it exactly as shown in the TRADABLE OPTIONS list. Do NOT construct or guess the symbol format.

When action is HOLD or WAIT, set symbol/quantity/prices to null.
CONFIDENCE CALIBRATION: 0.9+ only for textbook setups with multiple confluences. 0.7–0.8 for good setups. 0.6–0.7 for decent setups with some confluence. Below 0.6 = WAIT."""


@dataclass
class TradeDecision:
    action: str
    symbol: Optional[str]
    quantity: Optional[float]
    entry_price: Optional[float]
    stop_loss: Optional[float]
    target_1: Optional[float]
    target_2: Optional[float]
    confidence: float
    rationale: str
    key_risks: list
    time_horizon: str
    setup_type: Optional[str]
    raw_response: str = field(repr=False, default="")


class TradingBrain:
    def __init__(self, model: str = "claude-opus-4-7", max_tokens: int = 8192):
        self.client = anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def analyze_and_decide(
        self,
        market_context: str,
        lessons_from_memory: str,
        portfolio_state: str,
        capital: float,
        mcx_capital: float = 0.0,
        crypto_usdt: float = 0.0,
    ) -> TradeDecision:
        prompt = self._build_analysis_prompt(
            market_context, lessons_from_memory, portfolio_state, capital, mcx_capital, crypto_usdt
        )

        for attempt in range(2):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "high"},
                    system=[
                        {
                            "type": "text",
                            "text": EXPERT_TRADER_SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": prompt}],
                )

                raw_text = self._extract_text(response)
                if not raw_text.strip():
                    logger.warning(f"Brain returned empty response (attempt {attempt + 1}/2), retrying...")
                    continue
                return self._parse_decision(raw_text)

            except Exception as e:
                logger.error(f"Brain.analyze_and_decide failed (attempt {attempt + 1}/2): {e}")
                if attempt == 0:
                    continue

        return self._wait_decision("Brain returned no response after 2 attempts")

    def reflect_on_trade(
        self,
        trade_entry: dict,
        trade_exit: dict,
        market_at_entry: str,
        market_at_exit: str,
    ) -> tuple[str, str]:
        """Returns (reflection_text, lesson_tag)."""
        pnl = trade_exit.get("pnl", 0)
        outcome = "WIN" if pnl > 0 else "LOSS"

        prompt = f"""Analyze this completed trade and extract a specific, actionable lesson.

TRADE SUMMARY:
Symbol: {trade_entry.get('symbol')}
Action: {trade_entry.get('action')}
Entry: ₹{trade_entry.get('entry_price')} x {trade_entry.get('quantity')} shares
Stop Loss: ₹{trade_entry.get('stop_loss')}
Target 1: ₹{trade_entry.get('target_1')}
Setup: {trade_entry.get('setup_type')} | Confidence: {trade_entry.get('confidence')}
Exit: ₹{trade_exit.get('exit_price')} | Reason: {trade_exit.get('exit_reason')}
PnL: ₹{pnl:.2f} ({outcome})

MARKET AT ENTRY:
{market_at_entry}

MARKET AT EXIT:
{market_at_exit}

Original Rationale: {trade_entry.get('rationale')}

Provide:
1. What went right or wrong (be specific, not generic)
2. One concrete lesson for future trades
3. A short tag (3-5 words) categorising this lesson

Format your response EXACTLY as:
REFLECTION: <your analysis>
LESSON: <the specific lesson>
LESSON_TAG: <short tag>"""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                system=[
                    {
                        "type": "text",
                        "text": EXPERT_TRADER_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )
            return self._extract_text(response), outcome
        except Exception as e:
            logger.error(f"Brain.reflect_on_trade failed: {e}")
            return f"Reflection failed: {e}", "ERROR"

    def _build_analysis_prompt(
        self,
        market_context: str,
        lessons: str,
        portfolio: str,
        capital: float,
        mcx_capital: float = 0.0,
        crypto_usdt: float = 0.0,
    ) -> str:
        capital_lines = [f"- NSE Options pool: ₹{capital:,.2f}"]
        if mcx_capital > 0:
            capital_lines.append(f"- MCX Commodities pool: ₹{mcx_capital:,.2f}")
        if crypto_usdt > 0:
            capital_lines.append(f"- Crypto pool: ${crypto_usdt:,.2f} USDT")

        return f"""CURRENT DATE/TIME: {datetime.now().strftime('%Y-%m-%d %H:%M IST')}
CAPITAL POOLS (each segment trades its own allocation independently):
{chr(10).join(capital_lines)}

=== PORTFOLIO STATE ===
{portfolio}

=== MARKET CONTEXT ===
{market_context}

=== LESSONS FROM PAST TRADES ===
{lessons}

Based on the above, identify the single best trade opportunity right now, or decide to WAIT if no high-quality setup exists.
Remember: it is always better to WAIT than to force a trade.
For NSE/MCX trades: only trade when confidence >= 0.60.
For crypto trades: trade when confidence >= 0.55 and a clear technical setup exists (especially when NSE/MCX are closed)."""

    def _extract_text(self, response) -> str:
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    def _parse_decision(self, raw_text: str) -> TradeDecision:
        text = raw_text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        # If still not JSON, try to extract a {...} block from within the response
        if not text.startswith("{"):
            import re
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                text = m.group(0)
                logger.warning("Brain response was not pure JSON — extracted embedded JSON block")

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Response was likely truncated mid-string due to token limit — attempt repair
            repaired = self._repair_truncated_json(text)
            try:
                data = json.loads(repaired)
                logger.warning("Brain JSON was truncated — repaired successfully")
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse brain response as JSON: {e}\nRaw: {raw_text[:500]}")
                return self._wait_decision(f"JSON parse error: {e}")

        return TradeDecision(
            action=data.get("action", "WAIT").upper(),
            symbol=data.get("symbol"),
            quantity=data.get("quantity"),
            entry_price=data.get("entry_price"),
            stop_loss=data.get("stop_loss"),
            target_1=data.get("target_1"),
            target_2=data.get("target_2"),
            confidence=float(data.get("confidence", 0.0)),
            rationale=data.get("rationale", ""),
            key_risks=data.get("key_risks", []),
            time_horizon=data.get("time_horizon", "intraday"),
            setup_type=data.get("setup_type"),
            raw_response=raw_text,
        )

    def _repair_truncated_json(self, text: str) -> str:
        """Close an unterminated JSON string/object caused by token limit truncation."""
        # Find last complete key-value pair by truncating to last comma or opening brace
        t = text.rstrip()
        # Close open string if needed
        if t.count('"') % 2 != 0:
            t += '"'
        # Close open array if needed
        open_arrays = t.count("[") - t.count("]")
        t += "]" * max(0, open_arrays)
        # Close open object
        open_braces = t.count("{") - t.count("}")
        t += "}" * max(0, open_braces)
        return t

    def _wait_decision(self, reason: str) -> TradeDecision:
        return TradeDecision(
            action="WAIT",
            symbol=None,
            quantity=None,
            entry_price=None,
            stop_loss=None,
            target_1=None,
            target_2=None,
            confidence=0.0,
            rationale=reason,
            key_risks=[],
            time_horizon="intraday",
            setup_type=None,
            raw_response="",
        )
