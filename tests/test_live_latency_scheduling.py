"""Controlled delays verify concurrency and event scheduling without venue traffic."""

import asyncio
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nautilus_trader.model.identifiers import VenueOrderId

from pm_nautilus.live import LiveExecution

from test_live_recovery import controlled_runtime, flush
from test_live_submit_cleanup import prepared_order


@pytest.mark.parametrize("failing_read", ["orders", "balances"])
def test_buy_account_lookup_failure_never_posts_and_invalidates_gates(tmp_path, failing_read):
    async def run():
        async with controlled_runtime(tmp_path / "failure.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            yes = runtime.tokens["1"]
            runtime.add_tokens([replace(yes, token_id="2", direction="NO")])
            client._http_client.create_market_order = Mock(
                return_value=SimpleNamespace(makerAmount=1_000_000, takerAmount=10_000_000)
            )
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-1"))
            client._http_client.post_order = Mock()
            client._check_buy_account = LiveExecution._check_buy_account.__get__(client)
            client._check_open_orders = AsyncMock(return_value=True)
            client.wallet.token_balances = Mock(return_value={"1": 0, "2": 0})
            if failing_read == "orders":
                client._check_open_orders.side_effect = ConnectionError("controlled failure")
            else:
                client.wallet.token_balances.side_effect = ConnectionError("controlled failure")
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()
            client._http_client.post_order.assert_not_called()
            assert runtime.status == "PAUSED"
            assert client.ready is False and client.open_orders_clear is False
            assert runtime.store.intent(oid)["terminal"]
            assert runtime.held_cash() == 0
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_buy_account_reads_overlap_and_use_current_local_inventory():
    async def run():
        values = {}
        yes = SimpleNamespace(token_id="1", condition_id="condition")
        no = SimpleNamespace(token_id="2", condition_id="condition")
        cycle = {"token_id": "1", "quantity": 5_000_000}
        owner = SimpleNamespace(
            tokens={"1": yes, "2": no},
            business={"cycles": {"event": cycle}},
            store=SimpleNamespace(put=lambda key, value: values.update({key: value})),
            pause=Mock(),
        )
        balance_started = Event()
        release_balance = Event()

        def balances(_ids):
            balance_started.set()
            assert release_balance.wait(3)
            return {"1": 6_000_000, "2": 0}

        async def open_orders():
            # A serial implementation times out here before starting balances.
            assert await asyncio.to_thread(balance_started.wait, 3)
            cycle["quantity"] = 6_000_000  # A confirmed fill arrived during the reads.
            release_balance.set()
            return True

        client = SimpleNamespace(
            owner=owner,
            wallet=SimpleNamespace(token_balances=balances),
            _check_open_orders=open_orders,
        )
        try:
            assert await LiveExecution._check_buy_account(client, yes)
            owner.pause.assert_not_called()
        finally:
            release_balance.set()

    asyncio.run(run())


def test_reconciliation_wakes_during_round_and_coalesces_bursts():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        drained = asyncio.Event()
        rounds = 0
        in_sync = 0
        order = []
        owner = SimpleNamespace(
            store=SimpleNamespace(put=Mock()),
            validate=Mock(side_effect=lambda: order.append("validate") or {"ok": True}),
            pause=Mock(),
        )
        client = SimpleNamespace(
            owner=owner,
            ready=False,
            open_orders_clear=False,
            _reconcile_requested=asyncio.Event(),
        )

        async def sync():
            nonlocal rounds, in_sync
            rounds += 1
            in_sync += 1
            assert in_sync == 1
            order.append("sync")
            if rounds == 1:
                entered.set()
                await release.wait()
            in_sync -= 1

        async def positions(_command):
            order.append("positions")

        async def open_orders():
            order.append("open_orders")
            client.open_orders_clear = True

        def drain():
            order.append("drain")
            if rounds == 2:
                drained.set()

        client.sync_owned = sync
        client.generate_position_status_reports = positions
        client._check_open_orders = open_orders
        owner.drain = drain
        task = asyncio.create_task(LiveExecution.maintain(client))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(100):
                client._reconcile_requested.set()
            release.set()
            # The old 5s polling loop cannot satisfy this 1s bound.
            await asyncio.wait_for(drained.wait(), 1)
            await asyncio.sleep(0.1)
            assert rounds == 2
            assert order == ["sync", "positions", "validate", "open_orders", "drain"] * 2
            assert client.ready and client.open_orders_clear
            owner.pause.assert_not_called()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled()

    asyncio.run(run())


def test_reconciliation_keeps_periodic_fallback_without_messages(monkeypatch):
    async def run():
        owner = SimpleNamespace(
            store=SimpleNamespace(put=Mock()),
            validate=Mock(return_value={"ok": True}),
            drain=Mock(),
            pause=Mock(),
        )
        client = SimpleNamespace(
            owner=owner,
            ready=False,
            open_orders_clear=True,
            _reconcile_requested=asyncio.Event(),
            sync_owned=AsyncMock(),
            generate_position_status_reports=AsyncMock(),
            _check_open_orders=AsyncMock(),
        )
        waits = 0

        async def timer(wait, timeout):
            nonlocal waits
            wait.close()
            assert timeout == 5
            waits += 1
            if waits == 1:
                raise TimeoutError
            raise asyncio.CancelledError

        monkeypatch.setattr("pm_nautilus.live.asyncio.wait_for", timer)
        try:
            await LiveExecution.maintain(client)
        except asyncio.CancelledError:
            pass
        assert waits == 2 and client.sync_owned.await_count == 2
        assert owner.drain.call_count == 2

    asyncio.run(run())
