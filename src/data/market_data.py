from datetime import datetime, date
from typing import Optional
import time
import pandas as pd
from kiteconnect import KiteConnect
from loguru import logger


# Base names to match MCX futures contracts (e.g. GOLD -> GOLD24MAYFUT)
MCX_BASE_NAMES = {"GOLD", "GOLDM", "GOLDPETAL", "SILVER", "SILVERM", "CRUDEOIL", "NATURALGAS"}

KITE_TIMEOUT = 15        # seconds — Kite can be slow during market hours
KITE_RETRY_DELAY = 3     # seconds between retries
KITE_MAX_RETRIES = 2


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
