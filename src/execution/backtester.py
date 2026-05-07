from datetime import datetime, date, timedelta
from typing import Optional
import pandas as pd
from loguru import logger

from src.execution.executor import BaseExecutor, OrderResult
from src.data.market_data import MarketData


SLIPPAGE_PCT = 0.0005


class Backtester(BaseExecutor):
    """
    Historical replay executor. Iterates over candle-by-candle data,
    simulating order fills at the open of the next candle after signal.
    """

    def __init__(
        self,
        market_data: MarketData,
        initial_capital: float,
        start_date: date,
        end_date: date,
        interval: str = "5minute",
        exchange: str = "NSE",
    ):
        self.md = market_data
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.start_date = start_date
        self.end_date = end_date
        self.interval = interval
        self.exchange = exchange

        self.positions: dict[str, dict] = {}
        self.trade_log: list[dict] = []
        self.daily_pnl: float = 0.0
        self._current_bar_prices: dict[str, float] = {}

    def set_bar(self, symbol_prices: dict[str, float]):
        """Called by backtest loop to advance the 'current' market prices."""
        self._current_bar_prices.update(symbol_prices)

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
        exec_price = price * (1 + SLIPPAGE_PCT) if action in ("BUY", "COVER") else price * (1 - SLIPPAGE_PCT)
        trade_value = exec_price * quantity

        if action in ("BUY",) and trade_value > self.cash:
            return OrderResult(False, None, symbol, action, quantity, exec_price, "Insufficient capital")

        if action in ("BUY", "SHORT"):
            if action == "BUY":
                self.cash -= trade_value
            self.positions[symbol] = {
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "entry_price": exec_price,
                "stop_loss": stop_loss,
                "target": target,
                "entry_time": datetime.now().isoformat(),
            }

        return OrderResult(True, f"bt_{symbol}", symbol, action, quantity, exec_price, "Backtest fill")

    def close_position(
        self,
        symbol: str,
        quantity: int,
        current_price: float,
        reason: str,
        exchange: str = "NSE",
    ) -> OrderResult:
        if symbol not in self.positions:
            return OrderResult(False, None, symbol, "CLOSE", quantity, current_price, "No position")

        pos = self.positions.pop(symbol)
        qty = pos["quantity"]
        entry = pos["entry_price"]
        action = pos["action"]
        exec_price = current_price * (1 - SLIPPAGE_PCT if action == "BUY" else 1 + SLIPPAGE_PCT)

        if action == "BUY":
            pnl = (exec_price - entry) * qty
            self.cash += exec_price * qty
        else:
            pnl = (entry - exec_price) * qty
            self.cash += exec_price * qty

        self.daily_pnl += pnl
        self.trade_log.append({
            "symbol": symbol,
            "action": action,
            "entry": entry,
            "exit": exec_price,
            "quantity": qty,
            "pnl": round(pnl, 2),
            "reason": reason,
        })

        return OrderResult(True, f"bt_close_{symbol}", symbol, "CLOSE", qty, exec_price, reason)

    def check_sl_and_targets(self):
        """Check open positions against current bar prices for SL/target hits."""
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            price = self._current_bar_prices.get(symbol, 0)
            if not price:
                continue

            action = pos["action"]
            sl = pos["stop_loss"]
            target = pos["target"]

            if action == "BUY":
                if price <= sl:
                    self.close_position(symbol, pos["quantity"], sl, "stop_loss_hit")
                elif price >= target:
                    self.close_position(symbol, pos["quantity"], target, "target_hit")
            elif action == "SHORT":
                if price >= sl:
                    self.close_position(symbol, pos["quantity"], sl, "stop_loss_hit")
                elif price <= target:
                    self.close_position(symbol, pos["quantity"], target, "target_hit")

    def get_current_price(self, symbol: str, exchange: str = "NSE") -> float:
        return self._current_bar_prices.get(symbol, 0.0)

    def get_portfolio_value(self) -> float:
        unrealised = sum(
            (self._current_bar_prices.get(s, p["entry_price"]) - p["entry_price"]) * p["quantity"]
            if p["action"] == "BUY"
            else (p["entry_price"] - self._current_bar_prices.get(s, p["entry_price"])) * p["quantity"]
            for s, p in self.positions.items()
        )
        return self.cash + unrealised

    def get_daily_pnl(self) -> float:
        return self.daily_pnl

    def get_open_positions(self) -> list[dict]:
        return list(self.positions.values())

    def reset_daily_pnl(self):
        self.daily_pnl = 0.0

    def get_backtest_report(self) -> dict:
        if not self.trade_log:
            return {"message": "No trades executed"}

        df = pd.DataFrame(self.trade_log)
        pnls = df["pnl"]
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]

        return {
            "total_trades": len(df),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate_pct": round(len(wins) / len(df) * 100, 1),
            "total_pnl": round(pnls.sum(), 2),
            "avg_win": round(wins.mean(), 2) if not wins.empty else 0,
            "avg_loss": round(losses.mean(), 2) if not losses.empty else 0,
            "profit_factor": round(wins.sum() / abs(losses.sum()), 2) if not losses.empty and losses.sum() != 0 else None,
            "max_drawdown": round(self._max_drawdown(), 2),
            "final_capital": round(self.get_portfolio_value(), 2),
            "return_pct": round((self.get_portfolio_value() - self.initial_capital) / self.initial_capital * 100, 2),
        }

    def _max_drawdown(self) -> float:
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.trade_log:
            cumulative += t["pnl"]
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
        return max_dd
