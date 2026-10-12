"""Outbound terms and concurrent reads with controlled responses only."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import msgspec
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.order_builder.builder import OrderBuilder, ROUNDING_CONFIG

from pm_nautilus.live import LiveExecution, _LedgerGate, _TrackedRetryManager, _PostStateChanged
from pm_nautilus.order_terms import OrderTermsError, normalize_sell, check_signed_sell
from pm_nautilus.native import instrument_id
from pm_nautilus.rules import Fees
from pm_nautilus.strategy import Runtime
from test_live_recovery import accept, controlled_runtime, flush, fund, token, venue_trade
from test_live_submit_cleanup import prepared_order


def unsigned_sdk(t):
    """The real SDK client stops before cryptographic signing or any outbound request."""
    http = ClobClient("https://offline.invalid", 137)
    http.assert_level_1_auth = lambda: None
    http._ClobClient__tick_sizes[t.token_id] = "0.01"
    http._ClobClient__cached_version = 2
    info = {"mts": str(t.tick / 1_000_000), "nr": t.neg_risk, "t": [{"t": t.token_id}]}
    http._get = Mock(return_value=info)  # Only the public market metadata response.
    http._post = http._delete = Mock(side_effect=AssertionError("Network is forbidden"))
    builder = OrderBuilder(None)

    def build(args, options, **_kwargs):
        _, maker, taker = builder.get_order_amounts(
            args.side, args.size, args.price, ROUNDING_CONFIG[options.tick_size]
        )
        return SimpleNamespace(makerAmount=maker, takerAmount=taker)

    http.builder = SimpleNamespace(build_order=build)
    return http


async def prepared_sell(runtime, *, floor=963_000, quantity=5_009_000):
    buy_oid, _ = await prepared_order(runtime)
    client = runtime.client
    client._submit_order = AsyncMock()
    t = runtime.tokens["1"]
    await accept(runtime, buy_oid)
    trade = msgspec.structs.replace(venue_trade(t), size=str(Decimal(quantity) / 1_000_000))
    client.ingest_trade(trade)
    await flush()
    sell_oid = runtime.submit(t, "SELL", quantity, floor, targets=list(runtime.business["targets"]))
    await flush()
    return sell_oid, runtime.native.cache.order(ClientOrderId(sell_oid))


@pytest.mark.parametrize(
    "quantity,min_size", [(5_020_000, 5_020_000), (5_029_000, 5_020_000), (8_030_000, 5_000_000)]
)
def test_exact_sell_size_reaches_post_and_matches_test_owned_tail(tmp_path, quantity, min_size):
    async def run():
        async with controlled_runtime(tmp_path / "live-sell-size.sqlite") as runtime:
            oid, order = await prepared_sell(runtime, floor=963_001, quantity=quantity)
            client = runtime.client
            t = replace(runtime.tokens["1"], min_size=min_size)
            runtime.tokens[t.token_id] = t
            client._http_client = unsigned_sdk(t)
            client._sdk_terms.clear()
            client._post_signed_order = AsyncMock()
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-sell"))
            await LiveExecution._submit_order(
                client, SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()
            client._post_signed_order.assert_awaited_once()
            signed = client._post_signed_order.await_args.args[1]
            terms = normalize_sell(quantity, 963_001, t.tick, t.min_size)
            maker, _taker = check_signed_sell(signed, terms)
            assert maker == quantity // 10_000 * 10_000
            assert runtime.store.intent(oid)["outbound_terms"]["maker"] == maker
            assert not runtime._test_queue_errors
            client._http_client._post.assert_not_called()

            # Apply the controlled venue's actual signed fill, including its
            # durable identity, through the normal confirmed-trade projection.
            venue = VenueOrderId("venue-sell")
            runtime.native.cache.add_venue_order_id(order.client_order_id, venue)
            intent = runtime.store.intent(oid)
            intent["venue_id"] = str(venue)
            runtime.store.save_intent(oid, intent)
            client.generate_order_accepted(
                order.strategy_id, order.instrument_id, order.client_order_id, venue, runtime.now
            )
            await flush()
            trade = msgspec.to_builtins(venue_trade(t))
            trade.update(
                id="signed-sell-trade",
                taker_order_id=str(venue),
                side="SELL",
                size=str(Decimal(maker) / 1_000_000),
                price=str(Decimal(terms.limit) / 1_000_000),
            )
            client.ingest_trade(
                msgspec.json.decode(msgspec.json.encode(trade), type=type(venue_trade(t)))
            )
            await flush()
            live_cycle = runtime.business["cycles"].get(t.event_id)
            assert (live_cycle["quantity"] if live_cycle else 0) == quantity - maker
            positions = runtime.native.cache.positions_open(instrument_id=instrument_id(t))
            assert sum(p.quantity.as_decimal() for p in positions) * 1_000_000 == quantity - maker
            assert runtime.validate()["ok"]

            simulated = Runtime(tmp_path / "test-sell-size.sqlite")
            try:
                simulated.add_tokens([t])
                simulated.book(t.token_id, [(90_000, 100_000_000)], [(100_000, quantity)])
                simulated.submit(t, "BUY", quantity, 100_000, cash=900_000)
                simulated.books[t.token_id].snapshot(
                    [(964_000, 100_000_000)], [(965_000, 100_000_000)], simulated.now
                )
                test_oid = simulated.submit(
                    t, "SELL", quantity, 963_001, targets=list(simulated.business["targets"])
                )
                test_order = simulated.native.cache.order(ClientOrderId(test_oid))
                assert test_order.quantity.as_decimal() * 1_000_000 == maker
                cycle = simulated.business["cycles"].get(t.event_id)
                assert (cycle["quantity"] if cycle else 0) == quantity - maker
                assert simulated.validate()["ok"]
            finally:
                simulated.close()

    asyncio.run(run())


def test_dynamic_tick_refreshes_real_sdk_and_sell_keeps_floor_and_size(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "tick.sqlite") as runtime:
            oid, order = await prepared_sell(runtime)
            client = runtime.client
            t = runtime.tokens["1"]
            client._http_client = unsigned_sdk(t)
            client._sdk_terms = {t.token_id: (10_000, False)}
            client._post_signed_order = AsyncMock()
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-sell"))
            await client._submit_limit_order(SimpleNamespace(order=order), None)
            signed = client._post_signed_order.await_args.args[1]
            assert signed.makerAmount == 5_000_000
            assert signed.takerAmount == 4_815_000  # 0.963, never stale SDK's 0.96.
            assert client._http_client._get.call_count == 1
            assert runtime.store.intent(oid)["outbound_terms"]["tick"] == 1000
            client._check_post_ready(oid)
            # Cached SDK terms are reused only while the current runtime tick agrees.
            await client._ensure_signing_metadata(t)
            assert client._http_client._get.call_count == 1
            runtime.tokens[t.token_id] = replace(t, tick=10_000)
            with pytest.raises(ValueError, match="签名档位"):
                client._check_post_ready(oid)
            client._http_client._post.assert_not_called()
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_metadata_disagreement_or_lookup_failure_never_posts(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "metadata.sqlite") as runtime:
            _, order = await prepared_sell(runtime)
            client = runtime.client
            client._sdk_terms.clear()
            client._http_client.get_clob_market_info = Mock(
                return_value={
                    "mts": "0.01",
                    "nr": False,
                    "t": [{"t": "1"}],
                }
            )
            client._http_client.create_order = Mock()
            client._post_signed_order = AsyncMock()
            with pytest.raises(OrderTermsError):
                await client._submit_limit_order(SimpleNamespace(order=order), None)
            client._http_client.get_clob_market_info.side_effect = ConnectionError("offline")
            with pytest.raises(ConnectionError):
                await client._submit_limit_order(SimpleNamespace(order=order), None)
            client._http_client.create_order.assert_not_called()
            client._post_signed_order.assert_not_awaited()

    asyncio.run(run())


def test_fee_change_refreshes_sdk_and_missing_paid_fee_fails_closed(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "fee-metadata.sqlite") as runtime:
            await fund(runtime)
            t = token(runtime)
            runtime.add_tokens([t])
            client = runtime.client
            client._http_client = unsigned_sdk(t)
            await client._ensure_signing_metadata(t)  # No fd is valid for a free market.
            paid = replace(t, fees=Fees(True, 40_000, 1))
            runtime.tokens[t.token_id] = paid
            with pytest.raises(OrderTermsError, match="费用"):
                await client._ensure_signing_metadata(paid)
            assert t.token_id not in client._sdk_terms
            client._http_client._get.return_value["fd"] = {"r": 0.04, "e": 1.0}
            await client._ensure_signing_metadata(paid)
            assert client._sdk_terms[t.token_id][2] == paid.fees
            assert client._http_client._ClobClient__fee_infos[t.token_id].rate == 0.04
            assert client._http_client._get.call_count == 3
            # Identical current fee terms can reuse the newly verified SDK metadata.
            await client._ensure_signing_metadata(paid)
            assert client._http_client._get.call_count == 3

    asyncio.run(run())


@pytest.mark.parametrize("change", ["lower_floor", "excess_quantity", "tick_during_sign"])
def test_sell_invalid_signed_facts_or_changed_tick_never_cross_post(tmp_path, change):
    async def run():
        async with controlled_runtime(tmp_path / f"sell-{change}.sqlite") as runtime:
            oid, order = await prepared_sell(runtime)
            client = runtime.client
            t = runtime.tokens["1"]
            client._sdk_terms[t.token_id] = (t.tick, t.neg_risk, t.fees)

            async def signature(_token, _terms):
                if change == "tick_during_sign":
                    runtime.tokens[t.token_id] = replace(t, tick=10_000)
                return SimpleNamespace(
                    makerAmount=5_009_000 if change == "excess_quantity" else 5_000_000,
                    takerAmount=4_800_000 if change == "lower_floor" else 4_815_000,
                )

            client._prepare_sell_signature = signature
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-sell"))
            client._maintain_active_market = AsyncMock()
            client._http_client.post_order = Mock()
            await LiveExecution._submit_order(
                client, SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()
            client._http_client.post_order.assert_not_called()
            assert runtime.store.intent(oid)["terminal"]
            assert runtime.business["cycles"][t.event_id]["quantity"] == 5_009_000
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_wallet_and_sign_preparations_overlap_but_wallet_failure_cannot_post(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "overlap.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            wallet_started, sign_started = Event(), Event()

            def wallet_check():
                wallet_started.set()
                assert sign_started.wait(2), "Signing must overlap the wallet check"
                raise ValueError("controlled wallet failure")

            def sign(*_args, **_kwargs):
                sign_started.set()
                assert wallet_started.wait(2)
                return SimpleNamespace(makerAmount=1_000_000, takerAmount=10_000_000)

            client.wallet.buy_preflight = wallet_check
            client._http_client.create_market_order = Mock(side_effect=sign)
            client._http_client.post_order = Mock()
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()
            assert sign_started.is_set() and wallet_started.is_set()
            assert runtime.status == "PAUSED" and runtime.store.intent(oid)["terminal"]
            assert runtime.held_cash() == 0
            client._http_client.post_order.assert_not_called()

    asyncio.run(run())


def test_repeated_cancellation_waits_for_sign_worker_and_never_posts(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "cancel-sign.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            entered, release, finished = Event(), Event(), Event()

            def sign(*_args, **_kwargs):
                entered.set()
                try:
                    assert release.wait(3)
                    return SimpleNamespace(makerAmount=1_000_000, takerAmount=10_000_000)
                finally:
                    finished.set()

            client._http_client.create_market_order = Mock(side_effect=sign)
            client._http_client.post_order = Mock()
            task = asyncio.create_task(
                client._submit_order(
                    SimpleNamespace(order=order, instrument_id=order.instrument_id)
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                task.cancel()
                await flush()
                task.cancel()
                await flush()
                assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                await flush()
                assert finished.is_set() and runtime.store.intent(oid)["terminal"]
                assert runtime.held_cash() == 0
                client._http_client.post_order.assert_not_called()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_each_retry_checks_current_state_and_preserves_unknown_first_post():
    async def run():
        ready = True
        calls = 0

        def check(_oid):
            if not ready:
                raise ValueError("controlled current-state change")

        async def operation():
            nonlocal ready, calls
            calls += 1
            ready = False
            raise ConnectionError("first POST outcome unknown")

        async def retry(_name, _details, fn, *args, **kwargs):
            try:
                await fn(*args, **kwargs)
            except ConnectionError:
                return await fn(*args, **kwargs)

        client = SimpleNamespace(
            _ledger_gate=_LedgerGate(), _check_post_ready=check, _post_possible=set()
        )
        with pytest.raises(_PostStateChanged):
            await _TrackedRetryManager(SimpleNamespace(run=retry), client).run(
                "submit_order", ["owned"], operation
            )
        assert calls == 1 and client._post_possible == {"owned"}
        assert LiveExecution._is_unknown_submit_result(client, _PostStateChanged())

    asyncio.run(run())


def test_retry_stopped_by_changed_tick_keeps_real_native_unknown_reservation(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "retry-tick.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            t = runtime.tokens["1"]
            client._http_client.create_market_order = Mock(
                return_value=SimpleNamespace(
                    makerAmount=1_000_000,
                    takerAmount=10_000_000,
                )
            )
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-unknown"))
            client._http_client.post_order = Mock(side_effect=ConnectionError("unknown first POST"))
            manager = SimpleNamespace(last_exception=None, message="controlled retry")

            async def retry(_name, _details, operation, *args, **kwargs):
                try:
                    await operation(*args, **kwargs)
                except ConnectionError:
                    runtime.tokens[t.token_id] = replace(t, tick=10_000)
                try:
                    return await operation(*args, **kwargs)
                except _PostStateChanged as exc:
                    manager.last_exception = exc
                    return None

            manager.run = retry
            client._retry_manager_pool.inner.acquire = AsyncMock(return_value=manager)
            client._retry_manager_pool.inner.release = AsyncMock()
            await client._submit_order(
                SimpleNamespace(order=order, instrument_id=order.instrument_id)
            )
            await flush()
            assert client._http_client.post_order.call_count == 1
            assert not runtime.store.intent(oid)["terminal"]
            assert runtime.store.intent(oid)["venue_id"] == "venue-unknown"
            assert runtime.held_cash() == 1_000_000
            assert runtime.status == "PAUSED" and not client.ready
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_owned_order_reads_overlap_bound_concurrency_and_keep_unknown_rights(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "batch-orders.sqlite") as runtime:
            await fund(runtime)
            client = runtime.client
            lock, release, four_started = Lock(), Event(), Event()
            active = peak = total = 0
            for index in range(1, 7):
                t = token(runtime, index)
                runtime.add_tokens([t])
                oid = runtime.submit(t, "BUY", 10_000_000, 100_000, cash=100_000)
                await flush()
                i = runtime.store.intent(oid)
                i["venue_id"] = f"venue-{index}"
                runtime.store.save_intent(oid, i)

            def lookup(venue):
                nonlocal active, peak, total
                with lock:
                    active += 1
                    total += 1
                    peak = max(peak, active)
                    if total == 4:
                        four_started.set()
                try:
                    assert release.wait(3)
                    return None  # No response is never proof of zero fills.
                finally:
                    with lock:
                        active -= 1

            client._http_client.get_trades = Mock(return_value=[])
            client._http_client.get_order = Mock(side_effect=lookup)
            client._update_account_state = AsyncMock()
            task = asyncio.create_task(client.sync_owned())
            try:
                assert await asyncio.to_thread(four_started.wait, 2)
                await flush()
                assert total == 4 and peak == 4
                release.set()
                with pytest.raises(ValueError, match="结果不明"):
                    await asyncio.wait_for(task, 2)
                assert total == 6 and peak == 4
                assert len(runtime.store.intents(active_only=True)) == 6
                assert runtime.held_cash() == 600_000
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_rest_terminal_includes_ws_confirmation_received_during_query(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "fresh-fills.sqlite") as runtime:
            await fund(runtime)
            t = token(runtime)
            runtime.add_tokens([t])
            oid = runtime.submit(t, "BUY", 10_000_000, 100_000, cash=1_000_000)
            await flush()
            await accept(runtime, oid)
            entered, release = Event(), Event()

            def lookup(_venue):
                entered.set()
                assert release.wait(3)
                return {
                    "id": _venue,
                    "asset_id": "1",
                    "associate_trades": ["confirmed-trade-1"],
                    "size_matched": "10",
                    "status": "MATCHED",
                }

            client = runtime.client
            client._http_client.get_trades = Mock(return_value=[])
            client._http_client.get_order = Mock(side_effect=lookup)
            client._update_account_state = AsyncMock()
            task = asyncio.create_task(client.sync_owned())
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                client.ingest_trade(venue_trade(t, websocket=True))
                await flush()
                release.set()
                await asyncio.wait_for(task, 2)
                await flush()
                assert runtime.store.intent(oid)["terminal"]
                assert runtime.business["cycles"][t.event_id]["quantity"] == 10_000_000
                assert len(runtime.store.order_fills(oid)) == 1
                assert not runtime._test_queue_errors
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
