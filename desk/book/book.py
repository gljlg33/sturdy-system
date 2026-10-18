"""The book: at most one open position, plus a bench of rejected candidates.

While a position is open the desk does not scan for new trades. Rejected candidates
sit on the bench for a time set by why they were rejected.
"""
from __future__ import annotations

from dataclasses import dataclass

# Seconds a candidate stays benched, keyed by the funnel check that rejected it.
# Transient problems clear fast; judgments and risk vetoes stick longer.
BENCH_SECONDS: dict[str, int] = {
    "stale_data": 60,
    "spread_too_wide": 300,
    "low_liquidity": 600,
    "one_call_check": 300,
    "hard_facts": 900,
    "judge_timeout": 60,
    "judge_unsure": 900,
    "policy_hold": 600,
    "lost_final_pick": 120,
    "risk_veto": 3600,
    "stopped_out": 1800,
}
DEFAULT_BENCH_SECONDS = 600


@dataclass
class OpenPosition:
    symbol: str
    side: int            # +1 long, -1 short
    qty: float
    entry_price: float
    entry_ts: float
    decision_id: str


@dataclass(frozen=True)
class BenchEntry:
    symbol: str
    reason: str
    until_ts: float


class Book:
    def __init__(self):
        self.open: OpenPosition | None = None
        self._bench: dict[str, BenchEntry] = {}

    def can_scan(self) -> bool:
        return self.open is None

    def open_position(self, pos: OpenPosition) -> None:
        if self.open is not None:
            raise RuntimeError(f"book already holds {self.open.symbol}; one position at a time")
        self.open = pos

    def close_position(self) -> OpenPosition:
        if self.open is None:
            raise RuntimeError("no open position")
        pos, self.open = self.open, None
        return pos

    def bench(self, symbol: str, reason: str, now: float) -> BenchEntry:
        e = BenchEntry(symbol, reason, now + BENCH_SECONDS.get(reason, DEFAULT_BENCH_SECONDS))
        prev = self._bench.get(symbol)
        if prev is None or e.until_ts > prev.until_ts:
            self._bench[symbol] = e
        return self._bench[symbol]

    def benched(self, symbol: str, now: float) -> BenchEntry | None:
        e = self._bench.get(symbol)
        if e and e.until_ts <= now:
            del self._bench[symbol]
            return None
        return e
