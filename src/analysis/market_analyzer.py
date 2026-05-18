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
        self.option_underlyings = self.instruments.get("option_underlyings", [])  # list of symbols (strings)
        self.commodities = self.instruments.get("commodities", [])
        self.crypto = self.instruments.get("crypto", [])
        self.exchange = self.instruments.get("exchange", "NSE")
        self.options_exchange = self.instruments.get("options_exchange", "NFO")
        self.commodity_exchange = self.instruments.get("commodity_exchange", "MCX")

    def build_market_context(
        self,
        focus_underlyings: set = None,
        include_nse: bool = True,
        include_mcx: bool = True,
        include_crypto: bool = True,
    ) -> str:
        """Build market context.
        - focus_underlyings: only scan these stocks (in-trade mode)
        - include_nse/mcx/crypto: skip full sections when that pool is at max positions
        """
        sections = []
        now_ist = datetime.now(IST)
        equity_open = self._is_equity_session(now_ist)
        commodity_open = self._is_commodity_session(now_ist)

        # 1. Broad market (NIFTY/VIX) — only when NSE or MCX is open
        if equity_open or commodity_open:
            sections.append(self._broad_market_section())
        else:
            sections.append("### BROAD MARKET\nNSE and MCX both closed — no Kite data fetched.")

        # 2. NSE Stock Options
        if not include_nse:
            sections.append("### NSE STOCK OPTIONS (NFO)\nNSE pool at max positions — no new entries.")
        elif equity_open:
            sections.append(self._options_section(focus_underlyings=focus_underlyings))
        else:
            sections.append("### NSE STOCK OPTIONS (NFO)\nNSE market closed.")

        # 3. Commodities (MCX)
        agent_cfg = self.config.get("agent", {})
        mcx_enabled = agent_cfg.get("enable_mcx", True)
        crypto_enabled = agent_cfg.get("enable_crypto", True)

        if not include_mcx:
            sections.append("### MCX COMMODITIES\nMCX pool at max positions — no new entries.")
        elif not mcx_enabled:
            sections.append("### MCX COMMODITIES\nMCX trading paused.")
        elif commodity_open:
            sections.append(self._commodities_section())
        else:
            sections.append("### MCX COMMODITIES\nMCX market closed.")

        # 4. Crypto
        if not include_crypto:
            sections.append("### CRYPTO\nCrypto pool at max positions — no new entries.")
        elif crypto_enabled and self.bd and self.crypto:
            sections.append(self._crypto_section())
        elif not crypto_enabled:
            sections.append("### CRYPTO\nCrypto trading paused.")

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

    def _options_section(self, focus_underlyings: set = None) -> str:
        if not self.option_underlyings:
            return "### NSE STOCK OPTIONS (NFO)\nNo option underlyings configured."

        lines = ["### NSE STOCK OPTIONS (NFO) — BUY ONLY"]
        if focus_underlyings:
            lines.append(f"(In-trade scan: showing held underlyings only — {', '.join(sorted(focus_underlyings))})")

        for entry in self.option_underlyings:
            symbol = entry if isinstance(entry, str) else entry.get("symbol", "")
            # When in a trade, only fetch data for held underlyings
            if focus_underlyings and symbol not in focus_underlyings:
                continue
            if not symbol:
                continue
            lot_size = self.md.get_underlying_lot_size(symbol)

            try:
                snap = self.md.get_option_chain_snapshot(symbol, num_strikes=1, lot_size=lot_size)
            except Exception as e:
                logger.error(f"Option chain error for {symbol}: {e}")
                snap = {"symbol": symbol, "error": str(e)}

            lines.append("")  # blank separator

            if snap.get("error"):
                # Show underlying price with note when option data is unavailable
                up = snap.get("underlying_price", 0)
                upct = snap.get("underlying_pct", 0)
                if up:
                    lines.append(
                        f"{symbol}: ₹{up:.2f} ({'+' if upct >= 0 else ''}{upct:.2f}%) — option data unavailable"
                    )
                else:
                    lines.append(f"{symbol}: option data unavailable")
                continue

            up = snap["underlying_price"]
            upct = snap.get("underlying_pct", 0)
            expiry = snap["expiry"]
            dte = snap["dte"]
            atm = snap["atm_strike"]
            atm_ce_p = snap["atm_call_premium"]
            atm_pe_p = snap["atm_put_premium"]
            atm_ce_oi = snap["atm_call_oi"]
            atm_pe_oi = snap["atm_put_oi"]
            atm_iv = snap["atm_iv"]
            pcr = snap["pcr"]
            chain = snap.get("chain", {})

            # Technical indicators for the underlying
            indicators = self._get_kite_indicators(symbol, self.exchange)
            trend = indicators.get("trend", "unknown") if indicators else "unknown"
            rsi = indicators.get("rsi", "N/A") if indicators else "N/A"
            rsi_zone = indicators.get("rsi_zone", "") if indicators else ""

            # PCR signal
            if pcr > 1.2:
                pcr_signal = "BULLISH support (high put writing)"
            elif pcr < 0.8:
                pcr_signal = "BEARISH resistance (high call writing)"
            else:
                pcr_signal = "NEUTRAL"

            # IV guidance
            if atm_iv and atm_iv < 20:
                iv_note = "IDEAL buying conditions"
            elif atm_iv and atm_iv <= 35:
                iv_note = "normal"
            elif atm_iv and atm_iv > 40:
                iv_note = "EXPENSIVE — avoid buying"
            else:
                iv_note = "N/A"

            lines.append(
                f"{symbol}: ₹{up:.2f} ({'+' if upct >= 0 else ''}{upct:.2f}%) | "
                f"Trend: {trend} | RSI: {rsi} ({rsi_zone})"
            )
            lines.append(
                f"  Expiry: {expiry} | DTE: {dte} | Lot Size: {lot_size}"
            )
            lines.append(
                f"  ATM {atm}: CE ₹{atm_ce_p:.2f} (OI:{atm_ce_oi:,}) | PE ₹{atm_pe_p:.2f} (OI:{atm_pe_oi:,})"
                + (f" | IV: {atm_iv:.1f}% ({iv_note})" if atm_iv else "")
            )
            lines.append(f"  PCR: {pcr} — {pcr_signal}")

            # Tradable options (exact symbols for brain to use — no duplicate table)
            lines.append(f"  OPTIONS (symbol | type | premium | OI):")
            for ts, v in sorted(chain.items()):
                direction = "CALL (BUY if bullish)" if v["type"] == "CE" else "PUT (BUY if bearish)"
                lines.append(f"    {ts} — {direction} | Premium: ₹{v['premium']:.2f} | OI: {v['oi']:,}")

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

                df = self.bd.get_historical_data(symbol, interval=BINANCE_INTERVAL, limit=50)
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
