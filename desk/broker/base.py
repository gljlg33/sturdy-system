"""Broker interface. A live broker would implement exactly this; this repo ships only PaperBroker."""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field


class Side(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True)
class Order:
    client_id: str
    symbol: str
    side: Side
    qty: float
    limit_price: float          # never send unbounded market orders; range guard checks this
    ts: float
    expected_edge_bps: float    # policy's estimate, re-checked against fees + gas by risk/
    model_version: str          # Jev version that produced the judgment behind this order
    reduce_only: bool = False


@dataclass(frozen=True)
class Fill:
    client_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    gas: float
    ts: float


@dataclass
class Account:
    cash: float
    positions: dict[str, float] = field(default_factory=dict)   # symbol -> signed qty
    marks: dict[str, float] = field(default_factory=dict)       # symbol -> last mid
    day_start_equity: float = 0.0
    fees_today: float = 0.0

    @property
    def equity(self) -> float:
        return self.cash + sum(q * self.marks.get(s, 0.0) for s, q in self.positions.items())

    @property
    def gross_exposure(self) -> float:
        return sum(abs(q) * self.marks.get(s, 0.0) for s, q in self.positions.items())

    @property
    def pnl_today(self) -> float:
        return self.equity - self.day_start_equity


class OrderRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Broker(abc.ABC):
    @abc.abstractmethod
    def submit_order(self, order: Order) -> Fill: ...

    @abc.abstractmethod
    def account(self) -> Account: ...

    @abc.abstractmethod
    def position(self, symbol: str) -> float: ...

    @property
    @abc.abstractmethod
    def is_paper(self) -> bool: ...
