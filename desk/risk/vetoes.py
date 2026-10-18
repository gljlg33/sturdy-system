"""Hard risk vetoes. Pure code; checked before every order; no model input can override them.

The only model-derived field these checks look at is the version string, and only
to refuse it if it is not the pinned one. Probabilities, confidence and "reasons"
never reach this module.
"""
from __future__ import annotations

from dataclasses import dataclass

from desk.broker.base import Account, Broker, Fill, Order, OrderRejected
from desk.state.snapshot import BookTop


@dataclass(frozen=True)
class RiskLimits:
    max_position_notional: float    # per symbol, absolute
    daily_loss_limit: float         # positive number; trading stops once pnl_today <= -limit
    max_leverage: float             # gross exposure / equity after the order
    range_guard_bps: float          # limit price must sit within this distance of the current mid
    fee_bps: float                  # actual venue fee per side
    gas_per_order: float            # flat cost per order (on-chain venues), in quote currency
    min_edge_over_costs_bps: float  # expected edge must beat fees + gas by at least this much
    pinned_model: str               # exact Jev version string every order must carry
    max_book_age_s: float = 5.0


@dataclass(frozen=True)
class Veto:
    check: str
    detail: str


def check_order(order: Order, account: Account, book: BookTop | None, limits: RiskLimits, now: float) -> list[Veto]:
    v: list[Veto] = []

    # Risk-reducing exits are never blocked by model or edge checks: blocking them adds risk.
    if not order.reduce_only and order.model_version != limits.pinned_model:
        v.append(Veto("model_version", f"order carries {order.model_version!r}, pinned {limits.pinned_model!r}"))

    if book is None or now - book.ts > limits.max_book_age_s:
        v.append(Veto("range_guard", "no fresh book to check the price against"))
        return v  # every remaining check needs a price

    mid = book.mid
    dist_bps = abs(order.limit_price - mid) / mid * 1e4
    if dist_bps > limits.range_guard_bps:
        v.append(Veto("range_guard", f"limit {order.limit_price} is {dist_bps:.1f} bps from mid {mid}"))

    cur = account.positions.get(order.symbol, 0.0)
    new = cur + order.side.sign * order.qty
    reduces = abs(new) < abs(cur) and (new == 0 or (new > 0) == (cur > 0))

    if abs(new) * mid > limits.max_position_notional and not reduces:
        v.append(Veto("max_position", f"post-trade notional {abs(new) * mid:.2f} > {limits.max_position_notional}"))

    if account.pnl_today <= -limits.daily_loss_limit and not reduces:
        v.append(Veto("daily_loss_limit", f"pnl_today {account.pnl_today:.2f} <= -{limits.daily_loss_limit}"))

    equity = account.equity
    gross_after = account.gross_exposure - abs(cur) * mid + abs(new) * mid
    if equity <= 0 or (gross_after / equity > limits.max_leverage and not reduces):
        v.append(Veto("leverage_cap", f"gross/equity after order {gross_after / max(equity, 1e-9):.2f} > {limits.max_leverage}"))

    if not order.reduce_only:
        notional = order.qty * mid
        gas_bps = limits.gas_per_order * 2 / notional * 1e4 if notional > 0 else float("inf")
        cost_bps = limits.fee_bps * 2 + gas_bps  # round trip
        if order.expected_edge_bps < cost_bps + limits.min_edge_over_costs_bps:
            v.append(Veto("fees_plus_gas", f"edge {order.expected_edge_bps:.1f} bps < costs {cost_bps:.1f} + {limits.min_edge_over_costs_bps}"))

    return v


class VetoedOrder(OrderRejected):
    def __init__(self, vetoes: list[Veto]):
        super().__init__(";".join(x.check for x in vetoes))
        self.vetoes = vetoes


class RiskCheckedBroker(Broker):
    """The only broker the desk is handed. Every submit runs every veto first."""

    def __init__(self, inner: Broker, limits: RiskLimits, book_of, clock, on_veto=None):
        if not inner.is_paper:
            raise SystemExit("refusing to start: this desk is paper-only")
        self._inner, self._limits, self._book_of, self._clock = inner, limits, book_of, clock
        self._on_veto = on_veto or (lambda order, vetoes: None)

    @property
    def is_paper(self) -> bool:
        return True

    def account(self) -> Account:
        return self._inner.account()

    def position(self, symbol: str) -> float:
        return self._inner.position(symbol)

    def submit_order(self, order: Order) -> Fill:
        vetoes = check_order(order, self._inner.account(), self._book_of(order.symbol), self._limits, self._clock())
        if vetoes:
            self._on_veto(order, vetoes)
            raise VetoedOrder(vetoes)
        return self._inner.submit_order(order)
