from dataclasses import dataclass, field
from loguru import logger


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str
    max_quantity: float = 0.0
    adjusted_size: float = 0.0


class RiskManager:
    """Hard risk limits — none of these can be overridden at runtime."""

    def __init__(self, config: dict):
        r = config.get("risk", {})
        self._daily_loss_limit_pct = float(r.get("daily_loss_limit_pct", 2.0))
        self._per_trade_loss_pct = float(r.get("per_trade_loss_pct", 0.5))
        self._max_positions = int(r.get("max_positions", 3))
        self._max_trade_value_pct = float(r.get("max_trade_value_pct", 10.0))
        self._initial_capital = float(r.get("initial_capital", 100000))
        self._kill_switch = bool(r.get("kill_switch", False))

    def check_trade(
        self,
        action: str,
        entry_price: float,
        stop_loss: float,
        quantity: int,
        current_capital: float,
        daily_pnl: float,
        open_positions: list,
    ) -> RiskCheckResult:
        if self._kill_switch:
            return RiskCheckResult(False, "Kill switch is active — all trading halted.")

        if action in ("HOLD", "WAIT"):
            return RiskCheckResult(True, "No trade action required.")

        # Daily loss limit
        daily_loss_pct = abs(daily_pnl) / self._initial_capital * 100
        if daily_pnl < 0 and daily_loss_pct >= self._daily_loss_limit_pct:
            return RiskCheckResult(
                False,
                f"Daily loss limit hit: {daily_loss_pct:.2f}% >= {self._daily_loss_limit_pct}%",
            )

        # Max concurrent positions
        if len(open_positions) >= self._max_positions and action in ("BUY", "SHORT"):
            return RiskCheckResult(
                False,
                f"Max positions reached ({self._max_positions}). Close an existing position first.",
            )

        # Validate prices
        if entry_price <= 0 or stop_loss <= 0:
            return RiskCheckResult(False, "Invalid entry price or stop loss (must be > 0).")

        risk_per_share = abs(entry_price - stop_loss)
        if risk_per_share == 0:
            return RiskCheckResult(False, "Entry price equals stop loss — no risk defined.")

        # Per-trade risk check
        trade_risk = risk_per_share * quantity
        max_allowed_risk = current_capital * self._per_trade_loss_pct / 100
        if trade_risk > max_allowed_risk:
            max_qty = self.calculate_position_size(current_capital, entry_price, stop_loss)
            return RiskCheckResult(
                False,
                f"Trade risk {trade_risk:.4f} exceeds limit {max_allowed_risk:.4f}. Max qty: {max_qty:.6f}",
                max_quantity=max_qty,
            )

        # Max trade value check
        trade_value = entry_price * quantity
        max_trade_value = current_capital * self._max_trade_value_pct / 100
        if trade_value > max_trade_value:
            max_qty_by_value = max_trade_value / entry_price
            return RiskCheckResult(
                False,
                f"Trade value {trade_value:.2f} exceeds {self._max_trade_value_pct}% of capital. Max qty: {max_qty_by_value:.6f}",
                max_quantity=max_qty_by_value,
            )

        return RiskCheckResult(True, "Risk checks passed.", max_quantity=float(quantity))

    def calculate_position_size(
        self,
        capital: float,
        entry_price: float,
        stop_loss: float,
        risk_pct: float = None,
    ) -> float:
        pct = risk_pct if risk_pct is not None else self._per_trade_loss_pct
        max_risk_amount = capital * pct / 100
        risk_per_share = abs(entry_price - stop_loss)
        if risk_per_share <= 0:
            return 0.0
        raw_qty = max_risk_amount / risk_per_share
        # Also cap by max_trade_value_pct
        max_qty_by_value = (capital * self._max_trade_value_pct / 100) / entry_price
        qty = min(raw_qty, max_qty_by_value)
        # Round to int for equity, preserve precision for crypto (fractional)
        return round(qty, 8) if qty < 1 else max(0.0, float(int(qty)))

    def is_trading_allowed(self, daily_pnl: float) -> tuple[bool, str]:
        if self._kill_switch:
            return False, "Kill switch active."
        if daily_pnl < 0:
            loss_pct = abs(daily_pnl) / self._initial_capital * 100
            if loss_pct >= self._daily_loss_limit_pct:
                return False, f"Daily loss limit hit: {loss_pct:.2f}%"
        return True, "OK"

    def update_kill_switch(self, state: bool):
        self._kill_switch = state
        logger.warning(f"Kill switch set to: {state}")

    @property
    def kill_switch(self) -> bool:
        return self._kill_switch
