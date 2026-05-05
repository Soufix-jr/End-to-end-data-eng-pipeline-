"""
Per-symbol sentiment cache with exponential decay.
Pure logic, no IO, easy to unit test.
"""

import math
from collections import defaultdict, deque


class SentimentCache:
    def __init__(self, half_life_s: float, max_history: int):
        self._half_life_ms = half_life_s * 1000.0
        self._history: dict = defaultdict(lambda: deque(maxlen=max_history))

    def add(self, symbol: str, signed_score: float, ts_ms: int) -> None:
        self._history[symbol].append((ts_ms, signed_score))

    def score(self, symbol: str, now_ms: int) -> float:
        hist = self._history.get(symbol)
        if not hist:
            return 0.0
        total = 0.0
        for ts_ms, s in hist:
            age = max(0, now_ms - ts_ms)
            total += s * math.pow(0.5, age / self._half_life_ms)
        return total


def signed_score(label: str, score: float) -> float:
    if label == "positive":
        return score
    if label == "negative":
        return -score
    return 0.0
