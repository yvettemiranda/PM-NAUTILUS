"""Offline failures around the exact boundary before a venue order can be posted."""

import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId

from pm_nautilus.live import LiveExecution

from test_live_recovery import controlled_runtime, flush, fund, token


async def prepared_order(runtime):
    await fund(runtime)
    t = token(runtime)
    runtime.add_tokens([t])
    runtime.book(t.token_id, [(90_000, 100_000_000)], [(100_000, 10_000_000)])
    client = runtime.client
    client.ready = True
    client.open_orders_clear = True
    runtime.start()
    await flush()
    oid = next(iter(runtime.store.intents()))
    order = runtime.native.cache.order(ClientOrderId(oid))
    client._submit_order = LiveExecution._submit_order.__get__(client)
    client._maintain_active_market = AsyncMock()
    client.buy_valid = Mock(return_value=True)
    client._check_buy_account = AsyncMock(return_value=True)
    return oid, order


def test_signing_failure_releases_unposted_reservation(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "signing.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            client._http_client.create_market_order = Mock(
                side_effect=ConnectionError("controlled signing failure")
            )
            client._http_client.post_order = Mock()
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()

            assert runtime.store.intent(oid)["terminal"] is True
            assert runtime.held_cash() == 0
            assert not runtime.active_intents("e1")
            assert runtime.status == "PAUSED"
            assert "ConnectionError" in runtime.store.get("live_error")
            assert [type(event).__name__ for _, event in runtime.store.events()].count(
                "OrderDenied"
            ) == 1
            client._http_client.post_order.assert_not_called()
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_failure_after_submitted_but_before_venue_identity_rejects(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "identity.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            client._http_client.create_market_order = Mock(
                return_value=SimpleNamespace(takerAmount=10_000_000)
            )
            client._expected_venue_order_id = Mock(
                side_effect=ValueError("controlled identity failure")
            )
            client._http_client.post_order = Mock()
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()

            intent = runtime.store.intent(oid)
            assert intent["terminal"] is True
            assert "venue_id" not in intent
            assert runtime.held_cash() == 0
            kinds = [type(event).__name__ for _, event in runtime.store.events()]
            assert "OrderSubmitted" in kinds and "OrderRejected" in kinds
            client._http_client.post_order.assert_not_called()
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_missing_expected_venue_identity_rejects_without_post(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "missing-identity.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            client._http_client.create_market_order = Mock(
                return_value=SimpleNamespace(takerAmount=10_000_000)
            )
            client._expected_venue_order_id = Mock(return_value=None)
            client._http_client.post_order = Mock()
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()

            assert runtime.store.intent(oid)["terminal"] is True
            assert runtime.held_cash() == 0
            assert runtime.status == "PAUSED"
            kinds = [type(event).__name__ for _, event in runtime.store.events()]
            assert kinds.count("OrderRejected") == 1
            client._http_client.post_order.assert_not_called()
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_exception_after_post_boundary_keeps_unknown_outcome(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "unknown.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            client._http_client.create_market_order = Mock(
                return_value=SimpleNamespace(takerAmount=10_000_000)
            )
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-unknown"))
            client._http_client.post_order = Mock()
            controlled_post = AsyncMock(side_effect=ConnectionError("unknown POST result"))
            retry = SimpleNamespace(run=controlled_post)
            client._retry_manager_pool.inner.acquire = AsyncMock(return_value=retry)
            client._retry_manager_pool.inner.release = AsyncMock()
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()

            intent = runtime.store.intent(oid)
            assert intent["venue_id"] == "venue-unknown"
            assert intent["terminal"] is False
            assert runtime.held_cash() == 1_000_000
            assert runtime.active_intents("e1")
            assert runtime.status == "PAUSED"
            assert "结果未确认" in runtime.store.get("live_error")
            kinds = [type(event).__name__ for _, event in runtime.store.events()]
            assert "OrderDenied" not in kinds and "OrderRejected" not in kinds
            assert controlled_post.await_count == 1
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_retry_pool_acquire_failure_releases_unposted_reservation(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "pool-acquire.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            client._http_client.create_market_order = Mock(
                return_value=SimpleNamespace(takerAmount=10_000_000)
            )
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-unposted"))
            client._http_client.post_order = Mock()
            client._retry_manager_pool.inner.acquire = AsyncMock(
                side_effect=RuntimeError("controlled pool failure")
            )
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()

            intent = runtime.store.intent(oid)
            assert intent["venue_id"] == "venue-unposted"
            assert intent["terminal"] is True
            assert runtime.held_cash() == 0
            assert not runtime.active_intents("e1")
            assert runtime.status == "PAUSED"
            assert "提交前失败" in runtime.store.get("live_error")
            kinds = [type(event).__name__ for _, event in runtime.store.events()]
            assert kinds.count("OrderRejected") == 1
            client._http_client.post_order.assert_not_called()
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_cancelled_post_result_does_not_release_order(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "cancelled-post.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            client._http_client.create_market_order = Mock(
                return_value=SimpleNamespace(takerAmount=10_000_000)
            )
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-cancelled"))
            # The pinned RetryManager reports cancellation as None with no
            # last_exception even though its to_thread POST may still finish.
            retry = SimpleNamespace(
                run=AsyncMock(return_value=None),
                last_exception=None,
                message="Canceled retry",
            )
            client._retry_manager_pool.acquire = AsyncMock(return_value=retry)
            client._retry_manager_pool.release = AsyncMock()
            client._http_client.post_order = Mock()
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()

            intent = runtime.store.intent(oid)
            assert intent["venue_id"] == "venue-cancelled"
            assert intent["terminal"] is False
            assert runtime.held_cash() == 1_000_000
            assert runtime.status == "PAUSED"
            assert "结果未确认" in runtime.store.get("live_error")
            kinds = [type(event).__name__ for _, event in runtime.store.events()]
            assert "OrderRejected" not in kinds and "OrderDenied" not in kinds
            assert retry.run.await_count == 1
            client._http_client.post_order.assert_not_called()
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_cancel_during_inflight_post_keeps_unknown_reservation(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "inflight-cancel.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            client._http_client.create_market_order = Mock(
                return_value=SimpleNamespace(takerAmount=10_000_000)
            )
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-inflight"))
            entered = Event()
            release = Event()
            finished = Event()

            def post_order(*_args):
                entered.set()
                try:
                    if not release.wait(timeout=5):
                        raise TimeoutError("controlled POST did not resume")
                    return {"success": True, "orderID": "venue-inflight"}
                finally:
                    finished.set()

            client._http_client.post_order = Mock(side_effect=post_order)
            submit = asyncio.create_task(
                client._submit_order(
                    SimpleNamespace(order=order, instrument_id=order.instrument_id)
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                submit.cancel()
                try:
                    await submit
                except asyncio.CancelledError:
                    pass
                await flush()

                # The worker thread can still finish after the awaiting task is
                # cancelled. The venue outcome is unknown until reconciliation.
                intent = runtime.store.intent(oid)
                assert intent["venue_id"] == "venue-inflight"
                assert intent["terminal"] is False
                assert runtime.held_cash() == 1_000_000
                assert runtime.active_intents("e1")
                assert runtime.status == "PAUSED"
                assert "结果未确认" in runtime.store.get("live_error")
                kinds = [type(event).__name__ for _, event in runtime.store.events()]
                assert "OrderRejected" not in kinds and "OrderDenied" not in kinds
            finally:
                release.set()
                assert await asyncio.to_thread(finished.wait, 2)
            assert not runtime._test_queue_errors

    asyncio.run(run())
