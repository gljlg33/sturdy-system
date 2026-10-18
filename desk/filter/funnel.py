"""The funnel. Cheapest check first; every rejection is logged with the check that fired.

  1. free_cut      data already in hand (bench, staleness, spread)       cost: nothing
  2. one_call      one external lookup per candidate (venue status etc.)  cost: one call
  3. hard_facts    code-only risk precheck on a minimum order             cost: nothing, needs (2)
  4. jev           each bot's full battery, one judge call per bot        cost: three Jev calls
  5. final_pick    best expected edge among survivors                     cost: nothing
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from desk.book.book import Book
from desk.broker.base import Account, Order, Side
from desk.judge.client import JudgeClient, JudgeResult, MalformedQuestionError
from desk.logs.store import EventLog
from desk.policy.policy import MAX_OTHER_MASS, Action, Decision, decide
from desk.questions import BATTERIES
from desk.risk.vetoes import RiskLimits, check_order
from desk.state.snapshot import BookTop, Snapshot

MAX_SNAPSHOT_AGE_S = 5.0
MAX_SPREAD_BPS = 15.0


@dataclass
class Candidate:
    decision_id: str
    snapshot: Snapshot
    book_top: BookTop
    judged: dict[str, JudgeResult] = field(default_factory=dict)
    decision: Decision | None = None


@dataclass(frozen=True)
class Rejection:
    check: str
    detail: str


OneCallCheck = Callable[[str], "tuple[bool, str]"]


def no_external_check(symbol: str) -> tuple[bool, str]:
    return True, "no one-call check configured"


class Funnel:
    def __init__(self, *, book: Book, log: EventLog, judge: JudgeClient, limits: RiskLimits,
                 account: Callable[[], Account], min_order_notional: float,
                 one_call_check: OneCallCheck = no_external_check):
        self.book, self.log, self.judge, self.limits = book, log, judge, limits
        self.account, self.min_notional, self.one_call = account, min_order_notional, one_call_check

    # -- stages ---------------------------------------------------------------
    def free_cut(self, c: Candidate, now: float) -> Rejection | None:
        s = c.snapshot
        if (b := self.book.benched(s.symbol, now)) is not None:
            return Rejection("benched", f"{b.reason} until {b.until_ts:.0f}")
        if s.decision_ts - s.as_of_ts > MAX_SNAPSHOT_AGE_S:
            return Rejection("stale_data", f"age {s.decision_ts - s.as_of_ts:.1f}s")
        if s.spread_bps > MAX_SPREAD_BPS:
            return Rejection("spread_too_wide", f"{s.spread_bps:.1f} bps")
        return None

    def one_call_check(self, c: Candidate, now: float) -> Rejection | None:
        ok, detail = self.one_call(c.snapshot.symbol)
        return None if ok else Rejection("one_call_check", detail)

    def hard_facts(self, c: Candidate, now: float) -> Rejection | None:
        # Would a minimum-size order in either direction clear the size/loss/leverage vetoes?
        # Edge and model version are set to pass here; they're re-checked for real at order time.
        mid = c.snapshot.mid
        for side in (Side.BUY, Side.SELL):
            probe = Order("probe", c.snapshot.symbol, side, self.min_notional / mid, mid, now,
                          expected_edge_bps=float("inf"), model_version=self.limits.pinned_model)
            if not check_order(probe, self.account(), c.book_top, self.limits, now):
                return None
        return Rejection("hard_facts", "no minimum order would pass the risk vetoes")

    def jev(self, c: Candidate, now: float, deadline_ts: float) -> Rejection | None:
        ctx = c.snapshot.to_prompt()

        def ask(bot):
            battery = BATTERIES[bot]
            self.log.write("question", now, decision_id=c.decision_id, bot=bot, context=ctx, questions=battery.to_wire())
            try:
                return bot, self.judge.ask(bot, c.decision_id, ctx, battery.to_wire(), deadline_ts)
            except MalformedQuestionError as e:
                self.log.write("judge_error", now, decision_id=c.decision_id, bot=bot, error="malformed_question", detail=str(e))
                return bot, None

        with ThreadPoolExecutor(max_workers=len(BATTERIES)) as ex:
            results = dict(ex.map(ask, BATTERIES))

        for bot, res in results.items():
            if res is None:
                return Rejection("judge_timeout", f"{bot} returned nothing before the next block")
            self.log.write("answer", now, decision_id=c.decision_id, bot=bot, model_version=res.model_version,
                           pinned_model=self.limits.pinned_model, answers=res.answers, latency_ms=res.latency_ms)
        c.judged = results

        pos = self.book.open
        side = pos.side if pos and pos.symbol == c.snapshot.symbol else 0
        c.decision = decide({b: r.answers for b, r in results.items()}, c.snapshot, side)
        self.log.write("decision", now, decision_id=c.decision_id, symbol=c.snapshot.symbol,
                       action=c.decision.action.value, p_up=c.decision.p_up, p_down=c.decision.p_down,
                       p_other=c.decision.p_other, threshold=c.decision.threshold,
                       expected_edge_bps=c.decision.expected_edge_bps, reasons=list(c.decision.reasons),
                       policy_version=c.decision.policy_version)
        if c.decision.action is Action.HOLD:
            reason = "judge_unsure" if c.decision.p_other > MAX_OTHER_MASS else "policy_hold"
            return Rejection(reason, "; ".join(c.decision.reasons))
        return None

    # -- driver ---------------------------------------------------------------
    def run(self, candidates: list[Candidate], now: float, deadline_ts: float) -> Candidate | None:
        if not self.book.can_scan():
            raise RuntimeError("funnel must not scan while a position is open")

        def reject(c: Candidate, r: Rejection):
            self.log.write("rejection", now, decision_id=c.decision_id, symbol=c.snapshot.symbol, check=r.check, detail=r.detail)
            if r.check != "benched":
                self.book.bench(c.snapshot.symbol, r.check, now)

        for c in candidates:
            self.log.write("snapshot", now, decision_id=c.decision_id, **c.snapshot.to_dict())

        alive = candidates
        for stage in (self.free_cut, self.one_call_check, self.hard_facts):
            nxt = []
            for c in alive:
                (reject(c, r) if (r := stage(c, now)) else nxt.append(c))
            alive = nxt

        survivors = []
        for c in alive:
            if (r := self.jev(c, now, deadline_ts)) is not None:
                reject(c, r)
            else:
                survivors.append(c)

        if not survivors:
            return None
        survivors.sort(key=lambda c: c.decision.expected_edge_bps, reverse=True)
        pick, losers = survivors[0], survivors[1:]
        for c in losers:
            reject(c, Rejection("lost_final_pick", f"edge {c.decision.expected_edge_bps:.1f} < {pick.decision.expected_edge_bps:.1f}"))
        self.log.write("final_pick", now, decision_id=pick.decision_id, symbol=pick.snapshot.symbol, action=pick.decision.action.value)
        return pick
