from datetime import datetime, date, timedelta
from typing import Optional
import pytz
from loguru import logger

from src.data.market_data import MarketData
from src.data.indicators import add_all_indicators, get_signal_summary


IST = pytz.timezone("Asia/Kolkata")

# Binance interval mapping
BINANCE_INTERVAL = "5m"


class MarketAnalyzer:
    def __init__(self, market_data: MarketData, config: dict, binance_data=None):
        self.md = market_data
        self.bd = binance_data  # BinanceData instance, optional
        self.config = config
        self.instruments = config.get("instruments", {})
        self.equities = self.instruments.get("equities", [])
        self.commodities = self.instruments.get("commodities", [])
        self.crypto = self.instruments.get("crypto", [])
        self.exchange = self.instruments.get("exchange", "NSE")
        self.commodity_exchange = self.instruments.get("commodity_exchange", "MCX")

    def build_market_context(self) -> str:
        sections = []
        now_ist = datetime.now(IST)

        # 1. Broad market (NIFTY/VIX) — always included
        sections.append(self._broad_market_section())

        # 2. Equities — only during NSE hours
        if self._is_equity_session(now_ist):
            sections.append(self._equities_section())
        else:
            sections.append("### INDIAN EQUITIES\nNSE market closed.")

        # 3. Commodities (MCX) — only during MCX hours
        if self._is_commodity_session(now_ist):
            sections.append(self._commodities_section())
        else:
            sections.append("### MCX COMMODITIES\nMCX market closed.")

        # 4. Crypto — always open
        if self.bd and self.crypto:
            sections.append(self._crypto_section())

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

        vix_val = vix.get("last_price", 0)
        lines = [
            "### BROAD MARKET",
            f"NIFTY 50:   {fmt(nifty)}",
            f"BANKNIFTY:  {fmt(bnf)}",
            f"INDIA VIX:  {vix_val} — {self._vix_regime(vix_val)}",
        ]
        return "\n".join(lines)

    def _equities_section(self) -> str:
        if not self.equities:
            return "### INDIAN EQUITIES\nNo equities configured."

        quotes = self.md.get_quote(self.equities, self.exchange)
        lines = ["### INDIAN EQUITIES (NSE)"]

        for symbol in self.equities:
            key = f"{self.exchange}:{symbol}"
            q = quotes.get(key, {})
            lp = q.get("last_price", 0)
            pct = q.get("change", 0)
            volume = q.get("volume", 0)
            indicators = self._get_kite_indicators(symbol, self.exchange)
            if not indicators:
                lines.append(f"\n{symbol}: ₹{lp:.2f} ({'+' if pct >= 0 else ''}{pct:.2f}%) — no indicator data")
                continue
            lines.append(
                f"\n{symbol}: ₹{lp:.2f} ({'+' if pct >= 0 else ''}{pct:.2f}%) | Vol: {volume:,} ({indicators.get('volume_ratio', 1):.1f}x)"
            )
            lines.append(
                f"  Trend: {indicators.get('trend')} | RSI: {indicators.get('rsi')} ({indicators.get('rsi_zone')}) | "
                f"ATR: ₹{indicators.get('atr')} | Nearest pivot: {indicators.get('near_pivot_level')}"
            )
            if indicators.get("volume_surge"):
                lines.append(f"  ** VOLUME SURGE **")
        return "\n".join(lines)

    def _commodities_section(self) -> str:
        if not self.commodities:
            return "### MCX COMMODITIES\nNo commodities configured."

        quotes = self.md.get_mcx_quote(self.commodities)
        lines = ["### MCX COMMODITIES"]

        for base in self.commodities:
            q = quotes.get(base, {})
            active = q.get("active_contract", base)
            lp = q.get("last_price", 0)
            pct = q.get("change", 0)
            if not lp:
                lines.append(f"\n{base}: no data (contract not found)")
                continue
            indicators = self._get_kite_indicators(active, self.commodity_exchange)
            lines.append(f"\n{base} ({active}): ₹{lp:.2f} ({'+' if pct >= 0 else ''}{pct:.2f}%)")
            if indicators:
                lines.append(
                    f"  Trend: {indicators.get('trend')} | RSI: {indicators.get('rsi')} "
                    f"({indicators.get('rsi_zone')}) | ATR: ₹{indicators.get('atr')}"
                )
        return "\n".join(lines)

    def _crypto_section(self) -> str:
        lines = ["### CRYPTO (Binance)"]
        try:
            quotes = self.bd.get_quote(self.crypto)
            for symbol in self.crypto:
                q = quotes.get(symbol, {})
                price = q.get("last_price", 0)
                pct = q.get("change_pct", 0)
                vol = q.get("volume", 0)

                df = self.bd.get_historical_data(symbol, interval=BINANCE_INTERVAL, limit=100)
                if df.empty:
                    lines.append(f"\n{symbol}: ${price:,.4f} ({'+' if pct >= 0 else ''}{pct:.2f}%) — no indicator data")
                    continue

                df = add_all_indicators(df)
                ind = get_signal_summary(df)

                lines.append(
                    f"\n{symbol}: ${price:,.4f} ({'+' if pct >= 0 else ''}{pct:.2f}%) | Vol: {vol:,.1f}"
                )
                lines.append(
                    f"  Trend: {ind.get('trend')} | RSI: {ind.get('rsi')} ({ind.get('rsi_zone')}) | "
                    f"ATR: {ind.get('atr')} | BB: {ind.get('bb_position')}"
                )
                if ind.get("volume_surge"):
                    lines.append(f"  ** VOLUME SURGE ({ind.get('volume_ratio'):.1f}x) **")
        except Exception as e:
            logger.error(f"Crypto section error: {e}")
            lines.append(f"Error fetching crypto data: {e}")
        return "\n".join(lines)

    def _get_kite_indicators(self, symbol: str, exchange: str) -> dict:
        try:
            today = date.today()
            from_date = today - timedelta(days=5)
            df = self.md.get_historical_data(symbol, from_date, today, "5minute", exchange)
            if df.empty:
                return {}
            df = add_all_indicators(df)
            return get_signal_summary(df)
        except Exception as e:
            logger.debug(f"Indicator error for {symbol}: {e}")
            return {}

    def _is_equity_session(self, now_ist: datetime) -> bool:
        if now_ist.weekday() >= 5:
            return False
        market_cfg = self.config.get("market", {})
        open_t = market_cfg.get("equity_open", "09:15").split(":")
        close_t = market_cfg.get("equity_close", "15:30").split(":")
        open_dt = now_ist.replace(hour=int(open_t[0]), minute=int(open_t[1]), second=0, microsecond=0)
        close_dt = now_ist.replace(hour=int(close_t[0]), minute=int(close_t[1]), second=0, microsecond=0)
        return open_dt <= now_ist <= close_dt

    def _is_commodity_session(self, now_ist: datetime) -> bool:
        if now_ist.weekday() >= 5:
            return False
        market_cfg = self.config.get("market", {})
        open_t = market_cfg.get("commodity_open", "09:00").split(":")
        close_t = market_cfg.get("commodity_close", "23:30").split(":")
        open_dt = now_ist.replace(hour=int(open_t[0]), minute=int(open_t[1]), second=0, microsecond=0)
        close_dt = now_ist.replace(hour=int(close_t[0]), minute=int(close_t[1]), second=0, microsecond=0)
        return open_dt <= now_ist <= close_dt

    def _vix_regime(self, vix: float) -> str:
        if not vix:
            return "unknown"
        if vix < 12:
            return "very_low (complacency)"
        if vix < 15:
            return "low (calm, trend-following)"
        if vix < 20:
            return "normal (standard sizing)"
        if vix < 25:
            return "elevated (reduce size 50%)"
        return "high (avoid intraday)"
