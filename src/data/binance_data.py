from datetime import datetime, timezone
from typing import Optional
import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException
from loguru import logger


class BinanceData:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.client = Client(api_key, api_secret, testnet=testnet)
        self.testnet = testnet
        logger.info(f"Binance connected ({'testnet' if testnet else 'LIVE'})")

    def get_quote(self, symbols: list[str]) -> dict:
        result = {}
        try:
            tickers = {t["symbol"]: t for t in self.client.get_all_tickers()}
            stats = {t["symbol"]: t for t in self.client.get_ticker()}
            for sym in symbols:
                ticker = tickers.get(sym, {})
                stat = stats.get(sym, {})
                result[sym] = {
                    "last_price": float(ticker.get("price", 0)),
                    "volume": float(stat.get("volume", 0)),
                    "net_change": float(stat.get("priceChange", 0)),
                    "change_pct": float(stat.get("priceChangePercent", 0)),
                    "high": float(stat.get("highPrice", 0)),
                    "low": float(stat.get("lowPrice", 0)),
                }
        except BinanceAPIException as e:
            logger.error(f"Binance quote error: {e}")
        return result

    def get_historical_data(
        self,
        symbol: str,
        interval: str = "5m",
        limit: int = 200,
    ) -> pd.DataFrame:
        """Fetch recent OHLCV candles. interval: 1m/5m/15m/1h/4h/1d"""
        try:
            klines = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
            df = pd.DataFrame(klines, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote", "ignore",
            ])
            df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df.set_index("date", inplace=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df[["open", "high", "low", "close", "volume"]]
        except BinanceAPIException as e:
            logger.error(f"Binance historical data error for {symbol}: {e}")
            return pd.DataFrame()

    def get_account_balance(self) -> dict:
        """Return USDT balance and all non-zero asset balances."""
        try:
            account = self.client.get_account()
            balances = {
                b["asset"]: float(b["free"]) + float(b["locked"])
                for b in account["balances"]
                if float(b["free"]) + float(b["locked"]) > 0
            }
            return balances
        except BinanceAPIException as e:
            logger.error(f"Binance account balance error: {e}")
            return {}

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "LIMIT",
    ) -> dict:
        try:
            if order_type == "MARKET":
                order = self.client.create_order(
                    symbol=symbol,
                    side=side,
                    type="MARKET",
                    quantity=quantity,
                )
            else:
                order = self.client.create_order(
                    symbol=symbol,
                    side=side,
                    type="LIMIT",
                    timeInForce="GTC",
                    quantity=quantity,
                    price=str(price),
                )
            logger.info(f"Binance order placed: {side} {quantity} {symbol} @ {price or 'market'}")
            return order
        except BinanceAPIException as e:
            logger.error(f"Binance order failed for {symbol}: {e}")
            return {}

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        try:
            return self.client.get_open_orders(symbol=symbol) if symbol else self.client.get_open_orders()
        except BinanceAPIException as e:
            logger.error(f"Binance open orders error: {e}")
            return []

    def cancel_order(self, symbol: str, order_id: int) -> bool:
        try:
            self.client.cancel_order(symbol=symbol, orderId=order_id)
            return True
        except BinanceAPIException as e:
            logger.error(f"Binance cancel order error: {e}")
            return False
