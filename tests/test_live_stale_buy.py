"""Fresh local books at signing/account/pool gates; no network or real signatures."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId

from pm_nautilus.config import micros
from pm_nautilus.live import LiveExecution
from pm_nautilus.order_terms import canonical_buy_taker

from test_live_recovery import controlled_runtime, flush, fund, token


async def two_cent_order(runtime, max_cents="3"):
    await fund(runtime)
    runtime.preferences.maxBuyPriceCents = max_cents
    t = token(runtime)
    runtime.add_tokens([t])
    runtime.book(t.token_id, [(19_000, 100_000_000)], [(20_000, 100_000_000)])
    client = runtime.client
    client.ready = client.open_orders_clear = True
    runtime.start()
    await flush()
    oid = next(iter(runtime.store.intents()))
    order = runtime.native.cache.order(ClientOrderId(oid))
    assert runtime.store.intent(oid)["execution_limit"] == 20_000
    original_submit = LiveExecution._submit_order.__get__(client)
    client._followup_submit = AsyncMock()

    async def submit(command):
        if str(command.order.client_order_id) == oid:
            await original_submit(command)
        else:
            await client._followup_submit(command)

    client._submit_order = submit
    client._maintain_active_market = AsyncMock()
    client._check_buy_account = AsyncMock(return_value=True)
    client.wallet = SimpleNamespace(buy_preflight=Mock())
    client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-stale-check"))
    client._http_client.post_order = Mock()
    retry = SimpleNamespace(
        run=AsyncMock(return_value={"success": False, "errorMsg": "controlled response"})
    )
    client._retry_manager_pool.inner.acquire = AsyncMock(return_value=retry)
    client._retry_manager_pool.inner.release = AsyncMock()

    async def signature(_token, args):
        maker = micros(args.amount)
        return SimpleNamespace(
            makerAmount=maker,
            takerAmount=canonical_buy_taker(maker, micros(args.price), t.tick),
        )

    client._prepare_buy_signature = AsyncMock(side_effect=signature)
    return oid, order, retry


def change_book(runtime, asks):
    best = min((p for p, q in asks if q > 0), default=20_000)
    runtime.book("1", [(best - 1_000, 100_000_000)], asks)


@pytest.mark.parametrize("stage", ["signing", "account", "pool", "ledger_gate"])
@pytest.mark.parametrize("max_cents,expected_next", [("3", 30_000), ("2.5", None)])
def test_old_two_cent_buy_is_unsent_and_full_strategy_reconsiders_promptly(
    tmp_path, stage, max_cents, expected_next
):
    async def run():
        async with controlled_runtime(tmp_path / "stale.sqlite") as runtime:
            oid, order, retry = await two_cent_order(runtime, max_cents)
            client = runtime.client

            def change():
                change_book(runtime, [(30_000, 100_000_000)])

            entered, release_pool, release_gate = (asyncio.Event() for _ in range(3))
            if stage == "signing":
                original = client._prepare_buy_signature.side_effect

                async def signing(*args):
                    signed = await original(*args)
                    change()
                    return signed

                client._prepare_buy_signature.side_effect = signing
            elif stage == "account":

                async def account(_token):
                    change()
                    return True

                client._check_buy_account.side_effect = account
            elif stage == "pool":

                async def acquire():
                    change()
                    return retry

                client._retry_manager_pool.inner.acquire.side_effect = acquire
            else:

                async def acquire():
                    entered.set()
                    await release_pool.wait()
                    return retry

                client._retry_manager_pool.inner.acquire.side_effect = acquire

            submit = asyncio.create_task(
                client._submit_order(
                    SimpleNamespace(order=order, instrument_id=order.instrument_id)
                )
            )
            gate = None
            if stage == "ledger_gate":
                await asyncio.wait_for(entered.wait(), 2)

                async def hold_gate():
                    async with client._ledger_gate.validation():
                        entered.clear()
                        await release_gate.wait()

                gate = asyncio.create_task(hold_gate())
                await flush()
                assert client._ledger_gate.checking
                release_pool.set()
                await flush()
                retry.run.assert_not_awaited()
                change()
                release_gate.set()
            await asyncio.wait_for(asyncio.gather(submit, *(x for x in (gate,) if x)), 2)
            await flush()
            await asyncio.gather(*list(client._stale_buy_refresh_tasks.values()))
            await flush()

            retry.run.assert_not_awaited()
            client._http_client.post_order.assert_not_called()
            assert runtime.store.intent(oid)["terminal"]
            assert runtime.status == "RUNNING"
            assert client.ready and client.open_orders_clear
            client._prepare_buy_signature.assert_awaited_once()

            # One coalesced reevaluation uses the complete normal strategy;
            # it does not wait for another quote or the 5s control tick.
            active = runtime.store.intents(active_only=True)
            if expected_next is None:
                assert not active and runtime.held_cash() == 0
                assert len(runtime.store.intents()) == 1
                client._followup_submit.assert_not_awaited()
            else:
                assert len(active) == 1 and oid not in active
                intent = next(iter(active.values()))
                assert intent["execution_limit"] == expected_next
                assert intent["limit"] == runtime.preferences.max_price
                assert intent["cash"] == 1_000_000
                assert runtime.held_cash() == 1_000_000
                assert len(runtime.store.intents()) == 2
                client._followup_submit.assert_awaited_once()
            assert not runtime._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize("asks", [[(10_000, 100_000_000)], [(20_000, 1_000_000)]])
def test_better_quote_or_useful_partial_depth_keeps_original_signed_fak(tmp_path, asks):
    async def run():
        async with controlled_runtime(tmp_path / "useful.sqlite") as runtime:
            oid, order, retry = await two_cent_order(runtime)
            client = runtime.client

            async def account(_token):
                change_book(runtime, asks)
                return True

            client._check_buy_account.side_effect = account
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()
            retry.run.assert_awaited_once()
            client._prepare_buy_signature.assert_awaited_once()
            assert runtime.store.intent(oid)["outbound_terms"]["limit"] == 20_000
            assert not runtime._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize("change", ["tick", "neg_risk", "pause", "max_price"])
def test_final_pool_wait_never_bypasses_metadata_or_control_change(tmp_path, change):
    async def run():
        async with controlled_runtime(tmp_path / "control.sqlite") as runtime:
            oid, order, retry = await two_cent_order(runtime)
            client = runtime.client

            async def acquire():
                if change == "tick":
                    runtime.tokens["1"] = replace(runtime.tokens["1"], tick=10_000)
                elif change == "neg_risk":
                    runtime.tokens["1"] = replace(runtime.tokens["1"], neg_risk=True)
                elif change == "pause":
                    runtime.store.put("status", "PAUSED")
                else:
                    runtime.preferences.maxBuyPriceCents = "2.5"
                return retry

            client._retry_manager_pool.inner.acquire.side_effect = acquire
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()
            retry.run.assert_not_awaited()
            client._http_client.post_order.assert_not_called()
            assert runtime.store.intent(oid)["terminal"]
            assert runtime.held_cash() == 0
            assert runtime.status == "PAUSED"
            client._prepare_buy_signature.assert_awaited_once()
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_unknown_post_then_stale_quote_keeps_original_identity_and_reservation(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "unknown.sqlite") as runtime:
            oid, order, retry = await two_cent_order(runtime)
            client = runtime.client

            def post(*_args):
                raise ConnectionError("controlled unknown first POST")

            client._http_client.post_order.side_effect = post

            async def attempts(_name, _details, operation, *args, **kwargs):
                try:
                    await operation(*args, **kwargs)
                except ConnectionError:
                    change_book(runtime, [(30_000, 100_000_000)])
                    return await operation(*args, **kwargs)

            retry.run.side_effect = attempts
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()
            intent = runtime.store.intent(oid)
            assert not intent["terminal"]
            assert intent["venue_id"] == "venue-stale-check"
            assert intent["execution_limit"] == 20_000
            assert runtime.held_cash() == 1_000_000
            assert runtime.status == "PAUSED"
            assert not client.ready and not client.open_orders_clear
            client._http_client.post_order.assert_called_once()
            client._prepare_buy_signature.assert_awaited_once()
            assert len(runtime.store.intents()) == 1
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_continuously_moving_quote_allows_only_one_prompt_reprepare(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "bounded.sqlite") as runtime:
            _oid, order, retry = await two_cent_order(runtime, "5")
            client = runtime.client
            original_signature = client._prepare_buy_signature.side_effect
            original_submit = LiveExecution._submit_order.__get__(client)
            second_finished = asyncio.Event()
            completed = 0

            async def signing(t, args):
                signed = await original_signature(t, args)
                change_book(runtime, [(micros(args.price) + 10_000, 100_000_000)])
                return signed

            async def submit(command):
                nonlocal completed
                await original_submit(command)
                completed += 1
                if completed == 2:
                    second_finished.set()

            client._prepare_buy_signature.side_effect = signing
            client._submit_order = submit
            await submit(SimpleNamespace(order=order, instrument_id=order.instrument_id))
            await asyncio.wait_for(second_finished.wait(), 2)
            await flush()
            retry.run.assert_not_awaited()
            client._http_client.post_order.assert_not_called()
            assert client._prepare_buy_signature.await_count == 2
            assert len(runtime.store.intents()) == 2
            assert all(i["terminal"] for i in runtime.store.intents().values())
            assert runtime.held_cash() == 0 and runtime.status == "RUNNING"
            assert not client._stale_buy_refresh_tasks
            assert len(client._stale_buy_refresh_at) == 1
            assert not runtime._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize("guard", ["pause", "epoch", "preferences", "readiness", "active"])
def test_prompt_reevaluation_obeys_control_changes_after_stale_rejection(tmp_path, guard):
    async def run():
        async with controlled_runtime(tmp_path / "refresh-control.sqlite") as runtime:
            _oid, order, retry = await two_cent_order(runtime)
            client = runtime.client
            queue = client._queue_stale_buy_refresh

            def queue_then_change(*args):
                queue(*args)
                if guard == "pause":
                    runtime.store.put("status", "PAUSED")
                elif guard == "epoch":
                    runtime._start_epoch += 1
                elif guard == "preferences":
                    runtime.preferences.maxBuyPriceCents = "4"
                elif guard == "readiness":
                    client.ready = False
                else:
                    runtime.active_intents = Mock(return_value={"another-owned-order": {}})

            async def account(_token):
                change_book(runtime, [(30_000, 100_000_000)])
                return True

            client._queue_stale_buy_refresh = queue_then_change
            client._check_buy_account.side_effect = account
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await asyncio.gather(*list(client._stale_buy_refresh_tasks.values()))
            await flush()
            retry.run.assert_not_awaited()
            client._http_client.post_order.assert_not_called()
            client._followup_submit.assert_not_awaited()
            assert len(runtime.store.intents()) == 1
            assert not runtime._test_queue_errors

    asyncio.run(run())
