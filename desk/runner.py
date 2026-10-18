"""The desk loop: one decision per block.

shadow: Jev labels every decision; the policy logs what it would have done; nothing executes.
        A virtual position is tracked in the book so the one-position rule behaves the same.
paper:  the policy executes against the paper broker, through the risk-checked wrapper.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from desk.book.book import Book, OpenPosition
from desk.broker.base import Order, OrderRejected, Side
from desk.broker.paper import PaperBroker
from desk.config import Mode
from desk.feed import Feed
from desk.filter.funnel import Candidate, Funnel
from desk.judge.client import JudgeClient
from desk.logs.outcomes import RESOLVERS
from desk.logs.store import EventLog, new_id
from desk.policy.policy import Action
from desk.questions import BATTERIES
from desk.risk.vetoes import RiskCheckedBroker, RiskLimits, VetoedOrder
from desk.state.snapshot import BookTop, EquityPoint, Snapshot, build_snapshot

HISTORY_S = 2 * 3600
STOP_LOSS_BPS = 20     # hard exit in code, no judge needed
MAX_HOLD_S = 1800      # time stop


@dataclass
class _Pending:
    decision_id: str
    bot: str
    qid: str
    snapshot: Snapshot


@dataclass
class Desk:
    mode: Mode
    symbols: tuple[str, ...]
    feed: Feed
    judge: JudgeClient
    limits: RiskLimits
    log: EventLog
    block_seconds: int
    order_notional: float
    paper: PaperBroker
    book: Book = field(default_factory=Book)
    _history: dict[str, list[BookTop]] = field(default_factory=dict)
    _equity: list[EquityPoint] = field(default_factory=list)
    _pending: list[_Pending] = field(default_factory=list)
    _last_ts: float | None = None
    _day: str | None = None
    _exit_model_version: str | None = None

    def __post_init__(self):
        self.broker = RiskCheckedBroker(self.paper, self.limits, self._latest_book, self._now, self._on_veto)
        self.funnel = Funnel(book=self.book, log=self.log, judge=self.judge, limits=self.limits,
                             account=self.paper.account, min_order_notional=self.order_notional)
        self._clock_now = 0.0

    # -- plumbing -------------------------------------------------------------
    def _now(self) -> float:
        return self._clock_now

    def _latest_book(self, symbol: str) -> BookTop | None:
        h = self._history.get(symbol)
        return h[-1] if h else None

    def _on_veto(self, order: Order, vetoes) -> None:
        for v in vetoes:
            self.log.write("veto", self._now(), client_id=order.client_id, symbol=order.symbol,
                           side=order.side.value, check=v.check, detail=v.detail, mode=self.mode.value)

    def _ingest(self, now: float) -> None:
        start = self._last_ts if self._last_ts is not None else now - HISTORY_S
        for s in self.symbols:
            h = self._history.setdefault(s, [])
            h.extend(self.feed.books(s, start, now))
            cut = now - HISTORY_S
            while h and h[0].ts < cut:
                h.pop(0)
            if h:
                self.paper.on_book(s, h[-1])
        self._last_ts = now
        self._equity.append(EquityPoint(now, self.paper.account().equity))

    def _snapshot(self, symbol: str, now: float) -> Snapshot:
        return build_snapshot(symbol, now, self._history[symbol], self._equity, self.paper.position(symbol))

    def _resolve_outcomes(self, now: float) -> None:
        still = []
        for p in self._pending:
            q = BATTERIES[p.bot].get(p.qid)
            books = [b for b in self._history.get(p.snapshot.symbol, []) if b.ts < now]
            realized = RESOLVERS[q.resolver](p.snapshot, books, q.horizon_s)
            if realized is None:
                if now - p.snapshot.decision_ts < q.horizon_s + HISTORY_S / 2:
                    still.append(p)
                continue
            self.log.write("outcome", now, decision_id=p.decision_id, bot=p.bot, question_id=p.qid, realized=realized)
        self._pending = still

    # -- one block ------------------------------------------------------------
    def step(self, now: float) -> None:
        self._clock_now = now
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self._day:
            self.paper.start_day()
            self._day = day
        self._ingest(now)
        self._resolve_outcomes(now)
        next_block = (math.floor(now / self.block_seconds) + 1) * self.block_seconds
        # The judge client times against the wall clock; express "start of next block" in it.
        deadline = time.time() + (next_block - now)

        if self.book.open is not None:
            self._manage_open(now, deadline)
            return

        cands = []
        for s in self.symbols:
            if not self._history.get(s):
                continue
            snap = self._snapshot(s, now)
            cands.append(Candidate(new_id(), snap, self._history[s][-1]))
        pick = self.funnel.run(cands, now, deadline)
        self._track_questions(cands)
        if pick is not None:
            self._enter(pick, now)

    def _track_questions(self, cands: list[Candidate]) -> None:
        for c in cands:
            for bot in c.judged:
                for q in BATTERIES[bot].questions:
                    self._pending.append(_Pending(c.decision_id, bot, q.id, c.snapshot))

    def _enter(self, c: Candidate, now: float) -> None:
        d = c.decision
        side = Side.BUY if d.action is Action.ENTER_LONG else Side.SELL
        bt = c.book_top
        px = bt.ask if side is Side.BUY else bt.bid
        versions = {r.model_version for r in c.judged.values()}
        order = Order(new_id(), c.snapshot.symbol, side, self.order_notional / px, px, now,
                      expected_edge_bps=d.expected_edge_bps,
                      model_version=versions.pop() if len(versions) == 1 else "mixed:" + ",".join(sorted(versions)))
        self.log.write("intent", now, decision_id=c.decision_id, client_id=order.client_id, symbol=order.symbol,
                       side=side.value, qty=order.qty, limit_price=px, mode=self.mode.value)

        if self.mode is Mode.SHADOW:
            self.book.open_position(OpenPosition(order.symbol, side.sign, order.qty, px, now, c.decision_id))
            return
        try:
            fill = self.broker.submit_order(order)
        except VetoedOrder:
            self.book.bench(order.symbol, "risk_veto", now)
            return
        except OrderRejected as e:
            self.log.write("order_rejected", now, client_id=order.client_id, reason=e.reason)
            return
        self._log_fill(fill, c.decision_id)
        self.book.open_position(OpenPosition(fill.symbol, side.sign, fill.qty, fill.price, now, c.decision_id))

    def _manage_open(self, now: float, deadline: float) -> None:
        pos = self.book.open
        bt = self._latest_book(pos.symbol)
        mark = bt.bid if pos.side > 0 else bt.ask
        pnl_bps = pos.side * (mark - pos.entry_price) / pos.entry_price * 1e4

        reason = None
        if pnl_bps <= -STOP_LOSS_BPS:
            reason = f"stop_loss {pnl_bps:.1f} bps"
        elif now - pos.entry_ts >= MAX_HOLD_S:
            reason = "time_stop"
        else:
            c = Candidate(new_id(), self._snapshot(pos.symbol, now), bt)
            self.log.write("snapshot", now, decision_id=c.decision_id, **c.snapshot.to_dict())
            if self.funnel.jev(c, now, deadline) is None and c.decision.action is Action.EXIT:
                reason = "policy_exit"
                self._exit_model_version = ",".join(sorted({r.model_version for r in c.judged.values()}))
            self._track_questions([c])
        if reason is None:
            return

        self.log.write("exit_intent", now, decision_id=pos.decision_id, symbol=pos.symbol, reason=reason,
                       pnl_bps=pnl_bps, mode=self.mode.value)
        if self.mode is Mode.SHADOW:
            self._close(reason, now)
            return
        side = Side.SELL if pos.side > 0 else Side.BUY
        exit_version = (self._exit_model_version or "unknown") if reason == "policy_exit" else f"code:{reason.split()[0]}"
        order = Order(new_id(), pos.symbol, side, pos.qty, mark, now, expected_edge_bps=0.0,
                      model_version=exit_version, reduce_only=True)
        try:
            fill = self.broker.submit_order(order)
        except OrderRejected as e:
            self.log.write("order_rejected", now, client_id=order.client_id, reason=e.reason)
            return
        self._log_fill(fill, pos.decision_id)
        self._close(reason, now)

    def _close(self, reason: str, now: float) -> None:
        pos = self.book.close_position()
        if reason.startswith("stop_loss"):
            self.book.bench(pos.symbol, "stopped_out", now)

    def _log_fill(self, fill, decision_id: str) -> None:
        a = self.paper.account()
        self.log.write("fill", fill.ts, decision_id=decision_id, client_id=fill.client_id, symbol=fill.symbol,
                       side=fill.side.value, qty=fill.qty, price=fill.price, fee=fill.fee, gas=fill.gas,
                       equity_after=a.equity, pnl_today=a.pnl_today)

    def run_forever(self) -> None:
        while True:
            now = time.time()
            self.step(now)
            nxt = (math.floor(now / self.block_seconds) + 1) * self.block_seconds
            time.sleep(max(0.0, nxt - time.time()))
