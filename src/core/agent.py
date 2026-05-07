import time
from datetime import datetime
from pathlib import Path
import pytz
from loguru import logger

from src.core.brain import TradingBrain, TradeDecision
from src.core.memory import TradeMemory
from src.risk.risk_manager import RiskManager
from src.analysis.market_analyzer import MarketAnalyzer
from src.analysis.trade_reflector import TradeReflector
from src.execution.executor import BaseExecutor


IST = pytz.timezone("Asia/Kolkata")


class TradingAgent:
    def __init__(
        self,
        config: dict,
        brain: TradingBrain,
        memory: TradeMemory,
        risk_manager: RiskManager,
        market_analyzer: MarketAnalyzer,
        executor: BaseExecutor,
    ):
        self.config = config
        self.brain = brain
        self.memory = memory
        self.risk = risk_manager
        self.analyzer = market_analyzer
        self.executor = executor
        self.reflector = TradeReflector(brain, memory)
        self._running = False
        self._last_market_context: str = ""

    def run_once(self):
        """Execute one analysis-decision-execution cycle."""
        now_ist = datetime.now(IST)

        if not self._is_market_open(now_ist):
            logger.info("Market is closed. Skipping cycle.")
            return

        # Check daily loss limit
        daily_pnl = self.executor.get_daily_pnl()
        trading_ok, reason = self.risk.is_trading_allowed(daily_pnl)
        if not trading_ok:
            logger.warning(f"Trading halted: {reason}")
            return

        # Check open positions against SL/targets
        self._check_exit_conditions()

        # Build market context
        logger.info("Building market context...")
        market_context = self.analyzer.build_market_context()
        self._last_market_context = market_context

        # Portfolio state
        portfolio_str = self._format_portfolio()
        capital = self.executor.get_portfolio_value()

        # Past lessons
        lessons = self.memory.get_relevant_lessons(limit=10)
        stats = self.memory.get_stats_summary()
        lessons_text = f"PERFORMANCE:\n{stats}\n\nRECENT LESSONS:\n{lessons}"

        # Brain decides
        logger.info("Asking brain for trade decision...")
        decision = self.brain.analyze_and_decide(market_context, lessons_text, portfolio_str, capital)

        logger.info(
            f"Decision: {decision.action} | Symbol: {decision.symbol} | "
            f"Confidence: {decision.confidence:.2f} | Setup: {decision.setup_type}"
        )

        if decision.action in ("WAIT", "HOLD"):
            logger.info(f"Brain says wait: {decision.rationale[:200]}")
            return

        # Risk check
        open_positions = self.executor.get_open_positions()
        risk_result = self.risk.check_trade(
            action=decision.action,
            entry_price=decision.entry_price or 0,
            stop_loss=decision.stop_loss or 0,
            quantity=decision.quantity or 0,
            current_capital=capital,
            daily_pnl=daily_pnl,
            open_positions=open_positions,
        )

        if not risk_result.allowed:
            logger.warning(f"Risk check BLOCKED trade: {risk_result.reason}")
            if risk_result.max_quantity > 0:
                logger.info(f"Adjusted quantity: {risk_result.max_quantity}")
                decision.quantity = risk_result.max_quantity
            else:
                return

        # Execute
        self._execute_decision(decision)

    def _execute_decision(self, decision: TradeDecision):
        result = self.executor.place_order(
            symbol=decision.symbol,
            action=decision.action,
            quantity=decision.quantity,
            price=decision.entry_price,
            stop_loss=decision.stop_loss,
            target=decision.target_1,
        )

        if result.success:
            trade_id = self.memory.record_trade_entry({
                "symbol": decision.symbol,
                "action": decision.action,
                "quantity": decision.quantity,
                "entry_price": result.price,
                "stop_loss": decision.stop_loss,
                "target_1": decision.target_1,
                "target_2": decision.target_2,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "key_risks": decision.key_risks,
                "setup_type": decision.setup_type,
                "time_horizon": decision.time_horizon,
                "market_context_snapshot": self._last_market_context[:2000],
                "order_id": result.order_id,
            })
            logger.info(f"Trade recorded: {trade_id} — {decision.action} {decision.symbol}")
        else:
            logger.error(f"Order failed: {result.message}")

    def _check_exit_conditions(self):
        """Check all open trades for SL/target hits or EOD close."""
        open_trades = self.memory.get_open_trades()
        now_ist = datetime.now(IST)

        for trade in open_trades:
            symbol = trade["symbol"]
            current_price = self.executor.get_current_price(symbol)
            if not current_price:
                continue

            action = trade["action"]
            sl = float(trade.get("stop_loss", 0))
            t1 = float(trade.get("target_1", 0))
            should_close = False
            close_reason = ""

            if action == "BUY":
                if sl and current_price <= sl:
                    should_close, close_reason = True, "stop_loss_hit"
                elif t1 and current_price >= t1:
                    should_close, close_reason = True, "target_1_hit"
            elif action == "SHORT":
                if sl and current_price >= sl:
                    should_close, close_reason = True, "stop_loss_hit"
                elif t1 and current_price <= t1:
                    should_close, close_reason = True, "target_1_hit"

            # EOD auto-close at 15:15
            if now_ist.hour == 15 and now_ist.minute >= 15 and trade.get("time_horizon") == "intraday":
                should_close, close_reason = True, "eod_auto_close"

            if should_close:
                result = self.executor.close_position(
                    symbol=symbol,
                    quantity=int(trade["quantity"]),
                    current_price=current_price,
                    reason=close_reason,
                )
                if result.success:
                    self.reflector.reflect_and_learn(
                        trade_id=trade["trade_id"],
                        exit_price=result.price,
                        exit_reason=close_reason,
                        market_context_at_exit=self._last_market_context[:1000],
                    )

    def _format_portfolio(self) -> str:
        positions = self.executor.get_open_positions()
        capital = self.executor.get_portfolio_value()
        daily_pnl = self.executor.get_daily_pnl()

        lines = [
            f"Available Capital: ₹{capital:,.2f}",
            f"Daily PnL: ₹{daily_pnl:,.2f}",
            f"Open Positions: {len(positions)}",
        ]
        for pos in positions:
            cur = self.executor.get_current_price(pos.get("symbol", ""))
            lines.append(
                f"  {pos.get('symbol')}: {pos.get('action')} {pos.get('quantity')} "
                f"@ ₹{pos.get('entry_price', 0):.2f} | CMP: ₹{cur:.2f} | SL: ₹{pos.get('stop_loss', 0):.2f}"
            )
        return "\n".join(lines)

    def _is_market_open(self, now_ist: datetime) -> bool:
        if now_ist.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= now_ist <= market_close

    def run_scheduler(self, interval_minutes: int = 15):
        """Run continuously, calling run_once() every interval_minutes."""
        self._running = True
        logger.info(f"Agent started — analysis interval: {interval_minutes} min")
        while self._running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Agent cycle error: {e}", exc_info=True)
            time.sleep(interval_minutes * 60)

    def stop(self):
        self._running = False
        logger.info("Agent stopped.")
