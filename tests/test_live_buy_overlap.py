"""LIVE buy preparation: wallet reads and signing overlap; the account check runs after.

Injected delays show the wallet/approval check and signing run concurrently,
while the full account check (open orders + same-Condition balances) starts
only after both finished, so no part of it predates the signed order. Any
failed check blocks the POST boundary. Nothing here contacts a network.
"""

import asyncio
import time
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nautilus_trader.model.identifiers import VenueOrderId

from test_live_recovery import controlled_runtime, flush
from test_live_submit_cleanup import prepared_order

DELAY = 0.3


def staged(client, *, wallet=None, account=True, maker=1_000_000):
    """Wallet and sign block until both started; record when each stage ran."""
    started = {"wallet": Event(), "sign": Event()}
    events = []

    def enter(stage):
        started[stage].set()
        for other in started:
            assert started[other].wait(3), f"{stage} must overlap {other}"
        time.sleep(DELAY)
        events.append((stage, "end", time.perf_counter()))

    def wallet_check():
        enter("wallet")
        if wallet is not None:
            raise wallet

    def sign(*_args, **_kwargs):
        enter("sign")
        return SimpleNamespace(makerAmount=maker, takerAmount=maker * 10)

    async def account_check(_token):
        events.append(("account", "start", time.perf_counter()))
        if isinstance(account, BaseException):
            raise account
        return account

    client.wallet = SimpleNamespace(buy_preflight=Mock(side_effect=wallet_check))
    client._http_client.create_market_order = Mock(side_effect=sign)
    client._check_buy_account = AsyncMock(side_effect=account_check)
    client._http_client.post_order = Mock()
    client._post_signed_order = AsyncMock()
    client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-overlap"))
    return events


async def submit(client, order):
    started = time.perf_counter()
    await client._submit_order(SimpleNamespace(order=order, instrument_id=order.instrument_id))
    await flush()
    return time.perf_counter() - started


def test_wallet_and_sign_overlap_and_account_check_starts_after_both(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "overlap-pass.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            events = staged(client)
            elapsed = await submit(client, order)

            client.wallet.buy_preflight.assert_called_once()
            client._http_client.create_market_order.assert_called_once()
            client._check_buy_account.assert_awaited_once()
            client._post_signed_order.assert_awaited_once()
            ends = [at for stage, kind, at in events if kind == "end"]
            (account_start,) = [at for stage, kind, at in events if stage == "account"]
            # The whole account check is newer than both the wallet check and the signature.
            assert account_start >= max(ends)
            assert elapsed < 2 * DELAY  # Wallet and sign overlapped (serial: 2 * DELAY).
            assert runtime.store.intent(oid)["outbound_terms"]["side"] == "BUY"
            assert not runtime._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize(
    "wallet,account,paused,account_ran",
    [
        (ValueError("approval missing"), True, True, False),
        (None, False, False, True),
        (None, ConnectionError("open orders unavailable"), True, True),
    ],
)
def test_any_failed_check_blocks_post(tmp_path, wallet, account, paused, account_ran):
    async def run():
        async with controlled_runtime(tmp_path / "overlap-fail.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            staged(client, wallet=wallet, account=account)
            await submit(client, order)

            assert client._check_buy_account.await_count == (1 if account_ran else 0)
            client._post_signed_order.assert_not_awaited()
            client._http_client.post_order.assert_not_called()
            assert runtime.store.intent(oid)["terminal"] is True
            assert runtime.held_cash() == 0
            if paused:
                assert runtime.status == "PAUSED"
            if isinstance(account, BaseException):
                assert client.ready is False and client.open_orders_clear is False
            kinds = [type(event).__name__ for _, event in runtime.store.events()]
            assert "OrderSubmitted" not in kinds
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_control_change_during_account_check_is_seen_before_post(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "overlap-control.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            staged(client)
            seen = []

            def valid(*_args):
                seen.append(1)
                return len(seen) < 3  # Submit and post-sign valid; invalid after account check.

            client.buy_valid = Mock(side_effect=valid)
            await submit(client, order)

            client._check_buy_account.assert_awaited_once()
            client._post_signed_order.assert_not_awaited()
            assert runtime.store.intent(oid)["terminal"] is True
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_signed_amount_below_one_dollar_never_reaches_post(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "overlap-floor.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            staged(client, maker=990_000)  # A signer that rounds below the $1 floor.
            await submit(client, order)

            client._post_signed_order.assert_not_awaited()
            assert runtime.store.intent(oid)["terminal"] is True
            assert runtime.held_cash() == 0
            kinds = [type(event).__name__ for _, event in runtime.store.events()]
            assert "OrderSubmitted" not in kinds

    asyncio.run(run())
