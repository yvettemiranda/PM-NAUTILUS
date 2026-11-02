"""LIVE buy preparation overlaps its independent checks, yet every gate still decides.

Wallet/approval reads, signing and the account check (open orders plus same-
Condition balances) run concurrently with injected delays. None can POST; the
order reaches the POST boundary only when all three pass and control is current.
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
STAGES = ("wallet", "sign", "account")


def overlapped(client, *, wallet=None, account=True):
    """Each check blocks until all three have started, then sleeps DELAY."""
    started = {stage: Event() for stage in STAGES}
    calls = dict.fromkeys(STAGES, 0)

    def enter(stage):
        calls[stage] += 1
        started[stage].set()
        for other in STAGES:
            assert started[other].wait(3), f"{stage} must overlap {other}"
        time.sleep(DELAY)

    def wallet_check():
        enter("wallet")
        if wallet is not None:
            raise wallet

    def sign(*_args, **_kwargs):
        enter("sign")
        return SimpleNamespace(makerAmount=1_000_000, takerAmount=10_000_000)

    async def account_check(_token):
        await asyncio.to_thread(enter, "account")
        if isinstance(account, BaseException):
            raise account
        return account

    client.wallet = SimpleNamespace(buy_preflight=Mock(side_effect=wallet_check))
    client._http_client.create_market_order = Mock(side_effect=sign)
    client._check_buy_account = AsyncMock(side_effect=account_check)
    client._http_client.post_order = Mock()
    client._post_signed_order = AsyncMock()
    client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-overlap"))
    return calls


async def submit(client, order):
    started = time.perf_counter()
    await client._submit_order(SimpleNamespace(order=order, instrument_id=order.instrument_id))
    await flush()
    return time.perf_counter() - started


def test_all_three_checks_overlap_and_all_must_run_before_post(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "overlap-pass.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            calls = overlapped(client)
            stages = []
            original = client._timed_preparation

            async def timed(order, stage, operation):
                stages.append(stage)
                return await original(order, stage, operation)

            client._timed_preparation = timed
            elapsed = await submit(client, order)

            assert calls == dict.fromkeys(STAGES, 1)
            client._post_signed_order.assert_awaited_once()
            # Serial checks would take 3 * DELAY; overlap keeps it near one.
            assert elapsed < 2 * DELAY
            assert stages == ["wallet_check_ms", "sign_ms", "account_check_ms"]
            assert runtime.store.intent(oid)["outbound_terms"]["side"] == "BUY"
            assert not runtime._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize(
    "wallet,account,paused",
    [
        (ValueError("approval missing"), True, True),
        (None, False, False),
        (None, ConnectionError("open orders unavailable"), True),
        (ValueError("approval missing"), False, True),
    ],
)
def test_any_failed_overlapped_check_blocks_post(tmp_path, wallet, account, paused):
    async def run():
        async with controlled_runtime(tmp_path / "overlap-fail.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            calls = overlapped(client, wallet=wallet, account=account)
            await submit(client, order)

            assert calls == dict.fromkeys(STAGES, 1)
            client._post_signed_order.assert_not_awaited()
            client._http_client.post_order.assert_not_called()
            assert runtime.store.intent(oid)["terminal"] is True
            assert runtime.held_cash() == 0
            if paused:
                assert runtime.status == "PAUSED"
            kinds = [type(event).__name__ for _, event in runtime.store.events()]
            assert "OrderSubmitted" not in kinds
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_control_change_during_overlap_is_seen_after_all_checks(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "overlap-control.sqlite") as runtime:
            oid, order = await prepared_order(runtime)
            client = runtime.client
            calls = overlapped(client)
            seen = []

            def valid(*_args):
                seen.append(1)
                return len(seen) == 1  # Valid at submit, invalid after preparation.

            client.buy_valid = Mock(side_effect=valid)
            await submit(client, order)

            assert calls == dict.fromkeys(STAGES, 1)
            client._post_signed_order.assert_not_awaited()
            assert runtime.store.intent(oid)["terminal"] is True
            assert not runtime._test_queue_errors

    asyncio.run(run())
