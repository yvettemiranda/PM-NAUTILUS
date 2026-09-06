"""Independent side state; only observed additions replenish TEST liquidity."""

from dataclasses import dataclass, field
from hashlib import sha256
from .rules import Level


@dataclass
class Side:
    external: dict[int, int] = field(default_factory=dict)
    consumed: dict[int, int] = field(default_factory=dict)

    def update(self, levels: list[Level], snapshot: bool = True):
        incoming = dict(levels)
        if snapshot:
            incoming = {p: incoming.get(p, 0) for p in self.external.keys() | incoming.keys()}
        for p, qty in incoming.items():
            if not 0 < p < 1_000_000 or qty < 0:
                raise ValueError("非法盘口数值")
            # A reduction removes available public depth first. Consumed shadow size
            # remains up to new public size; later growth releases only the new size.
            self.consumed[p] = min(self.consumed.get(p, 0), qty)
            self.external[p] = qty
            if qty == 0:
                self.external.pop(p, None)
                self.consumed.pop(p, None)

    @property
    def version(self):
        return sha256(repr(sorted(self.external.items())).encode()).hexdigest()

    def available(self):
        return [
            (p, q - self.consumed.get(p, 0))
            for p, q in self.external.items()
            if q > self.consumed.get(p, 0)
        ]

    def consume(self, price: int, qty: int):
        if qty < 0 or qty > self.external.get(price, 0) - self.consumed.get(price, 0):
            raise ValueError("模拟成交超出可用深度")
        self.consumed[price] = self.consumed.get(price, 0) + qty


@dataclass
class Book:
    bid: Side = field(default_factory=Side)
    ask: Side = field(default_factory=Side)
    ready: bool = False
    timestamp: int = 0
    revision: int = 0

    def snapshot(self, bids: list[Level], asks: list[Level], timestamp: int):
        if timestamp < self.timestamp:
            return False
        self.bid.update(bids)
        self.ask.update(asks)
        self.timestamp, self.ready = timestamp, True
        self.revision += 1
        return True

    def delta(self, side: str, levels: list[Level], timestamp: int):
        if not self.ready or timestamp < self.timestamp:
            return False
        (self.bid if side == "BID" else self.ask).update(levels, False)
        self.timestamp = timestamp
        self.revision += 1
        return True

    def disconnect(self):
        self.ready = False
