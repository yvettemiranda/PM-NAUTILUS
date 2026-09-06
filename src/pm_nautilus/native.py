"""Version-pinned Nautilus components; no independent order/position state machine."""

from decimal import Decimal
import asyncio

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.config import ExecEngineConfig
from nautilus_trader.data.engine import DataEngine
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import AssetClass, BookAction, OrderSide
from nautilus_trader.model.events import OrderInitialized
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.orders.unpacker import OrderUnpacker
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.risk.engine import RiskEngine
from .config import SCALE


def quantity(n):
    return Quantity.from_str(f"{Decimal(n) / SCALE:.6f}")


def price(n):
    return Price.from_str(f"{Decimal(n) / SCALE:.6f}")


def instrument_id(t):
    return InstrumentId.from_str(f"{t.condition_id}-{t.token_id}.POLYMARKET")


def make_instrument(t):
    return BinaryOption(
        instrument_id(t),
        Symbol(t.token_id),
        AssetClass.ALTERNATIVE,
        pUSD,
        6,
        6,
        price(t.tick),
        quantity(1),
        t.opened_ns,
        t.ends_ns,
        0,
        0,
        outcome=t.direction,
        description=t.question or t.token_id,
        info={
            "neg_risk": t.neg_risk,
            "negRisk": t.neg_risk,
            "feeSchedule": {
                "rate": float(Decimal(t.fees.rate) / SCALE),
                "exponent": t.fees.exponent,
            },
        },
    )


class Native:
    def __init__(self, owner, clock=None):
        self.owner = owner
        self.clock = clock or LiveClock()
        self.trader_id = TraderId("PM-TEST-001" if owner.mode == "TEST" else "PM-LIVE-001")
        self.bus = MessageBus(trader_id=self.trader_id, clock=self.clock)
        self.cache = Cache()
        self.portfolio = Portfolio(msgbus=self.bus, cache=self.cache, clock=self.clock)
        self.data = DataEngine(msgbus=self.bus, cache=self.cache, clock=self.clock)
        self.risk = RiskEngine(
            portfolio=self.portfolio, msgbus=self.bus, cache=self.cache, clock=self.clock
        )
        if owner.mode == "LIVE":
            from nautilus_trader.live.execution_engine import LiveExecutionEngine
            from nautilus_trader.live.config import LiveExecEngineConfig

            self.execution = LiveExecutionEngine(
                loop=asyncio.get_running_loop(),
                msgbus=self.bus,
                cache=self.cache,
                clock=self.clock,
                config=LiveExecEngineConfig(
                    allow_overfills=True,
                    reconciliation=True,
                    filter_unclaimed_external_orders=True,
                    filter_position_reports=True,
                    generate_missing_orders=False,
                    inflight_check_interval_ms=0,
                ),
            )
        else:
            self.execution = ExecutionEngine(
                msgbus=self.bus,
                cache=self.cache,
                clock=self.clock,
                config=ExecEngineConfig(allow_overfills=True),
            )
        self.strategy = None

    def register(self, strategy, client):
        self.strategy = strategy
        strategy.register(self.trader_id, self.portfolio, self.bus, self.cache, self.clock)
        self.execution.register_oms_type(strategy)
        self.execution.register_client(client)
        for t in self.owner.tokens.values():
            self.data.process(make_instrument(t))
        # Initialize native order cache and rebuild positions by replaying canonical
        # native execution events. PM projection runs only after framework recovery.
        self.owner.replaying = True
        for _, e in self.owner.store.events():
            if isinstance(e, OrderInitialized):
                self.cache.add_order(OrderUnpacker.from_init(e))
            elif hasattr(e, "client_order_id"):
                ExecutionEngine.process(self.execution, e)
        self.owner.replaying = False
        self.bus.subscribe("events.order.*", self.owner.on_native, priority=100)
        for component in (self.data, self.risk, self.execution, strategy):
            component.start()

    def feed(self, t, book):
        """Publish complete public books through Nautilus DataEngine and Strategy."""
        iid = instrument_id(t)
        deltas = [OrderBookDelta.clear(iid, 0, book.timestamp, book.timestamp)]
        values = [(OrderSide.BUY, p, q) for p, q in book.bid.external.items()]
        values += [(OrderSide.SELL, p, q) for p, q in book.ask.external.items()]
        for i, (side, p, q) in enumerate(values):
            deltas.append(
                OrderBookDelta(
                    iid,
                    BookAction.ADD,
                    BookOrder(side, price(p), quantity(q), 0),
                    128 if i == len(values) - 1 else 0,
                    0,
                    book.timestamp,
                    book.timestamp,
                )
            )
        self.data.process(OrderBookDeltas(iid, deltas))

    def stop(self):
        for c in (self.strategy, self.execution, self.risk, self.data):
            if c is not None:
                c.stop()
