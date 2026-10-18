"""Policy: turns raw judge probabilities into one action.

All thresholds and weights live here, in code, so they are versioned and reviewable.
Each action has its own threshold derived from what being wrong costs:

    threshold = loss_if_wrong / (loss_if_wrong + gain_if_right) + margin

i.e. the break-even probability plus a safety margin. Expensive mistakes need
more conviction; cheap ones (like exiting early) need less.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from desk.state.snapshot import Snapshot

POLICY_VERSION = "policy-0.1.0"

Answers = dict[str, dict[str, dict[str, float]]]  # bot -> question id -> option -> probability


class Action(str, enum.Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"
    HOLD = "hold"


@dataclass(frozen=True)
class ActionThreshold:
    loss_if_wrong_bps: float   # stop distance / cost when the call is wrong, before spread and fees
    gain_if_right_bps: float   # target distance when the call is right, before spread and fees
    margin: float              # extra probability demanded above break-even

    def threshold(self, spread_bps: float, fee_bps: float, jump_p: float = 0.0) -> float:
        # Crossing the spread and paying fees makes a wrong call worse and a right call smaller.
        # Jump risk widens realized loss through stop slippage.
        loss = self.loss_if_wrong_bps * (1 + jump_p) + spread_bps + fee_bps
        gain = max(self.gain_if_right_bps - spread_bps - fee_bps, 1e-9)
        return min(loss / (loss + gain) + self.margin, 0.99)


# One threshold per action. EXIT is cheap to get wrong (you forgo some upside),
# so it needs far less conviction than opening a position.
THRESHOLDS: dict[Action, ActionThreshold] = {
    Action.ENTER_LONG: ActionThreshold(loss_if_wrong_bps=20, gain_if_right_bps=30, margin=0.05),
    Action.ENTER_SHORT: ActionThreshold(loss_if_wrong_bps=20, gain_if_right_bps=30, margin=0.07),  # shorts: borrow/squeeze risk
    Action.EXIT: ActionThreshold(loss_if_wrong_bps=5, gain_if_right_bps=20, margin=0.0),
}

# Combination weights: (bot, question) -> weight. Only directional questions vote on direction.
DIRECTION_WEIGHTS: dict[tuple[str, str], float] = {
    ("gambler", "g_dir_5m"): 0.45,
    ("gambler", "g_dir_30m"): 0.20,
    ("spy", "s_imb_follow_5m"): 0.35,
}
JUMP_QUESTION = ("gambler", "g_jump_15m")
ADVERSE_QUESTION = ("house", "h_adverse_1m")

MAX_OTHER_MASS = 0.25   # if the judge puts this much weight on "other", it doesn't know: hold
FEE_BPS = 10.0          # round-trip taker fees assumed by the policy (risk/ re-checks with real fees)


@dataclass(frozen=True)
class Decision:
    action: Action
    p_up: float
    p_down: float
    p_other: float
    threshold: float | None
    expected_edge_bps: float
    reasons: tuple[str, ...] = field(default_factory=tuple)
    policy_version: str = POLICY_VERSION


def _direction_probs(bot: str, qid: str, dist: dict[str, float], snap: Snapshot) -> tuple[float, float, float]:
    """Map one question's distribution to (p_up, p_down, p_other)."""
    other = dist.get("other", 0.0)
    if qid.startswith("g_dir"):
        return dist.get("up", 0.0), dist.get("down", 0.0), other
    if qid == "s_imb_follow_5m":
        f, r = dist.get("follows", 0.0), dist.get("reverses", 0.0)
        if snap.imbalance > 0:
            return f, r, other
        if snap.imbalance < 0:
            return r, f, other
        return 0.0, 0.0, other + f + r  # no imbalance -> no directional information
    raise KeyError(f"no direction mapping for {bot}/{qid}")


def combine(answers: Answers, snap: Snapshot) -> tuple[float, float, float]:
    tot = up = down = other = 0.0
    for (bot, qid), w in DIRECTION_WEIGHTS.items():
        dist = answers.get(bot, {}).get(qid)
        if dist is None:
            continue
        u, d, o = _direction_probs(bot, qid, dist, snap)
        up, down, other, tot = up + w * u, down + w * d, other + w * o, tot + w
    if tot == 0:
        return 0.0, 0.0, 1.0
    return up / tot, down / tot, other / tot


def decide(answers: Answers, snap: Snapshot, position_side: int = 0) -> Decision:
    """position_side: +1 long, -1 short, 0 flat."""
    p_up, p_down, p_other = combine(answers, snap)
    jump_p = answers.get(JUMP_QUESTION[0], {}).get(JUMP_QUESTION[1], {}).get("yes", 0.0)

    if p_other > MAX_OTHER_MASS:
        return Decision(Action.HOLD, p_up, p_down, p_other, None, 0.0, (f"other_mass {p_other:.2f} > {MAX_OTHER_MASS}",))

    if position_side != 0:
        p_against = p_down if position_side > 0 else p_up
        t = THRESHOLDS[Action.EXIT].threshold(snap.spread_bps, FEE_BPS / 2)
        if p_against >= t:
            return Decision(Action.EXIT, p_up, p_down, p_other, t, 0.0, (f"p_against {p_against:.2f} >= {t:.2f}",))
        return Decision(Action.HOLD, p_up, p_down, p_other, t, 0.0, ("position open, no exit signal",))

    best: Decision | None = None
    for action, p in ((Action.ENTER_LONG, p_up), (Action.ENTER_SHORT, p_down)):
        th = THRESHOLDS[action]
        t = th.threshold(snap.spread_bps, FEE_BPS, jump_p)
        loss = th.loss_if_wrong_bps * (1 + jump_p) + snap.spread_bps + FEE_BPS
        gain = th.gain_if_right_bps - snap.spread_bps - FEE_BPS
        edge = p * gain - (1 - p) * loss
        if p >= t and (best is None or edge > best.expected_edge_bps):
            best = Decision(action, p_up, p_down, p_other, t, edge, (f"p {p:.2f} >= threshold {t:.2f}",))

    if best is None:
        return Decision(Action.HOLD, p_up, p_down, p_other, None, 0.0, ("no action cleared its threshold",))

    adverse = answers.get(ADVERSE_QUESTION[0], {}).get(ADVERSE_QUESTION[1], {})
    # A resting bid gets picked off when price is about to fall (and an ask when it's about to rise).
    # If the house expects that on the side we'd be trading into, don't take the trade.
    against_side = "bid" if best.action is Action.ENTER_LONG else "ask"
    if adverse.get(against_side, 0.0) > 0.5:
        return Decision(Action.HOLD, p_up, p_down, p_other, best.threshold, best.expected_edge_bps,
                        (f"house expects {against_side} to be picked off",))
    return best
