"""Offline interleavings at the shared full-validation and actual POST boundary."""

import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId

from pm_nautilus.live import LiveExecution, _TrackedRetryManager
from pm_nautilus.store import Store

from test_live_recovery import controlled_runtime, flush
from test_live_submit_cleanup import prepared_order


def prepare_signing(client):
    client._http_client.create_market_order = Mock(
        return_value=SimpleNamespace(makerAmount=1_000_000, takerAmount=10_000_000)
    )
    client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-gated"))
    client._http_client.post_order = Mock()
    retry = SimpleNamespace(run=AsyncMock(return_value={"success": False, "errorMsg": "test"}))
    client._retry_manager_pool.inner.acquire = AsyncMock(return_value=retry)
    client._retry_manager_pool.inner.release = AsyncMock()
    return retry


@pytest.mark.parametrize("outcome", ["ok", "invalid", "error", "cancel"])
def test_full_validation_keeps_post_waiting_and_failures_close_gates(
    tmp_path, monkeypatch, outcome
):
    async def run():
        async with controlled_runtime(tmp_path / f"{outcome}.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            retry = prepare_signing(client)
            original = Store._read_integrity
            entered, release, finished = Event(), Event(), Event()

            def reader(path, stop):
                entered.set()
                try:
                    while not release.wait(0.01) and not stop.is_set():
                        pass
                    if outcome == "error":
                        raise RuntimeError("controlled worker failure")
                    return False if outcome == "invalid" else original(path, stop)
                finally:
                    finished.set()

            monkeypatch.setattr(Store, "_read_integrity", staticmethod(reader))
            validation = asyncio.create_task(client._validate_ledger())
            submit = None
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                submit = asyncio.create_task(
                    client._submit_order(
                        SimpleNamespace(order=order, instrument_id=order.instrument_id)
                    )
                )
                await flush()
                assert not submit.done()
                retry.run.assert_not_awaited()
                client._http_client.post_order.assert_not_called()
                # Current quotes keep flowing while the full read-only check runs.
                runtime.book("1", [(91_000, 100_000_000)], [(100_000, 10_000_000)])
                assert runtime.books["1"].bid.external[91_000] == 100_000_000
                if outcome == "cancel":
                    validation.cancel()
                else:
                    release.set()
                await asyncio.wait_for(
                    asyncio.gather(validation, submit, return_exceptions=True), 5
                )
                await flush()
                assert finished.is_set()
                assert client._ledger_validation is None
                if outcome == "ok":
                    retry.run.assert_awaited_once()
                else:
                    retry.run.assert_not_awaited()
                    assert not client.ready and not client.open_orders_clear
                    assert runtime.status == "PAUSED"
                assert runtime.store.intent(oid)["terminal"]
                assert runtime.held_cash() == 0
                assert not runtime._test_queue_errors
            finally:
                release.set()
                await asyncio.gather(
                    validation, *(x for x in (submit,) if x), return_exceptions=True
                )

    asyncio.run(run())


@pytest.mark.parametrize("change", ["invalid_ledger", "changed_buy_qualification"])
def test_validation_started_during_sdk_pool_wait_still_prevents_post(tmp_path, change):
    async def run():
        async with controlled_runtime(tmp_path / "pool-gap.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            retry = prepare_signing(client)
            pool_entered, release_pool = asyncio.Event(), asyncio.Event()
            ledger_entered, release_ledger = asyncio.Event(), asyncio.Event()

            async def acquire():
                pool_entered.set()
                await release_pool.wait()
                return retry

            async def validation():
                ledger_entered.set()
                await release_ledger.wait()
                return {"ok": change != "invalid_ledger"}

            client._retry_manager_pool.inner.acquire = acquire
            runtime.validate_async = validation
            submit = asyncio.create_task(
                client._submit_order(
                    SimpleNamespace(order=order, instrument_id=order.instrument_id)
                )
            )
            ledger = None
            try:
                await asyncio.wait_for(pool_entered.wait(), 3)
                assert runtime.store.intent(oid)["venue_id"] == "venue-gated"
                assert oid not in client._post_possible
                ledger = asyncio.create_task(client._validate_ledger())
                await asyncio.wait_for(ledger_entered.wait(), 3)
                if change == "changed_buy_qualification":
                    client.buy_valid.return_value = False
                release_pool.set()
                await flush()
                retry.run.assert_not_awaited()
                release_ledger.set()
                await asyncio.wait_for(asyncio.gather(ledger, submit), 3)
                await flush()
                retry.run.assert_not_awaited()
                client._http_client.post_order.assert_not_called()
                assert runtime.store.intent(oid)["terminal"]
                assert runtime.held_cash() == 0
                assert runtime.status == "PAUSED"
                assert not runtime._test_queue_errors
            finally:
                release_pool.set()
                release_ledger.set()
                await asyncio.gather(submit, *(x for x in (ledger,) if x), return_exceptions=True)

    asyncio.run(run())


def test_owned_reconciliation_does_not_query_provably_unposted_identity(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "unposted.sqlite") as runtime:
            oid, _order = await prepared_order(runtime)
            client = runtime.client
            intent = runtime.store.intent(oid)
            intent["venue_id"] = "venue-unposted"
            runtime.store.save_intent(oid, intent)
            client._http_client.get_trades = Mock(return_value=[])
            client._http_client.get_order = Mock(return_value=None)
            client._update_account_state = AsyncMock()
            client._submitting[oid] = True
            await client.sync_owned()
            client._http_client.get_order.assert_not_called()
            assert runtime.held_cash() == 1_000_000
            # Once POST may have happened, a missing venue result remains unknown.
            client._post_possible.add(oid)
            with pytest.raises(ValueError, match="结果不明"):
                await client.sync_owned()
            client._http_client.get_order.assert_called_once_with("venue-unposted")
            assert not runtime.store.intent(oid)["terminal"]
            assert runtime.held_cash() == 1_000_000

    asyncio.run(run())


def test_paused_sell_can_exit_with_external_open_order_but_validation_blocks_it():
    async def run():
        order_id = ClientOrderId("controlled-sell")
        order = SimpleNamespace(side=OrderSide.SELL, is_closed=False, is_pending_cancel=False)
        owner = SimpleNamespace(
            status="PAUSED",
            store=SimpleNamespace(
                generation="generation",
                intent=Mock(return_value={"generation": "generation", "terminal": False}),
            ),
        )
        client = SimpleNamespace(
            ready=True,
            open_orders_clear=False,
            owner=owner,
            _cache=SimpleNamespace(order=Mock(return_value=order)),
            buy_valid=Mock(side_effect=AssertionError("SELL is not a BUY")),
            _ledger_gate=asyncio.Lock(),
            _post_possible=set(),
        )
        client._check_post_ready = LiveExecution._check_post_ready.__get__(client)
        inner = SimpleNamespace(run=AsyncMock(return_value={"success": True}))
        await _TrackedRetryManager(inner, client).run("submit_order", [order_id])
        inner.run.assert_awaited_once()
        assert str(order_id) in client._post_possible
        client.ready = False
        with pytest.raises(ValueError, match="状态已变化"):
            await _TrackedRetryManager(inner, client).run("submit_order", [order_id])
        assert inner.run.await_count == 1

    asyncio.run(run())


def test_failed_api_validation_is_not_overwritten_by_older_maintenance_round(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "readiness-race.sqlite") as runtime:
            await prepared_order(runtime)
            client = runtime.client
            client.sync_owned = AsyncMock()
            client.generate_position_status_reports = AsyncMock()
            runtime.validate_async = AsyncMock(side_effect=[{"ok": True}, {"ok": False}])
            entered, release, failed = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def open_orders():
                entered.set()
                await release.wait()
                client.open_orders_clear = True

            client._check_open_orders = open_orders
            original_pause = runtime.pause

            def pause():
                original_pause()
                failed.set()

            runtime.pause = pause
            maintain = asyncio.create_task(client.maintain())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert not (await client._validate_ledger())["ok"]
                assert not client.ready and not client.open_orders_clear
                release.set()
                await asyncio.wait_for(failed.wait(), 3)
                assert not client.ready and not client.open_orders_clear
                assert runtime.status == "PAUSED"
                assert runtime.store.get("live_error") == "ValueError"
            finally:
                release.set()
                maintain.cancel()
                await asyncio.gather(maintain, return_exceptions=True)

    asyncio.run(run())
