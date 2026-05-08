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

# Crypto symbols traded via Binance (always open)
CRYPTO_SYMBOLS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"}


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

        # Crypto symbols from config (always tradeable)
        cfg_crypto = config.get("instruments", {}).get("crypto", [])
        self._crypto_symbols = set(cfg_crypto) if cfg_crypto else CRYPTO_SYMBOLS

    def run_once(self):
        """Execute one analysis-decision-execution cycle."""
        now_ist = datetime.now(IST)
        equity_open = self._is_equity_session(now_ist)
        commodity_open = self._is_commodity_session(now_ist)
        crypto_open = True  # Always open

        if not equity_open and not commodity_open:
            logger.info("NSE and MCX closed — crypto-only cycle")

        # Always check exit conditions (SL/target hits, EOD close)
        self._check_exit_conditions(now_ist)

        # Check daily loss limit
        daily_pnl = self.executor.get_daily_pnl()
        trading_ok, reason = self.risk.is_trading_allowed(daily_pnl)
        if not trading_ok:
            logger.warning(f"Trading halted: {reason}")
            return

        # Build market context (includes only open markets)
        logger.info(
            f"Building market context | NSE={'open' if equity_open else 'closed'} | "
            f"MCX={'open' if commodity_open else 'closed'} | Crypto=open"
        )
        market_context = self.analyzer.build_market_context()
        self._last_market_context = market_context

        # Portfolio state
        portfolio_str = self._format_portfolio()
        capital = self.executor.get_portfolio_value()

        # Past lessons
        lessons = self.memory.get_relevant_lessons(limit=10)
        stats = self.memory.get_stats_summary()
        lessons_text = f"PERFORMANCE:\n{stats}\n\nRECENT LESSONS:\n{lessons}"

        # Add session context so brain knows what it can trade
        session_note = self._session_note(equity_open, commodity_open)
        full_context = f"{session_note}\n\n{market_context}"

        # Brain decides
        logger.info("Asking brain for trade decision...")
        decision = self.brain.analyze_and_decide(full_context, lessons_text, portfolio_str, capital)

        logger.info(
            f"Decision: {decision.action} | Symbol: {decision.symbol} | "
            f"Confidence: {decision.confidence:.2f} | Setup: {decision.setup_type}"
        )

        if decision.action in ("WAIT", "HOLD"):
            logger.info(f"Brain says wait: {decision.rationale[:200]}")
            return

        # Validate: don't trade NSE stocks when market is closed
        symbol = decision.symbol or ""
        if not equity_open and symbol and symbol not in self._crypto_symbols and not self._is_mcx_symbol(symbol):
            logger.warning(f"Brain picked {symbol} but NSE is closed — skipping")
            return

        # Validate: don't trade MCX when closed
        if not commodity_open and self._is_mcx_symbol(symbol):
            logger.warning(f"Brain picked MCX symbol {symbol} but MCX is closed — skipping")
            return

        # Risk check — use USDT balance for crypto, INR capital for equities
        open_positions = self.executor.get_open_positions()
        is_crypto_trade = symbol in self._crypto_symbols

        if is_crypto_trade:
            # For crypto use USDT balance and skip INR-based risk limits
            if hasattr(self.executor, "binance_paper"):
                crypto_capital = self.executor.binance_paper.get_portfolio_value()
            elif hasattr(self.executor, "binance_live"):
                crypto_capital = self.executor.binance_live.get_portfolio_value()
            else:
                crypto_capital = capital * 0.2  # fallback: 20% of INR capital

            crypto_positions = []
            if hasattr(self.executor, "binance_paper"):
                crypto_positions = self.executor.binance_paper.get_open_positions()

            risk_result = self.risk.check_trade(
                action=decision.action,
                entry_price=decision.entry_price or 0,
                stop_loss=decision.stop_loss or 0,
                quantity=decision.quantity or 0,
                current_capital=crypto_capital,
                daily_pnl=0,
                open_positions=crypto_positions,
            )
        else:
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

        # Route to correct executor
        self._execute_decision(decision)

    def _execute_decision(self, decision: TradeDecision):
        # Crypto goes to binance_paper/binance_live if attached
        is_crypto = decision.symbol in self._crypto_symbols
        if is_crypto and hasattr(self.executor, "binance_paper"):
            exec_target = self.executor.binance_paper
        elif is_crypto and hasattr(self.executor, "binance_live"):
            exec_target = self.executor.binance_live
        else:
            exec_target = self.executor

        result = exec_target.place_order(
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
                "asset_class": "crypto" if is_crypto else "equity",
                "market_context_snapshot": self._last_market_context[:2000],
                "order_id": result.order_id,
            })
            logger.info(f"Trade recorded: {trade_id} — {decision.action} {decision.symbol}")
        else:
            logger.error(f"Order failed: {result.message}")

    def _check_exit_conditions(self, now_ist: datetime):
        """Check all open trades for SL/target hits or EOD close."""
        open_trades = self.memory.get_open_trades()

        for trade in open_trades:
            symbol = trade["symbol"]
            is_crypto = symbol in self._crypto_symbols

            # Get price from right executor
            if is_crypto and hasattr(self.executor, "binance_paper"):
                current_price = self.executor.binance_paper.get_current_price(symbol)
            elif is_crypto and hasattr(self.executor, "binance_live"):
                current_price = self.executor.binance_live.get_current_price(symbol)
            else:
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

            # EOD auto-close for intraday equity/commodity (not crypto — no EOD)
            if (not is_crypto and now_ist.hour == 15 and now_ist.minute >= 15
                    and trade.get("time_horizon") == "intraday"):
                should_close, close_reason = True, "eod_auto_close"

            if should_close:
                exec_target = self.executor
                if is_crypto and hasattr(self.executor, "binance_paper"):
                    exec_target = self.executor.binance_paper
                elif is_crypto and hasattr(self.executor, "binance_live"):
                    exec_target = self.executor.binance_live

                result = exec_target.close_position(
                    symbol=symbol,
                    quantity=float(trade["quantity"]),
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
            f"Open Positions (equity/commodity): {len(positions)}",
        ]
        for pos in positions:
            cur = self.executor.get_current_price(pos.get("symbol", ""))
            lines.append(
                f"  {pos.get('symbol')}: {pos.get('action')} {pos.get('quantity')} "
                f"@ ₹{pos.get('entry_price', 0):.2f} | CMP: ₹{cur:.2f} | SL: ₹{pos.get('stop_loss', 0):.2f}"
            )

        # Crypto portfolio
        if hasattr(self.executor, "binance_paper"):
            bp = self.executor.binance_paper
            lines.append(f"\nCrypto Portfolio (USDT): {bp.get_portfolio_value():.2f}")
            for pos in bp.get_open_positions():
                cur = bp.get_current_price(pos.get("symbol", ""))
                lines.append(
                    f"  {pos.get('symbol')}: {pos.get('action')} {pos.get('quantity')} "
                    f"@ {pos.get('entry_price', 0):.4f} | CMP: {cur:.4f}"
                )
        return "\n".join(lines)

    def _session_note(self, equity_open: bool, commodity_open: bool) -> str:
        lines = ["=== TRADING SESSION STATUS ==="]
        lines.append(f"NSE Equities: {'OPEN ✓' if equity_open else 'CLOSED — do NOT pick NSE stocks'}")
        lines.append(f"MCX Commodities: {'OPEN ✓' if commodity_open else 'CLOSED — do NOT pick MCX symbols'}")
        lines.append("Crypto (Binance): OPEN 24x7 ✓ — BTC/ETH/BNB/SOL/XRP available")
        if not equity_open and not commodity_open:
            lines.append("\nINSTRUCTION: Only crypto trades are valid right now.")
        return "\n".join(lines)

    def _is_equity_session(self, now_ist: datetime) -> bool:
        if now_ist.weekday() >= 5:
            return False
        market_cfg = self.config.get("market", {})
        open_t = market_cfg.get("equity_open", "09:15").split(":")
        close_t = market_cfg.get("equity_close", "15:30").split(":")
        o = now_ist.replace(hour=int(open_t[0]), minute=int(open_t[1]), second=0, microsecond=0)
        c = now_ist.replace(hour=int(close_t[0]), minute=int(close_t[1]), second=0, microsecond=0)
        return o <= now_ist <= c

    def _is_commodity_session(self, now_ist: datetime) -> bool:
        if now_ist.weekday() >= 5:
            return False
        market_cfg = self.config.get("market", {})
        open_t = market_cfg.get("commodity_open", "09:00").split(":")
        close_t = market_cfg.get("commodity_close", "23:30").split(":")
        o = now_ist.replace(hour=int(open_t[0]), minute=int(open_t[1]), second=0, microsecond=0)
        c = now_ist.replace(hour=int(close_t[0]), minute=int(close_t[1]), second=0, microsecond=0)
        return o <= now_ist <= c

    def _is_mcx_symbol(self, symbol: str) -> bool:
        mcx_symbols = set(self.config.get("instruments", {}).get("commodities", []))
        return symbol in mcx_symbols

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
