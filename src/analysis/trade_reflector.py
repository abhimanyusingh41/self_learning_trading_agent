from loguru import logger

from src.core.brain import TradingBrain
from src.core.memory import TradeMemory


class TradeReflector:
    def __init__(self, brain: TradingBrain, memory: TradeMemory):
        self.brain = brain
        self.memory = memory

    def reflect_and_learn(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str,
        market_context_at_exit: str,
    ) -> bool:
        """
        Close a trade, trigger reflection, and store the lesson.
        Returns True on success.
        """
        open_trades = self.memory.get_open_trades()
        trade = next((t for t in open_trades if t["trade_id"] == trade_id), None)

        if not trade:
            logger.error(f"Cannot reflect: trade {trade_id} not found in open trades.")
            return False

        # Calculate P&L
        entry_price = float(trade.get("entry_price", 0))
        quantity = int(trade.get("quantity", 0))
        action = trade.get("action", "BUY")

        if action in ("BUY", "COVER"):
            pnl = (exit_price - entry_price) * quantity
        else:  # SHORT/SELL
            pnl = (entry_price - exit_price) * quantity

        exit_data = {
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "pnl": round(pnl, 2),
        }

        closed_trade = self.memory.record_trade_exit(trade_id, exit_data)
        if not closed_trade:
            return False

        logger.info(
            f"Trade {trade_id} closed | {trade.get('symbol')} | "
            f"PnL: ₹{pnl:,.2f} | Reason: {exit_reason}"
        )

        # Run LLM reflection
        market_at_entry = trade.get("market_context_snapshot", "Not recorded")
        reflection_text, outcome_tag = self.brain.reflect_on_trade(
            trade_entry=trade,
            trade_exit=exit_data,
            market_at_entry=market_at_entry,
            market_at_exit=market_context_at_exit,
        )

        # Extract lesson from reflection text
        lesson = self._extract_lesson(reflection_text)
        tag = self._extract_tag(reflection_text, outcome_tag)

        self.memory.add_lesson(lesson, tag, trade_id=trade_id)
        logger.info(f"Lesson stored [{tag}]: {lesson[:100]}...")

        return True

    def _extract_lesson(self, reflection_text: str) -> str:
        for line in reflection_text.split("\n"):
            if line.startswith("LESSON:"):
                return line.replace("LESSON:", "").strip()
        # Fallback: return full reflection
        return reflection_text.strip()[:500]

    def _extract_tag(self, reflection_text: str, fallback: str) -> str:
        for line in reflection_text.split("\n"):
            if line.startswith("LESSON_TAG:"):
                return line.replace("LESSON_TAG:", "").strip()
        return fallback
