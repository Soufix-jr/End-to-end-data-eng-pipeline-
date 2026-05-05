"""
Position lifecycle: open one position per symbol, close on target/stop/expiry/flip.
All persistence goes through a small DB helper passed in at construction time.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Position:
    id: int
    symbol: str
    side: str          # LONG or SHORT
    entry_time: datetime
    entry_price: float
    target_price: float
    stop_price: float
    valid_until: datetime
    confidence: float


def pnl_pct(side: str, entry: float, exit_: float) -> float:
    if entry == 0:
        return 0.0
    raw = (exit_ - entry) / entry
    return round(100.0 * (raw if side == "LONG" else -raw), 4)


class PositionManager:
    """
    One open position per symbol at most. The decision engine asks this object
    'should I do anything for this trade?' and 'should I open?'.
    """

    def __init__(self, db, target_pct: float, stop_pct: float, horizon_minutes: int):
        self._db = db
        self._target_pct = target_pct
        self._stop_pct = stop_pct
        self._horizon_s = horizon_minutes * 60
        self._open: dict = {}  # symbol -> Position
        self._restore_open_positions()

    def _restore_open_positions(self) -> None:
        for row in self._db.fetch_open_positions():
            pos = Position(*row)
            self._open[pos.symbol] = pos

    def open_position(
        self, symbol: str, side: str, price: float, ts_ms: int, confidence: float
    ) -> Optional[Position]:
        if symbol in self._open:
            return None  # already in a position; ignore until it closes

        entry_time = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        if side == "LONG":
            target = price * (1 + self._target_pct)
            stop = price * (1 - self._stop_pct)
        else:
            target = price * (1 - self._target_pct)
            stop = price * (1 + self._stop_pct)
        valid_until = datetime.fromtimestamp((ts_ms / 1000.0) + self._horizon_s, tz=timezone.utc)

        pos_id = self._db.insert_position(
            symbol, side, entry_time, price, target, stop, valid_until, confidence
        )
        pos = Position(pos_id, symbol, side, entry_time, price, target, stop, valid_until, confidence)
        self._open[symbol] = pos
        return pos

    def evaluate_tick(self, symbol: str, price: float, ts_ms: int) -> Optional[tuple]:
        """
        Called on every trade tick. If the open position for this symbol should
        close, returns (position, exit_reason); otherwise None.
        """
        pos = self._open.get(symbol)
        if pos is None:
            return None

        now = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

        if pos.side == "LONG":
            if price >= pos.target_price:
                return self._close(pos, price, now, "target_hit")
            if price <= pos.stop_price:
                return self._close(pos, price, now, "stop_hit")
        else:
            if price <= pos.target_price:
                return self._close(pos, price, now, "target_hit")
            if price >= pos.stop_price:
                return self._close(pos, price, now, "stop_hit")

        if now >= pos.valid_until:
            return self._close(pos, price, now, "expired")

        return None

    def _close(self, pos: Position, exit_price: float, exit_time: datetime, reason: str):
        pnl = pnl_pct(pos.side, pos.entry_price, exit_price)
        self._db.close_position(pos.id, exit_time, exit_price, reason, pnl)
        del self._open[pos.symbol]
        return pos, reason, pnl

    def has_open(self, symbol: str) -> bool:
        return symbol in self._open
