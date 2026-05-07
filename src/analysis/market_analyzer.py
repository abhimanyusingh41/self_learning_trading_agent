from datetime import datetime, date, timedelta
from typing import Optional
import pytz
from loguru import logger

from src.data.market_data import MarketData
from src.data.indicators import add_all_indicators, get_signal_summary


IST = pytz.timezone("Asia/Kolkata")


class MarketAnalyzer:
    def __init__(self, market_data: MarketData, config: dict):
        self.md = market_data
        self.watchlist = config["instruments"]["watchlist"]
        self.exchange = config["instruments"].get("exchange", "NSE")
        self.interval = config.get("backtest", {}).get("interval", "5minute")

    def build_market_context(self) -> str:
        """Assemble a comprehensive market context string for the LLM."""
        sections = []

        # 1. Broad market
        sections.append(self._broad_market_section())

        # 2. Individual stocks
        sections.append(self._stocks_section())

        return "\n\n".join(sections)

    def _broad_market_section(self) -> str:
        indices = self.md.get_indices_data()
        nifty = indices.get("nifty", {})
        bnf = indices.get("banknifty", {})
        vix = indices.get("india_vix", {})

        def fmt(q: dict) -> str:
            lp = q.get("last_price", 0)
            chg = q.get("net_change", 0)
            pct = q.get("change", 0)
            return f"₹{lp:,.2f} ({'+' if chg >= 0 else ''}{chg:.2f}, {pct:.2f}%)"

        lines = [
            "### BROAD MARKET",
            f"NIFTY 50:     {fmt(nifty)}",
            f"BANKNIFTY:    {fmt(bnf)}",
            f"INDIA VIX:    {vix.get('last_price', 'N/A')}",
            f"VIX Regime:   {self._vix_regime(vix.get('last_price', 0))}",
        ]

        # Intraday NIFTY chart summary
        nifty_intraday = self._get_indicator_summary("NIFTY 50", exchange="NSE")
        if nifty_intraday:
            lines.append(f"NIFTY Trend:  {nifty_intraday.get('trend', 'unknown')}")
            lines.append(f"NIFTY RSI:    {nifty_intraday.get('rsi', 'N/A')} ({nifty_intraday.get('rsi_zone', '')})")

        return "\n".join(lines)

    def _stocks_section(self) -> str:
        quotes = self.md.get_quote(self.watchlist, self.exchange)
        lines = ["### WATCHLIST ANALYSIS"]

        for symbol in self.watchlist:
            key = f"{self.exchange}:{symbol}"
            q = quotes.get(key, {})
            lp = q.get("last_price", 0)
            chg = q.get("net_change", 0)
            pct = q.get("change", 0)
            volume = q.get("volume", 0)

            indicators = self._get_indicator_summary(symbol)
            if not indicators:
                lines.append(f"\n{symbol}: ₹{lp:.2f} ({'+' if chg >= 0 else ''}{pct:.2f}%) — no indicator data")
                continue

            lines.append(
                f"\n{symbol}: ₹{lp:.2f} ({'+' if chg >= 0 else ''}{pct:.2f}%) "
                f"| Vol: {volume:,} ({indicators.get('volume_ratio', 1):.1f}x avg)"
            )
            lines.append(
                f"  Trend: {indicators.get('trend')} | RSI: {indicators.get('rsi')} ({indicators.get('rsi_zone')}) "
                f"| MACD: {'bullish' if indicators.get('macd_bullish_crossover') else 'bearish' if indicators.get('macd_bearish_crossover') else 'neutral'}"
            )
            lines.append(
                f"  BB: {indicators.get('bb_position')} | ATR: ₹{indicators.get('atr')} "
                f"| Nearest pivot: {indicators.get('near_pivot_level')}"
            )
            if indicators.get("volume_surge"):
                lines.append(f"  ** VOLUME SURGE ({indicators.get('volume_ratio'):.1f}x) **")

        return "\n".join(lines)

    def _get_indicator_summary(self, symbol: str, exchange: Optional[str] = None) -> dict:
        ex = exchange or self.exchange
        try:
            today = date.today()
            # Fetch 3 days to get enough bars for indicators
            from_date = today - timedelta(days=5)
            df = self.md.get_historical_data(symbol, from_date, today, "5minute", ex)
            if df.empty:
                return {}
            df = add_all_indicators(df)
            return get_signal_summary(df)
        except Exception as e:
            logger.debug(f"Indicator summary failed for {symbol}: {e}")
            return {}

    def _vix_regime(self, vix: float) -> str:
        if not vix:
            return "unknown"
        if vix < 12:
            return "very_low (complacency — watch for reversal)"
        if vix < 15:
            return "low (calm market, trend-following setups)"
        if vix < 20:
            return "normal (standard sizing)"
        if vix < 25:
            return "elevated (reduce size 50%)"
        return "high (avoid intraday, risk of gap moves)"
