"""PM FAK simulation extension emitting native Nautilus execution/account events.

The stock sandbox charges quote-currency BUY fees and resets matching-book depth.
This venue-specific execution client implements PM's net-share and consumption
semantics; orders, event application, positions and portfolio remain Nautilus.
"""

from decimal import Decimal
from hashlib import sha256
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.events import OrderUpdated

from nautilus_trader.execution.client import ExecutionClient
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, LiquiditySide
from nautilus_trader.model.identifiers import AccountId, ClientId, Venue, VenueOrderId, TradeId
from nautilus_trader.model.objects import AccountBalance, Money
from .config import SCALE
from .native import price, quantity
from .rules import Target, plan_buy, sell_batches


class TestExecution(ExecutionClient):
    __test__ = False

    def __init__(self, owner):
        n = owner.native
        super().__init__(
            ClientId("POLYMARKET"),
            Venue("POLYMARKET"),
            OmsType.NETTING,
            AccountType.CASH,
            pUSD,
            n.bus,
            n.cache,
            n.clock,
        )
        self.owner = owner
        self._set_account_id(AccountId("POLYMARKET-TEST"))
        self._set_connected(True)

    def _send_order_event(self, event):
        # Commit before the native engine applies a simulated fact. Recovery can
        # replay a crash after this point; never regenerate a different fill.
        self.owner.store.journal(event)
        super()._send_order_event(event)

    def account(self):
        cash = self.owner.test_cash()
        amount = Money(Decimal(cash) / SCALE, pUSD)
        self.generate_account_state(
            balances=[AccountBalance(amount, Money(0, pUSD), amount)],
            margins=[],
            reported=True,
            ts_event=self._clock.timestamp_ns(),
        )

    def submit_order(self, command):
        o = command.order
        intent = self.owner.store.intent(o.client_order_id)
        if intent is None:
            raise ValueError("没有本程序的持久提交意图")
        t = self.owner.tokens[intent["token_id"]]
        b = self.owner.books[t.token_id]
        common = dict(
            strategy_id=o.strategy_id,
            instrument_id=o.instrument_id,
            client_order_id=o.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        self.generate_order_submitted(**common)
        common["venue_order_id"] = VenueOrderId(f"TEST-{o.client_order_id}")
        self.generate_order_accepted(**common)
        if intent["kind"] == "SETTLEMENT":
            from .rules import Fill, cost

            p = intent["limit"]
            fills = [
                Fill(p, intent["quantity"], intent["quantity"], cost(p, intent["quantity"]), 0)
            ]
        elif not b.ready:
            fills = []
        elif o.side == OrderSide.BUY:
            fills = plan_buy(
                b.ask.available(),
                intent["cash"],
                intent["limit"],
                t.min_size,
                t.fees,
                t.tick,
                self.owner.preferences,
            )
        else:
            target = Target("batch", intent["limit"], intent["quantity"], 0)
            batches = sell_batches(b.bid.available(), [target], t.min_size, t.fees)
            fills = [f for batch in batches for f in batch.fills]
        if o.is_quote_quantity and fills:
            now = self._clock.timestamp_ns()
            self._send_order_event(
                OrderUpdated(
                    trader_id=o.trader_id,
                    strategy_id=o.strategy_id,
                    instrument_id=o.instrument_id,
                    client_order_id=o.client_order_id,
                    venue_order_id=common["venue_order_id"],
                    account_id=self.account_id,
                    quantity=quantity(sum(f.net for f in fills)),
                    price=None,
                    trigger_price=None,
                    event_id=UUID4(),
                    ts_event=now,
                    ts_init=now,
                    is_quote_quantity=False,
                )
            )
        for index, f in enumerate(fills):
            # Cash delta is persisted in native fill info; no separate fill table.
            info = {
                "pm_gross": f.gross,
                "pm_net": f.net,
                "pm_amount": f.amount,
                "pm_fee": f.fee,
                "pm_kind": intent["kind"],
                "pm_generation": intent["generation"],
            }
            self.generate_order_filled(
                **common,
                venue_position_id=None,
                trade_id=TradeId(sha256(f"{o.client_order_id}-{index}".encode()).hexdigest()[:32]),
                order_side=o.side,
                order_type=o.order_type,
                last_qty=quantity(f.net),
                last_px=price(f.price),
                quote_currency=pUSD,
                commission=Money(Decimal(f.fee) / SCALE, pUSD),
                liquidity_side=LiquiditySide.TAKER,
                info=info,
            )
        if not o.is_closed:
            self.generate_order_canceled(**common)
        if self.owner.mode == "TEST":
            self.account()

    def cancel_order(self, command):
        o = self._cache.order(command.client_order_id)
        if o is not None and not o.is_closed:
            self.generate_order_canceled(
                o.strategy_id,
                o.instrument_id,
                o.client_order_id,
                o.venue_order_id,
                self._clock.timestamp_ns(),
            )
