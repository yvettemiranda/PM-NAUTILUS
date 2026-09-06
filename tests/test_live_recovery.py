"""Real native orders with controlled venue responses; no signer or network."""

import asyncio
from contextlib import asynccontextmanager

import msgspec
from unittest.mock import AsyncMock

from nautilus_trader.adapters.polymarket.common.credentials import PolymarketWebSocketAuth
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.schemas.trade import PolymarketTradeReport
from nautilus_trader.adapters.polymarket.schemas.user import PolymarketUserTrade
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from nautilus_trader.model.objects import AccountBalance, Money

from pm_nautilus.live import LiveExecution
from pm_nautilus.native import instrument_id, quantity
from pm_nautilus.rules import Fees, Token
from pm_nautilus.strategy import Runtime

from test_live import ADDRESS, CONDITION, HTTP, Provider, flush


def factory(runtime):
    # Surface native queue exceptions as test failures instead of allowing the
    # engine's production os._exit safeguard to terminate the entire pytest run.
    runtime._test_queue_errors = []
    runtime.native.execution._handle_queue_exception = (
        lambda error, queue: runtime._test_queue_errors.append((queue, error))
    )
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


@asynccontextmanager
async def controlled_runtime(path):
    runtime = Runtime(path, mode="LIVE", live_factory=factory)
    try:
        yield runtime
    finally:
        runtime.close()
        await flush()


async def fund(runtime):
    runtime.client.generate_account_state(
        [AccountBalance(Money(1, pUSD), Money(0, pUSD), Money(1, pUSD))],
        [],
        True,
        runtime.now,
    )
    runtime.store.put("last_cash", 1_000_000)
    await flush()


def token(runtime, index=1):
    day = 86_400_000_000_000
    return Token(
        str(index),
        f"e{index}",
        f"m{index}",
        CONDITION if index == 1 else "0x" + "b" * 64,
        "YES",
        2,
        False,
        runtime.now - day,
        runtime.now + 9 * day,
        Fees(False),
        5_000_000,
        1000,
    )


def venue_trade(t, *, websocket=False):
    payload = {
        "asset_id": t.token_id,
        "bucket_index": 0,
        "fee_rate_bps": "0",
        "id": "confirmed-trade-1",
        "last_update": "100",
        "maker_address": ADDRESS,
        "maker_orders": [],
        "market": t.condition_id,
        "match_time": "100",
        "outcome": "YES",
        "owner": "x",
        "price": "0.1",
        "side": "BUY",
        "size": "10",
        "status": "CONFIRMED",
        "taker_order_id": "venue-1",
        "trader_side": "TAKER",
    }
    if websocket:
        payload.update(event_type="trade", timestamp="100", trade_owner="x", type="TRADE")
        schema = PolymarketUserTrade
    else:
        payload["transaction_hash"] = "0x" + "c" * 64
        schema = PolymarketTradeReport
    return msgspec.json.decode(msgspec.json.encode(payload), type=schema)


async def accept(runtime, oid):
    order = runtime.native.cache.order(ClientOrderId(oid))
    venue = VenueOrderId("venue-1")
    runtime.native.cache.add_venue_order_id(order.client_order_id, venue)
    intent = runtime.store.intent(oid)
    intent["venue_id"] = str(venue)
    runtime.store.save_intent(oid, intent)
    runtime.client.generate_order_submitted(
        order.strategy_id, order.instrument_id, order.client_order_id, runtime.now
    )
    runtime.client._send_quote_to_base_update(order, venue, quantity(10_000_000))
    runtime.client.generate_order_accepted(
        order.strategy_id, order.instrument_id, order.client_order_id, venue, runtime.now
    )
    await flush()


def assert_owned_fill(runtime, t):
    assert not runtime._test_queue_errors, runtime._test_queue_errors
    fills = [event for _, event in runtime.store.events() if isinstance(event, OrderFilled)]
    assert len(fills) == 1
    assert runtime.business["cycles"][t.event_id]["quantity"] == 10_000_000
    assert runtime.business["cycles"][t.event_id]["spent"] == 1_000_000
    positions = runtime.native.cache.positions_open(instrument_id=instrument_id(t))
    assert sum(p.quantity.as_decimal() for p in positions) == 10


