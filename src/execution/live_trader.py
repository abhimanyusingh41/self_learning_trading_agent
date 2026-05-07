from loguru import logger
from kiteconnect import KiteConnect

from src.execution.executor import BaseExecutor, OrderResult


class LiveTrader(BaseExecutor):
    """
    Real-money execution via Kite Connect.
    All orders are limit orders; SL is placed immediately after entry fill.
    """

    def __init__(self, api_key: str, access_token: str):
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)
        logger.warning("LiveTrader initialised — REAL MONEY MODE ACTIVE")

    def place_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        stop_loss: float,
        target: float,
        exchange: str = "NSE",
    ) -> OrderResult:
        try:
            transaction = (
                self.kite.TRANSACTION_TYPE_BUY
                if action in ("BUY", "COVER")
                else self.kite.TRANSACTION_TYPE_SELL
            )

            order_id = self.kite.place_order(
                tradingsymbol=symbol,
                exchange=exchange,
                transaction_type=transaction,
                quantity=quantity,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=round(price, 2),
                product=self.kite.PRODUCT_MIS,  # Intraday
                variety=self.kite.VARIETY_REGULAR,
            )

            logger.info(f"[LIVE] Order placed: {action} {quantity} {symbol} @ ₹{price:.2f} | ID: {order_id}")

            # Place SL order immediately after entry
            self._place_sl_order(symbol, action, quantity, stop_loss, exchange)

            return OrderResult(
                True, str(order_id), symbol, action, quantity, price,
                f"Order placed: {order_id}"
            )

        except Exception as e:
            logger.error(f"[LIVE] Order failed for {symbol}: {e}")
            return OrderResult(False, None, symbol, action, quantity, price, str(e))

    def _place_sl_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        stop_loss: float,
        exchange: str,
    ):
        """Place stop-loss order after entry."""
        try:
            sl_transaction = (
                self.kite.TRANSACTION_TYPE_SELL
                if action in ("BUY", "COVER")
                else self.kite.TRANSACTION_TYPE_BUY
            )
            sl_order_id = self.kite.place_order(
                tradingsymbol=symbol,
                exchange=exchange,
                transaction_type=sl_transaction,
                quantity=quantity,
                order_type=self.kite.ORDER_TYPE_SL,
                price=round(stop_loss * 0.999, 2),  # limit price slightly below trigger
                trigger_price=round(stop_loss, 2),
                product=self.kite.PRODUCT_MIS,
                variety=self.kite.VARIETY_REGULAR,
            )
            logger.info(f"[LIVE] SL order placed @ ₹{stop_loss:.2f} | ID: {sl_order_id}")
        except Exception as e:
            logger.error(f"[LIVE] SL order failed for {symbol}: {e}")

    def close_position(
        self,
        symbol: str,
        quantity: int,
        current_price: float,
        reason: str,
        exchange: str = "NSE",
    ) -> OrderResult:
        try:
            # Determine current position direction from positions
            positions = self.kite.positions()
            net_positions = positions.get("net", [])
            pos = next((p for p in net_positions if p["tradingsymbol"] == symbol), None)

            if not pos or pos["quantity"] == 0:
                return OrderResult(False, None, symbol, "CLOSE", quantity, current_price, "No open position found")

            qty = abs(pos["quantity"])
            transaction = (
                self.kite.TRANSACTION_TYPE_SELL
                if pos["quantity"] > 0
                else self.kite.TRANSACTION_TYPE_BUY
            )

            order_id = self.kite.place_order(
                tradingsymbol=symbol,
                exchange=exchange,
                transaction_type=transaction,
                quantity=qty,
                order_type=self.kite.ORDER_TYPE_MARKET,
                product=self.kite.PRODUCT_MIS,
                variety=self.kite.VARIETY_REGULAR,
            )

            logger.info(f"[LIVE] Close order placed: {symbol} {qty} @ market | Reason: {reason} | ID: {order_id}")
            return OrderResult(True, str(order_id), symbol, "CLOSE", qty, current_price, f"Close: {reason}")

        except Exception as e:
            logger.error(f"[LIVE] Close position failed for {symbol}: {e}")
            return OrderResult(False, None, symbol, "CLOSE", quantity, current_price, str(e))

    def get_current_price(self, symbol: str, exchange: str = "NSE") -> float:
        try:
            quote = self.kite.quote([f"{exchange}:{symbol}"])
            return quote.get(f"{exchange}:{symbol}", {}).get("last_price", 0.0)
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return 0.0

    def get_portfolio_value(self) -> float:
        try:
            margins = self.kite.margins("equity")
            return float(margins.get("net", 0))
        except Exception as e:
            logger.error(f"Failed to get portfolio value: {e}")
            return 0.0

    def get_daily_pnl(self) -> float:
        try:
            positions = self.kite.positions()
            return sum(
                p.get("realised", 0) + p.get("unrealised", 0)
                for p in positions.get("net", [])
            )
        except Exception as e:
            logger.error(f"Failed to get daily PnL: {e}")
            return 0.0

    def get_open_positions(self) -> list[dict]:
        try:
            positions = self.kite.positions()
            return [p for p in positions.get("net", []) if p.get("quantity", 0) != 0]
        except Exception as e:
            logger.error(f"Failed to get open positions: {e}")
            return []
