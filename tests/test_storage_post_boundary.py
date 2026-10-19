"""Expected storage deferral must preserve exits without releasing unknown orders."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from nautilus_trader.model.objects import AccountBalance, Money

from pm_nautilus.live import LiveExecution
from pm_nautilus.storage_guard import GIB, StoragePressure

from test_live_order_terms import prepared_sell, unsigned_sdk
from test_live_recovery import controlled_runtime, flush, token
from test_live_submit_cleanup import prepared_order
from test_storage_integration import ControlledStorage


def configure_signed_buy(client, venue="deferred-buy"):
    client._http_client.create_market_order = Mock(
        return_value=SimpleNamespace(makerAmount=1_000_000, takerAmount=10_000_000)
    )
    client._http_client.post_order = Mock(side_effect=AssertionError("No real POST permitted"))
    client._expected_venue_order_id = Mock(return_value=VenueOrderId(venue))
    client._submit_order = LiveExecution._submit_order.__get__(client)


def command(order):
    return SimpleNamespace(order=order, instrument_id=order.instrument_id)


def test_pressure_after_signing_defers_only_unposted_buy_and_preserves_owned_sell(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live.sqlite") as runtime:
            sell_oid, sell_order = await prepared_sell(runtime)
            client = runtime.client
            # Keep the already-owned SELL while a separate Event prepares a BUY.
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
            configure_signed_buy(client)
            storage = ControlledStorage(tmp_path)
            runtime.storage_guard = storage.guard
            await storage.set(10 * GIB)
            retry = SimpleNamespace(run=AsyncMock())

            async def acquire_after_signing():
                # This await is after signing, account checks and durable venue ID.
                client._http_client.create_market_order.assert_called_once()
                assert runtime.store.intent(buy_oid)["outbound_terms"]
                assert runtime.store.intent(buy_oid)["venue_id"] == "deferred-buy"
                await storage.set(GIB)
                return retry

            client._retry_manager_pool.inner.acquire = AsyncMock(side_effect=acquire_after_signing)
            client._retry_manager_pool.inner.release = AsyncMock()
            previous_error = runtime.store.get("live_error")
            sell_before = runtime.store.intent(sell_oid)

            await client._submit_order(command(buy_order))
            await flush()

            client._http_client.post_order.assert_not_called()
            retry.run.assert_not_awaited()
            assert runtime.store.intent(buy_oid)["terminal"] is True
            terminal = [
                type(event).__name__
                for _, event in runtime.store.events()
                if str(getattr(event, "client_order_id", "")) == buy_oid
                and type(event).__name__ in {"OrderDenied", "OrderRejected"}
            ]
            assert terminal == ["OrderRejected"]
            assert runtime.status == "RUNNING"
            assert client.ready and client.open_orders_clear
            assert runtime.store.get("live_error") == previous_error
            assert runtime.store.intent(sell_oid) == sell_before
            assert not runtime._test_queue_errors

            # Run the real outer SELL path while capacity remains CRITICAL.
            # Only the final external POST is captured by a local mock.
            client._http_client = unsigned_sdk(runtime.tokens["1"])
            client._sdk_terms.clear()
            client._post_signed_order = AsyncMock()
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("owned-exit"))
            await client._submit_order(command(sell_order))
            await flush()
            client._post_signed_order.assert_awaited_once()
            assert runtime.status == "RUNNING"
            assert client.ready and client.open_orders_clear
            assert runtime.store.intent(sell_oid)["terminal"] is False
            assert runtime.validate()["ok"]
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_storage_error_after_possible_post_preserves_unknown_buy_reservation(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "unknown.sqlite") as runtime:
            buy_oid, buy_order = await prepared_order(runtime)
            client = runtime.client
            configure_signed_buy(client, "possible-buy")
            storage = ControlledStorage(tmp_path)
            runtime.storage_guard = storage.guard
            await storage.set(10 * GIB)

            async def uncertain_attempt(*_args, **_kwargs):
                # The tracked manager has crossed its conservative possible-POST
                # boundary; an error cannot establish that the venue saw nothing.
                assert buy_oid in client._post_possible
                await storage.set(GIB)
                raise StoragePressure("controlled storage pressure after possible POST")

            retry = SimpleNamespace(run=AsyncMock(side_effect=uncertain_attempt))
            client._retry_manager_pool.inner.acquire = AsyncMock(return_value=retry)
            client._retry_manager_pool.inner.release = AsyncMock()

            await client._submit_order(command(buy_order))
            await flush()

            assert retry.run.await_count == 1
            assert runtime.store.intent(buy_oid)["terminal"] is False
            assert runtime.store.intent(buy_oid)["venue_id"] == "possible-buy"
            assert runtime.held_cash() == 1_000_000
            assert runtime.status == "PAUSED"
            assert not client.ready and not client.open_orders_clear
            assert "结果未确认" in runtime.store.get("live_error")
            assert not [
                event
                for _, event in runtime.store.events()
                if str(getattr(event, "client_order_id", "")) == buy_oid
                and type(event).__name__ in {"OrderDenied", "OrderRejected"}
            ]
            client._http_client.post_order.assert_not_called()
            assert not runtime._test_queue_errors

    asyncio.run(run())