def test_confirmed_real_websocket_schema_without_transaction_hash(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live.sqlite") as runtime:
            await fund(runtime)
            t = token(runtime)
            runtime.add_tokens([t])
            oid = runtime.submit(t, "BUY", 10_000_000, 990_000, cash=1_000_000)
            await flush()
            await accept(runtime, oid)
            trade = venue_trade(t, websocket=True)
            assert not hasattr(trade, "transaction_hash")
            runtime.client.ingest_trade(trade)
            runtime.client.ingest_trade(trade)
            await flush()
            assert_owned_fill(runtime, t)

    asyncio.run(run())


def test_restart_restores_submitted_order_venue_identity_before_ack(tmp_path):
    async def run():
        path = tmp_path / "live.sqlite"
        runtime = Runtime(path, mode="LIVE", live_factory=factory)
        await fund(runtime)
        t = token(runtime)
        runtime.add_tokens([t])
        runtime.book(t.token_id, [(90_000, 100_000_000)], [(100_000, 10_000_000)])
        oid = runtime.submit(t, "BUY", 10_000_000, 990_000, cash=1_000_000)
        await flush()
        order = runtime.native.cache.order(ClientOrderId(oid))
        runtime.client.generate_order_submitted(
            order.strategy_id, order.instrument_id, order.client_order_id, runtime.now
        )
        await flush()
        intent = runtime.store.intent(oid)
        intent["venue_id"] = "venue-1"
        intent["submitted_ns"] = runtime.now
        intent["base_quantity"] = 10_000_000
        runtime.store.save_intent(oid, intent)
        runtime.native.cache.add_venue_order_id(order.client_order_id, VenueOrderId("venue-1"))
        assert not any(
            type(event).__name__ in {"OrderAccepted", "OrderUpdated", "OrderFilled"}
            for _, event in runtime.store.events()
        )
        # Crash after the durable POST identity, before any venue acknowledgement.
        # Stop native tasks without PAUSE/cancel creating an artificial venue result.
        runtime.native.stop()
        await flush()
        runtime.store.close()

        async with controlled_runtime(path) as restored:
            await flush()
            assert restored.native.cache.client_order_id(VenueOrderId("venue-1")) == ClientOrderId(
                oid
            )
            trade = venue_trade(t)
            restored.client.ingest_trade(trade)
            restored.client.ingest_trade(trade)
            await flush()
            assert_owned_fill(restored, t)

    asyncio.run(run())


def test_confirmed_spend_cannot_reuse_stale_cash_for_second_event(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live.sqlite") as runtime:
            await fund(runtime)
            tokens = [token(runtime, index) for index in (1, 2)]
            runtime.add_tokens(tokens)
            for t in tokens:
                runtime.book(t.token_id, [(90_000, 100_000_000)], [(100_000, 10_000_000)])
            runtime.client.ready = True
            runtime.start()
            await flush()
            assert len(runtime.store.intents()) == 1
            oid = next(iter(runtime.store.intents()))
            intent = runtime.store.intent(oid)
            first = runtime.tokens[intent["token_id"]]
            second = next(t for t in tokens if t.event_id != first.event_id)
            await accept(runtime, oid)
            runtime.client.ingest_trade(venue_trade(first))
            await flush()
            assert_owned_fill(runtime, first)
            # The venue has spent the entire 1U. No fresh account response has
            # arrived, so the unchanged 1U snapshot must not finance another Event.
            runtime.book(second.token_id, [(90_000, 100_000_000)], [(100_000, 10_000_000)])
            await flush()
            assert len(runtime.store.intents()) == 1, "已确认支出不能重新分配给第二个 Event"
            assert not any(
                i["event_id"] == second.event_id for i in runtime.store.intents().values()
            )

    asyncio.run(run())


def test_live_cash_fak_rounds_limit_down_to_tick_without_raising_budget(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import Mock

    async def run():
        async with controlled_runtime(tmp_path / "live.sqlite") as r:
            await fund(r)
            t = replace(token(r), tick=10000)
            r.add_tokens([t])
            r.update_preferences({"maxBuyPriceCents": "98.7"})
            r.book(t.token_id, [(90000, 100_000_000)], [(100000, 10_000_000)])
            r.client.ready = True
            r.start()
            await flush()
            oid = next(iter(r.store.intents()))
            order = r.native.cache.order(ClientOrderId(oid))
            r.client._http_client.create_market_order = Mock(
                return_value=SimpleNamespace(takerAmount=10_000_000)
            )
            r.client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-1"))
            r.client._post_signed_order = AsyncMock()
            await r.client._submit_market_order(SimpleNamespace(order=order), None)
            args = r.client._http_client.create_market_order.call_args.args[0]
            assert args.price == 0.98 and args.amount == 1 and args.order_type == "FAK"
            assert r.store.intent(oid)["cash"] == 1_000_000
            assert not r.business["cycles"]  # Signing result never becomes a Fill.

    asyncio.run(run())
