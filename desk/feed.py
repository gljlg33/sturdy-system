"""Market data sources. No exchange connection lives here (that would need keys: ask first)."""
from __future__ import annotations

import csv
import math
import random
from typing import Protocol

from desk.state.snapshot import BookTop


class Feed(Protocol):
    def books(self, symbol: str, start_ts: float, end_ts: float) -> list[BookTop]:
        """All book tops with start_ts <= ts < end_ts."""
        ...


class SyntheticFeed:
    """Seeded random walk, one book per second. For demos and tests only."""

    def __init__(self, seed: int = 7, start_price: float = 60000.0, vol_bps: float = 1.5, spread_bps: float = 1.0):
        self.seed, self.p0, self.vol, self.spread = seed, start_price, vol_bps, spread_bps
        self._hours: dict[tuple[str, int], list[float]] = {}

    def _hour(self, symbol: str, hour: int) -> list[float]:
        # Each hour continues from the previous hour if it has been generated, so the path is
        # continuous when read forward in time (which is how the runner reads it).
        key = (symbol, hour)
        if key not in self._hours:
            r = random.Random(f"{self.seed}:{symbol}:{hour}")
            prev = self._hours.get((symbol, hour - 1))
            lp = math.log(prev[-1] if prev else self.p0)
            path = []
            for _ in range(3600):
                lp += r.gauss(0, self.vol / 1e4)
                path.append(math.exp(lp))
            self._hours[key] = path
        return self._hours[key]

    def _book(self, symbol: str, t: int) -> BookTop:
        mid = self._hour(symbol, t // 3600)[t % 3600]
        half = mid * self.spread / 2e4
        rs = random.Random(f"{self.seed}:{symbol}:sz:{t}")
        return BookTop(float(t), mid - half, mid + half, rs.uniform(0.5, 5), rs.uniform(0.5, 5))

    def books(self, symbol: str, start_ts: float, end_ts: float) -> list[BookTop]:
        return [self._book(symbol, t) for t in range(math.ceil(start_ts), math.ceil(end_ts))]


class CsvFeed:
    """Replay from CSV with columns: symbol,ts,bid,ask,bid_size,ask_size."""

    def __init__(self, path: str):
        self._rows: dict[str, list[BookTop]] = {}
        with open(path) as f:
            for row in csv.DictReader(f):
                self._rows.setdefault(row["symbol"], []).append(BookTop(
                    float(row["ts"]), float(row["bid"]), float(row["ask"]), float(row["bid_size"]), float(row["ask_size"])))
        for v in self._rows.values():
            v.sort(key=lambda b: b.ts)

    def books(self, symbol: str, start_ts: float, end_ts: float) -> list[BookTop]:
        return [b for b in self._rows.get(symbol, []) if start_ts <= b.ts < end_ts]
