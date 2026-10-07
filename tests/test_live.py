"""Controlled adapter responses only; no keys, signatures or private network."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.common.credentials import PolymarketWebSocketAuth
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.identifiers import VenueOrderId, ClientOrderId
from pm_nautilus.live import LiveExecution
from pm_nautilus.strategy import Runtime
from pm_nautilus.rules import Token, Fees

ADDRESS = "0x" + "1" * 40
CONDITION = "0x" + "a" * 64


class HTTP:
    builder = SimpleNamespace(funder=ADDRESS)
    creds = SimpleNamespace(api_key="controlled-key")

    def get_address(self):
        return ADDRESS


class Provider(InstrumentProvider):
    pass


class Trade:
    id = "remote-trade-1"
    status = "MATCHED"
    market = CONDITION
    match_time = "100"
    transaction_hash = "0xcontrolled"

    def get_filled_user_order_ids(self, *args):
        return ["venue-1"]

    def get_asset_id(self, *args):
        return "1"

    def last_qty(self, *args):
        return Decimal("9.6")

    def last_px(self, *args):
        return Decimal("0.1")

    def liquidity_side(self):
        return LiquiditySide.TAKER


async def flush():
    for _ in range(5):
        await asyncio.sleep(0)


def test_confirmed_only_dedup_and_native_recovery(tmp_path):
    async def run():
        def factory(r):
            if r.store.get("last_cash") is None:
                r.store.put("last_cash", 100_000_000)
            return LiveExecution(
                r,
                HTTP(),
                Provider(),
                PolymarketWebSocketAuth("x", "y", "z"),
                PolymarketExecClientConfig(),
            )

        r = Runtime(tmp_path / "live.sqlite", mode="LIVE", live_factory=factory)
        now = r.now
        day = 86400000000000
        t = Token(
            "1",
            "e",
            "m",
            CONDITION,
            "YES",
            2,
            False,
            now - day,
            now + 9 * day,
            Fees(True, 40000),
            5_000_000,
            1000,
        )
        r.add_tokens([t])
        r.book("1", [(90000, 100_000_000)], [(100000, 10_000_000)])
        r.client._submit_order = AsyncMock()  # Capture native command, never sign or connect.
        oid = r.submit(t, "BUY", 10_000_000, 990000, cash=1_000_000)
        await flush()
        o = r.native.cache.order(ClientOrderId(oid))
        r.native.cache.add_venue_order_id(o.client_order_id, VenueOrderId("venue-1"))
        r.client.generate_order_submitted(o.strategy_id, o.instrument_id, o.client_order_id, r.now)
        r.client._send_quote_to_base_update(
            o,
            VenueOrderId("venue-1"),
            __import__("pm_nautilus.native", fromlist=["quantity"]).quantity(10_000_000),
        )
        r.client.generate_order_accepted(
            o.strategy_id, o.instrument_id, o.client_order_id, VenueOrderId("venue-1"), r.now
        )
        await flush()
        msg = Trade()
        r.client.ingest_trade(msg)
        await flush()
        assert not r.business["cycles"]
        assert r.held_cash() == 1_000_000
        msg.status = "CONFIRMED"
        r.client.ingest_trade(msg)
        r.client.ingest_trade(msg)
        await flush()
        assert r.business["cycles"]["e"]["quantity"] == 9_600_000
        assert r.business["cycles"]["e"]["spent"] == 994_560
        assert len([e for _, e in r.store.events() if type(e).__name__ == "OrderFilled"]) == 1
        assert not r.store.intent(oid)["terminal"]
        r.client._cancel_order = AsyncMock()
        r.close()
        await flush()
        r = Runtime(tmp_path / "live.sqlite", mode="LIVE", live_factory=factory)
        assert r.business["cycles"]["e"]["quantity"] == 9_600_000
        assert r.validate()["ok"]
        r.client._cancel_order = AsyncMock()
        r.close()
        await flush()

    asyncio.run(run())


def test_better_price_partial_fills_can_exceed_signed_minimum_shares_and_replay(tmp_path):
    async def run():
        def factory(runtime):
            runtime.store.put("last_cash", 100_000_000)
            client = LiveExecution(
                runtime,
                HTTP(),
                Provider(),
                PolymarketWebSocketAuth("x", "y", "z"),
                PolymarketExecClientConfig(),
            )
            client._submit_order = AsyncMock()
            client._cancel_order = AsyncMock()
            return client

        r = Runtime(tmp_path / "partial.sqlite", mode="LIVE", live_factory=factory)
        day = 86_400_000_000_000
        t = Token(
            "1",
            "e",
            "m",
            CONDITION,
            "YES",
            2,
            False,
            r.now - day,
            r.now + 9 * day,
            Fees(True, 40_000),
            5_000_000,
            10_000,
        )
        r.add_tokens([t])
        r.book("1", [(10_000, 100_000_000)], [(20_000, 50_000_000)])
        oid = r.submit(t, "BUY", 48_000_000, 30_000, cash=1_000_000, execution_limit=30_000)
        await flush()
        order = r.native.cache.order(ClientOrderId(oid))
        venue = VenueOrderId("venue-1")
        r.native.cache.add_venue_order_id(order.client_order_id, venue)
        r.client.generate_order_submitted(
            order.strategy_id, order.instrument_id, order.client_order_id, r.now
        )
        # A 0.96U order signed at 3¢ has 32 minimum shares, yet can receive
        # 40 shares across cheaper fills. Keep native order open for both fills.
        r.client._send_quote_to_base_update(
            order,
            venue,
            __import__("pm_nautilus.native", fromlist=["quantity"]).quantity(96_000_100),
        )
        r.client.generate_order_accepted(
            order.strategy_id, order.instrument_id, order.client_order_id, venue, r.now
        )
        await flush()
        first = Trade()
        first.id = "partial-1"
        first.status = "CONFIRMED"
        first.last_qty = lambda *_: Decimal("10")
        first.last_px = lambda *_: Decimal("0.03")
        r.client.ingest_trade(first)
        await flush()
        second = Trade()
        second.id = "partial-2"
        second.status = "CONFIRMED"
        second.last_qty = lambda *_: Decimal("30")
        second.last_px = lambda *_: Decimal("0.02")
        r.client.ingest_trade(second)
        r.client.ingest_trade(second)
        await flush()
        assert r.business["cycles"]["e"]["quantity"] == 40_000_000
        assert r.business["cycles"]["e"]["spent"] == 935_160
        assert len(r.store.order_fills(oid)) == 2
        assert r.validate()["ok"]
        r.close()
        await flush()
        r = Runtime(tmp_path / "partial.sqlite", mode="LIVE", live_factory=factory)
        assert r.business["cycles"]["e"]["quantity"] == 40_000_000
        assert r.business["cycles"]["e"]["spent"] == 935_160
        assert r.validate()["ok"]
        r.close()
        await flush()

    asyncio.run(run())
