import time
import logging
from collections import defaultdict
from typing import Dict, List

logger = logging.getLogger("StockHeuristics")

# ==========================================================
# PREDICTIVE STOCK RELEASE HEURISTICS ENGINE
# ==========================================================
class StockHeuristicsEngine:
    """
    HeroSMS-er stock release patterns track kore.
    Kokhono dead hour thakle request delay baraye dey (anti-ban),
    ebong stock drop window-te fast shotting enable kore.
    """
    def __init__(self):
        # Minute of the hour (0-59) -> capture count
        self._minute_hits: Dict[int, int] = defaultdict(int)
        self._history: List[float] = []

    def record_capture(self, operator: str = "claro"):
        now = time.time()
        minute = time.localtime(now).tm_min
        self._minute_hits[minute] += 1
        self._history.append(now)
        logger.info(f"Recorded stock drop at minute {minute} for {operator.upper()}")

        # Keep history to last 50 captures
        if len(self._history) > 50:
            self._history.pop(0)

    def get_optimal_delay(self) -> float:
        """
        Current minute jodi historical stock release minute hoy,
        delay komiye 1.8s kore (Burst Speed).
        Ar dead-hour e 3.2s kore server and IP safe rakhe.
        """
        current_minute = time.localtime().tm_min
        hits = self._minute_hits.get(current_minute, 0)

        # Jodi ei minute-e age stock pawa giye thake
        if hits >= 2:
            return 1.8  # Aggressive burst window
        elif hits == 1:
            return 2.2  # Moderate window
        return 3.0      # Safe baseline guard

    def get_telemetry_stats(self) -> str:
        if not self._minute_hits:
            return "No historical drops recorded yet."
        top_min = max(self._minute_hits, key=self._minute_hits.get)
        return f"Peak Drop Minute: {top_min}m past hour ({self._minute_hits[top_min]} captures)"


heuristics_engine = StockHeuristicsEngine()
