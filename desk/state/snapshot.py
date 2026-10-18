"""Raw market data -> compact, deterministic snapshot.

Point-in-time rule: every field is computed only from records with ts < decision_ts.
Records at or after the decision time are dropped before any computation, so a
late-arriving or future tick can never leak into a decision.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

# Return windows in seconds. Fixed so snapshots are comparable across days.
RETURN_WINDOWS = (60, 300, 1800)
VOL_WINDOW = 1800
MAX_TOKENS = 400


@dataclass(frozen=True)
class BookTop:
    ts: float  # unix seconds, exchange timestamp
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class EquityPoint:
    ts: float
    equity: float


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    decision_ts: float
    as_of_ts: float         # timestamp of the latest record actually used
    mid: float
    spread_bps: float
    imbalance: float        # (bid_size - ask_size) / (bid_size + ask_size), in [-1, 1]
    rvol_bps: float         # realized vol of 1-tick log returns over VOL_WINDOW, in bps
    ret_bps: tuple[float, float, float]  # log returns over RETURN_WINDOWS, in bps
    inventory: float        # signed position in base units
    drawdown_pct: float     # (peak equity - equity) / peak, from equity curve so far

    def to_prompt(self) -> str:
        """Compact text for the judge. Kept well under MAX_TOKENS."""
        r1, r5, r30 = self.ret_bps
        return (
            f"sym={self.symbol} t={int(self.decision_ts)} age_s={self.decision_ts - self.as_of_ts:.1f} "
            f"mid={self.mid:.6g} spread_bps={self.spread_bps:.2f} imb={self.imbalance:+.3f} "
            f"rvol_bps={self.rvol_bps:.2f} ret_bps_1m={r1:+.2f} ret_bps_5m={r5:+.2f} ret_bps_30m={r30:+.2f} "
            f"inv={self.inventory:+.6g} dd_pct={self.drawdown_pct:.2f}"
        )

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """Conservative estimate (~3 chars/token) so the budget holds for any tokenizer we'd use."""
    return math.ceil(len(text) / 3)


def _price_at_or_before(books: Sequence[BookTop], ts: float) -> float | None:
    """Mid of the last book with book.ts <= ts. `books` sorted ascending."""
    lo, hi, ans = 0, len(books) - 1, None
    while lo <= hi:
        m = (lo + hi) // 2
        if books[m].ts <= ts:
            ans, lo = books[m].mid, m + 1
        else:
            hi = m - 1
    return ans


def build_snapshot(
    symbol: str,
    decision_ts: float,
    books: Iterable[BookTop],
    equity_curve: Iterable[EquityPoint],
    inventory: float,
) -> Snapshot:
    past = sorted((b for b in books if b.ts < decision_ts), key=lambda b: b.ts)
    if not past:
        raise ValueError(f"no market data for {symbol} before {decision_ts}")
    last = past[-1]
    mid = last.mid

    spread_bps = (last.ask - last.bid) / mid * 1e4
    depth = last.bid_size + last.ask_size
    imbalance = (last.bid_size - last.ask_size) / depth if depth > 0 else 0.0

    rets = []
    for w in RETURN_WINDOWS:
        ref = _price_at_or_before(past, last.ts - w)
        rets.append(math.log(mid / ref) * 1e4 if ref else 0.0)

    window = [b.mid for b in past if b.ts >= last.ts - VOL_WINDOW]
    lr = [math.log(b / a) for a, b in zip(window, window[1:]) if a > 0 and b > 0]
    if len(lr) >= 2:
        mu = sum(lr) / len(lr)
        rvol = math.sqrt(sum((x - mu) ** 2 for x in lr) / (len(lr) - 1)) * 1e4
    else:
        rvol = 0.0

    peak, eq = 0.0, None
    for p in sorted((e for e in equity_curve if e.ts < decision_ts), key=lambda e: e.ts):
        peak, eq = max(peak, p.equity), p.equity
    dd = (peak - eq) / peak * 100 if eq is not None and peak > 0 else 0.0

    snap = Snapshot(
        symbol=symbol,
        decision_ts=decision_ts,
        as_of_ts=last.ts,
        mid=mid,
        spread_bps=spread_bps,
        imbalance=imbalance,
        rvol_bps=rvol,
        ret_bps=(rets[0], rets[1], rets[2]),
        inventory=inventory,
        drawdown_pct=dd,
    )
    assert estimate_tokens(snap.to_prompt()) < MAX_TOKENS
    return snap
