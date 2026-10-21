"""Current quotes through real LIVE submission gates; no keys or network traffic."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import msgspec
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from py_clob_client_v2.exceptions import PolyApiException

from pm_nautilus.config import SCALE, micros
from pm_nautilus.live import LiveExecution
from pm_nautilus.native import instrument_id
from pm_nautilus.order_terms import canonical_buy_taker
from pm_nautilus.rules import Fees, cost, signed_buy_notional
from test_live_recovery import controlled_runtime, flush, fund, token, venue_trade


def move_ask(runtime, ask, *, remaining_old_depth=0):
    asks = [(ask, 100_000_000)]
    if remaining_old_depth:
        asks.insert(0, (20_000, remaining_old_depth))
    runtime.book("1", [(min(20_000, ask - 1_000), 100_000_000)], asks)


@asynccontextmanager
async def profile_buy(path, *, fee_rate=0):
    async with controlled_runtime(path) as runtime:
        await fund(runtime)
        profile = json.loads(
            (Path(__file__).parents[1] / "config/strategy-profile.json").read_text()
        )
        runtime.update_preferences(profile)
        day = 86_400_000_000_000
        yes = replace(
            token(runtime),
            opened_ns=runtime.now - 10 * day,
            ends_ns=runtime.now + 90 * day,
            fees=Fees(bool(fee_rate), fee_rate),
        )
        no = replace(yes, token_id="2", direction="NO")
        runtime.add_tokens([yes, no])
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 100_000_000)])
        runtime.book("2", [(975_000, 100_000_000)], [(980_000, 100_000_000)])
        client = runtime.client
        client.ready = True
        client.open_orders_clear = True
        runtime.start()
        await flush()
        oid = next(iter(runtime.store.intents(active_only=True)))
        order = runtime.native.cache.order(ClientOrderId(oid))
        assert runtime.store.intent(oid)["execution_limit"] == 20_000
        assert runtime.preferences.max_price == 30_000
        assert runtime.preferences.budget == 1_000_000
        client._submit_order = LiveExecution._submit_order.__get__(client)
        client._maintain_active_market = AsyncMock()
        client._sdk_terms[yes.token_id] = (yes.tick, yes.neg_risk, yes.fees)
        client.wallet = SimpleNamespace(
            buy_preflight=Mock(),
            token_balances=Mock(return_value={"1": 0, "2": 0}),
        )
        client._http_client.get_open_orders = Mock(return_value=[])

        def sign(args, *, options):
            maker, limit = micros(args.amount), micros(args.price)
            return SimpleNamespace(
                makerAmount=maker,
                takerAmount=canonical_buy_taker(maker, limit, yes.tick),
                limit=limit,
            )

        client._http_client.create_market_order = Mock(side_effect=sign)
        client._expected_venue_order_id = Mock(
            side_effect=lambda signed, **kwargs: VenueOrderId(f"venue-{signed.limit}")
        )
        client._http_client.post_order = Mock(
            side_effect=lambda signed, *_args: {
                "success": True,
                "orderID": f"venue-{signed.limit}",
            }
        )
        state = SimpleNamespace(runtime=runtime, client=client, oid=oid, order=order, fees=yes.fees)
        try:
            yield state
        finally:
            assert not runtime._test_queue_errors, runtime._test_queue_errors


async def submit(state):
    await asyncio.wait_for(
        state.client._submit_order(
            SimpleNamespace(order=state.order, instrument_id=state.order.instrument_id)
        ),
        5,
    )
    await flush()


def change_after_account(state, change):
    original = state.client._check_buy_account
    rounds = []

    async def check(current_token):
        result = await original(current_token)
        rounds.append(result)
        change(len(rounds), result)
        return result

    state.client._check_buy_account = check
    return rounds


@pytest.mark.parametrize("fee_rate", [0, 100_000])
@pytest.mark.parametrize("change_stage", ["signing", "account"])
def test_refreshes_unposted_buy_with_current_price_and_same_budget(
    tmp_path, fee_rate, change_stage
):
    async def run():
        async with profile_buy(tmp_path / "refresh.sqlite", fee_rate=fee_rate) as state:
            runtime, client = state.runtime, state.client
            rounds = change_after_account(
                state,
                lambda count, _result: move_ask(runtime, 30_000)
                if count == 1 and change_stage == "account"
                else None,
            )
            if change_stage == "signing":
                original = client._prepare_buy_signature

                async def sign(current_token, args):
                    signed = await original(current_token, args)
                    if client._http_client.create_market_order.call_count == 1:
                        move_ask(runtime, 30_000)
                    return signed

                client._prepare_buy_signature = sign
            await submit(state)

            prices = [
                micros(call.args[0].price)
                for call in client._http_client.create_market_order.call_args_list
            ]
            assert prices == [20_000, 30_000]
            # Both preparations perform fresh wallet and account gates.
            assert client.wallet.buy_preflight.call_count == 2
            assert client.wallet.token_balances.call_count == 2
            assert client._http_client.get_open_orders.call_count == 2
            assert rounds == [True, True]
            client._http_client.post_order.assert_called_once()
            signed = client._http_client.post_order.call_args.args[0]
            assert signed.limit == 30_000
            assert signed.makerAmount == signed_buy_notional(
                1_000_000, state.fees, runtime.tokens["1"].tick, signed.limit
            )
            assert (
                signed.makerAmount + state.fees.fee(signed.takerAmount, signed.limit) <= 1_000_000
            )
            intent = runtime.store.intent(state.oid)
            assert intent["cash"] == 1_000_000
            assert intent["limit"] == 30_000
            assert intent["execution_limit"] == 30_000
            assert intent["outbound_terms"]["limit"] == signed.limit
            assert intent["outbound_terms"]["maker"] == signed.makerAmount
            assert intent["outbound_terms"]["taker"] == signed.takerAmount
            assert intent["venue_id"] == "venue-30000"
            assert not intent["terminal"]
            assert runtime.held_cash() == 1_000_000
            assert runtime.status == "RUNNING" and client.ready
            assert runtime.validate()["ok"]

    asyncio.run(run())


def test_keeps_old_signature_when_its_price_still_has_partial_depth(tmp_path):
    async def run():
        async with profile_buy(tmp_path / "partial.sqlite") as state:
            change_after_account(
                state,
                lambda _count, _result: move_ask(
                    state.runtime, 30_000, remaining_old_depth=10_000_000
                ),
            )
            await submit(state)
            state.client._http_client.create_market_order.assert_called_once()
            state.client._http_client.post_order.assert_called_once()
            assert state.client._http_client.post_order.call_args.args[0].limit == 20_000
            assert state.runtime.store.intent(state.oid)["execution_limit"] == 20_000

    asyncio.run(run())


def test_continuously_moving_quote_is_bounded_and_does_not_pause_account(tmp_path):
    async def run():
        async with profile_buy(tmp_path / "moving.sqlite") as state:
            rounds = change_after_account(
                state,
                lambda count, _result: move_ask(state.runtime, 25_000 if count == 1 else 30_000),
            )
            await submit(state)
            assert rounds == [True, True]
            assert state.client._http_client.create_market_order.call_count == 2
            state.client._http_client.post_order.assert_not_called()
            assert state.runtime.store.intent(state.oid)["terminal"]
            assert state.runtime.held_cash() == 0
            assert state.runtime.status == "RUNNING" and state.client.ready
            assert state.runtime.store.get("live_error") is None

    asyncio.run(run())


@pytest.mark.parametrize("change", ["over_maximum", "pause", "different_winner"])
def test_quote_refresh_does_not_bypass_current_strategy_or_control(tmp_path, change):
    async def run():
        async with profile_buy(tmp_path / f"{change}.sqlite") as state:

            def alter(_count, _result):
                if change == "over_maximum":
                    move_ask(state.runtime, 40_000)
                elif change == "pause":
                    state.runtime.pause()
                else:
                    move_ask(state.runtime, 30_000)
                    state.runtime.book("2", [(24_000, 100_000_000)], [(25_000, 100_000_000)])

            change_after_account(state, alter)
            await submit(state)
            state.client._http_client.post_order.assert_not_called()
            assert state.runtime.store.intent(state.oid)["terminal"]
            assert state.runtime.held_cash() == 0
            assert len(state.runtime.store.intents()) == 1
            assert state.runtime.status == ("PAUSED" if change == "pause" else "RUNNING")

    asyncio.run(run())


def test_refreshed_buy_still_requires_a_fresh_successful_account_check(tmp_path):
    async def run():
        async with profile_buy(tmp_path / "account-failure.sqlite") as state:
            state.client.wallet.token_balances.side_effect = [
                {"1": 0, "2": 0},
                ConnectionError("Controlled second account lookup failure"),
            ]
            rounds = change_after_account(
                state,
                lambda count, _result: move_ask(state.runtime, 30_000) if count == 1 else None,
            )
            await submit(state)
            assert rounds == [True, False]
            assert state.client._http_client.create_market_order.call_count == 2
            state.client._http_client.post_order.assert_not_called()
            assert state.runtime.store.intent(state.oid)["terminal"]
            assert state.runtime.held_cash() == 0
            assert state.runtime.status == "PAUSED"
            assert not state.client.ready

    asyncio.run(run())


@pytest.mark.parametrize("wait_stage", ["sdk_pool", "ledger_gate"])
def test_quote_change_after_identity_is_saved_never_posts_old_order(tmp_path, wait_stage):
    async def run():
        async with profile_buy(tmp_path / f"{wait_stage}.sqlite") as state:
            client, runtime = state.client, state.runtime
            acquired, release = asyncio.Event(), asyncio.Event()
            original = client._retry_manager_pool.inner.acquire

            async def acquire():
                acquired.set()
                await release.wait()
                return await original()

            client._retry_manager_pool.inner.acquire = acquire
            task = asyncio.create_task(submit(state))
            try:
                await asyncio.wait_for(acquired.wait(), 3)
                assert runtime.store.intent(state.oid)["venue_id"] == "venue-20000"
                if wait_stage == "ledger_gate":
                    async with client._ledger_gate.validation():
                        move_ask(runtime, 30_000)
                        release.set()
                        await flush()
                        client._http_client.post_order.assert_not_called()
                else:
                    move_ask(runtime, 30_000)
                    release.set()
                await task
                client._http_client.create_market_order.assert_called_once()
                client._http_client.post_order.assert_not_called()
                assert runtime.store.intent(state.oid)["terminal"]
                assert runtime.held_cash() == 0
                assert runtime.status == "RUNNING" and client.ready
                assert runtime.store.get("live_error") is None
                kinds = [type(event).__name__ for _, event in runtime.store.events()]
                assert kinds.count("OrderSubmitted") == 1
                assert kinds.count("OrderRejected") == 1
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_refreshed_post_timeout_preserves_new_identity_and_cash_across_restart(tmp_path):
    async def run():
        path = tmp_path / "unknown.sqlite"
        async with profile_buy(path) as state:
            change_after_account(
                state,
                lambda count, _result: move_ask(state.runtime, 30_000) if count == 1 else None,
            )
            state.client._http_client.post_order.side_effect = PolyApiException(
                error_msg="Controlled network timeout after POST"
            )
            await submit(state)
            state.client._http_client.post_order.assert_called_once()
            assert state.client._http_client.create_market_order.call_count == 2
            move_ask(state.runtime, 40_000)
            state.runtime.refresh_eligibility()
            state.runtime.drain()
            await flush()
            intent = state.runtime.store.intent(state.oid)
            assert not intent["terminal"]
            assert intent["venue_id"] == "venue-30000"
            assert intent["outbound_terms"]["limit"] == 30_000
            assert state.runtime.held_cash() == 1_000_000
            assert state.runtime.status == "PAUSED" and not state.client.ready
            assert len(state.runtime.store.intents()) == 1
            oid, durable_intent = state.oid, intent
        async with controlled_runtime(path) as recovered:
            assert recovered.store.intent(oid) == durable_intent
            assert recovered.held_cash() == 1_000_000
            assert not recovered.client.ready
            assert recovered.validate()["ok"]
            assert not recovered._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize("fee_rate", [0, 100_000])
def test_refreshed_buy_confirmed_fill_and_targets_recover_at_new_price(tmp_path, fee_rate):
    async def run():
        path = tmp_path / "confirmed.sqlite"
        async with profile_buy(path, fee_rate=fee_rate) as state:
            runtime, client = state.runtime, state.client
            change_after_account(
                state,
                lambda count, _result: move_ask(runtime, 30_000) if count == 1 else None,
            )
            await submit(state)
            signed = client._http_client.post_order.call_args.args[0]
            assert signed.limit == 30_000
            assert client._http_client.create_market_order.call_count == 2
            client._http_client.post_order.assert_called_once()
            venue = VenueOrderId("venue-30000")
            client.generate_order_accepted(
                state.order.strategy_id,
                state.order.instrument_id,
                state.order.client_order_id,
                venue,
                runtime.now,
            )
            await flush()
            source = venue_trade(runtime.tokens["1"])
            payload = msgspec.to_builtins(source)
            payload.update(
                id="refreshed-buy-confirmed",
                taker_order_id=str(venue),
                price=str(Decimal(signed.limit) / SCALE),
                size=str(Decimal(signed.takerAmount) / SCALE),
            )
            trade = msgspec.json.decode(msgspec.json.encode(payload), type=type(source))
            client.ingest_trade(trade)
            client.ingest_trade(trade)  # Duplicate confirmation cannot double the position.
            await flush()

            expected_quantity = signed.takerAmount
            expected_spent = cost(signed.limit, expected_quantity) + state.fees.fee(
                expected_quantity, signed.limit
            )
            assert expected_spent <= 1_000_000
            cycle = runtime.business["cycles"]["e1"]
            assert cycle["quantity"] == expected_quantity
            assert cycle["spent"] == expected_spent
            assert cycle["cost"] == expected_spent
            assert cycle["budget"] == 1_000_000
            targets = list(runtime.business["targets"].values())
            assert len(targets) == 1
            assert targets[0]["qty"] == expected_quantity
            assert targets[0]["price"] == 45_000  # New 3c fill * configured 1.5 target.
            positions = runtime.native.cache.positions_open(
                instrument_id=instrument_id(runtime.tokens["1"])
            )
            assert (
                sum(micros(position.quantity.as_decimal()) for position in positions)
                == expected_quantity
            )
            fills = [
                event
                for _, event in runtime.store.events()
                if type(event).__name__ == "OrderFilled"
            ]
            assert len(fills) == 1 and micros(fills[0].last_px.as_decimal()) == 30_000
            assert runtime.validate()["ok"]
            oid = state.oid

        async with controlled_runtime(path) as recovered:
            assert recovered.store.intent(oid)["venue_id"] == "venue-30000"
            assert recovered.business["cycles"]["e1"]["quantity"] == expected_quantity
            assert recovered.business["cycles"]["e1"]["spent"] == expected_spent
            targets = list(recovered.business["targets"].values())
            assert len(targets) == 1
            assert targets[0]["qty"] == expected_quantity
            assert targets[0]["price"] == 45_000
            positions = recovered.native.cache.positions_open(
                instrument_id=instrument_id(recovered.tokens["1"])
            )
            assert (
                sum(micros(position.quantity.as_decimal()) for position in positions)
                == expected_quantity
            )
            assert recovered.validate()["ok"]
            assert not recovered._test_queue_errors

    asyncio.run(run())
