"""Paper broker: simulated fills against the latest observed book. No network, no keys."""
from __future__ import annotations

from desk.broker.base import Account, Broker, Fill, Order, OrderRejected, Side
from desk.state.snapshot import BookTop


class PaperBroker(Broker):
    def __init__(self, starting_cash: float, fee_bps: float = 5.0, gas_per_order: float = 0.0):
        self._acct = Account(cash=starting_cash, day_start_equity=starting_cash)
        self._books: dict[str, BookTop] = {}
        self.fee_bps = fee_bps
        self.gas_per_order = gas_per_order

    @property
    def is_paper(self) -> bool:
        return True

    def on_book(self, symbol: str, book: BookTop) -> None:
        self._books[symbol] = book
        self._acct.marks[symbol] = book.mid

    def start_day(self) -> None:
        self._acct.day_start_equity = self._acct.equity
        self._acct.fees_today = 0.0

    def account(self) -> Account:
        return self._acct

    def position(self, symbol: str) -> float:
        return self._acct.positions.get(symbol, 0.0)

    def submit_order(self, order: Order) -> Fill:
        book = self._books.get(order.symbol)
        if book is None:
            raise OrderRejected("no_book")
        # Marketable limit: fill at the touch if the limit allows it, otherwise reject (no resting orders yet).
        px = book.ask if order.side is Side.BUY else book.bid
        if (order.side is Side.BUY and px > order.limit_price) or (order.side is Side.SELL and px < order.limit_price):
            raise OrderRejected("limit_not_marketable")
        fee = abs(order.qty) * px * self.fee_bps / 1e4
        signed = order.side.sign * order.qty
        a = self._acct
        a.cash -= signed * px + fee + self.gas_per_order
        a.fees_today += fee + self.gas_per_order
        new = a.positions.get(order.symbol, 0.0) + signed
        if abs(new) < 1e-12:
            a.positions.pop(order.symbol, None)
        else:
            a.positions[order.symbol] = new
        return Fill(order.client_id, order.symbol, order.side, order.qty, px, fee, self.gas_per_order, order.ts)
