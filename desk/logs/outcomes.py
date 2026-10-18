"""Resolve what actually happened for each question, once its horizon has passed.

Each resolver maps (snapshot at decision, books after the decision) -> the option that
came true, or None if the data can't settle it (gap, horizon not yet elapsed).
Unresolvable questions are left out of calibration rather than guessed.
"""
from __future__ import annotations

import math
from typing import Callable, Sequence

from desk.state.snapshot import BookTop, Snapshot

Resolver = Callable[[Snapshot, Sequence[BookTop], int], "str | None"]


def _window(snap: Snapshot, books: Sequence[BookTop], horizon_s: int) -> list[BookTop] | None:
    end = snap.decision_ts + horizon_s
    after = sorted((b for b in books if b.ts >= snap.decision_ts), key=lambda b: b.ts)
    if not after or after[-1].ts < end:
        return None  # horizon not covered yet
    win = [b for b in after if b.ts <= end]
    return win or None


def _ret_bps(snap: Snapshot, win: list[BookTop]) -> float:
    return math.log(win[-1].mid / snap.mid) * 1e4


def direction_2bps(snap, books, h):
    win = _window(snap, books, h)
    if win is None:
        return None
    r = _ret_bps(snap, win)
    return "up" if r > 2 else "down" if r < -2 else "flat"


def jump_25bps(snap, books, h):
    win = _window(snap, books, h)
    if win is None:
        return None
    return "yes" if max(abs(math.log(b.mid / snap.mid)) * 1e4 for b in win) > 25 else "no"


def spread_change(snap, books, h):
    win = _window(snap, books, h)
    if win is None:
        return None
    last = win[-1]
    s = (last.ask - last.bid) / last.mid * 1e4
    if snap.spread_bps <= 0:
        return None
    ratio = s / snap.spread_bps
    return "wider" if ratio > 1.2 else "narrower" if ratio < 0.8 else "same"


def adverse_side_1bp(snap, books, h):
    win = _window(snap, books, h)
    if win is None:
        return None
    r = _ret_bps(snap, win)
    return "bid" if r < -1 else "ask" if r > 1 else "neither"


def vol_change(snap, books, h):
    win = _window(snap, books, h)
    if win is None or len(win) < 3 or snap.rvol_bps <= 0:
        return None
    lr = [math.log(b.mid / a.mid) for a, b in zip(win, win[1:])]
    mu = sum(lr) / len(lr)
    v = math.sqrt(sum((x - mu) ** 2 for x in lr) / (len(lr) - 1)) * 1e4
    ratio = v / snap.rvol_bps
    return "higher" if ratio > 1.25 else "lower" if ratio < 0.8 else "similar"


def imbalance_follow_2bps(snap, books, h):
    if snap.imbalance == 0:
        return None
    win = _window(snap, books, h)
    if win is None:
        return None
    r = _ret_bps(snap, win) * (1 if snap.imbalance > 0 else -1)
    return "follows" if r > 2 else "reverses" if r < -2 else "flat"


def informed_proxy(snap, books, h):
    win = _window(snap, books, h)
    if win is None:
        return None
    return "informed" if abs(_ret_bps(snap, win)) > 2 * snap.spread_bps else "noise"


RESOLVERS: dict[str, Resolver] = {f.__name__: f for f in (
    direction_2bps, jump_25bps, spread_change, adverse_side_1bp, vol_change, imbalance_follow_2bps, informed_proxy,
)}
