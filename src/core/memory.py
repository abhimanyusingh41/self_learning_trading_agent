import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from loguru import logger


class TradeMemory:
    def __init__(self, memory_file: str = "data/memory/trade_memory.json"):
        self.memory_file = Path(memory_file)
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if self.memory_file.exists():
            try:
                with open(self.memory_file) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load memory file, starting fresh: {e}")
        return {
            "trades": [],
            "lessons": [],
            "stats": {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0.0,
                "total_brokerage": 0.0,
                "best_trade_pnl": 0.0,
                "worst_trade_pnl": 0.0,
            },
        }

    def _save(self):
        try:
            with open(self.memory_file, "w") as f:
                json.dump(self._data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save memory: {e}")

    def record_trade_entry(self, trade: dict) -> str:
        trade_id = str(uuid.uuid4())[:8]
        trade_record = {
            "trade_id": trade_id,
            "status": "open",
            "entry_time": datetime.now().isoformat(),
            **trade,
        }
        self._data["trades"].append(trade_record)
        self._save()
        return trade_id

    def record_trade_exit(self, trade_id: str, exit_data: dict) -> Optional[dict]:
        for trade in self._data["trades"]:
            if trade["trade_id"] == trade_id:
                trade.update({
                    "status": "closed",
                    "exit_time": datetime.now().isoformat(),
                    **exit_data,
                })
                pnl = exit_data.get("pnl", 0.0)
                brokerage = exit_data.get("brokerage", 0.0)
                self._update_stats(pnl, brokerage)
                self._save()
                return trade
        logger.warning(f"Trade {trade_id} not found in memory")
        return None

    def _update_stats(self, pnl: float, brokerage: float = 0.0):
        stats = self._data["stats"]
        stats["total_trades"] += 1
        stats["total_pnl"] = round(stats["total_pnl"] + pnl, 2)
        stats["total_brokerage"] = round(stats.get("total_brokerage", 0.0) + brokerage, 2)
        if pnl > 0:
            stats["winning_trades"] += 1
            stats["best_trade_pnl"] = max(stats["best_trade_pnl"], pnl)
        else:
            stats["losing_trades"] += 1
            stats["worst_trade_pnl"] = min(stats["worst_trade_pnl"], pnl)

    def add_lesson(self, lesson: str, lesson_tag: str, trade_id: Optional[str] = None):
        self._data["lessons"].append({
            "id": str(uuid.uuid4())[:8],
            "timestamp": datetime.now().isoformat(),
            "lesson": lesson,
            "tag": lesson_tag,
            "trade_id": trade_id,
        })
        # Keep only last 200 lessons
        self._data["lessons"] = self._data["lessons"][-200:]
        self._save()

    def get_relevant_lessons(self, limit: int = 10) -> str:
        lessons = self._data["lessons"][-limit:]
        if not lessons:
            return "No lessons recorded yet."
        lines = []
        for i, l in enumerate(reversed(lessons), 1):
            lines.append(f"{i}. [{l['tag']}] {l['lesson']}")
        return "\n".join(lines)

    def get_stats_summary(self) -> str:
        s = self._data["stats"]
        total = s["total_trades"]
        if total == 0:
            return "No completed trades yet."
        win_rate = round(s["winning_trades"] / total * 100, 1)
        brokerage = s.get("total_brokerage", 0.0)
        return (
            f"Total trades: {total} | Win rate: {win_rate}% "
            f"({s['winning_trades']}W/{s['losing_trades']}L) | "
            f"Net PnL: ₹{s['total_pnl']:,.2f} | Brokerage paid: ₹{brokerage:,.2f} | "
            f"Best: ₹{s['best_trade_pnl']:,.2f} | Worst: ₹{s['worst_trade_pnl']:,.2f}"
        )

    def update_trade_cmp(self, trade_id: str, price: float):
        """Record the last checked market price on an open trade."""
        for trade in self._data["trades"]:
            if trade["trade_id"] == trade_id and trade.get("status") == "open":
                trade["last_cmp"] = price
                trade["last_cmp_time"] = datetime.now().isoformat()
                self._save()
                return

    def get_open_trades(self) -> list:
        return [t for t in self._data["trades"] if t.get("status") == "open"]

    def get_recent_closed_trades(self, limit: int = 5) -> list:
        closed = [t for t in self._data["trades"] if t.get("status") == "closed"]
        return closed[-limit:]
