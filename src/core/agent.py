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

        # Refresh crypto price cache so SL/target checks have live prices
        self._refresh_crypto_prices()

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

        # Validate: don't trade NSE options/equities when market is closed
        symbol = decision.symbol or ""
        is_equity_instrument = (
            symbol not in self._crypto_symbols
            and not self._is_mcx_symbol(symbol)
        )
        if not equity_open and symbol and is_equity_instrument:
            logger.warning(f"Brain picked {symbol} but NSE is closed — skipping")
            return

        # Validate: don't trade MCX when closed
        if not commodity_open and self._is_mcx_symbol(symbol):
            logger.warning(f"Brain picked MCX symbol {symbol} but MCX is closed — skipping")
            return

        # NSE trades MUST be option symbols (CE/PE) — reject raw stock symbols
        if equity_open and is_equity_instrument and not self._is_option_symbol(symbol):
            logger.warning(f"Brain returned NSE stock '{symbol}' — only CE/PE options allowed, skipping")
            return

        # Skip if we already hold this exact symbol
        open_positions = self.executor.get_open_positions()
        open_symbols = {p.get("symbol") for p in open_positions}
        if decision.action in ("BUY", "SHORT") and symbol in open_symbols:
            logger.warning(f"Already have open position in {symbol} — skipping duplicate entry")
            return

        # For options: also block if same underlying already has any open option position
        if self._is_option_symbol(symbol) and decision.action in ("BUY", "SHORT"):
            underlying = self._get_underlying_from_option(symbol)
            for open_sym in open_symbols:
                if self._is_option_symbol(open_sym) and open_sym.startswith(underlying):
                    logger.warning(
                        f"Already have open option on {underlying} ({open_sym}) — skipping {symbol}"
                    )
                    return

        # Also check crypto positions
        if hasattr(self.executor, "binance_paper"):
            crypto_open_symbols = {p.get("symbol") for p in self.executor.binance_paper.get_open_positions()}
            if decision.action in ("BUY", "SHORT") and symbol in crypto_open_symbols:
                logger.warning(f"Already have open crypto position in {symbol} — skipping duplicate entry")
                return

        # Risk check — options, crypto, and equity each have their own limits
        is_crypto_trade = symbol in self._crypto_symbols
        is_option_trade = self._is_option_symbol(symbol)

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

            if not risk_result.allowed:
                logger.warning(f"Risk check BLOCKED crypto trade: {risk_result.reason}")
                if risk_result.max_quantity > 0:
                    logger.info(f"Adjusted quantity: {risk_result.max_quantity}")
                    decision.quantity = risk_result.max_quantity
                else:
                    return

        elif is_option_trade and decision.action in ("BUY", "SHORT"):
            # Options risk check: brain returns num_lots (1-2); validate using actual units
            if len(open_positions) >= self.risk._max_positions:
                logger.warning(f"Risk check BLOCKED option trade: max positions ({self.risk._max_positions}) reached")
                return

            underlying = self._get_underlying_from_option(symbol)
            lot_size = self._get_lot_size(underlying)
            num_lots = int(decision.quantity or 1)
            entry = decision.entry_price or 0
            sl = decision.stop_loss or 0

            risk_cfg = self.config.get("risk", {})
            max_loss_pct = risk_cfg.get("options_per_trade_loss_pct", 3.0)
            max_value_pct = risk_cfg.get("options_max_trade_value_pct", 15.0)
            max_risk = capital * max_loss_pct / 100
            max_value = capital * max_value_pct / 100

            # Try requested lots first; fall back to 1 lot before blocking
            approved_lots = None
            for try_lots in ([num_lots] + ([1] if num_lots > 1 else [])):
                units = try_lots * lot_size
                trade_value = entry * units
                trade_risk = abs(entry - sl) * units
                if trade_value <= max_value and trade_risk <= max_risk:
                    if try_lots != num_lots:
                        logger.info(
                            f"Option size reduced {num_lots}→{try_lots} lot(s) "
                            f"(value ₹{trade_value:.0f} ≤ ₹{max_value:.0f}, risk ₹{trade_risk:.0f} ≤ ₹{max_risk:.0f})"
                        )
                    approved_lots = try_lots
                    break

            if approved_lots is None:
                one_unit_value = entry * lot_size
                one_unit_risk = abs(entry - sl) * lot_size
                logger.warning(
                    f"Risk check BLOCKED option trade {symbol}: "
                    f"1 lot value ₹{one_unit_value:.0f} (limit ₹{max_value:.0f}) | "
                    f"risk ₹{one_unit_risk:.0f} (limit ₹{max_risk:.0f})"
                )
                return

            decision.quantity = approved_lots

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
        is_option = self._is_option_symbol(decision.symbol or "")

        if is_crypto and hasattr(self.executor, "binance_paper"):
            exec_target = self.executor.binance_paper
        elif is_crypto and hasattr(self.executor, "binance_live"):
            exec_target = self.executor.binance_live
        else:
            exec_target = self.executor

        # Resolve lot size and compute actual units for option orders
        num_lots = decision.quantity
        actual_quantity = decision.quantity
        lot_size = 1
        order_exchange = "NSE"

        if is_option:
            underlying = self._get_underlying_from_option(decision.symbol or "")
            lot_size = self._get_lot_size(underlying)
            actual_quantity = int((decision.quantity or 1) * lot_size)
            order_exchange = "NFO"
            logger.info(
                f"Option order: {decision.symbol} | {num_lots} lot(s) x {lot_size} = {actual_quantity} units | exchange=NFO"
            )

        result = exec_target.place_order(
            symbol=decision.symbol,
            action=decision.action,
            quantity=actual_quantity,
            price=decision.entry_price,
            stop_loss=decision.stop_loss,
            target=decision.target_1,
            exchange=order_exchange,
        )

        if result.success:
            extra = {}
            if is_option:
                extra = {
                    "asset_class": "option",
                    "lot_size": lot_size,
                    "num_lots": int(num_lots or 1),
                }
            trade_id = self.memory.record_trade_entry({
                "symbol": decision.symbol,
                "action": decision.action,
                "quantity": actual_quantity,
                "entry_price": result.price,
                "stop_loss": decision.stop_loss,
                "target_1": decision.target_1,
                "target_2": decision.target_2,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "key_risks": decision.key_risks,
                "setup_type": decision.setup_type,
                "time_horizon": decision.time_horizon,
                "asset_class": "option" if is_option else ("crypto" if is_crypto else "equity"),
                "market_context_snapshot": self._last_market_context[:2000],
                "order_id": result.order_id,
                **extra,
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
            is_option = self._is_option_symbol(symbol)

            # Get price from right executor / data source
            if is_option:
                # Options: fetch live premium via market data NFO feed
                try:
                    current_price = self.analyzer.md.get_option_quote(symbol)
                except Exception as e:
                    logger.debug(f"Option quote error for {symbol}: {e}")
                    current_price = 0.0
            elif is_crypto and hasattr(self.executor, "binance_paper"):
                current_price = self.executor.binance_paper.get_current_price(symbol)
            elif is_crypto and hasattr(self.executor, "binance_live"):
                current_price = self.executor.binance_live.get_current_price(symbol)
            else:
                current_price = self.executor.get_current_price(symbol)

            if not current_price:
                continue

            # Persist the latest checked price so the dashboard can show it
            self.memory.update_trade_cmp(trade["trade_id"], current_price)

            action = trade["action"]
            sl = float(trade.get("stop_loss", 0))
            t1 = float(trade.get("target_1", 0))
            should_close = False
            close_reason = ""
            exit_price = current_price  # default: current market price

            if action == "BUY":
                if sl and current_price <= sl:
                    should_close, close_reason = True, "stop_loss_hit"
                    # Simulate real SL-M order: exit at SL price unless a gap blew >20% through it
                    if current_price >= sl * 0.80:
                        exit_price = sl
                    # else: gap scenario — exit at current (worse) price, realistic for crashes
                elif t1 and current_price >= t1:
                    should_close, close_reason = True, "target_1_hit"
                    # Simulate real target order: exit at target price unless a gap ran >20% past it
                    if current_price <= t1 * 1.20:
                        exit_price = t1
            elif action == "SHORT":
                if sl and current_price >= sl:
                    should_close, close_reason = True, "stop_loss_hit"
                    if current_price <= sl * 1.20:
                        exit_price = sl
                elif t1 and current_price <= t1:
                    should_close, close_reason = True, "target_1_hit"
                    if current_price >= t1 * 0.80:
                        exit_price = t1

            # EOD auto-close ALL NSE/MCX positions — no overnight holds
            # NSE/NFO: close at 15:15 (market closes 15:30)
            # MCX: close at 23:15 (market closes 23:30)
            if not is_crypto:
                is_mcx_trade = self._is_mcx_symbol(symbol)
                eod_h, eod_m = (23, 15) if is_mcx_trade else (15, 15)
                if now_ist.hour == eod_h and now_ist.minute >= eod_m:
                    should_close, close_reason = True, "eod_auto_close"
                    exit_price = current_price  # EOD always uses market price

            if should_close:
                exec_target = self.executor
                if is_crypto and hasattr(self.executor, "binance_paper"):
                    exec_target = self.executor.binance_paper
                elif is_crypto and hasattr(self.executor, "binance_live"):
                    exec_target = self.executor.binance_live

                result = exec_target.close_position(
                    symbol=symbol,
                    quantity=float(trade["quantity"]),
                    current_price=exit_price,
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
            f"Open Positions (equity/options/commodity): {len(positions)}",
        ]
        for pos in positions:
            symbol = pos.get("symbol", "")
            is_option = self._is_option_symbol(symbol)
            if is_option:
                # Get live premium for options
                try:
                    cur = self.analyzer.md.get_option_quote(symbol)
                except Exception:
                    cur = 0.0
                underlying = self._get_underlying_from_option(symbol)
                lot_size = self._get_lot_size(underlying)
                qty = pos.get("quantity", 0)
                num_lots = qty // lot_size if lot_size else qty
                lines.append(
                    f"  {symbol} [OPTION/{underlying}]: {pos.get('action')} "
                    f"{num_lots} lot(s) ({qty} units) "
                    f"@ ₹{pos.get('entry_price', 0):.2f} premium | CMP: ₹{cur:.2f} | "
                    f"SL: ₹{pos.get('stop_loss', 0):.2f}"
                )
            else:
                cur = self.executor.get_current_price(symbol)
                lines.append(
                    f"  {symbol}: {pos.get('action')} {pos.get('quantity')} "
                    f"@ ₹{pos.get('entry_price', 0):.2f} | CMP: ₹{cur:.2f} | SL: ₹{pos.get('stop_loss', 0):.2f}"
                )

        # Crypto portfolio
        if hasattr(self.executor, "binance_paper"):
            bp = self.executor.binance_paper
            lines.append(
                f"\nCrypto Portfolio: {bp.get_portfolio_value():.2f} USDT "
                f"(Balance: {bp.usdt_balance:.2f} | Daily PnL: {bp.get_daily_pnl():.4f} USDT)"
            )
            lines.append(f"Crypto Positions: {len(bp.get_open_positions())}")
            for pos in bp.get_open_positions():
                cur = bp.get_current_price(pos.get("symbol", ""))
                unreal = (cur - pos.get("entry_price", 0)) * pos.get("quantity", 0) if cur else 0
                lines.append(
                    f"  {pos.get('symbol')}: {pos.get('action')} {pos.get('quantity')} "
                    f"@ {pos.get('entry_price', 0):.4f} | CMP: {cur:.4f} | "
                    f"Unreal: {unreal:.4f} USDT | SL: {pos.get('stop_loss', 0):.4f}"
                )
        return "\n".join(lines)

    def _session_note(self, equity_open: bool, commodity_open: bool) -> str:
        lines = ["=== TRADING SESSION STATUS ==="]
        lines.append(f"NSE Stock Options (NFO): {'OPEN ✓' if equity_open else 'CLOSED — do NOT pick NSE options'}")
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

    def _refresh_crypto_prices(self):
        """Push latest Binance prices into BinancePaperTrader cache for SL/target checks."""
        if not (self.analyzer.bd and hasattr(self.executor, "binance_paper")):
            return
        try:
            quotes = self.analyzer.bd.get_quote(list(self._crypto_symbols))
            prices = {sym: q.get("last_price", 0) for sym, q in quotes.items() if q.get("last_price")}
            if prices:
                self.executor.binance_paper.update_prices(prices)
        except Exception as e:
            logger.debug(f"Crypto price refresh failed: {e}")

    def _is_option_symbol(self, symbol: str) -> bool:
        """Return True if symbol is an options tradingsymbol (ends with CE or PE)."""
        if not symbol:
            return False
        return symbol.endswith("CE") or symbol.endswith("PE")

    def _get_lot_size(self, underlying_symbol: str) -> int:
        """Look up lot_size for an underlying from config's option_underlyings list."""
        underlyings = self.config.get("instruments", {}).get("option_underlyings", [])
        for entry in underlyings:
            if entry.get("symbol") == underlying_symbol:
                return entry.get("lot_size", 1)
        return 1

    def _get_underlying_from_option(self, option_symbol: str) -> str:
        """
        Extract underlying name from an option tradingsymbol.
        e.g. 'HDFCBANK26MAY785PE' -> 'HDFCBANK'
        Uses configured underlying names as prefix candidates.
        """
        underlyings = self.config.get("instruments", {}).get("option_underlyings", [])
        # Sort by length descending so longer names match first (e.g. BHARTIARTL before BHARTI)
        sorted_underlyings = sorted(
            [e.get("symbol", "") for e in underlyings],
            key=len,
            reverse=True,
        )
        for sym in sorted_underlyings:
            if sym and option_symbol.startswith(sym):
                return sym
        return option_symbol  # fallback: return the full symbol

    def _is_mcx_symbol(self, symbol: str) -> bool:
        mcx_symbols = set(self.config.get("instruments", {}).get("commodities", []))
        return symbol in mcx_symbols

    def _cleanup_orphaned_trades(self):
        """Close any memory trades whose positions no longer exist in the executor (e.g. after restart)."""
        open_trades = self.memory.get_open_trades()
        if not open_trades:
            return

        active_symbols = {p.get("symbol") for p in self.executor.get_open_positions()}
        if hasattr(self.executor, "binance_paper"):
            active_symbols |= {p.get("symbol") for p in self.executor.binance_paper.get_open_positions()}

        for trade in open_trades:
            symbol = trade["symbol"]
            if symbol not in active_symbols:
                logger.warning(f"Orphaned trade in memory: {symbol} ({trade['trade_id']}) — marking cancelled")
                self.memory.record_trade_exit(trade["trade_id"], {
                    "exit_price": trade.get("entry_price", 0),
                    "exit_reason": "cancelled_on_restart",
                    "pnl": 0.0,
                    "brokerage": 0.0,
                    "gross_pnl": 0.0,
                    "lesson": "Trade orphaned after agent restart — position lost.",
                    "lesson_tag": "operational",
                })

    def run_scheduler(self, interval_minutes: int = 15):
        """Run continuously, calling run_once() every interval_minutes."""
        self._running = True
        logger.info(f"Agent started — analysis interval: {interval_minutes} min")
        self._cleanup_orphaned_trades()
        while self._running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Agent cycle error: {e}", exc_info=True)
            time.sleep(interval_minutes * 60)

    def stop(self):
        self._running = False
        logger.info("Agent stopped.")
