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

        # Persist current capital so dashboard can display it
        nse_val = self.executor.get_portfolio_value()
        mcx_val = self.executor.mcx_paper.get_portfolio_value() if hasattr(self.executor, "mcx_paper") else 0.0
        crypto_val = self.executor.binance_paper.get_portfolio_value() if hasattr(self.executor, "binance_paper") else 0.0
        nse_cash = self.executor.cash
        mcx_cash = self.executor.mcx_paper.cash if hasattr(self.executor, "mcx_paper") else 0.0
        self.memory.update_portfolio_state(
            portfolio_value=nse_val + mcx_val,
            cash=nse_cash + mcx_cash,
            nse_value=nse_val,
            mcx_value=mcx_val,
            crypto_usdt=crypto_val,
        )

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
        nse_capital = self.executor.get_portfolio_value()
        mcx_capital = self.executor.mcx_paper.get_portfolio_value() if hasattr(self.executor, "mcx_paper") else 0.0
        crypto_usdt = self.executor.binance_paper.get_portfolio_value() if hasattr(self.executor, "binance_paper") else 0.0

        # Past lessons
        lessons = self.memory.get_relevant_lessons(limit=10)
        stats = self.memory.get_stats_summary()
        lessons_text = f"PERFORMANCE:\n{stats}\n\nRECENT LESSONS:\n{lessons}"

        # Add session context so brain knows what it can trade
        session_note = self._session_note(equity_open, commodity_open)
        full_context = f"{session_note}\n\n{market_context}"

        # Skip brain call if every tradeable pool is already at max positions
        max_pos = self.risk._max_positions
        nse_open = len(self.executor.get_open_positions())
        mcx_open = len(self.executor.mcx_paper.get_open_positions()) if hasattr(self.executor, "mcx_paper") else max_pos
        crypto_open_count = len(self.executor.binance_paper.get_open_positions()) if hasattr(self.executor, "binance_paper") else max_pos
        nse_full = not equity_open or nse_open >= max_pos
        mcx_full = not commodity_open or mcx_open >= max_pos
        crypto_full = crypto_open_count >= max_pos
        if nse_full and mcx_full and crypto_full:
            logger.info("All tradeable pools at max positions — skipping brain call")
            return

        # Brain decides
        logger.info("Asking brain for trade decision...")
        decision = self.brain.analyze_and_decide(
            full_context, lessons_text, portfolio_str,
            capital=nse_capital,
            mcx_capital=mcx_capital,
            crypto_usdt=crypto_usdt,
        )

        logger.info(
            f"Decision: {decision.action} | Symbol: {decision.symbol} | "
            f"Confidence: {decision.confidence:.2f} | Setup: {decision.setup_type}"
        )

        if decision.action in ("WAIT", "HOLD"):
            logger.info(f"Brain says wait: {decision.rationale}")
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

        # Risk check — skip for exit actions (SELL/COVER always allowed)
        # Only apply risk limits to new entries (BUY/SHORT)
        is_crypto_trade = symbol in self._crypto_symbols
        is_option_trade = self._is_option_symbol(symbol)

        if decision.action in ("SELL", "COVER"):
            pass  # exit trades always allowed through — no risk check needed

        elif is_crypto_trade:
            # For crypto use USDT balance and skip INR-based risk limits
            if hasattr(self.executor, "binance_paper"):
                crypto_capital = self.executor.binance_paper.get_portfolio_value()
            elif hasattr(self.executor, "binance_live"):
                crypto_capital = self.executor.binance_live.get_portfolio_value()
            else:
                crypto_capital = nse_capital * 0.2  # fallback: 20% of NSE capital

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
            max_risk = nse_capital * max_loss_pct / 100
            max_value = nse_capital * max_value_pct / 100

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
            # Use MCX capital pool for MCX trades
            is_mcx_trade = self._is_mcx_symbol(symbol)
            if is_mcx_trade and hasattr(self.executor, "mcx_paper"):
                trade_capital = self.executor.mcx_paper.get_portfolio_value()
                trade_daily_pnl = self.executor.mcx_paper.get_daily_pnl()
                trade_positions = self.executor.mcx_paper.get_open_positions()
            else:
                trade_capital = nse_capital
                trade_daily_pnl = daily_pnl
                trade_positions = open_positions

            risk_result = self.risk.check_trade(
                action=decision.action,
                entry_price=decision.entry_price or 0,
                stop_loss=decision.stop_loss or 0,
                quantity=decision.quantity or 0,
                current_capital=trade_capital,
                daily_pnl=trade_daily_pnl,
                open_positions=trade_positions,
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
        is_crypto = decision.symbol in self._crypto_symbols
        is_option = self._is_option_symbol(decision.symbol or "")
        is_mcx = self._is_mcx_symbol(decision.symbol or "")

        if is_crypto and hasattr(self.executor, "binance_paper"):
            exec_target = self.executor.binance_paper
        elif is_crypto and hasattr(self.executor, "binance_live"):
            exec_target = self.executor.binance_live
        elif is_mcx and hasattr(self.executor, "mcx_paper"):
            exec_target = self.executor.mcx_paper
        else:
            exec_target = self.executor  # NSE

        # Resolve lot size and compute actual units for option orders
        num_lots = decision.quantity
        actual_quantity = decision.quantity
        lot_size = 1
        order_exchange = "MCX" if is_mcx else "NSE"

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
                "asset_class": "option" if is_option else ("crypto" if is_crypto else ("mcx" if is_mcx else "equity")),
                "market_context_snapshot": self._last_market_context[:2000],
                "order_id": result.order_id,
                **extra,
            })
            logger.info(f"Trade recorded: {trade_id} — {decision.action} {decision.symbol}")
        else:
            logger.error(f"Order failed: {result.message}")

    def _check_exit_conditions(self, now_ist: datetime, options_only: bool = False):
        """Check all open trades for SL/target hits or EOD close."""
        open_trades = self.memory.get_open_trades()

        for trade in open_trades:
            symbol = trade["symbol"]
            is_crypto = symbol in self._crypto_symbols
            is_option = self._is_option_symbol(symbol)

            if options_only and not is_option:
                continue

            is_mcx = self._is_mcx_symbol(symbol)

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
            elif is_mcx and hasattr(self.executor, "mcx_paper"):
                current_price = self.executor.mcx_paper.get_current_price(symbol)
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
                elif is_mcx and hasattr(self.executor, "mcx_paper"):
                    exec_target = self.executor.mcx_paper

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
            f"NSE Pool: ₹{capital:,.2f} | Daily PnL: ₹{daily_pnl:,.2f}",
            f"NSE Open Positions: {len(positions)}",
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

        # MCX portfolio
        if hasattr(self.executor, "mcx_paper"):
            mp = self.executor.mcx_paper
            lines.append(
                f"\nMCX Pool: ₹{mp.get_portfolio_value():,.2f} | Cash: ₹{mp.cash:,.2f} | Daily PnL: ₹{mp.get_daily_pnl():,.2f}"
            )
            lines.append(f"MCX Open Positions: {len(mp.get_open_positions())}")
            for pos in mp.get_open_positions():
                cur = mp.get_current_price(pos.get("symbol", ""))
                lines.append(
                    f"  {pos.get('symbol')}: {pos.get('action')} {pos.get('quantity')} "
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
        """
        On restart the executor's positions dict is empty, but memory still has open trades.
        - Option positions  → restore into executor so they keep being tracked
        - Crypto positions  → restore into binance_paper
        - Old equity stocks → cancel (these are from pre-options code, no longer valid)
        """
        open_trades = self.memory.get_open_trades()
        if not open_trades:
            return

        for trade in open_trades:
            symbol = trade["symbol"]
            is_crypto = symbol in self._crypto_symbols
            is_option = self._is_option_symbol(symbol)
            is_mcx = self._is_mcx_symbol(symbol)
            entry_price = float(trade.get("entry_price", 0))
            quantity = trade.get("quantity", 0)

            if is_option:
                # Rebuild the position in PaperTrader so SL/target checks keep working
                self.executor.positions[symbol] = {
                    "symbol": symbol,
                    "action": trade.get("action", "BUY"),
                    "quantity": int(quantity),
                    "entry_price": entry_price,
                    "stop_loss": float(trade.get("stop_loss", 0)),
                    "target": float(trade.get("target_1", 0)),
                    "order_id": trade.get("order_id", "restored"),
                    "entry_time": trade.get("entry_time", ""),
                }
                # Deduct entry cost so portfolio value stays accurate
                cost = entry_price * int(quantity) + 20.0
                self.executor.cash = max(0.0, self.executor.cash - cost)
                logger.info(
                    f"Restored option position: {symbol} | {quantity} units @ ₹{entry_price:.2f} | "
                    f"₹{cost:.0f} deducted from cash"
                )

            elif is_crypto and hasattr(self.executor, "binance_paper"):
                qty = float(quantity)
                self.executor.binance_paper.positions[symbol] = {
                    "symbol": symbol,
                    "action": trade.get("action", "BUY"),
                    "quantity": qty,
                    "entry_price": entry_price,
                    "stop_loss": float(trade.get("stop_loss", 0)),
                    "target": float(trade.get("target_1", 0)),
                    "order_id": trade.get("order_id", "restored"),
                    "entry_time": trade.get("entry_time", ""),
                }
                cost = entry_price * qty
                self.executor.binance_paper.usdt_balance = max(
                    0.0, self.executor.binance_paper.usdt_balance - cost
                )
                logger.info(
                    f"Restored crypto position: {symbol} | {qty} @ {entry_price:.4f} | "
                    f"{cost:.4f} USDT deducted"
                )

            elif is_mcx and hasattr(self.executor, "mcx_paper"):
                self.executor.mcx_paper.positions[symbol] = {
                    "symbol": symbol,
                    "action": trade.get("action", "BUY"),
                    "quantity": int(quantity),
                    "entry_price": entry_price,
                    "stop_loss": float(trade.get("stop_loss", 0)),
                    "target": float(trade.get("target_1", 0)),
                    "order_id": trade.get("order_id", "restored"),
                    "entry_time": trade.get("entry_time", ""),
                }
                cost = entry_price * int(quantity) + 20.0
                self.executor.mcx_paper.cash = max(0.0, self.executor.mcx_paper.cash - cost)
                logger.info(
                    f"Restored MCX position: {symbol} | {quantity} @ ₹{entry_price:.2f} | "
                    f"₹{cost:.0f} deducted from MCX cash"
                )

            else:
                # Old non-option equity trade — cancel cleanly
                logger.warning(
                    f"Cancelling obsolete equity trade: {symbol} ({trade['trade_id']}) — not valid in options-only mode"
                )
                self.memory.record_trade_exit(trade["trade_id"], {
                    "exit_price": trade.get("entry_price", 0),
                    "exit_reason": "cancelled_obsolete_equity",
                    "pnl": 0.0,
                    "brokerage": 0.0,
                    "gross_pnl": 0.0,
                    "lesson": "Non-option equity trade removed after switch to options-only mode.",
                    "lesson_tag": "operational",
                })

    def run_scheduler(self, interval_minutes: int = 10):
        """
        Two-speed loop:
          - Every 3 min: fast options SL/target check only
          - Every 10 min: full cycle (brain analysis + all exit checks + new trades)
        """
        self._running = True
        logger.info(f"Agent started — full cycle: {interval_minutes} min | options SL check: 3 min")
        self._cleanup_orphaned_trades()

        OPTIONS_CHECK_SECS = 3 * 60
        CRYPTO_ONLY_SECS = 30 * 60  # slower cadence when only crypto is open
        last_full_run = 0.0  # force immediate full run on startup

        while self._running:
            loop_start = time.time()
            now_ist = datetime.now(IST)

            # Fast options SL/target check — runs every 3 min
            try:
                self._check_exit_conditions(now_ist, options_only=True)
            except Exception as e:
                logger.error(f"Options exit check error: {e}", exc_info=True)

            # Dynamic full-cycle interval: normal when NSE/MCX open, 30 min when crypto-only
            equity_open_now = self._is_equity_session(now_ist)
            commodity_open_now = self._is_commodity_session(now_ist)
            effective_secs = interval_minutes * 60 if (equity_open_now or commodity_open_now) else CRYPTO_ONLY_SECS

            # Full cycle
            if loop_start - last_full_run >= effective_secs:
                try:
                    self.run_once()
                    last_full_run = loop_start
                except Exception as e:
                    logger.error(f"Agent cycle error: {e}", exc_info=True)
                    last_full_run = loop_start  # avoid rapid retries on persistent error

            elapsed = time.time() - loop_start
            time.sleep(max(0, OPTIONS_CHECK_SECS - elapsed))

    def stop(self):
        self._running = False
        logger.info("Agent stopped.")
