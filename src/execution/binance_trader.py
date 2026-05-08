import uuid
from datetime import datetime
from typing import Optional
from loguru import logger

from src.execution.executor import BaseExecutor, OrderResult
from src.data.binance_data import BinanceData


SLIPPAGE_PCT = 0.001  # 0.1% for crypto


class BinancePaperTrader(BaseExecutor):
    """Paper trading executor for Binance crypto — simulates fills at market price."""

    def __init__(self, initial_usdt: float = 10000.0):
        self.usdt_balance = initial_usdt
        self.initial_usdt = initial_usdt
        self.positions: dict[str, dict] = {}
        self.trade_log: list[dict] = []
        self.daily_pnl: float = 0.0
        self._price_cache: dict[str, float] = {}

    def update_prices(self, prices: dict[str, float]):
        self._price_cache.update(prices)

    def get_current_price(self, symbol: str, exchange: str = "BINANCE") -> float:
        return self._price_cache.get(symbol, 0.0)

    def place_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        stop_loss: float,
        target: float,
        exchange: str = "BINANCE",
    ) -> OrderResult:
        qty = round(float(quantity), 8)  # crypto supports 8 decimal places
        exec_price = price * (1 + SLIPPAGE_PCT) if action == "BUY" else price * (1 - SLIPPAGE_PCT)
        trade_value = exec_price * qty

        if qty <= 0:
            return OrderResult(False, None, symbol, action, quantity, exec_price, "Quantity must be > 0")

        if action == "BUY":
            if trade_value > self.usdt_balance:
                return OrderResult(
                    False, None, symbol, action, quantity, exec_price,
                    f"Insufficient USDT: need {trade_value:.2f}, have {self.usdt_balance:.2f}"
                )
            self.usdt_balance -= trade_value
            self.positions[symbol] = {
                "symbol": symbol,
                "action": "BUY",
                "quantity": qty,
                "entry_price": exec_price,
                "stop_loss": stop_loss,
                "target": target,
                "entry_time": datetime.now().isoformat(),
            }
        elif action == "SELL" and symbol in self.positions:
            self._close(symbol, exec_price, "signal")

        order_id = str(uuid.uuid4())[:10]
        logger.info(f"[BINANCE PAPER] {action} {qty} {symbol} @ {exec_price:.4f} USDT")
        return OrderResult(True, order_id, symbol, action, quantity, exec_price, "Paper crypto fill")

    def close_position(
        self,
        symbol: str,
        quantity: int,
        current_price: float,
        reason: str,
        exchange: str = "BINANCE",
    ) -> OrderResult:
        if symbol not in self.positions:
            return OrderResult(False, None, symbol, "SELL", quantity, current_price, "No position")

        exec_price = current_price * (1 - SLIPPAGE_PCT)
        pnl = self._close(symbol, exec_price, reason)
        order_id = str(uuid.uuid4())[:10]
        logger.info(f"[BINANCE PAPER] SELL {symbol} @ {exec_price:.4f} | PnL: {pnl:.2f} USDT | {reason}")
        return OrderResult(True, order_id, symbol, "SELL", quantity, exec_price, reason)

    def _close(self, symbol: str, exit_price: float, reason: str) -> float:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return 0.0
        qty = pos["quantity"]
        pnl = (exit_price - pos["entry_price"]) * qty
        self.usdt_balance += exit_price * qty
        self.daily_pnl += pnl
        self.trade_log.append({
            "symbol": symbol,
            "entry": pos["entry_price"],
            "exit": exit_price,
            "quantity": qty,
            "pnl_usdt": round(pnl, 4),
            "reason": reason,
            "time": datetime.now().isoformat(),
        })
        return pnl

    def check_sl_and_targets(self):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            price = self._price_cache.get(symbol, 0)
            if not price:
                continue
            if price <= pos["stop_loss"]:
                self.close_position(symbol, int(pos["quantity"]), pos["stop_loss"], "stop_loss_hit")
            elif price >= pos["target"]:
                self.close_position(symbol, int(pos["quantity"]), pos["target"], "target_hit")

    def get_portfolio_value(self) -> float:
        unrealised = sum(
            (self._price_cache.get(s, p["entry_price"]) - p["entry_price"]) * p["quantity"]
            for s, p in self.positions.items()
        )
        return self.usdt_balance + unrealised

    def get_daily_pnl(self) -> float:
        return self.daily_pnl

    def get_open_positions(self) -> list[dict]:
        return list(self.positions.values())

    def reset_daily_pnl(self):
        self.daily_pnl = 0.0

    def portfolio_summary(self) -> str:
        lines = [
            f"USDT Balance: {self.usdt_balance:,.2f}",
            f"Portfolio Value: {self.get_portfolio_value():,.2f} USDT",
            f"Daily PnL: {self.daily_pnl:,.4f} USDT",
            f"Open Positions: {len(self.positions)}",
        ]
        for sym, pos in self.positions.items():
            cur = self._price_cache.get(sym, pos["entry_price"])
            unreal = (cur - pos["entry_price"]) * pos["quantity"]
            lines.append(
                f"  {sym}: {pos['quantity']} @ {pos['entry_price']:.4f} | "
                f"CMP: {cur:.4f} | Unrealised: {unreal:.4f} USDT | SL: {pos['stop_loss']:.4f}"
            )
        return "\n".join(lines)


class BinanceLiveTrader(BaseExecutor):
    """Real Binance execution."""

    def __init__(self, binance_data: BinanceData):
        self.bd = binance_data

    def place_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        stop_loss: float,
        target: float,
        exchange: str = "BINANCE",
    ) -> OrderResult:
        side = "BUY" if action == "BUY" else "SELL"
        result = self.bd.place_order(symbol, side, float(quantity), price, "LIMIT")
        if result:
            return OrderResult(True, str(result.get("orderId")), symbol, action, quantity, price, "Live order placed")
        return OrderResult(False, None, symbol, action, quantity, price, "Order failed")

    def close_position(
        self,
        symbol: str,
        quantity: int,
        current_price: float,
        reason: str,
        exchange: str = "BINANCE",
    ) -> OrderResult:
        result = self.bd.place_order(symbol, "SELL", float(quantity), order_type="MARKET")
        if result:
            return OrderResult(True, str(result.get("orderId")), symbol, "SELL", quantity, current_price, reason)
        return OrderResult(False, None, symbol, "SELL", quantity, current_price, "Close failed")

    def get_current_price(self, symbol: str, exchange: str = "BINANCE") -> float:
        quotes = self.bd.get_quote([symbol])
        return quotes.get(symbol, {}).get("last_price", 0.0)

    def get_portfolio_value(self) -> float:
        balances = self.bd.get_account_balance()
        return balances.get("USDT", 0.0)

    def get_daily_pnl(self) -> float:
        return 0.0  # Binance API doesn't expose daily PnL directly

    def get_open_positions(self) -> list[dict]:
        orders = self.bd.get_open_orders()
        return [{"symbol": o["symbol"], "quantity": float(o["origQty"]), "action": o["side"]} for o in orders]
