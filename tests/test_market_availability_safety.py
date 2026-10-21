"""Unavailable public markets cannot discard ownership or poison other markets."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from nautilus_trader.model.objects import AccountBalance, Money

from pm_nautilus.live import LiveExecution
from pm_nautilus.market_availability import MarketUnavailable
from pm_nautilus.market import MarketService
from pm_nautilus.redemption import RedemptionService

from test_live_order_terms import prepared_sell, unsigned_sdk
from test_live_recovery import controlled_runtime, flush, token
from test_live_submit_cleanup import prepared_order
from test_runtime import setup
from test_storage_post_boundary import configure_signed_buy, command


def mark_unavailable(runtime, item):
    runtime.market_availability[item.token_id] = {
        "status": "UNAVAILABLE",
        "reason": "archived",
        "marketId": item.market_id,
        "conditionId": item.condition_id,
        "tokenId": item.token_id,
        "since": "2026-10-20T00:00:00+00:00",
        "checkedAt": "2026-10-20T00:00:00+00:00",
        "lastError": None,
    }


def terminal_events(runtime, oid):
    return [
        type(event).__name__
        for _, event in runtime.store.events()
        if str(getattr(event, "client_order_id", "")) == oid
        and type(event).__name__ in {"OrderDenied", "OrderRejected"}
    ]


def test_direct_unavailable_orders_do_not_create_intent_or_touch_native_state(
    tmp_path, monkeypatch
):
    runtime, _, item = setup(tmp_path)
    mark_unavailable(runtime, item)
    before = runtime.store.db.total_changes
    try:
        for owner, method in (
            (runtime.native, "ensure_instrument"),
            (runtime, "checkpoint_book"),
            (runtime.store, "save_intent"),
            (runtime.store, "journal"),
        ):
            monkeypatch.setattr(owner, method, Mock(side_effect=AssertionError(method)))
        for side in ("BUY", "SELL"):
            assert runtime.submit(item, side, 10_000_000, 100_000, cash=1_000_000) is None
        assert runtime.submit(item, "BUY", 10_000_000, 100_000, kind="SETTLEMENT") is None
        assert runtime.store.db.total_changes == before
        assert not runtime.store.intents() and not runtime.store.events()
        assert runtime.cash() == 100_000_000 and runtime.status == "PAUSED"
    finally:
        runtime.close()


def test_stopped_market_gate_covers_counteroutcome_without_blocking_same_event_peer(tmp_path):
    runtime, _, item = setup(tmp_path)
    counter = replace(item, token_id="2", direction="NO")
    peer = replace(
        item, token_id="3", market_id="different-market", condition_id="different-condition"
    )
    runtime.add_tokens([counter, peer])
    mark_unavailable(runtime, item)
    try:
        assert not runtime.market_can_trade(item.token_id)
        assert not runtime.market_can_trade(counter.token_id)
        assert runtime.market_can_trade(peer.token_id)
        before = runtime.store.db.total_changes
        assert runtime.submit(counter, "BUY", 10_000_000, 100_000, cash=1_000_000) is None
        assert runtime.store.db.total_changes == before
        assert not runtime.store.intents() and not runtime.store.events()
    finally:
        runtime.close()


def test_reopened_counteroutcome_stays_monitored_and_blocks_buy_until_fresh_book(tmp_path):
    runtime, _, item = setup(tmp_path)
    counter = replace(item, token_id="2", direction="NO")
    runtime.add_tokens([counter])
    for candidate in (item, counter):
        mark_unavailable(runtime, candidate)
    raw_open = {
        "id": item.market_id,
        "conditionId": item.condition_id,
        "clobTokenIds": [item.token_id, counter.token_id],
        "outcomes": [item.direction, counter.direction],
        "active": True,
        "closed": False,
        "archived": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }

    def snapshot(token_id):
        return {
            "event_type": "book",
            "asset_id": token_id,
            "market": item.condition_id,
            "timestamp": 1,
            "bids": [{"price": "0.015", "size": "100"}],
            "asks": [{"price": "0.02", "size": "50"}],
        }

    async def run():
        service = MarketService(runtime)
        service.sync_subscriptions = AsyncMock()
        try:
            await service.observe_market_availability(raw_open, item)
            runtime.refresh_eligibility()
            assert {item.token_id, counter.token_id} <= runtime.monitored
            assert runtime.market_recovering == {item.token_id, counter.token_id}
            runtime.start()
            service.message(snapshot(item.token_id), runtime.monitored)
            assert runtime.market_can_trade(item.token_id)
            assert not runtime.market_can_trade(counter.token_id)
            assert runtime.evaluations[item.event_id][0] == "INCOMPLETE"
            assert not runtime.store.intents()
            service.message(snapshot(counter.token_id), runtime.monitored)
            assert not runtime.market_recovering
            assert runtime.market_can_trade(counter.token_id)
            assert [intent["side"] for intent in runtime.store.intents().values()] == ["BUY"]
            assert runtime.validate()["ok"]
        finally:
            await service.close()

    try:
        asyncio.run(run())
    finally:
        runtime.close()


def test_archive_does_not_block_strictly_identified_test_settlement(tmp_path):
    runtime, _, item = setup(tmp_path)
    runtime.book(item.token_id, [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    runtime.start()
    runtime.pause()
    mark_unavailable(runtime, item)
    service = RedemptionService(runtime, None)
    service.identify(item, {item.token_id: 1_000_000, "sibling": 0})
    claim = runtime.business["claims"][item.condition_id]

    async def settle():
        await service.advance(claim)
        await service.advance(claim)

    try:
        asyncio.run(settle())
        assert claim["state"] == "CREDITED"
        assert runtime.cash() == 149_000_000
        assert runtime.validate()["ok"]
    finally:
        runtime.close()


def test_unposted_buy_market_deferral_keeps_other_market_owned_sell_available(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "other-market.sqlite") as runtime:
            sell_oid, sell_order = await prepared_sell(runtime)
            client = runtime.client
            client.generate_account_state(
                [AccountBalance(Money(3, pUSD), Money(0, pUSD), Money(3, pUSD))],
                [],
                True,
                runtime.now,
            )
            runtime.store.put("last_cash", 3_000_000)
            await flush()
            next_token = token(runtime, index=2)
            runtime.add_tokens([next_token])
            runtime.book(next_token.token_id, [(90_000, 100_000_000)], [(100_000, 10_000_000)])
            buy_oid = runtime.submit(
                next_token,
                "BUY",
                10_000_000,
                990_000,
                cash=1_000_000,
                execution_limit=100_000,
            )
            await flush()
            buy_order = runtime.native.cache.order(ClientOrderId(buy_oid))
            client._sdk_terms[next_token.token_id] = (
                next_token.tick,
                next_token.neg_risk,
                next_token.fees,
            )
            configure_signed_buy(client, "unavailable-buy")
            retry = SimpleNamespace(run=AsyncMock())

            async def archive_after_signing():
                client._http_client.create_market_order.assert_called_once()
                assert runtime.store.intent(buy_oid)["venue_id"] == "unavailable-buy"
                mark_unavailable(runtime, next_token)
                return retry

            client._retry_manager_pool.inner.acquire = AsyncMock(side_effect=archive_after_signing)
            client._retry_manager_pool.inner.release = AsyncMock()
            sell_before = deepcopy(runtime.store.intent(sell_oid))
            error_before = runtime.store.get("live_error")
            await client._submit_order(command(buy_order))
            await flush()

            retry.run.assert_not_awaited()
            client._http_client.post_order.assert_not_called()
            assert runtime.store.intent(buy_oid)["terminal"] is True
            assert terminal_events(runtime, buy_oid) == ["OrderRejected"]
            assert runtime.store.intent(sell_oid) == sell_before
            assert runtime.status == "RUNNING"
            assert client.ready and client.open_orders_clear
            assert runtime.store.get("live_error") == error_before

            client._http_client = unsigned_sdk(runtime.tokens["1"])
            client._sdk_terms.clear()
            client._post_signed_order = AsyncMock()
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("other-market-exit"))
            await client._submit_order(command(sell_order))
            await flush()
            client._post_signed_order.assert_awaited_once()
            assert runtime.validate()["ok"]
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_queued_sell_archive_before_post_preserves_owned_quantity_and_readiness(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "queued-sell.sqlite") as runtime:
            sell_oid, sell_order = await prepared_sell(runtime)
            client = runtime.client
            item = runtime.tokens["1"]
            client._http_client = unsigned_sdk(item)
            client._sdk_terms.clear()
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("unavailable-exit"))
            retry = SimpleNamespace(run=AsyncMock())

            async def archive_after_signing():
                assert runtime.store.intent(sell_oid)["venue_id"] == "unavailable-exit"
                mark_unavailable(runtime, item)
                return retry

            client._retry_manager_pool.inner.acquire = AsyncMock(side_effect=archive_after_signing)
            client._retry_manager_pool.inner.release = AsyncMock()
            before_business = deepcopy(runtime.business)
            before_positions = [
                str(position.quantity) for position in runtime.native.cache.positions_open()
            ]
            before_cash = runtime.cash()
            before_error = runtime.store.get("live_error")
            await LiveExecution._submit_order(client, command(sell_order))
            await flush()

            retry.run.assert_not_awaited()
            client._http_client._post.assert_not_called()
            assert runtime.store.intent(sell_oid)["terminal"] is True
            assert terminal_events(runtime, sell_oid) == ["OrderRejected"]
            assert runtime.business["cycles"] == before_business["cycles"]
            assert runtime.business["targets"] == before_business["targets"]
            assert runtime.business["claims"] == before_business["claims"]
            assert [
                str(position.quantity) for position in runtime.native.cache.positions_open()
            ] == before_positions
            assert runtime.cash() == before_cash
            assert runtime.status == "RUNNING"
            assert client.ready and client.open_orders_clear
            assert runtime.store.get("live_error") == before_error
            assert runtime.validate()["ok"]
            assert not runtime._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_possible_post_market_error_preserves_uncertain_order_and_ownership(tmp_path, side):
    async def run():
        async with controlled_runtime(tmp_path / f"unknown-{side}.sqlite") as runtime:
            if side == "BUY":
                oid, order = await prepared_order(runtime)
                configure_signed_buy(runtime.client, "possible-order")
            else:
                oid, order = await prepared_sell(runtime)
                runtime.client._http_client = unsigned_sdk(runtime.tokens["1"])
                runtime.client._sdk_terms.clear()
                runtime.client._expected_venue_order_id = Mock(
                    return_value=VenueOrderId("possible-order")
                )
            client = runtime.client
            item = runtime.tokens["1"]
            before_quantity = deepcopy(runtime.business["cycles"])
            before_held_cash = runtime.held_cash()

            async def uncertain_attempt(*_args, **_kwargs):
                assert oid in client._post_possible
                mark_unavailable(runtime, item)
                raise MarketUnavailable("controlled unavailable market after possible POST")

            retry = SimpleNamespace(run=AsyncMock(side_effect=uncertain_attempt))
            client._retry_manager_pool.inner.acquire = AsyncMock(return_value=retry)
            client._retry_manager_pool.inner.release = AsyncMock()
            await LiveExecution._submit_order(client, command(order))
            await flush()

            assert retry.run.await_count == 1
            assert runtime.store.intent(oid)["terminal"] is False
            assert runtime.store.intent(oid)["venue_id"] == "possible-order"
            assert not terminal_events(runtime, oid)
            assert runtime.held_cash() == before_held_cash
            assert runtime.business["cycles"] == before_quantity
            assert runtime.status == "PAUSED"
            assert not client.ready and not client.open_orders_clear
            assert "结果未确认" in runtime.store.get("live_error")
            assert not runtime._test_queue_errors

    asyncio.run(run())
