import uuid
from datetime import datetime
from typing import Optional
from loguru import logger

from src.execution.executor import BaseExecutor, OrderResult


SLIPPAGE_PCT = 0.0005  # 0.05% slippage assumption


class PaperTrader(BaseExecutor):
    def __init__(self, initial_capital: float = 100000.0):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions: dict[str, dict] = {}  # symbol -> position dict
        self.trade_log: list[dict] = []
        self.daily_pnl: float = 0.0
        self._price_cache: dict[str, float] = {}

    def set_price_feed(self, price_fn):
        """Inject a function(symbol) -> float for live price lookups."""
        self._price_fn = price_fn

    def get_current_price(self, symbol: str, exchange: str = "NSE") -> float:
        if hasattr(self, "_price_fn"):
            try:
                price = self._price_fn(symbol)
                if price:
                    self._price_cache[symbol] = price  # keep cache fresh
                    return price
            except Exception:
                pass
        # Fall back to last known price — better than 0 for SL/target checks
        return self._price_cache.get(symbol, 0.0)

    def update_price(self, symbol: str, price: float):
        self._price_cache[symbol] = price

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
        # Apply slippage
        if action in ("BUY", "COVER"):
            exec_price = price * (1 + SLIPPAGE_PCT)
        else:
            exec_price = price * (1 - SLIPPAGE_PCT)

        trade_value = exec_price * quantity

        if action in ("BUY", "COVER"):
            if trade_value > self.cash:
                return OrderResult(
                    False, None, symbol, action, quantity, exec_price,
                    f"Insufficient cash: need ₹{trade_value:.0f}, have ₹{self.cash:.0f}"
                )
            self.cash -= trade_value

        order_id = str(uuid.uuid4())[:10]

        if action in ("BUY", "SHORT"):
            self.positions[symbol] = {
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "entry_price": exec_price,
                "stop_loss": stop_loss,
                "target": target,
                "order_id": order_id,
                "entry_time": datetime.now().isoformat(),
            }
        elif action in ("SELL", "COVER"):
            self._close_position_internal(symbol, exec_price, "signal")

        self.trade_log.append({
            "order_id": order_id,
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "price": exec_price,
            "time": datetime.now().isoformat(),
        })

        logger.info(f"[PAPER] {action} {quantity} {symbol} @ ₹{exec_price:.2f}")
        return OrderResult(True, order_id, symbol, action, quantity, exec_price, "Paper order filled")

    def close_position(
        self,
        symbol: str,
        quantity: int,
        current_price: float,
        reason: str,
        exchange: str = "NSE",
    ) -> OrderResult:
        if symbol not in self.positions:
            return OrderResult(False, None, symbol, "CLOSE", quantity, current_price, "No open position")

        pos = self.positions[symbol]
        action = pos["action"]
        exit_action = "SELL" if action == "BUY" else "COVER"
        exec_price = current_price * (1 - SLIPPAGE_PCT if exit_action == "SELL" else 1 + SLIPPAGE_PCT)

        pnl = self._close_position_internal(symbol, exec_price, reason)
        order_id = str(uuid.uuid4())[:10]

        logger.info(f"[PAPER] {exit_action} {pos['quantity']} {symbol} @ ₹{exec_price:.2f} | PnL: ₹{pnl:.2f} | {reason}")
        return OrderResult(True, order_id, symbol, exit_action, pos["quantity"], exec_price, f"Closed: {reason}")

    def _close_position_internal(self, symbol: str, exit_price: float, reason: str) -> float:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return 0.0

        qty = pos["quantity"]
        entry = pos["entry_price"]
        action = pos["action"]

        if action == "BUY":
            pnl = (exit_price - entry) * qty
            self.cash += exit_price * qty
        else:  # SHORT
            pnl = (entry - exit_price) * qty
            self.cash += exit_price * qty  # return borrowed shares' value

        self.daily_pnl += pnl
        self.trade_log.append({
            "symbol": symbol,
            "action": "CLOSE",
            "entry": entry,
            "exit": exit_price,
            "quantity": qty,
            "pnl": round(pnl, 2),
            "reason": reason,
            "time": datetime.now().isoformat(),
        })
        return pnl

    def get_portfolio_value(self) -> float:
        unrealised = sum(
            (self.get_current_price(s) - p["entry_price"]) * p["quantity"]
            if p["action"] == "BUY"
            else (p["entry_price"] - self.get_current_price(s)) * p["quantity"]
            for s, p in self.positions.items()
        )
        return self.cash + unrealised

    def get_daily_pnl(self) -> float:
        return self.daily_pnl

    def get_open_positions(self) -> list[dict]:
        return list(self.positions.values())

    def reset_daily_pnl(self):
        self.daily_pnl = 0.0

    def portfolio_summary(self) -> str:
        lines = [
            f"Cash: ₹{self.cash:,.2f}",
            f"Portfolio Value: ₹{self.get_portfolio_value():,.2f}",
            f"Daily PnL: ₹{self.daily_pnl:,.2f}",
            f"Open Positions: {len(self.positions)}",
        ]
        for sym, pos in self.positions.items():
            cur = self.get_current_price(sym)
            unreal = (cur - pos["entry_price"]) * pos["quantity"] if pos["action"] == "BUY" else (pos["entry_price"] - cur) * pos["quantity"]
            lines.append(
                f"  {sym}: {pos['action']} {pos['quantity']} @ ₹{pos['entry_price']:.2f} | "
                f"CMP: ₹{cur:.2f} | Unrealised: ₹{unreal:.2f} | SL: ₹{pos['stop_loss']:.2f}"
            )
        return "\n".join(lines)
