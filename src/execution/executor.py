from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    symbol: str
    action: str
    quantity: int
    price: float
    message: str


class BaseExecutor(ABC):
    @abstractmethod
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
        """Place a new entry order."""

    @abstractmethod
    def close_position(
        self,
        symbol: str,
        quantity: int,
        current_price: float,
        reason: str,
        exchange: str = "NSE",
    ) -> OrderResult:
        """Exit an open position."""

    @abstractmethod
    def get_current_price(self, symbol: str, exchange: str = "NSE") -> float:
        """Return latest price for a symbol."""

    @abstractmethod
    def get_portfolio_value(self) -> float:
        """Return total current portfolio value (cash + open positions)."""

    @abstractmethod
    def get_daily_pnl(self) -> float:
        """Return today's realised + unrealised P&L."""

    @abstractmethod
    def get_open_positions(self) -> list[dict]:
        """Return list of open position dicts."""
