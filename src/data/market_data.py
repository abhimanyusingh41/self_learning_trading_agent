from datetime import datetime, date
from typing import Optional
import time
import pandas as pd
from kiteconnect import KiteConnect
from loguru import logger


# Base names to match MCX futures contracts (e.g. GOLD -> GOLD24MAYFUT)
MCX_BASE_NAMES = {"GOLD", "GOLDM", "GOLDPETAL", "SILVER", "SILVERM", "SILVERMIC", "CRUDEOIL", "NATURALGAS"}

KITE_TIMEOUT = 15        # seconds — Kite can be slow during market hours
KITE_RETRY_DELAY = 3     # seconds between retries
KITE_MAX_RETRIES = 2

# Module-level NFO instruments cache — persists across MarketData instances, refreshed daily
_NFO_INSTRUMENTS_CACHE: list = []
_NFO_INSTRUMENTS_DATE: Optional[date] = None


class MarketData:
    def __init__(self, api_key: str, access_token: str):
        self.kite = KiteConnect(api_key=api_key, timeout=KITE_TIMEOUT)
        self.kite.set_access_token(access_token)
        self._mcx_contract_cache: dict[str, str] = {}  # base -> active contract symbol
        self._price_cache: dict[str, float] = {}       # last known prices for fallback

    def _kite_quote(self, instruments: list[str]) -> dict:
        """Call kite.quote() with retry on timeout. Returns cached values on persistent failure."""
        for attempt in range(1, KITE_MAX_RETRIES + 1):
            try:
                data = self.kite.quote(instruments)
                # Update price cache on success
                for key, q in data.items():
                    if q.get("last_price"):
                        self._price_cache[key] = q["last_price"]
                return data
            except Exception as e:
                if attempt < KITE_MAX_RETRIES:
                    logger.warning(f"Kite API timeout (attempt {attempt}/{KITE_MAX_RETRIES}), retrying in {KITE_RETRY_DELAY}s: {e}")
                    time.sleep(KITE_RETRY_DELAY)
                else:
                    logger.error(f"Kite API failed after {KITE_MAX_RETRIES} attempts: {e}")
        # Return synthetic quotes from cache for instruments we've seen before
        fallback = {}
        for inst in instruments:
            if inst in self._price_cache:
                fallback[inst] = {"last_price": self._price_cache[inst], "net_change": 0, "change": 0, "volume": 0}
                logger.warning(f"Using cached price for {inst}: {self._price_cache[inst]}")
        return fallback

    def get_quote(self, symbols: list[str], exchange: str = "NSE") -> dict:
        instruments = [f"{exchange}:{s}" for s in symbols]
        return self._kite_quote(instruments)

    def get_historical_data(
        self,
        symbol: str,
        from_date: date,
        to_date: date,
        interval: str = "5minute",
        exchange: str = "NSE",
        continuous: bool = False,
    ) -> pd.DataFrame:
        try:
            instrument_token = self._get_instrument_token(symbol, exchange)
            if not instrument_token:
                return pd.DataFrame()
            records = self.kite.historical_data(
                instrument_token, from_date, to_date, interval, continuous=continuous
            )
            df = pd.DataFrame(records)
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                df.set_index("date", inplace=True)
            return df
        except Exception as e:
            logger.error(f"Failed to get historical data for {symbol}: {e}")
            return pd.DataFrame()

    def get_intraday_data(
        self, symbol: str, interval: str = "5minute", exchange: str = "NSE"
    ) -> pd.DataFrame:
        today = datetime.now().date()
        return self.get_historical_data(symbol, today, today, interval, exchange)

    def get_oi_data(self, symbol: str, exchange: str = "NFO") -> dict:
        """Get open interest data for F&O instruments."""
        try:
            instrument = f"{exchange}:{symbol}"
            quote = self._kite_quote([instrument])
            data = quote.get(instrument, {})
            return {
                "oi": data.get("oi", 0),
                "oi_day_high": data.get("oi_day_high", 0),
                "oi_day_low": data.get("oi_day_low", 0),
                "last_price": data.get("last_price", 0),
            }
        except Exception as e:
            logger.error(f"Failed to get OI data for {symbol}: {e}")
            return {}

    def get_nifty_quote(self) -> dict:
        data = self._kite_quote(["NSE:NIFTY 50"])
        return data.get("NSE:NIFTY 50", {})

    def get_banknifty_quote(self) -> dict:
        data = self._kite_quote(["NSE:NIFTY BANK"])
        return data.get("NSE:NIFTY BANK", {})

    def get_indices_data(self) -> dict:
        """Fetch NIFTY50, BANKNIFTY, and India VIX in one call."""
        data = self._kite_quote(["NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:INDIA VIX"])
        return {
            "nifty": data.get("NSE:NIFTY 50", {}),
            "banknifty": data.get("NSE:NIFTY BANK", {}),
            "india_vix": data.get("NSE:INDIA VIX", {}),
        }

    def get_positions(self) -> dict:
        try:
            return self.kite.positions()
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return {"net": [], "day": []}

    def get_holdings(self) -> list:
        try:
            return self.kite.holdings()
        except Exception as e:
            logger.error(f"Failed to get holdings: {e}")
            return []

    def resolve_mcx_symbol(self, base: str) -> Optional[str]:
        """Resolve a base MCX name (e.g. 'GOLD') to the nearest active futures contract."""
        if base in self._mcx_contract_cache:
            return self._mcx_contract_cache[base]
        try:
            instruments = self.kite.instruments("MCX")
            now = datetime.now()
            # Filter futures for this base, pick nearest expiry >= today
            candidates = [
                i for i in instruments
                if i["tradingsymbol"].startswith(base)
                and i["instrument_type"] == "FUT"
                and i["expiry"] >= now.date()
            ]
            if not candidates:
                logger.warning(f"No active MCX futures found for base: {base}")
                return None
            # Sort by expiry ascending, pick nearest
            candidates.sort(key=lambda x: x["expiry"])
            active = candidates[0]["tradingsymbol"]
            self._mcx_contract_cache[base] = active
            logger.info(f"Resolved MCX {base} -> {active} (expiry: {candidates[0]['expiry']})")
            return active
        except Exception as e:
            logger.error(f"Failed to resolve MCX symbol for {base}: {e}")
            return None

    def get_mcx_quote(self, base_symbols: list[str]) -> dict:
        """Get MCX quotes using auto-resolved active contract names."""
        result = {}
        for base in base_symbols:
            active = self.resolve_mcx_symbol(base)
            if not active:
                continue
            try:
                data = self._kite_quote([f"MCX:{active}"])
                q = data.get(f"MCX:{active}", {})
                if q:
                    q["active_contract"] = active
                    result[base] = q
            except Exception as e:
                logger.error(f"Failed to get MCX quote for {base} ({active}): {e}")
        return result

    def _get_nfo_instruments(self) -> list:
        """Return NFO instruments list, refreshing the module-level cache once per day."""
        global _NFO_INSTRUMENTS_CACHE, _NFO_INSTRUMENTS_DATE
        today = date.today()
        if _NFO_INSTRUMENTS_DATE == today and _NFO_INSTRUMENTS_CACHE:
            return _NFO_INSTRUMENTS_CACHE
        try:
            instruments = self.kite.instruments("NFO")
            _NFO_INSTRUMENTS_CACHE = instruments
            _NFO_INSTRUMENTS_DATE = today
            logger.info(f"NFO instruments cache refreshed: {len(instruments)} instruments")
        except Exception as e:
            logger.error(f"Failed to fetch NFO instruments: {e}")
        return _NFO_INSTRUMENTS_CACHE

    def get_option_chain_snapshot(self, symbol: str, num_strikes: int = 3, lot_size: int = 1) -> dict:
        """
        Fetch a structured option chain snapshot for a single underlying.

        Returns a dict with:
          symbol, underlying_price, expiry, dte, lot_size,
          atm_strike, atm_call_premium, atm_put_premium,
          atm_call_oi, atm_put_oi, atm_iv, pcr,
          chain: {tradingsymbol: {strike, type, premium, oi, volume, iv}}
        """
        today = date.today()

        # --- Step 1: Get underlying price from NSE ---
        try:
            nse_data = self._kite_quote([f"NSE:{symbol}"])
            underlying_q = nse_data.get(f"NSE:{symbol}", {})
            underlying_price = underlying_q.get("last_price", 0)
            underlying_pct = underlying_q.get("change", 0)
        except Exception as e:
            logger.error(f"Failed to get underlying price for {symbol}: {e}")
            return {"symbol": symbol, "error": "underlying_price_unavailable"}

        if not underlying_price:
            return {"symbol": symbol, "error": "underlying_price_zero"}

        # --- Step 2: Filter NFO instruments for this underlying, CE/PE only ---
        try:
            all_nfo = self._get_nfo_instruments()
            # Filter to options (CE/PE) for this underlying, nearest expiry >= today
            candidates = [
                i for i in all_nfo
                if i.get("name") == symbol
                and i.get("instrument_type") in ("CE", "PE")
                and i.get("expiry") is not None
                and i.get("expiry") >= today
            ]
            if not candidates:
                logger.warning(f"No NFO options found for {symbol}")
                return {
                    "symbol": symbol,
                    "underlying_price": underlying_price,
                    "underlying_pct": underlying_pct,
                    "error": "option_data_unavailable",
                }

            # Find nearest expiry
            nearest_expiry = min(i["expiry"] for i in candidates)
            dte = (nearest_expiry - today).days

            # Filter to nearest expiry only
            expiry_instruments = [i for i in candidates if i["expiry"] == nearest_expiry]

        except Exception as e:
            logger.error(f"Failed to filter NFO instruments for {symbol}: {e}")
            return {
                "symbol": symbol,
                "underlying_price": underlying_price,
                "underlying_pct": underlying_pct,
                "error": "option_data_unavailable",
            }

        # --- Step 3: Find ATM strike ---
        strikes = sorted(set(i["strike"] for i in expiry_instruments))
        if not strikes:
            return {
                "symbol": symbol,
                "underlying_price": underlying_price,
                "underlying_pct": underlying_pct,
                "error": "no_strikes_found",
            }
        atm_strike = min(strikes, key=lambda s: abs(s - underlying_price))

        # --- Step 4: Select ATM ± num_strikes strikes ---
        atm_idx = strikes.index(atm_strike)
        lo = max(0, atm_idx - num_strikes)
        hi = min(len(strikes) - 1, atm_idx + num_strikes)
        selected_strikes = strikes[lo: hi + 1]

        # Build list of tradingsymbols for selected strikes (both CE and PE)
        selected_instruments = [
            i for i in expiry_instruments
            if i["strike"] in selected_strikes
        ]

        # --- Step 5: Batch-fetch quotes (groups of 200) ---
        tradingsymbols = [f"NFO:{i['tradingsymbol']}" for i in selected_instruments]
        quotes: dict = {}
        batch_size = 200
        for batch_start in range(0, len(tradingsymbols), batch_size):
            batch = tradingsymbols[batch_start: batch_start + batch_size]
            try:
                batch_quotes = self._kite_quote(batch)
                quotes.update(batch_quotes)
            except Exception as e:
                logger.error(f"Failed to fetch option quotes batch for {symbol}: {e}")

        # --- Step 6: Build chain dict ---
        chain: dict = {}
        for inst in selected_instruments:
            ts = inst["tradingsymbol"]
            key = f"NFO:{ts}"
            q = quotes.get(key, {})
            chain[ts] = {
                "strike": inst["strike"],
                "type": inst["instrument_type"],  # CE or PE
                "premium": q.get("last_price", 0),
                "oi": q.get("oi", 0),
                "volume": q.get("volume", 0),
                "iv": q.get("implied_volatility", 0),
            }

        # --- Step 7: Extract ATM call/put details ---
        atm_ce_ts = next(
            (i["tradingsymbol"] for i in selected_instruments
             if i["strike"] == atm_strike and i["instrument_type"] == "CE"),
            None,
        )
        atm_pe_ts = next(
            (i["tradingsymbol"] for i in selected_instruments
             if i["strike"] == atm_strike and i["instrument_type"] == "PE"),
            None,
        )

        atm_call_premium = chain.get(atm_ce_ts, {}).get("premium", 0) if atm_ce_ts else 0
        atm_put_premium = chain.get(atm_pe_ts, {}).get("premium", 0) if atm_pe_ts else 0
        atm_call_oi = chain.get(atm_ce_ts, {}).get("oi", 0) if atm_ce_ts else 0
        atm_put_oi = chain.get(atm_pe_ts, {}).get("oi", 0) if atm_pe_ts else 0
        atm_iv = chain.get(atm_ce_ts, {}).get("iv", 0) if atm_ce_ts else 0

        # PCR = total PE OI / total CE OI for selected strikes
        total_ce_oi = sum(v["oi"] for v in chain.values() if v["type"] == "CE") or 1
        total_pe_oi = sum(v["oi"] for v in chain.values() if v["type"] == "PE")
        pcr = round(total_pe_oi / total_ce_oi, 2)

        return {
            "symbol": symbol,
            "underlying_price": underlying_price,
            "underlying_pct": underlying_pct,
            "expiry": nearest_expiry.isoformat(),
            "dte": dte,
            "lot_size": lot_size,
            "atm_strike": atm_strike,
            "atm_call_premium": atm_call_premium,
            "atm_put_premium": atm_put_premium,
            "atm_call_oi": atm_call_oi,
            "atm_put_oi": atm_put_oi,
            "atm_iv": atm_iv,
            "pcr": pcr,
            "chain": chain,
        }

    def get_option_quote(self, tradingsymbol: str) -> float:
        """Fetch live premium for a single option tradingsymbol (e.g. 'HDFCBANK26MAY785PE')."""
        try:
            key = f"NFO:{tradingsymbol}"
            data = self._kite_quote([key])
            return data.get(key, {}).get("last_price", 0.0)
        except Exception as e:
            logger.error(f"Failed to get option quote for {tradingsymbol}: {e}")
            return 0.0

    def get_option_lot_size(self, tradingsymbol: str) -> int:
        """Fetch lot_size for a specific option tradingsymbol live from Kite at trade time (no cache)."""
        try:
            instruments = self.kite.instruments("NFO")
            for inst in instruments:
                if inst["tradingsymbol"] == tradingsymbol:
                    lot_size = inst.get("lot_size", 0)
                    if lot_size:
                        logger.info(f"Lot size for {tradingsymbol}: {lot_size} (from Kite)")
                        return lot_size
            logger.warning(f"Lot size not found for {tradingsymbol} in NFO instruments")
            return 0
        except Exception as e:
            logger.error(f"Failed to fetch lot size for {tradingsymbol}: {e}")
            return 0

    def get_underlying_lot_size(self, symbol: str) -> int:
        """Get lot_size for an underlying symbol (e.g. 'RELIANCE') from the daily NFO cache."""
        try:
            instruments = self._get_nfo_instruments()
            for inst in instruments:
                if inst.get("name") == symbol and inst.get("instrument_type") in ("CE", "PE"):
                    lot_size = inst.get("lot_size", 0)
                    if lot_size:
                        return lot_size
            logger.warning(f"Lot size not found for underlying {symbol}")
            return 1
        except Exception as e:
            logger.error(f"Failed to get lot size for underlying {symbol}: {e}")
            return 1

    def _get_instrument_token(self, symbol: str, exchange: str) -> Optional[int]:
        # For MCX base names, auto-resolve to active contract first
        actual_symbol = symbol
        if exchange == "MCX" and symbol in MCX_BASE_NAMES:
            resolved = self.resolve_mcx_symbol(symbol)
            if resolved:
                actual_symbol = resolved
            else:
                return None
        try:
            instruments = self.kite.instruments(exchange)
            for inst in instruments:
                if inst["tradingsymbol"] == actual_symbol:
                    return inst["instrument_token"]
            logger.warning(f"Instrument token not found for {exchange}:{actual_symbol}")
            return None
        except Exception as e:
            logger.error(f"Failed to fetch instruments: {e}")
            return None
